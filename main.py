import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

# Conversation 狀態
(
    MENU,                 # 主選單
    GENERAL_MENU,         # 一般提醒選單
    SD_DATE,              # 單一日期：輸入日期
    SD_TIME,              # 單一日期：輸入時間
    SD_TEXT,              # 單一日期：輸入內容
    REMINDER_LIST,        # 所有提醒列表
    PEOPLE_MENU,          # 人員名單編輯：選單
    PEOPLE_ADD,           # 人員名單編輯：新增
    PEOPLE_DELETE,        # 人員名單編輯：刪除
    APK_WEEKDAY,          # APK 提醒：選擇每週幾
    APK_TIME,             # APK 提醒：輸入時間
    APK_TEXT,             # APK 提醒：輸入內容
) = range(12)


# ========= SQLite 工具 =========

def init_db():
    """初始化 SQLite 資料庫。"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 提醒表：一般提醒 / APK / 六合彩
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

    # 人員名單表：可被 @ 的人
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS people (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id  INTEGER NOT NULL,
            tg_id    TEXT    NOT NULL,   -- 例如 @tohu54520
            nickname TEXT    NOT NULL    -- 例如 豆腐
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


def db_list_people(chat_id: int):
    """列出某個聊天室目前所有可 @ 的人員名單。"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, tg_id, nickname FROM people WHERE chat_id=? ORDER BY id ASC",
        (chat_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def db_add_people_batch(chat_id: int, pairs):
    """
    批次新增多筆人員名單。
    pairs: List[(tg_id, nickname)]
    回傳實際新增的筆數。
    """
    if not pairs:
        return 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO people (chat_id, tg_id, nickname) VALUES (?, ?, ?)",
        [(chat_id, tg, nick) for tg, nick in pairs],
    )
    inserted = cur.rowcount
    conn.commit()
    conn.close()
    return inserted


