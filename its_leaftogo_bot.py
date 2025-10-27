# its_helpdesk_bot.py
# Телеграм-бот инженерно-технической службы:
# - заявки на ремонт оборудования
# - заявки на закупку
# - журнал, приоритеты, назначение механиков
#
# Включены доработки:
#   • выбор помещения → оборудования (есть "Другое ...")
#   • приоритет (плановое / срочно / авария)
#   • хранение location / equipment / priority
#   • возврат главного меню после завершения действий
#   • автозаполнение started_at при закрытии, чтобы в журнале была длительность
#   • журнал и карточки показывают помещение + оборудование
#
# Требования:
#   python-telegram-bot==20.7
#   aiosqlite


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


# ======================
# БАЗОВЫЕ НАСТРОЙКИ
# ======================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Укажи BOT_TOKEN в переменных окружения.")

# Жёстко заданные админы (сюда можно вписать свой Telegram ID)
HARD_ADMIN_IDS = {826495316}

# Дополнительные админы через переменные окружения, например:
# ADMIN_IDS="12345 67890"
ENV_ADMIN_IDS = {
    int(x)
    for x in os.getenv("ADMIN_IDS", "").replace(",", " ").split()
    if x.isdigit()
}

# Техники (механики). Можно будет добавлять в рантайме через /add_tech
ENV_TECH_IDS: set[int] = set()

# Логи
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

DB_PATH = "its_helpdesk.sqlite3"

# Часовой пояс МСК
TZ = timezone(timedelta(hours=3), name="MSK")
DATE_FMT = "%Y-%m-%d %H:%M"

# Типы заявок
KIND_REPAIR = "repair"
KIND_PURCHASE = "purchase"

# Статусы
STATUS_NEW = "new"
STATUS_IN_WORK = "in_work"
STATUS_DONE = "done"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELED = "canceled"

# Приоритеты
PRIORITIES = ["low", "normal", "high"]  # low=плановое, normal=срочно, high=авария

# Ключи в context.user_data (состояние диалога)
UD_MODE = "mode"
UD_REASON_CONTEXT = "reason_ctx"        # {action, ticket_id}
UD_REPAIR_LOC = "repair_location"       # помещение
UD_REPAIR_EQUIP = "repair_equipment"    # оборудование
UD_REPAIR_PRIORITY = "repair_priority"  # low/normal/high
UD_DONE_CTX = "done_ctx"                # ticket_id для закрытия
UD_BUY_CONTEXT = "buy_ctx"              # {ticket_id} для закупки

# Возможные значения UD_MODE:
#   None
#   "choose_location_repair"   – выбрать помещение
#   "input_location_repair"    – ввести помещение вручную
#   "choose_equipment"         – выбрать оборудование
#   "input_equipment_custom"   – ввести оборудование вручную
#   "choose_priority_repair"   – выбрать срочность
#   "create_repair"            – описать проблему (текст/фото)
#   "create_purchase"          – свободное описание заявки на покупку
#   "await_done_photo"         – механик закрывает заявку (ждём фото или текст)
#   "await_buy_desc"           – механик описывает, что надо купить
#   "await_reason"             – ждём причину отказа/отклонения


# ======================
# СПРАВОЧНИКИ: ПОМЕЩЕНИЯ / ОБОРУДОВАНИЕ
# ======================

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
LOC_BACK = "◀️ Назад"

EQUIP_OTHER = "Другое оборудование…"
EQUIP_CANCEL = "↩ Отмена"
EQUIP_BACK = "◀️ Назад"

# Оборудование/зона ответственности по каждому помещению (из твоего списка)
EQUIPMENT_BY_LOCATION = {
    "цех варки 1": [
        "осушитель воздуха",
        "СБН-500",
        "СБН-200",
        "шнековый транспортер",
        "просеиватель новый",
        "просеиватель крашеный",
        "пылесос",
    ],
    "цех варки 2": [
        "обеспыливатель вертикальный",
        "обеспыливатель горизонтальный",
        "пылесос",
    ],
    "растарочная": [
        "дозатор 500",
        "дозатор 2000",
        "индукционный запайщик",
        "транспортер",
    ],
    "цех фасовки порошка": [
        "этикеровщик",
        "принтер",
        "термотонель",
    ],
    "цех фасовки капсул": [
        "счетная машина",
        "ручной этикеровщик",
        "индукционный запайщик",
        "принтер",
        "транспортер",
        "термотонель",
    ],
    "цех фасовки полуфабрикатов": [
        "стик новый",
        "стик старый",
        "саше новый",
        "саше старый",
    ],
    "административный отдел": [
        "директор",
        "мастера",
        "технологи",
        "АХО",
        "кухня",
        "уборная",
        "раздевалка",
        "лаборатория",
        "туалет",
    ],
    "склад": [
        "аппарат запайки резки",
        "термотонель",
        "ручной запайщик",
        "рохля",
    ],
}


# ======================
# УТИЛИТЫ ДАТ / ТЕКСТА
# ======================

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
    # Считает сколько занял ремонт = done_at - started_at
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
    # Делим длинный текст на куски до 4000 символов, чтобы не упереться в лимит телеги
    for i in range(0, len(s), limit):
        yield s[i:i+limit]

def ensure_int(s: str) -> int | None:
    try:
        return int(s)
    except Exception:
        return None


# ======================
# РАБОТА С БАЗОЙ ДАННЫХ
# ======================

async def init_db(app: Application):
    """
    Инициализация / миграция БД.
    Таблица tickets хранит:
    - location (помещение)
    - equipment (оборудование)
    - priority (срочность)
    - started_at / done_at
    """
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")

    # Таблица заявок
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
            equipment TEXT,
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

    # Таблица пользователей / ролей
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

    # Миграции существующей БД (если бот уже когда-то работал)
    try:
        async with db.execute("PRAGMA table_info(tickets);") as cur:
            cols = [row[1] async for row in cur]
        if "reason" not in cols:
            await db.execute("ALTER TABLE tickets ADD COLUMN reason TEXT;")
        if "location" not in cols:
            await db.execute("ALTER TABLE tickets ADD COLUMN location TEXT;")
        if "done_photo_file_id" not in cols:
            await db.execute("ALTER TABLE tickets ADD COLUMN done_photo_file_id TEXT;")
        if "equipment" not in cols:
            await db.execute("ALTER TABLE tickets ADD COLUMN equipment TEXT;")
    except Exception as e:
        log.warning(f"DB migration (tickets) check failed: {e}")

    try:
        async with db.execute("PRAGMA table_info(users);") as cur:
            cols = [row[1] async for row in cur]
        if "last_username" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN last_username TEXT;")
        if "last_seen" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN last_seen TEXT;")
        if "display_name" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN display_name TEXT;")
    except Exception as e:
        log.warning(f"DB migration (users) check failed: {e}")

    await db.commit()
    app.bot_data["db"] = db


async def db_close(app: Application):
    db = app.bot_data.get("db")
    if db:
        await db.close()


async def db_seen_user(db, uid: int, username: str | None):
    """
    Обновляем в users последний ник и время активности, чтобы потом /add_tech по @ника работал.
    """
    uname = (username or "").strip() or None
    now_iso = now_local().isoformat()
    await db.execute(
        "INSERT INTO users(uid, role, last_username, last_seen) "
        "VALUES(?, NULL, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET "
        "last_username=excluded.last_username, "
        "last_seen=excluded.last_seen",
        (uid, uname, now_iso),
    )
    await db.commit()


async def db_add_user_role(db, uid: int, role: str):
    """
    Выдать роль admin или tech.
    """
    await db.execute(
        "INSERT INTO users(uid, role) VALUES(?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET role=excluded.role",
        (uid, role),
    )
    await db.commit()


async def db_remove_user_role(db, uid: int):
    """
    Удалить роль пользователя (убрать из механиков/админов).
    """
    await db.execute(
        "DELETE FROM users WHERE uid=?",
        (uid,),
    )
    await db.commit()


async def db_set_display_name(db, uid: int, display_name: str):
    """
    Установить отображаемое имя для механика.
    """
    await db.execute(
        "INSERT INTO users(uid, display_name) VALUES(?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET display_name=excluded.display_name",
        (uid, display_name),
    )
    await db.commit()


