"""Owner-scoped data operations. Constructors never acquire a database connection."""
from copy import deepcopy
import base64
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from api.domain.data_privacy import DataConflict, DataExpired, DataNotFound, DataRateLimited, InvalidDataInput


class DataPrivacyUseCases:
    def __init__(self, uow_factory, policy, catalog, *, export_callbacks=(), erase_callbacks=(),
                 feedback_target_exists=None, clock=None):
        self.uow_factory = uow_factory
        self.policy = policy
        self.catalog = deepcopy(catalog)
        self.export_callbacks = tuple(export_callbacks)
        self.erase_callbacks = tuple(erase_callbacks)
        self.feedback_target_exists = feedback_target_exists
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _me(profile):
        return {'id': str(profile['owner_id']), 'kind': 'guest', 'locale': profile['locale'],
                'timezone': profile['timezone'], 'preferences': {key: profile[key]
                    for key in ('tone', 'reduced_motion', 'voice_mode')}}

    def get_me(self, owner):
        with self.uow_factory() as uow:
            uow.data.lock(owner)
            uow.data.active(owner)
            result = self._me(uow.data.profile(owner, self.policy))
            uow.commit()
            return result

    def update_preferences(self, owner, values):
        values = self.policy.preference_patch(values)
        with self.uow_factory() as uow:
            uow.data.lock(owner)
            uow.data.active(owner)
            uow.data.profile(owner, self.policy)
            uow.data.update_profile(owner, values)
            result = self._me(uow.data.profile(owner, self.policy))
            uow.commit()
            return result

    def _catalog_page(self, items, kind, cursor, limit, filters):
        if limit is None:
            return {'items': deepcopy(items), 'next_cursor': None}
        if type(limit) is not int or not 1 <= limit <= self.policy.catalog_page_max:
            raise InvalidDataInput('invalid_page_limit')
        binding = {'kind': kind, 'version': self.catalog['source_manifest_sha256'], 'filters': filters}
        start = 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024:
                    raise ValueError
                value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
                start = value.pop('offset')
                if value != binding or type(start) is not int or not 0 <= start < len(items):
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                raise InvalidDataInput('invalid_catalog_cursor') from None
        end = start + limit
        next_cursor = None
        if end < len(items):
            next_cursor = base64.urlsafe_b64encode(json.dumps({**binding, 'offset': end},
                sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')
        return {'items': deepcopy(items[start:end]), 'next_cursor': next_cursor}

    def list_decks(self, *, cursor=None, limit=None):
        return self._catalog_page(self.catalog['decks'], 'decks', cursor, limit, {})

    def get_deck(self, deck_id):
        for deck in self.catalog['decks']:
            if deck['id'] == str(deck_id):
                return deepcopy(deck)
        raise DataNotFound('deck_not_found')

    def list_cards(self, *, deck_id=None, deck_version_id=None, arcana=None, suit=None, cursor=None, limit=None):
        if deck_id is not None and deck_version_id is not None and str(deck_id) != str(deck_version_id):
            raise InvalidDataInput('conflicting_deck_version')
        deck_id = deck_version_id or deck_id
        if deck_id is not None:
            self.get_deck(deck_id)
        if arcana not in (None, 'major', 'minor') or suit not in (None, 'wands', 'cups', 'swords', 'pentacles'):
            raise InvalidDataInput('invalid_card_filter')
        cards = [card for card in self.catalog['cards']
                          if (arcana is None or card['arcana'] == arcana)
                          and (suit is None or card['suit'] == suit)]
        return self._catalog_page(cards, 'cards', cursor, limit,
                                  {'deck': str(deck_id) if deck_id else None, 'arcana': arcana, 'suit': suit})

    def get_card(self, card_id, *, deck_version_id=None):
        if deck_version_id is not None:
            self.get_deck(deck_version_id)
        for card in self.catalog['cards']:
            if card['id'] == card_id:
                return deepcopy(card)
        raise DataNotFound('card_not_found')

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    @classmethod
    def _fingerprint(cls, value):
        return cls._digest(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str))

    def create_feedback(self, owner, values, request_key):
        values = self.policy.feedback(values)
        key_hash = self._digest(str(request_key))
        fingerprint = self._fingerprint(values)
        with self.uow_factory() as uow:
            uow.data.lock(owner)
            uow.data.active(owner)
            existing = uow.data.feedback_operation(owner, key_hash)
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise DataConflict('idempotency_conflict')
                return {'id': str(existing['id']), 'created_at': existing['created_at'].isoformat()}
            if uow.data.feedback_count(owner, self.clock().replace(hour=0, minute=0, second=0, microsecond=0)) >= self.policy.feedback_daily_limit:
                raise DataRateLimited('feedback_daily_limit')
            for kind in ('reading', 'turn'):
                target = values.get(kind + '_id')
                if target:
                    exists = (self.feedback_target_exists(uow.connection, owner, kind, target)
                              if self.feedback_target_exists else uow.data.legacy_target_exists(owner, kind, target))
                    if not exists:
                        raise DataNotFound('feedback_target_not_found')
            saved = uow.data.add_feedback(owner, uuid4(), values, key_hash, fingerprint)
            uow.commit()
            return {'id': str(saved['id']), 'created_at': saved['created_at'].isoformat()}

    def _receipt(self, owner, request_id, request_key, private_salt):
        # Private random material prevents forgery even if ordinary request
        # metadata is known. Raw receipts and raw request keys are never stored.
        payload = f'yeoun:data-receipt:v1:{owner}:{request_id}:{request_key}'.encode()
        return hmac.new(bytes(private_salt), payload, hashlib.sha256).hexdigest()

    @staticmethod
    def _accepted(row, receipt):
        return {'id': str(row['id']), 'kind': row['kind'], 'state': row['state'],
                'receipt': receipt, 'expires_at': row['expires_at'].isoformat()}

    def create_data_request(self, owner, kind, confirmation, request_key):
        self.policy.data_request(kind, confirmation)
        key_hash = self._digest(str(request_key))
        fingerprint = self._fingerprint({'kind': kind, 'confirmation': confirmation})
        now = self.clock()
        with self.uow_factory() as uow:
            uow.data.lock(owner)
            existing = uow.data.request_operation(owner, key_hash)
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise DataConflict('idempotency_conflict')
                return self._accepted(existing, self._receipt(owner, existing['id'], request_key, existing['receipt_salt']))
            uow.data.active(owner)
            uow.data.profile(owner, self.policy)
            if uow.data.request_count(owner, now.replace(hour=0, minute=0, second=0, microsecond=0)) >= self.policy.data_request_daily_limit:
                raise DataRateLimited('data_request_daily_limit')
            request_id = uuid4()
            private_salt = secrets.token_bytes(32)
            receipt = self._receipt(owner, request_id, request_key, private_salt)
            lifetime = timedelta(days=self.policy.receipt_days) if kind == 'erase' else timedelta(hours=self.policy.export_file_hours)
            row = uow.data.add_request(request_id, owner, kind, key_hash, fingerprint,
                                       self._digest(receipt), now + lifetime, private_salt)
            if kind == 'erase':
                uow.data.set_deleting(owner)
            uow.commit()
            return self._accepted(row, receipt)

    def get_data_request(self, request_id, *, owner=None, receipt=None):
        with self.uow_factory() as uow:
            row = uow.data.request(request_id)
            if row is None:
                raise DataNotFound('data_request_not_found')
            by_receipt = receipt is not None
            if by_receipt:
                if not isinstance(receipt, str) or len(receipt) != 64 or not hmac.compare_digest(self._digest(receipt), row['receipt_hash']):
                    raise DataNotFound('data_request_not_found')
            elif owner is None or row['owner_id'] != owner:
                raise DataNotFound('data_request_not_found')
            if row['expires_at'] <= self.clock():
                uow.data.expire_export(request_id)
                uow.commit()
                raise DataExpired('data_request_expired')
            return {'id': str(row['id']), 'kind': row['kind'], 'state': row['state'],
                    'download_available': not by_receipt and row['kind'] == 'export'
                        and row['state'] == 'completed' and row['export_bytes'] is not None,
                    'expires_at': row['expires_at'].isoformat()}

    def download_export(self, owner, request_id):
        with self.uow_factory() as uow:
            uow.data.lock(owner)
            uow.data.active(owner)
            row = uow.data.request(request_id)
            if row is None or row['owner_id'] != owner or row['kind'] != 'export':
                raise DataNotFound('export_not_found')
            if row['expires_at'] <= self.clock():
                uow.data.expire_export(request_id)
                uow.commit()
                raise DataExpired('export_expired')
            if row['state'] != 'completed' or row['export_bytes'] is None:
                raise DataConflict('export_not_ready')
            return bytes(row['export_bytes'])

    @classmethod
    def _export_value(cls, value):
        if isinstance(value, dict):
            # Cross-context ports return a deliberate personal-data view. This
            # additional filter rejects accidental credential/internal snapshots.
            forbidden = ('token', 'secret', 'credential', 'password', 'authorization',
                         'fingerprint', 'idempotency', 'receipt', 'lease_', 'snapshot',
                         'storage_key', 'api_key', 'raw_audio', 'export_bytes')
            return {str(key): cls._export_value(item) for key, item in value.items()
                    if not any(word in str(key).lower() for word in forbidden)}
        if isinstance(value, (list, tuple)):
            return [cls._export_value(item) for item in value]
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise InvalidDataInput('binary_data_not_exportable')
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        return value.isoformat() if hasattr(value, 'isoformat') else str(value)

    def _archive(self, collections):
        collections = self._export_value(collections)
        stream = io.BytesIO()
        with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps({'format_version': 1,
                'policy_version': self.policy.version, 'generated_at': self.clock().isoformat(),
                'collections': list(collections)}, ensure_ascii=False, indent=2))
            archive.writestr('data.json', json.dumps(collections, ensure_ascii=False, indent=2))
            for name, data in collections.items():
                if not re.fullmatch('[A-Za-z][A-Za-z0-9_-]*', name):
                    raise InvalidDataInput('invalid_export_collection')
                rows = data if isinstance(data, list) else [data]
                if not rows:
                    archive.writestr(name + '.csv', '\ufeff')
                    continue
                if not all(isinstance(row, dict) for row in rows):
                    raise InvalidDataInput('invalid_export_rows')
                columns = sorted({key for row in rows for key in row})
                output = io.StringIO(newline='')
                writer = csv.DictWriter(output, fieldnames=columns)
                writer.writeheader()
                for row in rows:
                    cleaned = {}
                    for key, value in row.items():
                        cell = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else '' if value is None else str(value)
                        cleaned[key] = "'" + cell if cell.startswith(('=', '+', '-', '@', '\t', '\r')) else cell
                    writer.writerow(cleaned)
                archive.writestr(name + '.csv', '\ufeff' + output.getvalue())
        return stream.getvalue()

    def process_request(self, request_id):
        """Durable worker entry point. Provider calls and public file URLs are absent."""
        owner = None
        try:
            with self.uow_factory() as uow:
                row = uow.data.request(request_id)
                if row is None or row['state'] == 'completed' or row['owner_id'] is None:
                    return False
                owner = row['owner_id']
                uow.data.lock(owner)
                row = uow.data.request(request_id, for_update=True)
                if row is None or row['state'] == 'completed':
                    return False
                uow.data.mark_running(request_id)
                uow.commit()

            with self.uow_factory() as uow:
                uow.data.lock(owner)
                row = uow.data.request(request_id, for_update=True)
                if row is None or row['state'] == 'completed':
                    return False
                if row['kind'] == 'export':
                    uow.data.active(owner)
                    collections = {'profile': self._me(uow.data.profile(owner, self.policy)),
                                   **uow.data.export_legacy(owner)}
                    for callback in self.export_callbacks:
                        addition = callback(uow.connection, owner)
                        if not isinstance(addition, dict) or collections.keys() & addition.keys():
                            raise DataConflict('export_collection_collision')
                        collections.update(addition)
                    archive = self._archive(collections)
                    uow.data.complete_export(request_id, archive,
                                             self.clock() + timedelta(hours=self.policy.export_file_hours))
                else:
                    for callback in self.erase_callbacks:
                        callback(uow.connection, owner)
                    uow.data.erase_legacy(owner)
                    replacement_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                    uow.data.complete_erasure(request_id, owner, replacement_hash, self.policy,
                                              self.clock() + timedelta(days=self.policy.receipt_days))
                uow.commit()
                return True
        except Exception as error:
            # Neither failure text nor personal input is persisted or logged.
            logging.getLogger('yeoun').warning('Private data worker failed: %s', type(error).__name__)
            if owner is not None:
                with self.uow_factory() as uow:
                    uow.data.lock(owner)
                    uow.data.mark_failed(request_id)
                    uow.commit()
            return False

    def process_pending(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidDataInput('invalid_worker_limit')
        with self.uow_factory() as uow:
            pending = uow.data.pending_requests(limit)
        return sum(self.process_request(request_id) for request_id in pending)

    def purge_expired(self):
        now = self.clock()
        with self.uow_factory() as uow:
            uow.data.purge_expired(now, now - timedelta(days=self.policy.receipt_days))
            uow.commit()