def db_delete_person(person_id: int):
    """刪除單一人員名單。"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM people WHERE id=?", (person_id,))
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


def build_apk_weekday_keyboard(selected):
    """
    建立 APK 每週幾選單的 keyboard。
    selected: set[int] 0=一, 6=日
    """
    names = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    buttons = []
    # 排成 3 + 3 + 1
    rows_idx = [
        [0, 1, 2],
        [3, 4, 5],
        [6],
    ]
    for row in rows_idx:
        row_btns = []
        for idx in row:
            label = names[idx]
            if idx in selected:
                label = "✅ " + label
            row_btns.append(
                InlineKeyboardButton(label, callback_data=f"apk_wd_{idx}")
            )
        buttons.append(row_btns)

    buttons.append(
        [InlineKeyboardButton("下一步：設定時間", callback_data="apk_next_time")]
    )
    buttons.append(
        [InlineKeyboardButton("⬅️ 返回主選單", callback_data="apk_back_main")]
    )

    return InlineKeyboardMarkup(buttons)


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


async def send_people_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """發送【人員名單編輯】子選單。"""
    keyboard = [
        [
            InlineKeyboardButton("新增", callback_data="people_add"),
            InlineKeyboardButton("刪除", callback_data="people_delete"),
        ],
        [InlineKeyboardButton("⬅️ 返回主選單", callback_data="people_back_main")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        chat_id=chat_id,
        text="【人員名單編輯】請選擇操作：",
        reply_markup=markup,
    )


async def send_apk_weekday_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """發送【谷歌APK提醒】的每週幾選單。"""
    selected = context.user_data.get("apk_weekdays", set())
    if not isinstance(selected, set):
        selected = set()
        context.user_data["apk_weekdays"] = selected

    markup = build_apk_weekday_keyboard(selected)
    await context.bot.send_message(
        chat_id=chat_id,
        text="【谷歌APK提醒】\n請選擇每週要提醒的星期（可複選）：",
        reply_markup=markup,
    )


# ========= JobQueue：提醒任務 =========

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    when_str = data.get("when_str", "")
    reminder_id = data.get("reminder_id")

    kind = None
    run_at_ts = None
    db_text = None

    if reminder_id is not None:
        row = db_get_reminder(reminder_id)
        if row:
            _id, chat_id_db, kind, run_at_ts, db_text = row
            # 以 DB 裡的 chat_id 為主
            chat_id = chat_id_db or chat_id

    # 構造要送出的文字
    final_text = ""
    if kind == "apk" and run_at_ts is not None:
        dt = datetime.fromtimestamp(run_at_ts, TZ)
        mmdd = dt.strftime("%m/%d")
        weekday_index = dt.weekday()  # 0=一 ... 6=日
        zh_week = ["一", "二", "三", "四", "五", "六", "日"][weekday_index]
        prefix = f"【谷歌】【PROD】本周{zh_week}APK更新-紀錄單"
        if db_text:
            final_text = f"{prefix}\n{db_text}"
        else:
            final_text = prefix
    else:
        # 一般提醒或沒有 kind 資訊，就直接用 DB 內容或 data.text
        final_text = db_text or data.get("text", f"⏰ 提醒時間到囉（{when_str}）")

    if chat_id is None:
        logger.warning("reminder_job: chat_id 為 None，略過發送。")
        return

    await context.bot.send_message(chat_id=chat_id, text=final_text)

    # 若是 APK 提醒，建立下一週同時間的提醒（週期性）
    if reminder_id is not None and kind == "apk" and run_at_ts is not None:
        old_dt = datetime.fromtimestamp(run_at_ts, TZ)
        next_dt = old_dt + timedelta(days=7)

        try:
            next_id = db_add_reminder(chat_id, "apk", next_dt, db_text or "")
            next_when_str = next_dt.strftime("%m/%d %H:%M")

            context.application.job_queue.run_once(
                reminder_job,
                when=next_dt.astimezone(TZ),
                data={
                    "chat_id": chat_id,
                    "when_str": next_when_str,
                    "reminder_id": next_id,
                },
                name=f"reminder-{next_id}",
            )
        except Exception as e:
            logger.exception("建立下一週 APK 提醒失敗：%s", e)

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


# ========= 人員名單編輯 =========

async def people_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理『人員名單編輯』選單相關 callback（不含刪除列表內點選）。"""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    # 從其他地方回到人員名單主選單
    if data in ("menu_people", "people_menu"):
        await send_people_menu(chat_id, context)
        return PEOPLE_MENU

    if data == "people_back_main":
        await send_main_menu(chat_id, context)
        return MENU

    # 進入「新增」模式
    if data == "people_add":
        text = (
            "【人員名單編輯 ➜ 新增】\n"
            "請輸入要新增的 TG 名單，每行一位，格式為：\n"
            "    @TG_ID 暱稱\n"
            "例如：\n"
            "    @tohu12345 豆腐\n"
            "    @tohu54321 島湖\n\n"
            "你可以一次貼很多行，我會幫你批量新增。"
        )
        keyboard = [
            [InlineKeyboardButton("⬅️ 返回人員名單編輯", callback_data="people_menu")],
        ]
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return PEOPLE_ADD

    return PEOPLE_MENU


