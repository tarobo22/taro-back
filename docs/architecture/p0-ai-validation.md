# P0 AI validation follow-up

AI result validation rejects empty or whitespace-only empathy, summary, advice, selected-card meaning/reflection, action strings and any present follow-up questions. Existing schema limits, exact card IDs/positions and action count checks remain. Internal safety guidance supplies a nonempty card meaning so a safety response is subject to the same complete-output contract.

The additional output policy matches narrowly specified affirmative claims: explicit certain negative predictions, direct diagnoses of the recipient, and promises of financial returns, cures or guaranteed success. It uses bounded clauses and affirmative predicates, not generic substring checks for words such as failure, diagnosis or guarantee. Ordinary help-seeking, descriptions of uncertainty, conditional support and statements that returns/cures cannot be guaranteed remain valid. Normalization is used only for policy matching; the returned text is preserved. These deterministic patterns are a limited extra guard, not an exhaustive natural-language classifier. Provider instructions and typed refusal handling remain in place.

A blocked output follows the existing `AI_OUTPUT_INVALID` worker failure path: no result URL and no saved assistant answer or partial result. New unit cases cover both rejected claims and ordinary advice that contains similar vocabulary. Real worker tests use injected fake providers and verify persisted failure and absent answer. No external provider or real credential is used.

The existing positive fixture in `test_p0_openai_boundary.py` had an empty summary. With explicit authorization to align a positive fixture to the new stricter output contract, only that summary was replaced by a valid sentence. All original assertions remain; new negative cases prove empty/whitespace summaries are rejected.

The existing operational owner budget remains 20 daily AI attempts, shared with v1; the original P0 design document suggested 30. This follow-up does not raise that limit. Bootstrap/UI limits must describe the configured operational policy, not the unadopted design value.

## Caller-observed journal versions

Turn and interpretation inputs accept optional `context_entry_versions`, mapping each selected journal UUID to the positive integer version that the caller actually reviewed. Example: `context_entry_ids: [entry_id]` together with `context_entry_versions: {entry_id: 3}`. The supplied mapping's canonical UUID keys must match the selected ID set exactly; missing/extra keys and non-integer/boolean/non-positive values are rejected with 422. An explicit null mapping is invalid; omit the field for the prior contract. Empty IDs with an empty mapping are valid.

Every referenced journal still requires owner verification. A foreign/missing journal remains 404. A selected version that differs from the current database version returns 409 `RESOURCE_CHANGED` before provider availability, daily/concurrent quota reservation, job insertion or turn insertion. Valid requests capture the same actual ID/version metadata already used by the worker. No diary body is added to jobs, idempotency records or diagnostic metadata.

The frontend must keep each reviewed entry's version alongside its ID and send that snapshot on the actual AI request. On `RESOURCE_CHANGED`, retain the user's draft, show the current journal and obtain a fresh selection rather than silently replacing the reviewed version.

Omitting this additive field retains prior admission behavior and the exact prior idempotency fingerprint shape. HTTP serialization removes the new default when the client did not supply it. Authenticated replays remain reads of the original accepted operation. Existing worker and explicit-retry version checks are retained; a changed captured source still requires a fresh confirmed request rather than rebilling a retry with edited text.

New integration tests cover chat and tarot admission, strict maps and ownership, zero reservations/results for stale selections, legacy fingerprint/replay compatibility, and the existing retry rejection after a source changes. All providers are injected fakes.
