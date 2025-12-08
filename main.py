import os
import asyncio
import logging

from fastapi import FastAPI
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# 基本 log 設定，方便之後在 Render log 看到訊息
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# 從環境變數讀取 TOKEN（你在 Render 的 Environment Variables 已經設定 TG_BOT_TOKEN 了）
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
if not TG_BOT_TOKEN:
    raise RuntimeError("環境變數 TG_BOT_TOKEN 未設定")

# FastAPI app，給 Render 當 web service 用
app = FastAPI()

# 之後會把真正的 Telegram Application 放在這裡
telegram_app: Application | None = None


@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    # 給 Render 的 Health Check Path 用
    return {"status": "healthy"}


# /start 指令，先做一個最簡單的回覆確認 bot 有活著
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("嗨，我已經在 Render 上運作囉！")


# 啟動 Telegram Bot（用 polling）
async def start_telegram() -> None:
    global telegram_app

    telegram_app = (
        ApplicationBuilder()
        .token(TG_BOT_TOKEN)
        .build()
    )

    # 先只加 /start 指令，之後我們再把你的按鈕&提醒功能慢慢加進來
    telegram_app.add_handler(CommandHandler("start", cmd_start))

    # 初始化 & 啟動 polling，注意這裡用 await 而不是阻塞 run_polling()
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()


# 關閉 Telegram Bot（給 FastAPI shutdown 用）
async def stop_telegram() -> None:
    if telegram_app is None:
        return

    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


# FastAPI 啟動時一起啟動 Telegram Bot
@app.on_event("startup")
async def on_startup():
    # 建一個 background task，不要阻塞 FastAPI 本身
    asyncio.create_task(start_telegram())


# FastAPI 關閉時順便把 Telegram Bot 關掉
@app.on_event("shutdown")
async def on_shutdown():
    await stop_telegram()
