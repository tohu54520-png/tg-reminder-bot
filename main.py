import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

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

app = FastAPI()

# Conversation 狀態
(
    MENU,                 # 主選單
    GENERAL_MENU,         # 一般提醒選單
    SD_DATE,              # 單一日期：輸入日期
    SD_TIME,              # 單一日期：輸入時間
    SD_TEXT,              # 單一日期：輸入內容
    REMINDER_LIST,        # 所有提醒列表
) = range(6)

# ========= FastAPI 路由 =========


@app.get("/")
async def root():
    return {"status": "ok"}


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

    # Job 執行完，把這筆提醒從 DB 刪掉（如果還在）
    if reminder_id is not None:
        try:
            db_delete_reminder(reminder_id)
        except Exception as e:
            logger.warning("刪除提醒（ID=%s）時發生錯誤：%s", reminder_id, e)


# ========= 指令處理 =========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """進入主選單。"""
    chat_id = update.effective_chat.id
    await send_main_menu(
        chat_id,
        context,
        "嗨，我是你的提醒機器人～ ✅\n請先選擇功能：",
    )
    return MENU


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("目前指令：\n/start - 主選單\n/help - 顯示這個說明")


# ========= 所有提醒列表 =========

async def send_reminder_list(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """發送『所有提醒列表』畫面。"""
    rows = db_list_reminders(chat_id)
    if not rows:
        await context.bot.send_message(
            chat_id=chat_id,
            text="【所有提醒列表】\n目前這個聊天室還沒有任何提醒～",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ 返回主選單", callback_data="reminder_back_main")]]
            ),
        )
        return

    keyboard = []
    for rid, kind, run_at, text in rows:
        when_str = format_ts(run_at)
        kind_label = {
            "general_single": "一般提醒",
            "apk": "谷歌APK",
            "lottery": "香港六合彩",
        }.get(kind, kind)
        label = f"{when_str}｜{kind_label}"
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"reminder_{rid}")]
        )

    keyboard.append(
        [InlineKeyboardButton("⬅️ 返回主選單", callback_data="reminder_back_main")]
    )

    markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        chat_id=chat_id,
        text="【所有提醒列表】\n點選下面任一項目，可以查看或刪除提醒：",
        reply_markup=markup,
    )


async def reminder_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理『所有提醒列表』相關的所有 callback。"""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    # 回主選單
    if data == "reminder_back_main":
        await send_main_menu(chat_id, context)
        return MENU

    # 回列表（目前其實就是再發一次列表）
    if data == "reminder_back_list":
        await send_reminder_list(chat_id, context)
        return REMINDER_LIST

    # 刪除
    if data.startswith("reminder_delete_"):
        rid = int(data.split("_")[-1])
        # 先刪 DB
        db_delete_reminder(rid)
        # 再取消 Job
        job_name = f"reminder-{rid}"
        jobs = context.application.job_queue.get_jobs_by_name(job_name)
        for job in jobs:
            job.schedule_removal()

        await query.message.reply_text("✅ 已刪除這筆提醒。")
        await send_reminder_list(chat_id, context)
        return REMINDER_LIST

    # 查看詳細
    if data.startswith("reminder_"):
        rid = int(data.split("_")[-1])
        row = db_get_reminder(rid)
        if not row:
            await query.message.reply_text("這筆提醒已不存在，可能剛剛被刪除或已經觸發了。")
            await send_reminder_list(chat_id, context)
            return REMINDER_LIST

        _id, _chat_id, kind, run_at, text = row
        when_str = format_ts(run_at)
        kind_label = {
            "general_single": "一般提醒",
            "apk": "谷歌APK",
            "lottery": "香港六合彩",
        }.get(kind, kind)

        detail = (
            f"【提醒詳細】\n"
            f"類型：{kind_label}\n"
            f"時間：{when_str}\n"
            f"內容：{text}\n\n"
            f"目前先提供刪除功能，時間／內容編輯之後再幫你加上。"
        )

        keyboard = [
            [InlineKeyboardButton("🗑 刪除提醒", callback_data=f"reminder_delete_{rid}")],
            [InlineKeyboardButton("⬅️ 返回列表", callback_data="reminder_back_list")],
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="reminder_back_main")],
        ]
        await query.message.reply_text(detail, reply_markup=InlineKeyboardMarkup(keyboard))
        return REMINDER_LIST

    # 預設：留在列表狀態
    return REMINDER_LIST


# ========= 主選單 Callback =========

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "menu_general":
        # 一般提醒子選單
        keyboard = [
            [
                # 單一日期在左邊，固定週期右邊
                InlineKeyboardButton("單一日期", callback_data="general_single"),
                InlineKeyboardButton("固定週期（尚未實作）", callback_data="general_cycle"),
            ],
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="general_back")],
        ]
        markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("【一般提醒】請選擇類型：", reply_markup=markup)
        return GENERAL_MENU

    if data == "menu_list":
        # 所有提醒列表
        await send_reminder_list(chat_id, context)
        return REMINDER_LIST

    elif data.startswith("menu_"):
        # 其他主選單項目暫時先給個提示
        await query.message.reply_text("這個功能我還在幫你準備，之後再來試試看～")
        return MENU

    return MENU


# ========= 一般提醒選單 Callback =========

async def general_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "general_back":
        # 回主選單
        await send_main_menu(chat_id, context)
        return MENU

    if data == "general_cycle":
        await query.message.reply_text("固定週期提醒我之後再幫你做，現在先用「單一日期」吧～")
        return GENERAL_MENU

    if data == "general_single":
        # 進入「一般提醒 ➜ 單一日期」
        context.user_data.pop("sd_date", None)
        context.user_data.pop("sd_time", None)

        keyboard = [
            [InlineKeyboardButton("⬅️ 返回上一頁", callback_data="back_to_general")],
        ]
        markup = InlineKeyboardMarkup(keyboard)
        text = (
            "【一般提醒 ➜ 單一日期】\n"
            "請輸入日期四位數字（例如：1201 代表 12/01）。"
        )
        await query.message.reply_text(text, reply_markup=markup)
        return SD_DATE

    return GENERAL_MENU


# ========= 單一日期 flow：日期層 =========

async def back_from_date_to_general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """在輸入日期這層，按『返回上一頁』。"""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    keyboard = [
        [
            InlineKeyboardButton("單一日期", callback_data="general_single"),
            InlineKeyboardButton("固定週期（尚未實作）", callback_data="general_cycle"),
        ],
        [InlineKeyboardButton("⬅️ 返回主選單", callback_data="general_back")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("【一般提醒】請選擇類型：", reply_markup=markup)
    return GENERAL_MENU


async def single_date_got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """收到 MMDD。"""
    text = update.message.text.strip()
    parsed = parse_mmdd(text)
    if not parsed:
        await update.message.reply_text(
            "格式有誤，請輸入『四位數字』，例如：1201 代表 12/01。"
        )
        return SD_DATE

    month, day = parsed
    context.user_data["sd_date"] = (month, day)

    keyboard = [
        [InlineKeyboardButton("⬅️ 修改日期", callback_data="back_to_date")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "請輸入時間四位數字（24小時制例如1701）。",
        reply_markup=markup,
    )
    return SD_TIME


# ========= 單一日期 flow：時間層 =========

async def back_from_time_to_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """在時間層按『修改日期』，回到輸入日期。"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("⬅️ 返回上一頁", callback_data="back_to_general")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    text = (
        "【一般提醒 ➜ 單一日期】\n"
        "請輸入日期四位數字（例如：1201 代表 12/01）。"
    )
    await query.message.reply_text(text, reply_markup=markup)
    return SD_DATE


