import os
import asyncio
import logging

from fastapi import FastAPI
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
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


# ========= 主選單 & 按鈕 UI =========

MAIN_MENU_TEXT = "請選擇功能："


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("📌 一般提醒", callback_data="main:general"),
            InlineKeyboardButton("📱 谷歌APK提醒", callback_data="main:apk"),
        ],
        [
            InlineKeyboardButton("🎰 香港六合開獎", callback_data="main:hk"),
        ],
        [
            InlineKeyboardButton("👥 人員名單編輯", callback_data="main:members"),
        ],
        [
            InlineKeyboardButton("📋 所有提醒列表", callback_data="main:list"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ========= Telegram handlers =========

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /start：問候 + 主選單按鈕"""
    if update.message:
        await update.message.reply_text(
            "嗨，我是你的提醒機器人～ ✅\n\n" + MAIN_MENU_TEXT,
            reply_markup=build_main_menu_keyboard(),
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("目前指令：\n/start  開啟功能選單\n/help   顯示這個說明")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """處理所有按鈕點擊"""
    query = update.callback_query
    await query.answer()

    data = query.data  # 例如 "main:general"、"general:fixed" ...

    # ---------- 第一層主選單 ----------

    if data == "main:general":
        # 一般提醒 -> 第二層
        keyboard = [
            [
                InlineKeyboardButton("🔁 固定周期", callback_data="general:fixed"),
                InlineKeyboardButton("📅 單一日期", callback_data="general:single"),
            ],
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="nav:back_main")],
        ]
        await query.edit_message_text(
            text="【一般提醒】\n請選擇提醒類型：",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "main:apk":
        # 谷歌 APK 提醒 -> 第二層
        keyboard = [
            [
                InlineKeyboardButton("➕ 新增 APK 提醒", callback_data="apk:new"),
            ],
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="nav:back_main")],
        ]
        await query.edit_message_text(
            text="【谷歌APK提醒】\n之後會在這裡設定每週幾、時間、內容與 @ 人。",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "main:hk":
        # 香港六合開獎 -> 第二層
        keyboard = [
            [
                InlineKeyboardButton("本月開獎日期", callback_data="hk:this_month"),
                InlineKeyboardButton("次月開獎日期", callback_data="hk:next_month"),
            ],
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="nav:back_main")],
        ]
        await query.edit_message_text(
            text="【香港六合開獎】\n請選擇要查看的月份：",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "main:members":
        # 人員名單編輯 -> 第二層
        keyboard = [
            [
                InlineKeyboardButton("➕ 新增（整個群組）", callback_data="members:add_all"),
                InlineKeyboardButton("🗑 刪除名單成員", callback_data="members:remove"),
            ],
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="nav:back_main")],
        ]
        await query.edit_message_text(
            text="【人員名單編輯】\n請選擇操作：",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "main:list":
        # 所有提醒列表（之後會實作）
        keyboard = [
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="nav:back_main")],
        ]
        await query.edit_message_text(
            text="【所有提醒列表】\n之後會在這裡列出本群組所有提醒，並提供刪除 / 編輯功能。",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # ---------- 導覽：回主選單 ----------

    if data == "nav:back_main":
        await query.edit_message_text(
            text=MAIN_MENU_TEXT,
            reply_markup=build_main_menu_keyboard(),
        )
        return

    # ---------- 第二層 先放占位（之後會補流程） ----------

    if data == "general:fixed":
        keyboard = [
            [InlineKeyboardButton("⬅️ 返回上一頁", callback_data="main:general")],
        ]
        await query.edit_message_text(
            text="【一般提醒 ➜ 固定周期】\n之後會在這裡讓你選每週幾、時間，並填入內容與 @ 人。",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "general:single":
        keyboard = [
            [InlineKeyboardButton("⬅️ 返回上一頁", callback_data="main:general")],
        ]
        await query.edit_message_text(
            text="【一般提醒 ➜ 單一日期】\n之後會在這裡讓你選日期、時間，並填入內容與 @ 人。",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "hk:this_month":
        keyboard = [
            [InlineKeyboardButton("⬅️ 返回上一頁", callback_data="main:hk")],
        ]
        await query.edit_message_text(
            text="【香港六合開獎 ➜ 本月】\n之後會在這裡顯示本月開獎日期，並讓你針對每一天設定 @ 人。",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "hk:next_month":
        keyboard = [
            [InlineKeyboardButton("⬅️ 返回上一頁", callback_data="main:hk")],
        ]
        await query.edit_message_text(
            text="【香港六合開獎 ➜ 次月】\n之後會在這裡顯示次月開獎日期（或顯示『官網尚未提供』）。",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "members:add_all":
        keyboard = [
            [InlineKeyboardButton("⬅️ 返回上一頁", callback_data="main:members")],
        ]
        await query.edit_message_text(
            text="【人員名單編輯 ➜ 新增】\n之後會在這裡自動把本群所有成員加入可 @ 名單。",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "members:remove":
        keyboard = [
            [InlineKeyboardButton("⬅️ 返回上一頁", callback_data="main:members")],
        ]
        await query.edit_message_text(
            text="【人員名單編輯 ➜ 刪除】\n之後會在這裡列出名單，點名字即可移除。",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return


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

            # 指令 handler
            application.add_handler(CommandHandler("start", cmd_start))
            application.add_handler(CommandHandler("help", cmd_help))

            # 按鈕 callback handler
            application.add_handler(CallbackQueryHandler(menu_callback))

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
