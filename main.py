import asyncio
import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message
from google import genai

# ============================================================
# JOJO OVERDRIVE — Telegram group bot
# Python 3.10+ | aiogram 3 | Google Gen AI SDK | SQLite
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
DB_PATH = os.getenv("DB_PATH", "jojo_overdrive.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it to environment secrets.")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing. Add it to environment secrets.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("jojo_overdrive")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai = genai.Client(api_key=GEMINI_API_KEY)

# ============================================================
# DATABASE
# ============================================================

def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0,
                warnings INTEGER NOT NULL DEFAULT 0,
                joined_at TEXT NOT NULL,
                last_xp TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                user_id INTEGER PRIMARY KEY,
                messages INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS warnings_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                admin_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_history_user ON ai_history(user_id, id)"
        )


def save_user(user):
    username = (user.username or "").lower() if hasattr(user, "username") else None
    full_name = getattr(user, "full_name", None) or getattr(user, "first_name", None) or str(user.id)
    with connect_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, full_name, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
        """, (
            user.id,
            username,
            full_name,
            datetime.now(timezone.utc).isoformat(),
        ))


def get_user(user_id):
    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def get_user_by_username(username):
    clean = username.lstrip("@").lower()
    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = ?",
            (clean,),
        ).fetchone()


def add_warning(user_id, admin_id, reason):
    with connect_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, full_name, joined_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO NOTHING
        """, (
            user_id,
            str(user_id),
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.execute(
            "UPDATE users SET warnings = warnings + 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.execute("""
            INSERT INTO warnings_log (user_id, admin_id, reason, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            user_id,
            admin_id,
            reason,
            datetime.now(timezone.utc).isoformat(),
        ))


def get_warning_count(user_id):
    user = get_user(user_id)
    return user["warnings"] if user else 0


def clear_warnings(user_id):
    with connect_db() as conn:
        conn.execute(
            "UPDATE users SET warnings = 0 WHERE user_id = ?",
            (user_id,),
        )
        conn.execute(
            "DELETE FROM warnings_log WHERE user_id = ?",
            (user_id,),
        )


def get_warning_history(user_id):
    with connect_db() as conn:
        return conn.execute("""
            SELECT reason, created_at
            FROM warnings_log
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 10
        """, (user_id,)).fetchall()


def increment_messages(user_id):
    with connect_db() as conn:
        conn.execute("""
            INSERT INTO activity (user_id, messages)
            VALUES (?, 1)
            ON CONFLICT(user_id)
            DO UPDATE SET messages = messages + 1
        """, (user_id,))


def message_count(user_id):
    with connect_db() as conn:
        row = conn.execute(
            "SELECT messages FROM activity WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row["messages"] if row else 0


def leaderboard(limit=10):
    with connect_db() as conn:
        return conn.execute("""
            SELECT user_id, full_name, xp
            FROM users
            ORDER BY xp DESC
            LIMIT ?
        """, (limit,)).fetchall()


# ============================================================
# XP AND RANKS
# ============================================================

RANKS = [
    (0, "Новичок"),
    (100, "Ученик"),
    (300, "Боец"),
    (600, "Опытный боец"),
    (1000, "Пользователь стенда"),
    (2000, "Мастер стенда"),
    (3500, "Легенда"),
    (5000, "Повелитель времени"),
]


def rank_info(xp):
    current = RANKS[0][1]
    next_rank = None
    for threshold, title in RANKS:
        if xp >= threshold:
            current = title
        else:
            next_rank = (threshold, title)
            break
    return current, next_rank


def award_xp(user_id):
    now = datetime.now(timezone.utc)
    with connect_db() as conn:
        row = conn.execute(
            "SELECT last_xp FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return 0
        if row["last_xp"]:
            try:
                previous = datetime.fromisoformat(row["last_xp"])
                if now - previous < timedelta(seconds=60):
                    return 0
            except ValueError:
                pass
        amount = random.randint(3, 7)
        conn.execute("""
            UPDATE users
            SET xp = xp + ?, last_xp = ?
            WHERE user_id = ?
        """, (amount, now.isoformat(), user_id))
        return amount


# ============================================================
# USER TRACKING
# ============================================================

@dp.message(F.new_chat_members)
async def welcome_new_members(message: Message):
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        save_user(member)
        await message.answer(
            f"⚡ Добро пожаловать, {member.full_name}!\n"
            "Ты присоединился к JOJO OVERDRIVE."
        )


@dp.message(F.from_user, F.text, ~F.text.startswith("/"))
async def track_messages(message: Message):
    user = message.from_user
    if not user or user.is_bot:
        return
    save_user(user)
    increment_messages(user.id)
    award_xp(user.id)


# ============================================================
# GENERAL COMMANDS
# ============================================================

@dp.message(Command("start"))
async def start_command(message: Message):
    if message.from_user:
        save_user(message.from_user)
    await message.answer(
        "⚡ JOJO OVERDRIVE\n\n"
        "Бот сообщества по JoJo's Bizarre Adventure.\n"
        "Напиши /help, чтобы увидеть список команд."
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "⚡ JOJO OVERDRIVE — КОМАНДЫ\n\n"
        "👤 ПРОФИЛЬ И АКТИВНОСТЬ\n"
        "/profile — твой профиль\n"
        "/profile в ответ на сообщение — профиль участника\n"
        "/rank — твой ранг\n"
        "/top — топ-10 по XP\n\n"
        "🤖 GEMINI\n"
        "/ask вопрос — задать вопрос ИИ\n"
        "/resetai — очистить свою историю ИИ\n\n"
        "🛡 МОДЕРАЦИЯ (для администраторов)\n"
        "/mute 10m — мут на время\n"
        "/unmute — снять мут\n"
        "/ban — заблокировать\n"
        "/unban @username или ID — разбанить\n"
        "/kick — исключить\n"
        "/warn — предупреждение\n"
        "/warnings — посмотреть предупреждения\n"
        "/clearwarns — очистить предупреждения\n\n"
        "Время мута: 30s, 10m, 2h, 1d."
    )


@dp.message(Command("profile"))
async def profile_command(message: Message):
    target = (
        message.reply_to_message.from_user
        if message.reply_to_message
        else message.from_user
    )
    if not target:
        return
    save_user(target)
    user = get_user(target.id)
    xp = user["xp"]
    title, next_rank = rank_info(xp)
    progress = (
        f"Следующий ранг: {next_rank[1]}\n"
        f"До него: {next_rank[0] - xp} XP"
        if next_rank
        else "Достигнут максимальный ранг!"
    )
    await message.answer(
        "👤 ПРОФИЛЬ УЧАСТНИКА\n\n"
        f"Имя: {user['full_name']}\n"
        f"ID: {target.id}\n"
        f"Титул: {title}\n"
        f"Опыт: {xp} XP\n"
        f"Сообщений: {message_count(target.id)}\n"
        f"Предупреждений: {user['warnings']}\n\n"
        f"{progress}"
    )


@dp.message(Command("rank"))
async def rank_command(message: Message):
    if not message.from_user:
        return
    save_user(message.from_user)
    user = get_user(message.from_user.id)
    title, next_rank = rank_info(user["xp"])
    text = f"⚔️ ТВОЙ РАНГ: {title}\nОпыт: {user['xp']} XP"
    if next_rank:
        text += (
            f"\nСледующий ранг: {next_rank[1]}"
            f"\nОсталось: {next_rank[0] - user['xp']} XP"
        )
    await message.answer(text)


@dp.message(Command("top"))
async def top_command(message: Message):
    rows = leaderboard()
    if not rows:
        await message.answer("Пока рейтинг пуст.")
        return
    lines = ["🏆 ТОП-10 JOJO OVERDRIVE", ""]
    for number, row in enumerate(rows, 1):
        title, _ = rank_info(row["xp"])
        lines.append(
            f"{number}. {row['full_name']} — {row['xp']} XP ({title})"
        )
    await message.answer("\n".join(lines))


# ============================================================
# GEMINI AI WITH LOCAL CHAT HISTORY
# ============================================================

def load_ai_history(user_id, limit=10):
    with connect_db() as conn:
        rows = conn.execute("""
            SELECT role, content
            FROM ai_history
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
    return list(reversed(rows))


def save_ai_message(user_id, role, content):
    with connect_db() as conn:
        conn.execute("""
            INSERT INTO ai_history (user_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            user_id,
            role,
            content,
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.execute("""
            DELETE FROM ai_history
            WHERE user_id = ?
            AND id NOT IN (
                SELECT id FROM ai_history
                WHERE user_id = ?
                ORDER BY id DESC LIMIT 20
            )
        """, (user_id, user_id))


@dp.message(Command("ask"))
async def ask_command(message: Message, command: CommandObject):
    question = (command.args or "").strip()
    if not question:
        await message.answer("Напиши вопрос: /ask Кто такой Джотаро?")
        return
    if not message.from_user:
        return

    user_id = message.from_user.id
    history = load_ai_history(user_id)
    prompt_lines = [
        "Ты ИИ-ассистент сообщества JOJO OVERDRIVE.",
        "Общайся естественно, дружелюбно и без канцелярита.",
        "Хорошо разбирайся в JoJo, аниме, манге и других темах.",
        "Не выдумывай факты. Отвечай на языке пользователя.",
        "",
    ]
    for item in history:
        prompt_lines.append(f"{item['role']}: {item['content']}")
    prompt_lines.append(f"user: {question}")
    prompt = "\n".join(prompt_lines)

    await bot.send_chat_action(message.chat.id, "typing")
    try:
        response = await ai.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        answer = (response.text or "").strip()
        if not answer:
            answer = "Gemini не вернул текстовый ответ."
        save_ai_message(user_id, "user", question)
        save_ai_message(user_id, "assistant", answer)
        for start in range(0, len(answer), 4000):
            await message.answer(answer[start:start + 4000])
    except Exception:
        logger.exception("Gemini request failed")
        await message.answer(
            "⚠️ Не удалось получить ответ от Gemini. "
            "Проверь API-ключ и доступность модели."
        )


@dp.message(Command("resetai"))
async def reset_ai_command(message: Message):
    if not message.from_user:
        return
    with connect_db() as conn:
        conn.execute(
            "DELETE FROM ai_history WHERE user_id = ?",
            (message.from_user.id,),
        )
    await message.answer("🧹 История твоего общения с Gemini очищена.")


# ============================================================
# MODERATION HELPERS
# ============================================================

async def is_admin(message: Message):
    if not message.from_user:
        return False
    member = await bot.get_chat_member(
        message.chat.id,
        message.from_user.id,
    )
    return member.status in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    )


async def require_admin(message: Message):
    try:
        allowed = await is_admin(message)
    except Exception:
        logger.exception("Could not check administrator status")
        allowed = False
    if not allowed:
        await message.answer("⛔ Эта команда доступна только администраторам.")
        return False
    return True


async def resolve_target(message: Message, args=None):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user

    parts = (args or "").split()
    if not parts:
        return None

    identifier = parts[0]
    if identifier.startswith("@"):
        row = get_user_by_username(identifier)
        if not row:
            return None
        return SimpleNamespace(
            id=row["user_id"],
            full_name=row["full_name"],
            username=row["username"],
        )

    try:
        user_id = int(identifier)
    except ValueError:
        return None

    row = get_user(user_id)
    return SimpleNamespace(
        id=user_id,
        full_name=row["full_name"] if row else str(user_id),
        username=row["username"] if row else None,
    )


def parse_duration(args):
    match = re.search(r"(?<!\w)(\d+)\s*(s|m|h|d)\b", (args or "").lower())
    if not match:
        return None
    value = int(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = value * multiplier[match.group(2)]
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


# ============================================================
# MUTE / UNMUTE
# ============================================================

@dp.message(Command("mute"))
async def mute_command(message: Message, command: CommandObject):
    if not await require_admin(message):
        return
    target = await resolve_target(message, command.args)
    seconds = parse_duration(command.args)

    if not target:
        await message.answer(
            "Не нашёл пользователя. Ответь на его сообщение "
            "или используй сохранённый @username / ID."
        )
        return
    if not seconds:
        await message.answer(
            "Укажи длительность мута:\n"
            "/mute 30s\n/mute 10m\n/mute 2h\n/mute 1d\n"
            "Минимум — 30 секунд."
        )
        return

    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await message.answer(
            f"🔇 {target.full_name} получил мут на "
            f"{format_duration(seconds)}."
        )
    except Exception:
        logger.exception("Mute failed")
        await message.answer(
            "❌ Не удалось выдать мут. Проверь права бота "
            "и убедись, что цель не является администратором."
        )


@dp.message(Command("unmute"))
async def unmute_command(message: Message, command: CommandObject):
    if not await require_admin(message):
        return
    target = await resolve_target(message, command.args)
    if not target:
        await message.answer("Укажи пользователя через ответ, @username или ID.")
        return
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await message.answer(f"🔊 Ограничение для {target.full_name} снято.")
    except Exception:
        logger.exception("Unmute failed")
        await message.answer("❌ Не удалось снять мут.")


# ============================================================
# BAN / UNBAN / KICK
# ============================================================

@dp.message(Command("ban"))
async def ban_command(message: Message, command: CommandObject):
    if not await require_admin(message):
        return
    target = await resolve_target(message, command.args)
    if not target:
        await message.answer(
            "Не нашёл пользователя. Ответь на сообщение "
            "или используй сохранённый @username / ID."
        )
        return
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await message.answer(f"🔨 {target.full_name} заблокирован.")
    except Exception:
        logger.exception("Ban failed")
        await message.answer("❌ Не удалось заблокировать участника.")


@dp.message(Command("unban"))
async def unban_command(message: Message, command: CommandObject):
    if not await require_admin(message):
        return
    target = await resolve_target(message, command.args)
    if not target:
        await message.answer(
            "❌ Пользователь не найден в базе.\n"
            "Сначала бот должен был увидеть его в группе, "
            "чтобы сохранить username и ID."
        )
        return
    try:
        await bot.unban_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            only_if_banned=True,
        )
        await message.answer(f"🔓 {target.full_name} разблокирован.")
    except Exception:
        logger.exception("Unban failed")
        await message.answer("❌ Не удалось выполнить разбан.")


@dp.message(Command("kick"))
async def kick_command(message: Message, command: CommandObject):
    if not await require_admin(message):
        return
    target = await resolve_target(message, command.args)
    if not target:
        await message.answer("Укажи пользователя через ответ, @username или ID.")
        return
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(
            message.chat.id,
            target.id,
            only_if_banned=True,
        )
        await message.answer(f"👢 {target.full_name} исключён из группы.")
    except Exception:
        logger.exception("Kick failed")
        await message.answer("❌ Не удалось исключить участника.")


# ============================================================
# WARNINGS
# ============================================================

@dp.message(Command("warn"))
async def warn_command(message: Message, command: CommandObject):
    if not await require_admin(message):
        return
    target = await resolve_target(message, command.args)
    if not target:
        await message.answer("Укажи пользователя через ответ, @username или ID.")
        return

    parts = (command.args or "").split()
    reason = " ".join(parts[1:]) if parts else ""
    reason = reason or "Причина не указана"

    add_warning(target.id, message.from_user.id, reason)
    count = get_warning_count(target.id)
    await message.answer(
        f"⚠️ Предупреждение выдано.\n"
        f"Участник: {target.full_name}\n"
        f"Причина: {reason}\n"
        f"Всего предупреждений: {count}"
    )


@dp.message(Command("warnings"))
async def warnings_command(message: Message, command: CommandObject):
    target = await resolve_target(message, command.args)
    if not target:
        target = message.from_user
    if not target:
        return

    count = get_warning_count(target.id)
    records = get_warning_history(target.id)
    lines = [
        "⚠️ ПРЕДУПРЕЖДЕНИЯ",
        f"Участник: {target.full_name}",
        f"Всего: {count}",
        "",
    ]
    if records:
        for index, record in enumerate(records, 1):
            lines.append(f"{index}. {record['reason']}")
    else:
        lines.append("Предупреждений нет.")
    await message.answer("\n".join(lines))


@dp.message(Command("clearwarns"))
async def clearwarns_command(message: Message, command: CommandObject):
    if not await require_admin(message):
        return
    target = await resolve_target(message, command.args)
    if not target:
        await message.answer("Укажи пользователя через ответ, @username или ID.")
        return
    clear_warnings(target.id)
    await message.answer(
        f"✅ Предупреждения пользователя {target.full_name} очищены."
    )


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def main():
    init_db()
    logger.info("JOJO OVERDRIVE is starting")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await ai.aio.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("JOJO OVERDRIVE stopped")
