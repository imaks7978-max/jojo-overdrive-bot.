import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from groq import AsyncGroq

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.types import Message, ChatPermissions
from aiogram.filters import CommandStart

# ==========================================
# 1. НАСТРОЙКИ
# ==========================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "openai/gpt-oss-120b"
)

DB_PATH = "bot.db"

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в .env")

if not GROQ_API_KEY:
    raise RuntimeError("Не найден GROQ_API_KEY в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

groq = AsyncGroq(
    api_key=GROQ_API_KEY,
    timeout=45.0,
    max_retries=2
)

BOT_USERNAME = None

# ==========================================
# 2. БАЗА ДАННЫХ
# ==========================================

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False
)

db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")

db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT,
    nickname TEXT,
    messages INTEGER NOT NULL DEFAULT 0,
    xp INTEGER NOT NULL DEFAULT 0,
    warns INTEGER NOT NULL DEFAULT 0,
    joined_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS moderation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_username
ON users(chat_id, username);

CREATE INDEX IF NOT EXISTS idx_ai_history
ON ai_history(chat_id, user_id, id);

CREATE INDEX IF NOT EXISTS idx_moderation
ON moderation(chat_id, user_id, action);
""")

# Миграция старой базы.
columns = {
    row["name"]
    for row in db.execute("PRAGMA table_info(users)")
}

if "nickname" not in columns:
    db.execute(
        "ALTER TABLE users ADD COLUMN nickname TEXT"
    )

if "custom_nick" in columns:
    db.execute("""
    UPDATE users
    SET nickname=custom_nick
    WHERE nickname IS NULL
    """)

db.commit()


def execute(query, params=()):
    result = db.execute(query, params)
    db.commit()
    return result


def now():
    return datetime.now(timezone.utc)


def timestamp():
    return now().isoformat()


# ==========================================
# 3. СИСТЕМА РАНГОВ JOJO
# ==========================================

RANKS = [
    (0, "Новичок/Вампир"),
    (25, "Ученик Хамона"),
    (50, "Учитель Хамона"),
    (75, "Пиллармен"),
    (150, "Обладатель злого духа"),
    (300, "Обладатель станда"),
    (600, "Подсос Дио"),
    (1000, "Стардаст"),
    (1500, "Житель Морио"),
    (2000, "Угроза, скрывающаяся в Морио"),
    (2500, "Защитник Морио"),
    (3000, "Новичок Passione"),
    (3500, "Капо Passione"),
    (4000, "Убийца La Squadra"),
    (4500, "Дон Passione"),
    (5000, "Заключенный G.D. st"),
    (5500, "Священник G.D. st"),
    (6000, "Сбежавший из G.D. st"),
    (6500, "Достигший Рая"),
    (7000, "Участник гонки Steel Ball Run"),
    (7500, "Обладатель стальных шаров"),
    (8000, "Ученик Спина"),
    (8500, "Учитель Спина"),
    (9000, "Динозавр"),
    (9500, "Обладатель части трупа Иисуса"),
    (10000, "Выучивший/понявший все уроки Джайро")
]


def get_rank(xp):
    result = RANKS[0]

    for required, name in RANKS:
        if xp >= required:
            result = (required, name)
        else:
            break

    return result


def get_next_rank(xp):
    for required, name in RANKS:
        if xp < required:
            return required, name

    return None


# ==========================================
# 4. УЧЁТ УЧАСТНИКОВ
# ==========================================

def ensure_user(chat_id, user):
    if not user or user.is_bot:
        return

    execute("""
    INSERT INTO users (
        chat_id, user_id, username,
        first_name, joined_at, last_seen
    )
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, user_id)
    DO UPDATE SET
        username=excluded.username,
        first_name=excluded.first_name,
        last_seen=excluded.last_seen
    """, (
        chat_id,
        user.id,
        user.username,
        user.first_name or "Без имени",
        timestamp(),
        timestamp()
    ))


def add_message(chat_id, user):
    ensure_user(chat_id, user)

    execute("""
    UPDATE users
    SET messages=messages+1,
        xp=xp+1,
        last_seen=?
    WHERE chat_id=? AND user_id=?
    """, (
        timestamp(),
        chat_id,
        user.id
    ))


def get_user(chat_id, user_id):
    return db.execute("""
    SELECT * FROM users
    WHERE chat_id=? AND user_id=?
    """, (chat_id, user_id)).fetchone()


def find_username(chat_id, username):
    username = username.lstrip("@").lower()

    return db.execute("""
    SELECT * FROM users
    WHERE chat_id=?
    AND LOWER(username)=?
    LIMIT 1
    """, (chat_id, username)).fetchone()


def display_name(user):
    if not user:
        return "Неизвестный участник"

    return (
        user["nickname"]
        or user["first_name"]
        or user["username"]
        or str(user["user_id"])
    )


# ==========================================
# 5. ПРОФИЛЬ
# ==========================================

def make_profile(chat_id, user_id):
    user = get_user(chat_id, user_id)

    if not user:
        return "У участника пока нет статистики."

    xp = user["xp"]
    rank_xp, rank_name = get_rank(xp)
    upcoming = get_next_rank(xp)

    if upcoming:
        progress = (
            f"Следующий ранг: {upcoming[1]}\n"
            f"Осталось XP: {upcoming[0] - xp}"
        )
    else:
        progress = "Достигнут максимальный ранг!"

    username = (
        "@" + user["username"]
        if user["username"]
        else "Не указан"
    )

    return (
        "╔════════════════════╗\n"
        "          👤 ПРОФИЛЬ\n"
        "╚════════════════════╝\n\n"
        f"Имя: {display_name(user)}\n"
        f"Telegram: {username}\n"
        f"ID: {user_id}\n\n"
        f"💬 Сообщений: {user['messages']}\n"
        f"⭐ Опыт: {xp} XP\n"
        f"🏆 Ранг: {rank_name}\n"
        f"📊 {progress}\n"
        f"⚠️ Предупреждения: {user['warns']}/3"
    )


# ==========================================
# 6. КОМАНДЫ И ПСЕВДОНИМЫ
# ==========================================

ALIASES = {
    "проф": "profile",
    "профиль": "profile",
    "помощь": "help",
    "хелп": "help",
    "мут": "mute",
    "размут": "unmute",
    "бан": "ban",
    "разбан": "unban",
    "пред": "warn",
    "предупреждение": "warn",
    "предлист": "warnlist",
    "снятьпред": "unwarn",
    "кик": "kick",
    "топ": "top",
    "ранг": "rank",
    "спросить": "ask"
}

COMMANDS = {
    "help", "profile", "mute", "unmute",
    "ban", "unban", "warn", "warnlist",
    "unwarn", "kick", "top", "rank", "ask"
}

HELP_TEXT = """
╔══════════════════════╗
           📚 ПОМОЩЬ