async def db_get_display_name(db, uid: int) -> str | None:
    """
    Получить отображаемое имя механика из базы данных.
    Возвращает None если не задано.
    """
    async with db.execute(
        "SELECT display_name FROM users WHERE uid=? LIMIT 1",
        (uid,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def db_get_username(db, uid: int) -> str | None:
    """
    Получить последний известный username пользователя из базы.
    """
    async with db.execute(
        "SELECT last_username FROM users WHERE uid=? LIMIT 1",
        (uid,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def get_mechanic_display_name(db, uid: int, username: str | None = None) -> str:
    """
    Получить отображаемое имя механика.
    Приоритет: display_name > @username > user_id
    Если username не передан, пытается получить из базы данных.
    """
    # Сначала пытаемся получить display_name из базы
    display_name = await db_get_display_name(db, uid)
    if display_name:
        return display_name
    
    # Если нет display_name, пытаемся использовать username
    if not username:
        username = await db_get_username(db, uid)
    
    if username:
        return f"@{username}" if not username.startswith("@") else username
    
    # В крайнем случае возвращаем user_id
    return str(uid)


async def db_lookup_uid_by_username(db, username: str) -> int | None:
    """
    Поиск юзера по последнему известному @username.
    Нужно, чтобы админ мог написать /add_tech @ник.
    """
    uname = username.lstrip("@").strip().lower()
    async with db.execute(
        "SELECT uid FROM users WHERE lower(last_username)=? LIMIT 1",
        (uname,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def db_list_roles(db):
    """
    Возвращает два списка:
    - все админы
    - все техники
    (учитывая и захардкоженных, и выданных через БД)
    """
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
        "SELECT 1 FROM users WHERE uid=? AND role='admin' LIMIT 1",
        (uid,),
    ) as cur:
        row = await cur.fetchone()
    return bool(row)


async def is_tech(db, uid: int) -> bool:
    # Любой админ автоматически считается техником тоже.
    if uid in ENV_TECH_IDS or await is_admin(db, uid):
        return True
    async with db.execute(
        "SELECT 1 FROM users WHERE uid=? AND role='tech' LIMIT 1",
        (uid,),
    ) as cur:
        row = await cur.fetchone()
    return bool(row)


# ======================
# КЛАВИАТУРЫ
# ======================

async def main_menu(db, uid: int):
    """
    Главное меню. Мы теперь всегда шлём его в конце сценариев,
    чтобы меню не "пропадало".
    """
    if await is_admin(db, uid):
        rows = [
            [KeyboardButton("🛠 Заявка на ремонт"), KeyboardButton("🧾 Мои заявки")],
            [KeyboardButton("🛒 Заявка на покупку"), KeyboardButton("🛒 Мои покупки")],
            [KeyboardButton("🛠 Заявки на ремонт")],
            [KeyboardButton("🛒 Покупки"), KeyboardButton("📓 Журнал")],
            [KeyboardButton("📊 Аналитика"), KeyboardButton("👥 Управление")],
        ]
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    if await is_tech(db, uid):
        rows = [
            [KeyboardButton("🛠 Заявки на ремонт")],
            [KeyboardButton("🛒 Заявка на покупку"), KeyboardButton("🛒 Мои покупки")],
        ]
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    rows = [
        [KeyboardButton("🛠 Заявка на ремонт"), KeyboardButton("🧾 Мои заявки на ремонт")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def locations_keyboard():
    """
    Клавиатура выбора помещения
    """
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


def equipment_keyboard(location: str):
    """
    Клавиатура выбора оборудования после помещения.
    Добавляем список + "Другое оборудование…" + "◀️ Назад" + "↩ Отмена".
    """
    eq_list = EQUIPMENT_BY_LOCATION.get(location, [])
    rows = []
    row = []
    for i, eq in enumerate(eq_list, start=1):
        row.append(KeyboardButton(eq))
        if i % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([KeyboardButton(EQUIP_OTHER)])
    rows.append([KeyboardButton(EQUIP_BACK), KeyboardButton(EQUIP_CANCEL)])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def priority_keyboard():
    """
    Клавиатура выбора приоритета (срочности).
    """
    rows = [
        [KeyboardButton("🟢 Плановое (можно подождать)")],
        [KeyboardButton("🟡 Срочно, простой")],
        [KeyboardButton(LOC_BACK), KeyboardButton(LOC_CANCEL)],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def cancel_keyboard():
    """
    Простая клавиатура с кнопкой отмены для ручного ввода.
    """
    rows = [
        [KeyboardButton(LOC_CANCEL)],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ======================
# ОПЕРАЦИИ С ТИКЕТАМИ
# ======================

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
    equipment: str | None = None,
    priority: str | None = None,
):
    """
    Создаём заявку (ремонт или покупка).
    Для ремонта пишем location/equipment/priority.
    """
    now_iso = now_local().isoformat()
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
            equipment,
            reason,
            created_at, updated_at,
            started_at, done_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            None,    # done_photo_file_id
            None, None,  # assignee_id / assignee_name
            location,
            equipment,
            None,    # reason
            now_iso,
            now_iso,
            None,    # started_at
            None,    # done_at
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
    """
    Гибкий поиск заявок:
    - по типу (ремонт / покупка)
    - по статусу
    - по автору
    - по назначенному механику
    - только нераспределённые
    - по тексту/месту/оборудованию или по #id
    """
    sql = (
        "SELECT id, kind, status, priority, chat_id, user_id, username, description, "
        "photo_file_id, done_photo_file_id, assignee_id, assignee_name, "
        "location, equipment, reason, "
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
        # поиск по #ID
        if q.startswith("#") and q[1:].isdigit():
            where.append("id=?"); params.append(int(q[1:]))
        else:
            # поиск по описанию / помещению / оборудованию
            where.append(
                "(description LIKE ? OR location LIKE ? OR equipment LIKE ?)"
            )
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY id ASC LIMIT ? OFFSET ?"
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
                    "equipment": row[13],
                    "reason": row[14],
                    "created_at": row[15],
                    "updated_at": row[16],
                    "started_at": row[17],
                    "done_at": row[18],
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
               location, equipment, reason,
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
        "equipment": row[13],
        "reason": row[14],
        "created_at": row[15],
        "updated_at": row[16],
        "started_at": row[17],
        "done_at": row[18],
    }


async def update_ticket(db, ticket_id: int, **fields):
    """
    Обновление тикета (частично): статус, исполнитель, приоритет и т.д.
    Автоматически проставляет updated_at.
    """
    if not fields:
        return

    ALLOWED_COLUMNS = {
        "kind", "status", "priority", "chat_id", "user_id", "username",
        "description", "photo_file_id", "done_photo_file_id", "assignee_id",
        "assignee_name", "location", "equipment", "reason", "created_at",
        "updated_at", "started_at", "done_at"
    }
    
    for key in fields.keys():
        if key not in ALLOWED_COLUMNS:
            raise ValueError(f"Invalid column name: {key}")

    fields["updated_at"] = now_local().isoformat()
    cols = ", ".join([f"{k}=?" for k in fields.keys()])
    params = list(fields.values()) + [ticket_id]

    await db.execute(f"UPDATE tickets SET {cols} WHERE id=?", params)
    await db.commit()


# ======================
# ВЫВОД КАРТОЧЕК ЗАЯВОК
# ======================

def render_ticket_line(t: dict) -> str:
    """
    Человекочитаемый текст заявки:
    • статус
    • приоритет
    • помещение
    • оборудование
    • время создания / взятия / завершения
    • длительность
    """
    if t["kind"] == KIND_REPAIR:
        icon = "🛠"
        stat = {
            STATUS_NEW: "🆕 Новая",
            STATUS_IN_WORK: "⏱ В работе",
            STATUS_DONE: "✅ Выполнена",
            STATUS_REJECTED: "🛑 Отказ исполнителя",
            STATUS_CANCELED: "🗑 Отменена",
        }.get(t["status"], t["status"])

        prio_human = {
            "low": "🟢 плановое",
            "normal": "🟡 срочно",
            "high": "🔴 авария",
        }.get(t["priority"], t["priority"])

        assgn = f" • Исполнитель: {t['assignee_name'] or t['assignee_id'] or '—'}"

        loc_block = f"\nПомещение: {t.get('location') or '—'}"
        equip_block = f"\nОборудование: {t.get('equipment') or '—'}"

        times = f"\nСоздана: {fmt_dt(t['created_at'])}"
        if t["started_at"]:
            times += f" • Взята: {fmt_dt(t['started_at'])}"
        if t["done_at"]:
            times += (
                f" • Готово: {fmt_dt(t['done_at'])}"
                f" • Длит.: {human_duration(t['started_at'], t['done_at'])}"
            )

        reason = ""
        if t["status"] in (STATUS_REJECTED, STATUS_CANCELED) and t.get("reason"):
            reason = f"\nПричина: {t['reason']}"

        return (
            f"{icon} #{t['id']} • {stat} • Приоритет: {prio_human}{assgn}\n"
            f"{t['description']}{loc_block}{equip_block}{times}{reason}"
        )

    else:
        icon = "🛒"
        stat = {
            STATUS_NEW: "🆕 Новая",
            STATUS_APPROVED: "✅ Одобрена",
            STATUS_REJECTED: "🛑 Отклонена",
            STATUS_CANCELED: "🗑 Отменена",
        }.get(t["status"], t["status"])

        times = f"\nСоздана: {fmt_dt(t['created_at'])}"

        reason = ""
        if t["status"] in (STATUS_REJECTED, STATUS_CANCELED) and t.get("reason"):
            reason = f"\nПричина: {t['reason']}"

        return (
            f"{icon} #{t['id']} • {stat}\n"
            f"{t['description']}{times}{reason}"
        )


def ticket_inline_kb(ticket: dict, is_admin_flag: bool, me_id: int):
    """
    Инлайн-кнопки под карточкой заявки (назначение, приоритет, закрыть и т.д.)
    """
    kb = []

    if ticket["kind"] == KIND_REPAIR:
        if is_admin_flag:
            kb.append([
                InlineKeyboardButton("⚡ Приоритет ↑", callback_data=f"prio:{ticket['id']}")
            ])
            kb.append([
                InlineKeyboardButton("👤 Назначить себе", callback_data=f"assign_self:{ticket['id']}"),
                InlineKeyboardButton("👥 Назначить механику", callback_data=f"assign_menu:{ticket['id']}"),
            ])

        kb.append([
            InlineKeyboardButton("⏱ В работу", callback_data=f"to_work:{ticket['id']}")
        ])

        # Кнопки управления показываем исполнителю или администратору
        if ticket.get("assignee_id") == me_id or is_admin_flag:
            kb.append([
                InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{ticket['id']}")
            ])
            kb.append([
                InlineKeyboardButton("🛑 Отказ (с комментарием)", callback_data=f"decline:{ticket['id']}")
            ])
            kb.append([
                InlineKeyboardButton("🛒 Требует закупку", callback_data=f"need_buy:{ticket['id']}")
            ])

    elif ticket["kind"] == KIND_PURCHASE:
        if is_admin_flag:
            kb.append([
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{ticket['id']}"),
                InlineKeyboardButton("🛑 Отклонить (с причиной)", callback_data=f"reject:{ticket['id']}"),
            ])

    return InlineKeyboardMarkup(kb) if kb else None
# ======================
# ОТПРАВКА / РЕДАКТ КАРТОК
# ======================

async def send_ticket_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, t: dict, kb: InlineKeyboardMarkup | None):
    """
    Отправить карточку заявки в чат:
    - если ремонт с фото поломки -> фото с подписью
    - иначе просто текст
    """
    try:
        if t.get("photo_file_id") and t.get("kind") == KIND_REPAIR:
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
    Если исходное сообщение было с фото -> меняем подпись.
    Если текст -> меняем текст.
    """
    try:
        if getattr(query.message, "photo", None):
            await query.edit_message_caption(caption=new_text)
        else:
            await query.edit_message_text(new_text)
    except Exception as e:
        log.debug(f"edit_message_text_or_caption failed: {e}")


# ======================
# /start /help /whoami
# ======================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)

    kb = await main_menu(db, uid)

    await update.message.reply_text(
        "Привет! Это бот инженерно-технической службы.",
        reply_markup=kb
    )

    # сбрасываем состояние диалога
    context.user_data[UD_MODE] = None
    context.user_data[UD_REPAIR_LOC] = None
    context.user_data[UD_REPAIR_EQUIP] = None
    context.user_data[UD_REPAIR_PRIORITY] = None
    context.user_data[UD_DONE_CTX] = None
    context.user_data[UD_BUY_CONTEXT] = None
    context.user_data[UD_REASON_CONTEXT] = None


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Что я умею:\n\n"
        "• 🛠 Заявка на ремонт — выбери помещение → оборудование → срочность → опиши проблему (можно с фото).\n"
        "• 🧾 Мои заявки на ремонт — твои заявки на ремонт.\n"
        "\nДля механиков и администраторов:\n"
        "• 🛒 Заявка на покупку — запрос на закупку.\n"
        "• 🛒 Мои покупки — только заявки на покупку.\n"
        "• 🛠 Заявки на ремонт — список для механика/админа.\n"
        "• 🛒 Покупки — новые заявки на покупку для одобрения (админ).\n"
        "• 📓 Журнал — выполнено / в работе / отказы (админ).\n"
        "• 📊 Аналитика — статистика по заявкам (админ).\n"
        "• 👥 Управление — управление механиками и администраторами (админ).\n\n"
        "Команды:\n"
        "/repairs [status] [page]\n"
        "/me [status] [page]\n"
        "/mypurchases [page]\n"
        "/find <текст|#id>\n"
        "/export [week|month]\n"
        "/journal [days]\n"
        "/add_tech <user_id|@ник>\n"
        "/roles\n"
        "/whoami\n"
        "/help\n"
    )
    await update.message.reply_text(text)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)
    uname = update.effective_user.username or "—"
    await update.message.reply_text(f"Твой user_id: {uid}\nusername: @{uname}")


# ======================
# СОЗДАНИЕ ЗАЯВКИ: ЛОГИКА ДИАЛОГА
# ======================

async def on_text_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Центральный обработчик текстовых сообщений и кнопок ReplyKeyboard.
    Управляет сценарием:
        1. помещение
        2. оборудование
        3. приоритет
        4. описание (или фото с подписью)
    Плюс:
        - мои заявки
        - журнал
        - заявки на ремонт
        - заявки на покупку
        - закрытие заявок (режимы await_...)
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)

    text_in = (update.message.text or "").strip()
    mode = context.user_data.get(UD_MODE)

    # ===== ШАГ 0. НАЧАТЬ СОЗДАВАТЬ РЕМОНТ =====
    if text_in == "🛠 Заявка на ремонт" and mode is None:
        context.user_data[UD_MODE] = "choose_location_repair"
        context.user_data[UD_REPAIR_LOC] = None
        context.user_data[UD_REPAIR_EQUIP] = None
        context.user_data[UD_REPAIR_PRIORITY] = None

        await update.message.reply_text(
            "Выбери помещение:",
            reply_markup=locations_keyboard(),
        )
        return

    # ===== ШАГ 1. ВЫБОР ПОМЕЩЕНИЯ =====
    if mode == "choose_location_repair":
        if text_in == LOC_CANCEL:
            # Полная отмена
            context.user_data[UD_MODE] = None
            context.user_data[UD_REPAIR_LOC] = None
            context.user_data[UD_REPAIR_EQUIP] = None
            context.user_data[UD_REPAIR_PRIORITY] = None

            await update.message.reply_text(
                "Отмена.",
                reply_markup=await main_menu(db, uid),
            )
            return

        if text_in == LOC_OTHER:
            # хотим ввести помещение вручную
            context.user_data[UD_MODE] = "input_location_repair"
            await update.message.reply_text(
                "Введи помещение текстом:",
                reply_markup=cancel_keyboard(),
            )
            return

        if text_in in LOCATIONS:
            # выбрали помещение из списка
            context.user_data[UD_REPAIR_LOC] = text_in
            context.user_data[UD_MODE] = "choose_equipment"

            await update.message.reply_text(
                f"Помещение: {text_in}\n\nТеперь выбери оборудование:",
                reply_markup=equipment_keyboard(text_in),
            )
            return

        # непонятный ввод
        await update.message.reply_text(
            "Выбери помещение с клавиатуры или нажми «Другое помещение…».",
        )
        return

    # ручной ввод помещения (после "Другое помещение…")
    if mode == "input_location_repair":
        if text_in == LOC_CANCEL:
            # отмена всего
            context.user_data[UD_MODE] = None
            context.user_data[UD_REPAIR_LOC] = None
            context.user_data[UD_REPAIR_EQUIP] = None
            context.user_data[UD_REPAIR_PRIORITY] = None

            await update.message.reply_text(
                "Отмена.",
                reply_markup=await main_menu(db, uid),
            )
            return
        
        manual_loc = text_in
        if not manual_loc or manual_loc in (LOC_OTHER,):
            await update.message.reply_text(
                "Введи корректное название помещения или нажми «↩ Отмена».",
            )
            return

        context.user_data[UD_REPAIR_LOC] = manual_loc
        context.user_data[UD_MODE] = "choose_equipment"

        await update.message.reply_text(
            f"Помещение: {manual_loc}\n\nТеперь выбери оборудование:",
            reply_markup=equipment_keyboard(manual_loc),
        )
        return

    # ===== ШАГ 2. ВЫБОР ОБОРУДОВАНИЯ =====
    if mode == "choose_equipment":
        if text_in == EQUIP_BACK:
            # возвращаемся к выбору помещения
            context.user_data[UD_MODE] = "choose_location_repair"
            context.user_data[UD_REPAIR_EQUIP] = None
            
            await update.message.reply_text(
                "Выбери помещение:",
                reply_markup=locations_keyboard(),
            )
            return
        
        if text_in == EQUIP_CANCEL:
            # отменяем создание заявки полностью
            context.user_data[UD_MODE] = None
            context.user_data[UD_REPAIR_LOC] = None
            context.user_data[UD_REPAIR_EQUIP] = None
            context.user_data[UD_REPAIR_PRIORITY] = None

            await update.message.reply_text(
                "Отмена.",
                reply_markup=await main_menu(db, uid),
            )
            return

        if text_in == EQUIP_OTHER:
            # ручной ввод оборудования
            context.user_data[UD_MODE] = "input_equipment_custom"
            await update.message.reply_text(
                "Введи оборудование/узел текстом:",
                reply_markup=cancel_keyboard(),
            )
            return

        chosen_loc = context.user_data.get(UD_REPAIR_LOC)
        eq_list = EQUIPMENT_BY_LOCATION.get(chosen_loc, [])
        if text_in in eq_list:
            context.user_data[UD_REPAIR_EQUIP] = text_in
            context.user_data[UD_MODE] = "choose_priority_repair"

            await update.message.reply_text(
                f"Оборудование: {text_in}\n\nВыбери срочность:",
                reply_markup=priority_keyboard(),
            )
            return

        await update.message.reply_text(
            "Выбери оборудование с клавиатуры или нажми «Другое оборудование…».",
        )
        return

    # ручной ввод оборудования (после "Другое оборудование…")
    if mode == "input_equipment_custom":
        if text_in == LOC_CANCEL:
            # отмена всего
            context.user_data[UD_MODE] = None
            context.user_data[UD_REPAIR_LOC] = None
            context.user_data[UD_REPAIR_EQUIP] = None
            context.user_data[UD_REPAIR_PRIORITY] = None

            await update.message.reply_text(
                "Отмена.",
                reply_markup=await main_menu(db, uid),
            )
            return
        
        manual_equipment = text_in
        if not manual_equipment or manual_equipment in (EQUIP_OTHER,):
            await update.message.reply_text(
                "Введи корректное название оборудования или нажми «↩ Отмена».",
            )
            return

        context.user_data[UD_REPAIR_EQUIP] = manual_equipment
        context.user_data[UD_MODE] = "choose_priority_repair"

        await update.message.reply_text(
            f"Оборудование: {manual_equipment}\n\nВыбери срочность:",
            reply_markup=priority_keyboard(),
        )
        return

    # ===== ШАГ 3. ВЫБОР ПРИОРИТЕТА =====
    if mode == "choose_priority_repair":
        if text_in == LOC_BACK:
            # возвращаемся к выбору оборудования
            chosen_loc = context.user_data.get(UD_REPAIR_LOC)
            context.user_data[UD_MODE] = "choose_equipment"
            context.user_data[UD_REPAIR_PRIORITY] = None
            
            await update.message.reply_text(
                f"Помещение: {chosen_loc}\n\nВыбери оборудование:",
                reply_markup=equipment_keyboard(chosen_loc or ""),
            )
            return
        
        if text_in == LOC_CANCEL:
            # отмена всего
            context.user_data[UD_MODE] = None
            context.user_data[UD_REPAIR_LOC] = None
            context.user_data[UD_REPAIR_EQUIP] = None
            context.user_data[UD_REPAIR_PRIORITY] = None

            await update.message.reply_text(
                "Отмена.",
                reply_markup=await main_menu(db, uid),
            )
            return

        pr_map = {
            "🟢 Плановое (можно подождать)": "low",
            "🟡 Срочно, простой": "normal",
        }
        if text_in in pr_map:
            context.user_data[UD_REPAIR_PRIORITY] = pr_map[text_in]
            context.user_data[UD_MODE] = "create_repair"

            await update.message.reply_text(
                "Опиши проблему. Можно прикрепить фото с подписью.\n\n"
                "Текст или подпись к фото станет описанием заявки.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        await update.message.reply_text(
            "Выбери срочность с клавиатуры или нажми «↩ Отмена».",
        )
        return

    # ===== ШАГ 4. СОЗДАНИЕ ЗАЯВКИ НА ПОКУПКУ =====
    if text_in == "🛒 Заявка на покупку" and mode is None:
        context.user_data[UD_MODE] = "create_purchase"
        await update.message.reply_text(
            "Опиши, что нужно купить (наименование, количество, почему).",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # ===== МОИ ЗАЯВКИ =====
    if text_in in ("🧾 Мои заявки", "🧾 Мои заявки на ремонт") and mode is None:
        rows = await find_tickets(db, user_id=uid, limit=20, offset=0)
        if not rows:
            await update.message.reply_text(
                "У тебя пока нет заявок.",
                reply_markup=await main_menu(db, uid),
            )
            return
        for t in rows[:20]:
            await send_ticket_card(context, update.effective_chat.id, t, None)
        return

    # ===== МОИ ПОКУПКИ =====
    if text_in == "🛒 Мои покупки" and mode is None:
        rows = await find_tickets(
            db, kind=KIND_PURCHASE, user_id=uid, limit=20, offset=0
        )
        if not rows:
            await update.message.reply_text(
                "Твоих заявок на покупку пока нет.",
                reply_markup=await main_menu(db, uid),
            )
            return
        for t in rows[:20]:
            await send_ticket_card(context, update.effective_chat.id, t, None)
        return

    # ===== СПИСОК РЕМОНТОВ =====
    if text_in == "🛠 Заявки на ремонт" and mode is None:
        admin = await is_admin(db, uid)
        if admin:
            rows = await find_tickets(
                db, kind=KIND_REPAIR, status=STATUS_NEW, limit=20, offset=0
            )
            if not rows:
                await update.message.reply_text(
                    "Нет новых заявок на ремонт.",
                    reply_markup=await main_menu(db, uid),
                )
                return
            for t in rows:
                kb = ticket_inline_kb(t, is_admin_flag=True, me_id=uid)
                await send_ticket_card(
                    context, update.effective_chat.id, t, kb
                )
        else:
            new_unassigned = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=STATUS_NEW,
                unassigned_only=True,
                limit=20,
                offset=0,
            )
            new_assigned_to_me = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=STATUS_NEW,
                assignee_id=uid,
                limit=20,
                offset=0,
            )
            in_rows = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=STATUS_IN_WORK,
                assignee_id=uid,
                limit=20,
                offset=0,
            )
            rows = new_assigned_to_me + in_rows + new_unassigned
            if not rows:
                await update.message.reply_text(
                    "Нет доступных заявок.",
                    reply_markup=await main_menu(db, uid),
                )
                return
            for t in rows:
                kb = ticket_inline_kb(t, is_admin_flag=False, me_id=uid)
                await send_ticket_card(
                    context, update.effective_chat.id, t, kb
                )
        return

    # ===== СПИСОК НОВЫХ ПОКУПОК (для админа) =====
    if text_in == "🛒 Покупки" and mode is None:
        if not await is_admin(db, uid):
            await update.message.reply_text(
                "Недостаточно прав.",
                reply_markup=await main_menu(db, uid),
            )
            return
        rows = await find_tickets(
            db, kind=KIND_PURCHASE, status=STATUS_NEW, limit=20, offset=0
        )
        if not rows:
            await update.message.reply_text(
                "Нет новых заявок на покупку.",
                reply_markup=await main_menu(db, uid),
            )
            return
        for t in rows:
            kb = ticket_inline_kb(t, is_admin_flag=True, me_id=uid)
            await send_ticket_card(
                context, update.effective_chat.id, t, kb
            )
        return

    # ===== ЖУРНАЛ (быстрый доступ через кнопку) =====
    if text_in == "📓 Журнал" and mode is None:
        if not await is_admin(db, uid):
            await update.message.reply_text(
                "Недостаточно прав.",
                reply_markup=await main_menu(db, uid),
            )
            return
        await cmd_journal(update, context)
        return

    # ===== АНАЛИТИКА (быстрый доступ через кнопку) =====
    if text_in == "📊 Аналитика" and mode is None:
        if not await is_admin(db, uid):
            await update.message.reply_text(
                "Недостаточно прав.",
                reply_markup=await main_menu(db, uid),
            )
            return
        await cmd_analytics(update, context)
        return

    # ===== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ (быстрый доступ через кнопку) =====
    if text_in == "👥 Управление" and mode is None:
        if not await is_admin(db, uid):
            await update.message.reply_text(
                "Недостаточно прав.",
                reply_markup=await main_menu(db, uid),
            )
            return
        
        # Показываем список команд управления
        help_text = (
            "👥 Управление механиками и администраторами\n\n"
            "Доступные команды:\n\n"
            "📝 Добавить механика:\n"
            "/add_tech <user_id|@username>\n"
            "Пример: /add_tech @ivan\n\n"
            "👑 Добавить администратора:\n"
            "/add_admin <user_id|@username>\n"
            "Пример: /add_admin @maria\n\n"
            "❌ Удалить механика/админа:\n"
            "/remove_mechanic <user_id|@username>\n"
            "Пример: /remove_mechanic @ivan\n\n"
            "✏️ Установить отображаемое имя:\n"
            "/set_mechanic_name <user_id|@username> <имя>\n"
            "Пример: /set_mechanic_name @ivan Иван Петров\n\n"
            "📋 Список ролей:\n"
            "/roles\n\n"
            "💡 Совет: Пользователь должен сначала написать боту /start, "
            "чтобы попасть в систему."
        )
        
        await update.message.reply_text(
            help_text,
            reply_markup=await main_menu(db, uid),
        )
        return

    # ===== РЕЖИМ ЗАКРЫТИЯ ЗАЯВКИ МЕХАНИКОМ (без фото) =====
    if mode == "await_done_photo":
        tid = context.user_data.get(UD_DONE_CTX)
        if not tid:
            await update.message.reply_text(
                "Не удалось определить заявку для завершения.",
                reply_markup=await main_menu(db, uid),
            )
            context.user_data[UD_MODE] = None
            context.user_data[UD_DONE_CTX] = None
            return

        # Проверяем, что пользователь написал именно "готово"
        if text_in.lower() not in ("готово", "done", "ok"):
            await update.message.reply_text(
                "Пришли фото результата или напиши 'готово'.",
            )
            return

        t = await get_ticket(db, tid)
        if not t:
            await update.message.reply_text(
                "Заявка не найдена.",
                reply_markup=await main_menu(db, uid),
            )
        else:
            if t.get("assignee_id") != uid:
                await update.message.reply_text(
                    "Закрыть может только исполнитель.",
                    reply_markup=await main_menu(db, uid),
                )
            else:
                # Если не было started_at (заявку не брали официально "в работу"),
                # то поставим started_at сейчас, чтобы журнал не был пустой.
                if not t.get("started_at"):
                    await update_ticket(
                        db,
                        tid,
                        started_at=now_local().isoformat(),
                    )

                await update_ticket(
                    db,
                    tid,
                    status=STATUS_DONE,
                    done_at=now_local().isoformat(),
                )

                # уведомим автора
                try:
                    await context.bot.send_message(
                        chat_id=t["user_id"],
                        text=(f"Твоя заявка #{tid} отмечена как выполненная."),
                    )
                except Exception as e:
                    log.debug(f"Notify author done (text) failed: {e}")

                await update.message.reply_text(
                    f"Заявка #{tid} закрыта ✅.",
                    reply_markup=await main_menu(db, uid),
                )

        context.user_data[UD_MODE] = None
        context.user_data[UD_DONE_CTX] = None
        return

    # ===== РЕЖИМ ЗАКУПКИ ПО РЕМОНТУ =====
    if mode == "await_buy_desc":
        buy_ctx = context.user_data.get(UD_BUY_CONTEXT) or {}
        tid = buy_ctx.get("ticket_id")
        if not tid:
            await update.message.reply_text(
                "Не удалось связать с ремонтной заявкой.",
                reply_markup=await main_menu(db, uid),
            )
            context.user_data[UD_MODE] = None
            context.user_data[UD_BUY_CONTEXT] = None
            return

        base_ticket = await get_ticket(db, tid)
        loc = base_ticket.get("location") if base_ticket else "—"
        equip = base_ticket.get("equipment") if base_ticket else "—"

        uname = update.effective_user.username or ""
        chat_id = update.message.chat_id
        desc = (
            f"Запчасть для заявки #{tid} "
            f"({loc} / {equip}): {text_in}"
        )

        await create_ticket(
            db,
            kind=KIND_PURCHASE,
            chat_id=chat_id,
            user_id=uid,
            username=uname,
            description=desc,
            photo_file_id=None,
            location=None,
            equipment=None,
        )

        await update.message.reply_text(
            "Заявка на покупку создана и отправлена админу.",
            reply_markup=await main_menu(db, uid),
        )

        # Уведомить админов
        await notify_admins(
            context,
            f"🆕 Покупка по ремонту #{tid} от @{uname or uid}:\n{text_in}",
        )

        context.user_data[UD_MODE] = None
        context.user_data[UD_BUY_CONTEXT] = None
        return

    # ===== РЕЖИМ ОЖИДАНИЯ ПРИЧИНЫ ОТКАЗА / ОТКЛОНЕНИЯ =====
    if mode == "await_reason":
        await handle_reason_input(update, context)
        return

    # ===== СОЗДАНИЕ ЗАЯВКИ НА РЕМОНТ или ПОКУПКУ ПО ТЕКСТУ =====
    if mode in ("create_repair", "create_purchase"):
        await handle_create_from_text(update, context)
        return

    # Если вообще не узнал что это
    await update.message.reply_text(
        "Используй кнопки меню или /help.",
        reply_markup=await main_menu(db, uid),
    )


# ======================
# СОЗДАНИЕ ЗАЯВКИ ПО ТЕКСТУ
# ======================

async def handle_create_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь на шаге create_repair или create_purchase отправляет текст.
    Это финальное описание заявки.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    uname = update.effective_user.username or ""
    chat_id = update.message.chat_id
    mode = context.user_data.get(UD_MODE)

    description = (update.message.text or "").strip()
    if not description:
        await update.message.reply_text("Опиши заявку текстом.")
        return

    # ----- РЕМОНТ -----
    if mode == "create_repair":
        location = context.user_data.get(UD_REPAIR_LOC)
        equipment = context.user_data.get(UD_REPAIR_EQUIP)
        priority = context.user_data.get(UD_REPAIR_PRIORITY) or "normal"

        if not location:
            # Если почему-то потеряли помещение — вернём юзера в начало.
            context.user_data[UD_MODE] = "choose_location_repair"
            await update.message.reply_text(
                "Сначала выбери помещение:",
                reply_markup=locations_keyboard(),
            )
            return

        await create_ticket(
            db,
            kind=KIND_REPAIR,
            chat_id=chat_id,
            user_id=uid,
            username=uname,
            description=description,
            photo_file_id=None,
            location=location,
            equipment=equipment,
            priority=priority,
        )

        await update.message.reply_text(
            "Заявка на ремонт создана.\n"
            f"Помещение: {location}\n"
            f"Оборудование: {equipment or '—'}\n"
            f"Срочность сохранена.\n"
            "Админы и механики уведомлены.",
            reply_markup=await main_menu(db, uid),
        )

        # уведомим админов и механиков о новой заявке
        await notify_admins_ticket(context, uid)
        await notify_techs_ticket(context, uid)

        # сброс состояния
        context.user_data[UD_MODE] = None
        context.user_data[UD_REPAIR_LOC] = None
        context.user_data[UD_REPAIR_EQUIP] = None
        context.user_data[UD_REPAIR_PRIORITY] = None
        return

    # ----- ПОКУПКА -----
    if mode == "create_purchase":
        await create_ticket(
            db,
            kind=KIND_PURCHASE,
            chat_id=chat_id,
            user_id=uid,
            username=uname,
            description=description,
            photo_file_id=None,
            location=None,
            equipment=None,
        )

        await update.message.reply_text(
            "Заявка на покупку отправлена. Ожидает решения админа.",
            reply_markup=await main_menu(db, uid),
        )

        await notify_admins(
            context,
            f"🆕 Покупка от @{uname or uid}:\n{description}",
        )

        context.user_data[UD_MODE] = None
        return


# ======================
# СОЗДАНИЕ РЕМОНТА С ФОТО
# ======================

async def on_photo_with_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатываем два режима:
    1) await_done_photo — механик закрывает заявку и шлёт фото результата
    2) create_repair — оператор создаёт заявку, присылая фото поломки с подписью
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await db_seen_user(db, uid, update.effective_user.username)

    mode = context.user_data.get(UD_MODE)

    # ===== 1) МЕХАНИК ЗАКРЫВАЕТ ЗАЯВКУ С ФОТО ПОСЛЕ РЕМОНТА =====
    if mode == "await_done_photo":
        tid = context.user_data.get(UD_DONE_CTX)
        if not tid:
            await update.message.reply_text(
                "Не удалось определить заявку для завершения.",
                reply_markup=await main_menu(db, uid),
            )
            context.user_data[UD_MODE] = None
            context.user_data[UD_DONE_CTX] = None
            return

        t = await get_ticket(db, tid)
        if not t:
            await update.message.reply_text(
                "Заявка не найдена.",
                reply_markup=await main_menu(db, uid),
            )
        else:
            if t.get("assignee_id") != uid:
                await update.message.reply_text(
                    "Закрыть может только исполнитель.",
                    reply_markup=await main_menu(db, uid),
                )
            else:
                photo = update.message.photo[-1]
                file_id = photo.file_id

                # если не было started_at – подставим сейчас,
                # чтобы журнал не был пустой по времени начала
                if not t.get("started_at"):
                    await update_ticket(
                        db,
                        tid,
                        started_at=now_local().isoformat(),
                    )

                await update_ticket(
                    db,
                    tid,
                    status=STATUS_DONE,
                    done_at=now_local().isoformat(),
                    done_photo_file_id=file_id,
                )

                # уведомляем автора
                try:
                    await context.bot.send_message(
                        chat_id=t["user_id"],
                        text=(f"Твоя заявка #{tid} отмечена как выполненная."),
                    )
                except Exception as e:
                    log.debug(f"Notify author done-photo failed: {e}")

                await update.message.reply_text(
                    f"Заявка #{tid} закрыта ✅ (фото результата сохранено).",
                    reply_markup=await main_menu(db, uid),
                )

        context.user_data[UD_MODE] = None
        context.user_data[UD_DONE_CTX] = None
        return

    # ===== 2) ОПЕРАТОР СОЗДАЁТ РЕМОНТ С ФОТО ПОЛОМКИ =====
    if mode != "create_repair":
        # Пользователь просто шлёт фото не в том месте сценария
        await update.message.reply_text(
            "Чтобы создать заявку с фото: нажми «🛠 Заявка на ремонт», "
            "выбери помещение → оборудование → срочность, "
            "а потом пришли фото с подписью.",
            reply_markup=await main_menu(db, uid),
        )
        return

    uname = update.effective_user.username or ""
    chat_id = update.message.chat_id

    caption = (update.message.caption or "").strip()
    if not caption:
        await update.message.reply_text(
            "Добавь подпись к фото — это будет описанием заявки.",
        )
        return

    location = context.user_data.get(UD_REPAIR_LOC)
    equipment = context.user_data.get(UD_REPAIR_EQUIP)
    priority = context.user_data.get(UD_REPAIR_PRIORITY) or "normal"

    if not location:
        # потеряли помещение → вернём к выбору помещения
        context.user_data[UD_MODE] = "choose_location_repair"
        await update.message.reply_text(
            "Сначала выбери помещение:",
            reply_markup=locations_keyboard(),
        )
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id

    await create_ticket(
        db,
        kind=KIND_REPAIR,
        chat_id=chat_id,
        user_id=uid,
        username=uname,
        description=caption,
        photo_file_id=file_id,
        location=location,
        equipment=equipment,
        priority=priority,
    )

    await update.message.reply_text(
        "Заявка на ремонт с фото создана.\n"
        f"Помещение: {location}\n"
        f"Оборудование: {equipment or '—'}\n"
        f"Срочность сохранена.\n"
        "Админы и механики уведомлены.",
        reply_markup=await main_menu(db, uid),
    )

    # уведомим админов и механиков
    await notify_admins_ticket(context, uid)
    await notify_techs_ticket(context, uid)

    # сброс состояния
    context.user_data[UD_MODE] = None
    context.user_data[UD_REPAIR_LOC] = None
    context.user_data[UD_REPAIR_EQUIP] = None
    context.user_data[UD_REPAIR_PRIORITY] = None


# ======================
# УВЕДОМЛЕНИЯ
# ======================

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    """
    Шлём сообщение всем администраторам.
    """
    db = context.application.bot_data["db"]
    admins, _techs = await db_list_roles(db)
    for aid in admins:
        try:
            await context.bot.send_message(chat_id=aid, text=text)
        except Exception as e:
            log.debug(f"notify_admins fail {aid}: {e}")


async def notify_admins_ticket(context: ContextTypes.DEFAULT_TYPE, author_uid: int):
    """
    Берём самую свежую заявку автора и шлём админам карточку с инлайн-кнопками администратора.
    """
    db = context.application.bot_data["db"]
    # Берём самую последнюю заявку (с максимальным ID)
    async with db.execute(
        "SELECT id, kind, status, priority, chat_id, user_id, username, description, "
        "photo_file_id, done_photo_file_id, assignee_id, assignee_name, "
        "location, equipment, reason, created_at, updated_at, started_at, done_at "
        "FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (author_uid,)
    ) as cur:
        row = await cur.fetchone()
        if not row:
            return
        t = {
            "id": row[0], "kind": row[1], "status": row[2], "priority": row[3],
            "chat_id": row[4], "user_id": row[5], "username": row[6], "description": row[7],
            "photo_file_id": row[8], "done_photo_file_id": row[9],
            "assignee_id": row[10], "assignee_name": row[11],
            "location": row[12], "equipment": row[13], "reason": row[14],
            "created_at": row[15], "updated_at": row[16],
            "started_at": row[17], "done_at": row[18],
        }
    
    admins, _techs = await db_list_roles(db)
    for aid in admins:
        kb = ticket_inline_kb(t, is_admin_flag=True, me_id=aid)
        await send_ticket_card(context, aid, t, kb)


async def notify_techs_ticket(context: ContextTypes.DEFAULT_TYPE, author_uid: int):
    """
    То же самое, но отправляем механикам карточку с кнопками механика.
    """
    db = context.application.bot_data["db"]
    # Берём самую последнюю заявку (с максимальным ID)
    async with db.execute(
        "SELECT id, kind, status, priority, chat_id, user_id, username, description, "
        "photo_file_id, done_photo_file_id, assignee_id, assignee_name, "
        "location, equipment, reason, created_at, updated_at, started_at, done_at "
        "FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (author_uid,)
    ) as cur:
        row = await cur.fetchone()
        if not row:
            return
        t = {
            "id": row[0], "kind": row[1], "status": row[2], "priority": row[3],
            "chat_id": row[4], "user_id": row[5], "username": row[6], "description": row[7],
            "photo_file_id": row[8], "done_photo_file_id": row[9],
            "assignee_id": row[10], "assignee_name": row[11],
            "location": row[12], "equipment": row[13], "reason": row[14],
            "created_at": row[15], "updated_at": row[16],
            "started_at": row[17], "done_at": row[18],
        }
    
    _admins, techs = await db_list_roles(db)
    for tid in techs:
        kb = ticket_inline_kb(t, is_admin_flag=False, me_id=tid)
        await send_ticket_card(context, tid, t, kb)
# ======================
# АДМИН / ОТЧЁТЫ / СПИСКИ
# ======================

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /find <текст|#id>
    Только админ.
    Ищем по описанию / помещению / оборудованию или по номеру #ID.
    """
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
    """
    Получаем заявки за период (неделя / месяц) для CSV экспорта.
    """
    async with db.execute(
        """
        SELECT
            id, kind, status, priority,
            user_id, username,
            assignee_id, assignee_name,
            location, equipment,
            created_at, started_at, done_at,
            reason, description
        FROM tickets
        WHERE created_at >= ?
        ORDER BY id ASC
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
                    "equipment": row[9],
                    "created_at": row[10],
                    "started_at": row[11],
                    "done_at": row[12],
                    "reason": row[13],
                    "description": row[14],
                }
            )
    return rows


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /export [week|month]
    Только админ. Формируем CSV и отправляем.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id

    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return

    period = (context.args[0].lower() if context.args else "week").strip()
    if period not in ("week", "month"):
        await update.message.reply_text("Использование: /export [week|month]")
        return

    now_ = now_local()
    start = now_ - (timedelta(days=7) if period == "week" else timedelta(days=30))

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
        "equipment",
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
            r.get("equipment") or "",
            r["created_at"],
            r["started_at"] or "",
            r["done_at"] or "",
            dur,
            r["reason"] or "",
            (r["description"] or "").replace("\n", " ")[:500],
        ])

    data = buf.getvalue().encode("utf-8")

    await update.message.reply_document(
        document=InputFile(
            io.BytesIO(data),
            filename=f"tickets_{period}.csv"
        ),
        caption=f"Экспорт за {period}.",
    )


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /journal [days]
    Только админ.
    Журнал ремонтов за N дней:
    - В работе
    - Выполнена
    - Отказ исполнителя
    Показываем помещение / оборудование.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id

    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return

    days = ensure_int(context.args[0]) if context.args else 30
    days = days or 30
    since = now_local() - timedelta(days=days)

    async with db.execute(
        """
        SELECT id, description, location, equipment,
               assignee_name, assignee_id,
               started_at, done_at,
               created_at, updated_at,
               status, reason
        FROM tickets
        WHERE kind='repair'
          AND status IN ('in_work','done','rejected')
          AND updated_at >= ?
        ORDER BY updated_at ASC
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
        equip,
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
            STATUS_IN_WORK: "⏱ В работе",
            STATUS_DONE: "✅ Выполнена",
            STATUS_REJECTED: "🛑 Отказ исполнителя",
        }.get(status, status)

        created_s = f"Создана: {fmt_dt(created)}"
        loc_s = f"Помещение: {loc or '—'}"
        equip_s = f"Оборудование: {equip or '—'}"

        if status == STATUS_IN_WORK:
            dur = (
                human_duration(started, now_local().isoformat())
                if started
                else "—"
            )
            line = (
                f"#{id_} • {status_text} • Исп.: {who}\n"
                f"{loc_s}\n{equip_s}\n"
                f"{created_s} • Взята: {fmt_dt(started)} • "
                f"Длит.: {dur}\n"
                f"{desc}"
            )

        elif status == STATUS_DONE:
            dur = human_duration(started, done)
            line = (
                f"#{id_} • {status_text} • Исп.: {who}\n"
                f"{loc_s}\n{equip_s}\n"
                f"{created_s} • Взята: {fmt_dt(started)} • "
                f"Готово: {fmt_dt(done)} • "
                f"Длит.: {dur}\n"
                f"{desc}"
            )

        else:  # отказ исполнителя
            if started:
                timing_part = (
                    f"{created_s} • Взята: {fmt_dt(started)} • "
                    f"Обновлена: {fmt_dt(updated)}"
                )
            else:
                timing_part = (
                    f"{created_s} • Обновлена: {fmt_dt(updated)}"
                )

            line = (
                f"#{id_} • {status_text} • Исп.: {who}\n"
                f"{loc_s}\n{equip_s}\n"
                f"{timing_part}\n"
                f"{desc}"
            )
            if reason:
                line += f"\nПричина: {reason}"

        lines.append(line)

    text_out = "\n\n".join(lines)
    for part in chunk_text(text_out):
        await update.message.reply_text(part)


async def cmd_repairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /repairs [status] [page]
    status = new|in_work|done|all
    Админ видит всё. Механик видит доступные/свои.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id

    status_arg = (context.args[0].lower() if context.args else "new").strip()
    page = ensure_int(context.args[1]) if len(context.args) >= 2 else 1
    page = max(1, page or 1)
    offset = (page - 1) * 20

    status_map = {
        "new": STATUS_NEW,
        "in_work": STATUS_IN_WORK,
        "done": STATUS_DONE,
        "all": None,
    }
    stat = status_map.get(status_arg, STATUS_NEW)

    admin = await is_admin(db, uid)
    if admin:
        if stat:
            rows = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=stat,
                limit=20,
                offset=offset,
            )
        else:
            rows = await find_tickets(
                db,
                kind=KIND_REPAIR,
                limit=20,
                offset=offset,
            )
    else:
        # техник
        if stat == STATUS_NEW:
            unassigned = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=STATUS_NEW,
                unassigned_only=True,
                limit=20,
                offset=offset,
            )
            assigned_to_me = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=STATUS_NEW,
                assignee_id=uid,
                limit=20,
                offset=0,
            )
            rows = assigned_to_me + unassigned

        elif stat == STATUS_IN_WORK:
            rows = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=STATUS_IN_WORK,
                assignee_id=uid,
                limit=20,
                offset=offset,
            )

        elif stat == STATUS_DONE:
            rows = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=STATUS_DONE,
                assignee_id=uid,
                limit=20,
                offset=offset,
            )

        elif stat is None:  # all
            assigned_new = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=STATUS_NEW,
                assignee_id=uid,
                limit=20,
                offset=0,
            )
            in_work = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=STATUS_IN_WORK,
                assignee_id=uid,
                limit=20,
                offset=0,
            )
            unassigned_new = await find_tickets(
                db,
                kind=KIND_REPAIR,
                status=STATUS_NEW,
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
    """
    /me [status] [page]
    Показывает заявки, где я назначен исполнителем.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    status_arg = (context.args[0].lower() if context.args else "in_work").strip()

    page = ensure_int(context.args[1]) if len(context.args) >= 2 else 1
    page = max(1, page or 1)
    offset = (page - 1) * 20

    status_map = {
        "new": STATUS_NEW,
        "in_work": STATUS_IN_WORK,
        "done": STATUS_DONE,
        "all": None,
    }
    stat = status_map.get(status_arg, STATUS_IN_WORK)

    if stat:
        rows = await find_tickets(
            db,
            kind=KIND_REPAIR,
            status=stat,
            assignee_id=uid,
            limit=20,
            offset=offset,
        )
    else:
        rows = await find_tickets(
            db,
            kind=KIND_REPAIR,
            assignee_id=uid,
            limit=20,
            offset=offset,
        )

    if not rows:
        await update.message.reply_text("Пока пусто.")
        return

    admin_flag = await is_admin(db, uid)
    for t in rows:
        kb = ticket_inline_kb(t, is_admin_flag=admin_flag, me_id=uid)
        await send_ticket_card(context, update.effective_chat.id, t, kb)


async def cmd_mypurchases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mypurchases [page]
    Показывает мои заявки на покупку.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id

    page = ensure_int(context.args[0]) if context.args else 1
    page = max(1, page or 1)
    offset = (page - 1) * 20

    rows = await find_tickets(
        db,
        kind=KIND_PURCHASE,
        user_id=uid,
        limit=20,
        offset=offset,
    )

    if not rows:
        await update.message.reply_text("Твоих заявок на покупку пока нет.")
        return

    for t in rows:
        await send_ticket_card(context, update.effective_chat.id, t, None)


async def cmd_add_tech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_tech <user_id|@username>
    Только админ. Выдаёт роль механика (tech).
    """
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
    """
    /roles
    Показывает списки uid админов и механиков.
    """
    db = context.application.bot_data["db"]
    admins, techs = await db_list_roles(db)
    text = (
        "Роли:\n\nАдмины:\n"
        + (", ".join(map(str, admins)) or "—")
        + "\n\nМеханики:\n"
        + (", ".join(map(str, techs)) or "—")
    )
    await update.message.reply_text(text)


async def cmd_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_admin <user_id|@username>
    Только админ. Выдаёт роль администратора.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id

    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /add_admin <user_id|@username>\n"
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

    await db_add_user_role(db, target, "admin")
    await update.message.reply_text(
        f"Пользователь {target} добавлен как администратор."
    )


async def cmd_remove_mechanic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /remove_mechanic <user_id|@username>
    Только админ. Удаляет механика из системы.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id

    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /remove_mechanic <user_id|@username>\n"
            "Удаляет пользователя из списка механиков/администраторов."
        )
        return

    arg = context.args[0].strip()
    target = ensure_int(arg)
    if not target and arg.startswith("@"):
        target = await db_lookup_uid_by_username(db, arg)
    if not target:
        await update.message.reply_text(
            "Укажи числовой user_id или @username."
        )
        return

    # Проверяем, что это не захардкоженный админ
    if target in HARD_ADMIN_IDS:
        await update.message.reply_text(
            f"Нельзя удалить захардкоженного администратора ({target})."
        )
        return

    await db_remove_user_role(db, target)
    await update.message.reply_text(
        f"Пользователь {target} удален из системы (больше не механик/админ)."
    )


