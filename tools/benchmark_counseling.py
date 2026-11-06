"""Application+PostgreSQL overhead with a clearly labelled synthetic provider.

No OpenAI calls, real web searches or production databases are used.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID,uuid4

sys.path.insert(0,str(Path(__file__).parents[1]))
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from api.adapters.ai_jobs_postgres import PostgresAIJobsUnitOfWork
from api.adapters.counseling_postgres import PostgresCounselingStore
from api.application.ai_jobs import AIJobsService
from api.application.counseling import CounselingService
from api.domain.counseling import Analysis


class SyntheticModel:
    def available(self):return True
    def analyze(self,payload):
        time.sleep(.01)
        return Analysis(acknowledgement='함께 챙길 일이 많아 부담되셨겠어요.',
            understanding='지금 감당할 수 있는 일을 함께 살펴보고 싶다는 거죠?',
            clarification_question=None,explicit_definitions=[],
            unknown_terms=[{'term':'거절 민감성','kind':'public'}] if '거절 민감성' in payload['text'] else [],search_purpose='clarify_counseling_term' if '거절 민감성' in payload['text'] else 'none').model_dump()
    def search(self,query):
        time.sleep(.01)
        return [{'id':'web:synthetic','kind':'web','title':'합성 근거 · 실제 검색 아님',
            'url':'https://www.who.int/','text':'성능 측정용 합성 근거입니다.'}]
    def compose(self,payload):
        time.sleep(.01)
        web=[s for s in payload['sources'] if s['kind']=='web']
        return {'empathy':'마음을 들려주셔서 고마워요.',
            'facts':[{'text':'성능 측정용 합성 근거예요.','source_ids':[web[0]['id']]}] if web else [],
            'tarot':[{'card_id':c['card_id'],'reflection':'내 속도를 돌아보는 관점이에요.'} for c in payload['cards']],
            'advice':'오늘 할 수 있는 작은 일부터 생각해봐요.','actions':['작은 일 하나를 적어보세요.'],
            'follow_up_question':'어떤 일부터 해볼까요?','safety_mode':'standard'}


def percentile(values,p):
    a=sorted(values);x=(len(a)-1)*p;i=int(x);return round(a[i]+(a[min(i+1,len(a)-1)]-a[i])*(x-i),2)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--samples',type=int,default=50)
    ap.add_argument('--out',default='.local/counseling-benchmark.json');args=ap.parse_args()
    dsn=os.environ.get('YEOUN_TEST_DATABASE_URL','')
    if not conninfo_to_dict(dsn).get('dbname','').endswith('_test'):raise SystemExit('Use a dedicated database ending in _test.')
    root=Path(__file__).parents[1]
    with ConnectionPool(dsn,min_size=1,max_size=12,kwargs={'row_factory':dict_row}) as pool:
        with pool.connection() as c:
            c.execute((root/'api/schema.sql').read_text(encoding='utf-8'))
            for p in sorted((root/'api/migrations').glob('*.sql')):c.execute(p.read_text(encoding='utf-8'))
        s=CounselingService(PostgresCounselingStore(pool),SyntheticModel(),
            json.loads((root/'shared/data-catalog.json').read_text(encoding='utf-8'))['cards'],
            policy={'owner_daily_limit':10000,'global_daily_limit':1000000})
        ai=AIJobsService(lambda:PostgresAIJobsUnitOfWork(pool));owners=[]
        def wait(owner,sid):
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                v=s.get(owner,sid)
                if v['job']['state']=='succeeded':return v
                if v['job']['state']=='failed':raise RuntimeError(v['job']['error_code'])
                if not s.run_next():time.sleep(.002)
            raise TimeoutError('Synthetic benchmark deadline exceeded')
        def flow(i):
            owner=uuid4();owners.append(owner)
            with pool.connection() as c:c.execute('INSERT INTO app_sessions(id,token_hash) VALUES(%s,%s)',(owner,str(uuid4())))
            consent=ai.record_consent(owner,uuid4(),'ai_processing','2026-11-05',True)['id']
            profile=['plain','dictionary','term_reference_stub'][i%3]
            if profile=='dictionary':s.define(owner,'반짝','본인의 독서모임')
            text={'plain':'학생인데 수업과 알바가 부담돼요.','dictionary':'반짝에서 발표를 맡았어요.','term_reference_stub':'거절 민감성이라는 말을 들었는데, 거절당할까 봐 걱정돼요.'}[profile]
            start=time.perf_counter();v=s.create(owner,uuid4());sid=UUID(v['id'])
            s.enqueue(owner,sid,uuid4(),'message',{'text':text,'expected_version':v['version'],'consent_event_id':consent})
            v=wait(owner,sid);confirmation_ms=(time.perf_counter()-start)*1000
            assert not v['cards']
            s.enqueue(owner,sid,uuid4(),'confirm',{'confirmed':True,'expected_version':v['version'],'consent_event_id':consent,'web_search':True})
            v=wait(owner,sid)
            assert len(v['cards'])==3 and len({c['card_id'] for c in v['cards']})==3
            assert all('B_PRIVATE_ONLY' not in h['text'] for h in v['history'])
            return {'profile':profile,'confirmation_ms':round(confirmation_ms,2),'complete_ms':round((time.perf_counter()-start)*1000,2)}
        groups=[]
        try:
            for concurrency in (1,5,10):
                start=time.monotonic()
                with ThreadPoolExecutor(max_workers=concurrency) as workers:rows=list(workers.map(flow,range(args.samples)))
                values=[r['complete_ms'] for r in rows]
                groups.append({'concurrency':concurrency,'samples':len(rows),'seconds':round(time.monotonic()-start,2),
                    'p50_ms':percentile(values,.5),'p95_ms':percentile(values,.95),'p99_ms':percentile(values,.99),'rows':rows})
                print(json.dumps({k:v for k,v in groups[-1].items() if k!='rows'}),flush=True)
        finally:
            with pool.connection() as c:
                c.execute('DELETE FROM p0_ai_usage_attempts WHERE owner_id=ANY(%s)',(owners,))
                c.execute('DELETE FROM app_sessions WHERE id=ANY(%s)',(owners,))
        report={'provider':'synthetic-fixed-10ms','openai_calls':0,'real_web_searches':0,
            'database_pool_max':12,'production_limits_overridden':True,'measures':'Application + PostgreSQL + synthetic 10ms provider; not real GPT latency or language quality',
            'groups':groups}
        out=Path(args.out);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
