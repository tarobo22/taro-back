from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from api.application.story_ports import StoryUnitOfWork
from api.domain.story import StoryCatalog, StoryConflict, StoryInvalid, StoryNotFound, positive_version, run_view, story_uuid, strict_body


class StoryService:
    def __init__(self, uow_factory: Callable[[], StoryUnitOfWork], catalog: StoryCatalog, *, clock=None, new_id=None):
        self.uow_factory, self.catalog = uow_factory, catalog
        self.clock, self.new_id = clock or (lambda: datetime.now(timezone.utc)), new_id or uuid4

    @staticmethod
    def _owner(repository, owner): repository.lock(owner); repository.active(owner)

    @staticmethod
    def _identity(key, operation, target, values):
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        return digest(str(story_uuid(key))), digest(json.dumps({'operation': operation, 'target': str(target), 'values': values}, sort_keys=True, separators=(',', ':')))

    def _replay(self, repository, owner, key_hash, fingerprint):
        previous = repository.request(owner, key_hash)
        if previous is None: return None
        if previous['fingerprint'] != fingerprint: raise StoryConflict('idempotency_conflict')
        row = repository.get(owner, previous['run_id'])
        if row is None: raise StoryNotFound('run_not_found')
        return run_view(row, self.catalog)

    def _new(self, repository, owner, episode):
        now = self.clock()
        return repository.create(owner, self.new_id(), {'episode_id': episode['id'], 'episode_version': episode['version'],
            'state': 'active', 'current_node_id': episode['start_node'], 'ending_id': None, 'history': [],
            'created_at': now, 'updated_at': now, 'completed_at': None})

    def episodes(self, owner):
        with self.uow_factory() as uow:
            self._owner(uow.story, owner)
            return {'items': self.catalog.summaries()}

    def progress(self, owner, episode_id):
        self.catalog.episode(episode_id)
        with self.uow_factory() as uow:
            self._owner(uow.story, owner); row = uow.story.latest(owner, episode_id)
            return {'run': run_view(row, self.catalog) if row else None}

    def get(self, owner, run_id):
        run_id = story_uuid(run_id)
        with self.uow_factory() as uow:
            self._owner(uow.story, owner); row = uow.story.get(owner, run_id)
            if row is None: raise StoryNotFound('run_not_found')
            return run_view(row, self.catalog)

    def start(self, owner, values, key):
        strict_body(values, ('episode_id', 'episode_version'))
        if not isinstance(values['episode_id'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', values['episode_id']): raise StoryInvalid('invalid_episode')
        if not isinstance(values['episode_version'], str) or not re.fullmatch(r'\d+\.\d+\.\d+', values['episode_version']): raise StoryInvalid('invalid_episode_version')
        episode = self.catalog.episode(values['episode_id'], values['episode_version'])
        key_hash, fingerprint = self._identity(key, 'start', episode['id'], values)
        with self.uow_factory() as uow:
            repository = uow.story; self._owner(repository, owner)
            replay = self._replay(repository, owner, key_hash, fingerprint)
            if replay: return replay
            row = repository.latest(owner, episode['id']) or self._new(repository, owner, episode)
            repository.remember(owner, key_hash, fingerprint, row['id']); uow.commit()
            return run_view(row, self.catalog)

    def choose(self, owner, run_id, values, key):
        strict_body(values, ('expected_version', 'node_id', 'answer'))
        expected = positive_version(values['expected_version']); run_id = story_uuid(run_id)
        if type(values['answer']) is not bool or not isinstance(values['node_id'], str): raise StoryInvalid('invalid_answer')
        key_hash, fingerprint = self._identity(key, 'choice', run_id, values)
        with self.uow_factory() as uow:
            repository = uow.story; self._owner(repository, owner)
            replay = self._replay(repository, owner, key_hash, fingerprint)
            if replay: return replay
            row = repository.get(owner, run_id)
            if row is None: raise StoryNotFound('run_not_found')
            if row['version'] != expected or row['state'] != 'active' or row['current_node_id'] != values['node_id']: raise StoryConflict('stale_story')
            episode = self.catalog.episode(row['episode_id'], row['episode_version']); node = episode['nodes'][row['current_node_id']]
            next_node = episode['nodes'][node['yes'] if values['answer'] else node['no']]; completed = next_node['kind'] == 'ending'; now = self.clock()
            saved = repository.save(owner, run_id, expected, {'state': 'completed' if completed else 'active',
                'current_node_id': None if completed else next_node['id'], 'ending_id': next_node['id'] if completed else None,
                'history': [*row['history'], {'node_id': node['id'], 'answer': values['answer']}],
                'updated_at': now, 'completed_at': now if completed else None})
            if saved is None: raise StoryConflict('stale_story')
            repository.remember(owner, key_hash, fingerprint, saved['id']); uow.commit()
            return run_view(saved, self.catalog)

    def restart(self, owner, run_id, values, key):
        strict_body(values, ('expected_version',)); expected = positive_version(values['expected_version']); run_id = story_uuid(run_id)
        key_hash, fingerprint = self._identity(key, 'restart', run_id, values)
        with self.uow_factory() as uow:
            repository = uow.story; self._owner(repository, owner)
            replay = self._replay(repository, owner, key_hash, fingerprint)
            if replay: return replay
            row = repository.get(owner, run_id)
            if row is None: raise StoryNotFound('run_not_found')
            latest = repository.latest(owner, row['episode_id'])
            if row['version'] != expected or row['state'] == 'restarted' or latest is None or latest['id'] != row['id']: raise StoryConflict('stale_story')
            changes = {'updated_at': self.clock()}
            if row['state'] == 'active': changes.update(state='restarted', current_node_id=None, ending_id=None, completed_at=self.clock())
            if repository.save(owner, run_id, expected, changes) is None: raise StoryConflict('stale_story')
            fresh = self._new(repository, owner, self.catalog.episode(row['episode_id'], row['episode_version']))
            repository.remember(owner, key_hash, fingerprint, fresh['id']); uow.commit()
            return run_view(fresh, self.catalog)

    def export_owner(self, connection, owner):
        repository = self.uow_factory().repository_for(connection)
        return {'story_runs': [{**run_view(row, self.catalog), 'history': row['history']} for row in repository.all_owned(owner)]}

    def erase_owner(self, connection, owner): self.uow_factory().repository_for(connection).erase_owner(owner)
