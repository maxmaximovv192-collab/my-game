import asyncio
import os
import logging
from contextlib import suppress

from config import ROOT
from leaderboard import RatingStore, create_api, load_questions, daily_worker
from aiohttp import web
import rating_rules

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
DB_FILE = str(ROOT / os.environ.get("DB_FILE", "bot.db"))  # файл базы данных создастся сам
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://maxmaximovv192-collab.github.io/my-game/")
rating_store = RatingStore(DB_FILE)
START_TEXT = (
    "Привет! Открывай приложение и играй прямо в Telegram.\n"
    "/play — открыть приложение\n/top — рейтинг игры\n"
    "/stop — отключить рассылку\n/subscribe — включить рассылку"
)
PLAY_TEXT = "Нажми кнопку ниже, чтобы открыть приложение:"
RETIRED_TEXT = "Вопросы теперь только в приложении. Откройте его через /play."

def init_db():
    rating_store.init()


def register(user):
    rating_store.register(user.id, user.full_name)


def play_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])


dp = Dispatcher()
dp.message.filter(F.chat.type == "private")
dp.callback_query.filter(F.message.chat.type == "private")


@dp.message(CommandStart())
async def start(message: Message):
    register(message.from_user)
    await message.answer(START_TEXT, reply_markup=play_keyboard())


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


@dp.callback_query(F.data.startswith("a:") | (F.data == "next"))
async def retired_quiz(call: CallbackQuery):
    await call.answer(RETIRED_TEXT)
    if call.message:
        await call.message.edit_reply_markup(reply_markup=None)


@dp.message(Command("play"))
async def cmd_play(message: Message):
    register(message.from_user)
    await message.answer(PLAY_TEXT, reply_markup=play_keyboard())


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
