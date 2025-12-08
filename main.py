import os
import asyncio
import logging

from fastapi import FastAPI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ====== Logging 設定 ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====== 讀取環境變數 ======
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
if not TG_BOT_TOKEN:
    raise RuntimeError("環境變數 TG_BOT_TOKEN 未設定")

# ====== 建立 FastAPI app，給 Render 用 ======
app = FastAPI()


@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    # 給 Render 的 Health Check Path 用
    return {"status": "healthy"}


# ====== Telegram Bot 指令處理 ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("嗨，我已經在 Render 上運作囉！")


async def run_bot() -> None:
    """建立並啟動 Telegram Bot（使用新版 run_polling）"""
    application = (
        ApplicationBuilder()
        .token(TG_BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))

    logger.info("Starting Telegram bot polling...")
    # 這邊在 python-telegram-bot 20.7 是同步函式，所以用 to_thread 跑在背景
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, application.run_polling)


@app.on_event("startup")
async def on_startup():
    # 用 background task 跑，不要阻塞 FastAPI
    asyncio.create_task(run_bot())
    logger.info("Startup event: Telegram bot task created.")
