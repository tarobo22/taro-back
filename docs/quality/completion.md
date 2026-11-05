# Remaining core flows

New implementation on `codex/complete-core-flows-20261106`.
Existing tests are preserved. Each feature records a real failing behavior before implementation.

| Feature | RED | GREEN | Scope |
|---|---|---|---|
| Journal create retry identity | Two retries created different records, including concurrent requests | Full backend suite: 32 cases | Atomic owner-scoped receipts, payload conflict, unchanged original acknowledgement |

The journal request field is optional for compatibility; updated clients send it on every create.
Receipts are committed with the record, so a lost response can be retried safely.
Original Git reconstruction evidence remains in `tdd-audit.md`; this file records subsequent feature development.
