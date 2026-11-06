"""Only explicit operator flags enable real providers; private sources resolve at callback time."""
import os
from api.adapters.oidc_social import OIDCProviderConfig, OIDCSocialProvider, apple_client_secret


def runtime_social_provider(client):
    configurations = {}
    if os.environ.get('SOCIAL_GOOGLE_ENABLED') == 'true':
        client_id, redirect = os.environ.get('SOCIAL_GOOGLE_CLIENT_ID', ''), os.environ.get('SOCIAL_GOOGLE_REDIRECT_URI', '')
        if client_id and redirect:
            configurations['google'] = OIDCProviderConfig(client_id, redirect,
                lambda: os.environ.get('SOCIAL_GOOGLE_CLIENT_SECRET'))
    if os.environ.get('SOCIAL_APPLE_ENABLED') == 'true':
        client_id, redirect = os.environ.get('SOCIAL_APPLE_CLIENT_ID', ''), os.environ.get('SOCIAL_APPLE_REDIRECT_URI', '')
        team_id, key_id = os.environ.get('SOCIAL_APPLE_TEAM_ID', ''), os.environ.get('SOCIAL_APPLE_KEY_ID', '')
        if all((client_id, redirect, team_id, key_id)):
            configurations['apple'] = OIDCProviderConfig(client_id, redirect,
                lambda: apple_client_secret(team_id, key_id, client_id, lambda: os.environ.get('SOCIAL_APPLE_PRIVATE_KEY')))
    return OIDCSocialProvider(client, configurations)


def runtime_return_uris():
    return tuple(uri.strip() for uri in os.environ.get('SOCIAL_RETURN_URIS',
        'http://127.0.0.1:8807/auth/complete,http://127.0.0.1:8827/auth/complete,yeoun://auth/complete').split(',') if uri.strip())
