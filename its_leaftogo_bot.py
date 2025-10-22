# its_helpdesk_bot.py
# ИТС-бот с ролями (админ/механик), горячими клавишами,
# ремонты/покупки, причины при отмене/отклонении,
# учёт времени (start/done/длительность),
# журнал с описанием и исполнителем, кнопка "Заявки на ремонт".
# Требуется: python-telegram-bot==20.7, aiosqlite

import os, asyncio, aiosqlite, csv, io
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------- конфиг ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(map(int, filter(None, os.getenv("ADMIN_IDS", "").split(","))))
TECH_IDS  = set(map(int, filter(None, os.getenv("TECH_IDS",  "").split(","))))
DB_PATH = "tickets.db"

def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s: return None
    return datetime.fromisoformat(s)

def human_duration(start: Optional[str], end: Optional[str]) -> str:
    st, en = parse_iso(start), parse_iso(end) or datetime.utcnow()
    if not st: return "—"
    delta = en - st
    d = delta.days
    s = delta.seconds
    h = s // 3600
    m = (s % 3600) // 60
    parts = []
    if d: parts.append(f"{d}д")
    if h: parts.append(f"{h}ч")
    if m or not parts: parts.append(f"{m}м")
    return " ".join(parts)

def is_admin(uid:int) -> bool: return uid in ADMIN_IDS
def is_tech(uid:int)  -> bool: return uid in TECH_IDS

# ожидания
PENDING_REASON: Dict[int, Tuple[str,int]] = {}  # admin_id -> (action, ticket_id)
PENDING_NEW:    Dict[int, str]            = {}  # user_id  -> "repair"|"purchase"

# ---------- БД ----------
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
  started_at TEXT,
  done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_kind   ON tickets(kind);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_user   ON tickets(user_id);
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
    q, p = "SELECT * FROM tickets WHERE 1=1", []
    if kind:   q += " AND kind=?";    p.append(kind)
    if status: q += " AND status=?";  p.append(status)
    if user_id:q += " AND user_id=?"; p.append(user_id)
    if phrase: q += " AND description LIKE ?"; p.append(f"%{phrase}%")
    q += " ORDER BY id DESC LIMIT ?"; p.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(q,p)
        return await cur.fetchall()

# ---------- формат/кнопки ----------
def fmt_ticket(row) -> str:
    (id, kind, status, prio, chat_id, user_id, username,
     descr, photo_id, assignee_id, assignee_name, created_at, updated_at,
     started_at, done_at) = row
    kinds = {"repair":"🛠 Ремонт", "purchase":"🛒 Покупка"}
    statuses = {
        "new":"🆕 Новая", "in_work":"🔧 В работе", "done":"✅ Выполнена",
        "canceled":"🚫 Отменена", "approved":"✅ Одобрена", "rejected":"❌ Отклонена"
    }
    pr = {"low":"🟢","normal":"🟡","high":"🔴"}.get(prio,"🟡")
    lines = [
        f"{kinds.get(kind,kind)} №{id} • {statuses.get(status,status)} • Приоритет {pr}",
        f"👤 @{username or user_id}",
        f"📝 {descr}",
        f"🕒 Создана: {created_at.replace('T',' ')}"
    ]
    if kind == "repair":
        if started_at: lines.append(f"🔧 Взята: {started_at.replace('T',' ')}")
        if done_at:    lines.append(f"✅ Завершена: {done_at.replace('T',' ')}")
        if started_at: lines.append(f"⏳ Длительность: {human_duration(started_at, done_at)}")
    if assignee_id:
        lines.append(f"👨‍🔧 Исполнитель: {assignee_name or assignee_id}")
    return "\n".join(lines)

def kb_repair(tid:int, admin:bool, is_assignee:bool, tech:bool=False) -> InlineKeyboardMarkup:
    rows = []
    if admin:
        rows.append([
            InlineKeyboardButton("Назначить себе", callback_data=f"assign:{tid}"),
            InlineKeyboardButton("Назначить механику", callback_data=f"assign_menu:{tid}")
        ])
    if admin or is_assignee or tech:
        rows.append([
            InlineKeyboardButton("В работу",  callback_data=f"to_work:{tid}"),
            InlineKeyboardButton("Выполнено", callback_data=f"done:{tid}")
        ])
    if admin:
        rows.append([
            InlineKeyboardButton("Приоритет ↑", callback_data=f"prio_up:{tid}"),
            InlineKeyboardButton("Отменить (с причиной)", callback_data=f"cancel_with_reason:{tid}")
        ])
    return InlineKeyboardMarkup(rows)

