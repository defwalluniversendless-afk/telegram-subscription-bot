"""Telegram-бот: платная подписка на закрытый канал (ЮKassa, СБП/карты).

Работает так:
  оплата -> доступ (одноразовая ссылка-приглашение) -> напоминания -> (автосписание) -> закрытие доступа.
Оплаты подтверждаются двумя способами сразу: вебхуком ЮKassa (если настроен) и опросом статуса каждую минуту.
"""
import asyncio
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiohttp import web
from dotenv import load_dotenv
from yookassa import Configuration, Payment

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])      # вида -1001234567890
ADMIN_ID = int(os.environ["ADMIN_ID"])          # ваш Telegram ID
PRICE = float(os.getenv("PRICE", "990"))        # цена за период, руб
PERIOD_DAYS = float(os.getenv("PERIOD_DAYS", "30"))
REMIND_DAYS = float(os.getenv("REMIND_DAYS", "3"))
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"  # 1 = «оплата» без ЮKassa, для проверки бота
AUTOCHARGE = os.getenv("AUTOCHARGE", "0") == "1"  # автосписание (нужно включить у ЮKassa)
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "0"))  # 0 = вебхук выключен, только опрос
RETURN_URL = os.getenv("RETURN_URL", "https://t.me/")
if not TEST_MODE:
    Configuration.configure(os.environ["YOOKASSA_SHOP_ID"], os.environ["YOOKASSA_SECRET"])

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

db = sqlite3.connect("bot.db")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, username TEXT, expires_at TEXT, method_id TEXT,
  autopay INTEGER DEFAULT 1, last_remind TEXT, last_charge TEXT, active INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, user_id INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, user_id INTEGER, created TEXT);
""")


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def fmt(d: datetime) -> str:
    return d.strftime("%d.%m.%Y")


def pay_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"💳 Оплатить {PRICE:.0f} ₽", callback_data="pay")]])


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
        "description": f"Доступ к закрытому каналу, {PERIOD_DAYS:g} дн.",
        "metadata": {"user_id": str(user_id)},
    }
    if method_id:  # автосписание с сохранённого способа оплаты
        params["payment_method_id"] = method_id
    else:
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
    method = p.payment_method.id if p.payment_method and getattr(p.payment_method, "saved", False) else None
    await extend(uid, PERIOD_DAYS, method)


async def extend(uid: int, days: int, method_id: str | None = None):
    row = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    base, was_active = now(), False
    if row and row["expires_at"] and row["active"]:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp > base:
            base, was_active = exp, True
    new_exp = base + timedelta(days=days)
    db.execute("""INSERT INTO users(user_id, expires_at, active, method_id) VALUES(?,?,1,?)
                  ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, active=1,
                  method_id=COALESCE(excluded.method_id, method_id)""", (uid, iso(new_exp), method_id))
    db.commit()
    if was_active:
        await safe_send(uid, f"✅ Подписка продлена до {fmt(new_exp)}.")
        return
    try:
        link = await bot.create_chat_invite_link(
            CHANNEL_ID, member_limit=1, expire_date=int(time.time()) + 86400)
        await safe_send(uid, f"✅ Оплата получена. Подписка до {fmt(new_exp)}.\n\n"
                             f"Ваша ссылка в канал (одноразовая, действует сутки):\n{link.invite_link}")
    except Exception:
        logging.exception("не удалось создать ссылку")
        await safe_send(uid, "✅ Оплата получена, но ссылку выдать не удалось. Напишите администратору.")
        await safe_send(ADMIN_ID, f"⚠️ Не удалось выдать ссылку пользователю {uid}. Бот — админ канала?")


async def expire(uid: int):
    try:
        await bot.ban_chat_member(CHANNEL_ID, uid)
        await bot.unban_chat_member(CHANNEL_ID, uid, only_if_banned=True)  # убрать, но не банить навсегда
    except Exception:
        logging.exception("не удалось удалить %s из канала", uid)
    db.execute("UPDATE users SET active=0 WHERE user_id=?", (uid,))
    db.commit()
    await safe_send(uid, "⏰ Подписка закончилась, доступ к каналу закрыт. Продлить:", pay_kb())


# ---------- Команды ----------
@dp.message(CommandStart())
async def start(m: Message):
    db.execute("INSERT INTO users(user_id, username) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
               (m.from_user.id, m.from_user.username))
    db.commit()
    auto = "\nПодписка продлевается автоматически, отключить: /cancel." if AUTOCHARGE else \
           "\nЗа несколько дней до конца я напомню о продлении."
    await m.answer(f"Привет! Доступ к закрытому каналу с лекциями: {PRICE:.0f} ₽ за {PERIOD_DAYS:g} дн.{auto}\n"
                   f"Статус: /status", reply_markup=pay_kb())


@dp.message(Command("status"))
async def status(m: Message):
    u = db.execute("SELECT * FROM users WHERE user_id=?", (m.from_user.id,)).fetchone()
    if u and u["active"] and u["expires_at"]:
        await m.answer(f"Подписка активна до {fmt(datetime.fromisoformat(u['expires_at']))}.")
    else:
        await m.answer("Подписки нет.", reply_markup=pay_kb())


@dp.message(Command("cancel"))
async def cancel(m: Message):
    db.execute("UPDATE users SET autopay=0 WHERE user_id=?", (m.from_user.id,))
    db.commit()
    await m.answer("Автопродление отключено. Доступ сохранится до конца оплаченного срока.")


@dp.callback_query(F.data == "pay")
async def pay(cb: CallbackQuery):
    if TEST_MODE:  # тест: считаем, что оплата прошла
        await cb.answer()
        await extend(cb.from_user.id, PERIOD_DAYS)
        return
    p = await new_payment(cb.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Перейти к оплате (СБП / карта)", url=p.confirmation.confirmation_url)]])
    note = "\nОплачивая, вы соглашаетесь на ежемесячное автопродление (отключить: /cancel)." if AUTOCHARGE else ""
    await cb.message.answer(f"Ссылка на оплату. После оплаты доступ придёт сюда автоматически.{note}", reply_markup=kb)
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


@dp.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    n = db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    s = db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    await m.answer(f"Активных подписчиков: {n}\nВсего оплат: {s}")


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
                await safe_send(uid, "Не удалось списать оплату автоматически. Продлите вручную:", pay_kb())
        if left <= timedelta(days=REMIND_DAYS):
            key = u["expires_at"] if auto else today  # автоплатёж: 1 напоминание; иначе раз в день
            if u["last_remind"] != key:
                db.execute("UPDATE users SET last_remind=? WHERE user_id=?", (key, uid))
                db.commit()
                end = fmt(datetime.fromisoformat(u["expires_at"]))
                if auto:
                    await safe_send(uid, f"🔔 Подписка до {end}. Спишем {PRICE:.0f} ₽ автоматически. Отключить: /cancel")
                else:
                    await safe_send(uid, f"🔔 Подписка заканчивается {end}. Продлить:", pay_kb())


async def scheduler():
    while True:
        try:
            await check_pending()
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
