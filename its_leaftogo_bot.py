# its_helpdesk_bot.py
# Telegram bot for engineering/maintenance helpdesk
# python-telegram-bot==20.7, aiosqlite
#
# Обновления (24.10.2025):
# 1. Перед описанием поломки пользователь выбирает помещение и срочность:
#    - 🟢 Плановое (low)
#    - 🟡 Срочно, простой (normal)
#    - 🔴 Авария, линия стоит (high)
#    Приоритет сохраняется в ticket.priority.
#
# 2. Механик при "✅ Выполнено" теперь НЕ закрывает сразу. Бот просит:
#    "Пришли фото результата или напиши 'готово'".
#    После фото/текста заявка уходит в DONE, фиксируется done_at,
#    и фото результата сохраняется в done_photo_file_id.
#
# 3. Автор заявки получает уведомления:
#    - когда механик взял в работу;
#    - когда механик закрыл.
#
# 4. У механика в карточке ремонта появилась кнопка
#    "🛒 Требует закупку".
#    После неё бот спрашивает "Что купить?" и сам создаёт заявку на покупку,
#    в описании пишет "Запчасть для заявки #ID (место): ...".
#
# Плюс все старые фичи:
# - механики могут создавать ремонт/покупку и видеть свои заявки;
# - помещение выбирается из быстрых кнопок (цех варки 1 и т.д.);
# - карточки с фото приходят механикам/админам как фото с подписью;
# - админ может назначать механику, повышать приоритет;
# - журнал/экспорт для админов.

import os
import io
import csv
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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

# ------------------ НАСТРОЙКИ ------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Укажи BOT_TOKEN в переменных окружения.")

# Запиши сюда свой user_id как админа.
# Узнать можно через /whoami.
HARD_ADMIN_IDS = {826495316}

ENV_ADMIN_IDS = {
    int(x)
    for x in os.getenv("ADMIN_IDS", "").replace(",", " ").split()
    if x.isdigit()
}
# ENV_TECH_IDS не обязателен (механику можно выдать роль через /add_tech)
ENV_TECH_IDS: set[int] = set()

# ------------------ ЛОГИ ------------------

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

# ------------------ КОНСТАНТЫ ------------------

DB_PATH = "its_helpdesk.sqlite3"

# МСК (UTC+3)
TZ = timezone(timedelta(hours=3), name="MSK")
DATE_FMT = "%Y-%m-%d %H:%M"

KIND_REPAIR = "repair"
KIND_PURCHASE = "purchase"

STATUS_NEW = "new"
STATUS_IN_WORK = "in_work"
STATUS_DONE = "done"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELED = "canceled"

PRIORITIES = ["low", "normal", "high"]  # low=плановое, normal=срочно, high=авария

# user_data keys / состояния диалогов
# UD_MODE:
#   None
#   "choose_location_repair"   - выбрать помещение
#   "input_location_repair"    - ввести помещение руками
#   "choose_priority_repair"   - выбрать срочность
#   "create_repair"            - написать описание ремонта / отправить фото с подписью
#   "create_purchase"          - написать заявку на покупку вручную
#   "await_reason"             - ожидание причины отказа/отмены
#   "await_done_photo"         - механик закрывает заявку: ждём фото или текст
#   "await_buy_desc"           - механик запросил закупку по ремонту
UD_MODE = "mode"

UD_REASON_CONTEXT = "reason_ctx"     # {action, ticket_id}
UD_REPAIR_LOC = "repair_location"    # выбранное помещение
UD_REPAIR_PRIORITY = "repair_priority"  # low/normal/high
UD_DONE_CTX = "done_ctx"             # ticket_id для завершения
UD_BUY_CONTEXT = "buy_ctx"           # {ticket_id} для закупки

# быстрый список помещений
LOCATIONS = [
    "цех варки 1",
    "цех варки 2",
    "растарочная",
    "цех фасовки порошка",
    "цех фасовки капсул",
    "цех фасовки полуфабрикатов",
    "административный отдел",
    "склад",
]
LOC_OTHER = "Другое помещение…"
LOC_CANCEL = "↩ Отмена"

# ------------------ УТИЛИТЫ ------------------

def now_local():
    return datetime.now(tz=TZ)

def fmt_dt(dt_str: str | None) -> str:
    if not dt_str:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ).strftime(DATE_FMT)
    except Exception:
        return dt_str

def human_duration(start_iso: str | None, end_iso: str | None) -> str:
    if not start_iso or not end_iso:
        return "—"
    try:
        s = datetime.fromisoformat(start_iso)
        e = datetime.fromisoformat(end_iso)
        if s.tzinfo is None:
            s = s.replace(tzinfo=TZ)
        if e.tzinfo is None:
            e = e.replace(tzinfo=TZ)
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
    for i in range(0, len(s), limit):
        yield s[i:i+limit]

def ensure_int(s: str) -> int | None:
    try:
        return int(s)
    except Exception:
        return None

# ------------------ БАЗА ДАННЫХ ------------------

async def init_db(app: Application):
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")

    # В таблицу tickets добавлены:
    # - location TEXT
    # - reason TEXT
    # - done_photo_file_id TEXT (фото после ремонта)
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
            done_photo_file_id TEXT,
            assignee_id INTEGER,
            assignee_name TEXT,
            location TEXT,
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            done_at TEXT
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_kind ON tickets(kind);")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);")

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

    # миграции (добавить колонки если их не было)
    try:
        async with db.execute("PRAGMA table_info(tickets);") as cur:
            cols = [row[1] async for row in cur]
        if "reason" not in cols:
            await db.execute("ALTER TABLE tickets ADD COLUMN reason TEXT;")
        if "location" not in cols:
            await db.execute("ALTER TABLE tickets ADD COLUMN location TEXT;")
        if "done_photo_file_id" not in cols:
            await db.execute("ALTER TABLE tickets ADD COLUMN done_photo_file_id TEXT;")
    except Exception as e:
        log.warning(f"DB migration (tickets) check failed: {e}")

    try:
        async with db.execute("PRAGMA table_info(users);") as cur:
            cols = [row[1] async for row in cur]
        if "last_username" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN last_username TEXT;")
        if "last_seen" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN last_seen TEXT;")
    except Exception as e:
        log.warning(f"DB migration (users) check failed: {e}")

    await db.commit()
    app.bot_data["db"] = db

async def db_close(app: Application):
    db: aiosqlite.Connection | None = app.bot_data.get("db")
    if db:
        await db.close()

async def db_add_user_role(db, uid: int, role: str):
    await db.execute(
        "INSERT INTO users(uid, role) VALUES(?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET role=excluded.role",
        (uid, role),
    )
    await db.commit()

async def db_seen_user(db, uid: int, username: str | None):
    uname = (username or "").strip() or None
    now = now_local().isoformat()
    await db.execute(
        "INSERT INTO users(uid, role, last_username, last_seen) "
        "VALUES(?, NULL, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET "
        "last_username=excluded.last_username, "
        "last_seen=excluded.last_seen",
        (uid, uname, now),
    )
    await db.commit()

