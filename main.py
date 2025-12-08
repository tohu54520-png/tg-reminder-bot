import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

# ❌ Background Worker 不需要 FastAPI 了
# from fastapi import FastAPI
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut

# ========= 基本設定 =========

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TZ = ZoneInfo("Asia/Taipei")  # 預設時區

DB_PATH = "reminders.db"  # SQLite 檔案路徑

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("main")

# ❌ 不再需要 FastAPI app
# app = FastAPI()

# Conversation 狀態
(
    MENU,                 # 主選單
    GENERAL_MENU,         # 一般提醒選單
    SD_DATE,              # 單一日期：輸入日期
    SD_TIME,              # 單一日期：輸入時間
    SD_TEXT,              # 單一日期：輸入內容
    REMINDER_LIST,        # 所有提醒列表
) = range(6)

# ========= SQLite 工具 =========

def init_db():
    """初始化 SQLite 資料庫。"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            kind    TEXT    NOT NULL,   -- general_single / apk / lottery ... etc
            run_at  INTEGER NOT NULL,   -- Unix timestamp（秒）
            text    TEXT    NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    logger.info("DB initialized.")

def db_add_reminder(chat_id: int, kind: str, run_at: datetime, text: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reminders (chat_id, kind, run_at, text) VALUES (?, ?, ?, ?)",
        (chat_id, kind, int(run_at.timestamp()), text),
    )
    reminder_id = cur.lastrowid
    conn.commit()
    conn.close()
    return reminder_id

def db_list_reminders(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, kind, run_at, text FROM reminders WHERE chat_id=? ORDER BY run_at ASC",
        (chat_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def db_get_reminder(reminder_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, chat_id, kind, run_at, text FROM reminders WHERE id=?",
        (reminder_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row

def db_delete_reminder(reminder_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()

# ========= 小工具 =========

def parse_mmdd(text: str):
    """解析 MMDD，回傳 (month, day) 或 None。"""
    text = text.strip()
    if len(text) != 4 or not text.isdigit():
        return None
    month = int(text[:2])
    day = int(text[2:])
    try:
        datetime(2000, month, day)  # 年份隨便給一個，只為了驗證是否合法
    except ValueError:
        return None
    return month, day

def parse_hhmm(text: str):
    """解析 HHMM，回傳 (hour, minute) 或 None。"""
    text = text.strip()
    if len(text) != 4 or not text.isdigit():
        return None
    hour = int(text[:2])
    minute = int(text[2:])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute

def format_ts(ts: int) -> str:
    """把 timestamp 轉成 MM/DD HH:MM（台北時間）。"""
    dt = datetime.fromtimestamp(ts, TZ)
    return dt.strftime("%m/%d %H:%M")

async def send_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, text: str = "請選擇功能："):
    """發送主選單 Inline Keyboard。"""
    keyboard = [
        [InlineKeyboardButton("一般提醒", callback_data="menu_general")],
        [InlineKeyboardButton("谷歌APK提醒", callback_data="menu_apk")],
        [InlineKeyboardButton("香港六合開獎", callback_data="menu_lottery")],
        [InlineKeyboardButton("人員名單編輯", callback_data="menu_people")],
        [InlineKeyboardButton("所有提醒列表", callback_data="menu_list")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)

# ========= JobQueue：提醒任務 =========

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id = data["chat_id"]
    text = data["text"]
    when_str = data["when_str"]
    reminder_id = data.get("reminder_id")

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ 提醒時間到囉（{when_str}）：\n{text}",
    )

    if reminder_id is not None:
        try:
            db_delete_reminder(reminder_id)
        except Exception as e:
            logger.warning("刪除提醒（ID=%s）時發生錯誤：%s", reminder_id, e)

# ========= 指令處理 =========
#（這一區你的程式我都照原樣保留，略）

# ... 這裡保留你原本的 start / cmd_help / send_reminder_list / reminder_list_callback
# ... main_menu_callback / general_menu_callback / back_* / single_date_got_* 全部同上
# ... single_date_got_text 也不動

# ========= Bot 啟動邏輯 =========

async def run_bot():
    """持續啟動 / 維持 Telegram Bot。"""
    while True:
        try:
            logger.info("Building Telegram application...")

            request = HTTPXRequest(
                read_timeout=30.0,
                connect_timeout=10.0,
                pool_timeout=10.0,
            )

            application = (
                ApplicationBuilder()
                .token(TG_BOT_TOKEN)
                .request(request)
                .build()
            )

            conv_handler = ConversationHandler(
                entry_points=[CommandHandler("start", start)],
                states={
                    MENU: [
                        CallbackQueryHandler(main_menu_callback),
                    ],
                    GENERAL_MENU: [
                        CallbackQueryHandler(general_menu_callback),
                    ],
                    SD_DATE: [
                        CallbackQueryHandler(back_from_date_to_general, pattern="^back_to_general$"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, single_date_got_date),
                    ],
                    SD_TIME: [
                        CallbackQueryHandler(back_from_time_to_date, pattern="^back_to_date$"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, single_date_got_time),
                    ],
                    SD_TEXT: [
                        CallbackQueryHandler(back_from_text_to_time, pattern="^back_to_time$"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, single_date_got_text),
                    ],
                    REMINDER_LIST: [
                        CallbackQueryHandler(reminder_list_callback),
                    ],
                },
                fallbacks=[CommandHandler("start", start)],
                allow_reentry=True,
            )

            application.add_handler(conv_handler)
            application.add_handler(CommandHandler("help", cmd_help))

            await application.initialize()
            await application.start()
            await application.updater.start_polling()

            logger.info("Telegram bot started (polling).")

            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                logger.info("Stopping Telegram bot...")
                await application.updater.stop()
                await application.stop()
                await application.shutdown()

        except TimedOut:
            logger.warning("Telegram API TimedOut，5 秒後重試啟動 bot。")
            await asyncio.sleep(5)

        except Exception as e:
            logger.exception("run_bot 發生未預期錯誤：%s，30 秒後重試。", e)
            await asyncio.sleep(30)

# ========= Background Worker 入口點 =========

async def main():
    logger.info("Worker starting, init DB and bot...")
    init_db()
    await run_bot()

if __name__ == "__main__":
    asyncio.run(main())
