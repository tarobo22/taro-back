"""Owner-lifecycle transaction guard shared by the legacy PostgreSQL units."""
from api.domain.identity import InactiveOwnerError


def guard_owner(connection, owner_id, callback):
    if callback is None:
        return
    connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                       (f'yeoun:owner:{owner_id}',))
    if callback(connection, owner_id) is False:
        raise InactiveOwnerError('owner_unavailable')