async def db_lookup_uid_by_username(db, username: str) -> int | None:
    uname = username.lstrip('@').strip().lower()
    async with db.execute(
        "SELECT uid FROM users WHERE lower(last_username)=? LIMIT 1", (uname,)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None

async def db_list_roles(db):
    admins = set(HARD_ADMIN_IDS) | set(ENV_ADMIN_IDS)
    techs = set(ENV_TECH_IDS)
    async with db.execute("SELECT uid, role FROM users") as cur:
        async for uid, role in cur:
            if role == "admin":
                admins.add(uid)
            elif role == "tech":
                techs.add(uid)
    return sorted(admins), sorted(techs)

async def is_admin(db, uid: int) -> bool:
    if uid in HARD_ADMIN_IDS or uid in ENV_ADMIN_IDS:
        return True
    async with db.execute(
        "SELECT 1 FROM users WHERE uid=? AND role='admin' LIMIT 1", (uid,)
    ) as cur:
        row = await cur.fetchone()
    return bool(row)

async def is_tech(db, uid: int) -> bool:
    if uid in ENV_TECH_IDS or await is_admin(db, uid):
        return True
    async with db.execute(
        "SELECT 1 FROM users WHERE uid=? AND role='tech' LIMIT 1", (uid,)
    ) as cur:
        row = await cur.fetchone()
    return bool(row)

# ------------------ МЕНЮ ------------------

async def main_menu(db, uid: int):
    if await is_admin(db, uid):
        rows = [
            [KeyboardButton("🛠 Заявка на ремонт"), KeyboardButton("🧾 Мои заявки")],
            [KeyboardButton("🛒 Заявка на покупку"), KeyboardButton("🛒 Мои покупки")],
            [KeyboardButton("🛠 Заявки на ремонт")],
            [KeyboardButton("🛒 Покупки"), KeyboardButton("📓 Журнал")],
        ]
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    if await is_tech(db, uid):
        rows = [
            [KeyboardButton("🛠 Заявка на ремонт"), KeyboardButton("🧾 Мои заявки")],
            [KeyboardButton("🛒 Заявка на покупку"), KeyboardButton("🛒 Мои покупки")],
            [KeyboardButton("🛠 Заявки на ремонт")],
        ]
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    rows = [
        [KeyboardButton("🛠 Заявка на ремонт"), KeyboardButton("🧾 Мои заявки")],
        [KeyboardButton("🛒 Заявка на покупку"), KeyboardButton("🛒 Мои покупки")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def ticket_inline_kb(ticket: dict, is_admin_flag: bool, me_id: int):
    kb = []
    if ticket["kind"] == "repair":
        if is_admin_flag:
            kb.append([
                InlineKeyboardButton("⚡ Приоритет ↑", callback_data=f"prio:{ticket['id']}")
            ])
            kb.append([
                InlineKeyboardButton("👤 Назначить себе", callback_data=f"assign_self:{ticket['id']}"),
                InlineKeyboardButton("👥 Назначить механику", callback_data=f"assign_menu:{ticket['id']}"),
            ])
        kb.append([InlineKeyboardButton("⏱ В работу", callback_data=f"to_work:{ticket['id']}")])
        if (ticket.get("assignee_id") == me_id):
            kb.append([InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{ticket['id']}")])
            kb.append([InlineKeyboardButton("🛑 Отказ (с комментарием)", callback_data=f"decline:{ticket['id']}")])
            kb.append([InlineKeyboardButton("🛒 Требует закупку", callback_data=f"need_buy:{ticket['id']}")])
    elif ticket["kind"] == "purchase":
        if is_admin_flag:
            kb.append([
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{ticket['id']}"),
                InlineKeyboardButton("🛑 Отклонить (с причиной)", callback_data=f"reject:{ticket['id']}"),
            ])
    return InlineKeyboardMarkup(kb) if kb else None

# ------------------ ТИКЕТЫ ------------------

async def create_ticket(
    db,
    *,
    kind: str,
    chat_id: int,
    user_id: int,
    username: str | None,
    description: str,
    photo_file_id: str | None,
    location: str | None = None,
    priority: str | None = None,
    done_photo_file_id: str | None = None,
):
    now = now_local().isoformat()
    pr = priority or "normal"
    await db.execute(
        """
        INSERT INTO tickets(
            kind, status, priority,
            chat_id, user_id, username,
            description,
            photo_file_id,
            done_photo_file_id,
            assignee_id, assignee_name,
            location,
            reason,
            created_at, updated_at,
            started_at, done_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            kind,
            STATUS_NEW,
            pr,
            chat_id,
            user_id,
            username,
            description.strip(),
            photo_file_id,
            done_photo_file_id,
            None,
            None,
            location,
            None,
            now,
            now,
            None,
            None,
        ),
    )
    await db.commit()

async def find_tickets(
    db,
    *,
    kind: str | None = None,
    status: str | None = None,
    user_id: int | None = None,
    assignee_id: int | None = None,
    unassigned_only: bool = False,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
):
    sql = (
        "SELECT id, kind, status, priority, chat_id, user_id, username, description, "
        "photo_file_id, done_photo_file_id, assignee_id, assignee_name, location, reason, "
        "created_at, updated_at, started_at, done_at "
        "FROM tickets"
    )
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
            where.append("(description LIKE ? OR location LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = []
    async with db.execute(sql, params) as cur:
        async for row in cur:
            rows.append(
                {
                    "id": row[0],
                    "kind": row[1],
                    "status": row[2],
                    "priority": row[3],
                    "chat_id": row[4],
                    "user_id": row[5],
                    "username": row[6],
                    "description": row[7],
                    "photo_file_id": row[8],
                    "done_photo_file_id": row[9],
                    "assignee_id": row[10],
                    "assignee_name": row[11],
                    "location": row[12],
                    "reason": row[13],
                    "created_at": row[14],
                    "updated_at": row[15],
                    "started_at": row[16],
                    "done_at": row[17],
                }
            )
    return rows

async def get_ticket(db, ticket_id: int) -> dict | None:
    async with db.execute(
        """
        SELECT id, kind, status, priority,
               chat_id, user_id, username,
               description,
               photo_file_id,
               done_photo_file_id,
               assignee_id, assignee_name,
               location, reason,
               created_at, updated_at,
               started_at, done_at
        FROM tickets
        WHERE id=?
        """,
        (ticket_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "kind": row[1],
        "status": row[2],
        "priority": row[3],
        "chat_id": row[4],
        "user_id": row[5],
        "username": row[6],
        "description": row[7],
        "photo_file_id": row[8],
        "done_photo_file_id": row[9],
        "assignee_id": row[10],
        "assignee_name": row[11],
        "location": row[12],
        "reason": row[13],
        "created_at": row[14],
        "updated_at": row[15],
        "started_at": row[16],
        "done_at": row[17],
    }

async def update_ticket(db, ticket_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = now_local().isoformat()
    cols = ", ".join([f"{k}=?" for k in fields.keys()])
    params = list(fields.values()) + [ticket_id]
    await db.execute(f"UPDATE tickets SET {cols} WHERE id=?", params)
    await db.commit()

# ------------------ РЕНДЕР ------------------

def render_ticket_line(t: dict) -> str:
    if t["kind"] == "repair":
        icon = "🛠"
        stat = {
            "new": "🆕 Новая",
            "in_work": "⏱ В работе",
            "done": "✅ Выполнена",
            "rejected": "🛑 Отказ исполнителя",
            "canceled": "🗑 Отменена",
        }.get(t["status"], t["status"])
        prio_human = {
            "low": "🟢 плановое",
            "normal": "🟡 срочно",
            "high": "🔴 авария",
        }.get(t["priority"], t["priority"])
        assgn = f" • Исполнитель: {t['assignee_name'] or t['assignee_id'] or '—'}"
        loc = f"\nПомещение: {t.get('location') or '—'}"
        times = f"\nСоздана: {fmt_dt(t['created_at'])}"
        if t["started_at"]:
            times += f" • Взята: {fmt_dt(t['started_at'])}"
        if t["done_at"]:
            times += (
                f" • Готово: {fmt_dt(t['done_at'])}"
                f" • Длит.: {human_duration(t['started_at'], t['done_at'])}"
            )
        reason = ""
        if t["status"] in ("rejected", "canceled") and t.get("reason"):
            reason = f"\nПричина: {t['reason']}"
        return (
            f"{icon} #{t['id']} • {stat} • Приоритет: {prio_human}{assgn}\n"
            f"{t['description']}{loc}{times}{reason}"
        )
    else:
        icon = "🛒"
        stat = {
            "new": "🆕 Новая",
            "approved": "✅ Одобрена",
            "rejected": "🛑 Отклонена",
            "canceled": "🗑 Отменена",
        }.get(t["status"], t["status"])
        times = f"\nСоздана: {fmt_dt(t['created_at'])}"
        reason = (
            f"\nПричина: {t['reason']}"
            if t["status"] in ("rejected", "canceled") and t.get("reason")
            else ""
        )
        return f"{icon} #{t['id']} • {stat}\n{t['description']}{times}{reason}"

# ------------------ ОТПРАВКА КАРТОЧЕК ------------------

async def send_ticket_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, t: dict, kb: InlineKeyboardMarkup | None):
    """
    Отправляет карточку заявки:
    - если это ремонт и есть photo_file_id -> отправляем фото как карточку;
    - иначе просто текст.
    """
    try:
        if t.get("photo_file_id") and t.get("kind") == "repair":
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=t["photo_file_id"],
                caption=render_ticket_line(t),
                reply_markup=kb,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=render_ticket_line(t),
                reply_markup=kb,
            )
    except Exception as e:
        log.debug(f"send_ticket_card failed: {e}")

async def edit_message_text_or_caption(query, new_text: str):
    """
    Если исходное сообщение было фото — правим подпись.
    Если это был текст — правим текст.
    """
    try:
        if getattr(query.message, "photo", None):
            await query.edit_message_caption(caption=new_text)
        else:
            await query.edit_message_text(new_text)
    except Exception as e:
        log.debug(f"edit_message_text_or_caption failed: {e}")

# ------------------ ХЕНДЛЕРЫ КОМАНД ------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)
    kb = await main_menu(db, uid)
    await update.message.reply_text(
        "Привет! Это бот инженерно-технической службы.", reply_markup=kb
    )
    context.user_data[UD_MODE] = None
    context.user_data[UD_REPAIR_LOC] = None
    context.user_data[UD_REPAIR_PRIORITY] = None
    context.user_data[UD_DONE_CTX] = None
    context.user_data[UD_BUY_CONTEXT] = None

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Что он умеет:\n\n"
        "• 🛠 Заявка на ремонт — выбрать помещение, срочность, описать проблему (можно фото).\n"
        "• 🛒 Заявка на покупку — запросить закупку.\n"
        "• 🧾 Мои заявки — все мои заявки (ремонт и покупка).\n"
        "• 🛒 Мои покупки — только мои заявки на покупку.\n"
        "• 🛠 Заявки на ремонт — список для механика / админа.\n"
        "• 🛒 Покупки — новые покупки на одобрение (админ).\n"
        "• 📓 Журнал — выполненные / в работе (админ).\n\n"
        "Механик в карточке ремонта может:\n"
        "• взять в работу,\n"
        "• отметить выполнено (с фото результата),\n"
        "• отказать с комментарием,\n"
        "• запросить закупку запчасти 🛒.\n\n"
        "Команды:\n"
        "/repairs [status] [page] — ремонтные заявки (new|in_work|done|all)\n"
        "/me [status] [page] — мои заявки как исполнителя\n"
        "/mypurchases [page] — мои заявки на покупку\n"
        "/find <текст|#id> — поиск (админ)\n"
        "/export [week|month] — CSV (админ)\n"
        "/journal [days] — журнал (админ)\n"
        "/add_tech <user_id|@nick> — выдать роль механика (админ)\n"
        "/roles — роли\n"
        "/whoami — свой user_id\n"
        "/help — эта справка\n"
    )
    await update.message.reply_text(text)

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    uname = update.effective_user.username or "—"
    await db_seen_user(db, uid, update.effective_user.username)
    await update.message.reply_text(f"Твой user_id: {uid}\nusername: @{uname}")

# ------------------ КЛАВИАТУРЫ ВВОДА ------------------

def locations_keyboard():
    rows = []
    row = []
    for i, name in enumerate(LOCATIONS, start=1):
        row.append(KeyboardButton(name))
        if i % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton(LOC_OTHER)])
    rows.append([KeyboardButton(LOC_CANCEL)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

def priority_keyboard():
    rows = [
        [KeyboardButton("🟢 Плановое (можно подождать)")],
        [KeyboardButton("🟡 Срочно, простой")],
        [KeyboardButton("🔴 Авария, линия стоит")],
        [KeyboardButton(LOC_CANCEL)],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

# ------------------ ОСНОВНАЯ ЛОГИКА ТЕКСТА ------------------

async def on_text_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)

    text_in = (update.message.text or "").strip()
    mode = context.user_data.get(UD_MODE)

    # ШАГ 1. Создать ремонт -> выбор помещения
    if text_in == "🛠 Заявка на ремонт" and mode is None:
        context.user_data[UD_MODE] = "choose_location_repair"
        context.user_data[UD_REPAIR_LOC] = None
        context.user_data[UD_REPAIR_PRIORITY] = None
        await update.message.reply_text(
            "Выбери помещение для ремонта:",
            reply_markup=locations_keyboard(),
        )
        return

    # выбор помещения
    if mode == "choose_location_repair":
        if text_in == LOC_CANCEL:
            context.user_data[UD_MODE] = None
            context.user_data[UD_REPAIR_LOC] = None
            context.user_data[UD_REPAIR_PRIORITY] = None
            await update.message.reply_text(
                "Отмена.",
                reply_markup=await main_menu(db, uid),
            )
            return
        if text_in == LOC_OTHER:
            context.user_data[UD_MODE] = "input_location_repair"
            await update.message.reply_text(
                "Введи название помещения текстом:",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if text_in in LOCATIONS:
            context.user_data[UD_REPAIR_LOC] = text_in
            context.user_data[UD_MODE] = "choose_priority_repair"
            await update.message.reply_text(
                f"Помещение: {text_in}\n\nВыбери срочность/аварийность:",
                reply_markup=priority_keyboard(),
            )
            return
        await update.message.reply_text(
            "Пожалуйста, выбери помещение с клавиатуры или нажми «Другое помещение…».",
        )
        return

    # ручной ввод помещения
    if mode == "input_location_repair":
        custom_loc = text_in
        if not custom_loc or custom_loc in (LOC_CANCEL, LOC_OTHER):
            await update.message.reply_text(
                "Введи корректное название помещения или нажми «↩ Отмена».",
            )
            return
        context.user_data[UD_REPAIR_LOC] = custom_loc
        context.user_data[UD_MODE] = "choose_priority_repair"
        await update.message.reply_text(
            f"Помещение: {custom_loc}\n\nВыбери срочность/аварийность:",
            reply_markup=priority_keyboard(),
        )
        return

    # выбор приоритета
    if mode == "choose_priority_repair":
        if text_in == LOC_CANCEL:
            context.user_data[UD_MODE] = None
            context.user_data[UD_REPAIR_LOC] = None
            context.user_data[UD_REPAIR_PRIORITY] = None
            await update.message.reply_text(
                "Отмена.",
                reply_markup=await main_menu(db, uid),
            )
            return
        pr_map = {
            "🟢 Плановое (можно подождать)": "low",
            "🟡 Срочно, простой": "normal",
            "🔴 Авария, линия стоит": "high",
        }
        if text_in in pr_map:
            context.user_data[UD_REPAIR_PRIORITY] = pr_map[text_in]
            context.user_data[UD_MODE] = "create_repair"
            await update.message.reply_text(
                "Опиши проблему. Можно прикрепить фото с подписью.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        await update.message.reply_text(
            "Выбери один из вариантов срочности или нажми «↩ Отмена».",
        )
        return

    # ШАГ 2. Создать покупку вручную
    if text_in == "🛒 Заявка на покупку" and mode is None:
        context.user_data[UD_MODE] = "create_purchase"
        await update.message.reply_text(
            "Опиши, что нужно купить (наименование, количество, почему)."
        )
        return

    # МОИ ЗАЯВКИ
    if text_in == "🧾 Мои заявки" and mode is None:
        rows = await find_tickets(db, user_id=uid, limit=20, offset=0)
        if not rows:
            await update.message.reply_text("У тебя пока нет заявок.")
            return
        for t in rows[:20]:
            await send_ticket_card(context, update.effective_chat.id, t, None)
        return

    # МОИ ПОКУПКИ
    if text_in == "🛒 Мои покупки" and mode is None:
        rows = await find_tickets(
            db, kind="purchase", user_id=uid, limit=20, offset=0
        )
        if not rows:
            await update.message.reply_text("Твоих заявок на покупку пока нет.")
            return
        for t in rows[:20]:
            await send_ticket_card(context, update.effective_chat.id, t, None)
        return

    # СПИСОК ЗАЯВОК НА РЕМОНТ
    if text_in == "🛠 Заявки на ремонт" and mode is None:
        admin = await is_admin(db, uid)
        if admin:
            rows = await find_tickets(
                db, kind="repair", status="new", limit=20, offset=0
            )
            if not rows:
                await update.message.reply_text("Нет новых заявок на ремонт.")
                return
            for t in rows:
                kb = ticket_inline_kb(t, is_admin_flag=True, me_id=uid)
                await send_ticket_card(
                    context, update.effective_chat.id, t, kb
                )
        else:
            new_unassigned = await find_tickets(
                db,
                kind="repair",
                status="new",
                unassigned_only=True,
                limit=20,
                offset=0,
            )
            new_assigned_to_me = await find_tickets(
                db,
                kind="repair",
                status="new",
                assignee_id=uid,
                limit=20,
                offset=0,
            )
            in_rows = await find_tickets(
                db,
                kind="repair",
                status="in_work",
                assignee_id=uid,
                limit=20,
                offset=0,
            )
            rows = new_assigned_to_me + in_rows + new_unassigned
            if not rows:
                await update.message.reply_text("Нет доступных заявок.")
                return
            for t in rows:
                kb = ticket_inline_kb(t, is_admin_flag=False, me_id=uid)
                await send_ticket_card(
                    context, update.effective_chat.id, t, kb
                )
        return

    # АДМИН: заявки на покупку
    if text_in == "🛒 Покупки" and mode is None:
        if not await is_admin(db, uid):
            await update.message.reply_text("Недостаточно прав.")
            return
        rows = await find_tickets(
            db, kind="purchase", status="new", limit=20, offset=0
        )
        if not rows:
            await update.message.reply_text("Нет новых заявок на покупку.")
            return
        for t in rows:
            kb = ticket_inline_kb(t, is_admin_flag=True, me_id=uid)
            await send_ticket_card(
                context, update.effective_chat.id, t, kb
            )
        return

    # АДМИН: журнал
    if text_in == "📓 Журнал" and mode is None:
        if not await is_admin(db, uid):
            await update.message.reply_text("Недостаточно прав.")
            return
        await cmd_journal(update, context)
        return

    # ЗАВЕРШЕНИЕ ЗАЯВКИ (механик уже нажал ✅, ждём фото или "готово")
    if mode == "await_done_photo":
        tid = context.user_data.get(UD_DONE_CTX)
        if not tid:
            await update.message.reply_text(
                "Не удалось определить заявку для завершения."
            )
            context.user_data[UD_MODE] = None
            context.user_data[UD_DONE_CTX] = None
            return
        t = await get_ticket(db, tid)
        if not t:
            await update.message.reply_text("Заявка не найдена.")
        else:
            # только исполнитель может финализировать
            if t.get("assignee_id") != uid:
                await update.message.reply_text(
                    "Закрыть может только исполнитель."
                )
            else:
                # финальная фиксация без фото
                await update_ticket(
                    db,
                    tid,
                    status="done",
                    done_at=now_local().isoformat(),
                )
                # уведомляем автора
                try:
                    await context.bot.send_message(
                        chat_id=t["user_id"],
                        text=(
                            f"Твоя заявка #{tid} отмечена как выполненная."
                        ),
                    )
                except Exception as e:
                    log.debug(f"Notify author done (text) failed: {e}")
                await update.message.reply_text(
                    f"Заявка #{tid} закрыта ✅."
                )
        context.user_data[UD_MODE] = None
        context.user_data[UD_DONE_CTX] = None
        return

    # ОЖИДАЕМ ОПИСАНИЕ ЗАКУПКИ после "🛒 Требует закупку"
    if mode == "await_buy_desc":
        buy_ctx = context.user_data.get(UD_BUY_CONTEXT) or {}
        tid = buy_ctx.get("ticket_id")
        if not tid:
            await update.message.reply_text(
                "Не удалось связать с ремонтной заявкой."
            )
            context.user_data[UD_MODE] = None
            context.user_data[UD_BUY_CONTEXT] = None
            return
        base_ticket = await get_ticket(db, tid)
        loc = base_ticket.get("location") if base_ticket else "—"
        uname = update.effective_user.username or ""
        chat_id = update.message.chat_id
        desc = f"Запчасть для заявки #{tid} ({loc}): {text_in}"

        await create_ticket(
            db,
            kind="purchase",
            chat_id=chat_id,
            user_id=uid,
            username=uname,
            description=desc,
            photo_file_id=None,
            location=None,
        )
        await update.message.reply_text(
            "Заявка на покупку создана и отправлена админу."
        )
        # Уведомляем админов
        await notify_admins(
            context,
            f"🆕 Покупка по ремонту #{tid} от @{uname or uid}:\n{text_in}",
        )
        context.user_data[UD_MODE] = None
        context.user_data[UD_BUY_CONTEXT] = None
        return

    # Причина отказа / отмены / reject
    if mode == "await_reason":
        await handle_reason_input(update, context)
        return

    # Создание описания после помещения+приоритета (ремонт) или обычной покупки
    if mode in ("create_repair", "create_purchase"):
        await handle_create_from_text(update, context)
        return

    # если ничего не подошло
    await update.message.reply_text("Используй кнопки меню или /help.")

# ------------------ СОЗДАНИЕ ЗАЯВОК (ТЕКСТ/ФОТО) ------------------

async def handle_create_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    uname = update.effective_user.username or ""
    chat_id = update.message.chat_id
    mode = context.user_data.get(UD_MODE)

    description = (update.message.text or "").strip()
    if not description:
        await update.message.reply_text("Опиши заявку текстом.")
        return

    # ремонт
    if mode == "create_repair":
        location = context.user_data.get(UD_REPAIR_LOC)
        priority = context.user_data.get(UD_REPAIR_PRIORITY) or "normal"
        if not location:
            context.user_data[UD_MODE] = "choose_location_repair"
            await update.message.reply_text(
                "Сначала выбери помещение:",
                reply_markup=locations_keyboard(),
            )
            return

        await create_ticket(
            db,
            kind="repair",
            chat_id=chat_id,
            user_id=uid,
            username=uname,
            description=description,
            photo_file_id=None,
            location=location,
            priority=priority,
        )

        await update.message.reply_text(
            f"Заявка на ремонт создана.\n"
            f"Помещение: {location}\n"
            f"Срочность сохранена.\n"
            f"Админы уведомлены."
        )
        # админам отправим карточку
        await notify_admins_ticket(context, uid)

        context.user_data[UD_MODE] = None
        context.user_data[UD_REPAIR_LOC] = None
        context.user_data[UD_REPAIR_PRIORITY] = None
        return

    # покупка
    if mode == "create_purchase":
        await create_ticket(
            db,
            kind="purchase",
            chat_id=chat_id,
            user_id=uid,
            username=uname,
            description=description,
            photo_file_id=None,
            location=None,
        )
        await update.message.reply_text(
            "Заявка на покупку отправлена. Ожидает решения админа."
        )
        await notify_admins(
            context,
            f"🆕 Покупка от @{uname or uid}:\n{description}",
        )
        context.user_data[UD_MODE] = None
        return

async def on_photo_with_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает:
    1) Завершение ремонта (await_done_photo): механик прислал фото результата.
    2) Создание ремонта с фото (create_repair): пользователь прислал фото поломки с подписью.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)

    mode = context.user_data.get(UD_MODE)

    # 1) закрытие заявки с фото результата ремонта
    if mode == "await_done_photo":
        tid = context.user_data.get(UD_DONE_CTX)
        if not tid:
            await update.message.reply_text(
                "Не удалось определить заявку для завершения."
            )
            context.user_data[UD_MODE] = None
            context.user_data[UD_DONE_CTX] = None
            return

        t = await get_ticket(db, tid)
        if not t:
            await update.message.reply_text("Заявка не найдена.")
        else:
            if t.get("assignee_id") != uid:
                await update.message.reply_text(
                    "Закрыть может только исполнитель."
                )
            else:
                final_caption = (update.message.caption or "").strip()
                photo = update.message.photo[-1]
                file_id = photo.file_id

                await update_ticket(
                    db,
                    tid,
                    status="done",
                    done_at=now_local().isoformat(),
                    done_photo_file_id=file_id,
                )

                # уведомить автора
                try:
                    await context.bot.send_message(
                        chat_id=t["user_id"],
                        text=(
                            f"Твоя заявка #{tid} отмечена как выполненная."
                        ),
                    )
                except Exception as e:
                    log.debug(
                        f"Notify author done-photo failed: {e}"
                    )

                await update.message.reply_text(
                    f"Заявка #{tid} закрыта ✅ (фото результата сохранено)."
                )

        context.user_data[UD_MODE] = None
        context.user_data[UD_DONE_CTX] = None
        return

    # 2) создание новой ремонтной заявки с фото поломки
    if mode != "create_repair":
        await update.message.reply_text(
            "Чтобы создать заявку с фото: нажми «🛠 Заявка на ремонт», "
            "выбери помещение и срочность, затем пришли фото с подписью."
        )
        return

    uname = update.effective_user.username or ""
    chat_id = update.message.chat_id

    caption = (update.message.caption or "").strip()
    if not caption:
        await update.message.reply_text(
            "Добавь подпись к фото — это будет описанием заявки."
        )
        return

    location = context.user_data.get(UD_REPAIR_LOC)
    priority = context.user_data.get(UD_REPAIR_PRIORITY) or "normal"
    if not location:
        context.user_data[UD_MODE] = "choose_location_repair"
        await update.message.reply_text(
            "Сначала выбери помещение для ремонта:",
            reply_markup=locations_keyboard(),
        )
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id

    await create_ticket(
        db,
        kind="repair",
        chat_id=chat_id,
        user_id=uid,
        username=uname,
        description=caption,
        photo_file_id=file_id,
        location=location,
        priority=priority,
    )

    await update.message.reply_text(
        f"Заявка на ремонт с фото создана.\n"
        f"Помещение: {location}\n"
        f"Срочность сохранена.\n"
        f"Админы уведомлены."
    )

    await notify_admins_ticket(context, uid)

    context.user_data[UD_MODE] = None
    context.user_data[UD_REPAIR_LOC] = None
    context.user_data[UD_REPAIR_PRIORITY] = None

# ------------------ АДМИН ФУНКЦИИ: ПОИСК / ЭКСПОРТ / ЖУРНАЛ ------------------

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
        kb = ticket_inline_kb(t, is_admin_flag=True, me_id=uid)
        await send_ticket_card(context, update.effective_chat.id, t, kb)

async def export_rows(db, start_iso: str):
    async with db.execute(
        """
        SELECT
            id, kind, status, priority,
            user_id, username,
            assignee_id, assignee_name,
            location,
            created_at, started_at, done_at,
            reason, description
        FROM tickets
        WHERE created_at >= ?
        ORDER BY id DESC
        """,
        (start_iso,),
    ) as cur:
        rows = []
        async for row in cur:
            rows.append(
                {
                    "id": row[0],
                    "kind": row[1],
                    "status": row[2],
                    "priority": row[3],
                    "user_id": row[4],
                    "username": row[5],
                    "assignee_id": row[6],
                    "assignee_name": row[7],
                    "location": row[8],
                    "created_at": row[9],
                    "started_at": row[10],
                    "done_at": row[11],
                    "reason": row[12],
                    "description": row[13],
                }
            )
    return rows

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
    now = now_local()
    start = now - (timedelta(days=7) if period == "week" else timedelta(days=30))
    rows = await export_rows(db, start_iso=start.isoformat())
    if not rows:
        await update.message.reply_text("Нет данных для экспорта.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id",
        "kind",
        "status",
        "priority",
        "user_id",
        "username",
        "assignee_id",
        "assignee_name",
        "location",
        "created_at",
        "started_at",
        "done_at",
        "duration",
        "reason",
        "description",
    ])
    for r in rows:
        dur = human_duration(r["started_at"], r["done_at"])
        writer.writerow([
            r["id"],
            r["kind"],
            r["status"],
            r["priority"],
            r["user_id"],
            r["username"] or "",
            r["assignee_id"] or "",
            r["assignee_name"] or "",
            r.get("location") or "",
            r["created_at"],
            r["started_at"] or "",
            r["done_at"] or "",
            dur,
            r["reason"] or "",
            r["description"].replace("\n", " ")[:500],
        ])
    data = buf.getvalue().encode("utf-8")

    await update.message.reply_document(
        document=InputFile(
            io.BytesIO(data), filename=f"tickets_{period}.csv"
        ),
        caption=f"Экспорт за {period}.",
    )

async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return
    days = ensure_int(context.args[0]) if context.args else 30
    days = days or 30
    since = now_local() - timedelta(days=days)
    # показываем ремонтные статусы "в работе", "выполнена", "отказ"
    async with db.execute(
        """
        SELECT id, description, location, assignee_name, assignee_id,
               started_at, done_at,
               created_at, updated_at,
               status, reason
        FROM tickets
        WHERE kind='repair'
          AND status IN ('in_work','done','rejected')
          AND updated_at >= ?
        ORDER BY updated_at DESC
        """,
        (since.isoformat(),),
    ) as cur:
        items = await cur.fetchall()

    if not items:
        await update.message.reply_text("Журнал пуст.")
        return

    lines = []
    for (
        id_,
        desc,
        loc,
        aname,
        aid,
        started,
        done,
        created,
        updated,
        status,
        reason,
    ) in items:
        who = aname or aid or "—"
        status_text = {
            "in_work": "⏱ В работе",
            "done": "✅ Выполнена",
            "rejected": "🛑 Отказ исполнителя",
        }.get(status, status)

        created_s = f"Создана: {fmt_dt(created)}"
        loc_s = f"Помещение: {loc or '—'}"

        if status == "in_work":
            dur = (
                human_duration(started, now_local().isoformat())
                if started
                else "—"
            )
            line = (
                f"#{id_} • {status_text} • Исп.: {who}\n"
                f"{loc_s}\n"
                f"{created_s} • Взята: {fmt_dt(started)} • "
                f"Длит.: {dur}\n"
                f"{desc}"
            )
        elif status == "done":
            dur = human_duration(started, done)
            line = (
                f"#{id_} • {status_text} • Исп.: {who}\n"
                f"{loc_s}\n"
                f"{created_s} • Взята: {fmt_dt(started)} • "
                f"Готово: {fmt_dt(done)} • "
                f"Длит.: {dur}\n"
                f"{desc}"
            )
        else:  # rejected
            line = (
                f"#{id_} • {status_text} • Исп.: {who}\n"
                f"{loc_s}\n"
                f"{created_s} • Взята: {fmt_dt(started)} • "
                f"Обновлена: {fmt_dt(updated)}\n"
                f"{desc}"
            )
            if reason:
                line += f"\nПричина: {reason}"

        lines.append(line)

    text_out = "\n\n".join(lines)
    for part in chunk_text(text_out):
        await update.message.reply_text(part)

# ------------------ СПИСКИ / ФИЛЬТРЫ ------------------

async def cmd_repairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    status_arg = (context.args[0].lower() if context.args else "new").strip()
    page = ensure_int(context.args[1]) if len(context.args) >= 2 else 1
    page = max(1, page or 1)
    offset = (page - 1) * 20

    status_map = {
        "new": "new",
        "in_work": "in_work",
        "done": "done",
        "all": None,
    }
    stat = status_map.get(status_arg, "new")

    admin = await is_admin(db, uid)
    if admin:
        if stat:
            rows = await find_tickets(
                db,
                kind="repair",
                status=stat,
                limit=20,
                offset=offset,
            )
        else:
            rows = await find_tickets(
                db, kind="repair", limit=20, offset=offset
            )
    else:
        if stat == "new":
            unassigned = await find_tickets(
                db,
                kind="repair",
                status="new",
                unassigned_only=True,
                limit=20,
                offset=offset,
            )
            assigned_to_me = await find_tickets(
                db,
                kind="repair",
                status="new",
                assignee_id=uid,
                limit=20,
                offset=0,
            )
            rows = assigned_to_me + unassigned
        elif stat == "in_work":
            rows = await find_tickets(
                db,
                kind="repair",
                status="in_work",
                assignee_id=uid,
                limit=20,
                offset=offset,
            )
        elif stat == "done":
            rows = await find_tickets(
                db,
                kind="repair",
                status="done",
                assignee_id=uid,
                limit=20,
                offset=offset,
            )
        elif stat is None:  # all
            assigned_new = await find_tickets(
                db,
                kind="repair",
                status="new",
                assignee_id=uid,
                limit=20,
                offset=0,
            )
            in_work = await find_tickets(
                db,
                kind="repair",
                status="in_work",
                assignee_id=uid,
                limit=20,
                offset=0,
            )
            unassigned_new = await find_tickets(
                db,
                kind="repair",
                status="new",
                unassigned_only=True,
                limit=20,
                offset=0,
            )
            rows = assigned_new + in_work + unassigned_new
        else:
            rows = []

    if not rows:
        await update.message.reply_text("Ничего не найдено.")
        return

    for t in rows:
        kb = ticket_inline_kb(t, is_admin_flag=admin, me_id=uid)
        await send_ticket_card(context, update.effective_chat.id, t, kb)

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    status_arg = (context.args[0].lower() if context.args else "in_work").strip()
    page = ensure_int(context.args[1]) if len(context.args) >= 2 else 1
    page = max(1, page or 1)
    offset = (page - 1) * 20

    status_map = {
        "new": "new",
        "in_work": "in_work",
        "done": "done",
        "all": None,
    }
    stat = status_map.get(status_arg, "in_work")

    if stat:
        rows = await find_tickets(
            db,
            kind="repair",
            status=stat,
            assignee_id=uid,
            limit=20,
            offset=offset,
        )
    else:
        rows = await find_tickets(
            db,
            kind="repair",
            assignee_id=uid,
            limit=20,
            offset=offset,
        )

    if not rows:
        await update.message.reply_text("Пока пусто.")
        return

    for t in rows:
        kb = ticket_inline_kb(
            t, is_admin_flag=await is_admin(db, uid), me_id=uid
        )
        await send_ticket_card(context, update.effective_chat.id, t, kb)

async def cmd_mypurchases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    page = ensure_int(context.args[0]) if context.args else 1
    page = max(1, page or 1)
    offset = (page - 1) * 20

    rows = await find_tickets(
        db,
        kind="purchase",
        user_id=uid,
        limit=20,
        offset=offset,
    )
    if not rows:
        await update.message.reply_text("Твоих заявок на покупку пока нет.")
        return
    for t in rows:
        await send_ticket_card(context, update.effective_chat.id, t, None)

# ------------------ РОЛИ ------------------

async def cmd_add_tech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text(
            "Использование: /add_tech <user_id|@username>\n"
            "(Пользователь должен сначала написать боту /start.)"
        )
        return
    arg = context.args[0].strip()
    target = ensure_int(arg)
    if not target and arg.startswith("@"):
        target = await db_lookup_uid_by_username(db, arg)
    if not target:
        await update.message.reply_text(
            "Укажи числовой user_id или @username (после того, как пользователь написал боту /start)."
        )
        return
    await db_add_user_role(db, target, "tech")
    await update.message.reply_text(
        f"Пользователь {target} добавлен как mechanic (tech)."
    )

async def cmd_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    admins, techs = await db_list_roles(db)
    text = (
        "Роли:\n\nАдмины:\n"
        + (", ".join(map(str, admins)) or "—")
        + "\n\nМеханики:\n"
        + (", ".join(map(str, techs)) or "—")
    )
    await update.message.reply_text(text)

# ------------------ CALLBACK КНОПКИ ------------------

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    uname = update.effective_user.username or ""

    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # меню назначения механику
    if data.startswith("assign_menu:"):
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return
        admins, techs = await db_list_roles(db)
        kb = []
        row = []
        for i, tech_uid in enumerate(techs, start=1):
            row.append(
                InlineKeyboardButton(
                    f"{tech_uid}", callback_data=f"assign_to:{tech_uid}"
                )
            )
            if i % 3 == 0:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.append(
            [InlineKeyboardButton("↩️ Назад", callback_data="assign_back")]
        )
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data == "assign_back":
        await query.answer(
            "Выбери техника или команду ниже.", show_alert=False
        )
        return

    if data.startswith("assign_to:"):
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return
        tid = extract_ticket_id_from_message(query.message.text or "")
        assignee = ensure_int(data.split(":", 1)[1])
        if not tid or not assignee:
            await query.answer(
                "Не удалось определить заявку/пользователя."
            )
            return
        await update_ticket(
            db,
            tid,
            assignee_id=assignee,
            assignee_name=str(assignee),
        )
        await edit_message_text_or_caption(
            query,
            (query.message.text or "") + f"\n\nНазначено: {assignee}",
        )
        # отправить механику карточку
        try:
            t = await get_ticket(db, tid)
            if t:
                kb_for_tech = ticket_inline_kb(
                    t, is_admin_flag=False, me_id=assignee
                )
                await send_ticket_card(
                    context, assignee, t, kb_for_tech
                )
        except Exception as e:
            log.debug(
                f"Notify assignee {assignee} with card failed: {e}"
            )
        return

    if data.startswith("assign_self:"):
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return
        tid = ensure_int(data.split(":", 1)[1])
        if not tid:
            return
        await update_ticket(
            db,
            tid,
            assignee_id=uid,
            assignee_name=f"@{uname or uid}",
        )
        await edit_message_text_or_caption(
            query,
            (query.message.text or "")
            + f"\n\nНазначено: @{uname or uid}",
        )
        try:
            t = await get_ticket(db, tid)
            if t:
                kb_for_me = ticket_inline_kb(
                    t, is_admin_flag=False, me_id=uid
                )
                await send_ticket_card(context, uid, t, kb_for_me)
        except Exception as e:
            log.debug(f"Notify self with card failed: {e}")
        return

    if data.startswith("prio:"):
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return
        tid = ensure_int(data.split(":", 1)[1])
        t = await get_ticket(db, tid)
        if not t:
            await query.answer("Заявка не найдена.")
            return
        cur = t["priority"]
        try:
            idx = PRIORITIES.index(cur)
            new = PRIORITIES[min(idx + 1, len(PRIORITIES) - 1)]
        except Exception:
            new = "normal"
        await update_ticket(db, tid, priority=new)
        await edit_message_text_or_caption(
            query,
            (query.message.text or "") + f"\n\nПриоритет: {new}",
        )
        return

    if data.startswith("to_work:"):
        tid = ensure_int(data.split(":", 1)[1])
        if not tid:
            return
        t = await get_ticket(db, tid)
        if not t or t["kind"] != "repair":
            await query.answer("Некорректная заявка.")
            return
        if t["status"] != "new":
            await query.answer("Заявка уже не новая.")
            return

        # защита: если автор админ, механик-сам не должен брать без назначения
        author_is_admin = await is_admin(db, t["user_id"])
        if (
            author_is_admin
            and not await is_admin(db, uid)
            and not t["assignee_id"]
        ):
            await query.answer(
                "Эту заявку должен распределить админ."
            )
            return

        if (
            t["assignee_id"]
            and t["assignee_id"] != uid
            and not await is_admin(db, uid)
        ):
            await query.answer("Заявка назначена другому.")
            return

        now_iso = now_local().isoformat()
        if not t["assignee_id"]:
            await update_ticket(
                db,
                tid,
                assignee_id=uid,
                assignee_name=f"@{uname or uid}",
            )

        await update_ticket(
            db,
            tid,
            status="in_work",
            started_at=t["started_at"] or now_iso,
        )

        await edit_message_text_or_caption(
            query, (query.message.text or "") + "\n\nСтатус: ⏱ В работе"
        )

        # Уведомляем автора, что взяли в работу
        try:
            await context.bot.send_message(
                chat_id=t["user_id"],
                text=(
                    f"Твоя заявка #{tid} взята в работу механиком "
                    f"@{uname or uid}."
                ),
            )
        except Exception as e:
            log.debug(
                f"Notify author start-work failed: {e}"
            )

        return

    if data.startswith("done:"):
        tid = ensure_int(data.split(":", 1)[1])
        t = await get_ticket(db, tid)
        if not t or t["kind"] != "repair":
            await query.answer("Некорректная заявка.")
            return
        # закрывать может только исполнитель
        if t.get("assignee_id") != uid:
            await query.answer(
                "Только исполнитель может закрыть заявку."
            )
            return

        # включаем режим ожидания фото/текста
        context.user_data[UD_MODE] = "await_done_photo"
        context.user_data[UD_DONE_CTX] = tid

        await edit_message_text_or_caption(
            query,
            (query.message.text or "")
            + "\n\nПришли фото результата или напиши 'готово'.",
        )
        return

    if data.startswith("decline:"):
        tid = ensure_int(data.split(":", 1)[1])
        if not tid:
            return
        t = await get_ticket(db, tid)
        if not t or t["kind"] != "repair":
            await query.answer("Некорректная заявка.")
            return
        if t.get("assignee_id") != uid:
            await query.answer(
                "Только исполнитель может отказать по заявке."
            )
            return
        context.user_data[UD_MODE] = "await_reason"
        context.user_data[UD_REASON_CONTEXT] = {
            "action": "decline_repair",
            "ticket_id": tid,
        }
        await edit_message_text_or_caption(
            query,
            (query.message.text or "")
            + "\n\nНапиши причину отказа сообщением:",
        )
        return

    if data.startswith("need_buy:"):
        tid = ensure_int(data.split(":", 1)[1])
        t = await get_ticket(db, tid)
        if not t or t["kind"] != "repair":
            await query.answer("Некорректная заявка.")
            return
        # только исполнитель или админ
        if t.get("assignee_id") != uid and not await is_admin(db, uid):
            await query.answer(
                "Только исполнитель или админ может запросить закупку."
            )
            return

        context.user_data[UD_MODE] = "await_buy_desc"
        context.user_data[UD_BUY_CONTEXT] = {"ticket_id": tid}
        await edit_message_text_or_caption(
            query,
            (query.message.text or "")
            + "\n\nЧто нужно закупить? Укажи наименование, количество и причину.",
        )
        return

    if data.startswith("cancel:"):
        # оставлено для совместимости с покупками (админ может отменить)
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return
        tid = ensure_int(data.split(":", 1)[1])
        context.user_data[UD_MODE] = "await_reason"
        context.user_data[UD_REASON_CONTEXT] = {
            "action": "cancel",
            "ticket_id": tid,
        }
        await edit_message_text_or_caption(
            query,
            (query.message.text or "")
            + "\n\nНапиши причину отмены сообщением:",
        )
        return

    if data.startswith("approve:"):
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return
        tid = ensure_int(data.split(":", 1)[1])
        await update_ticket(db, tid, status="approved")
        await edit_message_text_or_caption(
            query,
            (query.message.text or "") + "\n\nСтатус: ✅ Одобрена",
        )
        t = await get_ticket(db, tid)
        if t:
            try:
                await context.bot.send_message(
                    chat_id=t["user_id"],
                    text=(
                        f"Твоя заявка на покупку #{tid} одобрена."
                    ),
                )
            except Exception as e:
                log.debug(f"Notify author approve failed: {e}")
        return

    if data.startswith("reject:"):
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return
        tid = ensure_int(data.split(":", 1)[1])
        context.user_data[UD_MODE] = "await_reason"
        context.user_data[UD_REASON_CONTEXT] = {
            "action": "reject",
            "ticket_id": tid,
        }
        await edit_message_text_or_caption(
            query,
            (query.message.text or "")
            + "\n\nНапиши причину отказа сообщением:",
        )
        return

def extract_ticket_id_from_message(text: str) -> int | None:
    # используется при assign_to
    # парсим первый #<число>
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

# ------------------ ДОП. ВВОД ТЕКСТОВОЙ ПРИЧИНЫ ------------------

async def handle_reason_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    reason_text = (update.message.text or "").strip()
    if not reason_text:
        await update.message.reply_text(
            "Причина не может быть пустой. Напиши текстом."
        )
        return

    ctx = context.user_data.get(UD_REASON_CONTEXT) or {}
    tid = ctx.get("ticket_id")
    action = ctx.get("action")

    if not tid or action not in ("cancel", "reject", "decline_repair"):
        await update.message.reply_text(
            "Контекст потерян. Попробуй снова."
        )
        context.user_data[UD_MODE] = None
        context.user_data[UD_REASON_CONTEXT] = None
        return

    t = await get_ticket(db, tid)
    if not t:
        await update.message.reply_text("Заявка не найдена.")
        context.user_data[UD_MODE] = None
        context.user_data[UD_REASON_CONTEXT] = None
        return

    # отмена админом
    if action == "cancel":
        await update_ticket(
            db, tid, status="canceled", reason=reason_text
        )
        await update.message.reply_text(
            f"Заявка #{tid} отменена.\nПричина: {reason_text}"
        )

    # механик отказался от ремонта
    elif action == "decline_repair":
        await update_ticket(
            db,
            tid,
            status="rejected",
            reason=reason_text,
        )
        await update.message.reply_text(
            f"Заявка #{tid} — отказ исполнителя.\nКомментарий: {reason_text}"
        )
        # уведомить автора отказа
        try:
            await context.bot.send_message(
                chat_id=t["user_id"],
                text=(
                    f"По заявке #{tid} механик оставил отказ.\n"
                    f"Комментарий: {reason_text}"
                ),
            )
        except Exception as e:
            log.debug(
                f"Notify author decline failed: {e}"
            )

    # отклонение покупки админом
    else:  # reject
        await update_ticket(
            db,
            tid,
            status="rejected",
            reason=reason_text,
        )
        await update.message.reply_text(
            f"Заявка #{tid} отклонена.\nПричина: {reason_text}"
        )
        try:
            await context.bot.send_message(
                chat_id=t["user_id"],
                text=(
                    f"Твоя заявка #{tid} отклонена.\n"
                    f"Причина: {reason_text}"
                ),
            )
        except Exception as e:
            log.debug(
                f"Notify author reject failed: {e}"
            )

    context.user_data[UD_MODE] = None
    context.user_data[UD_REASON_CONTEXT] = None

# ------------------ УВЕДОМЛЕНИЯ АДМИНАМ ------------------

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    db = context.application.bot_data["db"]
    admins, _ = await db_list_roles(db)
    for aid in admins:
        try:
            await context.bot.send_message(chat_id=aid, text=text)
        except Exception as e:
            log.debug(f"Notify admin {aid} failed: {e}")

async def notify_admins_ticket(context: ContextTypes.DEFAULT_TYPE, author_uid: int):
    """
    Берём последнюю заявку автора и кидаем карточку всем админам,
    с фото если есть.
    """
    db = context.application.bot_data["db"]
    rows = await find_tickets(db, user_id=author_uid, limit=1, offset=0)
    if not rows:
        return
    t = rows[0]
    admins, _ = await db_list_roles(db)
    kb = ticket_inline_kb(t, is_admin_flag=True, me_id=0)
    for aid in admins:
        await send_ticket_card(context, aid, t, kb)

# ------------------ РЕГИСТРАЦИЯ / MAIN ------------------

def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))

    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("journal", cmd_journal))

    app.add_handler(CommandHandler("repairs", cmd_repairs))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("mypurchases", cmd_mypurchases))

    app.add_handler(CommandHandler("add_tech", cmd_add_tech))
    app.add_handler(CommandHandler("roles", cmd_roles))

    app.add_handler(CallbackQueryHandler(cb_handler))

    # Фото с подписью: или создаём заявку на ремонт, или закрываем ремонт с фото "после"
    app.add_handler(MessageHandler(filters.PHOTO & filters.Caption(True), on_photo_with_caption))
    # Любой обычный текст (кнопки, описание заявок, причины и т.д.)
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
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
