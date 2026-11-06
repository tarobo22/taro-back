"""Social identity rules; provider tokens and names never become account keys."""
import base64
from dataclasses import dataclass
import hashlib
import re
from urllib.parse import urlsplit


PROVIDERS = ('google', 'apple')


class SocialAuthError(ValueError):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def pkce_challenge(verifier):
    if not isinstance(verifier, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
        raise SocialAuthError('PKCE_INVALID')
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')


def valid_opaque(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', value):
        raise SocialAuthError('AUTH_FLOW_INVALID', 400)
    return value


@dataclass(frozen=True)
class VerifiedSocialIdentity:
    provider: str
    subject_hash: str


@dataclass(frozen=True)
class SocialAuthPolicy:
    return_uris: tuple[str, ...] = ()
    flow_seconds: int = 600
    ticket_seconds: int = 120
    session_days: int = 180
    max_flows_per_owner: int = 5

    def validate_start(self, provider, return_uri, challenge, platform, intent):
        if provider not in PROVIDERS or platform not in ('web', 'android', 'ios') or intent not in ('sign_in', 'link'):
            raise SocialAuthError('AUTH_INPUT_INVALID')
        if return_uri not in self.return_uris:
            raise SocialAuthError('RETURN_URI_INVALID')
        parts = urlsplit(return_uri)
        if (parts.query or parts.fragment or parts.username or parts.password or
                parts.scheme in ('javascript', 'data', 'file', '') or
                parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', 'localhost') or
                parts.scheme == 'https' and not parts.hostname):
            raise SocialAuthError('RETURN_URI_INVALID')
        if not isinstance(challenge, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge):
            raise SocialAuthError('PKCE_INVALID')
