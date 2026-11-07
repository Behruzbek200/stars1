import ast,sqlite3,re,os,contextlib,time
from pathlib import Path
from decimal import Decimal,InvalidOperation
from datetime import datetime,timezone
source=Path(__file__).parent/'bot.py'
tree=ast.parse(source.read_text())
needed=['parse_bank_notice','process_bank_notice','approve_bank_payment']
block=[]
for n in tree.body:
 if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ('BANK_INCOMING_RE','BANK_AMOUNT_RE','BANK_REFERENCE_RE') for t in n.targets):block.append(n)
 if isinstance(n,ast.FunctionDef) and n.name in needed:block.append(n)
ctx={'re':re,'Decimal':Decimal,'InvalidOperation':InvalidOperation,'datetime':datetime,'timezone':timezone,'time':time,
     'HUMO_CARD_LAST4':'1234','HUMO_SENDER_ID':444444444,'PAYMENT_PROVIDER':'bank_session','LIVE_SALES_ENABLED':True,
     'USE_POSTGRES':False,'HUMO_AUTO_CREDIT':True,'OWNER_ID':12345,'esc':str}
ctx['expected_humo_last4']=lambda: '1234'
ctx['money']=lambda x: f'{x} som';ctx['now']=lambda:datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
log=[];ctx['safe_send']=lambda *a:log.append(('message',a));ctx['notify_admin']=lambda *a:log.append(('admin',a))
dbconn=sqlite3.connect(':memory:');dbconn.row_factory=sqlite3.Row
for ddl in [
 'CREATE TABLE users (id INTEGER PRIMARY KEY,balance INTEGER NOT NULL)',
 'CREATE TABLE payments (id TEXT PRIMARY KEY, user_id INTEGER, amount INTEGER, provider_id TEXT, expires_at INTEGER, status TEXT, created TEXT, updated TEXT)',
 'CREATE TABLE ledger (id INTEGER PRIMARY KEY,user_id INTEGER,delta INTEGER,reason TEXT,ref TEXT,created TEXT,UNIQUE(reason,ref))',
 'CREATE TABLE bank_events (sender_id INTEGER,message_id INTEGER,tx_ref TEXT UNIQUE,amount INTEGER,payment_id TEXT,status TEXT,created TEXT, PRIMARY KEY(sender_id,message_id))',
]:dbconn.execute(ddl)
dbconn.execute('INSERT INTO users(id,balance) VALUES(?,?)',(101,0))
t0=int(time.time())
dbconn.execute('INSERT INTO payments VALUES(?,?,?,?,?,?,?,?)',('B123',101,50001,'BANK:B123',t0+300,'pending',ctx['now'](),ctx['now']()))
dbconn.commit()
@contextlib.contextmanager
def db(write=False):
 if write:dbconn.execute('BEGIN IMMEDIATE')
 try:
  yield dbconn
  if write:dbconn.commit()
 except:
  dbconn.rollback();raise
ctx['db']=db
exec(compile(ast.Module(body=block,type_ignores=[]),str(source),'exec'),ctx)
msg='Kirim: +50 001 UZS\nKarta: ****1234\nTranzaksiya ID: ABC12345678'
assert ctx['process_bank_notice'](444444443,101,msg,t0+2)=='untrusted_sender'
assert ctx['process_bank_notice'](444444444,101,msg.replace('1234','5555'),t0+2)=='unrecognized'
assert ctx['process_bank_notice'](444444444,101,msg,t0+2)=='credited'
assert ctx['process_bank_notice'](444444444,101,msg,t0+2) in ('ambiguous','duplicate')
assert dbconn.execute('SELECT balance FROM users WHERE id=101').fetchone()['balance']==50001
assert dbconn.execute("SELECT COUNT(*) cnt FROM ledger WHERE reason='bank_credit'").fetchone()['cnt']==1
assert dbconn.execute("SELECT COUNT(*) cnt FROM bank_events").fetchone()['cnt']==1
assert ctx['approve_bank_payment'](12345,'B123')=='Pending HUMO to‘lov topilmadi.'
print('7/7 bank session tests passed: sender,card,once-only balance,ledger,event,approval')
