"""Rebuild the A/B report from saved real outputs. Makes no API requests."""
from pathlib import Path
import csv,hashlib,html,json,shutil,statistics
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,PageBreak
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'docs/counseling/ab-20261107';WORK=ROOT.parents[2]
load=lambda name:json.loads((OUT/name).read_text(encoding='utf-8'))
R=load('raw-results.json');M=load('metrics.json');S=load('unblinded-scores.json')['rows']
C=json.loads((ROOT/'evals/counseling-ab-20261107.json').read_text(encoding='utf-8'))['cases']
I=load('inputs.json')['contexts'];B=M['variants']['before'];A=M['variants']['after']
assert len(R['rows'])==48 and R['status']=='completed' and len(S)==48
assert hashlib.sha256((OUT/'manual-blind-scores.json').read_bytes()).hexdigest()==M['frozen_blind_scores_sha256']
for v,d in [('before',B),('after',A)]:
 rows=[r for r in R['rows'] if r['variant']==v]
 assert abs(sum(r['estimated_usd'] for r in rows)-d['overall']['estimated_usd'])<1e-8
 assert sum(r['usage']['input_tokens'] for r in rows)==d['overall']['input_tokens']
 assert abs(statistics.mean(r['quality_points'] for r in S if r['variant']==v)-d['quality_mean'])<.001
names={'AB01':'기여 인정','AB02':'친구의 취소','AB03':'계약 종료·경청','AB04':'오해 정정','AB05':'모호한 별칭','AB06':'개인사전·진행','AB07':'합격·불합격 친구','AB08':'가족 돌봄','AB09':'약속과 휴식','AB10':'질문 없는 경청','AB11':'면접·예측 요청','AB12':'이전 조언 실패'}
row=lambda cid,phase,v:next(r for r in R['rows'] if (r['case_id'],r['phase'],r['variant'])==(cid,phase,v))
score=lambda cid,phase,v:next(r for r in S if (r['case_id'],r['phase'],r['variant'])==(cid,phase,v))
money=lambda x:'US$'+format(x,'.7f')
sec=lambda x:format(x/1000,'.2f')+'s'
metrics=[
 ['Codex AI 평가 /100','74.38','78.96','+4.58점'],
 ['최종 상담 품질 /100','66.67','74.58','+7.92점'],
 ['출력 계약 통과','23/24','24/24','+1건'],
 ['행동 배열 정확히 1개','4/10','10/10','+6건'],
 ['평균 모델 호출',sec(B['overall']['mean_ms']),sec(A['overall']['mean_ms']),'+0.5% 느림'],
 ['평균 최종 상담',sec(B['stages']['compose']['mean_ms']),sec(A['stages']['compose']['mean_ms']),'+16.0% 느림'],
 ['24호출 비용·캐시 반영',money(B['overall']['estimated_usd']),money(A['overall']['estimated_usd']),'-1.65%'],
 ['24호출 비용·캐시 제외',money(B['overall']['estimated_usd_without_cache']),money(A['overall']['estimated_usd_without_cache']),'+2.86%']]
cases=[[x['case_id']+' '+names[x['case_id']],format(x['before']['quality_points'],'.1f'),format(x['after']['quality_points'],'.1f'),format(x['delta_points'],'+.1f')] for x in M['case_scores']]
checks=[['제공 카드 ID·순서','10/10','10/10'],['일상 고민 검색 불필요 판단','12/12','12/12'],['명시적 경청 의도 분류','2/2','2/2'],['모호한 개인 용어 확인','1/1','1/1'],['경청 후속 질문 없음','1/2','2/2'],['현재 발화의 정의 인용 정확','0/1','0/1']]
tokens=[
 ['입력 토큰','39,794','46,896','+17.85%'],['캐시 입력','8,064','15,872','조건 차이'],
 ['청구 출력 토큰','14,812','14,491','-2.17%'],['그중 reasoning','6,784','7,616','중복 가산 없음'],
 ['평균 이해 확인','6.72s','5.65s','-15.99%'],['최종 상담 p95','9.84s','11.90s','+20.88%']]
