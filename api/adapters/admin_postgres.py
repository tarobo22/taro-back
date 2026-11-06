from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from api.adapters.data_privacy_postgres import lock_owner, require_active_owner


class PostgresAdminRepository:
    def __init__(self, connection):
        self.connection = connection

    def lock_owner(self, owner):
        lock_owner(self.connection, owner)

    def lock_governance(self, *, shared=False):
        function = 'pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
        self.connection.execute('SELECT '+function+"(hashtextextended('yeoun:admin-governance',0))")

    def active(self, owner):
        require_active_owner(self.connection, owner)

    def member(self, owner):
        return bool(self.connection.execute('SELECT 1 FROM social_accounts WHERE owner_id=%s', (owner,)).fetchone())

    def role(self, owner):
        row = self.connection.execute('SELECT role FROM admin_roles WHERE owner_id=%s', (owner,)).fetchone()
        return row['role'] if row else None

    def set_role(self, owner, role):
        self.connection.execute('''INSERT INTO admin_roles(owner_id,role) VALUES(%s,%s)
            ON CONFLICT(owner_id) DO UPDATE SET role=EXCLUDED.role,updated_at=clock_timestamp()''', (owner,role))

    def revoke_role(self, owner):
        self.connection.execute('DELETE FROM admin_roles WHERE owner_id=%s', (owner,))

    def source_by_hash(self, token_hash):
        return self.connection.execute('SELECT * FROM social_device_sessions WHERE token_hash=%s', (token_hash,)).fetchone()

    def source_valid(self, session_id, owner, now):
        return bool(self.connection.execute('''SELECT 1 FROM social_device_sessions
            WHERE id=%s AND owner_id=%s AND revoked_at IS NULL AND expires_at>%s''', (session_id,owner,now)).fetchone())

    def add_session(self, row):
        self.connection.execute('''INSERT INTO admin_sessions(id,owner_id,token_hash,source_session_id,role,expires_at)
            VALUES(%(id)s,%(owner_id)s,%(token_hash)s,%(source_session_id)s,%(role)s,%(expires_at)s)''', row)

    def session(self, token_hash):
        return self.connection.execute('SELECT * FROM admin_sessions WHERE token_hash=%s', (token_hash,)).fetchone()

    def delete_session(self, session_id):
        self.connection.execute('DELETE FROM admin_sessions WHERE id=%s', (session_id,))

    def delete_owner_sessions(self, owner):
        self.connection.execute('DELETE FROM admin_sessions WHERE owner_id=%s', (owner,))

    def versions_list(self):
        return self.connection.execute('SELECT * FROM admin_catalog_versions ORDER BY version_id').fetchall()

    def version(self, version_id):
        return self.connection.execute('SELECT * FROM admin_catalog_versions WHERE version_id=%s', (version_id,)).fetchone()

    def save_review(self, row):
        values = {**row,'snapshot':Jsonb(row['snapshot'])}
        # A review changes metadata only. Stored snapshot bodies and asset IDs
        # are immutable even if the source template changes at runtime.
        self.connection.execute('''INSERT INTO admin_catalog_versions
            (version_id,snapshot,snapshot_hash,rights_status,review_note,review_version)
            VALUES(%(version_id)s,%(snapshot)s,%(snapshot_hash)s,%(rights_status)s,%(review_note)s,%(review_version)s)
            ON CONFLICT(version_id) DO UPDATE SET rights_status=EXCLUDED.rights_status,
                review_note=EXCLUDED.review_note,review_version=EXCLUDED.review_version''', values)

    def head(self, code):
        return self.connection.execute('SELECT publication_id,version FROM admin_catalog_heads WHERE code=%s',
                                       (code,)).fetchone() or {'publication_id':None,'version':0}

    def save_head(self, code, publication_id, version):
        self.connection.execute('''INSERT INTO admin_catalog_heads(code,publication_id,version) VALUES(%s,%s,%s)
            ON CONFLICT(code) DO UPDATE SET publication_id=EXCLUDED.publication_id,version=EXCLUDED.version''',
            (code,publication_id,version))

    def add_publication(self, row):
        self.connection.execute('''INSERT INTO admin_catalog_publications(id,version_id,code,action,previous_id,snapshot,created_at)
            VALUES(%(id)s,%(version_id)s,%(code)s,%(action)s,%(previous_id)s,%(snapshot)s,%(created_at)s)''',
            {**row,'snapshot':Jsonb(row['snapshot'])})

    def publication(self, publication_id):
        return self.connection.execute('SELECT * FROM admin_catalog_publications WHERE id=%s', (publication_id,)).fetchone()

    def publications_list(self):
        return self.connection.execute('SELECT * FROM admin_catalog_publications ORDER BY created_at DESC,id DESC').fetchall()

    @staticmethod
    def _feedback_select():
        return '''SELECT f.id,f.reading_id,f.turn_id,f.rating,f.reason,f.comment,f.created_at,
            COALESCE(t.status,'new') AS triage_status,COALESCE(t.version,0) AS triage_version
            FROM data_privacy_feedback f LEFT JOIN admin_feedback_triage t ON t.feedback_id=f.id'''

    def feedback(self, feedback_id):
        return self.connection.execute(self._feedback_select()+' WHERE f.id=%s', (feedback_id,)).fetchone()

    def feedback_list(self, *, after=None, limit=20, status=None):
        where,values = ['TRUE'],[]
        if status is not None:
            where.append("COALESCE(t.status,'new')=%s");values.append(status)
        if after is not None:
            where.append('(f.created_at,f.id)<(%s,%s)');values.extend(after)
        values.append(limit)
        return self.connection.execute(self._feedback_select()+' WHERE '+' AND '.join(where)+
            ' ORDER BY f.created_at DESC,f.id DESC LIMIT %s', values).fetchall()

    def triage(self, feedback_id, expected, status):
        # Prevent an erase of the feedback owner from leaving triage metadata
        # behind; this row lock and FK participate in that same transaction.
        found = self.connection.execute('SELECT id FROM data_privacy_feedback WHERE id=%s FOR KEY SHARE',
                                        (feedback_id,)).fetchone()
        if not found:
            return False
        if expected == 0:
            return self.connection.execute('''INSERT INTO admin_feedback_triage(feedback_id,status,version)
                VALUES(%s,%s,1) ON CONFLICT DO NOTHING''', (feedback_id,status)).rowcount == 1
        return self.connection.execute('''UPDATE admin_feedback_triage SET status=%s,version=version+1
            WHERE feedback_id=%s AND version=%s''', (status,feedback_id,expected)).rowcount == 1

    def audit(self, row):
        self.connection.execute('''INSERT INTO admin_audit(id,actor_owner_id,action,target_type,target_id,details,created_at)
            VALUES(%(id)s,%(actor_owner_id)s,%(action)s,%(target_type)s,%(target_id)s,%(details)s,%(created_at)s)''',
            {**row,'details':Jsonb(row['details'])})

    def audits(self, *, after=None, limit=20):
        if after is None:
            return self.connection.execute('SELECT * FROM admin_audit ORDER BY created_at DESC,id DESC LIMIT %s', (limit,)).fetchall()
        return self.connection.execute('''SELECT * FROM admin_audit WHERE (created_at,id)<(%s,%s)
            ORDER BY created_at DESC,id DESC LIMIT %s''', (*after,limit)).fetchall()

    def ota_policy(self):
        return self.connection.execute('''SELECT channel,min_native_version,updates_enabled,version
            FROM admin_ota_policy WHERE singleton=1''').fetchone()

    def save_ota(self, values):
        self.connection.execute('''INSERT INTO admin_ota_policy(singleton,channel,min_native_version,updates_enabled,version)
            VALUES(1,%(channel)s,%(min_native_version)s,%(updates_enabled)s,%(version)s)
            ON CONFLICT(singleton) DO UPDATE SET channel=EXCLUDED.channel,min_native_version=EXCLUDED.min_native_version,
                updates_enabled=EXCLUDED.updates_enabled,version=EXCLUDED.version''', values)

    def purge(self, now, cutoff):
        self.connection.execute('DELETE FROM admin_sessions WHERE expires_at<=%s', (now,))
        if cutoff is not None:
            self.connection.execute('DELETE FROM admin_audit WHERE created_at<=%s', (cutoff,))

    def export_owner(self, owner):
        roles = self.connection.execute('SELECT role,updated_at FROM admin_roles WHERE owner_id=%s',
                                        (owner,)).fetchall()
        sessions = self.connection.execute('''SELECT id,role,expires_at,source_session_id FROM admin_sessions
            WHERE owner_id=%s ORDER BY expires_at,id''', (owner,)).fetchall()
        actions = self.connection.execute('''SELECT a.id,a.action,a.target_type,
            CASE WHEN a.target_type IN ('catalog','ota_policy','admin_session') THEN a.target_id
                 WHEN a.target_type='feedback' AND EXISTS
                    (SELECT 1 FROM data_privacy_feedback f WHERE f.id::text=a.target_id AND f.owner_id=%s)
                 THEN a.target_id ELSE NULL END AS target_id,a.created_at
            FROM admin_audit a WHERE a.actor_owner_id=%s ORDER BY a.created_at,a.id''', (owner,owner)).fetchall()
        return {'admin_roles':roles, 'admin_sessions':sessions, 'admin_audit':actions}

    def erase_owner(self, owner):
        self.revoke_role(owner)
        self.delete_owner_sessions(owner)
        self.connection.execute('UPDATE admin_audit SET actor_owner_id=NULL WHERE actor_owner_id=%s', (owner,))
        self.connection.execute("UPDATE admin_audit SET target_id='erased' WHERE target_type='admin_role' AND target_id=%s", (str(owner),))


class PostgresAdminUnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def repository_for(self, transaction):
        return PostgresAdminRepository(transaction)

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.admin = self.repository_for(self.connection)
        return self

    def commit(self):
        self.connection.commit()

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
