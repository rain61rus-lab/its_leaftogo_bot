#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Helpdesk Bot (Admin / Mechanic / User)
- python-telegram-bot v20+ (async)
- aiosqlite for storage
- Roles: admin, mechanic, user
- Mechanics see all NEW tickets and can "take" them. Only assignee can close.
- Admin can assign tickets to a mechanic, or to self; can manage mechanics.
- Journal shows: Created, Taken, Done, Duration.
- Export supports "created" and "done" modes.
- Race-safe "take in work".
- Local timezone display via zoneinfo.

Fill BOT_TOKEN via environment variable BOT_TOKEN or hardcode below.
"""

import asyncio
import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import aiosqlite
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ---------------- Configuration ----------------
# Set your local timezone for display purposes
LOCAL_TZ = ZoneInfo("Europe/Moscow")   # Change if needed

# BOT TOKEN
BOT_TOKEN = None  # fallback to env if None

# Database file
DB_PATH = "helpdesk.sqlite3"

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("helpdesk-bot")

# ---------------- Data Models ----------------
@dataclass
class Ticket:
    id: int
    creator_id: int
    creator_username: Optional[str]
    assignee_id: Optional[int]
    assignee_username: Optional[str]
    status: str              # new | in_work | done | declined
    description: str
    created_at: Optional[datetime]
    started_at: Optional[datetime]
    done_at: Optional[datetime]


# ---------------- Utility ----------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def duration_str(started: Optional[datetime], done: Optional[datetime]) -> str:
    if not started or not done:
        return "—"
    diff = done - started
    total_min = int(diff.total_seconds() // 60)
    h, m = divmod(total_min, 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


def status_human(s: str) -> str:
    return {"new": "🆕 Новая", "in_work": "⏱ В работе", "done": "✅ Готово", "declined": "🚫 Отказ"}.\
        get(s, s)


async def fetch_username(db: aiosqlite.Connection, uid: int) -> Optional[str]:
    async with db.execute("SELECT last_username FROM users WHERE uid=?", (uid,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return row[0]


# ---------------- Database ----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            uid INTEGER PRIMARY KEY,
            role TEXT NOT NULL DEFAULT 'user',     -- admin | mechanic | user
            last_username TEXT
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER NOT NULL,
            assignee_id INTEGER,
            status TEXT NOT NULL DEFAULT 'new',
            description TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            started_at TIMESTAMP,
            done_at TIMESTAMP
        );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_dates ON tickets(created_at, started_at, done_at);")
        await db.commit()


async def ensure_user(db: aiosqlite.Connection, uid: int, username: Optional[str]):
    async with db.execute("SELECT uid FROM users WHERE uid=?", (uid,)) as cur:
        row = await cur.fetchone()
    if row:
        await db.execute("UPDATE users SET last_username=? WHERE uid=?", (username, uid))
    else:
        await db.execute("INSERT INTO users(uid, role, last_username) VALUES(?,?,?)",
                         (uid, "user", username))
    await db.commit()


async def set_role(db: aiosqlite.Connection, uid: int, role: str):
    await db.execute("INSERT INTO users(uid, role) VALUES(?, ?) ON CONFLICT(uid) DO UPDATE SET role=excluded.role",
                     (uid, role))
    await db.commit()


async def get_role(db: aiosqlite.Connection, uid: int) -> str:
    async with db.execute("SELECT role FROM users WHERE uid=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else "user"


async def list_mechanics(db: aiosqlite.Connection) -> List[int]:
    res = []
    async with db.execute("SELECT uid FROM users WHERE role='mechanic' ORDER BY uid") as cur:
        async for r in cur:
            res.append(r[0])
    return res


async def insert_ticket(db: aiosqlite.Connection, creator_id: int, description: str) -> int:
    await db.execute(
        "INSERT INTO tickets(creator_id, description, created_at) VALUES (?,?,?)",
        (creator_id, description, now_utc())
    )
    await db.commit()
    async with db.execute("SELECT last_insert_rowid()") as cur:
        row = await cur.fetchone()
    return int(row[0])


async def get_ticket(db: aiosqlite.Connection, tid: int) -> Optional[Ticket]:
    async with db.execute("""
    SELECT t.id, t.creator_id, u1.last_username, t.assignee_id, u2.last_username,
           t.status, t.description, t.created_at, t.started_at, t.done_at
      FROM tickets t
      LEFT JOIN users u1 ON u1.uid=t.creator_id
      LEFT JOIN users u2 ON u2.uid=t.assignee_id
     WHERE t.id=?
    """, (tid,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    # Parse datetimes as aware UTC
    def parse(dt):
        if dt is None:
            return None
        if isinstance(dt, str):
            try:
                return datetime.fromisoformat(dt).replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.strptime(dt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if isinstance(dt, datetime):
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        return None

    return Ticket(
        id=row[0],
        creator_id=row[1],
        creator_username=row[2],
        assignee_id=row[3],
        assignee_username=row[4],
        status=row[5],
        description=row[6],
        created_at=parse(row[7]),
        started_at=parse(row[8]),
        done_at=parse(row[9]),
    )


async def list_new_and_my_active(db: aiosqlite.Connection, uid: int) -> List[Ticket]:
    # NEW tickets + tickets in_work assigned to me (active until done/declined)
    out: List[Ticket] = []
    async with db.execute("""
    SELECT t.id, t.creator_id, u1.last_username, t.assignee_id, u2.last_username,
           t.status, t.description, t.created_at, t.started_at, t.done_at
      FROM tickets t
      LEFT JOIN users u1 ON u1.uid=t.creator_id
      LEFT JOIN users u2 ON u2.uid=t.assignee_id
     WHERE (t.status='new') OR (t.status='in_work' AND t.assignee_id=?)
     ORDER BY CASE WHEN t.status='in_work' THEN 0 ELSE 1 END, t.created_at ASC
    """, (uid,)) as cur:
        async for row in cur:
            out.append(Ticket(
                id=row[0], creator_id=row[1], creator_username=row[2],
                assignee_id=row[3], assignee_username=row[4],
                status=row[5], description=row[6],
                created_at=row[7] and datetime.fromisoformat(row[7]) if isinstance(row[7], str) else row[7],
                started_at=row[8] and datetime.fromisoformat(row[8]) if isinstance(row[8], str) else row[8],
                done_at=row[9] and datetime.fromisoformat(row[9]) if isinstance(row[9], str) else row[9],
            ))
    # normalize tz
    for t in out:
        for attr in ("created_at", "started_at", "done_at"):
            dt = getattr(t, attr)
            if isinstance(dt, datetime) and dt.tzinfo is None:
                setattr(t, attr, dt.replace(tzinfo=timezone.utc))
    return out


async def journal_rows(db: aiosqlite.Connection, limit: int = 50) -> List[Ticket]:
    out: List[Ticket] = []
    async with db.execute("""
    SELECT t.id, t.creator_id, u1.last_username, t.assignee_id, u2.last_username,
           t.status, t.description, t.created_at, t.started_at, t.done_at
      FROM tickets t
      LEFT JOIN users u1 ON u1.uid=t.creator_id
      LEFT JOIN users u2 ON u2.uid=t.assignee_id
     ORDER BY t.id DESC
     LIMIT ?
    """, (limit,)) as cur:
        async for row in cur:
            out.append(Ticket(
                id=row[0], creator_id=row[1], creator_username=row[2],
                assignee_id=row[3], assignee_username=row[4],
                status=row[5], description=row[6],
                created_at=row[7] and datetime.fromisoformat(row[7]) if isinstance(row[7], str) else row[7],
                started_at=row[8] and datetime.fromisoformat(row[8]) if isinstance(row[8], str) else row[8],
                done_at=row[9] and datetime.fromisoformat(row[9]) if isinstance(row[9], str) else row[9],
            ))
    for t in out:
        for attr in ("created_at", "started_at", "done_at"):
            dt = getattr(t, attr)
            if isinstance(dt, datetime) and dt.tzinfo is None:
                setattr(t, attr, dt.replace(tzinfo=timezone.utc))
    return out


# ---------------- Keyboards ----------------
def ticket_kb(t: Ticket, is_admin: bool, me_id: int) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []

    # Row 1: For mechanics/admins
    if t.status == "new":
        # Any mechanic (or admin acting as mechanic) can take it
        kb.append([InlineKeyboardButton("⏱ В работу", callback_data=f"take:{t.id}")])
        if is_admin:
            kb[-1].append(InlineKeyboardButton("👤 Назначить себе", callback_data=f"assign_self:{t.id}"))
            kb.append([InlineKeyboardButton("👥 Назначить механику", callback_data=f"assign_menu:{t.id}")])
    elif t.status == "in_work":
        # Only assignee can close or decline
        if t.assignee_id == me_id:
            kb.append([
                InlineKeyboardButton("✅ Закрыть (Готово)", callback_data=f"done:{t.id}"),
                InlineKeyboardButton("🚫 Отказ", callback_data=f"decline:{t.id}"),
            ])
    # Row N: Journal/back for assign menu
    kb.append([InlineKeyboardButton("📒 Журнал", callback_data=f"journal")])
    return InlineKeyboardMarkup(kb)


def assign_menu_kb(tid: int, mechanics: List[Tuple[int, Optional[str]]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for idx, (uid, uname) in enumerate(mechanics, start=1):
        label = f"@{uname}" if uname else str(uid)
        row.append(InlineKeyboardButton(label, callback_data=f"assign_to:{tid}:{uid}"))
        if idx % 3 == 0:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"assign_back:{tid}")])
    return InlineKeyboardMarkup(rows)


# ---------------- Bot Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user.id, user.username)

    text = (
        "Привет, это робот инженерно‑технической службы.\n\n"
        "Команды:\n"
        "/new — создать заявку\n"
        "/me — мои активные заявки\n"
        "/whoami — показать твой ID и username\n"
        "/journal — журнал последних заявок\n"
        "/export — экспорт CSV (параметры: mode=created|done; days=N)\n"
        "Админ: /add_mech, /rm_mech, /roles"
    )
    await update.message.reply_text(text)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uname_line = f"@{user.username}" if user.username else "—"
    async with aiosqlite.connect(DB_PATH) as db:
        role = await get_role(db, user.id)
    await update.message.reply_text(f"Твой user_id: {user.id}\nusername: {uname_line}\nrole: {role}")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Опиши проблему после команды: /new Не работает конвейер №2")
        return
    desc = " ".join(context.args).strip()
    user = update.effective_user
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user.id, user.username)
        tid = await insert_ticket(db, user.id, desc)
        t = await get_ticket(db, tid)
        role = await get_role(db, user.id)
    kb = ticket_kb(t, is_admin=(role == "admin"), me_id=user.id)
    text = (
        f"🆕 Заявка #{t.id}\n"
        f"Статус: {status_human(t.status)}\n"
        f"Автор: @{t.creator_username if t.creator_username else t.creator_id}\n"
        f"Создана: {to_local(t.created_at)}\n\n"
        f"{t.description}"
    )
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user.id, user.username)
        role = await get_role(db, user.id)
        my = await list_new_and_my_active(db, user.id)

    if not my:
        await update.message.reply_text("Нет доступных заявок.")
        return

    for t in my:
        kb = ticket_kb(t, is_admin=(role == "admin"), me_id=user.id)
        text = (
            f"#{t.id} • {status_human(t.status)}\n"
            f"Автор: @{t.creator_username if t.creator_username else t.creator_id}\n"
            f"Исполнитель: @{t.assignee_username if t.assignee_username else '—'}\n"
            f"Создана: {to_local(t.created_at)}\n"
            f"Взята: {to_local(t.started_at)}\n"
            f"Готово: {to_local(t.done_at)}\n"
            f"Длит.: {duration_str(t.started_at, t.done_at)}\n\n"
            f"{t.description}"
        )
        await update.message.reply_text(text, reply_markup=kb)


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        items = await journal_rows(db, limit=30)
    if not items:
        await update.message.reply_text("Журнал пуст.")
        return

    lines = []
    for t in items:
        lines.append(
            f"#{t.id} • {status_human(t.status)} • Автор: @{t.creator_username if t.creator_username else t.creator_id} • "
            f"Исп.: @{t.assignee_username if t.assignee_username else '—'}\n"
            f"Создана: {to_local(t.created_at)} • "
            f"Взята: {to_local(t.started_at)} • "
            f"Готово: {to_local(t.done_at)} • "
            f"Длит.: {duration_str(t.started_at, t.done_at)}\n"
            f"{t.description}\n"
            f"{'-'*40}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /export mode=created days=7
    /export mode=done days=7
    """
    params = {"mode": "created", "days": "7"}
    for p in context.args:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip()] = v.strip()

    mode = params.get("mode", "created")
    try:
        days = int(params.get("days", "7"))
    except Exception:
        days = 7

    since = now_utc() - timedelta(days=days)

    query_base = """
    SELECT t.id, t.creator_id, u1.last_username, t.assignee_id, u2.last_username,
           t.status, t.description, t.created_at, t.started_at, t.done_at
      FROM tickets t
      LEFT JOIN users u1 ON u1.uid=t.creator_id
      LEFT JOIN users u2 ON u2.uid=t.assignee_id
    """
    if mode == "done":
        where = "WHERE t.done_at >= ?"
        arg = since
    else:
        where = "WHERE t.created_at >= ?"
        arg = since

    async with aiosqlite.connect(DB_PATH) as db:
        rows = []
        async with db.execute(query_base + where + " ORDER BY t.id ASC", (arg,)) as cur:
            async for r in cur:
                rows.append(r)

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "creator", "creator_username", "assignee", "assignee_username",
                "status", "description", "created_at_local", "started_at_local", "done_at_local", "duration_min"])
    for r in rows:
        id_, creator, cu, assignee, au, st, desc, c_at, s_at, d_at = r
        # convert to aware
        def parse(dt):
            if dt is None: return None
            if isinstance(dt, str):
                try:
                    return datetime.fromisoformat(dt).replace(tzinfo=timezone.utc)
                except Exception:
                    return datetime.strptime(dt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        c_at = parse(c_at); s_at = parse(s_at); d_at = parse(d_at)
        dur_min = int((d_at - s_at).total_seconds() // 60) if (s_at and d_at) else ""
        w.writerow([id_, creator, cu or "", assignee or "", au or "", st, (desc or "").replace("\n", " "),
                    to_local(c_at), to_local(s_at), to_local(d_at), dur_min])

    out.seek(0)
    await update.message.reply_document(document=InputFile(out, filename=f"export_{mode}.csv"))


# -------------- Admin Commands --------------
async def cmd_add_mech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with aiosqlite.connect(DB_PATH) as db:
        role = await get_role(db, user.id)
        if role != "admin":
            await update.message.reply_text("Только админ может добавлять механиков.")
            return
        if not context.args:
            await update.message.reply_text("Использование: /add_mech <user_id>")
            return
        try:
            uid = int(context.args[0])
        except Exception:
            await update.message.reply_text("user_id должен быть числом.")
            return
        await set_role(db, uid, "mechanic")
        await update.message.reply_text(f"Пользователь {uid} добавлен как механик.")


async def cmd_rm_mech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with aiosqlite.connect(DB_PATH) as db:
        role = await get_role(db, user.id)
        if role != "admin":
            await update.message.reply_text("Только админ может убирать механиков.")
            return
        if not context.args:
            await update.message.reply_text("Использование: /rm_mech <user_id>")
            return
        try:
            uid = int(context.args[0])
        except Exception:
            await update.message.reply_text("user_id должен быть числом.")
            return
        await set_role(db, uid, "user")
        await update.message.reply_text(f"Пользователь {uid} переведён в роль user.")


async def cmd_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        out = []
        async with db.execute("SELECT uid, role, COALESCE(last_username,'') FROM users ORDER BY role, uid") as cur:
            async for uid, role, uname in cur:
                label = f"@{uname}" if uname else str(uid)
                out.append(f"{label} — {role}")
    await update.message.reply_text("Роли пользователей:\n" + "\n".join(out) if out else "Нет пользователей.")


# -------------- Callback Logic --------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user

    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user.id, user.username)
        role = await get_role(db, user.id)

        # journal quick button
        if data == "journal":
            items = await journal_rows(db, 15)
            if not items:
                await query.edit_message_text("Журнал пуст.")
                return
            lines = []
            for t in items:
                lines.append(
                    f"#{t.id} • {status_human(t.status)} • Автор: @{t.creator_username if t.creator_username else t.creator_id} • "
                    f"Исп.: @{t.assignee_username if t.assignee_username else '—'}\n"
                    f"Создана: {to_local(t.created_at)} • "
                    f"Взята: {to_local(t.started_at)} • "
                    f"Готово: {to_local(t.done_at)} • "
                    f"Длит.: {duration_str(t.started_at, t.done_at)}\n"
                    f"{t.description}\n"
                    f"{'-'*40}"
                )
            await query.edit_message_text("\n".join(lines))
            return

        # assign back
        if data.startswith("assign_back:"):
            tid = int(data.split(":")[1])
            t = await get_ticket(db, tid)
            if not t:
                await query.edit_message_text("Заявка не найдена.")
                return
            kb = ticket_kb(t, is_admin=(role == "admin"), me_id=user.id)
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        # take in work (race-safe)
        if data.startswith("take:"):
            tid = int(data.split(":")[1])
            # Only mechanics or admin can take
            if role not in ("mechanic", "admin"):
                await query.answer("Только механик или админ может взять заявку.", show_alert=True)
                return

            # UPDATE with condition to prevent race
            async with db.execute("""
                UPDATE tickets
                   SET status='in_work', assignee_id=?, started_at=?
                 WHERE id=? AND status='new' AND (assignee_id IS NULL)
            """, (user.id, now_utc(), tid)) as cur:
                await db.commit()
                if cur.rowcount == 0:
                    # either already taken or not new
                    await query.answer("Заявка уже взята другим или не новая.", show_alert=True)
                else:
                    await query.answer("Взята в работу.")
            t = await get_ticket(db, tid)
            kb = ticket_kb(t, is_admin=(role == "admin"), me_id=user.id)
            text = (
                f"#{t.id} • {status_human(t.status)}\n"
                f"Автор: @{t.creator_username if t.creator_username else t.creator_id}\n"
                f"Исполнитель: @{t.assignee_username if t.assignee_username else '—'}\n"
                f"Создана: {to_local(t.created_at)}\n"
                f"Взята: {to_local(t.started_at)}\n"
                f"Готово: {to_local(t.done_at)}\n"
                f"Длит.: {duration_str(t.started_at, t.done_at)}\n\n"
                f"{t.description}"
            )
            await query.edit_message_text(text, reply_markup=kb)
            return

        # assign to self (admin only)
        if data.startswith("assign_self:"):
            tid = int(data.split(":")[1])
            if role != "admin":
                await query.answer("Только админ может назначать исполнителя.", show_alert=True)
                return
            # allow assign only if new
            async with db.execute("""
                UPDATE tickets
                   SET assignee_id=?, started_at=?, status='in_work'
                 WHERE id=? AND status='new' AND (assignee_id IS NULL)
            """, (user.id, now_utc(), tid)) as cur:
                await db.commit()
                if cur.rowcount == 0:
                    await query.answer("Заявка уже не новая или назначена.", show_alert=True)
                else:
                    await query.answer("Назначена на вас и взята в работу.")
            t = await get_ticket(db, tid)
            kb = ticket_kb(t, is_admin=(role == "admin"), me_id=user.id)
            text = (
                f"#{t.id} • {status_human(t.status)}\n"
                f"Автор: @{t.creator_username if t.creator_username else t.creator_id}\n"
                f"Исполнитель: @{t.assignee_username if t.assignee_username else '—'}\n"
                f"Создана: {to_local(t.created_at)}\n"
                f"Взята: {to_local(t.started_at)}\n"
                f"Готово: {to_local(t.done_at)}\n"
                f"Длит.: {duration_str(t.started_at, t.done_at)}\n\n"
                f"{t.description}"
            )
            await query.edit_message_text(text, reply_markup=kb)
            return

        # open assign menu (admin only)
        if data.startswith("assign_menu:"):
            tid = int(data.split(":")[1])
            if role != "admin":
                await query.answer("Только админ может назначать исполнителя.", show_alert=True)
                return
            # build mechanics list (id + username)
            ulist: List[Tuple[int, Optional[str]]] = []
            mechs = await list_mechanics(db)
            for m in mechs:
                ulist.append((m, await fetch_username(db, m)))
            kb = assign_menu_kb(tid, ulist)
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        # assign to specific mechanic (admin only)
        if data.startswith("assign_to:"):
            _, tid_s, uid_s = data.split(":")
            tid, mech_uid = int(tid_s), int(uid_s)
            if role != "admin":
                await query.answer("Только админ может назначать исполнителя.", show_alert=True)
                return
            # only if ticket is still new
            async with db.execute("""
                UPDATE tickets
                   SET assignee_id=?, started_at=?, status='in_work'
                 WHERE id=? AND status='new' AND (assignee_id IS NULL)
            """, (mech_uid, now_utc(), tid)) as cur:
                await db.commit()
                if cur.rowcount == 0:
                    await query.answer("Заявка уже не новая или назначена.", show_alert=True)
                else:
                    await query.answer(f"Назначено механику {mech_uid}.")
            t = await get_ticket(db, tid)
            kb = ticket_kb(t, is_admin=(role == "admin"), me_id=user.id)
            text = (
                f"#{t.id} • {status_human(t.status)}\n"
                f"Автор: @{t.creator_username if t.creator_username else t.creator_id}\n"
                f"Исполнитель: @{t.assignee_username if t.assignee_username else '—'}\n"
                f"Создана: {to_local(t.created_at)}\n"
                f"Взята: {to_local(t.started_at)}\n"
                f"Готово: {to_local(t.done_at)}\n"
                f"Длит.: {duration_str(t.started_at, t.done_at)}\n\n"
                f"{t.description}"
            )
            await query.edit_message_text(text, reply_markup=kb)
            return

        # mark done (only assignee)
        if data.startswith("done:"):
            tid = int(data.split(":")[1])
            t = await get_ticket(db, tid)
            if not t:
                await query.answer("Заявка не найдена.", show_alert=True)
                return
            if t.assignee_id != user.id:
                await query.answer("Закрыть может только исполнитель.", show_alert=True)
                return
            if t.status != "in_work":
                await query.answer("Заявка должна быть в работе.", show_alert=True)
                return
            await db.execute("UPDATE tickets SET status='done', done_at=? WHERE id=?", (now_utc(), tid))
            await db.commit()
            t = await get_ticket(db, tid)
            kb = ticket_kb(t, is_admin=(role == "admin"), me_id=user.id)
            text = (
                f"#{t.id} • {status_human(t.status)}\n"
                f"Автор: @{t.creator_username if t.creator_username else t.creator_id}\n"
                f"Исполнитель: @{t.assignee_username if t.assignee_username else '—'}\n"
                f"Создана: {to_local(t.created_at)}\n"
                f"Взята: {to_local(t.started_at)}\n"
                f"Готово: {to_local(t.done_at)}\n"
                f"Длит.: {duration_str(t.started_at, t.done_at)}\n\n"
                f"{t.description}"
            )
            await query.edit_message_text(text, reply_markup=kb)
            return

        # decline (only assignee)
        if data.startswith("decline:"):
            tid = int(data.split(":")[1])
            t = await get_ticket(db, tid)
            if not t:
                await query.answer("Заявка не найдена.", show_alert=True)
                return
            if t.assignee_id != user.id:
                await query.answer("Отказ фиксирует только исполнитель.", show_alert=True)
                return
            if t.status != "in_work":
                await query.answer("Заявка должна быть в работе.", show_alert=True)
                return
            await db.execute("UPDATE tickets SET status='declined', done_at=? WHERE id=?", (now_utc(), tid))
            await db.commit()
            t = await get_ticket(db, tid)
            kb = ticket_kb(t, is_admin=(role == "admin"), me_id=user.id)
            text = (
                f"#{t.id} • {status_human(t.status)}\n"
                f"Автор: @{t.creator_username if t.creator_username else t.creator_id}\n"
                f"Исполнитель: @{t.assignee_username if t.assignee_username else '—'}\n"
                f"Создана: {to_local(t.created_at)}\n"
                f"Взята: {to_local(t.started_at)}\n"
                f"Готово: {to_local(t.done_at)}\n"
                f"Длит.: {duration_str(t.started_at, t.done_at)}\n\n"
                f"{t.description}"
            )
            await query.edit_message_text(text, reply_markup=kb)
            return

    # default
    await query.answer("Неизвестное действие.")


# ---------------- Main ----------------
def require_token() -> str:
    tok = BOT_TOKEN or os.getenv("BOT_TOKEN")
    if not tok:
        raise RuntimeError("Set BOT_TOKEN env var or edit BOT_TOKEN in script.")
    return tok


async def main():
    await init_db()
    token = require_token()

    app: Application = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(CommandHandler("export", cmd_export))

    # Admin
    app.add_handler(CommandHandler("add_mech", cmd_add_mech))
    app.add_handler(CommandHandler("rm_mech", cmd_rm_mech))
    app.add_handler(CommandHandler("roles", cmd_roles))

    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Bot started.")
    await app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
