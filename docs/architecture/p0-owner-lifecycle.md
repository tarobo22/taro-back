# Legacy owner-lifecycle port boundary

The application `guard_owner(uow, owner_id, callback)` helper delegates to a UoW port and contains no SQL or connection access. With no callback it returns immediately without requiring any new capability from existing fake units of work. Existing journal/experience use-case logic and deletion repository methods are unchanged.

`JournalUnitOfWork` and `ExperienceUnitOfWork` declare `guard_owner(owner_id, callback)`. Their PostgreSQL adapters use the shared `api.adapters.owner_lifecycle` implementation to acquire the existing `yeoun:owner:{owner_id}` transaction advisory lock and then invoke the callback on that exact connection. The lock remains until the caller's UoW ends. True/None continue to mean active; False raises `InactiveOwnerError`; callback exceptions propagate unchanged. The adapters also treat a None callback as a no-op without acquiring or accessing a connection.

`InactiveOwnerError` is defined in the pure identity domain and re-exported by the journal application module, preserving all existing import paths and exception handling. No API/schema, constructor signature, use-case behavior, existing test or root composition file changes are required.

New fake-guard unit cases prove delegation without a connection, the None no-op, lock-before-check ordering, transaction identity, inactive exception compatibility, callback exception propagation and both adapter UoW methods. Existing database lifecycle/late-provider/deletion tests verify the unchanged transaction behavior.
