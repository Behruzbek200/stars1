"""To'lov skrini formatiga mos tarmoqsiz regressiya sinovi."""
import ast,contextlib,sqlite3,re,time
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from pathlib import Path
source=Path(__file__).with_name('bot.py')
tree=ast.parse(source.read_text('utf8'))
needed={'parse_bank_notice','process_bank_notice'}
blocks=[]
for n in tree.body:
    if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ('BANK_INCOMING_RE','BANK_AMOUNT_RE','BANK_REFERENCE_RE') for t in n.targets):blocks.append(n)
    elif isinstance(n,ast.FunctionDef) and n.name in needed:blocks.append(n)
assert sum(isinstance(n,ast.FunctionDef) for n in blocks)==2
ctx={'re':re,'Decimal':Decimal,'InvalidOperation':InvalidOperation,'datetime':datetime,'timezone':timezone,'time':time,
'HUMO_SENDER_ID':135790246,'PAYMENT_PROVIDER':'bank_session','LIVE_SALES_ENABLED':True,'HUMO_AUTO_CREDIT':True,
'esc':str,'money':lambda x: f'{x} som','now':lambda:datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
'expected_humo_last4':lambda: '1802'}
messages=[]
ctx['safe_send']=lambda *x:messages.append(('user',x))
ctx['notify_admin']=lambda *x:messages.append(('admin',x))
conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
conn.executescript('''
CREATE TABLE users (id INTEGER PRIMARY KEY, balance INTEGER NOT NULL);
CREATE TABLE payments (id TEXT PRIMARY KEY, user_id INTEGER, amount INTEGER, provider_id TEXT, expires_at INTEGER, status TEXT, created TEXT, updated TEXT);
CREATE TABLE bank_events (sender_id BIGINT, message_id BIGINT, tx_ref TEXT NOT NULL UNIQUE, amount BIGINT, payment_id TEXT, status TEXT,created TEXT,PRIMARY KEY(sender_id,message_id));
CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,delta INTEGER,reason TEXT,ref TEXT,created TEXT, UNIQUE(reason,ref));
INSERT INTO users VALUES(101,0);
''')
@contextlib.contextmanager
def db(write=False):
    if write:conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
        if write:conn.commit()
    except BaseException:
        conn.rollback();raise
ctx['db']=db
exec(compile(ast.Module(body=blocks,type_ignores=[]),str(source),'exec'),ctx)
parse=ctx['parse_bank_notice'];process=ctx['process_bank_notice']
msg='''🎉 To'ldirish
➕ 1.001,00 UZS
📍 HUMO TO HUMO UZPAYNE
💳 HUMOCARD *1802
🕒 23:05 07.11.2026
💰 1.474,20 UZS'''
assert parse(msg)=={'amount':1001,'tx_ref':None},parse(msg)
assert parse(msg.replace("To'ldirish","Toʻldirish"))=={'amount':1001,'tx_ref':None}
assert parse(msg.replace('1.001,00','1 001,00'))['amount']==1001
assert parse(msg.replace('*1802','*1803')) is None
assert parse('''💸 Amaliyot
➖ 1.008,01 UZS
💳 VISA *5904
💰 625,49 UZS''') is None
assert parse('''💸 Amaliyot
➖ 1.510,50 UZS
💳 HUMOCARD *1802
💰 473,20 UZS''') is None
now=int(time.time())
conn.execute('INSERT INTO payments VALUES(?,?,?,?,?,?,?,?)',('BSAMPLE',101,1001,'BANK:BSAMPLE',now+300,'pending',ctx['now'](),ctx['now']()))
conn.commit()
assert process(111,201,msg,now+5)=='untrusted_sender'
assert conn.execute('SELECT balance FROM users WHERE id=101').fetchone()[0]==0
assert process(135790246,201,msg,now+5)=='credited'
assert conn.execute('SELECT balance FROM users WHERE id=101').fetchone()[0]==1001
assert conn.execute('SELECT tx_ref FROM bank_events').fetchone()[0]=='TG:135790246:201'
assert conn.execute("SELECT COUNT(*) FROM ledger WHERE reason='bank_credit'").fetchone()[0]==1
assert process(135790246,201,msg,now+5)!='credited'
assert conn.execute('SELECT balance FROM users WHERE id=101').fetchone()[0]==1001
print('12/12 screenshot format tests passed: incoming, outgoing, card, sender, once-only credit')