method='동일 모델 gpt-5-mini-2025-08-07, reasoning low, 동일 입력·RAG·카드·스키마를 사용했습니다. Python SystemRandom으로 사례별 카드를 한 번 뽑아 두 그룹에 똑같이 제공했습니다. 이해 확인과 최종 응답을 독립 비교했고, 최종 단계에는 동일한 수동 확인 맥락을 넣었습니다. 앱의 종단간 대화·DB·화면 성능 실험은 아닙니다.'
limit='12개 합성 사례, 각 단계·버전당 한 번 생성한 탐색 비교입니다. 프롬프트를 작성한 Codex AI가 X/Y를 가리고 채점표를 고정한 뒤 매핑을 열었습니다. 별도 인간·전문상담가 평가는 없었습니다. 독립 평가·모집단 정확도·임상 효과나 통계적으로 확정된 향상률을 뜻하지 않습니다.'
costnote='실제 사용 토큰에 확인일의 표준 단가를 적용했습니다. 100만 토큰당 입력 US$0.25 / 캐시 입력 US$0.025 / 출력 US$2.00입니다. reasoning은 output_tokens에 이미 포함돼 다시 더하지 않았습니다. 세금·계정 계약·웹 검색·음성·서버 비용은 제외한 청구 추정치입니다.'
judgments={
 'AB07':'기존은 불합격한 친구에게 축하하라고 제안해 주체를 혼동했습니다. 변경 후 이 오류는 줄었지만, 합격을 미안하다고 사과하는 문구는 여전히 죄책감을 강화할 수 있습니다.',
 'AB03':'기존은 경청 요청 뒤에 다시 질문했습니다. 변경 후 질문을 붙이지 않았습니다. 전체 사례 점수는 이해 단계의 차이까지 합쳐 동점입니다.',
 'AB06':'하락 사례입니다. 변경 후 공감 필드에 질문이 섞이고 요약이 메모체가 됐습니다. 두 버전 모두 사전 문장을 현재 사용자가 직접 말한 정의처럼 인용했습니다. 기존 서버 검사가 잘못된 인용의 기억 저장을 차단합니다.',
 'AB10':'기존의 읽을 수 있는 공감은 평가했지만 advice가 공백이라 공통 출력 검증에서 실패했습니다. 실패를 제외하거나 다시 생성하지 않았습니다.'}
def response_text(value,phase,full=False):
 if not isinstance(value,dict):return str(value or '읽을 수 있는 출력 없음')
 labels=[('acknowledgement','공감'),('understanding','이해'),('clarification_question','질문')] if phase=='analyze' else [('empathy','공감'),('advice','응답·조언'),('actions','행동'),('follow_up_question','후속 질문')]
 parts=[]
 for key,label in labels:
  v=value.get(key)
  if v is None or v==[]:continue
  if isinstance(v,list):v=' / '.join(v)
  parts.append(label+': '+(str(v) if str(v).strip() else '[공백]'))
 if full and phase=='compose':parts[1:1]=['카드 '+str(t['card_id'])+': '+t['reflection'] for t in value.get('tarot',[])]
 return '\n'.join(parts)
def mdtable(headers,rows):
 esc=lambda x:str(x).replace('|','/').replace('\n','<br>')
 return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join('---' for _ in headers)+' |']+['| '+' | '.join(esc(x) for x in r)+' |' for r in rows])
md=['# 여운 | 실제 GPT 패턴 전·후 A/B 평가','','2026-11-07 · 실제 API 48회 · 합성 상담 12사례 · 부하 테스트 없음','',
 '## 결론','','Codex AI의 종합 품질 평가는 **74.38→78.96점(+4.58점)**, 최종 상담은 **66.67→74.58점(+7.92점)**입니다. 사례별 개선 6개·동점 5개·하락 1개입니다. 감정 반영은 3.000→2.958/4로 좋아지지 않았습니다.','',
 '평균 호출 속도는 개선되지 않았고 최종 상담은 16.0% 느려졌습니다. 비용은 캐시 반영 시 1.65% 낮았지만 캐시 제외 시 2.86% 높았습니다. 보편적인 속도·비용 개선을 입증한 결과는 아닙니다.','',mdtable(['지표','전','후','차이'],metrics),'',
 '**총 48회 토큰 비용 추정: US$0.0748929.**','',
 '## 실험 조건','',method,'',limit,'',
 '패턴 전: f93bb9e / 패턴 후: 4e80d4b. 두 버전 모두 기존 RAG를 사용합니다. 프롬프트 전략·계약 변경을 비교하며 RAG 유무의 비교가 아닙니다. 호출 순서를 교차했고 재생성·실패 제거·추가 모델 심판 호출은 없었습니다.','',
 '## 출력 정확도와 품질 평가','',mdtable(['공통 기준','전','후'],checks),'',
 'actions 필드 한 개는 본문 전체의 행동 한 개를 뜻하지 않습니다. 조언·카드에 추가 과제가 남습니다. 응답 모드 사전 기대값 일치는 5/12→8/12였지만 조언을 명시하지 않은 고민에는 해석 여지가 있어 정답 정확도로 부르지 않습니다.','',
 '품질은 감정 반영·사연 이해·선택권·근거·간결함을 각 0~4점으로 채점한 합/20×100입니다. 공감 축은 개선되지 않았고 주로 사연 이해·선택권·간결함 점수가 올랐습니다. [사전 기준](rubric.json) · [매핑을 열기 전 고정 점수](manual-blind-scores.json) · [전후 매핑·점수](unblinded-scores.json)','',
 '## 전체 사례','',mdtable(['사례','전 /100','후 /100','변화'],cases),'',
 '## 실제 원문 비교','']