async def cmd_set_mechanic_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /set_mechanic_name <user_id|@username> <отображаемое имя>
    Только админ. Устанавливает отображаемое имя для механика.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id

    if not await is_admin(db, uid):
        await update.message.reply_text("Недостаточно прав.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /set_mechanic_name <user_id|@username> <отображаемое имя>\n"
            "Пример: /set_mechanic_name @ivan Иван Петров\n"
            "Или: /set_mechanic_name 123456789 Иван Петров"
        )
        return

    # Первый аргумент - user_id или @username
    arg = context.args[0].strip()
    target = ensure_int(arg)
    if not target and arg.startswith("@"):
        target = await db_lookup_uid_by_username(db, arg)
    if not target:
        await update.message.reply_text(
            "Укажи числовой user_id или @username (пользователь должен сначала написать боту /start)."
        )
        return

    # Остальные аргументы - отображаемое имя
    display_name = " ".join(context.args[1:]).strip()
    if not display_name:
        await update.message.reply_text("Укажи отображаемое имя механика.")
        return

    # Проверяем, что пользователь является механиком или админом
    if not await is_tech(db, target):
        await update.message.reply_text(
            f"Пользователь {target} не является механиком или администратором.\n"
            f"Сначала добавь его через /add_tech или /add_admin."
        )
        return

    await db_set_display_name(db, target, display_name)
    await update.message.reply_text(
        f"Установлено отображаемое имя для {target}: {display_name}"
    )


