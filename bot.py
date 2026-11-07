# -*- coding: utf-8 -*-
"""Stars & Premium Shop | telebot + Flask | Render.

PAYMENT PROVIDER: TolovAPI (https://tolovapi.uz/) via supplied tolovapi.py guide.
create/payment status/cancel: specified in user-provided integration guide.
This code has NOT been verified with live credentials and real bank transfers.
Credentials alone do not prove production readiness.
Fragment API contract: https://fragment-api.uz/docs/openapi.json

No live API call can be verified without the owner's *own* keys and a real payment.
ENABLE_LIVE_SALES=0 by default; for real sales DATABASE_URL + APP_SECRET + live API smoke test are mandatory.
Do NOT publish bot tokens, shop keys, Fragment keys, or APP_SECRET.

Run on Render: python bot.py --render-start
"""

import os
import re
import json
import time
import math
import uuid
import html
import hmac
import hashlib
import secrets
import logging
import sqlite3
import psycopg2
from psycopg2 import extras as pg_extras
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager

import requests
import telebot
from telebot import types
from flask import Flask, request, jsonify
from cryptography.fernet import Fernet, InvalidToken

# Render -> Web Service -> Environment bo'limida kiriting.
# Tokenni GitHub yoki ushbu faylga kiritmang.
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
OWNER_ID = int(os.getenv('OWNER_ID', '0'))  # Bosh adminning Telegram ID raqami

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Render ENV: DATA_DIR, DB_FILE, SECRET_FILE talab QILINMAYDI.
# Demo: local SQLite (Render restart/deployda yo'qolishi mumkin).
# Haqiqiy savdo: tashqi doimiy PostgreSQL URL orqali.
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
USE_POSTGRES = bool(DATABASE_URL)
LOCAL_DEMO_DB = os.path.join(BASE_DIR, 'demo_shop.sqlite3')
LIVE_SALES_ENABLED = os.getenv('ENABLE_LIVE_SALES', '0') == '1'
RENDER_HOST = os.getenv('RENDER_EXTERNAL_HOSTNAME', '').strip()
PUBLIC_URL = (os.getenv('PUBLIC_URL') or ('https://' + RENDER_HOST if RENDER_HOST else '')).rstrip('/')

FRAGMENT_BASE = 'https://fragment-api.uz/api/v1'
TOLOV_BASE = os.getenv('TOLOV_API_BASE', 'https://tolovapi.uz/api').rstrip('/')
# Format from user-supplied tolovapi.py (create/status/cancel + X-Shop-Key).
TOLOV_PATHS = ('payment/create','payment/status','payment/cancel')
TOLOV_AUTH_HEADER = 'X-Shop-Key' 
REQUEST_TIMEOUT = (4, 15)

if not BOT_TOKEN or OWNER_ID <= 0:
    raise SystemExit('Render Environment: BOT_TOKEN va OWNER_ID ni kiriting!')
# Pul qabul qilish hech qachon yo'qoladigan lokal baza bilan yoqilmaydi.
if LIVE_SALES_ENABLED and not DATABASE_URL:
    raise SystemExit('Haqiqiy savdo uchun DATABASE_URL (doimiy PostgreSQL) majburiy.')
if DATABASE_URL and not os.getenv('APP_SECRET', '').strip():
    raise SystemExit('PostgreSQL ishlatilsa, APP_SECRET (Fernet key) majburiy.')
if DATABASE_URL and not DATABASE_URL.startswith(('postgres://', 'postgresql://')):
    raise SystemExit('DATABASE_URL PostgreSQL ulanish manzili bo‘lishi kerak.')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('stars_shop')
bot = telebot.TeleBot(BOT_TOKEN, threaded=False, parse_mode='HTML')
app = Flask(__name__)


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def money(amount):
    return f'{int(amount):,}'.replace(',', ' ') + " so'm"


def esc(s):
    return html.escape(str(s or ''))


class PGConnection:
    """Existing sqlite-shaped calls supported over PostgreSQL.

    All write transactions take one transaction advisory lock. This serializes
    critical balance changes across concurrent Render web workers/instances.
    """
    def __init__(self):
        self.conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        self.cur = None
        self.lastrowid = None
        self.rowcount = -1

    def execute(self, statement, params=()):
        q = statement.strip()
        if q == 'BEGIN IMMEDIATE':
            self.cur = self.conn.cursor(cursor_factory=pg_extras.RealDictCursor)
            self.cur.execute('SELECT pg_advisory_xact_lock(%s)', (34889021171,))
            return self
        if q.startswith('PRAGMA '):
            return self
        if re.match(r'^INSERT\s+OR\s+IGNORE\s+INTO\s+', q, re.I):
            q = re.sub(r'^INSERT\s+OR\s+IGNORE\s+INTO\s+', 'INSERT INTO ', q, flags=re.I)
            q = q.rstrip(';') + ' ON CONFLICT DO NOTHING'
        if re.match(r'^INSERT\s+OR\s+REPLACE\s+INTO\s+packages\b', q, re.I):
            q = re.sub(r'^INSERT\s+OR\s+REPLACE\s+INTO\s+', 'INSERT INTO ', q, flags=re.I)
            q = q.rstrip(';') + ' ON CONFLICT(amount) DO UPDATE SET enabled=EXCLUDED.enabled'
        return_id = bool(re.match(r'^INSERT\s+INTO\s+tickets\b', q, re.I))
        if return_id:
            q = q.rstrip(';') + ' RETURNING id'
        q = q.replace('?', '%s')
        self.cur = self.conn.cursor(cursor_factory=pg_extras.RealDictCursor)
        self.cur.execute(q, params if params else None)
        self.rowcount = self.cur.rowcount
        self.lastrowid = self.cur.fetchone()['id'] if return_id else None
        return self

    def executemany(self, statement, rows):
        for params in rows:
            self.execute(statement, params)
        return self

    def executescript(self, sql):
        sql = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY', sql, flags=re.I)
        sql = re.sub(r'\bINTEGER\b', 'BIGINT', sql, flags=re.I)
        for stmt in sql.split(';'):
            if stmt.strip(): self.execute(stmt)

    def fetchone(self):
        return self.cur.fetchone() if self.cur is not None else None

    def fetchall(self):
        return self.cur.fetchall() if self.cur is not None else []

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        if self.cur is not None: self.cur.close()
        self.conn.close()


def db_open():
    if USE_POSTGRES:
        return PGConnection()
    conn = sqlite3.connect(LOCAL_DEMO_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)


