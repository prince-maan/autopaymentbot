import gc
import hmac
import secrets
import io
import json
import os
import urllib.error
import urllib.request
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

# .env फाइल से टोकन लोड करने के लिए
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, Response, session
from functools import wraps
from PIL import Image, ImageDraw, ImageFont
import pymongo
import qrcode
import telebot
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

# ==========================================
# 🛑 BOOT SETTINGS (पहली बार सिर्फ़ Dashboard से भरें)
# ==========================================
# MongoDB link और Dashboard login एक छोटी फाइल में रहते हैं (आपके home folder में)।
# इन्हें Dashboard के "Add Bot / Setup" में भरा जाता है। पुराने environment variables भी चलते हैं।
BOOT_FILE = os.path.join(os.path.expanduser("~"), ".telegram_store_bot_boot.json")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BOOT_DEFAULT_DB = "telegram_store_bot"

def load_boot():
    data = {}
    try:
        with open(BOOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}

    def pick(key, default=""):
        # पहले boot file, फिर पुराना environment variable
        return str(data.get(key) or os.environ.get(key) or default).strip()

    return {
        "MONGO_URI": pick("MONGO_URI"),
        "MONGO_DB_NAME": pick("MONGO_DB_NAME", BOOT_DEFAULT_DB) or BOOT_DEFAULT_DB,
        "DASHBOARD_USERNAME": pick("DASHBOARD_USERNAME", "admin") or "admin",
        "DASHBOARD_PASSWORD": pick("DASHBOARD_PASSWORD"),
    }

