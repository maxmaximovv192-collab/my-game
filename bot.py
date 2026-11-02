import asyncio
import random
import os
import logging
from contextlib import suppress

from config import ROOT
from leaderboard import RatingStore, create_api, load_questions, daily_worker
from aiohttp import web
import rating_rules
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

# ---------- НАСТРОЙКИ ----------
TOKEN = os.environ.get("BOT_TOKEN", "")
REWARD = 1  # сколько монет за верный ответ
DB_FILE = str(ROOT / os.environ.get("DB_FILE", "bot.db"))  # файл базы данных создастся сам
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://maxmaximovv192-collab.github.io/my-game/")
rating_store = RatingStore(DB_FILE)

# ---------- ТЕСТОВЫЕ ВОПРОСЫ ----------
# Пока это 3 примера, чтобы проверить, что всё работает.
# Позже вопросы будут загружаться из таблицы.
# "correct" - номер верного варианта (считается с нуля: 0 = первый вариант)
QUESTIONS = [
    {
        "text": "Цитолиз печеночных клеток при вирусных гепатитах характеризуется повышением",
        "options": ["АЛТ и АСТ", "сывороточного железа", "гаммаглобулина", "холестерина"],
        "correct": 0,
    },
    {
        "text": "Яркая гиперемия кожи с четкими границами неправильной формы в сочетании с лихорадкой свойственна",
        "options": ["флегмоне", "плоскому лишаю", "роже", "абсцессу"],
        "correct": 2,
    },
    {
        "text": "Золотым стандартом диагностики феохромоцитомы является анализ",
        "options": [
            "крови на ренин и альдостерон",
            "суточной мочи на ванилилминдальную кислоту",
            "суточной мочи на метанефрин и норметанефрин",
            "крови на электролиты",
        ],
        "correct": 2,
    },
]


# ---------- БАЗА ДАННЫХ ----------
def init_db():
    rating_store.init()
    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "user_id INTEGER PRIMARY KEY, "
            "name TEXT, "
            "coins INTEGER NOT NULL DEFAULT 0)"
        )


def register(user):
    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            "INSERT INTO users (user_id, name) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
            (user.id, user.full_name),
        )


def add_coins(user_id, amount):
    with sqlite3.connect(DB_FILE) as con:
        con.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id))


def get_coins(user_id):
    with sqlite3.connect(DB_FILE) as con:
        row = con.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else 0


def get_top(limit=10):
    with sqlite3.connect(DB_FILE) as con:
        return con.execute(
            "SELECT name, coins FROM users ORDER BY coins DESC LIMIT ?", (limit,)
        ).fetchall()


# ---------- ВОПРОСЫ И КНОПКИ ----------
def make_question():
    qid = random.randrange(len(QUESTIONS))
    q = QUESTIONS[qid]
    order = list(range(len(q["options"])))
    random.shuffle(order)  # варианты каждый раз в новом порядке

    lines = [q["text"], ""]
    buttons = []
    for number, i in enumerate(order, start=1):
        lines.append(f"{number}) {q['options'][i]}")
        buttons.append(InlineKeyboardButton(text=str(number), callback_data=f"a:{qid}:{i}"))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[buttons])


def next_keyboard():
    button = InlineKeyboardButton(text="Следующий вопрос ➡️", callback_data="next")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


# ---------- БОТ ----------
dp = Dispatcher()
dp.message.filter(F.chat.type == "private")
dp.callback_query.filter(F.message.chat.type == "private")


@dp.message(CommandStart())
async def start(message: Message):
    register(message.from_user)
    await message.answer(
        "Привет! Отвечай на вопросы и зарабатывай монеты.\n"
        "/play — открыть приложение\n/question — новый вопрос\n/me — мой баланс\n"
        "/top — рейтинг игры\n/stop — отключить рассылку\n/subscribe — включить рассылку"
    )
    text, keyboard = make_question()
    await message.answer(text, reply_markup=keyboard)


