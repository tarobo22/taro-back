"""Compare counseling service revisions on a frozen, synthetic Korean holdout."""
import argparse
from collections import Counter
from json import dumps
from math import ceil
import os
from pathlib import Path
import random
import socket
from statistics import mean
import sys
from time import perf_counter
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from evaluate_counseling_prompt_v3 import (
    AXES, HOLDOUT, MODEL, ResponsesRelay, dataset, endpoint, read_json, score,
    sha256, source_revision, write_new,
)

BASELINE = '4b25b4ddd44aae31d9aba9e74293d4ef01247df1'
SHARED = ('shared/counseling-runtime.json', 'shared/data-catalog.json')
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
PRICES = {'input': 0.25, 'cached_input': 0.025, 'output': 2.00}
PRICE_SOURCE = 'https://developers.openai.com/api/docs/pricing#text-tokens'
ARM_BUDGET_USD = 1.0
PROVIDER_FAILURES = {'AI_UNAVAILABLE', 'AI_RATE_LIMITED', 'AI_PROVIDER_ERROR', 'UNKNOWN_PROVIDER_OUTCOME'}


def plan(cases):
    messages = sum(len(case['turns']) for case in cases)
    limits = {'baseline': messages + len(cases), 'candidate': messages + 2 * len(cases)}
    return {
        'cases': len(cases), 'holdout_sha256': sha256(HOLDOUT), 'model': MODEL,
        'baseline_commit': BASELINE, 'concurrency': 1, 'web_search': False,
        'max_calls_by_arm': limits, 'max_total_calls': sum(limits.values()),
        'max_estimated_usd_by_arm': ARM_BUDGET_USD, 'prices_per_million_usd': PRICES,
        'price_source': PRICE_SOURCE, 'price_verified_on': '2026-11-07',
    }


def usage_cost(usage):
    try:
        inputs, outputs = usage['input_tokens'], usage['output_tokens']
        cached = usage.get('input_tokens_details', {}).get('cached_tokens', 0)
        if any(type(count) is not int or count < 0 for count in (inputs, outputs, cached)) or cached > inputs:
            raise ValueError('Invalid token counts')
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SystemExit('The provider receipt is missing valid token usage.') from None
    return ((inputs - cached) * PRICES['input'] + cached * PRICES['cached_input']
            + outputs * PRICES['output']) / 1_000_000


class BudgetRelay(ResponsesRelay):
    def __init__(self, client, url, call_limit):
        super().__init__(client, url, call_limit)
        self.cost_upper_usd = 0.0

    def post(self, url, *, json, headers, timeout):
        if json.get('tools') or json.get('store') is not False:
            raise SystemExit('Only non-stored, tool-free evaluation calls are permitted.')
        input_bound = len(dumps(json, ensure_ascii=False).encode('utf-8')) + 512
        reserve = (input_bound * PRICES['input'] + json['max_output_tokens'] * PRICES['output']) / 1_000_000
        if self.cost_upper_usd + reserve > ARM_BUDGET_USD:
            raise SystemExit('Stopped at the per-arm estimated spend limit.')
        self.cost_upper_usd += reserve
        response = super().post(url, json=json, headers=headers, timeout=timeout)
        if response.is_success:
            receipt = self.receipts[-1]
            cost = usage_cost(receipt['usage'])
            receipt['estimated_usd'] = cost
            receipt['schema'] = json['text']['format']['name']
            self.cost_upper_usd += cost - reserve
        return response


def ensure_relay(url):
    address = urlsplit(url)
    try:
        with socket.create_connection((address.hostname, address.port or 80), timeout=3):
            pass
    except OSError:
        raise SystemExit('EVAL_RELAY_UNREACHABLE. No provider calls made.') from None


def isolated_database():
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ.get('YEOUN_TEST_DATABASE_URL', '')
    try:
        params = conninfo_to_dict(dsn)
    except ProgrammingError:
        raise SystemExit('Set a valid isolated YEOUN_TEST_DATABASE_URL.') from None
    if (params.get('host') not in LOOPBACK or params.get('hostaddr', '127.0.0.1') not in LOOPBACK
            or not params.get('dbname', '').endswith('_test')):
        raise SystemExit('Use an isolated loopback PostgreSQL database ending in _test.')
    return dsn


