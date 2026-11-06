#!/usr/bin/env python3
"""HTTP-only, crash-safe outcome controller for the assignment simulator."""
from __future__ import annotations

import argparse, copy, json, math, os, sqlite3, uuid
from datetime import datetime
from pathlib import Path

import httpx

TICK, MIN_DIALS, MIN_CONNECTED, EXPLORE, Z = 15, 300, 80, .20, 2.33


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS seen(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS cursor(kind TEXT PRIMARY KEY, chunk INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS totals(wid TEXT PRIMARY KEY,dials INT DEFAULT 0,connected INT DEFAULT 0,engaged INT DEFAULT 0,halluc INT DEFAULT 0,last_ts TEXT);
        CREATE TABLE IF NOT EXISTS arms(wid TEXT,cohort TEXT,dim TEXT,option TEXT,dials INT DEFAULT 0,connected INT DEFAULT 0,engaged INT DEFAULT 0,halluc INT DEFAULT 0,PRIMARY KEY(wid,cohort,dim,option));
        CREATE TABLE IF NOT EXISTS proposals(pid TEXT PRIMARY KEY,aid TEXT,wid TEXT,kind TEXT,base INT,status TEXT,cfg TEXT,created TEXT,updated TEXT);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,ts TEXT,wid TEXT,event TEXT,details TEXT);
        ''')
        self.db.commit()
    def log(self, ts, event, data, wid=None): self.db.execute('INSERT INTO audit(ts,wid,event,details) VALUES(?,?,?,?)',(ts,wid,event,json.dumps(data,sort_keys=True)))
    def get_cursor(self, kind):
        r=self.db.execute('SELECT chunk FROM cursor WHERE kind=?',(kind,)).fetchone(); return r[0] if r else -1
    def set_cursor(self, kind, chunk): self.db.execute('INSERT INTO cursor VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET chunk=excluded.chunk',(kind,chunk))
    def is_new(self, eid):
        try: self.db.execute('INSERT INTO seen VALUES(?)',(eid,)); return True
        except sqlite3.IntegrityError: return False
    def call(self,e):
        w=e['workflow_id']; con=int(e['connected']); eng=int(e['engaged']); hal=int(bool((e.get('qa') or {}).get('hallucination_flag')))
        self.db.execute('INSERT INTO totals(wid,dials,connected,engaged,halluc,last_ts) VALUES(?,?,?,?,?,?) ON CONFLICT(wid) DO UPDATE SET dials=dials+1,connected=connected+excluded.connected,engaged=engaged+excluded.engaged,halluc=halluc+excluded.halluc,last_ts=excluded.last_ts',(w,1,con,eng,hal,e['ts']))
        for d,o in e['variant'].items():
            if d=='prompt_version': continue
            self.db.execute('INSERT INTO arms(wid,cohort,dim,option,dials,connected,engaged,halluc) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(wid,cohort,dim,option) DO UPDATE SET dials=dials+1,connected=connected+excluded.connected,engaged=engaged+excluded.engaged,halluc=halluc+excluded.halluc',(w,e.get('cohort_id') or 'default',d,o,1,con,eng,hal))
    def total(self,w):
        r=self.db.execute('SELECT * FROM totals WHERE wid=?',(w,)).fetchone(); return dict(r) if r else dict(dials=0,connected=0,engaged=0,halluc=0)
    def arms_for(self,w,d): return [dict(r) for r in self.db.execute('SELECT * FROM arms WHERE wid=? AND cohort="default" AND dim=?',(w,d))]
    def active(self,w): return bool(self.db.execute("SELECT 1 FROM proposals WHERE wid=? AND status IN ('pending','approved')",(w,)).fetchone())
    def recently_proposed(self,w,ts,hours=24):
        rows=self.db.execute('SELECT created FROM proposals WHERE wid=? ORDER BY created DESC LIMIT 1',(w,)).fetchone()
        if not rows:return False
        try:
            then=datetime.fromisoformat(rows[0]); now=datetime.fromisoformat(ts)
            return (now-then).total_seconds() < hours*3600
        except (TypeError,ValueError):
            return False
    def pendings(self): return [dict(r) for r in self.db.execute("SELECT * FROM proposals WHERE status IN ('pending','approved')")]
    def save(self,pid,aid,w,kind,base,cfg,ts): self.db.execute('INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?)',(pid,aid,w,kind,base,'pending',json.dumps(cfg,sort_keys=True),ts,ts))
    def status(self,pid,status,ts): self.db.execute('UPDATE proposals SET status=?,updated=? WHERE pid=?',(status,ts,pid))


def r(x,n): return x/n if n else 0
def lo(x,n): return max(0,r(x,n)-Z*math.sqrt(max(.0001,r(x,n)*(1-r(x,n)))/max(1,n)))
def hi(x,n): return min(1,r(x,n)+Z*math.sqrt(max(.0001,r(x,n)*(1-r(x,n)))/max(1,n)))


class Controller:
    def __init__(self,url,state):
        self.c=httpx.Client(base_url=url.rstrip('/'),timeout=120); self.s=Store(state); self.w={}
    def get(self,path,**kw):
        q=self.c.get(path,**kw); q.raise_for_status(); return q.json()
    def request(self,method,path,body):
        q=getattr(self.c,method)(path,json=body); return (q.json(),q) if q.status_code<400 else (None,q)
    def ingest(self,ch,kind):
        q=self.c.get('/events/chunks/'+str(ch['chunk'])); q.raise_for_status()
        for line in q.text.splitlines():
            e=json.loads(line)
            if not self.s.is_new(e['event_id']): continue
            if e['type']=='call': self.s.call(e)
            elif e['type']=='config_changed':
                self.s.log(e['ts'],'config_changed',e,e.get('workflow_id'))
                if e.get('actor')=='supervisor':
                    for p in self.s.pendings():
                        if p['wid']==e['workflow_id']: self.s.status(p['pid'],'superseded',e['ts'])
                    self.s.log(e['ts'],'supervisor_edit_reconciled',{'version':e.get('version')},e['workflow_id'])
            elif e['type']!='lead': self.s.log(e.get('ts',''),e['type'],e,e.get('workflow_id'))
        self.s.set_cursor(kind,ch['chunk'])
    def bootstrap(self):
        self.w={x['workflow_id']:x for x in self.get('/workflows')}
        for ch in self.get('/events/chunks',params={'kind':'history','after':self.s.get_cursor('history')}): self.ingest(ch,'history')
        self.s.db.commit()
    def changed(self,a,b):
        out=[]
        for d in ('welcome_message','voice','caller_id'):
            if a['allocations']['default'][d]!=b['allocations']['default'][d]: out.append(d)
        if a.get('prompt_version')!=b.get('prompt_version'): out.append('prompt_version')
        if a.get('schedule')!=b.get('schedule'): out.append('schedule')
        if a.get('cohorts')!=b.get('cohorts'): out.append('cohorts')
        return out
    def propose(self,w,kind,cur,cfg,ts,evidence=None):
        if cfg==cur['config'] or self.s.active(w) or self.s.recently_proposed(w,ts):
            return
        dims=self.changed(cur['config'],cfg); pid=str(uuid.uuid4()); t=self.s.total(w)
        body={'proposal_id':pid,'workflow_id':w,'base_version':cur['version'],'proposed_config':cfg,'change_summary':kind.replace('_',' ')+' with population evidence and guardrail monitoring','diff':{d:'updated' for d in dims},'affected_leads':int(t['dials']),'dry_run':{'method':'observed event comparison','dials':t['dials'],'connected':t['connected'],'guardrail':self.w[w]['objective']['guardrail']},'impact_score':round(min(.95,max(.05,abs(r(t['engaged'],t['connected'])-.5)+.15)),3),'rationale':'Conservative population-level evidence; no anecdotal changes.','expires_in_hours':min(12,self.w[w]['governance']['proposal_ttl_hours'])}
        if evidence: body['evidence']=evidence
        auto=self.w[w]['governance']['auto_apply']
        if kind=='caller_id_containment' and auto.get('enabled') and set(dims).issubset(auto.get('allowed_dims',[])):
            result,q=self.request('put','/workflows/'+w+'/config',{k:body[k] for k in ('proposal_id','base_version','proposed_config','rationale')})
            self.s.log(ts,'auto_applied' if result else 'auto_apply_declined',result or q.json(),w); return
        result,q=self.request('post','/approvals',body)
        if result:
            self.s.save(pid,result['approval_id'],w,kind,cur['version'],cfg,ts); self.s.log(ts,'proposal_submitted',{'approval_id':result['approval_id'],'kind':kind,'dims':dims},w)
        else: self.s.log(ts,'proposal_submit_failed',q.json(),w)
    def caller_health(self,w,cur,ts):
        a=[x for x in self.s.arms_for(w,'caller_id') if x['dials']>=MIN_DIALS]
        if len(a)<2:return
        good=max(a,key=lambda x:r(x['connected'],x['dials'])); bad=min(a,key=lambda x:r(x['connected'],x['dials']))
        if lo(good['connected'],good['dials'])<=hi(bad['connected'],bad['dials']):return
        cfg=copy.deepcopy(cur['config']); d=cfg['allocations']['default']['caller_id']; move=min(.1,d.get(bad['option'],0)-.05)
        if move<=0:return
        d[bad['option']]-=move; d[good['option']]=d.get(good['option'],0)+move
        self.propose(w,'caller_id_containment',cur,cfg,ts)
    def experiment(self,w,cur,ts):
        primary=self.w[w]['objective']['primary']; d='caller_id' if primary=='pickup_rate' else 'voice'; arms=self.s.arms_for(w,d); dist=cur['config']['allocations']['default'][d]
        if not arms:return
        baseline=max(arms,key=lambda x:x['dials']); unseen=[x for x in self.w[w]['options'][d] if x not in dist]
        if unseen and baseline['dials']>=MIN_DIALS:
            cfg=copy.deepcopy(cur['config']); old=max(dist,key=dist.get); cfg['allocations']['default'][d]={old:1-EXPLORE,unseen[0]:EXPLORE}; self.propose(w,'explore_'+d,cur,cfg,ts); return
        usable=[x for x in arms if x['dials']>=MIN_DIALS and x['connected']>=MIN_CONNECTED]
        den='connected' if primary=='engagement_rate' else 'dials'; num='engaged' if primary=='engagement_rate' else 'connected'
        if len(usable)<2:return
        best=max(usable,key=lambda x:r(x[num],x[den])); incumbent=max(dist,key=dist.get); old=next((x for x in usable if x['option']==incumbent),None)
        if not old or best['option']==incumbent or lo(best[num],best[den])<=hi(old[num],old[den]):return
        cfg=copy.deepcopy(cur['config']);cfg['allocations']['default'][d]={incumbent:.2,best['option']:.8};self.propose(w,'promote_'+d,cur,cfg,ts)
    def incident(self,w,cur,ts):
        t=self.s.total(w)
        if t['connected']<80 or t['halluc']<6 or r(t['halluc'],t['connected'])<.05:return
        if self.get('/sim/state')['credits_remaining']<18:self.s.log(ts,'incident_deferred_credit_floor',t,w);return
        sug,q=self.request('post','/llm/suggest-fixes',{'workflow_id':w,'problem':'Population hallucination flags exceed threshold.'})
        if not sug:return
        checks=[]
        for x in sug['suggestions'][:2]:
            val,_=self.request('post','/validation/runs',{'workflow_id':w,'prompt_version':x['prompt_version'],'n':500})
            if val:checks.append(val)
        ok=[x for x in checks if x['pass_rate']>=self.w[w]['governance']['min_validation_pass_rate']]
        if not ok:self.s.log(ts,'incident_no_validated_fix',{'validations':checks},w);return
        winner=max(ok,key=lambda x:x['pass_rate']);cfg=copy.deepcopy(cur['config']);cfg['prompt_version']=winner['prompt_version'];self.propose(w,'hallucination_remediation',cur,cfg,ts,{'validation_run_id':winner['validation_run_id']})
    def reconcile(self,ts):
        for p in self.s.pendings():
            a=self.get('/approvals/'+p['aid'])
            if a['status']=='pending':continue
            if a['status']=='approved':
                result,q=self.request('post','/workflows/'+p['wid']+'/apply',{'approval_id':p['aid']})
                status='applied' if result else ('stale' if q.status_code==409 else 'lapsed' if q.status_code==410 else 'failed');self.s.status(p['pid'],status,ts);self.s.log(ts,'approval_'+status,result or q.json(),p['wid'])
            else:self.s.status(p['pid'],a['status'],ts);self.s.log(ts,'approval_'+a['status'],a,p['wid'])
    def run(self):
        self.bootstrap()
        while not self.get('/sim/state')['finished']:
            out,q=self.request('post','/sim/advance',{'minutes':TICK})
            if not out:
                if q.status_code==409:break
                raise RuntimeError(q.text)
            for ch in out['new_chunks']:self.ingest(ch,'events')
            self.reconcile(out['ts'])
            for w in self.w:
                if self.s.active(w):continue
                cur=self.get('/workflows/'+w+'/config');self.caller_health(w,cur,out['ts'])
                if not self.s.active(w):self.incident(w,cur,out['ts'])
                if not self.s.active(w):self.experiment(w,cur,out['ts'])
            self.s.db.commit()
        st=self.get('/sim/state');self.s.log(st['ts'],'controller_finished',st);self.s.db.commit()

def main():
    p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--state',default=os.environ.get('CONTROLLER_STATE','.controller-state/controller.sqlite3'));a=p.parse_args();c=Controller(a.url,a.state)
    try:c.run()
    finally:c.s.db.close()
if __name__=='__main__':main()
