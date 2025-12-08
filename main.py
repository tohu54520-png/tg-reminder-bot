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
TZ = ZoneInfo("Asia/Taipei")
DB_PATH = "reminders.db"

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
    PEOPLE_MENU,          # 人員名單編輯主畫面
    PEOPLE_ADD_INPUT,     # 人員名單編輯：輸入批量名單
) = range(7)

# ========= DB 初始化 =========


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 提醒資料
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            kind    TEXT    NOT NULL,
            run_at  INTEGER NOT NULL,  -- Unix timestamp
            text    TEXT    NOT NULL
        )
        """
    )

    # 可被設為 @ 目標的人員名單
    # handle = TG ID / 使用者名稱（例如 @tohu54520 或純文字 ID）
    # alias  = 顯示的小名，方便之後刪除、辨識
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mention_targets (
            chat_id INTEGER NOT NULL,
            handle  TEXT    NOT NULL,
            alias   TEXT    NOT NULL,
            PRIMARY KEY (chat_id, handle)
        )
        """
    )

    conn.commit()
    conn.close()


init_db()

# ========= FastAPI 路由 =========


@app.get("/")
async def root():
    return {"status": "ok"}


# ========= 小工具 =========


def parse_mmdd(text: str):
    """解析 MMDD，回傳 (month, day) 或 None。"""
    text = text.strip()
    if len(text) != 4 or not text.isdigit():
        return None
    month = int(text[:2])
    day = int(text[2:])
    try:
        datetime(2000, month, day)
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


async def send_main_menu(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, text: str = "請選擇功能："
):
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
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ 提醒時間到囉（{when_str}）：\n{text}",
    )


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
                InlineKeyboardButton("單一日期", callback_data="general_single"),
                InlineKeyboardButton("固定週期（尚未實作）", callback_data="general_cycle"),
            ],
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="general_back")],
        ]
        markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("【一般提醒】請選擇類型：", reply_markup=markup)
        return GENERAL_MENU

    elif data == "menu_people":
        # 人員名單編輯主畫面
        keyboard = [
            [
                InlineKeyboardButton("新增", callback_data="people_add_manual"),
                InlineKeyboardButton("刪除", callback_data="people_delete_menu"),
            ],
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="people_back_main")],
        ]
        markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("【人員名單編輯】請選擇操作：", reply_markup=markup)
        return PEOPLE_MENU

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

    # 如果時間已經過了，就排到下一年
    if run_at <= now:
        run_at = datetime(year + 1, month, day, hour, minute, tzinfo=TZ)

    when_str = run_at.strftime("%m/%d %H:%M")

    # 存進資料庫
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO reminders (chat_id, kind, run_at, text)
                VALUES (?, ?, ?, ?)
                """,
                (update.effective_chat.id, "general_single", int(run_at.timestamp()), content),
            )
            reminder_id = cur.lastrowid
            conn.commit()
    except Exception:
        logger.exception("寫入提醒資料庫失敗")
        await update.message.reply_text("建立提醒時發生錯誤，麻煩稍後再試一次 🙏")
        return MENU

    # 建立 JobQueue
    job_queue = context.application.job_queue
    if job_queue is None:
        logger.error("JobQueue is None; cannot schedule job.")
        await update.message.reply_text("內部錯誤：JobQueue 未啟用，請稍後再試一次 🙏")
        return MENU

    job_queue.run_once(
        reminder_job,
        when=run_at,
        data={
            "chat_id": update.effective_chat.id,
            "text": content,
            "when_str": when_str,
        },
        name=f"single-{update.effective_chat.id}-{reminder_id}",
    )

    await update.message.reply_text(f"✅ 已記錄 {when_str} 提醒")

    await send_main_menu(
        update.effective_chat.id,
        context,
        "還需要我幫你設什麼提醒嗎？",
    )
    return MENU


# ========= 人員名單編輯 =========


async def people_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理人員名單編輯相關 callback。"""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    # 從人員名單編輯回主選單
    if data == "people_back_main":
        await send_main_menu(chat_id, context)
        return MENU

    # 進入「新增」輸入模式
    if data == "people_add_manual":
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ 完成新增 / 返回", callback_data="people_add_done"
                )
            ]
        ]
        markup = InlineKeyboardMarkup(keyboard)
        text = (
            "【人員名單編輯 ➜ 新增】\n"
            "請輸入要新增的 TG 名單，每行一位，格式為：\n"
            "    @TG_ID 暱稱\n"
            "例如：\n"
            "    @tohu54520 豆腐\n"
            "    @tohu51234 豆渣\n\n"
            "你可以一次貼很多行，我會幫你批量新增。\n"
            "若輸入完畢，請按下下面的「✅ 完成新增 / 返回」。"
        )
        await query.message.reply_text(text, reply_markup=markup)
        return PEOPLE_ADD_INPUT

    # 從新增模式返回「人員名單編輯」主畫面
    if data == "people_add_done":
        keyboard = [
            [
                InlineKeyboardButton("新增", callback_data="people_add_manual"),
                InlineKeyboardButton("刪除", callback_data="people_delete_menu"),
            ],
            [InlineKeyboardButton("⬅️ 返回主選單", callback_data="people_back_main")],
        ]
        markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("【人員名單編輯】請選擇操作：", reply_markup=markup)
        return PEOPLE_MENU

    # 顯示目前名單，供刪除
    if data == "people_delete_menu":
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT handle, alias
                    FROM mention_targets
                    WHERE chat_id = ?
                    ORDER BY alias
                    """,
                    (chat_id,),
                )
                rows = cur.fetchall()
        except Exception:
            logger.exception("people_delete_menu 查詢失敗")
            await query.message.reply_text("查詢名單失敗，請稍後再試一次 🙏")
            return PEOPLE_MENU

        if not rows:
            await query.message.reply_text("目前可設置 @ 的人員名單是空的。")
            return PEOPLE_MENU

        keyboard = []
        row_buttons = []
        for handle, alias in rows:
            row_buttons.append(
                InlineKeyboardButton(
                    alias, callback_data=f"people_del_sel:{handle}"
                )
            )
            if len(row_buttons) == 2:
                keyboard.append(row_buttons)
                row_buttons = []
        if row_buttons:
            keyboard.append(row_buttons)
        keyboard.append(
            [InlineKeyboardButton("⬅️ 返回", callback_data="people_add_done")]
        )

        markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            "點選要從名單中移除的人：", reply_markup=markup
        )
        return PEOPLE_MENU

    # 實際刪除單一成員
    if data.startswith("people_del_sel:"):
        handle = data.split(":", 1)[1]

        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT alias FROM mention_targets
                    WHERE chat_id = ? AND handle = ?
                    """,
                    (chat_id, handle),
                )
                row = cur.fetchone()
                if not row:
                    await query.message.reply_text("名單中已無此人。")
                    return PEOPLE_MENU
                alias = row[0]

                cur.execute(
                    """
                    DELETE FROM mention_targets
                    WHERE chat_id = ? AND handle = ?
                    """,
                    (chat_id, handle),
                )
                conn.commit()
        except Exception:
            logger.exception("people_del_sel 失敗")
            await query.message.reply_text("刪除失敗，請稍後再試一次 🙏")
            return PEOPLE_MENU

        await query.message.reply_text(f"已將「{alias}」自名單中移除。")
        return PEOPLE_MENU

    return PEOPLE_MENU