for cid,phase in [('AB07','compose'),('AB03','compose'),('AB06','analyze'),('AB10','compose')]:
 c=next(c for c in C if c['id']==cid);md+=['### '+cid+' '+names[cid],'','> '+c['input'],'']
 for v,label in [('before','패턴 전'),('after','패턴 후')]:
  rr=row(cid,phase,v);ss=score(cid,phase,v)
  md+=['**'+label+' · '+format(ss['quality_points'],'.0f')+'/100 · 계약 '+('통과' if rr['delivered'] else '차단')+'**','']
  md+=['> '+line for line in response_text(rr.get('raw_output'),phase).splitlines()]+['']
 md+=['Codex AI 평가: '+judgments[cid],'']
md+=['## 토큰·시간·비용','',mdtable(['항목','전','후','해석'],tokens),'',costnote,'',
 '실제 캐시 입력량이 달랐습니다. 2호출을 한 상담으로 가정한 1,000건 환산은 캐시 반영 시 약 US$3.15→US$3.09, 캐시 없이는 약 US$3.30→US$3.39입니다. 실제 계약·청구서의 금액은 아닙니다. [공식 모델·단가](https://developers.openai.com/api/docs/models/gpt-5-mini)','',
 '## 남은 문제와 다음 수정 후보','',
 '1. 주체·시간 보존: AB07 주체 혼동, AB09 두 버전 모두 주말을 오늘로 바꾸는 오류가 있었습니다.',
 '2. 사전/현재 발화의 출처 분리: AB06에서 두 버전 모두 인용 추출을 틀렸습니다.',
 '3. 배열뿐 아니라 본문·카드에 숨은 여러 과제도 줄여야 합니다.',
 '4. 마이너 카드 56/78개는 키워드 문장을 제외하면 4개 설명 템플릿입니다. 비슷한 조언을 반복하게 하는 입력 데이터 제약입니다.',
 '5. 고정된 15~20분·시간표를 제안하기 전에 내담자의 여건을 확인해야 합니다.',
 '6. 다음 프롬프트 조정 뒤에는 새 사례·반복 생성·독립 검토가 필요합니다. 이번 결과를 보고 프롬프트를 고치거나 같은 문제를 재생성하지 않았습니다.','',
 '## 재현·근거','',
 '- [실제 48회 원본·토큰·응답 ID](raw-results.json) · [실험 조건](plan.json) · [동일 입력·카드](inputs.json)',
 '- [전체 수치](metrics.json) · [호출 CSV](calls.csv) · [사례 점수 CSV](case-scores.csv)',
 '- [원문 비교 화면](COMPARISON.html) · [비교 PDF](REPORT.pdf)',
 '- 평가 준비: python tools/evaluate_counseling_ab.py --dry-run. 실제 실행은 별도 키 입력이 필요하며 이미 저장된 결과는 자동 덮어쓰지 않습니다.',
 '- 보고서 재생성: ReportLab·pypdf가 설치된 Python으로 python tools/report_counseling_ab.py. 추가 API 호출은 없습니다.',
 '- [공식 평가 가이드](https://developers.openai.com/api/docs/guides/evaluation-best-practices)','',
 '이번 요청에서 제품 코드·기존 테스트·운영 서버 키 설정은 변경하지 않았습니다. 승인된 키는 평가 프로세스 메모리에서만 사용하고 종료했습니다. 가중치 학습·부하 테스트도 실행하지 않았습니다.']