async def people_add_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    在 PEOPLE_ADD 狀態下收到文字：
    解析使用者貼上的多行 @TG_ID 暱稱，直接寫入 DB。
    """
    chat_id = update.effective_chat.id
    raw = (update.message.text or "").strip()
    if not raw:
        await update.message.reply_text("內容是空的，請輸入 @TG_ID 暱稱，每行一位。")
        return PEOPLE_ADD

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    pairs = []

    for line in lines:
        # 期待格式：@tgid 暱稱
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            # 略過格式不正確的那幾行
            continue
        tg_id, nickname = parts
        if not tg_id.startswith("@"):
            continue
        pairs.append((tg_id, nickname.strip()))

    if not pairs:
        await update.message.reply_text("沒有找到合法的『@TG_ID 暱稱』格式，請再試一次。")
        return PEOPLE_ADD

    inserted = db_add_people_batch(chat_id, pairs)

    lines_out = [f"✅ 已新增 {inserted} 筆名單。"]
    for tg, nick in pairs:
        lines_out.append(f"    {tg} {nick}")

    await update.message.reply_text("\n".join(lines_out))
    # 保持在 PEOPLE_ADD 狀態，可以繼續貼下一批，或按「返回人員名單編輯」
    return PEOPLE_ADD


async def people_delete_show_list(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """顯示目前所有人員名單，讓使用者點選刪除。"""
    rows = db_list_people(chat_id)
    if not rows:
        keyboard = [
            [InlineKeyboardButton("⬅️ 返回人員名單編輯", callback_data="people_menu")],
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="people_back_main")],
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text="【人員名單編輯 ➜ 刪除】\n目前沒有任何名單可以刪除～",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    keyboard = []
    for pid, tg_id, nickname in rows:
        label = f"{nickname} {tg_id}"
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"people_del_{pid}")]
        )

    keyboard.append(
        [InlineKeyboardButton("⬅️ 返回人員名單編輯", callback_data="people_menu")]
    )
    keyboard.append(
        [InlineKeyboardButton("⬅️ 返回主選單", callback_data="people_back_main")]
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text="【人員名單編輯 ➜ 刪除】\n請點選要刪除的人員：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def people_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理刪除名單相關的 callback。"""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "people_delete":
        # 從選單進來：顯示列表
        await people_delete_show_list(chat_id, context)
        return PEOPLE_DELETE

    if data == "people_menu":
        await send_people_menu(chat_id, context)
        return PEOPLE_MENU

    if data == "people_back_main":
        await send_main_menu(chat_id, context)
        return MENU

    if data.startswith("people_del_"):
        pid = int(data.split("_")[-1])
        db_delete_person(pid)
        await query.message.reply_text("✅ 已刪除這位人員。")
        # 刪完後重新顯示列表
        await people_delete_show_list(chat_id, context)
        return PEOPLE_DELETE

    return PEOPLE_DELETE


# ========= APK 提醒 Flow =========