def run_case(service, consents, pool, case, relay):
    from api.domain.counseling import draw_cards

    owner = uuid4()
    service.sample = random.Random(case['id']).sample
    expected_cards = draw_cards(service.cards, 3, random.Random(case['id']).sample)
    row = {'case_id': case['id'], 'draw_ids': [card['card_id'] for card in expected_cards],
           'actual_draw_ids': [], 'turns': [], 'steps': [], 'result': None}
    receipt_start = len(relay.receipts)
    with pool.connection() as connection:
        connection.execute('INSERT INTO app_sessions(id, token_hash) VALUES (%s, %s)', (owner, str(owner)))
    try:
        consent = consents.record_consent(owner, uuid4(), 'ai_processing', service.policy['policy_version'], True)['id']
        for entry in case.get('dictionary', []):
            service.define(owner, entry['term'], entry['definition'])
        view = service.create(owner, uuid4())
        session_id = UUID(view['id'])

        def submit(kind, payload):
            started = perf_counter()
            service.enqueue(owner, session_id, uuid4(), kind, {
                **payload, 'expected_version': view['version'], 'consent_event_id': consent,
            })
            if not service.run_next():
                raise SystemExit('The isolated counseling worker did not claim the synthetic command.')
            current = service.get(owner, session_id)
            row['steps'].append({'kind': kind, 'ms': round((perf_counter() - started) * 1000, 2)})
            error = current['job']['error_code']
            if error in PROVIDER_FAILURES:
                raise SystemExit(f'Provider run stopped at {case["id"]}: {error}')
            return current

        for message in case['turns']:
            view = submit('message', {'text': message})
            row['turns'].append({key: view[key] for key in (
                'acknowledgement', 'understanding', 'clarification_question', 'phase', 'response_mode',
            )})
            if view['job']['state'] == 'failed' or view['phase'] == 'support':
                break
        if view['phase'] == 'confirmation' and view['job']['state'] == 'succeeded':
            view = submit('confirm', {'confirmed': True, 'card_count': 3, 'web_search': False})
        row['phase'] = view['phase']
        row['status'] = view['job']['error_code'] or view['phase']
        row['response_mode'] = view['response_mode']
        row['actual_draw_ids'] = [card['card_id'] for card in view['cards']]
        if row['actual_draw_ids'] and row['actual_draw_ids'] != row['draw_ids']:
            raise SystemExit('The worker did not use the paired evaluation draw.')
        if view['phase'] == 'advice':
            row['result'] = view['result']
        row['provider_calls'] = len(relay.receipts) - receipt_start
        return row
    finally:
        with pool.connection() as connection:
            connection.execute('DELETE FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,))
            connection.execute('DELETE FROM app_sessions WHERE id=%s', (owner,))


def capture(args, cases):
    if not args.allow_paid or args.out.exists():
        raise SystemExit('Capture requires --allow-paid and a new output path.')
    root = args.source.resolve()
    revision = source_revision(root)
    if (args.arm == 'baseline') != (revision == BASELINE):
        raise SystemExit('The baseline must be the frozen v3 revision.')
    relay_url = endpoint(args.relay)
    ensure_relay(relay_url)
    dsn = isolated_database()

    import httpx
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    sys.path.insert(0, str(root))
    from api.adapters import counseling_openai as adapter
    from api.adapters.ai_jobs_postgres import PostgresAIJobsUnitOfWork
    from api.adapters.counseling_postgres import PostgresCounselingStore
    from api.application.ai_jobs import AIJobsService
    from api.application.counseling import CounselingService

    version = getattr(adapter, 'CONTEXT_PROMPT_VERSION', 'esc-strategy-20261107-v3')
    if args.arm == 'candidate' and version != 'confirmed-story-20261107-v4':
        raise SystemExit('The candidate must implement confirmed-story v4.')
    config = read_json(root / SHARED[0])
    cards = read_json(root / SHARED[1])['cards']
    report = {**plan(cases), 'arm': args.arm, 'source_commit': revision, 'prompt_version': version,
              'inputs_sha256': {name: sha256(root / name) for name in SHARED}, 'rows': []}
    with (httpx.Client(trust_env=False, follow_redirects=False) as client,
          ConnectionPool(dsn, min_size=1, max_size=2, kwargs={'row_factory': dict_row}) as pool):
        pool.wait(timeout=10)
        with pool.connection() as connection:
            if connection.execute('SELECT 1 FROM app_sessions LIMIT 1').fetchone():
                raise SystemExit('The evaluation database must start without user sessions.')
        relay = BudgetRelay(client, relay_url, report['max_calls_by_arm'][args.arm])
        model = adapter.OpenAICounselingModel(relay, MODEL, lambda: 'relay', policy=config,
                                             reasoning_effort=config['reasoning_effort'])
        service = CounselingService(PostgresCounselingStore(pool), model, cards)
        consents = AIJobsService(lambda: PostgresAIJobsUnitOfWork(pool))
        try:
            for case in cases:
                row = run_case(service, consents, pool, case, relay)
                report['rows'].append(row)
                print(f'{args.arm} {case["id"]} {row["status"]}', flush=True)
            report['status'] = 'captured'
        finally:
            report.setdefault('status', 'stopped')
            report.update(attempts=relay.attempts, receipts=relay.receipts,
                          estimated_upper_usd=round(relay.cost_upper_usd, 8))
            write_new(args.out, report)


def latency(values):
    values = sorted(values)
    return {'mean_ms': round(mean(values), 2), 'p95_ms': values[ceil(0.95 * len(values)) - 1]} if values else None


def performance(report):
    rows, receipts = report['rows'], report['receipts']
    totals = [sum(step['ms'] for step in row['steps']) for row in rows]
    confirms = [step['ms'] for row in rows for step in row['steps'] if step['kind'] == 'confirm']
    inputs = sum(r['usage']['input_tokens'] for r in receipts)
    outputs = sum(r['usage']['output_tokens'] for r in receipts)
    cached = sum(r['usage'].get('input_tokens_details', {}).get('cached_tokens', 0) for r in receipts)
    return {
        'source_commit': report['source_commit'], 'cases': len(rows),
        'delivered': sum(row['result'] is not None for row in rows),
        'case_states': dict(Counter(row['status'] for row in rows)),
        'provider_calls': report['attempts'], 'review_calls': sum(r['schema'] == 'ReviewedReply' for r in receipts),
        'provider_latency': latency([r['milliseconds'] for r in receipts]),
        'case_latency_without_user_wait': latency(totals), 'confirmation_latency': latency(confirms),
        'input_tokens': inputs, 'cached_input_tokens': cached, 'output_tokens': outputs,
        'estimated_usd': round(sum(r['estimated_usd'] for r in receipts), 8),
        'estimated_usd_without_cache': round((inputs * PRICES['input'] + outputs * PRICES['output']) / 1_000_000, 8),
    }


def compare(args, cases):
    reports = {arm: read_json(getattr(args, arm)) for arm in ('baseline', 'candidate')}
    for arm, report in reports.items():
        if (report['status'] != 'captured' or report['arm'] != arm or report['model'] != MODEL
                or report['holdout_sha256'] != sha256(HOLDOUT)
                or report['attempts'] != len(report['receipts'])
                or [row['case_id'] for row in report['rows']] != [case['id'] for case in cases]):
            raise SystemExit('A paired comparison requires both complete 50-case provider captures.')
    left, right = reports['baseline'], reports['candidate']
    if (left['source_commit'] != BASELINE or left['inputs_sha256'] != right['inputs_sha256']
            or any(a['draw_ids'] != b['draw_ids'] for a, b in zip(left['rows'], right['rows']))):
        raise SystemExit('Baseline, settings, catalog or paired draws do not match.')
    receipt_ids = [r['id'] for report in reports.values() for r in report['receipts']]
    if len(set(receipt_ids)) != len(receipt_ids) or len(receipt_ids) < 2 * len(cases):
        raise SystemExit('The comparison requires distinct provider receipts for both arms.')

    pairs, mapping = [], {}
    rng = random.SystemRandom()
    visible = ('turns', 'result', 'phase', 'response_mode', 'actual_draw_ids')
    for case, before, after in zip(cases, left['rows'], right['rows']):
        labels = list(reports)
        rng.shuffle(labels)
        mapping[case['id']] = dict(zip(('X', 'Y'), labels))
        by_arm = {'baseline': before, 'candidate': after}
        pairs.append({'case': case, **{label: {key: by_arm[arm][key] for key in visible}
                                     for label, arm in mapping[case['id']].items()}})
    write_new(args.review, {'holdout_sha256': sha256(HOLDOUT), 'axes': AXES, 'pairs': pairs})
    write_new(args.mapping, {'review_sha256': sha256(args.review), 'arms': mapping})
    write_new(args.out, {**plan(cases), 'arms': {arm: performance(report) for arm, report in reports.items()}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('plan')
    run = commands.add_parser('capture')
    run.add_argument('--arm', choices=('baseline', 'candidate'), required=True)
    run.add_argument('--source', type=Path, required=True)
    run.add_argument('--relay', required=True)
    run.add_argument('--allow-paid', action='store_true')
    run.add_argument('--out', type=Path, required=True)
    pair = commands.add_parser('compare')
    for name in ('baseline', 'candidate', 'review', 'mapping', 'out'):
        pair.add_argument('--' + name, type=Path, required=True)
    grade = commands.add_parser('score')
    for name in ('review', 'mapping', 'ratings', 'out'):
        grade.add_argument('--' + name, type=Path, required=True)

    args = parser.parse_args()
    cases = dataset()
    if read_json(HOLDOUT).get('synthetic') is not True:
        raise SystemExit('Only the synthetic holdout is eligible for this evaluation.')
    if args.command == 'plan':
        print(dumps(plan(cases), ensure_ascii=False, indent=2))
    else:
        {'capture': capture, 'compare': compare, 'score': score}[args.command](args, cases)


if __name__ == '__main__':
    main()