(OUT/'README.md').write_text('\n'.join(md)+'\n',encoding='utf-8')
with (OUT/'calls.csv').open('w',newline='',encoding='utf-8-sig') as f:
 w=csv.writer(f);w.writerow(['case_id','phase','variant','http','contract_passed','ms','input_tokens','cached_tokens','output_tokens','estimated_usd'])
 for r in R['rows']:w.writerow([r['case_id'],r['phase'],r['variant'],r['http_status'],r['delivered'],r['method_ms'],r['usage']['input_tokens'],r['usage'].get('input_tokens_details',{}).get('cached_tokens',0),r['usage']['output_tokens'],r['estimated_usd']])
with (OUT/'case-scores.csv').open('w',newline='',encoding='utf-8-sig') as f:
 w=csv.writer(f);w.writerow(['case','before','after','delta'])
 for r in M['case_scores']:w.writerow([r['case_id'],r['before']['quality_points'],r['after']['quality_points'],r['delta_points']])
# Full exact text comparison, selectable by case and stage, no network dependencies.
e=html.escape
pieces=['<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>여운 실제 GPT A/B 비교</title><style>',
':root{font-family:Malgun Gothic,Arial,sans-serif;color:#273c34;background:#f8f8f2}*{box-sizing:border-box}body{margin:0;padding:28px 18px}main{max-width:1160px;margin:auto}h1{font-size:27px}p{line-height:1.75}small{color:#68796f}.controls{display:flex;gap:16px;flex-wrap:wrap;margin:20px 0}label{display:grid;gap:7px}select{font:inherit;padding:12px;border-radius:10px;border:1px solid #a9c1ad;background:#fffdf8;max-width:100%}.pair{display:grid;grid-template-columns:1fr 1fr;gap:22px}.panel{background:#fffdf8;padding:22px;border-top:5px solid #a4c2aa;border-radius:14px;min-width:0}.after{border-color:#426a57}.answer{white-space:pre-wrap;word-break:break-word;line-height:1.85}.note{border-top:1px solid #d9e3d9;padding-top:13px;color:#56695c;line-height:1.7}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.stat{background:#e8f0e7;padding:17px;border-radius:14px}.stat strong{font-size:22px;display:block;margin:8px 0}.measure{color:#426a57;font-size:14px;line-height:1.7}pre{white-space:pre-wrap;word-break:break-word}details{margin-top:18px}summary{cursor:pointer}footer{padding:24px 0;font-size:13px;color:#68796f}@media(max-width:740px){.pair{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}h1{font-size:23px}}@media print{.controls{display:none}.panel{break-inside:avoid}}',
'</style><main><small>2026-11-07 · 실제 API 48회 · 합성 12사례</small><h1>같은 고민, 패턴 전·후의 실제 답변</h1><p>Codex AI가 X/Y를 가리고 채점했습니다. 별도 사람 평가는 없었습니다. 독립·임상 평가가 아니며 품질 점수는 정답 정확도가 아닙니다. 모델·RAG·카드를 고정하고 이해 확인과 최종 응답을 독립 비교했습니다.</p>',
'<div class="stats"><div class="stat">품질 평가 /100<strong>74.4 → 79.0</strong><small>+4.6점 · 작성자 평가</small></div><div class="stat">평균 호출시간<strong>6.93 → 6.97s</strong><small>속도 개선 관찰 없음</small></div><div class="stat">24회 캐시 반영 비용<strong>$0.0378 → $0.0371</strong><small>-1.65% · 캐시 없으면 +2.86%</small></div><div class="stat">출력 계약 통과<strong>23/24 → 24/24</strong><small>실제 화면 전송 실험은 아님</small></div></div>',
'<div class="controls"><label>상담 사례<select id="case">']
for c in C:pieces.append('<option value="'+c['id']+'">'+e(c['id']+' · '+names[c['id']])+'</option>')
pieces+=['</select></label><label>단계<select id="phase"><option value="compose">확인 후 상담 결과</option><option value="analyze">공감·이해 확인</option></select></label></div><div id="results" aria-live="polite">']
for c in C:
 for phase in ('analyze','compose'):
  pieces.append('<section data-case="'+c['id']+'" data-phase="'+phase+'"><h3>'+e(c['input'])+'</h3><div class="pair">')
  for v,label in [('before','패턴 전'),('after','패턴 후')]:
   rr=row(c['id'],phase,v);ss=score(c['id'],phase,v)
   measure='품질 '+format(ss['quality_points'],'.0f')+'/100 · '+sec(rr['method_ms'])+' · '+money(rr['estimated_usd'])+' · 계약 '+('통과' if rr['delivered'] else '차단(원문 표시)')
   pieces+=['<article class="panel '+('after' if v=='after' else '')+'"><h2>'+label+'</h2><div class="measure">'+e(measure)+'</div><p class="answer">'+e(response_text(rr.get('raw_output'),phase,True))+'</p><div class="note">Codex AI 평가: '+e(ss['reason'])+'</div><details><summary>응답 JSON</summary><pre>'+e(json.dumps(rr.get('raw_output'),ensure_ascii=False,indent=2))+'</pre></details></article>']
  pieces.append('</div></section>')
