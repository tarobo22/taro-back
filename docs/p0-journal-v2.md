# P0 journal v2 integration

The new `/v2/journal-entries` router reads and writes the existing `journal_entries` table. Existing v1 code and test files are unchanged. Each v2 English mood code maps to the corresponding existing Korean label from `shared/content.json`, in the established order `calm`, `happy`, `thinking`, `settled`, `talk`. Responses translate back to codes. Both versions see the same body, dates, timestamps and CAS version, so a v1 edit invalidates a stale v2 editor and vice versa.

Construct the policy with `JournalV2Policy.from_legacy(content['moods'], journal_rules, extra_rules)`, where the rules come from `shared/journal-policy.json` and `shared/journal-v2-policy.json`. The max body length, idempotency period, default page size and maximum page size are policy-driven. HTTP body schemas derive their `maxLength` from the configured policy. Constructors and OpenAPI generation do not acquire a database connection.

Construct `JournalV2UseCases(lambda: PostgresJournalV2UnitOfWork(pool), policy, *, source_exists=None, invalidate_context=None, clock=None)` and include `journal_v2_router(service, owner_dependency)`. Both live in their `journal_v2`-named modules in application/adapters/http/adapters. Protected operations acquire the shared `yeoun:owner:{owner}` advisory lock and reject deleting/erased owners.

The service methods used by the router are:

- `create(owner, values, request_key) -> Journal`, with specification `JournalInput` fields and a parsed UUID request key. HTTP requires `X-Idempotency-Key`; the shared compatibility alias and conflict validation also apply.
- `get(owner, entry_id) -> Journal`.
- `update(owner, entry_id, values) -> Journal`, with required positive integer `expected_version` plus at least one of `mood` and `body`. Missing, boolean or stale versions never overwrite the current entry. Updates increment the existing CAS version. Local dates and source links cannot be changed by PATCH.
- `list(owner, *, cursor=None, limit=None, start=None, end=None, mood=None) -> JournalList`. HTTP maps `from` and `to` to `start` and `end` as inclusive dates. The default limit is 20, range 1..50. Results use stable descending `(created_at,id)` keyset pagination. The opaque cursor binds the owner, date range and mood; a changed filter/owner or invalid cursor returns 422. Newer inserts do not shift subsequent pages.
- `delete(owner, entry_id) -> bool`. HTTP returns empty 204 for a successful deletion or an exact own prior deletion. Missing and other-owner IDs return 404. An owner-scoped deletion tombstone permits idempotence without revealing another owner's record.

`source_exists(connection, owner, kind, source_id) -> bool` validates `reading` or `turn` ownership in the same transaction. A source link fails closed without this callback. Reading and turn sources are mutually exclusive. The metadata table deliberately has no SQL dependency on the separate reading/AI modules.

`invalidate_context(connection, owner, entry_id) -> None` runs before deleting an existing entry, under the owner lock and in the deletion transaction. The AI context must cancel pending work and clear snapshots. The callback must use the supplied connection and must not commit. If it fails, the diary and metadata remain intact. Completed AI results remain separate resources.

Migration `0013_p0_journal.sql` adds `journal_v2_sources` (FK to existing entries with cascade), `journal_v2_create_requests` and `journal_v2_deleted_entries`, plus the pagination index. Creation idempotency stores a fingerprint and live entry reference, never a diary body/result snapshot. A retry returns the current referenced resource. A deleted or expired reference returns 410 and cannot recreate the diary. `purge_expired_idempotency()` clears expired fingerprints and entry references after 24 hours; only the spent request-key digest and minimal timing remain until owner erasure, preventing old retries from resurrecting data.

Privacy composition must include `service.export_owner(connection, owner)` and `service.erase_owner(connection, owner)`. Export adds the safe `v2_journal_entries` collection with source metadata and mood codes, alongside the base legacy export. Erasure clears the source, idempotency and deletion metadata using the supplied transaction; it intentionally works while the profile is already deleting and never commits. The base privacy eraser subsequently deletes the underlying journal rows. This is necessary because the approved privacy flow retains an anonymous session/usage tombstone rather than deleting `app_sessions`.

New tests exercise both API versions over shared rows, real CAS races, owned source links, strict payload/version validation, body-free request metadata, deleted/expired-key non-resurrection, callback rollback, full erasure metadata cleanup, stable pages during insertion, date/mood filters and database-free OpenAPI generation. Existing tests were not modified for this feature.