async def apk_weekday_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 APK 提醒：選擇每週幾的 callback。"""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    selected = context.user_data.get("apk_weekdays", set())
    if not isinstance(selected, set):
        selected = set()
        context.user_data["apk_weekdays"] = selected

    if data == "apk_back_main":
        await send_main_menu(chat_id, context)
        return MENU

    if data.startswith("apk_wd_"):
        idx = int(data.split("_")[-1])
        if idx in selected:
            selected.remove(idx)
        else:
            selected.add(idx)

        # 重新發一次選單（簡單做法：新訊息，不改舊訊息）
        markup = build_apk_weekday_keyboard(selected)
        await query.message.reply_text(
            "【谷歌APK提醒】\n請選擇每週要提醒的星期（可複選）：",
            reply_markup=markup,
        )
        return APK_WEEKDAY

    if data == "apk_next_time":
        if not selected:
            # 至少要選一個星期
            await query.answer("請至少勾選一個星期。", show_alert=True)
            return APK_WEEKDAY

        # 進入輸入時間
        keyboard = [
            [InlineKeyboardButton("⬅️ 返回選擇星期", callback_data="apk_back_weekday")],
        ]
        await query.message.reply_text(
            "請輸入時間四位數字（24小時制例如1701）。",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return APK_TIME

    return APK_WEEKDAY


async def apk_back_to_weekday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """從時間層回到選擇每週幾。"""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await send_apk_weekday_menu(chat_id, context)
    return APK_WEEKDAY


async def apk_got_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """APK 提醒：收到 HHMM。"""
    text = update.message.text.strip()
    parsed = parse_hhmm(text)
    if not parsed:
        await update.message.reply_text(
            "時間格式有誤，請輸入四位數字（24小時制），例如 1701。"
        )
        return APK_TIME

    hour, minute = parsed
    context.user_data["apk_time"] = (hour, minute)

    keyboard = [
        [InlineKeyboardButton("⬅️ 修改時間", callback_data="apk_back_time")],
    ]
    await update.message.reply_text(
        "請輸入提醒內容。",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return APK_TEXT


async def apk_back_to_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """從內容層回到輸入時間。"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("⬅️ 返回選擇星期", callback_data="apk_back_weekday")],
    ]
    await query.message.reply_text(
        "請輸入時間四位數字（24小時制例如1701）。",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return APK_TIME


def calc_next_datetime_for_weekday(target_wd: int, hour: int, minute: int) -> datetime:
    """
    計算「從現在開始」下一次落在指定 weekday + 時間的 datetime。
    target_wd: 0=週一 ... 6=週日
    """
    now = datetime.now(TZ)
    today_wd = now.weekday()  # 0=週一
    days_ahead = (target_wd - today_wd) % 7

    candidate = datetime(
        now.year, now.month, now.day, hour, minute, tzinfo=TZ
    ) + timedelta(days=days_ahead)

    if candidate <= now:
        candidate += timedelta(days=7)

    return candidate


async def apk_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """APK 提醒：收到自訂提醒內容，建立排程（週期性，每週一次）。"""
    chat_id = update.effective_chat.id
    content = (update.message.text or "").strip()
    if not content:
        await update.message.reply_text("提醒內容不能是空的，請再輸入一次。")
        return APK_TEXT

    weekdays = context.user_data.get("apk_weekdays", set())
    if not weekdays:
        await update.message.reply_text("內部資料遺失（未選擇星期），請重新從 /start 開始設定一次 🙏")
        return MENU

    hour_min = context.user_data.get("apk_time")
    if not hour_min:
        await update.message.reply_text("內部資料遺失（未設定時間），請重新從 /start 開始設定一次 🙏")
        return MENU

    hour, minute = hour_min

    created_times = []

    for wd in sorted(weekdays):
        run_at = calc_next_datetime_for_weekday(wd, hour, minute)
        when_str = run_at.strftime("%m/%d %H:%M")

        try:
            reminder_id = db_add_reminder(chat_id, "apk", run_at, content)
            job_name = f"reminder-{reminder_id}"

            context.application.job_queue.run_once(
                reminder_job,
                when=run_at.astimezone(TZ),
                data={
                    "chat_id": chat_id,
                    "when_str": when_str,
                    "reminder_id": reminder_id,
                },
                name=job_name,
            )
            created_times.append(when_str)
        except Exception as e:
            logger.exception("建立 APK 提醒 job 失敗：%s", e)

    if created_times:
        lines = ["✅ 已建立以下 APK 提醒（每週循環）："]
        for t in created_times:
            lines.append(f" - {t}")
        await update.message.reply_text("\n".join(lines))
    else:
        await update.message.reply_text("建立 APK 提醒時發生錯誤，麻煩稍後再試一次 🙏")

    # 清掉暫存
    context.user_data.pop("apk_weekdays", None)
    context.user_data.pop("apk_time", None)

    # 回主選單
    await send_main_menu(
        chat_id,
        context,
        "還需要我幫你設什麼提醒嗎？",
    )
    return MENU


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

    if data == "menu_people":
        await send_people_menu(chat_id, context)
        return PEOPLE_MENU

    if data == "menu_apk":
        # 進入 APK 提醒 flow：先選每週幾
        context.user_data["apk_weekdays"] = set()
        context.user_data.pop("apk_time", None)
        await send_apk_weekday_menu(chat_id, context)
        return APK_WEEKDAY

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

            # ConversationHandler：包含整個主選單 + 一般提醒 + 提醒列表 + 人員名單 + APK 提醒
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
                    PEOPLE_MENU: [
                        CallbackQueryHandler(people_menu_callback, pattern="^menu_people$|^people_"),
                    ],
                    PEOPLE_ADD: [
                        CallbackQueryHandler(people_menu_callback, pattern="^people_"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, people_add_got_text),
                    ],
                    PEOPLE_DELETE: [
                        CallbackQueryHandler(people_delete_callback, pattern="^people_"),
                    ],
                    APK_WEEKDAY: [
                        CallbackQueryHandler(apk_weekday_callback, pattern="^apk_"),
                    ],
                    APK_TIME: [
                        CallbackQueryHandler(apk_back_to_weekday, pattern="^apk_back_weekday$"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, apk_got_time),
                    ],
                    APK_TEXT: [
                        CallbackQueryHandler(apk_back_to_time, pattern="^apk_back_time$"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, apk_got_text),
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


# ========= Background Worker 入口點 =========

async def main():
    logger.info("Worker starting, init DB and bot...")
    init_db()
    await run_bot()


if __name__ == "__main__":
    asyncio.run(main())
