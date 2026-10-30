"""Telegram-бот: платная подписка на закрытый канал с лекциями (ЮKassa).

Воронка: /start или /join -> кнопка -> карточка товара + оферта -> оплата -> доступ в канал
-> напоминания -> (автосписание) -> закрытие доступа. /help — связь с владельцем.
Все тексты бота лежат в блоке TEXTS ниже: правьте формулировки там.
"""
import asyncio
import html
import logging
import math
import os
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           BotCommandScopeChat, InlineKeyboardMarkup, Message)
from aiohttp import web
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
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "0"))    # 0 = вебхук выключен, только опрос
RETURN_URL = os.getenv("RETURN_URL", "https://t.me/")
# Папка с базой. На Bothost постоянное хранилище лежит в /app/data (тариф Basic и выше).
DATA_DIR = os.getenv("DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot.db")
BACKUP_HOURS = float(os.getenv("BACKUP_HOURS", "24"))  # как часто бот присылает вам копию базы
if not TEST_MODE:
    Configuration.configure(os.environ["YOOKASSA_SHOP_ID"], os.environ["YOOKASSA_SECRET"])

# ============================== TEXTS ==============================
# Разметка: <b>жирный</b>, <i>курсив</i>. Подстановки в {фигурных скобках} не трогайте.
T_WELCOME = ("Привет, я бот Круга Ильи Хабирова. Я выдаю доступ к закрытому каналу с лекциями.\n"
             "Оплатить можно картой.\n\nЧтобы оплатить, нажмите на кнопку 👇")
T_PRODUCT = ("📚 <b>Ежемесячная подписка на закрытый канал с лекциями</b>\n"
             "Цена: <b>{price} ₽</b> в месяц\n\n"
             "Перед оплатой ознакомьтесь с {offer}.")
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
T_EXPIRED = "⏰ Подписка закончилась, доступ к каналу закрыт. Продлить:"
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
  blocked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, user_id INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, user_id INTEGER, created TEXT,
  is_auto INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pending_methods(
  id TEXT PRIMARY KEY, user_id INTEGER, created TEXT, confirmation_url TEXT);
