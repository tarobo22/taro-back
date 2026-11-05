# P0 server readings

Construct `ReadingV2Service(uow_factory, catalog, owner_guard=owner_guard, ai_jobs=None,
clock=None, random_sample=None)` with `PostgresReadingV2UnitOfWork(pool)` and the published
`DataPrivacyUseCases` catalogue. Register `create_reading_v2_router(service, owner_dependency)`.
Construction does not query the database. Apply `0012_p0_readings.sql` explicitly.

The v2 API supports `daily` and `three_card`. The existing v1 five-card API remains independent.
For three cards, the server samples twelve distinct card IDs with `random.SystemRandom` and assigns
fresh opaque UUID slots. Draft responses expose only slot IDs and indices. Shuffle replaces all
slots. Selection accepts exactly three distinct current slot IDs and stores their supplied order
at positions 1 지금의 마음, 2 주변의 흐름, 3 다음 한 걸음. Both operations compare the required
`expected_version` under the shared owner lock. The final selected cards carry the versioned
catalogue card metadata and reviewed basic meaning; P0 orientation is upright.

Daily draws use the server clock converted through the owner's current IANA profile timezone.
The local date and timezone are captured on the reading. An independent owner/date/topic anchor
retains the card and deck when the visible reading is deleted, until the captured local day ends.
Subsequent requests for the same daily draw reuse the visible reading, or recreate it with that
same card after deletion. Profile timezone changes do not rewrite old readings. Call
`purge_expired()` periodically to remove expired anchors and 24-hour command references.

Idempotency receipts store only a body fingerprint and reading reference. They never copy a
question, hidden card pool, response or AI result. Matching replays return the current referenced
resource; a deleted reference returns 410. All reads and writes are owner-scoped and check active
owner state. The RWS candidate deck is explicitly allowed for the prototype; unknown and retired
decks are rejected. `allow_candidate=False` can require published catalogue entries.

The service implements `ReadingSource.snapshot/validate/mark_generating/mark_failed/complete`
using the caller's connection and shared owner lock. AI completion validates the exact card IDs
and positions and commits the result with a version increment. `ai_jobs` can be wired after
constructing both services (`readings.ai_jobs=jobs`) to cancel dependent work on deletion.
`source_exists(connection,owner,id)` supports owned journal/feedback links. Privacy integration
uses `export_owner(connection,owner)` under the noncolliding `v2_readings` collection name and
`erase_owner(owner,connection=...)` (also accepts callback `(connection,owner)`).
