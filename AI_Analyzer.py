import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime
from io import BytesIO

import requests
from FunPayAPI.common.enums import OrderStatuses
from telebot.types import InlineKeyboardButton as B
from telebot.types import InlineKeyboardMarkup as K

logger = logging.getLogger("FPC.daily_report")

NAME = "DailyReport (AI Analyzer)"
VERSION = "2.0.0"
DESCRIPTION = "Автоматический сбор продаж, анализ переписки через GPT и вечерний отчёт в Telegram"
CREDITS = "Konfeta777"
SETTINGS_PAGE = False
BIND_TO_DELETE = False
UUID = "01586778-0809-4609-8bb4-462a8e314356"

CFG_DIR = "plugins/config/ai_order_analyzer"
CFG_PATH = os.path.join(CFG_DIR, "config.json")
LOG_PATH = os.path.join(CFG_DIR, "report_log.json")
YESTERDAY_PATH = os.path.join(CFG_DIR, "yesterday.json")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_API_KEY = "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"
OPENROUTER_MODEL = "arcee-ai/trinity-large-preview:free"

DEFAULT_PROMPT = """Ты анализируешь переписку продавца и покупателя на маркетплейсе.
Оцени общее настроение покупателя: позитивное / нейтральное / негативное.
Если есть признаки конфликта, скама или недовольства — отметь отдельно.
Отвечай только в формате JSON:
{"mood": "позитивное", "flag": false, "comment": "..."}"""

DEFAULT_CFG = {
    "enabled": False,
    "telegram_chat_id": 0,
    "report_time": "21:00",
    "api_key": DEFAULT_API_KEY,
    "currency": "RUB",
    "prompt": DEFAULT_PROMPT
}

# --- Telegram UI constants ---
PFX = "dr_"
CB_MAIN = f"{PFX}main"
CB_TOGGLE = f"{PFX}toggle"
CB_SET_TIME = f"{PFX}time"
CB_SET_CHAT = f"{PFX}chat"
CB_SET_API = f"{PFX}api"
CB_TEST_REPORT = f"{PFX}test"
CB_CANCEL = f"{PFX}cancel"

_cardinal_ref = None
_cfg_lock = threading.RLock()
_cfg_cache = None
_input_states = {}

os.makedirs(CFG_DIR, exist_ok=True)


def _load_config() -> dict:
    global _cfg_cache
    with _cfg_lock:
        if _cfg_cache is not None:
            return _cfg_cache

        if not os.path.exists(CFG_PATH):
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CFG, f, indent=4, ensure_ascii=False)
            _cfg_cache = DEFAULT_CFG.copy()
            return _cfg_cache

        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in DEFAULT_CFG.items():
                    if k not in data:
                        data[k] = v
                _cfg_cache = data
                return data
        except Exception:
            _cfg_cache = DEFAULT_CFG.copy()
            return _cfg_cache

def _save_config(cfg: dict) -> None:
    global _cfg_cache
    with _cfg_lock:
        _cfg_cache = cfg.copy()
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[DailyReport] Error saving config: {e}")

def _load_json(path, default=None):
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[DailyReport] Error saving {path}: {e}")

# --- Logic Functions ---

def log_order(order):
    if hasattr(order, "status") and order.status != OrderStatuses.PAID:
        return
        
    cfg = _load_config()
    if not cfg.get("enabled"):
        return

    order_id = getattr(order, 'id', str(order))
    chat_id = getattr(order, 'chat_id', None)
    
    logs = _load_json(LOG_PATH, {})
    
    if str(order_id) not in logs:
        logs[str(order_id)] = {
            "id": str(order_id),
            "chat_id": chat_id,
            "time": datetime.now().strftime("%H:%M"),
            "lot_name": getattr(order, "description", getattr(order, "title", "Unknown")),
            "amount_rub": getattr(order, "price", getattr(order, "sum", 0)),
            "buyer_id": getattr(order, "buyer_username", "Unknown"),
            "messages": []
        }
        _save_json(LOG_PATH, logs)
        logger.info(f"[DailyReport] Logged new order {order_id}")

