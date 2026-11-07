"""Offline safety tests. No real money is transferred."""
import importlib.util, os, sys, tempfile, types, hashlib, hmac, time
from pathlib import Path
from unittest import mock

# Minimal offline dependency stubs: no access to Telegram or payments.
telebot=types.ModuleType('telebot')
class FakeBot:
    def __init__(self,*args,**kwargs):self.sent=[]
    def message_handler(self,**kwargs):return lambda fn:fn
    def callback_query_handler(self,**kwargs):return lambda fn:fn
    def send_message(self,chat,text,**kwargs):self.sent.append((chat,text)); return True
    def set_webhook(self,**kwargs):return True
class FakeMarkup:
    def __init__(self,**kwargs):pass
    def row(self,*args):pass
    def add(self,*args):pass
class FakeBtn:
    def __init__(self,*args,**kwargs):pass
telebot.TeleBot=FakeBot
telebot.types=types.SimpleNamespace(InlineKeyboardMarkup=FakeMarkup,ReplyKeyboardMarkup=FakeMarkup,InlineKeyboardButton=FakeBtn)
sys.modules['telebot']=telebot
flask=types.ModuleType('flask')
class FakeApp:
    def __init__(self,*a,**k): pass
    def get(self,*args,**kwargs): return lambda fn:fn
    def post(self,*args,**kwargs): return lambda fn:fn
flask.Flask=FakeApp
flask.jsonify=lambda *args,**kwargs: args[0] if args else kwargs
flask.request=types.SimpleNamespace(is_json=True,get_json=lambda silent=False: {},form={})
sys.modules['flask']=flask

