import os
import asyncio
import logging
from datetime import datetime, timezone

import pytz
from fastapi import FastAPI
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut

# ========== 環境變數 ==========
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TZ_NAME = os.environ.get("TZ", "Asia/Taipei")
TZ = pytz.timezone(TZ_NAME)

# ========== Logging ==========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("main")

# ========== FastAPI App ==========
app = FastAPI()


@app.get("/")
async def root():
    return {"status": "ok"}


# ========== 主選單 UI ==========

def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("📌 一般提醒", callback_data="main:general"),
        ],
        [
            InlineKeyboardButton("📱 谷歌 APK 提醒（未實作）", callback_data="main:google_apk"),
        ],
        [
            InlineKeyboardButton("🎰 香港六合開獎（未實作）", callback_data="main:hk_lottery"),
        ],
        [
            InlineKeyboardButton("👥 人員名單編輯（未實作）", callback_data="main:members"),
        ],
        [
            InlineKeyboardButton("📝 所有提醒列表（未實作）", callback_data="main:list_all"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def general_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("📆 單一日期", callback_data="general:single"),
            InlineKeyboardButton("🔁 固定周期（未實作）", callback_data="general:repeat"),
        ],
        [
            InlineKeyboardButton("⬅️ 返回主選單", callback_data="main_menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ========== Command Handlers ==========

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "嗨，我是你的提醒機器人～ ✅\n\n"
        "請從下方選單選擇功能："
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())
    elif update.callback_query:
        # 如果之後想從按鈕回到主選單也可以用這個
        await update.callback_query.edit_message_text(
            text=text,
            reply_markup=main_menu_keyboard(),
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "目前功能：\n"
        " /start  開啟主選單\n"
        " /help   顯示說明\n\n"
        "已實作：\n"
        " - 📌 一般提醒 ➜ 📆 單一日期\n"
        "   日期輸入：MMDD，例如 1201 表示 12/01\n"
        "   時間輸入：HHMM（24 小時制），例如 2100 表示 21:00"
    )


# ========== CallbackQuery Handler（按鈕） ==========

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return

    query = update.callback_query
    data = query.data
    chat_data = context.chat_data

    await query.answer()

    # 回主選單
    if data == "main_menu":
        await query.edit_message_text(
            "請選擇功能：",
            reply_markup=main_menu_keyboard(),
        )
        return

    # 第一層：一般提醒
    if data == "main:general":
        chat_data.clear()
        await query.edit_message_text(
            "【一般提醒】\n請選擇提醒類型：",
            reply_markup=general_menu_keyboard(),
        )
        return

    # 一般提醒 ➜ 固定周期（先放佔位）
    if data == "general:repeat":
        await query.edit_message_text(
            "【一般提醒 ➜ 固定周期】\n"
            "這個功能尚未實作，之後再幫你加上 💪\n\n"
            "目前可以先使用：📆 單一日期。",
            reply_markup=general_menu_keyboard(),
        )
        return

    # 一般提醒 ➜ 單一日期
    if data == "general:single":
        # 狀態機：先要日期，再要時間，再要內容
        chat_data.clear()
        chat_data["state"] = "general_single_wait_date"
        chat_data["tmp"] = {}

        keyboard = [
            [InlineKeyboardButton("⬅️ 返回上一頁", callback_data="main:general")],
        ]
        await query.edit_message_text(
            text=(
                "【一般提醒 ➜ 單一日期】\n"
                "請輸入日期（四位數字 MMDD），例如：1201 代表 12/01。\n"
                "若要取消，輸入 /cancel。"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # 其它主選單項目暫時先提示未實作
    if data.startswith("main:"):
        await query.edit_message_text(
            "這個功能尚未實作，之後再幫你加上 🔧\n\n"
            "先回主選單：",
            reply_markup=main_menu_keyboard(),
        )
        return


# ========== Job: 單一日期提醒發送 ==========

async def single_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    text = job.data.get("text", "")
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"⏰ 提醒：{text}",
    )


# ========== 文字輸入流程（日期 / 時間 / 內容） ==========

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理建立【一般提醒 ➜ 單一日期】時的文字輸入。"""
    if not update.message:
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    chat_data = context.chat_data

    # 手動取消
    if text.lower() in {"/cancel", "cancel"}:
        chat_data.clear()
        await update.message.reply_text("已取消，若要重新設定請回 /start 選單。")
        return

    state = chat_data.get("state")

    # ---- 狀態 1：等日期（四位數字 MMDD）----
    if state == "general_single_wait_date":
        # 四位數字檢查
        if not (text.isdigit() and len(text) == 4):
            await update.message.reply_text(
                "日期格式不正確，請輸入四位數字 MMDD，例如 1201 表示 12/01。"
            )
            return

        month = int(text[:2])
        day = int(text[2:])

        try:
            now_local = datetime.now(TZ)
            year = now_local.year
            # 檢查日期是否合法（例如 02/30 會出錯）
            dt_local = TZ.localize(datetime(year, month, day, 0, 0))
        except Exception:
            await update.message.reply_text(
                "日期不合法，請重新輸入四位數字 MMDD，例如 1201 表示 12/01。"
            )
            return

        chat_data["tmp"]["date"] = dt_local  # 先存日期（不含時間）
        chat_data["state"] = "general_single_wait_time"

        await update.message.reply_text(
            "好的，日期已記錄為 {:%m/%d}。\n"
            "請輸入時間（四位數字 HHMM，24 小時制），例如 2100 表示 21:00。".format(dt_local)
        )
        return

    # ---- 狀態 2：等時間（四位數字 HHMM）----
    if state == "general_single_wait_time":
        base_date = chat_data["tmp"].get("date")
        if base_date is None:
            # 理論上不會發生，保險處理
            chat_data.clear()
            await update.message.reply_text("流程狀態遺失，請重新從 /start 開始設定。")
            return

        # 四位數字檢查
        if not (text.isdigit() and len(text) == 4):
            await update.message.reply_text(
                "時間格式不正確，請輸入四位數字 HHMM（24 小時制），例如 0930 或 2100。"
            )
            return

        hour = int(text[:2])
        minute = int(text[2:])

        try:
            # 用之前存好的日期 + 時間
            dt_local = TZ.localize(
                datetime(
                    year=base_date.year,
                    month=base_date.month,
                    day=base_date.day,
                    hour=hour,
                    minute=minute,
                )
            )
            dt_utc = dt_local.astimezone(timezone.utc)
        except Exception:
            await update.message.reply_text(
                "時間不合法，請重新輸入四位數字 HHMM（24 小時制），例如 0930 或 2100。"
            )
            return

        chat_data["tmp"]["dt_utc"] = dt_utc
        chat_data["state"] = "general_single_wait_text"

        await update.message.reply_text(
            "好的，時間已記錄為 {:%m/%d %H:%M}。\n"
            "請輸入提醒內容，例如：開會、發報表…".format(dt_local)
        )
        return

    # ---- 狀態 3：等提醒內容 ----
    if state == "general_single_wait_text":
        dt_utc = chat_data["tmp"].get("dt_utc")
        if dt_utc is None:
            chat_data.clear()
            await update.message.reply_text("流程狀態遺失，請重新從 /start 開始設定。")
            return

        reminder_text = text

        # 設定 Job Queue 單一提醒
        context.job_queue.run_once(
            single_reminder_job,
            when=dt_utc,
            chat_id=chat_id,
            data={"text": reminder_text},
        )

        # 清空狀態
        chat_data.clear()

        dt_local = dt_utc.astimezone(TZ)
        await update.message.reply_text(
            "✅ 已建立【一般提醒 ➜ 單一日期】\n"
            "時間：{:%m/%d %H:%M}\n"
            "內容：{}".format(dt_local, reminder_text)
        )
        return

    # 其他狀態：暫時不處理，之後你要可以做聊天或提示
    return


# ========== Bot 啟動邏輯（跟之前一樣） ==========

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

            # 指令
            application.add_handler(CommandHandler("start", cmd_start))
            application.add_handler(CommandHandler("help", cmd_help))

            # 按鈕 callback
            application.add_handler(CallbackQueryHandler(menu_callback))

            # 文字輸入（用來處理 MMDD / HHMM / 提醒內容）
            application.add_handler(
                MessageHandler(
                    filters.TEXT & (~filters.COMMAND),
                    text_message_handler,
                )
            )

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


# ========== FastAPI lifecycle ==========

@app.on_event("startup")
async def on_startup():
    logger.info("Startup event: creating Telegram bot task.")
    asyncio.create_task(run_bot())


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("FastAPI app is shutting down.")
