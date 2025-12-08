from fastapi import FastAPI
from aiogram import Bot, Dispatcher
from aiogram.types import Message
import os

TOKEN = os.getenv("TG_BOT_TOKEN")

app = FastAPI()
bot = Bot(token=TOKEN)
dp = Dispatcher()


@app.get("/")
def home():
    return {"status": "TG Bot is running on Render ✅"}


@dp.message()
async def echo(message: Message):
    await message.answer(f"你剛剛說的是：\n{message.text}")


@app.post("/webhook")
async def telegram_webhook(update: dict):
    from aiogram.types import Update
    telegram_update = Update(**update)
    await dp.feed_update(bot, telegram_update)
    return {"ok": True}
