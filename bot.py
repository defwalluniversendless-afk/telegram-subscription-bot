"""Telegram-бот: платная подписка на закрытый канал с лекциями (ЮKassa).

Воронка: /start или /join -> кнопка -> карточка товара + оферта -> оплата -> доступ в канал
-> напоминания -> (автосписание) -> закрытие доступа. /help — связь с владельцем.
Все тексты бота лежат в блоке TEXTS ниже: правьте формулировки там.
"""
import asyncio
import html
import logging
import os
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiohttp import web
from dotenv import load_dotenv
from yookassa import Configuration, Payment

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
T_PAID_NEW = ("✅ Оплата получена. Подписка до {date}.\n\n"
              "Ваша ссылка в канал (одноразовая, действует сутки):\n{link}")
T_PAID_EXT = "✅ Подписка продлена до {date}."
T_PAID_NOLINK = "✅ Оплата получена, но ссылку выдать не удалось. Нажмите /help, и мы всё решим."
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
  pay_started_at TEXT, stuck_reminded INTEGER DEFAULT 0, joined_at TEXT);
CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, user_id INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, user_id INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS help(msg_id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def migrate():
    """Создаёт таблицы и дополняет старую базу новыми колонками."""
    db.executescript(SCHEMA)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    for name, ddl in (("pay_started_at", "TEXT"), ("stuck_reminded", "INTEGER DEFAULT 0"),
                      ("joined_at", "TEXT")):
        if name not in cols:
            db.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
    db.commit()


migrate()
logging.info("База данных: %s", os.path.abspath(DB_PATH))

awaiting_help: set[int] = set()  # кто сейчас пишет вопрос администратору


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def fmt(d: datetime) -> str:
    return d.strftime("%d.%m.%Y")


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
def _create_payment(user_id: int, method_id: str | None = None):
    params = {
        "amount": {"value": f"{PRICE:.2f}", "currency": "RUB"},
        "capture": True,
        "description": "Ежемесячная подписка на закрытый канал с лекциями",
        "metadata": {"user_id": str(user_id)},
    }
    if method_id:  # автосписание с сохранённого способа оплаты
        params["payment_method_id"] = method_id
    else:  # ← ЗДЕСЬ создаётся платёж; когда ЮKassa подключат, добавим чек/доп. параметры
        params["confirmation"] = {"type": "redirect", "return_url": RETURN_URL}
        params["save_payment_method"] = AUTOCHARGE
    return Payment.create(params, str(uuid.uuid4()))


async def new_payment(user_id: int, method_id: str | None = None):
    p = await asyncio.to_thread(_create_payment, user_id, method_id)
    db.execute("INSERT OR IGNORE INTO pending VALUES(?,?,?)", (p.id, user_id, iso(now())))
    db.commit()
    return p


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
    await extend(uid, PERIOD_DAYS, method)


async def extend(uid: int, days: float, method_id: str | None = None):
    row = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    base, was_active = now(), False
    if row and row["expires_at"] and row["active"]:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp > base:
            base, was_active = exp, True
    new_exp = base + timedelta(days=days)
    db.execute("""INSERT INTO users(user_id, expires_at, active, method_id) VALUES(?,?,1,?)
                  ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, active=1,
                  method_id=COALESCE(excluded.method_id, method_id),
                  pay_started_at=NULL, stuck_reminded=0""", (uid, iso(new_exp), method_id))
    db.commit()
    if was_active:
        await safe_send(uid, T_PAID_EXT.format(date=fmt(new_exp)))
        return
    try:
        link = await bot.create_chat_invite_link(
            CHANNEL_ID, member_limit=1, expire_date=int(time.time()) + 86400)
        await safe_send(uid, T_PAID_NEW.format(date=fmt(new_exp), link=link.invite_link))
    except Exception:
        logging.exception("не удалось создать ссылку")
        await safe_send(uid, T_PAID_NOLINK, KB_HELP)
        await safe_send(ADMIN_ID, f"⚠️ Не удалось выдать ссылку пользователю <code>{uid}</code>. Бот — админ канала?")


async def expire(uid: int) -> bool:
    removed = False
    try:
        await bot.ban_chat_member(CHANNEL_ID, uid)
        await bot.unban_chat_member(CHANNEL_ID, uid, only_if_banned=True)  # убрать, но не банить навсегда
        removed = True
    except Exception:
        logging.exception("не удалось удалить %s из канала", uid)
    db.execute("UPDATE users SET active=0 WHERE user_id=?", (uid,))
    db.commit()
    await safe_send(uid, T_EXPIRED, KB_RENEW)
    return removed


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
    u = active_subscription(uid)
    if u:
        await cb.message.answer(T_ACTIVE.format(date=fmt(datetime.fromisoformat(u["expires_at"]))),
                                reply_markup=KB_HELP)
        await cb.answer()
        return
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
    u = active_subscription(uid)
    if u:
        await cb.message.answer(T_ACTIVE.format(date=fmt(datetime.fromisoformat(u["expires_at"]))),
                                reply_markup=KB_HELP)
        return
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
    """Ответ админа реплаем на пересланный вопрос уходит автору вопроса."""
    reply = m.reply_to_message
    row = db.execute("SELECT user_id FROM help WHERE msg_id=?", (reply.message_id,)).fetchone()
    uid = row["user_id"] if row else None
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
        await m.copy_to(uid)
        await m.answer("✅ Отправлено.")
    except Exception:
        await m.answer("Не удалось отправить: пользователь мог заблокировать бота.")


# ---------- Остальные команды ----------
@dp.message(Command("status"))
async def status(m: Message):
    ensure_user(m.from_user.id, m.from_user.username)
    u = active_subscription(m.from_user.id)
    if u:
        await m.answer(T_ACTIVE.format(date=fmt(datetime.fromisoformat(u["expires_at"]))), reply_markup=KB_HELP)
    else:
        await m.answer(T_STATUS_NO, reply_markup=KB_START)


@dp.message(Command("cancel"))
async def cancel(m: Message):
    ensure_user(m.from_user.id, m.from_user.username)
    db.execute("UPDATE users SET autopay=0 WHERE user_id=?", (m.from_user.id,))
    db.commit()
    await m.answer(T_CANCEL)


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
    removed = await expire(uid)
    db.execute(
        "UPDATE users SET active=0, expires_at=NULL, method_id=NULL, "
        "last_remind=NULL, last_charge=NULL WHERE user_id=?",
        (uid,),
    )
    db.commit()
    if removed:
        await m.answer(f"Подписка пользователя <code>{uid}</code> сброшена, доступ в канал удалён.")
    else:
        await m.answer(
            f"Подписка пользователя <code>{uid}</code> сброшена, но удалить его из канала не удалось. "
            "Проверьте права бота и логи."
        )


@dp.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    n = db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    s = db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    stuck = db.execute("SELECT COUNT(*) FROM users WHERE active=0 AND pay_started_at IS NOT NULL").fetchone()[0]
    await m.answer(f"Активных подписчиков: {n}\nВсего оплат: {s}\nДошли до оплаты, но не оплатили: {stuck}\nФайл базы: {os.path.abspath(DB_PATH)}")


@dp.message()
async def any_message(m: Message):
    """Всё остальное: либо вопрос администратору (после /help), либо подсказка."""
    uid = m.from_user.id
    ensure_user(uid, m.from_user.username)
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
    for r in db.execute("SELECT id, created FROM pending").fetchall():
        try:
            p = await asyncio.to_thread(Payment.find_one, r["id"])
        except Exception:
            continue
        if p.status == "succeeded":
            await handle_payment(p)
        elif p.status == "canceled" or datetime.fromisoformat(r["created"]) < now() - timedelta(days=2):
            db.execute("DELETE FROM pending WHERE id=?", (r["id"],))
            db.commit()


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
        if auto and left <= timedelta(days=1) and u["last_charge"] != today:
            db.execute("UPDATE users SET last_charge=? WHERE user_id=?", (today, uid))
            db.commit()
            try:
                await new_payment(uid, u["method_id"])
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