CREATE TABLE IF NOT EXISTS help(msg_id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE IF NOT EXISTS support_links(
  chat_id INTEGER, message_id INTEGER, user_id INTEGER,
  PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def migrate():
    """Создаёт таблицы и дополняет старую базу новыми колонками."""
    db.executescript(SCHEMA)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    for name, ddl in (("pay_started_at", "TEXT"), ("stuck_reminded", "INTEGER DEFAULT 0"),
                      ("joined_at", "TEXT"), ("blocked", "INTEGER DEFAULT 0")):
        if name not in cols:
            db.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
    pending_cols = {r["name"] for r in db.execute("PRAGMA table_info(pending)")}
    if "is_auto" not in pending_cols:
        db.execute("ALTER TABLE pending ADD COLUMN is_auto INTEGER DEFAULT 0")
    db.commit()


migrate()
logging.info("База данных: %s", os.path.abspath(DB_PATH))


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
    if not AUTOCHARGE:
        return "Способ оплаты сохранён, но автосписание выключено в настройках бота."
    if row["autopay"]:
        return "Автосписание включено."
    return "Способ оплаты сохранён, автосписание отключено."


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
                    replace_method: bool = False):
    if save_payment_method is None:
        save_payment_method = AUTOCHARGE
    params = {
        "amount": {"value": f"{PRICE:.2f}", "currency": "RUB"},
        "capture": True,
        "description": "Ежемесячная подписка на закрытый канал с лекциями",
        "metadata": {"user_id": str(user_id), "replace_method": str(replace_method).lower()},
    }
    if method_id:  # автосписание с сохранённого способа оплаты
        params["payment_method_id"] = method_id
    else:
        params["confirmation"] = {"type": "redirect", "return_url": RETURN_URL}
        params["save_payment_method"] = save_payment_method
    return Payment.create(params, str(uuid.uuid4()))


async def new_payment(user_id: int, method_id: str | None = None,
                      save_payment_method: bool | None = None, replace_method: bool = False):
    is_auto = method_id is not None
    p = await asyncio.to_thread(
        _create_payment, user_id, method_id, save_payment_method, replace_method
    )
    db.execute("INSERT OR IGNORE INTO pending(id, user_id, created, is_auto) VALUES(?,?,?,?)",
               (p.id, user_id, iso(now()), int(is_auto)))
    db.commit()
    return p


def _create_payment_method(user_id: int):
    params = {
        "type": "bank_card",
        "confirmation": {"type": "redirect", "return_url": RETURN_URL},
        "metadata": {"user_id": str(user_id)},
    }
    return PaymentMethod.create(params, str(uuid.uuid4()))


async def new_payment_method(user_id: int):
    pending = db.execute(
        "SELECT confirmation_url FROM pending_methods WHERE user_id=? ORDER BY created DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if pending:
        return pending["confirmation_url"]
    method = await asyncio.to_thread(_create_payment_method, user_id)
    confirmation_url = method.confirmation.confirmation_url
    db.execute(
        "INSERT INTO pending_methods(id,user_id,created,confirmation_url) VALUES(?,?,?,?)",
        (method.id, user_id, iso(now()), confirmation_url),
    )
    db.commit()
    return confirmation_url


async def handle_payment_method(method_id: str):
    method = await asyncio.to_thread(PaymentMethod.find_one, method_id)
    pending = db.execute("SELECT user_id FROM pending_methods WHERE id=?", (method_id,)).fetchone()
    if not pending:
        return
    uid = pending["user_id"]
    if method.status == "active" and method.saved:
        db.execute(
            "UPDATE users SET method_id=?, last_charge=NULL WHERE user_id=?",
            (method.id, uid),
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
    await extend(uid, PERIOD_DAYS, method, replace_method=bool(p.metadata.get("replace_method")))


async def extend(uid: int, days: float, method_id: str | None = None,
                 replace_method: bool = False):
    row = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    base, was_active = now(), False
    if row and row["expires_at"] and row["active"]:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp > base:
            base, was_active = exp, True
    new_exp = base + timedelta(days=days)
    db.execute("""INSERT INTO users(user_id, expires_at, active, method_id) VALUES(?,?,1,?)
                  ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, active=1,
                  method_id=CASE WHEN ? THEN excluded.method_id
                                 ELSE COALESCE(excluded.method_id, users.method_id) END,
                  autopay=CASE WHEN ? AND excluded.method_id IS NULL THEN 0 ELSE users.autopay END,
                  pay_started_at=NULL, stuck_reminded=0""",
               (uid, iso(new_exp), method_id, int(replace_method), int(replace_method)))
    db.commit()
    status = autopay_status(
        db.execute("SELECT autopay, method_id FROM users WHERE user_id=?", (uid,)).fetchone()
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
    db.execute("UPDATE users SET active=0 WHERE user_id=?", (uid,))
    db.commit()
    if notify_user:
        await safe_send(uid, T_EXPIRED, KB_RENEW)
    return removed


async def reset_subscription(uid: int, notify_user: bool = True) -> bool:
    removed = await expire(uid, notify_user)
    db.execute(
        "UPDATE users SET active=0, expires_at=NULL, method_id=NULL, autopay=0, "
        "last_remind=NULL, last_charge=NULL, pay_started_at=NULL, stuck_reminded=0 "
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
        "UPDATE users SET expires_at=?, active=1, last_remind=NULL, last_charge=NULL, "
        "pay_started_at=NULL, stuck_reminded=0 WHERE user_id=?",
        (iso(new_exp), uid),
    )
    db.commit()
    status = autopay_status(
        db.execute("SELECT autopay, method_id FROM users WHERE user_id=?", (uid,)).fetchone()
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
async def start(m: Message):
    ensure_user(m.from_user.id, m.from_user.username)
    u = db.execute("SELECT * FROM users WHERE user_id=?", (m.from_user.id,)).fetchone()
    if active_subscription(m.from_user.id):
        await m.answer(T_ACTIVE.format(date=fmt(datetime.fromisoformat(u["expires_at"]))), reply_markup=KB_HELP)
        return
    await m.answer(T_WELCOME, reply_markup=KB_START)


@dp.callback_query(F.data == "buy")
async def buy(cb: CallbackQuery):
    uid = cb.from_user.id
    ensure_user(uid, cb.from_user.username)
    # человек дошёл до оплаты: запускаем таймер «застрял на оплате»
    db.execute("UPDATE users SET pay_started_at=?, stuck_reminded=0 WHERE user_id=? AND active=0", (iso(now()), uid))
    db.commit()
    offer = T_OFFER_LINK.format(url=html.escape(OFFER_URL, quote=True)) if OFFER_URL else T_OFFER_SOON
    text = T_PRODUCT.format(price=f"{PRICE:.0f}", offer=offer) + (T_AUTO_NOTE if AUTOCHARGE else "")
    await cb.message.answer(text, reply_markup=KB_PAY())
    await cb.answer()


@dp.callback_query(F.data == "pay")
async def pay(cb: CallbackQuery):
    uid = cb.from_user.id
    ensure_user(uid, cb.from_user.username)
    await cb.answer()
    if TEST_MODE:  # ЮKassa ещё не подключена: «оплатить» работает только у админа (для проверки)
        if uid == ADMIN_ID:
            await extend(uid, PERIOD_DAYS)
        else:
            await cb.message.answer(T_NOT_READY, reply_markup=KB_HELP)
        return
    p = await new_payment(uid)
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


def subscription_status(uid: int):
    row = db.execute(
        "SELECT expires_at, method_id, autopay FROM users WHERE user_id=?", (uid,)
    ).fetchone()
    if row and active_subscription(uid):
        subscription = f"Подписка активна до <b>{fmt(datetime.fromisoformat(row['expires_at']))}</b>."
    else:
        subscription = "Активной подписки нет."
    has_method = bool(row and row["method_id"])
    if not AUTOCHARGE:
        auto_status = "Автосписание недоступно: его нужно включить в настройках бота."
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
        if not row or not row["method_id"]:
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
            "UPDATE users SET method_id=NULL, autopay=0, last_charge=NULL WHERE user_id=?",
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
            payment = await new_payment(uid, save_payment_method=True, replace_method=True)
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
        "SELECT user_id, username, expires_at, active, blocked, autopay, method_id FROM users "
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
        "SELECT user_id, username, expires_at, active, blocked, autopay, method_id "
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
        "SELECT COUNT(*) FROM users WHERE active=1 AND autopay=1 AND method_id IS NOT NULL"
    ).fetchone()[0]
    pending_auto = db.execute("SELECT COUNT(*) FROM pending WHERE is_auto=1").fetchone()[0]
    stuck = db.execute("SELECT COUNT(*) FROM users WHERE active=0 AND pay_started_at IS NOT NULL").fetchone()[0]
    await m.answer(
        f"Активных подписчиков: {n}\nВсего оплат: {s}\n"
        f"Автосписание включено в настройках: {'да' if AUTOCHARGE else 'нет'}\n"
        f"Активных подписок с сохранённым способом оплаты: {recurring}\n"
        f"Ожидающих автоплатежей: {pending_auto}\n"
        f"Дошли до оплаты, но не оплатили: {stuck}\n"
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
            await m.answer("Ваш ответ передан. Мы ответим вам сюда.")
            return
    if uid in awaiting_help:
        awaiting_help.discard(uid)
        who = html.escape(m.from_user.full_name) + (f" (@{m.from_user.username})" if m.from_user.username else "")
        head = await bot.send_message(
            ADMIN_ID, f"💬 Вопрос от {who}, id <code>{uid}</code>.\nОтветьте реплаем на это сообщение или на следующее.")
        copy = await m.copy_to(ADMIN_ID)
        db.executemany("INSERT OR REPLACE INTO help VALUES(?,?)", [(head.message_id, uid), (copy.message_id, uid)])
        db.commit()
        await m.answer(T_HELP_SENT)
    else:
        await m.answer(T_UNKNOWN, reply_markup=KB_HELP)


# ---------- Фоновая проверка ----------
async def check_pending():
    for r in db.execute("SELECT id, created, user_id, is_auto FROM pending").fetchall():
        try:
            p = await asyncio.to_thread(Payment.find_one, r["id"])
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
    t = now()
    today = t.date().isoformat()
    for u in db.execute("SELECT * FROM users WHERE active=1 AND expires_at IS NOT NULL").fetchall():
        uid, left = u["user_id"], datetime.fromisoformat(u["expires_at"]) - t
        if left <= timedelta(0):
            await expire(uid)
            continue
        auto = bool(AUTOCHARGE and u["autopay"] and u["method_id"])
        has_pending_auto = db.execute(
            "SELECT 1 FROM pending WHERE user_id=? AND is_auto=1 LIMIT 1", (uid,)
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


async def scheduler():
    while True:
        try:
            await check_daily_report()
            if not TEST_MODE:
                await check_pending()
                await check_pending_methods()
            await check_stuck()
            await check_backup()
            await check_subscriptions()
        except Exception:
            logging.exception("ошибка фоновой проверки")
        await asyncio.sleep(60)


# ---------- Вебхук ЮKassa (необязателен) ----------
async def yk_hook(request: web.Request):
    try:
        data = await request.json()
        if data.get("event") == "payment.succeeded":
            p = await asyncio.to_thread(Payment.find_one, data["object"]["id"])  # перепроверяем у ЮKassa
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
        BotCommand(command="grant", description="Выдать подписку"),
        BotCommand(command="del", description="Сбросить подписку"),
        BotCommand(command="stats", description="Статистика бота"),
        BotCommand(command="backup", description="Копия базы данных"),
        BotCommand(command="all", description="Сообщение всем пользователям"),
        BotCommand(command="all_sub", description="Сообщение активным подписчикам"),
        BotCommand(command="block", description="Заблокировать пользователя"),
        BotCommand(command="unblock", description="Снять блокировку пользователя"),
    ], scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    if WEBHOOK_PORT:
        app = web.Application()
        app.router.add_post("/yookassa", yk_hook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT).start()
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
