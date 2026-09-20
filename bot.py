import gc
import io
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedeltaimport gc
import io
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify, Response
from functools import wraps
from PIL import Image, ImageDraw
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
# 🛑 ENVIRONMENT VARIABLES
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "telegram_store_bot")

UPI_ID = os.environ.get("UPI_ID")
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "Store")
SMS_HOOK_SECRET = os.environ.get("SMS_HOOK_SECRET")

CHAT_LINK = os.environ.get("CHAT_LINK")
INTERNATIONAL_LINK = os.environ.get("INTERNATIONAL_LINK")

PROTECT_CONTENT = os.environ.get("PROTECT_CONTENT", "False").strip().lower() == "true"
QR_EXPIRY_SECONDS = int(os.environ.get("QR_EXPIRY_MINUTES", "10")) * 60
STALE_ORDER_SECONDS = int(os.environ.get("STALE_ORDER_HOURS", "24")) * 3600
SMS_POOL_TTL_HOURS = int(os.environ.get("SMS_POOL_TTL_HOURS", "5"))
ORDER_TTL_HOURS = int(os.environ.get("ORDER_TTL_HOURS", "48"))

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme123")

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID"))
    DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID"))
except (TypeError, ValueError):
    print("❌ ERROR: 'ADMIN_ID' or 'DB_CHANNEL_ID' Environment Variable is not set properly.")
    sys.exit(1)

if not BOT_TOKEN or not MONGO_URI or not UPI_ID or not SMS_HOOK_SECRET:
    print("❌ ERROR: Required Environment Variables are missing.")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN, num_threads=20)
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_time():
    return datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p")

# ==========================================
# 🍃 MONGODB SETUP
# ==========================================
try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
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

    try:
        sms_pool_col.create_index("created_at_dt", expireAfterSeconds=SMS_POOL_TTL_HOURS * 3600)
        orders_col.create_index("created_at_dt", expireAfterSeconds=ORDER_TTL_HOURS * 3600)
        orders_col.create_index([("user_id", 1), ("offer_id", 1), ("status", 1)])
        orders_col.create_index([("course_id", 1), ("status", 1)])
    except Exception:
        pass
    print(f"✅ MongoDB Connected! (Database: {MONGO_DB_NAME})")
except Exception as e:
    print(f"❌ MongoDB Error: {e}")
    sys.exit(1)

# ==========================================
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
    if not isinstance(chat_id, int) or chat_id <= 0 or chat_id == ADMIN_ID: return
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