async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /analytics или кнопка "📊 Аналитика"
    Только админ. Показывает детальную статистику по заявкам:
    - Общее количество заявок
    - Статистика по помещениям
    - Статистика по оборудованию
    - Детализация по механикам с помещениями и оборудованием
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id

    if not await is_admin(db, uid):
        await update.message.reply_text(
            "Недостаточно прав.",
            reply_markup=await main_menu(db, uid),
        )
        return

    # Общее количество заявок
    async with db.execute("SELECT COUNT(*) FROM tickets") as cur:
        row = await cur.fetchone()
        total_tickets = row[0] if row else 0

    # Количество заявок по типам
    async with db.execute(
        "SELECT kind, COUNT(*) FROM tickets GROUP BY kind"
    ) as cur:
        kind_stats = {kind: count async for kind, count in cur}

    # Статистика по помещениям
    async with db.execute(
        """
        SELECT location, COUNT(*) 
        FROM tickets 
        WHERE location IS NOT NULL AND kind='repair'
        GROUP BY location
        ORDER BY COUNT(*) DESC
        LIMIT 10
        """
    ) as cur:
        location_stats = []
        async for loc, count in cur:
            location_stats.append((loc, count))

    # Статистика по оборудованию
    async with db.execute(
        """
        SELECT equipment, COUNT(*) 
        FROM tickets 
        WHERE equipment IS NOT NULL AND kind='repair'
        GROUP BY equipment
        ORDER BY COUNT(*) DESC
        LIMIT 10
        """
    ) as cur:
        equipment_stats = []
        async for equip, count in cur:
            equipment_stats.append((equip, count))

    # Детализированная статистика по механикам
    async with db.execute(
        """
        SELECT assignee_id, assignee_name, 
               location, equipment,
               COUNT(*) as cnt
        FROM tickets 
        WHERE assignee_id IS NOT NULL 
          AND kind='repair'
          AND status IN ('done', 'in_work')
        GROUP BY assignee_id, assignee_name, location, equipment
        ORDER BY assignee_name, cnt DESC
        """
    ) as cur:
        mechanic_details = []
        async for aid, aname, loc, equip, count in cur:
            mechanic_details.append((aid, aname, loc, equip, count))

    # Общая статистика по механикам с разбивкой по статусам
    async with db.execute(
        """
        SELECT assignee_id, assignee_name, 
               SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done_count,
               SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected_count,
               COUNT(*) as total_count
        FROM tickets 
        WHERE assignee_id IS NOT NULL 
        GROUP BY assignee_id, assignee_name
        ORDER BY total_count DESC
        """
    ) as cur:
        mechanic_totals = []
        async for aid, aname, done_cnt, rejected_cnt, total_cnt in cur:
            mechanic_totals.append((aid, aname, done_cnt, rejected_cnt, total_cnt))

    # Формируем текст
    repair_count = kind_stats.get(KIND_REPAIR, 0)
    purchase_count = kind_stats.get(KIND_PURCHASE, 0)

    text = f"📊 Аналитика\n\n"
    text += f"Всего заявок: {total_tickets}\n"
    text += f"  • Ремонт: {repair_count}\n"
    text += f"  • Покупка: {purchase_count}\n\n"

    # Статистика по помещениям
    if location_stats:
        text += "🏢 Топ помещений по заявкам:\n"
        for loc, count in location_stats[:5]:
            text += f"  • {loc}: {count} заявок\n"
        text += "\n"

    # Статистика по оборудованию
    if equipment_stats:
        text += "🔧 Топ оборудования по заявкам:\n"
        for equip, count in equipment_stats[:5]:
            text += f"  • {equip}: {count} заявок\n"
        text += "\n"

    # Статистика по механикам (общая)
    if mechanic_totals:
        text += "👷 Статистика по механикам:\n"
        for aid, aname, done_cnt, rejected_cnt, total_cnt in mechanic_totals:
            name_display = aname or str(aid)
            text += f"\n{name_display}: {total_cnt} заявок\n"
            text += f"  ✅ Выполнено: {done_cnt}\n"
            text += f"  🛑 Отклонено: {rejected_cnt}\n"
            
            # Детализация по этому механику (топ помещения/оборудование)
            mech_details = [
                (loc, equip, cnt) 
                for m_aid, m_aname, loc, equip, cnt in mechanic_details 
                if m_aid == aid
            ]
            if mech_details:
                text += f"  📍 Топ работ:\n"
                for loc, equip, cnt in mech_details[:3]:  # топ-3
                    loc_text = loc or "—"
                    equip_text = equip or "—"
                    text += f"    └ {loc_text} / {equip_text}: {cnt}\n"
    else:
        text += "Пока нет заявок, взятых механиками.\n"

    # Разбиваем на части, если слишком длинный
    for chunk in chunk_text(text, 4000):
        await update.message.reply_text(
            chunk,
            reply_markup=await main_menu(db, uid) if chunk == text or chunk.endswith(text[-100:]) else None,
        )


