# Google and Apple account authentication

P1-02 extends device guests with explicit social sign-in. No existing journal,
reading, conversation, or AI implementation is replaced. Constructors perform
no database or provider I/O. A verified identity is `(provider, issuer, sub)`;
email, display name, and Apple form `user` JSON are never account keys.

## API contract

| Method and path | Contract |
|---|---|
| GET `/v2/auth/providers` | Public `{providers:[{provider,available,reason}]}`. Missing configuration reports `available:false`, `reason:"provider_not_configured"`. |
| POST `/v2/auth/social/{google|apple}/start` | Current owner bearer; JSON `{return_uri,code_challenge,platform,intent?}`. `intent` defaults to `sign_in`; explicit provider linking uses `link`. Returns 201 `{authorization_url,expires_at}`. |
| GET `/v2/auth/social/{provider}/callback` | Provider query callback containing one state and code/error. |
| POST `/v2/auth/social/{provider}/callback` | Apple-compatible bounded `application/x-www-form-urlencoded` callback. Untrusted profile/name data is ignored. |
| POST `/v2/auth/social/exchange` | Initiating guest/member bearer plus JSON `{ticket,code_verifier}`. Returns 201 `{user_id,session_id,access_token,expires_at,previous_user_id,guest_records_preserved:true}`. |
| GET `/v2/auth/account` | Current owner `{user_id,kind:"guest"|"member",providers:[...]}`. |
| GET `/v2/auth/sessions` | Active, unexpired owned sessions `{items:[{id,platform,created_at,expires_at,current}]}`. No token hashes. |
| POST `/v2/auth/logout` | Revoke the current tracked session; empty 204. |
| DELETE `/v2/auth/sessions/{id}` | Revoke an owned session; empty 204; foreign/missing id 404. |
| POST `/v2/auth/logout-all` | Revoke all owned tracked sessions and expire the original legacy session; empty 204. |

The return URI must exactly match an integration-owned allowlist and contain
neither query nor fragment. HTTPS URLs, explicitly allowed loopback HTTP
development URLs, and an explicitly allowed application scheme are supported.
Examples are `https://app.example/auth/complete` and `yeoun://auth/complete`.
The callback sends a 303 to `return_uri#auth_ticket=...&auth_provider=...`.
The ticket is not an application bearer, expires in 120 seconds, requires the
initiator's bearer plus its S256 verifier, and can be exchanged only once.
The client removes the fragment promptly. Bearers are returned only in JSON.

Cancellation and provider failures return the saved allowlisted URI with a
fixed `auth_error=cancelled|authentication_failed|flow_expired`. Provider error
text, authorization codes, names, and tokens are never reflected. Unknown,
replayed, or wrong-provider states cannot create a redirect or a session.
Responses use `Cache-Control:no-store`; redirects also use
`Referrer-Policy:no-referrer`. GET callback query text is removed from the ASGI
scope before responding so access logging cannot retain code/state parameters.

## Preservation and session rules

The first social account uses the initiating guest's existing `app_sessions.id`
as its data owner. No personal records move. A login to an existing account
from another guest issues a new session for the account owner and reports the
previous guest ID. The previous guest's row, token, and records are preserved.
The client must preserve that guest connection and make the switch explicit.
There is no automatic email merge or same-day record conflict resolution.

An existing member can add a previously unlinked provider only through an
explicit `intent:"link"` flow. Linking an identity already owned by someone
else returns 409. Each owner can link at most one identity per provider.

The first link captures the original guest bearer hash as a tracked member
session. It can subsequently be revoked alongside new devices. Authentication
checks the social session table first, including revoked/expired rows; only an
unknown token may fall through to the legacy authenticator. A recognized
revoked or expired token raises `SessionRejected` and must never fall through.
This includes the legacy no-prefix token syntax. New bearers keep the existing
64-hex-character format. Refresh extends a recognized, valid session for 180
days without minting a replacement token.

## Storage and concurrency

