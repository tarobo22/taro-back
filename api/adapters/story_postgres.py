from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from api.adapters.data_privacy_postgres import lock_owner, require_active_owner


class PostgresStoryRepository:
    def __init__(self, connection): self.connection = connection
    def lock(self, owner): lock_owner(self.connection, owner)
    def active(self, owner): require_active_owner(self.connection, owner)
    def request(self, owner, key_hash):
        return self.connection.execute('SELECT fingerprint,result_run_id AS run_id FROM story_requests WHERE owner_id=%s AND key_hash=%s', (owner, key_hash)).fetchone()
    def remember(self, owner, key_hash, fingerprint, run_id):
        self.connection.execute('INSERT INTO story_requests(owner_id,key_hash,fingerprint,result_run_id) VALUES(%s,%s,%s,%s)', (owner, key_hash, fingerprint, run_id))
    def get(self, owner, run_id):
        return self.connection.execute('SELECT * FROM story_runs WHERE owner_id=%s AND id=%s', (owner, run_id)).fetchone()
    def latest(self, owner, episode_id):
        return self.connection.execute('SELECT * FROM story_runs WHERE owner_id=%s AND episode_id=%s ORDER BY run_sequence DESC LIMIT 1', (owner, episode_id)).fetchone()
    def create(self, owner, run_id, values):
        return self.connection.execute('''INSERT INTO story_runs(id,owner_id,episode_id,episode_version,state,current_node_id,ending_id,history,created_at,updated_at,completed_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (run_id, owner, values['episode_id'], values['episode_version'], values['state'], values['current_node_id'], values['ending_id'], Jsonb(values['history']), values['created_at'], values['updated_at'], values['completed_at'])).fetchone()
    def save(self, owner, run_id, expected, values):
        if not values or set(values) - {'state', 'current_node_id', 'ending_id', 'history', 'updated_at', 'completed_at'}: raise ValueError('invalid_story_update')
        assignments = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in values)
        params = [Jsonb(value) if key == 'history' else value for key, value in values.items()]
        return self.connection.execute(sql.SQL('UPDATE story_runs SET {},version=version+1 WHERE owner_id=%s AND id=%s AND version=%s RETURNING *').format(assignments), (*params, owner, run_id, expected)).fetchone()
    def all_owned(self, owner):
        return self.connection.execute('SELECT * FROM story_runs WHERE owner_id=%s ORDER BY run_sequence', (owner,)).fetchall()
    def erase_owner(self, owner):
        self.connection.execute('DELETE FROM story_requests WHERE owner_id=%s', (owner,))
        self.connection.execute('DELETE FROM story_runs WHERE owner_id=%s', (owner,))


class PostgresStoryUnitOfWork:
    def __init__(self, pool): self.pool = pool
    def repository_for(self, transaction): return PostgresStoryRepository(transaction)
    def __enter__(self):
        self.connection = self.pool.getconn(); self.connection.row_factory = dict_row
        self.story = self.repository_for(self.connection)
        return self
    def commit(self): self.connection.commit()
    def __exit__(self, *exc):
        try: self.connection.rollback()
        finally: self.pool.putconn(self.connection)