# ======================
# INLINE CALLBACK HANDLER
# ======================

def extract_ticket_id_from_message(text: str) -> int | None:
    """
    Достаём номер заявки из текста карточки/подписи — ищем '#<число>'.
    """
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


async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатываем нажатия инлайн-кнопок:
    - prio: поднять приоритет
    - assign_self / assign_menu / assign_to : назначение механика
    - to_work: взять в работу
    - done: выполнить
    - decline: отказ с причиной
    - need_buy: запросить закупку
    - approve / reject: решения по покупке
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    uname = update.effective_user.username or ""

    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # Меню выбора конкретного механика (админ)
    if data.startswith("assign_menu:"):
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return

        admins, techs = await db_list_roles(db)
        kb = []
        row = []
        for i, tech_uid in enumerate(techs, start=1):
            # Получаем отображаемое имя механика для кнопки
            display_name = await get_mechanic_display_name(db, tech_uid)
            row.append(
                InlineKeyboardButton(
                    display_name, callback_data=f"assign_to:{tech_uid}"
                )
            )
            if i % 3 == 0:
                kb.append(row)
                row = []
        if row:
            kb.append(row)

        kb.append([InlineKeyboardButton("↩️ Назад", callback_data="assign_back")])

        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "assign_back":
        await query.answer("Выбери техника или команду ниже.", show_alert=False)
        return

    # Назначение на конкретного механика (админ)
    if data.startswith("assign_to:"):
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return

        tid = extract_ticket_id_from_message(query.message.caption or query.message.text or "")
        assignee = ensure_int(data.split(":", 1)[1])
        if not tid or not assignee:
            await query.answer("Не удалось определить заявку/пользователя.")
            return

        # Получаем отображаемое имя механика
        assignee_display = await get_mechanic_display_name(db, assignee)

        await update_ticket(
            db,
            tid,
            assignee_id=assignee,
            assignee_name=assignee_display,
        )

        await edit_message_text_or_caption(
            query,
            (query.message.caption or query.message.text or "")
            + f"\n\nНазначено: {assignee}",
        )

        # кинуть механику карточку в личку
        try:
            t = await get_ticket(db, tid)
            if t:
                kb_for_tech = ticket_inline_kb(t, is_admin_flag=False, me_id=assignee)
                await send_ticket_card(context, assignee, t, kb_for_tech)
        except Exception as e:
            log.debug(f"Notify assignee {assignee} card failed: {e}")
        return

    # Админ назначает заявку себе
    if data.startswith("assign_self:"):
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return

        tid = ensure_int(data.split(":", 1)[1])
        if not tid:
            return

        # Получаем отображаемое имя механика
        assignee_display = await get_mechanic_display_name(db, uid, uname)

        await update_ticket(
            db,
            tid,
            assignee_id=uid,
            assignee_name=assignee_display,
        )

        await edit_message_text_or_caption(
            query,
            (query.message.caption or query.message.text or "")
            + f"\n\nНазначено: @{uname or uid}",
        )

        # отправить себе карточку
        try:
            t = await get_ticket(db, tid)
            if t:
                kb_for_me = ticket_inline_kb(t, is_admin_flag=False, me_id=uid)
                await send_ticket_card(context, uid, t, kb_for_me)
        except Exception as e:
            log.debug(f"Notify self with card failed: {e}")
        return

    # Поднять приоритет (только админ)
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
            (query.message.caption or query.message.text or "")
            + f"\n\nПриоритет: {new}",
        )
        return

    # Механик жмёт «⏱ В работу»
    if data.startswith("to_work:"):
        tid = ensure_int(data.split(":", 1)[1])
        if not tid:
            return
        t = await get_ticket(db, tid)
        if not t or t["kind"] != KIND_REPAIR:
            await query.answer("Некорректная заявка.")
            return
        if t["status"] != STATUS_NEW:
            await query.answer("Заявка уже не новая.")
            return

        # защита: если автор админ, заявку должен распределить админ,
        # не даём обычному механику схватить без назначения
        author_is_admin = await is_admin(db, t["user_id"])
        if author_is_admin and (not await is_admin(db, uid)) and not t["assignee_id"]:
            await query.answer("Эту заявку должен распределить админ.")
            return

        # если заявка назначена не на меня, и я не админ — не даём воровать
        if t["assignee_id"] and t["assignee_id"] != uid and not await is_admin(db, uid):
            await query.answer("Заявка назначена другому.")
            return

        now_iso = now_local().isoformat()

        # если ещё нет исполнителя — назначаем того, кто нажал
        if not t["assignee_id"]:
            # Получаем отображаемое имя механика
            assignee_display = await get_mechanic_display_name(db, uid, uname)
            
            await update_ticket(
                db,
                tid,
                assignee_id=uid,
                assignee_name=assignee_display,
            )

        # ставим статус в работу и фиксируем started_at, если пусто
        await update_ticket(
            db,
            tid,
            status=STATUS_IN_WORK,
            started_at=t["started_at"] or now_iso,
        )

        await edit_message_text_or_caption(
            query,
            (query.message.caption or query.message.text or "")
            + "\n\nСтатус: ⏱ В работе",
        )

        # уведомляем автора
        try:
            mechanic_name = await get_mechanic_display_name(db, uid, uname)
            await context.bot.send_message(
                chat_id=t["user_id"],
                text=(
                    f"Твоя заявка #{tid} взята в работу механиком "
                    f"{mechanic_name}."
                ),
            )
        except Exception as e:
            log.debug(f"Notify author start-work failed: {e}")
        return

    # Механик или администратор жмёт «✅ Выполнено»
    if data.startswith("done:"):
        tid = ensure_int(data.split(":", 1)[1])
        t = await get_ticket(db, tid)
        if not t or t["kind"] != KIND_REPAIR:
            await query.answer("Некорректная заявка.")
            return

        # закрывать может исполнитель или администратор
        user_is_admin = await is_admin(db, uid)
        if t.get("assignee_id") != uid and not user_is_admin:
            await query.answer("Только исполнитель или администратор может закрыть заявку.")
            return

        # Если не было started_at, поставим сейчас
        if not t.get("started_at"):
            await update_ticket(
                db,
                tid,
                started_at=now_local().isoformat(),
            )

        # Закрываем заявку
        await update_ticket(
            db,
            tid,
            status=STATUS_DONE,
            done_at=now_local().isoformat(),
        )

        await query.answer("Заявка выполнена ✅")
        
        await edit_message_text_or_caption(
            query,
            (query.message.caption or query.message.text or "")
            + "\n\nСтатус: ✅ Выполнена",
        )

        # Уведомляем автора
        try:
            await context.bot.send_message(
                chat_id=t["user_id"],
                text=f"Твоя заявка #{tid} отмечена как выполненная.",
            )
        except Exception as e:
            log.debug(f"Notify author done failed: {e}")
        return

    # Механик или администратор жмёт «🛑 Отказ (с комментарием)»
    if data.startswith("decline:"):
        tid = ensure_int(data.split(":", 1)[1])
        if not tid:
            return
        t = await get_ticket(db, tid)
        if not t or t["kind"] != KIND_REPAIR:
            await query.answer("Некорректная заявка.")
            return
        
        # Отказать может исполнитель или администратор
        user_is_admin = await is_admin(db, uid)
        if t.get("assignee_id") != uid and not user_is_admin:
            await query.answer("Только исполнитель или администратор может отказать по заявке.")
            return

        context.user_data[UD_MODE] = "await_reason"
        context.user_data[UD_REASON_CONTEXT] = {
            "action": "decline_repair",
            "ticket_id": tid,
        }

        await edit_message_text_or_caption(
            query,
            (query.message.caption or query.message.text or "")
            + "\n\nНапиши причину отказа сообщением:",
        )
        return

    # Механик жмёт «🛒 Требует закупку»
    if data.startswith("need_buy:"):
        tid = ensure_int(data.split(":", 1)[1])
        t = await get_ticket(db, tid)
        if not t or t["kind"] != KIND_REPAIR:
            await query.answer("Некорректная заявка.")
            return

        # Только исполнитель или админ может инициировать закупку
        if t.get("assignee_id") != uid and not await is_admin(db, uid):
            await query.answer("Только исполнитель или админ может запросить закупку.")
            return

        context.user_data[UD_MODE] = "await_buy_desc"
        context.user_data[UD_BUY_CONTEXT] = {"ticket_id": tid}

        await edit_message_text_or_caption(
            query,
            (query.message.caption or query.message.text or "")
            + "\n\nЧто нужно закупить? Укажи наименование, количество и причину.",
        )
        return

    # Админ жмёт «✅ Одобрить» покупку
    if data.startswith("approve:"):
        if not await is_admin(db, uid):
            await edit_message_text_or_caption(query, "Недостаточно прав.")
            return

        tid = ensure_int(data.split(":", 1)[1])
        await update_ticket(db, tid, status=STATUS_APPROVED)

        await edit_message_text_or_caption(
            query,
            (query.message.caption or query.message.text or "")
            + "\n\nСтатус: ✅ Одобрена",
        )

        t = await get_ticket(db, tid)
        if t:
            try:
                await context.bot.send_message(
                    chat_id=t["user_id"],
                    text=(f"Твоя заявка на покупку #{tid} одобрена."),
                )
            except Exception as e:
                log.debug(f"Notify author approve failed: {e}")
        return

    # Админ жмёт «🛑 Отклонить (с причиной)»
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
            (query.message.caption or query.message.text or "")
            + "\n\nНапиши причину отказа сообщением:",
        )
        return