def kb_assign_list(tid:int) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for uid in sorted(TECH_IDS):
        label = f"Назначить ID{uid}"
        row.append(InlineKeyboardButton(label, callback_data=f"assign_to:{tid}:{uid}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton("↩️ Назад", callback_data=f"assign_back:{tid}")])
    return InlineKeyboardMarkup(buttons)

def kb_purchase_admin(tid:int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{tid}"),
         InlineKeyboardButton("❌ Отклонить (с причиной)", callback_data=f"reject_with_reason_{tid}")]
    ])

# ---------- команды ----------
async def cmd_start(u:Update, c:ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    isadm, istec = is_admin(uid), is_tech(uid)
    kb = [
        [KeyboardButton("🛠 Новая заявка"), KeyboardButton("🧾 Мои заявки")],
        [KeyboardButton("🛒 Покупка")],
        [KeyboardButton("🛠 Заявки на ремонт")]
    ]
    if isadm:
        kb.append([KeyboardButton("🛒 Покупки")])
        kb.append([KeyboardButton("📓 Журнал")])
    reply_kb = ReplyKeyboardMarkup(kb, resize_keyboard=True)

    await u.message.reply_text(
        "Привет это робот инженерно технической службы",
        reply_markup=reply_kb
    )

async def cmd_my(u:Update, c:ContextTypes.DEFAULT_TYPE):
    rows = await find_tickets(user_id=u.effective_user.id, limit=20)
    if not rows:
        await u.message.reply_text("У тебя пока нет заявок."); return
    await u.message.reply_text("\n\n".join(map(fmt_ticket, rows)))

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
        await u.message.reply_text("Напиши: /buy <что купить и для чего>")
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

async def cmd_repairs(u:Update, c:ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Заявки на ремонт':
       - Админ: показывает новые ремонты (status=new) с кнопками
       - Механик: показывает свои 'в работе' (status=in_work, assignee=он)
    """
    uid = u.effective_user.id
    if is_admin(uid):
        rows = await find_tickets(kind="repair", status="new", limit=20)
        if not rows: await u.message.reply_text("Новых заявок на ремонт нет."); return
        for r in rows:
            await u.message.reply_text(fmt_ticket(r), reply_markup=kb_repair(r[0], True, False, tech=False))
    elif is_tech(uid):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT * FROM tickets WHERE kind='repair' AND status='in_work' AND assignee_id=? ORDER BY id DESC LIMIT 20",
                (uid,)
            )
            rows = await cur.fetchall()
        if not rows: await u.message.reply_text("У тебя пока нет заявок 'в работе'."); return
        for r in rows:
            await u.message.reply_text(fmt_ticket(r), reply_markup=kb_repair(r[0], False, True, tech=True))
    else:
        await u.message.reply_text("Недостаточно прав.")

async def cmd_purchases(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    rows = await find_tickets(kind="purchase", status="new", limit=20)
    if not rows: await u.message.reply_text("Новых заявок на покупку нет."); return
    for r in rows:
        await u.message.reply_text(fmt_ticket(r), reply_markup=kb_purchase_admin(r[0]))

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
    since  = datetime.utcnow() - (timedelta(days=7) if period=="week" else timedelta(days=30))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM tickets WHERE created_at>=? ORDER BY id", (since.isoformat(timespec='seconds'),))
        rows = await cur.fetchall()
    if not rows: await u.message.reply_text("За период заявок нет."); return
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
    """Журнал: какая заявка, текст, кто взял, время взятия и выполнения"""
    if not is_admin(u.effective_user.id): return
    days = 30
    if c.args and c.args[0].isdigit(): days = max(1, min(365, int(c.args[0])))
    since = datetime.utcnow() - timedelta(days=days)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,description,assignee_name,started_at,done_at FROM tickets "
            "WHERE kind='repair' AND status='done' AND created_at>=? "
            "ORDER BY id DESC LIMIT 200",
            (since.isoformat(timespec='seconds'),)
        )
        rows = await cur.fetchall()
    if not rows: await u.message.reply_text(f"Завершённых ремонтов за {days} дн. нет."); return
    lines = [f"🧾 Журнал (последние {len(rows)}):"]
    for (tid, descr, assignee, started, done) in rows:
        dur = human_duration(started, done)
        lines.append(f"#{tid} • 👨‍🔧 {assignee or '—'} • 🔧 {str(started).replace('T',' ') if started else '—'} → ✅ {str(done).replace('T',' ') if done else '—'} • ⌛ {dur}\n— {descr}")
    chunk, chunks = "", []
    for ln in lines:
        if len(chunk)+len(ln)+1 > 3800:
            chunks.append(chunk); chunk=""
        chunk += ln+"\n"
    if chunk: chunks.append(chunk)
    for part in chunks:
        await u.message.reply_text(part)

# ---------- текст / горячие клавиши ----------
async def any_text(u:Update, c:ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    text = (u.message.text or "").strip()

    # Горячие клавиши
    if text in ("🛠 Новая заявка", "🧾 Мои заявки", "🛒 Покупка", "🛠 Заявки на ремонт", "🛒 Покупки", "📓 Журнал"):
        if text == "🛠 Новая заявка":
            PENDING_NEW[uid] = "repair"
            await u.message.reply_text("Опиши проблему одним сообщением — создам заявку на ремонт."); return
        if text == "🛒 Покупка":
            PENDING_NEW[uid] = "purchase"
            await u.message.reply_text("Напиши, что купить и для чего — создам заявку на покупку."); return
        if text == "🧾 Мои заявки":
            await cmd_my(u,c); return
        if text == "🛠 Заявки на ремонт":
            await cmd_repairs(u,c); return
        if text == "🛒 Покупки":
            if is_admin(uid): await cmd_purchases(u,c)
            else: await u.message.reply_text("Недостаточно прав."); return
        if text == "📓 Журнал":
            if is_admin(uid): await cmd_journal(u,c)
            else: await u.message.reply_text("Недостаточно прав."); return

    # Причины (отмена/отклонение) — только для админа
    if is_admin(uid) and uid in PENDING_REASON:
        action, tid = PENDING_REASON.pop(uid)
        reason = text
        if not reason:
            await u.message.reply_text("Причина не должна быть пустой. Напиши текст причины.")
            PENDING_REASON[uid]=(action, tid); return
        if action == "reject_purchase":
            await update_ticket(tid, status="rejected")
            await u.message.reply_text(f"🛒 Заявка №{tid} отклонена. Причина сохранена.")
        elif action == "cancel_repair":
            await update_ticket(tid, status="canceled")
            await u.message.reply_text(f"🛠 Заявка №{tid} отменена. Причина сохранена.")
        row = await get_ticket(tid)
        if row:
            author_id = row[5]
            note = "отклонена ❌" if action=="reject_purchase" else "отменена ❌"
            try: await c.bot.send_message(author_id, f"ℹ️ Ваша заявка №{tid} {note}. Причина: {reason}")
            except: pass
        return

    # Ожидаем текст после нажатия «Новая заявка/Покупка»
    if uid in PENDING_NEW:
        mode = PENDING_NEW.pop(uid)
        if mode == "repair":
            tid = await add_ticket("repair", u.effective_chat.id, uid, u.effective_user.username, text)
            await u.message.reply_text(f"🛠 Заявка №{tid} создана.")
            for aid in ADMIN_IDS:
                try: await c.bot.send_message(aid, f"🛠 Новая заявка №{tid}\n{text}")
                except: pass
            return
        else:
            tid = await add_ticket("purchase", u.effective_chat.id, uid, u.effective_user.username, text)
            await u.message.reply_text(f"🛒 Заявка на покупку №{tid} создана. Ожидает решения.")
            for aid in ADMIN_IDS:
                try:
                    await c.bot.send_message(aid,
                        f"🛒 Новая заявка на покупку №{tid}\nОт @{u.effective_user.username or uid}\n— {text}",
                        reply_markup=kb_purchase_admin(tid))
                except: pass
            return

    # Обычный текст → заявка на ремонт
    if not text or text.startswith("/"):
        return
    tid = await add_ticket("repair", u.effective_chat.id, uid, u.effective_user.username, text)
    await u.message.reply_text(f"🛠 Заявка №{tid} создана.")
    for aid in ADMIN_IDS:
        try: await c.bot.send_message(aid, f"🛠 Новая заявка №{tid}\n{text}")
        except: pass

# ---------- кнопки ----------
async def on_btn(u:Update, c:ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    data = q.data

    # Меню назначения механику
    if data.startswith("assign_menu:"):
        tid = int(data.split(":")[1])
        if not is_admin(q.from_user.id):
            await q.answer("Недостаточно прав", show_alert=True); return
        try: await q.edit_message_reply_markup(reply_markup=kb_assign_list(tid))
        except: await q.message.reply_text("Выбери механика:", reply_markup=kb_assign_list(tid))
        return

    if data.startswith("assign_back:"):
        tid = int(data.split(":")[1])
        if not is_admin(q.from_user.id):
            await q.answer("Недостаточно прав", show_alert=True); return
        row = await get_ticket(tid)
        if row:
            is_assignee = (row[9] == q.from_user.id)
            try:
                await q.edit_message_reply_markup(
                    reply_markup=kb_repair(tid, True, is_assignee, tech=False)
                )
            except:
                await q.message.reply_text(fmt_ticket(row),
                    reply_markup=kb_repair(tid, True, is_assignee, tech=False))
        return

    if data.startswith("assign_to:"):
        _, tid_s, uid_s = data.split(":")
        tid = int(tid_s); who = int(uid_s)
        if not is_admin(q.from_user.id):
            await q.answer("Недостаточно прав", show_alert=True); return
        await update_ticket(tid, assignee_id=who, assignee_name=str(who))
        row = await get_ticket(tid)
        try:
            await q.edit_message_text(fmt_ticket(row),
                reply_markup=kb_repair(tid, True, is_assignee=False, tech=False))
        except:
            await q.message.reply_text(fmt_ticket(row),
                reply_markup=kb_repair(tid, True, is_assignee=False, tech=False))
        try: await c.bot.send_message(who, f"🔔 Вам назначена заявка №{tid}")
        except: pass
        return

    # Покупки: одобрить / отклонить с причиной
    if data.startswith("approve_") or data.startswith("reject_with_reason_"):
        tid = int(data.rsplit("_", 1)[1])
        if data.startswith("approve_"):
            if not is_admin(q.from_user.id):
                await q.answer("Недостаточно прав", show_alert=True); return
            await update_ticket(tid, status="approved")
            try: await q.edit_message_text(f"🛒 Заявка №{tid} одобрена ✅")
            except: pass
            row = await get_ticket(tid)
            if row:
                try: await c.bot.send_message(row[5], f"🛒 Ваша заявка №{tid} одобрена ✅")
                except: pass
            return
        else:
            if not is_admin(q.from_user.id):
                await q.answer("Недостаточно прав", show_alert=True); return
            PENDING_REASON[q.from_user.id] = ("reject_purchase", tid)
            try: await q.edit_message_text(f"📝 Введите причину отклонения для заявки №{tid} одним сообщением.")
            except: pass
            return

    # Ремонты
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
    assignee_uid = row[9]
    is_assignee = (assignee_uid == uid)

    if action == "assign" and admin:
        await update_ticket(tid, assignee_id=uid, assignee_name=q.from_user.full_name)
        row = await get_ticket(tid)
        try:
            await q.edit_message_text(fmt_ticket(row),
                reply_markup=kb_repair(tid, True, True, tech=False))
        except: pass
        return

    if action == "to_work" and (admin or is_assignee or is_tech(uid)):
        updates = {"status": "in_work"}
        if assignee_uid is None and is_tech(uid):
            updates["assignee_id"] = uid
            updates["assignee_name"] = q.from_user.full_name
        started_at = row[13] or now_iso()
        updates["started_at"] = started_at
        await update_ticket(tid, **updates)
        row = await get_ticket(tid)
        try:
            await q.edit_message_text(
                fmt_ticket(row),
                reply_markup=kb_repair(tid, admin, is_assignee=(row[9]==uid), tech=is_tech(uid))
            )
        except: pass
        return

    if action == "done" and (admin or is_assignee or is_tech(uid)):
        if assignee_uid is not None and assignee_uid != uid and not admin:
            await q.answer("Закрыть может назначенный исполнитель или админ", show_alert=True); return
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
        try:
            await q.edit_message_text(fmt_ticket(row),
                reply_markup=kb_repair(tid, admin, is_assignee, tech=is_tech(uid)))
        except: pass
        return

    if action == "cancel_with_reason" and admin:
        PENDING_REASON[uid] = ("cancel_repair", tid)
        try: await q.edit_message_text(f"📝 Введите причину отмены для заявки №{tid} одним сообщением.")
        except: pass
        return

# ---------- main ----------
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN отсутствует. Добавь его в Secrets.")
    await init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("new",     cmd_new))
    app.add_handler(CommandHandler("buy",     cmd_buy))
    app.add_handler(CommandHandler("my",      cmd_my))
    app.add_handler(CommandHandler("repairs", cmd_repairs))
    app.add_handler(CommandHandler("purchases", cmd_purchases))
    app.add_handler(CommandHandler("find",    cmd_find))
    app.add_handler(CommandHandler("export",  cmd_export))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.PHOTO, any_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_text))

    print("ITS bot running...\nBot is polling for updates...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
