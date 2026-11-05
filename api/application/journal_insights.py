"""Owner-scoped journal insights and the transaction-bound AI source port."""
from uuid import UUID

from api.domain.ai_jobs import AIError, load_policy, validate_result
from api.domain.journal_insights import insight_input, insight_view


class JournalInsightsService:
    def __init__(self, uow_factory, ai_jobs=None, owner_guard=None, policy=None):
        self._uow, self.ai_jobs, self.owner_guard = uow_factory, ai_jobs, owner_guard
        self.policy = {**load_policy(), **(policy or {})}

    def _repository(self, transaction):
        return self._uow().repository_for(transaction)

    def _owner(self, repository, owner):
        repository.lock_owner(owner)
        if not repository.owner_available(owner, self.owner_guard):
            raise AIError('OWNER_UNAVAILABLE', 401)

    def create(self, owner, key, body):
        body = insight_input(body, self.policy['insight_entries_max'])
        if self.ai_jobs is None:
            raise AIError('AI_UNAVAILABLE', 503)
        return self.ai_jobs.create_insight(owner, key, body)

    def _row(self, repository, owner, insight_id):
        row = repository.get(owner, insight_id)
        if not row:
            raise AIError('NOT_FOUND', 404)
        return row

    def _valid(self, repository, row):
        sources = row['sources']
        if (row['state'] == 'invalidated' or not 1 <= len(sources) <= self.policy['insight_entries_max']
                or row['based_on_count'] != len(sources)):
            return False
        expected = {str(source['entry_id']): source['entry_version'] for source in sources}
        current = repository.current_sources(row['owner_id'], [source['entry_id'] for source in sources])
        if len(current) != len(sources) or any(expected.get(str(source['id'])) != source['version']
                or not str(row['range_start']) <= str(source['day']) <= str(row['range_end']) for source in current):
            return False
        for kind, field in (('ai_processing', 'consent_event_id'), ('journal_context', 'context_consent_id')):
            latest = repository.latest_consent(row['owner_id'], kind)
            if (not latest or not latest['granted'] or latest['policy_version'] != self.policy['policy_version']
                    or str(latest['id']) != str(row[field])):
                return False
        return True

    def _current(self, repository, row):
        if row['state'] != 'invalidated' and not self._valid(repository, row):
            repository.invalidate(row['owner_id'], row['id'])
            return self._row(repository, row['owner_id'], row['id'])
        return row

    def get(self, owner, insight_id):
        with self._uow() as uow:
            self._owner(uow.insights, owner)
            row = self._current(uow.insights, self._row(uow.insights, owner, insight_id))
            result = insight_view(row)
            uow.commit()
            return result

    def create_source(self, transaction, owner, insight_id, body, sources, consent_id, context_consent_id):
        repository = self._repository(transaction)
        self._owner(repository, owner)
        return repository.add({'id': insight_id, 'owner_id': owner, 'range_start': body['range_start'],
            'range_end': body['range_end'], 'based_on_count': len(sources),
            'consent_event_id': consent_id, 'context_consent_id': context_consent_id}, sources)

    def snapshot(self, transaction, owner, insight_id):
        repository = self._repository(transaction)
        self._owner(repository, owner)
        return repository.get(owner, insight_id)

    def validate(self, transaction, owner, insight_id, version):
        repository = self._repository(transaction)
        self._owner(repository, owner)
        row = repository.get(owner, insight_id)
        return bool(row and row['version'] == version and self._valid(repository, row))

    def mark_generating(self, transaction, owner, insight_id, version):
        repository = self._repository(transaction)
        self._owner(repository, owner)
        row = repository.get(owner, insight_id)
        return bool(row and row['version'] == version and self._valid(repository, row)
                    and repository.mark_generating(owner, insight_id, version))

    def mark_failed(self, transaction, owner, insight_id, version):
        repository = self._repository(transaction)
        repository.lock_owner(owner)
        repository.mark_terminal(owner, insight_id, version, 'failed')

    def mark_cancelled(self, transaction, owner, insight_id, version):
        repository = self._repository(transaction)
        repository.lock_owner(owner)
        repository.mark_terminal(owner, insight_id, version, 'cancelled')

    def invalidate(self, transaction, owner, insight_id):
        repository = self._repository(transaction)
        repository.lock_owner(owner)
        repository.invalidate(owner, insight_id)

    def complete(self, transaction, owner, insight_id, version, result):
        repository = self._repository(transaction)
        self._owner(repository, owner)
        row = repository.get(owner, insight_id)
        if not row or row['version'] != version or not self._valid(repository, row):
            return False
        return repository.complete(owner, insight_id, version, validate_result(result, 'insight', []))

    def invalidate_for_journal(self, transaction, owner, entry_id):
        repository = self._repository(transaction)
        repository.lock_owner(owner)
        repository.invalidate_for_journal(owner, entry_id)

    def invalidate_for_consent(self, transaction, owner, kind):
        repository = self._repository(transaction)
        repository.lock_owner(owner)
        repository.invalidate_for_consent(owner, kind)

    def export_owner(self, transaction, owner):
        repository = self._repository(transaction)
        repository.lock_owner(owner)
        return {'journal_insights': [insight_view(self._current(repository, row)) for row in repository.all_owned(owner)]}

    def erase_owner(self, owner, connection=None):
        if connection is not None and not isinstance(owner, (UUID, str)):
            owner, connection = connection, owner
        if connection is None:
            with self._uow() as uow:
                self._erase(uow.insights, owner)
                uow.commit()
            return
        self._erase(self._repository(connection), owner)

    def _erase(self, repository, owner):
        repository.lock_owner(owner)
        if self.ai_jobs:
            for row in repository.all_owned(owner):
                self.ai_jobs.cancel_for_resource(repository.transaction, owner, row['id'])
        repository.erase_owner(owner)
