from psycopg.rows import dict_row

from api.adapters.data_privacy_postgres import lock_owner, require_active_owner
from api.application.favorites_ports import FavoriteRecord, FavoriteRequest


def favorite_record(row):
    return FavoriteRecord(row['reading_id'], row['created_at']) if row else None


class PostgresFavoritesRepository:
    def __init__(self, connection):
        self.connection = connection

    def lock(self, owner):
        lock_owner(self.connection, owner)

    def active(self, owner):
        require_active_owner(self.connection, owner)

    def has_reading(self, owner, reading_id):
        # Also protects the source between validation and FK insertion against raw SQL deletion.
        return bool(self.connection.execute('SELECT 1 FROM p0_readings WHERE owner_id=%s AND id=%s FOR KEY SHARE',
                                            (owner, reading_id)).fetchone())

    def request(self, owner, key_hash):
        row = self.connection.execute('''SELECT fingerprint,reading_id,favorite_created_at
            FROM p1_favorite_create_requests WHERE owner_id=%s AND key_hash=%s''', (owner, key_hash)).fetchone()
        return FavoriteRequest(row['fingerprint'], row['reading_id'], row['favorite_created_at']) if row else None

    def remember(self, owner, key_hash, fingerprint, favorite):
        self.connection.execute('''INSERT INTO p1_favorite_create_requests
            (owner_id,key_hash,fingerprint,reading_id,favorite_created_at) VALUES(%s,%s,%s,%s,%s)''',
            (owner, key_hash, fingerprint, favorite.reading_id, favorite.created_at))

    def get(self, owner, reading_id):
        return favorite_record(self.connection.execute('SELECT reading_id,created_at FROM favorites WHERE owner_id=%s AND reading_id=%s',
                                                       (owner, reading_id)).fetchone())

    def add(self, owner, reading_id):
        return favorite_record(self.connection.execute('''INSERT INTO favorites(owner_id,reading_id) VALUES(%s,%s)
            RETURNING reading_id,created_at''', (owner, reading_id)).fetchone())

    def remove(self, owner, reading_id):
        return self.connection.execute('DELETE FROM favorites WHERE owner_id=%s AND reading_id=%s', (owner, reading_id)).rowcount == 1

    def list(self, owner, *, after, limit):
        query, params = 'SELECT reading_id,created_at FROM favorites WHERE owner_id=%s', [owner]
        if after is not None:
            query += ' AND (created_at,reading_id)<(%s,%s)'
            params.extend(after)
        rows = self.connection.execute(query + ' ORDER BY created_at DESC,reading_id DESC LIMIT %s', [*params, limit]).fetchall()
        return [favorite_record(row) for row in rows]

    def all_owned(self, owner):
        rows = self.connection.execute('SELECT reading_id,created_at FROM favorites WHERE owner_id=%s ORDER BY created_at,reading_id',
                                       (owner,)).fetchall()
        return [favorite_record(row) for row in rows]

    def erase_owner(self, owner):
        self.connection.execute('DELETE FROM p1_favorite_create_requests WHERE owner_id=%s', (owner,))
        self.connection.execute('DELETE FROM favorites WHERE owner_id=%s', (owner,))


class PostgresFavoritesUnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def repository_for(self, transaction):
        return PostgresFavoritesRepository(transaction)

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.favorites = self.repository_for(self.connection)
        return self

    def commit(self):
        self.connection.commit()

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
