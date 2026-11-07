"""Fragment avtomatik yetkazishni tarmoqsiz sinash; real xarid emas."""
import ast,contextlib,logging,sqlite3
from pathlib import Path
source=Path(__file__).with_name('bot.py')
tree=ast.parse(source.read_text(encoding='utf-8'))
blocks=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'process_purchase','finish_order'}]
assert len(blocks)==2
conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
conn.executescript('''
CREATE TABLE orders(id TEXT PRIMARY KEY,user_id INTEGER,kind TEXT,qty INTEGER,recipient TEXT,price INTEGER,status TEXT,detail TEXT,created TEXT,updated TEXT);
CREATE TABLE users(id INTEGER PRIMARY KEY,balance INTEGER);
CREATE TABLE ledger(id INTEGER PRIMARY KEY,user_id INTEGER,delta INTEGER,reason TEXT,ref TEXT,created TEXT,UNIQUE(reason,ref));
INSERT INTO users VALUES(1,1000);
''')
@contextlib.contextmanager
def db(write=False):
    if write:conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
        if write:conn.commit()
    except:
        conn.rollback();raise
ctx={'LIVE_SALES_ENABLED':True,'db':db,'logger':logging.getLogger('offline-fragment'),
     'esc':str,'now':lambda:'NOW','safe_send':lambda *x:None,'notify_admin':lambda *x:None}
paths=[]
def fragment(path,payload):
    paths.append((path,payload))
    return {'ok':True,'result':{'transaction':'mock'}}
ctx['fragment']=fragment
exec(compile(ast.Module(body=blocks,type_ignores=[]),str(source),'exec'),ctx)
for oid,kind,qty in [('STARS','stars',50),('PREMIUM','premium',3)]:
    conn.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)',(oid,1,kind,qty,'example',500,'processing','','now','now'))
conn.commit()
ctx['process_purchase']('STARS')
assert paths[-1]==('/stars/buy',{'username':'example','amount':50})
assert conn.execute("SELECT status FROM orders WHERE id='STARS'").fetchone()['status']=='completed'
ctx['process_purchase']('PREMIUM')
assert paths[-1]==('/premium/buy',{'username':'example','duration':3})
assert conn.execute("SELECT status FROM orders WHERE id='PREMIUM'").fetchone()['status']=='completed'
prior=len(paths)
ctx['process_purchase']('STARS')
assert len(paths)==prior,'Qayta xarid yuborildi'
print('Fragment mock tests passed: Stars, Premium, completed, no duplicate')
