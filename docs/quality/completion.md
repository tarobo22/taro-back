# Remaining core flows

New implementation on `codex/complete-core-flows-20261106`.
Existing tests are preserved. Each feature records a real failing behavior before implementation.

| Feature | RED | GREEN | Scope |
|---|---|---|---|
| Journal create retry identity | Two retries created different records, including concurrent requests | Full backend suite: 32 cases | Atomic owner-scoped receipts, payload conflict, unchanged original acknowledgement |
| Journal edit conflict | Concurrent stale edits both succeeded | Full backend suite: 34 cases | Versioned responses and atomic compare-and-update; stale editors receive 409 |
| AI reservation and lease fencing | Provider held a DB transaction; failed calls did not consume quota; no global cap or lease | Full backend suite: 38 cases | Reserve/commit, provider outside UoW, fenced completion, owner/global UTC-day attempt caps including failed calls |

AI pending duplicates return 409; exhausted limits return 429. Defaults live in shared/experience-policy.json.
An expired worker may finish its provider call, but cannot overwrite a newer committed result.
An unknown provider outcome still consumes a reservation. This conservatively bounds calls; it is not a currency-denominated billing reconciliation system.

The journal request field is optional for compatibility; updated clients send it on every create.
Receipts are committed with the record, so a lost response can be retried safely.
Original Git reconstruction evidence remains in `tdd-audit.md`; this file records subsequent feature development.