with tempfile.TemporaryDirectory() as td:
    os.environ.update(BOT_TOKEN='123:FAKE',OWNER_ID='6232278895',DATA_DIR=td,ENABLE_LIVE_SALES='1',APP_SECRET='')
    filename=Path(__file__).parent/'bot.py'
    spec=importlib.util.spec_from_file_location('production_bot_under_test',filename)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    mod.put('shop_id','shop_demo')
    mod.set_secret('shop_key','secret_123')
    mod.set_secret('fragment_key','fragment_fake')
    mod.put('payment_enabled','1')
    mod.put('star_price','250')
    assert mod.payment_ready()
    assert mod.stars_total(777)==194250
    # Store user with authenticated Telegram user object
    mod.user_record(types.SimpleNamespace(id=10,first_name='Buyer',username='buyer'))
    with mod.db(True) as c:
        c.execute("INSERT INTO payments(id,user_id,amount,provider_id,card,expires_at,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                  ('PTEST0001',10,50000,'PROVIDER1','8600123456789012',int(time.time())+300,'pending',mod.now(),mod.now()))
    with mod.db() as c: p=c.execute('SELECT * FROM payments WHERE id=?',('PTEST0001',)).fetchone()
    data={'payment_id':'PROVIDER1','order_id':'PTEST0001','amount':'50000','status':'paid','sign':'fake'}
    assert not mod.verified_callback(data,p,'shop_demo','secret_123'), 'Forged payment accepted'
    data['sign']=mod.payment_callback_signature('shop_demo','PROVIDER1',50000,'secret_123')
    assert mod.verified_callback(data,p,'shop_demo','secret_123')
    assert not mod.verified_callback({**data,'amount':50001},p,'shop_demo','secret_123')
    assert not mod.verified_callback({**data,'order_id':'POTHER'},p,'shop_demo','secret_123')
    assert not mod.verified_callback({**data,'payment_id':'OTH'},p,'shop_demo','secret_123')
    assert not mod.verified_callback({**data,'test':True},p,'shop_demo','secret_123')
    # No GET status can credit unverified payments
    with mock.patch.object(mod,'tolov_request',return_value=(200,{'status':'paid'})):
        assert mod.verify_payment('PTEST0001')=='paid_unverified'
    assert mod.user_row(10)['balance']==0
    # Correct callback pays ONCE, even if provider retries 100x
    for i in range(100): assert mod.apply_verified_payment('PTEST0001','paid')=='paid'
    assert mod.user_row(10)['balance']==50000
    with mod.db() as c:
        rows=c.execute('SELECT * FROM ledger WHERE reason=?',('payment_credit',)).fetchall()
    assert len(rows)==1
    # Already paid cannot be cancelled
    assert mod.apply_verified_payment('PTEST0001','cancel')=='paid'
    assert mod.user_row(10)['balance']==50000
    # Two invoice requests cannot share live payment amount
    with mod.db(True) as c:
        c.execute("INSERT INTO payments(id,user_id,amount,status,created,updated) VALUES(?,?,?,?,?,?)",
                  ('PWAIT',10,50000,'pending',mod.now(),mod.now()))
        assert mod.reserve_payment_amount(c,50000)==50001
    # Hamyon create request: officially form-encoded (not fabricated JSON /v1 endpoints)
    class PaymentReply:
        status_code=200
        def json(self):return {'payment_id':'PAYNEW','order_id':'ORDERNEW','card':'8600123456789012','amount':50001,'expires_in':300}
    with mock.patch.object(mod.requests,'post',return_value=PaymentReply()) as post:
        status,response=mod.tolov_request('payment/create','POST',{'amount':50001,'order_id':'ORDERNEW'})
        assert status==200 and response['payment_id']=='PAYNEW'
        assert post.call_args.kwargs['data']['shop_id']=='shop_demo'
        assert post.call_args.kwargs['data']['shop_key']=='secret_123'
        assert post.call_args.kwargs['data']['amount']==50001
        assert post.call_args.kwargs['data']['order_id']=='ORDERNEW'
    # Purchase uses stored balance once, no double debit
    mod.put('premium_3','15000')
    purchase={'kind':'premium','qty':3,'total':15000,'recipient':'buyer'}
    mod.set_state(10,'review_order',purchase)
    oid,msg=mod.debit_and_create(10,purchase)
    assert oid and not msg,(oid,msg)
    assert mod.user_row(10)['balance']==35000
    assert mod.debit_and_create(10,purchase)[0] is None
    # refunds only once
    assert mod.finish_order(oid,'refunded','Provider definitely not charged') is not None
    assert mod.finish_order(oid,'refunded','Repeated') is None
    assert mod.user_row(10)['balance']==50000
    # mocked Fragment server response contract success and failure parsing
    class Reply:
        def __init__(self,data):self.data=data
        def json(self):return self.data
    with mock.patch.object(mod.requests,'post',return_value=Reply({'ok':True,'result':{'username':'buyer'}})) as request_mock:
        assert mod.fragment('/getInfo',{'username':'buyer'})['ok']
        assert request_mock.call_args.kwargs['headers']['X-API-Key']=='fragment_fake'
    # callback Flask function tested with fake request and signed data
    with mod.db(True) as c:
        c.execute("INSERT INTO payments(id,user_id,amount,provider_id,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                  ('PTEST0002',10,16000,'PROVIDER2','pending',mod.now(),mod.now()))
    second={'payment_id':'PROVIDER2','order_id':'PTEST0002','amount':'16000','status':'paid'}
    second['sign']=mod.payment_callback_signature('shop_demo','PROVIDER2',16000,'secret_123')
    flask.request.get_json=lambda silent=False:second
    assert mod.hamyon_complete(mod.get('callback_token'))[1]==200
    assert mod.user_row(10)['balance']==66000
    assert mod.hamyon_complete(mod.get('callback_token'))[1]==200
    assert mod.user_row(10)['balance']==66000
    flask.request.get_json=lambda silent=False:{**second,'sign':'0'*32}
    assert mod.hamyon_complete(mod.get('callback_token'))[1]==403
    assert mod.user_row(10)['balance']==66000
    print('PASS: 22 safety checks (signatures, duplicates, amount, balance, refund, premium, stars, mock Fragment)')
