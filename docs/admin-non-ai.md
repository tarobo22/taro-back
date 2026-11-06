# Non-AI administration

`P1-03` is an isolated administration boundary. It does not expose AI prompts,
models, or AI usage. Migration `0024_admin.sql` creates no roles, publications, or
OTA overrides. Existing social members receive a `viewer` or `publisher` role
only through an explicit operator command. No real grants or publications were
performed during implementation.

## Composition

All constructors are database free. The application imports neither SQL nor
psycopg. PostgreSQL operations live in `api/adapters/admin_postgres.py`.

```python
factory = lambda: PostgresAdminUnitOfWork(pool)
query_factory = lambda: PostgresAdminUnitOfWork(admin_query_pool)
admin = AdminService(
    factory, social_auth, catalog_template,
    query_uow_factory=query_factory,
    ota_defaults=current_ota_defaults,
    policy=AdminPolicy(audit_retention_days=None),
)
data = DataPrivacyUseCases(
    data_factory, data_policy, catalog_template,
    catalog_source=admin.catalog_source,
    erase_callbacks=(admin.erase_owner, social_auth.erase_owner, ...),
)
app.include_router(admin_router(admin))
```

The composition root opens/closes `admin_query_pool` with the same DSN,
`min_size=0`, `max_size=2`, and dict rows. `catalog_source()` and
`ota_policy_for_bootstrap()` use only this query factory. They must remain usable
when all worker-pool connections are occupied. Query failures propagate; there
is no successful fallback to a stale published catalog or OTA setting. The
bootstrap adapter can call `admin.ota_policy_for_bootstrap` on each request.
Its DTO is exactly `{channel, min_native_version, updates_enabled}`. The admin
DTO additionally includes integer `version`. Register `admin.purge_expired` with
maintenance and `admin.erase_owner(transaction, owner)` before or after the
social erasure callback in the same privacy transaction. Session FKs cascade in
either order. Existing `DataPrivacyUseCases` callers that omit `catalog_source`
keep the template-based catalog behavior and lazy constructor.

## Credentials and roles

`GET /v2/admin/eligibility` and `POST /v2/admin/sessions` accept the normal social
bearer. Eligibility returns `{eligible, role, session_seconds}`. Issue returns
201 with `{access_token, session_id, owner_id, role, expires_at}`. The new token
has an `admin_` prefix and 64 random hex characters, is stored only as a hash,
and expires after 900 seconds or the source session expiry, whichever is earlier.
All other admin endpoints require that separate token. User APIs retain their
existing 64-hex credential format and reject an admin token.

Every admin request rechecks the issued role, current role, active member owner,
source social session revocation/expiry, and admin expiry. It re-reads the admin
session after acquiring the owner lifecycle lock so a concurrent logout cannot
reuse a stale session. Role changes invalidate previous admin sessions. Viewers
can read; publishers can read and write. There is no HTTP role-grant endpoint.
Clients keep admin credentials only in memory and clear them on background,
unmount, owner change, expiry, or authorization rejection.

The explicit operator commands are `python -m tools.admin_role grant --owner UUID
--role viewer` (or `publisher`) and `python -m tools.admin_role revoke --owner UUID`.
They consume runtime-injected `DATABASE_URL`, never load an environment file,
and append an audit entry. Grant requires an existing active social member.
These commands were tested only with generated fixture owners through the same
application role manager; no real operator role was assigned.

## HTTP contract

Successful and authorization-error responses use `Cache-Control: no-store`.
Mutation bodies reject extra fields and require strict nonnegative integer CAS
versions. They do not use idempotency headers. A stale version returns 409,
missing resources 404, invalid input 422, invalid/expired admin sessions 401,
and insufficient write permission 403.

| Method and path under `/v2/admin` | Body / result |
|---|---|
| GET `/me` | owner_id, role, expires_at, audit_retention_days |
| DELETE `/sessions/current` | 204; revokes only this admin session |
| GET `/catalog` | items, publications |
| POST `/catalog/{version_id}/review` | expected_review_version, rights_status approved/rejected, notes 1..1000; new catalog state |
| POST `/catalog/{version_id}/publish` | expected_review_version, expected_publication_version; new state |
| POST `/catalog/{version_id}/rollback` | publication_id, expected_publication_version; new state |
| GET `/feedback` | optional cursor, limit 1..50 (default 20), status; items and next_cursor |
| PATCH `/feedback/{id}/triage` | expected_version, status new/reviewing/resolved/dismissed; feedback item |
| GET `/audit` | optional cursor and limit; items and next_cursor |
| GET `/ota-policy` | channel, min_native_version, updates_enabled, version |
| PUT `/ota-policy` | expected_version plus the three public policy fields; new policy |

Catalog items contain `version_id`, `code`, `name`, `version`, `rights_status`,
`review_version`, `review_note` (always a string), `published`,
`publication_version`, `current_publication_id`, and 79 public assets. Assets
reuse the existing public Asset DTO: `id,url,sha256,width,height,source_page,
license_name,license_url`. Publication entries contain `id,version_id,code,
action,previous_id,created_at`. IDs are UUIDs. Publication CAS is shared by
versions of the same deck code. Reviews and publications are separate actions;
approval never publishes automatically.

Candidate versions remain visible for rights review when source/license
evidence is missing, but cannot be approved or published. Publication requires
78 cards (IDs 0..77), one back asset, 79 unique asset IDs, checksums/dimensions,
and HTTPS source/license references. This records an operator's review; it does
not assert that a remote license has been independently verified. Review notes
are version metadata and are not copied into audit details. Rejecting the
currently published version withdraws it and increments publication CAS;
reapproval does not restore it automatically.

Stored version snapshots and their asset IDs are immutable. Changing source
content under a registered version ID returns `IMMUTABLE_CATALOG_VERSION` 409.
Rollback requires an explicit real previous publication belonging to that
version and a currently approved review. It appends a new publication and audit
record rather than mutating the old snapshot. Public reads retain retired
version cards for existing records. Dynamic catalog cursors bind to the actual
registry snapshot hash, and version-specific card reads use `cards_by_deck`.
Catalog snapshot reads hold a shared governance lock; mutations use the same
key exclusively. A publication cannot change halfway through the multiple
SELECT statements that compose one public/admin catalog response.

Feedback items contain `id,reading_id,turn_id,rating,reason,comment,created_at,
triage_status,triage_version`. Triage is CAS, and feedback erasure cascades to
triage records. Concurrent erasure cannot recreate a deleted feedback row.
Audit records contain `id,action,target_type,target_id,actor_owner_id,details,
created_at`. They record mutations and explicit role/session actions without
copying feedback text or credentials. Owner erasure removes roles/admin
sessions and anonymizes audit owner references and role target IDs in the same
privacy transaction; publication and nonpersonal operation history remain.

## Retention and deployment boundaries

`AdminPolicy.audit_retention_days=None` is the default and disables automatic
audit deletion. An explicitly supplied positive retention value enables purge
by timestamp. A generated 30-day policy is covered by tests; no production
retention period is silently chosen. Expired admin sessions are always purged.
OTA mutations update metadata only. Hosting, release signing, credential
provisioning, actual account grants, and actual candidate publication are
separate operator actions.

The evidence directory is `output/taro-non-ai-20261106/admin` in the parent
workspace. Tests use the new `taro_non_ai_admin_test` database and a fresh random
schema for each admin integration case, removing only that schema at teardown.
Existing tests, fixtures, assertions, provider implementations, and secret files
remain unchanged.