def scan_today_orders(cardinal):
    cfg = _load_config()
    if not cfg.get("enabled"):
        return
        
    try:
        _, sales, _, _ = cardinal.account.get_sales(include_paid=True)
        logs = _load_json(LOG_PATH, {})
        start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        added = 0
        for order in sales:
            # Считаем и ОПЛАЧЕННЫЕ, и ЗАКРЫТЫЕ (полученные средства) сегодня
            if order.status not in (OrderStatuses.PAID, OrderStatuses.CLOSED):
                continue
            if order.date < start_of_day:
                continue
                
            if str(order.id) not in logs:
                messages_data = []
                try:
                    chat = cardinal.account.get_chat(order.chat_id)
                    if chat and chat.messages:
                        # Берем последние 50 сообщений
                        for m in chat.messages[-50:]:
                            text = getattr(m, "text", "") or "[Изображение]"
                            author_id = getattr(m, "author_id", None)
                            card_id = getattr(cardinal.account, "id", None)
                            from_who = "buyer" if author_id != card_id else "seller"
                            messages_data.append({"from": from_who, "text": text})
                except Exception as e:
                    logger.debug(f"[DailyReport] Fail to fetch history for {order.id}: {e}")

                logs[str(order.id)] = {
                    "id": str(order.id),
                    "chat_id": order.chat_id,
                    "time": order.date.strftime("%H:%M"),
                    "lot_name": order.description or order.title or "Unknown",
                    "amount_rub": order.price,
                    "buyer_id": order.buyer_username,
                    "messages": messages_data
                }
                added += 1
        
        if added > 0:
            _save_json(LOG_PATH, logs)
            logger.info(f"[DailyReport] Backfilled {added} orders from today's history")
    except Exception as e:
        logger.error(f"[DailyReport] Historical scan failed: {e}")

def log_message(message):
    cfg = _load_config()
    if not cfg.get("enabled"):
        return
        
    chat_id = getattr(message, "chat_id", None)
    if not chat_id:
        return
        
    logs = _load_json(LOG_PATH, {})
    found = False
    for order_id, data in logs.items():
        if str(data.get("chat_id")) == str(chat_id):
            text = getattr(message, "text", "")
            if not text:
                text = "[Изображение/Системное сообщение]"
            
            from_who = "seller"
            try:
                cardinal_account_id = getattr(_cardinal_ref.account, "id", None)
                if getattr(message, "author_id", None) != cardinal_account_id:
                    from_who = "buyer"
            except:
                from_who = getattr(message, "author", "unknown")
            
            data["messages"].append({
                "from": from_who,
                "text": text
            })
            found = True
            break
            
    if found:
        _save_json(LOG_PATH, logs)

def _analyze_mood_sync(api_key, prompt, messages):
    if not messages:
        return {"mood": "нейтральное", "flag": False, "comment": "Нет диалога"}
        
    dialog_text = "\n".join([f"{msg['from']}: {msg['text']}" for msg in messages])
    
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/FunPayDestroy",
        "X-Title": "FunPayDailyReport"
    }
    
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Диалог:\n\n{dialog_text}"}
        ],
        "temperature": 0.3,
        "max_tokens": 150
    }
    
    try:
        response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "{}")
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.replace("```", "").strip()
            return json.loads(content)
        else:
            logger.error(f"[DailyReport] API Error {response.status_code}")
    except Exception as e:
        logger.error(f"[DailyReport] Request Error: {e}")
        
    return {"mood": "ошибка", "flag": False, "comment": ""}

