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

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TZ = ZoneInfo("Asia/Taipei")

DB_PATH = "reminders.db"     # 提醒資料庫
PEOPLE_DB = "people.db"     # 人員名單資料庫

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("main")

app = FastAPI()

# ========= Conversation 狀態 =========

(
    MENU,                 # 主選單
    GENERAL_MENU,         # 一般提醒選單
    SD_DATE,              # 單一日期：輸入日期
    SD_TIME,              # 單一日期：輸入時間
    SD_TEXT,              # 單一日期：輸入內容

    REMINDER_LIST,        # 所有提醒列表

    PEOPLE_MENU,          # 人員名單選單
    PEOPLE_ADD,           # 人員名單 ➜ 新增
    PEOPLE_DELETE,        # 人員名單 ➜ 刪除
) = range(8)

# ========= FastAPI =========

@app.get("/")
async def root():
    return {"status": "ok"}

# ========= SQLite 初始化 =========

def init_db():
    # 提醒資料表
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            run_at INTEGER NOT NULL,
            text TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    # 人員名單資料表
    conn = sqlite3.connect(PEOPLE_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            tg_id TEXT NOT NULL,
            nickname TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    logger.info("✅ DB 初始化完成")

# ========= 人員名單 DB 操作 =========

def db_add_people(chat_id: int, tg_id: str, nickname: str):
    conn = sqlite3.connect(PEOPLE_DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO people (chat_id, tg_id, nickname) VALUES (?, ?, ?)",
        (chat_id, tg_id, nickname)
    )
    conn.commit()
    conn.close()

def db_get_people(chat_id: int):
    conn = sqlite3.connect(PEOPLE_DB)
    cur = conn.cursor()
    cur.execute("SELECT id, tg_id, nickname FROM people WHERE chat_id=?", (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def db_delete_people(pid: int):
    conn = sqlite3.connect(PEOPLE_DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM people WHERE id=?", (pid,))
    conn.commit()
    conn.close()

# ========= 提醒 DB 操作 =========

def db_add_reminder(chat_id: int, kind: str, run_at: datetime, text: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reminders (chat_id, kind, run_at, text) VALUES (?, ?, ?, ?)",
        (chat_id, kind, int(run_at.timestamp()), text),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid

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

def db_get_reminder(rid: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, chat_id, kind, run_at, text FROM reminders WHERE id=?",
        (rid,),
    )
    row = cur.fetchone()
    conn.close()
    return row

def db_delete_reminder(rid: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM reminders WHERE id=?", (rid,))
    conn.commit()
    conn.close()

# ========= 工具函式 =========

def parse_mmdd(text: str):
    if len(text) != 4 or not text.isdigit():
        return None
    m, d = int(text[:2]), int(text[2:])
    try:
        datetime(2000, m, d)
    except:
        return None
    return m, d

def parse_hhmm(text: str):
    if len(text) != 4 or not text.isdigit():
        return None
    h, m = int(text[:2]), int(text[2:])
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None

def format_ts(ts: int):
    dt = datetime.fromtimestamp(ts, TZ)
    return dt.strftime("%m/%d %H:%M")

# ✅ 主選單

async def send_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("一般提醒", callback_data="menu_general")],
        [InlineKeyboardButton("谷歌APK提醒", callback_data="menu_apk")],
        [InlineKeyboardButton("香港六合彩", callback_data="menu_lottery")],
        [InlineKeyboardButton("人員名單編輯", callback_data="menu_people")],
        [InlineKeyboardButton("所有提醒列表", callback_data="menu_list")],
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text="請選擇功能：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# ========= 提醒觸發 Job =========

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

    if reminder_id:
        db_delete_reminder(reminder_id)


# ========= /start 指令 =========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_main_menu(update.effective_chat.id, context)
    return MENU


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start 主選單")


# ========= 所有提醒列表 =========

async def send_reminder_list(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    rows = db_list_reminders(chat_id)

    if not rows:
        await context.bot.send_message(
            chat_id=chat_id,
            text="【所有提醒列表】\n目前沒有任何提醒。",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅ 返回主選單", callback_data="reminder_back_main")]]
            ),
        )
        return

    keyboard = []
    for rid, kind, run_at, text in rows:
        when_str = format_ts(run_at)
        label = f"{when_str}｜{kind}"
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"reminder_{rid}")]
        )

    keyboard.append(
        [InlineKeyboardButton("⬅ 返回主選單", callback_data="reminder_back_main")]
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text="【所有提醒列表】",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def reminder_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "reminder_back_main":
        await send_main_menu(chat_id, context)
        return MENU

    if data.startswith("reminder_delete_"):
        rid = int(data.split("_")[-1])
        db_delete_reminder(rid)

        jobs = context.application.job_queue.get_jobs_by_name(f"reminder-{rid}")
        for j in jobs:
            j.schedule_removal()

        await query.message.reply_text("✅ 已刪除提醒")
        await send_reminder_list(chat_id, context)
        return REMINDER_LIST

    if data.startswith("reminder_"):
        rid = int(data.split("_")[-1])
        row = db_get_reminder(rid)
        if not row:
            await query.message.reply_text("提醒不存在")
            return REMINDER_LIST

        _, _, kind, run_at, text = row
        when_str = format_ts(run_at)

        keyboard = [
            [InlineKeyboardButton("🗑 刪除", callback_data=f"reminder_delete_{rid}")],
            [InlineKeyboardButton("⬅ 返回列表", callback_data="menu_list")],
        ]

        await query.message.reply_text(
            f"【提醒詳細】\n{when_str}\n{text}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return REMINDER_LIST


# ========= 一般提醒 UI =========

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = q.message.chat_id
    d = q.data

    if d == "menu_general":
        kb = [
            [
                InlineKeyboardButton("單一日期", callback_data="general_single"),
                InlineKeyboardButton("固定週期(未開放)", callback_data="general_cycle"),
            ],
            [InlineKeyboardButton("⬅ 返回主選單", callback_data="general_back")],
        ]
        await q.message.reply_text(
            "【一般提醒】請選擇：",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return GENERAL_MENU

    if d == "menu_list":
        await send_reminder_list(cid, context)
        return REMINDER_LIST

    if d == "menu_people":
        kb = [
            [InlineKeyboardButton("新增", callback_data="people_add")],
            [InlineKeyboardButton("刪除", callback_data="people_delete")],
            [InlineKeyboardButton("⬅ 返回主選單", callback_data="people_back")],
        ]
        await q.message.reply_text(
            "【人員名單編輯】",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return PEOPLE_MENU

    await send_main_menu(cid, context)
    return MENU


async def general_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == "general_back":
        await send_main_menu(q.message.chat_id, context)
        return MENU

    if d == "general_single":
        text = "【一般提醒 ➜ 單一日期】\n請輸入日期四位數字(例如：1201 代表 12/01)。"
        kb = [[InlineKeyboardButton("⬅ 返回上一頁", callback_data="general_back")]]
        await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
        return SD_DATE

    return GENERAL_MENU


async def single_date_got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_mmdd(update.message.text)
    if not parsed:
        await update.message.reply_text("格式錯誤，請輸入四位數字")
        return SD_DATE

    context.user_data["sd_date"] = parsed
    await update.message.reply_text("請輸入時間四位數字(24小時制例如1701)。")
    return SD_TIME


async def single_date_got_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_hhmm(update.message.text)
    if not parsed:
        await update.message.reply_text("時間格式錯誤")
        return SD_TIME

    context.user_data["sd_time"] = parsed
    await update.message.reply_text("請輸入提醒內容。")
    return SD_TEXT


async def single_date_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    content = update.message.text.strip()
    (m, d) = context.user_data["sd_date"]
    (h, mi) = context.user_data["sd_time"]

    now = datetime.now(TZ)
    run_at = datetime(now.year, m, d, h, mi, tzinfo=TZ)
    if run_at <= now:
        run_at = datetime(now.year + 1, m, d, h, mi, tzinfo=TZ)

    when_str = run_at.strftime("%m/%d %H:%M")
    chat_id = update.effective_chat.id

    rid = db_add_reminder(chat_id, "general_single", run_at, content)

    context.application.job_queue.run_once(
        reminder_job,
        run_at,
        data={
            "chat_id": chat_id,
            "text": content,
            "when_str": when_str,
            "reminder_id": rid,
        },
        name=f"reminder-{rid}",
    )

    await update.message.reply_text(f"✅ 已記錄 {when_str} 提醒")
    await send_main_menu(chat_id, context)
    return MENU

# ========= 人員名單 DB =========

def init_people_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            tg_id TEXT NOT NULL,
            nickname TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def db_add_person(chat_id: int, tg_id: str, nickname: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO people (chat_id, tg_id, nickname) VALUES (?, ?, ?)",
        (chat_id, tg_id, nickname),
    )
    conn.commit()
    conn.close()


def db_list_people(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, tg_id, nickname FROM people WHERE chat_id=?",
        (chat_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def db_delete_person(pid: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM people WHERE id=?", (pid,))
    conn.commit()
    conn.close()


# ========= 人員名單 UI =========

async def people_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    keyboard = [
        [InlineKeyboardButton("新增", callback_data="people_add")],
        [InlineKeyboardButton("刪除", callback_data="people_delete")],
        [InlineKeyboardButton("⬅ 返回主選單", callback_data="people_back_main")],
    ]

    await q.message.reply_text(
        "【人員名單編輯】",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PEOPLE_MENU


async def people_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    text = (
        "【人員名單編輯 ➜ 新增】\n"
        "請輸入要新增的 TG 名單，每行一位，格式為：\n"
        "    @TG_ID 暱稱\n"
        "例如：\n"
        "    @tohu12345 豆腐\n"
        "    @tohu54321 島湖"
    )

    await q.message.reply_text(text)
    return PEOPLE_ADD


async def people_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lines = update.message.text.strip().splitlines()
    count = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        try:
            tg_id, nickname = line.split(maxsplit=1)
            db_add_person(chat_id, tg_id, nickname)
            count += 1
        except:
            continue

    await update.message.reply_text(f"✅ 已新增完成（{count} 筆）")
    await send_main_menu(chat_id, context)
    return MENU


async def people_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    rows = db_list_people(chat_id)
    if not rows:
        await q.message.reply_text("目前沒有任何人員。")
        return PEOPLE_MENU

    keyboard = []
    for pid, tg_id, nickname in rows:
        label = f"{nickname} ({tg_id})"
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"people_delete_{pid}")]
        )

    keyboard.append(
        [InlineKeyboardButton("⬅ 返回人員名單", callback_data="people_back_people")]
    )

    await q.message.reply_text(
        "請點選要刪除的人員：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PEOPLE_MENU


async def people_delete_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split("_")[-1])

    db_delete_person(pid)
    await q.message.reply_text("✅ 已刪除")

    return await people_delete_menu(update, context)


# ========= Bot 啟動 =========

async def run_bot():
    while True:
        try:
            request = HTTPXRequest(
                read_timeout=30.0,
                connect_timeout=10.0,
                pool_timeout=10.0,
            )

            application = (
                ApplicationBuilder()
                .token(TG_BOT_TOKEN)
                .request(request)
                .job_queue()
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
                        MessageHandler(filters.TEXT & ~filters.COMMAND, single_date_got_date),
                    ],
                    SD_TIME: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, single_date_got_time),
                    ],
                    SD_TEXT: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, single_date_got_text),
                    ],
                    REMINDER_LIST: [
                        CallbackQueryHandler(reminder_list_callback),
                    ],
                    PEOPLE_MENU: [
                        CallbackQueryHandler(people_menu_callback, pattern="^menu_people$"),
                        CallbackQueryHandler(people_add_start, pattern="^people_add$"),
                        CallbackQueryHandler(people_delete_menu, pattern="^people_delete$"),
                        CallbackQueryHandler(people_delete_action, pattern="^people_delete_"),
                        CallbackQueryHandler(lambda u,c: send_main_menu(u.effective_chat.id,c), pattern="people_back_main"),
                    ],
                    PEOPLE_ADD: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, people_add_input),
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

            while True:
                await asyncio.sleep(3600)

        except Exception as e:
            logger.exception("Bot crash，30 秒後重啟：%s", e)
            await asyncio.sleep(30)


# ========= FastAPI lifecycle =========

@app.on_event("startup")
async def on_startup():
    init_db()
    init_people_db()
    asyncio.create_task(run_bot())


@app.on_event("shutdown")
async def on_shutdown():
    pass
