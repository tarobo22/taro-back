"""Google/Apple code exchange and signed ID-token validation at trusted endpoints."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hmac
import re
from threading import Lock
import time
from typing import Callable
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from api.domain.social_auth import PROVIDERS, SocialAuthError, VerifiedSocialIdentity, digest, pkce_challenge


ENDPOINTS = {
    'google': {'authorize': 'https://accounts.google.com/o/oauth2/v2/auth',
               'token': 'https://oauth2.googleapis.com/token', 'keys': 'https://www.googleapis.com/oauth2/v3/certs',
               'issuers': ('https://accounts.google.com', 'accounts.google.com')},
    'apple': {'authorize': 'https://appleid.apple.com/auth/authorize',
              'token': 'https://appleid.apple.com/auth/token', 'keys': 'https://appleid.apple.com/auth/keys',
              'issuers': ('https://appleid.apple.com',)},
}


@dataclass(frozen=True)
class OIDCProviderConfig:
    client_id: str
    redirect_uri: str
    client_secret: Callable[[], str | None] = field(repr=False)


class OIDCSocialProvider:
    def __init__(self, client: httpx.Client, configurations=None):
        self.client = client
        self.configurations = dict(configurations or {})
        self._keys, self._lock = {}, Lock()

    def available(self, provider):
        config = self.configurations.get(provider)
        if provider not in PROVIDERS or config is None or not config.client_id or not callable(config.client_secret):
            return False
        parts = urlsplit(config.redirect_uri)
        return bool(parts.scheme == 'https' and parts.hostname and not parts.query and not parts.fragment
                    and not parts.username and not parts.password)

    def _config(self, provider):
        if not self.available(provider):
            raise SocialAuthError('PROVIDER_UNAVAILABLE', 503)
        return self.configurations[provider]

    def authorization_url(self, provider, *, state, nonce, verifier):
        config = self._config(provider)
        values = {'client_id': config.client_id, 'redirect_uri': config.redirect_uri,
                  'response_type': 'code', 'scope': 'openid email' if provider == 'google' else 'email',
                  'state': state, 'nonce': nonce}
        if provider == 'google':
            values.update(code_challenge=pkce_challenge(verifier), code_challenge_method='S256', prompt='select_account')
        else:
            # Apple's published discovery does not advertise PKCE. Completion
            # tickets still require the independent client S256 verifier.
            values['response_mode'] = 'form_post'
        return ENDPOINTS[provider]['authorize'] + '?' + urlencode(values)

    def _jwk(self, provider, kid):
        with self._lock:
            cached = self._keys.get(provider)
            if cached is not None and cached[0] > time.monotonic():
                candidate = next((key for key in cached[1] if key.get('kid') == kid), None)
                if candidate is not None:
                    return candidate
            response = self.client.get(ENDPOINTS[provider]['keys'], timeout=10, follow_redirects=False)
            response.raise_for_status()
            if len(response.content) > 65536:
                raise SocialAuthError('PROVIDER_IDENTITY_INVALID', 401)
            keys = response.json().get('keys')
            if not isinstance(keys, list) or not 1 <= len(keys) <= 20 or not all(isinstance(key, dict) for key in keys):
                raise SocialAuthError('PROVIDER_IDENTITY_INVALID', 401)
            self._keys[provider] = (time.monotonic() + 300, keys)
            candidate = next((key for key in keys if key.get('kid') == kid), None)
            if candidate is None:
                raise SocialAuthError('PROVIDER_IDENTITY_INVALID', 401)
            return candidate

    def verify(self, provider, encoded, nonce_hash):
        config = self._config(provider)
        try:
            if not isinstance(encoded, str) or len(encoded) > 32768:
                raise ValueError
            header = jwt.get_unverified_header(encoded)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str) or not 1 <= len(header['kid']) <= 128:
                raise ValueError
            jwk = self._jwk(provider, header['kid'])
            if jwk.get('kty') != 'RSA' or jwk.get('use', 'sig') != 'sig' or jwk.get('alg', 'RS256') != 'RS256':
                raise ValueError
            key = jwt.PyJWK.from_dict(jwk, algorithm='RS256')
            claims = jwt.decode(encoded, key, algorithms=['RS256'], audience=config.client_id,
                issuer=ENDPOINTS[provider]['issuers'], leeway=0,
                options={'require': ['iss', 'aud', 'sub', 'iat', 'exp', 'nonce'], 'enforce_minimum_key_length': True})
            if (not isinstance(claims['nonce'], str) or not hmac.compare_digest(digest(claims['nonce']), nonce_hash)
                    or type(claims['iat']) is not int or type(claims['exp']) is not int or claims['iat'] >= claims['exp']
                    or claims.get('azp', config.client_id) != config.client_id
                    or isinstance(claims['aud'], list) and len(claims['aud']) > 1 and claims.get('azp') != config.client_id
                    or not isinstance(claims['sub'], str) or not re.fullmatch(r'[\x21-\x7e]{1,255}', claims['sub'])):
                raise ValueError
            # The issuer is canonicalized for Google's documented legacy alias.
            return VerifiedSocialIdentity(provider, digest(ENDPOINTS[provider]['issuers'][0] + '\0' + claims['sub']))
        except SocialAuthError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
            raise SocialAuthError('PROVIDER_IDENTITY_INVALID', 401) from None
        except httpx.HTTPError:
            raise SocialAuthError('PROVIDER_UNAVAILABLE', 503) from None

    def exchange(self, provider, code, *, nonce_hash, verifier):
        config = self._config(provider)
        # The source is invoked only by an authorized callback, never at
        # construction/status/start, and its value never enters URLs or logs.
        try:
            secret = config.client_secret()
        except Exception:
            raise SocialAuthError('PROVIDER_UNAVAILABLE', 503) from None
        if not secret:
            raise SocialAuthError('PROVIDER_UNAVAILABLE', 503)
        values = {'client_id': config.client_id, 'client_secret': secret, 'code': code,
                  'redirect_uri': config.redirect_uri, 'grant_type': 'authorization_code'}
        if provider == 'google':
            values['code_verifier'] = verifier
        try:
            response = self.client.post(ENDPOINTS[provider]['token'], data=values, timeout=15, follow_redirects=False)
            response.raise_for_status()
            if len(response.content) > 65536:
                raise ValueError
            identity = response.json().get('id_token')
            return self.verify(provider, identity, nonce_hash)
        except SocialAuthError:
            raise
        except (httpx.HTTPError, ValueError, AttributeError):
            raise SocialAuthError('PROVIDER_AUTHENTICATION_FAILED', 401) from None


def apple_client_secret(team_id, key_id, client_id, private_key_source, *, clock=None):
    """Create Apple's short-lived ES256 client assertion from a runtime-only key."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    key = private_key_source()
    if not key or not all(isinstance(value, str) and value for value in (team_id, key_id, client_id)):
        raise SocialAuthError('PROVIDER_UNAVAILABLE', 503)
    try:
        return jwt.encode({'iss': team_id, 'sub': client_id, 'aud': 'https://appleid.apple.com',
            'iat': int(now.timestamp()), 'exp': int((now + timedelta(minutes=5)).timestamp())},
            key, algorithm='ES256', headers={'kid': key_id})
    except (jwt.PyJWTError, ValueError, TypeError):
        raise SocialAuthError('PROVIDER_UNAVAILABLE', 503) from None
