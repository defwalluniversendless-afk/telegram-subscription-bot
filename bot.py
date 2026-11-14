"""Telegram-бот: платная подписка на закрытый канал с лекциями (ЮKassa).

Воронка: /start или /join -> кнопка -> карточка товара + оферта -> оплата -> доступ в канал
-> напоминания -> (автосписание) -> закрытие доступа. /help — связь с владельцем.
Все тексты бота лежат в блоке TEXTS ниже: правьте формулировки там.

Воронка из Instagram (в этом же файле, блок FUNNEL): ссылка t.me/бот?start=insta -> вопрос об аудитории
-> урок -> видео «Что такое Круг» -> тест из 7 вопросов -> профиль приходит администратору
с кнопкой «Написать ему». Обычный /start работает как раньше.
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import math
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           BotCommandScopeChat, InlineKeyboardMarkup, MenuButtonWebApp, Message,
                           WebAppInfo)
from aiohttp import web
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from yookassa import Configuration, Payment, PaymentMethod

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])        # вида -1001234567890
ADMIN_ID = int(os.environ["ADMIN_ID"])            # ваш Telegram ID
PRICE = float(os.getenv("PRICE", "2500"))         # цена за период, руб
PERIOD_DAYS = float(os.getenv("PERIOD_DAYS", "30"))
REMIND_DAYS = float(os.getenv("REMIND_DAYS", "3"))    # за сколько дней напоминать о продлении
STUCK_HOURS = float(os.getenv("STUCK_HOURS", "24"))   # через сколько часов напомнить «застрявшему» на оплате
OFFER_URL = os.getenv("OFFER_URL") or "https://disk.yandex.ru/d/E2dDsFUW5SX4mQ"  # ссылка на публичную оферту
AUTOCHARGE = os.getenv("AUTOCHARGE", "0") == "1"  # автосписание (нужно включить у ЮKassa)
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"    # 1 = локальная имитация оплаты без ЮKassa
PAYMENT_STORE_MODE = os.getenv("PAYMENT_STORE_MODE", "test").lower()
if PAYMENT_STORE_MODE not in {"test", "live"}:
    raise ValueError("PAYMENT_STORE_MODE должен быть test или live")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "0"))    # 0 = вебхук выключен, только опрос
RETURN_URL = os.getenv("RETURN_URL", "https://t.me/")
# --- CRM-мини-апп (Telegram Web App на этом же сервере) ---
MINIAPP_ENABLED = os.getenv("MINIAPP", "1") == "1"
MINIAPP_URL = (os.getenv("MINIAPP_URL") or "https://bot-1794299880-3352-adminhabirov.bothost.tech").rstrip("/")
# Порт должен совпадать с «Порт веб-приложения» на вкладке «Домен» в Bothost (там 3000).
WEB_PORT = int(os.getenv("PORT") or 3000) if MINIAPP_ENABLED else WEBHOOK_PORT
# --- Автоочистка CRM (дни; можно менять переменными в Bothost) ---
CRM_TRASH_DAYS = int(os.getenv("CRM_TRASH_DAYS", "15"))          # «Не купил / закрыт»: через столько дней данные лида очищаются
CRM_BUYER_SYS_DAYS = int(os.getenv("CRM_BUYER_SYS_DAYS", "15"))  # у покупателей серые события старше N дней удаляются
CRM_SYS_DAYS = int(os.getenv("CRM_SYS_DAYS", "90"))              # у остальных серые события старше N дней (0 = не чистить)
CRM_STALE_DAYS = int(os.getenv("CRM_STALE_DAYS", "0"))           # зашёл, не прошёл тест и молчит N дней: в корзину (0 = выкл.)
# Папка с базой. На Bothost постоянное хранилище лежит в /app/data (тариф Basic и выше).
DATA_DIR = os.getenv("DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot.db")
PAYMENT_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Inter.png")
BACKUP_HOURS = float(os.getenv("BACKUP_HOURS", "24"))  # как часто бот присылает вам копию базы
# --- воронка из Instagram ---
FUNNEL_PREFIX = os.getenv("FUNNEL_PAYLOAD_PREFIX", "insta")        # ссылка вида t.me/бот?start=insta
# Источники лидов: параметр ссылки -> (значок, название). Ссылка t.me/бот?start=yt = лид из YouTube.
# К метке можно добавить уточнение: ?start=yt_shadow, в CRM будет «YouTube (shadow)». Новый источник добавьте строкой.
FUNNEL_SOURCES = {FUNNEL_PREFIX: ("📸", "Instagram"), "yt": ("▶️", "YouTube")}
# ============================== TEXTS ==============================
# Разметка: <b>жирный</b>, <i>курсив</i>. Подстановки в {фигурных скобках} не трогайте.
T_WELCOME = ("Привет, я бот Круга Ильи Хабирова. Я выдаю доступ к закрытому каналу с лекциями.\n"
             "Оплатить можно картой.\n\nЧтобы оплатить, нажмите на кнопку 👇")
T_PRODUCT = ("📚 <b>Ежемесячная подписка на закрытый канал с лекциями</b>\n"
             "Цена: <b>{price} ₽</b> в месяц\n\n"
             "Перед оплатой ознакомьтесь с {offer}.")
T_WHAT_YOU_GET = (
    "Приобретая подписку в «Круге», вы получаете доступ к ежемесячно обновляемому "
    "Telegram-каналу с лекциями и статьями о духовном и личностном развитии. "
    "Примеры статей можно посмотреть в нашем бесплатном канале @habirov_cyrcle.\n\n"
    "В платной подписке, помимо статей, доступны видеолекции — обычно длительностью "
    "час и более — на те же темы, но с более глубоким погружением вместе с автором."
)
T_AUTO_NOTE = "\n\nОплачивая, вы соглашаетесь на ежемесячное автопродление. Отключить: /cancel"
T_OFFER_LINK = '<a href="{url}">публичной офертой</a>'
T_OFFER_SOON = "публичной офертой (ссылка скоро появится)"
T_PAY_LINK = "Ссылка на оплату готова. После оплаты доступ придёт сюда автоматически."
T_NOT_READY = "Приём оплаты ещё настраивается, скоро всё заработает. Если вопрос срочный, нажмите «Связаться»."
T_ACTIVE = ("У вас уже есть активная подписка до {date}.\n"
            "Если у вас есть вопросы, свяжитесь с нами.")
T_STATUS_NO = "Подписки нет. Чтобы получить доступ, нажмите кнопку."
T_PAID_NEW = ("✅ Оплата получена. Подписка до {date}.\n"
              "{autopay_status}\n"
              "Управление подпиской и автосписанием: /status\n\n"
              "Ваша ссылка в канал (одноразовая, действует сутки):\n{link}")
T_PAID_EXT = ("✅ Подписка продлена до {date}.\n"
              "{autopay_status}\n"
              "Управление подпиской и автосписанием: /status")
T_PAID_NOLINK = ("✅ Оплата получена, но ссылку выдать не удалось. Нажмите /help, и мы всё решим.\n"
                 "{autopay_status}\nУправление подпиской: /status")
T_REMIND = "🔔 Подписка заканчивается {end}. Чтобы не потерять доступ, продлите её:"
T_REMIND_AUTO = "🔔 Подписка до {end}. Спишем {price} ₽ автоматически. Отключить автопродление: /cancel"
T_AUTO_FAIL = "Не удалось списать оплату автоматически. Продлите вручную:"
T_MAINTENANCE_GRACE = (
    "⏸️ Пока бот был на технических работах, срок вашей подписки закончился. "
    "У вас есть 3 дня, чтобы продлить её и сохранить доступ к каналу:"
)
T_EXPIRED = (
    "⏰ Срок подписки закончился, доступ к каналу закрыт. Оплатите продление, "
    "и доступ будет восстановлен:"
)
T_STUCK = ("Вижу, вы застопорились на моменте оплаты. Что-то не так?\n"
           "Если не получается оплатить или остались вопросы, нажмите «Связаться», и мы поможем.")
T_HELP_PROMPT = ("Опишите проблему или вопрос одним сообщением (можно приложить скриншот). "
                 "Я передам его Илье, и ответ придёт сюда.")
T_HELP_SENT = "Передал. Ответим вам здесь, как только сможем."
T_UNKNOWN = "Не понял сообщение. Нажмите /start, чтобы начать, или /help, чтобы связаться с нами."
T_CANCEL = "Автопродление отключено. Доступ сохранится до конца оплаченного срока."
T_BLOCKED = "Доступ к этому боту ограничен. Если вы считаете, что это ошибка, свяжитесь с администратором."
# ===================================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
dp = Dispatcher()

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, username TEXT, expires_at TEXT, method_id TEXT,
  autopay INTEGER DEFAULT 1, last_remind TEXT, last_charge TEXT, active INTEGER DEFAULT 0,
  pay_started_at TEXT, stuck_reminded INTEGER DEFAULT 0, joined_at TEXT,
  blocked INTEGER DEFAULT 0, method_mode TEXT, grace_until TEXT);
CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, user_id INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, user_id INTEGER, created TEXT,
  is_auto INTEGER DEFAULT 0, store_mode TEXT);
CREATE TABLE IF NOT EXISTS pending_methods(
  id TEXT PRIMARY KEY, user_id INTEGER, created TEXT, confirmation_url TEXT, store_mode TEXT);
CREATE TABLE IF NOT EXISTS help(msg_id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE IF NOT EXISTS support_links(
  chat_id INTEGER, message_id INTEGER, user_id INTEGER,
  PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS funnel_users(
  user_id INTEGER PRIMARY KEY, source TEXT, answers TEXT DEFAULT '{}', stage TEXT DEFAULT 'group',
  step INTEGER DEFAULT 0, tmp TEXT DEFAULT '[]', done INTEGER DEFAULT 0, created INTEGER);
CREATE TABLE IF NOT EXISTS crm_topics(user_id INTEGER PRIMARY KEY, chat_id INTEGER, thread_id INTEGER);
CREATE TABLE IF NOT EXISTS crm_out(
  chat_id INTEGER, message_id INTEGER, user_id INTEGER, user_message_id INTEGER,
  PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS crm_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, direction TEXT, text TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS crm_notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS crm_leads(user_id INTEGER PRIMARY KEY, stage TEXT DEFAULT 'new', amount REAL, updated INTEGER);
CREATE TABLE IF NOT EXISTS crm_seen(user_id INTEGER PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS crm_profiles(user_id INTEGER PRIMARY KEY, name TEXT);
CREATE INDEX IF NOT EXISTS idx_crm_msg_user ON crm_messages(user_id, id);
CREATE INDEX IF NOT EXISTS idx_crm_notes_user ON crm_notes(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
"""


def migrate():
    """Создаёт таблицы и дополняет старую базу новыми колонками."""
    db.executescript(SCHEMA)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    for name, ddl in (("pay_started_at", "TEXT"), ("stuck_reminded", "INTEGER DEFAULT 0"),
                      ("joined_at", "TEXT"), ("blocked", "INTEGER DEFAULT 0")):
        if name not in cols:
            db.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
    if "method_mode" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN method_mode TEXT")
    if "grace_until" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN grace_until TEXT")
    pending_cols = {r["name"] for r in db.execute("PRAGMA table_info(pending)")}
    if "is_auto" not in pending_cols:
        db.execute("ALTER TABLE pending ADD COLUMN is_auto INTEGER DEFAULT 0")
    if "store_mode" not in pending_cols:
        db.execute("ALTER TABLE pending ADD COLUMN store_mode TEXT")
    pending_method_cols = {r["name"] for r in db.execute("PRAGMA table_info(pending_methods)")}
    if "store_mode" not in pending_method_cols:
        db.execute("ALTER TABLE pending_methods ADD COLUMN store_mode TEXT")
    funnel_cols = {r["name"] for r in db.execute("PRAGMA table_info(funnel_users)")}
    if "stage" not in funnel_cols:
        db.execute("ALTER TABLE funnel_users ADD COLUMN stage TEXT DEFAULT 'group'")
    if "tmp" not in funnel_cols:
        db.execute("ALTER TABLE funnel_users ADD COLUMN tmp TEXT DEFAULT '[]'")
    if "done_at" not in funnel_cols:  # когда закончил тест (0 = старые лиды: серия им не шлётся)
        db.execute("ALTER TABLE funnel_users ADD COLUMN done_at INTEGER DEFAULT 0")
    if "series_idx" not in funnel_cols:  # сколько писем серии уже отправлено
        db.execute("ALTER TABLE funnel_users ADD COLUMN series_idx INTEGER DEFAULT 0")
    if "next_at" not in funnel_cols:  # когда сработает таймер следующего шага воронки
        db.execute("ALTER TABLE funnel_users ADD COLUMN next_at INTEGER DEFAULT 0")
    cm_cols = {r[1] for r in db.execute("PRAGMA table_info(crm_messages)")}
    if "tg_id" not in cm_cols:  # номер сообщения в Telegram: нужен, чтобы удалять сообщение у человека
        db.execute("ALTER TABLE crm_messages ADD COLUMN tg_id INTEGER DEFAULT 0")
    if "deleted" not in cm_cols:
        db.execute("ALTER TABLE crm_messages ADD COLUMN deleted INTEGER DEFAULT 0")
    cl_cols = {r[1] for r in db.execute("PRAGMA table_info(crm_leads)")}
    for col, ddl in (("trashed_at", "INTEGER DEFAULT 0"), ("purged_at", "INTEGER DEFAULT 0"), ("summary", "TEXT")):
        if col not in cl_cols:  # корзина и справка после очистки
            db.execute(f"ALTER TABLE crm_leads ADD COLUMN {col} {ddl}")
    db.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('payment_store_mode',?)", (PAYMENT_STORE_MODE,))
    db.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('maintenance_mode','1')")
    db.execute(
        "UPDATE users SET method_mode=(SELECT v FROM meta WHERE k='payment_store_mode') "
        "WHERE method_id IS NOT NULL AND method_mode IS NULL"
    )
    db.execute(
        "UPDATE pending SET store_mode=(SELECT v FROM meta WHERE k='payment_store_mode') "
        "WHERE store_mode IS NULL"
    )
    db.execute(
        "UPDATE pending_methods SET store_mode=(SELECT v FROM meta WHERE k='payment_store_mode') "
        "WHERE store_mode IS NULL"
    )
    db.commit()


migrate()
logging.info("База данных: %s", os.path.abspath(DB_PATH))
PAYMENT_API_LOCK = threading.Lock()


class BlockedUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        if user and user.id != ADMIN_ID:
            row = db.execute("SELECT blocked FROM users WHERE user_id=?", (user.id,)).fetchone()
            if row and row["blocked"]:
                if isinstance(event, Message):
                    await event.answer(T_BLOCKED)
                elif isinstance(event, CallbackQuery):
                    await event.answer(T_BLOCKED, show_alert=True)
                return None
        return await handler(event, data)


dp.message.outer_middleware(BlockedUserMiddleware())
dp.callback_query.outer_middleware(BlockedUserMiddleware())


