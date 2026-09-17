import sqlite3
from pathlib import Path
from threading import Lock
class ConversationStore:
    def __init__(self,p):
        Path(p).parent.mkdir(parents=True,exist_ok=True); self.p=p; self.l=Lock()
        with self.l,sqlite3.connect(p) as c: c.execute("CREATE TABLE IF NOT EXISTS conversations(session_key TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    def get(self,k):
        with self.l,sqlite3.connect(self.p) as c:
            r=c.execute("SELECT conversation_id FROM conversations WHERE session_key=?",(k,)).fetchone(); return r[0] if r else None
    def put(self,k,v):
        with self.l,sqlite3.connect(self.p) as c:
            c.execute("INSERT INTO conversations(session_key,conversation_id) VALUES(?,?) ON CONFLICT(session_key) DO UPDATE SET conversation_id=excluded.conversation_id,updated_at=CURRENT_TIMESTAMP",(k,v)); c.commit()
