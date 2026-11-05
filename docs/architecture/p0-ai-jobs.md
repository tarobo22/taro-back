# P0 AI jobs

`AIJobsService(uow_factory, provider=None, transcription_provider=None, reading_source=None,
owner_guard=None, audio_store=None, policy=None)` constructs without database queries.
`PostgresAIJobsUnitOfWork(pool)` exposes `connection` and `commit()`.
Register `create_ai_jobs_router(service, owner_dependency)` for the v2 routes.

Apply migrations explicitly before starting workers. `run_next(worker_id)` processes one queued job;
`recover()` requeues expired leases only before the provider-start marker, and otherwise fails with
`UNKNOWN_PROVIDER_OUTCOME`. `purge_expired()` removes expired audio/transcripts and orphaned files.
Run recovery and expiry cleanup periodically even when the queue is empty. PostgreSQL leases and
`FOR UPDATE SKIP LOCKED` permit several workers. The external request runs after closing its unit of
work, and completion reacquires `yeoun:owner:{owner}` before checking owner lifecycle, captured consent
events, resource versions and every selected journal ID/version. Use the same owner lock for erasure
and journal/reading mutation. `cancel_for_journal(connection,owner,id)` and
`cancel_for_resource(connection,owner,id)` cancel dependent pending work in the caller's transaction.

`ReadingSource` supplies selected card snapshots and guarded completion; it owns reading storage.
`erase_owner(owner, connection=...)` and `export_owner(connection,owner)` integrate with privacy.
The erasure method also accepts callback arguments `(connection,owner)`. Usage accounting survives
erasure without retaining an owner link. The owner lifecycle callback must reject missing owners and
states other than active; absent privacy profile means active for an existing owner.

For production v1, construct `ExperienceUseCases(..., ai_guard=service.legacy_guard())`. The optional
guard captures the current persisted consent before external I/O and rechecks its exact event and
the active owner under the shared owner lock in the final v1 save transaction. Isolated legacy
callers that omit this hook retain their existing behavior. A revoke/regrant sequence still blocks
the old answer because the exact consent event changed.

All mutating POST routes require UUID `X-Idempotency-Key` (`Idempotency-Key` alias accepted).
Conversation turns additionally deduplicate `client_message_id`. `TurnInput.use_ai=false` is an
additive local-guide mode: no external request, AI consent or usage reservation. For `use_ai=true`,
`consent_event_id` must identify the latest `ai_processing` event under policy `2026-11-05`.
Selected journal IDs (at most three) additionally require current `journal_context` consent.
Only selected journals and up to six earlier ready turns from the same conversation enter the prompt.
No journal body is persisted in jobs, idempotency records, diagnostics or source references.

Accepted requests return 202 with `job_id`, `resource_id`, `state`, `status_url`, `events_url`.
GET job never calls a provider. SSE sends committed status/heartbeat/completed/failed snapshots only.
Validated results appear at `result_url` after the completion transaction. Failed/cancelled jobs can
be explicitly retried into a new billed job retaining the original turn/card selection. Transcription
retry requires a fresh upload because terminal paths delete the original audio.

`PrivateAudioStore(root)` keeps random private filenames outside public routes. Configure a dedicated
server-user directory. Runtime requirements pin `av==19.0.2` and `python-multipart==0.0.22`; PyAV wheels
bundle FFmpeg for Python 3.12+ on supported Windows/Linux/macOS targets. WAV, m4a/mp4, mp3, ogg, webm
and FLAC audio undergo container/MIME checks, exactly-one-audio-stream validation and bounded full
decoding before transmission. Limits are 10 MiB, 60 seconds, two channels, 192 kHz, 100 MiB decoded
frames and a ten-second decoding budget. Declared duration is checked against decoded duration.
Audio files are removed on terminal completion/cancellation and at most one hour after admission;
temporary transcripts expire at the same deadline. Transcription never creates a conversation turn.

`OpenAIResultProvider` and `OpenAITranscriptionProvider` use an injected HTTP client, model and runtime
key callback. Blank configuration returns `AI_UNAVAILABLE` without HTTP. They issue one HTTP request
per attempt; configure injected transports without retries. Responses uses strict Structured Outputs,
`store:false` and a 1500-token output limit. Schema and card ID/position semantics are checked again
locally. Diagnostics contain only model/prompt/schema versions, request/response IDs, usage and timing.
All automated boundary tests use `httpx.MockTransport`, fake credentials and generated silent audio;
no real OpenAI requests, microphone access or credential registration were performed.

Primary references used:

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI Transcription API](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
- [PyAV installation](https://github.com/PyAV-Org/PyAV/blob/master/docs/overview/installation.rst)
- [PyAV decoding containers](https://pyav.basswood.io/docs/stable/api/container.html)