def _generate_and_send_report(bot, chat_id, is_test=False):
    if _cardinal_ref:
        scan_today_orders(_cardinal_ref)
    cfg = _load_config()
    logs = _load_json(LOG_PATH, {})
    yesterday = _load_json(YESTERDAY_PATH, {"revenue": 0, "count": 0})
    
    total_revenue = 0
    orders_count = len(logs)
    lot_counts = {}
    
    mood_counts = {"позитивное": 0, "нейтральное": 0, "негативное": 0}
    attention_required = []
    
    for order_id, data in logs.items():
        try:
            amount = float(data.get("amount_rub", 0))
        except:
            amount = 0
        total_revenue += amount
        
        lot = data.get("lot_name", "Unknown")
        lot_counts[lot] = lot_counts.get(lot, 0) + 1
        
        messages = data.get("messages", [])
        if messages:
            try:
                mood_res = _analyze_mood_sync(cfg.get("api_key"), cfg.get("prompt"), messages)
                if mood_res:
                    m = str(mood_res.get("mood", "нейтральное")).lower()
                    if "позитив" in m: mood_counts["позитивное"] += 1
                    elif "негатив" in m: mood_counts["негативное"] += 1
                    else: mood_counts["нейтральное"] += 1
                    
                    if mood_res.get("flag", False):
                        attention_required.append(order_id)
            except Exception as e:
                logger.error(f"[DailyReport] Mood analysis failed for {order_id}: {e}")
    
    avg_check = total_revenue / orders_count if orders_count > 0 else 0
    y_rev = float(yesterday.get("revenue", 0))
    d_rev = 0
    if y_rev > 0:
        d_rev = ((total_revenue - y_rev) / y_rev) * 100
    else:
        d_rev = 100 if total_revenue > 0 else 0
        
    top_lot = max(lot_counts.items(), key=lambda x: x[1]) if lot_counts else ("-", 0)
    
    if mood_counts["позитивное"] > 0 and mood_counts["позитивное"] >= mood_counts["нейтральное"] and mood_counts["позитивное"] >= mood_counts["негативное"]:
        main_mood = "позитивное"
        count = mood_counts["позитивное"]
    elif mood_counts["негативное"] > 0 and mood_counts["негативное"] >= mood_counts["нейтральное"]:
        main_mood = "негативное"
        count = mood_counts["негативное"]
    else:
        main_mood = "нейтральное"
        count = mood_counts["нейтральное"]
        
    total_analyzed = sum(mood_counts.values())
    if total_analyzed == 0:
        main_mood_str = "нет диалогов"
    else:
        main_mood_str = f"{main_mood} ({count} из {total_analyzed})"
        
    months = {
        "January": "января", "February": "февраля", "March": "марта",
        "April": "апреля", "May": "мая", "June": "июня",
        "July": "июля", "August": "августа", "September": "сентября",
        "October": "октября", "November": "ноября", "December": "декабря"
    }
    date_now = datetime.now()
    month_ru = months.get(date_now.strftime("%B"), date_now.strftime("%B"))
    date_str = f"{date_now.day} {month_ru}"
    
    sign = "+" if d_rev >= 0 else ""
    def fmt(val):
        return f"{int(val):,}".replace(",", " ")
    
    report = (
        f"📊 Отчёт {'(Тест) ' if is_test else ''}за {date_str}\n\n"
        f"💰 Выручка: {fmt(total_revenue)} ₽ ({sign}{int(d_rev)}% к вчера)\n"
        f"📦 Сделок: {orders_count}\n"
        f"🏆 Топ лот: {top_lot[0]} — {top_lot[1]} продажи\n"
        f"🧾 Средний чек: {fmt(avg_check)} ₽\n"
        f"😊 Настроение: {main_mood_str}\n"
    )
    
    if attention_required:
        report += f"\n⚠️ {len(attention_required)} диалог(ов) требует внимания → " + ", ".join(attention_required)
        
    try:
        bot.send_message(chat_id, report)
    except Exception as e:
        logger.error(f"[DailyReport] Failed to send report: {e}")
        
    if not is_test:
        _save_json(YESTERDAY_PATH, {"revenue": total_revenue, "count": orders_count})
        _save_json(LOG_PATH, {})

def _cron_loop():
    logger.info("[DailyReport] CRON loop started")
    while True:
        try:
            cfg = _load_config()
            enabled = cfg.get("enabled", False)
            rep_time = cfg.get("report_time", "21:00")
            t_chat_id = cfg.get("telegram_chat_id", 0)
            
            if enabled and t_chat_id:
                now = datetime.now().strftime("%H:%M")
                if now == rep_time:
                    logger.info(f"[DailyReport] Triggering report at {now}")
                    if _cardinal_ref and getattr(_cardinal_ref, "telegram", None):
                        bot = _cardinal_ref.telegram.bot
                        _generate_and_send_report(bot, t_chat_id, is_test=False)
                    time.sleep(65)
        except Exception as e:
            logger.error(f"[DailyReport] CRON error: {e}")
        
        time.sleep(30)

# --- Telegram UI ---

def _menu_text(cfg: dict) -> str:
    status = "🟢 Включен" if cfg.get("enabled") else "🔴 Выключен"
    rep_time = cfg.get("report_time", "21:00")
    t_chat = cfg.get("telegram_chat_id", 0)
    
    return (
        f"📊 <b>DailyReport (AI Analyzer)</b>\n\n"
        f"Статус: {status}\n"
        f"Время отчета: {rep_time}\n"
        f"Чат для отчета: {t_chat if t_chat else 'Не установлен'}\n\n"
        f"Команды:\n"
        f"/daily_report - Настройки плагина"
    )

def _menu_kb(cfg: dict) -> K:
    kb = K()
    kb.row(B("🔴 Выкл" if cfg.get("enabled") else "🟢 Вкл", callback_data=CB_TOGGLE))
    kb.row(
        B(f"🕒 Время: {cfg.get('report_time', '21:00')}", callback_data=CB_SET_TIME),
        B("💬 Сюда", callback_data=CB_SET_CHAT)
    )
    kb.row(B("🔑 API Ключ", callback_data=CB_SET_API))
    kb.row(B("📨 Тестовый отчет", callback_data=CB_TEST_REPORT))
    kb.row(B("⬅️ Назад", callback_data="plugins_settings"))
    return kb

