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
from telegram.request import HTTPXRequest
from telegram.error import TimedOut

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("main")

app = FastAPI()


@app.get("/")
async def root():
    return {"status": "ok"}


# ========= Telegram handlers =========

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("嗨，我是你的提醒機器人～ ✅")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("目前指令：/start /help")


# ========= Bot 啟動邏輯 =========

async def run_bot():
    """持續啟動 / 維持 Telegram Bot。"""
    while True:
        try:
            logger.info("Building Telegram application...")

            # 調高 Telegram HTTP 請求的 timeout
            request = HTTPXRequest(
                read_timeout=30.0,       # 回應讀取最多等 30 秒
                connect_timeout=10.0,    # 連線最多等 10 秒
                pool_timeout=10.0,
            )

            application = (
                ApplicationBuilder()
                .token(TG_BOT_TOKEN)
                .request(request)
                .build()
            )

            # 加上指令
            application.add_handler(CommandHandler("start", cmd_start))
            application.add_handler(CommandHandler("help", cmd_help))

            # 初始化 & 啟動 bot（非阻塞）
            await application.initialize()
            await application.start()
            await application.updater.start_polling()

            logger.info("Telegram bot started (polling).")

            # 讓 bot 一直活著，直到被取消
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                logger.info("Stopping Telegram bot...")
                await application.updater.stop()
                await application.stop()
                await application.shutdown()

        except TimedOut:
            # 連 Telegram API 超時，稍後重試
            logger.warning("Telegram API TimedOut，5 秒後重試啟動 bot。")
            await asyncio.sleep(5)

        except Exception as e:
            # 其他非預期錯誤，也記 log 後重試
            logger.exception("run_bot 發生未預期錯誤：%s，30 秒後重試。", e)
            await asyncio.sleep(30)


# ========= FastAPI lifecycle =========

@app.on_event("startup")
async def on_startup():
    logger.info("Startup event: creating Telegram bot task.")
    asyncio.create_task(run_bot())


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("FastAPI app is shutting down.")
