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

# 從環境變數讀取 TG token & 時區
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TZ = os.getenv("TZ", "Asia/Taipei")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("main")

app = FastAPI()


@app.get("/")
async def root():
    return {"status": "ok"}


# ======== Telegram 指令 handler ========

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("嗨，我是你的提醒機器人～ ✅")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("目前指令：/start /help")


# ======== 啟動 Telegram Bot 的協程 ========

async def run_bot():
    logger.info("Building Telegram application...")

    application = (
        ApplicationBuilder()
        .token(TG_BOT_TOKEN)
        .build()
    )

    # 加入指令 handler
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))

    # 用「非阻塞」的方式啟動 bot
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    logger.info("Telegram bot started (polling).")

    # 讓這個 task 一直存活，直到服務被關閉
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Shutting down Telegram bot...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        raise


# ======== FastAPI 的啟動 / 關閉事件 ========

@app.on_event("startup")
async def on_startup():
    logger.info("Startup event: creating Telegram bot task.")
    asyncio.create_task(run_bot())


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("FastAPI app is shutting down.")
