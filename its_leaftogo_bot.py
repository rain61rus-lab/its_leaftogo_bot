# its_helpdesk_bot.py
# Бот ИТС: ремонты и покупки, комментарии, причины при отмене/отказе,
# учёт времени: когда взято в ремонт, когда завершено, длительность,
# журнал для админа и экспорт CSV.
# Требуется: python-telegram-bot==20.7, aiosqlite

import os, asyncio, aiosqlite, csv, io
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(map(int, filter(None, os.getenv("ADMIN_IDS", "").split(","))))
DB_PATH = "tickets.db"

def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s: return None
    return datetime.fromisoformat(s)

def human_duration(start: Optional[str], end: Optional[str]) -> str:
    st, en = parse_iso(start), parse_iso(end)
    if not st: return "—"
    if not en: en = datetime.utcnow()
    delta = en - st
    # формат: 2д 5ч 12м
    days = delta.days
    secs = delta.seconds
    h = secs // 3600
    m = (secs % 3600) // 60
    parts = []
    if days: parts.append(f"{days}д")
    if h:    parts.append(f"{h}ч")
    if m or not parts: parts.append(f"{m}м")
    return " ".join(parts)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# --- ожидание причины (отмена ремонта / отклонение покупки) ---
PENDING_REASON: Dict[int, Tuple[str, int]] = {}  # admin_id -> (action, ticket_id)

# ===================== БАЗА ДАННЫХ =====================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT CHECK(kind IN ('repair','purchase')) DEFAULT 'repair',
  status TEXT DEFAULT 'new',
  priority TEXT DEFAULT 'normal',
  chat_id INTEGER,
  user_id INTEGER,
  username TEXT,
  description TEXT,
  photo_file_id TEXT,
  assignee_id INTEGER,
  assignee_name TEXT,
  created_at TEXT,
  updated_at TEXT,
  started_at TEXT,   -- когда взяли в работу
  done_at TEXT       -- когда завершили
);
CREATE INDEX IF NOT EXISTS idx_tickets_status  ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_kind    ON tickets(kind);
CREATE INDEX IF NOT EXISTS idx_tickets_user    ON tickets(user_id);

CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  username TEXT,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_ticket ON comments(ticket_id);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def add_ticket(kind:str, chat_id:int, user_id:int, username:str,
                     description:str, status:str="new",
                     priority:str="normal", photo_id:Optional[str]=None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO tickets(kind,status,priority,chat_id,user_id,username,description,photo_file_id,created_at,updated_at,started_at,done_at)"
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, status, priority, chat_id, user_id, username or "", description, photo_id, now_iso(), now_iso(), None, None)
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        (tid,) = await cur.fetchone()
        return tid