# ======================
# ПРИЧИНА ОТКАЗА / ОТКЛОНЕНИЯ / ОТМЕНЫ
# ======================

async def handle_reason_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь ввёл причину:
    - cancel (отмена админом) [зарезервировано]
    - reject (отклонить заявку на покупку)
    - decline_repair (исполнитель отказывается по ремонту)
    После выполнения возвращаем главное меню.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id

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
            "Контекст потерян. Попробуй снова.",
            reply_markup=await main_menu(db, uid),
        )
        context.user_data[UD_MODE] = None
        context.user_data[UD_REASON_CONTEXT] = None
        return

    t = await get_ticket(db, tid)
    if not t:
        await update.message.reply_text(
            "Заявка не найдена.",
            reply_markup=await main_menu(db, uid),
        )
        context.user_data[UD_MODE] = None
        context.user_data[UD_REASON_CONTEXT] = None
        return

    # отмена админом (на будущее)
    if action == "cancel":
        await update_ticket(db, tid, status=STATUS_CANCELED, reason=reason_text)
        await update.message.reply_text(
            f"Заявка #{tid} отменена.",
            reply_markup=await main_menu(db, uid),
        )

    # отклонение заявки на покупку админом
    elif action == "reject":
        await update_ticket(db, tid, status=STATUS_REJECTED, reason=reason_text)

        await update.message.reply_text(
            f"Заявка #{tid} отклонена.",
            reply_markup=await main_menu(db, uid),
        )

        # уведомить автора
        try:
            await context.bot.send_message(
                chat_id=t["user_id"],
                text=(f"Твоя заявка #{tid} отклонена: {reason_text}"),
            )
        except Exception as e:
            log.debug(f"Notify author reject failed: {e}")

    # отказ исполнителя от ремонта
    elif action == "decline_repair":
        # только исполнитель может отказать
        if t.get("assignee_id") != uid:
            await update.message.reply_text(
                "Отказ может оформить только исполнитель.",
                reply_markup=await main_menu(db, uid),
            )
        else:
            await update_ticket(
                db,
                tid,
                status=STATUS_REJECTED,
                reason=reason_text,
            )

            await update.message.reply_text(
                f"Заявка #{tid} помечена как отказ исполнителя.",
                reply_markup=await main_menu(db, uid),
            )

            # уведомим автора
            try:
                await context.bot.send_message(
                    chat_id=t["user_id"],
                    text=(
                        f"По твоей заявке #{tid} исполнитель отказался:\n"
                        f"{reason_text}"
                    ),
                )
            except Exception as e:
                log.debug(f"Notify author decline_repair failed: {e}")

    context.user_data[UD_MODE] = None
    context.user_data[UD_REASON_CONTEXT] = None


