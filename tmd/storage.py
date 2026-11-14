from __future__ import annotations
import json, sqlite3, time
from pathlib import Path
from .model import Task

# Resume existing prefixes first, then prefer files closest to completion.
# This is independent of `updated`, which changes on every pause/restart.
RESUME_ORDER = """CASE WHEN coalesce(json_extract(payload,'$.checkpoint_bytes'),0)>0 THEN 0 ELSE 1 END,
  (1.0*coalesce(json_extract(payload,'$.checkpoint_bytes'),0)/max(1,json_extract(payload,'$.size'))) DESC"""

class Storage:
    def __init__(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL");self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(key TEXT PRIMARY KEY, account TEXT NOT NULL, peer INTEGER NOT NULL,
          kind TEXT NOT NULL,status TEXT NOT NULL,updated REAL NOT NULL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS queue_idx ON tasks(account,peer,status,updated);
        CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, task_key TEXT, at REAL,attempt INTEGER,code TEXT,message TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        """)
        with self.db:
            rows=self.db.execute("SELECT payload FROM tasks WHERE status='downloading'").fetchall()
            for row in rows:
                task=Task(**json.loads(row[0]));task.status="paused";task.error="上次会话中断，将校验 .part 后继续";self.put(task)
    def put(self,task):
        task.updated=time.time()
        with self.db: self.db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?)",
          (task.key,task.account,task.peer,task.kind,task.status,task.updated,json.dumps(task.dump(),ensure_ascii=False)))
    def get(self,key):
        row=self.db.execute("SELECT payload FROM tasks WHERE key=?",(key,)).fetchone()
        return Task(**json.loads(row[0])) if row else None
    def rows(self,account,peer=None,statuses=None,limit=None):
        sql="SELECT payload FROM tasks WHERE account=?";args=[account]
        if peer is not None:sql+=" AND peer=?";args.append(peer)
        if statuses:
            sql+=" AND status IN ("+",".join("?" for _ in statuses)+")";args.extend(statuses)
        sql+=" ORDER BY CASE status WHEN 'downloading' THEN 0 WHEN 'failed' THEN 1 WHEN 'queued' THEN 2 WHEN 'paused' THEN 3 ELSE 4 END, "+RESUME_ORDER+", updated DESC, key"
        if limit:sql+=" LIMIT ?";args.append(limit)
        return [Task(**json.loads(row[0])) for row in self.db.execute(sql,args)]
    def next_task(self,account,peer,allow_video=True):
        sql="SELECT payload FROM tasks WHERE account=? AND peer=? AND status='queued'"
        if not allow_video:sql+=" AND kind!='video'"
        row=self.db.execute(sql+" ORDER BY "+RESUME_ORDER+", updated, key LIMIT 1",(account,peer)).fetchone()
        return Task(**json.loads(row[0])) if row else None
    def error(self,task,code,message):
        with self.db:self.db.execute("INSERT INTO errors(task_key,at,attempt,code,message) VALUES(?,?,?,?,?)",(task.key,time.time(),task.attempts,code,message))
    def errors(self,key):
        return [dict(zip(("at","attempt","code","message"),row)) for row in self.db.execute("SELECT at,attempt,code,message FROM errors WHERE task_key=? ORDER BY id",(key,))]
    def setting(self,key,default=None):
        row=self.db.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set_setting(self,key,value):
        with self.db:self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(key,json.dumps(value)))
    def summary(self,account,peer=None):
        sql="SELECT status,kind,count(*),coalesce(sum(json_extract(payload,'$.size')),0),coalesce(sum(json_extract(payload,'$.received')),0) FROM tasks WHERE account=?"
        args=[account]
        if peer is not None:sql+=" AND peer=?";args.append(peer)
        sql+=" GROUP BY status,kind"
        result={"total":0,"video":0,"image":0,"queued":0,"downloading":0,"completed":0,"failed":0,"paused":0,"size":0,"received":0}
        for status,kind,count,size,received in self.db.execute(sql,args):
            result["total"]+=count;result[kind]=result.get(kind,0)+count;result[status]=result.get(status,0)+count
            result["size"]+=size;result["received"]+=received
        return result
    def close(self):self.db.close()
