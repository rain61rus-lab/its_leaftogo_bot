
# its_helpdesk_bot_updated_full.py
# Telegram bot for IT/Engineering service desk
# Requires: python-telegram-bot==20.7, aiosqlite
# Run: BOT_TOKEN=... ADMIN_IDS=12345 TECH_IDS=67890 python its_helpdesk_bot_updated_full.py

import os
import io
import csv
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ------------------ CONFIG & LOGGING ------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables.")

# ENV roles (optional)
ENV_ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(",", " ").split() if x.isdigit()
}
ENV_TECH_IDS = {
    int(x) for x in os.getenv("TECH_IDS", "").replace(",", " ").split() if x.isdigit()
}

# Logging
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("its-helpdesk-bot")

if not ENV_ADMIN_IDS:
    log.warning("No ADMIN_IDS provided via ENV. You can add admins with /add_admin <id> (for existing admin only).")

# ------------------ CONSTANTS ------------------

DB_PATH = "its_helpdesk.sqlite3"

TZ = timezone.utc  # store everything in UTC
DATE_FMT = "%Y-%m-%d %H:%M"

KIND_REPAIR = "repair"
KIND_PURCHASE = "purchase"

STATUS_NEW = "new"
STATUS_IN_WORK = "in_work"
STATUS_DONE = "done"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELED = "canceled"

PRIORITIES = ["low", "normal", "high"]

MAIN_MENU_BUTTONS_COMMON = [
    [KeyboardButton("🛠 Заявка на ремонт"), KeyboardButton("🧾 Мои заявки")],
    [KeyboardButton("🛒 Заявка на покупку"), KeyboardButton("🛠 Заявки на ремонт")],
]
# Admin-only row
ADMIN_ROW = [KeyboardButton("🛒 Покупки"), KeyboardButton("📓 Журнал")]

# user_data keys
UD_MODE = "mode"  # values: None | "create_repair" | "create_purchase" | "await_reason"
UD_REASON_CONTEXT = "reason_ctx"  # dict with action, ticket_id

# ------------------ UTILS ------------------

def now_utc():
    return datetime.now(tz=TZ)

def fmt_dt(dt_str: str | None) -> str:
    if not dt_str:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime(DATE_FMT)
    except Exception:
        return dt_str

def human_duration(start_iso: str | None, end_iso: str | None) -> str:
    if not start_iso or not end_iso:
        return "—"
    try:
        s = datetime.fromisoformat(start_iso)
        e = datetime.fromisoformat(end_iso)
        if e < s:
            s, e = e, s
        delta = e - s
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        parts = []
        if days:
            parts.append(f"{days}д")
        if hours:
            parts.append(f"{hours}ч")
        if minutes or not parts:
            parts.append(f"{minutes}м")
        return " ".join(parts)
    except Exception:
        return "—"

def chunk_text(s: str, limit: int = 4000):
    # Telegram message limit is ~4096
    for i in range(0, len(s), limit):
        yield s[i:i+limit]

def ensure_int(s: str) -> int | None:
    try:
        return int(s)
    except Exception:
        return None

# ------------------ DB INIT & ROLES ------------------

async def init_db(app: Application):
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")

    # tickets table
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT CHECK(kind IN ('repair','purchase')) NOT NULL,
            status TEXT NOT NULL,
            priority TEXT CHECK(priority IN ('low','normal','high')) NOT NULL DEFAULT 'normal',
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            description TEXT NOT NULL,
            photo_file_id TEXT,
            assignee_id INTEGER,
            assignee_name TEXT,
            reason TEXT, -- NEW: stores cancel/reject reason
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            done_at TEXT
        )
        """
    )
    # Indexes
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_kind ON tickets(kind);")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);")

    # Simple users-role table
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            uid INTEGER PRIMARY KEY,
            role TEXT CHECK(role IN ('admin','tech')),
            last_username TEXT,
            last_seen TEXT
        )
        """
    )

    # MIGRATION: ensure 'reason' column exists (if upgrading from older schema)
    try:
        async with db.execute("PRAGMA table_info(tickets);") as cursor:
            cols = [row[1] async for row in cursor]  # name at index 1
        if "reason" not in cols:
            await db.execute("ALTER TABLE tickets ADD COLUMN reason TEXT;")
            log.info("DB migration: added 'reason' column to tickets.")
    except Exception as e:
        log.warning(f"DB migration check failed: {e}")

    await db.commit()
    app.bot_data["db"] = db

async def db_close(app: Application):
    db: aiosqlite.Connection | None = app.bot_data.get("db")
    if db:
        await db.close()

async def db_add_user_role(db, uid: int, role: str):
    await db.execute(
        "INSERT INTO users(uid, role) VALUES(?, ?) ON CONFLICT(uid) DO UPDATE SET role=excluded.role",
        (uid, role),
    )
    await db.commit()