@contextmanager
def db(write=False):
    c = db_open()
    try:
        if write:
            c.execute('BEGIN IMMEDIATE')
        yield c
        if write or USE_POSTGRES:
            c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def setup_db():
    c = db_open()
    try:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
          username TEXT NOT NULL DEFAULT '', balance INTEGER NOT NULL DEFAULT 0,
          banned INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, val TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS states (user_id INTEGER PRIMARY KEY, step TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS packages (amount INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
          qty INTEGER NOT NULL, recipient TEXT NOT NULL, price INTEGER NOT NULL,
          status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id,created);
        CREATE TABLE IF NOT EXISTS payments (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
          provider_id TEXT UNIQUE, card TEXT NOT NULL DEFAULT '',
          card_holder TEXT NOT NULL DEFAULT '', expires_at INTEGER,
          status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_pay_user ON payments(user_id,created);
        CREATE TABLE IF NOT EXISTS ledger (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          delta INTEGER NOT NULL, reason TEXT NOT NULL, ref TEXT NOT NULL,
          created TEXT NOT NULL, UNIQUE(reason,ref));
        CREATE TABLE IF NOT EXISTS tickets (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ticket_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL,
          sender_id INTEGER NOT NULL, body TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS telegram_updates (
          update_id INTEGER PRIMARY KEY, status TEXT NOT NULL, arrived TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS broadcast_queue (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
          from_chat INTEGER NOT NULL, source_message INTEGER NOT NULL,
          target_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created TEXT NOT NULL, UNIQUE(job_id,target_id));
        CREATE INDEX IF NOT EXISTS ix_broadcast_queue ON broadcast_queue(status,id);
        ''')
        defaults = {
          'star_price': '0', 'premium_3': '0', 'premium_6': '0', 'premium_12': '0',
          'usd_rate': '0', 'min_topup': '5000', 'max_topup': '10000000',
          'min_stars': '50', 'max_stars': '100000', 'maintenance': '0',
          'welcome': '',
          'callback_token': secrets.token_urlsafe(24), 'public_url':'', 'payment_enabled':'0',
          'shop_id': '', 'shop_key': '', 'fragment_key': '', 'tolov_webhook_secret': '',
          'payment_card_number': '', 'payment_card_name': '', 'payment_minutes': '5',
          'telegram_webhook_path': secrets.token_urlsafe(26),
          'telegram_header_secret': secrets.token_urlsafe(32),
          'payment_monitor_token': secrets.token_urlsafe(30)
        }
        for k,v in defaults.items():
            c.execute('INSERT OR IGNORE INTO settings(key,val) VALUES(?,?)', (k,v))
        # Oldingi SQLite bazalariga yangilanish (ma'lumotlar o'chirilmaydi).
        if USE_POSTGRES:
            paycols = {r['column_name'] for r in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='payments'"
            ).fetchall()}
        else:
            paycols = {r[1] for r in c.execute('PRAGMA table_info(payments)').fetchall()}
        if 'card_holder' not in paycols:
            c.execute("ALTER TABLE payments ADD COLUMN card_holder TEXT NOT NULL DEFAULT ''")
        if 'expires_at' not in paycols:
            c.execute('ALTER TABLE payments ADD COLUMN expires_at INTEGER')
        for qty in (50,100,250,500,1000,2500,5000,10000):
            c.execute('INSERT OR IGNORE INTO packages(amount) VALUES(?)', (qty,))
        c.commit()
    finally:
        c.close()


def get(k, default=''):
    with db() as c:
        r = c.execute('SELECT val FROM settings WHERE key=?',(k,)).fetchone()
        return r['val'] if r else default


def put(k, v):
    with db(True) as c:
        c.execute('INSERT INTO settings(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val',(k,str(v)))


def param(k, fallback=0):
    try: return int(get(k, str(fallback)))
    except (TypeError, ValueError): return fallback


def stars_total(qty, unit_price=None):
    """Barcha Stars tariflari bitta 1⭐ narxidan hisoblanadi."""
    if unit_price is None:
        unit_price = param('star_price')
    return int(qty) * int(unit_price)


def init_cipher():
    secret = os.getenv('APP_SECRET', '').strip()
    if not secret:
        # Sinov rejimida vaqtinchalik shifrlash: deploydan keyin API kalitlar
        # qayta kiritiladi. Productionda APP_SECRET majburiy.
        secret = Fernet.generate_key().decode()
        logger.warning('APP_SECRET yo‘q: demo rejimida maxfiy kalitlar restartdan so‘ng ochilmasligi mumkin.')
    try:
        return Fernet(secret.encode())
    except (ValueError, TypeError) as exc:
        raise SystemExit('APP_SECRET noto‘g‘ri: Fernet.generate_key() qiymatini kiriting.') from exc


CIPHER = None
SECURE_KEYS = {'shop_key','fragment_key','tolov_webhook_secret'}


def set_secret(k,v):
    if k not in SECURE_KEYS: raise ValueError('secret setting')
    put(k, 'enc:' + CIPHER.encrypt(v.encode()).decode())


def read_secret(k):
    value = get(k)
    if not value: return ''
    if not value.startswith('enc:'):
        return ''  # eski plaintext qiymatlarni yuklamang
    try: return CIPHER.decrypt(value[4:].encode()).decode()
    except InvalidToken:
        logger.error('Shifrlash kaliti topilmadi: %s', k)
        return ''


def mask_secret(k):
    v=read_secret(k)
    return '✅ ••••'+v[-4:] if v else '❌ Kiritilmagan'


def is_admin(uid):
    if uid == OWNER_ID: return True
    with db() as c:
        return c.execute('SELECT 1 FROM admins WHERE id=?',(uid,)).fetchone() is not None


def only_owner(uid):
    return uid == OWNER_ID


def admin_ids():
    with db() as c: ids=[r['id'] for r in c.execute('SELECT id FROM admins').fetchall()]
    return list(set(ids+[OWNER_ID]))


def notify_admin(text, markup=None):
    for uid in admin_ids():
        try: bot.send_message(uid,text,reply_markup=markup)
        except Exception as e: logger.info('Admin xabar yetmadi (%s): %s',uid,type(e).__name__)


def safe_send(uid, text, markup=None):
    try: return bot.send_message(uid,text,reply_markup=markup,disable_web_page_preview=True)
    except Exception as e:
        logger.warning('Xabar yuborilmadi (%s): %s',uid,type(e).__name__)
        return None


def user_record(u):
    if not u: return
    with db(True) as c:
        c.execute('''INSERT INTO users(id,name,username,created) VALUES(?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET name=excluded.name,username=excluded.username''',
                  (u.id, (u.first_name or '')[:100],(u.username or '')[:100],now()))


def user_row(uid):
    with db() as c: return c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()


def set_state(uid,step,data=None):
    with db(True) as c:
        c.execute('INSERT INTO states(user_id,step,data) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET step=excluded.step,data=excluded.data',
                  (uid,step,json.dumps(data or {},ensure_ascii=False)))


def state(uid):
    with db() as c:
        r=c.execute('SELECT * FROM states WHERE user_id=?',(uid,)).fetchone()
    return (r['step'],json.loads(r['data'])) if r else (None,{})


def clear_state(uid):
    with db(True) as c: c.execute('DELETE FROM states WHERE user_id=?',(uid,))


def keyboard(rows):
    k=types.InlineKeyboardMarkup(row_width=2)
    for row in rows:
        k.row(*[types.InlineKeyboardButton(label,callback_data=data) for label,data in row])
    return k


def menu_kb(admin=False):
    k=types.ReplyKeyboardMarkup(resize_keyboard=True)
    if admin:
        buttons=[('⭐ Stars narxlari','💎 Premium narxlari'),('📦 Buyurtmalar','👥 Foydalanuvchilar'),
                 ('📊 Statistika','💳 To‘lovlar'),('🔌 API va to‘lov','📢 Xabar yuborish'),
                 ('📣 Majburiy obuna','👑 Adminlar'),('🎧 Murojaatlar','⚙️ Sozlamalar'),('🏠 User menyu',)]
    else:
        buttons=[('⭐ Stars olish','💎 Premium olish'),('💳 Hisob to‘ldirish','💰 Hisobim'),
                 ('📦 Buyurtmalarim','🎧 Yordam')]
    for row in buttons: k.row(*[types.KeyboardButton(s) for s in row])
    return k


# Reply keyboard show, change through /admin, no public admin buttons.
def main_menu(uid):
    clear_state(uid)
    r=user_row(uid)
    name=esc(r['name'] if r else 'do‘stimiz')
    greeting=get('welcome').strip() or f'Assalomu alaykum, <b>{name}</b>! 👋\n\n⭐ Stars va 💎 Premium xarid qilish botiga xush kelibsiz!'
    safe_send(uid,greeting+'\n\n🛍 Kerakli bo‘limni tanlang:',menu_kb())


def admin_menu(uid):
    clear_state(uid)
    safe_send(uid,'👑 <b>ADMIN PANEL</b>\n\nKerakli bo‘limni tanlang.',menu_kb(admin=True))


def needed_subscription(uid):
    if is_admin(uid): return []
    with db() as c: channels=c.execute("SELECT key,val FROM settings WHERE key LIKE 'channel_%'").fetchall()
    missing=[]
    for r in channels:
        val=r['val']
        if not val: continue
        parts=val.split('|',1)
        chat=parts[0]
        link=parts[1] if len(parts)>1 else ''
        try:
            cm=bot.get_chat_member(chat,uid)
            if cm.status not in ('creator','administrator','member') and not (cm.status=='restricted' and getattr(cm,'is_member',False)):
                missing.append((chat,link))
        except Exception:
            # kanal adminligi sozlanmagan bo'lishi mumkin; adminlarga xabar beriladi
            missing.append((chat,link))
    return missing


def sub_prompt(uid, missing):
    k=types.InlineKeyboardMarkup()
    for chat,link in missing:
        url=link or ('https://t.me/'+chat.lstrip('@') if chat.startswith('@') else '')
        if url.startswith('https://t.me/'):
            k.add(types.InlineKeyboardButton('📢 Kanalga obuna bo‘lish',url=url))
    k.add(types.InlineKeyboardButton('✅ Obunani tekshirish',callback_data='sub:check'))
    safe_send(uid,'🔒 Botdan foydalanish uchun kanal(lar)ga obuna bo‘ling.',k)


def check_access(uid):
    u=user_row(uid)
    if u and u['banned'] and uid!=OWNER_ID:
        safe_send(uid,'🚫 Hisobingiz vaqtincha bloklangan.'); return False
    missing=needed_subscription(uid)
    if missing:
        sub_prompt(uid,missing); return False
    if get('maintenance')=='1' and not is_admin(uid):
        safe_send(uid,'🛠 Botda texnik xizmat ishlari olib borilmoqda.'); return False
    return True


def stars_menu(uid):
    if not check_access(uid): return
    price=param('star_price')
    if price<=0:
        safe_send(uid,'⭐ Stars narxlari hali administrator tomonidan belgilanmagan.');return
    with db() as c: packs=[r['amount'] for r in c.execute('SELECT amount FROM packages WHERE enabled=1 ORDER BY amount')]
    rows=[]
    for i in range(0,len(packs),2):
        rows.append([(f'⭐ {x:,} — {money(stars_total(x, price))}',f's:pack:{x}') for x in packs[i:i+2]])
    rows += [[('🔢 Boshqa miqdor','s:custom')],[('🏠 Bosh menyu','home')]]
    safe_send(uid,f'⭐ <b>Stars olish</b>\n\n1 ⭐ = <b>{money(price)}</b>\nTo‘plamni tanlang:',keyboard(rows))


def premium_menu(uid):
    if not check_access(uid):return
    rows=[]
    for months in (3,6,12):
        price=param(f'premium_{months}')
        if price>0: rows.append([(f'💎 {months} oy — {money(price)}',f'p:pack:{months}')])
    if not rows: safe_send(uid,'💎 Premium narxlari hali sozlanmagan.');return
    rows.append([('🏠 Bosh menyu','home')])
    safe_send(uid,'💎 <b>Telegram Premium</b>\n\nObuna muddatini tanlang:',keyboard(rows))


def choose_target(uid, kind, qty):
    if kind=='stars':
        minq,maxq=param('min_stars'),param('max_stars')
        if not minq<=qty<=maxq or param('star_price')<=0: safe_send(uid,'❌ Miqdor yoki narx xato.');return
        total=stars_total(qty); title=f'⭐ {qty} Stars'
    else:
        if qty not in (3,6,12) or param(f'premium_{qty}')<=0:return
        total=param(f'premium_{qty}');title=f'💎 {qty} oylik Premium'
    set_state(uid,'choose_target',{'kind':kind,'qty':qty,'total':total})
    safe_send(uid,f'<b>{title}</b>\n💰 Narx: <b>{money(total)}</b>\n\nKim uchun xarid qilasiz?',
              keyboard([[('👤 O‘zim uchun','t:self'),('🎁 Boshqaga','t:other')],[('❌ Bekor qilish','home')]]))


def extract_username(s):
    s=(s or '').strip()
    if s.startswith('https://t.me/') or s.startswith('http://t.me/'):
        s=s.split('t.me/',1)[1].split('?',1)[0].split('/',1)[0]
    elif s.startswith('t.me/'):
        s=s[5:].split('?',1)[0].split('/',1)[0]
    s=s.lstrip('@')
    if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',s):return s
    return ''


def preview_recipient(uid, username):
    step,data=state(uid)
    if step!='enter_recipient':return
    api_key=read_secret('fragment_key')
    if not api_key:
        safe_send(uid,'❌ Fragment API kaliti sozlanmagan.');clear_state(uid);return
    safe_send(uid,'🔎 Telegram foydalanuvchisi tekshirilmoqda...')
    try:
        j=fragment('/getInfo',{'username':username})
        if j.get('ok') is not True: raise ValueError(str(j.get('message') or 'Profil topilmadi'))
        profile=j.get('result') or {}
        resolved=extract_username(str(profile.get('username') or username))
        if resolved.lower()!=username.lower(): raise ValueError('Qabul qiluvchi username mos kelmadi')
    except Exception as e:
        safe_send(uid,'❌ Qabul qiluvchi tekshirilmadi: '+esc(str(e))[:300]+'\nUsername yoki havolani qayta yuboring.');return
    data['recipient']=username
    set_state(uid,'review_order',data)
    r=user_row(uid)
    title=f"⭐ {data['qty']} Stars" if data['kind']=='stars' else f"💎 {data['qty']} oylik Premium"
    safe_send(uid,f'🧾 <b>Buyurtmani tekshiring</b>\n\n📦 {title}\n👤 Qabul qiluvchi: @{esc(username)}'
             f"\n💰 Narx: <b>{money(data['total'])}</b>\n💳 Balans: {money(r['balance'] if r else 0)}"
             '\n\n✅ Tasdiqlasangiz balansingizdan pul yechilib, buyurtma avtomatik yuboriladi.',
             keyboard([[('✅ Tasdiqlash','buy:confirm')],[('💳 Hisob to‘ldirish','top:menu'),('❌ Bekor','home')]]))


def fragment(path,payload):
    key=read_secret('fragment_key')
    if not key: raise RuntimeError('Fragment API Key kiritilmagan')
    try:
        res=requests.post(FRAGMENT_BASE+path,json=payload,
            headers={'X-API-Key':key,'Content-Type':'application/json'},timeout=REQUEST_TIMEOUT)
        data=res.json()
    except requests.RequestException as e:
        raise RuntimeError('Fragment bilan tarmoq aloqasi uzildi') from e
    except ValueError as e:
        raise RuntimeError('Fragment javobi JSON emas') from e
    if not isinstance(data,dict):raise RuntimeError('Fragment javobi noto‘g‘ri')
    return data


def safe_price_check(kind,qty,sell):
    # API tannarxiga nisbatan zararli narx qo'yilishini cheklaydi.
    usd_rate=param('usd_rate')
    if usd_rate<=0: return False,'Admin USD/UZS kursini kiritmagan.'
    try:
        j=fragment('/stars/pricing',{'amount':qty}) if kind=='stars' else fragment('/premium/pricing',{})
        if j.get('ok') is not True: return False,str(j.get('message','Tannarx olinmadi'))
        result=j.get('result') or {}
        if kind=='stars':
            usd=Decimal(str((result.get('price') or {}).get('usd','')))
        else:
            package=next((x for x in result.get('packages',[]) if int(x.get('months',0))==qty),None)
            usd=Decimal(str(package.get('usd',''))) if package else Decimal('0')
        if usd<=0: return False,'API tannarxi aniqlanmadi.'
        # 2% bufer (1.5% komissiya va dumaloqlashdan himoya), qo'shimcha TON gas alohida
        minimum=math.ceil(usd*Decimal(usd_rate)*Decimal('1.02'))
        if sell<minimum:
            return False,f'Sotuv narxi past: {money(sell)}. Taxminiy tannarx+komissiya: {money(minimum)}.'
        return True,''
    except (InvalidOperation,TypeError,ValueError) as e:
        return False,'Narx formati xato: '+str(e)[:60]
    except Exception as e:
        return False,'Tannarx tekshiruvi ishlamadi: '+str(e)[:110]


def debit_and_create(uid,data):
    if not LIVE_SALES_ENABLED:
        return None, 'Demo rejim: savdo o‘chirilgan. Doimiy DATABASE_URL va APP_SECRET o‘rnating, sinovdan keyin ENABLE_LIVE_SALES=1 qiling.'
    # Bir userning ikkita confirm bosishi bitta orderga aylanadi.
    oid='O'+uuid.uuid4().hex[:12].upper()
    with db(True) as c:
        r=c.execute('SELECT step,data FROM states WHERE user_id=?',(uid,)).fetchone()
        if not r or r['step']!='review_order':return None,'Buyurtma allaqachon yuborilgan yoki bekor qilingan.'
        fresh=json.loads(r['data'])
        if fresh!=data:return None,'Buyurtma ma’lumoti yangilandi.'
        expected=stars_total(fresh['qty']) if fresh['kind']=='stars' else param('premium_'+str(fresh['qty']))
        if expected<=0 or expected!=fresh['total']:
            c.execute('DELETE FROM states WHERE user_id=?',(uid,))
            return None,'Narx o‘zgargan. Qayta tanlang.'
        u=c.execute('SELECT balance FROM users WHERE id=?',(uid,)).fetchone()
        if not u or u['balance']<fresh['total']:return None,'Balans yetarli emas.'
        c.execute('UPDATE users SET balance=balance-? WHERE id=?',(fresh['total'],uid))
        c.execute('''INSERT INTO orders(id,user_id,kind,qty,recipient,price,status,created,updated)
                  VALUES(?,?,?,?,?,?,?,?,?)''',
                  (oid,uid,fresh['kind'],fresh['qty'],fresh['recipient'],fresh['total'],'processing',now(),now()))
        c.execute('INSERT INTO ledger(user_id,delta,reason,ref,created) VALUES(?,?,?,?,?)',
                  (uid,-fresh['total'],'order_debit',oid,now()))
        c.execute('DELETE FROM states WHERE user_id=?',(uid,))
    return oid,''


def finish_order(oid, status, reason=''):
    with db(True) as c:
        o=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not o or o['status'] not in ('processing','review'):
            return None
        if status=='refunded':
            # Aynan bir marta refund, ledger reference UNIQUE.
            c.execute('UPDATE users SET balance=balance+? WHERE id=?',(o['price'],o['user_id']))
            c.execute('INSERT INTO ledger(user_id,delta,reason,ref,created) VALUES(?,?,?,?,?)',
                      (o['user_id'],o['price'],'order_refund',oid,now()))
        c.execute('UPDATE orders SET status=?,detail=?,updated=? WHERE id=?',
                  (status,reason[:500],now(),oid))
    return o


def process_purchase(oid):
    if not LIVE_SALES_ENABLED:
        logger.warning('Demo rejim: Fragment API orqali yetkazish bloklandi: %s', oid)
        return
    with db() as c: o=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
    if not o or o['status']!='processing':return
    try:
        path='/stars/buy' if o['kind']=='stars' else '/premium/buy'
        payload={'username':o['recipient']}
        payload['amount' if o['kind']=='stars' else 'duration']=o['qty']
        j=fragment(path,payload)  # ATAYLAB hech qachon avtomatik retry qilinmaydi.
        if j.get('ok') is True:
            finish_order(oid,'completed',str(j.get('result',{}))[:450])
            safe_send(o['user_id'],f'✅ <b>Buyurtma bajarildi!</b>\n🧾 {esc(oid)}\n👤 @{esc(o["recipient"])}')
            notify_admin(f'✅ Buyurtma bajarildi: {oid} | {o["kind"]} {o["qty"]}')
            return
        code=str(j.get('code',''))
        reason=str(j.get('message','API xatosi'))[:220]
        # faqat provayderning aniq deterministik xatolari auto-refund
        safe_codes={'INSUFFICIENT_FUNDS','INSUFFICIENT_GAS','INVALID_AMOUNT_MIN','INVALID_AMOUNT_MAX','INVALID_DURATION','IP_NOT_ALLOWED'}
        if code in safe_codes:
            finish_order(oid,'refunded',code+': '+reason)
            safe_send(o['user_id'],f'❌ Buyurtma bajarilmadi. Pul balansga qaytarildi.\n🧾 {esc(oid)}\nSabab: {esc(reason)}')
            notify_admin(f'⚠️ Buyurtma qaytarildi: {oid} | {esc(code)} {esc(reason)}')
            return
        finish_order(oid,'review',code+': '+reason)
        safe_send(o['user_id'],f'⚠️ Buyurtma #{esc(oid)} tekshiruvda. Pul vaqtincha ushlab turiladi. Qayta buyurtma bermang; admin bilan bog‘laning.')
        notify_admin(f'🚨 BUYURTMA TEKSHIRUVDA\n{oid}\nXato: {esc(code)} {esc(reason)}')
    except Exception as e:
        logger.exception('Buyurtma noaniq natija: %s',oid)
        finish_order(oid,'review',str(e)[:300])
        safe_send(o['user_id'],f'⚠️ {esc(oid)} bo‘yicha API javobi aniq emas. Ikki marta xarid bo‘lmasligi uchun qayta yuborilmadi. Admin tekshiradi.')
        notify_admin(f'🚨 BUYURTMA NOANIQ NATIJA: {oid} — {esc(str(e))[:220]}')


def order_card(uid, oid, admin=False):
    with db() as c: o=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
    if not o or (not admin and o['user_id']!=uid):return safe_send(uid,'Buyurtma topilmadi.')
    text=(f'📦 <b>Buyurtma {esc(o["id"])}</b>\n'
          f'Turi: {"⭐ Stars" if o["kind"]=="stars" else "💎 Premium"}\n'
          f'Miqdori: {o["qty"]}{" ta" if o["kind"]=="stars" else " oy"}\n'
          f'Qabul qiluvchi: @{esc(o["recipient"])}\nNarx: {money(o["price"])}\n'
          f'Holat: <b>{esc(o["status"])}</b>\nSana: {esc(o["created"])}')
    if admin:text+=f'\nUser: <code>{o["user_id"]}</code>\nIzoh: {esc(o["detail"])[:250]}'
    rows=[]
    if admin and o['status']=='review':
        rows=[[('✅ Yetkazilgan deb belgilash','a:done:'+oid)],
              [('↩️ Qaytarish (faqat tekshirib)','a:refund:'+oid)]]
    rows.append([('⬅️ Buyurtmalar','a:orders' if admin else 'u:orders')])
    safe_send(uid,text,keyboard(rows))


def my_orders(uid):
    with db() as c: items=c.execute('SELECT * FROM orders WHERE user_id=? ORDER BY created DESC LIMIT 15',(uid,)).fetchall()
    if not items:return safe_send(uid,'📦 Hozircha buyurtmalaringiz yo‘q.')
    rows=[[(f'{"⭐" if o["kind"]=="stars" else "💎"} {o["qty"]} | {o["status"]} | {o["id"][-6:]}','o:show:'+o['id'])] for o in items]
    safe_send(uid,'📦 <b>Buyurtmalarim</b>',keyboard(rows))


def topup_menu(uid):
    if not check_access(uid):return
    rows=[]
    for pair in ((20000,50000),(100000,200000),(500000,1000000)):
        rows.append([(money(x),f'top:sum:{x}') for x in pair])
    rows += [[('✏️ Boshqa summa','top:other')],[('🏠 Bosh menyu','home')]]
    safe_send(uid,f'💳 <b>Hisob to‘ldirish</b>\nMinimal: {money(param("min_topup"))}\nSummani tanlang:',keyboard(rows))


def shop_id_value():
    """Rasmiy API integer shop_id qabul qiladi; xavfsiz mos turga o'tkazamiz."""
    val = get('shop_id').strip()
    return int(val) if val.isdecimal() else val


def payment_configured():
    # HTTPS URL callback uchun; PythonAnywhere-da background polling ishlatilmaydi.
    return bool(get('shop_id').strip() and read_secret('shop_key'))


def payment_ready():
    return LIVE_SALES_ENABLED and payment_configured() and get('payment_enabled') == '1'


def tolov_request(endpoint, method='GET', payload=None):
    """Exact request pattern from supplied TolovAPI guide.

    POST /api/payment/create, POST /api/payment/cancel and
    GET /api/payment/status?shop_id=...&payment_id=...
    X-Shop-Key header carries the secret; never place it in a URL.
    Returns (HTTP status, JSON object). HTTP 409 must remain observable.
    """
    key = read_secret('shop_key')
    shop = get('shop_id').strip()
    if not key or not shop:
        raise RuntimeError('TolovAPI Shop ID va Shop Key kiritilmagan')
    allowed_methods = {'payment/create':'POST', 'payment/status':'GET', 'payment/cancel':'POST'}
    if endpoint not in allowed_methods or method != allowed_methods[endpoint]:
        raise ValueError('TolovAPI endpoint yoki HTTP method noto‘g‘ri')
    url = TOLOV_BASE + '/' + endpoint
    args = {'shop_id': shop_id_value(), **dict(payload or {})}
    headers = {TOLOV_AUTH_HEADER: key, 'Accept': 'application/json'}
    try:
        if method == 'POST':
            result = requests.post(url, json=args, headers=headers, timeout=(5, 15))
        else:
            result = requests.get(url, params=args, headers=headers, timeout=(5, 15))
        response = result.json()
    except (requests.RequestException, ValueError) as exc:
        # Do not assume a network timeout means the payment has failed.
        raise RuntimeError('TolovAPI dan tasdiqlangan javob olinmadi') from exc
    if not isinstance(response, dict):
        raise RuntimeError('TolovAPI JSON obyekt qaytarmadi')
    return result.status_code, response


def shop_id_value():
    s = get('shop_id').strip()
    return int(s) if s.isdecimal() else s


def verified_provider_status(response, payment):
    """Provider status is obtained with private Shop Key over server-side HTTPS.

    Supplied guide documents ``status``, ``order_id``, ``amount`` and ``test``
    on the paid response, but does NOT promise that status contains payment_id.
    Therefore compare payment_id if returned, never require it to be present.
    Paid credit requires exact order, amount, test:false and the requested ID.
    """
    if not isinstance(response, dict) or not payment:
        return 'mismatch'
    if response.get('ok') is False:
        return 'unavailable'
    status = str(response.get('status') or '').strip().lower()
    if status not in ('paid','pending','cancel'):
        return 'unavailable'
    if response.get('payment_id') is not None and str(response['payment_id']) != str(payment['provider_id']):
        return 'mismatch'
    if response.get('order_id') is not None and str(response['order_id']) != str(payment['id']):
        return 'mismatch'
    if status == 'pending':
        return 'pending'
    if status == 'cancel':
        # This was a direct authenticated status lookup by provider payment_id.
        # Don't mark cancelled if provider explicitly refers to a different order.
        return 'cancel'
    # Strict identity and amount checks only before money is credited.
    if str(response.get('order_id') or '') != str(payment['id']):
        return 'mismatch'
    try:
        amount = Decimal(str(response['amount']))
        if not amount.is_finite() or amount != int(payment['amount']):
            return 'mismatch'
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return 'mismatch'
    if response.get('test') is not False:
        return 'test' if response.get('test') is True else 'unavailable'
    return 'paid'


def apply_verified_payment(pid, new_status):
    """Use an IMMEDIATE transaction: 1 payment = 1 credit ledger row (atomic)."""
    user_id=None; amount=0
    with db(True) as c:
        p=c.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
        if not p:return 'missing'
        if p['status']=='paid':return 'paid'
        if p['status'] not in ('pending','review'):
            return p['status']
        if new_status=='paid':
            c.execute("UPDATE payments SET status='paid',updated=? WHERE id=?",(now(),pid))
            c.execute('UPDATE users SET balance=balance+? WHERE id=?',(p['amount'],p['user_id']))
            c.execute('INSERT INTO ledger(user_id,delta,reason,ref,created) VALUES(?,?,?,?,?)',
                      (p['user_id'],p['amount'],'payment_credit',pid,now()))
            user_id=p['user_id'];amount=p['amount']
        elif new_status=='cancel':
            c.execute("UPDATE payments SET status='cancel',updated=? WHERE id=?",(now(),pid))
        else:
            return 'pending'
    if user_id:
        safe_send(user_id,f'✅ <b>To‘lov tasdiqlandi!</b>\nBalansingizga {money(amount)} qo‘shildi.\n🧾 {esc(pid)}')
        notify_admin(f'✅ TolovAPI tasdiqlangan to‘lov: {esc(pid)} | {money(amount)}')
    return new_status

def normalize_card(number):
    """Karta raqamlarini faqat taqqoslash uchun raqamlarga keltirish."""
    return re.sub(r'\D', '', str(number or ''))


def payment_card_name(api_response, card_number):
    """API ma'lumoti ustuvor; admin ismi faqat shu karta raqami mos bo'lsa."""
    info = api_response.get('card')
    nested = info if isinstance(info, dict) else {}
    names = ('card_holder', 'card_holder_name', 'holder_name', 'card_owner',
             'cardholder', 'cardHolderName', 'owner_name', 'recipient_name', 'fio')
    for source in (api_response, nested):
        for k in names:
            val = source.get(k)
            if isinstance(val, str) and val.strip():
                return val.strip()[:120]
    admin_card = normalize_card(get('payment_card_number'))
    if admin_card and admin_card == normalize_card(card_number):
        return get('payment_card_name').strip()[:120]
    return ''


def payment_expiry(response):
    """Provayder muddati birinchi o'rinda; aks holda admin belgilagan vaqt."""
    ttl = response.get('expires_in')
    if ttl is not None:
        try:
            secs = int(ttl)
            if 0 < secs <= 86400:
                return int(time.time()) + secs
        except (TypeError, ValueError):
            pass
    value = response.get('expires_at') or response.get('expire_at')
    if isinstance(value, (float, int)) and 1_600_000_000 <= float(value) <= 4_000_000_000:
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
            if dt.tzinfo is not None:
                return int(dt.timestamp())
            # Vaqt mintaqasi aytilmagan bo'lsa noto'g'ri muddat hisoblamaymiz.
            logger.warning('TolovAPI expires_at timezone berilmagan: %s', value)
        except ValueError:
            logger.warning('TolovAPI expires_at formatini tanimadik: %s',value[:60])
    return int(time.time()) + 300  # TolovAPI hujjati: aniq 5 daqiqa


def payment_time_text(p):
    expiry = p['expires_at']
    if expiry is None:
        return '⏱ Muddat: provayderda tekshiriladi.'
    secs = int(expiry) - int(time.time())
    if secs <= 0:
        return ('⌛ <b>To‘lov uchun ajratilgan vaqt tugagan.</b>\n'
                '❗ Endi bu rekvizitga pul yubormang. Agar avval to‘lagan bo‘lsangiz, '
                'tekshirish tugmasini bosing. Provayder tasdiqlagan to‘lov hisobga olinadi.')
    minutes, seconds = divmod(secs, 60)
    return f'⏱ Qolgan vaqt: <b>{minutes:02d}:{seconds:02d}</b> (daq:soniya)'


def payment_receipt_text(p, new=False):
    holder = str(p['card_holder'] or '').strip()
    lines = [
        '💳 <b>To‘lov rekvizitlari</b>' if new else '💳 <b>To‘lov ma’lumotlari</b>',
        '',
        f'🏦 Karta raqami: <code>{esc(p["card"])}</code>',
        f'👤 Karta egasi: <b>{esc(holder)}</b>' if holder else '👤 Karta egasi: provayder ko‘rsatmagan',
        f'💰 To‘lov summasi: <b>{money(p["amount"])}</b>',
        f'🧾 To‘lov ID: <code>{esc(p["id"])}</code>',
    ]
    if p['status'] in ('pending','review'):
        lines.extend([payment_time_text(p), '',
                      '❗ Aynan ko‘rsatilgan summani yuboring.',
                      '✅ To‘lov tasdiqlansa, balans avtomatik to‘ladi.'])
    else:
        lines.append(f'📌 Holat: <b>{esc(p["status"])}</b>')
    return '\n'.join(lines)


def reserve_payment_amount(c, requested):
    """Provider identifies transfers by amount. Reserve a different sum per pending payment."""
    # Do not reclaim a temporarily unknown payment amount automatically.
    used={int(x['amount']) for x in c.execute(
        "SELECT amount FROM payments WHERE status IN ('creating','pending','review')").fetchall()}
    for add in range(100):
        if requested+add not in used and requested+add<=param('max_topup'):
            return requested+add
    raise RuntimeError('Hozir bu summa band. Boshqa summa kiriting.')


def create_payment(uid, requested):
    if not payment_ready():
        return safe_send(uid, '⚠️ TolovAPI to‘lovi hali yoqilmagan. Admin bilan bog‘laning.')
    if not param('min_topup') <= requested <= param('max_topup'):
        return safe_send(uid, f'❌ Summa {money(param("min_topup"))}—{money(param("max_topup"))} oralig‘ida bo‘lsin.')
    pid='P'+uuid.uuid4().hex[:15].upper()
    try:
        with db(True) as c:
            amount=reserve_payment_amount(c,requested)
            if amount==requested:
                c.execute('INSERT INTO payments(id,user_id,amount,status,created,updated) VALUES(?,?,?,?,?,?)',
                          (pid,uid,amount,'creating',now(),now()))
    except RuntimeError as exc:
        return safe_send(uid,'⚠️ '+esc(str(exc)))
    if amount!=requested:
        # The invoice must show the precise amount the customer AGREED to.
        # A locally reserved amount is not silently increased by 1 UZS.
        set_state(uid,'payment_suggestion',{'amount':amount})
        return safe_send(uid,
            f'⚠️ {money(requested)} boshqa to‘lovda band.\n'
            f'✅ Yangi aniq summa: <b>{money(amount)}</b>\nRozimisiz?',
            keyboard([[('✅ Roziman','pay:accept_suggest')],[('❌ Boshqa summa','top:menu')]]))
    try:
        code,res=tolov_request('payment/create','POST',{'amount':amount,'order_id':pid})
    except Exception:
        # Network timeout: the payment MAY have been created. Do not auto-retry.
        with db(True) as c:
            c.execute("UPDATE payments SET status='review',updated=? WHERE id=?",(now(),pid))
        notify_admin(f'🚨 TolovAPI to‘lov yaratish NATIJASI NOANIQ: {pid}; {money(amount)}. Shop kabinetini tekshiring.')
        return safe_send(uid,'⚠️ To‘lov yaratishda aloqa uzildi. Hozir pul yubormang; adminga murojaat qiling.')
    # 409 amount_busy: the supplied guide specifically requires user consent.
    if res.get('error_code')=='amount_busy' and code==409:
        with db(True) as c:
            c.execute("UPDATE payments SET status='failed',updated=? WHERE id=?",(now(),pid))
        try:
            offered=int(res['suggested_amount'])
        except (KeyError,ValueError,TypeError):
            return safe_send(uid,'⚠️ Summa band; provayder tavsiya qilgan miqdor noto‘g‘ri. Boshqa summa kiriting.')
        if not param('min_topup') <= offered <= param('max_topup') or offered==amount:
            return safe_send(uid,'⚠️ Taklif qilingan summa mos emas. Boshqa summa kiriting.')
        set_state(uid,'payment_suggestion',{'amount':offered})
        return safe_send(uid,
            f'⚠️ {money(amount)} summasi TolovAPI’da band ekan.\n'
            f'✅ Provayder taklifi: <b>{money(offered)}</b>\n'
            'Aynan taklif qilingan summani yuborishga rozimisiz?',
            keyboard([[('✅ Roziman','pay:accept_suggest')],[('❌ Boshqa summa','top:menu')]]))
    if code >= 400 or res.get('ok') is not True or not res.get('payment_id'):
        uncertain = code>=500 or (code<400 and res.get('ok') is not False and not res.get('error'))
        with db(True) as c:
            c.execute("UPDATE payments SET status=?,updated=? WHERE id=?",('review' if uncertain else 'failed',now(),pid))
        if uncertain:
            notify_admin(f'🚨 TolovAPI create javobi noaniq: {pid}; HTTP {code}.')
            return safe_send(uid,'⚠️ To‘lov natijasi noaniq. Rekvizit berilmadi. Adminga murojaat qiling.')
        return safe_send(uid,'❌ TolovAPI to‘lov yaratishni rad etdi: '+esc(str(res.get('error') or res.get('message') or code))[:250])
    provider=str(res['payment_id'])
    card_info=res.get('card')
    card=str(res.get('card_number') or (card_info.get('number') if isinstance(card_info,dict) else card_info) or '')
    holder=payment_card_name(res,card)
    expiry=payment_expiry(res)
    try:
        actual=Decimal(str(res.get('amount',amount)))
        valid_amount=actual.is_finite() and actual==amount
    except (ValueError,TypeError,InvalidOperation):
        valid_amount=False
    # Supplied documentation lists payment_id, amount, card_number,
    # card_last4 and expires_at, but does NOT require order_id on CREATE.
    optional_order=res.get('order_id')
    order_ok=(optional_order is None or str(optional_order)==pid)
    if not (16<=len(normalize_card(card))<=19 and valid_amount and order_ok):
        with db(True) as c:
            c.execute("UPDATE payments SET status='review',updated=? WHERE id=?",(now(),pid))
        notify_admin(f'🚨 TolovAPI create response mismatch: {pid}; provider={esc(provider)}')
        return safe_send(uid,'⚠️ TolovAPI karta/summa ma’lumoti mos kelmadi. Hozir pul yubormang.')
    try:
        with db(True) as c:
            c.execute("UPDATE payments SET provider_id=?,card=?,card_holder=?,expires_at=?,status='pending',updated=? WHERE id=?",
                      (provider,card,holder,expiry,now(),pid))
    except DB_INTEGRITY_ERRORS:
        with db(True) as c:
            c.execute("UPDATE payments SET status='review',updated=? WHERE id=?",(now(),pid))
        notify_admin('🚨 TolovAPI takroriy payment_id qaytardi: '+esc(pid))
        return safe_send(uid,'⚠️ To‘lovda ID takrorlandi. Admin tekshiradi. Pul yubormang.')
    with db() as c:
        p=c.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
    return safe_send(uid,payment_receipt_text(p,new=True),keyboard([
        [('🔄 To‘lov holatini tekshirish','pay:check:'+pid)],
        [('❌ To‘lovni bekor qilish','pay:cancel:'+pid)],
        [('🏠 Bosh menyu','home')]]))


def verify_payment(pid):
    """Use ONLY authenticated provider status API to grant user funds, once."""
    with db() as c:
        p=c.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
    if not p or not p['provider_id']:
        return 'missing'
    if p['status']=='paid':
        return 'paid'
    try:
        code,res=tolov_request('payment/status','GET',{
            'shop_id':shop_id_value(),'payment_id':p['provider_id']})
    except Exception:
        logger.exception('TolovAPI status tekshiruvi ishlamadi: %s',pid)
        return 'unavailable'
    if code!=200:
        return 'unavailable'
    result=verified_provider_status(res,p)
    if result == 'paid':
        return apply_verified_payment(pid,'paid')
    if result == 'cancel':
        return apply_verified_payment(pid,'cancel')
    if result in ('mismatch','test'):
        notify_admin(f'🚨 TolovAPI status mos kelmadi / test to‘lov: {esc(pid)} — balans qo‘shilmadi.')
    return result

def cancel_pending_payment(uid,pid):
    """Cancel via documented payment/cancel; confirm final state with status API.

    A network failure or pending response does NOT mean the payment is cancelled.
    """
    with db() as c:
        p=c.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
    if not p or p['user_id']!=uid:
        return safe_send(uid,'⛔ Bu to‘lovga ruxsat yo‘q.')
    if p['status'] not in ('pending','review') or not p['provider_id']:
        return payment_card(uid,pid)
    try:
        code,res=tolov_request('payment/cancel','POST',{'payment_id':p['provider_id']})
    except Exception:
        return safe_send(uid,'⚠️ Bekor qilish javobi olinmadi. To‘lov holatini tekshiring.')
    if code>=500:
        return safe_send(uid,'⚠️ Provayder javob bermadi. To‘lov hali faol bo‘lishi mumkin.')
    result=verify_payment(pid)
    if result=='paid':
        return safe_send(uid,'✅ To‘lov avval tasdiqlangan — balansingizga qo‘shildi.')
    if result=='cancel':
        return safe_send(uid,'✅ TolovAPI to‘lovni bekor qilganini tasdiqladi.')
    safe_send(uid,'⏳ Bekor qilish hali tasdiqlanmadi. To‘lov oynasidagi kartaga pul yubormang.')
    return payment_card(uid,pid)


def payment_sweep(limit=3):
    """Optional monitoring. It never credits an unsigned payment."""
    if not LIVE_SALES_ENABLED or not payment_configured():return 0
    with db() as c:
        ids=[r['id'] for r in c.execute(
            "SELECT id FROM payments WHERE provider_id IS NOT NULL AND status IN ('pending','review') ORDER BY updated ASC LIMIT ?",(limit,)).fetchall()]
    for pid in ids:
        try:verify_payment(pid)
        except Exception:logger.exception('payment sweep: %s',pid)
        with db(True) as c:c.execute("UPDATE payments SET updated=? WHERE id=?",(now(),pid))
    return len(ids)

def payment_card(uid,pid,admin=False):
    with db() as c: p=c.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
    if not p or (not admin and p['user_id']!=uid): return safe_send(uid,'To‘lov topilmadi.')
    text=payment_receipt_text(p) + f'\n📅 Sana: {esc(p["created"])}' 
    rows = [[('🔄 To‘lovni qayta tekshirish', 'pay:check:'+pid)],
            [('❌ To‘lovni bekor qilish','pay:cancel:'+pid)]] if p['status'] in ('pending','review') else []
    if rows: safe_send(uid,text,keyboard(rows))
    else: safe_send(uid,text)


def balance_history(uid):
    with db() as c: rows=c.execute('SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 15',(uid,)).fetchall()
    if not rows:return safe_send(uid,'🧾 Hozircha tranzaksiyalar yo‘q.')
    text='🧾 <b>Balans harakatlari</b>\n\n'
    for r in rows:
        text+=f'{"➕" if r["delta"]>0 else "➖"} {money(abs(r["delta"]))} | {esc(r["reason"])} | {esc(r["created"][:16])}\n'
    safe_send(uid,text[:4000])


def profile(uid):
    r=user_row(uid)
    if not r:return
    with db() as c:
        x=c.execute("SELECT COUNT(*) as n, COALESCE(SUM(CASE WHEN status='completed' THEN price ELSE 0 END),0) AS sold FROM orders WHERE user_id=?",(uid,)).fetchone()
    safe_send(uid,f'💰 <b>Hisobim</b>\n\n👤 {esc(r["name"])}\n'
              f'🔗 @{esc(r["username"] or "username yo‘q")}\n🆔 <code>{uid}</code>\n'
              f'💳 Balans: <b>{money(r["balance"])}</b>\n'
              f'📦 Buyurtmalar: {x["n"]} ta\n🛍 Bajarilgan xarid: {money(x["sold"])}',
              keyboard([[('🧾 Tranzaksiyalar tarixi','u:ledger')],[('💳 Hisob to‘ldirish','top:menu')]]))


def report_ticket(uid, m):
    with db(True) as c:
        t=c.execute('INSERT INTO tickets(user_id,status,created,updated) VALUES(?,?,?,?)',(uid,'open',now(),now()))
        tid=t.lastrowid
        c.execute('INSERT INTO ticket_messages(ticket_id,sender_id,body,created) VALUES(?,?,?,?)',
                  (tid,uid,str(m.text or m.caption or '['+m.content_type+']')[:2000],now()))
    k=keyboard([[('↩️ Javob berish',f't:reply:{tid}'),('✅ Yopish',f't:close:{tid}')]])
    for a in admin_ids():
        safe_send(a,f'📩 <b>Yangi murojaat #{tid}</b>\nUser: <code>{uid}</code>\nIsm: {esc(m.from_user.first_name)}',k)
        try:bot.copy_message(a,uid,m.message_id)
        except Exception:pass
    safe_send(uid,f'✅ Murojaatingiz #{tid} adminga yuborildi. Javob shu botga keladi.')
    clear_state(uid)


def admin_star(uid):
    price = param('star_price')
    with db() as c:
        packs = c.execute('SELECT * FROM packages ORDER BY amount').fetchall()
    pack_text = '\n'.join(
        f' {"✅" if p["enabled"] else "❌"} {p["amount"]:,} ⭐ = '
        + (money(stars_total(p['amount'], price)) if price > 0 else 'narx kiritilmagan')
        for p in packs
    ) or 'To‘plam mavjud emas.'
    safe_send(uid,
              f'⭐ <b>Stars narxlarini boshqarish</b>\n\n'
              f'💵 <b>1 ⭐ = {money(price) if price > 0 else "Narx belgilanmagan"}</b>\n'
              f'🧮 Barcha to‘plamlar va boshqa miqdor avtomatik hisoblanadi.\n'
              f'📏 Chegara: {param("min_stars"):,}–{param("max_stars"):,} ⭐\n\n'
              f'<b>To‘plam narxlari:</b>\n{pack_text}',
              keyboard([[('✏️ 1 Stars narxini o‘zgartirish','a:input:star_price')],
                        [('➕ To‘plam qo‘shish','a:input:pack_add'),('🗑 To‘plam o‘chirish','a:input:pack_del')],
                        [('🔢 Minimal miqdor','a:input:min_stars'),('🔢 Maksimal miqdor','a:input:max_stars')]]))


def admin_premium(uid):
    rows=[[(f'💎 {m} oy — {money(param("premium_"+str(m)))}',f'a:input:premium_{m}')] for m in (3,6,12)]
    safe_send(uid,'💎 <b>Premium narxlari</b>\nTanlang va so‘mda yangi narx kiriting.',keyboard(rows))


def admin_orders(uid):
    with db() as c:items=c.execute('SELECT * FROM orders ORDER BY created DESC LIMIT 20').fetchall()
    if not items:return safe_send(uid,'📦 Hozircha buyurtmalar yo‘q.')
    safe_send(uid,'📦 <b>Oxirgi 20 buyurtma</b>',keyboard([[(f'{o["id"][-6:]} | {o["status"]} | {o["qty"]}', 'a:order:'+o['id'])] for o in items]))


def admin_payments(uid):
    with db() as c:items=c.execute('SELECT * FROM payments ORDER BY created DESC LIMIT 15').fetchall()
    if not items:return safe_send(uid,'💳 To‘lovlar hali yo‘q.')
    safe_send(uid,'💳 <b>Oxirgi to‘lovlar</b>',keyboard([[(f'{p["id"][-7:]} | {money(p["amount"])} | {p["status"]}','a:pay:'+p['id'])] for p in items]))


def admin_stats(uid):
    with db() as c:
        u=c.execute('SELECT COUNT(*) n, COALESCE(SUM(balance),0) b FROM users').fetchone()
        o=c.execute('''SELECT COUNT(*) n,
          COALESCE(SUM(CASE WHEN status='completed' THEN price ELSE 0 END),0) revenue,
          SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) done,
          SUM(CASE WHEN status='review' THEN 1 ELSE 0 END) review FROM orders''').fetchone()
        p=c.execute("SELECT COALESCE(SUM(amount),0) n FROM payments WHERE status='paid'").fetchone()
    safe_send(uid,f'📊 <b>Statistika</b>\n\n👥 Userlar: {u["n"]}\n💰 User balanslari yig‘indisi: {money(u["b"])}\n'
              f'📦 Buyurtmalar: {o["n"]}\n✅ Bajarilgan: {o["done"] or 0}\n'
              f'⚠️ Tekshiruvda: {o["review"] or 0}\n🛍 Bajarilgan savdo: {money(o["revenue"])}\n'
              f'💳 Tasdiqlangan to‘lovlar: {money(p["n"])}\n\n'
              'ℹ️ Sof foyda emas: API tannarxi, kurs va komissiya alohida hisoblanadi.')


def admin_integrations(uid):
    if not only_owner(uid):return safe_send(uid,'🔒 Faqat bosh admin API sozlaydi.')
    public = get('public_url').rstrip('/') or PUBLIC_URL
    url = (public + '/tolovapi/complete/' + get('callback_token')) if public.startswith('https://') else ''
    prepare_url=''  # TolovAPI prepare endpoint format tasdiqlanmagan
    monitor_url = (public + '/internal/payment-check/' + get('payment_monitor_token')) if url else ''
    text = (f'🔌 <b>API va to‘lov sozlamalari</b>\n\n'
            f'⭐ Fragment API: {mask_secret("fragment_key")}\n'
            f'💳 TolovAPI Shop ID: {esc(get("shop_id") or "❌ Kiritilmagan")}\n'
            f'🔑 TolovAPI Shop Key: {mask_secret("shop_key")}\n'
            f'🔏 Callback imzo kaliti: {mask_secret("tolov_webhook_secret")}\n'
            f'🏦 Karta (moslik uchun): {esc(get("payment_card_number") or "Kiritilmagan")}\n'
            f'👤 Karta egasi: {esc(get("payment_card_name") or "Kiritilmagan")}\n'
            '⏱ To‘lov muddati: <b>5 daqiqa (TolovAPI)</b>\n'
            f'🌐 TolovAPI: <code>{TOLOV_BASE}</code>\n'
            f'💱 USD/UZS: {param("usd_rate")}\n'
            f'Avtomatik to‘lov: {"✅ FAOL" if payment_ready() else "🛑 O‘CHIRILGAN"}\n\n'
            f'🔗 complete_url: <code>{esc(url) if url else "PUBLIC_URL kiritilmagan"}</code>\n'
            
            f'🔐 Monitoring URL: <code>{esc(monitor_url) if monitor_url else "PUBLIC_URL kiritilmagan"}</code>\n\n'
            '✅ Callback — faqat signal; Shop Key bilan status API tekshirilib, paid+test=false tasdiqlansa balans to‘ladi.\n'
            '⏱ 5 daqiqalik TolovAPI hisob va X-Signature imzo qo‘llab-quvvatlanadi.\n'
            '✅ TolovAPI so‘rov formati foydalanuvchi taqdim etgan API qo‘llanmasiga mos. Jonli test zarur.\n'
            + ('🟢 Haqiqiy savdo rejimi yoqilgan.' if LIVE_SALES_ENABLED else '🔒 Demo rejim: haqiqiy savdo bloklangan.'))
    safe_send(uid,text,keyboard([
        [('⭐ Fragment API Key','a:input:fragment_key')],
        [('🆔 Shop ID','a:input:shop_id'),('🔑 Shop Key','a:input:shop_key')],
        [('🔏 Webhook imzo kaliti','a:input:tolov_webhook_secret')],
        [('🏦 Karta raqami (moslik)','a:input:payment_card_number')],
        [('👤 Karta egasi ismi','a:input:payment_card_name')],
        [('🌐 Bot HTTPS domeni','a:input:public_url')],
        [('💱 USD/UZS kursi','a:input:usd_rate')],
        [('🔎 Fragment test','a:testfrag'),('💼 Fragment hamyon','a:wallet')],
        [('🔎 TolovAPI test','a:testpay')],
        [('🔴/🟢 Avto to‘lovni boshqarish','a:paytoggle')],
    ]))


def admin_channels(uid):
    with db() as c:chs=c.execute("SELECT * FROM settings WHERE key LIKE 'channel_%' ORDER BY key").fetchall()
    lines='\n'.join(esc(x['val'].split('|')[0]) for x in chs)
    safe_send(uid,'📣 <b>Majburiy obuna kanallari</b>\n'+(lines or 'Hozircha yo‘q'),
              keyboard([[('➕ Kanal qo‘shish','a:input:channel_add'),('🗑 Kanal o‘chirish','a:input:channel_del')]]))


def admin_admins(uid):
    with db() as c:admins=[x['id'] for x in c.execute('SELECT id FROM admins').fetchall()]
    safe_send(uid,'👑 <b>Adminlar</b>\nBosh admin: '+str(OWNER_ID)+'\n'+('\n'.join(map(str,admins)) or 'Qo‘shimcha admin yo‘q.'),
              keyboard([[('➕ Admin qo‘shish','a:input:admin_add'),('🗑 Admin o‘chirish','a:input:admin_del')]]))


def admin_settings(uid):
    safe_send(uid,'⚙️ <b>Bot sozlamalari</b>\n\n'
              f'Texnik rejim: {"YOQILGAN" if get("maintenance")=="1" else "O‘CHIRILGAN"}\n'
              f'Min to‘ldirish: {money(param("min_topup"))}',
              keyboard([[('🛠 Texnik rejimni almashtirish','a:maintenance')],
                        [('💳 Min to‘ldirish','a:input:min_topup')],
                        [('✍️ Salomlashuv matni','a:input:welcome')]]))


def admin_users(uid):
    safe_send(uid,'👥 Foydalanuvchi ID raqamini kiriting.',keyboard([[('🔎 ID bo‘yicha qidirish','a:input:user_find')]]))


def user_admin_card(uid,target):
    r=user_row(target)
    if not r:return safe_send(uid,'Bunday foydalanuvchi bazada yo‘q.')
    safe_send(uid,f'👤 <b>Foydalanuvchi</b>\n🆔 <code>{target}</code>\n'
              f'Ism: {esc(r["name"])}\n@{esc(r["username"])}\n'
              f'Balans: <b>{money(r["balance"])}</b>\nBloklangan: {"HA" if r["banned"] else "YO‘Q"}',
              keyboard([[('➕/➖ Balansni tahrirlash','a:balance:'+str(target))],
                        [('🚫 Bloklash / ochish','a:ban:'+str(target))]]))


def admin_tickets(uid):
    with db() as c:ts=c.execute('SELECT * FROM tickets ORDER BY created DESC LIMIT 15').fetchall()
    if not ts:return safe_send(uid,'🎧 Murojaatlar yo‘q.')
    safe_send(uid,'🎧 <b>Murojaatlar</b>',keyboard([[(f'#{t["id"]} User {t["user_id"]} | {t["status"]}',f't:show:{t["id"]}')] for t in ts]))


def ticket_show(uid,tid):
    with db() as c:
        t=c.execute('SELECT * FROM tickets WHERE id=?',(tid,)).fetchone()
        msgs=c.execute('SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 7',(tid,)).fetchall()
    if not t:return safe_send(uid,'Murojaat topilmadi.')
    text=f'🎧 <b>Murojaat #{tid}</b>\nUser: <code>{t["user_id"]}</code>\nHolat: {esc(t["status"])}\n\n'
    for m in reversed(msgs):text+=f'{"👤" if m["sender_id"]==t["user_id"] else "👑"}: {esc(m["body"])[:250]}\n'
    safe_send(uid,text[:3500],keyboard([[('↩️ Javob berish',f't:reply:{tid}'),('✅ Yopish',f't:close:{tid}')]]))


def broadcast_admin(uid):
    if not is_admin(uid):return
    set_state(uid,'admin_broadcast')
    safe_send(uid,'📢 Hammaga yuboriladigan matn, rasm yoki video yuboring.\nBekor qilish: /cancel')


def admin_input(uid,k):
    owner_keys={'fragment_key','shop_id','shop_key','tolov_webhook_secret','public_url','usd_rate','admin_add','admin_del'}
    if k in owner_keys and not only_owner(uid):return safe_send(uid,'🔒 Faqat bosh admin.')
    allowed={'star_price','pack_add','pack_del','min_stars','max_stars','premium_3','premium_6','premium_12',
             'fragment_key','shop_id','shop_key','tolov_webhook_secret','public_url','usd_rate','channel_add','channel_del',
             'admin_add','admin_del','min_topup','welcome','user_find',
             'payment_card_number','payment_card_name','payment_minutes'}
    if k not in allowed:return
    set_state(uid,'admin_input',{'key':k})
    prompt={
      'star_price':'1 ⭐ narxini so‘mda kiriting (masalan, 250).\nBot 50, 100, 500 va boshqa miqdorlarning narxini o‘zi hisoblaydi.',
      'fragment_key':'Fragment API Key ni yuboring (xabar o‘chiriladi).',
      'shop_id':'Shop ID ni yuboring.', 'shop_key':'Shop Key ni yuboring (xabar o‘chiriladi).',
      'tolov_webhook_secret':'TolovAPI webhook HMAC imzo kalitini kiriting. Bu kalit provayderdagi webhook secret bilan MOS bo‘lsin.',
      'payment_card_number':'API qaytaradigan karta bilan mos kelishi uchun karta raqamini kiriting (16–19 raqam). Avtomatik to‘lovda faqat API qaytargan karta ko‘rsatiladi.',
      'payment_card_name':'Karta egasining ismi va familiyasini kiriting. Bu ism faqat API kartasi admin kiritgan karta raqamiga mos bo‘lsa ishlatiladi.',
      'payment_minutes':'Provayder muddat bermasa ko‘rsatiladigan vaqt: 5–30 daqiqa. Provayder qaytargan haqiqiy muddat ustun.',
      'public_url':'Botning tashqi HTTPS manzilini yuboring (masalan https://myshop.onrender.com).',
      'usd_rate':'1 USD necha so‘m? (Masalan: 12500).',
      'channel_add':'Kanalni kiriting: @kanal yoki -100... ID. Private kanal uchun: -100...|https://t.me/+invite',
      'channel_del':'O‘chiriladigan kanal @username yoki ID sini kiriting.',
      'admin_add':'Yangi admin Telegram ID sini yuboring.', 'admin_del':'O‘chirish uchun admin ID sini yuboring.',
      'pack_add':'Stars to‘plam miqdorini yuboring (masalan 750).',
      'pack_del':'O‘chirish uchun Stars to‘plam miqdorini yuboring.',
      'user_find':'Foydalanuvchining Telegram ID sini kiriting.',
      'min_topup':'Minimal to‘lov summasini so‘mda yuboring.',
      'min_stars':'Minimal Stars miqdorini kiriting (kamida 50).',
      'max_stars':'Maksimal Stars miqdorini kiriting.',
      'welcome':'Yangi salomlashuv matnini yuboring. HTML ishlatmang.'
    }
    safe_send(uid,'✏️ '+prompt.get(k,'Yangi qiymatni yuboring.'))


def handle_admin_input(m,data):
    uid=m.from_user.id
    k=data.get('key','')
    if not is_admin(uid):return
    if k in {'fragment_key','shop_id','shop_key','tolov_webhook_secret','public_url','usd_rate','admin_add','admin_del',
              'payment_card_number','payment_card_name','payment_minutes'} and not only_owner(uid):return
    value=(m.text or '').strip()
    if not value:return safe_send(uid,'Matn yuboring.')
    try:
        if k in {'fragment_key','shop_key','tolov_webhook_secret'}:
            set_secret(k,value)
            try:bot.delete_message(uid,m.message_id)
            except Exception:pass
            clear_state(uid);safe_send(uid,'✅ Maxfiy kalit shifrlangan holda saqlandi.');return
        if k=='shop_id':
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
                raise ValueError('TolovAPI Shop ID formatini tekshiring.')
            put(k,value)
        elif k=='payment_card_number':
            cardval = normalize_card(value)
            if not 16 <= len(cardval) <= 19 or not value.replace(' ','').isdigit():
                raise ValueError('Karta raqami 16–19 raqamdan iborat bo‘lsin.')
            put(k,cardval)
        elif k=='payment_card_name':
            if not 3<=len(value)<=120 or not re.fullmatch(r"[\w .\-’'ʻҒғЎўҚқҲҳЁёА-Яа-я]+",value,re.UNICODE):
                raise ValueError('Karta egasining ismini to‘g‘ri yuboring (3–120 belgi).')
            put(k,value)
        elif k=='payment_minutes':
            if not value.isdecimal() or not 5 <= int(value) <= 30:
                raise ValueError('To‘lov muddati 5–30 daqiqa bo‘lsin.')
            put(k,value)
        elif k in ('public_url'):
            if not re.fullmatch(r'https://[a-zA-Z0-9.-]+(?::\d{2,5})?',value.rstrip('/')):
                raise ValueError('Masalan: https://shop.onrender.com')
            put(k,value.rstrip('/'))
        elif k=='channel_add':
            parts=value.split('|',1)
            chat=parts[0].strip()
            if not (re.fullmatch(r'@[A-Za-z0-9_]{5,32}',chat) or re.fullmatch(r'-100\d{5,20}',chat)):
                raise ValueError('Kanal: @username yoki -100... formatida bo‘lsin.')
            channel=bot.get_chat(chat)
            member=bot.get_chat_member(channel.id,bot.get_me().id)
            if member.status not in ('creator','administrator'):
                raise ValueError('Bot avval kanalga admin qilinishi kerak.')
            link=parts[1].strip() if len(parts)>1 else ''
            if link and not re.fullmatch(r'https://t\.me/[A-Za-z0-9_+/-]+',link):
                raise ValueError('Taklif havolasi noto‘g‘ri.')
            put('channel_'+str(channel.id),str(channel.id)+'|'+(link or ('https://t.me/'+chat[1:] if chat.startswith('@') else '')))
        elif k=='channel_del':
            with db(True) as c:
                entries=c.execute("SELECT key,val FROM settings WHERE key LIKE 'channel_%'").fetchall()
                found=[r['key'] for r in entries if value in r['val'].split('|',1)[0] or value==r['key'].replace('channel_','')]
                if not found:
                    try: found=['channel_'+str(bot.get_chat(value).id)]
                    except Exception:pass
                for key in found:c.execute('DELETE FROM settings WHERE key=?',(key,))
            if not found:raise ValueError('Kanal topilmadi.')
        elif k in ('admin_add','admin_del'):
            n=int(value)
            if n<=0 or n==OWNER_ID:raise ValueError('Bosh adminni o‘zgartirish mumkin emas.')
            with db(True) as c:
                if k=='admin_add':c.execute('INSERT OR IGNORE INTO admins(id) VALUES(?)',(n,))
                else:c.execute('DELETE FROM admins WHERE id=?',(n,))
        elif k=='user_find':
            n=int(value)
            clear_state(uid)
            return user_admin_card(uid,n)
        elif k=='welcome':
            if len(value)>3500:raise ValueError('Matn juda uzun.')
            put(k,html.escape(value))
        else:
            n=int(value.replace(' ','').replace(',',''))
            if k in ('pack_add','pack_del'):
                if not 50<=n<=10000000:raise ValueError('50—10 000 000 oralig‘i.')
                with db(True) as c:
                    if k=='pack_add':c.execute('INSERT OR REPLACE INTO packages(amount,enabled) VALUES(?,1)',(n,))
                    else:c.execute('DELETE FROM packages WHERE amount=?',(n,))
            else:
                if n<0 or n>1000000000:raise ValueError('Noto‘g‘ri son.')
                if k=='star_price' and n<=0:raise ValueError('1 Stars narxi 0 dan katta bo‘lishi kerak.')
                if k=='min_stars' and n<50:raise ValueError('Fragment API minimal 50 Stars.')
                if k=='max_stars' and not 50<=n<=10000000:raise ValueError('Chegara 50—10 000 000.')
                if k=='usd_rate' and n<1000:raise ValueError('USD/UZS kursi juda kichik.')
                if k=='min_topup' and n<1000:raise ValueError('Minimal 1000 so‘m.')
                put(k,n)
        clear_state(uid)
        if k=='star_price':
            safe_send(uid, f'✅ 1 ⭐ narxi {money(n)} qilib belgilandi.\n'
                           'Barcha Stars to‘plamlari va ixtiyoriy miqdor narxi yangilandi.')
            return admin_star(uid)
        safe_send(uid,'✅ Sozlama saqlandi.')
    except (ValueError,TypeError) as e:
        safe_send(uid,'❌ '+esc(str(e))[:280]+'\nQayta yuboring.')
    except Exception:
        logger.exception('Admin input error')
        safe_send(uid,'❌ Xatolik. Qiymatni qayta tekshiring.')


def handle_admin_adjust(m,data):
    uid=m.from_user.id
    target=int(data['user'])
    s=(m.text or '').replace(' ','').strip()
    if not re.fullmatch(r'[+-]\d{1,10}',s):
        return safe_send(uid,'Masalan +50000 yoki -10000 kiriting.')
    delta=int(s)
    if delta==0 or abs(delta)>10000000:return safe_send(uid,'Bir urinishda ko‘pi bilan 10 mln so‘m.')
    ref='A'+uuid.uuid4().hex[:12]
    with db(True) as c:
        r=c.execute('SELECT balance FROM users WHERE id=?',(target,)).fetchone()
        if not r:return safe_send(uid,'Foydalanuvchi topilmadi.')
        if r['balance']+delta<0:return safe_send(uid,'❌ Balans manfiy bo‘lib qoladi.')
        c.execute('UPDATE users SET balance=balance+? WHERE id=?',(delta,target))
        c.execute('INSERT INTO ledger(user_id,delta,reason,ref,created) VALUES(?,?,?,?,?)',
                  (target,delta,f'admin_{uid}',ref,now()))
    clear_state(uid)
    safe_send(uid,f'✅ Balans o‘zgardi: {delta:+,} so‘m')
    safe_send(target,f'💳 Balansingiz administrator tomonidan {money(abs(delta))} ga {"to‘ldirildi" if delta>0 else "kamaytirildi"}.')


def send_broadcast(m):
    """WSGI worker ichida background thread emas, saqlanadigan yuborish navbati."""
    uid=m.from_user.id
    if not is_admin(uid):return
    clear_state(uid)
    with db() as c: ids=[r['id'] for r in c.execute('SELECT id FROM users WHERE banned=0')]
    job_id='B'+uuid.uuid4().hex[:14].upper()
    with db(True) as c:
        c.executemany('INSERT INTO broadcast_queue(job_id,from_chat,source_message,target_id,status,created) VALUES(?,?,?,?,?,?)',
                      [(job_id,uid,m.message_id,i,'pending',now()) for i in ids])
    safe_send(uid,f'📢 Xabar {len(ids)} foydalanuvchi navbatiga qo‘shildi.\n'
                  '⏱ Avval bir nechtasiga yuboriladi, qolgani monitoring URL orqali davom etadi.')
    drain_broadcast(limit=3)


def drain_broadcast(limit=5):
    with db() as c:
        rows=c.execute("SELECT * FROM broadcast_queue WHERE status='pending' ORDER BY id LIMIT ?",(limit,)).fetchall()
    sent=0
    for row in rows:
        # Bir xil qatorni boshqa worker parallel yubormasligi uchun atomic claim.
        with db(True) as c:
            claim=c.execute("UPDATE broadcast_queue SET status='sending' WHERE id=? AND status='pending'",(row['id'],))
        if not claim.rowcount:continue
        try:
            bot.copy_message(row['target_id'],row['from_chat'],row['source_message'])
            state='sent';sent+=1
        except Exception:
            state='error';logger.exception('Broadcast user: %s',row['target_id'])
        with db(True) as c:
            c.execute('UPDATE broadcast_queue SET status=? WHERE id=?',(state,row['id']))
        time.sleep(0.07)
    return sent


@app.get('/')
@app.get('/health')
def site_home():
    return jsonify({'ok':True,'service':'Stars Shop / Render Webhook','sales_enabled':LIVE_SALES_ENABLED})


@app.get('/internal/payment-check/<token>')
def internal_payment_check(token):
    """Maxfiy monitoring endpoint. UptimeRobot HTTP(s) monitor yoki cron uchun."""
    if not hmac.compare_digest(token, get('payment_monitor_token')):
        return jsonify({'ok': False}), 403
    count=payment_sweep(limit=6)
    sent=drain_broadcast(limit=4)
    return jsonify({'ok':True,'payments_checked':count,'broadcast_sent':sent})


@app.post('/telegram/<token>')
def telegram_webhook(token):
    """pyTelegramBotAPI uchun Telegram webhook; majburiy header secret va update dedup."""
    if not hmac.compare_digest(token, get('telegram_webhook_path')):
        return jsonify({'ok':False}),403
    header=request.headers.get('X-Telegram-Bot-Api-Secret-Token','')
    if not hmac.compare_digest(header, get('telegram_header_secret')):
        return jsonify({'ok':False}),403
    if not request.is_json or len(request.data)>1024*1024:
        return jsonify({'ok':False,'error':'JSON required'}),415
    try:
        raw=request.get_json()
        update_id=int(raw['update_id'])
        update=telebot.types.Update.de_json(json.dumps(raw))
    except (KeyError,ValueError,TypeError):
        return jsonify({'ok':False,'error':'invalid update'}),400
    # Xuddi shu update Telegram tomonidan takror yuborilsa, ikki marta bajarmaymiz.
    with db(True) as c:
        row=c.execute('SELECT status FROM telegram_updates WHERE update_id=?',(update_id,)).fetchone()
        if row:
            return jsonify({'ok':True,'duplicate':True})
        c.execute('INSERT INTO telegram_updates(update_id,status,arrived) VALUES(?,?,?)',
                  (update_id,'processing',now()))
    try:
        bot.process_new_updates([update])
    except Exception:
        logger.exception('Telegram webhook update failed: %s',update_id)
        with db(True) as c:
            c.execute("UPDATE telegram_updates SET status='failed' WHERE update_id=?",(update_id,))
        # Fail-closed: API buyurtma bajarilgan-bajarilmagan noma'lum bo'lishi mumkin.
        return jsonify({'ok':False,'error':'update failed; manual review'}),500
    with db(True) as c:
        c.execute("UPDATE telegram_updates SET status='done' WHERE update_id=?",(update_id,))
        # Cheklanmagan ko'payishni oldini olish (so'nggi 20 ming update saqlanadi).
        c.execute('DELETE FROM telegram_updates WHERE update_id < (SELECT COALESCE(MAX(update_id),0)-20000 FROM telegram_updates)')
    return jsonify({'ok':True})


@app.post('/tolovapi/complete/<token>')
@app.post('/payments/complete/<token>')
def tolovapi_complete(token):
    """Callback = untrusted notification only. Server verifies by status API.

    Never grant money based on incoming callback's claimed `paid` status.
    """
    if not hmac.compare_digest(token,get('callback_token')):
        return jsonify(ok=False,error='forbidden'),403
    if not LIVE_SALES_ENABLED:
        return jsonify(ok=False,error='live sales disabled'),503
    # Example from supplied guide: X-Signature = HMAC-SHA256(raw_body, WEBHOOK_SECRET).
    # Shop ID/Key alone do not configure the provider callback secret.
    signing_key=read_secret('tolov_webhook_secret') or os.getenv('TOLOV_WEBHOOK_SECRET','').strip()
    if signing_key:
        received=request.headers.get('X-Signature','')
        expected=hmac.new(signing_key.encode(),request.get_data(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received.lower(),expected):
            return jsonify(ok=False,error='bad signature'),403
    # Even if the provider is not yet configured to sign, this URL's random
    # token is only a notification; money is NEVER credited from callback body.
    data=request.get_json(silent=True) if request.is_json else request.form.to_dict()
    if not isinstance(data,dict):
        return jsonify(ok=False,error='invalid body'),400
    provider=str(data.get('payment_id') or '')
    order=str(data.get('order_id') or '')
    if not provider and not order:
        return jsonify(ok=False,error='missing payment reference'),400
    with db() as c:
        p=c.execute('SELECT id FROM payments WHERE provider_id=? OR id=? LIMIT 1',
                    (provider, order)).fetchone()
    if not p:
        return jsonify(ok=False,error='unknown payment'),404
    result=verify_payment(p['id'])
    if result in ('unavailable','mismatch'):
        return jsonify(ok=False,status=result),503
    return jsonify(ok=True,status=result),200

@bot.message_handler(commands=['start','menu','admin','cancel','id'])
def commands(m):
    if m.chat.type!='private':return
    user_record(m.from_user)
    uid=m.from_user.id
    if m.text.startswith('/id'):
        return safe_send(uid,f'🆔 Telegram ID: <code>{uid}</code>')
    if m.text.startswith('/cancel'):
        main_menu(uid);return
    if m.text.startswith('/admin'):
        return admin_menu(uid) if is_admin(uid) else safe_send(uid,'⛔ Admin huquqi yo‘q.')
    if not check_access(uid):return
    main_menu(uid)


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    uid=call.from_user.id
    if call.message.chat.type!='private':return
    user_record(call.from_user)
    s=call.data or ''
    try:bot.answer_callback_query(call.id)
    except Exception:pass
    if s=='sub:check':
        if check_access(uid):main_menu(uid)
        return
    if s=='home':return main_menu(uid)
    if (s.startswith('a:') or (s.startswith('t:') and s not in ('t:self','t:other'))) and not is_admin(uid):
        return safe_send(uid,'⛔ Ruxsat yo‘q.')
    if s.startswith('a:') and not check_access(uid):return
    if s in ('s:custom','top:other'):
        if not check_access(uid):return
        set_state(uid,'stars_custom' if s=='s:custom' else 'topup_other')
        return safe_send(uid,f'🔢 Stars miqdorini kiriting ({param("min_stars"):,}–{param("max_stars"):,}).\nNarx: miqdor × {money(param("star_price"))}' if s=='s:custom' else '💳 To‘lov summasini so‘mda kiriting:')
    if s.startswith('s:pack:'):
        if not check_access(uid):return
        return choose_target(uid,'stars',int(s.split(':')[2]))
    if s.startswith('p:pack:'):
        if not check_access(uid):return
        return choose_target(uid,'premium',int(s.split(':')[2]))
    if s in ('t:self','t:other'):
        if not check_access(uid):return
        step,data=state(uid)
        if step!='choose_target':return safe_send(uid,'Xaridni qaytadan boshlang.')
        set_state(uid,'enter_recipient',data)
        if s=='t:self' and call.from_user.username:
            username=extract_username(call.from_user.username)
            if username:return preview_recipient(uid,username)
        return safe_send(uid,('🔗 Sizda umumiy username yo‘q. Telegram profilingizga username qo‘ying va @username yoki t.me/username yuboring.'
                             if s=='t:self' else '🎁 Qabul qiluvchining @username yoki https://t.me/username havolasini yuboring.'))
    if s=='buy:confirm':
        if not check_access(uid):return
        step,data=state(uid)
        if step!='review_order':return safe_send(uid,'Ushbu buyurtma bekor qilingan yoki allaqachon yuborilgan.')
        if not read_secret('fragment_key'):return safe_send(uid,'❌ API kaliti sozlanmagan.')
        ok,reason=safe_price_check(data['kind'],data['qty'],data['total'])
        if not ok:
            notify_admin('⚠️ Sotuv tannarxi tekshiruvi: '+esc(reason))
            return safe_send(uid,'⚠️ Hozir buyurtma berib bo‘lmaydi. Admin narxlarni tekshiradi.')
        oid,msg=debit_and_create(uid,data)
        if not oid:return safe_send(uid,'❌ '+esc(msg))
        safe_send(uid,f'⏳ Buyurtma #{esc(oid)} qabul qilindi. API orqali yuborilmoqda...')
        process_purchase(oid)  # WSGI: PythonAnywhere web worker fon threadlarini qo‘llab-quvvatlamaydi.
        return
    if s=='top:menu':return topup_menu(uid)
    if s.startswith('top:sum:'):
        if not check_access(uid):return
        return create_payment(uid,int(s.split(':')[2]))
    if s=='pay:accept_suggest':
        if not check_access(uid):return
        step,data=state(uid)
        if step!='payment_suggestion':return safe_send(uid,'Summa taklifi eskirgan. Qayta boshlang.')
        clear_state(uid)
        return create_payment(uid,int(data['amount']))
    if s.startswith('pay:cancel:'):
        if not check_access(uid):return
        pid=s.split(':',2)[2]
        return cancel_pending_payment(uid,pid)
    if s.startswith('pay:check:'):
        pid=s.split(':',2)[2]
        with db() as c:p=c.execute('SELECT user_id FROM payments WHERE id=?',(pid,)).fetchone()
        if not p or (p['user_id']!=uid and not is_admin(uid)):
            return safe_send(uid,'⛔ Bunday to‘lovga ruxsat yo‘q.')
        result=verify_payment(pid)
        if result=='unavailable':safe_send(uid,'⏳ Provayderdan javob olinmadi. Avtomatik tekshirish davom etadi.')
        if result=='mismatch':safe_send(uid,'⚠️ To‘lov ma’lumotlari mos kelmadi. Admin tekshiradi.')
        return payment_card(uid,pid,admin=is_admin(uid))
    if s.startswith('pay:show:'):return payment_card(uid,s.split(':',2)[2])
    if s.startswith('o:show:'):return order_card(uid,s.split(':',2)[2])
    if s=='u:ledger':return balance_history(uid)
    if s=='u:orders':return my_orders(uid)
    if s=='a:orders':return admin_orders(uid)
    if s.startswith('a:order:'):return order_card(uid,s.split(':',2)[2],admin=True)
    if s.startswith('a:pay:'):return payment_card(uid,s.split(':',2)[2],admin=True)
    if s.startswith('a:input:'):return admin_input(uid,s.split(':',2)[2])
    if s=='a:maintenance':
        if not only_owner(uid):return safe_send(uid,'Faqat bosh admin.')
        put('maintenance','0' if get('maintenance')=='1' else '1');return admin_settings(uid)
    if s=='a:testpay':
        if not only_owner(uid):return safe_send(uid,'🔒 Faqat bosh admin.')
        if not payment_configured():return safe_send(uid,'❌ Shop ID va Shop Key ni kiriting.')
        with db() as c:
            p=c.execute("SELECT id FROM payments WHERE provider_id IS NOT NULL ORDER BY created DESC LIMIT 1").fetchone()
        if not p:
            return safe_send(uid,'⚠️ API ma’lumotlari saqlangan. Sinov uchun botda to‘lov yarating. '
                             'TolovAPI status API va haqiqiy to‘lov sinovisiz ishga tayyor deb bo‘lmaydi.')
        result=verify_payment(p['id'])
        return safe_send(uid,f'🔎 TolovAPI status tekshiruvi: <b>{esc(result)}</b>\n🧾 {esc(p["id"])}')
    if s=='a:testfrag':
        try:
            d=fragment('/wallet/balance',{})
            safe_send(uid,'✅ Fragment API javobi:\n'+esc(json.dumps(d,ensure_ascii=False)[:1700]))
        except Exception as e:safe_send(uid,'❌ '+esc(str(e)))
        return
    if s=='a:wallet':
        try:
            d=fragment('/wallet/balance',{})
            safe_send(uid,'💼 <b>Fragment hamyon</b>\n'+esc(json.dumps(d.get('result',d),ensure_ascii=False)[:1900]))
        except Exception as e:safe_send(uid,'❌ '+esc(str(e)))
        return
    if s=='a:paytoggle':
        if not only_owner(uid):return
        if not LIVE_SALES_ENABLED:return safe_send(uid,'🔒 Sinov rejimi. Doimiy DATABASE_URL va APP_SECRET o‘rnating; keyin savdo sinovidan so‘ng ENABLE_LIVE_SALES=1 qiling.')
        if get('payment_enabled')=='1':
            put('payment_enabled','0')
            return safe_send(uid,'🛑 Yangi to‘lovlar to‘xtatildi. Oldingi pending to‘lov callbacklari qabul qilinadi.')
        if not payment_configured():
            return safe_send(uid,'❌ TolovAPI Shop ID va Shop Key kerak.')
        return safe_send(uid,'⚠️ Shop ID va Shop Key kiritilgan, ammo bu ishlashini isbotlamaydi.\n'
                     'TolovAPI do‘konida complete_url ni kiriting va imzo kaliti sozlang.\n'
                     'Balans faqat autentifikatsiyalangan status API summasi, ID va order_id mos paid javobida to‘ladi.',
                     keyboard([[('⚠️ MEN MAS’ULMAN — YOQISH','a:payenable')]]))
    if s=='a:payenable':
        if not only_owner(uid):return
        if not LIVE_SALES_ENABLED:return safe_send(uid,'🔒 Sinov rejimi. Haqiqiy savdo uchun DATABASE_URL, APP_SECRET va ENABLE_LIVE_SALES=1 kerak.')
        if not payment_configured():return safe_send(uid,'❌ To‘lov sozlamalari to‘liq emas.')
        put('payment_enabled','1')
        return safe_send(uid,'✅ To‘lov qabul qilishga ruxsat berildi. Haqiqiy callback sinovini o‘tkazing.')
    if s.startswith('a:balance:'):
        target=int(s.rsplit(':',1)[1]);set_state(uid,'admin_balance',{'user':target})
        return safe_send(uid,f'User {target} balansi uchun +50000 yoki -10000 yuboring.')
    if s.startswith('a:ban:'):
        target=int(s.rsplit(':',1)[1])
        if target in admin_ids():return safe_send(uid,'Adminni bloklab bo‘lmaydi.')
        with db(True) as c:c.execute('UPDATE users SET banned=1-banned WHERE id=?',(target,))
        return user_admin_card(uid,target)
    if s.startswith('a:done:') or s.startswith('a:refund:'):
        oid=s.split(':',2)[2]
        with db() as c:o=c.execute('SELECT status FROM orders WHERE id=?',(oid,)).fetchone()
        if not o or o['status']!='review':return safe_send(uid,'Buyurtma tekshiruvda emas.')
        action='done' if s.startswith('a:done:') else 'refund'
        choice = 'YETKAZILGAN' if action=='done' else 'PULNI QAYTARISH'
        return safe_send(uid,f'⚠️ {oid}: {choice}\n'
                 'Avval Fragment dashboarddagi real natijani tekshiring! Tasdiqlaysizmi?',
                 keyboard([[('⚠️ HA, TASDIQLAYMAN',f'a:resolve:{action}:{oid}')],[('❌ Orqaga',f'a:order:{oid}')]]))
    if s.startswith('a:resolve:'):
        _,_,action,oid=s.split(':',3)
        if action not in ('refund','done'):return
        o=finish_order(oid,'refunded' if action=='refund' else 'completed','Admin tekshirgan: '+str(uid))
        if o:
            safe_send(o['user_id'],f'🧾 Buyurtma {oid}: '+('pul balansga qaytarildi' if action=='refund' else 'yetkazish tasdiqlandi'))
            safe_send(uid,'✅ Holat o‘zgartirildi.')
        return
    if s.startswith('t:'):
        _,action,n=s.split(':',2)
        tid=int(n)
        with db() as c:t=c.execute('SELECT * FROM tickets WHERE id=?',(tid,)).fetchone()
        if not t:return safe_send(uid,'Murojaat topilmadi.')
        if action=='show':return ticket_show(uid,tid)
        if action=='reply':
            set_state(uid,'admin_reply',{'ticket':tid})
            return safe_send(uid,f'✍️ Murojaat #{tid} uchun javobni yuboring (matn, rasm, video).')
        if action=='close':
            with db(True) as c:c.execute('UPDATE tickets SET status=?,updated=? WHERE id=?',('closed',now(),tid))
            return safe_send(uid,'✅ Murojaat yopildi.')


@bot.message_handler(content_types=['text','photo','video','document','voice','audio','sticker'])
def messages(m):
    if m.chat.type!='private':return
    uid=m.from_user.id
    user_record(m.from_user)
    text=(m.text or '').strip()
    if text in ('🏠 User menyu','🏠 Bosh menyu','⬅️ Orqaga'):
        return main_menu(uid)
    if text=='/cancel':return main_menu(uid)
    if text=='👑 Admin panel' and is_admin(uid):return admin_menu(uid)
    if text in ('⭐ Stars olish','💎 Premium olish','💳 Hisob to‘ldirish','💰 Hisobim','📦 Buyurtmalarim','🎧 Yordam'):
        if not check_access(uid):return
        clear_state(uid)
        if text=='⭐ Stars olish':return stars_menu(uid)
        if text=='💎 Premium olish':return premium_menu(uid)
        if text=='💳 Hisob to‘ldirish':return topup_menu(uid)
        if text=='💰 Hisobim':return profile(uid)
        if text=='📦 Buyurtmalarim':return my_orders(uid)
        if text=='🎧 Yordam':
            set_state(uid,'user_support')
            return safe_send(uid,'🎧 <b>Adminga murojaat</b>\nSavolingizni matn, rasm yoki video ko‘rinishida yuboring.\nBekor: /cancel')
    admin_texts={
        '⭐ Stars narxlari':admin_star,'💎 Premium narxlari':admin_premium,
        '📦 Buyurtmalar':admin_orders,'👥 Foydalanuvchilar':admin_users,
        '📊 Statistika':admin_stats,'💳 To‘lovlar':admin_payments,
        '🔌 API va to‘lov':admin_integrations,'📢 Xabar yuborish':broadcast_admin,
        '📣 Majburiy obuna':admin_channels,'👑 Adminlar':admin_admins,
        '🎧 Murojaatlar':admin_tickets,'⚙️ Sozlamalar':admin_settings,
    }
    if text in admin_texts:
        if not is_admin(uid):return safe_send(uid,'⛔ Ruxsat yo‘q.')
        clear_state(uid)
        return admin_texts[text](uid)
    if not check_access(uid):return
    step,data=state(uid)
    if step=='admin_input':return handle_admin_input(m,data)
    if step=='admin_balance':
        if is_admin(uid):return handle_admin_adjust(m,data)
        return
    if step=='admin_broadcast':return send_broadcast(m)
    if step=='admin_reply':
        if not is_admin(uid):return
        tid=data.get('ticket')
        with db() as c:t=c.execute('SELECT * FROM tickets WHERE id=?',(tid,)).fetchone()
        if not t:return safe_send(uid,'Murojaat topilmadi.')
        with db(True) as c:
            c.execute('INSERT INTO ticket_messages(ticket_id,sender_id,body,created) VALUES(?,?,?,?)',
                      (tid,uid,str(m.text or m.caption or '['+m.content_type+']')[:2000],now()))
            c.execute('UPDATE tickets SET status=?,updated=? WHERE id=?',('answered',now(),tid))
        safe_send(t['user_id'],f'🎧 <b>Admin javobi, murojaat #{tid}</b>')
        try:bot.copy_message(t['user_id'],uid,m.message_id)
        except Exception:safe_send(uid,'⚠️ Javobni foydalanuvchiga yuborib bo‘lmadi.')
        clear_state(uid);return safe_send(uid,'✅ Javob yuborildi.')
    if step=='user_support':return report_ticket(uid,m)
    if m.content_type!='text':return safe_send(uid,'Iltimos, matn yuboring yoki /cancel bosing.')
    if step=='stars_custom':
        try:qty=int(text.replace(' ','').replace(',',''))
        except ValueError:return safe_send(uid,'❌ Butun son kiriting.')
        if not param('min_stars')<=qty<=param('max_stars'):
            return safe_send(uid,f'❌ {param("min_stars")} — {param("max_stars")} oralig‘ida kiriting.')
        return choose_target(uid,'stars',qty)
    if step=='enter_recipient':
        name=extract_username(text)
        if not name:return safe_send(uid,'❌ Faqat @username yoki https://t.me/username yuboring. Raqamli ID yoki taklif havolasi o‘tmaydi.')
        return preview_recipient(uid,name)
    if step=='topup_other':
        try:amount=int(text.replace(' ','').replace(',',''))
        except ValueError:return safe_send(uid,'❌ To‘g‘ri summa kiriting.')
        clear_state(uid);return create_payment(uid,amount)
    return main_menu(uid)


def initialize_runtime():
    """Gunicorn import paytida PostgreSQL/SQLite va shifrlashni tayyorlash."""
    global CIPHER
    setup_db()
    CIPHER=init_cipher()
    # O'rtada server to'xtagan buyurtmalarni qayta yubormaymiz.
    # 5 daqiqadan eski processing holati qo'lda tekshirishga yuboriladi.
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S UTC')
    with db(True) as c:
        c.execute("UPDATE orders SET status='review', detail='Server restart/timeout: Fragment dashboarddan tekshiring',updated=? WHERE status='processing' AND updated<?",(now(),cutoff))


def setup_telegram_webhook(public_url):
    """Faqat bir marta Bash konsolida: python bot.py --set-webhook https://..."""
    public_url=public_url.strip().rstrip('/')
    if not re.fullmatch(r'https://[A-Za-z0-9.-]+',public_url):
        raise ValueError('Faqat https://SIZNIKI.onrender.com kabi HTTPS manzil kiriting.')
    put('public_url',public_url)
    webhook_url=public_url+'/telegram/'+get('telegram_webhook_path')
    result=bot.set_webhook(url=webhook_url, secret_token=get('telegram_header_secret'),
                           allowed_updates=['message','callback_query'], max_connections=1,
                           drop_pending_updates=False)
    print('Webhook o‘rnatildi:',result)
    if not result:
        raise RuntimeError('Telegram webhook o‘rnatilmadi')
    info=bot.get_webhook_info()
    print('Telegram URL mos:',info.url==webhook_url)
    print('Oxirgi Telegram xatosi:',info.last_error_message or 'yo‘q')
    print('TolovAPI complete_url admin panelda ko‘rsatiladi.')
    print('Maxfiy webhook URL va secretni boshqalarga yubormang.')


initialize_runtime()  # Gunicorn: from bot import app; DB+shifrlash tayyor.
application = app


def start_render():
    """Render Start Command: python bot.py --render-start

    Telegram webhookini har bir yangi deployda to'g'ri hostga ulaydi,
    so'ng yagona Gunicorn worker bilan HTTP serverni ishga tushiradi.
    """
    if not PUBLIC_URL:
        raise SystemExit('PUBLIC_URL yoki RENDER_EXTERNAL_HOSTNAME topilmadi. Render Environment ni tekshiring.')
    logger.info('Render start: %s; live_sales=%s', PUBLIC_URL, LIVE_SALES_ENABLED)
    for i in range(3):
        try:
            setup_telegram_webhook(PUBLIC_URL)
            break
        except Exception as exc:
            logger.warning('Webhook urinish %s/3: %s', i + 1, type(exc).__name__)
            if i == 2:
                raise
            time.sleep(2 ** i)
    port = os.getenv('PORT', '10000')
    if not port.isdecimal() or not (1 <= int(port) <= 65535):
        raise SystemExit('PORT noto‘g‘ri')
    os.execvp('gunicorn', [
        'gunicorn', '--bind', '0.0.0.0:' + port,
        '--workers', '1', '--threads', '4', '--timeout', '120',
        '--access-logfile', '-', '--error-logfile', '-', 'bot:app'
    ])


if __name__=='__main__':
    if len(sys.argv)>=2 and sys.argv[1]=='--render-start':
        start_render()
    elif len(sys.argv)>=3 and sys.argv[1]=='--set-webhook':
        setup_telegram_webhook(sys.argv[2])
    elif len(sys.argv)>=2 and sys.argv[1]=='--webhook-info':
        info=bot.get_webhook_info()
        print('Webhook mavjud:',bool(info.url))
        print('Oxirgi xato:',info.last_error_message or 'yo‘q')
        print('Navbatdagi update soni:',info.pending_update_count)
    elif len(sys.argv)>=2 and sys.argv[1]=='--check-api':
        # Maxfiy kalitlar hech qachon ko‘rsatilmaydi.
        for title,url in (('Telegram','https://api.telegram.org'),
                          ('Fragment','https://fragment-api.uz/api/v1/getInfo'),
                          ('TolovAPI',TOLOV_BASE)):
            try:
                r=requests.get(url,timeout=(4,8))
                print(title, 'HTTP',r.status_code, '- HTTP 403 bo‘lsa proxy/kalit cheklovini tekshiring' if r.status_code==403 else '- HTTP javobi olindi')
            except requests.RequestException as e:
                print(title,'ULANMADI:',type(e).__name__,str(e)[:180])
    else:
        print('Render Start Command: python bot.py --render-start')
        print('Qo‘lda webhook: python bot.py --set-webhook https://SIZNIKI.onrender.com')
        print('Render env: BOT_TOKEN, OWNER_ID; haqiqiy savdo uchun DATABASE_URL, APP_SECRET va ENABLE_LIVE_SALES=1')
