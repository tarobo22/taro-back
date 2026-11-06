"""Single-user real-API A/B evaluation. Never persists a key or auth header."""
import argparse
import csv
from datetime import datetime, timezone
import getpass
import hashlib
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import time
import types

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.domain.counseling import (Analysis, CounselResult, CounselingError,
                                  draw_cards, grounded_result, retrieve_definitions, strict_schema)

BASELINE = 'f93bb9e'
AFTER = '4e80d4b'
MODEL = 'gpt-5-mini-2025-08-07'
PRICES = {'input_per_million': .25, 'cached_input_per_million': .025, 'output_per_million': 2.0}
PRICE_SOURCE = 'https://developers.openai.com/api/docs/models/gpt-5-mini'
MAX_CALLS = 48
MAX_USD = 1.0


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def save(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    # The only input to the model is synthetic; still fail closed on secret-shaped output.
    if re.search(r'sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{25,}', text):
        raise RuntimeError('SECRET_SHAPED_OUTPUT_NOT_SAVED')
    path.write_text(text, encoding='utf-8')


def module_at(commit):
    result = subprocess.run(['git', 'show', commit + ':api/adapters/counseling_openai.py'],
                            cwd=ROOT, capture_output=True, check=True)
    mod = types.ModuleType('counseling_at_' + commit)
    exec(compile(result.stdout, '<git:' + commit + '>', 'exec'), mod.__dict__)
    return mod, digest(result.stdout)


def estimated_cost(usage):
    n = int(usage.get('input_tokens', 0))
    cached = min(n, int(usage.get('input_tokens_details', {}).get('cached_tokens', 0)))
    output = int(usage.get('output_tokens', 0))
    return ((n-cached)*PRICES['input_per_million'] +
            cached*PRICES['cached_input_per_million'] + output*PRICES['output_per_million'])/1_000_000


def prepare_cases(cases, cards):
    contexts = {}
    for c in cases:
        definitions = c.get('definitions', [])
        known = retrieve_definitions(c['input'], definitions)
        final_defs = c.get('confirmed_definitions', definitions)
        selected = draw_cards(cards, 3) if c['mode'] == 'advice' else []
        sources = [{'id': 'personal:' + d['id'], 'kind': 'user_definition',
                    'title': d['term'], 'url': None, 'text': d['definition']} for d in final_defs]
        sources += [{'id': 'tarot:' + str(card['card_id']), 'kind': 'tarot_reference',
                     'title': card['card'].get('name_ko', '타로 카드'),
                     'url': None, 'text': card['card'].get('basic_meaning', '')} for card in selected]
        history = c.get('history', []) + [{'role': 'user', 'text': c['input']}]
        if c.get('confirmed_user'):
            history += [{'role': 'assistant', 'text': '말씀하신 모래가 어떤 뜻인지 알려주실래요?'},
                        {'role': 'user', 'text': c['confirmed_user']}]
        history += [{'role': 'assistant', 'text': c['understanding']},
                    {'role': 'user', 'text': '맞아. 내가 원한 방식으로 이어가줘.'}]
        contexts[c['id']] = {
            'analyze': {'text': c['input'], 'history': c.get('history', []), 'dictionary': known},
            'compose': {'understanding': c['understanding'], 'perspective': c.get('perspective'),
                        'response_mode': c['mode'], 'history': history,
                        'cards': selected, 'sources': sources, 'search_status': 'not_requested'}}
    return contexts


def checks_for(case, phase, value, context):
    if phase == 'analyze':
        checks = {name: value.get(name) == expected for name, expected in case['expected'].items()
                  if name != 'clarification_required'}
        if case['expected'].get('clarification_required'):
            checks['clarification_required'] = bool(value.get('clarification_question'))
        return checks
    checks = {'correct_card_ids_and_order': [v['card_id'] for v in value['tarot']] ==
              [v['card_id'] for v in context['cards']]}
    if case['mode'] == 'listen':
        checks.update(no_actions=not value['actions'], no_cards=not value['tarot'],
                      no_fact_dump=not value['facts'], no_followup_question=value['follow_up_question'] is None)
    else:
        checks['one_small_action'] = len(value['actions']) == 1
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--out-dir', default='docs/counseling/ab-20261107')
    args = parser.parse_args()
    out = ROOT / args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    case_file = ROOT/'evals/counseling-ab-20261107.json'
    cases = json.loads(case_file.read_text(encoding='utf-8'))['cases']
    assert len(cases) == 12 and len({c['id'] for c in cases}) == 12
    cards = json.loads((ROOT/'shared/data-catalog.json').read_text(encoding='utf-8'))['cards']
    config = json.loads((ROOT/'shared/counseling-runtime.json').read_text(encoding='utf-8'))
    modules = {}
    hashes = {}
    for name, commit in [('before', BASELINE), ('after', AFTER)]:
        modules[name], hashes[name] = module_at(commit)
    inputs_file = out/'inputs.json'
    if inputs_file.exists():
        existing = json.loads(inputs_file.read_text(encoding='utf-8'))
        if existing['case_sha256'] != digest(case_file.read_bytes()):
            raise RuntimeError('CASE_SET_CHANGED')
        contexts = existing['contexts']
    else:
        contexts = prepare_cases(cases, cards)
        save(inputs_file, {'case_sha256': digest(case_file.read_bytes()), 'contexts': contexts})
    plan = {'model': MODEL, 'reasoning_effort': config['reasoning_effort'],
            'baseline_commit': BASELINE, 'after_commit': AFTER, 'module_sha256': hashes,
            'cases': len(cases), 'max_inference_calls': MAX_CALLS, 'concurrency': 1,
            'budget_usd': MAX_USD, 'prices_usd': PRICES, 'price_source': PRICE_SOURCE,
            'price_verified_on': '2026-11-07', 'web_search_calls': 0,
            'stage_comparison': 'independent stages with identical manually confirmed context',
            'load_test': False, 'temperature': 'omitted; model default', 'repetitions_per_stage': 1}
    save(out/'plan.json', plan)
    if args.dry_run:
        print(json.dumps({'ready': True, **plan}))
        return
    result_file = out/'raw-results.json'
    if result_file.exists():
        raise RuntimeError('RESULTS_EXIST_USE_A_NEW_RUN_DIRECTORY_NO_AUTOMATIC_REPLAY')
    print('API key input (not saved or echoed).', flush=True)
    api_key = getpass.getpass('OpenAI key: ')
    if not api_key.strip():
        print('EMPTY_KEY_NO_CALLS', flush=True)
        return
    rows = []
    active = {}
    unknown_reserved = 0.0
    report = {'plan': plan, 'started_at': datetime.now(timezone.utc).isoformat(),
              'status': 'running', 'rows': rows}

    def request_hook(request):
        if request.url.host != 'api.openai.com' or request.url.path != '/v1/responses':
            raise RuntimeError('UNEXPECTED_API_TARGET')
        body = json.loads(request.content)
        if body.get('tools'):
            raise RuntimeError('TOOLS_NOT_ALLOWED_IN_CONTROLLED_AB')
        # Upper-bound token estimate uses UTF-8 bytes + protocol overhead, with no cache benefit.
        bound_tokens = len(request.content) + 1024
        reserve = (bound_tokens*PRICES['input_per_million'] +
                   body['max_output_tokens']*PRICES['output_per_million'])/1_000_000
        spent = sum(r.get('estimated_usd', 0) for r in rows) + unknown_reserved
        if spent + reserve > MAX_USD:
            raise RuntimeError('BUDGET_LIMIT')
        active.update(start=time.perf_counter(), reserve=reserve, instructions_sha256=digest(body['instructions']),
                      request_sha256=digest(request.content), input_sha256=digest(body['input']),
                      max_output_tokens=body['max_output_tokens'])

    def response_hook(response):
        response.read()
        active['provider_ms'] = round((time.perf_counter()-active['start'])*1000, 2)
        active['http_status'] = response.status_code
        try:
            data = response.json()
        except ValueError:
            active['provider_error_code'] = 'NON_JSON_RESPONSE'
            return
        active.update(response_id=data.get('id'), response_model=data.get('model'),
                      response_status=data.get('status'), usage=data.get('usage') or {},
                      incomplete_details=data.get('incomplete_details'))
        if response.is_success:
            texts = [part.get('text', '') for item in data.get('output', []) if item.get('type') == 'message'
                     for part in item.get('content', []) if part.get('type') == 'output_text']
            text = ''.join(texts)
            active['raw_output_text'] = text
            try:active['raw_output'] = json.loads(text)
            except ValueError:active['raw_output'] = None
        else:
            code = str((data.get('error') or {}).get('code') or 'HTTP_ERROR')
            active['provider_error_code'] = code if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', code) else 'HTTP_ERROR'

    with httpx.Client(event_hooks={'request': [request_hook], 'response': [response_hook]}) as client:
        clients = {name: mod.OpenAICounselingModel(client, MODEL, lambda: api_key, deadline=90,
                   policy=config, reasoning_effort=config['reasoning_effort']) for name, mod in modules.items()}
        fatal = False
        for index, case in enumerate(cases):
            for phase in ('analyze', 'compose'):
                order = ('before', 'after') if (index + (phase == 'compose')) % 2 == 0 else ('after', 'before')
                for variant in order:
                    if len(rows) >= MAX_CALLS:raise RuntimeError('CALL_LIMIT')
                    active.clear()
                    start = time.perf_counter()
                    error = None
                    delivered = None
                    try:
                        method = getattr(clients[variant], phase)
                        delivered = method(contexts[case['id']][phase])
                        if phase == 'compose':
                            context = contexts[case['id']][phase]
                            grounded_result(delivered, context['cards'], context['sources'], case['mode'])
                    except CounselingError as exc:
                        error = exc.code
                    except Exception as exc:
                        # Never print exception content: transport errors can contain credentials.
                        error = type(exc).__name__
                        fatal = True
                    raw = active.get('raw_output')
                    checks = {}
                    if isinstance(raw, dict):
                        try:
                            schema = Analysis if phase == 'analyze' else CounselResult
                            shaped = schema.model_validate(raw).model_dump()
                            checks = {'json_schema_valid': True, **checks_for(case, phase, shaped, contexts[case['id']][phase])}
                        except (ValueError, KeyError, TypeError):
                            checks = {'json_schema_valid': False}
                    else:checks = {'json_schema_valid': False}
                    usage = active.get('usage') or {}
                    cost = estimated_cost(usage)
                    if active.get('http_status') is None:unknown_reserved += active.get('reserve', 0)
                    row = {'case_id': case['id'], 'phase': phase, 'variant': variant,
                           'method_ms': round((time.perf_counter()-start)*1000, 2),
                           **{k: v for k, v in active.items() if k not in ('start', 'reserve')},
                           'estimated_usd': round(cost, 9), 'contract_error': error,
                           'delivered': error is None, 'objective_checks': checks}
                    rows.append(row)
                    report['estimated_usd'] = round(sum(r['estimated_usd'] for r in rows), 9)
                    report['inference_attempts'] = len(rows)
                    save(result_file, report)
                    print(json.dumps({'call': len(rows), 'case': case['id'], 'phase': phase,
                                      'variant': variant, 'http': row.get('http_status'),
                                      'error': error, 'milliseconds': row['method_ms'],
                                      'total_estimated_usd': report['estimated_usd']}), flush=True)
                    if row.get('http_status') in (401, 403, 404, 429):fatal = True
                    if fatal:break
                if fatal:break
            if fatal:break
    api_key = None
    report['status'] = 'stopped' if fatal else 'completed'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    report['unknown_outcome_reserved_usd'] = round(unknown_reserved, 9)
    save(result_file, report)
    blind = []
    mapping = {}
    rng = random.SystemRandom()
    for case in cases:
        for phase in ('analyze', 'compose'):
            pair = [r for r in rows if r['case_id'] == case['id'] and r['phase'] == phase]
            if len(pair) != 2:continue
            rng.shuffle(pair)
            candidates = []
            for label, row in zip(('X', 'Y'), pair):
                key = case['id'] + ':' + phase + ':' + label
                mapping[key] = row['variant']
                candidates.append({'candidate': label, 'output': row.get('raw_output') or row.get('raw_output_text'),
                                   'provider_completed': row.get('response_status') == 'completed'})
            blind.append({'case_id': case['id'], 'phase': phase, 'user_input': case['input'],
                          'focus': case['grading_focus'], 'context': contexts[case['id']][phase],
                          'candidates': candidates})
    save(out/'blinded-review.json', blind)
    private = ROOT/'.local/ab-eval-20261107'
    private.mkdir(parents=True, exist_ok=True)
    save(private/'blind-map.json', mapping)
    print(json.dumps({'status': report['status'], 'inference_attempts': len(rows),
                      'estimated_usd': report['estimated_usd'], 'complete_pairs': len(blind)}), flush=True)


if __name__ == '__main__':
    main()