def handle_cb(call):
    cardinal = _cardinal_ref
    if not cardinal: return
    
    d = call.data
    cid = call.message.chat.id
    mid = call.message.message_id
    bot = cardinal.telegram.bot
    
    if getattr(call.from_user, "id", None) not in getattr(cardinal.telegram, "authorized_users", {}):
        bot.answer_callback_query(call.id, "❌ У вас нет прав.", show_alert=True)
        return
        
    if d == CB_MAIN:
        cfg = _load_config()
        bot.edit_message_text(_menu_text(cfg), cid, mid, parse_mode="HTML", reply_markup=_menu_kb(cfg))
        bot.answer_callback_query(call.id)
        return
        
    if d == CB_TOGGLE:
        cfg = _load_config()
        cfg["enabled"] = not cfg.get("enabled", False)
        _save_config(cfg)
        bot.edit_message_text(_menu_text(cfg), cid, mid, parse_mode="HTML", reply_markup=_menu_kb(cfg))
        bot.answer_callback_query(call.id, "Статус изменен")
        return
        
    if d == CB_SET_CHAT:
        cfg = _load_config()
        cfg["telegram_chat_id"] = cid
        _save_config(cfg)
        bot.edit_message_text(_menu_text(cfg), cid, mid, parse_mode="HTML", reply_markup=_menu_kb(cfg))
        bot.answer_callback_query(call.id, "Чат установлен!")
        return
        
    if d == CB_TEST_REPORT:
        bot.answer_callback_query(call.id, "Собираем тестовый отчет...")
        threading.Thread(target=lambda: _generate_and_send_report(bot, cid, is_test=True), daemon=True).start()
        return

    wait_fields = {
        CB_SET_TIME: ("report_time", "🕒 Отправьте время для ежедневного отчета (формат ЧЧ:ММ, например, 21:00):"),
        CB_SET_API: ("api_key", "🔑 Отправьте новый API ключ (OpenRouter):")
    }
    
    if d in wait_fields:
        field, prompt_txt = wait_fields[d]
        kb_cancel = K().add(B("❌ Отмена", callback_data=CB_CANCEL))
        bot.answer_callback_query(call.id)
        pm = bot.send_message(cid, prompt_txt, reply_markup=kb_cancel)
        _input_states[cid] = {"field": field, "mid": mid, "prompt_mid": pm.message_id}
        return
        
    if d == CB_CANCEL:
        if cid in _input_states:
            state = _input_states.pop(cid)
            pmid = state.get("prompt_mid")
            if pmid:
                try: bot.delete_message(cid, pmid)
                except: pass
            bot.answer_callback_query(call.id, "Отменено")
        return

def input_handler(message):
    cardinal = _cardinal_ref
    if not cardinal: return
    
    cid = message.chat.id
    if cid not in _input_states: return
    
    state = _input_states.pop(cid)
    field = state["field"]
    mid = state["mid"]
    bot = cardinal.telegram.bot
    
    if state.get("prompt_mid"):
        try: bot.delete_message(cid, state["prompt_mid"])
        except: pass
    try: bot.delete_message(cid, message.message_id)
    except: pass
    
    cfg = _load_config()
    val = message.text.strip()
    
    if field == "report_time":
        if len(val) == 5 and ":" in val:
            cfg[field] = val
    else:
        cfg[field] = val
        
    _save_config(cfg)
    bot.edit_message_text(_menu_text(cfg), cid, mid, parse_mode="HTML", reply_markup=_menu_kb(cfg))

def cmd_settings(message):
    cardinal = _cardinal_ref
    if not cardinal: return
    
    if getattr(message.from_user, "id", None) not in getattr(cardinal.telegram, "authorized_users", {}): return
    
    cfg = _load_config()
    cardinal.telegram.bot.send_message(
        message.chat.id, _menu_text(cfg), parse_mode="HTML", reply_markup=_menu_kb(cfg)
    )

def init(cardinal):
    global _cardinal_ref
    _cardinal_ref = cardinal
    _load_config()
    
    if cardinal.telegram:
        cardinal.telegram.msg_handler(cmd_settings, commands=["daily_report", "ai_order_settings"])
        cardinal.telegram.msg_handler(input_handler, func=lambda m: m.chat.id in _input_states)
        cardinal.telegram.cbq_handler(handle_cb, lambda c: c.data.startswith(PFX))
        
        cardinal.add_telegram_commands(UUID, [
            ("daily_report", "Настройки DailyReport", True)
        ])
        
    scan_today_orders(cardinal)
    threading.Thread(target=_cron_loop, daemon=True).start()

def new_order_handler(cardinal, event):
    order = getattr(event, 'order', event)
    log_order(order)

def new_message_handler(cardinal, event):
    message = getattr(event, 'message', event)
    log_message(message)

def order_status_changed_handler(cardinal, event):
    order = getattr(event, 'order', event)
    log_order(order)

BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_ORDER_STATUS_CHANGED = [order_status_changed_handler]
BIND_TO_NEW_MESSAGE = [new_message_handler]