async def back_from_text_to_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """在內容層按『修改時間』，回到時間層。"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("⬅️ 修改日期", callback_data="back_to_date")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(
        "請輸入時間四位數字（24小時制例如1701）。",
        reply_markup=markup,
    )
    return SD_TIME


async def single_date_got_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """收到 HHMM。"""
    text = update.message.text.strip()
    parsed = parse_hhmm(text)
    if not parsed:
        await update.message.reply_text(
            "時間格式有誤，請輸入四位數字（24小時制），例如 1701。"
        )
        return SD_TIME

    hour, minute = parsed
    context.user_data["sd_time"] = (hour, minute)

    keyboard = [
        [InlineKeyboardButton("⬅️ 修改時間", callback_data="back_to_time")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "請輸入提醒內容。",
        reply_markup=markup,
    )
    return SD_TEXT


# ========= 單一日期 flow：內容層 =========

async def single_date_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """收到提醒內容，建立排程（不顯示內容本身，避免洗頻）"""
    content = (update.message.text or "").strip()
    if not content:
        await update.message.reply_text("提醒內容不能是空的，請再輸入一次。")
        return SD_TEXT

    month, day = context.user_data.get("sd_date", (None, None))
    hour, minute = context.user_data.get("sd_time", (None, None))

    if month is None or day is None or hour is None or minute is None:
        await update.message.reply_text("內部資料遺失，請重新從 /start 開始設定一次 🙏")
        return MENU

    now = datetime.now(TZ)
    year = now.year
    run_at = datetime(year, month, day, hour, minute, tzinfo=TZ)

    # 如果時間已經過了，預設往下一年
    if run_at <= now:
        run_at = datetime(year + 1, month, day, hour, minute, tzinfo=TZ)

    when_str = run_at.strftime("%m/%d %H:%M")

    chat_id = update.effective_chat.id

    try:
        # 先寫進 DB
        reminder_id = db_add_reminder(chat_id, "general_single", run_at, content)

        # 再建立提醒 Job，name 綁 reminder_id，之後刪除用
        job_name = f"reminder-{reminder_id}"
        context.application.job_queue.run_once(
            reminder_job,
            when=run_at.astimezone(TZ),
            data={
                "chat_id": chat_id,
                "text": content,
                "when_str": when_str,
                "reminder_id": reminder_id,
            },
            name=job_name,
        )

        await update.message.reply_text(f"✅ 已記錄 {when_str} 提醒")

    except Exception as e:
        logger.exception("建立單一日期提醒 job 失敗：%s", e)
        await update.message.reply_text("建立提醒時發生錯誤，麻煩稍後再試一次 🙏")
        return MENU

    # 回主選單
    await send_main_menu(
        update.effective_chat.id,
        context,
        "還需要我幫你設什麼提醒嗎？",
    )
    return MENU


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

            # ConversationHandler：包含整個主選單 + 一般提醒 ➜ 單一日期 flow + 提醒列表
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

            # 初始化 & 啟動 bot
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
            logger.warning("Telegram API TimedOut，5 秒後重試啟動 bot。")
            await asyncio.sleep(5)

        except Exception as e:
            logger.exception("run_bot 發生未預期錯誤：%s，30 秒後重試。", e)
            await asyncio.sleep(30)


# ========= FastAPI lifecycle =========

@app.on_event("startup")
async def on_startup():
    logger.info("Startup event: creating Telegram bot task.")
    init_db()
    asyncio.create_task(run_bot())


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("FastAPI app is shutting down.")