pieces+=['</div><footer>실패·차단 응답도 그대로 포함했습니다. 금액은 실제 토큰에 표준 단가를 적용한 추정치입니다. 공감 표현 자체의 점수는 개선되지 않았습니다. <a href="README.md">분석 원문</a> · <a href="REPORT.pdf">PDF</a> · <a href="raw-results.json">48회 원본</a></footer>',
'<script>const c=document.getElementById("case"),p=document.getElementById("phase");function render(){for(const n of document.querySelectorAll("[data-case]"))n.hidden=n.dataset.case!==c.value||n.dataset.phase!==p.value;}c.addEventListener("change",render);p.addEventListener("change",render);render();</script></main></html>']
(OUT/'COMPARISON.html').write_text(''.join(pieces),encoding='utf-8')

pdfmetrics.registerFont(TTFont('KR','C:/Windows/Fonts/malgun.ttf'));pdfmetrics.registerFont(TTFont('KRB','C:/Windows/Fonts/malgunbd.ttf'))
pdfmetrics.registerFontFamily('KR',normal='KR',bold='KRB')
W,H=A4;CW=W-80
INK=colors.HexColor('#273C34');GREEN=colors.HexColor('#426A57');PALE=colors.HexColor('#EDF4ED');LINE=colors.HexColor('#D7E2D9')
styles={k:ParagraphStyle(k,fontName='KRB' if k in ('title','sub','head') else 'KR',fontSize=size,leading=leading,textColor=colors.white if k=='head' else GREEN if k=='sub' else INK,spaceAfter=space,wordWrap='CJK',keepWithNext=k in ('title','sub')) for k,size,leading,space in [('title',21,29,11),('sub',12,18,8),('p',9.2,14,7),('small',8.1,12,6),('cell',8.5,12.3,0),('head',8.5,12.3,0),('quote',8.7,13.2,5)]}
def p(s,k='p'):return Paragraph(html.escape(str(s)).replace('\n','<br/>'),styles[k])
def tab(headers,rows,widths,pad=5):
 vals=[[p(x,'head') for x in headers]]+[[p(x,'cell') for x in r] for r in rows]
 t=Table(vals,colWidths=[CW*x for x in widths],repeatRows=1,hAlign='LEFT')
 t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),GREEN),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,PALE]),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),pad),('BOTTOMPADDING',(0,0),(-1,-1),pad),('LINEBELOW',(0,0),(-1,-1),.35,LINE)]))
 return t
