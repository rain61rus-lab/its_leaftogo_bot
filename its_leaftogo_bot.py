# its_leaftogo_bot.py
# Бот ИТС: заявки на ремонт и заявки на покупку
# Зависимости: python-telegram-bot>=20.7, aiosqlite

import os
import asyncio
import aiosqlite
from datetime import datetime
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# -------------------- Конфигурация --------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(map(int, filter(None, os.getenv("ADMIN_IDS", "").split(","))))

DB_PATH = "tickets.db"

STATUSES_REPAIR = ("new", "in_work", "done", "canceled")
STATUSES_PURCHASE = ("new", "approved", "rejected")

def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# -------------------- База данных --------------------

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER,
  user_id INTEGER,
  username TEXT,
  description TEXT,
  status TEXT DEFAULT 'new',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  assignee_id INTEGER,
  assignee_name TEXT,
  kind TEXT DEFAULT 'repair'       -- 'repair' | 'purchase'
);
"""

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def add_ticket(chat_id:int, user_id:int, username:str, description:str,
                     kind:str="repair", status:str="new") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO tickets (chat_id,user_id,username,description,kind,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (chat_id, user_id, username or "", description, kind, status, now_iso())
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        (tid,) = await cur.fetchone()
        return tid

async def get_ticket(tid:int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM tickets WHERE id=?", (tid,))
        return await cur.fetchone()

async def list_tickets(kind:Optional[str]=None, status:Optional[str]=None,
                       user_id:Optional[int]=None, limit:int=50):
    q = "SELECT * FROM tickets WHERE 1=1"
    params = []
    if kind:
        q += " AND kind=?"; params.append(kind)
    if status:
        q += " AND status=?"; params.append(status)
    if user_id:
        q += " AND user_id=?"; params.append(user_id)
    q += " ORDER BY id DESC LIMIT ?"; params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(q, params)
        return await cur.fetchall()

async def update_ticket_status(tid:int, status:str,
                               assignee_id:Optional[int]=None,
                               assignee_name:Optional[str]=None):
    updates, params = ["status=?"], [status]
    if assignee_id is not None:
        updates.append("assignee_id=?"); params.append(assignee_id)
    if assignee_name is not None:
        updates.append("assignee_name=?"); params.append(assignee_name)
    updates.append("created_at=created_at")  # no-op для согласования
    params.append(tid)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE tickets SET {', '.join(updates)}, "
                         "updated_at=CURRENT_TIMESTAMP WHERE id=?", params)
        await db.commit()


# -------------------- Вспомогательные --------------------

def is_admin(uid:int) -> bool:
    return uid in ADMIN_IDS

def fmt_ticket(row) -> str:
    # row: (id, chat_id, user_id, username, description, status, created_at, assignee_id, assignee_name, kind)
    (tid, _chat, user_id, username, descr, status, created_at, assignee_id, assignee_name, kind) = row
    kinds = {"repair": "🛠 Ремонт", "purchase": "🛒 Покупка"}
    statuses = {
        "new": "🆕 Новая",
        "in_work": "🔧 В работе",
        "done": "✅ Выполнена",
        "canceled": "🚫 Отменена",
        "approved": "✅ Одобрена",
        "rejected": "❌ Отклонена",
    }
    lines = [
        f"{kinds.get(kind, kind)} №{tid} • {statuses.get(status, status)}",
        f"👤 @{username or user_id}",
        f"📝 {descr}",
        f"⏱ {str(created_at).replace('T',' ')}",
    ]
    if assignee_name:
        lines.append(f"👨‍🔧 Исполнитель: {assignee_name}")
    return "\n".join(lines)

def kb_repair_admin(tid:int, is_admin_user:bool, is_assignee:bool) -> Optional[InlineKeyboardMarkup]:
    buttons = []
    if is_admin_user:
        buttons.append([InlineKeyboardButton("Взять", callback_data=f"assign:{tid}")])
    if is_admin_user or is_assignee:
        buttons.append([
            InlineKeyboardButton("В работу", callback_data=f"to_work:{tid}"),
            InlineKeyboardButton("Выполнено", callback_data=f"done:{tid}")
        ])
    if is_admin_user:
        buttons.append([InlineKeyboardButton("Отменить", callback_data=f"cancel:{tid}")])
    return InlineKeyboardMarkup(buttons) if buttons else None

def kb_purchase_admin(tid:int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{tid}"),
         InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{tid}")]
    ])


# -------------------- Команды --------------------

async def cmd_start(update:Update, _:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = [
        "Привет! Это бот заявок ИТС.",
        "✍ Просто напиши текст — создастся заявка на ремонт.",
        "🛒 Для заявок на покупку: /buy <что купить и зачем>",
        "🧾 Примеры: /buy фильтр осушителя для компрессора",
        "👤 Личные заявки: /my",
    ]
    if is_admin(uid):
        txt += ["\nАдмин:", "/admin — новые ремонты", "/purchases — новые покупки"]
    await update.message.reply_text("\n".join(txt), disable_web_page_preview=True, parse_mode="Markdown")

# ручное создание заявки на ремонт одной командой
async def cmd_new(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Напиши: /new <описание проблемы>")
        return
    descr = " ".join(args).strip()
    tid = await add_ticket(update.effective_chat.id, update.effective_user.id,
                           update.effective_user.username, descr, "repair", "new")
    await update.message.reply_text(f"🛠 Заявка №{tid} создана.")
    # уведомим админов
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"🛠 Новая заявка на ремонт №{tid}\n{descr}")
        except:
            pass

# универсально: любое сообщение без команды — заявка на ремонт
async def text_as_ticket(update:Update, context:ContextTypes.DEFAULT_TYPE):
    descr = update.message.text.strip()
    if descr.startswith("/"):  # не перехватываем команды
        return
    tid = await add_ticket(update.effective_chat.id, update.effective_user.id,
                           update.effective_user.username, descr, "repair", "new")
    await update.message.reply_text(f"🛠 Заявка №{tid} создана.")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"🛠 Новая заявка на ремонт №{tid}\n{descr}")
        except:
            pass

# заявки на покупку
async def cmd_buy(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши в одной строке: /buy <что купить и для чего>\nНапр.: /buy фильтр осушителя для компрессора")
        return
    descr = " ".join(context.args).strip()
    tid = await add_ticket(update.effective_chat.id, update.effective_user.id,
                           update.effective_user.username, descr, "purchase", "new")
    await update.message.reply_text(f"🛒 Заявка на покупку №{tid} создана. Ожидает решения.")
    # админам с кнопками
    kb = kb_purchase_admin(tid)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid,
                f"🛒 Новая заявка на покупку №{tid}\nОт @{update.effective_user.username or update.effective_user.id}\n— {descr}",
                reply_markup=kb)
        except:
            pass

# мои заявки
async def my_tickets(update:Update, _:ContextTypes.DEFAULT_TYPE):
    rows = await list_tickets(user_id=update.effective_user.id, limit=20)
    if not rows:
        await update.message.reply_text("У тебя пока нет заявок.")
        return
    await update.message.reply_text("\n\n".join(map(fmt_ticket, rows)))

# панель админа (новые ремонты)
async def admin_panel(update:Update, _:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    rows = await list_tickets(kind="repair", status="new", limit=20)
    if not rows:
        await update.message.reply_text("Новых заявок на ремонт нет.")
        return
    for r in rows:
        kb = kb_repair_admin(r[0], True, False)
        await update.message.reply_text(fmt_ticket(r), reply_markup=kb)

# новые покупки с кнопками
async def purchases_panel(update:Update, _:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    rows = await list_tickets(kind="purchase", status="new", limit=20)
    if not rows:
        await update.message.reply_text("Новых заявок на покупку нет.")
        return
    for r in rows:
        await update.message.reply_text(fmt_ticket(r), reply_markup=kb_purchase_admin(r[0]))


# -------------------- Обработка кнопок --------------------

async def on_buttons(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    # --- Покупки: одобрить / отклонить ---
    if data.startswith("approve_") or data.startswith("reject_"):
        tid = int(data.split("_")[1])
        new_status = "approved" if data.startswith("approve_") else "rejected"

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE tickets SET status=? WHERE id=?", (new_status, tid))
            await db.commit()
            cur = await db.execute("SELECT user_id FROM tickets WHERE id=?", (tid,))
            row = await cur.fetchone()
            author_id = row[0] if row else None

        try:
            await q.edit_message_text(f"🛒 Заявка №{tid} {'одобрена ✅' if new_status=='approved' else 'отклонена ❌'}")
        except:
            pass
        if author_id:
            try:
                await context.bot.send_message(author_id, f"🛒 Ваша заявка №{tid} {'одобрена ✅' if new_status=='approved' else 'отклонена ❌'}")
            except:
                pass
        return

    # --- Ремонты: назначение и статусы ---
    uid = q.from_user.id
    isadm = is_admin(uid)
    try:
        action, sid = data.split(":")
        tid = int(sid)
    except:
        return

    row = await get_ticket(tid)
    if not row:
        try: await q.edit_message_text("Заявка не найдена.")
        except: pass
        return

    # r indices: id,chat_id,user_id,username,description,status,created_at,assignee_id,assignee_name,kind
    assignee_id = row[7]
    is_assignee = assignee_id == uid

    if action == "assign" and isadm:
        await update_ticket_status(tid, row[5], assignee_id=uid, assignee_name=q.from_user.full_name)
        try: await q.edit_message_text(fmt_ticket((row[0],row[1],row[2],row[3],row[4],row[5],row[6],uid,q.from_user.full_name,row[9])),
                                       reply_markup=kb_repair_admin(tid, True, True))
        except: pass
        return

    if action == "to_work" and (isadm or is_assignee):
        await update_ticket_status(tid, "in_work")
        try: await q.edit_message_text(fmt_ticket((row[0],row[1],row[2],row[3],row[4],"in_work",row[6],row[7],row[8],row[9])),
                                       reply_markup=kb_repair_admin(tid, isadm, True))
        except: pass
        return

    if action == "done" and (isadm or is_assignee):
        await update_ticket_status(tid, "done")
        try: await q.edit_message_text(fmt_ticket((row[0],row[1],row[2],row[3],row[4],"done",row[6],row[7],row[8],row[9])))
        except: pass
        return

    if action == "cancel" and isadm:
        await update_ticket_status(tid, "canceled")
        try: await q.edit_message_text(fmt_ticket((row[0],row[1],row[2],row[3],row[4],"canceled",row[6],row[7],row[8],row[9])))
        except: pass
        return


# -------------------- main --------------------

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set it in Replit → Secrets.")
    await init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("buy", cmd_buy))          # заявки на покупку
    app.add_handler(CommandHandler("my", my_tickets))
    app.add_handler(CommandHandler("admin", admin_panel))    # новые ремонты
    app.add_handler(CommandHandler("purchases", purchases_panel))  # новые покупки
    app.add_handler(CallbackQueryHandler(on_buttons))

    # любое текстовое сообщение = новая заявка на ремонт
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_as_ticket))

    print("ITS bot running...\nBot is polling for updates...")
    await app.run_polling()

if _name_ == "_main_":
    asyncio.run(main())