@dp.message(Command("question"))
async def question(message: Message):
    register(message.from_user)
    text, keyboard = make_question()
    await message.answer(text, reply_markup=keyboard)


@dp.message(Command("me"))
async def me(message: Message):
    register(message.from_user)
    await message.answer(f"Твой баланс: {get_coins(message.from_user.id)} монет")


@dp.message(Command("top"))
async def top(message: Message):
    register(message.from_user)
    if not rating_rules.RULES_READY:
        await message.answer("Рейтинг готовится: правила начисления баллов пока не настроены.")
        return
    data = rating_store.ranking(message.from_user.id)
    lines = ["🏆 Рейтинг игры за всё время", ""]
    for row in data["top"]:
        lines.append(f"{row['place']}. {row['name']}: {row['points']} балл.")
    if not data["top"]:
        lines.append("Пока нет верных ответов. /play — начать игру")
    me = data["me"]
    if me:
        lines.append(f"\nВаше место: {me['place']} из {data['total']} · {me['points']} балл.")
    await message.answer("\n".join(lines))


@dp.message(Command("stop"))
async def stop_reports(message: Message):
    register(message.from_user)
    rating_store.subscribe(message.from_user.id, False)
    await message.answer("Ежедневная рассылка отключена. /subscribe — включить снова.")


@dp.message(Command("subscribe"))
async def subscribe_reports(message: Message):
    register(message.from_user)
    rating_store.subscribe(message.from_user.id, True)
    await message.answer("Ежедневная рассылка рейтинга включена.")


@dp.callback_query(F.data.startswith("a:"))
async def on_answer(call: CallbackQuery):
    _, qid, chosen = call.data.split(":")
    q = QUESTIONS[int(qid)]
    register(call.from_user)

    if int(chosen) == q["correct"]:
        add_coins(call.from_user.id, REWARD)
        result = f"✅ Верно! +{REWARD} монет"
    else:
        result = f"❌ Неверно. Правильный ответ: {q['options'][q['correct']]}"

    balance = get_coins(call.from_user.id)
    await call.message.edit_text(
        f"{call.message.text}\n\n{result}\nБаланс: {balance}",
        reply_markup=next_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "next")
async def on_next(call: CallbackQuery):
    text, keyboard = make_question()
    await call.message.answer(text, reply_markup=keyboard)
    await call.answer()

@dp.message(Command("play"))
async def cmd_play(message: Message):
    register(message.from_user)
    secret_url = WEBAPP_URL
    
    # Создаем кнопку Web App
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Запустить игру", web_app=WebAppInfo(url=secret_url))]
    ])
    
    await message.answer("Жми на кнопку ниже, чтобы открыть игру прямо здесь:", reply_markup=keyboard)


async def main():
    if not TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN в .env или настройках сервера.")
    logging.basicConfig(level=logging.INFO)
    daily_time = os.environ.get("DAILY_TIME", "21:00")
    from datetime import time as clock_time
    clock_time.fromisoformat(daily_time)  # fail before launching if config is invalid
    if len(daily_time) != 5:
        raise ValueError("DAILY_TIME must be HH:MM")
    init_db()
    bank = load_questions(ROOT / "index.html")
    api = create_api(rating_store, TOKEN, bank, os.environ.get("ALLOWED_ORIGIN", "https://maxmaximovv192-collab.github.io"))
    runner = web.AppRunner(api, access_log=None)
    bot = Bot(token=TOKEN)
    worker = None
    try:
        await runner.setup()
        await web.TCPSite(runner, os.environ.get("API_HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))).start()
        if os.environ.get("DAILY_REPORTS_ENABLED", "false").lower() == "true" and rating_rules.RULES_READY:
            worker = asyncio.create_task(daily_worker(bot, rating_store, daily_time))
        print("Бот и API рейтинга запущены. Ctrl+C — остановка.")
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
