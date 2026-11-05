# P1 journal data

## Journal summary (FR-JOU-06 / S16)

`JournalSummaryUseCases` uses the repository and unit-of-work ports in
`api/application/journal_summary_ports.py`. Construction acquires no connection.
The PostgreSQL adapter groups the existing v1/v2 `journal_entries` by diary
`day` and mood; it never reads bodies. `JournalV2Policy.public_mood` is the
single existing Korean-label/English-code mapping, so mixed shared rows count
together. `created_at` and the current device timezone do not alter diary days.

`GET /v2/journal-summary` requires ISO dates `from` and `to`. Both endpoints are
inclusive, and the start must not follow the end. No arbitrary maximum span is
added: the public specification permits a date range, and the query aggregates
stored rows rather than allocating a day-by-day array. The screen chooses its
7-day or 30-day range. Dates with entries are unique and ascending. Empty
ranges return zero for every mood and an empty dates array. There are no
scores, diagnoses, job creation, provider calls, or inferred mood records.

Every query acquires the shared owner-lifecycle advisory-lock function and
checks active status on that transaction before querying journal data. Erased
or absent owners are unavailable, and deleting owners cannot query the data.

Composition (in `api/main.py`, maintained by the integration owner):

```python
summary = JournalSummaryUseCases(
    lambda: PostgresJournalSummaryUnitOfWork(pool), journal_v2_policy)
app.include_router(journal_summary_router(summary, current_session))
```

The service needs no privacy callbacks: it persists no additional personal data.

## Favorites (FR-JOU-08 / S28)

`FavoritesUseCases` depends only on the explicit repository and unit-of-work
ports in `api/application/favorites_ports.py`. It acquires no database
connection during construction. `PostgresFavoritesUnitOfWork` supplies those
ports, and every public operation holds the same owner-lifecycle lock and
active-owner guard as the journal, reading, and privacy adapters.

Only owned v2 `p0_readings` can be saved. Migration
`0014_p1_journal_data.sql` adds the `favorites` composite primary key and the
composite owner/source foreign key with `ON DELETE CASCADE`. A separate index
on `p0_readings(owner_id,id)` supports that ownership constraint. No legacy
reading table or reading deletion implementation is changed. A short row lock
also protects source validation and insert against a direct SQL deletion.

`POST /v2/favorites` requires a UUID `X-Idempotency-Key`. The public response
is only `{reading_id, created_at}`. A new key for an already-saved source
returns its existing timestamp without creating a duplicate. Reusing a key
with a changed normalized reading id returns 409. A matching key returns its
original timestamp without writing a row. Request metadata stores only hashed
key, fingerprint, source reference, and original favorite timestamp. Deleting
the reading nulls that reference and makes replay return 404. Removing the
favorite also makes replay return 404 while it is absent; a fresh key can save
an existing owned reading again. The original timestamp remains stable if
that original request is replayed after a later explicit save. Request
tombstones remain until owner erasure, because the public contract specifies
no shorter idempotency period.

`GET /v2/favorites` uses descending `(created_at, reading_id)` keyset paging,
with default 20 and maximum 50. Cursors bind to the owner and collection;
invalid or foreign cursors return 422. `DELETE /v2/favorites/{reading_id}`
returns an empty 204 for a removed favorite, and 404 for missing/foreign rows.

Composition (in `api/main.py`, maintained by the integration owner):

```python
favorites = FavoritesUseCases(lambda: PostgresFavoritesUnitOfWork(pool))
app.include_router(favorites_router(favorites, current_session))
data.export_callbacks += (favorites.export_owner,)
data.erase_callbacks += (favorites.erase_owner,)
```

Both privacy callbacks take `(connection, owner)`. They reuse the caller's
transaction without opening or committing another one and do not require an
active owner during erasure. The export collection is the unique
`v2_favorites` dataset containing only public favorite fields. Erasure removes
favorites and all their request metadata. Source deletion and full erasure
serialize with favorite writes using the shared owner lock; cascading foreign
keys provide the database invariant as well.
