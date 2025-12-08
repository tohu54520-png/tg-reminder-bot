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
    MENU,
    GENERAL_MENU,
    SD_DATE,
    SD_TIME,
    SD_TEXT,
    REMINDER_LIST,
    PEOPLE_MENU,
    PEOPLE_ADD,
    PEOPLE_DELETE,

    GENERAL_WEEKDAY,
    GENERAL_TIME,
    GENERAL_TEXT,
    GENERAL_MENTIONS,

    APK_WEEKDAY,     # 選星期
    APK_TIME,        # 選時間 HHMM
    APK_TEXT,        # 輸入內容
    APK_TAG_PEOPLE,  # 選 @ 人
) = range(17)
 
 
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
@@ -203,65 +208,281 @@ def parse_hhmm(text: str):
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


# ========= 一般提醒（固定週期）工具 =========

def build_general_weekday_keyboard(selected: set[int]):
    labels = ["一", "二", "三", "四", "五", "六", "日"]
    keyboard = []
    row = []

    for i in range(7):
        mark = "✅" if i in selected else "⬜"
        row.append(
            InlineKeyboardButton(
                f"{mark} 週{labels[i]}",
                callback_data=f"gen_wd_{i}",
            )
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append(
        [
            InlineKeyboardButton("➡️ 下一步（選時間）", callback_data="gen_wd_next"),
            InlineKeyboardButton("⬅️ 返回主選單", callback_data="gen_wd_back"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)


async def general_cycle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    selected = context.user_data.get("gen_weekdays", set())

    await query.message.reply_text(
        "【一般提醒 ➜ 固定週期】\n請選擇每週要提醒的「星期」（可複選）：",
        reply_markup=build_general_weekday_keyboard(selected),
    )

    return GENERAL_WEEKDAY


async def general_cycle_weekday_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    selected = context.user_data.setdefault("gen_weekdays", set())

    if data.startswith("gen_wd_") and data[-1].isdigit():
        wd = int(data[-1])
        if wd in selected:
            selected.remove(wd)
        else:
            selected.add(wd)

        await query.message.edit_reply_markup(
            reply_markup=build_general_weekday_keyboard(selected)
        )
        return GENERAL_WEEKDAY

    if data == "gen_wd_next":
        if not selected:
            await query.message.reply_text("⚠️ 請至少選擇一天星期。")
            return GENERAL_WEEKDAY

        await query.message.reply_text(
            "請輸入提醒時間（HHMM，例如：0930 或 1830）："
        )
        return GENERAL_TIME

    if data == "gen_wd_back":
        await send_main_menu(chat_id, context)
        return MENU

    return GENERAL_WEEKDAY


async def general_cycle_time_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    parsed = parse_hhmm(text)
    if not parsed:
        await update.message.reply_text("時間格式錯誤，請輸入 HHMM，例如 0930 或 1830")
        return GENERAL_TIME

    context.user_data["gen_time"] = parsed
    await update.message.reply_text("請輸入提醒內容：")
    return GENERAL_TEXT


async def general_cycle_text_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("提醒內容不能為空，請重新輸入。")
        return GENERAL_TEXT

    context.user_data["gen_text"] = text

    people = db_list_people(update.effective_chat.id)
    if not people:
        context.user_data["gen_mentions"] = set()
        await finalize_general_cycle(update, context)
        return MENU

    keyboard = []
    for pid, tg_id, nickname in people:
        keyboard.append([
            InlineKeyboardButton(f"{nickname} {tg_id}", callback_data=f"gen_at_{pid}")
        ])

    keyboard.append(
        [InlineKeyboardButton("✅ 不 @ 任何人，直接完成", callback_data="gen_at_done")]
    )

    await update.message.reply_text(
        "請選擇要 @ 的人（可複選，選完點 ✅ 完成）：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    context.user_data["gen_mentions"] = set()
    return GENERAL_MENTIONS


async def general_cycle_at_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    mentions = context.user_data.setdefault("gen_mentions", set())

    if data.startswith("gen_at_"):
        pid = int(data.split("_")[-1])
        if pid in mentions:
            mentions.remove(pid)
        else:
            mentions.add(pid)

        return GENERAL_MENTIONS

    if data == "gen_at_done":
        await finalize_general_cycle(update, context)
        return MENU


async def finalize_general_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    weekdays = context.user_data.get("gen_weekdays", set())
    hour, minute = context.user_data.get("gen_time")
    text = context.user_data.get("gen_text")
    mention_ids = context.user_data.get("gen_mentions", set())

    mentions = []
    if mention_ids:
        people = db_list_people(chat_id)
        for pid, tg_id, nickname in people:
            if pid in mention_ids:
                mentions.append(tg_id)

    mention_str = "\n".join(mentions)

    now = datetime.now(TZ)
    labels = ["一", "二", "三", "四", "五", "六", "日"]
    created = 0

    for wd in weekdays:
        days_ahead = (wd - now.weekday()) % 7
        run_at = datetime(now.year, now.month, now.day, hour, minute, tzinfo=TZ) + timedelta(
            days=days_ahead
        )

        if run_at <= now:
            run_at += timedelta(days=7)

        mmdd = run_at.strftime("%m/%d")
        label = labels[wd]

        final_text = f"【固定週期｜週{label}】{text}"
        if mention_str:
            final_text += f"\n{mention_str}"

        reminder_id = db_add_reminder(chat_id, "general_cycle", run_at, final_text)

        job_name = f"reminder-{reminder_id}"
        context.application.job_queue.run_once(
            reminder_job,
            when=run_at,
            data={
                "chat_id": chat_id,
                "text": final_text,
                "when_str": mmdd,
                "reminder_id": reminder_id,
            },
            name=job_name,
        )

        created += 1

    await update.effective_chat.send_message(
        f"✅ 已建立 {created} 個固定週期提醒"
    )

    context.user_data.pop("gen_weekdays", None)
    context.user_data.pop("gen_time", None)
    context.user_data.pop("gen_text", None)
    context.user_data.pop("gen_mentions", None)

    await send_main_menu(chat_id, context)
 # ========= 谷歌 APK 提醒：選擇星期（可複選） =========
 
 def build_weekday_keyboard(selected: set[int]):
     labels = ["一", "二", "三", "四", "五", "六", "日"]
     keyboard = []
     row = []
 
     for i in range(7):
         mark = "✅" if i in selected else "⬜"
         row.append(
             InlineKeyboardButton(
                 f"{mark} 週{labels[i]}",
                 callback_data=f"apk_wd_{i}",
             )
         )
         if len(row) == 2:
             keyboard.append(row)
             row = []
 
     if row:
         keyboard.append(row)
 
     keyboard.append([
         InlineKeyboardButton("➡️ 下一步（選時間）", callback_data="apk_wd_next"),
         InlineKeyboardButton("⬅️ 返回主選單", callback_data="apk_wd_back"),
@@ -497,55 +718,56 @@ async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            "general_cycle": "一般提醒（固定週期）",
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
@@ -748,130 +970,135 @@ async def people_delete_callback(update: Update, context: ContextTypes.DEFAULT_T
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
                InlineKeyboardButton("固定週期", callback_data="general_cycle"),
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
         context.user_data.pop("apk_weekdays", None)
         await apk_weekday_menu(update, context)
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
        context.user_data.pop("gen_weekdays", None)
        context.user_data.pop("gen_time", None)
        context.user_data.pop("gen_text", None)
        context.user_data.pop("gen_mentions", None)

        await general_cycle_menu(update, context)
        return GENERAL_WEEKDAY
 
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
            InlineKeyboardButton("固定週期", callback_data="general_cycle"),
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
 
@@ -1006,59 +1233,75 @@ async def single_date_got_text(update: Update, context: ContextTypes.DEFAULT_TYP
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
                        CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                    ],
                    GENERAL_WEEKDAY: [
                        CallbackQueryHandler(general_cycle_weekday_callback, pattern="^gen_"),
                        CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                    ],
                    GENERAL_TIME: [
                        CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, general_cycle_time_got),
                    ],
                    GENERAL_TEXT: [
                        CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, general_cycle_text_got),
                    ],
                    GENERAL_MENTIONS: [
                        CallbackQueryHandler(general_cycle_at_callback, pattern="^gen_"),
                        CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                    ],
                    SD_DATE: [
                        CallbackQueryHandler(back_from_date_to_general, pattern="^back_to_general$"),
                        CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, single_date_got_date),
                    ],
                     SD_TIME: [
                         CallbackQueryHandler(back_from_time_to_date, pattern="^back_to_date$"),
                         CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                         MessageHandler(filters.TEXT & ~filters.COMMAND, single_date_got_time),
                     ],
                     SD_TEXT: [
                         CallbackQueryHandler(back_from_text_to_time, pattern="^back_to_time$"),
                         CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                         MessageHandler(filters.TEXT & ~filters.COMMAND, single_date_got_text),
                     ],
                     REMINDER_LIST: [
                         CallbackQueryHandler(reminder_list_callback),
                         CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                     ],
                     PEOPLE_MENU: [
                         CallbackQueryHandler(people_menu_callback, pattern="^people_"),
                         CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                     ],
                     PEOPLE_ADD: [
                         CallbackQueryHandler(people_menu_callback, pattern="^people_"),
                         CallbackQueryHandler(main_menu_callback, pattern="^menu_"),
                         MessageHandler(filters.TEXT & ~filters.COMMAND, people_add_got_text),
                     ],
                     PEOPLE_DELETE: [
                         CallbackQueryHandler(people_delete_callback, pattern="^people_"),