def save_boot(values):
    data = {}
    try:
        with open(BOOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    data.update(values)
    with open(BOOT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(BOOT_FILE, 0o600)
    except Exception:
        pass

BOOT = load_boot()
MONGO_URI = BOOT["MONGO_URI"]
MONGO_DB_NAME = BOOT["MONGO_DB_NAME"]
DASHBOARD_USERNAME = BOOT["DASHBOARD_USERNAME"]
DASHBOARD_PASSWORD = BOOT["DASHBOARD_PASSWORD"]
if DASHBOARD_PASSWORD == "changeme123":
    DASHBOARD_PASSWORD = ""

# पहली setup के लिए 6-digit code (terminal में दिखता है), ताकि कोई अनजान व्यक्ति settings न बदल सके
SETUP_CODE = "".join(secrets.choice("0123456789") for _ in range(6))

# Dashboard से भरी जाने वाली bot settings (key -> default)
BOT_CONFIG_KEYS = {
    "BOT_TOKEN": "",
    "ADMIN_ID": "",
    "DB_CHANNEL_ID": "",
    "UPI_ID": "",
    "MERCHANT_NAME": "Store",
    "SMS_HOOK_SECRET": "",
    "CHAT_LINK": "",
    "INTERNATIONAL_LINK": "",
    "QR_EXPIRY_MINUTES": "10",
    "STALE_ORDER_HOURS": "12",
    "SMS_POOL_TTL_HOURS": "5",
    "ORDER_TTL_HOURS": "48",
    "APP_GITHUB_REPO": "",
    "APP_GITHUB_TOKEN": "",
}

_boot_client = None
_boot_db = None
if MONGO_URI:
    try:
        _boot_client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
        _boot_db = _boot_client.get_database(MONGO_DB_NAME)
    except Exception as e:
        print(f"⚠️ MongoDB URL समझ नहीं आया: {e}")

def load_bot_config(db=None):
    """MongoDB की "bot_config" से settings पढ़ता है। Environment variables भी fallback हैं।"""
    cfg = {}
    for k, default in BOT_CONFIG_KEYS.items():
        cfg[k] = os.environ.get(k, default)
    try:
        target = db if db is not None else _boot_db
        if target is not None:
            doc = target["settings"].find_one({"_id": "bot_config"}) or {}
            for k in BOT_CONFIG_KEYS:
                if doc.get(k) is not None:
                    cfg[k] = str(doc[k])
    except Exception as e:
        print(f"⚠️ Config load error: {e}")
    return cfg

def _to_int(v):
    try: return int(str(v).strip())
    except (TypeError, ValueError): return None

def _to_num(v, default):
    try: return float(str(v).strip())
    except (TypeError, ValueError): return default

CFG = load_bot_config()
BOT_TOKEN = CFG["BOT_TOKEN"].strip()
ADMIN_ID = _to_int(CFG["ADMIN_ID"])
DB_CHANNEL_ID = _to_int(CFG["DB_CHANNEL_ID"])
UPI_ID = CFG["UPI_ID"].strip()
MERCHANT_NAME = CFG["MERCHANT_NAME"].strip() or "Store"
SMS_HOOK_SECRET = CFG["SMS_HOOK_SECRET"].strip()
CHAT_LINK = CFG["CHAT_LINK"].strip()
INTERNATIONAL_LINK = CFG["INTERNATIONAL_LINK"].strip()
QR_EXPIRY_SECONDS = int(_to_num(CFG["QR_EXPIRY_MINUTES"], 10) * 60)
STALE_ORDER_SECONDS = int(_to_num(CFG["STALE_ORDER_HOURS"], 12) * 3600)
SMS_POOL_TTL_HOURS = int(_to_num(CFG["SMS_POOL_TTL_HOURS"], 5))
ORDER_TTL_HOURS = int(_to_num(CFG["ORDER_TTL_HOURS"], 48))

# Bot तभी चलेगा जब ज़रूरी settings भरी हों। वरना सिर्फ़ Dashboard खुलेगा (Setup mode)।
BOT_CONFIGURED = bool(MONGO_URI and DASHBOARD_PASSWORD and BOT_TOKEN and ADMIN_ID and DB_CHANNEL_ID and UPI_ID and SMS_HOOK_SECRET)

bot = telebot.TeleBot(BOT_TOKEN or "1:SETUP", num_threads=20)

# 🎨 BUTTON COLOUR (Telegram: green = success, red = danger, blue = primary; खाली = default grey)
BTN_STYLES = {"": None, "default": None, "green": "success", "success": "success",
              "red": "danger", "danger": "danger", "blue": "primary", "primary": "primary"}

class StyledButton(telebot.types.InlineKeyboardButton):
    def __init__(self, text, style=None, **kwargs):
        super().__init__(text, **kwargs)
        self.style = None  # रंग फ़िलहाल बंद है; बटन हमेशा default रंग के होंगे
    def to_dict(self):
        d = super().to_dict()
        if self.style: d["style"] = self.style
        return d

def kbtn(text, url=None, callback_data=None, style=None):
    return StyledButton(text, style=style, url=url, callback_data=callback_data)

def parse_btn_line(txt):
    """'Text - URL' या 'Text - URL - red/green/blue' → (text, url, style)"""
    parts = [p.strip() for p in str(txt).split(" - ")]
    style = ""
    if len(parts) >= 3 and parts[-1].lower() in BTN_STYLES:
        style = parts[-1].lower(); parts = parts[:-1]
    if len(parts) < 2 or not parts[0] or not parts[-1]:
        raise ValueError("format")
    return " - ".join(parts[:-1]), parts[-1], style
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_time():
    return datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p")

# ==========================================
# 👑 MULTI-ADMIN HELPERS
# ==========================================
# ENV वाला ADMIN_ID = Owner (हमेशा admin, डिलीट नहीं हो सकता)
# बाकी admins MongoDB की "admins" collection में रहते हैं, जो Dashboard से add/remove होते हैं।
_admin_cache = {"ids": set(), "ts": 0.0}
_admin_lock = threading.Lock()

def get_admin_ids():
    now = time.time()
    with _admin_lock:
        if now - _admin_cache["ts"] < 10:
            return set(_admin_cache["ids"])
    ids = set()
    try:
        for d in admins_col.find({}, {"user_id": 1}):
            try: ids.add(int(d["user_id"]))
            except (TypeError, ValueError): pass
    except Exception:
        pass
    with _admin_lock:
        _admin_cache["ids"], _admin_cache["ts"] = ids, now
    return set(ids)

def refresh_admin_cache():
    with _admin_lock:
        _admin_cache["ts"] = 0.0

def is_admin(uid):
    if uid is None: return False
    if uid == ADMIN_ID: return True
    return uid in get_admin_ids()

# ==========================================
# 🍃 MONGODB SETUP
# ==========================================
DB_READY = False
try:
    if not MONGO_URI:
        raise RuntimeError("MongoDB URL अभी सेट नहीं है")
    mongo_client = _boot_client
    mongo_client.admin.command("ping")
    db = mongo_client.get_database(MONGO_DB_NAME)
    users_col = db["users"]
    courses_col = db["courses"]
    batches_col = db["batches"]
    purchases_col = db["purchases"]
    file_links_col = db["file_links"]
    settings_col = db["settings"]
    orders_col = db["orders"]
    sms_pool_col = db["sms_pool"]
    offers_col = db["offers"]
    channel_logs_col = db["channel_logs"]
    admins_col = db["admins"]  # Dashboard से जोड़े गए extra admins
    broadcasts_col = db["broadcasts"]  # Dashboard से भेजे गए broadcasts का रिकॉर्ड (हटाने के लिए)
    banned_col = db["banned_users"]  # Ban list (Dashboard + bot panel)
    member_events_col = db["member_events"]  # join / left events (chat_member updates)
    join_prompts_col = db["join_prompts"]  # "Send request" message का रिकॉर्ड, request आने पर उसे हटाने के लिए

    try:
        sms_pool_col.create_index("created_at_dt", expireAfterSeconds=SMS_POOL_TTL_HOURS * 3600)
        orders_col.create_index("created_at_dt", expireAfterSeconds=ORDER_TTL_HOURS * 3600)
        orders_col.create_index([("user_id", 1), ("offer_id", 1), ("status", 1)])
        orders_col.create_index([("course_id", 1), ("status", 1)])
        orders_col.create_index([("created_at", -1)])
        orders_col.create_index([("status", 1), ("delivered_at_ts", -1)])
        purchases_col.create_index("user_id")
        users_col.create_index("user_id")
        broadcasts_col.create_index("bid")
    except Exception:
        pass
    DB_READY = True
    print(f"✅ MongoDB Connected! (Database: {MONGO_DB_NAME})")
except Exception as e:
    print(f"❌ MongoDB Error: {e}")
    print("⚠️ Dashboard खुलेगा ताकि आप 'Add Bot / Setup' में MongoDB URL ठीक कर सकें।")

# ==========================================
BOT_CONFIGURED = BOT_CONFIGURED and DB_READY
if not BOT_CONFIGURED:
    print("⚠️ Setup mode: Dashboard खोलें और 'Add Bot / Setup' में settings भरें।")

# 🚀 SMART CACHE SYSTEM
# ==========================================
db_cache = {}
cache_lock = threading.Lock()

def get_cached_setting(key, ttl=15):
    now = time.time()
    with cache_lock:
        if key in db_cache and now - db_cache[key]['time'] < ttl:
            return db_cache[key]['data']
    val = settings_col.find_one({"_id": key})
    with cache_lock: db_cache[key] = {'data': val, 'time': now}
    return val

def get_cached_course(course_id, ttl=30):
    cache_key = f"course_{course_id}"
    now = time.time()
    with cache_lock:
        if cache_key in db_cache and now - db_cache[cache_key]['time'] < ttl:
            return db_cache[cache_key]['data']
    val = courses_col.find_one({"course_id": course_id})
    with cache_lock: db_cache[cache_key] = {'data': val, 'time': now}
    return val

def get_cached_user(user_id, ttl=5):
    cache_key = f"user_{user_id}"
    now = time.time()
    with cache_lock:
        if cache_key in db_cache and now - db_cache[cache_key]['time'] < ttl:
            return db_cache[cache_key]['data']
    val = users_col.find_one({"user_id": user_id})
    with cache_lock: db_cache[cache_key] = {'data': val, 'time': now}
    return val

def get_cached_offer(offer_code, ttl=15):
    cache_key = f"offer_{offer_code}"
    now = time.time()
    with cache_lock:
        if cache_key in db_cache and now - db_cache[cache_key]['time'] < ttl:
            return db_cache[cache_key]['data']
    val = offers_col.find_one({"offer_code": offer_code})
    with cache_lock: db_cache[cache_key] = {'data': val, 'time': now}
    return val

def invalidate_cache(key):
    with cache_lock: db_cache.pop(key, None)

# ==========================================
# ⚙️ MAINTENANCE & AUTO-CLEAR SETTINGS
# ==========================================
def is_protected():
    """Content protection (forward/save बंद) — Dashboard की Settings से ON/OFF होता है, restart नहीं चाहिए"""
    cfg = get_cached_setting("system_settings") or {}
    return bool(cfg.get("protect_content", False))

def is_maintenance_mode():
    cfg = get_cached_setting("system_settings")
    return cfg.get("maintenance", False) if cfg else False

def get_cleanup_settings():
    cfg = get_cached_setting("system_settings") or {}
    return {
        "seconds": int(cfg.get("chat_cleanup_seconds", 86400)),
        "del_promos": cfg.get("auto_del_promos", True),
        "del_broadcasts": cfg.get("auto_del_broadcasts", True),
        "del_purchases": cfg.get("auto_del_purchases", False)
    }

def schedule_course_auto_delete(chat_id, message_ids):
    """Schedules deletion of course/file contents if timer is set in Dashboard"""
    if not message_ids: return
    cfg = get_cached_setting("system_settings") or {}
    mins = int(cfg.get("course_timer_minutes", 0))
    if mins > 0:
        def del_task():
            for mid in message_ids:
                try: bot.delete_message(chat_id, mid)
                except: pass
                with tracker_lock:
                    if chat_id in user_chat_messages:
                        user_chat_messages[chat_id] = [x for x in user_chat_messages[chat_id] if x["id"] != mid]
        threading.Timer(mins * 60, del_task).start()

# ==========================================
# 📝 TEXT FORMATTING & ACTIVITY TRACKER
# ==========================================
def get_formatted_text(message):
    if hasattr(message, "html_text") and message.html_text: return message.html_text
    if hasattr(message, "html_caption") and message.html_caption: return message.html_caption
    return message.caption or message.text or ""

user_chat_messages, user_inactivity_timers, tracker_lock = {}, {}, threading.Lock()

def clear_inactive_chat(chat_id, is_force=False, force_del_promos=True, force_del_broadcasts=True, force_del_purchases=False):
    cfg = get_cleanup_settings()
    del_promos = force_del_promos if is_force else cfg["del_promos"]
    del_broadcasts = force_del_broadcasts if is_force else cfg["del_broadcasts"]
    del_purchases = force_del_purchases if is_force else cfg["del_purchases"]

    with tracker_lock:
        if chat_id not in user_chat_messages: return
        msgs = user_chat_messages[chat_id]
        to_delete, to_keep = [], []

        for m in msgs:
            m_type = m.get("type", "general")
            delete_it = False
            if m_type in ["course", "menu", "general"] and del_promos: delete_it = True
            elif m_type == "broadcast" and del_broadcasts: delete_it = True
            elif m_type == "purchase" and del_purchases: delete_it = True

            if delete_it: to_delete.append(m["id"])
            else: to_keep.append(m)

        user_chat_messages[chat_id] = to_keep
        if not is_force:
            timer = user_inactivity_timers.pop(chat_id, None)
            if timer: timer.cancel()

    for mid in to_delete:
        try: 
            bot.delete_message(chat_id, mid)
            time.sleep(0.05)
        except Exception: pass

def register_activity(chat_id, message_id=None, msg_type="general"):
    if not isinstance(chat_id, int) or chat_id <= 0 or is_admin(chat_id): return
    with tracker_lock:
        if chat_id not in user_chat_messages: user_chat_messages[chat_id] = []
        if message_id:
            if not any(m['id'] == message_id for m in user_chat_messages[chat_id]):
                user_chat_messages[chat_id].append({"id": message_id, "type": msg_type})
        
        cfg = get_cleanup_settings()
        cleanup_time = cfg["seconds"]

        if chat_id in user_inactivity_timers: user_inactivity_timers[chat_id].cancel()
        if cleanup_time > 0: 
            new_timer = threading.Timer(cleanup_time, clear_inactive_chat, args=(chat_id, False))
            user_inactivity_timers[chat_id] = new_timer
            new_timer.start()

orig_send_message = bot.send_message
orig_send_photo = bot.send_photo
orig_send_video = bot.send_video
orig_send_document = bot.send_document
orig_send_media_group = bot.send_media_group

def tracked_send_message(chat_id, *args, **kwargs):
    msg_type = kwargs.pop("msg_type", "general")
    if 'disable_web_page_preview' not in kwargs: kwargs['disable_web_page_preview'] = True
    msg = orig_send_message(chat_id, *args, **kwargs)
    register_activity(chat_id, msg.message_id, msg_type)
    return msg
bot.send_message = tracked_send_message

def tracked_send_photo(chat_id, *args, **kwargs):
    msg_type = kwargs.pop("msg_type", "general")
    msg = orig_send_photo(chat_id, *args, **kwargs)
    register_activity(chat_id, msg.message_id, msg_type)
    return msg
bot.send_photo = tracked_send_photo

def tracked_send_video(chat_id, *args, **kwargs):
    msg_type = kwargs.pop("msg_type", "general")
    msg = orig_send_video(chat_id, *args, **kwargs)
    register_activity(chat_id, msg.message_id, msg_type)
    return msg
bot.send_video = tracked_send_video

def tracked_send_document(chat_id, *args, **kwargs):
    msg_type = kwargs.pop("msg_type", "general")
    msg = orig_send_document(chat_id, *args, **kwargs)
    register_activity(chat_id, msg.message_id, msg_type)
    return msg
bot.send_document = tracked_send_document
bot.send_media_group = orig_send_media_group

# ==========================================
# 📸 QR CODE & CUSTOM FRAME GENERATOR
# ==========================================
FRAME_QR_BOX = (485, 552, 1562, 1631)      
QR_PADDING = 26                             
FRAME_RUPEE_RIGHT = 1160                    
FRAME_AMOUNT_BOX = (1061, 1742, 1445, 1872) 
AMOUNT_MAX_FONT = 100
AMOUNT_MIN_FONT = 40

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRAME_PATH = os.path.join(BASE_DIR, "frame.png")
FONT_PATHS = [
    os.path.join(BASE_DIR, "Roboto-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]

def _load_amount_font(size):
    from PIL import ImageFont
    for path in FONT_PATHS:
        if os.path.exists(path):
            try: return ImageFont.truetype(path, size)
            except Exception: pass
    try:
        import urllib.request
        target = os.path.join(BASE_DIR, "Roboto-Bold.ttf")
        urllib.request.urlretrieve("https://github.com/google/fonts/raw/main/ofl/roboto/Roboto-Bold.ttf", target)
        return ImageFont.truetype(target, size)
    except Exception: pass
    return ImageFont.load_default(size=size)

def generate_upi_qr(amount, order_id):
    from PIL import ImageFont, ImageDraw, Image

    clean_amt = re.sub(r"[^\d.]", "", str(amount))
    upi_url = f"upi://pay?pa={UPI_ID}&pn={MERCHANT_NAME}&am={clean_amt}&cu=INR&tn=Order_{order_id}"

    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=1)
    qr.add_data(upi_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")

    try:
        background = Image.open(FRAME_PATH).convert("RGBA")
        box_l, box_t, box_r, box_b = FRAME_QR_BOX
        qr_size = min(box_r - box_l + 1, box_b - box_t + 1) - 2 * QR_PADDING
        qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)

        mask = Image.new("L", (qr_size, qr_size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, qr_size, qr_size), int(qr_size * 0.06), fill=255)
        qr_img.putalpha(mask)

        qr_x = (box_l + box_r + 1) // 2 - qr_size // 2
        qr_y = (box_t + box_b + 1) // 2 - qr_size // 2
        background.paste(qr_img, (qr_x, qr_y), qr_img)

        draw = ImageDraw.Draw(background)
        amt_text = clean_amt
        f_l, f_t, f_r, f_b = FRAME_AMOUNT_BOX
        text_left = FRAME_RUPEE_RIGHT + 22
        text_right_limit = f_r - 20
        avail_w = text_right_limit - text_left
        field_cy = (f_t + f_b) // 2

        font = None
        for size in range(AMOUNT_MAX_FONT, AMOUNT_MIN_FONT - 1, -2):
            candidate = _load_amount_font(size)
            l, t, r, b = candidate.getbbox(amt_text)
            if (r - l) <= avail_w:
                font = candidate
                break
        if font is None:
            font = _load_amount_font(AMOUNT_MIN_FONT)

        l, t, r, b = font.getbbox(amt_text)
        ct_l, ct_t, ct_r, ct_b = font.getbbox("0")
        text_x = text_left - l
        text_y = field_cy - (ct_t + ct_b) // 2
        draw.text((text_x, text_y), amt_text, font=font, fill="#1D3B70")

        final_img = background
    except Exception as ec:
        print(f"⚠️ Frame Error: {ec}")
        final_img = qr_img

    bio = io.BytesIO()
    final_img.save(bio, "PNG")
    bio.seek(0)
    return bio, clean_amt

# ==========================================
# 🛡️ RATE LIMITER & STATE MANAGEMENT
# ==========================================
admin_data, user_states, user_qr_messages, pending_orders, all_orders_cache = {}, {}, {}, {}, {}
user_cooldowns = {}
pending_lock = threading.Lock()
paise_counter = 0
paise_lock = threading.Lock()

def check_rate_limit(user_id, cooldown=1.2):
    now = time.time()
    if user_id in user_cooldowns and now - user_cooldowns[user_id] < cooldown: return False
    user_cooldowns[user_id] = now
    return True

def set_user_state(user_id, step, order_id=None, amount_key=None):
    user_states[user_id] = {"step": step, "order_id": order_id, "amount_key": amount_key}
    def _bg():
        try: users_col.update_one({"user_id": user_id}, {"$set": {"bot_state": step, "bot_state_order": order_id, "bot_state_amt": amount_key}}, upsert=True)
        except: pass
    threading.Thread(target=_bg, daemon=True).start()

def get_user_state(user_id):
    if user_id in user_states: return user_states[user_id]
    u = get_cached_user(user_id)
    if u and u.get("bot_state"):
        st = {"step": u.get("bot_state"), "order_id": u.get("bot_state_order"), "amount_key": u.get("bot_state_amt")}
        user_states[user_id] = st
        return st
    return {}

def clear_user_state(user_id):
    user_states.pop(user_id, None)
    def _bg():
        try: users_col.update_one({"user_id": user_id}, {"$unset": {"bot_state": "", "bot_state_order": "", "bot_state_amt": ""}})
        except: pass
    threading.Thread(target=_bg, daemon=True).start()

alloc_lock = threading.Lock()
reserved_now = {}  # amount -> समय; order बनने की प्रक्रिया में चल रहे amounts (race से बचाव)
ALLOC_RESERVE_SECONDS = 900

def first_exact_enabled():
    """ON हो तो पहला order ठीक base amount (जैसे 100.00) पर बनता है, बाद वाले 100.01, 100.02 ..."""
    cfg = get_cached_setting("system_settings") or {}
    return bool(cfg.get("first_exact_amount", False))

def get_order_hold_seconds():
    """Expired/pending order का amount कितने समय तक late SMS के लिए बुक रहेगा.
    Dashboard > Settings > 'Order amount hold' से बदला जाता है. Default 12 घंटे."""
    cfg = get_cached_setting("system_settings") or {}
    try: hrs = float(cfg.get("order_hold_hours", 12))
    except (TypeError, ValueError): hrs = 12.0
    return max(1.0, hrs) * 3600

def _taken_amounts(base_clean, include_exact=False):
    """उस base (जैसे 50) के 50.01 से 50.99 तक में कौन से amounts बुक हैं"""
    cands = ([f"{base_clean:.2f}"] if include_exact else []) + [f"{base_clean + p / 100:.2f}" for p in range(1, 100)]
    taken = set()
    now = time.time()
    with pending_lock:
        taken.update(k for k in pending_orders.keys() if k in cands)
    for k, ts in list(reserved_now.items()):
        if now - ts < ALLOC_RESERVE_SECONDS: taken.add(k)
        else: reserved_now.pop(k, None)
    # वही orders जो webhook अभी भी match कर सकता है: PENDING, या hold समय के अंदर EXPIRED
    cutoff = now - get_order_hold_seconds()
    for o in orders_col.find({"amount": {"$in": cands}, "status": {"$in": ["PENDING", "EXPIRED"]}, "created_at": {"$gte": cutoff}}, {"amount": 1}):
        taken.add(o.get("amount"))
    # SMS pool में बिना match के पड़े SMS: उन amounts पर नया order नहीं देंगे, वरना पुराना SMS गलत match हो जाएगा
    for sm in sms_pool_col.find({"amount": {"$in": cands}, "status": "UNUSED"}, {"amount": 1}):
        taken.add(sm.get("amount"))
    return cands, taken

def generate_unique_amount(base_amount):
    """सबसे छोटा खाली paise वाला amount देता है (जैसे 50.10). पेमेंट approve/cancel होते ही वो amount फिर से खाली हो जाता है.
    सारे 99 बुक हों तो None लौटता है."""
    base_clean = int(float(base_amount))
    cands, taken = _taken_amounts(base_clean, include_exact=first_exact_enabled())
    for c in cands:
        if c not in taken:
            reserved_now[c] = time.time()
            return c
    return None

# ==========================================
# 🎟 DYNAMIC PRICE & OFFER VALIDATION
# ==========================================
# ==========================================
# 🚫 BAN LIST (Dashboard + bot panel)
# ==========================================
BAN_CACHE = {"ts": 0.0, "ids": {}, "names": {}}
BAN_TZ = timezone(timedelta(hours=5, minutes=30))

def _norm_uname(u):
    return str(u or "").strip().lstrip("@").lower()

def parse_ban_target(raw):
    """'123456' → ('id', 123456); '@name' या 'name' → ('name', 'name'); गलत हो तो (None, None)"""
    t = str(raw or "").strip()
    if t.isdigit(): return "id", int(t)
    n = _norm_uname(t)
    if re.fullmatch(r"[A-Za-z0-9_]{3,64}", n): return "name", n
    return None, None

def _resolve_user(kind, val):
    if kind == "id":
        u = users_col.find_one({"user_id": val}, {"username": 1}) or {}
        return val, _norm_uname(u.get("username"))
    u = users_col.find_one({"username": {"$regex": "^" + re.escape(val) + "$", "$options": "i"}}, {"user_id": 1}) or {}
    return u.get("user_id"), val

def _ban_match(uid, uname):
    conds = []
    if uid is not None: conds.append({"user_id": uid})
    if uname: conds.append({"username": uname})
    return {"$or": conds} if conds else {"_id": None}

def _ban_index(force=False):
    now = time.time()
    if not force and now - BAN_CACHE["ts"] < 20: return BAN_CACHE
    ids, names = {}, {}
    try:
        for b in banned_col.find({}):
            until = None if b.get("permanent") else b.get("until")
            if until is not None and until <= now: continue
            if b.get("user_id") is not None: ids[int(b["user_id"])] = until
            if b.get("username"): names[_norm_uname(b["username"])] = until
        BAN_CACHE.update({"ts": now, "ids": ids, "names": names})
    except Exception:
        pass  # DB अभी नहीं है: पुराना cache ही चलेगा
    return BAN_CACHE

def is_banned(user_id=None, username=None):
    if user_id is not None and is_admin(user_id): return False
    idx = _ban_index()
    now = time.time()
    checks = ((idx["ids"], user_id), (idx["names"], _norm_uname(username) if username else None))
    for store, key in checks:
        if key is None or key == "" or key not in store: continue
        until = store[key]
        if until is None or until > now: return True
    return False

def set_ban(target_raw, hours=None, reason=""):
    """hours=None मतलब permanent; वरना इतने घंटे तक ban"""
    kind, val = parse_ban_target(target_raw)
    if not kind: raise ValueError("User ID (सिर्फ़ numbers) या @username डालें")
    uid, uname = _resolve_user(kind, val)
    if uid is None and not uname: raise ValueError("यूज़र नहीं मिला")
    banned_col.delete_many(_ban_match(uid, uname))
    now = time.time()
    banned_col.insert_one({
        "user_id": uid, "username": uname, "permanent": hours is None,
        "until": None if hours is None else now + float(hours) * 3600,
        "banned_at": get_ist_time(), "banned_at_ts": now, "reason": (reason or "")[:200],
    })
    BAN_CACHE["ts"] = 0.0
    return {"user_id": uid, "username": uname}

def unban_user(target_raw):
    kind, val = parse_ban_target(target_raw)
    if not kind: raise ValueError("User ID (सिर्फ़ numbers) या @username डालें")
    uid, uname = _resolve_user(kind, val)
    n = banned_col.delete_many(_ban_match(uid, uname)).deleted_count
    BAN_CACHE["ts"] = 0.0
    return n

def get_offer_usage_count(user_id, offer_code):
    return orders_col.count_documents({"user_id": user_id, "offer_id": offer_code, "status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}})

def check_offer_validity(offer, user_id, course_id=None):
    if not offer: return False, "not_found"
    now_ts = time.time()
    if offer.get("expires_at_ts") and now_ts > offer["expires_at_ts"]: return False, "expired"
    if offer.get("max_users", -1) != -1 and offer.get("used_count", 0) >= offer["max_users"]: return False, "max_reached"
    per_user_limit = offer.get("per_user_limit", 1) 
    if per_user_limit != -1 and get_offer_usage_count(user_id, offer["offer_code"]) >= per_user_limit: return False, "already_used"
    if course_id and offer.get("target_type") == "single" and offer.get("target_course_id") != course_id: return False, "wrong_course"
    return True, "ok"

def clear_user_offer(user_id, offer_code):
    def _bg():
        try:
            users_col.update_one({"user_id": user_id}, {"$unset": {"active_offer_code": "", "active_offer": ""}})
            invalidate_cache(f"user_{user_id}")
        except: pass
    threading.Thread(target=_bg, daemon=True).start()

def calculate_final_price(user_id, course_id, base_amount):
    final_price = float(base_amount)
    applied_flash_id = None
    
    flash_sale = get_cached_setting("flash_sale")
    if flash_sale and flash_sale.get("expires_at_ts", 0) > time.time():
        if flash_sale["target"] == "all" or flash_sale.get("course_id") == course_id:
            limit = flash_sale.get("limit", 0)
            allowed = True
            if limit > 0:
                uses = orders_col.count_documents({"user_id": user_id, "flash_id": flash_sale["sale_id"], "status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}})
                if uses >= limit: allowed = False
            
            if allowed:
                pct = flash_sale["percent"]
                if flash_sale["mode"] == "hike": final_price = final_price * (1 + pct / 100.0)
                else: final_price = final_price * (1 - pct / 100.0)
                final_price = round(final_price, 2)
                applied_flash_id = flash_sale["sale_id"]

    u_rec = get_cached_user(user_id) or {}
    active_off_code = u_rec.get("active_offer_code") or (u_rec.get("active_offer") or {}).get("offer_code")
    applied_promo_code, promo_disc_pct = None, None
    
    if active_off_code:
        live_offer = get_cached_offer(active_off_code)
        is_valid, _ = check_offer_validity(live_offer, user_id, course_id)
        if is_valid:
            promo_disc_pct = live_offer["discount_percent"]
            final_price = round(final_price * (1.0 - (promo_disc_pct / 100.0)), 2)
            applied_promo_code = active_off_code
        else:
            clear_user_offer(user_id, active_off_code)
            
    return final_price, applied_flash_id, applied_promo_code, promo_disc_pct, flash_sale

def update_channel_order_status(order, status_type, extra_text=""):
    channel_msg_id = order.get("channel_msg_id")
    if not channel_msg_id: return
    user_mention = order.get("user_mention", f"User ({order['user_id']})")
    discount_info = f"\n🎟 <b>Offer Applied:</b> {order.get('discount_percent')}% OFF (Original: ₹{order.get('original_amount')})" if order.get("discount_percent") else ""

    course = get_cached_course(order.get("course_id")) or {}
    ch_name = f"\n📺 <b>Channel:</b> {course.get('channel_name')}" if course and course.get("channel_name") else ""

    if status_type == "EXPIRED":
        new_text = f"🔴 <b>[UNPAID / QR EXPIRED]</b>\n\n👤 <b>User:</b> {user_mention}\n🔖 <b>Order ID:</b> <code>{order['order_id']}</code>\n📚 <b>Pack:</b> <code>{order['course_id']}</code>{ch_name}\n💰 <b>Amount:</b> ₹{order['amount']}{discount_info}\n⏰ <b>Initiated at:</b> {order.get('created_at_str', '')}\n⏳ <b>Status:</b> ⚠️ 10 मिनट में पेमेंट नहीं आई (स्क्रीनशॉट पेंडिंग)"
    elif status_type == "AUTO_VERIFIED":
        new_text = f"🟢 <b>[PAYMENT COMPLETED & AUTO-DELIVERED]</b>\n\n👤 <b>User:</b> {user_mention}\n🔖 <b>Order ID:</b> <code>{order['order_id']}</code>\n📚 <b>Pack:</b> <code>{order['course_id']}</code>{ch_name}\n💰 <b>Amount Paid:</b> ₹{order['amount']}{discount_info}\n⏰ <b>Delivered at:</b> {get_ist_time()}\n⚡ <b>Status:</b> ✅ ऑटो-वेरिफाइड (SMS द्वारा)\n\n📩 <code>{extra_text[:180]}</code>"
    elif status_type == "MANUAL_APPROVED":
        new_text = f"✅ <b>[MANUAL-APPROVED & DELIVERED]</b>\n\n👤 <b>User:</b> {user_mention}\n🔖 <b>Order ID:</b> <code>{order['order_id']}</code>\n📚 <b>Pack:</b> <code>{order['course_id']}</code>{ch_name}\n💰 <b>Amount:</b> ₹{order['amount']}{discount_info}\n⏰ <b>Approved at:</b> {get_ist_time()}\n⚡ <b>Status:</b> ✅ एडमिन द्वारा अप्रूव किया गया"
    elif status_type == "CANCELLED":
        new_text = f"❌ <b>[ORDER CANCELLED]</b>\n\n👤 <b>User:</b> {user_mention}\n🔖 <b>Order ID:</b> <code>{order['order_id']}</code>\n📚 <b>Pack:</b> <code>{order['course_id']}</code>{ch_name}\n💰 <b>Amount:</b> ₹{order['amount']}\n⏰ <b>Cancelled at:</b> {get_ist_time()}"
    else: return

    try: 
        bot.edit_message_text(new_text, chat_id=DB_CHANNEL_ID, message_id=channel_msg_id, parse_mode="HTML")
        bot.edit_message_reply_markup(chat_id=DB_CHANNEL_ID, message_id=channel_msg_id, reply_markup=None)
    except Exception: pass

def screenshot_timeout(chat_id, order_id, prompt_msg_id):
    state = get_user_state(chat_id)
    if state and state.get("step") == "WAITING_PAYMENT_SS" and state.get("order_id") == order_id:
        clear_user_state(chat_id) 
        try: bot.edit_message_text("⏳ <b>Time is up!</b>\nYou didn't send the screenshot within 10 minutes.\nVerification failed. Please click on the pack again.", chat_id=chat_id, message_id=prompt_msg_id, parse_mode="HTML")
        except Exception: pass

def expire_qr(chat_id, message_id, course_id, amount_key, order_id):
    order = all_orders_cache.get(order_id) or orders_col.find_one({"order_id": order_id})
    if not order: return
    if order.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL", "CANCELLED"): return

    sms_rec = sms_pool_col.find_one({"amount": amount_key, "status": "UNUSED"})
    if sms_rec:
        updated = sms_pool_col.update_one({"_id": sms_rec["_id"], "status": "UNUSED"}, {"$set": {"status": "PROCESSED"}})
        if updated.modified_count > 0:
            deliver_course_to_buyer(order, sms_text=sms_rec.get("raw_text"), is_manual=False)
            return

    res = orders_col.update_one({"order_id": order_id, "status": "PENDING"}, {"$set": {"status": "EXPIRED"}})
    if res.modified_count == 0: return 

    order["status"] = "EXPIRED"
    update_channel_order_status(order, "EXPIRED")

    with pending_lock: pending_orders.pop(amount_key, None)
    if chat_id in user_qr_messages and user_qr_messages[chat_id] == message_id: del user_qr_messages[chat_id]
    
    try: bot.delete_message(chat_id, message_id)
    except Exception: pass
    if order.get("qr_msg_id") and order.get("qr_msg_id") != message_id:
        try: bot.delete_message(chat_id, order.get("qr_msg_id"))
        except Exception: pass

    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Verify Payment", callback_data=f"paydone_{order_id}"))
    try: bot.send_message(chat_id, "⏳ <b>QR Code has expired!</b>\n\nIf you have already paid, click <b>'✅ Verify Payment'</b> below.", reply_markup=markup, parse_mode="HTML", msg_type="general")
    except Exception: pass

# ==========================================
# 📱 BOTTOM MENU + MY ORDERS + LIVE FEED
# ==========================================
# बटन वाले मैसेज के लिए खाली (अदृश्य) टेक्स्ट, ताकि 👇 न दिखे
CARRIER_TEXT = "\u200b"
MENU_DEFAULT = [{"text": "📦 My Orders", "action": "my_orders", "value": ""}]

def esc_html(s):
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def get_menu_cfg():
    saved = get_cached_setting("menu_cfg") or {}
    btns = saved.get("buttons") if isinstance(saved.get("buttons"), list) else MENU_DEFAULT
    btns = [b for b in btns if b.get("text")]
    if not saved.get("my_orders_removed") and not any(b.get("action") == "my_orders" for b in btns):
        btns = [dict(MENU_DEFAULT[0])] + btns
    return btns

def save_menu_buttons(buttons):
    """Saves the full bottom-menu list. Remembers if the owner removed My Orders on purpose."""
    removed = not any(b.get("action") == "my_orders" for b in buttons)
    settings_col.update_one({"_id": "menu_cfg"},
        {"$set": {"buttons": buttons, "my_orders_removed": removed, "updated_at": get_ist_time()}}, upsert=True)
    invalidate_cache("menu_cfg")

def menu_markup():
    btns = get_menu_cfg()
    if not btns: return None
    m = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    for i in range(0, len(btns), 2):
        m.row(*[telebot.types.KeyboardButton(b["text"]) for b in btns[i:i + 2]])
    return m

def find_menu_button(text):
    t = (text or "").strip()
    for b in get_menu_cfg():
        if b["text"].strip() == t: return b
    return None

def handle_menu_action(user_id, btn, first_name):
    act = btn.get("action")
    if act == "my_orders":
        send_my_orders(user_id, first_name)
    elif act == "link" and str(btn.get("value", "")).startswith("http"):
        m = InlineKeyboardMarkup().row(kbtn(btn["text"], url=btn["value"]))
        orig_send_message(user_id, CARRIER_TEXT, reply_markup=m)
    elif act == "message":
        markup = None
        links = [l for l in (btn.get("links") or []) if str(l.get("url", "")).startswith("http")]
        if links:
            markup = InlineKeyboardMarkup()
            for l in links[:8]: markup.row(kbtn(l.get("text") or "Open", url=l["url"], style=l.get("style")))
        body = btn.get("value") or CARRIER_TEXT
        try: orig_send_message(user_id, body, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception: orig_send_message(user_id, re.sub(r"<[^>]*>", "", body), reply_markup=markup, disable_web_page_preview=True)

CHAT_TITLE_CACHE = {}

def channel_title_for(course):
    """Channel का असली नाम: पहले channel_name, नहीं तो Telegram से चैनल का title, फिर pack का title"""
    name = (course or {}).get("channel_name")
    if name: return name
    ids = (course or {}).get("channel_ids") or []
    cid = (course or {}).get("channel_id") or (ids[0] if ids else None)
    if cid:
        if cid not in CHAT_TITLE_CACHE:
            try: CHAT_TITLE_CACHE[cid] = bot.get_chat(int(cid)).title or ""
            except Exception: CHAT_TITLE_CACHE[cid] = ""
        if CHAT_TITLE_CACHE[cid]: return CHAT_TITLE_CACHE[cid]
    return (course or {}).get("title") or (course or {}).get("course_id") or "Channel"

def get_footer_text():
    saved = get_cached_setting("menu_cfg") or {}
    return saved["footer"] if "footer" in saved else "🔥 More packs coming soon, stay tuned!"

def send_my_orders(user_id, first_name):
    purchases = list(purchases_col.find({"user_id": user_id}).sort("_id", 1))
    info_cache = {}

    def label(cid):
        if cid not in info_cache:
            c = courses_col.find_one({"course_id": cid}, {"channel_name": 1, "title": 1, "channel_id": 1, "channel_ids": 1}) or {}
            info_cache[cid] = channel_title_for(c) if c else cid
        return info_cache[cid]

    lines = ["📋 <b>MY ORDER LIST</b>", "━━━━━━━━━━━━━━", "",
             f"👤 <b>{esc_html(first_name or 'User')}</b>",
             f"🛍 Total purchases: <b>{len(purchases)}</b>", "",
             "💸 <b>My purchases :-</b>", ""]
    if not purchases:
        lines.append("You have no purchases yet.")
    for i, p in enumerate(purchases[:20], 1):
        cid = str(p.get("item_info", "")).split(" | ")[0]
        lines += [f"<b>{i}. Order :-</b>",
                  f"   📡 Channel : {esc_html(label(cid))}",
                  f"   💰 Amount : ₹{esc_html(purchase_amount(p))}",
                  f"   🗓️ Date : {esc_html(p.get('date', ''))}", ""]

    # Left section: सिर्फ़ उन channels का, जिनसे यूज़र अभी बाहर है (latest event = left)
    last = {}
    for e in member_events_col.find({"user_id": user_id}).sort("ts", 1):
        last[e.get("course_id")] = e
    left = sorted([e for e in last.values() if e.get("event") == "left"], key=lambda e: e.get("ts", 0), reverse=True)
    if left:
        lines += ["━━━━━━━━━━━━━━", "", "🕘 <b>Channel left</b>", ""]
        for i, e in enumerate(left[:20], 1):
            lines += [f"<b>{i}. Channel left :-</b>",
                      f"   📡 Channel : {esc_html(label(e.get('course_id', '')))}",
                      f"   🗓️ Date : {esc_html(e.get('date', ''))}", ""]

    footer = get_footer_text()
    if footer: lines += [footer]

    text = "\n".join(lines).rstrip()
    if len(text) > 3900: text = text[:3900] + "…"
    link = CHAT_LINK if str(CHAT_LINK).startswith(("http", "tg://")) else f"tg://user?id={ADMIN_ID}"
    markup = InlineKeyboardMarkup().row(kbtn("💬 Contact Admin", url=link))
    try: orig_send_message(user_id, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        print(f"⚠️ My Orders HTML error: {e}", flush=True)
        try: orig_send_message(user_id, re.sub(r"<[^>]*>", "", text), reply_markup=markup, disable_web_page_preview=True)
        except Exception as e2: print(f"⚠️ My Orders send failed: {e2}", flush=True)

def get_live_cfg():
    saved = get_cached_setting("live_feed_cfg") or {}
    return {"enabled": bool(saved.get("enabled")), "channel_id": str(saved.get("channel_id", "")),
            "share_text": saved.get("share_text") or "Join Now", "share_link": saved.get("share_link", ""),
            "backfilled_at": saved.get("backfilled_at", "")}

def purchase_amount(p):
    m = re.search(r"Rate: ₹([\d.]+)", str(p.get("item_info", "")))
    return m.group(1) if m else ""

def feed_display_name(raw, uid):
    """Feed में सिर्फ़ नाम: @username और (ID) हटाकर. नाम न मिले तो 'Customer N' (N स्थिर रहता है)"""
    cleaned = re.sub(r"\(\s*\d*\s*\)", "", re.sub(r"@\w*", "", _strip_html(raw or ""))).strip()
    if cleaned and cleaned.lower() != "user": return cleaned
    try:
        first = purchases_col.find_one({"user_id": uid}, sort=[("_id", 1)])
        n = len(purchases_col.distinct("user_id", {"_id": {"$lte": first["_id"]}})) if first else 0
    except Exception:
        n = 0
    return f"Customer {n or ''}".strip()

def post_live_feed(name, amount, channel_label, link=""):
    """सिर्फ़: ग्राहक का नाम, PAYMENT DONE, चैनल का नाम, और उस कोर्स का link. कोई बटन या 'View' नहीं."""
    cfg = get_live_cfg()
    if not cfg["enabled"] or not cfg["channel_id"]: return False
    text = (f"🔥 <b>Customer : {esc_html(name)}</b> 🔥\n\n"
            f"✅ <b>PAYMENT DONE · ₹{esc_html(amount)}</b>\n\n"
            f"📺 Channel: <b>{esc_html(channel_label)}</b> - Added")
    markup = None
    if cfg["share_link"].startswith("http"):
        markup = InlineKeyboardMarkup().row(kbtn(cfg["share_text"] or "Join Now", url=cfg["share_link"]))
    try:
        orig_send_message(int(cfg["channel_id"]), text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return True
    except Exception as e:
        print(f"⚠️ live feed post failed, plain fallback: {e}", flush=True)
        try:
            orig_send_message(int(cfg["channel_id"]), re.sub(r"<[^>]*>", "", text), reply_markup=markup, disable_web_page_preview=True)
            return True
        except Exception as e2:
            print(f"⚠️ live feed post failed: {e2}", flush=True)
            return False

LIVE_JOB = {"status": "idle", "total": 0, "done": 0, "failed": 0, "stop": False, "started": "", "message": ""}
LIVE_JOB_LOCK = threading.Lock()

def live_job_snapshot():
    with LIVE_JOB_LOCK:
        j = dict(LIVE_JOB)
    j.pop("stop", None)
    j["percent"] = 100 if not j["total"] else min(100, round((j["done"] + j["failed"]) * 100 / j["total"]))
    return j

def backfill_live_feed():
    """पुरानी purchases को feed में एक-एक करके भेजो. हर भेजने के बाद progress अपडेट होती है, और stop हो सकता है."""
    status, msg = "done", ""
    try:
        for p in purchases_col.find({"feed_posted": {"$ne": True}}).sort("_id", 1):
            with LIVE_JOB_LOCK:
                stop = LIVE_JOB["stop"]
            if stop:
                status = "stopped"; break
            if not get_live_cfg()["enabled"]:
                status, msg = "stopped", "Live feed बंद है"; break
            cid = str(p.get("item_info", "")).split(" | ")[0]
            course = courses_col.find_one({"course_id": cid}, {"channel_name": 1, "title": 1, "secret_text": 1, "channel_id": 1, "channel_ids": 1}) or {}
            ok = post_live_feed(feed_display_name(p.get("username", ""), p.get("user_id")), purchase_amount(p),
                                channel_title_for(course), extract_invite_link(course.get("secret_text", "")))
            if ok: purchases_col.update_one({"_id": p["_id"]}, {"$set": {"feed_posted": True}})
            with LIVE_JOB_LOCK:
                LIVE_JOB["done" if ok else "failed"] += 1
            time.sleep(3.2)  # Telegram: एक channel में लगभग 20 messages प्रति मिनट
    except Exception as e:
        status, msg = "error", str(e)[:200]
        print(f"⚠️ live feed job error: {e}", flush=True)
    with LIVE_JOB_LOCK:
        LIVE_JOB.update(status=status, message=msg, updated=get_ist_time())
    if status == "done":
        settings_col.update_one({"_id": "live_feed_cfg"}, {"$set": {"backfilled_at": get_ist_time()}}, upsert=True)
        invalidate_cache("live_feed_cfg")

def start_backfill_job():
    """एक समय में एक ही job. चालू हो तो False"""
    with LIVE_JOB_LOCK:
        if LIVE_JOB["status"] in ("running", "stopping"): return False
        total = purchases_col.count_documents({"feed_posted": {"$ne": True}})
        LIVE_JOB.update(status="running", total=total, done=0, failed=0, stop=False, started=get_ist_time(), message="")
    threading.Thread(target=backfill_live_feed, daemon=True).start()
    return True

SUCC_DEFAULTS = {
    "header": "🎉 <b>Payment Successful!</b>",
    "between": "",
    "body": "👉 <b>Please send a request</b> to the channel using the button below.",
    "main_btn": {"text": "📢 Send Request", "style": "success", "enabled": True},
    "buttons": [],
    "received_text": "✅ <b>You have been added to this channel.</b>\nTap the button below to open the channel.",
    "received_btn": {"text": "📺 Open Channel", "style": "primary", "enabled": True},
}

def get_success_cfg():
    """Post-purchase message की settings (Dashboard > Post-purchase message, या bot panel से).
    जो field सेट नहीं है उसका default रहता है."""
    saved = get_cached_setting("success_msg_cfg") or {}
    cfg = json.loads(json.dumps(SUCC_DEFAULTS))
    for k in ("header", "body", "received_text"):
        if k in saved and saved[k] is not None: cfg[k] = saved[k]
    cfg["between"] = saved["between"] if "between" in saved else saved.get("text", "")
    for k in ("main_btn", "received_btn"):
        if isinstance(saved.get(k), dict): cfg[k].update(saved[k])
    if isinstance(saved.get("buttons"), list): cfg["buttons"] = saved["buttons"]
    return cfg

def add_custom_buttons(markup, btns):
    for b in btns or []:
        b_url = str(b.get("url", "")).strip()
        b_text = str(b.get("text", "")).strip() or "Button"
        style = b.get("style")
        if b_url.lower() == "close": markup.row(kbtn(b_text, callback_data="close_msg", style=style))
        elif b_url.startswith("http"): markup.row(kbtn(b_text, url=b_url, style=style))
        elif b_url: markup.row(kbtn(b_text, callback_data=b_url, style=style))

def extract_invite_link(secret_text):
    m = re.search(r"https?://[^\s<>\"']+", str(secret_text or ""))
    return m.group(0) if m else ""

def send_html_safe(chat_id, text, markup=None, protect=False):
    """HTML भेजने की कोशिश; अगर HTML गलत हो तो बिना tags के plain text भेजो (customer को कुछ न कुछ ज़रूर मिले)"""
    try:
        return bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML", protect_content=protect, msg_type="purchase")
    except Exception as e:
        print(f"⚠️ HTML send failed, plain fallback: {e}", flush=True)
        plain = re.sub(r"<[^>]*>", "", text)
        return bot.send_message(chat_id, plain, reply_markup=markup, protect_content=protect, msg_type="purchase")

def send_request_received(user_id, link):
    """Join request उठने के बाद: 'request received' मैसेज + channel का बटन"""
    scfg = get_success_cfg()
    rb = scfg["received_btn"]
    markup = InlineKeyboardMarkup()
    if rb.get("enabled", True) and link:
        markup.row(kbtn(rb.get("text") or "📺 Open Channel", url=link, style=rb.get("style")))
    try:
        orig_send_message(user_id, scfg["received_text"], reply_markup=markup if markup.keyboard else None, parse_mode="HTML")
    except Exception:
        try: orig_send_message(user_id, re.sub(r"<[^>]*>", "", scfg["received_text"]), reply_markup=markup if markup.keyboard else None)
        except Exception: pass

def deliver_course_to_buyer(order, sms_text=None, is_manual=False):
    order_id, chat_id, user_id, course_id = order["order_id"], order["chat_id"], order["user_id"], order["course_id"]
    course = get_cached_course(course_id) or {}
    new_status = "COMPLETED_MANUAL" if is_manual else "COMPLETED_AUTO"
    
    res = orders_col.update_one({"order_id": order_id, "status": {"$in": ["PENDING", "EXPIRED"]}}, {"$set": {"status": new_status, "delivered_at": get_ist_time(), "delivered_at_ts": time.time()}})
    if res.modified_count == 0: return 

    order["status"] = new_status
    with pending_lock: pending_orders.pop(order.get("amount"), None)
    
    state = get_user_state(user_id)
    if state and state.get("order_id") == order_id: clear_user_state(user_id)
    
    qr_msg_id = user_qr_messages.get(chat_id) or order.get("qr_msg_id")
    if qr_msg_id:
        try: bot.delete_message(chat_id, qr_msg_id)
        except Exception: pass
    if chat_id in user_qr_messages: del user_qr_messages[chat_id]

    if not course:
        try: bot.send_message(chat_id, "⚠️ Payment verified, but pack not found. Please contact admin.", msg_type="general")
        except Exception: pass
        return

    scfg = get_success_cfg()
    is_channel = bool(course.get("channel_id") or course.get("channel_ids"))
    markup = InlineKeyboardMarkup()
    join_link = extract_invite_link(course.get("secret_text", "")) if is_channel else ""
    if is_channel:
        # Channel pack: secret link text में नहीं दिखता, सिर्फ़ बटन में जाता है
        final_text = scfg["header"]
        if scfg["between"]: final_text += f"\n\n{scfg['between']}"
        if scfg["body"]: final_text += f"\n\n{scfg['body']}"
        mb = scfg["main_btn"]
        if mb.get("enabled", True) and join_link:
            markup.row(kbtn(mb.get("text") or "📢 Send Request", url=join_link, style=mb.get("style")))
    else:
        final_text = f"{scfg['header']}\n\n{course.get('secret_text','')}"
        if scfg["between"]: final_text += f"\n\n{scfg['between']}"
    add_custom_buttons(markup, scfg["buttons"])
    if not markup.keyboard: markup = None

    try:
        sent = send_html_safe(chat_id, final_text, markup, protect=is_protected())
        if is_channel and join_link and scfg["main_btn"].get("enabled", True) and sent is not None:
            join_prompts_col.update_one({"user_id": user_id, "course_id": course_id},
                {"$set": {"chat_id": chat_id, "msg_id": sent.message_id, "invite_link": join_link, "created_at": get_ist_time()}}, upsert=True)
    except Exception: pass

    date_now = get_ist_time()
    verify_type = "MANUAL-APPROVED" if is_manual else "AUTO-VERIFIED"
    
    def _bg_log():
        purchases_col.insert_one({"user_id": user_id, "username": order.get("user_mention", f"User ({user_id})"), "item_info": f"{course_id} | Rate: ₹{order['amount']} | {verify_type} (order {order_id})", "date": date_now, "title": course.get('title', course_id), "link": course.get('secret_text', '')})
        if not is_channel:
            fp = purchases_col.find_one_and_update({"user_id": user_id, "date": date_now, "feed_posted": {"$ne": True}}, {"$set": {"feed_posted": True}})
            if fp: post_live_feed(feed_display_name(order.get("user_mention", ""), user_id), order.get("amount"), course.get("title") or course_id, extract_invite_link(course.get("secret_text", "")))
        if order.get("offer_id"):
            offers_col.update_one({"offer_code": order["offer_id"]}, {"$inc": {"used_count": 1}})
            fresh_offer = offers_col.find_one({"offer_code": order["offer_id"]})
            per_user_limit = (fresh_offer or {}).get("per_user_limit", 1)
            if per_user_limit != -1 and get_offer_usage_count(user_id, order["offer_id"]) >= per_user_limit:
                clear_user_offer(user_id, order["offer_id"])
    threading.Thread(target=_bg_log, daemon=True).start()
    
    update_channel_order_status(order, "MANUAL_APPROVED" if is_manual else "AUTO_VERIFIED", extra_text=sms_text or "")

    manual_msg_id = order.get("manual_msg_id")
    if manual_msg_id and not is_manual:
        try:
            bot.edit_message_reply_markup(DB_CHANNEL_ID, manual_msg_id, reply_markup=None)
            orig_send_message(DB_CHANNEL_ID, f"✅ <b>ORDER {order_id} Auto-Verified (delayed SMS received)</b>.", parse_mode="HTML")
        except Exception: pass

# ==========================================
# 🛑 GATEKEEPER: JOIN REQUEST HANDLER
# ==========================================
def handle_chat_member(update):
    """Channel में कौन join हुआ और कौन left हुआ, उसका रिकॉर्ड (My Orders की history के लिए)"""
    try:
        if update.new_chat_member.user.is_bot: return
        chat_id = update.chat.id
        uid = update.new_chat_member.user.id
        was_in = update.old_chat_member.status in ("member", "administrator", "creator", "restricted")
        now_in = update.new_chat_member.status in ("member", "administrator", "creator", "restricted")
        if was_in == now_in: return
        course = courses_col.find_one({"$or": [{"channel_id": chat_id}, {"channel_ids": chat_id}]}, {"course_id": 1})
        if not course: return
        member_events_col.insert_one({"user_id": uid, "course_id": course["course_id"], "chat_id": chat_id,
                                      "event": "joined" if now_in else "left", "date": get_ist_time(), "ts": time.time()})
    except Exception as e:
        print(f"⚠️ chat_member error: {e}", flush=True)

if hasattr(bot, "chat_member_handler"):
    bot.chat_member_handler()(handle_chat_member)

@bot.chat_join_request_handler()
def handle_join_request(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    used_link = message.invite_link.invite_link if message.invite_link else ""
    
    course = None
    # Now searching for either single channel_id or within channel_ids array
    for c in courses_col.find({"$or": [{"channel_id": chat_id}, {"channel_ids": chat_id}]}):
        if c.get("secret_text", "") and used_link in c.get("secret_text", ""):
            course = c
            break
            
    if not course: return
        
    if is_banned(user_id, getattr(message.from_user, "username", None)):
        try: bot.decline_chat_join_request(chat_id, user_id)
        except Exception: pass
        return

    course_id = course["course_id"]
    channel_name = course.get("channel_name") or message.chat.title or "Private Channel/Group"
    purchase = purchases_col.find_one({"user_id": user_id, "item_info": {"$regex": re.escape(course_id)}})
    now_str = get_ist_time()
    
    u_first_name = message.from_user.first_name
    u_username = message.from_user.username or "None"
    u_men = f"<a href='tg://user?id={user_id}'>{u_first_name}</a>"

    if purchase:
        try:
            bot.approve_chat_join_request(chat_id, user_id)
            def _bg_log_ap():
                channel_logs_col.insert_one({"user_id": user_id, "first_name": u_first_name, "username": u_username, "course_id": course_id, "channel_name": channel_name, "status": "APPROVED", "date": now_str})
            threading.Thread(target=_bg_log_ap, daemon=True).start()
            try:
                fp = purchases_col.find_one_and_update({"_id": purchase["_id"], "feed_posted": {"$ne": True}}, {"$set": {"feed_posted": True}})
                if fp: threading.Thread(target=post_live_feed, args=(feed_display_name(u_first_name, user_id), purchase_amount(purchase), channel_name, extract_invite_link(course.get("secret_text", "")) or used_link), daemon=True).start()
            except Exception: pass
            prompt = join_prompts_col.find_one_and_delete({"user_id": user_id, "course_id": course_id})
            if prompt:
                try: bot.delete_message(prompt.get("chat_id", user_id), prompt.get("msg_id"))
                except Exception: pass
            send_request_received(user_id, (prompt or {}).get("invite_link") or used_link or extract_invite_link(course.get("secret_text", "")))
        except Exception: pass
    else:
        try:
            bot.decline_chat_join_request(chat_id, user_id)
            def _bg_log_dn():
                channel_logs_col.insert_one({"user_id": user_id, "first_name": u_first_name, "username": u_username, "course_id": course_id, "channel_name": channel_name, "status": "DENIED", "date": now_str})
            threading.Thread(target=_bg_log_dn, daemon=True).start()
            orig_send_message(user_id, "❌ <b>Access Denied!</b>\nYou haven't purchased this pack yet. Please buy it from the bot first.", parse_mode="HTML")
            log_msg = f"🚫 <b>[JOIN DENIED - NO PAYMENT]</b>\n\n👤 <b>User:</b> {u_men} (<code>{user_id}</code>)\n📺 <b>Channel:</b> {channel_name}\n⏰ <b>Time:</b> {now_str}"
            orig_send_message(DB_CHANNEL_ID, log_msg, parse_mode="HTML")
        except Exception: pass

# ==========================================
# 🛑 COURSE SENDING & MENUS
# ==========================================
def send_course_to_user(chat_id, course):
    raw_promo = course.get("promo_media", [])
    if isinstance(raw_promo, str):
        try: promo_items = json.loads(raw_promo)
        except Exception: promo_items = []
    elif isinstance(raw_promo, list): promo_items = raw_promo
    else: promo_items = []

    base_price = float(course["amount"])
    final_price, applied_flash_id, applied_promo_code, promo_disc_pct, flash_sale = calculate_final_price(chat_id, course["course_id"], base_price)

    btn_text = f"🇮🇳 UPI (Pay ₹{int(final_price) if final_price.is_integer() else final_price})"
    if applied_promo_code: btn_text = f"🎉 Offer Applied (Pay ₹{int(final_price) if final_price.is_integer() else final_price})"
    elif applied_flash_id and flash_sale and flash_sale.get("mode") == "drop": btn_text = f"⚡ Flash Sale (Pay ₹{int(final_price) if final_price.is_integer() else final_price})"

    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton(btn_text, callback_data=f"pay_upi_{course['course_id']}"))
    
    intl_cfg = get_cached_setting("intl_btn_cfg")
    if intl_cfg:
        b_name = intl_cfg.get("btn_name", "🌍 International")
        markup.row(InlineKeyboardButton(b_name, callback_data="show_intl_info"))
    elif INTERNATIONAL_LINK:
        markup.row(InlineKeyboardButton("🌍 International", url=INTERNATIONAL_LINK))

    global_cbtns_cfg = get_cached_setting("global_course_btns")
    if global_cbtns_cfg:
        for b in global_cbtns_cfg.get("buttons", []):
            b_url = b.get("url", "")
            if b_url.lower() == "close": markup.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
            elif b_url.startswith("http"): markup.row(InlineKeyboardButton(b["text"], url=b_url))
            else: markup.row(InlineKeyboardButton(b["text"], callback_data=b_url))

    extra_btns = course.get("extra_buttons", [])
    for b in extra_btns:
        b_url = b.get("url", "")
        if b_url.lower() == "close": markup.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
        elif b_url.startswith("http"): markup.row(InlineKeyboardButton(b["text"], url=b_url))
        else: markup.row(InlineKeyboardButton(b["text"], callback_data=b_url))

    media_items = [it for it in promo_items if isinstance(it, dict) and it.get("type") in ["photo", "video"]]
    text_items = [it for it in promo_items if isinstance(it, dict) and it.get("type") == "text"]

    first_photo_caption = media_items[0].get("caption", "").strip() if media_items else ""
    custom_caption = course.get("custom_caption", "").strip()
    final_cap = f"{first_photo_caption}\n\n{custom_caption}" if first_photo_caption and custom_caption else (first_photo_caption or custom_caption)

    sent_mids = []
    if not media_items:
        full_text = "".join(t.get("caption", "") + "\n\n" for t in text_items) + custom_caption
        if not full_text.strip(): full_text = f"📚 <b>Pack: {course.get('title', course['course_id'])}</b>\nPrice: ₹{int(final_price) if final_price.is_integer() else final_price}"
        msg = bot.send_message(chat_id, full_text.strip(), reply_markup=markup, parse_mode="HTML", protect_content=is_protected(), msg_type="course")
        sent_mids.append(msg.message_id)
    elif len(media_items) == 1:
        it = media_items[0]
        try:
            if it["type"] == "photo": msg = bot.send_photo(chat_id, it["file_id"], caption=final_cap, reply_markup=markup, parse_mode="HTML", protect_content=is_protected(), msg_type="course")
            elif it["type"] == "video": msg = bot.send_video(chat_id, it["file_id"], caption=final_cap, reply_markup=markup, parse_mode="HTML", protect_content=is_protected(), msg_type="course")
            if msg: sent_mids.append(msg.message_id)
        except Exception: pass
    else:
        media_group_html = []
        for i, item in enumerate(media_items):
            cap = final_cap if i == 0 else ""
            if item["type"] == "photo": media_group_html.append(InputMediaPhoto(item["file_id"], caption=cap, parse_mode="HTML"))
            elif item["type"] == "video": media_group_html.append(InputMediaVideo(item["file_id"], caption=cap, parse_mode="HTML"))
        try:
            sent_grp = orig_send_media_group(chat_id, media_group_html, protect_content=is_protected())
            for m in sent_grp: 
                register_activity(chat_id, m.message_id, "course")
                sent_mids.append(m.message_id)
        except Exception: pass
        try: 
            msg = bot.send_message(chat_id, "👆 <b>Choose an option to buy:</b>\n", reply_markup=markup, parse_mode="HTML", msg_type="course")
            if msg: sent_mids.append(msg.message_id)
        except Exception: pass

    schedule_course_auto_delete(chat_id, sent_mids)

def send_batch_to_user(chat_id, batch):
    msg = bot.send_message(chat_id, f"📦 <b>{batch['title']}</b>\nAll packs are listed below:", parse_mode="HTML", msg_type="course")
    if msg: schedule_course_auto_delete(chat_id, [msg.message_id])
    
    raw_cids = batch.get("course_ids", [])
    course_ids = json.loads(raw_cids) if isinstance(raw_cids, str) else raw_cids if isinstance(raw_cids, list) else []
    for cid in course_ids:
        c_data = get_cached_course(cid)
        if c_data: send_course_to_user(chat_id, c_data)

def send_custom_start_menu(chat_id):
    user_data = purchases_col.find({"user_id": chat_id})
    total_purchases = list(user_data)
    
    active_offers = offers_col.count_documents({"expires_at_ts": {"$gt": time.time()}})
    offer_status = "✅ Active" if active_offers > 0 else "❌ No Active Offers"
    
    cfg = get_cached_setting("start_menu")
    markup = InlineKeyboardMarkup()
    
    if cfg:
        for b in cfg.get("buttons", []):
            b_url = b.get("url", "")
            st = b.get("style")
            if b_url.lower() == "close": markup.row(kbtn(b["text"], callback_data="close_msg", style=st))
            elif b_url.startswith("http"): markup.row(kbtn(b["text"], url=b_url, style=st))
            else: markup.row(kbtn(b["text"], callback_data=f"mainmenu_{b_url}", style=st))
        
        base_text = cfg.get("text", "👋 Welcome to our Store!")
        custom_status_footer = f"\n\n📊 <b>Your Stats:</b>\n🛒 Total Courses Bought: <b>{len(total_purchases)}</b>\n🎁 Offers Status: <b>{offer_status}</b>"
        final_welcome_text = base_text + custom_status_footer

        m_type, fid = cfg.get("media_type"), cfg.get("file_id")
        if m_type == "photo" and fid: bot.send_photo(chat_id, fid, caption=final_welcome_text, reply_markup=markup, parse_mode="HTML", msg_type="menu")
        elif m_type == "video" and fid: bot.send_video(chat_id, fid, caption=final_welcome_text, reply_markup=markup, parse_mode="HTML", msg_type="menu")
        else: bot.send_message(chat_id, final_welcome_text, reply_markup=markup, parse_mode="HTML", msg_type="menu")
    else:
        default_text = f"👋 <b>Welcome to our Store!</b>\n\nSelect an option below to get started:\n\n📊 <b>Your Stats:</b>\n🛒 Total Courses Bought: <b>{len(total_purchases)}</b>\n🎁 Offers Status: <b>{offer_status}</b>"
        bot.send_message(chat_id, default_text, reply_markup=markup, parse_mode="HTML", msg_type="menu")

# ---- Admin panel: categories -> sub-menus (सभी callbacks वही हैं, बस व्यवस्थित) ----
ADMIN_CATEGORIES = {
    "packs": ("📦 Packs & Batches", [
        ("➕ Add Single Pack", "admin_add_course"),
        ("✏️ Edit Course", "admin_edit_course"),
        ("🗑 Delete Pack/Batch/File", "admin_delete_course"),
        ("📦 Create Batch (New Packs)", "admin_create_batch"),
        ("📚 Create Batch (Existing Packs)", "admin_batch_existing"),
        ("📋 Manage Store Plans", "admin_manage_plans"),
    ]),
    "offers": ("🎟 Offers & Pricing", [
        ("🎟 Create Promo Offer", "admin_create_offer"),
        ("⚡ Flash Price (Hike/Drop)", "admin_flash_price"),
    ]),
    "menus": ("🎨 Menus & Buttons", [
        ("🎨 Customize Start Menu", "admin_custom_menu"),
        ("🌍 Custom International Button", "admin_custom_intl"),
        ("🔘 Global Course Buttons", "admin_global_cbtns"),
        ("📱 Bottom Menu Buttons", "admin_menu_btns"),
    ]),
    "delivery": ("🎉 Delivery & Files", [
        ("🎉 Post-Purchase Setup", "admin_success_msg"),
        ("🔗 Advanced File to Link", "admin_file_link"),
    ]),
    "broadcast": ("📢 Broadcast & Users", [
        ("📢 Advanced Broadcast", "admin_broadcast"),
        ("👥 User Info", "admin_user_info"),
        ("🚫 Ban / Unban", "admin_ban_menu"),
    ]),
}
ADMIN_HOME_TEXT = "🛠 <b>Admin Panel</b>\nनीचे से कैटेगरी चुनें 👇"

def admin_home_markup():
    m = InlineKeyboardMarkup()
    for key, (title, _) in ADMIN_CATEGORIES.items():
        m.row(InlineKeyboardButton(title, callback_data=f"admin_cat_{key}"))
    return m

def send_admin_panel(chat_id):
    bot.send_message(chat_id, ADMIN_HOME_TEXT, reply_markup=admin_home_markup(), parse_mode="HTML")

def show_menu_btns(chat_id, msg_id):
    btns = get_menu_cfg()
    kinds = {"my_orders": "My Order List", "link": "link", "message": "message"}
    lines = [f"{i + 1}. {esc_html(b['text'])} <i>({kinds.get(b.get('action'), '')})</i>" for i, b in enumerate(btns)]
    txt = "📱 <b>Bottom Menu Buttons</b>\n\n" + ("\n".join(lines) if lines else "अभी कोई बटन नहीं है।")
    m = InlineKeyboardMarkup()
    for i, b in enumerate(btns):
        m.row(InlineKeyboardButton(f"🗑 {b['text'][:40]}", callback_data=f"menubtn_del_{i}"))
    m.row(InlineKeyboardButton("➕ Add button", callback_data="menubtn_add"))
    m.row(InlineKeyboardButton("🔙 Back", callback_data="admin_cat_menus"))
    bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")

def show_admin_category(chat_id, msg_id, key):
    title, items = ADMIN_CATEGORIES[key]
    m = InlineKeyboardMarkup()
    for label, cb in items:
        m.row(InlineKeyboardButton(label, callback_data=cb))
    m.row(InlineKeyboardButton("🔙 Back", callback_data="admin_home"))
    bot.edit_message_text(f"<b>{title}</b>\nपसंद का ऑप्शन चुनें:", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")


# ==========================================
# COMMANDS & HANDLERS
# ==========================================
@bot.message_handler(commands=["orders"])
def orders_command(message):
    """/orders = My Order List (बटन के बिना भी चलता है)"""
    if is_banned(message.chat.id, getattr(message.from_user, "username", None)): return
    send_my_orders(message.chat.id, getattr(message.from_user, "first_name", ""))

@bot.message_handler(commands=["cancel"])
def cancel_command(message):
    """किसी भी चल रहे step/flow को रोकने के लिए (user + admin दोनों)"""
    uid = message.chat.id
    clear_user_state(uid)
    admin_data.pop(uid, None)
    bot.send_message(uid, "❌ <b>Cancelled.</b>\nकोई भी चल रहा process रोक दिया गया है। /start भेजें।", parse_mode="HTML", msg_type="general")

@bot.message_handler(commands=["start"])
def start_command(message):
    if is_banned(message.chat.id, getattr(message.from_user, "username", None)): return
    user_id = message.chat.id
    
    if is_maintenance_mode() and not is_admin(user_id):
        orig_send_message(user_id, "⚠️ <b>Bot is currently under maintenance.</b>\nServers are busy or undergoing updates. Please try again after some time.", parse_mode="HTML")
        return

    if not check_rate_limit(user_id, 1): return
    register_activity(user_id, message.message_id, "general")
    
    mk = menu_markup()
    if mk: orig_send_message(user_id, CARRIER_TEXT, reply_markup=mk)

    def _bg_start():
        try: users_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id, "username": message.from_user.username or "", "updated_at": get_ist_time()}}, upsert=True)
        except: pass
    threading.Thread(target=_bg_start, daemon=True).start()
    
    param = message.text.split()[1].strip() if len(message.text.split()) > 1 else ""

    if param.startswith("off_"):
        offer = get_cached_offer(param)
        if not offer:
            bot.send_message(user_id, "❌ <b>This offer is invalid or has expired.</b>", parse_mode="HTML", msg_type="general")
            return send_custom_start_menu(user_id)
        now_ts = time.time()
        if offer.get("expires_at_ts") and now_ts > offer["expires_at_ts"]:
            bot.send_message(user_id, "⏳ <b>This offer has expired!</b>", parse_mode="HTML", msg_type="general")
            return send_custom_start_menu(user_id)
        if offer.get("max_users", -1) != -1 and offer.get("used_count", 0) >= offer["max_users"]:
            bot.send_message(user_id, "⚠️ <b>This offer has reached its maximum claim limit!</b>", parse_mode="HTML", msg_type="general")
            return send_custom_start_menu(user_id)
        per_user_limit = offer.get("per_user_limit", 1)
        if per_user_limit != -1 and get_offer_usage_count(user_id, offer["offer_code"]) >= per_user_limit:
            bot.send_message(user_id, "⚠️ <b>You have already used this offer once.</b>\nIt cannot be claimed again.", parse_mode="HTML", msg_type="general")
            return send_custom_start_menu(user_id)
        
        def _bg_off():
            try:
                users_col.update_one({"user_id": user_id}, {"$set": {"active_offer_code": offer["offer_code"]}, "$unset": {"active_offer": ""}}, upsert=True)
                invalidate_cache(f"user_{user_id}")
            except: pass
        threading.Thread(target=_bg_off, daemon=True).start()
        
        bot.send_message(user_id, f"🎉 <b>Congrats! {offer['discount_percent']}% discount activated!</b>", parse_mode="HTML", msg_type="general")
        if offer["target_type"] == "single":
            c = get_cached_course(offer["target_course_id"])
            if c:
                def _sc(): send_course_to_user(user_id, c)
                threading.Thread(target=_sc, daemon=True).start()
            else: send_custom_start_menu(user_id)
        else: send_custom_start_menu(user_id)

    elif param.startswith("b_"):
        def _sb():
            batch = batches_col.find_one({"batch_id": param})
            if batch: send_batch_to_user(user_id, batch)
            else: bot.send_message(user_id, "❌ <b>This link has expired.</b>", parse_mode="HTML", msg_type="general")
        threading.Thread(target=_sb, daemon=True).start()

    elif param.startswith("c_"):
        def _sc():
            course = get_cached_course(param)
            if course: send_course_to_user(user_id, course)
            else: bot.send_message(user_id, "❌ <b>This link is not available.</b>", parse_mode="HTML", msg_type="general")
        threading.Thread(target=_sc, daemon=True).start()

    elif param.startswith("f_"):
        file_data = file_links_col.find_one({"file_code": param})
        if file_data:
            m_items, btns = file_data.get("media_data", []), file_data.get("button_data", [])
            markup = InlineKeyboardMarkup()
            for b in btns:
                b_url = b.get("url", "")
                if b_url.lower() == "close": markup.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
                elif b_url.startswith("http"): markup.row(InlineKeyboardButton(b["text"], url=b_url))
                else: markup.row(InlineKeyboardButton(b["text"], callback_data=b_url))
                
            def _sf():
                sent_mids = []
                if len(m_items) == 1:
                    it = m_items[0]
                    try:
                        msg = None
                        if it["type"] == "text": msg = bot.send_message(user_id, it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=is_protected(), msg_type="course")
                        elif it["type"] == "photo": msg = bot.send_photo(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=is_protected(), msg_type="course")
                        elif it["type"] == "video": msg = bot.send_video(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=is_protected(), msg_type="course")
                        elif it["type"] == "document": msg = bot.send_document(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=is_protected(), msg_type="course")
                        if msg: sent_mids.append(msg.message_id)
                    except Exception as e: bot.send_message(user_id, f"❌ Error: {e}", msg_type="general")
                elif len(m_items) > 1:
                    m_group = []
                    for it in m_items:
                        if it["type"] == "photo": m_group.append(InputMediaPhoto(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                        elif it["type"] == "video": m_group.append(InputMediaVideo(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                        elif it["type"] == "document": m_group.append(InputMediaDocument(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                    try:
                        sent = orig_send_media_group(user_id, m_group, protect_content=is_protected())
                        for m in sent: 
                            register_activity(user_id, m.message_id, "course")
                            sent_mids.append(m.message_id)
                        if btns or any(i["type"] == "text" for i in m_items): 
                            msg = bot.send_message(user_id, CARRIER_TEXT, reply_markup=markup, parse_mode="HTML", msg_type="course")
                            if msg: sent_mids.append(msg.message_id)
                    except Exception as e: bot.send_message(user_id, f"❌ Error: {e}", msg_type="general")
                
                schedule_course_auto_delete(user_id, sent_mids)

            threading.Thread(target=_sf, daemon=True).start()
        else: bot.send_message(user_id, "❌ <b>File not found or expired.</b>", parse_mode="HTML", msg_type="general")
    else:
        if is_admin(user_id): send_admin_panel(user_id)
        else: send_custom_start_menu(user_id)

@bot.message_handler(content_types=["photo", "video", "document", "text"])
def handle_all_messages(message):
    user_id = message.chat.id
    chat_id = user_id  # private chat में chat_id == admin/user id (admin flows इसी key से चलते हैं)
    if is_maintenance_mode() and not is_admin(user_id):
        orig_send_message(user_id, "⚠ <b>Bot is currently under maintenance.</b>\nServers are busy or undergoing updates. Please try again after some time.", parse_mode="HTML")
        return
    if is_banned(user_id, getattr(message.from_user, "username", None)): return
    if message.content_type == "text":
        mb = find_menu_button(message.text)
        if mb:
            try: handle_menu_action(user_id, mb, getattr(message.from_user, "first_name", ""))
            except Exception as e:
                import traceback; traceback.print_exc()
                orig_send_message(user_id, "⚠️ Something went wrong. Please try again in a moment.")
            return
    if not check_rate_limit(user_id, 1): return
    register_activity(user_id, message.message_id, "general")

    state = get_user_state(user_id)
    if state and state.get("step") == "WAITING_PAYMENT_SS":
        order_id = state.get("order_id")
        order = all_orders_cache.get(order_id) or orders_col.find_one({"order_id": order_id})
        if not message.photo and not message.document:
            bot.send_message(user_id, "❌ <b>Please send your screenshot as a photo or document.</b>", parse_mode="HTML", msg_type="general")
            return
        fid = message.photo[-1].file_id if message.photo else message.document.file_id
        bot.send_message(user_id, "⏳ <b>Verification pending...</b>\nYour screenshot has been sent to admin.", parse_mode="HTML", msg_type="general")
        clear_user_state(user_id)
        u_str = f"@{message.from_user.username}" if message.from_user.username else "No Username"
        u_men = f"<a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> ({u_str})"
        
        course_id = order['course_id'] if order else 'N/A'
        course_obj = get_cached_course(course_id) if order else None
        ch_name_display = f"\n📺 <b>Channel:</b> {course_obj.get('channel_name')}" if course_obj and course_obj.get("channel_name") else ""

        cap = f"📩 <b>[MANUAL APPROVAL - PAYMENT SCREENSHOT]</b>\n\n👤 <b>User:</b> {u_men}\n🆔 <b>ID:</b> <code>{user_id}</code>\n🔖 <b>Order:</b> <code>{order_id}</code>\n📚 <b>Pack:</b> <code>{course_id}</code>{ch_name_display}\n💰 <b>Amount:</b> ₹{order['amount'] if order else 'N/A'}\n⏰ <b>Time:</b> {get_ist_time()}"
        
        c_url = f"https://t.me/{message.from_user.username}" if message.from_user.username else f"tg://user?id={user_id}"
        m_admin = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Approve", callback_data=f"man_appr_{order_id}"), InlineKeyboardButton("❌ Deny", callback_data=f"man_deny_{order_id}")).row(InlineKeyboardButton("💬 Chat with User", url=c_url))
        try:
            if message.photo: sent_admin_msg = orig_send_photo(DB_CHANNEL_ID, fid, caption=cap, reply_markup=m_admin, parse_mode="HTML")
            else: sent_admin_msg = orig_send_document(DB_CHANNEL_ID, fid, caption=cap, reply_markup=m_admin, parse_mode="HTML")
            orders_col.update_one({"order_id": order_id}, {"$set": {"manual_msg_id": sent_admin_msg.message_id}})
            if order: order["manual_msg_id"] = sent_admin_msg.message_id
        except Exception as e: orig_send_message(ADMIN_ID, f"❌ Channel error: {e}")
        return

    if is_admin(user_id) and user_id in admin_data:
        step = admin_data[user_id].get("step")
        
        if step == "DELETE_COURSE":
            cid = message.text.strip()
            if courses_col.delete_one({"course_id": cid}).deleted_count:
                settings_col.update_one({"_id": "store_plans"}, {"$pull": {"course_ids": cid}})
                invalidate_cache(f"course_{cid}")
                bot.send_message(chat_id,f"✅ <b>Pack <code>{cid}</code> Deleted!</b>", parse_mode="HTML")
            elif batches_col.delete_one({"batch_id": cid}).deleted_count:
                bot.send_message(chat_id,f"✅ <b>Batch <code>{cid}</code> Deleted!</b>", parse_mode="HTML")
            elif file_links_col.delete_one({"file_code": cid}).deleted_count:
                bot.send_message(chat_id,f"✅ <b>File Link <code>{cid}</code> Deleted!</b>", parse_mode="HTML")
            else: 
                bot.send_message(chat_id,"❌ <b>ID not found in Courses, Batches, or File Links.</b>", parse_mode="HTML")
            del admin_data[chat_id]
            return send_admin_panel(chat_id)
            
        elif step == "ADD_PLAN_ID":
            cid = message.text.strip()
            if courses_col.find_one({"course_id": cid}):
                settings_col.update_one({"_id": "store_plans"}, {"$addToSet": {"course_ids": cid}}, upsert=True)
                invalidate_cache("store_plans")
                bot.send_message(chat_id,f"✅ <b>Course <code>{cid}</code> added to Store Plans!</b>", parse_mode="HTML")
            else: bot.send_message(chat_id,"❌ <b>Invalid ID.</b>", parse_mode="HTML")
            del admin_data[chat_id]
            return send_admin_panel(chat_id)
            
        elif step == "BATCH_EXISTING_TITLE":
            admin_data[chat_id]["title"] = message.text.strip()
            admin_data[chat_id]["step"] = "BATCH_EXISTING_IDS"
            bot.send_message(chat_id,"✅ Title saved.\n\nNow send <b>Course IDs</b> one by one (e.g. <code>c_123456</code>).\nSend <code>done</code> when finished.", parse_mode="HTML")
            return
            
        elif step == "BATCH_EXISTING_IDS":
            txt = message.text.strip()
            if txt.lower() == "done":
                if not admin_data[chat_id].get("course_ids"):
                    bot.send_message(chat_id,"❌ Batch is empty. Setup cancelled.")
                    del admin_data[chat_id]
                    return send_admin_panel(chat_id)
                bid = "b_" + str(uuid.uuid4())[:6]
                batches_col.update_one({"batch_id": bid}, {"$set": {"batch_id": bid, "title": admin_data[chat_id]["title"], "course_ids": admin_data[chat_id]["course_ids"]}}, upsert=True)
                bot.send_message(chat_id,f"🎉 <b>Batch Created Successfully!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={bid}</code>", parse_mode="HTML")
                del admin_data[chat_id]
                return send_admin_panel(chat_id)
            else:
                cid = txt
                if not courses_col.find_one({"course_id": cid}):
                    bot.send_message(chat_id,f"❌ <b>Course ID <code>{cid}</code> not found!</b> Try again.", parse_mode="HTML")
                else:
                    admin_data[chat_id]["course_ids"].append(cid)
                    bot.send_message(chat_id,f"✅ Added <code>{cid}</code>. Send next ID or type <code>done</code>.", parse_mode="HTML")
            return

        elif step == "EDIT_COURSE_ID":
            cid = message.text.strip()
            course = courses_col.find_one({"course_id": cid})
            if not course:
                bot.send_message(chat_id,"❌ Course ID not found. Send correct ID:")
                return
            admin_data[chat_id]["course_id"] = cid
            admin_data[chat_id]["step"] = "EDIT_COURSE_TEXT"
            bot.send_message(chat_id,f"✅ Found course: <b>{course.get('title', cid)}</b>\n\nSend new <b>Text/Description</b> (or type <code>skip</code> to keep old):", parse_mode="HTML")
            return
        elif step == "EDIT_COURSE_TEXT":
            txt = get_formatted_text(message)
            if txt.lower() != "skip":
                courses_col.update_one({"course_id": admin_data[chat_id]["course_id"]}, {"$set": {"custom_caption": txt}})
            admin_data[chat_id]["step"] = "EDIT_COURSE_PHOTO"
            bot.send_message(chat_id,"✅ Text updated!\n\nNow send new <b>Photo</b> (or type <code>skip</code> if photo change nahi karni):", parse_mode="HTML")
            return
        elif step == "EDIT_COURSE_PHOTO":
            cid = admin_data[chat_id]["course_id"]
            if message.photo:
                photo_id = message.photo[-1].file_id
                promo_data = [{"type": "photo", "file_id": photo_id, "caption": ""}]
                courses_col.update_one({"course_id": cid}, {"$set": {"promo_media": promo_data}})
            bot.send_message(chat_id,f"🎉 <b>Course <code>{cid}</code> successfully updated!</b>", parse_mode="HTML")
            del admin_data[chat_id]
            return send_admin_panel(chat_id)
        elif step == "FLASH_PCT":
            try:
                pct = float(re.sub(r"[^\d.]", "", message.text.strip()))
                admin_data[chat_id]["percent"] = pct
                admin_data[chat_id]["step"] = "FLASH_TARGET"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("🌐 All Courses", callback_data="flash_tgt_all")).row(InlineKeyboardButton("🎯 Single Course", callback_data="flash_tgt_single"))
                bot.send_message(chat_id,f"✅ <b>{pct}% Set!</b>\nApply to:", reply_markup=m, parse_mode="HTML")
            except Exception: bot.send_message(chat_id,"❌ Numbers only.")
            return
        elif step == "FLASH_CID":
            cid = message.text.strip()
            if not courses_col.find_one({"course_id": cid}): return bot.send_message(chat_id,"❌ Course ID not found.")
            admin_data[chat_id]["course_id"] = cid
            admin_data[chat_id]["step"] = "FLASH_HOURS"
            bot.send_message(chat_id,"⏳ <b>Active for how many hours?</b> (e.g., 24)", parse_mode="HTML")
            return
        elif step == "FLASH_HOURS":
            try:
                hrs = float(re.sub(r"[^\d.]", "", message.text.strip()))
                admin_data[chat_id]["hours"] = hrs
                admin_data[chat_id]["step"] = "FLASH_LIMIT"
                bot.send_message(chat_id,"🔁 <b>How many times can a user buy at this new price?</b>\n(1 = Once per user, 0 = Unlimited):", parse_mode="HTML")
            except Exception: bot.send_message(chat_id,"❌ Invalid hours.")
            return
        elif step == "FLASH_LIMIT":
            try:
                lim = int(re.sub(r"[^\d]", "", message.text.strip()))
                sale_id = "fs_" + str(uuid.uuid4())[:6]
                now_ts = time.time()
                doc = {
                    "_id": "flash_sale", "sale_id": sale_id, "mode": admin_data[chat_id]["flash_mode"],
                    "percent": admin_data[chat_id]["percent"], "target": admin_data[chat_id]["target"],
                    "course_id": admin_data[chat_id].get("course_id"), "hours": admin_data[chat_id]["hours"],
                    "limit": lim, "created_at_ts": now_ts, "expires_at_ts": now_ts + (admin_data[chat_id]["hours"] * 3600)
                }
                settings_col.update_one({"_id": "flash_sale"}, {"$set": doc}, upsert=True)
                invalidate_cache("flash_sale")
                bot.send_message(chat_id,"🎉 <b>Flash Price Applied Successfully!</b>", parse_mode="HTML")
                del admin_data[chat_id]
                send_admin_panel(chat_id)
            except Exception: bot.send_message(chat_id,"❌ Invalid limit.")
            return
        elif step == "OFFER_DISCOUNT":
            try:
                disc = int(re.sub(r"[^\d]", "", message.text.strip()))
                if not (1 <= disc <= 100): raise ValueError()
                admin_data[chat_id]["discount"], admin_data[chat_id]["step"] = disc, "OFFER_TARGET"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("🌐 All Courses", callback_data="offtarget_all")).row(InlineKeyboardButton("🎯 Single Course", callback_data="offtarget_single"))
                bot.send_message(chat_id,f"✅ Discount <b>{disc}%</b> set!\nApply to:", reply_markup=m, parse_mode="HTML")
            except Exception: bot.send_message(chat_id,"❌ 1 to 100 only.")
            return
        elif step == "OFFER_SINGLE_CID":
            cid = message.text.strip()
            if not courses_col.find_one({"course_id": cid}): return bot.send_message(chat_id,"❌ <b>Course ID not found. Send correct ID:</b>", parse_mode="HTML")
            admin_data[chat_id]["target_course_id"], admin_data[chat_id]["step"] = cid, "OFFER_LIMIT"
            bot.send_message(chat_id,"👥 <b>Max users?</b> (0 for unlimited):", parse_mode="HTML")
            return
        elif step == "OFFER_LIMIT":
            try:
                lim = int(re.sub(r"[^\d]", "", message.text.strip()))
                admin_data[chat_id]["max_users"], admin_data[chat_id]["step"] = -1 if lim == 0 else lim, "OFFER_PERUSER"
                bot.send_message(chat_id,"🔁 <b>How many times can a user claim this offer?</b>\n(<b>1</b> = just once per user. <b>0</b> = unlimited):", parse_mode="HTML")
            except Exception: bot.send_message(chat_id,"❌ Invalid number.")
            return
        elif step == "OFFER_PERUSER":
            try:
                pl = int(re.sub(r"[^\d]", "", message.text.strip()))
                admin_data[chat_id]["per_user_limit"], admin_data[chat_id]["step"] = -1 if pl == 0 else pl, "OFFER_HOURS"
                bot.send_message(chat_id,"⏳ <b>Active for how many hours?</b> (e.g. 24 or 48):", parse_mode="HTML")
            except Exception: bot.send_message(chat_id,"❌ Invalid number.")
            return
        elif step == "OFFER_HOURS":
            try:
                hrs = float(re.sub(r"[^\d.]", "", message.text.strip()))
                off_code, now_ts = "off_" + str(uuid.uuid4())[:6], time.time()
                doc = {
                    "offer_code": off_code, "discount_percent": admin_data[chat_id]["discount"],
                    "target_type": admin_data[chat_id]["target_type"], "target_course_id": admin_data[chat_id].get("target_course_id"),
                    "max_users": admin_data[chat_id]["max_users"], "per_user_limit": admin_data[chat_id].get("per_user_limit", 1), "used_count": 0, "created_at_ts": now_ts,
                    "expires_at_ts": now_ts + (hrs * 3600), "expires_str": (datetime.now(IST) + timedelta(hours=hrs)).strftime("%d-%m-%Y %I:%M %p")
                }
                offers_col.insert_one(doc)
                bot.send_message(chat_id,f"🎉 <b>Discount Offer Link Created!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={off_code}</code>", parse_mode="HTML")
                del admin_data[chat_id]
                send_admin_panel(chat_id)
            except Exception: bot.send_message(chat_id,"❌ Invalid hours.")
            return
        elif step == "MENU_CUSTOM_CONTENT":
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            admin_data[chat_id]["menu_content"] = {"media_type": mt, "file_id": fid, "text": get_formatted_text(message)}
            admin_data[chat_id]["buttons"], admin_data[chat_id]["step"] = [], "MENU_ADD_BUTTONS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="menu_finish_save"))
            bot.send_message(chat_id,"✅ <b>Content Saved!</b>\nAdd buttons: <code>Button Name - Link</code> (or type <code>Close - close</code>).", reply_markup=m, parse_mode="HTML")
            return
        elif step == "MENU_ADD_BUTTONS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[chat_id]["buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save Menu", callback_data="menu_finish_save"))
                    bot.send_message(chat_id,f"✅ <b>Button Added! ({len(admin_data[chat_id]['buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(chat_id,"❌ Format error. <code>Name - Link</code>", parse_mode="HTML")
            return
        elif step == "GCBTN_ADD":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[chat_id]["buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="gcbtn_finish"))
                    bot.send_message(chat_id,f"✅ <b>Global Button Added! ({len(admin_data[chat_id]['buttons'])})</b>\nSend another or Finish:", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(chat_id,"❌ Format error. <code>Name - Link</code>", parse_mode="HTML")
            return
        elif step in ("BAN_TARGET", "UNBAN_TARGET"):
            tgt = message.text.strip()
            kind, _ = parse_ban_target(tgt)
            if not kind:
                return bot.send_message(chat_id, "❌ सही User ID (सिर्फ़ numbers) या @username भेजें।", parse_mode="HTML")
            if step == "UNBAN_TARGET":
                admin_data.pop(chat_id, None)
                try:
                    n = unban_user(tgt)
                    bot.send_message(chat_id, f"✅ <b>Unban हो गया।</b> ({n} record हटाए)", parse_mode="HTML")
                except Exception as e:
                    bot.send_message(chat_id, f"❌ {e}", parse_mode="HTML")
                return
            admin_data[chat_id]["ban_target"] = tgt
            admin_data[chat_id]["step"] = "BAN_DURATION"
            m = (InlineKeyboardMarkup()
                 .row(InlineKeyboardButton("1 hour", callback_data="ban_dur_1"), InlineKeyboardButton("24 hours", callback_data="ban_dur_24"))
                 .row(InlineKeyboardButton("7 days", callback_data="ban_dur_168"), InlineKeyboardButton("30 days", callback_data="ban_dur_720"))
                 .row(InlineKeyboardButton("♾ Permanent", callback_data="ban_dur_perm")))
            bot.send_message(chat_id, "⏱ <b>कितने समय के लिए ban करें?</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "MBTN_NAME":
            name = message.text.strip()[:64]
            if not name: return bot.send_message(chat_id, "❌ Button name खाली नहीं हो सकता।", parse_mode="HTML")
            admin_data[chat_id]["mbtn_name"] = name
            admin_data[chat_id]["step"] = "MBTN_TEXT"
            bot.send_message(chat_id, "✏️ <b>अब वो text भेजें</b> जो बटन दबाने पर जाए.\nBold, link वगैरह Telegram के formatting से लगाकर भेज सकते हैं।", parse_mode="HTML")
            return
        elif step == "MBTN_TEXT":
            body = get_formatted_text(message).strip()
            if not body: return bot.send_message(chat_id, "❌ Text खाली नहीं हो सकता।", parse_mode="HTML")
            admin_data[chat_id]["mbtn_text"] = body[:2000]
            admin_data[chat_id]["step"] = "MBTN_LINKS"
            bot.send_message(chat_id, "🔗 <b>Step 3:</b> Link buttons भेजें, एक लाइन में एक:\n<code>Button text - https://link</code>\nकई बटन एक साथ भेज सकते हैं। कोई link नहीं चाहिए तो <code>skip</code> भेजें।", parse_mode="HTML")
            return
        elif step == "MBTN_LINKS":
            raw = (message.text or "").strip()
            links = []
            if raw.lower() != "skip":
                for line in raw.splitlines():
                    if not line.strip(): continue
                    try:
                        t, u, st = parse_btn_line(line)
                    except ValueError:
                        return bot.send_message(chat_id, "❌ Format गलत है। हर लाइन: <code>Button text - https://link</code>", parse_mode="HTML")
                    if not u.startswith("http"):
                        return bot.send_message(chat_id, "❌ Link https:// से शुरू होना चाहिए।", parse_mode="HTML")
                    links.append({"text": t[:64], "url": u, "style": st})
                if len(links) > 8:
                    return bot.send_message(chat_id, "❌ एक message में ज़्यादा से ज़्यादा 8 link buttons।", parse_mode="HTML")
            name = admin_data[chat_id].get("mbtn_name", "Button")
            body = admin_data[chat_id].get("mbtn_text", "")
            admin_data.pop(chat_id, None)
            buttons = get_menu_cfg() + [{"text": name, "action": "message", "value": body, "links": links}]
            save_menu_buttons(buttons)
            bot.send_message(chat_id, f"✅ <b>Button added:</b> {esc_html(name)} ({len(links)} link button{'s' if len(links) != 1 else ''})", parse_mode="HTML")
            return
        elif step == "SUCC_SET_TEXT":
            admin_data[chat_id]["text"] = get_formatted_text(message)
            admin_data[chat_id]["buttons"] = []
            admin_data[chat_id]["step"] = "SUCC_ADD_BTNS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="succ_finish"))
            bot.send_message(chat_id,"✅ <b>Text Saved!</b>\n\n🔘 <b>Step 2: Add Buttons</b>\nFormat: <code>Button Text - URL</code>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "SUCC_ADD_BTNS":
            txt = message.text.strip()
            try:
                t, u, st = parse_btn_line(txt)
                admin_data[chat_id]["buttons"].append({"text": t, "url": u, "style": st})
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="succ_finish"))
                bot.send_message(chat_id, f"✅ <b>Button Added! ({len(admin_data[chat_id]['buttons'])})</b>", reply_markup=m, parse_mode="HTML")
            except Exception:
                bot.send_message(chat_id, "❌ Format: <code>Button Text - URL</code>", parse_mode="HTML")
            return
        elif step == "INTL_SET_NAME":
            b_name = message.text.strip()
            if not b_name: return bot.send_message(chat_id,"❌ Name cannot be empty.")
            admin_data[chat_id]["intl_name"] = b_name
            admin_data[chat_id]["step"] = "INTL_SET_CONTENT"
            bot.send_message(chat_id,f"✅ <b>Button Name Saved:</b> <code>{b_name}</code>\n\n📝 Send Photo with Caption OR Plain Text:", parse_mode="HTML")
            return
        elif step == "INTL_SET_CONTENT":
            fid = message.photo[-1].file_id if message.photo else None
            txt = get_formatted_text(message)
            admin_data[chat_id]["intl_photo"] = fid
            admin_data[chat_id]["intl_text"] = txt
            admin_data[chat_id]["intl_buttons"] = []
            admin_data[chat_id]["step"] = "INTL_ADD_BUTTONS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="intl_finish_save"))
            bot.send_message(chat_id,"✅ <b>Content Saved!</b> Add Action Buttons: <code>Button Text - URL</code>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "INTL_ADD_BUTTONS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[chat_id]["intl_buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="intl_finish_save"))
                    bot.send_message(chat_id,f"✅ <b>Button Added! ({len(admin_data[chat_id]['intl_buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(chat_id,"❌ Format error.", parse_mode="HTML")
            return
        elif step == "PROMO":
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            admin_data[chat_id]["promo"].append({"type": mt, "file_id": fid, "caption": get_formatted_text(message)})
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step (Price)", callback_data="next_price"))
            bot.send_message(chat_id,f"✅ <b>{mt.capitalize()} saved!</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "AMOUNT":
            amt = re.sub(r"[^\d.]", "", message.text.strip())
            if not amt: return bot.send_message(chat_id,"❌ <b>Numbers only.</b>", parse_mode="HTML")
            admin_data[chat_id]["amount"], admin_data[chat_id]["step"] = amt, "CAPTION"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Skip (No Caption)", callback_data="skip_caption"))
            bot.send_message(chat_id,f"✅ <b>Price ₹{amt} saved!</b>\n📝 Type optional extra caption, or skip:", reply_markup=m, parse_mode="HTML")
            return
        elif step == "CAPTION":
            admin_data[chat_id]["caption"] = get_formatted_text(message)
            admin_data[chat_id]["course_buttons"] = []
            admin_data[chat_id]["step"] = "COURSE_BUTTONS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Finish Buttons", callback_data="skip_course_buttons"))
            bot.send_message(chat_id,"✅ <b>Caption saved!</b>\n🔘 Optional Extra Buttons: <code>Button Text - URL</code>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "COURSE_BUTTONS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[chat_id]["course_buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Finish Buttons", callback_data="skip_course_buttons"))
                    bot.send_message(chat_id,f"✅ <b>Button Added! ({len(admin_data[chat_id]['course_buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(chat_id,"❌ Format error.", parse_mode="HTML")
            return
        elif step == "SECRET":
            cid = "c_" + str(uuid.uuid4())[:6]
            courses_col.update_one({"course_id": cid}, {"$set": {
                "course_id": cid, "title": admin_data[chat_id].get("title", cid), "promo_media": admin_data[chat_id]["promo"], "amount": admin_data[chat_id]["amount"],
                "custom_caption": admin_data[chat_id].get("caption",""), "secret_text": get_formatted_text(message),
                "extra_buttons": admin_data[chat_id].get("course_buttons", [])
            }}, upsert=True)
            if admin_data[chat_id].get("mode") == "single":
                bot.send_message(chat_id,f"🎉 <b>Pack created!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={cid}</code>", parse_mode="HTML")
                del admin_data[chat_id]
                send_admin_panel(chat_id)
            elif admin_data[chat_id].get("mode") == "batch":
                admin_data[chat_id]["course_ids"].append(cid)
                admin_data[chat_id]["step"] = "NEXT_ACTION"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Another", callback_data="batch_add_next")).row(InlineKeyboardButton("✅ Finish Batch", callback_data="batch_finish"))
                bot.send_message(chat_id,"✅ <b>Pack saved!</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "CHANNEL_ID":
            channel_id = None
            if message.forward_from_chat: channel_id = message.forward_from_chat.id
            elif message.text:
                text = message.text.strip()
                match = re.search(r"t\.me/c/(\d+)", text)
                if match: channel_id = int(f"-100{match.group(1)}")
                elif text.startswith("-100") and text.replace("-", "").isdigit(): channel_id = int(text)
            
            if not channel_id:
                return bot.send_message(chat_id,"❌ Please forward a message from the Private Channel/Group or send its link.")
            
            try:
                chat_info = bot.get_chat(channel_id)
                channel_name = chat_info.title if chat_info.title else "Private Channel"
                link = bot.create_chat_invite_link(channel_id, creates_join_request=True)
                
                if "channels" not in admin_data[chat_id]:
                    admin_data[chat_id]["channels"] = []
                    
                admin_data[chat_id]["channels"].append({
                    "id": channel_id,
                    "name": channel_name,
                    "link": link.invite_link
                })
                
                m = InlineKeyboardMarkup().row(
                    InlineKeyboardButton("➕ Add Another Channel", callback_data="batch_add_channel")
                ).row(
                    InlineKeyboardButton("✅ Finish & Save Pack", callback_data="batch_finish_channel")
                )
                bot.send_message(chat_id,f"✅ <b>Added: {channel_name}</b>\n\nDo you want to add another channel to this pack, or finish?", reply_markup=m, parse_mode="HTML")
            except Exception as e:
                bot.send_message(chat_id,f"❌ Error: Make sure the bot is an Admin in the Channel/Group first! ({e})")
            return
        elif step == "TITLE":
            admin_data[chat_id]["title"], admin_data[chat_id]["step"], admin_data[chat_id]["promo"] = message.text.strip(), "PROMO", []
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step", callback_data="next_price"))
            bot.send_message(chat_id,"✅ Title saved. <b>Send promo media/text:</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step in ["BC_MEDIA", "FTL_MEDIA"]:
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            elif message.document: mt, fid = "document", message.document.file_id
            admin_data[chat_id].setdefault("media", []).append({"type": mt, "file_id": fid, "caption": get_formatted_text(message)})
            cb = "bc_done" if step == "BC_MEDIA" else "ftl_done"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Done Adding", callback_data=cb))
            bot.send_message(chat_id,"✅ <b>Saved!</b> Send another or click Done.", reply_markup=m, parse_mode="HTML")
            return
        elif step in ["BC_BUTTONS", "FTL_BUTTONS"]:
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[chat_id].setdefault("buttons", []).append({"text": t.strip(), "url": u.strip()})
                    cb = "bc_finish" if step == "BC_BUTTONS" else "ftl_finish"
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data=cb))
                    bot.send_message(chat_id,"✅ <b>Button Added!</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(chat_id,"❌ Format Error.", parse_mode="HTML")
            return
        elif step == "BC_TIME":
            try:
                hrs = float(re.sub(r"[^\d.]", "", message.text.strip()))
                m_items = admin_data[chat_id].get("media", [])
                btns = admin_data[chat_id].get("buttons", [])
                
                m = InlineKeyboardMarkup()
                for b in btns:
                    b_url = b.get("url", "")
                    if b_url.lower() == "close": m.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
                    elif b_url.startswith("http"): m.row(InlineKeyboardButton(b["text"], url=b_url))
                    else: m.row(InlineKeyboardButton(b["text"], callback_data=b_url))
                
                if not m.keyboard: m = None
                bot.send_message(chat_id,"⏳ Broadcasting started...")
                
                def run_bc():
                    success = 0
                    delete_list = []
                    all_users = list(users_col.find({}))
                    for u in all_users:
                        raw_uid = u.get("user_id") or u.get("chat_id") or u.get("_id")
                        if not raw_uid: continue
                        try: uid = int(raw_uid)
                        except: continue
                        if is_banned(uid): continue  # banned यूज़र को broadcast नहीं

                        try:
                            if not m_items:
                                if m:
                                    msg = orig_send_message(uid, CARRIER_TEXT, reply_markup=m, parse_mode="HTML", disable_web_page_preview=True)
                                    register_activity(uid, msg.message_id, "broadcast")
                                    if hrs > 0: delete_list.append((uid, msg.message_id))
                                    success += 1
                                continue

                            sent_ids = []
                            if len(m_items) == 1:
                                it = m_items[0]
                                if it["type"] == "text": msg = orig_send_message(uid, it["caption"], reply_markup=m, parse_mode="HTML", disable_web_page_preview=True)
                                elif it["type"] == "photo": msg = orig_send_photo(uid, it["file_id"], caption=it["caption"], reply_markup=m, parse_mode="HTML")
                                elif it["type"] == "video": msg = orig_send_video(uid, it["file_id"], caption=it["caption"], reply_markup=m, parse_mode="HTML")
                                elif it["type"] == "document": msg = orig_send_document(uid, it["file_id"], caption=it["caption"], reply_markup=m, parse_mode="HTML")
                                sent_ids.append(msg.message_id)
                            else:
                                m_group = [InputMediaPhoto(it["file_id"], caption=it["caption"], parse_mode="HTML") if it["type"] == "photo" else InputMediaVideo(it["file_id"], caption=it["caption"], parse_mode="HTML") if it["type"] == "video" else InputMediaDocument(it["file_id"], caption=it["caption"], parse_mode="HTML") for it in m_items]
                                sent = orig_send_media_group(uid, m_group)
                                sent_ids.extend([s.message_id for s in sent])
                                if m or any(i["type"] == "text" for i in m_items): 
                                    msg = orig_send_message(uid, CARRIER_TEXT, reply_markup=m, parse_mode="HTML", disable_web_page_preview=True)
                                    sent_ids.append(msg.message_id)
                                    
                            for mid in sent_ids:
                                register_activity(uid, mid, "broadcast")
                                if hrs > 0: delete_list.append((uid, mid))
                                
                            success += 1
                            time.sleep(0.04)
                        except Exception: pass
                        
                    bot.send_message(chat_id,f"✅ <b>Broadcast Complete!</b> ({success} users).", parse_mode="HTML")
                    
                    if hrs > 0:
                        def delayed_delete(d_list):
                            for uid, mid in d_list:
                                try:
                                    bot.delete_message(uid, mid)
                                    time.sleep(0.05)
                                except Exception: pass
                                with tracker_lock:
                                    if uid in user_chat_messages:
                                        user_chat_messages[uid] = [x for x in user_chat_messages[uid] if x["id"] != mid]
                        threading.Timer(hrs * 3600, delayed_delete, args=(delete_list,)).start()
                        
                threading.Thread(target=run_bc, daemon=True).start()
                del admin_data[chat_id]
                send_admin_panel(chat_id)
                
            except Exception:
                bot.send_message(chat_id,"❌ Invalid number. Send 0 or higher.")
            return

# ये prefixes सिर्फ admins के लिए हैं (user-facing बटन इनमें नहीं आते)
ADMIN_ONLY_PREFIXES = (
    "admin_", "ban_", "unban_", "menubtn_", "back_to_admin", "flash_", "gcbtn_", "succ_", "intl_", "menu_", "batch_",
    "bc_", "ftl_", "offtarget_", "ctype_", "plan_", "next_price", "skip_caption",
    "skip_course_buttons", "ch_app_", "ch_can_", "man_appr_", "man_deny_",
)

@bot.callback_query_handler(func=lambda call: True)
def handle_buttons(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    if data == "close_msg":
        try: bot.delete_message(chat_id, msg_id)
        except Exception: pass
        try: bot.answer_callback_query(call.id)
        except Exception: pass
        return

    # actor = जिसने बटन दबाया (channel के बटन में chat_id channel का होता है, इसलिए from_user ज़रूरी है)
    actor = call.from_user.id

    if is_banned(actor, getattr(call.from_user, "username", None)):
        try: bot.answer_callback_query(call.id, "🚫 आप इस bot से banned हैं।", show_alert=True)
        except Exception: pass
        return

    if is_maintenance_mode() and not is_admin(actor):
        try: bot.answer_callback_query(call.id, "⚠️ Bot is currently under maintenance. Please try again later.", show_alert=True)
        except Exception: pass
        return

    # 🔐 Admin-only बटन सिर्फ admins चला सकते हैं (Owner + Dashboard से add किए गए admins)
    if data.startswith(ADMIN_ONLY_PREFIXES) and not is_admin(actor):
        try: bot.answer_callback_query(call.id, "⛔ इस ऑप्शन की permission आपके पास नहीं है।", show_alert=True)
        except Exception: pass
        return

    if not check_rate_limit(chat_id, 1.2): 
        try: bot.answer_callback_query(call.id, "⚠️ Please slow down! Don't click too fast.", show_alert=False)
        except Exception: pass
        return

    def bg_answer(text=None, alert=False):
        try: bot.answer_callback_query(call.id, text=text, show_alert=alert)
        except Exception: pass

    threading.Thread(target=bg_answer, daemon=True).start()
    register_activity(chat_id, msg_id, "general")

    if data.startswith("ch_app_"):
        oid = data.replace("ch_app_", "")
        o = all_orders_cache.get(oid) or orders_col.find_one({"order_id": oid})
        if o and o.get("status") in ["PENDING", "EXPIRED"]:
            deliver_course_to_buyer(o, sms_text="Channel Force Approved", is_manual=True)
            try:
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
                orig_send_message(chat_id, f"✅ <b>ORDER {oid} Force Approved by Admin</b>", reply_to_message_id=msg_id, parse_mode="HTML")
            except Exception: pass
        return

    if data.startswith("ch_can_"):
        oid = data.replace("ch_can_", "")
        o = all_orders_cache.get(oid) or orders_col.find_one({"order_id": oid})
        if o and o.get("status") in ["PENDING", "EXPIRED"]:
            orders_col.update_one({"order_id": oid}, {"$set": {"status": "CANCELLED"}})
            with pending_lock: pending_orders.pop(o.get("amount"), None)
            update_channel_order_status(o, "CANCELLED")
            uid = o.get("user_id") or o.get("chat_id")
            qmid = o.get("qr_msg_id")
            if uid and qmid:
                try: bot.delete_message(uid, qmid)
                except Exception: pass
                try: bot.send_message(uid, "❌ <b>Your order has been cancelled by Admin.</b>", parse_mode="HTML", msg_type="general")
                except Exception: pass
            try:
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
                orig_send_message(chat_id, f"❌ <b>ORDER {oid} Cancelled by Admin</b>", reply_to_message_id=msg_id, parse_mode="HTML")
            except Exception: pass
        return

    if data == "user_my_purchases_btn":
        def _bg_my_purchases():
            purchases = list(purchases_col.find({"user_id": chat_id}))
            if not purchases:
                bot.send_message(chat_id, "❌ तुमने अभी तक कोई कोर्स नहीं खरीदा है।", parse_mode="HTML", msg_type="general")
                return
            text = "🛍 <b>तुम्हारी खरीदारी (My Purchases):</b>\n\n"
            for p in purchases:
                text += f"🔹 <b>Course:</b> {p.get('title', 'Unknown')}\n🔗 <b>Link:</b> {p.get('link', 'No link')}\n\n"
            bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True, msg_type="purchase")
        threading.Thread(target=_bg_my_purchases, daemon=True).start()
        return

    if data.startswith("cancel_order_"):
        oid = data.replace("cancel_order_", "")
        o_chk = orders_col.find_one({"order_id": oid}, {"user_id": 1, "chat_id": 1})
        if o_chk and o_chk.get("user_id") != actor and not is_admin(actor):
            return  # दूसरे user का order cancel नहीं कर सकते
        orders_col.update_one({"order_id": oid}, {"$set": {"status": "CANCELLED"}})
        with pending_lock:
            for k, v in list(pending_orders.items()):
                if v.get("order_id") == oid:
                    pending_orders.pop(k, None)
                    break
        try:
            bot.delete_message(chat_id, msg_id)
            bot.send_message(chat_id, "❌ तुम्हारा ऑर्डर कैंसल कर दिया गया है।", parse_mode="HTML", msg_type="general")
        except Exception: pass
        return

    if data.startswith("paydone_"):
        def _bg_paydone():
            oid = data.replace("paydone_", "")
            order = all_orders_cache.get(oid) or orders_col.find_one({"order_id": oid})
            if not order: return
            if order.get("user_id") != actor and not is_admin(actor): return  # दूसरे का order verify नहीं होगा
            if order.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"): return

            amt_key = order.get("amount")
            sms_rec = sms_pool_col.find_one({"amount": amt_key, "status": "UNUSED"})
            if sms_rec:
                updated = sms_pool_col.update_one({"_id": sms_rec["_id"], "status": "UNUSED"}, {"$set": {"status": "PROCESSED"}})
                if updated.modified_count > 0:
                    deliver_course_to_buyer(order, sms_text=sms_rec.get("raw_text"), is_manual=False)
                    return
                
            try: bot.delete_message(chat_id, msg_id)
            except Exception: pass
            
            set_user_state(chat_id, "WAITING_PAYMENT_SS", oid)
            try: 
                prompt_msg = bot.send_message(chat_id, "📸 <b>Please send your payment screenshot here.</b>\n⏳ <i>(Please send it within 10 minutes, otherwise it will fail)</i>", parse_mode="HTML", msg_type="general")
                threading.Timer(600, screenshot_timeout, args=(chat_id, oid, prompt_msg.message_id)).start()
            except Exception: pass
        threading.Thread(target=_bg_paydone, daemon=True).start()
        return

    if data == "admin_menu_btns":
        show_menu_btns(chat_id, msg_id)
        return
    if data == "menubtn_add":
        admin_data[chat_id] = {"step": "MBTN_NAME"}
        bot.edit_message_text("✏️ <b>Step 1:</b> बटन का नाम भेजें (जैसे 📋 Help)", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return
    if data.startswith("menubtn_del_"):
        try: idx = int(data.replace("menubtn_del_", ""))
        except ValueError: return
        buttons = get_menu_cfg()
        if 0 <= idx < len(buttons):
            removed = buttons.pop(idx)
            save_menu_buttons(buttons)
            bot.answer_callback_query(call.id, f"Deleted: {removed.get('text', '')}")
        show_menu_btns(chat_id, msg_id)
        return
    if data == "admin_ban_menu":
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚫 Ban user", callback_data="ban_start")).row(InlineKeyboardButton("✅ Unban user", callback_data="unban_start")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("🚫 <b>Ban / Unban</b>\nUser ID या @username से ban या unban करें।", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
        return
    if data in ("ban_start", "unban_start"):
        admin_data[chat_id] = {"step": "BAN_TARGET" if data == "ban_start" else "UNBAN_TARGET"}
        bot.edit_message_text("✏️ User ID (सिर्फ़ numbers) या @username भेजें:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return
    if data.startswith("ban_dur_"):
        tgt = admin_data.get(chat_id, {}).get("ban_target")
        if not tgt:
            bot.edit_message_text("❌ पहले user ID या username भेजें।", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
            return
        code = data.replace("ban_dur_", "")
        labels = {"1": "1 घंटे", "24": "24 घंटे", "168": "7 दिन", "720": "30 दिन", "perm": "हमेशा के लिए"}
        try:
            set_ban(tgt, None if code == "perm" else float(code))
            admin_data.pop(chat_id, None)
            bot.edit_message_text(f"🚫 <b>Ban हो गया:</b> {labels.get(code, code)}", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        except Exception as e:
            bot.edit_message_text(f"❌ Ban नहीं हुआ: {e}", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return

    if data.startswith("send_ss_"):
        o_chk = orders_col.find_one({"order_id": data.replace("send_ss_", "")}, {"user_id": 1})
        if o_chk and o_chk.get("user_id") != actor and not is_admin(actor): return
        set_user_state(chat_id, "WAITING_PAYMENT_SS", data.replace("send_ss_", ""))
        bot.send_message(chat_id, "📸 <b>Please send your payment screenshot.</b>", parse_mode="HTML", msg_type="general")
        return

    if data.startswith("man_appr_"):
        def _bg_man_appr():
            oid = data.replace("man_appr_", "")
            o = all_orders_cache.get(oid) or orders_col.find_one({"order_id": oid})
            if not o: return
            if o.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"):
                try: bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
                except Exception: pass
                return
            
            deliver_course_to_buyer(o, sms_text="Manual Approval", is_manual=True)
            try:
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
                orig_send_message(chat_id, f"✅ <b>ORDER {oid} APPROVED</b>\n⏰ {get_ist_time()}", reply_to_message_id=msg_id, parse_mode="HTML")
            except Exception: pass
        threading.Thread(target=_bg_man_appr, daemon=True).start()
        return

    if data.startswith("man_deny_"):
        def _bg_man_deny():
            oid = data.replace("man_deny_", "")
            o = all_orders_cache.get(oid) or orders_col.find_one({"order_id": oid})
            if not o: return
            if o.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"):
                try: bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
                except Exception: pass
                return
                
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("💬 Contact Admin", url=CHAT_LINK)) if CHAT_LINK else None
            try: bot.send_message(o["chat_id"], f"❌ <b>Payment Failed!</b>\nOrder <code>{oid}</code> rejected.", reply_markup=m, parse_mode="HTML", msg_type="general")
            except Exception: pass
            
            try:
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
                orig_send_message(chat_id, f"❌ <b>ORDER {oid} REJECTED</b>", reply_to_message_id=msg_id, parse_mode="HTML")
            except Exception: pass
        threading.Thread(target=_bg_man_deny, daemon=True).start()
        return

    if data.startswith("pay_upi_"):
        def _bg_pay_upi():
            course_id = data.replace("pay_upi_", "")
            course = get_cached_course(course_id)
            if course:
                base_price = float(course["amount"])
                final_price, applied_flash_id, applied_promo_code, promo_disc_pct, flash_sale = calculate_final_price(chat_id, course_id, base_price)

                clicked_price = None
                if call.message and call.message.reply_markup:
                    for row in call.message.reply_markup.keyboard:
                        for btn in row:
                            if btn.callback_data == data:
                                m = re.search(r'₹([\d\.]+)', btn.text)
                                if m: clicked_price = float(m.group(1))
                                break

                if clicked_price is not None and abs(clicked_price - final_price) > 0.01:
                    try: bot.send_message(chat_id, f"⚠️ <b>Price Updated / कीमत बदल गई है!</b>\nCurrent price is ₹{int(final_price) if final_price.is_integer() else final_price}.", parse_mode="HTML", msg_type="general")
                    except: pass
                    return

                wait_msg = None
                try: wait_msg = bot.send_message(chat_id, "⏳ <i>Generating your UPI QR Code... Just a moment!</i>", parse_mode="HTML", msg_type="general")
                except Exception: pass

                state = get_user_state(chat_id)
                if state and state.get("step") == "PENDING_UPI":
                    with pending_lock: pending_orders.pop(state.get("amount_key", ""), None)
                    if chat_id in user_qr_messages:
                        try: bot.delete_message(chat_id, user_qr_messages.pop(chat_id))
                        except Exception: pass
                        
                order_id = str(uuid.uuid4())[:8]
                with alloc_lock:
                    amt_key = generate_unique_amount(final_price)
                if amt_key is None:
                    try:
                        if wait_msg: bot.delete_message(chat_id, wait_msg.message_id)
                    except Exception: pass
                    try: bot.send_message(chat_id, "⚠️ <b>इस समय बहुत सारे payments चल रहे हैं।</b>\nकृपया कुछ मिनट बाद फिर से कोशिश करें।", parse_mode="HTML", msg_type="general")
                    except Exception: pass
                    return
                u_men = f"<a href='tg://user?id={call.from_user.id}'>{call.from_user.first_name}</a> (@{call.from_user.username or ''})"
                o_data = {
                    "order_id": order_id, "course_id": course_id, "user_id": call.from_user.id, "chat_id": chat_id,
                    "user_mention": u_men, "amount": amt_key, "original_amount": str(base_price), "discount_percent": promo_disc_pct,
                    "offer_id": applied_promo_code, "flash_id": applied_flash_id, "status": "PENDING", 
                    "created_at_str": get_ist_time(), "created_at": time.time(), "created_at_dt": datetime.now(timezone.utc), "channel_msg_id": None
                }
                d_log = f"\n🎟 <b>Offer Applied:</b> {promo_disc_pct}% OFF" if promo_disc_pct else ""
                
                ch_name_display = f"\n📺 <b>Channel:</b> {course.get('channel_name')}" if course.get("channel_name") else f"\n📚 <b>Pack:</b> <code>{course_id}</code>"
                ch_txt = f"🟡 <b>[ORDER INITIATED - QR]</b>\n\n👤 <b>User:</b> {u_men}\n🔖 <b>Order:</b> <code>{order_id}</code>{ch_name_display}\n💰 <b>Amount:</b> ₹{amt_key}{d_log}\n⏳ <b>Status:</b> ⏳ Pending"
                
                ch_markup = InlineKeyboardMarkup()
                ch_markup.row(
                    InlineKeyboardButton("✅ Force Approve", callback_data=f"ch_app_{order_id}"),
                    InlineKeyboardButton("❌ Cancel Order", callback_data=f"ch_can_{order_id}")
                )
                ch_markup.row(InlineKeyboardButton("💬 Chat with User", url=f"tg://user?id={call.from_user.id}"))

                try:
                    ch_msg = bot.send_message(DB_CHANNEL_ID, ch_txt, reply_markup=ch_markup, parse_mode="HTML")
                    o_data["channel_msg_id"] = ch_msg.message_id
                except Exception: pass
                
                orders_col.insert_one(o_data.copy())
                reserved_now.pop(amt_key, None)
                with pending_lock:
                    pending_orders[amt_key] = o_data
                    all_orders_cache[order_id] = o_data
                    
                set_user_state(chat_id, "PENDING_UPI", order_id, amt_key)

                sms_rec = sms_pool_col.find_one({"amount": amt_key, "status": "UNUSED"})
                if sms_rec:
                    updated = sms_pool_col.update_one({"_id": sms_rec["_id"], "status": "UNUSED"}, {"$set": {"status": "PROCESSED"}})
                    if updated.modified_count > 0:
                        if wait_msg:
                            try: bot.delete_message(chat_id, wait_msg.message_id)
                            except Exception: pass
                        return deliver_course_to_buyer(o_data, sms_text=sms_rec.get("raw_text"), is_manual=False)

                qr_img_bio, clean_amt = generate_upi_qr(amt_key, order_id)
                inv = f"👤 <b>User:</b> {call.from_user.first_name}\n🆔 <b>Order:</b> <code>{order_id}</code>\n💰 <b>Amount:</b> ₹{clean_amt}\n⚠️ <b>Please pay the exact amount shown.</b>\n⏳ <i>QR will expire in {QR_EXPIRY_SECONDS // 60} minutes.</i>\n\n⚡ <b>Scan & Pay to get automatically added to the Channel/Group!</b>"

                m = InlineKeyboardMarkup()
                if CHAT_LINK: m.row(InlineKeyboardButton("💬 Chat with Me", url=CHAT_LINK))
                m.row(InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_order_{order_id}"))
                
                sent_msg = bot.send_photo(chat_id, photo=qr_img_bio, caption=inv, reply_markup=m, parse_mode="HTML", msg_type="general")
                user_qr_messages[chat_id] = sent_msg.message_id
                orders_col.update_one({"order_id": order_id}, {"$set": {"qr_msg_id": sent_msg.message_id}})
                
                if wait_msg:
                    try: bot.delete_message(chat_id, wait_msg.message_id)
                    except Exception: pass
                    
                threading.Timer(QR_EXPIRY_SECONDS, expire_qr, args=(chat_id, sent_msg.message_id, course_id, amt_key, order_id)).start()
        threading.Thread(target=_bg_pay_upi, daemon=True).start()
        return

    # ADMIN PANEL HANDLERS
    if data == "admin_home":
        bot.edit_message_text(ADMIN_HOME_TEXT, chat_id=chat_id, message_id=msg_id, reply_markup=admin_home_markup(), parse_mode="HTML")
    elif data.startswith("admin_cat_"):
        cat = data[len("admin_cat_"):]
        if cat in ADMIN_CATEGORIES: show_admin_category(chat_id, msg_id, cat)
    elif data == "admin_flash_price":
        m = InlineKeyboardMarkup()
        m.row(InlineKeyboardButton("📈 Price Hike (+)", callback_data="flash_start_hike"), InlineKeyboardButton("📉 Price Drop (-)", callback_data="flash_start_drop"))
        m.row(InlineKeyboardButton("🔄 Reset to Normal Prices", callback_data="flash_reset"))
        m.row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("⚡ <b>Flash Price Manager</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data in ["flash_start_hike", "flash_start_drop"]:
        admin_data[chat_id] = {"step": "FLASH_PCT", "flash_mode": "hike" if data == "flash_start_hike" else "drop"}
        bot.edit_message_text("✏️ <b>Enter Percentage:</b>\n(e.g., Send 20 for 20%)", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "flash_reset":
        settings_col.delete_one({"_id": "flash_sale"})
        invalidate_cache("flash_sale")
        bot.edit_message_text("✅ <b>All prices reset to normal!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "flash_tgt_all":
        admin_data[chat_id]["target"] = "all"
        admin_data[chat_id]["step"] = "FLASH_HOURS"
        bot.edit_message_text("⏳ <b>Active for how many hours?</b> (e.g., 24)", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "flash_tgt_single":
        admin_data[chat_id]["target"] = "single"
        admin_data[chat_id]["step"] = "FLASH_CID"
        bot.edit_message_text("🎯 <b>Send Course ID:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "skip_course_buttons":
        admin_data[chat_id]["step"] = "COURSE_TYPE"
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("📝 Text / Secret Link", callback_data="ctype_text"), InlineKeyboardButton("📢 Private Channel/Group", callback_data="ctype_channel"))
        bot.edit_message_text("✅ <b>Buttons saved!</b>\nWhat will the user get after payment?", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "admin_global_cbtns":
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add/Update Buttons", callback_data="gcbtn_add")).row(InlineKeyboardButton("🗑 Clear All Buttons", callback_data="gcbtn_clear")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("🔘 <b>Global Course Buttons</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "gcbtn_add":
        admin_data[chat_id] = {"step": "GCBTN_ADD", "buttons": []}
        bot.edit_message_text("➕ Send format: <code>Button Text - URL</code>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "gcbtn_finish":
        d = admin_data.get(chat_id, {})
        settings_col.update_one({"_id": "global_course_btns"}, {"$set": {"buttons": d.get("buttons", []), "updated_at": get_ist_time()}}, upsert=True)
        invalidate_cache("global_course_btns")
        del admin_data[chat_id]
        bot.edit_message_text("🎉 <b>Global Buttons Saved!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "gcbtn_clear":
        settings_col.delete_one({"_id": "global_course_btns"})
        invalidate_cache("global_course_btns")
        bot.edit_message_text("✅ <b>Global Buttons Cleared!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "admin_success_msg":
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("✏️ Set Up Success Message", callback_data="succ_set_text")).row(InlineKeyboardButton("🗑 Clear Settings", callback_data="succ_clear")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("🎉 <b>Post-Purchase (Success) Message</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "succ_set_text":
        admin_data[chat_id] = {"step": "SUCC_SET_TEXT"}
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Skip Text", callback_data="succ_skip_text"))
        bot.edit_message_text("✏️ Send extra caption/text:", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "succ_skip_text":
        admin_data[chat_id] = {"step": "SUCC_ADD_BTNS", "text": "", "buttons": []}
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="succ_finish"))
        bot.edit_message_text("🔘 Add Buttons: <code>Button Text - URL</code>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "succ_finish":
        d = admin_data.get(chat_id, {})
        settings_col.update_one({"_id": "success_msg_cfg"}, {"$set": {"text": d.get("text", ""), "between": d.get("text", ""), "buttons": d.get("buttons", []), "updated_at": get_ist_time()}}, upsert=True)
        invalidate_cache("success_msg_cfg")
        del admin_data[chat_id]
        bot.edit_message_text("🎉 <b>Settings Saved!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "succ_clear":
        settings_col.delete_one({"_id": "success_msg_cfg"})
        invalidate_cache("success_msg_cfg")
        bot.edit_message_text("✅ <b>Cleared!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "show_intl_info":
        cfg = get_cached_setting("intl_btn_cfg")
        if not cfg:
            if INTERNATIONAL_LINK: bot.send_message(chat_id, f"🌍 <b>International Payment:</b>\n{INTERNATIONAL_LINK}", parse_mode="HTML", msg_type="general", disable_web_page_preview=True)
            else: bot.send_message(chat_id, "ℹ️ No details available.", parse_mode="HTML", msg_type="general")
            return
        markup = InlineKeyboardMarkup()
        for b in cfg.get("buttons", []):
            b_url = b.get("url", "")
            if b_url.lower() == "close": markup.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
            elif b_url.startswith("http"): markup.row(InlineKeyboardButton(b["text"], url=b_url))
            else: markup.row(InlineKeyboardButton(b["text"], callback_data=b_url))
        if not markup.keyboard: markup = None
        photo_id = cfg.get("photo_id")
        text = cfg.get("text", "")
        if photo_id: bot.send_photo(chat_id, photo_id, caption=text, reply_markup=markup, parse_mode="HTML", msg_type="general")
        else: bot.send_message(chat_id, text or "🌍 <b>International Payment Details</b>", reply_markup=markup, parse_mode="HTML", msg_type="general", disable_web_page_preview=True)
    elif data == "admin_custom_intl":
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("✏️ Customize", callback_data="intl_customize")).row(InlineKeyboardButton("🗑 Reset Default", callback_data="intl_reset_default")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("🌍 <b>Customize International Button</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "intl_customize":
        admin_data[chat_id] = {"step": "INTL_SET_NAME"}
        bot.edit_message_text("✏️ Send Name (e.g. <code>🌍 International Pay</code>):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "intl_finish_save":
        d = admin_data.get(chat_id, {})
        doc = {"_id": "intl_btn_cfg", "btn_name": d.get("intl_name", "🌍 International"), "photo_id": d.get("intl_photo"), "text": d.get("intl_text", ""), "buttons": d.get("intl_buttons", []), "updated_at": get_ist_time()}
        settings_col.update_one({"_id": "intl_btn_cfg"}, {"$set": doc}, upsert=True)
        invalidate_cache("intl_btn_cfg")
        del admin_data[chat_id]
        bot.edit_message_text("🎉 <b>Saved successfully!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "intl_reset_default":
        settings_col.delete_one({"_id": "intl_btn_cfg"})
        invalidate_cache("intl_btn_cfg")
        bot.edit_message_text("✅ <b>Reset!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "user_view_plans":
        def _bg_plans():
            plans = get_cached_setting("store_plans")
            c_ids = plans.get("course_ids", []) if plans else [c["course_id"] for c in courses_col.find().limit(10)]
            if not c_ids: return bot.send_message(chat_id, "ℹ️ No plans available.", parse_mode="HTML", msg_type="general")
            bot.send_message(chat_id, "📚 <b>Available Plans:</b>", parse_mode="HTML", msg_type="general", disable_web_page_preview=True)
            for cid in c_ids:
                c = get_cached_course(cid)
                if c: send_course_to_user(chat_id, c)
        threading.Thread(target=_bg_plans, daemon=True).start()
    elif data == "ctype_text":
        admin_data[chat_id]["step"] = "SECRET"
        bot.edit_message_text("✅ <b>Send secret link or text:</b>", chat_id, msg_id, parse_mode="HTML")
    elif data == "ctype_channel":
        admin_data[chat_id]["step"] = "CHANNEL_ID"
        admin_data[chat_id]["channels"] = []
        bot.edit_message_text("📢 <b>Forward a message OR send Private Channel Link:</b>", chat_id, msg_id, parse_mode="HTML")
    elif data == "skip_caption" and chat_id in admin_data:
        admin_data[chat_id]["caption"] = ""
        admin_data[chat_id]["course_buttons"] = []
        admin_data[chat_id]["step"] = "COURSE_BUTTONS"
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Finish Buttons", callback_data="skip_course_buttons"))
        bot.edit_message_text("🔘 Add Extra Buttons: <code>Text - URL</code>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "admin_create_offer":
        admin_data[chat_id] = {"step": "OFFER_DISCOUNT"}
        bot.edit_message_text("🎟 <b>Enter Discount % (e.g. 50):</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "offtarget_all":
        admin_data[chat_id]["target_type"], admin_data[chat_id]["step"] = "all", "OFFER_LIMIT"
        bot.edit_message_text("👥 Max claims? (0 = unlimited):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "offtarget_single":
        admin_data[chat_id]["target_type"], admin_data[chat_id]["step"] = "single", "OFFER_SINGLE_CID"
        bot.edit_message_text("🎯 Send Course ID (e.g. <code>c_abc123</code>):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_delete_course":
        admin_data[chat_id] = {"step": "DELETE_COURSE"}
        bot.edit_message_text("🗑 Send <b>Course ID / Batch ID / File ID</b> to delete:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_edit_course":
        admin_data[chat_id] = {"step": "EDIT_COURSE_ID"}
        bot.edit_message_text("✏️ Send Course ID to edit:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_manage_plans":
        c_ids = (get_cached_setting("store_plans") or {}).get("course_ids", [])
        text = f"📋 <b>Manage Store Plans ({len(c_ids)}):</b>\n" + "".join(f"• <code>{cid}</code>\n" for cid in c_ids)
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Course", callback_data="plan_add_id")).row(InlineKeyboardButton("🗑 Clear All", callback_data="plan_clear_all")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "plan_add_id":
        admin_data[chat_id] = {"step": "ADD_PLAN_ID"}
        bot.edit_message_text("➕ Send Course ID:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "plan_clear_all":
        settings_col.delete_one({"_id": "store_plans"})
        invalidate_cache("store_plans")
        bot.send_message(chat_id, "✅ <b>All plans cleared!</b>", parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "admin_custom_menu":
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("✏️ Set New", callback_data="menu_set_new")).row(InlineKeyboardButton("🗑 Reset Default", callback_data="menu_reset_default")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("🎨 <b>Customize Start Menu</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "menu_set_new":
        admin_data[chat_id] = {"step": "MENU_CUSTOM_CONTENT"}
        bot.edit_message_text("📝 Send Photo, Video or Text:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "menu_finish_save":
        c = admin_data.get(chat_id, {}).get("menu_content", {})
        settings_col.update_one({"_id": "start_menu"}, {"$set": {"media_type": c.get("media_type", "text"), "file_id": c.get("file_id"), "text": c.get("text", ""), "buttons": admin_data.get(chat_id, {}).get("buttons", []), "updated_at": get_ist_time()}}, upsert=True)
        invalidate_cache("start_menu")
        del admin_data[chat_id]
        bot.edit_message_text("🎉 <b>Saved!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "menu_reset_default":
        settings_col.delete_one({"_id": "start_menu"})
        invalidate_cache("start_menu")
        bot.edit_message_text("✅ <b>Reset!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "admin_create_batch":
        admin_data[chat_id] = {"mode": "batch", "step": "TITLE", "course_ids": []}
        bot.edit_message_text("📦 <b>Send Batch Title:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_batch_existing":
        admin_data[chat_id] = {"mode": "batch_existing", "step": "BATCH_EXISTING_TITLE", "course_ids": []}
        bot.edit_message_text("📚 <b>Send Batch Title:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_add_course":
        admin_data[chat_id] = {"mode": "single", "step": "TITLE", "promo": [], "amount": None, "caption": "", "course_buttons": []}
        bot.edit_message_text("📝 <b>Step 1: Send Course Title</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data in ["admin_file_link", "admin_broadcast"]:
        admin_data[chat_id] = {"step": "FTL_MEDIA" if data == "admin_file_link" else "BC_MEDIA", "media": []}
        bot.edit_message_text(f"{'📎 File to Link' if data == 'admin_file_link' else '📢 Broadcast'}\nSend Media/Text.", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "next_price" and chat_id in admin_data:
        admin_data[chat_id]["step"] = "AMOUNT"
        bot.edit_message_text("💰 <b>Price (INR)</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "batch_add_next":
        admin_data[chat_id]["step"], admin_data[chat_id]["promo"], admin_data[chat_id]["caption"], admin_data[chat_id]["course_buttons"] = "TITLE", [], "", []
        bot.edit_message_text("📝 <b>Send title for next pack:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "batch_finish":
        d = admin_data.get(chat_id)
        if d and d.get("course_ids"):
            bid = "b_" + str(uuid.uuid4())[:6]
            batches_col.update_one({"batch_id": bid}, {"$set": {"batch_id": bid, "title": d["title"], "course_ids": d["course_ids"]}}, upsert=True)
            bot.edit_message_text(f"🎉 <b>Batch Created!</b>\n👉 <code>https://t.me/{BOT_USERNAME}?start={bid}</code>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
            del admin_data[chat_id]
            send_admin_panel(chat_id)
            
    elif data == "batch_add_channel":
        admin_data[chat_id]["step"] = "CHANNEL_ID"
        bot.edit_message_text("📢 <b>Forward next message OR send next Private Channel Link:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        
    elif data == "batch_finish_channel":
        channels = admin_data.get(chat_id, {}).get("channels", [])
        if not channels: return bot.edit_message_text("❌ No channels added.", chat_id=chat_id, message_id=msg_id)

        cid = "c_" + str(uuid.uuid4())[:6]
        secret_text = "👉 <b>Click below to join the Group(s)/Channel(s):</b>\n\n"
        channel_ids = []
        channel_names = []
        
        for ch in channels:
            secret_text += f"🔹 {ch['name']}:\n{ch['link']}\n\n"
            channel_ids.append(ch['id'])
            channel_names.append(ch['name'])

        combined_name = " + ".join(channel_names)

        courses_col.update_one({"course_id": cid}, {"$set": {
            "course_id": cid, "title": admin_data[chat_id].get("title", combined_name),
            "promo_media": admin_data[chat_id]["promo"], "amount": admin_data[chat_id]["amount"],
            "custom_caption": admin_data[chat_id].get("caption", ""), "secret_text": secret_text,
            "channel_ids": channel_ids, "channel_name": combined_name,
            "extra_buttons": admin_data[chat_id].get("course_buttons", [])
        }}, upsert=True)

        if admin_data[chat_id].get("mode") == "single":
            bot.edit_message_text(f"🎉 <b>Channel Pack created! ({combined_name})</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={cid}</code>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
            del admin_data[chat_id]
            send_admin_panel(chat_id)
        elif admin_data[chat_id].get("mode") == "batch":
            admin_data[chat_id]["course_ids"].append(cid)
            admin_data[chat_id]["step"] = "NEXT_ACTION"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Another", callback_data="batch_add_next")).row(InlineKeyboardButton("✅ Finish Batch", callback_data="batch_finish"))
            bot.edit_message_text("✅ <b>Pack saved to batch!</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
            
    elif data == "admin_user_info":
        recs = list(purchases_col.find().sort("_id", -1).limit(15))
        txt = "👥 <b>Recent Purchases:</b>\n\n" + "".join(f"👤 {r.get('username')} | 📅 {r.get('date', '')[:10]} | 📚 <code>{r.get('item_info')}</code>\n" for r in recs) if recs else "No purchases yet."
        bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin")), parse_mode="HTML")
    elif data == "back_to_admin":
        try: bot.delete_message(chat_id, msg_id)
        except Exception: pass
        send_admin_panel(chat_id)
    elif data.startswith("mainmenu_"):
        t = data.replace("mainmenu_", "")
        if t.startswith("c_"):
            def _bg_c():
                c = get_cached_course(t)
                if c: send_course_to_user(chat_id, c)
            threading.Thread(target=_bg_c, daemon=True).start()
        elif t.startswith("b_"):
            def _bg_b():
                b = batches_col.find_one({"batch_id": t})
                if b: send_batch_to_user(chat_id, b)
            threading.Thread(target=_bg_b, daemon=True).start()
    elif data == "bc_done":
        admin_data[chat_id]["step"], admin_data[chat_id]["buttons"] = "BC_BUTTONS", []
        bot.send_message(chat_id,"✅ <b>Media Saved!</b> Add button or Finish.", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data="bc_finish")), parse_mode="HTML")
    elif data == "bc_finish":
        admin_data[chat_id]["step"] = "BC_TIME"
        bot.send_message(chat_id,"⏳ <b>How many hours should this broadcast stay? (0 for permanent):</b>", parse_mode="HTML")
    elif data == "ftl_done":
        admin_data[chat_id]["step"], admin_data[chat_id]["buttons"] = "FTL_BUTTONS", []
        bot.send_message(chat_id,"✅ <b>Media Saved!</b> Add button or Finish.", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data="ftl_finish")), parse_mode="HTML")
    elif data == "ftl_finish":
        fid = "f_" + str(uuid.uuid4())[:6]
        file_links_col.update_one({"file_code": fid}, {"$set": {"file_code": fid, "media_data": admin_data[chat_id].get("media", []), "button_data": admin_data[chat_id].get("buttons", [])}}, upsert=True)
        bot.send_message(chat_id,f"🎉 <b>Link Created!</b>\n👉 <code>https://t.me/{BOT_USERNAME}?start={fid}</code>", parse_mode="HTML")
        del admin_data[chat_id]
        send_admin_panel(chat_id)

# ==========================================
# FLASK WEB SERVER & API
# ==========================================
app = Flask(__name__)

def _session_secret():
    """Login cookie का secret। पहली बार बनता है और boot file में सेव रहता है।"""
    data = {}
    try:
        with open(BOOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    if not data.get("SESSION_SECRET"):
        data["SESSION_SECRET"] = secrets.token_urlsafe(32)
        try:
            with open(BOOT_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.chmod(BOOT_FILE, 0o600)
        except Exception:
            pass
    return data["SESSION_SECRET"]

app.secret_key = _session_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7))
AMOUNT_RE_DECIMAL = re.compile(r"(?:Rs\.?|₹|INR)\s*([\d,]+\.\d{1,2})", re.IGNORECASE)
AMOUNT_RE_INT = re.compile(r"(?:Rs\.?|₹|INR)\s*([\d,]+)(?!\.\d)", re.IGNORECASE)
BOT_USERNAME = "your_bot" 

@app.route("/")
def home(): return "Telegram Bot API Running."

_WEBHOOK_TEXT_KEYS = ("text", "message", "body", "sms", "msg")

def _webhook_fields():
    """SMS text और source को JSON, form या query string से निकालता है.
    अलग-अलग forwarder apps अलग नाम भेजते हैं (text/message/body...), इसलिए सब मान लिए जाते हैं."""
    data = {}
    if request.method == "POST":
        j = request.get_json(silent=True)
        if isinstance(j, dict): data.update(j)
        if request.form: data.update({k: v for k, v in request.form.items()})
    data_args = {k: v for k, v in request.args.items()}
    for k, v in data_args.items(): data.setdefault(k, v)
    text = ""
    for key in _WEBHOOK_TEXT_KEYS:
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    src = data.get("source")
    src_name = str(src).strip()[:60] if isinstance(src, (str, int, float)) else ""
    return text, src_name

@app.route("/sms-webhook/<secret>", methods=["GET", "POST"])
def sms_webhook(secret):
    if secret != SMS_HOOK_SECRET: return "forbidden", 403
    sms_text, src_name = _webhook_fields()
    if not sms_text: return "no 'text' param", 400
    
    m = AMOUNT_RE_DECIMAL.search(sms_text)
    has_dec = bool(m)
    if not m: m = AMOUNT_RE_INT.search(sms_text)
    if not m: return "no amount", 200

    amt_str = m.group(1).replace(",", "")
    f_round = f"{float(amt_str):.2f}" if not has_dec else amt_str
    sms_pool_col.insert_one({"amount": f_round, "raw_text": sms_text, "status": "UNUSED", "created_at_dt": datetime.now(timezone.utc), "created_at_str": get_ist_time(), "source": src_name})

    order, amb = None, False
    with pending_lock:
        cands = [amt_str] if has_dec and amt_str in pending_orders else [f_round] if not has_dec and f_round in pending_orders else [k for k in pending_orders if not has_dec and k.startswith(amt_str + ".")]
        if len(cands) == 1: order = pending_orders.pop(cands[0])
        else: amb = len(cands) > 1

    if not order:
        stale_cutoff = time.time() - get_order_hold_seconds()
        order = orders_col.find_one({"amount": f_round, "status": {"$in": ["PENDING", "EXPIRED"]}, "created_at": {"$gte": stale_cutoff}})
    
    if order:
        updated = sms_pool_col.update_one({"amount": f_round, "status": "UNUSED"}, {"$set": {"status": "PROCESSED"}})
        if updated.modified_count > 0:
            deliver_course_to_buyer(order, sms_text=sms_text, is_manual=False)
        return "matched", 200
    if amb:
        try: orig_send_message(DB_CHANNEL_ID, f"⚠️ <b>Ambiguous</b> ₹{amt_str}\n📩 <code>{sms_text[:300]}</code>", parse_mode="HTML")
        except Exception: pass
        return "ambiguous", 200
    return "saved_to_pool", 200

def _app_status_public():
    """Android ऐप का आखिरी heartbeat, dashboard के status icon के लिए"""
    try:
        doc = settings_col.find_one({"_id": "app_status"}) or {}
    except Exception:
        doc = {}
    last = doc.get("last_seen") or 0
    return {
        "online": bool(last) and (time.time() - last) < 150,
        "connected_ever": bool(last),
        "last_seen": doc.get("last_seen_str", ""),
        "device": doc.get("device", ""),
        "pending": doc.get("pending", 0),
    }

_GH_CACHE = {"t": 0.0, "data": None}
_APK_CACHE = {"tag": "", "data": None}

def _ver_tuple(s):
    nums = re.findall(r"\d+", str(s or ""))
    return tuple(int(x) for x in nums[:4]) or (0,)

def _github_latest_release(force=False):
    """GitHub से latest release लाता है। 10 मिनट तक cache रहता है, ताकि GitHub पर बार-बार call न जाए।"""
    repo = str(CFG.get("APP_GITHUB_REPO", "") or "").strip()
    if not repo:
        return None
    now = time.time()
    age = now - _GH_CACHE["t"]
    if _GH_CACHE["data"] is not None and (age < 600 or (force and age < 60)):
        return _GH_CACHE["data"]
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "store-bot"}
    tok = str(CFG.get("APP_GITHUB_TOKEN", "") or "").strip()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    try:
        req = urllib.request.Request("https://api.github.com/repos/" + repo + "/releases/latest", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"⚠️ GitHub release check failed: {e}", flush=True)
        _GH_CACHE["t"] = now
        return _GH_CACHE["data"]
    apk = next((a for a in rel.get("assets", []) if str(a.get("name", "")).lower().endswith(".apk")), None)
    data = {"tag": rel.get("tag_name", ""), "notes": (rel.get("body") or "")[:500],
            "published": rel.get("published_at", ""), "apk": apk}
    _GH_CACHE["t"] = now
    _GH_CACHE["data"] = data
    return data

def _download_apk(rel):
    """APK लाता है। Private repo के लिए token यहीं सर्वर पर रहता है, फ़ोन को कभी नहीं जाता।"""
    if _APK_CACHE["tag"] == rel["tag"] and _APK_CACHE["data"]:
        return _APK_CACHE["data"]
    asset = rel["apk"]
    tok = str(CFG.get("APP_GITHUB_TOKEN", "") or "").strip()
    if tok:
        req = urllib.request.Request(asset["url"], headers={"Accept": "application/octet-stream",
                                     "Authorization": "Bearer " + tok, "User-Agent": "store-bot"})
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if e.code not in (301, 302, 303, 307, 308) or not loc:
                raise
            with urllib.request.urlopen(loc, timeout=120) as r2:
                data = r2.read()
    else:
        req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "store-bot"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    _APK_CACHE["tag"] = rel["tag"]
    _APK_CACHE["data"] = data
    return data

@app.route("/sms-webhook/<secret>/apk")
def sms_app_apk(secret):
    """Android ऐप यहीं से नया APK डाउनलोड करता है"""
    if secret != SMS_HOOK_SECRET: return "forbidden", 403
    rel = _github_latest_release()
    if not rel or not rel.get("apk"): return "no apk", 404
    data = _download_apk(rel)
    return Response(data, mimetype="application/vnd.android.package-archive",
                    headers={"Content-Disposition": "attachment; filename=app-update.apk"})

@app.route("/sms-webhook/<secret>/heartbeat", methods=["GET", "POST"])
def sms_app_heartbeat(secret):
    """Android ऐप हर मिनट यहाँ 'मैं चालू हूँ' भेजता है, और Save के समय कनेक्शन टेस्ट के लिए"""
    if secret != SMS_HOOK_SECRET: return "forbidden", 403
    d = request.get_json(silent=True) or {}
    try: pending = int(d.get("pending") or 0)
    except (TypeError, ValueError): pending = 0
    app_ver = str(d.get("app_version") or "")[:20]
    settings_col.update_one({"_id": "app_status"}, {"$set": {
        "last_seen": time.time(),
        "last_seen_str": get_ist_time(),
        "device": str(d.get("device") or "Android")[:80],
        "pending": pending,
        "app_version": app_ver,
    }}, upsert=True)
    reply = {"ok": True, "server_time": get_ist_time()}
    rel = _github_latest_release()
    if rel and rel.get("apk") and _ver_tuple(rel["tag"]) > _ver_tuple(app_ver):
        reply["update"] = {
            "available": True,
            "version": rel["tag"],
            "apk_url": request.host_url.rstrip("/") + "/sms-webhook/" + secret + "/apk",
            "notes": rel.get("notes", "")[:300],
        }
    return jsonify(reply)

def _is_logged_in():
    return bool(DASHBOARD_PASSWORD and session.get("dash_ok"))

_login_fail = {}  # IP -> गलत कोशिशों का समय (memory में, 15 मिनट की सीमा)

def _login_blocked(ip):
    now = time.time()
    arr = [t for t in _login_fail.get(ip, []) if now - t < 900]
    _login_fail[ip] = arr
    return len(arr) >= 5

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _is_logged_in():
            return f(*args, **kwargs)
        # पहली setup (dashboard password अभी नहीं बना): सिर्फ़ config खुला रहता है, उसमें setup code लगता है
        if not DASHBOARD_PASSWORD and request.path == "/dashboard/api/config":
            return f(*args, **kwargs)
        return jsonify({"error": "Login required", "login_required": True}), 401
    return wrapper

@app.route("/dashboard/api/app-update")
@require_auth
def api_app_update():
    """Dashboard की 'App update' page: GitHub पर नया version है या नहीं"""
    repo = str(CFG.get("APP_GITHUB_REPO", "") or "").strip()
    if not repo:
        return jsonify({"configured": False})
    check = request.args.get("check") == "1"
    rel = _github_latest_release(force=check)
    try:
        installed = (settings_col.find_one({"_id": "app_status"}) or {}).get("app_version", "")
    except Exception:
        installed = ""
    available = bool(rel and rel.get("apk") and _ver_tuple(rel["tag"]) > _ver_tuple(installed))
    return jsonify({
        "configured": True,
        "repo": repo,
        "installed": installed or "",
        "latest": (rel or {}).get("tag", ""),
        "has_apk": bool(rel and rel.get("apk")),
        "available": available,
        "notes": (rel or {}).get("notes", ""),
        "published": (rel or {}).get("published", ""),
        "error": "" if rel else "GitHub से जानकारी नहीं मिली। Repo नाम, token या internet जाँचें।",
    })

@app.route("/dashboard/api/app-status")
@require_auth
def api_app_status():
    return jsonify(_app_status_public())

def _strip_html(s): return re.sub(r"<[^>]*>", "", s or "").strip()
def _order_ts(o):
    if o.get("delivered_at_ts"): return o["delivered_at_ts"]
    ds = o.get("delivered_at")
    if ds:
        try: return datetime.strptime(ds, "%d-%m-%Y %I:%M:%S %p").replace(tzinfo=IST).timestamp()
        except Exception: pass
    return o.get("created_at", 0)

@app.route("/dashboard/api/overview")
@require_auth
def api_overview():
    completed_q = {"status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}}
    today_start = (datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)).timestamp()
    # पहले हर order लोड होता था (धीमा); अब database खुद जोड़ता है (तेज़)
    agg = list(orders_col.aggregate([
        {"$match": {**completed_q, "delivered_at_ts": {"$gte": today_start}}},
        {"$group": {"_id": None, "total": {"$sum": {"$toDouble": "$amount"}}}},
    ]))
    today_amt = agg[0]["total"] if agg else 0
    
    return jsonify({
        "total_users": users_col.count_documents({}), "total_qr_generated": orders_col.count_documents({}),
        "active_pending": orders_col.count_documents({"status": "PENDING"}), "completed_total": orders_col.count_documents(completed_q),
        "expired_total": orders_col.count_documents({"status": "EXPIRED"}), "pending_sms": sms_pool_col.count_documents({"status": "UNUSED"}),
        "today_amount": round(today_amt, 2)
    })

@app.route("/dashboard/api/orders")
@require_auth
def api_orders():
    st = request.args.get("status", "all")
    q = {"status": "PENDING"} if st == "pending" else {"status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}} if st == "completed" else {"status": "EXPIRED"} if st == "expired" else {}
    now = time.time()
    out = []
    for o in orders_col.find(q).sort("created_at", -1).limit(300):
        remaining = max(0, int(QR_EXPIRY_SECONDS - (now - o.get("created_at", now)))) if o.get("status") == "PENDING" else None
        out.append({
            "order_id": o.get("order_id"), "user": _strip_html(o.get("user_mention", "")), "course_id": o.get("course_id"), 
            "amount": o.get("amount"), "status": o.get("status"), "created_at": o.get("created_at_str"),
            "remaining_seconds": remaining, "method": {"COMPLETED_AUTO": "Auto SMS", "COMPLETED_MANUAL": "Manual"}.get(o.get("status")),
            "has_screenshot": bool(o.get("manual_msg_id"))
        })
    return jsonify(out)

@app.route("/dashboard/api/orders/clear-open", methods=["POST"])
@require_auth
def api_clear_open_orders():
    """सारे PENDING और EXPIRED orders CANCELLED करता है, ताकि उनके amounts फिर खाली हो जाएँ. कुछ delete नहीं होता, COMPLETED orders को नहीं छुआ जाता."""
    to_clear = list(orders_col.find({"status": {"$in": ["PENDING", "EXPIRED"]}}))
    if not to_clear: return jsonify({"status": "success", "cleared": 0})
    ids = [o["order_id"] for o in to_clear if o.get("order_id")]
    res = orders_col.update_many({"order_id": {"$in": ids}, "status": {"$in": ["PENDING", "EXPIRED"]}},
                                 {"$set": {"status": "CANCELLED", "cleared_at": get_ist_time()}})
    with pending_lock:
        for k, v in list(pending_orders.items()):
            if v.get("order_id") in ids: pending_orders.pop(k, None)
    for oid in ids:
        o = all_orders_cache.get(oid)
        if isinstance(o, dict): o["status"] = "CANCELLED"
    def _update_channel():
        for o in to_clear:
            try: update_channel_order_status(o, "CANCELLED")
            except Exception: pass
    threading.Thread(target=_update_channel, daemon=True).start()
    return jsonify({"status": "success", "cleared": res.modified_count})

@app.route("/dashboard/api/orders/<order_id>/approve", methods=["POST"])
@require_auth
def api_approve_order(order_id):
    order = orders_col.find_one({"order_id": order_id})
    if order and order.get("status") in ["PENDING", "EXPIRED"]:
        deliver_course_to_buyer(order, sms_text="Dashboard Approved", is_manual=True)
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

@app.route("/dashboard/api/orders/<order_id>/cancel", methods=["POST"])
@require_auth
def api_cancel_order(order_id):
    order = orders_col.find_one({"order_id": order_id})
    if order and order.get("status") in ["PENDING", "EXPIRED"]:
        orders_col.update_one({"order_id": order_id}, {"$set": {"status": "CANCELLED"}})
        with pending_lock: pending_orders.pop(order.get("amount"), None)
        update_channel_order_status(order, "CANCELLED")
        uid = order.get("user_id") or order.get("chat_id")
        qmid = order.get("qr_msg_id")
        if uid and qmid:
            try: bot.delete_message(uid, qmid)
            except Exception: pass
            try: bot.send_message(uid, "❌ <b>Your order has been cancelled by Admin.</b>", parse_mode="HTML", msg_type="general")
            except Exception: pass
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

@app.route("/dashboard/api/inject-sms", methods=["POST"])
@require_auth
def api_inject_sms():
    d = request.json or {}
    amount = str(d.get("amount", "")).strip()
    if not amount: return jsonify({"error": "Amount is required"}), 400
    if not re.fullmatch(r"\d+(\.\d{1,2})?", amount): return jsonify({"error": "Amount must be a number, e.g. 50 or 50.01"}), 400
    sms_text = f"You've received Rs.{amount} from Dashboard Test via PhonePe for txn T{int(time.time()*1000)}"
    
    with app.test_client() as client:
        resp = client.get(f"/sms-webhook/{SMS_HOOK_SECRET}?text={sms_text}")
        return jsonify({"status": "success", "result": resp.get_data(as_text=True)})

@app.route("/dashboard/api/courses", methods=["GET"])
@require_auth
def api_courses():
    out = []
    for c in courses_col.find().sort("_id", -1):
        out.append({"course_id": c.get("course_id"), "title": c.get("title", ""), "amount": c.get("amount"), "caption": c.get("custom_caption", "")[:40], "is_channel": bool(c.get("channel_id") or c.get("channel_ids")), "channel_name": c.get("channel_name", "")})
    return jsonify(out)

@app.route("/dashboard/api/courses/<course_id>", methods=["DELETE"])
@require_auth
def api_delete_course(course_id):
    courses_col.delete_one({"course_id": course_id})
    settings_col.update_one({"_id": "store_plans"}, {"$pull": {"course_ids": course_id}})
    invalidate_cache("store_plans")
    invalidate_cache(f"course_{course_id}")
    return jsonify({"status": "success"})

@app.route("/dashboard/api/courses/<course_id>/buyers")
@require_auth
def api_course_buyers(course_id):
    course = get_cached_course(course_id)
    is_channel = bool(course and (course.get("channel_id") or course.get("channel_ids")))
    completed_q = {"course_id": course_id, "status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}}
    out = []
    for o in orders_col.find(completed_q).sort("delivered_at_ts", -1):
        uid = o.get("user_id")
        joined = None
        if is_channel:
            joined = channel_logs_col.count_documents({"user_id": uid, "course_id": course_id, "status": "APPROVED"}) > 0
        out.append({
            "user_id": uid, "user": _strip_html(o.get("user_mention", f"User ({uid})")),
            "amount": o.get("amount"), "purchased_at": o.get("delivered_at") or o.get("created_at_str"),
            "method": {"COMPLETED_AUTO": "Auto SMS", "COMPLETED_MANUAL": "Manual"}.get(o.get("status")),
            "channel_joined": joined
        })
    return jsonify({"course_id": course_id, "is_channel": is_channel, "channel_name": (course or {}).get("channel_name"), "buyers": out})

@app.route("/dashboard/api/offers", methods=["GET", "POST"])
@require_auth
def api_offers():
    if request.method == "POST":
        d = request.json
        off_code = "off_" + str(uuid.uuid4())[:6]
        now_ts = time.time()
        hrs = float(d.get("hours", 24))
        doc = {
            "offer_code": off_code, "discount_percent": int(d.get("discount", 10)),
            "target_type": d.get("target_type", "all"), "target_course_id": d.get("course_id", ""),
            "max_users": int(d.get("max_users", -1)), "per_user_limit": int(d.get("per_user_limit", 1)), "used_count": 0,
            "created_at_ts": now_ts, "expires_at_ts": now_ts + (hrs * 3600),
            "expires_str": (datetime.now(IST) + timedelta(hours=hrs)).strftime("%d-%m-%Y %I:%M %p")
        }
        offers_col.insert_one(doc)
        return jsonify({"status": "success"})
    
    out = []
    now = time.time()
    for o in offers_col.find().sort("created_at_ts", -1):
        status = "Active" if o["expires_at_ts"] > now and (o["max_users"] == -1 or o["used_count"] < o["max_users"]) else "Expired"
        link = f"https://t.me/{BOT_USERNAME}?start={o['offer_code']}"
        out.append({"offer_code": o["offer_code"], "discount": o["discount_percent"], "target": o["target_type"], "course": o["target_course_id"], "used": o["used_count"], "max": o["max_users"], "per_user": o.get("per_user_limit", 1), "expires": o["expires_str"], "status": status, "link": link})
    return jsonify(out)

@app.route("/dashboard/api/offers/<offer_code>", methods=["DELETE"])
@require_auth
def api_delete_offer(offer_code):
    offers_col.delete_one({"offer_code": offer_code})
    return jsonify({"status": "success"})

@app.route("/dashboard/api/system-settings", methods=["GET", "POST"])
@require_auth
def api_system_settings():
    if request.method == "POST":
        data = request.json
        old_cfg = get_cached_setting("system_settings") or {}
        was_maintenance = old_cfg.get("maintenance", False)
        is_maintenance_now = data.get("maintenance", False)
        
        doc = {
            "maintenance": is_maintenance_now,
            "chat_cleanup_seconds": int(data.get("cleanup_seconds", 86400)),
            "auto_del_promos": data.get("auto_del_promos", True),
            "auto_del_broadcasts": data.get("auto_del_broadcasts", True),
            "auto_del_purchases": data.get("auto_del_purchases", False),
            "protect_content": bool(data.get("protect_content", False)),
            "course_timer_minutes": int(data.get("course_timer_minutes", 0)),
            "order_hold_hours": max(1, int(float(data.get("order_hold_hours", 12) or 12))),
            "first_exact_amount": bool(data.get("first_exact_amount", old_cfg.get("first_exact_amount", False))),
            "updated_at": get_ist_time()
        }
        settings_col.update_one({"_id": "system_settings"}, {"$set": doc}, upsert=True)
        invalidate_cache("system_settings")

        if was_maintenance and not is_maintenance_now:
            def broadcast_back_online():
                msg = "✅ <b>Bot is Back Online! / बोट चालू हो गया है!</b>\n\nMaintenance is complete. You can now continue using the bot smoothly."
                for u in users_col.find():
                    uid = u.get("user_id") or u.get("chat_id")
                    if uid and int(uid) != ADMIN_ID and not is_banned(int(uid)):
                        try:
                            orig_send_message(int(uid), msg, parse_mode="HTML")
                            time.sleep(0.04)
                        except: pass
            threading.Thread(target=broadcast_back_online, daemon=True).start()

        return jsonify({"status": "success"})
    
    cfg = get_cached_setting("system_settings") or {}
    return jsonify({
        "maintenance": cfg.get("maintenance", False),
        "cleanup_seconds": int(cfg.get("cleanup_seconds", 86400)),
        "order_hold_hours": int(cfg.get("order_hold_hours", 12)),
        "auto_del_promos": cfg.get("auto_del_promos", True),
        "auto_del_broadcasts": cfg.get("auto_del_broadcasts", True),
        "auto_del_purchases": cfg.get("auto_del_purchases", False),
        "protect_content": cfg.get("protect_content", False),
        "course_timer_minutes": int(cfg.get("course_timer_minutes", 0)),
        "first_exact_amount": bool(cfg.get("first_exact_amount", False)),
    })

@app.route("/dashboard/api/force-clear-chats", methods=["POST"])
@require_auth
def api_force_clear_chats():
    data = request.json or {}
    del_promos = data.get("del_promos", True)
    del_broadcasts = data.get("del_broadcasts", True)
    del_purchases = data.get("del_purchases", False)

    def manual_clear_all():
        with tracker_lock: chats = list(user_chat_messages.keys())
        for cid in chats:
            clear_inactive_chat(cid, is_force=True, force_del_promos=del_promos, force_del_broadcasts=del_broadcasts, force_del_purchases=del_purchases)
            time.sleep(0.05)
            
    threading.Thread(target=manual_clear_all, daemon=True).start()
    return jsonify({"status": "success", "message": "Chat clearing started safely in background."})

@app.route("/dashboard/api/user-lookup")
@require_auth
def api_user_lookup():
    """ऊपर की सर्च बार: ID या username से यूज़र ढूंढो + उसके कोर्स और चैनल के नाम"""
    q = request.args.get("q", "").strip()
    if not q: return jsonify([])
    if q.isdigit():
        uid = int(q)
        u = users_col.find_one({"user_id": uid}, {"user_id": 1, "username": 1}) or {}
        users = [{"user_id": uid, "username": u.get("username", "")}]
    else:
        found = users_col.find({"username": {"$regex": re.escape(q.lstrip("@")), "$options": "i"}}, {"user_id": 1, "username": 1}).limit(10)
        users = [{"user_id": x.get("user_id"), "username": x.get("username", "")} for x in found]
    users = [u for u in users if u["user_id"] is not None]
    uids = [u["user_id"] for u in users]
    purchases = list(purchases_col.find({"user_id": {"$in": uids}}).sort("_id", 1)) if uids else []
    rows = _purchase_rows(purchases)
    by_uid = {}
    for p, row in zip(purchases, rows):
        by_uid.setdefault(p.get("user_id"), []).append(row)
    out = []
    for u in users:
        prs = by_uid.get(u["user_id"], [])
        out.append({
            "user_id": u["user_id"],
            "username": u["username"],
            "purchases": prs,
            "total": round(sum(r.get("rate") or 0 for r in prs), 2),
            "channels": sorted({r["channel_name"] for r in prs if r.get("channel_name")}),
        })
    return jsonify(out)

# ==========================================
# 📊 SERVER LOAD (Linux /proc से, कोई extra package नहीं)
# ==========================================
LOAD_STATE = {"supported": False, "cpu": None, "mem": None, "mem_used_mb": None, "mem_total_mb": None,
              "bot_cpu": None, "bot_mem_mb": None, "load1": None, "ncpu": os.cpu_count() or 1, "updated": ""}

def _read_cpu_total():
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:9]]
    return sum(vals), vals[3] + vals[4]  # total, idle (+iowait)

def _read_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1); info[k] = int(v.split()[0])  # kB
    total = info["MemTotal"]; avail = info.get("MemAvailable", info.get("MemFree", 0))
    return round((1 - avail / total) * 100, 1), round((total - avail) / 1024), round(total / 1024)

def _read_bot_proc():
    with open("/proc/self/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    ticks = int(fields[11]) + int(fields[12])  # utime + stime
    rss_mb = None
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"): rss_mb = round(int(line.split()[1]) / 1024, 1)
    return ticks, rss_mb

def load_sampler():
    try: clk = os.sysconf("SC_CLK_TCK")
    except Exception: clk = 100
    prev = None
    while True:
        try:
            tot, idle = _read_cpu_total()
            ticks, rss = _read_bot_proc()
            now = time.time()
            mem_pct, used_mb, total_mb = _read_mem()
            cpu = bot_cpu = None
            if prev and tot > prev["tot"]:
                cpu = round((1 - (idle - prev["idle"]) / (tot - prev["tot"])) * 100, 1)
                bot_cpu = round((ticks - prev["ticks"]) / clk / max(now - prev["t"], 0.001) / LOAD_STATE["ncpu"] * 100, 1)
            prev = {"tot": tot, "idle": idle, "ticks": ticks, "t": now}
            LOAD_STATE.update(supported=True, cpu=cpu, mem=mem_pct, mem_used_mb=used_mb, mem_total_mb=total_mb,
                              bot_cpu=bot_cpu, bot_mem_mb=rss, load1=round(os.getloadavg()[0], 2),
                              updated=get_ist_time())
        except Exception:
            LOAD_STATE["supported"] = False  # Linux नहीं है तो बस बंद
            return
        time.sleep(4)

def load_level(cpu):
    if cpu is None: return "unknown", "Measuring… please wait a few seconds"
    if cpu < 30: return "free", "Free: the bot is free and runs smoothly"
    if cpu < 60: return "light", "Light traffic: all good"
    if cpu < 85: return "busy", "Busy: traffic is high right now"
    return "heavy", "Heavy load: replies may be slow for a moment"

def _ist_text(ts):
    if not ts: return ""
    return datetime.fromtimestamp(float(ts), BAN_TZ).strftime("%d %b %Y, %I:%M %p")

@app.route("/dashboard/api/bans", methods=["GET", "POST"])
@require_auth
def api_bans():
    if request.method == "POST":
        d = request.json or {}
        raw_h = d.get("hours")
        try: hours = None if raw_h in (None, "", "perm") else float(raw_h)
        except (TypeError, ValueError): return jsonify({"error": "Invalid duration"}), 400
        if hours is not None and hours <= 0: return jsonify({"error": "Duration must be more than 0"}), 400
        try: set_ban(str(d.get("target", "")), hours, str(d.get("reason", "")))
        except ValueError as e: return jsonify({"error": str(e)}), 400
        return jsonify({"status": "success"})
    now = time.time()
    out = []
    for b in banned_col.find({}).sort("banned_at_ts", -1):
        if not b.get("permanent") and b.get("until") and b["until"] <= now:
            banned_col.delete_one({"_id": b["_id"]})
            continue
        out.append({
            "user_id": b.get("user_id"), "username": b.get("username", ""),
            "permanent": bool(b.get("permanent")), "until_text": _ist_text(b.get("until")),
            "banned_at": b.get("banned_at", ""), "reason": b.get("reason", ""),
        })
    BAN_CACHE["ts"] = 0.0
    return jsonify(out)

@app.route("/dashboard/api/bans/remove", methods=["POST"])
@require_auth
def api_unban():
    try: n = unban_user(str((request.json or {}).get("target", "")))
    except ValueError as e: return jsonify({"error": str(e)}), 400
    return jsonify({"status": "success", "removed": n})

@app.route("/dashboard/api/courses/<course_id>/price", methods=["POST"])
@require_auth
def api_course_price(course_id):
    raw = str((request.json or {}).get("amount", "")).strip()
    try:
        amt = float(raw)
        if amt <= 0: raise ValueError
    except ValueError:
        return jsonify({"error": "Price 0 से ज़्यादा डालें"}), 400
    amt_s = str(int(amt)) if amt.is_integer() else f"{amt:.2f}"
    res = courses_col.update_one({"course_id": course_id}, {"$set": {"amount": amt_s}})
    if res.matched_count == 0: return jsonify({"error": "Course नहीं मिला"}), 404
    invalidate_cache(f"course_{course_id}")
    return jsonify({"status": "success", "amount": amt_s})

@app.route("/dashboard/api/success-msg", methods=["GET", "POST"])
@require_auth
def api_success_msg():
    if request.method == "POST":
        d = request.json or {}
        if d.get("reset"):
            settings_col.delete_one({"_id": "success_msg_cfg"})
            invalidate_cache("success_msg_cfg")
            return jsonify({"status": "success"})
        buttons = []
        for b in (d.get("buttons") or [])[:8]:
            t = str(b.get("text", "")).strip()[:64]
            u = str(b.get("url", "")).strip()
            if not t or not (u.lower() == "close" or u.startswith("http")):
                return jsonify({"error": f"Button '{t or '?'}': link https:// से शुरू होना चाहिए (या close)"}), 400
            buttons.append({"text": t, "url": u, "style": str(b.get("style", "")).lower()})
        def _clean_btn(x, default_text):
            x = x or {}
            return {"text": str(x.get("text", "")).strip()[:64] or default_text,
                    "style": str(x.get("style", "")).lower(), "enabled": bool(x.get("enabled", True))}
        between = str(d.get("between", ""))[:2000]
        cfg = {
            "header": str(d.get("header", ""))[:1000],
            "between": between,
            "body": str(d.get("body", ""))[:1000],
            "main_btn": _clean_btn(d.get("main_btn"), "📢 Send Request"),
            "buttons": buttons,
            "received_text": str(d.get("received_text", ""))[:1000],
            "received_btn": _clean_btn(d.get("received_btn"), "📺 Open Channel"),
            "text": between,
            "updated_at": get_ist_time(),
        }
        settings_col.update_one({"_id": "success_msg_cfg"}, {"$set": cfg}, upsert=True)
        invalidate_cache("success_msg_cfg")
        return jsonify({"status": "success"})
    return jsonify(get_success_cfg())

def _parse_menu_lines(txt):
    out = []
    for line in str(txt or "").splitlines():
        if not line.strip(): continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0]: raise ValueError(f"गलत लाइन: {line.strip()[:60]}")
        text, act = parts[0][:64], parts[1].lower()
        val = parts[2] if len(parts) > 2 else ""
        if act in ("orders", "my orders", "my_orders"):
            out.append({"text": text, "action": "my_orders", "value": ""})
        elif act == "link":
            if not val.startswith("http"): raise ValueError(f"'{text}': link https:// से शुरू होना चाहिए")
            out.append({"text": text, "action": "link", "value": val})
        elif act == "message":
            if not val: raise ValueError(f"'{text}': message खाली नहीं हो सकता")
            out.append({"text": text, "action": "message", "value": val[:2000]})
        else:
            raise ValueError(f"'{text}': action 'orders', 'link' या 'message' होना चाहिए")
    if len(out) > 12: raise ValueError("Maximum 12 buttons")
    return out

def _validate_menu_buttons(items):
    if len(items) > 12: raise ValueError("Maximum 12 buttons")
    out = []
    for b in items:
        text = str(b.get("text", "")).strip()[:64]
        act = str(b.get("action", "")).strip()
        val = str(b.get("value", "")).strip()
        if not text: raise ValueError("बटन का नाम खाली नहीं हो सकता")
        if act == "my_orders":
            out.append({"text": text, "action": "my_orders", "value": ""})
        elif act == "link":
            if not val.startswith("http"): raise ValueError(f"'{text}': link https:// से शुरू होना चाहिए")
            out.append({"text": text, "action": "link", "value": val})
        elif act == "message":
            links = []
            for l in (b.get("links") or [])[:8]:
                lt = str(l.get("text", "")).strip()[:64]
                lu = str(l.get("url", "")).strip()
                if not lt or not lu.startswith("http"):
                    raise ValueError(f"'{text}': हर link button के लिए text और https:// link चाहिए")
                links.append({"text": lt, "url": lu, "style": str(l.get("style", "")).lower()})
            if not val and not links: raise ValueError(f"'{text}': message या कम से कम एक link चाहिए")
            out.append({"text": text, "action": "message", "value": val[:2000], "links": links})
        else:
            raise ValueError(f"'{text}': action गलत है")
    return out

@app.route("/dashboard/api/menu-config", methods=["GET", "POST"])
@require_auth
def api_menu_config():
    if request.method == "GET":
        return jsonify({"buttons": get_menu_cfg(), "footer": get_footer_text(), "live": get_live_cfg()})
    d = request.json or {}
    try:
        if isinstance(d.get("buttons"), list): buttons = _validate_menu_buttons(d["buttons"])
        elif "menu_text" in d: buttons = _parse_menu_lines(d.get("menu_text", ""))
        else: buttons = None
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    live_set = None
    old_live = get_live_cfg()
    if isinstance(d.get("live"), dict):
        lv = d["live"]
        enabled = bool(lv.get("enabled"))
        ch = str(lv.get("channel_id", "")).strip()
        if enabled and not re.fullmatch(r"-?\d{6,}", ch):
            return jsonify({"error": "Feed channel ID सिर्फ़ numbers में डालें (जैसे -1001234567890)"}), 400
        link = str(lv.get("share_link", "")).strip()
        if link and not link.startswith("http"):
            return jsonify({"error": "Share link https:// से शुरू होना चाहिए"}), 400
        live_set = {"enabled": enabled, "channel_id": ch,
                    "share_text": str(lv.get("share_text", "")).strip()[:64] or "🔗 Join & Buy",
                    "share_link": link, "updated_at": get_ist_time()}

    menu_set = {"updated_at": get_ist_time()}
    if buttons is not None:
        menu_set["buttons"] = buttons
        menu_set["my_orders_removed"] = not any(b.get("action") == "my_orders" for b in buttons)
    if "footer" in d: menu_set["footer"] = str(d.get("footer") or "")[:300]
    settings_col.update_one({"_id": "menu_cfg"}, {"$set": menu_set}, upsert=True)
    invalidate_cache("menu_cfg")

    if live_set is not None:
        settings_col.update_one({"_id": "live_feed_cfg"}, {"$set": live_set}, upsert=True)
        invalidate_cache("live_feed_cfg")
        if live_set["enabled"] and live_set["channel_id"] and live_set["channel_id"] != old_live["channel_id"]:
            purchases_col.update_many({}, {"$unset": {"feed_posted": ""}})
            if start_backfill_job(): return jsonify({"status": "success", "backfill": "started"})
            return jsonify({"status": "success", "backfill": "busy"})
    return jsonify({"status": "success"})

@app.route("/dashboard/api/live-feed/backfill", methods=["POST"])
@require_auth
def api_live_backfill():
    cfg = get_live_cfg()
    if not cfg["enabled"] or not cfg["channel_id"]:
        return jsonify({"error": "पहले Live feed ON करके channel ID save करें"}), 400
    if not start_backfill_job():
        return jsonify({"error": "पहले से भेजा जा रहा है"}), 409
    return jsonify({"status": "started"})

@app.route("/dashboard/api/live-feed/status")
@require_auth
def api_live_status():
    return jsonify(live_job_snapshot())

@app.route("/dashboard/api/live-feed/stop", methods=["POST"])
@require_auth
def api_live_stop():
    with LIVE_JOB_LOCK:
        if LIVE_JOB["status"] not in ("running", "stopping"):
            return jsonify({"error": "कुछ चल नहीं रहा"}), 400
        LIVE_JOB.update(stop=True, status="stopping")
    return jsonify({"status": "stopping"})

@app.route("/dashboard/api/load")
@require_auth
def api_load():
    snap = dict(LOAD_STATE)
    lvl, msg = load_level(snap.get("cpu"))
    snap["level"], snap["message"] = lvl, msg
    return jsonify(snap)

@app.route("/dashboard/api/purchases", methods=["DELETE"])
@require_auth
def api_remove_purchase():
    """किसी यूज़र का एक कोर्स का खरीदी-रिकॉर्ड हटाता है (इससे नया join access बंद होता है)"""
    d = request.json or {}
    try: uid = int(d.get("user_id"))
    except (TypeError, ValueError): return jsonify({"error": "Invalid user ID"}), 400
    cid = str(d.get("course_id", "")).strip()
    if not cid: return jsonify({"error": "Course ID is required"}), 400
    res = purchases_col.delete_many({"user_id": uid, "item_info": {"$regex": "^" + re.escape(cid) + r" \|"}})
    return jsonify({"status": "success", "removed": res.deleted_count})

@app.route("/dashboard/api/broadcast", methods=["POST"])
@require_auth
def api_broadcast():
    data = request.json or {}
    msg = (data.get("message") or "").strip()
    btns = data.get("buttons", [])
    if not msg: return jsonify({"error": "Empty message"}), 400

    markup = telebot.types.InlineKeyboardMarkup()
    for b in btns:
        if b.get("text") and b.get("url"):
            b_url = b["url"]
            if b_url.lower() == "close": markup.add(telebot.types.InlineKeyboardButton(b["text"], callback_data="close_msg"))
            elif b_url.startswith("http"): markup.add(telebot.types.InlineKeyboardButton(b["text"], url=b_url))
            else: markup.add(telebot.types.InlineKeyboardButton(b["text"], callback_data=b_url))
    if not markup.keyboard: markup = None

    bid = "bc_" + uuid.uuid4().hex[:8]
    broadcasts_col.insert_one({
        "bid": bid, "message": msg, "created_at": get_ist_time(), "created_ts": time.time(),
        "status": "SENDING", "sent": [], "sent_count": 0, "removed": False,
    })

    def run_bc():
        sent = []
        for u in users_col.find({}):
            raw_uid = u.get("user_id") or u.get("chat_id") or u.get("_id")
            if not raw_uid: continue
            try:
                uid = int(raw_uid)
                if is_banned(uid): continue  # banned यूज़र को broadcast नहीं
                m_sent = bot.send_message(uid, msg, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
                sent.append([uid, m_sent.message_id])
                time.sleep(0.04)
            except Exception: pass
        # भेजे गए हर message की ID सेव होती है, ताकि बाद में सबकी चैट से हटाया जा सके
        broadcasts_col.update_one({"bid": bid}, {"$set": {"status": "SENT", "sent": sent, "sent_count": len(sent)}})

    threading.Thread(target=run_bc, daemon=True).start()
    return jsonify({"status": "success", "bid": bid})

@app.route("/dashboard/api/broadcasts")
@require_auth
def api_broadcasts():
    out = []
    for b in broadcasts_col.find({}, {"sent": 0}).sort("created_ts", -1).limit(30):
        out.append({
            "bid": b.get("bid"), "message": b.get("message", ""), "created_at": b.get("created_at", ""),
            "status": b.get("status", ""), "sent_count": b.get("sent_count", 0), "removed": b.get("removed", False),
        })
    return jsonify(out)

@app.route("/dashboard/api/broadcasts/<bid>", methods=["DELETE"])
@require_auth
def api_remove_broadcast(bid):
    """भेजे गए broadcast को सभी यूज़र्स की चैट से हटाता है (Telegram 48 घंटे के अंदर वाले messages हटाने देता है)"""
    b = broadcasts_col.find_one({"bid": bid})
    if not b: return jsonify({"error": "Broadcast not found"}), 404
    if b.get("removed"): return jsonify({"error": "This broadcast was already removed"}), 400
    if b.get("status") != "SENT": return jsonify({"error": "Broadcast is still sending. Remove it after it finishes"}), 400

    def run_rm():
        for uid, mid in b.get("sent", []):
            try: bot.delete_message(uid, mid)
            except Exception: pass
            time.sleep(0.05)
        broadcasts_col.update_one({"bid": bid}, {"$set": {"removed": True, "sent": []}})

    threading.Thread(target=run_rm, daemon=True).start()
    return jsonify({"status": "success"})

DASH_ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                 '<rect width="64" height="64" rx="16" fill="#4f8cff"/>'
                 '<text x="32" y="43" font-size="34" text-anchor="middle">🛍</text></svg>')

@app.route("/dashboard/icon.svg")
def dashboard_icon():
    return Response(DASH_ICON_SVG, mimetype="image/svg+xml")

@app.route("/dashboard/manifest.json")
def dashboard_manifest():
    """Dashboard को phone/PC में App की तरह install करने के लिए"""
    return jsonify({
        "name": "Store Dashboard", "short_name": "Store", "start_url": "/dashboard", "display": "standalone",
        "background_color": "#0b0f14", "theme_color": "#0b0f14",
        "icons": [{"src": "/dashboard/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    })

@app.route("/dashboard/api/channel-logs")
@require_auth
def api_channel_logs():
    return jsonify([{
        "user_id": l["user_id"], 
        "first_name": l.get("first_name", "Unknown"),
        "username": l.get("username", "None"),
        "course": l["course_id"], 
        "channel_name": l.get("channel_name", "Unknown Channel"), 
        "status": l["status"], 
        "date": l["date"]
    } for l in channel_logs_col.find().sort("_id", -1).limit(100)])

@app.route("/dashboard/api/sms-pool")
@require_auth
def api_sms_pool():
    return jsonify([{"amount": s.get("amount"), "created_at": s.get("created_at_str"), "preview": (s.get("raw_text") or "")[:100]} for s in sms_pool_col.find({"status": "UNUSED"}).sort("created_at_dt", -1).limit(100)])

def _cid_of(p):
    """खरीदी के रिकॉर्ड से course ID निकालता है (item_info = 'c_xxx | Rate: ...')"""
    return (p.get("item_info") or "").split(" |")[0].strip()

def _rate_of(p):
    """खरीदी के रिकॉर्ड से rate निकालता है (item_info में 'Rate: ₹xxx' लिखा होता है)"""
    m = re.search(r"Rate: ₹\s*([\d.]+)", p.get("item_info") or "")
    return float(m.group(1)) if m else None

def _purchase_rows(purchases):
    """खरीदी के रिकॉर्ड को dashboard के लिए आसान format में बदलता है (कोर्स + चैनल का नाम)"""
    cids = list({_cid_of(p) for p in purchases if _cid_of(p)})
    cmap = {}
    if cids:
        for c in courses_col.find({"course_id": {"$in": cids}}, {"course_id": 1, "channel_name": 1, "title": 1}):
            cmap[c.get("course_id")] = c
    rows = []
    for p in purchases:
        cid = _cid_of(p)
        c = cmap.get(cid, {})
        rows.append({
            "course_id": cid,
            "title": p.get("title") or c.get("title") or cid,
            "channel_name": c.get("channel_name") or "",
            "date": p.get("date", ""),
            "link": p.get("link", ""),
            "rate": _rate_of(p),
        })
    return rows

@app.route("/dashboard/api/users")
@require_auth
def api_users():
    search_q = request.args.get("search", "").strip()
    query = {}
    if search_q:
        if search_q.isdigit(): query = {"user_id": int(search_q)}
        else: query = {"username": {"$regex": re.escape(search_q), "$options": "i"}}
    users = list(users_col.find(query).sort("updated_at", -1).limit(200))
    uids = [u.get("user_id") for u in users]
    purchases = list(purchases_col.find({"user_id": {"$in": uids}}).sort("_id", 1))
    rows = _purchase_rows(purchases)
    by_uid = {}
    for p, row in zip(purchases, rows):
        by_uid.setdefault(p.get("user_id"), []).append(row)
    return jsonify([{
        "user_id": u.get("user_id"),
        "username": u.get("username", "None"),
        "updated_at": u.get("updated_at"),
        "purchases": by_uid.get(u.get("user_id"), []),
    } for u in users])

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#000000">
<meta name="color-scheme" content="dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Store">
<link rel="manifest" href="/dashboard/manifest.json">
<link rel="icon" href="/dashboard/icon.svg" type="image/svg+xml">
<title>Store Dashboard</title>
<style>
:root{
  --bg:#000000; --surface:#0a0a0a; --surface-2:#111111; --surface-3:#171717;
  --line:#1f1f1f; --line-2:#2a2a2a;
  --text:#f2f2f2; --muted:#8a8a8a; --dim:#5c5c5c;
  --ok:#4ade80; --warn:#fbbf24; --bad:#f87171;
  --radius:14px;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:var(--bg)}
body{color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer}
input,select,textarea{background:var(--bg);border:1px solid var(--line-2);border-radius:10px;padding:10px 12px;width:100%;outline:none;transition:border-color .15s}
input:focus,select:focus,textarea:focus{border-color:#4a4a4a}
textarea{min-height:110px;resize:vertical}
input[type=checkbox]{width:18px;height:18px;accent-color:#fff;flex:none}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
.muted{color:var(--muted)}
.dim{color:var(--dim)}
a{color:var(--text);text-decoration:underline;text-underline-offset:3px;text-decoration-color:#444}

/* ---------- top bar ---------- */
.topbar{position:fixed;top:0;left:0;right:0;height:60px;display:flex;align-items:center;gap:12px;padding:0 16px;background:#000;border-bottom:1px solid var(--line);z-index:20}
.icon-btn{width:38px;height:38px;border-radius:10px;border:1px solid var(--line-2);background:transparent;display:grid;place-items:center;flex:none;transition:background .15s}
.icon-btn:hover{background:var(--surface-2)}
.icon-btn svg{width:18px;height:18px;stroke:var(--text);fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.brand{font-weight:600;font-size:15px;letter-spacing:.01em;white-space:nowrap}
.search{flex:1;display:flex;gap:8px;max-width:560px;margin:0 auto}
.search input{padding:9px 14px;border-radius:999px;background:var(--surface);font-size:13.5px}
.search input::placeholder{color:var(--dim)}
.search button{padding:0 16px;border-radius:999px;border:1px solid var(--line-2);background:var(--text);color:#000;font-weight:600;font-size:13px;white-space:nowrap}
.avatar{width:36px;height:36px;border-radius:50%;background:var(--surface-3);border:1px solid var(--line-2);display:grid;place-items:center;font-weight:600;font-size:13px;flex:none}
.menu{position:fixed;right:14px;top:68px;background:var(--surface);border:1px solid var(--line-2);border-radius:12px;min-width:200px;padding:6px;display:none;box-shadow:0 20px 40px rgba(0,0,0,.8);z-index:25}
.menu.open{display:block}
.menu button{display:flex;align-items:center;gap:10px;width:100%;padding:10px 12px;border:none;background:none;border-radius:8px;text-align:left;font-size:13.5px}
.menu button:hover{background:var(--surface-2)}
.menu hr{border:none;border-top:1px solid var(--line);margin:6px 4px}

/* ---------- side panel ---------- */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);opacity:0;pointer-events:none;transition:opacity .25s;z-index:30}
.overlay.show{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;bottom:0;left:0;width:260px;max-width:84vw;background:#000;border-right:1px solid var(--line);transform:translateX(-102%);transition:transform .28s cubic-bezier(.2,.8,.2,1);z-index:40;padding:18px 10px;overflow-y:auto}
.drawer.open{transform:translateX(0)}
.drawer .head{display:flex;align-items:center;gap:10px;padding:4px 10px 14px}
.drawer .head .logo{width:34px;height:34px;border-radius:10px;background:var(--text);color:#000;display:grid;place-items:center;font-weight:700}
.nav-title{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);padding:16px 12px 6px}
.nav-item{display:flex;align-items:center;gap:10px;width:100%;padding:9px 12px;border:none;background:none;border-radius:9px;text-align:left;color:var(--muted);font-size:13.5px;transition:background .12s,color .12s}
.nav-item svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;flex:none}
.nav-item:hover{background:var(--surface-2);color:var(--text)}
.nav-item.active{background:var(--surface-3);color:var(--text);font-weight:600}

/* ---------- content ---------- */
main{padding:84px 18px 56px;max-width:1040px;margin:0 auto}
.page-head{margin-bottom:18px}
.page-head h1{font-size:20px;font-weight:600;margin:0;letter-spacing:-.01em}
.page-head p{margin:4px 0 0;color:var(--muted);font-size:13px}
.note{border:1px solid var(--line);border-radius:12px;padding:12px 14px;color:var(--muted);font-size:13px;margin-bottom:16px;background:var(--surface)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px;margin-bottom:16px}
.card h3{margin:0 0 4px;font-size:14px;font-weight:600}
.card .sub-h{margin:0 0 14px;color:var(--muted);font-size:12.5px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:18px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:16px;cursor:pointer;transition:border-color .15s,background .15s}
.stat:hover{border-color:var(--line-2);background:var(--surface-2)}
.stat .k{color:var(--muted);font-size:12px}
.stat .v{font-size:24px;font-weight:600;margin-top:6px;letter-spacing:-.02em}

/* list rows */
.row{display:flex;align-items:center;gap:14px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:8px;transition:opacity .2s,border-color .15s}
.row:hover{border-color:var(--line-2)}
.row .grow{flex:1;min-width:0}
.row .title{font-weight:600;overflow-wrap:anywhere}
.row .meta{color:var(--muted);font-size:12.5px;margin-top:3px;overflow-wrap:anywhere}
.row .right{display:flex;flex-direction:column;align-items:flex-end;gap:8px;flex:none}
.row .end{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.amount{font-weight:600;font-size:15px;font-variant-numeric:tabular-nums}
.price-edit{display:flex;gap:6px;margin-top:8px;justify-content:flex-end;align-items:center}
.price-in{width:110px;padding:6px 8px}
.row.state-pending{box-shadow:inset 2px 0 0 var(--warn)}
.row.state-ok{box-shadow:inset 2px 0 0 var(--ok)}
.row.state-bad{box-shadow:inset 2px 0 0 var(--bad)}
.row.removing{opacity:.3;pointer-events:none}

.pill{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:999px;font-size:11.5px;border:1px solid var(--line-2);color:var(--muted);white-space:nowrap}
.pill.ok{color:var(--ok);border-color:rgba(74,222,128,.25)}
.pill.warn{color:var(--warn);border-color:rgba(251,191,36,.25)}
.pill.bad{color:var(--bad);border-color:rgba(248,113,113,.25)}
.pill.solid{background:var(--text);color:#000;border-color:var(--text)}
.pill.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 13px;border-radius:9px;border:1px solid var(--line-2);background:var(--surface-2);font-weight:500;font-size:13px;white-space:nowrap;transition:background .12s,border-color .12s,opacity .12s}
.btn:hover{background:var(--surface-3)}
.btn svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;flex:none}
.btn.sm{padding:6px 10px;font-size:12px;border-radius:8px}
.btn.primary{background:var(--text);color:#000;border-color:var(--text)}
.btn.primary:hover{background:#d9d9d9}
.btn.ghost{background:transparent;border-color:transparent;color:var(--muted)}
.btn.ghost:hover{color:var(--text);background:var(--surface-2)}
.btn.danger{color:#fca5a5;background:rgba(248,113,113,.06);border-color:rgba(248,113,113,.22)}
.btn.danger:hover{background:rgba(248,113,113,.14);border-color:rgba(248,113,113,.4)}
.btn.success{color:#86efac;background:rgba(74,222,128,.06);border-color:rgba(74,222,128,.22)}
.btn.success:hover{background:rgba(74,222,128,.14);border-color:rgba(74,222,128,.4)}
.btn:disabled{opacity:.4;cursor:not-allowed}

/* segmented filter */
.seg{display:inline-flex;gap:4px;padding:4px;background:var(--surface);border:1px solid var(--line);border-radius:11px;margin-bottom:16px;flex-wrap:wrap}
.seg button{padding:6px 14px;border-radius:8px;border:none;background:none;color:var(--muted);font-size:13px}
.seg button:hover{color:var(--text)}
.seg button.active{background:var(--surface-3);color:var(--text);font-weight:600}

/* forms */
.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;align-items:end}
.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
.field .hint{font-size:11.5px;color:var(--dim);margin-top:5px}
.toggle-row{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px 0;border-bottom:1px solid var(--line)}
.toggle-row:last-child{border-bottom:none}
.toggle-row .lbl{font-size:13.5px}
.toggle-row .lbl small{display:block;color:var(--muted);font-size:12px;margin-top:2px}

/* user / purchase cards */
.user{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:16px;margin-bottom:12px}
.user-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.user-head .name{font-weight:600}
.purch{display:flex;align-items:center;gap:12px;padding:11px 12px;border:1px solid var(--line);border-radius:10px;margin-top:8px;background:#000}
.purch .grow{flex:1;min-width:0}
.purch .t{font-weight:500;overflow-wrap:anywhere}
.purch .m{margin-top:5px;display:flex;flex-wrap:wrap;gap:6px}
.empty{text-align:center;color:var(--muted);padding:36px 12px;border:1px dashed var(--line);border-radius:12px;font-size:13px}
.spinner{display:inline-block;width:15px;height:15px;border:2px solid var(--line-2);border-top-color:var(--text);border-radius:50%;animation:spin .7s linear infinite;vertical-align:-3px}
@keyframes spin{to{transform:rotate(360deg)}}

/* modal + confirm */
.modal-wrap{position:fixed;inset:0;background:rgba(0,0,0,.75);display:none;align-items:center;justify-content:center;padding:18px;z-index:50}
.modal-wrap.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--line-2);border-radius:18px;width:100%;max-width:560px;max-height:86vh;display:flex;flex-direction:column;animation:pop .18s ease-out}
.modal.small{max-width:400px}
@keyframes pop{from{transform:scale(.97);opacity:0}to{transform:none;opacity:1}}
.modal-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:16px 18px;border-bottom:1px solid var(--line)}
.modal-head b{font-weight:600}
.modal-body{padding:18px;overflow-y:auto}
.modal-foot{display:flex;justify-content:flex-end;gap:8px;padding:14px 18px;border-top:1px solid var(--line)}
.confirm-icon{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;margin-bottom:12px;background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.22)}
.confirm-icon svg{width:18px;height:18px;stroke:#fca5a5;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.confirm-title{font-weight:600;font-size:15px;margin-bottom:6px}
.confirm-text{color:var(--muted);font-size:13px}

/* toast */
.toast{position:fixed;left:50%;bottom:22px;transform:translate(-50%,16px);background:var(--surface-3);border:1px solid var(--line-2);color:var(--text);padding:10px 16px;border-radius:12px;opacity:0;transition:.22s;z-index:60;max-width:90vw;font-size:13px;box-shadow:0 10px 30px rgba(0,0,0,.6);pointer-events:none}
.toast.show{opacity:1;transform:translate(-50%,0)}
.toast.err{border-color:rgba(248,113,113,.35);color:#fecaca}

@media (max-width:640px){
  .brand{display:none}
  .topbar{gap:8px;padding:0 10px}
  .search button{padding:0 12px}
  #clock{display:none}
  main{padding:76px 12px 48px}
  .row{flex-direction:column;align-items:stretch}
  .row .right{align-items:flex-start}
  .row .end{justify-content:flex-start}
}
.label-s{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.total-row{display:flex;justify-content:space-between;align-items:center;margin-top:16px;padding-top:14px;border-top:1px solid var(--line);font-size:15px}
.total-row strong{font-size:18px}
.conn{display:flex;align-items:center;gap:7px;padding:6px 11px;border:1px solid var(--line-2);border-radius:999px;font-size:12px;color:var(--muted);white-space:nowrap}
.conn .dot{width:8px;height:8px;border-radius:50%;background:var(--bad);flex:none}
.conn.on{color:var(--text)}
.conn.on .dot{background:var(--ok);box-shadow:0 0 0 3px rgba(74,222,128,.18)}
.kv{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-top:1px solid var(--line);font-size:13.5px}
.kv>div:first-child{color:var(--muted)}
@media (max-width:640px){.conn-text{display:none}}
</style>
</head>
<body>

<header class="topbar">
  <button class="icon-btn" id="btn-back" title="Back" aria-label="Go back">
    <svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg>
  </button>
  <button class="icon-btn" id="btn-menu" title="Menu" aria-label="Open menu">
    <svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h10"/></svg>
  </button>
  <div class="brand">Store</div>
  <form class="search" id="search-form" autocomplete="off">
    <input id="q" type="text" placeholder="Search user by ID or username, press Enter" inputmode="search">
    <button type="submit">Search</button>
  </form>
  <div class="conn" id="conn" title="App status"><span class="dot"></span><span class="conn-text" id="conn-text">Checking…</span></div>
  <div class="mono dim" id="clock"></div>
  <button class="avatar" id="btn-avatar" title="Account" aria-label="Account">A</button>
</header>

<div class="menu" id="profile-menu">
  <button data-act="open-setup">Add Bot / Setup</button>
  <button data-act="open-admins">Admins</button>
  <button data-act="refresh">Refresh</button>
  <hr>
  <button data-act="open-appupdate">App update</button>
  <button data-act="update-code">Update code</button>
  <button data-act="logout">Log out</button>
  <button data-act="close-menu">Close</button>
</div>

<div class="overlay" id="overlay"></div>
<aside class="drawer" id="drawer">
  <div class="head">
    <div class="logo">S</div>
    <div><b>Store Dashboard</b><div class="muted" style="font-size:12px">Admin panel</div></div>
  </div>
  <div id="nav"></div>
</aside>

<main>
  <div class="page-head"><h1 id="page-title">Dashboard</h1><p id="page-sub"></p></div>
  <div id="body"></div>
</main>

<div class="modal-wrap" id="modal">
  <div class="modal" id="modal-box">
    <div class="modal-head"><b id="modal-title"></b><button class="icon-btn" data-act="close-modal" aria-label="Close"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
    <div class="modal-body" id="modal-body"></div>
    <div class="modal-foot" id="modal-foot" style="display:none"></div>
  </div>
</div>

<script>
/* ============ helpers ============ */
const $ = s => document.querySelector(s);
const enc = encodeURIComponent;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = v => "₹" + (v ?? 0);
const empty = msg => `<div class="empty">${esc(msg)}</div>`;
const fmtSecs = s => { s = Math.max(0, +s || 0); return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s"; };
const ICON = {
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  alert: '<path d="M12 8v5M12 16.5v.5M10.3 3.9L2.6 17.2A2 2 0 004.3 20h15.4a2 2 0 001.7-2.8L13.7 3.9a2 2 0 00-3.4 0z"/>',
  link: '<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.6-3.4 3.2-5.5 6.5-5.5s5.9 2.1 6.5 5.5M16 4.5a3.5 3.5 0 010 7M18.5 14.8c1.7.7 2.8 2.6 3 5.2"/>',
};
const svg = (name, cls = "") => `<svg viewBox="0 0 24 24" class="${cls}">${ICON[name]}</svg>`;

function toast(msg, type = "ok"){
  const t = document.createElement("div");
  t.className = "toast" + (type === "err" ? " err" : "");
  t.textContent = msg;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); }, 2600);
}

async function api(path, opts = {}){
  const o = Object.assign({headers: {"Content-Type": "application/json"}}, opts);
  if(o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const r = await fetch(path, o);
  if(r.status === 401){ location.reload(); return new Promise(() => {}); }  // session ended, show login
  let d = {};
  try { d = await r.json(); } catch(e) {}
  if(!r.ok) throw new Error(d.error || ("Request failed (" + r.status + ")"));
  return d;
}

/* ============ confirm popup (replaces browser confirm) ============ */
function confirmBox({title, text, okText = "Confirm", danger = true}){
  return new Promise(resolve => {
    openModal("", `
      <div class="confirm-icon">${danger ? svg("alert") : svg("check")}</div>
      <div class="confirm-title">${esc(title)}</div>
      <div class="confirm-text">${esc(text)}</div>`, {small: true, foot: `
      <button class="btn ghost" data-act="cancel-confirm">Cancel</button>
      <button class="btn ${danger ? "danger" : "primary"}" data-act="ok-confirm">${esc(okText)}</button>`});
    pendingConfirm = resolve;
  });
}
let pendingConfirm = null;
function settleConfirm(v){
  if(pendingConfirm){ const r = pendingConfirm; pendingConfirm = null; r(v); }
  closeModal();
}

/* ============ navigation ============ */
const NAV = [
  ["Overview", [["home", "Dashboard", "users"]]],
  ["Sales", [["orders", "Orders", "check"], ["users", "Users & purchases", "users"], ["bans", "Ban list", "users"]]],
  ["Store", [["courses", "Courses", "check"], ["delivery", "Post-purchase message", "check"], ["offers", "Offers", "check"], ["logs", "Channel logs", "check"]]],
  ["Messaging", [["broadcast", "Broadcast", "check"], ["menu", "Menu & live feed", "check"], ["sms", "SMS pool & trigger", "check"]]],
  ["System", [["setup", "Bot setup", "check"], ["appupdate", "App update", "check"], ["settings", "Bot settings", "check"]]],
];
const TITLES = {home: "Dashboard", orders: "Orders", users: "Users & purchases", courses: "Courses", offers: "Offers",
  menu: "Menu & live feed", bans: "Ban list", delivery: "Post-purchase message", logs: "Channel logs", broadcast: "Broadcast", sms: "SMS pool & trigger", settings: "Bot settings", setup: "Bot setup", search: "Search"};
const SUBS = {home: "Overview of today's store activity.", orders: "Approve or cancel payment requests.",
  users: "Every user and the courses they bought.", courses: "Browse, inspect and remove packs.",
  menu: "Bottom buttons in the bot and the public live purchase feed.", bans: "Block users by ID or @username, for a set time or forever.", delivery: "Text and buttons shown right after a payment.", offers: "Discount links for customers.", logs: "Channel join requests.", broadcast: "Message all users, and remove sent messages.",
  sms: "Bank SMS pool and manual payment trigger.", settings: "Maintenance and auto-clean options.", setup: "MongoDB link, bot token, IDs and payment details. Saving restarts the bot."};

function buildNav(){
  $("#nav").innerHTML = NAV.map(([g, items]) =>
    `<div class="nav-title">${g}</div>` +
    items.map(([k, label]) => `<button class="nav-item" data-view="${k}">${label}</button>`).join("")
  ).join("");
}
function highlightNav(v){
  document.querySelectorAll(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.view === v));
}

/* ============ screen templates ============ */
const SHELL = {
  appupdate: () => `<div id="list"></div>`,
  home: () => `<div id="load-card"></div><div class="note">Search any user from the top bar to see their full purchase history.</div><div id="list"></div>`,
  orders: () => `<div class="seg">${[["all","All"],["pending","Pending"],["completed","Completed"],["expired","Expired"]]
      .map(([k, l]) => `<button data-of="${k}" class="${k === orderFilter ? "active" : ""}">${l}</button>`).join("")}</div><div class="toolbar" style="margin:12px 0"><button class="btn danger sm" data-act="clear-open">Clear open orders</button></div><div id="list"></div>`,
  users: () => `<div class="note">Recent users and their courses. Use the search bar to find a specific user.</div><div id="list"></div>`,
  courses: () => `<div class="note">Create new packs from the bot's Admin Panel (Packs & Batches). You can view and remove them here.</div><div id="list"></div>`,
  offers: () => `<div class="card"><h3>Create offer</h3><p class="sub-h">Generates a discount link you can share.</p>
    <div class="form-grid">
      <div class="field"><label>Discount (%)</label><input id="off-disc" type="number" value="50"></div>
      <div class="field"><label>Applies to</label><select id="off-tgt"><option value="all">All courses</option><option value="single">One course</option></select></div>
      <div class="field"><label>Course ID (one course only)</label><input id="off-cid" placeholder="c_12345"></div>
      <div class="field"><label>Max users (0 = unlimited)</label><input id="off-max" type="number" value="0"></div>
      <div class="field"><label>Times per user (0 = unlimited)</label><input id="off-peruser" type="number" value="1"></div>
      <div class="field"><label>Valid for (hours)</label><input id="off-hrs" type="number" value="24"></div>
      <div><button class="btn primary" data-act="create-offer">Create offer</button></div>
    </div></div><div id="list"></div>`,
  logs: () => `<div id="list"></div>`,
  broadcast: () => `<div class="card"><h3>New broadcast</h3><p class="sub-h">Sent to every user. HTML tags like &lt;b&gt; are supported.</p>
    <textarea id="bc-msg" placeholder="Write your message…"></textarea>
    <div style="margin-top:12px;display:flex;justify-content:flex-end"><button class="btn primary" data-act="send-broadcast">Send to all users</button></div></div>
    <div class="card"><h3>Sent broadcasts</h3><p class="sub-h">Remove a broadcast from every user's chat. Telegram only allows deleting messages sent within the last 48 hours.</p><div id="list"></div></div>`,
  sms: () => `<div class="card"><h3>Manual payment trigger</h3><p class="sub-h">If a bank SMS did not arrive, enter the exact amount (for example 50.01) and trigger it.</p>
    <div class="form-grid"><div class="field"><label>Amount</label><input id="inj-amt" placeholder="50.01" inputmode="decimal"></div>
    <div><button class="btn primary" data-act="inject">Trigger</button></div></div></div><div id="list"></div>`,
  bans: () => `<div id="list"></div>`,
  menu: () => `<div id="list"></div>`,
  delivery: () => `<div id="list"></div>`,
  settings: () => `<div id="list"></div>`,
  setup: () => `<div id="list"></div>`,
};

/* ============ data ============ */
let view = "home", orderFilter = "all", lastQuery = "";
const cache = {};
const cacheKey = (v = view) => v === "orders" ? "orders-" + orderFilter : v;

const FETCH = {
  home: () => api("/dashboard/api/overview"),
  orders: () => api("/dashboard/api/orders?status=" + orderFilter),
  users: () => api("/dashboard/api/users"),
  courses: () => api("/dashboard/api/courses"),
  offers: () => api("/dashboard/api/offers"),
  logs: () => api("/dashboard/api/channel-logs"),
  broadcast: () => api("/dashboard/api/broadcasts"),
  sms: () => api("/dashboard/api/sms-pool"),
  settings: () => api("/dashboard/api/system-settings"),
  bans: () => api("/dashboard/api/bans"),
  menu: () => api("/dashboard/api/menu-config"),
  delivery: () => api("/dashboard/api/success-msg"),
  setup: () => api("/dashboard/api/config"),
  appupdate: () => api("/dashboard/api/app-update"),
};

function setListLoading(){
  const l = $("#list");
  if(l) l.innerHTML = `<div class="empty"><span class="spinner"></span></div>`;
}

// Show cached data instantly, then refresh quietly in the background.
async function loadView(quiet = false){
  const v = view, key = cacheKey(v);
  if(cache[key] !== undefined) RENDER[v](cache[key]);
  else if(!quiet) setListLoading();
  try{
    const data = await FETCH[v]();
    if(view !== v || cacheKey(v) !== key) return;
    cache[key] = data;
    RENDER[v](data);
  }catch(e){
    if(!quiet) toast(e.message, "err");
  }
}

const statCard = (k, v, target) => `<div class="stat" data-view="${target}"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`;

function purchasesHtml(uid, list){
  if(!list || !list.length) return `<div class="muted" style="font-size:13px;margin-top:10px">No courses purchased yet.</div>`;
  return list.map(p => {
    const ch = p.channel_name ? `<span class="pill">${esc(p.channel_name)}</span>` : `<span class="pill">Text pack</span>`;
    const link = p.link && p.link.startsWith("http") ? `<a class="btn ghost sm" href="${esc(p.link)}" target="_blank" rel="noopener">Open</a>` : "";
    return `<div class="purch"><div class="grow">
        <div class="t">${esc(p.title)}</div>
        <div class="m">${ch}<span class="pill mono">${esc(p.course_id)}</span>${p.date ? `<span class="pill">${esc(p.date)}</span>` : ""}</div>
      </div>${link}
      <button class="btn danger sm" data-act="rm-purchase" data-uid="${esc(uid)}" data-cid="${esc(p.course_id)}">${svg("trash")}Remove</button></div>`;
  }).join("");
}

let loadPolling = false;
const LOAD_COLOR = p => p < 50 ? "#22c55e" : p < 80 ? "#eab308" : "#ef4444";
function loadRow(label, pct, extra){
  const p = pct == null ? 0 : Math.min(100, pct);
  return `<div style="margin:12px 0">
    <div style="display:flex;justify-content:space-between;font-size:13px;gap:8px"><span>${esc(label)}</span><span class="mono">${pct == null ? "…" : pct.toFixed(1) + "%"}${extra ? " · " + esc(extra) : ""}</span></div>
    <div style="background:var(--surface-3);border-radius:8px;height:10px;overflow:hidden;margin-top:6px"><div style="width:${p}%;height:100%;background:${LOAD_COLOR(p)};transition:width .5s,background .5s"></div></div></div>`;
}
function renderLoad(d){
  if(!d.supported) return `<div class="card"><h3>Bot load</h3><p class="sub-h">Live load shows only when the bot runs on a Linux server (AWS, Azure or Render).</p></div>`;
  const colors = {free: "#22c55e", light: "#84cc16", busy: "#eab308", heavy: "#ef4444", unknown: "#94a3b8"};
  const names = {free: "FREE", light: "LIGHT", busy: "BUSY", heavy: "HEAVY", unknown: "MEASURING"};
  const botMemPct = d.bot_mem_mb != null && d.mem_total_mb ? d.bot_mem_mb * 100 / d.mem_total_mb : null;
  return `<div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
      <div><h3 style="margin:0">Bot load</h3><p class="sub-h" style="margin:4px 0 0">${esc(d.message)}</p></div>
      <span class="pill" style="background:${colors[d.level] || "#94a3b8"};color:#000;font-weight:700">${names[d.level] || ""}</span></div>
    <div style="margin-top:8px">
      ${loadRow("Server CPU", d.cpu)}
      ${loadRow("Server memory", d.mem, d.mem_used_mb != null ? d.mem_used_mb + " of " + d.mem_total_mb + " MB" : "")}
      ${loadRow("Bot CPU", d.bot_cpu)}
      ${loadRow("Bot memory", botMemPct, d.bot_mem_mb != null ? d.bot_mem_mb + " MB" : "")}
    </div>
    <p class="sub-h" style="margin-top:6px">Load (1 min): ${d.load1 != null ? d.load1 : "…"} · Updated ${esc(d.updated)} · refreshes every 5 seconds</p></div>`;
}
async function loadPoll(){
  if(loadPolling) return;
  loadPolling = true;
  while(view === "home" && document.getElementById("load-card")){
    try{
      const d = await api("/dashboard/api/load");
      const el = document.getElementById("load-card");
      if(el) el.innerHTML = renderLoad(d);
    }catch(e){ /* try again next round */ }
    await new Promise(r => setTimeout(r, 5000));
  }
  loadPolling = false;
}

let feedPolling = false;
function renderFeedJob(j){
  if(!j || j.status === "idle") return `<div class="muted" style="font-size:13px">No sending in progress.</div>`;
  const labels = {running: "Sending past purchases…", stopping: "Stopping…", done: "Finished", stopped: "Stopped", error: "Error"};
  const pct = j.percent || 0;
  const running = j.status === "running" || j.status === "stopping";
  return `<div class="meta" style="margin-bottom:6px"><b>${esc(labels[j.status] || j.status)}</b> · ${j.done + j.failed} of ${j.total} (${pct}%) · sent ${j.done} · failed ${j.failed}${j.message ? " · " + esc(j.message) : ""}</div>
    <div style="background:var(--surface-3);border-radius:8px;height:10px;overflow:hidden"><div style="width:${pct}%;height:100%;background:#3b82f6;transition:width .4s"></div></div>
    ${running && j.status === "running" ? `<div style="display:flex;justify-content:flex-end;margin-top:10px"><button class="btn danger sm" data-act="stop-feed">Stop sending</button></div>` : ""}`;
}
async function pollFeedJob(){
  const el = document.getElementById("feed-job");
  if(!el) { feedPolling = false; return; }
  if(feedPolling) return;
  feedPolling = true;
  try{
    const j = await api("/dashboard/api/live-feed/status");
    const box = document.getElementById("feed-job");
    if(box) box.innerHTML = renderFeedJob(j);
    if(box && (j.status === "running" || j.status === "stopping")) { feedPolling = false; setTimeout(pollFeedJob, 2000); return; }
  }catch(e){ /* ignore, try again on next render */ }
  feedPolling = false;
}

const RENDER = {
  appupdate: c => {
    if(!c.configured){
      $("#list").innerHTML = `<div class="card"><h3>App update</h3><p class="sub-h">Bot setup में "GitHub repo" भरें (जैसे ravi/now-app), फिर यहाँ आएँ।</p></div>`;
      return;
    }
    const status = c.available
      ? `<span class="pill ok">New version ${esc(c.latest)} available</span>`
      : c.latest
        ? `<span class="pill">Up to date (latest ${esc(c.latest)})</span>`
        : `<span class="pill warn">Could not read GitHub</span>`;
    $("#list").innerHTML = `
      <div class="card"><h3>App update</h3>
        <p class="sub-h">${status}</p>
        <div class="kv"><div>Repo</div><div class="mono">${esc(c.repo)}</div></div>
        <div class="kv"><div>Installed on phone</div><div class="mono">${esc(c.installed || "not connected yet")}</div></div>
        <div class="kv"><div>Latest on GitHub</div><div class="mono">${esc(c.latest || "—")}</div></div>
        ${c.published ? `<div class="kv"><div>Published</div><div class="mono">${esc(c.published)}</div></div>` : ""}
        ${c.error ? `<p class="sub-h" style="color:var(--bad)">${esc(c.error)}</p>` : ""}
        ${c.notes ? `<div class="label-s" style="margin-top:14px">Release notes</div><pre class="mono" style="white-space:pre-wrap;margin:0;color:var(--muted)">${esc(c.notes)}</pre>` : ""}
        <p class="sub-h" style="margin-top:14px">अपडेट दिखने पर ऐप को अगले एक मिनट में नोटिस मिल जाएगा। ऐप में Install दबाने पर Android की install screen खुलेगी।</p>
        <div style="margin-top:14px;display:flex;justify-content:flex-end"><button class="btn primary" data-act="check-app-update">Check GitHub now</button></div>
      </div>`;
  },
  home: d => {
    $("#list").innerHTML = `<div class="stats">
      ${statCard("Today's earnings", money(d.today_amount), "orders")}
      ${statCard("Pending payments", d.active_pending, "orders")}
      ${statCard("Unused SMS", d.pending_sms, "sms")}
      ${statCard("Total sales", d.completed_total, "orders")}
      ${statCard("Total users", d.total_users, "users")}
      ${statCard("Expired QR", d.expired_total, "orders")}</div>`;
  },

  orders: list => {
    $("#list").innerHTML = list.length ? list.map(x => {
      const st = x.status === "PENDING" ? "state-pending" : x.status === "EXPIRED" ? "state-bad" : "state-ok";
      const badge = x.status === "PENDING"
        ? `<span class="pill warn" data-remain="${esc(x.remaining_seconds)}">${fmtSecs(x.remaining_seconds)} left</span>`
        : `<span class="pill ${x.status === "EXPIRED" ? "bad" : "ok"}">${esc(x.method || x.status)}</span>`;
      const acts = (x.status === "PENDING" || x.status === "EXPIRED")
        ? `<button class="btn success sm" data-act="approve" data-id="${esc(x.order_id)}">${svg("check")}Approve</button>
           <button class="btn danger sm" data-act="cancel" data-id="${esc(x.order_id)}">${svg("x")}Cancel</button>` : "";
      return `<div class="row ${st}">
        <div class="grow"><div class="title">${esc(x.user)} <span class="dim">·</span> <span class="mono muted">${esc(x.course_id)}</span></div>
          <div class="meta mono">${esc(x.order_id)} · ${esc(x.created_at)}</div></div>
        <div class="right"><div class="amount">₹${esc(x.amount)}</div><div class="end">${badge}${acts}</div></div></div>`;
    }).join("") : empty("No orders in this list.");
  },

  users: list => {
    $("#list").innerHTML = list.length ? list.map(u => `<div class="user">
      <div class="user-head"><div><span class="name">@${esc(u.username || "none")}</span> <span class="mono dim">${esc(u.user_id)}</span></div>
      <span class="pill">${u.purchases.length} course${u.purchases.length === 1 ? "" : "s"}</span></div>
      ${purchasesHtml(u.user_id, u.purchases)}</div>`).join("") : empty("No users found.");
  },

  menu: c => {
    const live = c.live || {};
    const btns = c.buttons || [];
    const actLabel = {my_orders: "Shows My Order List", link: "Opens a link", message: "Sends a message"};
    const rows = btns.length ? btns.map((b, i) => `<div class="row">
        <div class="grow"><div class="title">${esc(b.text)}</div>
          <div class="meta">${esc(actLabel[b.action] || b.action)}${b.value ? " · " + esc(b.value) : ""}</div></div>
        <div class="right"><div class="end"><button class="btn danger sm" data-act="menu-del" data-idx="${i}">${svg("trash")}Delete</button></div></div></div>`).join("") : empty("No buttons yet. Tap + Add button.");
    $("#list").innerHTML = `
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
          <div><h3>Bottom menu buttons</h3><p class="sub-h" style="margin:0">These appear at the bottom of the bot chat. Customers see them after /start.</p></div>
          <button class="btn primary" data-act="menu-add">+ Add button</button></div>
        <div style="margin-top:12px">${rows}</div></div>
      <div class="card"><h3>My Order List teaser</h3><p class="sub-h">One line shown at the bottom of My Order List. Leave it empty to hide it.</p>
        <div class="field"><input id="menu-footer" value="${esc(c.footer)}"></div></div>
      <div class="card"><h3>Live purchase feed</h3>
        <p class="sub-h">Posts every new paid purchase to a public channel. It shows only the buyer's name, amount, channel name and date. There is no link to the buyer, so nobody can open a chat with them from the feed.</p>
        <div class="toggle-row"><div class="lbl">Live feed on<small>Needs the channel ID below</small></div><input type="checkbox" id="live-on" ${live.enabled ? "checked" : ""}></div>
        <div class="form-grid" style="margin-top:12px">
          <div class="field"><label>Feed channel ID</label><input id="live-channel" placeholder="-1001234567890" value="${esc(live.channel_id)}"></div>
          <div class="field"><label>Button text (under each post)</label><input id="live-share-text" value="${esc(live.share_text)}"></div>
          <div class="field"><label>Button link (under each post)</label><input id="live-share-link" placeholder="https://t.me/+..." value="${esc(live.share_link)}"></div>
        </div>
        <div class="note" style="margin-top:12px"><b>Before turning this on:</b> add the bot as an <b>admin</b> of the feed channel, with permission to post messages. Use the channel ID (it starts with -100), not the invite link. When you save a new channel ID, all past purchases are sent to it automatically.</div>
        <div id="feed-job" style="margin-top:14px"></div>
        <div style="display:flex;justify-content:flex-end;gap:8px;flex-wrap:wrap;margin-top:12px">
          <button class="btn ghost" data-act="backfill-feed">Send past purchases again</button>
          <button class="btn primary" data-act="save-menu">Save feed & teaser</button></div>
      </div>`;
    pollFeedJob();
  },

  bans: list => {
    const rows = list.length ? list.map(b => `<div class="row state-bad">
      <div class="grow"><div class="title">${b.username ? "@" + esc(b.username) : "No username"} <span class="mono dim">${b.user_id != null ? "· " + esc(b.user_id) : ""}</span></div>
        <div class="meta">${b.permanent ? "Permanent" : "Until " + esc(b.until_text)} · banned ${esc(b.banned_at)}${b.reason ? " · " + esc(b.reason) : ""}</div></div>
      <div class="right"><div class="end"><button class="btn sm" data-act="unban" data-target="${esc(b.user_id != null ? b.user_id : "@" + b.username)}">Unban</button></div></div></div>`).join("") : empty("No banned users.");
    $("#list").innerHTML = `<div class="card"><h3>Ban a user</h3><p class="sub-h">Enter a numeric user ID or a @username. A banned user cannot use the bot or join channels.</p>
      <div class="form-grid">
        <div class="field"><label>User ID or @username</label><input id="ban-target" placeholder="123456789 or @username"></div>
        <div class="field"><label>Duration</label><select id="ban-hours"><option value="1">1 hour</option><option value="24">24 hours</option><option value="168">7 days</option><option value="720">30 days</option><option value="perm" selected>Permanent</option></select></div>
        <div><button class="btn danger" data-act="ban-add">Ban user</button></div>
      </div></div>
      <div class="card"><h3>Banned users</h3>${rows}</div>`;
  },

  delivery: c => {
    const btnLines = (c.buttons || []).map(b => `${b.text} - ${b.url}`).join("\n");
    $("#list").innerHTML = `
      <div class="card"><h3>Message text</h3><p class="sub-h">Shown right after a payment is verified. HTML tags like &lt;b&gt; are supported.</p>
        <div class="field"><label>Header</label><textarea id="dl-header">${esc(c.header)}</textarea></div>
        <div class="field"><label>Text between header and body (optional)</label><textarea id="dl-between">${esc(c.between)}</textarea></div>
        <div class="field"><label>Body (channel packs only)</label><textarea id="dl-body">${esc(c.body)}</textarea></div></div>
      <div class="card"><h3>Main button (channel link)</h3><p class="sub-h">Opens the channel's join-request link. The link is never shown as plain text.</p>
        <div class="toggle-row"><div class="lbl">Show main button<small>Turn off to hide it</small></div><input type="checkbox" id="dl-main-on" ${c.main_btn && c.main_btn.enabled === false ? "" : "checked"}></div>
        <div class="form-grid" style="margin-top:12px">
          <div class="field"><label>Button text</label><input id="dl-main-text" value="${esc(c.main_btn && c.main_btn.text)}"></div>
        </div></div>
      <div class="card"><h3>Extra buttons</h3><p class="sub-h">One per line: <code>Button text - https://link</code>. Use <code>close</code> as the link to make a button that only closes the message.</p>
        <textarea id="dl-buttons" placeholder="Button text - https://example.com - blue">${esc(btnLines)}</textarea></div>
      <div class="card"><h3>Request received message</h3><p class="sub-h">Sent after the join request is accepted. The earlier "send request" message is deleted.</p>
        <div class="field"><label>Text</label><textarea id="dl-recv">${esc(c.received_text)}</textarea></div>
        <div class="toggle-row"><div class="lbl">Show channel button<small>Uses the same channel link</small></div><input type="checkbox" id="dl-recv-on" ${c.received_btn && c.received_btn.enabled === false ? "" : "checked"}></div>
        <div class="form-grid" style="margin-top:12px">
          <div class="field"><label>Button text</label><input id="dl-recv-text" value="${esc(c.received_btn && c.received_btn.text)}"></div>
        </div></div>
      <div style="display:flex;justify-content:flex-end;gap:8px;flex-wrap:wrap"><button class="btn ghost" data-act="reset-delivery">Reset to default</button><button class="btn primary" data-act="save-delivery">Save message</button></div>`;
  },

  courses: list => {
    $("#list").innerHTML = list.length ? list.map(c => `<div class="row">
      <div class="grow"><div class="title">${esc(c.title || "Untitled")}</div>
        <div class="meta mono">${esc(c.course_id)}${c.caption ? " · " + esc(c.caption) : ""}</div>
        <div style="margin-top:8px"><span class="pill">${c.is_channel ? esc(c.channel_name || "Channel") : "Text / link"}</span></div></div>
      <div class="right"><div class="amount">₹${esc(c.amount)}</div>
        <div class="price-edit"><input class="price-in" type="number" min="1" step="0.01" value="${esc(c.amount)}"><button class="btn sm" data-act="save-price" data-id="${esc(c.course_id)}">Save price</button></div>
        <div class="end">
          <button class="btn sm" data-act="buyers" data-id="${esc(c.course_id)}" data-title="${esc(c.title || c.course_id)}">${svg("users")}Buyers</button>
          <button class="btn danger sm" data-act="del-course" data-id="${esc(c.course_id)}">${svg("trash")}Delete</button>
        </div></div></div>`).join("") : empty("No courses yet.");
  },

  offers: list => {
    $("#list").innerHTML = list.length ? list.map(x => `<div class="row ${x.status === "Active" ? "state-ok" : "state-bad"}">
      <div class="grow"><div class="title">${esc(x.discount)}% off <span class="mono dim">· ${esc(x.offer_code)}</span></div>
        <div class="meta"><a href="${esc(x.link)}" target="_blank" rel="noopener">${esc(x.link)}</a></div>
        <div class="meta">Target: ${esc(x.target)}${x.course ? " (" + esc(x.course) + ")" : ""} · Used ${esc(x.used)}/${x.max == -1 ? "∞" : esc(x.max)} · Per user ${x.per_user == -1 ? "∞" : esc(x.per_user)} · Expires ${esc(x.expires)}</div></div>
      <div class="right"><span class="pill ${x.status === "Active" ? "ok" : "bad"}">${esc(x.status)}</span>
        <div class="end"><button class="btn danger sm" data-act="del-offer" data-id="${esc(x.offer_code)}">${svg("trash")}Delete</button></div></div></div>`).join("")
      : empty("No offers yet.");
  },

  logs: list => {
    $("#list").innerHTML = list.length ? list.map(x => `<div class="row ${x.status === "APPROVED" ? "state-ok" : "state-bad"}">
      <div class="grow"><div class="title">${esc(x.first_name)} <span class="muted">@${esc(x.username)}</span></div>
        <div class="meta mono">${esc(x.user_id)}</div>
        <div class="meta">${esc(x.channel_name)} · Pack ${esc(x.course)} · ${esc(x.date)}</div></div>
      <div class="right"><span class="pill ${x.status === "APPROVED" ? "ok" : "bad"}">${esc(x.status)}</span></div></div>`).join("")
      : empty("No channel logs yet.");
  },

  broadcast: list => {
    $("#list").innerHTML = list.length ? list.map(b => {
      const msg = b.message || "";
      const state = b.status === "SENDING" ? `<span class="pill warn">Sending</span>`
        : b.removed ? `<span class="pill bad">Removed</span>` : `<span class="pill ok">Sent</span>`;
      return `<div class="row">
        <div class="grow"><div class="title" style="white-space:pre-wrap;font-weight:500">${esc(msg.slice(0, 160))}${msg.length > 160 ? "…" : ""}</div>
          <div class="meta">${esc(b.created_at)} · ${esc(b.sent_count)} users</div></div>
        <div class="right">${state}
          <div class="end"><button class="btn danger sm" data-act="rm-broadcast" data-id="${esc(b.bid)}" ${b.removed || b.status !== "SENT" ? "disabled" : ""}>${svg("trash")}Remove from chats</button></div></div></div>`;
    }).join("") : empty("No broadcasts sent yet.");
  },

  sms: list => {
    $("#list").innerHTML = list.length ? list.map(x => `<div class="row state-pending">
      <div class="grow"><div class="title">₹${esc(x.amount)}</div><div class="meta mono">${esc(x.preview)}</div></div>
      <div class="right"><div class="meta">${esc(x.created_at)}</div></div></div>`).join("") : empty("No unused SMS in the pool.");
  },

  setup: c => {
    const v = c.values || {}, boot = c.boot || {};
    const row = ([k, label, desc, type = "text", cls = ""], value) => `<div class="field" style="grid-column:auto">
        <label>${esc(label)}</label>
        <input data-cfg="${k}" type="${type}" value="${esc(value)}" autocomplete="off" spellcheck="false" class="${cls}">
        <div class="hint">${esc(desc)}</div></div>`;
    const section = (title, sub, rows) => `<div class="card"><h3>${esc(title)}</h3><p class="sub-h">${esc(sub)}</p>
        <div class="form-grid">${rows}</div></div>`;

    const status = c.first_run
      ? `<span class="pill warn">First setup: fill the sections below</span>`
      : c.configured
        ? `<span class="pill ok">Bot is configured</span>`
        : `<span class="pill warn">Setup required, the bot is not running yet</span>`;
    const codeCard = c.first_run
      ? `<div class="card"><h3>Setup code</h3>
          <p class="sub-h">Look at the terminal where bot.py is running. A 6-digit code is shown there. Type it here.</p>
          <div class="field"><label>Setup code *</label><input data-cfg="SETUP_CODE" inputmode="numeric" autocomplete="off" class="mono"></div></div>`
      : "";

    const mongo = section("1. MongoDB database",
      "Saved on the server file, so the bot can connect to your data. Change this only when you move to another database.",
      [
        row(["MONGO_URI", "MongoDB URL *", "Shown masked. Leave it as it is to keep the current one.", "text", "mono"], boot.MONGO_URI),
        row(["MONGO_DB_NAME", "Database name", "Same name as your old database, so old data loads. Default: telegram_store_bot", "text", "mono"], boot.MONGO_DB_NAME),
        row(["DASHBOARD_USERNAME", "Dashboard username", "Login name for this dashboard", "text", ""], boot.DASHBOARD_USERNAME),
        row(["DASHBOARD_PASSWORD", "Dashboard password", "At least 8 characters. Leave empty to keep the current one.", "password", ""], ""),
      ].join(""));

    const bot = section("2. Bot details",
      "Saved in your database. Saving restarts the bot.",
      [
        row(["BOT_TOKEN", "Bot token *", "From BotFather. Shown masked. Leave it as it is to keep the current token.", "text", "mono"], v.BOT_TOKEN),
        row(["ADMIN_ID", "Owner Telegram ID *", "Your numeric Telegram ID. You are always an admin.", "text", ""], v.ADMIN_ID),
        row(["DB_CHANNEL_ID", "Private channel / log group ID *", "Usually starts with -100. Add the bot there as admin first.", "text", ""], v.DB_CHANNEL_ID),
      ].join(""));

    const payment = section("3. Payment details",
      "Shown to buyers on the payment screen and in the start menu.",
      [
        row(["MERCHANT_NAME", "Merchant name", "Shown on the UPI payment screen", "text", ""], v.MERCHANT_NAME),
        row(["UPI_ID", "UPI ID *", "For example name@bank", "text", ""], v.UPI_ID),
        row(["CHAT_LINK", "Contact link", "Optional. For example https://t.me/yourname", "text", ""], v.CHAT_LINK),
        row(["INTERNATIONAL_LINK", "International payment link", "Optional", "text", ""], v.INTERNATIONAL_LINK),
      ].join(""));

    const timers = section("4. QR & order timers",
      "How long a payment QR stays valid and how long old orders are kept.",
      [
        row(["QR_EXPIRY_MINUTES", "QR expiry (minutes)", "Default 10", "text", ""], v.QR_EXPIRY_MINUTES),
        row(["ORDER_TTL_HOURS", "Order keep time (hours)", "Default 48", "text", ""], v.ORDER_TTL_HOURS),
      ].join(""));

    const pool = section("5. SMS pool",
      "Bank SMS messages that wait to be matched with a payment.",
      [
        row(["SMS_POOL_TTL_HOURS", "SMS pool keep time (hours)", "Default 5", "text", ""], v.SMS_POOL_TTL_HOURS),
      ].join(""));

    const appStatus = c.app || {};
    const appLine = appStatus.online
      ? `<p class="sub-h" style="color:var(--ok)">✅ App connected: ${esc(appStatus.device || "Android")} · last seen ${esc(appStatus.last_seen)}</p>`
      : appStatus.connected_ever
        ? `<p class="sub-h" style="color:var(--warn)">⚪ App is offline. Last seen ${esc(appStatus.last_seen)}</p>`
        : `<p class="sub-h">⚪ App not connected yet. Paste this link in the app and tap Save.</p>`;
    const appUpd = section("6. App updates",
      "Put the Android APK in your GitHub repo's Releases (tag like v1.1). The app is notified automatically.",
      [
        row(["APP_GITHUB_REPO", "GitHub repo", "Format owner/repo, for example ravi/now-app", "text", "mono"], v.APP_GITHUB_REPO),
        row(["APP_GITHUB_TOKEN", "GitHub token (only for a private repo)", "Leave empty for a public repo. Shown masked.", "password", "mono"], v.APP_GITHUB_TOKEN),
      ].join(""));
    const hook = `<div class="card"><h3>7. SMS webhook</h3>
        <p class="sub-h">Paste the link below into your SMS forwarding app. Change the secret and the link updates by itself.</p>
        ${appLine}
        <div class="field"><label>Webhook secret</label>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <input data-cfg="SMS_HOOK_SECRET" id="wh-secret" value="${esc(v.SMS_HOOK_SECRET)}" autocomplete="off" spellcheck="false" class="mono" style="flex:1;min-width:200px">
            <button class="btn" data-act="gen-secret" type="button">Generate</button></div>
          <div class="hint">Leave empty and it is created automatically on Save. Use letters, numbers, - or _ (at least 12).</div></div>
        <div class="field" style="margin-top:14px"><label>Webhook URL (copy this)</label>
          <div style="display:flex;gap:8px"><input readonly id="wh-url" class="mono" style="font-size:12px" value="">
            <button class="btn" data-act="copy-webhook" type="button" style="white-space:nowrap">Copy</button></div>
          <div class="hint">Use your https:// address, not the http one. The link works after you Save.</div></div>
      </div>`;

    $("#list").innerHTML = `
      ${codeCard}
      <div class="card"><h3>Bot status</h3><p class="sub-h">${status}</p>
        <p class="sub-h" style="margin:0">Fields marked * are required. After you save, the bot restarts by itself in a few seconds.</p></div>
      ${mongo}${bot}${payment}${timers}${pool}${appUpd}${hook}
      <div style="display:flex;justify-content:flex-end;margin:4px 0 24px"><button class="btn primary" data-act="save-config">Save and restart bot</button></div>`;

    // Webhook link: builds itself as you type the secret
    const base = c.webhook_base || "";
    const sec = document.getElementById("wh-secret"), out = document.getElementById("wh-url");
    const refreshUrl = () => {
      const s = sec.value.trim();
      out.value = s ? base + s : "Generate दबाएं या secret डालें";
    };
    sec.oninput = refreshUrl;
    refreshUrl();
  },

  settings: c => {
    const opt = (cur, list) => list.map(([v, l]) => `<option value="${v}" ${Number(cur) === v ? "selected" : ""}>${l}</option>`).join("");
    const tgl = (key, label, desc, on) => `<div class="toggle-row"><div class="lbl">${label}<small>${desc}</small></div><input type="checkbox" data-setting="${key}" ${on ? "checked" : ""}></div>`;
    $("#list").innerHTML = `
      <div class="card"><h3>Maintenance mode</h3><p class="sub-h">Turn on to pause the bot for customers. Admins can still use it.</p>
        ${tgl("maintenance", "Maintenance", "Customers see a maintenance message", c.maintenance)}</div>
      <div class="card"><h3>Course auto-delete</h3><p class="sub-h">Content a customer opens is removed after this time.</p>
        <div class="field"><select data-setting="course_timer_minutes">${opt(c.course_timer_minutes, [[0,"Off"],[5,"5 minutes"],[10,"10 minutes"],[20,"20 minutes"],[30,"30 minutes"]])}</select></div></div>
      <div class="card"><h3>First order amount</h3><p class="sub-h">ON: the first order gets the exact price (₹100), then ₹100.01, ₹100.02 and so on. OFF: the first order gets ₹100.01.</p>
        ${tgl("first_exact_amount", "Start from the exact price", "Applies to new orders from now on", c.first_exact_amount)}</div>
      <div class="card"><h3>Order amount hold</h3><p class="sub-h">Paid or cancelled orders free their amount at once. An expired order keeps its amount this long, so a late payment can still be matched.</p>
        <div class="field"><select data-setting="order_hold_hours">${opt(c.order_hold_hours, [[6,"6 hours"],[12,"12 hours (default)"],[24,"24 hours"],[48,"48 hours"]])}</select></div></div>
      <div class="card"><h3>Chat auto-clear</h3><p class="sub-h">Clean up chats after a period of inactivity.</p>
        <div class="field"><select data-setting="cleanup_seconds">${opt(c.cleanup_seconds, [[0,"Off"],[3600,"1 hour"],[43200,"12 hours"],[86400,"24 hours"],[172800,"48 hours"]])}</select></div>
        <div style="margin-top:12px">
          ${tgl("auto_del_promos", "Remove promos and menus", "Recommended", c.auto_del_promos)}
          ${tgl("auto_del_broadcasts", "Remove broadcast messages", "Recommended", c.auto_del_broadcasts)}
          ${tgl("auto_del_purchases", "Remove purchased links", "Customers lose old links from chat", c.auto_del_purchases)}
        </div></div>
      <div class="card"><h3>Content protection</h3><p class="sub-h">Stops customers from forwarding or saving course content.</p>
        ${tgl("protect_content", "Protect course content", "Works right away, no restart needed", c.protect_content)}</div>
      <div class="card"><h3>Clear chats now</h3><p class="sub-h">Removes promos and broadcasts from all chats immediately.</p>
        <button class="btn danger" data-act="force-clear">${svg("trash")}Clear now</button></div>`;
  },
};

/* ============ search ============ */
function renderSearch(list){
  if(!list.length){ $("#list").innerHTML = empty("No user found for this ID or username."); return; }
  $("#list").innerHTML = list.map(u => {
    const chips = (u.channels || []).length
      ? u.channels.map(n => `<span class="pill">${esc(n)}</span>`).join("")
      : `<span class="muted" style="font-size:13px">Not joined to any channel yet</span>`;
    const rows = u.purchases.length
      ? u.purchases.map(p => `<div class="purch"><div class="grow">
          <div class="t">${esc(p.channel_name || p.title)}</div>
          <div class="m"><span class="pill">${esc(p.date || "no date")}</span>${p.channel_name ? `<span class="pill">${esc(p.title)}</span>` : ""}<span class="pill mono">${esc(p.course_id)}</span></div>
        </div>
        <span class="mono" style="white-space:nowrap;align-self:center">${p.rate != null ? money(p.rate) : "—"}</span>
        <button class="btn danger sm" data-act="rm-purchase" data-uid="${esc(u.user_id)}" data-cid="${esc(p.course_id)}">${svg("trash")}Remove</button></div>`).join("")
      : `<div class="muted" style="font-size:13px;margin-top:10px">No purchases yet.</div>`;
    return `<div class="user">
      <div class="user-head"><div><span class="name">@${esc(u.username || "none")}</span> <span class="mono dim">${esc(u.user_id)}</span></div>
        <span class="pill ${u.purchases.length ? "solid" : ""}">${u.purchases.length} purchase${u.purchases.length === 1 ? "" : "s"}</span></div>
      <div style="margin-top:14px"><div class="label-s">Channels joined</div><div class="chips">${chips}</div></div>
      <div style="margin-top:18px"><div class="label-s">Purchase history</div>${rows}</div>
      <div class="total-row"><span>Total spent</span><strong>${money(u.total)}</strong></div>
    </div>`;
  }).join("");
}

async function doSearch(q, push = true){
  q = (q || "").trim();
  if(!q) return;
  lastQuery = q;
  if(push) pushState("#search=" + encodeURIComponent(q));
  view = "search";
  closeAll();
  highlightNav(null);
  $("#page-title").textContent = "Search";
  $("#page-sub").textContent = "Results for “" + q + "”";
  $("#body").innerHTML = `<div id="list"></div>`;
  setListLoading();
  try{
    const d = await api("/dashboard/api/user-lookup?q=" + enc(q));
    if(view === "search") renderSearch(d);
  }catch(e){
    if(view === "search") $("#list").innerHTML = empty(e.message);
  }
}

/* ============ history: Back button and browser Back both work ============ */
const pushState = hash => { if(location.hash !== hash) history.pushState({h: hash}, "", hash); };
function routeFromHash(){
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  if(h.startsWith("search=")){ doSearch(h.slice(7), false); return; }
  go(TITLES[h] ? h : "home", false);
}
window.addEventListener("popstate", routeFromHash);

function go(v, push = true){
  view = v;
  if(push) pushState("#" + v);
  closeAll();
  highlightNav(v);
  $("#page-title").textContent = TITLES[v];
  $("#page-sub").textContent = SUBS[v] || "";
  $("#body").innerHTML = SHELL[v]();
  loadView();
  if(v === "home") loadPoll();
}

/* ============ actions ============ */
const fadeRow = el => { const r = el && el.closest(".row, .purch"); if(r) r.classList.add("removing"); };
const refresh = () => loadView(true);

function openMenuAdd(keep){
  const v = keep || {};
  openModal("Add a bottom button", `
      <div class="field"><label>1. Button name (with emoji)</label><input id="mb-name" placeholder="📋 Help" value="${esc(v.name||"")}"></div>
      <div class="field"><label>2. Text the bot sends when tapped</label><textarea id="mb-value" placeholder="Type the message. Bold works with &lt;b&gt;bold&lt;/b&gt;.">${esc(v.text||"")}</textarea></div>
      <div class="field"><label>3. Link buttons (optional)</label><textarea id="mb-links" placeholder="Join Channel - https://t.me/yourchannel" style="min-height:70px">${esc(v.links||"")}</textarea></div>
      <p class="sub-h">One per line: <b>Button text - https://link</b>. Several lines = several buttons under the message. Leave empty for text only.</p>`,
      {foot: `<button class="btn ghost" onclick="closeModal()">Cancel</button><button class="btn" data-act="menu-save-new" data-again="1">Save &amp; add another</button><button class="btn primary" data-act="menu-save-new">Save</button>`});
}
async function saveMenuButton(again){
  const name = $("#mb-name").value.trim(), text = $("#mb-value").value.trim(), raw = $("#mb-links").value;
  const keep = {name: $("#mb-name").value, text: $("#mb-value").value, links: raw};
  if(!name) return toast("Enter a button name", "err");
  if(!text) return toast("Enter the text the bot should send", "err");
  const links = [];
  for(const line of raw.split("\n").map(x => x.trim()).filter(Boolean)){
    const i = line.lastIndexOf(" - ");
    const lt = i > 0 ? line.slice(0, i).trim() : "", lu = i > 0 ? line.slice(i + 3).trim() : "";
    if(!lt || !lu.startsWith("https://")) return toast("Each link line: Text - https://link", "err");
    links.push({text: lt, url: lu});
  }
  if(links.length > 8) return toast("Maximum 8 link buttons per message", "err");
  const list = ((cache.menu && cache.menu.buttons) || []).concat([{text: name, action: "message", value: text, links}]);
  try{
    await api("/dashboard/api/menu-config", {method: "POST", body: {buttons: list}});
  }catch(e){ return toast(e.message || "Could not save", "err"); }
  delete cache.menu; loadView(true);
  if(again){ toast("Saved. Add the next one"); openMenuAdd(); }
  else { closeModal(); toast("Button added"); }
}

const ACTIONS = {
  "approve": async el => {
    const ok = await confirmBox({title: "Approve this payment?", text: "The customer will receive the course immediately.", okText: "Approve", danger: false});
    if(!ok) return;
    fadeRow(el);
    await api(`/dashboard/api/orders/${enc(el.dataset.id)}/approve`, {method: "POST"});
    toast("Order approved"); refresh();
  },
  "cancel": async el => {
    const ok = await confirmBox({title: "Cancel this order?", text: "The customer will be notified that the order was cancelled.", okText: "Cancel order"});
    if(!ok) return;
    fadeRow(el);
    await api(`/dashboard/api/orders/${enc(el.dataset.id)}/cancel`, {method: "POST"});
    toast("Order cancelled"); refresh();
  },
  "clear-open": async () => {
    const ok = await confirmBox({title: "Clear all open orders?", text: "Every PENDING and EXPIRED order will be cancelled, so their amounts become free again. Completed orders are not touched and nothing is deleted.", okText: "Clear all"});
    if(!ok) return;
    const r = await api("/dashboard/api/orders/clear-open", {method: "POST"});
    toast("Cleared " + (r.cleared || 0) + " open orders"); refresh();
  },
  "buyers": el => openBuyers(el.dataset.id, el.dataset.title),
  "del-course": async el => {
    const ok = await confirmBox({title: "Delete this course?", text: "This pack will be removed from the store. Existing buyers keep their access.", okText: "Delete"});
    if(!ok) return;
    fadeRow(el);
    await api(`/dashboard/api/courses/${enc(el.dataset.id)}`, {method: "DELETE"});
    toast("Course deleted"); refresh();
  },
  "save-price": async el => {
    const amt = el.closest(".row").querySelector(".price-in").value.trim();
    if(!amt || +amt <= 0) return toast("Enter a price above 0", "err");
    await api(`/dashboard/api/courses/${enc(el.dataset.id)}/price`, {method: "POST", body: {amount: amt}});
    toast("Price updated"); delete cache.courses; loadView(true);
  },
  "ban-add": async () => {
    const target = $("#ban-target").value.trim();
    if(!target) return toast("Enter a user ID or @username", "err");
    const h = $("#ban-hours").value;
    const ok = await confirmBox({title: "Ban this user?", text: "They will not be able to use the bot or join channels until the ban ends.", okText: "Ban user"});
    if(!ok) return;
    await api("/dashboard/api/bans", {method: "POST", body: {target, hours: h === "perm" ? null : +h}});
    toast("User banned"); refresh();
  },
  "unban": async el => {
    const ok = await confirmBox({title: "Unban this user?", text: "They can use the bot and join channels again.", okText: "Unban", danger: false});
    if(!ok) return;
    fadeRow(el);
    await api("/dashboard/api/bans/remove", {method: "POST", body: {target: el.dataset.target}});
    toast("User unbanned"); refresh();
  },
  "save-delivery": async () => {
    const parseBtns = txt => txt.split("\n").map(l => l.trim()).filter(Boolean).map(l => {
      const p = l.split(" - ").map(s => s.trim());
      if(p.length >= 3 && ["green", "red", "blue"].includes(p[p.length - 1].toLowerCase())) p.pop();
      return {text: p.slice(0, -1).join(" - "), url: p[p.length - 1] || ""};
    });
    const body = {
      header: $("#dl-header").value, between: $("#dl-between").value, body: $("#dl-body").value,
      main_btn: {enabled: $("#dl-main-on").checked, text: $("#dl-main-text").value},
      buttons: parseBtns($("#dl-buttons").value),
      received_text: $("#dl-recv").value,
      received_btn: {enabled: $("#dl-recv-on").checked, text: $("#dl-recv-text").value},
    };
    await api("/dashboard/api/success-msg", {method: "POST", body});
    toast("Message saved"); delete cache.delivery; loadView(true);
  },
  "reset-delivery": async () => {
    const ok = await confirmBox({title: "Reset to default?", text: "The post-purchase message goes back to the standard text and buttons.", okText: "Reset"});
    if(!ok) return;
    await api("/dashboard/api/success-msg", {method: "POST", body: {reset: true}});
    toast("Reset done"); delete cache.delivery; loadView(true);
  },
  "menu-add": () => openMenuAdd(),
  "menu-save-new": async el => { await saveMenuButton(el.dataset.again === "1"); },
  "menu-del": async el => {
    const ok = await confirmBox({title: "Delete this button?", text: "Customers will no longer see it in the bot menu.", okText: "Delete"});
    if(!ok) return;
    fadeRow(el);
    const list = ((cache.menu && cache.menu.buttons) || []).filter((_, i) => i !== +el.dataset.idx);
    await api("/dashboard/api/menu-config", {method: "POST", body: {buttons: list}});
    toast("Button deleted"); delete cache.menu; loadView(true);
  },
  "save-menu": async () => {
    const body = {
      footer: $("#menu-footer").value,
      live: {enabled: $("#live-on").checked, channel_id: $("#live-channel").value.trim(),
             share_text: $("#live-share-text").value, share_link: $("#live-share-link").value.trim()},
    };
    const r = await api("/dashboard/api/menu-config", {method: "POST", body});
    toast(r && r.backfill === "started" ? "Saved. Past purchases are being sent to the feed." : "Saved");
    delete cache.menu; loadView(true);
  },
  "backfill-feed": async () => {
    const ok = await confirmBox({title: "Send past purchases to the feed?", text: "Every purchase that has not been posted yet goes to the feed channel, one by one. You can stop it any time.", okText: "Start", danger: false});
    if(!ok) return;
    await api("/dashboard/api/live-feed/backfill", {method: "POST"});
    toast("Sending started"); feedPolling = false; pollFeedJob();
  },
  "stop-feed": async () => {
    await api("/dashboard/api/live-feed/stop", {method: "POST"});
    toast("Stopping after the current message"); feedPolling = false; pollFeedJob();
  },
  "create-offer": async () => {
    const max = +$("#off-max").value, pu = +$("#off-peruser").value;
    await api("/dashboard/api/offers", {method: "POST", body: {
      discount: $("#off-disc").value, target_type: $("#off-tgt").value, course_id: $("#off-cid").value.trim(),
      max_users: max === 0 ? -1 : max, per_user_limit: pu === 0 ? -1 : pu, hours: $("#off-hrs").value,
    }});
    toast("Offer created"); loadView(true);
  },
  "del-offer": async el => {
    const ok = await confirmBox({title: "Delete this offer?", text: "The link will stop working immediately.", okText: "Delete"});
    if(!ok) return;
    fadeRow(el);
    await api(`/dashboard/api/offers/${enc(el.dataset.id)}`, {method: "DELETE"});
    toast("Offer deleted"); refresh();
  },
  "inject": async () => {
    const amt = $("#inj-amt").value.trim();
    if(!amt) throw new Error("Enter an amount");
    const d = await api("/dashboard/api/inject-sms", {method: "POST", body: {amount: amt}});
    toast("Result: " + d.result); loadView(true);
  },
  "send-broadcast": async () => {
    const msg = $("#bc-msg").value.trim();
    if(!msg) throw new Error("Message is empty");
    const ok = await confirmBox({title: "Send to all users?", text: "This message will be delivered to every user of the bot.", okText: "Send", danger: false});
    if(!ok) return;
    await api("/dashboard/api/broadcast", {method: "POST", body: {message: msg}});
    $("#bc-msg").value = "";
    toast("Broadcast started");
    loadView(true);
    setTimeout(() => { if(view === "broadcast") loadView(true); }, 6000);
  },
  "rm-broadcast": async el => {
    const ok = await confirmBox({title: "Remove this broadcast?", text: "It will be deleted from every user's chat. This cannot be undone.", okText: "Remove"});
    if(!ok) return;
    await api(`/dashboard/api/broadcasts/${enc(el.dataset.id)}`, {method: "DELETE"});
    toast("Removing from chats…"); loadView(true);
  },
  "rm-purchase": async el => {
    const ok = await confirmBox({title: "Remove this purchase?", text: "The customer will lose the ability to join this channel. Members already inside stay in the channel.", okText: "Remove"});
    if(!ok) return;
    await api("/dashboard/api/purchases", {method: "DELETE", body: {user_id: el.dataset.uid, course_id: el.dataset.cid}});
    toast("Purchase removed");
    const row = el.closest(".purch"); if(row) row.remove();
    if(view === "search") doSearch(lastQuery); else loadView(true);
  },
  "force-clear": async () => {
    const ok = await confirmBox({title: "Clear chats now?", text: "Promos and broadcasts will be removed from all user chats.", okText: "Clear"});
    if(!ok) return;
    await api("/dashboard/api/force-clear-chats", {method: "POST", body: {del_promos: true, del_broadcasts: true, del_purchases: false}});
    toast("Clearing started");
  },
  "save-config": async () => {
    const body = {};
    document.querySelectorAll("[data-cfg]").forEach(i => { body[i.dataset.cfg] = i.value; });
    const r = await api("/dashboard/api/config", {method: "POST", body});
    const f = r.found || {};
    toast(`Old data found: ${f.courses||0} courses, ${f.users||0} users, ${f.purchases||0} purchases, ${f.orders||0} orders. Bot is restarting.`);
    setTimeout(() => location.reload(), 6000);
  },
  "copy-webhook": async el => {
    const v = el.closest(".field").querySelector("input").value;
    try { await navigator.clipboard.writeText(v); toast("Webhook URL copied"); }
    catch(e) { toast("Copy failed, select the text and copy it manually", "err"); }
  },
  "open-setup": () => { closeMenu(); go("setup"); },
  "gen-secret": () => {
    const a = new Uint8Array(24);
    crypto.getRandomValues(a);
    const s = btoa(String.fromCharCode(...a)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const inp = document.getElementById("wh-secret");
    inp.value = s;
    inp.dispatchEvent(new Event("input"));
    toast("New secret generated. Save to use it.");
  },
  "update-code": async () => {
    closeMenu();
    const ok = await confirmBox({title: "Update code from GitHub?",
      text: "The server will run git pull and restart the bot. Takes about 10 to 30 seconds.", okText: "Update", danger: false});
    if(!ok) return;
    try {
      const r = await api("/dashboard/api/update-code", {method: "POST"});
      toast(r.message || "Done");
      if(r.status === "updated") setTimeout(() => location.reload(), 10000);
    } catch(e) {
      toast(e.message, "err");
    }
  },
  "open-appupdate": () => { closeMenu(); go("appupdate"); },
  "check-app-update": async () => {
    try {
      await api("/dashboard/api/app-update?check=1");
      toast("Checked GitHub");
    } catch(e) {
      toast(e.message, "err");
    }
    loadView(true);
  },
  "logout": async () => {
    closeMenu();
    try { await fetch("/dashboard/logout", {method: "POST"}); } catch(e) {}
    location.reload();
  },
  "open-admins": () => { closeMenu(); openAdmins(); },
  "refresh": () => {
    closeMenu();
    if(view === "search") doSearch(lastQuery); else loadView();
    toast("Refreshed");
  },
  "close-menu": () => closeMenu(),
  "close-modal": () => { pendingConfirm && settleConfirm(false); closeModal(); },
  "cancel-confirm": () => settleConfirm(false),
  "ok-confirm": () => settleConfirm(true),
  "add-admin": async () => {
    const v = $("#adm-id").value.trim();
    if(!v) throw new Error("Enter a Telegram user ID");
    await api("/dashboard/api/admins", {method: "POST", body: {user_id: v}});
    $("#adm-id").value = "";
    toast("Admin added"); loadAdmins();
  },
  "del-admin": async el => {
    const ok = await confirmBox({title: "Remove this admin?", text: "They will lose access to the admin panel immediately.", okText: "Remove"});
    if(!ok) return;
    await api(`/dashboard/api/admins/${enc(el.dataset.id)}`, {method: "DELETE"});
    toast("Admin removed"); loadAdmins();
  },
};

document.addEventListener("click", async e => {
  const nav = e.target.closest("[data-view]");
  if(nav){ go(nav.dataset.view); return; }

  const seg = e.target.closest("[data-of]");
  if(seg){
    orderFilter = seg.dataset.of;
    document.querySelectorAll("[data-of]").forEach(b => b.classList.toggle("active", b === seg));
    loadView();
    return;
  }

  const act = e.target.closest("[data-act]");
  if(!act) return;
  const fn = ACTIONS[act.dataset.act];
  if(!fn) return;
  try { await fn(act); }
  catch(err){ toast(err.message, "err"); }
});

/* settings save instantly on change */
document.addEventListener("change", async e => {
  const el = e.target.closest("[data-setting]");
  if(!el) return;
  const get = k => { const x = document.querySelector(`[data-setting="${k}"]`); return x ? (x.type === "checkbox" ? x.checked : x.value) : undefined; };
  try{
    await api("/dashboard/api/system-settings", {method: "POST", body: {
      maintenance: get("maintenance"), cleanup_seconds: get("cleanup_seconds"),
      auto_del_promos: get("auto_del_promos"), auto_del_broadcasts: get("auto_del_broadcasts"),
      auto_del_purchases: get("auto_del_purchases"), protect_content: get("protect_content"), course_timer_minutes: get("course_timer_minutes"), order_hold_hours: get("order_hold_hours"), first_exact_amount: get("first_exact_amount"),
    }});
    delete cache.settings;
    toast("Settings saved");
  }catch(err){ toast(err.message, "err"); }
});

/* ============ modal ============ */
function openModal(title, html, opts = {}){
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  const foot = $("#modal-foot");
  foot.innerHTML = opts.foot || "";
  foot.style.display = opts.foot ? "flex" : "none";
  $("#modal-box").classList.toggle("small", !!opts.small);
  // confirm popup has no header title; hide the empty header
  $("#modal-title").parentElement.style.display = title ? "flex" : "none";
  $("#modal").classList.add("open");
}
function closeModal(){ $("#modal").classList.remove("open"); }

function openAdmins(){
  openModal("Admins", `
    <div class="field"><label>Add an admin by Telegram user ID</label>
      <div style="display:flex;gap:8px"><input id="adm-id" placeholder="e.g. 123456789" inputmode="numeric">
      <button class="btn primary" data-act="add-admin" style="white-space:nowrap">Add admin</button></div>
      <div class="hint">The person must send /start to the bot once before they can open the panel.</div></div>
    <div id="adm-list" style="margin-top:18px"><div class="empty"><span class="spinner"></span></div></div>`);
  loadAdmins();
}
async function loadAdmins(){
  try{
    const d = await api("/dashboard/api/admins");
    $("#adm-list").innerHTML =
      `<div class="row"><div class="grow"><div class="title">Owner</div><div class="meta mono">${esc(d.owner_id)}</div></div><span class="pill solid">Permanent</span></div>` +
      (d.admins.length ? d.admins.map(a => `<div class="row">
          <div class="grow"><div class="title">Admin</div><div class="meta mono">${esc(a.user_id)} · added ${esc(a.added_at)}</div></div>
          <button class="btn danger sm" data-act="del-admin" data-id="${esc(a.user_id)}">${svg("trash")}Remove</button></div>`).join("")
        : empty("No additional admins yet."));
  }catch(e){ $("#adm-list").innerHTML = empty(e.message); }
}

async function openBuyers(id, title){
  openModal("Buyers · " + title, `<div class="empty"><span class="spinner"></span></div>`);
  try{
    const d = await api(`/dashboard/api/courses/${enc(id)}/buyers`);
    $("#modal-body").innerHTML = d.buyers.length ? d.buyers.map(b => `<div class="row">
        <div class="grow"><div class="title">${esc(b.user)}</div><div class="meta mono">${esc(b.user_id)} · ${esc(b.purchased_at || "")}</div></div>
        <div class="right"><div class="amount">₹${esc(b.amount)}</div>
          <div class="end"><span class="pill">${esc(b.method || "")}</span>
          ${d.is_channel ? `<span class="pill ${b.channel_joined ? "ok" : "warn"}">${b.channel_joined ? "Joined" : "Not joined"}</span>` : ""}</div></div></div>`).join("")
      : empty("No buyers yet.");
  }catch(e){ $("#modal-body").innerHTML = empty(e.message); }
}

/* ============ top bar + side panel ============ */
const openDrawer = () => { $("#drawer").classList.add("open"); $("#overlay").classList.add("show"); };
const closeDrawer = () => { $("#drawer").classList.remove("open"); $("#overlay").classList.remove("show"); };
const closeMenu = () => $("#profile-menu").classList.remove("open");
const closeAll = () => { closeDrawer(); closeMenu(); };

$("#btn-menu").onclick = openDrawer;
$("#btn-back").onclick = () => history.back();
$("#overlay").onclick = closeDrawer;
$("#btn-avatar").onclick = e => { e.stopPropagation(); $("#profile-menu").classList.toggle("open"); };
$("#search-form").onsubmit = e => { e.preventDefault(); doSearch($("#q").value); };
$("#modal").addEventListener("click", e => { if(e.target.id === "modal"){ if(pendingConfirm) settleConfirm(false); else closeModal(); } });

document.addEventListener("click", e => {
  if(!e.target.closest("#profile-menu") && !e.target.closest("#btn-avatar")) closeMenu();
});
document.addEventListener("keydown", e => {
  if(e.key === "Escape"){ if(pendingConfirm) settleConfirm(false); closeAll(); closeModal(); }
  const tag = document.activeElement.tagName;
  if(e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA"){ e.preventDefault(); $("#q").focus(); }
});

/* ============ timers (kept light to save server cost) ============ */
setInterval(() => {
  $("#clock").textContent = new Date().toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit"});
  document.querySelectorAll("[data-remain]").forEach(el => {
    const s = Math.max(0, (+el.dataset.remain) - 1);
    el.dataset.remain = s;
    el.textContent = fmtSecs(s) + " left";
  });
}, 1000);
// Only Dashboard and Orders auto-refresh, every 30 s, and never while the tab is hidden.
setInterval(() => {
  if(!document.hidden && (view === "home" || view === "orders")) loadView(true);
}, 30000);

/* ============ app connection icon (top bar) ============ */
async function refreshConn(){
  try{
    const s = await api("/dashboard/api/app-status");
    const el = $("#conn");
    el.classList.toggle("on", !!s.online);
    let text = s.online ? "App online" : (s.connected_ever ? "App offline" : "App not connected");
    if(s.online && s.pending) text += " · " + s.pending + " pending";
    $("#conn-text").textContent = text;
    el.title = s.connected_ever ? ("Device: " + (s.device || "—") + " · Last seen: " + s.last_seen) : "App has not connected yet";
  }catch(e){}
}
refreshConn();
setInterval(() => { if(!document.hidden) refreshConn(); }, 30000);

buildNav();
api("/dashboard/api/config")
  .then(c => { if(location.hash) routeFromHash(); else go(c.configured ? "home" : "setup", false); })
  .catch(() => routeFromHash());
</script>
</body>
</html>
"""  # Dashboard की पूरी screen (ये bot.py के अंदर ही है, अलग file की ज़रूरत नहीं)

@app.route("/dashboard/api/admins", methods=["GET", "POST"])
@require_auth
def api_admins():
    """Dashboard से Telegram ID डालो → वो user admin बन जाता है (Owner ENV वाला ही रहेगा)"""
    if request.method == "POST":
        raw = str((request.json or {}).get("user_id", "")).strip()
        if not re.fullmatch(r"\d{5,15}", raw):
            return jsonify({"error": "Enter a valid numeric Telegram ID (e.g. 123456789)"}), 400
        uid = int(raw)
        if uid == ADMIN_ID:
            return jsonify({"error": "This ID is the owner and is already an admin."}), 400
        admins_col.update_one({"user_id": uid}, {"$set": {"user_id": uid, "added_at": get_ist_time(), "added_by": "dashboard"}}, upsert=True)
        refresh_admin_cache()
        try: orig_send_message(uid, "🎉 <b>You are now an admin of this bot!</b>\nSend /start to open the Admin Panel.", parse_mode="HTML")
        except Exception: pass
        return jsonify({"status": "success"})

    out = [{"user_id": a.get("user_id"), "added_at": a.get("added_at", "")} for a in admins_col.find().sort("_id", 1)]
    return jsonify({"owner_id": ADMIN_ID, "admins": out})

@app.route("/dashboard/api/admins/<int:uid>", methods=["DELETE"])
@require_auth
def api_delete_admin(uid):
    if uid == ADMIN_ID: return jsonify({"error": "The owner cannot be removed"}), 400
    admins_col.delete_one({"user_id": uid})
    refresh_admin_cache()
    return jsonify({"status": "success"})

def _mask(v):
    v = str(v or "")
    if len(v) <= 10: return "•" * len(v)
    return v[:6] + "••••••" + v[-4:]

@app.before_request
def _gate_until_setup():
    """Setup पूरा न होने तक बाकी routes बंद रहते हैं (सिर्फ़ dashboard page और config खुले रहते हैं)"""
    if BOT_CONFIGURED: return None
    p = request.path
    if p in ("/dashboard", "/dashboard/login", "/dashboard/logout", "/dashboard/icon.svg", "/dashboard/manifest.json") or p.startswith("/dashboard/api/config"):
        return None
    if p.startswith("/dashboard"):
        return jsonify({"error": "Setup is not complete yet. Open 'Add Bot / Setup' and save.", "setup_required": True}), 503
    if p.startswith("/sms-webhook/"):
        return "Setup pending", 503
    return None

def _boot_public():
    """Dashboard में दिखने वाली boot values: MongoDB link masked रहता है, password कभी नहीं दिखता"""
    return {
        "MONGO_URI": _mask(MONGO_URI),
        "MONGO_DB_NAME": MONGO_DB_NAME,
        "DASHBOARD_USERNAME": DASHBOARD_USERNAME,
        "DASHBOARD_PASSWORD": "",
    }

def _check_mongo(uri, name):
    """नए MongoDB link को जाँचता है। ठीक हो तो उस database का object लौटाता है।"""
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    return client.get_database(name)

@app.route("/dashboard/api/config", methods=["GET", "POST"])
@require_auth
def api_config():
    if request.method == "GET":
        if not DASHBOARD_PASSWORD:
            return jsonify({"configured": False, "first_run": True, "webhook_url": "",
                            "webhook_base": request.host_url.rstrip("/") + "/sms-webhook/",
                            "values": dict(BOT_CONFIG_KEYS), "boot": _boot_public()})
        cfg = load_bot_config()
        secret = cfg.get("SMS_HOOK_SECRET", "")
        return jsonify({
            "configured": BOT_CONFIGURED,
            "first_run": False,
            "webhook_url": (request.host_url.rstrip("/") + "/sms-webhook/" + secret) if secret else "",
            "webhook_base": request.host_url.rstrip("/") + "/sms-webhook/",
            "values": {
                "BOT_TOKEN": _mask(cfg.get("BOT_TOKEN", "")),
                "ADMIN_ID": cfg.get("ADMIN_ID", ""),
                "DB_CHANNEL_ID": cfg.get("DB_CHANNEL_ID", ""),
                "UPI_ID": cfg.get("UPI_ID", ""),
                "MERCHANT_NAME": cfg.get("MERCHANT_NAME", ""),
                "SMS_HOOK_SECRET": secret,
                "CHAT_LINK": cfg.get("CHAT_LINK", ""),
                "INTERNATIONAL_LINK": cfg.get("INTERNATIONAL_LINK", ""),
                "QR_EXPIRY_MINUTES": cfg.get("QR_EXPIRY_MINUTES", "10"),
                "STALE_ORDER_HOURS": cfg.get("STALE_ORDER_HOURS", "24"),
                "SMS_POOL_TTL_HOURS": cfg.get("SMS_POOL_TTL_HOURS", "5"),
                "ORDER_TTL_HOURS": cfg.get("ORDER_TTL_HOURS", "48"),
                "APP_GITHUB_REPO": cfg.get("APP_GITHUB_REPO", ""),
                "APP_GITHUB_TOKEN": _mask(cfg.get("APP_GITHUB_TOKEN", "")),
            },
            "boot": _boot_public(),
            "app": _app_status_public(),
        })

    d = request.get_json(silent=True) or {}
    if not DASHBOARD_PASSWORD and str(d.get("SETUP_CODE", "")).strip() != SETUP_CODE:
        return jsonify({"error": "Setup code गलत है। Terminal में दिखा 6-digit code डालें।"}), 403

    # ---- 1. MongoDB + dashboard login (boot file में जाते हैं) ----
    boot_new = {}
    uri = str(d.get("MONGO_URI") or "").strip()
    if uri and "•" not in uri:
        if not re.match(r"^mongodb(\+srv)?://\S+$", uri):
            return jsonify({"error": "MongoDB URL 'mongodb://' या 'mongodb+srv://' से शुरू होना चाहिए।"}), 400
    else:
        uri = MONGO_URI
    if not uri:
        return jsonify({"error": "MongoDB URL भरें।"}), 400
    boot_new["MONGO_URI"] = uri  # हमेशा सेव, ताकि boot file अपने-आप पूरी रहे

    name = str(d.get("MONGO_DB_NAME") or "").strip() or MONGO_DB_NAME
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,63}", name):
        return jsonify({"error": "Database name में सिर्फ़ letters, numbers, - या _ हों।"}), 400
    boot_new["MONGO_DB_NAME"] = name

    uname = str(d.get("DASHBOARD_USERNAME") or "").strip() or DASHBOARD_USERNAME
    boot_new["DASHBOARD_USERNAME"] = uname

    pw = str(d.get("DASHBOARD_PASSWORD") or "")
    if pw:
        if len(pw) < 8 or pw == "changeme123":
            return jsonify({"error": "Dashboard password कम से कम 8 अक्षर का हो।"}), 400
        boot_new["DASHBOARD_PASSWORD"] = pw
    if not (pw or DASHBOARD_PASSWORD):
        return jsonify({"error": "Dashboard password भरें।"}), 400
    boot_new["DASHBOARD_PASSWORD"] = pw or DASHBOARD_PASSWORD

    try:
        target_db = _check_mongo(uri, name)
    except Exception as e:
        return jsonify({"error": "MongoDB से connect नहीं हो पा रहा। URL और password जाँचें, और Atlas में Network Access सही है या नहीं देखें. (" + str(e)[:160] + ")"}), 400

    # पुराना डेटा मिला या नहीं, यह पहले जाँचते हैं। कुछ न मिले तो कुछ भी सेव नहीं होता।
    try:
        found = {c: target_db[c].estimated_document_count() for c in ("courses", "users", "purchases", "orders")}
    except Exception as e:
        return jsonify({"error": "Database से डेटा नहीं पढ़ पा रहा: " + str(e)[:160] + ". कुछ सेव नहीं किया गया है।"}), 400
    if not any(found.values()):
        return jsonify({"error": f"इस database ('{name}') में पुराना डेटा नहीं मिला। Database नाम और URL जाँचें। कुछ सेव नहीं किया गया है।"}), 400

    # ---- 2. Bot settings (MongoDB के settings doc में जाती हैं) ----
    cur = load_bot_config(target_db)
    new = {}
    for k in BOT_CONFIG_KEYS:
        if k not in d: continue
        v = str(d.get(k) or "").strip()
        if k == "BOT_TOKEN" and (not v or "•" in v):
            # खाली या masked है: मौजूदा token (MongoDB या पुराने env से) MongoDB में भी सेव कर देते हैं
            if cur.get("BOT_TOKEN"):
                new["BOT_TOKEN"] = cur["BOT_TOKEN"]
            continue
        if k == "BOT_TOKEN" and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", v):
            return jsonify({"error": "Bot token looks wrong. Paste the full token from BotFather."}), 400
        if k == "APP_GITHUB_REPO" and v and not re.fullmatch(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", v):
            return jsonify({"error": "GitHub repo 'owner/repo' जैसा होना चाहिए, जैसे ravi/now-app"}), 400
        if k == "APP_GITHUB_TOKEN" and (not v or "•" in v):
            if cur.get("APP_GITHUB_TOKEN"):
                new["APP_GITHUB_TOKEN"] = cur["APP_GITHUB_TOKEN"]
            continue
        if k == "ADMIN_ID" and v and not re.fullmatch(r"\d{5,15}", v):
            return jsonify({"error": "Owner ID must be a numeric Telegram ID."}), 400
        if k == "DB_CHANNEL_ID" and v and not re.fullmatch(r"-?\d{5,20}", v):
            return jsonify({"error": "Channel ID must be a number (usually starts with -100)."}), 400
        if k == "SMS_HOOK_SECRET" and v and not re.fullmatch(r"[A-Za-z0-9_\-]{12,}", v):
            return jsonify({"error": "SMS secret: use 12+ letters, numbers, - or _ only."}), 400
        if k.endswith("_HOURS") or k.endswith("_MINUTES"):
            if v and not re.fullmatch(r"\d+(\.\d+)?", v):
                return jsonify({"error": f"{k} must be a number."}), 400
        new[k] = v

    merged = {**cur, **new}
    if not merged.get("SMS_HOOK_SECRET"):
        merged["SMS_HOOK_SECRET"] = secrets.token_urlsafe(24)
        new["SMS_HOOK_SECRET"] = merged["SMS_HOOK_SECRET"]
    missing = [k for k in ("BOT_TOKEN", "ADMIN_ID", "DB_CHANNEL_ID", "UPI_ID") if not merged.get(k)]
    if missing:
        return jsonify({"error": "Required: " + ", ".join(missing)}), 400

    new["updated_at"] = get_ist_time()
    target_db["settings"].update_one({"_id": "bot_config"}, {"$set": new}, upsert=True)
    save_boot(boot_new)

    # नई settings पूरी तरह लागू करने के लिए बोट अपने-आप restart होता है
    threading.Thread(target=_restart_later, daemon=True).start()
    return jsonify({"status": "success", "restarting": True, "found": found})

def _restart_later():
    """बोट को नए process के रूप में दोबारा शुरू करता है.
    नया process 4 सेकंड बाद शुरू होता है, तब तक पुराना process port छोड़कर पूरी तरह बंद हो चुका होता है."""
    time.sleep(2)
    print("🔄 Restarting bot with new settings...", flush=True)
    import shlex
    cmd = "sleep 4; exec " + " ".join(shlex.quote(a) for a in [sys.executable] + sys.argv)
    subprocess.Popen(["sh", "-c", cmd], cwd=APP_DIR, stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    os._exit(0)

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard login</title>
<style>
html,body{margin:0;height:100%;background:#000;color:#f2f2f2;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Helvetica,Arial,sans-serif}
.wrap{min-height:100%;display:grid;place-items:center;padding:24px;box-sizing:border-box}
.card{width:100%;max-width:380px;background:#0a0a0a;border:1px solid #1f1f1f;border-radius:18px;padding:32px 28px;box-shadow:0 30px 80px rgba(0,0,0,.8);box-sizing:border-box}
.logo{width:48px;height:48px;border-radius:14px;background:#fff;color:#000;display:grid;place-items:center;font-weight:800;font-size:22px;margin:0 auto 18px}
h1{font-size:20px;margin:0 0 6px;text-align:center;font-weight:600}
p.sub{margin:0 0 24px;color:#8a8a8a;font-size:13.5px;text-align:center}
label{display:block;font-size:12.5px;color:#8a8a8a;margin:14px 0 6px}
input{width:100%;box-sizing:border-box;background:#000;border:1px solid #2a2a2a;border-radius:12px;padding:12px 14px;color:#f2f2f2;font:inherit;font-size:15px;outline:none;transition:border-color .15s}
input:focus{border-color:#555}
button{margin-top:22px;width:100%;padding:12px;border:0;border-radius:12px;background:#fff;color:#000;font:inherit;font-weight:600;font-size:15px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.err{min-height:20px;color:#f87171;font-size:13px;text-align:center;margin-top:14px}
</style></head>
<body>
<div class="wrap">
<form class="card" id="f" autocomplete="on">
  <div class="logo">S</div>
  <h1>Dashboard login</h1>
  <p class="sub">Sign in to manage your store</p>
  <label for="u">Username</label>
  <input id="u" name="username" autocomplete="username" required>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  <button id="b" type="submit">Sign in</button>
  <div class="err" id="err"></div>
</form>
</div>
<script>
document.getElementById("f").onsubmit = async e => {
  e.preventDefault();
  const b = document.getElementById("b"), err = document.getElementById("err");
  b.disabled = true; err.textContent = "";
  try {
    const r = await fetch("/dashboard/login", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username: document.getElementById("u").value, password: document.getElementById("p").value})});
    const d = await r.json().catch(() => ({}));
    if (r.ok) { location.reload(); return; }
    err.textContent = d.error || "Login failed";
  } catch (x) { err.textContent = "Network error. Try again."; }
  b.disabled = false;
};
</script>
</body></html>
"""

@app.route("/dashboard/login", methods=["POST"])
def dashboard_login():
    ip = request.remote_addr or "?"
    if _login_blocked(ip):
        return jsonify({"error": "बहुत बार गलत कोशिश हुई। 15 मिनट बाद फिर कोशिश करें।"}), 429
    d = request.get_json(silent=True) or {}
    u = str(d.get("username") or "").strip()
    p = str(d.get("password") or "")
    if DASHBOARD_PASSWORD and hmac.compare_digest(u.encode(), DASHBOARD_USERNAME.encode()) \
            and hmac.compare_digest(p.encode(), DASHBOARD_PASSWORD.encode()):
        session.clear()
        session.permanent = True
        session["dash_ok"] = True
        _login_fail.pop(ip, None)
        return jsonify({"ok": True})
    _login_fail.setdefault(ip, []).append(time.time())
    return jsonify({"error": "Username या password गलत है।"}), 401

@app.route("/dashboard/logout", methods=["POST"])
def dashboard_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/dashboard/api/update-code", methods=["POST"])
@require_auth
def api_update_code():
    """GitHub से नया code लाता है (git pull) और ज़रूरत हो तो packages लगाकर बोट को restart करता है"""
    if not os.path.isdir(os.path.join(APP_DIR, ".git")):
        return jsonify({"error": "यह फोल्डर GitHub से clone किया हुआ नहीं है (.git नहीं मिला)। पहले git clone करें।"}), 400
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # password न माँगे, बस error दे
    def git(*args, timeout=120):
        return subprocess.run(["git", *args], cwd=APP_DIR, capture_output=True, text=True, timeout=timeout, env=env)
    try:
        before = git("rev-parse", "HEAD", timeout=30).stdout.strip()
        # VM पर bot.py में हाथ से किए गए बदलाव (local changes) pull को रोकते हैं.
        # इसलिए पहले पुरानी फ़ाइल का backup लेते हैं, फिर GitHub वाला code सीधे लेते हैं.
        fetch = git("fetch", "--quiet", timeout=120)
        if fetch.returncode != 0:
            return jsonify({"error": "GitHub से connect नहीं हो पाया। Internet या GitHub login जाँचें.",
                            "details": (fetch.stdout + fetch.stderr).strip()[-800:]}), 400
        try:
            import shutil
            shutil.copy2(os.path.join(APP_DIR, "bot.py"), os.path.join(APP_DIR, f"bot.py.bak_{int(time.time())}"))
        except Exception:
            pass
        pull = git("reset", "--hard", "@{u}", timeout=120)
        if pull.returncode != 0:
            return jsonify({"error": "Git pull नहीं हो पाया। Server पर कोई local बदलाव या GitHub login की दिक्कत हो सकती है.",
                            "details": (pull.stdout + pull.stderr).strip()[-800:]}), 400
        after = git("rev-parse", "HEAD", timeout=30).stdout.strip()
        if before == after:
            return jsonify({"status": "up_to_date", "message": "पहले से नवीनतम code है। कुछ नहीं बदला।"})
        changed = git("diff", "--name-only", before, after, timeout=30).stdout
        if "requirements.txt" in changed:
            pip = subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                                 cwd=APP_DIR, capture_output=True, text=True, timeout=600)
            if pip.returncode != 0:
                return jsonify({"error": "Code आ गया, पर packages नहीं लग पाए। Restart नहीं किया गया।",
                                "details": (pip.stdout + pip.stderr).strip()[-800:]}), 500
    except Exception as e:
        return jsonify({"error": "Update में दिक्कत आई: " + str(e)[:200]}), 500
    threading.Thread(target=_restart_later, daemon=True).start()
    return jsonify({"status": "updated", "message": "नया code आ गया। बोट restart हो रहा है, कुछ सेकंड में नया page खुलेगा।"})

@app.route("/dashboard")
def dashboard_page():
    # Login नहीं है तो अपना login page दिखता है (browser का popup नहीं)
    if not DASHBOARD_PASSWORD or _is_logged_in():
        return DASHBOARD_HTML
    return LOGIN_HTML

# ==========================================
# 🔄 GLOBAL THREADS
# ==========================================
def global_sms_checker():
    while True:
        try:
            time.sleep(10)
            pending_orders_list = list(orders_col.find({"status": "PENDING"}))
            for order in pending_orders_list:
                amt_key = order.get("amount")
                sms_rec = sms_pool_col.find_one({"amount": amt_key, "status": "UNUSED"})
                if sms_rec:
                    updated = sms_pool_col.update_one({"_id": sms_rec["_id"], "status": "UNUSED"}, {"$set": {"status": "PROCESSED"}})
                    if updated.modified_count > 0:
                        deliver_course_to_buyer(order, sms_text=sms_rec.get("raw_text"), is_manual=False)
        except Exception: pass

def global_memory_cleanup():
    while True:
        try:
            time.sleep(3600)
            now = time.time()
            keys_to_del = [oid for oid, o in all_orders_cache.items() if now - o.get("created_at", 0) > 86400]
            for k in keys_to_del: all_orders_cache.pop(k, None)
            cool_keys = [uid for uid, t in user_cooldowns.items() if now - t > 3600]
            for k in cool_keys: user_cooldowns.pop(k, None)
            gc.collect() 
        except Exception: pass

def restore_pending_orders():
    for order in orders_col.find({"status": "PENDING"}):
        order_id, amt_key, chat_id, created_ts = order["order_id"], order.get("amount"), order.get("chat_id"), order.get("created_at", 0)
        remaining = QR_EXPIRY_SECONDS - (time.time() - created_ts if created_ts else QR_EXPIRY_SECONDS)
        with pending_lock: pending_orders[amt_key], all_orders_cache[order_id] = order, order
        if remaining > 0:
            threading.Timer(remaining, expire_qr, args=(chat_id, order.get("qr_msg_id"), order["course_id"], amt_key, order_id)).start()
        else:
            threading.Thread(target=expire_qr, args=(chat_id, order.get("qr_msg_id"), order["course_id"], amt_key, order_id), daemon=True).start()

def start_polling():
    time.sleep(2)
    while True:
        try:
            bot.remove_webhook()
            bot.infinity_polling(timeout=20, long_polling_timeout=15, skip_pending=True, logger_level=None,
                                 allowed_updates=["message", "callback_query", "chat_join_request", "chat_member"])
        except Exception:
            time.sleep(5)

if __name__ == "__main__":
    if not DASHBOARD_PASSWORD:
        print(f"🔐 पहली बार setup: Dashboard में यह 6-digit code डालें → {SETUP_CODE}", flush=True)
    if BOT_CONFIGURED:
        # सारी settings पूरी हों तो ही बोट चलता है
        try: BOT_USERNAME = bot.get_me().username
        except Exception as e: print(f"⚠️ Bot token check failed: {e}")
        restore_pending_orders()
        threading.Thread(target=global_sms_checker, daemon=True).start()
        threading.Thread(target=global_memory_cleanup, daemon=True).start()
        threading.Thread(target=start_polling, daemon=True).start()
    else:
        print("🛠 Setup mode: Dashboard में 'Add Bot / Setup' खोलें और settings भरें।", flush=True)

    threading.Thread(target=load_sampler, daemon=True).start()
    # Dashboard हमेशा चलता है, ताकि settings वहीं से भरी जा सकें
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