╚══════════════════════╝

👤 ПРОФИЛЬ И СТАТИСТИКА

/profile — профиль
проф / +проф — профиль
/top / топ / +топ — рейтинг
/rank / ранг / +ранг — ранг
+ник Имя — сменить ник

🤖 GROQ AI

/ask вопрос — задать вопрос
@имя_бота вопрос — обратиться к ИИ

🔨 МОДЕРАЦИЯ

МУТ:
/mute — 1 час
/mute 30m — 30 минут
/mute 2h — 2 часа
мут / +мут — 1 час
размут / +размут — снять мут

БАН:
/ban — бан
/ban @username — бан
/unban @username — разбан
бан / +бан — бан
разбан / +разбан — разбан

ПРЕДУПРЕЖДЕНИЯ:
/warn — выдать пред
/warnlist — список предов
/unwarn — снять один пред
пред / +пред — выдать пред
предлист / +предлист — список
снятьпред / +снятьпред — снять пред

КИК:
/kick — исключить участника
кик / +кик — исключить

⚠️ 3 предупреждения = автоматический бан.
При успешном разбане предупреждения обнуляются.

Команды модерации можно использовать
ответом на сообщение или через @username.
"""

def parse_command(text):
    if not text:
        return None, ""

    parts = text.strip().split(maxsplit=1)

    if not parts:
        return None, ""

    token = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    if token.startswith("/"):
        raw = token[1:].split("@")[0].lower()
        command = ALIASES.get(raw, raw)
        return command, args

    if token.startswith("+"):
        raw = token[1:].lower()
        command = ALIASES.get(raw, raw)
        return command, args

    command = ALIASES.get(token.lower())

    return command, args


# ==========================================
# 7. ОПРЕДЕЛЕНИЕ ЦЕЛИ НАКАЗАНИЯ
# ==========================================

async def get_target(message, args):
    chat_id = message.chat.id

    # Вариант 1: ответ на сообщение.
    if message.reply_to_message:
        target = message.reply_to_message.from_user

        if target and not target.is_bot:
            ensure_user(chat_id, target)
            return target.id, args

    # Вариант 2: скрытое Telegram-упоминание.
    entities = message.entities or []

    for entity in entities:
        if entity.type == "text_mention" and entity.user:
            target = entity.user

            if not target.is_bot:
                ensure_user(chat_id, target)
                return target.id, args

    # Вариант 3: @username.
    match = re.search(
        r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{5,32})",
        args
    )

    if match:
        username = match.group(1)
        row = find_username(chat_id, username)

        if not row:
            return None, args

        remaining = (
            args[:match.start()] +
            " " +
            args[match.end():]
        ).strip()

        return row["user_id"], remaining

    # Вариант 4: числовой ID.
    parts = args.split(maxsplit=1)

    if parts and parts[0].isdigit():
        target_id = int(parts[0])
        remaining = parts[1] if len(parts) > 1 else ""

        if get_user(chat_id, target_id):
            return target_id, remaining

    return None, args


# ==========================================
# 8. ПРОВЕРКА ПРАВ
# ==========================================

async def is_admin(message):
    if not message.from_user:
        return False

    try:
        member = await bot.get_chat_member(
            message.chat.id,
            message.from_user.id
        )

        return member.status in (
            "administrator",
            "creator"
        )

    except Exception:
        logging.exception("Ошибка проверки прав администратора")
        return False


async def protected_target(chat_id, target_id):
    try:
        member = await bot.get_chat_member(
            chat_id,
            target_id
        )

        if member.status in ("administrator", "creator"):
            return True

    except Exception:
        return False

    return False


# ==========================================
# 9. ВРЕМЯ МУТА
# ==========================================

def parse_duration(value):
    match = re.fullmatch(
        r"(\d+)(s|m|h|d|w|с|м|ч|д|н)",
        value.lower().strip()
    )

    if not match:
        return None

    number = int(match.group(1))
    unit = match.group(2)

    multipliers = {
        "s": 1, "с": 1,
        "m": 60, "м": 60,
        "h": 3600, "ч": 3600,
        "d": 86400, "д": 86400,
        "w": 604800, "н": 604800
    }

    seconds = number * multipliers[unit]

    if seconds < 30 or seconds > 366 * 86400:
        return None

    return seconds


def format_duration(seconds):
    if seconds % 86400 == 0:
        return f"{seconds // 86400} дн."

    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч."

    if seconds % 60 == 0:
        return f"{seconds // 60} мин."

    return f"{seconds} сек."


# ==========================================
# 10. ЖУРНАЛ НАКАЗАНИЙ
# ==========================================

def log_action(
    chat_id,
    user_id,
    admin_id,
    action,
    reason=""
):
    execute("""
    INSERT INTO moderation (
        chat_id, user_id, admin_id,
        action, reason, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        chat_id,
        user_id,
        admin_id,
        action,
        reason,
        timestamp()
    ))