# ======================
# FALLBACK / UNKNOWN
# ======================

async def on_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ловим команды, которые мы не знаем.
    """
    db = context.application.bot_data["db"]
    uid = update.effective_user.id
    await update.message.reply_text(
        "Я не понял команду. Используй кнопки меню или /help.",
        reply_markup=await main_menu(db, uid),
    )


# ======================
# ЗАПУСК ПРИЛОЖЕНИЯ
# ======================

def build_application() -> Application:
    """
    Создаём Application, регистрируем хендлеры.
    """
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    # Команды
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
    app.add_handler(CommandHandler("add_admin", cmd_add_admin))
    app.add_handler(CommandHandler("remove_mechanic", cmd_remove_mechanic))
    app.add_handler(CommandHandler("set_mechanic_name", cmd_set_mechanic_name))
    app.add_handler(CommandHandler("roles", cmd_roles))
    app.add_handler(CommandHandler("analytics", cmd_analytics))

    # Инлайн-кнопки из карточек
    app.add_handler(CallbackQueryHandler(cb_handler))

    # Фото с подписью (либо закрытие заявки с фото, либо создание заявки с фото)
    app.add_handler(
        MessageHandler(
            filters.PHOTO & (~filters.COMMAND),
            on_photo_with_caption,
        )
    )

    # Любые текстовые сообщения и кнопки ReplyKeyboardMarkup
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            on_text_button,
        )
    )

    # Неизвестные команды
    app.add_handler(
        MessageHandler(
            filters.COMMAND,
            on_unknown,
        )
    )

    return app


async def on_startup(app: Application):
    """
    Старт бота: инициализировать БД, применить миграции.
    """
    await init_db(app)
    log.info("DB initialized")


async def on_shutdown(app: Application):
    """
    Остановка бота: закрыть БД.
    """
    await db_close(app)
    log.info("DB closed")


def main():
    """
    Точка входа.
    """
    app = build_application()
    log.info("Starting bot polling...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
