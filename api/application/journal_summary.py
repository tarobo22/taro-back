"""Owner-scoped counts, with no AI, jobs, persistence writes, or diagnostics."""
from collections.abc import Callable

from api.application.journal_summary_ports import JournalSummaryUnitOfWork
from api.domain.journal_summary import journal_summary, validate_summary_range
from api.domain.journal_v2 import JournalV2Policy


class JournalSummaryUseCases:
    def __init__(self, uow_factory: Callable[[], JournalSummaryUnitOfWork], policy: JournalV2Policy):
        self.uow_factory = uow_factory
        self.policy = policy

    def summary(self, owner, *, start, end):
        validate_summary_range(start, end)
        with self.uow_factory() as uow:
            uow.summary.lock(owner)
            uow.summary.active(owner)
            return journal_summary(start, end, uow.summary.groups(owner, start, end), self.policy)