async def people_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """在 PEOPLE_ADD_INPUT 狀態下，處理使用者貼上的批量名單。"""
    chat_id = update.effective_chat.id
    raw = (update.message.text or "").strip()

    if not raw:
        await update.message.reply_text("沒讀到任何文字，請再貼一次名單哦。")
        return PEOPLE_ADD_INPUT

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    success_count = 0
    error_lines = []

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            for line in lines:
                parts = line.split(maxsplit=1)
                if len(parts) < 2:
                    error_lines.append(line)
                    continue

                handle = parts[0].strip()
                alias = parts[1].strip()

                if not handle:
                    error_lines.append(line)
                    continue

                if not handle.startswith("@"):
                    handle = "@" + handle

                if not alias:
                    error_lines.append(line)
                    continue

                cur.execute(
                    """
                    INSERT OR REPLACE INTO mention_targets (chat_id, handle, alias)
                    VALUES (?, ?, ?)
                    """,
                    (chat_id, handle, alias),
                )
                success_count += 1

            conn.commit()
    except Exception:
        logger.exception("people_add_input 寫入失敗")
        await update.message.reply_text("寫入名單時發生錯誤，請稍後再試一次 🙏")
        return PEOPLE_ADD_INPUT

    msg_parts = []
    if success_count > 0:
        msg_parts.append(f"✅ 已新增 {success_count} 筆名單。")
    if error_lines:
        msg_parts.append(
            "以下這些行格式不正確（應該是：@TG_ID 暱稱），沒有被新增：\n"
            + "\n".join(error_lines)
        )

    msg_parts.append(
        "若還要繼續新增，可以再貼一次名單。\n"
        "若輸入完畢，請按「✅ 完成新增 / 返回」。"
    )

    await update.message.reply_text("\n\n".join(msg_parts))
    return PEOPLE_ADD_INPUT


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
                        CallbackQueryHandler(
                            back_from_date_to_general, pattern="^back_to_general$"
                        ),
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND, single_date_got_date
                        ),
                    ],
                    SD_TIME: [
                        CallbackQueryHandler(
                            back_from_time_to_date, pattern="^back_to_date$"
                        ),
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND, single_date_got_time
                        ),
                    ],
                    SD_TEXT: [
                        CallbackQueryHandler(
                            back_from_text_to_time, pattern="^back_to_time$"
                        ),
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND, single_date_got_text
                        ),
                    ],
                    PEOPLE_MENU: [
                        CallbackQueryHandler(people_menu_callback),
                    ],
                    PEOPLE_ADD_INPUT: [
                        CallbackQueryHandler(
                            people_menu_callback, pattern="^people_add_done$"
                        ),
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND, people_add_input
                        ),
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


# ========= FastAPI lifecycle =========


@app.on_event("startup")
async def on_startup():
    logger.info("Startup event: creating Telegram bot task.")
    asyncio.create_task(run_bot())


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("FastAPI app is shutting down.")