async def get_ticket(tid:int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM tickets WHERE id=?", (tid,))
        return await cur.fetchone()

async def update_ticket(tid:int, **fields):
    if not fields: return
    cols, params = [], []
    for k,v in fields.items():
        cols.append(f"{k}=?"); params.append(v)
    cols.append("updated_at=?"); params.append(now_iso())
    params.append(tid)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE tickets SET {', '.join(cols)} WHERE id=?", params)
        await db.commit()

async def find_tickets(kind:Optional[str]=None, status:Optional[str]=None,
                       user_id:Optional[int]=None, phrase:Optional[str]=None,
                       limit:int=50):
    q = "SELECT * FROM tickets WHERE 1=1"
    p = []
    if kind:   q += " AND kind=?";    p.append(kind)
    if status: q += " AND status=?";  p.append(status)
    if user_id:q += " AND user_id=?"; p.append(user_id)
    if phrase:
        q += " AND description LIKE ?"; p.append(f"%{phrase}%")
    q += " ORDER BY id DESC LIMIT ?"; p.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(q,p)
        return await cur.fetchall()

async def add_comment(ticket_id:int, user_id:int, username:str, text:str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO comments(ticket_id,user_id,username,text,created_at) VALUES (?,?,?,?,?)",
            (ticket_id, user_id, username or "", text, now_iso())
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        (cid,) = await cur.fetchone()
        return cid

async def list_comments(ticket_id:int, limit:int=50):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id,username,text,created_at FROM comments WHERE ticket_id=? ORDER BY id LIMIT ?",
            (ticket_id, limit)
        )
        return await cur.fetchall()

# ===================== ФОРМАТЫ/КНОПКИ =====================
def fmt_ticket(row) -> str:
    (id, kind, status, priority, chat_id, user_id, username,
     descr, photo_id, assignee_id, assignee_name, created_at, updated_at,
     started_at, done_at) = row
    kinds = {"repair":"🛠 Ремонт", "purchase":"🛒 Покупка"}
    statuses = {
        "new":"🆕 Новая", "in_work":"🔧 В работе", "done":"✅ Выполнена",
        "canceled":"🚫 Отменена", "approved":"✅ Одобрена", "rejected":"❌ Отклонена"
    }
    pr = {"low":"🟢", "normal":"🟡", "high":"🔴"}.get(priority, "🟡")
    lines = [
        f"{kinds.get(kind,kind)} №{id} • {statuses.get(status,status)} • Приоритет {pr}",
        f"👤 @{username or user_id}",
        f"📝 {descr}",
        f"🕒 Создана: {created_at.replace('T',' ')}"
    ]
    if kind == "repair":
        if started_at:
            lines.append(f"🔧 Взята в работу: {started_at.replace('T',' ')}")
        if done_at:
            lines.append(f"✅ Завершена: {done_at.replace('T',' ')}")
        if started_at:
            lines.append(f"⏳ Длительность: {human_duration(started_at, done_at)}")
    return "\n".join(lines)

def kb_repair_admin(tid:int, admin:bool, is_assignee:bool) -> InlineKeyboardMarkup:
    rows = []
    if admin:
        rows.append([InlineKeyboardButton("Назначить себе", callback_data=f"assign:{tid}")])
    if admin or is_assignee:
        rows.append([
            InlineKeyboardButton("В работу",   callback_data=f"to_work:{tid}"),
            InlineKeyboardButton("Выполнено",  callback_data=f"done:{tid}")
        ])
    if admin:
        rows.append([
            InlineKeyboardButton("Приоритет ↑", callback_data=f"prio_up:{tid}"),
            InlineKeyboardButton("Отменить (с причиной)", callback_data=f"cancel_with_reason:{tid}")
        ])
    rows.append([InlineKeyboardButton("💬 Комментарии", callback_data=f"show_comments:{tid}")])
    return InlineKeyboardMarkup(rows)

def kb_purchase_admin(tid:int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{tid}"),
         InlineKeyboardButton("❌ Отклонить (с причиной)", callback_data=f"reject_with_reason_{tid}")],
        [InlineKeyboardButton("💬 Комментарии", callback_data=f"show_comments:{tid}")]
    ])