async def db_seen_user(db, uid: int, username: str | None):
    uname = (username or "").strip() or None
    now = now_utc().isoformat()
    await db.execute(
        "INSERT INTO users(uid, role, last_username, last_seen) VALUES(?, NULL, ?, ?) ON CONFLICT(uid) DO UPDATE SET last_username=excluded.last_username, last_seen=excluded.last_seen",
        (uid, uname, now),
    )
    await db.commit()

async def db_lookup_uid_by_username(db, username: str) -> int | None:
    uname = username.lstrip('@').strip().lower()
    async with db.execute("SELECT uid FROM users WHERE lower(last_username)=? LIMIT 1", (uname,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    uname = update.effective_user.username or "—"
    await db_seen_user(db, uid, update.effective_user.username)
    await update.message.reply_text(f"Твой user_id: {uid}\nusername: @{uname}")

async def db_list_roles(db):
    admins = set(ENV_ADMIN_IDS)
    techs = set(ENV_TECH_IDS)
    async with db.execute("SELECT uid, role FROM users") as cur:
        async for uid, role in cur:
            if role == "admin":
                admins.add(uid)
            elif role == "tech":
                techs.add(uid)
    return sorted(admins), sorted(techs)

async def is_admin(db, uid: int) -> bool:
    if uid in ENV_ADMIN_IDS:
        return True
    async with db.execute("SELECT 1 FROM users WHERE uid=? AND role='admin' LIMIT 1", (uid,)) as cur:
        row = await cur.fetchone()
    return bool(row)

async def is_tech(db, uid: int) -> bool:
    if uid in ENV_TECH_IDS or await is_admin(db, uid):
        return True
    async with db.execute("SELECT 1 FROM users WHERE uid=? AND role='tech' LIMIT 1", (uid,)) as cur:
        row = await cur.fetchone()
    return bool(row)

# ------------------ UI BUILDERS ------------------

async def main_menu(db, uid: int):
    rows = list(MAIN_MENU_BUTTONS_COMMON)
    if await is_admin(db, uid):
        rows.append(ADMIN_ROW)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def ticket_inline_kb(ticket: dict, is_admin_flag: bool, is_tech_flag: bool, me_id: int):
    kb = []
    # Priority (admin only)
    if ticket["kind"] == KIND_REPAIR:
        if is_admin_flag:
            kb.append([InlineKeyboardButton("⚡ Приоритет ↑", callback_data=f"prio:{ticket['id']}")])
        # Assignments (admin only)
        if is_admin_flag:
            kb.append([
                InlineKeyboardButton("👤 Назначить себе", callback_data=f"assign_self:{ticket['id']}"),
                InlineKeyboardButton("👥 Назначить механику", callback_data=f"assign_menu:{ticket['id']}"),
            ])
        # To Work
        kb.append([InlineKeyboardButton("⏱ В работу", callback_data=f"to_work:{ticket['id']}")])
        # Done (admin or assignee)
        if is_admin_flag or (ticket.get("assignee_id") == me_id):
            kb.append([InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{ticket['id']}")])
            kb.append([InlineKeyboardButton("🛑 Отказ (с комментарием)", callback_data=f"decline:{ticket['id']}")])
        # Cancel (admin)
        if is_admin_flag:
            kb.append([InlineKeyboardButton("🗑 Отмена (с причиной)", callback_data=f"cancel:{ticket['id']}")])
    elif ticket["kind"] == KIND_PURCHASE:
        if is_admin_flag:
            kb.append([
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{ticket['id']}"),
                InlineKeyboardButton("🛑 Отклонить (с причиной)", callback_data=f"reject:{ticket['id']}"),
            ])
    return InlineKeyboardMarkup(kb) if kb else None

def assign_menu_kb(tech_list: list[int]):
    kb = []
    row = []
    for idx, uid in enumerate(tech_list, start=1):
        row.append(InlineKeyboardButton(f"{uid}", callback_data=f"assign_to:{uid}"))
        if idx % 3 == 0:
            kb.append(row); row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("↩️ Назад", callback_data="assign_back")])
    return InlineKeyboardMarkup(kb)

# ------------------ TICKET HELPERS ------------------

async def create_ticket(db, *, kind: str, chat_id: int, user_id: int, username: str | None, description: str, photo_file_id: str | None):
    now = now_utc().isoformat()
    await db.execute(
        """
        INSERT INTO tickets(kind, status, priority, chat_id, user_id, username, description, photo_file_id, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (kind, STATUS_NEW, "normal", chat_id, user_id, username, description.strip(), photo_file_id, now, now),
    )
    await db.commit()

async def find_tickets(db, *, kind: str | None = None, status: str | None = None, user_id: int | None = None,
                       assignee_id: int | None = None, unassigned_only: bool = False, q: str | None = None,
                       limit: int = 20, offset: int = 0):
    sql = "SELECT id, kind, status, priority, chat_id, user_id, username, description, photo_file_id, assignee_id, assignee_name, reason, created_at, updated_at, started_at, done_at FROM tickets"
    where, params = [], []
    if kind:
        where.append("kind=?"); params.append(kind)
    if status:
        where.append("status=?"); params.append(status)
    if user_id is not None:
        where.append("user_id=?"); params.append(user_id)
    if assignee_id is not None:
        where.append("assignee_id=?"); params.append(assignee_id)
    if unassigned_only:
        where.append("assignee_id IS NULL")
    if q:
        if q.startswith("#") and q[1:].isdigit():
            where.append("id=?"); params.append(int(q[1:]))
        else:
            where.append("description LIKE ?"); params.append(f"%{q}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = []
    async with db.execute(sql, params) as cur:
        async for row in cur:
            rows.append({
                "id": row[0], "kind": row[1], "status": row[2], "priority": row[3], "chat_id": row[4],
                "user_id": row[5], "username": row[6], "description": row[7], "photo_file_id": row[8],
                "assignee_id": row[9], "assignee_name": row[10], "reason": row[11], "created_at": row[12],
                "updated_at": row[13], "started_at": row[14], "done_at": row[15],
            })
    return rows

async def get_ticket(db, ticket_id: int) -> dict | None:
    async with db.execute(
        "SELECT id, kind, status, priority, chat_id, user_id, username, description, photo_file_id, assignee_id, assignee_name, reason, created_at, updated_at, started_at, done_at FROM tickets WHERE id=?",
        (ticket_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "kind": row[1], "status": row[2], "priority": row[3], "chat_id": row[4],
        "user_id": row[5], "username": row[6], "description": row[7], "photo_file_id": row[8],
        "assignee_id": row[9], "assignee_name": row[10], "reason": row[11], "created_at": row[12],
        "updated_at": row[13], "started_at": row[14], "done_at": row[15],
    }

async def update_ticket(db, ticket_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = now_utc().isoformat()
    cols = ", ".join([f"{k}=?" for k in fields.keys()])
    params = list(fields.values()) + [ticket_id]
    await db.execute(f"UPDATE tickets SET {cols} WHERE id=?", params)
    await db.commit()

# ------------------ MESSAGES RENDER ------------------

def render_ticket_line(t: dict) -> str:
    if t["kind"] == KIND_REPAIR:
        icon = "🛠"
        stat = {
            STATUS_NEW: "🆕 Новая",
            STATUS_IN_WORK: "⏱ В работе",
            STATUS_DONE: "✅ Выполнена",
            STATUS_REJECTED: "🛑 Отказ исполнителя",
            STATUS_CANCELED: "🗑 Отменена",
        }.get(t["status"], t["status"])
        assgn = f" • Исполнитель: {t['assignee_name'] or t['assignee_id'] or '—'}"
        times = f"\nСоздана: {fmt_dt(t['created_at'])}"
        if t["started_at"]:
            times += f" • Взята: {fmt_dt(t['started_at'])}"
        if t["done_at"]:
            times += f" • Готово: {fmt_dt(t['done_at'])} • Длит.: {human_duration(t['started_at'], t['done_at'])}"
        prio = f" • Приоритет: {t['priority']}"
        reason = f"\nПричина: {t['reason']}" if t["status"] in (STATUS_CANCELED, STATUS_REJECTED) and t.get("reason") else ""
        return f"{icon} #{t['id']} • {stat}{prio}{assgn}\n{t['description']}{times}{reason}"
    else:
        icon = "🛒"
        stat = {
            STATUS_NEW: "🆕 Новая",
            STATUS_APPROVED: "✅ Одобрена",
            STATUS_REJECTED: "🛑 Отклонена",
            STATUS_CANCELED: "🗑 Отменена",
        }.get(t["status"], t["status"])
        times = f"\nСоздана: {fmt_dt(t['created_at'])}"
        reason = f"\nПричина: {t['reason']}" if t["status"] in (STATUS_REJECTED, STATUS_CANCELED) and t.get("reason") else ""
        return f"{icon} #{t['id']} • {stat}\n{t['description']}{times}{reason}"

# ------------------ HANDLERS ------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)
    kb = await main_menu(db, uid)
    # Greeting as requested
    text = "Привет это робот инженерно технической службы"
    await update.message.reply_text(text, reply_markup=kb)
    context.user_data[UD_MODE] = None

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Команды и действия:\n\n"
        "• 🛠 Заявка на ремонт — нажми кнопку, затем отправь описание или фото с подписью.\n"
        "• 🛒 Заявка на покупку — нажми кнопку и отправь описание.\n"
        "• 🧾 Мои заявки — список твоих заявок.\n"
        "• 🛠 Заявки на ремонт — новые заявки (и свои в работе).\n"
        "• 📓 Журнал — закрытые ремонты за период (для админов).\n\n"
        "Команды:\n"
        "/repairs [status] [page] — список заявок на ремонт. status: new|in_work|done|all.\n"
        "/me [status] [page] — мои заявки как исполнителя (механика). status: new|in_work|done|all.\n"
        "/find <текст|#id> — поиск заявок (для админов).\n"
        "/export [week|month] — экспорт CSV.\n"
        "/journal [days] — журнал за N дней (по умолчанию 30).\n"
        "/add_admin <id> — добавить админа (для админов).\n"
        "/add_tech <id> — добавить техника (для админов).\n"
        "/roles — показать роли.\n"
        "/whoami — показать твой user_id.\n"
        "/help — эта справка.\n"
    )
    await update.message.reply_text(text)

# --- Text buttons ---

async def on_text_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)
    text = (update.message.text or "").strip()

    if text == "🛠 Заявка на ремонт":
        context.user_data[UD_MODE] = "create_repair"
        await update.message.reply_text("Опиши проблему. Можно прикрепить фото с подписью.")
        return

    if text == "🛒 Заявка на покупку":
        context.user_data[UD_MODE] = "create_purchase"
        await update.message.reply_text("Опиши, что нужно купить (наименование, количество, почему).")
        return

    if text == "🧾 Мои заявки":
        rows = await find_tickets(db, user_id=uid, limit=20, offset=0)
        if not rows:
            await update.message.reply_text("У тебя пока нет заявок.")
            return
        for t in rows[:20]:
            await update.message.reply_text(render_ticket_line(t))
        return

    if text == "🛠 Заявки на ремонт":
        admin = await is_admin(db, uid)
        if admin:
            rows = await find_tickets(db, kind=KIND_REPAIR, status=STATUS_NEW, limit=20, offset=0)
            if not rows:
                await update.message.reply_text("Нет новых заявок на ремонт.")
                return
            for t in rows:
                kb = ticket_inline_kb(t, is_admin_flag=True, is_tech_flag=True, me_id=uid)
                await update.message.reply_text(render_ticket_line(t), reply_markup=kb)
        else:
            # For tech: show unassigned NEW and their IN_WORK
            new_rows = await find_tickets(db, kind=KIND_REPAIR, status=STATUS_NEW, unassigned_only=True, limit=20, offset=0)
            in_rows = await find_tickets(db, kind=KIND_REPAIR, status=STATUS_IN_WORK, assignee_id=uid, limit=20, offset=0)
            if not new_rows and not in_rows:
                await update.message.reply_text("Нет доступных заявок.")
                return
            for t in (new_rows + in_rows):
                kb = ticket_inline_kb(t, is_admin_flag=False, is_tech_flag=True, me_id=uid)
                await update.message.reply_text(render_ticket_line(t), reply_markup=kb)
        return

    if text == "🛒 Покупки":
        if not await is_admin(db, uid):
            await update.message.reply_text("Недостаточно прав.")
            return
        rows = await find_tickets(db, kind=KIND_PURCHASE, status=STATUS_NEW, limit=20, offset=0)
        if not rows:
            await update.message.reply_text("Нет новых заявок на покупку.")
            return
        for t in rows:
            kb = ticket_inline_kb(t, is_admin_flag=True, is_tech_flag=True, me_id=uid)
            await update.message.reply_text(render_ticket_line(t), reply_markup=kb)
        return

    if text == "📓 Журнал":
        if not await is_admin(db, uid):
            await update.message.reply_text("Недостаточно прав.")
            return
        await cmd_journal(update, context)
        return

    # Fallback: if awaiting reason
    if context.user_data.get(UD_MODE) == "await_reason":
        await handle_reason_input(update, context)
        return

    # If in create mode, treat message as description
    if context.user_data.get(UD_MODE) in ("create_repair", "create_purchase"):
        await handle_create_from_text(update, context)
        return

    # Otherwise ignore free-form text to avoid accidental ticket creation
    await update.message.reply_text("Используй кнопки меню или /help.")

# --- Create flows ---

async def handle_create_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)
    uname = update.effective_user.username or ""
    chat_id = update.message.chat_id
    mode = context.user_data.get(UD_MODE)

    description = (update.message.text or "").strip()
    if not description:
        await update.message.reply_text("Опиши заявку текстом.")
        return

    if mode == "create_repair":
        await create_ticket(db, kind=KIND_REPAIR, chat_id=chat_id, user_id=uid, username=uname, description=description, photo_file_id=None)
        await update.message.reply_text("Заявка на ремонт создана. Админы уведомлены.")
        await notify_admins(context, f"🆕 Новая заявка на ремонт от @{uname or uid}:\n{description}")
        context.user_data[UD_MODE] = None
        return
    elif mode == "create_purchase":
        await create_ticket(db, kind=KIND_PURCHASE, chat_id=chat_id, user_id=uid, username=uname, description=description, photo_file_id=None)
        await update.message.reply_text("Заявка на покупку отправлена. Ожидает решения админа.")
        await notify_admins(context, f"🆕 Новая заявка на покупку от @{uname or uid}:\n{description}")
        context.user_data[UD_MODE] = None
        return

async def on_photo_with_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only create repair if we are in create_repair mode
    if context.user_data.get(UD_MODE) != "create_repair":
        await update.message.reply_text("Чтобы создать заявку с фото, нажми «🛠 Заявка на ремонт», затем пришли фото с подписью.")
        return
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)
    uname = update.effective_user.username or ""
    chat_id = update.message.chat_id

    caption = (update.message.caption or "").strip()
    if not caption:
        await update.message.reply_text("Добавь подпись к фото — это будет описанием заявки.")
        return

    # take last photo size
    photo = update.message.photo[-1]
    file_id = photo.file_id

    await create_ticket(db, kind=KIND_REPAIR, chat_id=chat_id, user_id=uid, username=uname, description=caption, photo_file_id=file_id)
    await update.message.reply_text("Заявка на ремонт с фото создана. Админы уведомлены.")
    await notify_admins(context, f"🆕 Новая заявка на ремонт с фото от @{uname or uid}:\n{caption}")
    context.user_data[UD_MODE] = None

# --- Admin search/export/journal ---

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Использование: /find <строка|#id>")
        return
    rows = await find_tickets(db, q=q, limit=50, offset=0)
    if not rows:
        await update.message.reply_text("Ничего не найдено.")
        return
    for t in rows:
        kb = ticket_inline_kb(t, is_admin_flag=True, is_tech_flag=True, me_id=uid)
        await update.message.reply_text(render_ticket_line(t), reply_markup=kb)

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return
    period = (context.args[0].lower() if context.args else "week").strip()
    if period not in ("week", "month"):
        await update.message.reply_text("Использование: /export [week|month]")
        return
    now = now_utc()
    start = now - (timedelta(days=7) if period == "week" else timedelta(days=30))
    rows = await export_rows(db, start_iso=start.isoformat())
    if not rows:
        await update.message.reply_text("Нет данных для экспорта.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id","kind","status","priority","user_id","username","assignee_id","assignee_name","created_at","started_at","done_at","duration","reason","description"])
    for r in rows:
        dur = human_duration(r["started_at"], r["done_at"])
        writer.writerow([
            r["id"], r["kind"], r["status"], r["priority"], r["user_id"], r["username"] or "",
            r["assignee_id"] or "", r["assignee_name"] or "", r["created_at"], r["started_at"] or "",
            r["done_at"] or "", dur, r["reason"] or "", r["description"].replace("\n"," ")[:500],
        ])
    data = buf.getvalue().encode("utf-8")
    await update.message.reply_document(
        document=InputFile(io.BytesIO(data), filename=f"tickets_{period}.csv"),
        caption=f"Экспорт за {period}."
    )

async def export_rows(db, start_iso: str):
    async with db.execute(
        """
        SELECT id, kind, status, priority, user_id, username, assignee_id, assignee_name,
               created_at, started_at, done_at, reason, description
        FROM tickets
        WHERE created_at >= ?
        ORDER BY id DESC
        """,
        (start_iso,)
    ) as cur:
        rows = []
        async for row in cur:
            rows.append({
                "id": row[0], "kind": row[1], "status": row[2], "priority": row[3],
                "user_id": row[4], "username": row[5], "assignee_id": row[6], "assignee_name": row[7],
                "created_at": row[8], "started_at": row[9], "done_at": row[10], "reason": row[11],
                "description": row[12],
            })
    return rows

async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return
    days = ensure_int(context.args[0]) if context.args else 30
    days = days or 30
    since = now_utc() - timedelta(days=days)
    async with db.execute(
        """
        SELECT id, description, assignee_name, assignee_id, started_at, done_at, created_at, reason
        FROM tickets
        WHERE kind='repair' AND status='done' AND done_at >= ?
        ORDER BY done_at DESC
        """,
        (since.isoformat(),)
    ) as cur:
        items = await cur.fetchall()
    if not items:
        await update.message.reply_text("Журнал пуст.")
        return
    lines = []
    for i in items:
        id_, desc, aname, aid, started, done, created, reason = i
        dur = human_duration(started, done)
        who = aname or aid or "—"
        line = f"#{id_} • {who} • Взята: {fmt_dt(started)} • Готово: {fmt_dt(done)} • Длит.: {dur}\n{desc}"
        if reason:
            line += f"\nПричина: {reason}"
        lines.append(line)
    text = "\n\n".join(lines)
    for part in chunk_text(text):
        await update.message.reply_text(part)

# --- Repairs list with filters/pagination ---

async def cmd_repairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    status = (context.args[0].lower() if context.args else "new").strip()
    page = ensure_int(context.args[1]) if len(context.args) >= 2 else 1
    page = max(1, page or 1)
    offset = (page - 1) * 20

    status_map = {"new": STATUS_NEW, "in_work": STATUS_IN_WORK, "done": STATUS_DONE, "all": None}
    stat = status_map.get(status, STATUS_NEW)

    # Admin: show all; Tech: show new unassigned + their in_work/done
    admin = await is_admin(db, uid)
    if admin:
        rows = await find_tickets(db, kind=KIND_REPAIR, status=stat, limit=20, offset=offset) if stat else \
               await find_tickets(db, kind=KIND_REPAIR, limit=20, offset=offset)
    else:
        if stat == STATUS_NEW or stat is None:
            rows = await find_tickets(db, kind=KIND_REPAIR, status=STATUS_NEW, unassigned_only=True, limit=20, offset=offset)
        elif stat == STATUS_IN_WORK:
            rows = await find_tickets(db, kind=KIND_REPAIR, status=STATUS_IN_WORK, assignee_id=uid, limit=20, offset=offset)
        elif stat == STATUS_DONE:
            rows = await find_tickets(db, kind=KIND_REPAIR, status=STATUS_DONE, assignee_id=uid, limit=20, offset=offset)
        else:
            rows = []

    if not rows:
        await update.message.reply_text("Ничего не найдено.")
        return

    for t in rows:
        kb = ticket_inline_kb(t, is_admin_flag=admin, is_tech_flag=await is_tech(db, uid), me_id=uid)
        await update.message.reply_text(render_ticket_line(t), reply_markup=kb)

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    status = (context.args[0].lower() if context.args else "in_work").strip()
    page = ensure_int(context.args[1]) if len(context.args) >= 2 else 1
    page = max(1, page or 1)
    offset = (page - 1) * 20

    status_map = {"new": STATUS_NEW, "in_work": STATUS_IN_WORK, "done": STATUS_DONE, "all": None}
    stat = status_map.get(status, STATUS_IN_WORK)

    if stat:
        rows = await find_tickets(db, kind=KIND_REPAIR, status=stat, assignee_id=uid, limit=20, offset=offset)
    else:
        rows = await find_tickets(db, kind=KIND_REPAIR, assignee_id=uid, limit=20, offset=offset)
    if not rows:
        await update.message.reply_text("Пока пусто.")
        return
    for t in rows:
        kb = ticket_inline_kb(t, is_admin_flag=await is_admin(db, uid), is_tech_flag=True, me_id=uid)
        await update.message.reply_text(render_ticket_line(t), reply_markup=kb)

# --- Roles management ---

async def cmd_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /add_admin <user_id|@username>")
        return
    arg = context.args[0].strip()
    target = ensure_int(arg)
    if not target and arg.startswith('@'):
        target = await db_lookup_uid_by_username(db, arg)
    if not target:
        await update.message.reply_text("Укажи числовой user_id или известный @username (после того как пользователь написал боту /start).")
        return
    await db_add_user_role(db, target, "admin")
    await update.message.reply_text(f"Пользователь {target} добавлен как admin.")

async def cmd_add_tech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /add_tech <user_id|@username>")
        return
    arg = context.args[0].strip()
    target = ensure_int(arg)
    if not target and arg.startswith('@'):
        target = await db_lookup_uid_by_username(db, arg)
    if not target:
        await update.message.reply_text("Укажи числовой user_id или известный @username (после того как пользователь написал боту /start).")
        return
    await db_add_user_role(db, target, "tech")
    await update.message.reply_text(f"Пользователь {target} добавлен как tech.")

async def cmd_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    admins, techs = await db_list_roles(db)
    text = "Роли:\n\nАдмины:\n" + (", ".join(map(str, admins)) or "—") + "\n\nМеханики:\n" + (", ".join(map(str, techs)) or "—")
    await update.message.reply_text(text)

# --- Callback actions ---

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    uname = update.effective_user.username or ""

    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # Assign menu (admin opens list of techs)
    if data.startswith("assign_menu:"):
        if not await is_admin(db, uid):
            await query.edit_message_text("Недостаточно прав.")
            return
        admins, techs = await db_list_roles(db)
        kb = assign_menu_kb(techs)
        await query.edit_message_reply_markup(reply_markup=kb)
        return

    if data == "assign_back":
        await query.answer("Выбери техника или команду ниже.", show_alert=False)
        return

    if data.startswith("assign_to:"):
        if not await is_admin(db, uid):
            await query.edit_message_text("Недостаточно прав.")
            return
        tid = extract_ticket_id_from_message(query.message.text or "")
        assignee = ensure_int(data.split(":",1)[1])
        if not tid or not assignee:
            await query.answer("Не удалось определить заявку/пользователя.")
            return
        await update_ticket(db, tid, assignee_id=assignee, assignee_name=str(assignee))
        await query.edit_message_text((query.message.text or "") + f"\n\nНазначено: {assignee}")
        try:
            await context.bot.send_message(chat_id=assignee, text=f"Вам назначена заявка #{tid}.")
        except Exception as e:
            log.debug(f"Notify assignee {assignee} failed: {e}")
        return

    if data.startswith("assign_self:"):
        if not await is_admin(db, uid):
            await query.edit_message_text("Недостаточно прав.")
            return
        tid = ensure_int(data.split(":",1)[1])
        if not tid:
            return
        await update_ticket(db, tid, assignee_id=uid, assignee_name=f"@{uname or uid}")
        await query.edit_message_text((query.message.text or "") + f"\n\nНазначено: @{uname or uid}")
        return

    if data.startswith("prio:"):
        if not await is_admin(db, uid):
            await query.edit_message_text("Недостаточно прав.")
            return
        tid = ensure_int(data.split(":",1)[1])
        t = await get_ticket(db, tid)
        if not t:
            await query.answer("Заявка не найдена.")
            return
        cur = t["priority"]
        try:
            idx = PRIORITIES.index(cur)
            new = PRIORITIES[min(idx+1, len(PRIORITIES)-1)]
        except Exception:
            new = "normal"
        await update_ticket(db, tid, priority=new)
        await query.edit_message_text((query.message.text or "") + f"\n\nПриоритет: {new}")
        return

    if data.startswith("to_work:"):
        tid = ensure_int(data.split(":",1)[1])
        if not tid:
            return
        t = await get_ticket(db, tid)
        if not t or t["kind"] != KIND_REPAIR:
            await query.answer("Некорректная заявка.")
            return
        # Only for NEW status
        if t["status"] != STATUS_NEW:
            await query.answer("Заявка уже не новая.")
            return
        # Race guard: if assigned to someone else and not admin — deny
        if t["assignee_id"] and t["assignee_id"] != uid and not await is_admin(db, uid):
            await query.answer("Заявка назначена другому.")
            return
        now_iso = now_utc().isoformat()
        # If not assigned, set to current user
        if not t["assignee_id"]:
            await update_ticket(db, tid, assignee_id=uid, assignee_name=f"@{uname or uid}")
        await update_ticket(db, tid, status=STATUS_IN_WORK, started_at=t["started_at"] or now_iso)
        await query.edit_message_text((query.message.text or "") + "\n\nСтатус: ⏱ В работе")
        return

    if data.startswith("done:"):
        tid = ensure_int(data.split(":",1)[1])
        t = await get_ticket(db, tid)
        if not t or t["kind"] != KIND_REPAIR:
            await query.answer("Некорректная заявка.")
            return
        admin = await is_admin(db, uid)
        if not admin and t.get("assignee_id") != uid:
            await query.answer("Только исполнитель или админ может закрыть.")
            return
        await update_ticket(db, tid, status=STATUS_DONE, done_at=now_utc().isoformat())
        await query.edit_message_text((query.message.text or "") + "\n\nСтатус: ✅ Выполнена")
        return

    if data.startswith("cancel:"):
        if not await is_admin(db, uid):
            await query.edit_message_text("Недостаточно прав.")
            return
        tid = ensure_int(data.split(":",1)[1])
        context.user_data[UD_MODE] = "await_reason"
        context.user_data[UD_REASON_CONTEXT] = {"action": "cancel", "ticket_id": tid}
        await query.edit_message_text((query.message.text or "") + "\n\nНапиши причину отмены сообщением:")
        return

    if data.startswith("approve:"):
        if not await is_admin(db, uid):
            await query.edit_message_text("Недостаточно прав.")
            return
        tid = ensure_int(data.split(":",1)[1])
        await update_ticket(db, tid, status=STATUS_APPROVED)
        await query.edit_message_text((query.message.text or "") + "\n\nСтатус: ✅ Одобрена")
        # notify author
        t = await get_ticket(db, tid)
        if t:
            try:
                await context.bot.send_message(chat_id=t["user_id"], text=f"Твоя заявка на покупку #{tid} одобрена.")
            except Exception as e:
                log.debug(f"Notify author failed: {e}")
        return


    if data.startswith("decline:"):
        tid = ensure_int(data.split(":",1)[1])
        if not tid:
            return
        t = await get_ticket(db, tid)
        if not t or t["kind"] != KIND_REPAIR:
            await query.answer("Некорректная заявка.")
            return
        admin = await is_admin(db, uid)
        if not admin and t.get("assignee_id") != uid:
            await query.answer("Только исполнитель или админ может отказать.")
            return
        context.user_data[UD_MODE] = "await_reason"
        context.user_data[UD_REASON_CONTEXT] = {"action": "decline_repair", "ticket_id": tid}
        await query.edit_message_text((query.message.text or "") + "\n\nНапиши причину отказа сообщением:")
        return

    if data.startswith("reject:"):
        if not await is_admin(db, uid):
            await query.edit_message_text("Недостаточно прав.")
            return
        tid = ensure_int(data.split(":",1)[1])
        context.user_data[UD_MODE] = "await_reason"
        context.user_data[UD_REASON_CONTEXT] = {"action": "reject", "ticket_id": tid}
        await query.edit_message_text((query.message.text or "") + "\n\nНапиши причину отказа сообщением:")
        return

def extract_ticket_id_from_message(text: str) -> int | None:
    # expects format like "🛠 #123 • ..." or "🛒 #45 • ..."
    try:
        parts = text.split("#", 1)
        if len(parts) < 2:
            return None
        tail = parts[1]
        num = ""
        for ch in tail:
            if ch.isdigit():
                num += ch
            else:
                break
        return int(num) if num else None
    except Exception:
        return None

async def handle_reason_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    reason_text = (update.message.text or "").strip()
    if not reason_text:
        await update.message.reply_text("Причина не может быть пустой. Напиши текстом.")
        return
    ctx = context.user_data.get(UD_REASON_CONTEXT) or {}
    tid = ctx.get("ticket_id")
    action = ctx.get("action")
    if not tid or action not in ("cancel", "reject"):
        await update.message.reply_text("Контекст потерян. Попробуй снова.")
        context.user_data[UD_MODE] = None
        context.user_data[UD_REASON_CONTEXT] = None
        return
    t = await get_ticket(db, tid)
    if not t:
        await update.message.reply_text("Заявка не найдена.")
        context.user_data[UD_MODE] = None
        context.user_data[UD_REASON_CONTEXT] = None
        return
    if action == "cancel":
        await update_ticket(db, tid, status=STATUS_CANCELED, reason=reason_text)
        await update.message.reply_text(f"Заявка #{tid} отменена.\nПричина: {reason_text}")
    elif action == "decline_repair":
        await update_ticket(db, tid, status=STATUS_REJECTED, reason=reason_text)
        await update.message.reply_text(f"Заявка #{tid} — отказ исполнителя.\nКомментарий: {reason_text}")
        try:
            await context.bot.send_message(chat_id=t["user_id"], text=f"Твоя заявка #{tid} — отказ исполнителя.\nКомментарий: {reason_text}")
        except Exception as e:
            log.debug(f"Notify author failed: {e}")
    else:
        await update_ticket(db, tid, status=STATUS_REJECTED, reason=reason_text)
        await update.message.reply_text(f"Заявка #{tid} отклонена.\nПричина: {reason_text}")
        try:
            await context.bot.send_message(chat_id=t["user_id"], text=f"Твоя заявка #{tid} отклонена.\nПричина: {reason_text}")
        except Exception as e:
            log.debug(f"Notify author failed: {e}")
    context.user_data[UD_MODE] = None
    context.user_data[UD_REASON_CONTEXT] = None

# --- Helpers ---

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    db = context.application.bot_data["db"]
    admins, _ = await db_list_roles(db)
    for aid in admins:
        try:
            await context.bot.send_message(chat_id=aid, text=text)
        except Exception as e:
            log.debug(f"Notify admin {aid} failed: {e}")

# ------------------ APP BUILD & RUN ------------------

def register_handlers(app: Application):
    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(CommandHandler("repairs", cmd_repairs))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("add_admin", cmd_add_admin))
    app.add_handler(CommandHandler("add_tech", cmd_add_tech))
    app.add_handler(CommandHandler("roles", cmd_roles))
    app.add_handler(CommandHandler("whoami", cmd_whoami))

    # Callback buttons
    app.add_handler(CallbackQueryHandler(cb_handler))

    # Photos with caption (only in create_repair mode)
    app.add_handler(MessageHandler(filters.PHOTO & filters.Caption(True), on_photo_with_caption))

    # Text buttons / free-form
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_button))

async def on_startup(app: Application):
    await init_db(app)
    log.info("ITS bot running...")

async def on_shutdown(app: Application):
    await db_close(app)
    log.info("ITS bot stopped.")

def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    register_handlers(application)
    application.post_init = on_startup
    application.post_shutdown = on_shutdown
    # Run polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
