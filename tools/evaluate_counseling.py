"""Bounded live HTTP evaluation. Never reads or prints the OpenAI project key.

The server owns that key. Run against an isolated local preview only.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--base-url',default='http://127.0.0.1:8818')
    ap.add_argument('--allow-paid',action='store_true');ap.add_argument('--limit',type=int,default=50)
    ap.add_argument('--finish-confirmed',action='store_true',help='Simulate explicit confirmation for clear synthetic cases and measure final advice.')
    ap.add_argument('--concurrency',type=int,choices=(1,5,10),default=1)
    ap.add_argument('--out',default='.local/counseling-live-evaluation.json');args=ap.parse_args()
    if urlsplit(args.base_url).hostname not in ('localhost','127.0.0.1','::1'):raise SystemExit('Only an isolated loopback preview is allowed.')
    cases=json.loads((Path(__file__).parents[1]/'evals/counseling-cases.json').read_text(encoding='utf-8'))['cases'][:max(1,min(args.limit,50))]
    out=Path(args.out);out.parent.mkdir(parents=True,exist_ok=True)
    def save(report):out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    with httpx.Client(base_url=args.base_url,timeout=15) as c:
        caps=c.get('/v2/counseling/capabilities');caps.raise_for_status();caps=caps.json()
    if not caps['available']:
        save({'status':'blocked','reason':'SERVER_AI_NOT_CONFIGURED','paid_calls':0,'case_count':len(cases)})
        print('Server AI is not configured. No paid calls or test accounts were created.');return
    if not args.allow_paid:
        save({'status':'ready','reason':'EXPLICIT_PAID_RUN_FLAG_REQUIRED','paid_calls':0,'case_count':len(cases)})
        print('Ready. Add --allow-paid to execute synthetic model cases.');return
    def one(case):
        if case['evaluation']!='model':return {'id':case['id'],'status':'separate_suite','suite':case['evaluation']}
        start=time.perf_counter()
        with httpx.Client(base_url=args.base_url,timeout=20) as c:
            grant=c.post('/v2/sessions',json={'platform':'web','locale':'ko-KR','timezone':'Asia/Seoul'});grant.raise_for_status()
            c.headers['Authorization']='Bearer '+grant.json()['access_token']
            consent=c.post('/v2/me/consents',headers={'X-Idempotency-Key':str(uuid4())},json={'kind':'ai_processing','policy_version':caps['policy_version'],'granted':True});consent.raise_for_status()
            for d in case['preconditions'].get('dictionary',[]):
                r=c.put('/v2/counseling/glossary',json={'term':d['term'],'definition':d['definition']});r.raise_for_status()
            r=c.post('/v2/counseling/sessions',headers={'X-Idempotency-Key':str(uuid4())},json={'topic':'care'});r.raise_for_status();v=r.json()
            # Additional dialog preconditions are reported for human review; never injected as privileged state.
            r=c.post('/v2/counseling/sessions/'+v['id']+'/messages',headers={'X-Idempotency-Key':str(uuid4())},
                json={'text':case['input'],'expected_version':v['version'],'consent_event_id':consent.json()['id']});r.raise_for_status()
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                r=c.get('/v2/counseling/sessions/'+v['id']);r.raise_for_status();v=r.json()
                if v['job'] and v['job']['state'] in ('succeeded','failed'):break
                time.sleep(.7)
            checks={'job_succeeded':v['job']['state']=='succeeded','no_early_draw':not v['cards'],
                    'acknowledgement':bool(v['acknowledgement']),'short_acknowledgement':len(v['acknowledgement'])<=100}
            if 'clarification_required' in case['checks']:checks['clarification_required']=bool(v['clarification_question'])
            if 'listen_mode' in case['checks']:checks['listen_mode']=v['response_mode']=='listen'
            if 'no_private_web_query' in case['checks']:checks['no_private_web_query']=all('@' not in q for q in v['search_queries'])
            if 'no_unnecessary_search' in case['checks']:checks['no_unnecessary_search']=not v['search_queries']
            analysis_ms=round((time.perf_counter()-start)*1000,2)
            if args.finish_confirmed and v['phase']=='confirmation' and v['job']['state']=='succeeded':
                r=c.post('/v2/counseling/sessions/'+v['id']+'/confirmation',headers={'X-Idempotency-Key':str(uuid4())},
                    json={'confirmed':True,'expected_version':v['version'],'consent_event_id':consent.json()['id'],'web_search':True,'card_count':3})
                r.raise_for_status();deadline=time.monotonic()+300
                while time.monotonic()<deadline:
                    r=c.get('/v2/counseling/sessions/'+v['id']);r.raise_for_status();v=r.json()
                    if v['job']['state'] in ('succeeded','failed'):break
                    time.sleep(.7)
                checks['final_job_succeeded']=v['job']['state']=='succeeded'
                checks['no_duplicate_cards']=len(v['cards'])==len({card['card_id'] for card in v['cards']})
            return {'id':case['id'],'status':'recorded','milliseconds':round((time.perf_counter()-start)*1000,2),
                'analysis_ms':analysis_ms,'final_result':v['result'] if args.finish_confirmed else None,
                'checks':checks,'semantic_review':'required','preconditions_requiring_review':case['preconditions'].get('history',[]),
                'acknowledgement':v['acknowledgement'],'understanding':v['understanding'],
                'question':v['clarification_question'],'public_queries':v['search_queries'],'error':v['job']['error_code']}
    with ThreadPoolExecutor(max_workers=args.concurrency) as workers:rows=list(workers.map(one,cases))
    save({'status':'recorded','synthetic_inputs':True,'concurrency':args.concurrency,'phase':'acknowledgement-and-understanding',
          'full_consultation_latency_measured':args.finish_confirmed,'rows':rows})
    print(f'Recorded {sum(r["status"]=="recorded" for r in rows)} model cases; semantic quality requires review.')


if __name__=='__main__':main()