def pair(cid,phase):
 cells=[]
 for v,label in [('before','패턴 전'),('after','패턴 후')]:
  rr=row(cid,phase,v);ss=score(cid,phase,v)
  cells.append([p(label+' · '+format(ss['quality_points'],'.0f')+'/100','sub'),p(response_text(rr.get('raw_output'),phase),'quote')])
 t=Table([[cells[0],cells[1]]],colWidths=[CW/2,CW/2])
 t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(1,0),(1,0),PALE),('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
 return t
def frame(c,doc):
 c.saveState();c.setFillColor(colors.HexColor('#FCFCF8'));c.rect(0,0,W,H,fill=1,stroke=0);c.setFillColor(GREEN);c.setFont('KRB',8);c.drawString(40,H-27,'YE OUN / REAL API A-B EVALUATION');c.setStrokeColor(LINE);c.line(40,38,W-40,38);c.setFillColor(INK);c.setFont('KR',7.3);c.drawString(40,24,'2026-11-07 · 12 합성 사례 · 실제 API 48회 · 작성자 평가');c.drawRightString(W-40,24,format(doc.page,'02d')+' / 04');c.restoreState()
story=[]
def add(s,k='p'):story.append(p(s,k))
def table(h,r,w,pad=5):story.extend([tab(h,r,w,pad),Spacer(1,8)])
add('실제 GPT: 패턴 전·후 비교','title')
add('모의 응답이 아닌 실제 OpenAI API 결과입니다. 12사례 × 이해 확인·최종 응답 × 두 버전 = 48회. 총 토큰 비용 추정은 US$0.0748929입니다.')
table(['지표','패턴 전','패턴 후','관찰'],metrics,[.34,.22,.22,.22],6)
add('효과와 한계','sub')
add('종합 품질 +4.58점 / 최종 상담 +7.92점. 사례별 개선 6개·동점 5개·하락 1개입니다. 공감 축은 3.000→2.958/4로 좋아지지 않았습니다. 품질 점수의 차이는 정답 정확도 향상률이 아닙니다.')
add(method,'small');add(limit,'small')
story.append(PageBreak())
add('정확도와 전체 사례','title')
table(['공통 검사','전','후'],checks,[.60,.20,.20],5)
add('12사례 모두 공개: 이해·최종 응답 평균','sub')
table(['사례','전 /100','후 /100','차이'],cases,[.48,.17,.17,.18],4)
add('감정 반영·사연 이해·선택권·근거·간결함을 각 0~4점, 합/20×100으로 평가했습니다. X/Y 점수를 먼저 고정한 뒤 매핑을 열었습니다. Codex AI의 단독 평가이며 독립적인 사람 평가와 구분합니다.','small')
add('actions 한 개는 본문 전체의 행동 한 개가 아닙니다. 응답 모드 기대값 일치는 5/12→8/12였으나 명시적 조언 요청이 없는 문항에는 해석 여지가 있어 정답 정확도로 부르지 않습니다. 두 버전 모두 기존 RAG를 사용합니다.','small')
story.append(PageBreak())
add('실제 원문: 좋아진 부분','title')
add('AB07 · 나는 합격, 친구는 불합격','sub')
add(next(c['input'] for c in C if c['id']=='AB07'),'small');story.append(pair('AB07','compose'));add(judgments['AB07'],'small')
add('AB03 · 해결책보다 경청을 요청','sub');story.append(pair('AB03','compose'));add(judgments['AB03'],'small')
add('공감·조언·행동·질문 필드를 원문 그대로 발췌했습니다. 카드별 전체 문장과 12사례의 전체 출력은 비교 화면·JSON에 있습니다.','small')
story.append(PageBreak())
add('회귀 사례와 비용의 실제 차이','title')
add('AB06 · 개인사전의 등산 모임에서 첫 진행','sub');story.append(pair('AB06','analyze'));add('종합 70.0→67.5로 하락. '+judgments['AB06'],'small')
table(['항목','전','후','해석'],tokens,[.30,.22,.22,.26],5)
add(costnote,'small')
add('캐시 입력량이 달라 실제 캐시 기준 -1.65%를 고유한 절감 효과로 일반화하지 않습니다. 캐시가 없으면 비용은 +2.86%입니다. 2호출을 한 상담으로 가정한 1,000건 환산은 캐시 반영 약 US$3.15→US$3.09, 캐시 제외 약 US$3.30→US$3.39입니다.','small')
add('다음 개선 후보','sub')
add('주체·시간 보존, 사전/현재 발화 출처 구분, 본문·카드에 숨은 여러 과제 축소를 우선 검토할 필요가 있습니다. 마이너 56장의 기본 뜻이 4개 템플릿인 입력 데이터 제약도 남습니다. 이번 결과를 본 뒤 제품·프롬프트·기존 테스트를 바꾸거나 실패를 재생성하지 않았습니다.','small')
for label,url in [('공식 모델·단가','https://developers.openai.com/api/docs/models/gpt-5-mini'),('48회 원본·전체 채점·비교 화면','https://github.com/tarobo22/taro-back/tree/codex/counseling-rag-20261107/docs/counseling/ab-20261107')]:
 story.append(Paragraph('<link href="'+html.escape(url,quote=True)+'" color="#426A57"><u>'+html.escape(label)+'</u></link>',styles['small']))
dest=WORK/'output/pdf';dest.mkdir(parents=True,exist_ok=True);pdf=dest/'yeoun-real-ai-ab-20261107.pdf'
SimpleDocTemplate(str(pdf),pagesize=A4,leftMargin=40,rightMargin=40,topMargin=50,bottomMargin=50,title='여운 실제 GPT 패턴 전후 품질·비용 평가',author='여운 프로젝트').build(story,onFirstPage=frame,onLaterPages=frame)
n=len(PdfReader(pdf).pages)
if n!=4:raise RuntimeError('Expected 4 pages, got '+str(n))
shutil.copy2(pdf,OUT/'REPORT.pdf')
print(json.dumps({'pdf_pages':n,'pdf_bytes':pdf.stat().st_size,'html_bytes':(OUT/'COMPARISON.html').stat().st_size,'case_count':len(C)}))