class CrmEventLogMiddleware(BaseMiddleware):
    """Пишет в CRM, на какие кнопки человек нажимает и какие команды запускает (серые строки в чате мини-аппа)."""
    async def __call__(self, handler, event, data):
        try:
            u = event.from_user
            if u and u.id != ADMIN_ID:
                if isinstance(event, CallbackQuery):
                    msg = event.message
                    label = None
                    for kb_row in (getattr(getattr(msg, "reply_markup", None), "inline_keyboard", None) or []):
                        for b in kb_row:
                            if b.callback_data == event.data:
                                label = b.text
                    lines = ((getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip().splitlines())
                    hint = lines[0][:60] if lines else ""
                    crm_remember_name(u)
                    crm_sys(u.id, f"🔘 Нажал «{label or event.data}»" + (f"\nпод сообщением: «{hint}»" if hint else ""))
                elif isinstance(event, Message) and event.text and event.text.startswith("/"):
                    crm_remember_name(u)
                    crm_sys(u.id, f"⌨️ Команда {event.text[:80]}")
        except Exception:
            logging.exception("не удалось записать событие в CRM")
        return await handler(event, data)


dp.message.outer_middleware(CrmEventLogMiddleware())
dp.callback_query.outer_middleware(CrmEventLogMiddleware())

awaiting_help: set[int] = set()  # кто сейчас пишет вопрос администратору
awaiting_custom_days: dict[int, tuple[int, int, int, int]] = {}  # user_id, страница, панель, запрос срока
awaiting_compose: dict[int, tuple[int, int, int]] = {}  # user_id, страница, сообщение-запрос


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def fmt(d: datetime) -> str:
    return d.strftime("%d.%m.%Y")


def autopay_status(row) -> str:
    if not row or not row["method_id"]:
        return "Автосписание не настроено: сохранённого способа оплаты нет."
    method_mode = row["method_mode"] if "method_mode" in row.keys() else None
    if method_mode and method_mode != get_store_mode():
        return "Способ оплаты сохранён в другом магазине; привяжите его заново."
    if not AUTOCHARGE:
        return "Способ оплаты сохранён, но автосписание выключено в настройках бота."
    if row["autopay"]:
        return "Автосписание включено."
    return "Способ оплаты сохранён, автосписание отключено."


def get_store_mode() -> str:
    row = db.execute("SELECT v FROM meta WHERE k='payment_store_mode'").fetchone()
    return row["v"] if row else PAYMENT_STORE_MODE


def store_credentials(mode: str):
    prefix = "YOOKASSA_TEST" if mode == "test" else "YOOKASSA_LIVE"
    shop_id = os.getenv(f"{prefix}_SHOP_ID")
    secret = os.getenv(f"{prefix}_SECRET")
    if mode == PAYMENT_STORE_MODE and not shop_id and not secret:
        shop_id = os.getenv("YOOKASSA_SHOP_ID")
        secret = os.getenv("YOOKASSA_SECRET")
    if not shop_id or not secret:
        raise RuntimeError(f"Не заданы ключи ЮKassa для магазина {mode}.")
    return shop_id, secret


def available_store(mode: str) -> bool:
    try:
        store_credentials(mode)
    except RuntimeError:
        return False
    return True


def yookassa_call(mode: str, method, *args):
    shop_id, secret = store_credentials(mode)
    with PAYMENT_API_LOCK:
        Configuration.configure(shop_id, secret)
        return method(*args)


def maintenance_enabled() -> bool:
    row = db.execute("SELECT v FROM meta WHERE k='maintenance_mode'").fetchone()
    return bool(row and row["v"] == "1")


async def set_maintenance_mode(enabled: bool):
    currently_enabled = maintenance_enabled()
    if enabled == currently_enabled:
        return
    if enabled:
        db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('maintenance_started_at',?)", (iso(now()),))
        db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('maintenance_mode','1')")
        db.commit()
        return

    started = db.execute(
        "SELECT v FROM meta WHERE k='maintenance_started_at'"
    ).fetchone()
    resumed_at = now()
    expired_during_maintenance = []
    if started:
        expired_during_maintenance = db.execute(
            "SELECT user_id FROM users WHERE active=1 AND expires_at IS NOT NULL "
            "AND expires_at<=? AND expires_at>? AND blocked=0",
            (iso(resumed_at), started["v"]),
        ).fetchall()
    grace_until = iso(resumed_at + timedelta(days=3))
    for row in expired_during_maintenance:
        db.execute(
            "UPDATE users SET grace_until=?, last_remind=NULL WHERE user_id=?",
            (grace_until, row["user_id"]),
        )
    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('maintenance_mode','0')")
    db.execute("DELETE FROM meta WHERE k='maintenance_started_at'")
    db.commit()
    for row in expired_during_maintenance:
        await safe_send(row["user_id"], T_MAINTENANCE_GRACE, KB_RENEW)


def maintenance_keyboard():
    return markup([btn("💬 Связаться с поддержкой", "help")])


def maintenance_message():
    return (
        "Сейчас бот находится на технических работах. Оформление и продление подписок временно остановлены.\n"
        "Все подписки и оставшиеся сроки сохранены. Узнать статус можно командой /status."
    )


def link_support_message(chat_id: int, message_id: int, user_id: int):
    db.execute(
        "INSERT OR REPLACE INTO support_links(chat_id, message_id, user_id) VALUES(?,?,?)",
        (chat_id, message_id, user_id),
    )
    db.commit()


def support_user(chat_id: int, message_id: int):
    row = db.execute(
        "SELECT user_id FROM support_links WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    ).fetchone()
    return row["user_id"] if row else None


def active_subscription(uid: int):
    row = db.execute("SELECT expires_at FROM users WHERE user_id=? AND active=1", (uid,)).fetchone()
    return row if row and row["expires_at"] and datetime.fromisoformat(row["expires_at"]) > now() else None


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def markup(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


KB_START = markup([btn("💳 Оплатить", "buy")])
KB_RENEW = markup([btn("💳 Продлить подписку", "buy")])
KB_PAY = lambda: markup([btn(f"💳 Оплатить {PRICE:.0f} ₽", "pay")], [btn("💬 Связаться", "help")])
KB_PRODUCT = lambda: markup(
    [btn(f"💳 Оплатить {PRICE:.0f} ₽", "pay")],
    [btn("❓ Я не понимаю, что получу", "payment:details")],
    [btn("💬 Связаться", "help")],
)
KB_STUCK = markup([btn("💬 Связаться", "help")], [btn("💳 Оплатить", "buy")])
KB_HELP = markup([btn("💬 Связаться", "help")])
USERS_PAGE_SIZE = 8


def ensure_user(uid: int, username: str | None):
    db.execute("INSERT INTO users(user_id, username, joined_at) VALUES(?,?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
               (uid, username, iso(now())))
    db.commit()


async def safe_send(uid: int, text: str, kb=None):
    try:
        await bot.send_message(uid, text, reply_markup=kb)
    except Exception:
        logging.warning("не удалось написать %s", uid)


# ---------- ЮKassa ----------
def _create_payment(user_id: int, method_id: str | None = None,
                    save_payment_method: bool | None = None,
                    replace_method: bool = False, store_mode: str | None = None):
    if save_payment_method is None:
        save_payment_method = AUTOCHARGE
    store_mode = store_mode or get_store_mode()
    params = {
        "amount": {"value": f"{PRICE:.2f}", "currency": "RUB"},
        "capture": True,
        "description": "Ежемесячная подписка на закрытый канал с лекциями",
        "metadata": {
            "user_id": str(user_id),
            "replace_method": str(replace_method).lower(),
            "store_mode": store_mode,
        },
    }
    if method_id:  # автосписание с сохранённого способа оплаты
        params["payment_method_id"] = method_id
    else:
        params["confirmation"] = {"type": "redirect", "return_url": RETURN_URL}
        params["save_payment_method"] = save_payment_method
    return yookassa_call(store_mode, Payment.create, params, str(uuid.uuid4()))


async def new_payment(user_id: int, method_id: str | None = None,
                      save_payment_method: bool | None = None, replace_method: bool = False):
    is_auto = method_id is not None
    if is_auto and maintenance_enabled():
        raise RuntimeError("Автосписания приостановлены на время технических работ.")
    store_mode = get_store_mode()
    if method_id:
        user = db.execute("SELECT method_mode FROM users WHERE user_id=?", (user_id,)).fetchone()
        if user and user["method_mode"] and user["method_mode"] != store_mode:
            raise RuntimeError("Сохранённый способ оплаты принадлежит другому магазину ЮKassa.")
    p = await asyncio.to_thread(
        _create_payment, user_id, method_id, save_payment_method, replace_method, store_mode
    )
    db.execute(
        "INSERT OR IGNORE INTO pending(id, user_id, created, is_auto, store_mode) VALUES(?,?,?,?,?)",
        (p.id, user_id, iso(now()), int(is_auto), store_mode),
    )
    db.commit()
    return p


def _create_payment_method(user_id: int, store_mode: str):
    params = {
        "type": "bank_card",
        "confirmation": {"type": "redirect", "return_url": RETURN_URL},
        "metadata": {"user_id": str(user_id)},
    }
    return yookassa_call(store_mode, PaymentMethod.create, params, str(uuid.uuid4()))


async def new_payment_method(user_id: int):
    store_mode = get_store_mode()
    pending = db.execute(
        "SELECT confirmation_url FROM pending_methods WHERE user_id=? AND store_mode=? "
        "ORDER BY created DESC LIMIT 1",
        (user_id, store_mode),
    ).fetchone()
    if pending:
        return pending["confirmation_url"]
    method = await asyncio.to_thread(_create_payment_method, user_id, store_mode)
    confirmation_url = method.confirmation.confirmation_url
    db.execute(
        "INSERT INTO pending_methods(id,user_id,created,confirmation_url,store_mode) VALUES(?,?,?,?,?)",
        (method.id, user_id, iso(now()), confirmation_url, store_mode),
    )
    db.commit()
    return confirmation_url


async def handle_payment_method(method_id: str):
    pending = db.execute(
        "SELECT user_id, store_mode FROM pending_methods WHERE id=?", (method_id,)
    ).fetchone()
    if not pending:
        return
    mode = pending["store_mode"] or get_store_mode()
    method = await asyncio.to_thread(yookassa_call, mode, PaymentMethod.find_one, method_id)
    uid = pending["user_id"]
    if method.status == "active" and method.saved:
        db.execute(
            "UPDATE users SET method_id=?, method_mode=?, last_charge=NULL WHERE user_id=?",
            (method.id, mode, uid),
        )
        db.execute("DELETE FROM pending_methods WHERE user_id=?", (uid,))
        db.commit()
        await safe_send(
            uid,
            "✅ Новый способ оплаты сохранён без списания средств. "
            "Срок подписки не изменился. Посмотреть автосписание можно в /status.",
        )
    elif method.status == "inactive" or datetime.fromisoformat(pending["created"]) < now() - timedelta(days=2):
        db.execute("DELETE FROM pending_methods WHERE id=?", (method_id,))
        db.commit()
        await safe_send(
            uid,
            "Не удалось привязать способ оплаты или ссылка устарела. Попробуйте снова через /status.",
        )


async def handle_payment(p):
    """Вызывается, когда платёж успешен. Идемпотентна: повторный вызов ничего не делает."""
    if p.status != "succeeded":
        return
    uid = int(p.metadata["user_id"])
    pending = db.execute("SELECT store_mode FROM pending WHERE id=?", (p.id,)).fetchone()
    store_mode = p.metadata.get("store_mode") or (
        pending["store_mode"] if pending and pending["store_mode"] else get_store_mode()
    )
    cur = db.execute("INSERT OR IGNORE INTO payments VALUES(?,?,?)", (p.id, uid, iso(now())))
    db.execute("DELETE FROM pending WHERE id=?", (p.id,))
    db.commit()
    if cur.rowcount == 0:
        return
    user = db.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    username = f" (@{user['username']})" if user and user["username"] else ""
    amount = p.amount.value if p.amount else f"{PRICE:.2f}"
    currency = p.amount.currency if p.amount else "RUB"
    await safe_send(
        ADMIN_ID,
        f"💳 Получена оплата\nПользователь: <code>{uid}</code>{username}\n"
        f"Сумма: {amount} {currency}\nПлатёж: <code>{html.escape(str(p.id))}</code>",
    )
    method = p.payment_method.id if p.payment_method and getattr(p.payment_method, "saved", False) else None
    await extend(
        uid, PERIOD_DAYS, method,
        replace_method=bool(p.metadata.get("replace_method")),
        method_mode=store_mode,
    )


async def extend(uid: int, days: float, method_id: str | None = None,
                 replace_method: bool = False, method_mode: str | None = None):
    row = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    base, was_active = now(), False
    if row and row["expires_at"] and row["active"]:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp > base:
            base, was_active = exp, True
    new_exp = base + timedelta(days=days)
    sub_text = f"💳 Подписка {'продлена' if was_active else 'оформлена'}, активна до {fmt(new_exp)}."
    await crm_notify(uid, sub_text)
    db.execute("""INSERT INTO users(user_id, expires_at, active, method_id, method_mode) VALUES(?,?,1,?,?)
                  ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, active=1,
                  method_id=CASE WHEN ? THEN excluded.method_id
                                 ELSE COALESCE(excluded.method_id, users.method_id) END,
                  method_mode=CASE WHEN ? THEN excluded.method_mode
                                   WHEN excluded.method_id IS NOT NULL THEN excluded.method_mode
                                   ELSE users.method_mode END,
                  autopay=CASE WHEN ? AND excluded.method_id IS NULL THEN 0 ELSE users.autopay END,
                  pay_started_at=NULL, stuck_reminded=0, grace_until=NULL""",
               (uid, iso(new_exp), method_id, method_mode if method_id else None,
                int(replace_method), int(replace_method), int(replace_method)))
    db.commit()
    try:  # отметка в CRM: после сохранения подписки и без права сломать выдачу доступа
        crm_sys(uid, sub_text)
    except Exception:
        logging.exception("не удалось записать оплату в CRM")
    status = autopay_status(
        db.execute("SELECT autopay, method_id, method_mode FROM users WHERE user_id=?", (uid,)).fetchone()
    )
    if was_active:
        await safe_send(uid, T_PAID_EXT.format(date=fmt(new_exp), autopay_status=status))
        return
    try:
        link = await bot.create_chat_invite_link(
            CHANNEL_ID, member_limit=1, expire_date=int(time.time()) + 86400)
        await safe_send(uid, T_PAID_NEW.format(
            date=fmt(new_exp), link=link.invite_link, autopay_status=status
        ))
    except Exception:
        logging.exception("не удалось создать ссылку")
        await safe_send(uid, T_PAID_NOLINK.format(autopay_status=status), KB_HELP)
        await safe_send(ADMIN_ID, f"⚠️ Не удалось выдать ссылку пользователю <code>{uid}</code>. Бот — админ канала?")


async def expire(uid: int, notify_user: bool = True) -> bool:
    removed = False
    try:
        await bot.ban_chat_member(CHANNEL_ID, uid)
        await bot.unban_chat_member(CHANNEL_ID, uid, only_if_banned=True)  # убрать, но не банить навсегда
        removed = True
    except Exception:
        logging.exception("не удалось удалить %s из канала", uid)
    db.execute("UPDATE users SET active=0, grace_until=NULL WHERE user_id=?", (uid,))
    db.commit()
    if notify_user:
        await safe_send(uid, T_EXPIRED, KB_RENEW)
    return removed


async def reset_subscription(uid: int, notify_user: bool = True) -> bool:
    removed = await expire(uid, notify_user)
    db.execute(
        "UPDATE users SET active=0, expires_at=NULL, method_id=NULL, autopay=0, "
        "last_remind=NULL, last_charge=NULL, pay_started_at=NULL, stuck_reminded=0, grace_until=NULL "
        "WHERE user_id=?",
        (uid,),
    )
    db.commit()
    return removed


async def set_subscription_days(uid: int, days: float):
    row = db.execute("SELECT active, expires_at FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        raise ValueError("Пользователь не найден.")
    was_active = bool(
        row["active"] and row["expires_at"]
        and datetime.fromisoformat(row["expires_at"]) > now()
    )
    new_exp = now() + timedelta(days=days)
    db.execute(
        "UPDATE users SET expires_at=?, active=1, last_remind=NULL, last_charge=NULL, grace_until=NULL, "
        "pay_started_at=NULL, stuck_reminded=0 WHERE user_id=?",
        (iso(new_exp), uid),
    )
    db.commit()
    status = autopay_status(
        db.execute(
            "SELECT autopay, method_id, method_mode FROM users WHERE user_id=?", (uid,)
        ).fetchone()
    )
    if was_active:
        await safe_send(uid, T_PAID_EXT.format(date=fmt(new_exp), autopay_status=status))
        return new_exp
    try:
        link = await bot.create_chat_invite_link(
            CHANNEL_ID, member_limit=1, expire_date=int(time.time()) + 86400)
        await safe_send(uid, T_PAID_NEW.format(
            date=fmt(new_exp), link=link.invite_link, autopay_status=status
        ))
    except Exception:
        logging.exception("не удалось создать ссылку")
        await safe_send(uid, T_PAID_NOLINK.format(autopay_status=status), KB_HELP)
        await safe_send(ADMIN_ID, f"⚠️ Не удалось выдать ссылку пользователю <code>{uid}</code>. Бот — админ канала?")
    return new_exp


# ---------- Воронка: /start -> оплата ----------
@dp.message(Command("start", "join"))
async def start(m: Message, command: CommandObject):
    payload = (command.args or "").strip()
    if funnel_source_id(payload) and not active_subscription(m.from_user.id):
        await funnel_start(m, payload)  # пришёл по ссылке из Instagram, YouTube и т.п. -> воронка
        return
    ensure_user(m.from_user.id, m.from_user.username)
    u = db.execute("SELECT * FROM users WHERE user_id=?", (m.from_user.id,)).fetchone()
    if active_subscription(m.from_user.id):
        await m.answer(T_ACTIVE.format(date=fmt(datetime.fromisoformat(u["expires_at"]))), reply_markup=KB_HELP)
        return
    if maintenance_enabled() and m.from_user.id != ADMIN_ID:
        await m.answer(maintenance_message(), reply_markup=maintenance_keyboard())
        return
    await m.answer(T_WELCOME, reply_markup=KB_START)


@dp.callback_query(F.data == "buy")
async def buy(cb: CallbackQuery):
    uid = cb.from_user.id
    ensure_user(uid, cb.from_user.username)
    if maintenance_enabled() and uid != ADMIN_ID:
        await cb.message.answer(maintenance_message(), reply_markup=maintenance_keyboard())
        await cb.answer()
        return
    # человек дошёл до оплаты: запускаем таймер «застрял на оплате»
    db.execute("UPDATE users SET pay_started_at=?, stuck_reminded=0 WHERE user_id=? AND active=0", (iso(now()), uid))
    db.commit()
    offer = T_OFFER_LINK.format(url=html.escape(OFFER_URL, quote=True)) if OFFER_URL else T_OFFER_SOON
    text = T_PRODUCT.format(price=f"{PRICE:.0f}", offer=offer) + (T_AUTO_NOTE if AUTOCHARGE else "")
    if os.path.isfile(PAYMENT_IMAGE_PATH):
        await cb.message.answer_photo(
            FSInputFile(PAYMENT_IMAGE_PATH), caption=text, reply_markup=KB_PRODUCT()
        )
    else:
        logging.error("Изображение оплаты не найдено: %s", PAYMENT_IMAGE_PATH)
        await safe_send(ADMIN_ID, f"⚠️ Не найден файл картинки оплаты: <code>{html.escape(PAYMENT_IMAGE_PATH)}</code>")
        await cb.message.answer(text, reply_markup=KB_PRODUCT())
    await cb.answer()


@dp.callback_query(F.data == "payment:details")
async def payment_details(cb: CallbackQuery):
    if maintenance_enabled() and cb.from_user.id != ADMIN_ID:
        await cb.message.answer(maintenance_message(), reply_markup=maintenance_keyboard())
        await cb.answer()
        return
    await cb.message.answer(T_WHAT_YOU_GET, reply_markup=KB_PAY())
    await cb.answer()


@dp.callback_query(F.data == "pay")
async def pay(cb: CallbackQuery):
    uid = cb.from_user.id
    ensure_user(uid, cb.from_user.username)
    await cb.answer()
    if maintenance_enabled() and uid != ADMIN_ID:
        await cb.message.answer(maintenance_message(), reply_markup=maintenance_keyboard())
        return
    if TEST_MODE:  # ЮKassa ещё не подключена: «оплатить» работает только у админа (для проверки)
        if uid == ADMIN_ID:
            await extend(uid, PERIOD_DAYS)
        else:
            await cb.message.answer(T_NOT_READY, reply_markup=KB_HELP)
        return
    try:
        p = await new_payment(uid)
    except Exception:
        logging.exception("не удалось создать платёж пользователю %s", uid)
        await cb.message.answer(
            "Не удалось создать платёж в выбранном магазине. Попробуйте позже или свяжитесь с нами.",
            reply_markup=KB_HELP,
        )
        return
    kb = markup([InlineKeyboardButton(text="Перейти к оплате", url=p.confirmation.confirmation_url)],
                [btn("💬 Связаться", "help")])
    await cb.message.answer(T_PAY_LINK, reply_markup=kb)


# ---------- Связь с владельцем ----------
@dp.message(Command("help"))
async def help_cmd(m: Message):
    ensure_user(m.from_user.id, m.from_user.username)
    awaiting_help.add(m.from_user.id)
    await m.answer(T_HELP_PROMPT)


@dp.callback_query(F.data == "help")
async def help_btn(cb: CallbackQuery):
    ensure_user(cb.from_user.id, cb.from_user.username)
    awaiting_help.add(cb.from_user.id)
    await cb.message.answer(T_HELP_PROMPT)
    await cb.answer()


# ====================== FUNNEL: Instagram -> группа -> урок -> видео -> тест -> профиль админу ======================
# Тексты воронки. Разметка HTML (<b>, <i>, <a href="...">) работает в сообщениях бота.
FUNNEL_GREETING = ("Привет! Я бот Круга Ильи Хабирова. "
                   "Чтобы подобрать для вас подходящий материал, ответьте на один вопрос.")
FUNNEL_GROUP_Q = "<b>Кто вы и что ищете?</b>"
# (текст кнопки, описание для админа)
FUNNEL_GROUPS = [
    ("🧭 Ищу себя и своё предназначение",
     "Ищет себя и своё предназначение, хочет развить глубину понимания своей личности"),
    ("💪 Стать сильнее и реализовать потенциал",
     "Хочет стать сильнее, целостнее и реализовать свой потенциал на 100%"),
    ("🔬 Я психолог / исследую человека",
     "Психолог или исследователь человека и его глубины"),
]

# Единственный урок, который получают все. Замените на свой.
FUNNEL_LESSON = "🎁 <b>Ваш первый урок</b>\nНазвание урока: https://example.com/lesson"

# Видео «Что такое Круг»: ссылка (YouTube и т.п.) ИЛИ file_id видео из Telegram.
# Как получить file_id: отправьте видео этому боту от своего аккаунта, он ответит file_id.
FUNNEL_VIDEO = os.getenv("FUNNEL_VIDEO", "")
# CRM-группа: закрытая группа с включёнными топиками (id вида -100...). Бот создаёт в ней тему на каждого лида.
# 0 = не используется, профили и переписка приходят админу в личку.
CRM_CHAT_ID = int(os.getenv("CRM_CHAT_ID", "0") or 0)
FUNNEL_TOPIC_COLORS = {"cold": 7322096, "interested": 16766590, "ready": 9367192}  # синий, жёлтый, зелёный
FUNNEL_VIDEO_TEXT = ("Посмотрите короткое видео о том, что такое Круг.\n\n"
                     "А я подготовлю для вас уникальный подарок: лекцию из закрытого сообщества "
                     "под вашу уникальную цель.")
FUNNEL_TEST_INTRO = ("Отлично! Теперь пройдите, пожалуйста, наш короткий тест (7 вопросов), "
                     "чтобы я мог подобрать для вас подходящую лекцию.")
FUNNEL_THANKS = ("Спасибо за ваши ответы! 🙏\n"
                 "Наш администратор лично подберёт для вас урок и пришлёт его сюда, в бот. Ожидайте.")

# Рекомендации по сегментам (редактируйте под свои продукты)
FUNNEL_SEGMENTS = {
    "cold": ("❄️ Холодный", "знакомство с бесплатным контентом и бесплатным уроком",
             "бесплатный канал @habirov_cyrcle и бесплатный урок"),
    "interested": ("🙂 Заинтересованный", "подходящий курс или образовательная программа",
                   "подписка «Круг» (закрытый канал с лекциями)"),
    "ready": ("🔥 Готовый", "наиболее подходящий платный продукт",
              "подписка «Круг»: предложить оплату в первую очередь"),
}
# Рекомендуемое направление по ответу на 1-й вопрос теста (по порядку вариантов)
FUNNEL_DIRECTIONS = [
    "реализация потенциала, деньги и действия",
    "уверенность в себе и самопроявление",
    "дисциплина, привычки и внутренняя опора",
    "отношения с людьми и повторяющиеся сценарии",
    "эмоциональная устойчивость и ресурс",
    "поиск предназначения и самоопределение",
    "глубинное самопознание и внутреннее развитие",
    "определить по свободному ответу",
]

# Тест. type: single (одна кнопка) | multi (несколько + «Готово») | text (только текстом).
# free=True: можно ответить и своим сообщением вместо кнопки.
FUNNEL_TEST = [
    {"key": "q1", "type": "single", "free": True,
     "title": "Главный запрос",
     "text": "Если бы ты мог прямо сейчас изменить одну сферу своей жизни, чтобы почувствовать, "
             "что действительно движешься вперёд, что бы ты выбрал?",
     "options": ["Финансы и реализация", "Уверенность в себе", "Дисциплина и сила воли",
                 "Отношения с людьми", "Эмоциональное состояние", "Поиск своего предназначения",
                 "Самопознание и внутреннее развитие", "Другое"]},
    {"key": "q2", "type": "single", "free": True,
     "title": "Основная проблема",
     "text": "А что сейчас больше всего мешает тебе получить желаемое?",
     "options": ["Не хватает дисциплины и последовательности", "Не понимаю, чего действительно хочу",
                 "Знаю, что нужно делать, но постоянно откладываю",
                 "Боюсь проявляться и заявлять о себе",
                 "Чувствую, что мой потенциал намного больше, чем мои результаты",
                 "Не могу избавиться от повторяющихся сценариев",
                 "Не хватает энергии и внутренней опоры", "Другое"]},
    {"key": "q3", "type": "single", "free": False,
     "title": "Продолжительность проблемы",
     "text": "Как давно ты замечаешь за собой эту ситуацию?",
     "options": ["Несколько недель", "Несколько месяцев", "Несколько лет",
                 "Кажется, это повторяется всю мою жизнь"]},
    {"key": "q4", "type": "multi", "free": False,
     "title": "Предыдущий опыт",
     "text": "Пробовал ли ты раньше что-нибудь делать, чтобы изменить эту ситуацию?\n"
             "(Можно выбрать несколько, затем нажмите «Готово».)",
     "options": ["Читал книги по психологии и саморазвитию", "Проходил курсы и тренинги",
                 "Работал с психологом или наставником",
                 "Пробовал самостоятельно внедрять привычки и практики",
                 "Много изучал, но мало применял", "Практически ничего не пробовал"]},
    {"key": "q5", "type": "text", "free": True,
     "title": "Желаемый результат",
     "text": "Представь, что через 3 месяца ты действительно проделал большую внутреннюю работу. "
             "Что должно измениться в твоей жизни, чтобы ты сказал: «Это было одно из лучших "
             "решений, которые я принял»?\n\nНапишите ответ сообщением 👇",
     "options": []},
    {"key": "q6", "type": "single", "free": False,
     "title": "Мотивация",
     "text": "Насколько для тебя сейчас важно изменить эту ситуацию?",
     "options": ["Просто интересуюсь саморазвитием", "Было бы здорово что-то изменить",
                 "Давно хочу изменений, но никак не могу начать",
                 "Я действительно готов заниматься собой и действовать"]},
    {"key": "q7", "type": "single", "free": False,
     "title": "Предпочтительный формат",
     "text": "Какой формат развития тебе ближе?",
     "options": ["Самостоятельное обучение через приложение и практические задания",
                 "Структурированная программа с уроками и упражнениями", "Сообщество единомышленников",
                 "Работа с наставником и обратная связь",
                 "Комплексный подход: обучение, практика и окружение"]},
]
OTHER = "Другое"
NO_EXPERIENCE = "Практически ничего не пробовал"
LOTS_OF_STUDY = "Много изучал, но мало применял"


def funnel_source_id(payload) -> str | None:
    """Какому источнику (insta, yt...) соответствует параметр ссылки. None = это не ссылка воронки."""
    p = str(payload or "").strip()
    for key in FUNNEL_SOURCES:
        if p.startswith(key):
            return key
    return None


def funnel_source_detail(payload) -> str | None:
    """Уточнение после названия источника: yt_shadow -> shadow."""
    p, key = str(payload or "").strip(), funnel_source_id(payload)
    d = p[len(key):].lstrip("_-") if key else ""
    return d or None


def funnel_source_label(payload) -> str:
    """Подпись источника для CRM: «▶️ YouTube (shadow)»."""
    key = funnel_source_id(payload)
    if not key:
        return str(payload or "—")
    icon, name = FUNNEL_SOURCES[key]
    d = funnel_source_detail(payload)
    return f"{icon} {name}" + (f" ({d})" if d else "")


def funnel_trunc(s, n: int = 300) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def funnel_segment(a: dict) -> str:
    """Сегмент отражает коммерческую готовность, а не оценку личности."""
    if a.get("flow") == "v2":  # новая воронка: дошёл до конца = готов к работе
        return "ready"
    q6 = a.get("q6_i")
    q5 = (a.get("q5") or "").strip()
    q4 = a.get("q4") or []
    no_exp = q4 == [NO_EXPERIENCE]
    if q6 == 3 and len(q5) >= 20:
        return "ready"
    if q6 == 0 or (q6 == 1 and no_exp and len(q5) < 20):
        return "cold"
    return "interested"


def funnel_extra_problems(a: dict) -> str:
    out = []
    if LOTS_OF_STUDY in (a.get("q4") or []):
        out.append("знания не превращаются в действия")
    if a.get("q3_i") == 3:
        out.append("устойчивый повторяющийся сценарий «на всю жизнь»")
    if a.get("q2_i") in (1, 4) and a.get("q1_i") != 5:
        out.append("неясность собственных желаний и предназначения" if a.get("q2_i") == 1
                   else "разрыв между потенциалом и результатами")
    return "; ".join(out) or "не выявлены"


def funnel_resume(a: dict, seg: str) -> str:
    """Короткое индивидуальное резюме по шаблонам: фразы меняются в зависимости от ответов."""
    low = lambda s: str(s or "").strip().rstrip(".").lower()
    parts = [f"Главный запрос сейчас: «{low(a.get('q1'))}». Основное, что мешает: «{low(a.get('q2'))}»."]
    if a.get("q3"):
        parts.append(f"Как давно это длится: {low(a['q3'])}.")
    q4 = a.get("q4") or []
    if NO_EXPERIENCE in q4:
        parts.append("Опыта работы над собой почти нет, это первый шаг.")
    elif LOTS_OF_STUDY in q4:
        parts.append("Много изучал, но знания слабо превращаются в действия: нужна практика и структура.")
    elif q4:
        parts.append("Уже пробовал: " + ", ".join(low(x) for x in q4) + ".")
    if (a.get("q5") or "").strip():
        parts.append(f"Через 3 месяца хочет прийти к такому результату: «{funnel_trunc(a['q5'], 200)}».")
    motiv = ["пока просто интересуется саморазвитием", "считает, что было бы здорово что-то изменить",
             "давно хочет перемен, но не может начать", "готов заниматься собой и действовать"]
    if a.get("q6_i") is not None and 0 <= a["q6_i"] < 4:
        parts.append(f"Мотивация: {motiv[a['q6_i']]}.")
    if a.get("q7"):
        parts.append(f"Ближе формат: {low(a['q7'])}.")
    return " ".join(parts)


def funnel_card_v2(uid: int, full_name: str, username, row, a: dict) -> str:
    e = html.escape
    who = f'<a href="tg://user?id={uid}">{e(full_name)}</a>' + (f" @{e(username)}" if username else "")
    lines = ["🟢 <b>Новая заявка: прошёл воронку, готов к работе</b>",
             f"Пользователь: {who}, id <code>{uid}</code>",
             f"Источник: {e(funnel_source_label(row['source']))}",
             f"Бесплатный урок: {e(a.get('lesson') or '—')}", "", "<b>Ответы на вопросы</b>"]
    for i, t in enumerate(FX_Q_SHORT):
        lines += [f"<b>{i + 1}. {e(t)}</b>", e(funnel_trunc(a.get(f"q{i + 1}"), 450) or "—"), ""]
    lines += ["<b>Опыт в приложении</b>",
              "Получил ценность: " + ("да" if a.get("fb_value") == "yes" else "—"),
              f"Что понравилось: {e(funnel_trunc(a.get('fb_text'), 450) or '—')}", "",
              "<i>Полные ответы в карточке лида в CRM-мини-аппе.</i>"]
    return "\n".join(lines)


def funnel_card(uid: int, full_name: str, username, row, a: dict) -> str:
    if a.get("flow") == "v2":
        return funnel_card_v2(uid, full_name, username, row, a)
    e = html.escape
    seg = funnel_segment(a)
    label, seg_hint, product = FUNNEL_SEGMENTS[seg]
    q1_i = a.get("q1_i")
    direction = FUNNEL_DIRECTIONS[q1_i] if isinstance(q1_i, int) and 0 <= q1_i < len(FUNNEL_DIRECTIONS) \
        else FUNNEL_DIRECTIONS[-1]
    who = f'<a href="tg://user?id={uid}">{e(full_name)}</a>' + (f" @{e(username)}" if username else "")
    q4 = a.get("q4") or []
    lines = [
        "🆕 <b>Новая заявка из воронки</b>",
        f"Пользователь: {who}, id <code>{uid}</code>",
        f"Источник: {e(funnel_source_label(row['source']))}",
        f"Аудитория: {e(a.get('group') or '—')}",
        f"Сегмент: <b>{label}</b> (рекомендация: {seg_hint})",
        "",
        "<b>Профиль</b>",
        f"• Главный запрос: {e(funnel_trunc(a.get('q1')))}",
        f"• Основная проблема: {e(funnel_trunc(a.get('q2')))}",
        f"• Дополнительные проблемы: {e(funnel_extra_problems(a))}",
        f"• Как давно: {e(a.get('q3') or '—')}",
        f"• Предыдущий опыт: {e('; '.join(q4) or '—')}",
        f"• Желаемый результат: {e(funnel_trunc(a.get('q5')) or '—')}",
        f"• Мотивация: {e(a.get('q6') or '—')}",
        f"• Формат: {e(a.get('q7') or '—')}",
        f"• Рекомендуемое направление: {e(direction)}",
        f"• Рекомендуемый продукт: {e(product)}",
        "",
        "<b>Резюме</b>",
        e(funnel_resume(a, seg)),
        "",
        "<i>Сегмент отражает коммерческую готовность, а не оценку личности.</i>",
    ]
    return "\n".join(lines)


# ---------- база ----------
def funnel_get(uid: int):
    return db.execute("SELECT * FROM funnel_users WHERE user_id=?", (uid,)).fetchone()


def funnel_answers(row) -> dict:
    return json.loads(row["answers"] or "{}")


def funnel_update(uid: int, **fields):
    keys = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE funnel_users SET {keys} WHERE user_id=?", (*fields.values(), uid))
    db.commit()


def funnel_reset(uid: int, source: str):
    db.execute("""INSERT INTO funnel_users(user_id, source, answers, stage, step, tmp, done, created)
                  VALUES(?,?,'{"flow":"v2"}','pick',0,'[]',0,?)
                  ON CONFLICT(user_id) DO UPDATE SET source=excluded.source, answers='{"flow":"v2"}',
                    stage='pick', step=0, tmp='[]', done=0, done_at=0, series_idx=0, next_at=0""",
               (uid, source, int(time.time())))
    db.commit()


def funnel_set_answer(uid: int, key: str, value, idx=None):
    a = funnel_answers(funnel_get(uid))
    a[key] = value
    a[key + "_i"] = idx
    funnel_update(uid, answers=json.dumps(a, ensure_ascii=False))


# ---------- шаги ----------
def funnel_test_text(qi: int) -> str:
    q = FUNNEL_TEST[qi]
    text = f"<b>Вопрос {qi + 1} из {len(FUNNEL_TEST)}</b>\n{html.escape(q['text'])}"
    if q["options"]:
        text += "\n\n" + "\n".join(f"{i + 1}. {html.escape(o)}" for i, o in enumerate(q["options"]))
    if q["free"] and q["type"] != "text":
        text += "\n\n<i>Можно выбрать номер кнопкой или написать свой ответ сообщением.</i>"
    return text


def funnel_test_markup(qi: int, selected=()):
    q = FUNNEL_TEST[qi]
    if not q["options"]:
        return None
    buttons = []
    for oi in range(len(q["options"])):
        mark = "✅" if (q["type"] == "multi" and oi in selected) else ""
        buttons.append(InlineKeyboardButton(text=f"{mark}{oi + 1}", callback_data=f"ft:{qi}:{oi}"))
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    if q["type"] == "multi":
        rows.append([btn("Готово ➡️", f"ftd:{qi}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def funnel_ask(uid: int, qi: int):
    await bot.send_message(uid, funnel_test_text(qi), reply_markup=funnel_test_markup(qi))


async def funnel_next(uid: int):
    step = funnel_get(uid)["step"] + 1
    if step < len(FUNNEL_TEST):
        funnel_update(uid, step=step, tmp="[]")
        await funnel_ask(uid, step)
    else:
        await funnel_finish(uid)


FUNNEL_STAGE_NAMES = {"group": "выбор аудитории", "video": "видео «Что такое Круг»", "test": "тест",
                      "pick": "выбор урока", "wait1": "ждёт предложения вопросов", "offer": "предложение ответить на вопросы",
                      "qa": "5 вопросов", "wait2": "ждёт вопроса про опыт", "fb": "вопрос про опыт",
                      "fbreason": "причина «нет»", "fbtext": "что понравилось", "closed": "закрыт (отказался)",
                      "done": "заявка готова"}
FUNNEL_HINTED: set = set()  # кому уже показали подсказку на текущем шаге (чтобы не повторять)


def funnel_partial_card(uid: int, full_name: str, username, row, a: dict) -> str:
    e = html.escape
    who = f'<a href="tg://user?id={uid}">{e(full_name)}</a>' + (f" @{e(username)}" if username else "")
    stage = FUNNEL_STAGE_NAMES.get(row["stage"], row["stage"])
    if row["stage"] == "test":
        stage += f", вопрос {row['step'] + 1} из {len(FUNNEL_TEST)}"
    if a.get("flow") == "v2":
        if row["stage"] == "qa":
            stage += f", вопрос {row['step'] + 1} из {len(FX_QUESTIONS)}"
        return "\n".join([
            "💬 <b>Лид из воронки (в процессе)</b>",
            f"Пользователь: {who}, id <code>{uid}</code>",
            f"Источник: {e(funnel_source_label(row['source']))}",
            f"Урок: {e(a.get('lesson') or 'ещё не выбрал')}",
            f"Этап: {e(stage)}",
        ])
    return "\n".join([
        "💬 <b>Лид из воронки: тест не завершён</b>",
        f"Пользователь: {who}, id <code>{uid}</code>",
        f"Источник: {e(funnel_source_label(row['source']))}",
        f"Аудитория: {e(a.get('group') or 'ещё не выбрал')}",
        f"Остановился на этапе: {e(stage)}",
    ])


async def funnel_build_card(uid: int):
    """Карточка лида: полный профиль после теста, краткая, если тест не завершён."""
    row = funnel_get(uid)
    u = db.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    try:
        chat = await bot.get_chat(uid)
        full_name = chat.full_name or str(uid)
    except Exception:
        full_name = str(uid)
    a = funnel_answers(row)
    username = u["username"] if u else None
    if row["done"]:
        return funnel_card(uid, full_name, username, row, a), full_name, funnel_segment(a)
    return funnel_partial_card(uid, full_name, username, row, a), full_name, None


# ---------- Мини-апп CRM: настройки и переключатель топиков ----------
# Стадии воронки. Определяются сами; в карточке лида стадию можно поставить вручную («Авто» возвращает автоматику).
CRM_STAGES = {"entered": "Зашёл в бота", "series": "Серия уроков", "ready": "Готов к закрытию",
              "won": "Купил подписку", "lost": "Не купил / закрыт"}
# Вероятность оплаты по стадиям для оценки «потенциальной прибыли». Правьте под свою реальную конверсию.
CRM_PROB_STAGE = {"entered": 0.05, "series": 0.2, "ready": 0.5}

# ---------- СЕРИЯ УРОКОВ ----------
# Письма, которые бот сам присылает после того, как человек прошёл квалификацию (тест).
# Формат: (через сколько дней после теста, текст). 0 = сразу. Разметка: <b>жирный</b>, <i>курсив</i>.
# Пока список пустой: стадия «Серия уроков» заканчивается сразу после теста и человек
# попадает в «Готов к закрытию». Добавьте письма, например:
#   (1, "📩 <b>Урок 2.</b> Текст письма..."),
#   (3, "📩 <b>Урок 3.</b> Текст письма..."),
FUNNEL_SERIES: list[tuple[float, str]] = []


def crm_topics_on() -> bool:
    """CRM в топиках включена: задана CRM-группа и она не выключена кнопкой (команда /crm_topics или мини-апп)."""
    if not CRM_CHAT_ID:
        return False
    r = db.execute("SELECT v FROM meta WHERE k='crm_topics'").fetchone()
    return not (r and r["v"] == "0")


def crm_topics_set(on: bool):
    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('crm_topics',?)", ("1" if on else "0",))
    db.commit()


def crm_open_kb(uid: int | None = None):
    """Кнопка «Открыть в CRM» (мини-апп). None, если мини-апп выключен."""
    if not (MINIAPP_ENABLED and MINIAPP_URL):
        return None
    url = MINIAPP_URL + "/app" + (f"?lead={uid}" if uid else "")
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Открыть в CRM", web_app=WebAppInfo(url=url))]])


def crm_remember_name(u):
    db.execute("INSERT INTO crm_profiles(user_id, name) VALUES(?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name", (u.id, u.full_name or ""))


def crm_untrash(uid: int):
    """Человек из корзины снова написал или нажал кнопку: возвращаем его в работу (коммитит вызывающий)."""
    r = db.execute("SELECT stage FROM crm_leads WHERE user_id=?", (uid,)).fetchone()
    if r and r["stage"] == "lost":
        db.execute("UPDATE crm_leads SET stage='auto', trashed_at=0 WHERE user_id=?", (uid,))
        db.execute("INSERT INTO crm_messages(user_id, direction, text, ts) VALUES(?,?,?,?)",
                   (uid, "sys", "↩️ Вернулся: снова написал или нажал кнопку, стадия «закрыт» снята", int(time.time())))


def crm_sys(uid: int, text: str):
    """Служебная серая строка в чате мини-аппа: что человек нажал в боте, какой этап прошёл."""
    if uid == ADMIN_ID:
        return
    crm_untrash(uid)
    db.execute("INSERT INTO crm_messages(user_id, direction, text, ts) VALUES(?,?,?,?)",
               (uid, "sys", text, int(time.time())))
    db.commit()


def crm_log(uid: int, direction: str, m: Message, tg_id: int = 0):
    """Сохраняет переписку в базу (видна в мини-аппе). tg_id нужен, чтобы потом удалить сообщение у человека."""
    text = m.text or m.caption or f"[{m.content_type}]"
    db.execute("INSERT INTO crm_messages(user_id, direction, text, ts, tg_id) VALUES(?,?,?,?,?)",
               (uid, direction, text, int(time.time()), tg_id or (m.message_id if direction == "in" else 0)))
    if direction == "in":
        crm_untrash(uid)
    if direction == "in" and m.from_user:
        crm_remember_name(m.from_user)
    db.commit()


def crm_topic_title(name: str, seg) -> str:
    return f"{name} · {FUNNEL_SEGMENTS[seg][0] if seg else 'новый'}"[:128]


async def crm_post_card(uid: int, thread: int):
    """Кладёт карточку лида в его тему и закрепляет."""
    card, _, _ = await funnel_build_card(uid)
    sent = await bot.send_message(
        CRM_CHAT_ID, card + "\n\n<i>Отвечайте прямо в этой теме: сообщение уйдёт пользователю.</i>",
        message_thread_id=thread)
    try:
        await bot.pin_chat_message(CRM_CHAT_ID, sent.message_id, disable_notification=True)
    except Exception:
        logging.warning("не удалось закрепить карточку лида")


async def crm_topic(uid: int):
    """Тема лида в CRM-группе: возвращает id темы, создаёт при необходимости. None, если CRM не настроена."""
    if not crm_topics_on():
        return None
    r = db.execute("SELECT thread_id FROM crm_topics WHERE user_id=?", (uid,)).fetchone()
    if r:
        return r["thread_id"]
    try:
        _, name, seg = await funnel_build_card(uid)
        kwargs = {"icon_color": FUNNEL_TOPIC_COLORS[seg]} if seg else {}
        topic = await bot.create_forum_topic(CRM_CHAT_ID, crm_topic_title(name, seg), **kwargs)
        thread = topic.message_thread_id
        db.execute("INSERT OR REPLACE INTO crm_topics(user_id, chat_id, thread_id) VALUES(?,?,?)",
                   (uid, CRM_CHAT_ID, thread))
        db.commit()
        await crm_post_card(uid, thread)
        return thread
    except Exception:
        logging.exception("не удалось создать тему в CRM-группе (бот админ с правом «Управление темами»?)")
        return None


async def funnel_card_to_admin(uid: int):
    card, _, _ = await funnel_build_card(uid)
    kb = markup([btn("✉️ Написать ему", f"users:message:{uid}:0")],
                [btn("👤 Карточка пользователя", f"users:view:{uid}:0")])
    try:
        await bot.send_message(ADMIN_ID, card, reply_markup=kb)
    except Exception:
        logging.exception("не удалось отправить профиль админу")


async def funnel_finish(uid: int):
    funnel_update(uid, stage="done", done=1, tmp="[]", done_at=int(time.time()), series_idx=0)
    try:
        crm_sys(uid, "✅ Прошёл квалификацию (тест): " + FUNNEL_SEGMENTS[funnel_segment(funnel_answers(funnel_get(uid)))][0])
    except Exception:
        crm_sys(uid, "✅ Прошёл квалификацию (тест)")
    await bot.send_message(uid, FUNNEL_THANKS)
    existed = crm_topics_on() and db.execute(
        "SELECT 1 FROM crm_topics WHERE user_id=?", (uid,)).fetchone() is not None
    thread = await crm_topic(uid)
    if thread is None:  # CRM-группа не настроена или тема не создалась: профиль в личку админу
        await funnel_card_to_admin(uid)
    elif existed:  # тема уже была (человек писал до конца теста): полный профиль и новое название
        try:
            await crm_post_card(uid, thread)
            _, name, seg = await funnel_build_card(uid)
            await bot.edit_forum_topic(CRM_CHAT_ID, thread, name=crm_topic_title(name, seg))
        except Exception:
            logging.exception("не удалось обновить тему лида")


async def funnel_send_video(uid: int):
    kb = markup([btn("✅ Я посмотрел", "fv:ok")])
    v = FUNNEL_VIDEO.strip()
    text = FUNNEL_VIDEO_TEXT
    if v and not v.startswith("http"):  # file_id видео
        try:
            await bot.send_video(uid, v, caption=text, reply_markup=kb)
            return
        except Exception:
            logging.exception("не удалось отправить видео воронки")
    text += f"\n\n{v}" if v.startswith("http") else "\n\n(видео будет добавлено позже)"
    await bot.send_message(uid, text, reply_markup=kb)


async def funnel_start(m: Message, payload: str):
    uid = m.from_user.id
    ensure_user(uid, m.from_user.username)
    row = funnel_get(uid)
    if row and funnel_answers(row).get("flow") == "v2" and row["stage"] != "pick":
        await fx_resend(uid, row)  # уже в воронке: повторяем текущий шаг, не начинаем заново
        return
    funnel_reset(uid, payload)
    await m.answer(FX_GREETING)
    kb = markup(*[[btn(label, f"fx:l:{i}")] for i, (label, _) in enumerate(FX_LESSONS)])
    await bot.send_message(uid, FX_PICK_Q, reply_markup=kb)


async def funnel_clear_buttons(cb: CallbackQuery):
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ---------- обработчики ----------
@dp.callback_query(F.data.startswith("fg:"))
async def funnel_group(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    row = funnel_get(uid)
    try:
        i = int(cb.data.split(":")[1])
        assert 0 <= i < len(FUNNEL_GROUPS)
    except (ValueError, AssertionError):
        return
    if not row or row["stage"] != "group":
        return
    funnel_set_answer(uid, "group", FUNNEL_GROUPS[i][1], i)
    funnel_update(uid, stage="video")
    await funnel_clear_buttons(cb)
    await bot.send_message(uid, FUNNEL_LESSON)
    await funnel_send_video(uid)


@dp.callback_query(F.data == "fv:ok")
async def funnel_watched(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    row = funnel_get(uid)
    if not row or row["stage"] != "video":
        return
    funnel_update(uid, stage="test", step=0, tmp="[]")
    try:  # кнопку убираем, подпись у видео остаётся
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await bot.send_message(uid, FUNNEL_TEST_INTRO)
    await funnel_ask(uid, 0)


@dp.callback_query(F.data.startswith("ft:"))
async def funnel_test_answer(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    row = funnel_get(uid)
    try:
        _, qi, oi = cb.data.split(":")
        qi, oi = int(qi), int(oi)
        q = FUNNEL_TEST[qi]
        opt = q["options"][oi]
    except (ValueError, IndexError):
        return
    if not row or row["stage"] != "test" or row["step"] != qi:  # защита от старых кнопок
        return
    if q["type"] == "multi":
        selected = set(json.loads(row["tmp"] or "[]"))
        selected ^= {oi}
        funnel_update(uid, tmp=json.dumps(sorted(selected)))
        try:
            await cb.message.edit_reply_markup(reply_markup=funnel_test_markup(qi, selected))
        except Exception:
            pass
        return
    if opt == OTHER:
        await bot.send_message(uid, "Напишите свой вариант одним сообщением 👇")
        return
    funnel_set_answer(uid, q["key"], opt, oi)
    await funnel_clear_buttons(cb)
    await funnel_next(uid)


@dp.callback_query(F.data.startswith("ftd:"))
async def funnel_test_multi_done(cb: CallbackQuery):
    uid = cb.from_user.id
    row = funnel_get(uid)
    try:
        qi = int(cb.data.split(":")[1])
        q = FUNNEL_TEST[qi]
    except (ValueError, IndexError):
        await cb.answer()
        return
    if not row or row["stage"] != "test" or row["step"] != qi:
        await cb.answer()
        return
    selected = sorted(json.loads(row["tmp"] or "[]"))
    if not selected:
        await cb.answer("Выберите хотя бы один вариант.", show_alert=True)
        return
    await cb.answer()
    funnel_set_answer(uid, q["key"], [q["options"][i] for i in selected], selected)
    await funnel_clear_buttons(cb)
    await funnel_next(uid)


@dp.message(Command("app"), F.from_user.id == ADMIN_ID, F.chat.type == "private")
async def cmd_app(m: Message):
    """Открыть CRM-мини-апп."""
    kb = crm_open_kb()
    if not kb:
        await m.answer("Мини-апп выключен: задайте MINIAPP_URL (адрес из вкладки «Домен» в Bothost) и MINIAPP=1.")
        return
    await m.answer("📱 CRM-мини-апп:", reply_markup=kb)


@dp.message(Command("crm_topics"), F.from_user.id == ADMIN_ID, F.chat.type == "private")
async def cmd_crm_topics(m: Message, command: CommandObject):
    """/crm_topics — переключить CRM в топиках; /crm_topics on|off — включить или выключить явно."""
    if not CRM_CHAT_ID:
        await m.answer("CRM-группа с топиками не настроена (нет CRM_CHAT_ID).")
        return
    arg = (command.args or "").strip().lower()
    crm_topics_set(arg == "on" if arg in ("on", "off") else not crm_topics_on())
    if crm_topics_on():
        await m.answer("✅ CRM в топиках включена: новые лиды и сообщения идут в темы группы.")
    else:
        await m.answer("⛔ CRM в топиках выключена. Сообщения лидов приходят сюда уведомлением "
                       "с кнопкой «Открыть в CRM», вся работа в мини-аппе. Вернуть: /crm_topics",
                       reply_markup=crm_open_kb())


@dp.message(Command("links"), F.from_user.id == ADMIN_ID, F.chat.type == "private")
async def cmd_links(m: Message):
    """Ссылки на воронку по источникам."""
    me = await bot.get_me()
    lines = ["🔗 <b>Ссылки на воронку</b> (источник лида отмечается в CRM):", ""]
    for key, (icon, name) in FUNNEL_SOURCES.items():
        lines.append(f"{icon} {name}: <code>https://t.me/{me.username}?start={key}</code>")
    lines += ["", "Чтобы различать ролики или посты, добавьте метку: "
              f"<code>?start={next(iter(FUNNEL_SOURCES))}_reels1</code>. В CRM будет видно источник и метку."]
    await m.answer("\n".join(lines), disable_web_page_preview=True)


def crm_resolve_user(arg: str):
    arg = arg.strip()
    if arg.lstrip("-").isdigit():
        return int(arg)
    r = db.execute("SELECT user_id FROM users WHERE lower(username)=lower(?)", (arg.lstrip("@"),)).fetchone()
    return r["user_id"] if r else None


def crm_cmd_target(m: Message, command: CommandObject):
    """Кого касается команда: id или @ник в аргументе; в теме лида в CRM-группе это сам лид; иначе вы сами."""
    arg = (command.args or "").strip()
    if arg:
        return crm_resolve_user(arg)
    if CRM_CHAT_ID and m.chat.id == CRM_CHAT_ID and m.message_thread_id:
        r = db.execute("SELECT user_id FROM crm_topics WHERE chat_id=? AND thread_id=?",
                       (m.chat.id, m.message_thread_id)).fetchone()
        return r["user_id"] if r else None
    return ADMIN_ID


@dp.message(Command("reset"), F.from_user.id == ADMIN_ID)
async def cmd_reset(m: Message, command: CommandObject):
    """/reset: сбросить свою воронку и начать заново. /reset 123456 или /reset @ник: сбросить человека.
    В теме лида в CRM-группе: просто /reset."""
    uid = crm_cmd_target(m, command)
    if uid is None:
        await m.answer("Не нашёл такого человека. Укажите id или @ник, либо отправьте команду в его теме.")
        return
    await crm_reset_lead(uid, start=(uid == ADMIN_ID))
    if uid == ADMIN_ID:
        await m.answer("🔄 Ваша воронка сброшена, начинаем с нулевого шага 👆")
    else:
        await m.answer(f"🔄 Воронка {html.escape(crm_display_name(uid))} сброшена на нулевой шаг. Ему ничего не "
                       "отправлено: когда он снова откроет ссылку воронки, начнёт сначала.")


@dp.message(Command("erase"), F.from_user.id == ADMIN_ID)
async def cmd_erase(m: Message, command: CommandObject):
    """/erase: удалить из базы себя. /erase 123456 или /erase @ник: удалить человека. В теме лида: просто /erase."""
    uid = crm_cmd_target(m, command)
    if uid is None:
        await m.answer("Не нашёл такого человека. Укажите id или @ник, либо отправьте команду в его теме.")
        return
    await m.answer(
        f"Удалить из базы <b>{html.escape(crm_display_name(uid))}</b> (id <code>{uid}</code>)?\n"
        "Пропадут переписка, заметки, воронка и запись о нём. Платежи останутся в истории. "
        "Если он напишет снова, начнёт с нулевого шага.",
        reply_markup=markup([btn("🗑 Да, удалить", f"er:y:{uid}"), btn("Отмена", "er:n:0")]))


@dp.callback_query(F.data.startswith("er:"), F.from_user.id == ADMIN_ID)
async def cb_erase(cb: CallbackQuery):
    _, act, uid = cb.data.split(":")
    if act != "y":
        await cb.answer("Отменено")
        await cb.message.delete()
        return
    err = await crm_erase_lead(int(uid))
    await cb.answer("Удалено" if not err else "Не удалено", show_alert=bool(err))
    if err:
        await cb.message.answer("⚠️ " + err)
    else:
        try:
            await cb.message.edit_text(f"🗑 Удалён из базы: id <code>{uid}</code>.")
        except Exception:
            pass


@dp.message(Command("chatid"))
async def crm_chat_id(m: Message):
    """Админ отправляет /chatid в группе и получает id для переменной CRM_CHAT_ID."""
    if m.from_user.id == ADMIN_ID:
        await m.answer(f"id этого чата (вставьте в переменную CRM_CHAT_ID):\n<code>{m.chat.id}</code>")


T_PAY_REQUEST = "Чтобы оформить подписку, нажмите на кнопку ниже 👇"


async def crm_send_pay(m: Message, uid: int, custom: str):
    """/pay в теме лида: пользователю уходит сообщение с кнопкой оплаты (карточка, оферта, оплата)."""
    sub = active_subscription(uid)
    if sub:
        await m.reply(f"У пользователя уже активная подписка до {fmt(datetime.fromisoformat(sub['expires_at']))}. "
                      "Форму оплаты не отправляю.")
        return
    try:
        sent = await bot.send_message(uid, html.escape(custom) if custom else T_PAY_REQUEST, reply_markup=KB_START)
        crm_out_save(m.chat.id, m.message_id, uid, sent.message_id)
    except Exception:
        logging.exception("не удалось отправить форму оплаты пользователю %s", uid)
        await m.reply("⚠️ Не удалось отправить: пользователь мог заблокировать бота.")
        return
    db.execute("INSERT INTO crm_messages(user_id, direction, text, ts) VALUES(?,?,?,?)",
               (uid, "out", "[форма оплаты]" + (f" {custom}" if custom else ""), int(time.time())))
    db.commit()
    db.execute("UPDATE users SET pay_started_at=? WHERE user_id=? AND active=0", (iso(now()), uid))
    db.commit()
    await m.reply("✅ Форма оплаты отправлена.")


def crm_out_save(chat_id: int, message_id: int, uid: int, user_message_id: int):
    """Запоминает, какому сообщению пользователя соответствует сообщение админа в теме."""
    db.execute("INSERT OR REPLACE INTO crm_out(chat_id, message_id, user_id, user_message_id) VALUES(?,?,?,?)",
               (chat_id, message_id, uid, user_message_id))
    db.commit()


async def crm_delete(m: Message, uid: int):
    """/del реплаем на своё сообщение в теме: удаляет его у пользователя (Telegram позволяет ~48 часов)."""
    target = m.reply_to_message
    row = target and db.execute(
        "SELECT user_message_id FROM crm_out WHERE chat_id=? AND message_id=? AND user_id=?",
        (m.chat.id, target.message_id, uid)).fetchone()
    if not row:
        await m.reply("Ответьте командой /del на своё сообщение в этой теме, которое нужно удалить у пользователя.")
        return
    try:
        await bot.delete_message(uid, row["user_message_id"])
    except Exception:
        logging.exception("не удалось удалить сообщение у пользователя %s", uid)
        await m.reply("⚠️ Не удалось удалить: возможно, прошло больше 48 часов или сообщение уже удалено.")
        return
    db.execute("DELETE FROM crm_out WHERE chat_id=? AND message_id=?", (m.chat.id, target.message_id))
    db.commit()
    await m.reply("🗑 Удалено у пользователя.")


async def crm_notify(uid: int, text: str):
    """Короткая пометка в теме лида (например, об оплате). Ничего не делает, если темы нет."""
    if not crm_topics_on():
        return
    r = db.execute("SELECT thread_id FROM crm_topics WHERE user_id=?", (uid,)).fetchone()
    if not r:
        return
    try:
        await bot.send_message(CRM_CHAT_ID, text, message_thread_id=r["thread_id"])
    except Exception:
        logging.warning("не удалось написать в CRM-тему %s", uid)


CRM_RELAY_TYPES = {"text", "photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation"}


@dp.message(F.chat.id == CRM_CHAT_ID)
async def crm_group_message(m: Message):
    """Админ пишет в теме лида: сообщение уходит пользователю. Остальное в CRM-группе игнорируем."""
    if not m.from_user or m.from_user.id != ADMIN_ID or not m.message_thread_id:
        return
    if m.content_type not in CRM_RELAY_TYPES:
        return
    r = db.execute("SELECT user_id FROM crm_topics WHERE chat_id=? AND thread_id=?",
                   (m.chat.id, m.message_thread_id)).fetchone()
    if not r:
        return
    uid = r["user_id"]
    if m.text and m.text.startswith("/"):
        parts = m.text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        if cmd == "/pay":  # /pay или /pay свой текст
            await crm_send_pay(m, uid, parts[1].strip() if len(parts) > 1 else "")
        elif cmd == "/del":  # реплаем на своё сообщение: удалить его у пользователя
            await crm_delete(m, uid)
        return
    try:
        sent = await bot.copy_message(uid, m.chat.id, m.message_id)
        crm_out_save(m.chat.id, m.message_id, uid, sent.message_id)
    except Exception:
        logging.exception("не удалось доставить сообщение пользователю %s", uid)
        await m.reply("⚠️ Не удалось доставить: пользователь мог заблокировать бота.")
        return
    crm_log(uid, "out", m, sent.message_id)


# ====================== НОВАЯ ВОРОНКА: урок -> предложение -> 5 вопросов -> урок 2 -> обратная связь -> заявка ======================
# Этапы (funnel_users.stage): pick -> wait1 -> offer -> qa -> wait2 -> fb -> (fbreason) / fbtext -> done.
# «Нет» на шаге offer или fb ведёт в closed (в CRM: «Не купил / закрыт», корзина). «Хочу продолжить» возвращает человека.
# Тексты ниже можно менять свободно. Разметка HTML: <b>жирный</b>, <i>курсив</i>.
FX_GREETING = "Привет! Я бот Круга Ильи Хабирова 👋"
FX_PICK_Q = "<b>Какой бесплатный урок вы хотите получить?</b>"
FX_LESSONS = [("1. Тени", "Тени")]  # (текст кнопки, название урока); пока урок один
FX_LESSON1_CAPTION = "🎁 <b>Ваш бесплатный урок: «{name}»</b>"
FX_TOMORROW = "Завтра напишу вам с новым интересным материалом по теме 🤝"
FX_OFFER = ("А хотите ещё глубже узнать себя? Предлагаю вам ответить на несколько вопросов, а за это подарю "
            "вам подробный урок, как найти у себя те качества, которые ушли в тень.")
FX_BTN_YES = "✍️ Да, хочу ответить"
FX_BTN_NO = "Нет, я получил что хотел"
FX_CLOSED = ("Тогда более не беспокоим. Если захотите продолжить взаимодействие, нажмите на эту кнопку.")
FX_BTN_RESUME = "▶️ Хочу продолжить"
FX_RESUME_Q = "Рад, что вы вернулись! Что выбираете?"
FX_BTN_ANSWER = "✍️ Ответить на вопросы"
FX_BTN_BUY = "💳 Купить подписку"
FX_BTN_TELL = "💬 Рассказать, что понравилось"
FX_QA_INTRO = ("Отлично! Вас ждут 5 вопросов. Отвечайте своими словами, в свободной форме, "
               "не менее трёх предложений: чем честнее и глубже, тем полезнее будет для вас результат.")
# (заголовок, пояснение)
FX_QUESTIONS = [
    ("Что сейчас происходит в твоей жизни?",
     "Какие вопросы, ситуации или внутренние противоречия занимают твои мысли в последнее время? "
     "Есть ли что-то, что ты давно хочешь изменить в себе или своей жизни?"),
    ("Что в себе тебе сложнее всего принять?",
     "Может быть, это определённые эмоции, привычки, реакции, страхи, желания или качества характера. "
     "Есть ли стороны своей личности, которые ты стараешься скрывать, подавлять или не показывать окружающим?"),
    ("В каких ситуациях ты замечаешь, что действуешь не так, как на самом деле хочешь?",
     "Например, подстраиваешься под других, боишься заявить о себе, откладываешь важные решения, "
     "испытываешь внутренний конфликт или повторяешь одни и те же сценарии. Расскажи о конкретном примере, "
     "если он есть."),
    ("Каким человеком ты хотел бы стать и что хотел бы изменить в своём внутреннем состоянии?",
     "Представь, что через полгода ты стал лучше понимать себя, свои желания, эмоции и реакции. "
     "Что конкретно изменилось бы в твоём мышлении, поведении, отношениях и ощущении собственной жизни?"),
    ("Что ты уже предпринимал для собственного развития и чего тебе сейчас не хватает, чтобы двигаться дальше?",
     "Может быть, ты изучал психологию, Юнга, медитировал, проходил курсы, читал книги или работал самостоятельно. "
     "Что помогало, что не давало результата и какая поддержка или среда могли бы тебе помочь?"),
]
FX_Q_SHORT = ["Что происходит в жизни", "Что сложнее всего принять", "Где действует не так, как хочет",
              "Каким хочет стать", "Что уже предпринимал и чего не хватает"]
FX_MIN_CHARS = int(os.getenv("FUNNEL_MIN_CHARS", "150"))        # минимум символов в ответе на вопрос (≈ три предложения)
FX_MIN_CHARS_FB = int(os.getenv("FUNNEL_MIN_CHARS_FB", "60"))   # минимум для рассказа «что понравилось»
FX_TOO_SHORT = ("Расскажите чуть подробнее 🙏 Здесь важна глубина: нужно не менее трёх предложений "
                "(хотя бы {need} символов). Сейчас: {have}.")
FX_TEXT_ONLY = "Пожалуйста, напишите ответ текстом 🙏"
FX_LESSON2_CAPTION = ("🎁 <b>Ваш подарок за ответы</b>\nУрок о том, как найти у себя те качества, "
                      "которые ушли в тень.\n\nЗавтра я вам напишу 🤝")
FX_FB_Q = "Скажите, как вам опыт в приложении? Получили ценность?"
FX_BTN_VAL_YES = "👍 Да"
FX_BTN_VAL_NO = "👎 Нет"
FX_WHY = "Жаль это слышать. Скажите, пожалуйста, почему?"
FX_NO_REASONS = ["Материал оказался не по моей теме", "Было слишком сложно или непонятно",
                 "Не хватило практики и конкретных шагов", "Сейчас нет времени заниматься", "Другое"]
FX_WHY_OTHER = "Напишите, пожалуйста, своими словами 👇"
FX_FB_TEXT = ("Расскажите, а что вам особенно понравилось, за это я подарю вам эксклюзивный подарок. "
              "Какой, узнаете после ответа 😁\n\nОпишите, пожалуйста, подробно. Пишите в свободной форме, от сердца.")
FX_DONE = "Отлично, на днях я вернусь к вам с персональным подарком от Ильи Хабирова 🎁"
FX_WAIT_NOTE = "Вы уже на пути 🙌 Следующее сообщение придёт автоматически, ничего делать не нужно."
FX_ALREADY_DONE = "Ваша заявка уже у меня 🙌 Илья скоро свяжется с вами лично."
FX_DELAY_1_H = float(os.getenv("FUNNEL_DELAY_1_H", "24"))  # через сколько часов после урока 1 предложить вопросы
FX_DELAY_2_H = float(os.getenv("FUNNEL_DELAY_2_H", "24"))  # через сколько часов после урока 2 спросить про опыт
FX_V2_STAGES = ("pick", "wait1", "offer", "qa", "wait2", "fb", "fbreason", "fbtext", "closed")


def fx_video_ref(n: int) -> str:
    """Видео урока n: file_id из базы (админ присылает видео боту с подписью 1 или 2) либо переменная FUNNEL_VIDEO_n."""
    r = db.execute("SELECT v FROM meta WHERE k=?", (f"funnel_video_{n}",)).fetchone()
    return ((r["v"] if r else "") or os.getenv(f"FUNNEL_VIDEO_{n}", "") or (FUNNEL_VIDEO if n == 1 else "")).strip()


def fx_log(uid: int, text: str):
    crm_sys(uid, "🤖 " + text)


def fx_log_in(uid: int, m: Message):
    """Ответ человека на шаге воронки пишем в переписку CRM, но «новым сообщением» для вас не считаем."""
    n = db.execute("SELECT COUNT(*) FROM crm_messages m LEFT JOIN crm_seen s ON s.user_id=m.user_id "
                   "WHERE m.user_id=? AND m.direction='in' AND m.ts>COALESCE(s.ts,0)", (uid,)).fetchone()[0]
    crm_log(uid, "in", m)
    if n == 0:
        db.execute("INSERT OR REPLACE INTO crm_seen(user_id, ts) VALUES(?,?)", (uid, int(time.time())))
        db.commit()


async def fx_send(uid: int, text: str, kb=None) -> bool:
    try:
        await bot.send_message(uid, text, reply_markup=kb)
        return True
    except Exception:
        logging.warning("воронка: не удалось отправить сообщение %s (бот заблокирован?)", uid)
        return False


async def fx_send_lesson(uid: int, n: int, caption: str):
    v = fx_video_ref(n)
    if v and not v.startswith("http"):
        try:
            await bot.send_video(uid, v, caption=caption)
            return
        except Exception:
            logging.exception("воронка: не удалось отправить видео урока %s", n)
    if not v:
        await safe_send(ADMIN_ID, f"⚠️ Не задано видео урока {n}. Отправьте видео этому боту с подписью «{n}».")
    await bot.send_message(uid, caption + (f"\n\n{v}" if v.startswith("http") else "\n\n(видео скоро добавлю)"))


# --- конструкторы сообщений: (текст, клавиатура) ---
def fx_m_offer():
    return FX_OFFER, markup([btn(FX_BTN_YES, "fx:yes")], [btn(FX_BTN_NO, "fx:no")])


def fx_m_question(step: int):
    title, details = FX_QUESTIONS[step]
    return (f"<b>Вопрос {step + 1} из {len(FX_QUESTIONS)}</b>\n\n<b>{html.escape(title)}</b>\n{html.escape(details)}"
            f"\n\n<i>Ответьте сообщением, не менее трёх предложений.</i>"), None


def fx_m_fb():
    return FX_FB_Q, markup([btn(FX_BTN_VAL_YES, "fx:v:y"), btn(FX_BTN_VAL_NO, "fx:v:n")])


def fx_m_why():
    return FX_WHY, markup(*[[btn(o, f"fx:r:{i}")] for i, o in enumerate(FX_NO_REASONS)])


def fx_m_closed():
    return FX_CLOSED, markup([btn(FX_BTN_RESUME, "fx:resume")])


def fx_m_resume(frm: str):
    if frm == "fb":
        return FX_RESUME_Q, markup([btn(FX_BTN_TELL, "fx:tell")], [btn(FX_BTN_BUY, "fx:buy")])
    return FX_RESUME_Q, markup([btn(FX_BTN_ANSWER, "fx:qa")], [btn(FX_BTN_BUY, "fx:buy")])


def fx_save(uid: int, a: dict, **fields):
    funnel_update(uid, answers=json.dumps(a, ensure_ascii=False), **fields)


def fx_close(uid: int, frm: str, why: str):
    """«Закрыто и не реализовано»: человек сам отказался. В CRM лид серый и уходит в корзину."""
    row = funnel_get(uid)
    a = funnel_answers(row)
    a["closed_from"] = frm
    crm_sys(uid, why)  # сначала событие, потом стадия «закрыт» (иначе событие тут же вернуло бы лида из корзины)
    fx_save(uid, a, stage="closed", next_at=0)
    t = int(time.time())
    db.execute("INSERT INTO crm_leads(user_id, stage, trashed_at, updated) VALUES(?,?,?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET stage='lost', trashed_at=excluded.trashed_at, "
               "updated=excluded.updated", (uid, "lost", t, t))
    db.commit()


async def fx_start_qa(uid: int):
    row = funnel_get(uid)
    a = funnel_answers(row)
    a.setdefault("t_qa", int(time.time()))
    fx_save(uid, a, stage="qa", step=0, tmp="[]", next_at=0)
    fx_log(uid, "Предложил ответить на 5 вопросов, человек согласился")
    await bot.send_message(uid, FX_QA_INTRO)
    await bot.send_message(uid, fx_m_question(0)[0])


async def fx_qa_finished(uid: int):
    """Ответил на все 5 вопросов: подарок (урок 2), завтра спросим про опыт."""
    t = int(time.time())
    a = funnel_answers(funnel_get(uid))
    a["t_lesson2"] = t
    fx_save(uid, a, stage="wait2", next_at=t + int(FX_DELAY_2_H * 3600), step=len(FX_QUESTIONS))
    fx_log(uid, "Ответил на все 5 вопросов, отправил урок 2")
    await fx_send_lesson(uid, 2, FX_LESSON2_CAPTION)


async def fx_send_offer(uid: int):
    text, kb = fx_m_offer()
    funnel_update(uid, stage="offer", next_at=0)
    if await fx_send(uid, text, kb):
        fx_log(uid, "Предложил ответить на вопросы за подарок")


async def fx_send_fb(uid: int):
    text, kb = fx_m_fb()
    funnel_update(uid, stage="fb", next_at=0)
    if await fx_send(uid, text, kb):
        fx_log(uid, "Спросил про опыт в приложении")


async def fx_finish(uid: int):
    """Все данные собраны: заявка готова, лид зелёный, вам приходит уведомление и карточка."""
    funnel_update(uid, stage="done", done=1, tmp="[]", done_at=int(time.time()), series_idx=0, next_at=0)
    crm_sys(uid, "✅ Прошёл квалификацию: ответил на 5 вопросов и дал обратную связь")
    await fx_send(uid, FX_DONE)
    existed = crm_topics_on() and db.execute("SELECT 1 FROM crm_topics WHERE user_id=?", (uid,)).fetchone() is not None
    thread = await crm_topic(uid)  # новая тема сразу создаётся с полной карточкой
    if thread and existed:  # тема уже была: добавляем полную карточку и обновляем название
        try:
            await crm_post_card(uid, thread)
            _, name, seg = await funnel_build_card(uid)
            await bot.edit_forum_topic(CRM_CHAT_ID, thread, name=crm_topic_title(name, seg))
        except Exception:
            logging.exception("не удалось обновить тему лида")
    card, name, _ = await funnel_build_card(uid)
    kb = crm_open_kb(uid)
    rows = [[btn("✉️ Написать ему", f"users:message:{uid}:0")]]
    if kb:
        rows.append(kb.inline_keyboard[0])
    try:
        text = (f"🟢 <b>Новая заявка готова к работе:</b> {html.escape(name)}\nОтветы и карточка в CRM." if thread
                else card)
        await bot.send_message(ADMIN_ID, text[:4000], reply_markup=markup(*rows))
    except Exception:
        logging.exception("не удалось отправить уведомление о новой заявке")


async def fx_resend(uid: int, row):
    """Человек снова открыл ссылку воронки: повторяем текущий шаг, а не начинаем сначала."""
    stage, a = row["stage"], funnel_answers(row)
    if stage == "done":
        await fx_send(uid, FX_ALREADY_DONE)
    elif stage in ("wait1", "wait2"):
        await fx_send(uid, FX_WAIT_NOTE)
    elif stage == "offer":
        await fx_send(uid, *fx_m_offer())
    elif stage == "qa":
        await fx_send(uid, fx_m_question(min(row["step"], len(FX_QUESTIONS) - 1))[0])
    elif stage == "fb":
        await fx_send(uid, *fx_m_fb())
    elif stage == "fbreason":
        await fx_send(uid, *fx_m_why())
    elif stage == "fbtext":
        await fx_send(uid, FX_FB_TEXT)
    elif stage == "closed":
        await fx_send(uid, *fx_m_closed())


async def check_funnel():
    """Раз в минуту: по таймеру отправляем предложение вопросов (после урока 1) и вопрос про опыт (после урока 2)."""
    t = int(time.time())
    for r in db.execute("SELECT user_id, stage FROM funnel_users WHERE next_at>0 AND next_at<=? "
                        "AND stage IN ('wait1','wait2')", (t,)).fetchall():
        uid = r["user_id"]
        try:
            if active_subscription(uid):  # уже купил: воронку не продолжаем
                funnel_update(uid, stage="done", done=1, next_at=0)
            elif r["stage"] == "wait1":
                await fx_send_offer(uid)
            else:
                await fx_send_fb(uid)
        except Exception:
            logging.exception("воронка: ошибка шага для %s", uid)
            funnel_update(uid, next_at=0)


async def fx_text(m: Message, row) -> bool:
    """Текстовые ответы на шагах воронки. True = сообщение обработано здесь."""
    a = funnel_answers(row)
    if a.get("flow") != "v2":
        return False
    uid, stage = m.from_user.id, row["stage"]
    if stage not in ("qa", "fbtext", "fbreason"):
        return False
    if not m.text:
        await m.answer(FX_TEXT_ONLY)
        return True
    text = m.text.strip()
    need = FX_MIN_CHARS if stage == "qa" else (FX_MIN_CHARS_FB if stage == "fbtext" else 3)
    if len(text) < need:
        await m.answer(FX_TOO_SHORT.format(need=need, have=len(text)) if stage != "fbreason"
                       else "Напишите, пожалуйста, хотя бы пару слов 🙏")
        return True
    fx_log_in(uid, m)
    if stage == "qa":
        step = row["step"]
        a[f"q{step + 1}"] = text
        if step + 1 < len(FX_QUESTIONS):
            fx_save(uid, a, step=step + 1)
            await m.answer(fx_m_question(step + 1)[0])
        else:
            fx_save(uid, a)
            await fx_qa_finished(uid)
    elif stage == "fbtext":
        a["fb_value"], a["fb_text"] = "yes", text
        fx_save(uid, a)
        await fx_finish(uid)
    else:  # fbreason: причина своими словами
        a["fb_reason"] = text
        fx_save(uid, a)
        fx_close(uid, "fb", "🚪 Закрыт: опыт без ценности, причина: " + funnel_trunc(text, 80))
        await m.answer(*fx_m_closed()[:1], reply_markup=fx_m_closed()[1])
    return True


@dp.callback_query(F.data.startswith("fx:"))
async def fx_callback(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    row = funnel_get(uid)
    if not row:
        return
    parts = cb.data.split(":")
    act, stage = parts[1], row["stage"]
    a = funnel_answers(row)
    if act == "l" and stage == "pick":  # выбрал бесплатный урок
        try:
            name = FX_LESSONS[int(parts[2])][1]
        except (ValueError, IndexError):
            return
        t = int(time.time())
        a.update(lesson=name, t_lesson1=t)
        fx_save(uid, a, stage="wait1", next_at=t + int(FX_DELAY_1_H * 3600))
        await funnel_clear_buttons(cb)
        fx_log(uid, f"Отправил бесплатный урок «{name}»")
        await fx_send_lesson(uid, 1, FX_LESSON1_CAPTION.format(name=html.escape(name)))
        await bot.send_message(uid, FX_TOMORROW)
    elif act == "yes" and stage == "offer":
        await funnel_clear_buttons(cb)
        await fx_start_qa(uid)
    elif act == "no" and stage == "offer":
        await funnel_clear_buttons(cb)
        fx_close(uid, "offer", "🚪 Закрыт: получил урок и не захотел отвечать на вопросы")
        await bot.send_message(uid, *fx_m_closed()[:1], reply_markup=fx_m_closed()[1])
    elif act == "resume" and stage == "closed":
        await funnel_clear_buttons(cb)
        text, kb = fx_m_resume(a.get("closed_from", "offer"))
        await bot.send_message(uid, text, reply_markup=kb)
    elif act == "qa" and stage == "closed":
        await funnel_clear_buttons(cb)
        await fx_start_qa(uid)
    elif act == "tell" and stage == "closed":
        await funnel_clear_buttons(cb)
        funnel_update(uid, stage="fbtext")
        await bot.send_message(uid, FX_FB_TEXT)
    elif act == "buy" and stage == "closed":
        await funnel_clear_buttons(cb)
        fx_log(uid, "Человек выбрал «Купить подписку», отправил форму оплаты")
        await bot.send_message(uid, T_PAY_REQUEST, reply_markup=KB_START)
    elif act == "v" and stage == "fb":
        await funnel_clear_buttons(cb)
        if parts[2] == "y":
            funnel_update(uid, stage="fbtext")
            await bot.send_message(uid, FX_FB_TEXT)
        else:
            funnel_update(uid, stage="fbreason")
            await bot.send_message(uid, *fx_m_why()[:1], reply_markup=fx_m_why()[1])
    elif act == "r" and stage == "fbreason":
        try:
            reason = FX_NO_REASONS[int(parts[2])]
        except (ValueError, IndexError):
            return
        if reason == "Другое":
            await bot.send_message(uid, FX_WHY_OTHER)
            return
        await funnel_clear_buttons(cb)
        a.update(fb_value="no", fb_reason=reason)
        fx_save(uid, a)
        fx_close(uid, "fb", "🚪 Закрыт: опыт без ценности, причина: " + reason)
        await bot.send_message(uid, *fx_m_closed()[:1], reply_markup=fx_m_closed()[1])


@dp.message(Command("fskip"), F.from_user.id == ADMIN_ID, F.chat.type == "private")
async def cmd_fskip(m: Message):
    """Для проверки воронки: не ждать сутки, сразу выполнить ожидающий шаг (свой тестовый проход)."""
    row = funnel_get(ADMIN_ID)
    if not row or row["stage"] not in ("wait1", "wait2"):
        await m.answer("Нет ожидающего шага. Начните воронку ссылкой t.me/бот?start=insta.")
        return
    funnel_update(ADMIN_ID, next_at=1)
    await check_funnel()


@dp.message(F.from_user.id == ADMIN_ID, F.chat.type == "private", F.video)
async def funnel_video_id(m: Message):
    """Видео с подписью 1 или 2: сохраняется как видео урока 1 или 2 воронки. Без подписи: показывает file_id."""
    cap = (m.caption or "").strip()
    if cap in ("1", "2"):
        db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (f"funnel_video_{cap}", m.video.file_id))
        db.commit()
        await m.answer(f"✅ Видео урока {cap} сохранено. Новые лиды получат именно его.")
        return
    await m.answer(f"Чтобы сохранить как урок, отправьте это видео с подписью 1 или 2.\n")
    await m.answer(f"file_id видео (вставьте в переменную FUNNEL_VIDEO):\n<code>{m.video.file_id}</code>")


async def funnel_chat_filter(m: Message) -> bool:
    """Сообщения людей из воронки: ответы теста текстом и переписка с администратором."""
    uid = m.from_user.id
    if m.chat.type != "private":
        return False
    if m.text and m.text.startswith("/"):
        return False
    if uid in awaiting_help:
        return False
    if active_subscription(uid) and not db.execute(
            "SELECT 1 FROM crm_topics WHERE user_id=?", (uid,)).fetchone():
        return False  # оплатившие без темы в CRM идут по старой логике
    if m.reply_to_message and support_user(uid, m.reply_to_message.message_id) == uid:
        return False  # ответ на сообщение админа: обработает any_message
    row = funnel_get(uid)
    if not row:
        return False
    if uid == ADMIN_ID and (m.reply_to_message or row["stage"] == "done" or not m.text):
        return False  # админ не должен «застревать» в воронке при ответах и /restore
    return True


async def funnel_relay(m: Message):
    """Сообщение человека администратору: в его тему CRM, а если CRM не настроена, в личку."""
    uid = m.from_user.id
    crm_log(uid, "in", m)
    thread = await crm_topic(uid)
    if thread:
        try:
            await bot.copy_message(CRM_CHAT_ID, uid, m.message_id, message_thread_id=thread)
        except Exception:
            logging.exception("не удалось переслать сообщение в CRM-тему")
        return
    who = html.escape(m.from_user.full_name) + (f" (@{html.escape(m.from_user.username)})"
                                                 if m.from_user.username else "")
    header = await bot.send_message(
        ADMIN_ID, f"💬 Сообщение от {who}, id <code>{uid}</code>.\n"
                  "Ответьте реплаем на это сообщение или на пересланное.",
        reply_markup=crm_open_kb(uid))
    link_support_message(ADMIN_ID, header.message_id, uid)
    forwarded = await m.copy_to(ADMIN_ID)
    link_support_message(ADMIN_ID, forwarded.message_id, uid)


@dp.message(funnel_chat_filter)
async def funnel_message(m: Message):
    """Ответы теста текстом принимаем как ответы. Всё остальное пересылаем админу: человек не «застревает»."""
    uid = m.from_user.id
    row = funnel_get(uid)
    stage = row["stage"]
    paid = bool(active_subscription(uid))
    if await fx_text(m, row):  # ответы новой воронки (5 вопросов, обратная связь)
        return
    if stage == "test" and not paid:
        q = FUNNEL_TEST[row["step"]]
        if m.text and q["free"]:
            funnel_set_answer(uid, q["key"], m.text.strip(), -1)
            await funnel_next(uid)
            return
    await funnel_relay(m)
    if stage == "done" or paid:
        if row["tmp"] != '["ack"]':
            funnel_update(uid, tmp='["ack"]')
            await m.answer("Передал администратору. Ответ придёт сюда.")
        return
    key = (uid, stage, row["step"])
    if key in FUNNEL_HINTED:
        return
    FUNNEL_HINTED.add(key)
    if stage in ("wait1", "wait2"):
        hint = "Следующий материал пришлю автоматически 🤝"
    elif stage in ("pick", "offer", "fb", "closed", "fbreason"):
        hint = "Нажмите на кнопку выше 👆"
    elif stage == "test":
        hint = "Выберите вариант кнопкой выше 👆"
    else:
        hint = "Нажмите на кнопку выше 👆, чтобы я подобрал для вас урок."
    await m.answer("Передал ваше сообщение администратору 🙌 " + hint)


# ====================== конец блока FUNNEL ======================


# ---------- Копии базы (под вашим контролем) ----------
async def send_backup(reason: str = "авто"):
    """Присылает админу в Telegram консистентную копию bot.db."""
    db.execute("INSERT OR REPLACE INTO meta VALUES('last_backup', ?)", (iso(now()),))
    db.commit()
    path = os.path.join(tempfile.gettempdir(), f"bot-backup-{now():%Y%m%d-%H%M}.db")
    dst = sqlite3.connect(path)
    try:
        db.backup(dst)
    finally:
        dst.close()
    n = db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    try:
        await bot.send_document(ADMIN_ID, FSInputFile(path), caption=(
            f"💾 Копия базы ({reason}). Активных подписчиков: {n}.\n"
            "Восстановить: отправьте этот файл боту с подписью /restore"))
    finally:
        os.remove(path)


@dp.message(Command("backup"))
async def backup_cmd(m: Message):
    if m.from_user.id == ADMIN_ID:
        await send_backup("по запросу")


@dp.message(F.from_user.id == ADMIN_ID, F.document)
async def restore_cmd(m: Message):
    """Файл базы + подпись /restore: полностью заменяет текущую базу."""
    if not (m.caption or "").startswith("/restore"):
        if m.reply_to_message:
            await admin_reply(m)  # это ответ пользователю документом
        else:
            await m.answer("Чтобы восстановить базу, отправьте файл с подписью /restore")
        return
    path = os.path.join(tempfile.gettempdir(), f"restore-{uuid.uuid4().hex}.db")
    try:
        await bot.download(m.document, destination=path)
        src = sqlite3.connect(path)
        try:
            n = src.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            src.backup(db)
        finally:
            src.close()
        migrate()
        await m.answer(f"✅ База восстановлена. Пользователей в ней: {n}.")
    except Exception:
        logging.exception("не удалось восстановить базу")
        await m.answer("Не удалось восстановить: файл не похож на копию базы бота.")
    finally:
        if os.path.exists(path):
            os.remove(path)


@dp.message(F.from_user.id == ADMIN_ID, F.reply_to_message)
async def admin_reply(m: Message):
    reply = m.reply_to_message
    pending_custom = awaiting_custom_days.get(ADMIN_ID)
    if pending_custom and reply.message_id == pending_custom[3]:
        target_uid, page, panel_message_id, _ = pending_custom
        try:
            days = float(m.text or "")
            if not math.isfinite(days) or days <= 0:
                raise ValueError
        except ValueError:
            await m.answer("Ответьте на запрос положительным числом дней или используйте /cancel.")
            return
        try:
            new_exp = await set_subscription_days(target_uid, days)
        except OverflowError:
            await m.answer("Слишком большой срок. Введите меньшее число дней.")
            return
        awaiting_custom_days.pop(ADMIN_ID, None)
        text, keyboard = user_details(target_uid, page)
        await bot.edit_message_text(text, chat_id=ADMIN_ID, message_id=panel_message_id,
                                    reply_markup=keyboard)
        await m.answer(f"Подписка установлена до {fmt(new_exp)}.")
        return

    pending_compose = awaiting_compose.get(ADMIN_ID)
    if pending_compose and reply.message_id == pending_compose[2]:
        target_uid, _, _ = pending_compose
        try:
            sent = await m.copy_to(target_uid)
        except Exception:
            logging.exception("не удалось отправить сообщение пользователю %s", target_uid)
            await m.answer("Не удалось отправить сообщение: пользователь мог заблокировать бота.")
            return
        link_support_message(target_uid, sent.message_id, target_uid)
        crm_log(target_uid, "out", m, sent.message_id)
        awaiting_compose.pop(ADMIN_ID, None)
        await m.answer("✅ Сообщение отправлено. Ответ пользователя придёт сюда; отвечайте на него реплаем.")
        return

    row = db.execute("SELECT user_id FROM help WHERE msg_id=?", (reply.message_id,)).fetchone()
    uid = row["user_id"] if row else support_user(ADMIN_ID, reply.message_id)
    if uid is None and reply.from_user and reply.from_user.is_bot:
        text = reply.text or reply.caption or ""
        first_line = text.splitlines()[0] if text else ""
        if first_line.startswith("💬 Вопрос от "):
            digits = first_line.rsplit(", id ", 1)[-1].removesuffix(".").strip()
            if digits.isdigit():
                uid = int(digits)
    if uid is None:
        await m.answer("Не нашёл, кому отвечать. Отвечайте реплаем именно на сообщение с вопросом.")
        return
    try:
        sent = await m.copy_to(uid)
        link_support_message(uid, sent.message_id, uid)
        crm_log(uid, "out", m, sent.message_id)
        await m.answer("✅ Отправлено. Пользователь сможет ответить реплаем на это сообщение.")
    except Exception:
        logging.exception("не удалось отправить ответ пользователю %s", uid)
        await m.answer("Не удалось отправить: пользователь мог заблокировать бота.")


# ---------- Остальные команды ----------
@dp.message(Command("status"))
async def status(m: Message):
    ensure_user(m.from_user.id, m.from_user.username)
    text, keyboard = subscription_status(m.from_user.id)
    await m.answer(text, reply_markup=keyboard)


def admin_menu():
    total = db.execute("SELECT COUNT(*) FROM users WHERE user_id!=?", (ADMIN_ID,)).fetchone()[0]
    active = db.execute(
        "SELECT COUNT(*) FROM users WHERE user_id!=? AND active=1 AND expires_at>?",
        (ADMIN_ID, iso(now())),
    ).fetchone()[0]
    mode = get_store_mode()
    recurring = db.execute(
        "SELECT COUNT(*) FROM users WHERE user_id!=? AND active=1 AND expires_at>? "
        "AND autopay=1 AND method_id IS NOT NULL AND method_mode=?",
        (ADMIN_ID, iso(now()), mode),
    ).fetchone()[0] if AUTOCHARGE else 0
    revenue = recurring * PRICE
    maintenance = maintenance_enabled()
    test_available = available_store("test")
    live_available = available_store("live")
    if TEST_MODE:
        store_status = f"Выбран магазин: <b>{'тестовый' if mode == 'test' else 'реальный'}</b> "
        store_status += "(сейчас включена локальная имитация платежей, ЮKassa не вызывается)."
    else:
        store_status = f"Выбран магазин: <b>{'тестовый' if mode == 'test' else 'реальный'}</b>."
    text = (
        "<b>Админ-панель</b>\n"
        f"Всего пользователей: <b>{total}</b>\n"
        f"Активных подписок: <b>{active}</b>\n"
        f"С включённым автопродлением в выбранном магазине: <b>{recurring}</b>\n"
        f"Ориентировочные списания за следующий период: <b>{revenue:,.0f} ₽</b> "
        f"({PRICE:,.0f} ₽ × {recurring}; не гарантия фактической выручки)\n\n"
        f"Тех. режим: <b>{'запущен' if maintenance else 'не запущен'}</b>\n"
        f"{store_status}\n"
        f"Доступность ключей: тестовый — {'есть' if test_available else 'нет'}, "
        f"реальный — {'есть' if live_available else 'нет'}."
    )
    keyboard = markup(
        [btn("🛠 Включить тех. режим" if not maintenance else "✅ Выключить тех. режим",
             "menu:maintenance_off" if not maintenance else "menu:maintenance_on")],
        [btn("🧪 Переключиться на тестовый магазин", "menu:store_test")],
        [btn("⚠️ Запросить переключение на реальный", "menu:store_live")],
        [btn("🔄 Обновить", "menu:refresh")],
    )
    return text, keyboard


@dp.message(Command("menu"))
async def menu_command(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    text, keyboard = admin_menu()
    await m.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("menu:"))
async def menu_action(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Команда доступна только администратору.", show_alert=True)
        return
    action = cb.data.split(":", 1)[1]
    if action == "maintenance_off":
        await set_maintenance_mode(True)
        await cb.answer("Новые подписки временно отключены.")
    elif action == "maintenance_on":
        await set_maintenance_mode(False)
        await cb.answer("Приём подписок включён. Подписчикам с истёкшим сроком дано 3 дня на продление.")
    elif action == "store_test":
        if not available_store("test"):
            await cb.answer("Нет тестовых ключей ЮKassa в окружении.", show_alert=True)
            return
        db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('payment_store_mode','test')")
        db.commit()
        await cb.answer("Выбран тестовый магазин.")
    elif action == "store_live":
        if not available_store("live"):
            await cb.answer("Нет реальных ключей ЮKassa в окружении.", show_alert=True)
            return
        await cb.message.answer(
            "Внимание: переключение выберет реальные ключи ЮKassa. "
            "Новые оплаты могут списывать настоящие деньги.",
            reply_markup=markup([btn("Подтвердить реальный магазин", "menu:confirm_live")],
                                [btn("Отмена", "menu:refresh")]),
        )
        await cb.answer()
        return
    elif action == "confirm_live":
        if not available_store("live"):
            await cb.answer("Нет реальных ключей ЮKassa в окружении.", show_alert=True)
            return
        db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('payment_store_mode','live')")
        db.commit()
        await cb.answer("Выбран реальный магазин.")
    elif action != "refresh":
        await cb.answer("Неизвестное действие.", show_alert=True)
        return
    text, keyboard = admin_menu()
    await cb.message.edit_text(text, reply_markup=keyboard)


def subscription_status(uid: int):
    row = db.execute(
        "SELECT expires_at, method_id, method_mode, autopay, grace_until FROM users WHERE user_id=?",
        (uid,),
    ).fetchone()
    is_active = bool(active_subscription(uid))
    if not is_active and uid != ADMIN_ID:
        if maintenance_enabled():
            state = "Активной подписки нет."
            if row and row["expires_at"]:
                state = f"Подписка истекла <b>{fmt(datetime.fromisoformat(row['expires_at']))}</b>."
            return (
                f"{state}\n"
                "Сейчас бот находится на технических работах, поэтому форма оплаты временно недоступна.",
                maintenance_keyboard(),
            )
        if row and row["grace_until"] and datetime.fromisoformat(row["grace_until"]) > now():
            text = (
                "Срок подписки закончился. Продлите её до "
                f"<b>{fmt(datetime.fromisoformat(row['grace_until']))}</b>, чтобы сохранить доступ."
            )
        elif row and row["expires_at"]:
            text = (
                "Ваша подписка истекла "
                f"<b>{fmt(datetime.fromisoformat(row['expires_at']))}</b>. "
                "Оформите её снова, чтобы восстановить доступ."
            )
        else:
            text = "Активной подписки нет. Чтобы получить доступ, оформите подписку."
        return text, markup([btn("💳 Оформить / продлить подписку", "buy")])

    if is_active:
        subscription = f"Подписка активна до <b>{fmt(datetime.fromisoformat(row['expires_at']))}</b>."
    elif row and row["expires_at"]:
        subscription = f"Подписка истекла <b>{fmt(datetime.fromisoformat(row['expires_at']))}</b>."
    else:
        subscription = "Активной подписки нет."
    has_method = bool(row and row["method_id"])
    if not AUTOCHARGE:
        auto_status = "Автосписание недоступно: его нужно включить в настройках бота."
    elif row and row["method_mode"] and row["method_mode"] != get_store_mode():
        auto_status = (
            "Автосписание не будет работать в выбранном магазине: способ оплаты привязан "
            f"к {'тестовому' if row['method_mode'] == 'test' else 'реальному'} магазину. "
            "Измените способ оплаты."
        )
    elif row and row["autopay"] and has_method:
        auto_status = "Автосписание: <b>включено</b>."
    elif has_method:
        auto_status = "Автосписание: <b>отключено</b>. Способ оплаты сохранён."
    else:
        auto_status = "Автосписание: <b>не настроено</b>. Сначала обновите способ оплаты."
    text = f"{subscription}\n{auto_status}"
    keyboard = markup(
        [btn("🔕 Отключить автосписание", "status:disable")],
        [btn("🔔 Включить автосписание", "status:enable")],
        [btn("💳 Изменить способ оплаты", "status:change_method")],
        [btn("🗑 Удалить способ оплаты", "status:delete_method")],
        [btn("💬 Связаться с нами", "help")],
    )
    return text, keyboard


@dp.callback_query(F.data.startswith("status:"))
async def status_action(cb: CallbackQuery):
    uid = cb.from_user.id
    row = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    action = cb.data.split(":", 1)[1]
    if maintenance_enabled() and uid != ADMIN_ID and action in {
        "change_method", "change_paid", "change_free"
    }:
        await cb.message.answer(maintenance_message(), reply_markup=maintenance_keyboard())
        await cb.answer()
        return
    if not active_subscription(uid) and uid != ADMIN_ID:
        text, keyboard = subscription_status(uid)
        await cb.message.edit_text(text, reply_markup=keyboard)
        await cb.answer()
        return
    if action == "disable":
        if row:
            db.execute("UPDATE users SET autopay=0 WHERE user_id=?", (uid,))
            db.commit()
        await cb.answer("Автосписание отключено.")
    elif action == "enable":
        if not AUTOCHARGE:
            await cb.answer("Автосписание пока не включено в настройках бота.", show_alert=True)
            return
        if not active_subscription(uid):
            await cb.answer("Автосписание можно включить при активной подписке.", show_alert=True)
            return
        if not row or not row["method_id"] or row["method_mode"] != get_store_mode():
            await cb.answer("Сначала нажмите «Изменить способ оплаты».", show_alert=True)
            return
        db.execute("UPDATE users SET autopay=1, last_charge=NULL WHERE user_id=?", (uid,))
        db.commit()
        await cb.answer("Автосписание включено.")
    elif action == "delete_method":
        if not row or not row["method_id"]:
            await cb.answer("Сохранённого способа оплаты нет.", show_alert=True)
            return
        db.execute(
            "UPDATE users SET method_id=NULL, method_mode=NULL, autopay=0, last_charge=NULL "
            "WHERE user_id=?",
            (uid,),
        )
        db.commit()
        await cb.answer("Способ оплаты удалён, автосписание отключено.")
    elif action == "change_method":
        await cb.message.answer(
            "Как вы хотите изменить способ оплаты?",
            reply_markup=markup(
                [btn("Изменить и оплатить продление", "status:change_paid")],
                [btn("Только привязать карту без списания", "status:change_free")],
            ),
        )
        await cb.answer()
        return
    elif action == "change_paid":
        if TEST_MODE:
            await cb.answer("Платежи ЮKassa сейчас не настроены.", show_alert=True)
            return
        try:
            payment = await new_payment(
                uid, save_payment_method=True, replace_method=True
            )
        except Exception:
            logging.exception("не удалось создать платёж для смены способа оплаты: %s", uid)
            await cb.answer("Не удалось создать платёж. Попробуйте позже или свяжитесь с нами.", show_alert=True)
            return
        await cb.message.answer(
            "Чтобы заменить сохранённый способ оплаты, оплатите продление по ссылке. "
            f"К текущему сроку подписки добавится {PERIOD_DAYS:g} дней.",
            reply_markup=markup(
                [InlineKeyboardButton(text="Перейти к оплате", url=payment.confirmation.confirmation_url)],
                [btn("💬 Связаться с нами", "help")],
            ),
        )
        await cb.answer()
        return
    elif action == "change_free":
        if TEST_MODE:
            await cb.answer("Платежи ЮKassa сейчас не настроены.", show_alert=True)
            return
        try:
            confirmation_url = await new_payment_method(uid)
        except Exception:
            logging.exception("не удалось создать привязку способа оплаты: %s", uid)
            await cb.answer("Не удалось создать привязку. Попробуйте позже или свяжитесь с нами.", show_alert=True)
            return
        await cb.message.answer(
            "Откройте форму ЮKassa и подтвердите привязку карты. Списания при привязке не будет, "
            "срок подписки не изменится. После подтверждения можно включить автосписание в /status.",
            reply_markup=markup(
                [InlineKeyboardButton(text="Привязать карту", url=confirmation_url)],
                [btn("💬 Связаться с нами", "help")],
            ),
        )
        await cb.answer()
        return
    else:
        await cb.answer("Неизвестное действие.", show_alert=True)
        return
    text, keyboard = subscription_status(uid)
    await cb.message.edit_text(text, reply_markup=keyboard)


@dp.message(Command("cancel"))
async def cancel(m: Message):
    if m.from_user.id == ADMIN_ID and (
        ADMIN_ID in awaiting_custom_days or ADMIN_ID in awaiting_compose
    ):
        awaiting_custom_days.pop(ADMIN_ID, None)
        awaiting_compose.pop(ADMIN_ID, None)
        await m.answer("Действие отменено.")
        return
    ensure_user(m.from_user.id, m.from_user.username)
    db.execute("UPDATE users SET autopay=0 WHERE user_id=?", (m.from_user.id,))
    db.commit()
    await m.answer(T_CANCEL)


def users_page(page: int):
    count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    pages = max(1, (count + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = db.execute(
        "SELECT user_id, username, expires_at, active, blocked, autopay, method_id, method_mode FROM users "
        "ORDER BY user_id DESC LIMIT ? OFFSET ?",
        (USERS_PAGE_SIZE, page * USERS_PAGE_SIZE),
    ).fetchall()
    lines = [f"<b>Пользователи</b> — всего: {count} (страница {page + 1}/{pages})"]
    keyboard_rows = []
    for row in rows:
        uid = row["user_id"]
        username = f"@{html.escape(row['username'])} " if row["username"] else ""
        if active_subscription(uid):
            subscription = f"активна до {fmt(datetime.fromisoformat(row['expires_at']))}"
        elif row["expires_at"]:
            subscription = f"неактивна (срок был до {fmt(datetime.fromisoformat(row['expires_at']))})"
        else:
            subscription = "подписки нет"
        if row["blocked"]:
            subscription += "; заблокирован"
        subscription += f"; {autopay_status(row)}"
        lines.append(f"• {username}<code>{uid}</code> — {subscription}")
        label = f"@{row['username']}" if row["username"] else str(uid)
        keyboard_rows.append([btn(label[:40], f"users:view:{uid}:{page}")])
    navigation = []
    if page > 0:
        navigation.append(btn("⬅️ Назад", f"users:page:{page - 1}"))
    if page + 1 < pages:
        navigation.append(btn("Далее ➡️", f"users:page:{page + 1}"))
    if navigation:
        keyboard_rows.append(navigation)
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
    return "\n".join(lines), keyboard, page


def user_details(uid: int, page: int):
    row = db.execute(
        "SELECT user_id, username, expires_at, active, blocked, autopay, method_id, method_mode "
        "FROM users WHERE user_id=?",
        (uid,),
    ).fetchone()
    if not row:
        return "Пользователь не найден в базе.", markup([btn("⬅️ К списку", f"users:page:{page}")])
    username = f"@{html.escape(row['username'])}" if row["username"] else "не указан"
    if active_subscription(uid):
        subscription = f"Активна до {fmt(datetime.fromisoformat(row['expires_at']))}"
    elif row["expires_at"]:
        subscription = f"Неактивна (срок был до {fmt(datetime.fromisoformat(row['expires_at']))})"
    else:
        subscription = "Подписки нет"
    if row["blocked"]:
        subscription += "\nСтатус бота: заблокирован"
    subscription += f"\n{autopay_status(row)}"
    text = f"<b>Пользователь</b>\nID: <code>{uid}</code>\nUsername: {username}\nПодписка: {subscription}"
    keyboard = markup(
        [btn("➕ Продлить на 30 дней", f"users:extend30:{uid}:{page}")],
        [btn("📅 Продлить на срок", f"users:custom:{uid}:{page}")],
        [btn("🗑 Сбросить и удалить из канала", f"users:reset:{uid}:{page}")],
        [btn("✉️ Написать пользователю", f"users:message:{uid}:{page}")],
        [btn("⬅️ К списку", f"users:page:{page}")],
    )
    return text, keyboard


@dp.message(Command("users"))
async def users_cmd(m: Message):
    if m.from_user.id != ADMIN_ID:
        await m.answer("Эта команда доступна только администратору.")
        return
    text, keyboard, _ = users_page(0)
    await m.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("users:"))
async def users_action(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Команда доступна только администратору.", show_alert=True)
        return
    parts = cb.data.split(":")
    try:
        action = parts[1]
        if action == "page":
            text, keyboard, _ = users_page(int(parts[2]))
            await cb.message.edit_text(text, reply_markup=keyboard)
        elif action == "view":
            uid, page = int(parts[2]), int(parts[3])
            text, keyboard = user_details(uid, page)
            await cb.message.edit_text(text, reply_markup=keyboard)
        elif action == "extend30":
            uid, page = int(parts[2]), int(parts[3])
            if not db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
                await cb.answer("Пользователь не найден.", show_alert=True)
                return
            await extend(uid, 30)
            text, keyboard = user_details(uid, page)
            await cb.message.edit_text(text, reply_markup=keyboard)
        elif action == "custom":
            uid, page = int(parts[2]), int(parts[3])
            if not db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
                await cb.answer("Пользователь не найден.", show_alert=True)
                return
            prompt = await cb.message.answer(
                f"Ответьте на это сообщение числом дней для пользователя <code>{uid}</code> "
                "(например, <code>45</code>)."
            )
            awaiting_custom_days[ADMIN_ID] = (uid, page, cb.message.message_id, prompt.message_id)
        elif action == "message":
            uid, page = int(parts[2]), int(parts[3])
            if not db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
                await cb.answer("Пользователь не найден.", show_alert=True)
                return
            prompt = await cb.message.answer(
                f"Ответьте на это сообщение текстом или медиа для пользователя "
                f"<code>{uid}</code> — оно будет отправлено ему."
            )
            awaiting_compose[ADMIN_ID] = (uid, page, prompt.message_id)
        elif action == "reset":
            uid, page = int(parts[2]), int(parts[3])
            if not db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
                await cb.answer("Пользователь не найден.", show_alert=True)
                return
            removed = await reset_subscription(uid)
            text, keyboard = user_details(uid, page)
            await cb.message.edit_text(text, reply_markup=keyboard)
            await cb.answer("Подписка сброшена." if removed else "Подписка сброшена; удаление из канала не удалось.")
            return
        else:
            await cb.answer("Неизвестное действие.", show_alert=True)
            return
    except (IndexError, ValueError):
        await cb.answer("Не удалось обработать действие.", show_alert=True)
        return
    await cb.answer()


@dp.message(Command("grant"))
async def grant(m: Message):  # /grant 123456789 30 — выдать доступ вручную
    if m.from_user.id != ADMIN_ID:
        return
    try:
        _, uid, days = m.text.split()
        await extend(int(uid), float(days))
        await m.answer("Готово.")
    except ValueError:
        await m.answer("Формат: /grant ID дни")


@dp.message(Command("del"))
async def delete_subscription(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    parts = (m.text or "").split()
    if len(parts) != 2:
        await m.answer("Формат: /del @username или /del Telegram_ID")
        return

    target = parts[1].lstrip("@")
    if target.isdigit():
        row = db.execute("SELECT user_id FROM users WHERE user_id=?", (int(target),)).fetchone()
    else:
        row = db.execute("SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (target,)).fetchone()
    if not row:
        await m.answer("Пользователь не найден в базе бота. Проверьте username или Telegram ID.")
        return

    uid = row["user_id"]
    removed = await reset_subscription(uid)
    if removed:
        await m.answer(f"Подписка пользователя <code>{uid}</code> сброшена, доступ в канал удалён.")
    else:
        await m.answer(
            f"Подписка пользователя <code>{uid}</code> сброшена, но удалить его из канала не удалось. "
            "Проверьте права бота и логи."
        )


async def broadcast(m: Message, active_only: bool):
    if m.from_user.id != ADMIN_ID:
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        command = "/all_sub" if active_only else "/all"
        await m.answer(f"Формат: {command} текст сообщения")
        return
    body = html.escape(parts[1].strip())
    query = "SELECT user_id FROM users WHERE blocked=0 AND user_id!=?"
    params: tuple = (ADMIN_ID,)
    if active_only:
        query += " AND active=1 AND expires_at>?"
        params += (iso(now()),)
    recipients = db.execute(query, params).fetchall()
    sent = failed = 0
    for row in recipients:
        try:
            await bot.send_message(row["user_id"], body)
            sent += 1
        except Exception:
            logging.exception("не удалось отправить рассылку пользователю %s", row["user_id"])
            failed += 1
        await asyncio.sleep(0.04)
    await m.answer(
        f"Рассылка завершена.\nПолучателей: {len(recipients)}\n"
        f"Отправлено: {sent}\nНе доставлено: {failed}"
    )


@dp.message(Command("all"))
async def broadcast_all(m: Message):
    await broadcast(m, active_only=False)


@dp.message(Command("all_sub"))
async def broadcast_active(m: Message):
    await broadcast(m, active_only=True)


@dp.message(Command("block"))
async def block_user(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    parts = (m.text or "").split()
    if len(parts) != 2:
        await m.answer("Формат: /block @username или /block Telegram_ID")
        return
    target = parts[1].lstrip("@")
    if target.isdigit():
        row = db.execute("SELECT user_id FROM users WHERE user_id=?", (int(target),)).fetchone()
    else:
        row = db.execute("SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (target,)).fetchone()
    if not row:
        await m.answer("Пользователь не найден в базе бота.")
        return
    uid = row["user_id"]
    if uid == ADMIN_ID:
        await m.answer("Нельзя заблокировать администратора.")
        return
    removed = await reset_subscription(uid, notify_user=False)
    db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))
    db.commit()
    result = "из канала удалён" if removed else "не удалось удалить из канала — проверьте права бота"
    await m.answer(
        f"Пользователь <code>{uid}</code> заблокирован в боте и {result}. "
        "Записи и история сохранены; пользователь больше не сможет пользоваться ботом."
    )


@dp.message(Command("unblock"))
async def unblock_user(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    parts = (m.text or "").split()
    if len(parts) != 2:
        await m.answer("Формат: /unblock @username или /unblock Telegram_ID")
        return
    target = parts[1].lstrip("@")
    if target.isdigit():
        row = db.execute("SELECT user_id FROM users WHERE user_id=?", (int(target),)).fetchone()
    else:
        row = db.execute("SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (target,)).fetchone()
    if not row:
        await m.answer("Пользователь не найден в базе бота.")
        return
    db.execute("UPDATE users SET blocked=0 WHERE user_id=?", (row["user_id"],))
    db.commit()
    await m.answer(f"Блокировка пользователя <code>{row['user_id']}</code> снята.")


@dp.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    n = db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    s = db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    recurring = db.execute(
        "SELECT COUNT(*) FROM users WHERE active=1 AND autopay=1 AND method_id IS NOT NULL "
        "AND method_mode=?",
        (get_store_mode(),),
    ).fetchone()[0] if AUTOCHARGE else 0
    pending_auto = db.execute("SELECT COUNT(*) FROM pending WHERE is_auto=1").fetchone()[0]
    stuck = db.execute("SELECT COUNT(*) FROM users WHERE active=0 AND pay_started_at IS NOT NULL").fetchone()[0]
    funnel_done = db.execute("SELECT COUNT(*) FROM funnel_users WHERE done=1").fetchone()[0]
    await m.answer(
        f"Активных подписчиков: {n}\nВсего оплат: {s}\n"
        f"Автосписание включено в настройках: {'да' if AUTOCHARGE else 'нет'}\n"
        f"Активных подписок с автопродлением в текущем магазине: {recurring}\n"
        f"Ожидающих автоплатежей: {pending_auto}\n"
        f"Дошли до оплаты, но не оплатили: {stuck}\n"
        f"Прошли квалификацию в воронке: {funnel_done}\n"
        f"Файл базы: {os.path.abspath(DB_PATH)}"
    )


@dp.message()
async def any_message(m: Message):
    """Всё остальное: либо вопрос администратору (после /help), либо подсказка."""
    uid = m.from_user.id
    ensure_user(uid, m.from_user.username)
    if uid != ADMIN_ID and m.reply_to_message:
        target_uid = support_user(uid, m.reply_to_message.message_id)
        if target_uid == uid:
            who = html.escape(m.from_user.full_name)
            username = f" (@{html.escape(m.from_user.username)})" if m.from_user.username else ""
            header = await bot.send_message(
                ADMIN_ID,
                f"💬 Ответ от {who}{username}, id <code>{uid}</code>.\n"
                "Ответьте реплаем на это сообщение или на пересланное сообщение пользователя.",
            )
            link_support_message(ADMIN_ID, header.message_id, uid)
            forwarded = await m.copy_to(ADMIN_ID)
            link_support_message(ADMIN_ID, forwarded.message_id, uid)
            crm_log(uid, "in", m)
            await m.answer("Ваш ответ передан. Мы ответим вам сюда.")
            return
    if uid in awaiting_help:
        awaiting_help.discard(uid)
        who = html.escape(m.from_user.full_name) + (f" (@{m.from_user.username})" if m.from_user.username else "")
        head = await bot.send_message(
            ADMIN_ID, f"💬 Вопрос от {who}, id <code>{uid}</code>.\nОтветьте реплаем на это сообщение или на следующее.")
        copy = await m.copy_to(ADMIN_ID)
        crm_log(uid, "in", m)
        db.executemany("INSERT OR REPLACE INTO help VALUES(?,?)", [(head.message_id, uid), (copy.message_id, uid)])
        db.commit()
        await m.answer(T_HELP_SENT)
    else:
        await m.answer(T_UNKNOWN, reply_markup=KB_HELP)


# ---------- Фоновая проверка ----------
async def check_pending():
    for r in db.execute(
        "SELECT id, created, user_id, is_auto, store_mode FROM pending"
    ).fetchall():
        try:
            mode = r["store_mode"] or get_store_mode()
            p = await asyncio.to_thread(yookassa_call, mode, Payment.find_one, r["id"])
        except Exception:
            logging.exception("не удалось проверить платёж %s", r["id"])
            continue
        if p.status == "succeeded":
            await handle_payment(p)
        elif p.status == "canceled" or datetime.fromisoformat(r["created"]) < now() - timedelta(days=2):
            db.execute("DELETE FROM pending WHERE id=?", (r["id"],))
            db.commit()
            if r["is_auto"] and p.status == "canceled":
                logging.warning("автоплатёж %s отменён для пользователя %s", r["id"], r["user_id"])
                await safe_send(r["user_id"], T_AUTO_FAIL, KB_RENEW)


async def check_pending_methods():
    pending = db.execute("SELECT id FROM pending_methods").fetchall()
    for row in pending:
        try:
            await handle_payment_method(row["id"])
        except Exception:
            logging.exception("не удалось проверить сохранение способа оплаты %s", row["id"])


async def check_stuck():
    """Дошёл до оплаты, но через STUCK_HOURS так и не оплатил: один раз напоминаем."""
    limit = iso(now() - timedelta(hours=STUCK_HOURS))
    for u in db.execute("SELECT user_id FROM users WHERE active=0 AND stuck_reminded=0 "
                        "AND pay_started_at IS NOT NULL AND pay_started_at<=?", (limit,)).fetchall():
        db.execute("UPDATE users SET stuck_reminded=1 WHERE user_id=?", (u["user_id"],))
        db.commit()
        await safe_send(u["user_id"], T_STUCK, KB_STUCK)


async def check_subscriptions():
    if maintenance_enabled():
        return
    t = now()
    today = t.date().isoformat()
    store_mode = get_store_mode()
    for u in db.execute("SELECT * FROM users WHERE active=1 AND expires_at IS NOT NULL").fetchall():
        uid, left = u["user_id"], datetime.fromisoformat(u["expires_at"]) - t
        if left <= timedelta(0):
            if u["grace_until"] and datetime.fromisoformat(u["grace_until"]) > t:
                continue
            await expire(uid)
            continue
        auto = bool(
            AUTOCHARGE and u["autopay"] and u["method_id"]
            and u["method_mode"] == store_mode
        )
        has_pending_auto = db.execute(
            "SELECT 1 FROM pending WHERE user_id=? AND is_auto=1 "
            "AND (store_mode=? OR store_mode IS NULL) LIMIT 1",
            (uid, store_mode),
        ).fetchone()
        if auto and not has_pending_auto and left <= timedelta(days=1) and u["last_charge"] != today:
            try:
                await new_payment(uid, u["method_id"])
                db.execute("UPDATE users SET last_charge=? WHERE user_id=?", (today, uid))
                db.commit()
            except Exception:
                logging.exception("автосписание не удалось для %s", uid)
                await safe_send(uid, T_AUTO_FAIL, KB_RENEW)
        if left <= timedelta(days=REMIND_DAYS):
            key = u["expires_at"] if auto else today  # автоплатёж: 1 напоминание; иначе раз в день
            if u["last_remind"] != key:
                db.execute("UPDATE users SET last_remind=? WHERE user_id=?", (key, uid))
                db.commit()
                end = fmt(datetime.fromisoformat(u["expires_at"]))
                if auto:
                    await safe_send(uid, T_REMIND_AUTO.format(end=end, price=f"{PRICE:.0f}"))
                else:
                    await safe_send(uid, T_REMIND.format(end=end), KB_RENEW)


async def check_backup():
    r = db.execute("SELECT v FROM meta WHERE k='last_backup'").fetchone()
    if r and datetime.fromisoformat(r["v"]) > now() - timedelta(hours=BACKUP_HOURS):
        return
    await send_backup()


async def check_daily_report():
    """Раз в день сообщает админу число новых пользователей за текущий день по UTC."""
    day = now().date()
    day_key = day.isoformat()
    last = db.execute("SELECT v FROM meta WHERE k='last_daily_report'").fetchone()
    if last and last["v"] == day_key:
        return
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    count = db.execute(
        "SELECT COUNT(*) FROM users WHERE user_id!=? AND joined_at>=? AND joined_at<?",
        (ADMIN_ID, iso(start), iso(end)),
    ).fetchone()[0]
    await bot.send_message(ADMIN_ID, f"📊 Новых пользователей за {day.strftime('%d.%m.%Y')}: {count}")
    db.execute("INSERT OR REPLACE INTO meta VALUES('last_daily_report', ?)", (day_key,))
    db.commit()


# ---------- Автоочистка CRM ----------
# Что чистим и что НЕ трогаем:
#  • Лид в стадии «Не купил / закрыт» попадает в корзину; через CRM_TRASH_DAYS дней удаляются переписка,
#    заметки, профиль теста и тема в CRM-группе. В списке остаётся серая запись с короткой справкой
#    (имя, сегмент, источник, дата входа). Если человек снова напишет или нажмёт кнопку, он возвращается в работу.
#  • У покупателей серые события (нажатия, команды) старше CRM_BUYER_SYS_DAYS дней удаляются.
#  • У остальных серые события старше CRM_SYS_DAYS дней удаляются.
#  • Всегда остаются: строки о прохождении теста (✅) и об оплате подписки (💳), живая переписка
#    (кроме корзины), заметки (кроме корзины), таблица оплат и пользователи.
#  • CRM_STALE_DAYS > 0: лиды, которые зашли, но не прошли тест и не проявляют активности столько дней,
#    сами уходят в корзину (по умолчанию выключено).
CRM_KEEP_SQL = "text NOT LIKE '✅%' AND text NOT LIKE '💳%'"
CRM_BUYERS_SQL = ("SELECT user_id FROM users WHERE active=1 UNION SELECT user_id FROM payments "
                  "UNION SELECT user_id FROM crm_leads WHERE stage='won'")


async def crm_purge(uid: int):
    """Очистка лида из корзины: данные удаляются, остаётся короткая справка."""
    f = funnel_get(uid)
    u = db.execute("SELECT joined_at FROM users WHERE user_id=?", (uid,)).fetchone()
    bits = []
    if f:
        try:
            bits.append("сегмент: " + FUNNEL_SEGMENTS[funnel_segment(funnel_answers(f))][0] if f["done"]
                        else "тест не закончен")
        except Exception:
            pass
        if f["source"]:
            bits.append("источник: " + funnel_source_label(f["source"]))
    if u and u["joined_at"]:
        bits.append("зашёл " + u["joined_at"][:10])
    n_msg = db.execute("SELECT COUNT(*) FROM crm_messages WHERE user_id=? AND direction!='sys'", (uid,)).fetchone()[0]
    if n_msg:
        bits.append(f"сообщений в переписке было: {n_msg}")
    t = db.execute("SELECT chat_id, thread_id FROM crm_topics WHERE user_id=?", (uid,)).fetchone()
    if t:
        try:
            await bot.delete_forum_topic(chat_id=t["chat_id"], message_thread_id=t["thread_id"])
        except Exception:
            logging.warning("не удалось удалить тему CRM лида %s", uid)
    for q in ("DELETE FROM crm_messages WHERE user_id=?", "DELETE FROM crm_notes WHERE user_id=?",
              "DELETE FROM crm_seen WHERE user_id=?", "DELETE FROM crm_topics WHERE user_id=?",
              "DELETE FROM crm_out WHERE user_id=?", "DELETE FROM funnel_users WHERE user_id=?"):
        db.execute(q, (uid,))
    db.execute("UPDATE crm_leads SET purged_at=?, summary=?, trashed_at=0 WHERE user_id=?",
               (int(time.time()), "; ".join(bits), uid))
    db.commit()


async def check_cleanup():
    """Раз в сутки: корзина и серые события, чтобы база не разрасталась."""
    r = db.execute("SELECT v FROM meta WHERE k='last_cleanup'").fetchone()
    if r and datetime.fromisoformat(r["v"]) > now() - timedelta(hours=20):
        return
    db.execute("INSERT OR REPLACE INTO meta VALUES('last_cleanup', ?)", (iso(now()),))
    db.commit()
    t = int(time.time())
    trashed = 0
    if CRM_STALE_DAYS > 0:  # давно ничего не делают и не дошли до конца теста: в корзину
        for row in db.execute(
                """SELECT u.user_id FROM users u
                   LEFT JOIN funnel_users f ON f.user_id=u.user_id
                   LEFT JOIN crm_leads l ON l.user_id=u.user_id
                   WHERE u.user_id!=? AND u.active=0 AND u.joined_at IS NOT NULL
                     AND (f.user_id IS NULL OR f.done=0)
                     AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.user_id=u.user_id)
                     AND (l.user_id IS NULL OR l.stage NOT IN ('entered','series','ready','won','lost'))
                     AND COALESCE((SELECT MAX(ts) FROM crm_messages m WHERE m.user_id=u.user_id),
                                  CAST(strftime('%s', u.joined_at) AS INTEGER), 0) < ?""",
                (ADMIN_ID, t - CRM_STALE_DAYS * 86400)).fetchall():
            db.execute("INSERT INTO crm_leads(user_id, stage, trashed_at, updated) VALUES(?,?,?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET stage='lost', trashed_at=excluded.trashed_at, "
                       "updated=excluded.updated", (row["user_id"], "lost", t, t))
            db.execute("INSERT INTO crm_messages(user_id, direction, text, ts) VALUES(?,?,?,?)",
                       (row["user_id"], "sys", f"🗑 Автокорзина: нет активности {CRM_STALE_DAYS} дн.", t))
            trashed += 1
        db.commit()
    purged = 0
    for row in db.execute(f"SELECT user_id FROM crm_leads WHERE stage='lost' AND trashed_at>0 AND trashed_at<? "
                          f"AND purged_at=0 AND user_id NOT IN ({CRM_BUYERS_SQL})",
                          (t - CRM_TRASH_DAYS * 86400,)).fetchall():
        try:
            await crm_purge(row["user_id"])
            purged += 1
        except Exception:
            logging.exception("не удалось очистить лида %s", row["user_id"])
    c1 = db.execute(f"DELETE FROM crm_messages WHERE direction='sys' AND ts<? AND {CRM_KEEP_SQL} "
                    f"AND user_id IN ({CRM_BUYERS_SQL})", (t - CRM_BUYER_SYS_DAYS * 86400,)).rowcount
    c2 = db.execute(f"DELETE FROM crm_messages WHERE direction='sys' AND ts<? AND {CRM_KEEP_SQL}",
                    (t - CRM_SYS_DAYS * 86400,)).rowcount if CRM_SYS_DAYS > 0 else 0
    db.commit()
    logging.info("Автоочистка CRM: в корзину %s, очищено %s, серых событий удалено %s", trashed, purged, c1 + c2)
    if purged or c1 + c2 >= 500 or trashed:
        await safe_send(ADMIN_ID, f"🧹 Автоочистка CRM: в корзину автоматически {trashed}, очищено лидов {purged}, "
                                  f"удалено серых событий {c1 + c2}.")
    v = db.execute("SELECT v FROM meta WHERE k='last_vacuum'").fetchone()
    if (purged or c1 + c2 >= 1000) and not (v and datetime.fromisoformat(v["v"]) > now() - timedelta(days=7)):
        try:  # возвращает место на диске, иначе файл базы не уменьшается
            db.execute("VACUUM")
            db.execute("INSERT OR REPLACE INTO meta VALUES('last_vacuum', ?)", (iso(now()),))
            db.commit()
        except Exception:
            logging.exception("VACUUM не удался")


# ---------- Сброс воронки и удаление человека (для тестов и уборки) ----------
def crm_display_name(uid: int) -> str:
    r = db.execute("SELECT name FROM crm_profiles WHERE user_id=?", (uid,)).fetchone()
    if r and r["name"]:
        return r["name"]
    u = db.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
    return f"@{u['username']}" if u and u["username"] else str(uid)


def crm_forget_state(uid: int):
    awaiting_help.discard(uid)
    awaiting_compose.pop(uid, None)
    awaiting_custom_days.pop(uid, None)


async def fx_begin(uid: int):
    """Первый шаг воронки: приветствие и выбор бесплатного урока."""
    await bot.send_message(uid, FX_GREETING)
    await bot.send_message(uid, FX_PICK_Q, reply_markup=markup(
        *[[btn(label, f"fx:l:{i}")] for i, (label, _) in enumerate(FX_LESSONS)]))


async def crm_reset_lead(uid: int, start: bool = False):
    """Воронка с нулевого шага. Подписка, платежи, переписка и заметки остаются.
    start=True (свой тест): сразу присылает первый шаг. Для чужого человека ему ничего не отправляется:
    когда он снова откроет ссылку воронки, начнёт сначала."""
    row = db.execute("SELECT source FROM funnel_users WHERE user_id=?", (uid,)).fetchone()
    if start:
        db.execute("INSERT OR IGNORE INTO users(user_id, joined_at) VALUES(?,?)", (uid, iso(now())))
    if row or start:
        funnel_reset(uid, (row["source"] if row and row["source"] else FUNNEL_PREFIX))
    db.execute("DELETE FROM crm_messages WHERE user_id=? AND direction='sys'", (uid,))
    db.execute("UPDATE crm_leads SET stage='auto', trashed_at=0, purged_at=0, summary=NULL WHERE user_id=?", (uid,))
    db.execute("INSERT INTO crm_messages(user_id, direction, text, ts) VALUES(?,?,?,?)",
               (uid, "sys", "🔄 Воронка сброшена на нулевой шаг", int(time.time())))
    db.commit()
    crm_forget_state(uid)
    if start:
        await fx_begin(uid)


async def crm_erase_lead(uid: int):
    """Полностью удаляет человека из базы и из воронки. Платежи (история денег) остаются.
    Возвращает текст ошибки или None. Если он напишет снова, начнёт с нулевого шага."""
    u = db.execute("SELECT blocked FROM users WHERE user_id=?", (uid,)).fetchone()
    if u and u["blocked"]:
        return "Он заблокирован: удаление сняло бы блокировку. Сначала разблокируйте (/unblock), потом удаляйте."
    sub = db.execute("SELECT expires_at, active, method_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if sub and (sub["active"] or sub["method_id"]):
        until = f" до {fmt(datetime.fromisoformat(sub['expires_at']))}" if sub["expires_at"] else ""
        return (f"У него есть действующая подписка{until} или сохранённая карта: удаление сотрёт запись о подписке и "
                "автоплатёж. Сначала завершите подписку.")
    t = db.execute("SELECT chat_id, thread_id FROM crm_topics WHERE user_id=?", (uid,)).fetchone()
    if t:
        try:
            await bot.delete_forum_topic(chat_id=t["chat_id"], message_thread_id=t["thread_id"])
        except Exception:
            logging.warning("не удалось удалить тему CRM лида %s", uid)
    for q in ("DELETE FROM crm_messages WHERE user_id=?", "DELETE FROM crm_notes WHERE user_id=?",
              "DELETE FROM crm_seen WHERE user_id=?", "DELETE FROM crm_topics WHERE user_id=?",
              "DELETE FROM crm_out WHERE user_id=?", "DELETE FROM crm_leads WHERE user_id=?",
              "DELETE FROM crm_profiles WHERE user_id=?", "DELETE FROM funnel_users WHERE user_id=?",
              "DELETE FROM support_links WHERE user_id=?", "DELETE FROM help WHERE user_id=?",
              "DELETE FROM users WHERE user_id=?"):
        db.execute(q, (uid,))
    db.commit()
    crm_forget_state(uid)
    return None


async def check_series():
    """Серия уроков: после теста бот сам присылает письма по расписанию FUNNEL_SERIES."""
    if not FUNNEL_SERIES:
        return
    t = int(time.time())
    for f in db.execute("SELECT user_id, done_at, series_idx FROM funnel_users "
                        "WHERE done=1 AND done_at>0 AND series_idx<?", (len(FUNNEL_SERIES),)).fetchall():
        uid, idx = f["user_id"], f["series_idx"]
        days, text = FUNNEL_SERIES[idx]
        if t < f["done_at"] + days * 86400:
            continue
        blocked = db.execute("SELECT blocked FROM users WHERE user_id=?", (uid,)).fetchone()
        if active_subscription(uid) or (blocked and blocked["blocked"]):  # купил или заблокирован: серию заканчиваем
            db.execute("UPDATE funnel_users SET series_idx=? WHERE user_id=?", (len(FUNNEL_SERIES), uid))
            db.commit()
            continue
        db.execute("UPDATE funnel_users SET series_idx=series_idx+1 WHERE user_id=?", (uid,))
        db.commit()
        await safe_send(uid, text)
        db.execute("INSERT INTO crm_messages(user_id, direction, text, ts) VALUES(?,?,?,?)",
                   (uid, "out", f"[урок серии {idx + 1}] " + text, int(time.time())))
        db.commit()
        await crm_notify(uid, f"📚 Отправлен урок серии {idx + 1} из {len(FUNNEL_SERIES)}.")


async def scheduler():
    while True:
        try:
            await check_daily_report()
            if not TEST_MODE:
                await check_pending()
                await check_pending_methods()
            await check_stuck()
            await check_funnel()
            await check_series()
            await check_cleanup()
            await check_backup()
            await check_subscriptions()
        except Exception:
            logging.exception("ошибка фоновой проверки")
        await asyncio.sleep(60)


# ====================== МИНИ-АПП CRM (веб-часть) ======================
# Страница miniapp.html лежит рядом с bot.py. Доступ только у ADMIN_ID: каждый запрос
# проверяется по подписи Telegram (initData), подделать её без токена бота нельзя.
MINIAPP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp.html")


def crm_auth(init_data: str) -> bool:
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(calc, got):
            return False
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return False
        return int(json.loads(pairs["user"])["id"]) == ADMIN_ID
    except Exception:
        return False


def crm_api(fn):
    async def wrapper(request: web.Request):
        if not crm_auth(request.headers.get("X-Init-Data", "")):
            return web.json_response({"error": "auth"}, status=401)
        try:
            data = await request.json()
        except Exception:
            data = {}
        try:
            return web.json_response(await fn(data))
        except Exception:
            logging.exception("ошибка API мини-аппа")
            return web.json_response({"error": "server"}, status=500)
    return wrapper


def _ts(s) -> int | None:
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def crm_is_active(u) -> bool:
    return bool(u and u["active"] and u["expires_at"] and datetime.fromisoformat(u["expires_at"]) > now())


def crm_seg(f):
    return funnel_segment(funnel_answers(f)) if f and f["done"] else None


def crm_auto_stage(active: bool, f) -> str:
    """Стадия по фактам: вход -> серия уроков (вместе с тестом) -> готов к закрытию -> купил."""
    if active:
        return "won"
    if not f or f["stage"] in ("group", "pick"):
        return "entered"
    if not f["done"]:
        return "series"
    if f["done_at"] and f["series_idx"] < len(FUNNEL_SERIES):
        return "series"
    return "ready"


def crm_build(uid, u, f, name, lead, last, unread) -> dict:
    active = crm_is_active(u)
    seg = crm_seg(f)
    username = u["username"] if u else None
    auto = crm_auto_stage(active, f)
    manual = lead["stage"] if lead and lead["stage"] in CRM_STAGES else None
    stage = "won" if active else (manual or auto)
    exp_ts = _ts(u["expires_at"]) if u and u["expires_at"] else None
    days_left = math.ceil((exp_ts - time.time()) / 86400) if (active and exp_ts) else None
    joined = _ts(u["joined_at"]) if u and u["joined_at"] else (f["created"] if f else None)
    trashed = lead["trashed_at"] if lead and stage == "lost" and lead["trashed_at"] else 0
    trash_left = max(0, math.ceil((trashed + CRM_TRASH_DAYS * 86400 - time.time()) / 86400)) if trashed else None
    return {
        "trash_left": trash_left, "purged": bool(lead and lead["purged_at"]),
        "summary": lead["summary"] if lead and lead["summary"] else None,
        "id": uid, "name": name or (f"@{username}" if username else str(uid)), "username": username,
        "seg": seg, "active": active, "expires": fmt(datetime.fromisoformat(u["expires_at"])) if exp_ts else None,
        "days_left": days_left, "stage": stage, "override": bool(manual and not active), "auto_stage": auto, "amount": lead["amount"] if lead and lead["amount"] else None,
        "blocked": bool(u and u["blocked"]), "joined": joined,
        "source": f["source"] if f else None,
        "src": funnel_source_id(f["source"]) if f else None,
        "src_detail": funnel_source_detail(f["source"]) if f else None,
        "funnel": ("done" if f and f["done"] else (f["stage"] if f else None)),
        "last": ({"dir": last["direction"], "text": last["text"], "ts": last["ts"]} if last else None),
        "unread": unread,
    }


async def crm_fill_names(ids):
    """Имён в базе нет, поэтому берём их у Telegram и кэшируем (не больше 15 за раз)."""
    have = {r["user_id"] for r in db.execute("SELECT user_id FROM crm_profiles")}
    missing = [i for i in ids if i not in have][:15]
    if not missing:
        return

    async def one(i):
        try:
            chat = await asyncio.wait_for(bot.get_chat(i), 5)
            name = chat.full_name or ""
        except Exception:
            name = ""  # не получилось: запомним пустое, чтобы не дёргать Telegram каждый раз
        db.execute("INSERT OR IGNORE INTO crm_profiles(user_id, name) VALUES(?,?)", (i, name))

    await asyncio.gather(*(one(i) for i in missing))
    db.commit()


def crm_profile(f):
    if not f:
        return None
    a = funnel_answers(f)
    if a.get("flow") == "v2":  # новая воронка: урок, 5 открытых вопросов, обратная связь
        qa = [{"q": "Бесплатный урок", "a": a.get("lesson") or "—"}]
        qa += [{"q": t, "a": a.get(f"q{i + 1}") or "—"} for i, t in enumerate(FX_Q_SHORT)]
        qa.append({"q": "Получил ценность в приложении", "a": {"yes": "Да", "no": "Нет"}.get(a.get("fb_value"), "—")})
        if a.get("fb_reason"):
            qa.append({"q": "Почему нет", "a": a["fb_reason"]})
        if a.get("fb_text"):
            qa.append({"q": "Что особенно понравилось", "a": a["fb_text"]})
        prof = {"qa": qa, "done": bool(f["done"]), "source": funnel_source_label(f["source"]),
                "stage": FUNNEL_STAGE_NAMES.get(f["stage"], f["stage"])}
        if f["done"]:
            prof.update(segment="🟢 Готов к работе", product="индивидуальное предложение, подарок и подписка «Круг»")
        return prof
    qa = [{"q": "Аудитория", "a": a.get("group") or "—"}]
    for q in FUNNEL_TEST:
        v = a.get(q["key"])
        if isinstance(v, list):
            v = "; ".join(v)
        qa.append({"q": q["title"], "a": v or "—"})
    prof = {"qa": qa, "done": bool(f["done"]), "source": funnel_source_label(f["source"]),
            "stage": FUNNEL_STAGE_NAMES.get(f["stage"], f["stage"])}
    if f["done"]:
        seg = funnel_segment(a)
        i = a.get("q1_i")
        prof.update(
            resume=funnel_resume(a, seg), problems=funnel_extra_problems(a),
            direction=FUNNEL_DIRECTIONS[i] if isinstance(i, int) and 0 <= i < len(FUNNEL_DIRECTIONS)
            else FUNNEL_DIRECTIONS[-1],
            product=FUNNEL_SEGMENTS[seg][2], segment=FUNNEL_SEGMENTS[seg][0])
    return prof


async def api_leads(data):
    ids = [r[0] for r in db.execute(
        "SELECT user_id FROM users WHERE user_id!=? UNION SELECT user_id FROM funnel_users WHERE user_id!=?",
        (ADMIN_ID, ADMIN_ID))]
    await crm_fill_names(ids)
    users = {r["user_id"]: r for r in db.execute("SELECT * FROM users")}
    funnel = {r["user_id"]: r for r in db.execute("SELECT * FROM funnel_users")}
    names = {r["user_id"]: r["name"] for r in db.execute("SELECT * FROM crm_profiles")}
    leads = {r["user_id"]: r for r in db.execute("SELECT * FROM crm_leads")}
    last = {r["user_id"]: r for r in db.execute(
        "SELECT user_id, direction, text, MAX(ts) AS ts FROM crm_messages WHERE direction!='sys' GROUP BY user_id")}
    unread = {r["user_id"]: r["c"] for r in db.execute(
        "SELECT m.user_id, COUNT(*) AS c FROM crm_messages m LEFT JOIN crm_seen s ON s.user_id=m.user_id "
        "WHERE m.direction='in' AND m.ts>COALESCE(s.ts,0) GROUP BY m.user_id")}
    out = [crm_build(i, users.get(i), funnel.get(i), names.get(i), leads.get(i), last.get(i), unread.get(i, 0))
           for i in ids]

    t = time.time()
    day0 = t - t % 86400
    open_leads = [l for l in out if not l["active"] and not l["blocked"] and l["stage"] in CRM_PROB_STAGE]
    potential_all = sum(l["amount"] or PRICE for l in open_leads)
    weighted = 0.0
    for l in open_leads:
        weighted += (l["amount"] or PRICE) * CRM_PROB_STAGE[l["stage"]]
    pay_total = db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    pay_30 = db.execute("SELECT COUNT(*) FROM payments WHERE created>=?",
                        (iso(now() - timedelta(days=30)),)).fetchone()[0]
    stats = {
        "users": len(out), "funnel": sum(1 for l in out if l["funnel"]),
        "active": sum(1 for l in out if l["active"]),
        "expiring": sum(1 for l in out if l["active"] and l["days_left"] is not None and l["days_left"] <= 7),
        "new_today": sum(1 for l in out if l["joined"] and l["joined"] >= day0),
        "new_week": sum(1 for l in out if l["joined"] and l["joined"] >= t - 7 * 86400),
        "open": len(open_leads), "potential_all": potential_all, "potential": weighted,
        "revenue": pay_total * PRICE, "revenue_30": pay_30 * PRICE, "payments": pay_total,
        "mrr": sum(1 for l in out if l["active"]) * PRICE * 30 / PERIOD_DAYS,
        "seg": {k: sum(1 for l in out if l["seg"] == k) for k in ("ready", "interested", "cold")},
        "src": {k: {"leads": sum(1 for l in out if l["src"] == k),
                    "active": sum(1 for l in out if l["src"] == k and l["active"]),
                    "ready": sum(1 for l in out if l["src"] == k and l["stage"] == "ready")}
                for k in FUNNEL_SOURCES},
        "stages": {k: sum(1 for l in out if l["stage"] == k) for k in CRM_STAGES},
    }
    return {"leads": out, "stats": stats, "price": PRICE, "trash_days": CRM_TRASH_DAYS,
            "sources": [{"id": k, "icon": v[0], "label": v[1]} for k, v in FUNNEL_SOURCES.items()]}


async def api_lead(data):
    uid = int(data["id"])
    await crm_fill_names([uid])
    u = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    f = funnel_get(uid)
    nm = db.execute("SELECT name FROM crm_profiles WHERE user_id=?", (uid,)).fetchone()
    lead = db.execute("SELECT * FROM crm_leads WHERE user_id=?", (uid,)).fetchone()
    last = db.execute("SELECT direction, text, ts FROM crm_messages WHERE user_id=? AND direction!='sys' ORDER BY id DESC LIMIT 1",
                      (uid,)).fetchone()
    msgs = [{"id": r["id"], "direction": r["direction"], "text": r["text"], "ts": r["ts"],
             "deleted": bool(r["deleted"]), "can_del": bool(r["direction"] == "out" and r["tg_id"] and not r["deleted"])}
            for r in reversed(db.execute(
                "SELECT id, direction, text, ts, tg_id, deleted FROM crm_messages WHERE user_id=? "
                "ORDER BY id DESC LIMIT 400", (uid,)).fetchall())]
    notes = [dict(r) for r in db.execute(
        "SELECT id, text, ts FROM crm_notes WHERE user_id=? ORDER BY id DESC", (uid,))]
    db.execute("INSERT OR REPLACE INTO crm_seen(user_id, ts) VALUES(?,?)", (uid, int(time.time())))
    db.commit()
    res = {"lead": crm_build(uid, u, f, nm["name"] if nm else None, lead, last, 0), "messages": msgs, "notes": notes}
    if not data.get("light"):
        res["profile"] = crm_profile(f)
    return res


async def api_send(data):
    uid, text = int(data["id"]), (data.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "Пустое сообщение"}
    try:
        sent = await bot.send_message(uid, html.escape(text))
    except Exception:
        logging.exception("мини-апп: не удалось отправить сообщение %s", uid)
        return {"ok": False, "error": "Не доставлено: пользователь мог заблокировать бота."}
    db.execute("INSERT INTO crm_messages(user_id, direction, text, ts, tg_id) VALUES(?,?,?,?,?)",
               (uid, "out", text, int(time.time()), sent.message_id))
    db.commit()
    await crm_notify(uid, "📱 Из CRM: " + html.escape(text))  # копия в тему, если CRM в топиках включена
    return {"ok": True}


async def api_pay(data):
    uid = int(data["id"])
    sub = active_subscription(uid)
    if sub:
        return {"ok": False, "error": f"Подписка уже активна до {fmt(datetime.fromisoformat(sub['expires_at']))}."}
    try:
        sent = await bot.send_message(uid, T_PAY_REQUEST, reply_markup=KB_START)
    except Exception:
        logging.exception("мини-апп: не удалось отправить форму оплаты %s", uid)
        return {"ok": False, "error": "Не доставлено: пользователь мог заблокировать бота."}
    db.execute("INSERT INTO crm_messages(user_id, direction, text, ts, tg_id) VALUES(?,?,?,?,?)",
               (uid, "out", "[форма оплаты]", int(time.time()), sent.message_id))
    db.execute("UPDATE users SET pay_started_at=? WHERE user_id=? AND active=0", (iso(now()), uid))
    db.commit()
    return {"ok": True}


async def api_del(data):
    """Удалить своё сообщение у человека в Telegram (Telegram разрешает в течение 48 часов)."""
    uid, mid = int(data["id"]), int(data["msg_id"])
    r = db.execute("SELECT * FROM crm_messages WHERE id=? AND user_id=? AND direction='out'", (mid, uid)).fetchone()
    if not r:
        return {"ok": False, "error": "Сообщение не найдено."}
    if r["deleted"]:
        return {"ok": True}
    if not r["tg_id"]:
        return {"ok": False, "error": "Это сообщение отправлено до обновления: у него нет номера в Telegram, удалить нельзя."}
    try:
        await bot.delete_message(uid, r["tg_id"])
    except Exception:
        logging.exception("мини-апп: не удалось удалить сообщение %s у %s", r["tg_id"], uid)
        return {"ok": False, "error": "Telegram не дал удалить: прошло больше 48 часов или сообщение уже удалено."}
    db.execute("UPDATE crm_messages SET deleted=1 WHERE id=?", (mid,))
    db.commit()
    return {"ok": True}


async def api_reset(data):
    uid = ADMIN_ID if data.get("self") else int(data["id"])
    await crm_reset_lead(uid, start=(uid == ADMIN_ID))
    return {"ok": True, "started": uid == ADMIN_ID}


async def api_erase(data):
    uid = ADMIN_ID if data.get("self") else int(data["id"])
    err = await crm_erase_lead(uid)
    return {"ok": not err, "error": err}


async def api_note(data):
    uid, text = int(data["id"]), (data.get("text") or "").strip()
    if text:
        db.execute("INSERT INTO crm_notes(user_id, text, ts) VALUES(?,?,?)", (uid, text[:4000], int(time.time())))
        db.commit()
    return {"ok": True}


async def api_note_del(data):
    db.execute("DELETE FROM crm_notes WHERE id=?", (int(data["note_id"]),))
    db.commit()
    return {"ok": True}


async def api_stage(data):
    uid, stage = int(data["id"]), data.get("stage")
    if stage != "auto" and stage not in CRM_STAGES:
        return {"ok": False, "error": "Неизвестная стадия"}
    try:
        amount = float(data["amount"]) if data.get("amount") not in (None, "") else None
    except (TypeError, ValueError):
        amount = None
    prev = db.execute("SELECT stage, trashed_at FROM crm_leads WHERE user_id=?", (uid,)).fetchone()
    trashed = 0  # «Не купил / закрыт» = корзина: через CRM_TRASH_DAYS дней данные очистятся
    if stage == "lost":
        trashed = prev["trashed_at"] if prev and prev["stage"] == "lost" and prev["trashed_at"] else int(time.time())
    db.execute("INSERT INTO crm_leads(user_id, stage, amount, updated, trashed_at) VALUES(?,?,?,?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET stage=excluded.stage, amount=excluded.amount, "
               "updated=excluded.updated, trashed_at=excluded.trashed_at",
               (uid, stage, amount, int(time.time()), trashed))
    db.commit()
    return {"ok": True}


async def api_settings(data):
    if data.get("set") and CRM_CHAT_ID:
        crm_topics_set(bool(data.get("topics")))
    return {"topics_available": bool(CRM_CHAT_ID), "topics_on": crm_topics_on(),
            "price": PRICE, "period_days": PERIOD_DAYS, "trash_days": CRM_TRASH_DAYS,
            "buyer_sys_days": CRM_BUYER_SYS_DAYS, "sys_days": CRM_SYS_DAYS, "stale_days": CRM_STALE_DAYS}


async def crm_index(request: web.Request):
    try:
        with open(MINIAPP_FILE, encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        return web.Response(status=404, text="miniapp.html не найден рядом с bot.py")
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store"})


async def crm_health(request: web.Request):
    return web.Response(text="ok")


def crm_web_routes(app: web.Application):
    app.router.add_get("/", crm_index)
    app.router.add_get("/app", crm_index)
    app.router.add_get("/health", crm_health)
    for name, fn in (("leads", api_leads), ("lead", api_lead), ("send", api_send), ("pay", api_pay), ("del", api_del), ("reset", api_reset), ("erase", api_erase),
                     ("note", api_note), ("note_del", api_note_del), ("stage", api_stage),
                     ("settings", api_settings)):
        app.router.add_post(f"/api/{name}", crm_api(fn))


# ---------- Вебхук ЮKassa (необязателен) ----------
async def yk_hook(request: web.Request):
    try:
        data = await request.json()
        if data.get("event") == "payment.succeeded":
            payment_id = data["object"]["id"]
            pending = db.execute(
                "SELECT store_mode FROM pending WHERE id=?", (payment_id,)
            ).fetchone()
            if not pending:
                return web.Response(text="ok")
            mode = pending["store_mode"] or get_store_mode()
            p = await asyncio.to_thread(yookassa_call, mode, Payment.find_one, payment_id)
            await handle_payment(p)
        elif data.get("event") == "payment_method.active":
            await handle_payment_method(data["object"]["id"])
    except Exception:
        logging.exception("ошибка вебхука")
    return web.Response(text="ok")


async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать / оформить подписку"),
        BotCommand(command="status", description="Статус подписки"),
        BotCommand(command="help", description="Связаться с нами"),
        BotCommand(command="cancel", description="Отключить автопродление"),
    ])
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать / оформить подписку"),
        BotCommand(command="status", description="Статус подписки"),
        BotCommand(command="help", description="Связаться с нами"),
        BotCommand(command="cancel", description="Отключить автопродление"),
        BotCommand(command="users", description="Управление пользователями"),
        BotCommand(command="menu", description="Админ-панель"),
        BotCommand(command="grant", description="Выдать подписку"),
        BotCommand(command="del", description="Сбросить подписку"),
        BotCommand(command="stats", description="Статистика бота"),
        BotCommand(command="backup", description="Копия базы данных"),
        BotCommand(command="all", description="Сообщение всем пользователям"),
        BotCommand(command="all_sub", description="Сообщение активным подписчикам"),
        BotCommand(command="block", description="Заблокировать пользователя"),
        BotCommand(command="unblock", description="Снять блокировку пользователя"),
        BotCommand(command="app", description="CRM-мини-апп"),
        BotCommand(command="links", description="Ссылки на воронку по источникам"),
        BotCommand(command="reset", description="Сбросить воронку (себе, id или @ник)"),
        BotCommand(command="erase", description="Удалить человека из базы (себя, id или @ник)"),
        BotCommand(command="crm_topics", description="Вкл/выкл CRM в топиках"),
    ], scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    if MINIAPP_ENABLED and MINIAPP_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(chat_id=ADMIN_ID, menu_button=MenuButtonWebApp(
                text="CRM", web_app=WebAppInfo(url=MINIAPP_URL + "/app")))
        except Exception:
            logging.exception("не удалось поставить кнопку CRM в меню бота")
    if WEB_PORT:
        app = web.Application()
        app.router.add_post("/yookassa", yk_hook)
        if MINIAPP_ENABLED:
            crm_web_routes(app)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
        logging.info("Веб-сервер запущен на порту %s", WEB_PORT)
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