Migration `0021_social_accounts.sql` adds accounts, hashed provider-subject
identities, device sessions, and short-lived auth flows. Raw application
bearers, state, completion tickets, and nonce are not stored. The `nonce`
column contains a SHA-256 digest for constant-time ID-token claim comparison.
The server-generated Google PKCE verifier exists only in the private flow row
until callback completion or expiry, then is removed. Provider access/refresh
tokens, email, names, and the ID token are not persisted.

Flow state moves `pending -> processing -> ready -> deleted`. Claiming state
commits before external provider I/O. Callback completion reacquires the same
owner-lifecycle lock and active-owner check. Flow TTL is ten minutes; at most
five unexpired flows per owner are admitted. Start and periodic maintenance
purge expired rows in bounded batches with `SKIP LOCKED`. Completion consumes
the ticket under a short account-admission advisory lock and owner locks.
Provider subject uniqueness is enforced by a database primary key. A common
account-admission lock precedes owner locks to avoid cross-account link cycles.

Privacy callbacks reuse the orchestrator's transaction. Export adds separate
`social_accounts` and `social_sessions` datasets with public metadata only.
Erasure removes flows, identities, accounts, and device sessions. A callback
that returns after erasure cannot restore any of them. Existing personal-data
erasure still rotates the legacy bearer and marks its owner unavailable.

## Provider verification

`OIDCSocialProvider` uses fixed official authorization, token, and JWKS endpoints
with bounded responses, timeouts, and no redirects. PyJWT 2.15.1 validates
RS256 signatures using trusted JWKS, expected issuer and audience, expiry,
issued-at, subject, nonce hash, and authorized-party binding. JWT-specified
key URLs/algorithms cannot select endpoints or enable a symmetric algorithm.
Public keys are cached briefly and refreshed on an unknown key ID.

Google authorization uses S256 PKCE. Apple's discovery does not advertise
PKCE, so its server code exchange uses its client assertion, while the
independent client completion-ticket S256 protection remains mandatory.
Apple authorization requests `response_mode=form_post`; its client assertion
is ES256 with a five-minute lifetime. Generated signing keys are used only by
tests. Runtime credential sources are invoked only by a callback, never by a
constructor, provider-status request, or start request. Configurations must be
omitted until their ID, redirect registration, and runtime credential source
are actually configured; the default registry is empty.

Official sources checked on 2026-11-06:

- [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect)
- [Google current discovery](https://accounts.google.com/.well-known/openid-configuration)
- [Google OIDC claims reference](https://developers.google.com/identity/openid-connect/reference)
- [Apple authorization request](https://developer.apple.com/documentation/signinwithapplerestapi/request-an-authorization-to-the-sign-in-with-apple-server.)
- [Apple token validation](https://developer.apple.com/documentation/signinwithapplerestapi/generate-and-validate-tokens)
- [Apple current discovery](https://appleid.apple.com/.well-known/openid-configuration)
- [Apple public signing-key set](https://appleid.apple.com/auth/keys)
- [PyJWT current changelog](https://pyjwt.readthedocs.io/en/stable/changelog.html)

## Composition seams

```python
provider = OIDCSocialProvider(http_client, configurations={})  # Empty until configured.
auth = SocialAuthService(
    lambda: PostgresSocialUnitOfWork(pool), provider,
    SocialAuthPolicy(tuple(allowed_return_uris)))
app.include_router(social_auth_router(auth, current_session))
data.owner_kind = auth.owner_kind
data.export_callbacks += (auth.export_owner,)
data.erase_callbacks += (auth.erase_owner,)
```

`current_session` calls `auth.authenticate(authorization)` first and falls back
to `identity.authenticate` only for `None`, never for `SessionRejected`.
The existing `/v2/sessions/refresh` handler similarly uses
`auth.refresh(authorization)` before its legacy refresh. Add
`auth.purge_expired()` to existing maintenance. These main/config/bootstrap
composition changes belong to the integration owner.

Real OAuth registration, credentials, actual provider logins, redirect-domain
verification, and store publication were not performed. No AI code or actual
secret/environment file was read or changed.
