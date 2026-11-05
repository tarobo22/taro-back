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