# ==========================================
# 11. ПРАВА МУТА
# ==========================================

MUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False
)

UNMUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True
)


# ==========================================
# 12. МОДЕРАЦИЯ
# ==========================================

async def moderation(message, command, args):
    if not await is_admin(message):
        await message.reply(
            "⛔ Команда доступна только администраторам."
        )
        return

    chat_id = message.chat.id
    admin_id = message.from_user.id

    target_id, remaining = await get_target(
        message,
        args
    )

    if not target_id:
        await message.reply(
            "❗ Не удалось определить участника.\n\n"
            "Используй ответ на сообщение или @username.\n"
            "Участник должен быть известен боту в этой группе.\n"
            "Также можно указать сохранённый ID."
        )
        return

    if target_id == admin_id:
        await message.reply(
            "Нельзя применить наказание к самому себе."
        )
        return

    # Разбан разрешён и для отсутствующих участников.
    if command != "unban":
        if await protected_target(chat_id, target_id):
            await message.reply(
                "⛔ Нельзя применять наказания к администраторам."
            )
            return

    user = get_user(chat_id, target_id)
    name = display_name(user) if user else str(target_id)

    # ------------------------------
    # МУТ
    # ------------------------------

    if command == "mute":
        duration = 3600
        reason = ""

        if remaining:
            parts = remaining.split(maxsplit=1)
            parsed = parse_duration(parts[0])

            if parsed is None:
                await message.reply(
                    "Неверный формат времени.\n"
                    "Примеры: 30m, 2h, 1d, 1w."
                )
                return

            duration = parsed
            reason = parts[1] if len(parts) > 1 else ""

        try:
            await bot.restrict_chat_member(
                chat_id,
                target_id,
                permissions=MUTE_PERMISSIONS,
                until_date=now() + timedelta(seconds=duration)
            )

            log_action(
                chat_id,
                target_id,
                admin_id,
                "mute",
                reason
            )

            await message.reply(
                f"🔇 {name} получил мут.\n"
                f"Время: {format_duration(duration)}\n"
                f"Причина: {reason or 'Не указана'}"
            )

        except Exception as error:
            logging.exception("Ошибка выдачи мута")
            await message.reply(
                f"Не удалось выдать мут: {error}"
            )

    # ------------------------------
    # РАЗМУТ
    # ------------------------------

    elif command == "unmute":
        try:
            await bot.restrict_chat_member(
                chat_id,
                target_id,
                permissions=UNMUTE_PERMISSIONS
            )

            log_action(
                chat_id,
                target_id,
                admin_id,
                "unmute"
            )

            await message.reply(
                f"🔊 Мут с {name} снят."
            )

        except Exception as error:
            logging.exception("Ошибка размута")
            await message.reply(
                f"Не удалось снять мут: {error}"
            )

    # ------------------------------
    # БАН
    # ------------------------------

    elif command == "ban":
        reason = remaining or "Причина не указана"

        try:
            await bot.ban_chat_member(
                chat_id,
                target_id
            )

            log_action(
                chat_id,
                target_id,
                admin_id,
                "ban",
                reason
            )

            await message.reply(
                f"🔨 {name} забанен.\n"
                f"Причина: {reason}"
            )

        except Exception as error:
            logging.exception("Ошибка бана")
            await message.reply(
                f"Не удалось выдать бан: {error}"
            )

    # ------------------------------
    # РАЗБАН + ОБНУЛЕНИЕ ПРЕДОВ
    # ------------------------------

    elif command == "unban":
        try:
            result = await bot.unban_chat_member(
                chat_id,
                target_id,
                only_if_banned=True
            )

            if result:
                # Сбрасываем активные предупреждения.
                execute("""
                UPDATE users
                SET warns=0
                WHERE chat_id=? AND user_id=?
                """, (
                    chat_id,
                    target_id
                ))

                # Сохраняем старую историю,
                # но помечаем предупреждения сброшенными.
                execute("""
                UPDATE moderation
                SET action='warn_reset'
                WHERE chat_id=?
                AND user_id=?
                AND action='warn'
                """, (
                    chat_id,
                    target_id
                ))

                log_action(
                    chat_id,
                    target_id,
                    admin_id,
                    "unban"
                )

                log_action(
                    chat_id,
                    target_id,
                    admin_id,
                    "warns_reset",
                    "Предупреждения обнулены при разбане"
                )

                await message.reply(
                    f"✅ {name} разбанен!\n\n"
                    f"⚠️ Предупреждения обнулены: 0/3.\n"
                    f"История старых наказаний сохранена."
                )

            else:
                await message.reply(
                    "Telegram не подтвердил разбан."
                )

        except Exception as error:
            logging.exception("Ошибка разбана")
            await message.reply(
                f"Не удалось выполнить разбан: {error}"
            )

    # ------------------------------
    # КИК
    # ------------------------------

    elif command == "kick":
        try:
            await bot.ban_chat_member(
                chat_id,
                target_id
            )

            await bot.unban_chat_member(
                chat_id,
                target_id,
                only_if_banned=True
            )

            log_action(
                chat_id,
                target_id,
  