# ===================== КОМАНДЫ =====================
async def cmd_start(u:Update, c:ContextTypes.DEFAULT_TYPE):
    txt = [
        "Привет! Это бот инженерно-технической службы.",
        "• Любой текст → заявка на ремонт.",
        "• Покупка: /buy <что купить и зачем>",
        "• Комментарий: /comment #<id> <текст>",
        "• Смотреть комментарии: /comments #<id>",
        "• Мои заявки: /my",
    ]
    if is_admin(u.effective_user.id):
        txt += ["\nАдмин:",
                "/admin — новые ремонты",
                "/purchases — новые покупки",
                "/journal [days] — журнал завершённых ремонтов (по умолчанию 30 дней)",
                "/find <текст|#id>",
                "/export week|month"]
    await u.message.reply_text("\n".join(txt), parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_new(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if not c.args:
        await u.message.reply_text("Используй: /new <описание проблемы>")
        return
    descr = " ".join(c.args)
    tid = await add_ticket("repair", u.effective_chat.id, u.effective_user.id, u.effective_user.username, descr)
    await u.message.reply_text(f"🛠 Заявка №{tid} создана.")
    for aid in ADMIN_IDS:
        try: await c.bot.send_message(aid, f"🛠 Новая заявка №{tid}\n{descr}")
        except: pass

async def any_text(u:Update, c:ContextTypes.DEFAULT_TYPE):
    # ожидание причин (отмена/отказ)
    if is_admin(u.effective_user.id) and u.effective_user.id in PENDING_REASON:
        action, tid = PENDING_REASON.pop(u.effective_user.id)
        reason = (u.message.text or "").strip()
        if not reason:
            await u.message.reply_text("Причина не должна быть пустой. Напиши текст причины.")
            PENDING_REASON[u.effective_user.id] = (action, tid)
            return
        if action == "reject_purchase":
            await update_ticket(tid, status="rejected")
            await add_comment(tid, u.effective_user.id, u.effective_user.username, f"Отклонено: {reason}")
            await u.message.reply_text(f"🛒 Заявка №{tid} отклонена. Причина сохранена.")
        elif action == "cancel_repair":
            await update_ticket(tid, status="canceled")
            await add_comment(tid, u.effective_user.id, u.effective_user.username, f"Отменено: {reason}")
            await u.message.reply_text(f"🛠 Заявка №{tid} отменена. Причина сохранена.")

        row = await get_ticket(tid)
        if row:
            author_id = row[5]
            note = "отклонена ❌" if action=="reject_purchase" else "отменена ❌"
            try: await c.bot.send_message(author_id, f"ℹ Ваша заявка №{tid} {note}. Причина: {reason}")
            except: pass
        return

    # обычный текст => ремонт
    descr = (u.message.text or "").strip()
    if not descr or descr.startswith("/"): return
    tid = await add_ticket("repair", u.effective_chat.id, u.effective_user.id, u.effective_user.username, descr)
    await u.message.reply_text(f"🛠 Заявка №{tid} создана.")
    for aid in ADMIN_IDS:
        try: await c.bot.send_message(aid, f"🛠 Новая заявка №{tid}\n{descr}")
        except: pass

async def any_photo(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if not u.message.caption or u.message.caption.startswith("/"):
        await u.message.reply_text("Добавь подпись к фото (описание проблемы).")
        return
    photo_id = u.message.photo[-1].file_id
    tid = await add_ticket("repair", u.effective_chat.id, u.effective_user.id, u.effective_user.username, u.message.caption, photo_id=photo_id)
    await u.message.reply_text(f"🛠 Заявка с фото №{tid} создана.")
    for aid in ADMIN_IDS:
        try: await c.bot.send_message(aid, f"🛠 Новая заявка №{tid}\n{u.message.caption}")
        except: pass

async def cmd_buy(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if not c.args:
        await u.message.reply_text("Напиши: /buy <что купить и для чего>\nНапр.: /buy фильтр для компрессора")
        return
    descr = " ".join(c.args)
    tid = await add_ticket("purchase", u.effective_chat.id, u.effective_user.id, u.effective_user.username, descr)
    await u.message.reply_text(f"🛒 Заявка на покупку №{tid} создана. Ожидает решения.")
    for aid in ADMIN_IDS:
        try:
            await c.bot.send_message(aid,
                f"🛒 Новая заявка на покупку №{tid}\nОт @{u.effective_user.username or u.effective_user.id}\n— {descr}",
                reply_markup=kb_purchase_admin(tid))
        except: pass

async def cmd_my(u:Update, c:ContextTypes.DEFAULT_TYPE):
    rows = await find_tickets(user_id=u.effective_user.id, limit=20)
    if not rows: await u.message.reply_text("У тебя пока нет заявок."); return
    await u.message.reply_text("\n\n".join(map(fmt_ticket, rows)))

async def cmd_admin(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    rows = await find_tickets(kind="repair", status="new", limit=20)
    if not rows: await u.message.reply_text("Новых заявок на ремонт нет."); return
    for r in rows:
        await u.message.reply_text(fmt_ticket(r), reply_markup=kb_repair_admin(r[0], True, False))

async def cmd_purchases(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    rows = await find_tickets(kind="purchase", status="new", limit=20)
    if not rows: await u.message.reply_text("Новых заявок на покупку нет."); return
    for r in rows:
        await u.message.reply_text(fmt_ticket(r), reply_markup=kb_purchase_admin(r[0]))

async def cmd_comment(u:Update, c:ContextTypes.DEFAULT_TYPE):
    # /comment #12 Текст комментария
    if not c.args or not c.args[0].startswith("#"):
        await u.message.reply_text("Используй: /comment #<id> <текст>")
        return
    tid_s = c.args[0][1:]
    if not tid_s.isdigit():
        await u.message.reply_text("Неверный номер заявки.")
        return
    tid = int(tid_s)
    text = " ".join(c.args[1:]).strip()
    if not text:
        await u.message.reply_text("Добавь текст комментария.")
        return
    row = await get_ticket(tid)
    if not row:
        await u.message.reply_text("Заявка не найдена.")
        return
    await add_comment(tid, u.effective_user.id, u.effective_user.username, text)
    await u.message.reply_text(f"💬 Комментарий добавлен к заявке №{tid}.")
    author_id, assignee_id = row[5], row[9]
    for tgt in (author_id, assignee_id):
        if tgt and tgt != u.effective_user.id:
            try: await c.bot.send_message(tgt, f"💬 Комментарий по заявке №{tid} от @{u.effective_user.username or u.effective_user.id}: {text}")
            except: pass

async def cmd_comments(u:Update, c:ContextTypes.DEFAULT_TYPE):
    # /comments #12
    if not c.args or not c.args[0].startswith("#"):
        await u.message.reply_text("Используй: /comments #<id>")
        return
    tid_s = c.args[0][1:]
    if not tid_s.isdigit():
        await u.message.reply_text("Неверный номер заявки.")
        return
    tid = int(tid_s)
    row = await get_ticket(tid)
    if not row:
        await u.message.reply_text("Заявка не найдена.")
        return
    comms = await list_comments(tid, 50)
    if not comms:
        await u.message.reply_text("Комментариев пока нет."); return
    lines = [f"💬 Комментарии к заявке №{tid}:"]
    for (uid, uname, text, created) in comms:
        lines.append(f"— @{uname or uid} [{created.replace('T',' ')}]: {text}")
    await u.message.reply_text("\n".join(lines))

async def cmd_find(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    if not c.args:
        await u.message.reply_text("Поиск: /find <текст> или /find #<id>")
        return
    q = " ".join(c.args)
    if q.startswith("#") and q[1:].isdigit():
        row = await get_ticket(int(q[1:]))
        await u.message.reply_text(fmt_ticket(row) if row else "Не найдено.")
        return
    rows = await find_tickets(phrase=q, limit=20)
    await u.message.reply_text("\n\n".join(map(fmt_ticket, rows)) if rows else "Не найдено.")

async def cmd_export(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    period = (c.args[0] if c.args else "week").lower()
    date_from = datetime.utcnow() - (timedelta(days=7) if period=="week" else timedelta(days=30))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM tickets WHERE created_at>=? ORDER BY id", (date_from.isoformat(timespec='seconds'),))
        rows = await cur.fetchall()
    if not rows:
        await u.message.reply_text("За период заявок нет."); return
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    w.writerow(["id","kind","status","priority","user_id","username","description","assignee","created_at","started_at","done_at","duration"])
    for r in rows:
        duration = human_duration(r[13], r[14]) if r[1]=="repair" else ""
        w.writerow([r[0], r[1], r[2], r[3], r[5], r[6], r[7], r[10] or "", r[11], r[13] or "", r[14] or "", duration])
    buf.seek(0)
    await u.message.reply_document(document=InputFile(io.BytesIO(buf.getvalue().encode('utf-8')), filename=f"its_export_{period}.csv"),
                                   caption=f"Экспорт за {period}")

async def cmd_journal(u:Update, c:ContextTypes.DEFAULT_TYPE):
    """Журнал завершённых ремонтов с длительностями.
       /journal           (за 30 дней)
       /journal 7         (за 7 дней)
    """
    if not is_admin(u.effective_user.id): return
    days = 30
    if c.args and c.args[0].isdigit():
        days = max(1, min(365, int(c.args[0])))
    since = datetime.utcnow() - timedelta(days=days)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,description,created_at,started_at,done_at FROM tickets "
            "WHERE kind='repair' AND status='done' AND created_at>=? ORDER BY id DESC LIMIT 100",
            (since.isoformat(timespec='seconds'),)
        )
        rows = await cur.fetchall()
    if not rows:
        await u.message.reply_text(f"Завершённых ремонтов за {days} дн. нет.")
        return
    lines = [f"🧾 Журнал завершённых ремонтов (последние {len(rows)}):"]
    for (tid, descr, created, started, done) in rows:
        dur = human_duration(started, done)
        lines.append(
            f"#{tid} • ⏱ {created.replace('T',' ')} → 🔧 {str(started).replace('T',' ') if started else '—'} → ✅ {str(done).replace('T',' ') if done else '—'} • ⌛ {dur}\n— {descr}"
        )
    # Telegram ограничивает длину сообщения, поэтому режем порциями
    chunk = ""
    chunks = []
    for ln in lines:
        if len(chunk) + len(ln) + 1 > 3800:
            chunks.append(chunk); chunk = ""
        chunk += (ln + "\n")
    if chunk: chunks.append(chunk)
    for part in chunks:
        await u.message.reply_text(part)

# ===================== КНОПКИ =====================
async def on_btn(u:Update, c:ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    data = q.data

    # показать комментарии
    if data.startswith("show_comments:"):
        tid = int(data.split(":")[1])
        comms = await list_comments(tid, 50)
        if not comms:
            await q.answer("Комментариев нет.", show_alert=True)
            return
        lines = [f"💬 Комментарии к заявке №{tid}:"]
        for (uid, uname, text, created) in comms:
            lines.append(f"— @{uname or uid} [{created.replace('T',' ')}]: {text}")
        try: await q.edit_message_text("\n".join(lines))
        except: pass
        return

    # покупки: одобрить / отклонить с причиной
    if data.startswith("approve_") or data.startswith("reject_with_reason_"):
        tid = int(data.rsplit("_", 1)[1])
        if data.startswith("approve_"):
            await update_ticket(tid, status="approved")
            try: await q.edit_message_text(f"🛒 Заявка №{tid} одобрена ✅")
            except: pass
            row = await get_ticket(tid)
            if row:
                try: await c.bot.send_message(row[5], f"🛒 Ваша заявка №{tid} одобрена ✅")
                except: pass
            return
        else:
            PENDING_REASON[q.from_user.id] = ("reject_purchase", tid)
            try: await q.edit_message_text(f"📝 Введите причину отклонения для заявки №{tid} одним сообщением.")
            except: pass
            return

    # ремонты: назначение/статусы/приоритет/отмена с причиной
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

    uid = q.from_user.id
    admin = is_admin(uid)
    is_assignee = (row[9] == uid)

    if action == "assign" and admin:
        await update_ticket(tid, assignee_id=uid, assignee_name=q.from_user.full_name)
        row = await get_ticket(tid)
        try: await q.edit_message_text(fmt_ticket(row), reply_markup=kb_repair_admin(tid, True, True))
        except: pass
        return

    if action == "to_work" and (admin or is_assignee):
        # фиксируем старт если его ещё не было
        started_at = row[13] or now_iso()
        await update_ticket(tid, status="in_work", started_at=started_at)
        row = await get_ticket(tid)
        try: await q.edit_message_text(fmt_ticket(row), reply_markup=kb_repair_admin(tid, admin, True))
        except: pass
        return

    if action == "done" and (admin or is_assignee):
        # фиксируем завершение
        done_at = row[14] or now_iso()
        await update_ticket(tid, status="done", done_at=done_at)
        row = await get_ticket(tid)
        try: await q.edit_message_text(fmt_ticket(row))
        except: pass
        return

    if action == "prio_up" and admin:
        new = {"low":"normal","normal":"high","high":"high"}[row[3]]
        await update_ticket(tid, priority=new)
        row = await get_ticket(tid)
        try: await q.edit_message_text(fmt_ticket(row), reply_markup=kb_repair_admin(tid, admin, is_assignee))
        except: pass
        return

    if action == "cancel_with_reason" and admin:
        PENDING_REASON[uid] = ("cancel_repair", tid)
        try: await q.edit_message_text(f"📝 Введите причину отмены для заявки №{tid} одним сообщением.")
        except: pass
        return

# ===================== MAIN =====================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN отсутствует. Добавь его в Secrets.")
    await init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("new",     cmd_new))
    app.add_handler(CommandHandler("buy",     cmd_buy))
    app.add_handler(CommandHandler("my",      cmd_my))
    app.add_handler(CommandHandler("admin",   cmd_admin))
    app.add_handler(CommandHandler("purchases", cmd_purchases))
    app.add_handler(CommandHandler("comment", cmd_comment))
    app.add_handler(CommandHandler("comments", cmd_comments))
    app.add_handler(CommandHandler("find",    cmd_find))
    app.add_handler(CommandHandler("export",  cmd_export))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.PHOTO, any_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_text))

    print("ITS bot running...\nBot is polling for updates...")
    await app.run_polling()

if _name_ == "_main_":
    asyncio.run(main())
