"""Explicit local role administration; no HTTP grant endpoint or seeded roles.

Runtime DATABASE_URL is supplied by the operator's process, never an env file.
Example: python -m tools.admin_role grant --owner UUID --role viewer
"""
import argparse
import os
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from api.adapters.admin_postgres import PostgresAdminUnitOfWork
from api.application.admin import AdminRoleManager


def main(argv=None):
    parser=argparse.ArgumentParser(description='Explicitly grant or revoke an existing social member admin role.')
    actions=parser.add_subparsers(dest='action',required=True)
    grant=actions.add_parser('grant');grant.add_argument('--owner',type=UUID,required=True)
    grant.add_argument('--role',choices=('viewer','publisher'),required=True)
    revoke=actions.add_parser('revoke');revoke.add_argument('--owner',type=UUID,required=True)
    args=parser.parse_args(argv)
    dsn=os.environ.get('DATABASE_URL')
    if not dsn:
        parser.error('DATABASE_URL must be injected into this process at runtime')
    with ConnectionPool(dsn,min_size=1,max_size=1,kwargs={'row_factory':dict_row}) as pool:
        manager=AdminRoleManager(lambda:PostgresAdminUnitOfWork(pool))
        if args.action=='grant':manager.grant(args.owner,args.role)
        else:manager.revoke(args.owner)
    print('Role operation completed and audit recorded.')


if __name__=='__main__':
    main()