def generate_upi_qr(amount, order_id):
    clean_amt = re.sub(r"[^\d.]", "", str(amount))
    upi_url = f"upi://pay?pa={UPI_ID}&pn={MERCHANT_NAME}&am={clean_amt}&cu=INR&tn=Order_{order_id}"
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=2)
    qr.add_data(upi_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    w, h = qr_img.size
    box_w, box_h = int(w * 0.28), int(int(w * 0.28) * 0.40)
    box_x, box_y = (w - box_w) // 2, (h - box_h) // 2
    draw = ImageDraw.Draw(qr_img)
    draw.rounded_rectangle([box_x, box_y, box_x + box_w, box_y + box_h], radius=6, fill="#ffffff", outline="#0b1329", width=2)
    lw = max(2, int(box_h * 0.12))
    let_w, let_h, gap = box_w * 0.18, box_h * 0.45, box_w * 0.08
    start_x = box_x + (box_w - ((let_w * 2) + gap * 2 + lw)) // 2
    start_y = box_y + (box_h - let_h) // 2
    u_x = start_x
    draw.line([(u_x, start_y), (u_x, start_y + let_h)], fill="#097939", width=lw)
    draw.line([(u_x, start_y + let_h), (u_x + let_w, start_y + let_h)], fill="#097939", width=lw)
    draw.line([(u_x + let_w, start_y + let_h), (u_x + let_w, start_y)], fill="#097939", width=lw)
    p_x = u_x + let_w + gap
    draw.line([(p_x, start_y), (p_x, start_y + let_h)], fill="#F37021", width=lw)
    draw.line([(p_x, start_y), (p_x + let_w, start_y)], fill="#F37021", width=lw)
    draw.line([(p_x + let_w, start_y), (p_x + let_w, start_y + let_h // 2)], fill="#F37021", width=lw)
    draw.line([(p_x + let_w, start_y + let_h // 2), (p_x, start_y + let_h // 2)], fill="#F37021", width=lw)
    i_x = p_x + let_w + gap + lw // 2
    draw.line([(i_x, start_y), (i_x, start_y + let_h)], fill="#1a73e8", width=lw)
    bio = io.BytesIO()
    qr_img.save(bio, "PNG")
    bio.seek(0)
    return bio, clean_amt

# ==========================================
# 🛡️ RATE LIMITER & STATE MANAGEMENT
# ==========================================
admin_data, user_states, user_qr_messages, pending_orders, all_orders_cache = {}, {}, {}, {}, {}
user_cooldowns = {}
pending_lock = threading.Lock()

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

# --- 🪑 SMART SEAT (SLOT) SYSTEM (.01 to .99) ---
def get_available_order_amount(base_amount):
    base_clean = float(base_amount)
    stale_cutoff = time.time() - 86400 # 24 hours lock for EXPIRED
    
    # 1. Get all locked amounts from Database (PENDING or EXPIRED within 24h)
    locked_orders = orders_col.find({
        "original_amount": str(base_clean),
        "status": {"$in": ["PENDING", "EXPIRED"]},
        "created_at": {"$gte": stale_cutoff}
    })
    
    locked_amounts = set()
    for o in locked_orders:
        try: locked_amounts.add(float(o["amount"]))
        except: pass
        
    # 2. Also check in-memory list just to be double safe
    with pending_lock:
        for k, v in pending_orders.items():
            if v.get("original_amount") == str(base_clean):
                try: locked_amounts.add(float(k))
                except: pass

    # 3. Find the first empty seat
    for paise in range(1, 100):
        test_amt = round(base_clean + (paise / 100.0), 2)
        if test_amt not in locked_amounts:
            return f"{test_amt:.2f}"
            
    # Fallback if all 99 seats are full (extremely rare)
    return f"{round(base_clean + 0.99, 2):.2f}"

# ==========================================
# 🎟 DYNAMIC PRICE & OFFER VALIDATION
# ==========================================
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
        # Keep buttons only if EXPIRED (so admin can still force approve late payments)
        if status_type in ["AUTO_VERIFIED", "MANUAL_APPROVED", "CANCELLED"]:
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

    # Slot remains blocked for 24h because status is EXPIRED, but we pop from memory list
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

    succ_cfg = get_cached_setting("success_msg_cfg") or {}
    extra_txt = succ_cfg.get("text", "").strip()
    succ_btns = succ_cfg.get("buttons", [])

    final_text = f"🎉 <b>Payment Verified Successfully!</b>\n\n{course.get('secret_text','')}"
    if extra_txt: final_text += f"\n\n{extra_txt}"

    markup = InlineKeyboardMarkup()
    for b in succ_btns:
        b_url = b.get("url", "")
        if b_url.lower() == "close": markup.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
        elif b_url.startswith("http"): markup.row(InlineKeyboardButton(b["text"], url=b_url))
        else: markup.row(InlineKeyboardButton(b["text"], callback_data=b_url))
    
    if not markup.keyboard: markup = None

    try: bot.send_message(chat_id, final_text, reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="purchase")
    except Exception: pass

    date_now = get_ist_time()
    verify_type = "MANUAL-APPROVED" if is_manual else "AUTO-VERIFIED"
    
    def _bg_log():
        purchases_col.insert_one({"user_id": user_id, "username": order.get("user_mention", f"User ({user_id})"), "item_info": f"{course_id} | Rate: ₹{order['amount']} | {verify_type} (order {order_id})", "date": date_now, "title": course.get('title', course_id), "link": course.get('secret_text', '')})
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
@bot.chat_join_request_handler()
def handle_join_request(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    used_link = message.invite_link.invite_link if message.invite_link else ""
    
    course = None
    for c in courses_col.find({"channel_id": chat_id}):
        if c.get("secret_text", "") and used_link in c.get("secret_text", ""):
            course = c
            break
            
    if not course: return
        
    course_id = course["course_id"]
    channel_name = course.get("channel_name") or message.chat.title or "Private Channel/Group"
    purchase = purchases_col.find_one({"user_id": user_id, "item_info": {"$regex": course_id}})
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
            orig_send_message(user_id, f"✅ <b>Request Approved!</b>\nWelcome to <b>{channel_name}</b>.", parse_mode="HTML")
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

    if not media_items:
        full_text = "".join(t.get("caption", "") + "\n\n" for t in text_items) + custom_caption
        if not full_text.strip(): full_text = f"📚 <b>Pack: {course.get('title', course['course_id'])}</b>\nPrice: ₹{int(final_price) if final_price.is_integer() else final_price}"
        bot.send_message(chat_id, full_text.strip(), reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
    elif len(media_items) == 1:
        it = media_items[0]
        try:
            if it["type"] == "photo": bot.send_photo(chat_id, it["file_id"], caption=final_cap, reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
            elif it["type"] == "video": bot.send_video(chat_id, it["file_id"], caption=final_cap, reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
        except Exception: pass
    else:
        media_group_html = []
        for i, item in enumerate(media_items):
            cap = final_cap if i == 0 else ""
            if item["type"] == "photo": media_group_html.append(InputMediaPhoto(item["file_id"], caption=cap, parse_mode="HTML"))
            elif item["type"] == "video": media_group_html.append(InputMediaVideo(item["file_id"], caption=cap, parse_mode="HTML"))
        try:
            sent_grp = orig_send_media_group(chat_id, media_group_html, protect_content=PROTECT_CONTENT)
            for m in sent_grp: register_activity(chat_id, m.message_id, "course")
        except Exception: pass
        try: bot.send_message(chat_id, "👆 <b>Choose an option to buy:</b>\n", reply_markup=markup, parse_mode="HTML", msg_type="course")
        except Exception: pass

def send_batch_to_user(chat_id, batch):
    bot.send_message(chat_id, f"📦 <b>{batch['title']}</b>\nAll packs are listed below:", parse_mode="HTML", msg_type="course")
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
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("📋 View All Plans / Packs", callback_data="user_view_plans"))
    markup.row(InlineKeyboardButton("🛍 My Purchases", callback_data="user_my_purchases_btn"))
    
    if cfg:
        for b in cfg.get("buttons", []):
            b_url = b.get("url", "")
            if b_url.lower() == "close": markup.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
            elif b_url.startswith("http"): markup.row(InlineKeyboardButton(b["text"], url=b_url))
            else: markup.row(InlineKeyboardButton(b["text"], callback_data=f"mainmenu_{b_url}"))
        
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

def send_admin_panel(chat_id):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("➕ Add Single Pack", callback_data="admin_add_course"))
    markup.row(InlineKeyboardButton("✏️ Edit Course", callback_data="admin_edit_course"))
    markup.row(InlineKeyboardButton("🗑 Delete Pack", callback_data="admin_delete_course"))
    markup.row(InlineKeyboardButton("📦 Pack Batch (Multi-Pack)", callback_data="admin_create_batch"))
    markup.row(InlineKeyboardButton("🎟 Create Promo Offer", callback_data="admin_create_offer"))
    markup.row(InlineKeyboardButton("📋 Manage Store Plans", callback_data="admin_manage_plans"))
    markup.row(InlineKeyboardButton("⚡ Flash Price (Hike/Drop)", callback_data="admin_flash_price"))
    markup.row(InlineKeyboardButton("🎨 Customize Start Menu", callback_data="admin_custom_menu"))
    markup.row(InlineKeyboardButton("🌍 Custom International Button", callback_data="admin_custom_intl"))
    markup.row(InlineKeyboardButton("🔘 Global Course Buttons", callback_data="admin_global_cbtns"))
    markup.row(InlineKeyboardButton("🎉 Post-Purchase Setup", callback_data="admin_success_msg"))
    markup.row(InlineKeyboardButton("🔗 Advanced File to Link", callback_data="admin_file_link"))
    markup.row(InlineKeyboardButton("📢 Advanced Broadcast", callback_data="admin_broadcast"))
    markup.row(InlineKeyboardButton("👥 User Info", callback_data="admin_user_info"))
    bot.send_message(chat_id, "🛠 <b>Admin Panel</b>\nPlease select an option:\n", reply_markup=markup, parse_mode="HTML")

# ==========================================
# COMMANDS & HANDLERS
# ==========================================
@bot.message_handler(commands=["start"])
def start_command(message):
    user_id = message.chat.id
    
    if is_maintenance_mode() and user_id != ADMIN_ID:
        orig_send_message(user_id, "⚠️ <b>Bot is currently under maintenance.</b>\nServers are busy or undergoing updates. Please try again after some time.", parse_mode="HTML")
        return

    if not check_rate_limit(user_id, 1): return
    register_activity(user_id, message.message_id, "general")
    
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
                if len(m_items) == 1:
                    it = m_items[0]
                    try:
                        if it["type"] == "text": bot.send_message(user_id, it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
                        elif it["type"] == "photo": bot.send_photo(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
                        elif it["type"] == "video": bot.send_video(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
                        elif it["type"] == "document": bot.send_document(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
                    except Exception as e: bot.send_message(user_id, f"❌ Error: {e}", msg_type="general")
                elif len(m_items) > 1:
                    m_group = []
                    for it in m_items:
                        if it["type"] == "photo": m_group.append(InputMediaPhoto(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                        elif it["type"] == "video": m_group.append(InputMediaVideo(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                        elif it["type"] == "document": m_group.append(InputMediaDocument(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                    try:
                        sent = orig_send_media_group(user_id, m_group, protect_content=PROTECT_CONTENT)
                        for m in sent: register_activity(user_id, m.message_id, "course")
                        if btns or any(i["type"] == "text" for i in m_items): bot.send_message(user_id, "👇", reply_markup=markup, parse_mode="HTML", msg_type="course")
                    except Exception as e: bot.send_message(user_id, f"❌ Error: {e}", msg_type="general")
            threading.Thread(target=_sf, daemon=True).start()
        else: bot.send_message(user_id, "❌ <b>File not found or expired.</b>", parse_mode="HTML", msg_type="general")
    else:
        if user_id == ADMIN_ID: send_admin_panel(user_id)
        else: send_custom_start_menu(user_id)

@bot.message_handler(content_types=["photo", "video", "document", "text"])
def handle_all_messages(message):
    user_id = message.chat.id
    if is_maintenance_mode() and user_id != ADMIN_ID:
        orig_send_message(user_id, "⚠️ <b>Bot is currently under maintenance.</b>\nServers are busy or undergoing updates. Please try again after some time.", parse_mode="HTML")
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

    if user_id == ADMIN_ID and user_id in admin_data:
        step = admin_data[ADMIN_ID].get("step")
        if step == "DELETE_COURSE":
            cid = message.text.strip()
            if courses_col.delete_one({"course_id": cid}).deleted_count:
                settings_col.update_one({"_id": "store_plans"}, {"$pull": {"course_ids": cid}})
                invalidate_cache(f"course_{cid}")
                bot.send_message(ADMIN_ID, f"✅ <b>Course <code>{cid}</code> Deleted!</b>", parse_mode="HTML")
            else: bot.send_message(ADMIN_ID, "❌ <b>Not found.</b>", parse_mode="HTML")
            del admin_data[ADMIN_ID]
            return send_admin_panel(ADMIN_ID)
        elif step == "ADD_PLAN_ID":
            cid = message.text.strip()
            if courses_col.find_one({"course_id": cid}):
                settings_col.update_one({"_id": "store_plans"}, {"$addToSet": {"course_ids": cid}}, upsert=True)
                invalidate_cache("store_plans")
                bot.send_message(ADMIN_ID, f"✅ <b>Course <code>{cid}</code> added to Store Plans!</b>", parse_mode="HTML")
            else: bot.send_message(ADMIN_ID, "❌ <b>Invalid ID.</b>", parse_mode="HTML")
            del admin_data[ADMIN_ID]
            return send_admin_panel(ADMIN_ID)
        elif step == "EDIT_COURSE_ID":
            cid = message.text.strip()
            course = courses_col.find_one({"course_id": cid})
            if not course:
                bot.send_message(ADMIN_ID, "❌ Course ID not found. Send correct ID:")
                return
            admin_data[ADMIN_ID]["course_id"] = cid
            admin_data[ADMIN_ID]["step"] = "EDIT_COURSE_TEXT"
            bot.send_message(ADMIN_ID, f"✅ Found course: <b>{course.get('title', cid)}</b>\n\nSend new <b>Text/Description</b> (or type <code>skip</code> to keep old):", parse_mode="HTML")
            return
        elif step == "EDIT_COURSE_TEXT":
            txt = get_formatted_text(message)
            if txt.lower() != "skip":
                courses_col.update_one({"course_id": admin_data[ADMIN_ID]["course_id"]}, {"$set": {"custom_caption": txt}})
            admin_data[ADMIN_ID]["step"] = "EDIT_COURSE_PHOTO"
            bot.send_message(ADMIN_ID, "✅ Text updated!\n\nNow send new <b>Photo</b> (or type <code>skip</code> if photo change nahi karni):", parse_mode="HTML")
            return
        elif step == "EDIT_COURSE_PHOTO":
            cid = admin_data[ADMIN_ID]["course_id"]
            if message.photo:
                photo_id = message.photo[-1].file_id
                promo_data = [{"type": "photo", "file_id": photo_id, "caption": ""}]
                courses_col.update_one({"course_id": cid}, {"$set": {"promo_media": promo_data}})
            bot.send_message(ADMIN_ID, f"🎉 <b>Course <code>{cid}</code> successfully updated!</b>", parse_mode="HTML")
            del admin_data[ADMIN_ID]
            return send_admin_panel(ADMIN_ID)
        elif step == "FLASH_PCT":
            try:
                pct = float(re.sub(r"[^\d.]", "", message.text.strip()))
                admin_data[ADMIN_ID]["percent"] = pct
                admin_data[ADMIN_ID]["step"] = "FLASH_TARGET"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("🌐 All Courses", callback_data="flash_tgt_all")).row(InlineKeyboardButton("🎯 Single Course", callback_data="flash_tgt_single"))
                bot.send_message(ADMIN_ID, f"✅ <b>{pct}% Set!</b>\nApply to:", reply_markup=m, parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ Numbers only.")
            return
        elif step == "FLASH_CID":
            cid = message.text.strip()
            if not courses_col.find_one({"course_id": cid}): return bot.send_message(ADMIN_ID, "❌ Course ID not found.")
            admin_data[ADMIN_ID]["course_id"] = cid
            admin_data[ADMIN_ID]["step"] = "FLASH_HOURS"
            bot.send_message(ADMIN_ID, "⏳ <b>Active for how many hours?</b> (e.g., 24)", parse_mode="HTML")
            return
        elif step == "FLASH_HOURS":
            try:
                hrs = float(re.sub(r"[^\d.]", "", message.text.strip()))
                admin_data[ADMIN_ID]["hours"] = hrs
                admin_data[ADMIN_ID]["step"] = "FLASH_LIMIT"
                bot.send_message(ADMIN_ID, "🔁 <b>How many times can a user buy at this new price?</b>\n(1 = Once per user, 0 = Unlimited):", parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid hours.")
            return
        elif step == "FLASH_LIMIT":
            try:
                lim = int(re.sub(r"[^\d]", "", message.text.strip()))
                sale_id = "fs_" + str(uuid.uuid4())[:6]
                now_ts = time.time()
                doc = {
                    "_id": "flash_sale", "sale_id": sale_id, "mode": admin_data[ADMIN_ID]["flash_mode"],
                    "percent": admin_data[ADMIN_ID]["percent"], "target": admin_data[ADMIN_ID]["target"],
                    "course_id": admin_data[ADMIN_ID].get("course_id"), "hours": admin_data[ADMIN_ID]["hours"],
                    "limit": lim, "created_at_ts": now_ts, "expires_at_ts": now_ts + (admin_data[ADMIN_ID]["hours"] * 3600)
                }
                settings_col.update_one({"_id": "flash_sale"}, {"$set": doc}, upsert=True)
                invalidate_cache("flash_sale")
                bot.send_message(ADMIN_ID, "🎉 <b>Flash Price Applied Successfully!</b>", parse_mode="HTML")
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid limit.")
            return
        elif step == "OFFER_DISCOUNT":
            try:
                disc = int(re.sub(r"[^\d]", "", message.text.strip()))
                if not (1 <= disc <= 100): raise ValueError()
                admin_data[ADMIN_ID]["discount"], admin_data[ADMIN_ID]["step"] = disc, "OFFER_TARGET"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("🌐 All Courses", callback_data="offtarget_all")).row(InlineKeyboardButton("🎯 Single Course", callback_data="offtarget_single"))
                bot.send_message(ADMIN_ID, f"✅ Discount <b>{disc}%</b> set!\nApply to:", reply_markup=m, parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ 1 to 100 only.")
            return
        elif step == "OFFER_SINGLE_CID":
            cid = message.text.strip()
            if not courses_col.find_one({"course_id": cid}): return bot.send_message(ADMIN_ID, "❌ <b>Course ID not found. Send correct ID:</b>", parse_mode="HTML")
            admin_data[ADMIN_ID]["target_course_id"], admin_data[ADMIN_ID]["step"] = cid, "OFFER_LIMIT"
            bot.send_message(ADMIN_ID, "👥 <b>Max users?</b> (0 for unlimited):", parse_mode="HTML")
            return
        elif step == "OFFER_LIMIT":
            try:
                lim = int(re.sub(r"[^\d]", "", message.text.strip()))
                admin_data[ADMIN_ID]["max_users"], admin_data[ADMIN_ID]["step"] = -1 if lim == 0 else lim, "OFFER_PERUSER"
                bot.send_message(ADMIN_ID, "🔁 <b>How many times can a user claim this offer?</b>\n(<b>1</b> = just once per user. <b>0</b> = unlimited):", parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid number.")
            return
        elif step == "OFFER_PERUSER":
            try:
                pl = int(re.sub(r"[^\d]", "", message.text.strip()))
                admin_data[ADMIN_ID]["per_user_limit"], admin_data[ADMIN_ID]["step"] = -1 if pl == 0 else pl, "OFFER_HOURS"
                bot.send_message(ADMIN_ID, "⏳ <b>Active for how many hours?</b> (e.g. 24 or 48):", parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid number.")
            return
        elif step == "OFFER_HOURS":
            try:
                hrs = float(re.sub(r"[^\d.]", "", message.text.strip()))
                off_code, now_ts = "off_" + str(uuid.uuid4())[:6], time.time()
                doc = {
                    "offer_code": off_code, "discount_percent": admin_data[ADMIN_ID]["discount"],
                    "target_type": admin_data[ADMIN_ID]["target_type"], "target_course_id": admin_data[ADMIN_ID].get("target_course_id"),
                    "max_users": admin_data[ADMIN_ID]["max_users"], "per_user_limit": admin_data[ADMIN_ID].get("per_user_limit", 1), "used_count": 0, "created_at_ts": now_ts,
                    "expires_at_ts": now_ts + (hrs * 3600), "expires_str": (datetime.now(IST) + timedelta(hours=hrs)).strftime("%d-%m-%Y %I:%M %p")
                }
                offers_col.insert_one(doc)
                bot.send_message(ADMIN_ID, f"🎉 <b>Discount Offer Link Created!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={off_code}</code>", parse_mode="HTML")
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid hours.")
            return
        elif step == "MENU_CUSTOM_CONTENT":
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            admin_data[ADMIN_ID]["menu_content"] = {"media_type": mt, "file_id": fid, "text": get_formatted_text(message)}
            admin_data[ADMIN_ID]["buttons"], admin_data[ADMIN_ID]["step"] = [], "MENU_ADD_BUTTONS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="menu_finish_save"))
            bot.send_message(ADMIN_ID, "✅ <b>Content Saved!</b>\nAdd buttons: <code>Button Name - Link</code> (or type <code>Close - close</code>).", reply_markup=m, parse_mode="HTML")
            return
        elif step == "MENU_ADD_BUTTONS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save Menu", callback_data="menu_finish_save"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! ({len(admin_data[ADMIN_ID]['buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error. <code>Name - Link</code>", parse_mode="HTML")
            return
        elif step == "GCBTN_ADD":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="gcbtn_finish"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Global Button Added! ({len(admin_data[ADMIN_ID]['buttons'])})</b>\nSend another or Finish:", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error. <code>Name - Link</code>", parse_mode="HTML")
            return
        elif step == "SUCC_SET_TEXT":
            admin_data[ADMIN_ID]["text"] = get_formatted_text(message)
            admin_data[ADMIN_ID]["buttons"] = []
            admin_data[ADMIN_ID]["step"] = "SUCC_ADD_BTNS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="succ_finish"))
            bot.send_message(ADMIN_ID, "✅ <b>Text Saved!</b>\n\n🔘 <b>Step 2: Add Buttons</b>\nFormat: <code>Button Text - URL</code>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "SUCC_ADD_BTNS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="succ_finish"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! ({len(admin_data[ADMIN_ID]['buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error. <code>Name - Link</code>", parse_mode="HTML")
            return
        elif step == "INTL_SET_NAME":
            b_name = message.text.strip()
            if not b_name: return bot.send_message(ADMIN_ID, "❌ Name cannot be empty.")
            admin_data[ADMIN_ID]["intl_name"] = b_name
            admin_data[ADMIN_ID]["step"] = "INTL_SET_CONTENT"
            bot.send_message(ADMIN_ID, f"✅ <b>Button Name Saved:</b> <code>{b_name}</code>\n\n📝 Send Photo with Caption OR Plain Text:", parse_mode="HTML")
            return
        elif step == "INTL_SET_CONTENT":
            fid = message.photo[-1].file_id if message.photo else None
            txt = get_formatted_text(message)
            admin_data[ADMIN_ID]["intl_photo"] = fid
            admin_data[ADMIN_ID]["intl_text"] = txt
            admin_data[ADMIN_ID]["intl_buttons"] = []
            admin_data[ADMIN_ID]["step"] = "INTL_ADD_BUTTONS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="intl_finish_save"))
            bot.send_message(ADMIN_ID, "✅ <b>Content Saved!</b> Add Action Buttons: <code>Button Text - URL</code>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "INTL_ADD_BUTTONS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["intl_buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="intl_finish_save"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! ({len(admin_data[ADMIN_ID]['intl_buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error.", parse_mode="HTML")
            return
        elif step == "PROMO":
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            admin_data[ADMIN_ID]["promo"].append({"type": mt, "file_id": fid, "caption": get_formatted_text(message)})
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step (Price)", callback_data="next_price"))
            bot.send_message(ADMIN_ID, f"✅ <b>{mt.capitalize()} saved!</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "AMOUNT":
            amt = re.sub(r"[^\d.]", "", message.text.strip())
            if not amt: return bot.send_message(ADMIN_ID, "❌ <b>Numbers only.</b>", parse_mode="HTML")
            admin_data[ADMIN_ID]["amount"], admin_data[ADMIN_ID]["step"] = amt, "CAPTION"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Skip (No Caption)", callback_data="skip_caption"))
            bot.send_message(ADMIN_ID, f"✅ <b>Price ₹{amt} saved!</b>\n📝 Type optional extra caption, or skip:", reply_markup=m, parse_mode="HTML")
            return
        elif step == "CAPTION":
            admin_data[ADMIN_ID]["caption"] = get_formatted_text(message)
            admin_data[ADMIN_ID]["course_buttons"] = []
            admin_data[ADMIN_ID]["step"] = "COURSE_BUTTONS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Finish Buttons", callback_data="skip_course_buttons"))
            bot.send_message(ADMIN_ID, "✅ <b>Caption saved!</b>\n🔘 Optional Extra Buttons: <code>Button Text - URL</code>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "COURSE_BUTTONS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["course_buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Finish Buttons", callback_data="skip_course_buttons"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! ({len(admin_data[ADMIN_ID]['course_buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error.", parse_mode="HTML")
            return
        elif step == "SECRET":
            cid = "c_" + str(uuid.uuid4())[:6]
            courses_col.update_one({"course_id": cid}, {"$set": {
                "course_id": cid, "title": admin_data[ADMIN_ID].get("title", cid), "promo_media": admin_data[ADMIN_ID]["promo"], "amount": admin_data[ADMIN_ID]["amount"],
                "custom_caption": admin_data[ADMIN_ID].get("caption",""), "secret_text": get_formatted_text(message),
                "extra_buttons": admin_data[ADMIN_ID].get("course_buttons", [])
            }}, upsert=True)
            if admin_data[ADMIN_ID].get("mode") == "single":
                bot.send_message(ADMIN_ID, f"🎉 <b>Pack created!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={cid}</code>", parse_mode="HTML")
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
            elif admin_data[ADMIN_ID].get("mode") == "batch":
                admin_data[ADMIN_ID]["course_ids"].append(cid)
                admin_data[ADMIN_ID]["step"] = "NEXT_ACTION"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Another", callback_data="batch_add_next")).row(InlineKeyboardButton("✅ Finish Batch", callback_data="batch_finish"))
                bot.send_message(ADMIN_ID, "✅ <b>Pack saved!</b>", reply_markup=m, parse_mode="HTML")
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
                return bot.send_message(ADMIN_ID, "❌ Please forward a message from the Private Channel/Group or send its link.")
            
            try:
                chat_info = bot.get_chat(channel_id)
                channel_name = chat_info.title if chat_info.title else "Private Channel"
                link = bot.create_chat_invite_link(channel_id, creates_join_request=True)
                secret_text = f"👉 <b>Click here to join the Group/Channel:</b>\n{link.invite_link}"
                cid = "c_" + str(uuid.uuid4())[:6]
                
                courses_col.update_one({"course_id": cid}, {"$set": {
                    "course_id": cid, "title": admin_data[ADMIN_ID].get("title", channel_name), "promo_media": admin_data[ADMIN_ID]["promo"], "amount": admin_data[ADMIN_ID]["amount"],
                    "custom_caption": admin_data[ADMIN_ID].get("caption", ""), "secret_text": secret_text, "channel_id": channel_id,
                    "channel_name": channel_name, "extra_buttons": admin_data[ADMIN_ID].get("course_buttons", [])
                }}, upsert=True)
                
                if admin_data[ADMIN_ID].get("mode") == "single":
                    bot.send_message(ADMIN_ID, f"🎉 <b>Channel Pack created! ({channel_name})</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={cid}</code>", parse_mode="HTML")
                    del admin_data[ADMIN_ID]
                    send_admin_panel(ADMIN_ID)
                elif admin_data[ADMIN_ID].get("mode") == "batch":
                    admin_data[ADMIN_ID]["course_ids"].append(cid)
                    admin_data[ADMIN_ID]["step"] = "NEXT_ACTION"
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Another", callback_data="batch_add_next")).row(InlineKeyboardButton("✅ Finish Batch", callback_data="batch_finish"))
                    bot.send_message(ADMIN_ID, "✅ <b>Pack saved!</b>", reply_markup=m, parse_mode="HTML")
            except Exception as e:
                bot.send_message(ADMIN_ID, f"❌ Error: Make sure the bot is an Admin in the Channel/Group first! ({e})")
            return
        elif step == "TITLE":
            admin_data[ADMIN_ID]["title"], admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["promo"] = message.text.strip(), "PROMO", []
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step", callback_data="next_price"))
            bot.send_message(ADMIN_ID, "✅ Title saved. <b>Send promo media/text:</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step in ["BC_MEDIA", "FTL_MEDIA"]:
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            elif message.document: mt, fid = "document", message.document.file_id
            admin_data[ADMIN_ID].setdefault("media", []).append({"type": mt, "file_id": fid, "caption": get_formatted_text(message)})
            cb = "bc_done" if step == "BC_MEDIA" else "ftl_done"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Done Adding", callback_data=cb))
            bot.send_message(ADMIN_ID, "✅ <b>Saved!</b> Send another or click Done.", reply_markup=m, parse_mode="HTML")
            return
        elif step in ["BC_BUTTONS", "FTL_BUTTONS"]:
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID].setdefault("buttons", []).append({"text": t.strip(), "url": u.strip()})
                    cb = "bc_finish" if step == "BC_BUTTONS" else "ftl_finish"
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data=cb))
                    bot.send_message(ADMIN_ID, "✅ <b>Button Added!</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format Error.", parse_mode="HTML")
            return
        elif step == "BC_TIME":
            try:
                hrs = float(re.sub(r"[^\d.]", "", message.text.strip()))
                m_items = admin_data[ADMIN_ID].get("media", [])
                btns = admin_data[ADMIN_ID].get("buttons", [])
                
                m = InlineKeyboardMarkup()
                for b in btns:
                    b_url = b.get("url", "")
                    if b_url.lower() == "close": m.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
                    elif b_url.startswith("http"): m.row(InlineKeyboardButton(b["text"], url=b_url))
                    else: m.row(InlineKeyboardButton(b["text"], callback_data=b_url))
                
                if not m.keyboard: m = None
                bot.send_message(ADMIN_ID, "⏳ Broadcasting started...")
                
                def run_bc():
                    success = 0
                    delete_list = []
                    all_users = list(users_col.find({}))
                    for u in all_users:
                        # FIXED: Broadcast Bug (Sent 0 users)
                        raw_uid = u.get("user_id") or u.get("chat_id")
                        if not raw_uid: continue
                        try: uid = int(raw_uid)
                        except: continue

                        try:
                            if not m_items:
                                if m:
                                    msg = orig_send_message(uid, "👇", reply_markup=m, parse_mode="HTML", disable_web_page_preview=True)
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
                                    msg = orig_send_message(uid, "👇", reply_markup=m, parse_mode="HTML", disable_web_page_preview=True)
                                    sent_ids.append(msg.message_id)
                                    
                            for mid in sent_ids:
                                register_activity(uid, mid, "broadcast")
                                if hrs > 0: delete_list.append((uid, mid))
                                
                            success += 1
                            time.sleep(0.04)
                        except Exception: pass
                        
                    bot.send_message(ADMIN_ID, f"✅ <b>Broadcast Complete!</b> ({success} users).", parse_mode="HTML")
                    
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
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
                
            except Exception:
                bot.send_message(ADMIN_ID, "❌ Invalid number. Send 0 or higher.")
            return

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

    if is_maintenance_mode() and chat_id != ADMIN_ID:
        try: bot.answer_callback_query(call.id, "⚠️ Bot is currently under maintenance. Please try again later.", show_alert=True)
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

    # --- CHANNEL FORCE ACTIONS (Approve / Cancel) ---
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

    if data.startswith("send_ss_"):
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
                try: wait_msg = bot.send_message(chat_id, "⏳ <i>Generating your UPI QR Code... Just a second!</i>", parse_mode="HTML", msg_type="general")
                except Exception: pass

                state = get_user_state(chat_id)
                if state and state.get("step") == "PENDING_UPI":
                    with pending_lock: pending_orders.pop(state.get("amount_key", ""), None)
                    if chat_id in user_qr_messages:
                        try: bot.delete_message(chat_id, user_qr_messages.pop(chat_id))
                        except Exception: pass
                        
                order_id = str(uuid.uuid4())[:8]
                # Smart Seat Allotment (0.01 to 0.99)
                amt_key = get_available_order_amount(final_price)
                
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
                
                # --- CHANNEL INLINE BUTTONS (Approve & Cancel) ---
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
                inv = f"👤 <b>User:</b> {call.from_user.first_name}\n🆔 <b>Order:</b> <code>{order_id}</code>\n💰 <b>Amount:</b> ₹{clean_amt}\n⚠️ <b>Please pay the exact amount shown.</b>\n⏳ <i>QR will expire in {QR_EXPIRY_SECONDS // 60} minutes.</i>"
                
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
    if data == "admin_flash_price":
        m = InlineKeyboardMarkup()
        m.row(InlineKeyboardButton("📈 Price Hike (+)", callback_data="flash_start_hike"), InlineKeyboardButton("📉 Price Drop (-)", callback_data="flash_start_drop"))
        m.row(InlineKeyboardButton("🔄 Reset to Normal Prices", callback_data="flash_reset"))
        m.row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("⚡ <b>Flash Price Manager</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data in ["flash_start_hike", "flash_start_drop"]:
        admin_data[ADMIN_ID] = {"step": "FLASH_PCT", "flash_mode": "hike" if data == "flash_start_hike" else "drop"}
        bot.edit_message_text("✏️ <b>Enter Percentage:</b>\n(e.g., Send 20 for 20%)", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "flash_reset":
        settings_col.delete_one({"_id": "flash_sale"})
        invalidate_cache("flash_sale")
        bot.edit_message_text("✅ <b>All prices reset to normal!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "flash_tgt_all":
        admin_data[ADMIN_ID]["target"] = "all"
        admin_data[ADMIN_ID]["step"] = "FLASH_HOURS"
        bot.edit_message_text("⏳ <b>Active for how many hours?</b> (e.g., 24)", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "flash_tgt_single":
        admin_data[ADMIN_ID]["target"] = "single"
        admin_data[ADMIN_ID]["step"] = "FLASH_CID"
        bot.edit_message_text("🎯 <b>Send Course ID:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "skip_course_buttons":
        admin_data[ADMIN_ID]["step"] = "COURSE_TYPE"
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("📝 Text / Secret Link", callback_data="ctype_text"), InlineKeyboardButton("📢 Private Channel/Group", callback_data="ctype_channel"))
        bot.edit_message_text("✅ <b>Buttons saved!</b>\nWhat will the user get after payment?", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "admin_global_cbtns":
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add/Update Buttons", callback_data="gcbtn_add")).row(InlineKeyboardButton("🗑 Clear All Buttons", callback_data="gcbtn_clear")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("🔘 <b>Global Course Buttons</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "gcbtn_add":
        admin_data[ADMIN_ID] = {"step": "GCBTN_ADD", "buttons": []}
        bot.edit_message_text("➕ Send format: <code>Button Text - URL</code>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "gcbtn_finish":
        d = admin_data.get(ADMIN_ID, {})
        settings_col.update_one({"_id": "global_course_btns"}, {"$set": {"buttons": d.get("buttons", []), "updated_at": get_ist_time()}}, upsert=True)
        invalidate_cache("global_course_btns")
        del admin_data[ADMIN_ID]
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
        admin_data[ADMIN_ID] = {"step": "SUCC_SET_TEXT"}
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Skip Text", callback_data="succ_skip_text"))
        bot.edit_message_text("✏️ Send extra caption/text:", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "succ_skip_text":
        admin_data[ADMIN_ID] = {"step": "SUCC_ADD_BTNS", "text": "", "buttons": []}
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="succ_finish"))
        bot.edit_message_text("🔘 Add Buttons: <code>Button Text - URL</code>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "succ_finish":
        d = admin_data.get(ADMIN_ID, {})
        settings_col.update_one({"_id": "success_msg_cfg"}, {"$set": {"text": d.get("text", ""), "buttons": d.get("buttons", []), "updated_at": get_ist_time()}}, upsert=True)
        invalidate_cache("success_msg_cfg")
        del admin_data[ADMIN_ID]
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
        admin_data[ADMIN_ID] = {"step": "INTL_SET_NAME"}
        bot.edit_message_text("✏️ Send Name (e.g. <code>🌍 International Pay</code>):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "intl_finish_save":
        d = admin_data.get(ADMIN_ID, {})
        doc = {"_id": "intl_btn_cfg", "btn_name": d.get("intl_name", "🌍 International"), "photo_id": d.get("intl_photo"), "text": d.get("intl_text", ""), "buttons": d.get("intl_buttons", []), "updated_at": get_ist_time()}
        settings_col.update_one({"_id": "intl_btn_cfg"}, {"$set": doc}, upsert=True)
        invalidate_cache("intl_btn_cfg")
        del admin_data[ADMIN_ID]
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
        admin_data[ADMIN_ID]["step"] = "SECRET"
        bot.edit_message_text("✅ <b>Send secret link or text:</b>", chat_id, msg_id, parse_mode="HTML")
    elif data == "ctype_channel":
        admin_data[ADMIN_ID]["step"] = "CHANNEL_ID"
        bot.edit_message_text("📢 <b>Forward a message OR send Private Channel Link:</b>", chat_id, msg_id, parse_mode="HTML")
    elif data == "skip_caption" and ADMIN_ID in admin_data:
        admin_data[ADMIN_ID]["caption"] = ""
        admin_data[ADMIN_ID]["course_buttons"] = []
        admin_data[ADMIN_ID]["step"] = "COURSE_BUTTONS"
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Finish Buttons", callback_data="skip_course_buttons"))
        bot.edit_message_text("🔘 Add Extra Buttons: <code>Text - URL</code>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "admin_create_offer":
        admin_data[ADMIN_ID] = {"step": "OFFER_DISCOUNT"}
        bot.edit_message_text("🎟 <b>Enter Discount % (e.g. 50):</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "offtarget_all":
        admin_data[ADMIN_ID]["target_type"], admin_data[ADMIN_ID]["step"] = "all", "OFFER_LIMIT"
        bot.edit_message_text("👥 Max claims? (0 = unlimited):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "offtarget_single":
        admin_data[ADMIN_ID]["target_type"], admin_data[ADMIN_ID]["step"] = "single", "OFFER_SINGLE_CID"
        bot.edit_message_text("🎯 Send Course ID (e.g. <code>c_abc123</code>):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_delete_course":
        admin_data[ADMIN_ID] = {"step": "DELETE_COURSE"}
        bot.edit_message_text("🗑 Send Course ID to delete:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_edit_course":
        admin_data[ADMIN_ID] = {"step": "EDIT_COURSE_ID"}
        bot.edit_message_text("✏️ Send Course ID to edit:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_manage_plans":
        c_ids = (get_cached_setting("store_plans") or {}).get("course_ids", [])
        text = f"📋 <b>Manage Store Plans ({len(c_ids)}):</b>\n" + "".join(f"• <code>{cid}</code>\n" for cid in c_ids)
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Course", callback_data="plan_add_id")).row(InlineKeyboardButton("🗑 Clear All", callback_data="plan_clear_all")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "plan_add_id":
        admin_data[ADMIN_ID] = {"step": "ADD_PLAN_ID"}
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
        admin_data[ADMIN_ID] = {"step": "MENU_CUSTOM_CONTENT"}
        bot.edit_message_text("📝 Send Photo, Video or Text:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "menu_finish_save":
        c = admin_data.get(ADMIN_ID, {}).get("menu_content", {})
        settings_col.update_one({"_id": "start_menu"}, {"$set": {"media_type": c.get("media_type", "text"), "file_id": c.get("file_id"), "text": c.get("text", ""), "buttons": admin_data.get(ADMIN_ID, {}).get("buttons", []), "updated_at": get_ist_time()}}, upsert=True)
        invalidate_cache("start_menu")
        del admin_data[ADMIN_ID]
        bot.edit_message_text("🎉 <b>Saved!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "menu_reset_default":
        settings_col.delete_one({"_id": "start_menu"})
        invalidate_cache("start_menu")
        bot.edit_message_text("✅ <b>Reset!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "admin_add_course":
        admin_data[ADMIN_ID] = {"mode": "single", "step": "TITLE", "promo": [], "amount": None, "caption": "", "course_buttons": []}
        bot.edit_message_text("📝 <b>Step 1: Send Course Title</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_create_batch":
        admin_data[ADMIN_ID] = {"mode": "batch", "step": "TITLE", "course_ids": []}
        bot.edit_message_text("📦 <b>Send Batch Title:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data in ["admin_file_link", "admin_broadcast"]:
        admin_data[ADMIN_ID] = {"step": "FTL_MEDIA" if data == "admin_file_link" else "BC_MEDIA", "media": []}
        bot.edit_message_text(f"{'📎 File to Link' if data == 'admin_file_link' else '📢 Broadcast'}\nSend Media/Text.", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "next_price" and ADMIN_ID in admin_data:
        admin_data[ADMIN_ID]["step"] = "AMOUNT"
        bot.edit_message_text("💰 <b>Price (INR)</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "batch_add_next":
        admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["promo"], admin_data[ADMIN_ID]["caption"], admin_data[ADMIN_ID]["course_buttons"] = "TITLE", [], "", []
        bot.edit_message_text("📝 <b>Send title for next pack:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "batch_finish":
        d = admin_data.get(ADMIN_ID)
        if d and d.get("course_ids"):
            bid = "b_" + str(uuid.uuid4())[:6]
            batches_col.update_one({"batch_id": bid}, {"$set": {"batch_id": bid, "title": d["title"], "course_ids": d["course_ids"]}}, upsert=True)
            bot.edit_message_text(f"🎉 <b>Batch Created!</b>\n👉 <code>https://t.me/{BOT_USERNAME}?start={bid}</code>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
            del admin_data[ADMIN_ID]
            send_admin_panel(chat_id)
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
        admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["buttons"] = "BC_BUTTONS", []
        bot.send_message(ADMIN_ID, "✅ <b>Media Saved!</b> Add button or Finish.", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data="bc_finish")), parse_mode="HTML")
    elif data == "bc_finish":
        admin_data[ADMIN_ID]["step"] = "BC_TIME"
        bot.send_message(ADMIN_ID, "⏳ <b>How many hours should this broadcast stay? (0 for permanent):</b>", parse_mode="HTML")
    elif data == "ftl_done":
        admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["buttons"] = "FTL_BUTTONS", []
        bot.send_message(ADMIN_ID, "✅ <b>Media Saved!</b> Add button or Finish.", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data="ftl_finish")), parse_mode="HTML")
    elif data == "ftl_finish":
        fid = "f_" + str(uuid.uuid4())[:6]
        file_links_col.update_one({"file_code": fid}, {"$set": {"file_code": fid, "media_data": admin_data[ADMIN_ID].get("media", []), "button_data": admin_data[ADMIN_ID].get("buttons", [])}}, upsert=True)
        bot.send_message(ADMIN_ID, f"🎉 <b>Link Created!</b>\n👉 <code>https://t.me/{BOT_USERNAME}?start={fid}</code>", parse_mode="HTML")
        del admin_data[ADMIN_ID]
        send_admin_panel(ADMIN_ID)

# ==========================================
# FLASK WEB SERVER & API
# ==========================================
app = Flask(__name__)
AMOUNT_RE_DECIMAL = re.compile(r"(?:Rs\.?|₹|INR)\s*([\d,]+\.\d{1,2})", re.IGNORECASE)
AMOUNT_RE_INT = re.compile(r"(?:Rs\.?|₹|INR)\s*([\d,]+)(?!\.\d)", re.IGNORECASE)
BOT_USERNAME = "your_bot" 

@app.route("/")
def home(): return "Telegram Bot API Running."

@app.route("/sms-webhook/<secret>", methods=["GET", "POST"])
def sms_webhook(secret):
    if secret != SMS_HOOK_SECRET: return "forbidden", 403
    sms_text = (request.get_json(silent=True) or request.form).get("text", "").strip() if request.method == "POST" else request.args.get("text", "").strip()
    if not sms_text: return "no 'text' param", 400
    
    m = AMOUNT_RE_DECIMAL.search(sms_text)
    has_dec = bool(m)
    if not m: m = AMOUNT_RE_INT.search(sms_text)
    if not m: return "no amount", 200

    amt_str = m.group(1).replace(",", "")
    f_round = f"{float(amt_str):.2f}" if not has_dec else amt_str
    sms_pool_col.insert_one({"amount": f_round, "raw_text": sms_text, "status": "UNUSED", "created_at_dt": datetime.now(timezone.utc), "created_at_str": get_ist_time()})

    order, amb = None, False
    with pending_lock:
        cands = [amt_str] if has_dec and amt_str in pending_orders else [f_round] if not has_dec and f_round in pending_orders else [k for k in pending_orders if not has_dec and k.startswith(amt_str + ".")]
        if len(cands) == 1: order = pending_orders.pop(cands[0])
        else: amb = len(cands) > 1

    if not order:
        stale_cutoff = time.time() - STALE_ORDER_SECONDS
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

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USERNAME or auth.password != DASHBOARD_PASSWORD:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Dashboard"'})
        return f(*args, **kwargs)
    return wrapper

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
    today_amt = sum(float(o.get("amount", 0) or 0) for o in orders_col.find(completed_q) if _order_ts(o) >= today_start)
    
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
    sms_text = f"You've received Rs.{amount} from Dashboard Test via PhonePe for txn T{int(time.time()*1000)}"
    
    with app.test_client() as client:
        resp = client.get(f"/sms-webhook/{SMS_HOOK_SECRET}?text={sms_text}")
        return jsonify({"status": "success", "result": resp.get_data(as_text=True)})

@app.route("/dashboard/api/courses", methods=["GET"])
@require_auth
def api_courses():
    out = []
    for c in courses_col.find().sort("_id", -1):
        out.append({"course_id": c.get("course_id"), "title": c.get("title", ""), "amount": c.get("amount"), "caption": c.get("custom_caption", "")[:40], "is_channel": bool(c.get("channel_id"))})
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
    is_channel = bool(course and course.get("channel_id"))
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
            "updated_at": get_ist_time()
        }
        settings_col.update_one({"_id": "system_settings"}, {"$set": doc}, upsert=True)
        invalidate_cache("system_settings")

        if was_maintenance and not is_maintenance_now:
            def broadcast_back_online():
                msg = "✅ <b>Bot is Back Online! / बोट चालू हो गया है!</b>\n\nMaintenance is complete. You can now continue using the bot smoothly."
                for u in users_col.find():
                    uid = u.get("user_id") or u.get("chat_id")
                    if uid and int(uid) != ADMIN_ID:
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
        "auto_del_promos": cfg.get("auto_del_promos", True),
        "auto_del_broadcasts": cfg.get("auto_del_broadcasts", True),
        "auto_del_purchases": cfg.get("auto_del_purchases", False)
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

@app.route("/dashboard/api/broadcast", methods=["POST"])
@require_auth
def api_broadcast():
    data = request.json or {}
    msg = data.get("message")
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
    
    def run_bc():
        sent_count = 0
        for u in users_col.find():
            # FIXED: Broadcast Bug (Dashboard)
            raw_uid = u.get("user_id") or u.get("chat_id")
            if not raw_uid: continue
            try:
                bot.send_message(int(raw_uid), msg, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
                sent_count += 1
                time.sleep(0.04)
            except Exception: pass
            
    threading.Thread(target=run_bc, daemon=True).start()
    return jsonify({"status": "success"})

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

@app.route("/dashboard/api/users")
@require_auth
def api_users():
    search_q = request.args.get("search", "").strip()
    query = {}
    if search_q:
        if search_q.isdigit(): query = {"user_id": int(search_q)}
        else: query = {"username": {"$regex": search_q, "$options": "i"}}
    
    out = []
    for u in users_col.find(query).sort("updated_at", -1).limit(200):
        uid = u.get("user_id")
        user_purchases = list(purchases_col.find({"user_id": uid}))
        out.append({
            "user_id": uid, 
            "username": u.get("username", "None"), 
            "updated_at": u.get("updated_at"),
            "purchases": [{"title": p.get("title", "Course"), "link": p.get("link", "#")} for p in user_purchases]
        })
    return jsonify(out)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Store Dashboard</title>
<style>
  :root{--bg:#0F1512; --surface:#141C18; --line:#26332C; --text:#EDF2EF; --muted:#8FA398; --ok:#3ED9A0; --pending:#E8A94A; --danger:#E8695F;}
  *{box-sizing:border-box;} body{margin:0; background:var(--bg); color:var(--text); font-family:sans-serif; font-size:14px;}
  .mono{font-family:monospace;}
  header{padding:15px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between;}
  .ledger{display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:10px; padding:15px;}
  .ledger .row{background:var(--surface); padding:15px; border-radius:6px; border:1px solid var(--line);}
  .tabs{display:flex; gap:15px; padding:10px 15px; border-bottom:1px solid var(--line); overflow-x:auto;}
  .tab{color:var(--muted); cursor:pointer; padding-bottom:5px; border-bottom:2px solid transparent; white-space:nowrap;}
  .tab.active{color:var(--text); border-bottom-color:var(--ok);}
  .subtabs{display:flex; gap:10px; padding:10px 15px;}
  .subtab{color:var(--muted); padding:4px 10px; border:1px solid var(--line); border-radius:15px; cursor:pointer;}
  .subtab.active{background:var(--ok); color:var(--bg); border-color:var(--ok);}
  .list{padding:15px; padding-bottom:50px;}
  .item{display:flex; justify-content:space-between; align-items:center; background:var(--surface); padding:10px; margin-bottom:8px; border-left:3px solid var(--line); border-radius:4px;}
  .item.ok{border-color:var(--ok);} .item.pending{border-color:var(--pending);} .item.expired{border-color:var(--danger);}
  .item .main{flex:1;} .item .name{font-weight:bold;} .item .sub{color:var(--muted); font-size:12px; margin-top:3px;}
  .item .amt{font-size:15px; font-weight:bold;} 
  .action-btn{background:var(--surface); color:var(--text); border:1px solid var(--line); padding:6px 10px; border-radius:4px; cursor:pointer; font-size:12px; margin-top:6px; display:inline-block; margin-left:4px;}
  .ok-btn{border-color:var(--ok); color:var(--ok);} .danger-btn{border-color:var(--danger); color:var(--danger);}
  .form-box{background:var(--surface); padding:15px; border-radius:6px; border:1px solid var(--line); margin-bottom:15px;}
  .form-box input, .form-box select, .form-box textarea{width:100%; padding:8px; margin:5px 0 10px; background:var(--bg); border:1px solid var(--line); color:var(--text); border-radius:4px;}
  .form-box button{background:var(--ok); color:var(--bg); border:none; padding:10px 15px; border-radius:4px; font-weight:bold; cursor:pointer;}
  .switch { position: relative; display: inline-block; width: 50px; height: 24px; vertical-align: middle; margin-left:10px;}
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: var(--line); transition: .4s; border-radius: 24px; }
  .slider:before { position: absolute; content: ""; height: 16px; width: 16px; left: 4px; bottom: 4px; background-color: white; transition: .4s; border-radius: 50%; }
  input:checked + .slider { background-color: var(--danger); }
  input:checked + .slider:before { transform: translateX(26px); }
  .checkbox-lbl { display: block; margin-top: 8px; color: var(--text); cursor: pointer;}
</style></head><body>
<header><h2>Store Dashboard</h2><div id="clock" class="mono"></div></header>
<div class="ledger" id="overview"></div>
<div class="tabs">
  <div class="tab active" data-tab="orders">Orders</div>
  <div class="tab" data-tab="courses">Courses</div>
  <div class="tab" data-tab="offers">Offers</div>
  <div class="tab" data-tab="logs">Channel Logs</div>
  <div class="tab" data-tab="sms">SMS Pool & Trigger</div>
  <div class="tab" data-tab="broadcast">📢 Broadcast</div>
  <div class="tab" data-tab="users">Users</div>
  <div class="tab" data-tab="settings">⚙️ Settings</div>
</div>
<div class="subtabs" id="subtabs">
  <div class="subtab active" data-status="all">All</div>
  <div class="subtab" data-status="pending">Pending</div>
  <div class="subtab" data-status="completed">Completed</div>
  <div class="subtab" data-status="expired">Expired</div>
</div>
<div class="list" id="list">Loading...</div>
<script>
let curTab="orders", curSt="all", searchUserQ="";
function fmtSecs(s){ if(s<=0)return "0s"; let m=Math.floor(s/60), sec=s%60; return m+"m "+sec+"s"; }
async function load(){
  let r=await fetch("/dashboard/api/overview"), d=await r.json();
  document.getElementById("overview").innerHTML=`
    <div class="row">Today: <br><b class="mono" style="font-size:18px">₹${d.today_amount}</b></div>
    <div class="row">Active QR: <b>${d.active_pending}</b><br>Pending SMS: <b>${d.pending_sms}</b></div>
    <div class="row">Total Sales: <b>${d.completed_total}</b><br>Total Users: <b>${d.total_users}</b></div>
  `;
  if(curTab==="orders"){
    r=await fetch("/dashboard/api/orders?status="+curSt); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>{
      let c=x.status==="PENDING"?"pending":x.status==="EXPIRED"?"expired":"ok";
      let rgt = x.status==="PENDING"?`<div class="timer mono" data-remain="${x.remaining_seconds}">${fmtSecs(x.remaining_seconds)} bacha</div>`:`<div class="sub">${x.method||x.status}</div>`;
      let actions = "";
      if(x.status==="PENDING" || x.status==="EXPIRED"){
        actions = `<br><button class="action-btn ok-btn" onclick="appr('${x.order_id}')">✅ Force Approve</button><button class="action-btn danger-btn" onclick="canO('${x.order_id}')">❌ Cancel</button>`;
      }
      return `<div class="item ${c}"><div class="main"><div class="name">${x.user} · <span class="mono">${x.course_id}</span></div><div class="sub">${x.order_id} · ${x.created_at}</div></div><div style="text-align:right"><div class="amt mono">₹${x.amount}</div>${rgt}${actions}</div></div>`;
    }).join("")||"No orders.";
  } else if(curTab==="courses"){
    r=await fetch("/dashboard/api/courses"); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>`<div class="item ok" style="cursor:pointer" onclick="viewBuyers('${x.course_id}')"><div class="main"><div class="name"><span class="mono">${x.course_id}</span> - ${x.title||'No Title'} ${x.is_channel?'📢 Channel':'📝 Text'}</div><div class="sub">₹${x.amount} · ${x.caption}</div></div><div><button class="action-btn danger-btn" onclick="event.stopPropagation(); delC('${x.course_id}')">🗑 Delete</button></div></div>`).join("")||"No courses.";
  } else if(curTab==="offers"){
    r=await fetch("/dashboard/api/offers"); let o=await r.json();
    let formHTML = `<div class="form-box"><h3>Create New Offer</h3>
      <label>Discount %:</label><input type="number" id="off_disc" value="50">
      <label>Target:</label><select id="off_tgt" onchange="document.getElementById('off_cid').style.display=this.value=='single'?'block':'none'"><option value="all">All Courses</option><option value="single">Single Course</option></select>
      <input type="text" id="off_cid" placeholder="Course ID (e.g. c_12345)" style="display:none">
      <label>Max Users (0 for unlimited):</label><input type="number" id="off_max" value="0">
      <label>Per User Limit (1 = one-time only per user, 0 = unlimited):</label><input type="number" id="off_peruser" value="1">
      <label>Active Hours:</label><input type="number" id="off_hrs" value="24">
      <button onclick="createOffer()">Create Offer</button></div>`;
    let listHTML = o.map(x=>`<div class="item ${x.status==='Active'?'ok':'expired'}"><div class="main"><div class="name">${x.discount}% OFF - <span class="mono">${x.offer_code}</span></div><div class="sub">🔗 Link: <a href="${x.link}" target="_blank" style="color:#3ED9A0">${x.link}</a></div><div class="sub">Target: ${x.target} ${x.course?'('+x.course+')':''} | Used: ${x.used}/${x.max==-1?'∞':x.max} | Per User: ${x.per_user==-1?'∞':x.per_user+'x'} | Exp: ${x.expires}</div></div><div><button class="action-btn danger-btn" onclick="delOffer('${x.offer_code}')">🗑</button></div></div>`).join("");
    document.getElementById("list").innerHTML = formHTML + (listHTML||"No offers.");
  } else if(curTab==="logs"){
    r=await fetch("/dashboard/api/channel-logs"); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>`<div class="item ${x.status==='APPROVED'?'ok':'expired'}"><div class="main"><div class="name">${x.first_name} (@${x.username}) - <span class="mono">${x.user_id}</span></div><div class="sub">📺 Channel: <b style="color:var(--text)">${x.channel_name}</b></div><div class="sub">Pack: ${x.course} · ${x.date}</div></div><div style="font-weight:bold; color:var(--${x.status==='APPROVED'?'ok':'danger'})">${x.status}</div></div>`).join("")||"No logs yet.";
  } else if(curTab==="sms"){
    r=await fetch("/dashboard/api/sms-pool"); let o=await r.json();
    let injectHTML = `
      <div class="form-box">
        <h3>🚀 Manual SMS / Payment Injector</h3>
        <p style="font-size:12px; color:var(--muted)">अगर बैंक का SMS नहीं आया, तो यहाँ बस अमाउंट डालें (जैसे <code>50.01</code>) और ट्रिगर दबाएं। बोट खुद पेमेंट को मैच करके डिलीवरी कर देगा।</p>
        <div style="display:flex; gap:10px;">
          <input type="text" id="inject_amt" placeholder="Amount (e.g. 50.01)" style="flex:1;">
          <button onclick="injectSMS()" style="padding:0 20px;">🚀 Trigger Webhook</button>
        </div>
      </div>
    `;
    let listHTML = o.map(x=>`<div class="item pending"><div class="main"><div class="name">₹${x.amount}</div><div class="sub mono">${x.preview}</div></div><div><div class="sub">${x.created_at}</div></div></div>`).join("")||"No SMS in pool.";
    document.getElementById("list").innerHTML = injectHTML + listHTML;
  } else if(curTab==="broadcast") {
    document.getElementById("list").innerHTML = `
      <div class="form-box">
        <h3>📢 Send Global Broadcast</h3>
        <p style="font-size:12px; color:var(--muted)">यहाँ से सभी बोट यूज़र्स को सीधा मैसेज भेजें।</p>
        <textarea id="bc_msg" rows="5" placeholder="HTML मैसेज टाइप करें (जैसे <b>Hello Users</b>)..."></textarea>
        <button onclick="sendDashboardBroadcast()">🚀 Send Broadcast to All</button>
      </div>
    `;
  } else if(curTab==="settings") {
    r=await fetch("/dashboard/api/system-settings"); let cfg=await r.json();
    document.getElementById("list").innerHTML = `
      <div class="form-box">
        <h3>🛑 Maintenance Mode</h3>
        <div style="margin-bottom: 20px;">
          <span style="font-weight:bold;">Maintenance Mode:</span>
          <label class="switch">
            <input type="checkbox" id="maint_toggle" ${cfg.maintenance ? 'checked' : ''} onchange="saveSettings()">
            <span class="slider"></span>
          </label>
        </div>
        <hr style="border-color:var(--line); margin: 20px 0;">
        <h3>🧹 Auto Chat-Delete Settings</h3>
        <label>Auto-Clear Timer:</label>
        <select id="cleanup_time" onchange="saveSettings()">
            <option value="0" ${cfg.cleanup_seconds==0?'selected':''}>Disabled (Off)</option>
            <option value="3600" ${cfg.cleanup_seconds==3600?'selected':''}>1 Hour</option>
            <option value="43200" ${cfg.cleanup_seconds==43200?'selected':''}>12 Hours</option>
            <option value="86400" ${cfg.cleanup_seconds==86400?'selected':''}>24 Hours</option>
            <option value="172800" ${cfg.cleanup_seconds==172800?'selected':''}>48 Hours</option>
        </select>
        <label class="checkbox-lbl"><input type="checkbox" id="auto_del_promos" ${cfg.auto_del_promos?'checked':''} onchange="saveSettings()"> 🗑️ Auto-Delete Promos, Menus & Courses</label>
        <label class="checkbox-lbl"><input type="checkbox" id="auto_del_broadcasts" ${cfg.auto_del_broadcasts?'checked':''} onchange="saveSettings()"> 🗑️ Auto-Delete Broadcast Messages</label>
        <label class="checkbox-lbl"><input type="checkbox" id="auto_del_purchases" ${cfg.auto_del_purchases?'checked':''} onchange="saveSettings()"> <span style="color:var(--danger)">⚠️ Auto-Delete Purchased Links</span></label>
        <hr style="border-color:var(--line); margin: 20px 0;">
        <button style="background-color:var(--danger);" onclick="forceClearChats()">🧹 Clear Selected Chats Now</button>
      </div>
    `;
  } else {
    r=await fetch("/dashboard/api/users?search="+encodeURIComponent(searchUserQ)); let o=await r.json();
    let searchBox = `<div class="form-box"><h3>🔍 Search User</h3><div style="display:flex; gap:10px;"><input type="text" id="user_search_input" placeholder="Search by Username or User ID..." value="${searchUserQ}"><button onclick="searchUserQ=document.getElementById('user_search_input').value; load();" style="padding:0 20px;">Search</button><button onclick="searchUserQ=''; load();" style="background:var(--danger); padding:0 15px;">Reset</button></div></div>`;
    let usersList = o.map(x=>{
      let purchasesHTML = x.purchases.length > 0 ? x.purchases.map(p=>`<li><b>${p.title}</b> - <a href="${p.link}" target="_blank" style="color:var(--ok)">Link</a></li>`).join("") : "<span style='color:var(--muted)'>No purchases</span>";
      return `<div class="item ok" style="flex-direction:column; align-items:flex-start;"><div style="width:100%; display:flex; justify-content:space-between;"><div><b>User ID:</b> <span class="mono">${x.user_id}</span> | <b>Username:</b> @${x.username}</div><div class="sub">Active: ${x.updated_at}</div></div><div style="margin-top:8px; width:100%; background:var(--bg); padding:8px; border-radius:4px;"><ul style="margin:0; padding-left:15px; font-size:12px;">${purchasesHTML}</ul></div></div>`;
    }).join("")||"No users found.";
    document.getElementById("list").innerHTML = searchBox + usersList;
  }
}

async function injectSMS(){
  let amt = document.getElementById("inject_amt").value.trim();
  if(!amt) return alert("Amount enter karein!");
  let res = await fetch("/dashboard/api/inject-sms", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({amount: amt})
  });
  let d = await res.json();
  alert("Result: " + d.result);
  load();
}

async function sendDashboardBroadcast(){
  let msg = document.getElementById("bc_msg").value.trim();
  if(!msg) return alert("Message cannot be empty!");
  if(confirm("Send broadcast to ALL users?")){
    await fetch("/dashboard/api/broadcast", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({message: msg})
    });
    alert("Broadcast started in background!");
    document.getElementById("bc_msg").value = "";
  }
}

async function canO(id){ if(confirm("Cancel this order?")){ await fetch("/dashboard/api/orders/"+id+"/cancel",{method:"POST"}); load(); } }
async function appr(id){ if(confirm("Approve order manually?")){ await fetch("/dashboard/api/orders/"+id+"/approve",{method:"POST"}); load(); } }
async function delC(id){ if(confirm("Delete course?")){ await fetch("/dashboard/api/courses/"+id,{method:"DELETE"}); load(); } }
async function delOffer(id){ if(confirm("Delete this offer?")){ await fetch("/dashboard/api/offers/"+id,{method:"DELETE"}); load(); } }
async function createOffer(){
  let d = { discount: document.getElementById('off_disc').value, target_type: document.getElementById('off_tgt').value, course_id: document.getElementById('off_cid').value, max_users: document.getElementById('off_max').value==-1?-1:(document.getElementById('off_max').value==0?-1:document.getElementById('off_max').value), per_user_limit: document.getElementById('off_peruser').value==0?-1:document.getElementById('off_peruser').value, hours: document.getElementById('off_hrs').value };
  await fetch("/dashboard/api/offers", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(d)}); load();
}
async function saveSettings(){
  await fetch("/dashboard/api/system-settings", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      maintenance: document.getElementById("maint_toggle").checked,
      cleanup_seconds: document.getElementById("cleanup_time").value,
      auto_del_promos: document.getElementById("auto_del_promos").checked,
      auto_del_broadcasts: document.getElementById("auto_del_broadcasts").checked,
      auto_del_purchases: document.getElementById("auto_del_purchases").checked
    })
  });
}

document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>{ document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active")); t.classList.add("active"); curTab=t.dataset.tab; document.getElementById("subtabs").style.display=curTab==="orders"?"flex":"none"; load(); }));
document.querySelectorAll(".subtab").forEach(t=>t.addEventListener("click",()=>{ document.querySelectorAll(".subtab").forEach(x=>x.classList.remove("active")); t.classList.add("active"); curSt=t.dataset.status; load(); }));
setInterval(()=>{ document.querySelectorAll("[data-remain]").forEach(el=>{ let s=Math.max(0,el.dataset.remain-1); el.dataset.remain=s; el.textContent=fmtSecs(s)+" bacha"; }); document.getElementById("clock").textContent=new Date().toLocaleTimeString("en-IN"); }, 1000);
load(); setInterval(load, 15000);
</script></body></html>"""

@app.route("/dashboard")
@require_auth
def dashboard_page(): return DASHBOARD_HTML

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
            
            # 1. 🗑️ 24h Auto-Purge: EXPIRED / CANCELLED ऑर्डर्स को परमानेंट डिलीट करना ताकि स्लॉट्स पूरी तरह खाली हो जाएं
            purge_cutoff = now - 86400 # 24 Hours
            orders_col.delete_many({"status": {"$in": ["EXPIRED", "CANCELLED"]}, "created_at": {"$lt": purge_cutoff}})
            
            # 2. 🧹 मेमोरी कैश साफ़ करना
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
            bot.infinity_polling(timeout=20, long_polling_timeout=15, skip_pending=True, logger_level=None)
        except Exception:
            time.sleep(5)

if __name__ == "__main__":
    try: BOT_USERNAME = bot.get_me().username
    except Exception: pass
    
    restore_pending_orders()
    threading.Thread(target=global_sms_checker, daemon=True).start()
    threading.Thread(target=global_memory_cleanup, daemon=True).start()
    threading.Thread(target=start_polling, daemon=True).start()
    
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

from flask import Flask, request, jsonify, Response
from functools import wraps
from PIL import Image, ImageDraw
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
# 🛑 ENVIRONMENT VARIABLES
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "telegram_store_bot")

UPI_ID = os.environ.get("UPI_ID")
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "Store")
SMS_HOOK_SECRET = os.environ.get("SMS_HOOK_SECRET")

CHAT_LINK = os.environ.get("CHAT_LINK")
INTERNATIONAL_LINK = os.environ.get("INTERNATIONAL_LINK")

PROTECT_CONTENT = os.environ.get("PROTECT_CONTENT", "False").strip().lower() == "true"
QR_EXPIRY_SECONDS = int(os.environ.get("QR_EXPIRY_MINUTES", "10")) * 60
STALE_ORDER_SECONDS = int(os.environ.get("STALE_ORDER_HOURS", "24")) * 3600
SMS_POOL_TTL_HOURS = int(os.environ.get("SMS_POOL_TTL_HOURS", "5"))
ORDER_TTL_HOURS = int(os.environ.get("ORDER_TTL_HOURS", "48"))

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme123")

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID"))
    DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID"))
except (TypeError, ValueError):
    print("❌ ERROR: 'ADMIN_ID' or 'DB_CHANNEL_ID' Environment Variable is not set properly.")
    sys.exit(1)

if not BOT_TOKEN or not MONGO_URI or not UPI_ID or not SMS_HOOK_SECRET:
    print("❌ ERROR: Required Environment Variables are missing.")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN, num_threads=20)
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_time():
    return datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p")

# ==========================================
# 🍃 MONGODB SETUP
# ==========================================
try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
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

    try:
        sms_pool_col.create_index("created_at_dt", expireAfterSeconds=SMS_POOL_TTL_HOURS * 3600)
        orders_col.create_index("created_at_dt", expireAfterSeconds=ORDER_TTL_HOURS * 3600)
        orders_col.create_index([("user_id", 1), ("offer_id", 1), ("status", 1)])
        orders_col.create_index([("course_id", 1), ("status", 1)])
    except Exception:
        pass
    print(f"✅ MongoDB Connected! (Database: {MONGO_DB_NAME})")
except Exception as e:
    print(f"❌ MongoDB Error: {e}")
    sys.exit(1)

# ==========================================
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
    if not isinstance(chat_id, int) or chat_id <= 0 or chat_id == ADMIN_ID: return
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

def generate_upi_qr(amount, order_id):
    clean_amt = re.sub(r"[^\d.]", "", str(amount))
    upi_url = f"upi://pay?pa={UPI_ID}&pn={MERCHANT_NAME}&am={clean_amt}&cu=INR&tn=Order_{order_id}"
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=2)
    qr.add_data(upi_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    w, h = qr_img.size
    box_w, box_h = int(w * 0.28), int(int(w * 0.28) * 0.40)
    box_x, box_y = (w - box_w) // 2, (h - box_h) // 2
    draw = ImageDraw.Draw(qr_img)
    draw.rounded_rectangle([box_x, box_y, box_x + box_w, box_y + box_h], radius=6, fill="#ffffff", outline="#0b1329", width=2)
    lw = max(2, int(box_h * 0.12))
    let_w, let_h, gap = box_w * 0.18, box_h * 0.45, box_w * 0.08
    start_x = box_x + (box_w - ((let_w * 2) + gap * 2 + lw)) // 2
    start_y = box_y + (box_h - let_h) // 2
    u_x = start_x
    draw.line([(u_x, start_y), (u_x, start_y + let_h)], fill="#097939", width=lw)
    draw.line([(u_x, start_y + let_h), (u_x + let_w, start_y + let_h)], fill="#097939", width=lw)
    draw.line([(u_x + let_w, start_y + let_h), (u_x + let_w, start_y)], fill="#097939", width=lw)
    p_x = u_x + let_w + gap
    draw.line([(p_x, start_y), (p_x, start_y + let_h)], fill="#F37021", width=lw)
    draw.line([(p_x, start_y), (p_x + let_w, start_y)], fill="#F37021", width=lw)
    draw.line([(p_x + let_w, start_y), (p_x + let_w, start_y + let_h // 2)], fill="#F37021", width=lw)
    draw.line([(p_x + let_w, start_y + let_h // 2), (p_x, start_y + let_h // 2)], fill="#F37021", width=lw)
    i_x = p_x + let_w + gap + lw // 2
    draw.line([(i_x, start_y), (i_x, start_y + let_h)], fill="#1a73e8", width=lw)
    bio = io.BytesIO()
    qr_img.save(bio, "PNG")
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

# --- SEQUENTIAL PAISE COUNTER (.01 to .99) ---
def generate_unique_amount(base_amount):
    global paise_counter
    base_clean = int(float(base_amount))
    with paise_lock:
        paise_counter = (paise_counter % 99) + 1
        current_p = paise_counter

    candidate = f"{base_clean + (current_p / 100.0):.2f}"
    with pending_lock:
        if candidate not in pending_orders:
            return candidate
        for offset in range(1, 100):
            alt_p = ((current_p + offset - 1) % 99) + 1
            alt_candidate = f"{base_clean + (alt_p / 100.0):.2f}"
            if alt_candidate not in pending_orders:
                return alt_candidate
    return candidate

# ==========================================
# 🎟 DYNAMIC PRICE & OFFER VALIDATION
# ==========================================
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

    succ_cfg = get_cached_setting("success_msg_cfg") or {}
    extra_txt = succ_cfg.get("text", "").strip()
    succ_btns = succ_cfg.get("buttons", [])

    final_text = f"🎉 <b>Payment Verified Successfully!</b>\n\n{course.get('secret_text','')}"
    if extra_txt: final_text += f"\n\n{extra_txt}"

    markup = InlineKeyboardMarkup()
    for b in succ_btns:
        b_url = b.get("url", "")
        if b_url.lower() == "close": markup.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
        elif b_url.startswith("http"): markup.row(InlineKeyboardButton(b["text"], url=b_url))
        else: markup.row(InlineKeyboardButton(b["text"], callback_data=b_url))
    
    if not markup.keyboard: markup = None

    try: bot.send_message(chat_id, final_text, reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="purchase")
    except Exception: pass

    date_now = get_ist_time()
    verify_type = "MANUAL-APPROVED" if is_manual else "AUTO-VERIFIED"
    
    def _bg_log():
        purchases_col.insert_one({"user_id": user_id, "username": order.get("user_mention", f"User ({user_id})"), "item_info": f"{course_id} | Rate: ₹{order['amount']} | {verify_type} (order {order_id})", "date": date_now, "title": course.get('title', course_id), "link": course.get('secret_text', '')})
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
@bot.chat_join_request_handler()
def handle_join_request(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    used_link = message.invite_link.invite_link if message.invite_link else ""
    
    course = None
    for c in courses_col.find({"channel_id": chat_id}):
        if c.get("secret_text", "") and used_link in c.get("secret_text", ""):
            course = c
            break
            
    if not course: return
        
    course_id = course["course_id"]
    channel_name = course.get("channel_name") or message.chat.title or "Private Channel/Group"
    purchase = purchases_col.find_one({"user_id": user_id, "item_info": {"$regex": course_id}})
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
            orig_send_message(user_id, f"✅ <b>Request Approved!</b>\nWelcome to <b>{channel_name}</b>.", parse_mode="HTML")
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

    if not media_items:
        full_text = "".join(t.get("caption", "") + "\n\n" for t in text_items) + custom_caption
        if not full_text.strip(): full_text = f"📚 <b>Pack: {course.get('title', course['course_id'])}</b>\nPrice: ₹{int(final_price) if final_price.is_integer() else final_price}"
        bot.send_message(chat_id, full_text.strip(), reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
    elif len(media_items) == 1:
        it = media_items[0]
        try:
            if it["type"] == "photo": bot.send_photo(chat_id, it["file_id"], caption=final_cap, reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
            elif it["type"] == "video": bot.send_video(chat_id, it["file_id"], caption=final_cap, reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
        except Exception: pass
    else:
        media_group_html = []
        for i, item in enumerate(media_items):
            cap = final_cap if i == 0 else ""
            if item["type"] == "photo": media_group_html.append(InputMediaPhoto(item["file_id"], caption=cap, parse_mode="HTML"))
            elif item["type"] == "video": media_group_html.append(InputMediaVideo(item["file_id"], caption=cap, parse_mode="HTML"))
        try:
            sent_grp = orig_send_media_group(chat_id, media_group_html, protect_content=PROTECT_CONTENT)
            for m in sent_grp: register_activity(chat_id, m.message_id, "course")
        except Exception: pass
        try: bot.send_message(chat_id, "👆 <b>Choose an option to buy:</b>\n", reply_markup=markup, parse_mode="HTML", msg_type="course")
        except Exception: pass

def send_batch_to_user(chat_id, batch):
    bot.send_message(chat_id, f"📦 <b>{batch['title']}</b>\nAll packs are listed below:", parse_mode="HTML", msg_type="course")
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
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("📋 View All Plans / Packs", callback_data="user_view_plans"))
    markup.row(InlineKeyboardButton("🛍 My Purchases", callback_data="user_my_purchases_btn"))
    
    if cfg:
        for b in cfg.get("buttons", []):
            b_url = b.get("url", "")
            if b_url.lower() == "close": markup.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
            elif b_url.startswith("http"): markup.row(InlineKeyboardButton(b["text"], url=b_url))
            else: markup.row(InlineKeyboardButton(b["text"], callback_data=f"mainmenu_{b_url}"))
        
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

def send_admin_panel(chat_id):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("➕ Add Single Pack", callback_data="admin_add_course"))
    markup.row(InlineKeyboardButton("✏️ Edit Course", callback_data="admin_edit_course"))
    markup.row(InlineKeyboardButton("🗑 Delete Pack", callback_data="admin_delete_course"))
    markup.row(InlineKeyboardButton("📦 Pack Batch (Multi-Pack)", callback_data="admin_create_batch"))
    markup.row(InlineKeyboardButton("🎟 Create Promo Offer", callback_data="admin_create_offer"))
    markup.row(InlineKeyboardButton("📋 Manage Store Plans", callback_data="admin_manage_plans"))
    markup.row(InlineKeyboardButton("⚡ Flash Price (Hike/Drop)", callback_data="admin_flash_price"))
    markup.row(InlineKeyboardButton("🎨 Customize Start Menu", callback_data="admin_custom_menu"))
    markup.row(InlineKeyboardButton("🌍 Custom International Button", callback_data="admin_custom_intl"))
    markup.row(InlineKeyboardButton("🔘 Global Course Buttons", callback_data="admin_global_cbtns"))
    markup.row(InlineKeyboardButton("🎉 Post-Purchase Setup", callback_data="admin_success_msg"))
    markup.row(InlineKeyboardButton("🔗 Advanced File to Link", callback_data="admin_file_link"))
    markup.row(InlineKeyboardButton("📢 Advanced Broadcast", callback_data="admin_broadcast"))
    markup.row(InlineKeyboardButton("👥 User Info", callback_data="admin_user_info"))
    bot.send_message(chat_id, "🛠 <b>Admin Panel</b>\nPlease select an option:\n", reply_markup=markup, parse_mode="HTML")

# ==========================================
# COMMANDS & HANDLERS
# ==========================================
@bot.message_handler(commands=["start"])
def start_command(message):
    user_id = message.chat.id
    
    if is_maintenance_mode() and user_id != ADMIN_ID:
        orig_send_message(user_id, "⚠️ <b>Bot is currently under maintenance.</b>\nServers are busy or undergoing updates. Please try again after some time.", parse_mode="HTML")
        return

    if not check_rate_limit(user_id, 1): return
    register_activity(user_id, message.message_id, "general")
    
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
                if len(m_items) == 1:
                    it = m_items[0]
                    try:
                        if it["type"] == "text": bot.send_message(user_id, it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
                        elif it["type"] == "photo": bot.send_photo(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
                        elif it["type"] == "video": bot.send_video(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
                        elif it["type"] == "document": bot.send_document(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT, msg_type="course")
                    except Exception as e: bot.send_message(user_id, f"❌ Error: {e}", msg_type="general")
                elif len(m_items) > 1:
                    m_group = []
                    for it in m_items:
                        if it["type"] == "photo": m_group.append(InputMediaPhoto(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                        elif it["type"] == "video": m_group.append(InputMediaVideo(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                        elif it["type"] == "document": m_group.append(InputMediaDocument(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                    try:
                        sent = orig_send_media_group(user_id, m_group, protect_content=PROTECT_CONTENT)
                        for m in sent: register_activity(user_id, m.message_id, "course")
                        if btns or any(i["type"] == "text" for i in m_items): bot.send_message(user_id, "👇", reply_markup=markup, parse_mode="HTML", msg_type="course")
                    except Exception as e: bot.send_message(user_id, f"❌ Error: {e}", msg_type="general")
            threading.Thread(target=_sf, daemon=True).start()
        else: bot.send_message(user_id, "❌ <b>File not found or expired.</b>", parse_mode="HTML", msg_type="general")
    else:
        if user_id == ADMIN_ID: send_admin_panel(user_id)
        else: send_custom_start_menu(user_id)

@bot.message_handler(content_types=["photo", "video", "document", "text"])
def handle_all_messages(message):
    user_id = message.chat.id
    if is_maintenance_mode() and user_id != ADMIN_ID:
        orig_send_message(user_id, "⚠️ <b>Bot is currently under maintenance.</b>\nServers are busy or undergoing updates. Please try again after some time.", parse_mode="HTML")
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

    if user_id == ADMIN_ID and user_id in admin_data:
        step = admin_data[ADMIN_ID].get("step")
        if step == "DELETE_COURSE":
            cid = message.text.strip()
            if courses_col.delete_one({"course_id": cid}).deleted_count:
                settings_col.update_one({"_id": "store_plans"}, {"$pull": {"course_ids": cid}})
                invalidate_cache(f"course_{cid}")
                bot.send_message(ADMIN_ID, f"✅ <b>Course <code>{cid}</code> Deleted!</b>", parse_mode="HTML")
            else: bot.send_message(ADMIN_ID, "❌ <b>Not found.</b>", parse_mode="HTML")
            del admin_data[ADMIN_ID]
            return send_admin_panel(ADMIN_ID)
        elif step == "ADD_PLAN_ID":
            cid = message.text.strip()
            if courses_col.find_one({"course_id": cid}):
                settings_col.update_one({"_id": "store_plans"}, {"$addToSet": {"course_ids": cid}}, upsert=True)
                invalidate_cache("store_plans")
                bot.send_message(ADMIN_ID, f"✅ <b>Course <code>{cid}</code> added to Store Plans!</b>", parse_mode="HTML")
            else: bot.send_message(ADMIN_ID, "❌ <b>Invalid ID.</b>", parse_mode="HTML")
            del admin_data[ADMIN_ID]
            return send_admin_panel(ADMIN_ID)
        elif step == "EDIT_COURSE_ID":
            cid = message.text.strip()
            course = courses_col.find_one({"course_id": cid})
            if not course:
                bot.send_message(ADMIN_ID, "❌ Course ID not found. Send correct ID:")
                return
            admin_data[ADMIN_ID]["course_id"] = cid
            admin_data[ADMIN_ID]["step"] = "EDIT_COURSE_TEXT"
            bot.send_message(ADMIN_ID, f"✅ Found course: <b>{course.get('title', cid)}</b>\n\nSend new <b>Text/Description</b> (or type <code>skip</code> to keep old):", parse_mode="HTML")
            return
        elif step == "EDIT_COURSE_TEXT":
            txt = get_formatted_text(message)
            if txt.lower() != "skip":
                courses_col.update_one({"course_id": admin_data[ADMIN_ID]["course_id"]}, {"$set": {"custom_caption": txt}})
            admin_data[ADMIN_ID]["step"] = "EDIT_COURSE_PHOTO"
            bot.send_message(ADMIN_ID, "✅ Text updated!\n\nNow send new <b>Photo</b> (or type <code>skip</code> if photo change nahi karni):", parse_mode="HTML")
            return
        elif step == "EDIT_COURSE_PHOTO":
            cid = admin_data[ADMIN_ID]["course_id"]
            if message.photo:
                photo_id = message.photo[-1].file_id
                promo_data = [{"type": "photo", "file_id": photo_id, "caption": ""}]
                courses_col.update_one({"course_id": cid}, {"$set": {"promo_media": promo_data}})
            bot.send_message(ADMIN_ID, f"🎉 <b>Course <code>{cid}</code> successfully updated!</b>", parse_mode="HTML")
            del admin_data[ADMIN_ID]
            return send_admin_panel(ADMIN_ID)
        elif step == "FLASH_PCT":
            try:
                pct = float(re.sub(r"[^\d.]", "", message.text.strip()))
                admin_data[ADMIN_ID]["percent"] = pct
                admin_data[ADMIN_ID]["step"] = "FLASH_TARGET"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("🌐 All Courses", callback_data="flash_tgt_all")).row(InlineKeyboardButton("🎯 Single Course", callback_data="flash_tgt_single"))
                bot.send_message(ADMIN_ID, f"✅ <b>{pct}% Set!</b>\nApply to:", reply_markup=m, parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ Numbers only.")
            return
        elif step == "FLASH_CID":
            cid = message.text.strip()
            if not courses_col.find_one({"course_id": cid}): return bot.send_message(ADMIN_ID, "❌ Course ID not found.")
            admin_data[ADMIN_ID]["course_id"] = cid
            admin_data[ADMIN_ID]["step"] = "FLASH_HOURS"
            bot.send_message(ADMIN_ID, "⏳ <b>Active for how many hours?</b> (e.g., 24)", parse_mode="HTML")
            return
        elif step == "FLASH_HOURS":
            try:
                hrs = float(re.sub(r"[^\d.]", "", message.text.strip()))
                admin_data[ADMIN_ID]["hours"] = hrs
                admin_data[ADMIN_ID]["step"] = "FLASH_LIMIT"
                bot.send_message(ADMIN_ID, "🔁 <b>How many times can a user buy at this new price?</b>\n(1 = Once per user, 0 = Unlimited):", parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid hours.")
            return
        elif step == "FLASH_LIMIT":
            try:
                lim = int(re.sub(r"[^\d]", "", message.text.strip()))
                sale_id = "fs_" + str(uuid.uuid4())[:6]
                now_ts = time.time()
                doc = {
                    "_id": "flash_sale", "sale_id": sale_id, "mode": admin_data[ADMIN_ID]["flash_mode"],
                    "percent": admin_data[ADMIN_ID]["percent"], "target": admin_data[ADMIN_ID]["target"],
                    "course_id": admin_data[ADMIN_ID].get("course_id"), "hours": admin_data[ADMIN_ID]["hours"],
                    "limit": lim, "created_at_ts": now_ts, "expires_at_ts": now_ts + (admin_data[ADMIN_ID]["hours"] * 3600)
                }
                settings_col.update_one({"_id": "flash_sale"}, {"$set": doc}, upsert=True)
                invalidate_cache("flash_sale")
                bot.send_message(ADMIN_ID, "🎉 <b>Flash Price Applied Successfully!</b>", parse_mode="HTML")
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid limit.")
            return
        elif step == "OFFER_DISCOUNT":
            try:
                disc = int(re.sub(r"[^\d]", "", message.text.strip()))
                if not (1 <= disc <= 100): raise ValueError()
                admin_data[ADMIN_ID]["discount"], admin_data[ADMIN_ID]["step"] = disc, "OFFER_TARGET"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("🌐 All Courses", callback_data="offtarget_all")).row(InlineKeyboardButton("🎯 Single Course", callback_data="offtarget_single"))
                bot.send_message(ADMIN_ID, f"✅ Discount <b>{disc}%</b> set!\nApply to:", reply_markup=m, parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ 1 to 100 only.")
            return
        elif step == "OFFER_SINGLE_CID":
            cid = message.text.strip()
            if not courses_col.find_one({"course_id": cid}): return bot.send_message(ADMIN_ID, "❌ <b>Course ID not found. Send correct ID:</b>", parse_mode="HTML")
            admin_data[ADMIN_ID]["target_course_id"], admin_data[ADMIN_ID]["step"] = cid, "OFFER_LIMIT"
            bot.send_message(ADMIN_ID, "👥 <b>Max users?</b> (0 for unlimited):", parse_mode="HTML")
            return
        elif step == "OFFER_LIMIT":
            try:
                lim = int(re.sub(r"[^\d]", "", message.text.strip()))
                admin_data[ADMIN_ID]["max_users"], admin_data[ADMIN_ID]["step"] = -1 if lim == 0 else lim, "OFFER_PERUSER"
                bot.send_message(ADMIN_ID, "🔁 <b>How many times can a user claim this offer?</b>\n(<b>1</b> = just once per user. <b>0</b> = unlimited):", parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid number.")
            return
        elif step == "OFFER_PERUSER":
            try:
                pl = int(re.sub(r"[^\d]", "", message.text.strip()))
                admin_data[ADMIN_ID]["per_user_limit"], admin_data[ADMIN_ID]["step"] = -1 if pl == 0 else pl, "OFFER_HOURS"
                bot.send_message(ADMIN_ID, "⏳ <b>Active for how many hours?</b> (e.g. 24 or 48):", parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid number.")
            return
        elif step == "OFFER_HOURS":
            try:
                hrs = float(re.sub(r"[^\d.]", "", message.text.strip()))
                off_code, now_ts = "off_" + str(uuid.uuid4())[:6], time.time()
                doc = {
                    "offer_code": off_code, "discount_percent": admin_data[ADMIN_ID]["discount"],
                    "target_type": admin_data[ADMIN_ID]["target_type"], "target_course_id": admin_data[ADMIN_ID].get("target_course_id"),
                    "max_users": admin_data[ADMIN_ID]["max_users"], "per_user_limit": admin_data[ADMIN_ID].get("per_user_limit", 1), "used_count": 0, "created_at_ts": now_ts,
                    "expires_at_ts": now_ts + (hrs * 3600), "expires_str": (datetime.now(IST) + timedelta(hours=hrs)).strftime("%d-%m-%Y %I:%M %p")
                }
                offers_col.insert_one(doc)
                bot.send_message(ADMIN_ID, f"🎉 <b>Discount Offer Link Created!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={off_code}</code>", parse_mode="HTML")
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid hours.")
            return
        elif step == "MENU_CUSTOM_CONTENT":
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            admin_data[ADMIN_ID]["menu_content"] = {"media_type": mt, "file_id": fid, "text": get_formatted_text(message)}
            admin_data[ADMIN_ID]["buttons"], admin_data[ADMIN_ID]["step"] = [], "MENU_ADD_BUTTONS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="menu_finish_save"))
            bot.send_message(ADMIN_ID, "✅ <b>Content Saved!</b>\nAdd buttons: <code>Button Name - Link</code> (or type <code>Close - close</code>).", reply_markup=m, parse_mode="HTML")
            return
        elif step == "MENU_ADD_BUTTONS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save Menu", callback_data="menu_finish_save"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! ({len(admin_data[ADMIN_ID]['buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error. <code>Name - Link</code>", parse_mode="HTML")
            return
        elif step == "GCBTN_ADD":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="gcbtn_finish"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Global Button Added! ({len(admin_data[ADMIN_ID]['buttons'])})</b>\nSend another or Finish:", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error. <code>Name - Link</code>", parse_mode="HTML")
            return
        elif step == "SUCC_SET_TEXT":
            admin_data[ADMIN_ID]["text"] = get_formatted_text(message)
            admin_data[ADMIN_ID]["buttons"] = []
            admin_data[ADMIN_ID]["step"] = "SUCC_ADD_BTNS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="succ_finish"))
            bot.send_message(ADMIN_ID, "✅ <b>Text Saved!</b>\n\n🔘 <b>Step 2: Add Buttons</b>\nFormat: <code>Button Text - URL</code>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "SUCC_ADD_BTNS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="succ_finish"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! ({len(admin_data[ADMIN_ID]['buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error. <code>Name - Link</code>", parse_mode="HTML")
            return
        elif step == "INTL_SET_NAME":
            b_name = message.text.strip()
            if not b_name: return bot.send_message(ADMIN_ID, "❌ Name cannot be empty.")
            admin_data[ADMIN_ID]["intl_name"] = b_name
            admin_data[ADMIN_ID]["step"] = "INTL_SET_CONTENT"
            bot.send_message(ADMIN_ID, f"✅ <b>Button Name Saved:</b> <code>{b_name}</code>\n\n📝 Send Photo with Caption OR Plain Text:", parse_mode="HTML")
            return
        elif step == "INTL_SET_CONTENT":
            fid = message.photo[-1].file_id if message.photo else None
            txt = get_formatted_text(message)
            admin_data[ADMIN_ID]["intl_photo"] = fid
            admin_data[ADMIN_ID]["intl_text"] = txt
            admin_data[ADMIN_ID]["intl_buttons"] = []
            admin_data[ADMIN_ID]["step"] = "INTL_ADD_BUTTONS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="intl_finish_save"))
            bot.send_message(ADMIN_ID, "✅ <b>Content Saved!</b> Add Action Buttons: <code>Button Text - URL</code>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "INTL_ADD_BUTTONS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["intl_buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="intl_finish_save"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! ({len(admin_data[ADMIN_ID]['intl_buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error.", parse_mode="HTML")
            return
        elif step == "PROMO":
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            admin_data[ADMIN_ID]["promo"].append({"type": mt, "file_id": fid, "caption": get_formatted_text(message)})
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step (Price)", callback_data="next_price"))
            bot.send_message(ADMIN_ID, f"✅ <b>{mt.capitalize()} saved!</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "AMOUNT":
            amt = re.sub(r"[^\d.]", "", message.text.strip())
            if not amt: return bot.send_message(ADMIN_ID, "❌ <b>Numbers only.</b>", parse_mode="HTML")
            admin_data[ADMIN_ID]["amount"], admin_data[ADMIN_ID]["step"] = amt, "CAPTION"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Skip (No Caption)", callback_data="skip_caption"))
            bot.send_message(ADMIN_ID, f"✅ <b>Price ₹{amt} saved!</b>\n📝 Type optional extra caption, or skip:", reply_markup=m, parse_mode="HTML")
            return
        elif step == "CAPTION":
            admin_data[ADMIN_ID]["caption"] = get_formatted_text(message)
            admin_data[ADMIN_ID]["course_buttons"] = []
            admin_data[ADMIN_ID]["step"] = "COURSE_BUTTONS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Finish Buttons", callback_data="skip_course_buttons"))
            bot.send_message(ADMIN_ID, "✅ <b>Caption saved!</b>\n🔘 Optional Extra Buttons: <code>Button Text - URL</code>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "COURSE_BUTTONS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["course_buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Finish Buttons", callback_data="skip_course_buttons"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! ({len(admin_data[ADMIN_ID]['course_buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error.", parse_mode="HTML")
            return
        elif step == "SECRET":
            cid = "c_" + str(uuid.uuid4())[:6]
            courses_col.update_one({"course_id": cid}, {"$set": {
                "course_id": cid, "title": admin_data[ADMIN_ID].get("title", cid), "promo_media": admin_data[ADMIN_ID]["promo"], "amount": admin_data[ADMIN_ID]["amount"],
                "custom_caption": admin_data[ADMIN_ID].get("caption",""), "secret_text": get_formatted_text(message),
                "extra_buttons": admin_data[ADMIN_ID].get("course_buttons", [])
            }}, upsert=True)
            if admin_data[ADMIN_ID].get("mode") == "single":
                bot.send_message(ADMIN_ID, f"🎉 <b>Pack created!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={cid}</code>", parse_mode="HTML")
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
            elif admin_data[ADMIN_ID].get("mode") == "batch":
                admin_data[ADMIN_ID]["course_ids"].append(cid)
                admin_data[ADMIN_ID]["step"] = "NEXT_ACTION"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Another", callback_data="batch_add_next")).row(InlineKeyboardButton("✅ Finish Batch", callback_data="batch_finish"))
                bot.send_message(ADMIN_ID, "✅ <b>Pack saved!</b>", reply_markup=m, parse_mode="HTML")
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
                return bot.send_message(ADMIN_ID, "❌ Please forward a message from the Private Channel/Group or send its link.")
            
            try:
                chat_info = bot.get_chat(channel_id)
                channel_name = chat_info.title if chat_info.title else "Private Channel"
                link = bot.create_chat_invite_link(channel_id, creates_join_request=True)
                secret_text = f"👉 <b>Click here to join the Group/Channel:</b>\n{link.invite_link}"
                cid = "c_" + str(uuid.uuid4())[:6]
                
                courses_col.update_one({"course_id": cid}, {"$set": {
                    "course_id": cid, "title": admin_data[ADMIN_ID].get("title", channel_name), "promo_media": admin_data[ADMIN_ID]["promo"], "amount": admin_data[ADMIN_ID]["amount"],
                    "custom_caption": admin_data[ADMIN_ID].get("caption", ""), "secret_text": secret_text, "channel_id": channel_id,
                    "channel_name": channel_name, "extra_buttons": admin_data[ADMIN_ID].get("course_buttons", [])
                }}, upsert=True)
                
                if admin_data[ADMIN_ID].get("mode") == "single":
                    bot.send_message(ADMIN_ID, f"🎉 <b>Channel Pack created! ({channel_name})</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={cid}</code>", parse_mode="HTML")
                    del admin_data[ADMIN_ID]
                    send_admin_panel(ADMIN_ID)
                elif admin_data[ADMIN_ID].get("mode") == "batch":
                    admin_data[ADMIN_ID]["course_ids"].append(cid)
                    admin_data[ADMIN_ID]["step"] = "NEXT_ACTION"
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Another", callback_data="batch_add_next")).row(InlineKeyboardButton("✅ Finish Batch", callback_data="batch_finish"))
                    bot.send_message(ADMIN_ID, "✅ <b>Pack saved!</b>", reply_markup=m, parse_mode="HTML")
            except Exception as e:
                bot.send_message(ADMIN_ID, f"❌ Error: Make sure the bot is an Admin in the Channel/Group first! ({e})")
            return
        elif step == "TITLE":
            admin_data[ADMIN_ID]["title"], admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["promo"] = message.text.strip(), "PROMO", []
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step", callback_data="next_price"))
            bot.send_message(ADMIN_ID, "✅ Title saved. <b>Send promo media/text:</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step in ["BC_MEDIA", "FTL_MEDIA"]:
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            elif message.document: mt, fid = "document", message.document.file_id
            admin_data[ADMIN_ID].setdefault("media", []).append({"type": mt, "file_id": fid, "caption": get_formatted_text(message)})
            cb = "bc_done" if step == "BC_MEDIA" else "ftl_done"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Done Adding", callback_data=cb))
            bot.send_message(ADMIN_ID, "✅ <b>Saved!</b> Send another or click Done.", reply_markup=m, parse_mode="HTML")
            return
        elif step in ["BC_BUTTONS", "FTL_BUTTONS"]:
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID].setdefault("buttons", []).append({"text": t.strip(), "url": u.strip()})
                    cb = "bc_finish" if step == "BC_BUTTONS" else "ftl_finish"
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data=cb))
                    bot.send_message(ADMIN_ID, "✅ <b>Button Added!</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format Error.", parse_mode="HTML")
            return
        elif step == "BC_TIME":
            try:
                hrs = float(re.sub(r"[^\d.]", "", message.text.strip()))
                m_items = admin_data[ADMIN_ID].get("media", [])
                btns = admin_data[ADMIN_ID].get("buttons", [])
                
                m = InlineKeyboardMarkup()
                for b in btns:
                    b_url = b.get("url", "")
                    if b_url.lower() == "close": m.row(InlineKeyboardButton(b["text"], callback_data="close_msg"))
                    elif b_url.startswith("http"): m.row(InlineKeyboardButton(b["text"], url=b_url))
                    else: m.row(InlineKeyboardButton(b["text"], callback_data=b_url))
                
                if not m.keyboard: m = None
                bot.send_message(ADMIN_ID, "⏳ Broadcasting started...")
                
                def run_bc():
                    success = 0
                    delete_list = []
                    all_users = list(users_col.find({}))
                    for u in all_users:
                        raw_uid = u.get("user_id") or u.get("chat_id") or u.get("_id")
                        if not raw_uid: continue
                        try: uid = int(raw_uid)
                        except: continue

                        try:
                            if not m_items:
                                if m:
                                    msg = orig_send_message(uid, "👇", reply_markup=m, parse_mode="HTML", disable_web_page_preview=True)
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
                                    msg = orig_send_message(uid, "👇", reply_markup=m, parse_mode="HTML", disable_web_page_preview=True)
                                    sent_ids.append(msg.message_id)
                                    
                            for mid in sent_ids:
                                register_activity(uid, mid, "broadcast")
                                if hrs > 0: delete_list.append((uid, mid))
                                
                            success += 1
                            time.sleep(0.04)
                        except Exception: pass
                        
                    bot.send_message(ADMIN_ID, f"✅ <b>Broadcast Complete!</b> ({success} users).", parse_mode="HTML")
                    
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
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
                
            except Exception:
                bot.send_message(ADMIN_ID, "❌ Invalid number. Send 0 or higher.")
            return

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

    if is_maintenance_mode() and chat_id != ADMIN_ID:
        try: bot.answer_callback_query(call.id, "⚠️ Bot is currently under maintenance. Please try again later.", show_alert=True)
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

    # --- CHANNEL FORCE ACTIONS (Approve / Cancel) ---
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

    if data.startswith("send_ss_"):
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
                try: wait_msg = bot.send_message(chat_id, "⏳ <i>Generating your UPI QR Code... Just a second!</i>", parse_mode="HTML", msg_type="general")
                except Exception: pass

                state = get_user_state(chat_id)
                if state and state.get("step") == "PENDING_UPI":
                    with pending_lock: pending_orders.pop(state.get("amount_key", ""), None)
                    if chat_id in user_qr_messages:
                        try: bot.delete_message(chat_id, user_qr_messages.pop(chat_id))
                        except Exception: pass
                        
                order_id, amt_key = str(uuid.uuid4())[:8], generate_unique_amount(final_price)
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
                
                # --- CHANNEL INLINE BUTTONS (Approve & Cancel) ---
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
                inv = f"👤 <b>User:</b> {call.from_user.first_name}\n🆔 <b>Order:</b> <code>{order_id}</code>\n💰 <b>Amount:</b> ₹{clean_amt}\n⚠️ <b>Please pay the exact amount shown.</b>\n⏳ <i>QR will expire in {QR_EXPIRY_SECONDS // 60} minutes.</i>"
                
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
    if data == "admin_flash_price":
        m = InlineKeyboardMarkup()
        m.row(InlineKeyboardButton("📈 Price Hike (+)", callback_data="flash_start_hike"), InlineKeyboardButton("📉 Price Drop (-)", callback_data="flash_start_drop"))
        m.row(InlineKeyboardButton("🔄 Reset to Normal Prices", callback_data="flash_reset"))
        m.row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("⚡ <b>Flash Price Manager</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data in ["flash_start_hike", "flash_start_drop"]:
        admin_data[ADMIN_ID] = {"step": "FLASH_PCT", "flash_mode": "hike" if data == "flash_start_hike" else "drop"}
        bot.edit_message_text("✏️ <b>Enter Percentage:</b>\n(e.g., Send 20 for 20%)", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "flash_reset":
        settings_col.delete_one({"_id": "flash_sale"})
        invalidate_cache("flash_sale")
        bot.edit_message_text("✅ <b>All prices reset to normal!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "flash_tgt_all":
        admin_data[ADMIN_ID]["target"] = "all"
        admin_data[ADMIN_ID]["step"] = "FLASH_HOURS"
        bot.edit_message_text("⏳ <b>Active for how many hours?</b> (e.g., 24)", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "flash_tgt_single":
        admin_data[ADMIN_ID]["target"] = "single"
        admin_data[ADMIN_ID]["step"] = "FLASH_CID"
        bot.edit_message_text("🎯 <b>Send Course ID:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "skip_course_buttons":
        admin_data[ADMIN_ID]["step"] = "COURSE_TYPE"
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("📝 Text / Secret Link", callback_data="ctype_text"), InlineKeyboardButton("📢 Private Channel/Group", callback_data="ctype_channel"))
        bot.edit_message_text("✅ <b>Buttons saved!</b>\nWhat will the user get after payment?", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "admin_global_cbtns":
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add/Update Buttons", callback_data="gcbtn_add")).row(InlineKeyboardButton("🗑 Clear All Buttons", callback_data="gcbtn_clear")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("🔘 <b>Global Course Buttons</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "gcbtn_add":
        admin_data[ADMIN_ID] = {"step": "GCBTN_ADD", "buttons": []}
        bot.edit_message_text("➕ Send format: <code>Button Text - URL</code>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "gcbtn_finish":
        d = admin_data.get(ADMIN_ID, {})
        settings_col.update_one({"_id": "global_course_btns"}, {"$set": {"buttons": d.get("buttons", []), "updated_at": get_ist_time()}}, upsert=True)
        invalidate_cache("global_course_btns")
        del admin_data[ADMIN_ID]
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
        admin_data[ADMIN_ID] = {"step": "SUCC_SET_TEXT"}
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Skip Text", callback_data="succ_skip_text"))
        bot.edit_message_text("✏️ Send extra caption/text:", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "succ_skip_text":
        admin_data[ADMIN_ID] = {"step": "SUCC_ADD_BTNS", "text": "", "buttons": []}
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="succ_finish"))
        bot.edit_message_text("🔘 Add Buttons: <code>Button Text - URL</code>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "succ_finish":
        d = admin_data.get(ADMIN_ID, {})
        settings_col.update_one({"_id": "success_msg_cfg"}, {"$set": {"text": d.get("text", ""), "buttons": d.get("buttons", []), "updated_at": get_ist_time()}}, upsert=True)
        invalidate_cache("success_msg_cfg")
        del admin_data[ADMIN_ID]
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
        admin_data[ADMIN_ID] = {"step": "INTL_SET_NAME"}
        bot.edit_message_text("✏️ Send Name (e.g. <code>🌍 International Pay</code>):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "intl_finish_save":
        d = admin_data.get(ADMIN_ID, {})
        doc = {"_id": "intl_btn_cfg", "btn_name": d.get("intl_name", "🌍 International"), "photo_id": d.get("intl_photo"), "text": d.get("intl_text", ""), "buttons": d.get("intl_buttons", []), "updated_at": get_ist_time()}
        settings_col.update_one({"_id": "intl_btn_cfg"}, {"$set": doc}, upsert=True)
        invalidate_cache("intl_btn_cfg")
        del admin_data[ADMIN_ID]
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
        admin_data[ADMIN_ID]["step"] = "SECRET"
        bot.edit_message_text("✅ <b>Send secret link or text:</b>", chat_id, msg_id, parse_mode="HTML")
    elif data == "ctype_channel":
        admin_data[ADMIN_ID]["step"] = "CHANNEL_ID"
        bot.edit_message_text("📢 <b>Forward a message OR send Private Channel Link:</b>", chat_id, msg_id, parse_mode="HTML")
    elif data == "skip_caption" and ADMIN_ID in admin_data:
        admin_data[ADMIN_ID]["caption"] = ""
        admin_data[ADMIN_ID]["course_buttons"] = []
        admin_data[ADMIN_ID]["step"] = "COURSE_BUTTONS"
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Finish Buttons", callback_data="skip_course_buttons"))
        bot.edit_message_text("🔘 Add Extra Buttons: <code>Text - URL</code>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "admin_create_offer":
        admin_data[ADMIN_ID] = {"step": "OFFER_DISCOUNT"}
        bot.edit_message_text("🎟 <b>Enter Discount % (e.g. 50):</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "offtarget_all":
        admin_data[ADMIN_ID]["target_type"], admin_data[ADMIN_ID]["step"] = "all", "OFFER_LIMIT"
        bot.edit_message_text("👥 Max claims? (0 = unlimited):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "offtarget_single":
        admin_data[ADMIN_ID]["target_type"], admin_data[ADMIN_ID]["step"] = "single", "OFFER_SINGLE_CID"
        bot.edit_message_text("🎯 Send Course ID (e.g. <code>c_abc123</code>):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_delete_course":
        admin_data[ADMIN_ID] = {"step": "DELETE_COURSE"}
        bot.edit_message_text("🗑 Send Course ID to delete:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_edit_course":
        admin_data[ADMIN_ID] = {"step": "EDIT_COURSE_ID"}
        bot.edit_message_text("✏️ Send Course ID to edit:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_manage_plans":
        c_ids = (get_cached_setting("store_plans") or {}).get("course_ids", [])
        text = f"📋 <b>Manage Store Plans ({len(c_ids)}):</b>\n" + "".join(f"• <code>{cid}</code>\n" for cid in c_ids)
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Course", callback_data="plan_add_id")).row(InlineKeyboardButton("🗑 Clear All", callback_data="plan_clear_all")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
    elif data == "plan_add_id":
        admin_data[ADMIN_ID] = {"step": "ADD_PLAN_ID"}
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
        admin_data[ADMIN_ID] = {"step": "MENU_CUSTOM_CONTENT"}
        bot.edit_message_text("📝 Send Photo, Video or Text:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "menu_finish_save":
        c = admin_data.get(ADMIN_ID, {}).get("menu_content", {})
        settings_col.update_one({"_id": "start_menu"}, {"$set": {"media_type": c.get("media_type", "text"), "file_id": c.get("file_id"), "text": c.get("text", ""), "buttons": admin_data.get(ADMIN_ID, {}).get("buttons", []), "updated_at": get_ist_time()}}, upsert=True)
        invalidate_cache("start_menu")
        del admin_data[ADMIN_ID]
        bot.edit_message_text("🎉 <b>Saved!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "menu_reset_default":
        settings_col.delete_one({"_id": "start_menu"})
        invalidate_cache("start_menu")
        bot.edit_message_text("✅ <b>Reset!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        send_admin_panel(chat_id)
    elif data == "admin_add_course":
        admin_data[ADMIN_ID] = {"mode": "single", "step": "TITLE", "promo": [], "amount": None, "caption": "", "course_buttons": []}
        bot.edit_message_text("📝 <b>Step 1: Send Course Title</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "admin_create_batch":
        admin_data[ADMIN_ID] = {"mode": "batch", "step": "TITLE", "course_ids": []}
        bot.edit_message_text("📦 <b>Send Batch Title:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data in ["admin_file_link", "admin_broadcast"]:
        admin_data[ADMIN_ID] = {"step": "FTL_MEDIA" if data == "admin_file_link" else "BC_MEDIA", "media": []}
        bot.edit_message_text(f"{'📎 File to Link' if data == 'admin_file_link' else '📢 Broadcast'}\nSend Media/Text.", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "next_price" and ADMIN_ID in admin_data:
        admin_data[ADMIN_ID]["step"] = "AMOUNT"
        bot.edit_message_text("💰 <b>Price (INR)</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "batch_add_next":
        admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["promo"], admin_data[ADMIN_ID]["caption"], admin_data[ADMIN_ID]["course_buttons"] = "TITLE", [], "", []
        bot.edit_message_text("📝 <b>Send title for next pack:</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "batch_finish":
        d = admin_data.get(ADMIN_ID)
        if d and d.get("course_ids"):
            bid = "b_" + str(uuid.uuid4())[:6]
            batches_col.update_one({"batch_id": bid}, {"$set": {"batch_id": bid, "title": d["title"], "course_ids": d["course_ids"]}}, upsert=True)
            bot.edit_message_text(f"🎉 <b>Batch Created!</b>\n👉 <code>https://t.me/{BOT_USERNAME}?start={bid}</code>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
            del admin_data[ADMIN_ID]
            send_admin_panel(chat_id)
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
        admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["buttons"] = "BC_BUTTONS", []
        bot.send_message(ADMIN_ID, "✅ <b>Media Saved!</b> Add button or Finish.", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data="bc_finish")), parse_mode="HTML")
    elif data == "bc_finish":
        admin_data[ADMIN_ID]["step"] = "BC_TIME"
        bot.send_message(ADMIN_ID, "⏳ <b>How many hours should this broadcast stay? (0 for permanent):</b>", parse_mode="HTML")
    elif data == "ftl_done":
        admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["buttons"] = "FTL_BUTTONS", []
        bot.send_message(ADMIN_ID, "✅ <b>Media Saved!</b> Add button or Finish.", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data="ftl_finish")), parse_mode="HTML")
    elif data == "ftl_finish":
        fid = "f_" + str(uuid.uuid4())[:6]
        file_links_col.update_one({"file_code": fid}, {"$set": {"file_code": fid, "media_data": admin_data[ADMIN_ID].get("media", []), "button_data": admin_data[ADMIN_ID].get("buttons", [])}}, upsert=True)
        bot.send_message(ADMIN_ID, f"🎉 <b>Link Created!</b>\n👉 <code>https://t.me/{BOT_USERNAME}?start={fid}</code>", parse_mode="HTML")
        del admin_data[ADMIN_ID]
        send_admin_panel(ADMIN_ID)

# ==========================================
# FLASK WEB SERVER & API
# ==========================================
app = Flask(__name__)
AMOUNT_RE_DECIMAL = re.compile(r"(?:Rs\.?|₹|INR)\s*([\d,]+\.\d{1,2})", re.IGNORECASE)
AMOUNT_RE_INT = re.compile(r"(?:Rs\.?|₹|INR)\s*([\d,]+)(?!\.\d)", re.IGNORECASE)
BOT_USERNAME = "your_bot" 

@app.route("/")
def home(): return "Telegram Bot API Running."

@app.route("/sms-webhook/<secret>", methods=["GET", "POST"])
def sms_webhook(secret):
    if secret != SMS_HOOK_SECRET: return "forbidden", 403
    sms_text = (request.get_json(silent=True) or request.form).get("text", "").strip() if request.method == "POST" else request.args.get("text", "").strip()
    if not sms_text: return "no 'text' param", 400
    
    m = AMOUNT_RE_DECIMAL.search(sms_text)
    has_dec = bool(m)
    if not m: m = AMOUNT_RE_INT.search(sms_text)
    if not m: return "no amount", 200

    amt_str = m.group(1).replace(",", "")
    f_round = f"{float(amt_str):.2f}" if not has_dec else amt_str
    sms_pool_col.insert_one({"amount": f_round, "raw_text": sms_text, "status": "UNUSED", "created_at_dt": datetime.now(timezone.utc), "created_at_str": get_ist_time()})

    order, amb = None, False
    with pending_lock:
        cands = [amt_str] if has_dec and amt_str in pending_orders else [f_round] if not has_dec and f_round in pending_orders else [k for k in pending_orders if not has_dec and k.startswith(amt_str + ".")]
        if len(cands) == 1: order = pending_orders.pop(cands[0])
        else: amb = len(cands) > 1

    if not order:
        stale_cutoff = time.time() - STALE_ORDER_SECONDS
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

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USERNAME or auth.password != DASHBOARD_PASSWORD:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Dashboard"'})
        return f(*args, **kwargs)
    return wrapper

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
    today_amt = sum(float(o.get("amount", 0) or 0) for o in orders_col.find(completed_q) if _order_ts(o) >= today_start)
    
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
    sms_text = f"You've received Rs.{amount} from Dashboard Test via PhonePe for txn T{int(time.time()*1000)}"
    
    with app.test_client() as client:
        resp = client.get(f"/sms-webhook/{SMS_HOOK_SECRET}?text={sms_text}")
        return jsonify({"status": "success", "result": resp.get_data(as_text=True)})

@app.route("/dashboard/api/courses", methods=["GET"])
@require_auth
def api_courses():
    out = []
    for c in courses_col.find().sort("_id", -1):
        out.append({"course_id": c.get("course_id"), "title": c.get("title", ""), "amount": c.get("amount"), "caption": c.get("custom_caption", "")[:40], "is_channel": bool(c.get("channel_id"))})
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
    is_channel = bool(course and course.get("channel_id"))
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
            "updated_at": get_ist_time()
        }
        settings_col.update_one({"_id": "system_settings"}, {"$set": doc}, upsert=True)
        invalidate_cache("system_settings")

        if was_maintenance and not is_maintenance_now:
            def broadcast_back_online():
                msg = "✅ <b>Bot is Back Online! / बोट चालू हो गया है!</b>\n\nMaintenance is complete. You can now continue using the bot smoothly."
                for u in users_col.find():
                    uid = u.get("user_id") or u.get("chat_id")
                    if uid and int(uid) != ADMIN_ID:
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
        "auto_del_promos": cfg.get("auto_del_promos", True),
        "auto_del_broadcasts": cfg.get("auto_del_broadcasts", True),
        "auto_del_purchases": cfg.get("auto_del_purchases", False)
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

@app.route("/dashboard/api/broadcast", methods=["POST"])
@require_auth
def api_broadcast():
    data = request.json or {}
    msg = data.get("message")
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
    
    def run_bc():
        sent_count = 0
        for u in users_col.find():
            raw_uid = u.get("user_id") or u.get("chat_id") or u.get("_id")
            if not raw_uid: continue
            try:
                bot.send_message(int(raw_uid), msg, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
                sent_count += 1
                time.sleep(0.04)
            except Exception: pass
            
    threading.Thread(target=run_bc, daemon=True).start()
    return jsonify({"status": "success"})

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

@app.route("/dashboard/api/users")
@require_auth
def api_users():
    search_q = request.args.get("search", "").strip()
    query = {}
    if search_q:
        if search_q.isdigit(): query = {"user_id": int(search_q)}
        else: query = {"username": {"$regex": search_q, "$options": "i"}}
    
    out = []
    for u in users_col.find(query).sort("updated_at", -1).limit(200):
        uid = u.get("user_id")
        user_purchases = list(purchases_col.find({"user_id": uid}))
        out.append({
            "user_id": uid, 
            "username": u.get("username", "None"), 
            "updated_at": u.get("updated_at"),
            "purchases": [{"title": p.get("title", "Course"), "link": p.get("link", "#")} for p in user_purchases]
        })
    return jsonify(out)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Store Dashboard</title>
<style>
  :root{--bg:#0F1512; --surface:#141C18; --line:#26332C; --text:#EDF2EF; --muted:#8FA398; --ok:#3ED9A0; --pending:#E8A94A; --danger:#E8695F;}
  *{box-sizing:border-box;} body{margin:0; background:var(--bg); color:var(--text); font-family:sans-serif; font-size:14px;}
  .mono{font-family:monospace;}
  header{padding:15px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between;}
  .ledger{display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:10px; padding:15px;}
  .ledger .row{background:var(--surface); padding:15px; border-radius:6px; border:1px solid var(--line);}
  .tabs{display:flex; gap:15px; padding:10px 15px; border-bottom:1px solid var(--line); overflow-x:auto;}
  .tab{color:var(--muted); cursor:pointer; padding-bottom:5px; border-bottom:2px solid transparent; white-space:nowrap;}
  .tab.active{color:var(--text); border-bottom-color:var(--ok);}
  .subtabs{display:flex; gap:10px; padding:10px 15px;}
  .subtab{color:var(--muted); padding:4px 10px; border:1px solid var(--line); border-radius:15px; cursor:pointer;}
  .subtab.active{background:var(--ok); color:var(--bg); border-color:var(--ok);}
  .list{padding:15px; padding-bottom:50px;}
  .item{display:flex; justify-content:space-between; align-items:center; background:var(--surface); padding:10px; margin-bottom:8px; border-left:3px solid var(--line); border-radius:4px;}
  .item.ok{border-color:var(--ok);} .item.pending{border-color:var(--pending);} .item.expired{border-color:var(--danger);}
  .item .main{flex:1;} .item .name{font-weight:bold;} .item .sub{color:var(--muted); font-size:12px; margin-top:3px;}
  .item .amt{font-size:15px; font-weight:bold;} 
  .action-btn{background:var(--surface); color:var(--text); border:1px solid var(--line); padding:6px 10px; border-radius:4px; cursor:pointer; font-size:12px; margin-top:6px; display:inline-block; margin-left:4px;}
  .ok-btn{border-color:var(--ok); color:var(--ok);} .danger-btn{border-color:var(--danger); color:var(--danger);}
  .form-box{background:var(--surface); padding:15px; border-radius:6px; border:1px solid var(--line); margin-bottom:15px;}
  .form-box input, .form-box select, .form-box textarea{width:100%; padding:8px; margin:5px 0 10px; background:var(--bg); border:1px solid var(--line); color:var(--text); border-radius:4px;}
  .form-box button{background:var(--ok); color:var(--bg); border:none; padding:10px 15px; border-radius:4px; font-weight:bold; cursor:pointer;}
  .switch { position: relative; display: inline-block; width: 50px; height: 24px; vertical-align: middle; margin-left:10px;}
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: var(--line); transition: .4s; border-radius: 24px; }
  .slider:before { position: absolute; content: ""; height: 16px; width: 16px; left: 4px; bottom: 4px; background-color: white; transition: .4s; border-radius: 50%; }
  input:checked + .slider { background-color: var(--danger); }
  input:checked + .slider:before { transform: translateX(26px); }
  .checkbox-lbl { display: block; margin-top: 8px; color: var(--text); cursor: pointer;}
</style></head><body>
<header><h2>Store Dashboard</h2><div id="clock" class="mono"></div></header>
<div class="ledger" id="overview"></div>
<div class="tabs">
  <div class="tab active" data-tab="orders">Orders</div>
  <div class="tab" data-tab="courses">Courses</div>
  <div class="tab" data-tab="offers">Offers</div>
  <div class="tab" data-tab="logs">Channel Logs</div>
  <div class="tab" data-tab="sms">SMS Pool & Trigger</div>
  <div class="tab" data-tab="broadcast">📢 Broadcast</div>
  <div class="tab" data-tab="users">Users</div>
  <div class="tab" data-tab="settings">⚙️ Settings</div>
</div>
<div class="subtabs" id="subtabs">
  <div class="subtab active" data-status="all">All</div>
  <div class="subtab" data-status="pending">Pending</div>
  <div class="subtab" data-status="completed">Completed</div>
  <div class="subtab" data-status="expired">Expired</div>
</div>
<div class="list" id="list">Loading...</div>
<script>
let curTab="orders", curSt="all", searchUserQ="";
function fmtSecs(s){ if(s<=0)return "0s"; let m=Math.floor(s/60), sec=s%60; return m+"m "+sec+"s"; }
async function load(){
  let r=await fetch("/dashboard/api/overview"), d=await r.json();
  document.getElementById("overview").innerHTML=`
    <div class="row">Today: <br><b class="mono" style="font-size:18px">₹${d.today_amount}</b></div>
    <div class="row">Active QR: <b>${d.active_pending}</b><br>Pending SMS: <b>${d.pending_sms}</b></div>
    <div class="row">Total Sales: <b>${d.completed_total}</b><br>Total Users: <b>${d.total_users}</b></div>
  `;
  if(curTab==="orders"){
    r=await fetch("/dashboard/api/orders?status="+curSt); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>{
      let c=x.status==="PENDING"?"pending":x.status==="EXPIRED"?"expired":"ok";
      let rgt = x.status==="PENDING"?`<div class="timer mono" data-remain="${x.remaining_seconds}">${fmtSecs(x.remaining_seconds)} bacha</div>`:`<div class="sub">${x.method||x.status}</div>`;
      let actions = "";
      if(x.status==="PENDING" || x.status==="EXPIRED"){
        actions = `<br><button class="action-btn ok-btn" onclick="appr('${x.order_id}')">✅ Force Approve</button><button class="action-btn danger-btn" onclick="canO('${x.order_id}')">❌ Cancel</button>`;
      }
      return `<div class="item ${c}"><div class="main"><div class="name">${x.user} · <span class="mono">${x.course_id}</span></div><div class="sub">${x.order_id} · ${x.created_at}</div></div><div style="text-align:right"><div class="amt mono">₹${x.amount}</div>${rgt}${actions}</div></div>`;
    }).join("")||"No orders.";
  } else if(curTab==="courses"){
    r=await fetch("/dashboard/api/courses"); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>`<div class="item ok" style="cursor:pointer" onclick="viewBuyers('${x.course_id}')"><div class="main"><div class="name"><span class="mono">${x.course_id}</span> - ${x.title||'No Title'} ${x.is_channel?'📢 Channel':'📝 Text'}</div><div class="sub">₹${x.amount} · ${x.caption}</div></div><div><button class="action-btn danger-btn" onclick="event.stopPropagation(); delC('${x.course_id}')">🗑 Delete</button></div></div>`).join("")||"No courses.";
  } else if(curTab==="offers"){
    r=await fetch("/dashboard/api/offers"); let o=await r.json();
    let formHTML = `<div class="form-box"><h3>Create New Offer</h3>
      <label>Discount %:</label><input type="number" id="off_disc" value="50">
      <label>Target:</label><select id="off_tgt" onchange="document.getElementById('off_cid').style.display=this.value=='single'?'block':'none'"><option value="all">All Courses</option><option value="single">Single Course</option></select>
      <input type="text" id="off_cid" placeholder="Course ID (e.g. c_12345)" style="display:none">
      <label>Max Users (0 for unlimited):</label><input type="number" id="off_max" value="0">
      <label>Per User Limit (1 = one-time only per user, 0 = unlimited):</label><input type="number" id="off_peruser" value="1">
      <label>Active Hours:</label><input type="number" id="off_hrs" value="24">
      <button onclick="createOffer()">Create Offer</button></div>`;
    let listHTML = o.map(x=>`<div class="item ${x.status==='Active'?'ok':'expired'}"><div class="main"><div class="name">${x.discount}% OFF - <span class="mono">${x.offer_code}</span></div><div class="sub">🔗 Link: <a href="${x.link}" target="_blank" style="color:#3ED9A0">${x.link}</a></div><div class="sub">Target: ${x.target} ${x.course?'('+x.course+')':''} | Used: ${x.used}/${x.max==-1?'∞':x.max} | Per User: ${x.per_user==-1?'∞':x.per_user+'x'} | Exp: ${x.expires}</div></div><div><button class="action-btn danger-btn" onclick="delOffer('${x.offer_code}')">🗑</button></div></div>`).join("");
    document.getElementById("list").innerHTML = formHTML + (listHTML||"No offers.");
  } else if(curTab==="logs"){
    r=await fetch("/dashboard/api/channel-logs"); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>`<div class="item ${x.status==='APPROVED'?'ok':'expired'}"><div class="main"><div class="name">${x.first_name} (@${x.username}) - <span class="mono">${x.user_id}</span></div><div class="sub">📺 Channel: <b style="color:var(--text)">${x.channel_name}</b></div><div class="sub">Pack: ${x.course} · ${x.date}</div></div><div style="font-weight:bold; color:var(--${x.status==='APPROVED'?'ok':'danger'})">${x.status}</div></div>`).join("")||"No logs yet.";
  } else if(curTab==="sms"){
    r=await fetch("/dashboard/api/sms-pool"); let o=await r.json();
    let injectHTML = `
      <div class="form-box">
        <h3>🚀 Manual SMS / Payment Injector</h3>
        <p style="font-size:12px; color:var(--muted)">अगर बैंक का SMS नहीं आया, तो यहाँ बस अमाउंट डालें (जैसे <code>50.01</code>) और ट्रिगर दबाएं। बोट खुद पेमेंट को मैच करके डिलीवरी कर देगा।</p>
        <div style="display:flex; gap:10px;">
          <input type="text" id="inject_amt" placeholder="Amount (e.g. 50.01)" style="flex:1;">
          <button onclick="injectSMS()" style="padding:0 20px;">🚀 Trigger Webhook</button>
        </div>
      </div>
    `;
    let listHTML = o.map(x=>`<div class="item pending"><div class="main"><div class="name">₹${x.amount}</div><div class="sub mono">${x.preview}</div></div><div><div class="sub">${x.created_at}</div></div></div>`).join("")||"No SMS in pool.";
    document.getElementById("list").innerHTML = injectHTML + listHTML;
  } else if(curTab==="broadcast") {
    document.getElementById("list").innerHTML = `
      <div class="form-box">
        <h3>📢 Send Global Broadcast</h3>
        <p style="font-size:12px; color:var(--muted)">यहाँ से सभी बोट यूज़र्स को सीधा मैसेज भेजें।</p>
        <textarea id="bc_msg" rows="5" placeholder="HTML मैसेज टाइप करें (जैसे <b>Hello Users</b>)..."></textarea>
        <button onclick="sendDashboardBroadcast()">🚀 Send Broadcast to All</button>
      </div>
    `;
  } else if(curTab==="settings") {
    r=await fetch("/dashboard/api/system-settings"); let cfg=await r.json();
    document.getElementById("list").innerHTML = `
      <div class="form-box">
        <h3>🛑 Maintenance Mode</h3>
        <div style="margin-bottom: 20px;">
          <span style="font-weight:bold;">Maintenance Mode:</span>
          <label class="switch">
            <input type="checkbox" id="maint_toggle" ${cfg.maintenance ? 'checked' : ''} onchange="saveSettings()">
            <span class="slider"></span>
          </label>
        </div>
        <hr style="border-color:var(--line); margin: 20px 0;">
        <h3>🧹 Auto Chat-Delete Settings</h3>
        <label>Auto-Clear Timer:</label>
        <select id="cleanup_time" onchange="saveSettings()">
            <option value="0" ${cfg.cleanup_seconds==0?'selected':''}>Disabled (Off)</option>
            <option value="3600" ${cfg.cleanup_seconds==3600?'selected':''}>1 Hour</option>
            <option value="43200" ${cfg.cleanup_seconds==43200?'selected':''}>12 Hours</option>
            <option value="86400" ${cfg.cleanup_seconds==86400?'selected':''}>24 Hours</option>
            <option value="172800" ${cfg.cleanup_seconds==172800?'selected':''}>48 Hours</option>
        </select>
        <label class="checkbox-lbl"><input type="checkbox" id="auto_del_promos" ${cfg.auto_del_promos?'checked':''} onchange="saveSettings()"> 🗑️ Auto-Delete Promos, Menus & Courses</label>
        <label class="checkbox-lbl"><input type="checkbox" id="auto_del_broadcasts" ${cfg.auto_del_broadcasts?'checked':''} onchange="saveSettings()"> 🗑️ Auto-Delete Broadcast Messages</label>
        <label class="checkbox-lbl"><input type="checkbox" id="auto_del_purchases" ${cfg.auto_del_purchases?'checked':''} onchange="saveSettings()"> <span style="color:var(--danger)">⚠️ Auto-Delete Purchased Links</span></label>
        <hr style="border-color:var(--line); margin: 20px 0;">
        <button style="background-color:var(--danger);" onclick="forceClearChats()">🧹 Clear Selected Chats Now</button>
      </div>
    `;
  } else {
    r=await fetch("/dashboard/api/users?search="+encodeURIComponent(searchUserQ)); let o=await r.json();
    let searchBox = `<div class="form-box"><h3>🔍 Search User</h3><div style="display:flex; gap:10px;"><input type="text" id="user_search_input" placeholder="Search by Username or User ID..." value="${searchUserQ}"><button onclick="searchUserQ=document.getElementById('user_search_input').value; load();" style="padding:0 20px;">Search</button><button onclick="searchUserQ=''; load();" style="background:var(--danger); padding:0 15px;">Reset</button></div></div>`;
    let usersList = o.map(x=>{
      let purchasesHTML = x.purchases.length > 0 ? x.purchases.map(p=>`<li><b>${p.title}</b> - <a href="${p.link}" target="_blank" style="color:var(--ok)">Link</a></li>`).join("") : "<span style='color:var(--muted)'>No purchases</span>";
      return `<div class="item ok" style="flex-direction:column; align-items:flex-start;"><div style="width:100%; display:flex; justify-content:space-between;"><div><b>User ID:</b> <span class="mono">${x.user_id}</span> | <b>Username:</b> @${x.username}</div><div class="sub">Active: ${x.updated_at}</div></div><div style="margin-top:8px; width:100%; background:var(--bg); padding:8px; border-radius:4px;"><ul style="margin:0; padding-left:15px; font-size:12px;">${purchasesHTML}</ul></div></div>`;
    }).join("")||"No users found.";
    document.getElementById("list").innerHTML = searchBox + usersList;
  }
}

async function injectSMS(){
  let amt = document.getElementById("inject_amt").value.trim();
  if(!amt) return alert("Amount enter karein!");
  let res = await fetch("/dashboard/api/inject-sms", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({amount: amt})
  });
  let d = await res.json();
  alert("Result: " + d.result);
  load();
}

async function sendDashboardBroadcast(){
  let msg = document.getElementById("bc_msg").value.trim();
  if(!msg) return alert("Message cannot be empty!");
  if(confirm("Send broadcast to ALL users?")){
    await fetch("/dashboard/api/broadcast", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({message: msg})
    });
    alert("Broadcast started in background!");
    document.getElementById("bc_msg").value = "";
  }
}

async function canO(id){ if(confirm("Cancel this order?")){ await fetch("/dashboard/api/orders/"+id+"/cancel",{method:"POST"}); load(); } }
async function appr(id){ if(confirm("Approve order manually?")){ await fetch("/dashboard/api/orders/"+id+"/approve",{method:"POST"}); load(); } }
async function delC(id){ if(confirm("Delete course?")){ await fetch("/dashboard/api/courses/"+id,{method:"DELETE"}); load(); } }
async function delOffer(id){ if(confirm("Delete this offer?")){ await fetch("/dashboard/api/offers/"+id,{method:"DELETE"}); load(); } }
async function createOffer(){
  let d = { discount: document.getElementById('off_disc').value, target_type: document.getElementById('off_tgt').value, course_id: document.getElementById('off_cid').value, max_users: document.getElementById('off_max').value==-1?-1:(document.getElementById('off_max').value==0?-1:document.getElementById('off_max').value), per_user_limit: document.getElementById('off_peruser').value==0?-1:document.getElementById('off_peruser').value, hours: document.getElementById('off_hrs').value };
  await fetch("/dashboard/api/offers", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(d)}); load();
}
async function saveSettings(){
  await fetch("/dashboard/api/system-settings", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      maintenance: document.getElementById("maint_toggle").checked,
      cleanup_seconds: document.getElementById("cleanup_time").value,
      auto_del_promos: document.getElementById("auto_del_promos").checked,
      auto_del_broadcasts: document.getElementById("auto_del_broadcasts").checked,
      auto_del_purchases: document.getElementById("auto_del_purchases").checked
    })
  });
}

document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>{ document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active")); t.classList.add("active"); curTab=t.dataset.tab; document.getElementById("subtabs").style.display=curTab==="orders"?"flex":"none"; load(); }));
document.querySelectorAll(".subtab").forEach(t=>t.addEventListener("click",()=>{ document.querySelectorAll(".subtab").forEach(x=>x.classList.remove("active")); t.classList.add("active"); curSt=t.dataset.status; load(); }));
setInterval(()=>{ document.querySelectorAll("[data-remain]").forEach(el=>{ let s=Math.max(0,el.dataset.remain-1); el.dataset.remain=s; el.textContent=fmtSecs(s)+" bacha"; }); document.getElementById("clock").textContent=new Date().toLocaleTimeString("en-IN"); }, 1000);
load(); setInterval(load, 15000);
</script></body></html>"""

@app.route("/dashboard")
@require_auth
def dashboard_page(): return DASHBOARD_HTML

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
            bot.infinity_polling(timeout=20, long_polling_timeout=15, skip_pending=True, logger_level=None)
        except Exception:
            time.sleep(5)

if __name__ == "__main__":
    try: BOT_USERNAME = bot.get_me().username
    except Exception: pass
    
    restore_pending_orders()
    threading.Thread(target=global_sms_checker, daemon=True).start()
    threading.Thread(target=global_memory_cleanup, daemon=True).start()
    threading.Thread(target=start_polling, daemon=True).start()
    
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
