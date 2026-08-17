from __future__ import annotations
import json, os, re, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'/'papers.json'
METRICS=ROOT/'data'/'journal_metrics.json'
BERLIN=ZoneInfo('Europe/Berlin')
TOPICS={
 'organic':["electrochemical carboxylation CO2", "electrocarboxylation CO2", "diene CO2 nickel carboxylation"],
 'gde':["gas diffusion electrode CO2 micro CT", "X-ray tomography gas diffusion electrode", "GDE flooding wetting CO2 electrolysis"],
 'analysis':["cyclic voltammetry coupled chemical reaction kinetics", "non aqueous reference electrode DMF electrochemistry", "electrochemical impedance spectroscopy porous electrode"]
}
KEYWORDS={
 'organic':{'electrocarboxylation':18,'carboxylation':12,'co2':9,'nickel':9,'ni ':6,'diene':9,'dmf':7,'electrosynthesis':6},
 'gde':{'gas diffusion':15,'gde':12,'micro-ct':15,'micro ct':15,'tomography':12,'x-ray ct':12,'flooding':12,'wetting':10,'porous':5},
 'analysis':{'cyclic voltammetry':13,'voltammetry':9,'kinetic':7,'impedance':9,'eis':7,'reference electrode':8,'dmf':5,'mechanism':6}
}

def abstract(inv):
    if not inv:return ''
    words=[]
    for w,positions in inv.items():
        for p in positions: words.append((p,w))
    return ' '.join(w for _,w in sorted(words))

def score(topic,text):
    s=55
    low=' '+text.lower()+' '
    for k,v in KEYWORDS[topic].items():
        if k in low:s+=v
    if 'review' in low:s+=3
    return min(99,s)

def fetch(topic,query,date_from,date_to):
    params={'search':query,'filter':f'from_publication_date:{date_from},to_publication_date:{date_to},is_paratext:false','sort':'publication_date:desc','per-page':35}
    mail=os.getenv('OPENALEX_MAILTO')
    if mail:params['mailto']=mail
    r=requests.get('https://api.openalex.org/works',params=params,timeout=30);r.raise_for_status()
    out=[]
    for w in r.json().get('results',[]):
        title=w.get('title') or ''
        abst=abstract(w.get('abstract_inverted_index'))
        text=title+' '+abst
        sc=score(topic,text)
        if sc<68:continue
        loc=w.get('primary_location') or {}
        src=(loc.get('source') or {}).get('display_name') or 'Unknown journal'
        doi=w.get('doi')
        url=doi or loc.get('landing_page_url') or w.get('id')
        out.append({'id':w.get('id','').split('/')[-1] or re.sub(r'\W+','-',title.lower())[:60], 'topic':topic,'badge':'最新研究','read_minutes':10,'title':title,'journal':src,'year':w.get('publication_year'),'summary_zh':'','abstract':abst[:1800],'relevance_reason':'','tags':[],'score':sc,'url':url,'archive':False})
    return out

def enrich_llm(papers):
    key=os.getenv('OPENAI_API_KEY')
    if not key:return papers
    from openai import OpenAI
    client=OpenAI(api_key=key)
    model=os.getenv('OPENAI_MODEL','gpt-5.6')
    for p in sorted(papers,key=lambda x:x['score'],reverse=True)[:8]:
        prompt=f'''你是电化学文献雷达。根据标题和摘要，仅输出 JSON：summary_zh(60-100字中文摘要)、relevance_reason(40-80字，说明与 Ni/DMF/CO2 electrocarboxylation、GDE/Micro-CT 或电化学分析的关系)、tags(3个短标签)。\n标题：{p['title']}\n摘要：{p.get('abstract','')[:5000]}'''
        try:
            resp=client.responses.create(model=model,input=prompt)
            txt=resp.output_text.strip(); txt=re.sub(r'^```json|```$','',txt).strip(); obj=json.loads(txt)
            p['summary_zh']=obj.get('summary_zh','');p['relevance_reason']=obj.get('relevance_reason','');p['tags']=obj.get('tags',[])[:4]
        except Exception as e: print('LLM enrich failed:',e,file=sys.stderr)
        time.sleep(.2)
    return papers

def fallback_text(p):
    if not p['summary_zh']:
        p['summary_zh']=f"最新研究聚焦于 {p['title']}。已抓取题目、期刊和摘要信息；如配置 OPENAI_API_KEY，可自动生成中文摘要与研究关联解读。"
    if not p['relevance_reason']:
        p['relevance_reason']='该论文与当前主题关键词高度匹配，建议结合摘要与实验方法进一步判断是否纳入精读。'
    if not p['tags']:
        p['tags']=[p['topic'], 'latest', 'screened']

def main():
    now=datetime.now(BERLIN)
    if os.getenv('GITHUB_EVENT_NAME')=='schedule' and not (now.weekday()<5 and now.hour==8):
        print('Not Berlin 08:xx; skip DST duplicate cron.');return
    old=json.loads(DATA.read_text(encoding='utf-8')) if DATA.exists() else {'papers':[]}
    metrics=json.loads(METRICS.read_text(encoding='utf-8')) if METRICS.exists() else {}
    for p in old.get('papers',[]): p['archive']=True
    start=(now.date()-timedelta(days=14)).isoformat(); end=now.date().isoformat()
    found=[]
    for topic,queries in TOPICS.items():
        for q in queries:
            try:found.extend(fetch(topic,q,start,end))
            except Exception as e:print('fetch failed',topic,q,e,file=sys.stderr)
    dedup={}
    for p in sorted(found,key=lambda x:x['score'],reverse=True):
        key=p['url'] or p['title'].lower()
        if key not in dedup:dedup[key]=p
    fresh=list(dedup.values())[:24]
    enrich_llm(fresh)
    for p in fresh:
        fallback_text(p)
        m=metrics.get(p['journal'],{});p['impact_factor']=m.get('impact_factor');p['quartile']=m.get('quartile','Q —')
    existing_ids={p['id'] for p in fresh}
    archive=[p for p in old.get('papers',[]) if p.get('id') not in existing_ids][:180]
    payload={'brief_date':now.strftime('%Y · %m · %d'),'last_updated':now.date().isoformat(),'match_score':96,'trackers':old.get('trackers',[]),'papers':fresh+archive}
    DATA.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Updated {len(fresh)} fresh papers, {len(archive)} archived.')
if __name__=='__main__':main()
