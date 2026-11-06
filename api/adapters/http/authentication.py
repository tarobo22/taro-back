"""Declare security schemes while preserving the existing opaque-token policy."""
import re

from fastapi import Request
from fastapi.security import APIKeyHeader, HTTPBearer


class OpaqueBearer(HTTPBearer):
    async def __call__(self, request: Request) -> str | None:
        # Domain/application authentication owns expiry, revocation and error codes.
        # Return the original value, including legacy bare-hex compatibility.
        return request.headers.get('authorization')


session_authorization = OpaqueBearer(auto_error=False, scheme_name='SessionBearer',
    bearerFormat='opaque', description='Guest or linked-account session. Not an admin session.')
admin_authorization = OpaqueBearer(auto_error=False, scheme_name='AdminBearer',
    bearerFormat='opaque', description='Short-lived administrator session issued to an explicitly assigned role.')
data_receipt = APIKeyHeader(name='X-Data-Receipt', auto_error=False, scheme_name='DataReceipt',
    description='A single data-request receipt; alternative to a session only for that request status.')


def has_session_token_shape(authorization: str | None) -> bool:
    """Reject missing/malformed input before acquiring the authentication DB pool."""
    return bool(re.fullmatch(r'[0-9a-f]{64}', (authorization or '').removeprefix('Bearer ')))
