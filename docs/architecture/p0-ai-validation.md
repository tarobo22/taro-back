# P0 AI validation follow-up

AI result validation rejects empty or whitespace-only empathy, summary, advice, selected-card meaning/reflection, action strings and any present follow-up questions. Existing schema limits, exact card IDs/positions and action count checks remain. Internal safety guidance supplies a nonempty card meaning so a safety response is subject to the same complete-output contract.

The additional output policy matches narrowly specified affirmative claims: explicit certain negative predictions, direct diagnoses of the recipient, and promises of financial returns, cures or guaranteed success. It uses bounded clauses and affirmative predicates, not generic substring checks for words such as failure, diagnosis or guarantee. Ordinary help-seeking, descriptions of uncertainty, conditional support and statements that returns/cures cannot be guaranteed remain valid. Normalization is used only for policy matching; the returned text is preserved. These deterministic patterns are a limited extra guard, not an exhaustive natural-language classifier. Provider instructions and typed refusal handling remain in place.

A blocked output follows the existing `AI_OUTPUT_INVALID` worker failure path: no result URL and no saved assistant answer or partial result. New unit cases cover both rejected claims and ordinary advice that contains similar vocabulary. Real worker tests use injected fake providers and verify persisted failure and absent answer. No external provider or real credential is used.

The existing positive fixture in `test_p0_openai_boundary.py` had an empty summary. With explicit authorization to align a positive fixture to the new stricter output contract, only that summary was replaced by a valid sentence. All original assertions remain; new negative cases prove empty/whitespace summaries are rejected.

The existing operational owner budget remains 20 daily AI attempts, shared with v1; the original P0 design document suggested 30. This follow-up does not raise that limit. Bootstrap/UI limits must describe the configured operational policy, not the unadopted design value.
