import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.types import Message, ChatPermissions
from aiogram.filters import CommandStart
from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
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
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA foreign_keys=ON")

conn.executescript("""
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT,
    custom_nick TEXT,
    messages INTEGER NOT NULL DEFAULT 0,
    xp INTEGER NOT NULL DEFAULT 0,
    warns INTEGER NOT NULL DEFAULT 0,
    joined_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS ai_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS moderation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_history
ON ai_history(chat_id, user_id, id);

CREATE INDEX IF NOT EXISTS idx_moderation
ON moderation(chat_id, user_id);
""")
conn.commit()


def db_execute(query, params=()):
    cur = conn.execute(query, params)
    conn.commit()
    return cur


def now():
    return datetime.now(timezone.utc)


def now_text():
    return now().isoformat()


def ensure_user(chat_id, user):
    if not user or user.is_bot:
        return

    timestamp = now_text()

    db_execute("""
    INSERT INTO users (
        chat_id, user_id, username, first_name,
        joined_at, last_seen
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
        timestamp,
        timestamp
    ))


def add_message(chat_id, user):
    ensure_user(chat_id, user)

    db_execute("""
    UPDATE users
    SET messages = messages + 1,
        xp = xp + 1,
        last_seen = ?
    WHERE chat_id = ? AND user_id = ?
    """, (now_text(), chat_id, user.id))


def get_user(chat_id, user_id):
    return conn.execute("""
    SELECT * FROM users
    WHERE chat_id = ? AND user_id = ?
    """, (chat_id, user_id)).fetchone()


def display_name(row):
    if not row:
        return "Неизвестный участник"

    return (
        row["custom_nick"]
        or row["first_name"]
        or row["username"]
        or str(row["user_id"])
    )


def find_user(chat_id, identifier):
    identifier = identifier.strip()

    if identifier.isdigit():
        return get_user(chat_id, int(identifier))

    username = identifier.lstrip("@").lower()

    return conn.execute("""
    SELECT * FROM users
    WHERE chat_id = ? AND LOWER(username) = ?
    LIMIT 1
    """, (chat_id, username)).fetchone()


def get_level(xp):
    return max(1, int((xp / 100) ** 0.5) + 1)


def get_rank(xp):
    if xp >= 10000:
        return "Выучивший/понявший все уроки Джайро"
    if xp >= 9500:
        return "Обладатель части трупа Иисуса"
    if xp >= 9000:
        return "Динозавр"
    if xp >= 8500:
        return "Учитель Спина"
    if xp >= 8000:
        return "Ученик Спина"
    if xp >= 7500:
        return "Обладатель стальных шаров"
    if xp >= 7000:
        return "Участник гонки Steel Ball Run"
    if xp >= 6500:
        return "Достигший Рая"
    if xp >= 6000:
        return "Сбежавший из G.D. st"
    if xp >= 5500:
        return "Священник G.D. st"
    if xp >= 5000:
        return "Заключённый G.D. st"
    if xp >= 4500:
        return "Дон Passione"
    if xp >= 4000:
        return "Убийца La Squadra"
    if xp >= 3500:
        return "Капо Passione"
    if xp >= 3000:
        return "Новичок Passione"
    if xp >= 2500:
        return "Защитник Морио"
    if xp >= 2000:
        return "Угроза, скрывающаяся в Морио"
    if xp >= 1500:
        return "Житель Морио"
    if xp >= 1000:
        return "Стардаст"
    if xp >= 600:
        return "Подсос ДИО"
    if xp >= 300:
        return "Обладатель стенда"
    if xp >= 150:
        return "Обладатель злого духа"
    if xp >= 75:
        return "Пиллармен"
    if xp >= 50:
        return "Учитель хамона"
    if xp >= 25:
        return "Ученик хамона"
    if xp >= 0:
        return "Новичок/вампир"
    return "Новичок/вампир"


def profile_text(chat_id, user_id):
    row = get_user(chat_id, user_id)

    if not row:
        return "Профиль пока не создан. Напиши несколько сообщений!"

    xp = row["xp"]
    level = get_level(xp)
    next_level_xp = (level ** 2) * 100

    username = (
        "@" + row["username"]
        if row["username"]
        else "Не указан"
    )

    return (
        f"👤 ПРОФИЛЬ УЧАСТНИКА\n\n"
        f"Имя: {display_name(row)}\n"
        f"Telegram: {username}\n"
        f"ID: {user_id}\n\n"
        f"💬 Сообщений: {row['messages']}\n"
        f"⭐ Опыт: {xp} XP\n"
        f"🏅 Уровень: {level}\n"
        f"🎖 Ранг: {get_rank(xp)}\n"
        f"📈 До следующего уровня: "
        f"{max(0, next_level_xp - xp)} XP\n"
        f"⚠️ Предупреждений: {row['warns']}/3"
    )


async def is_admin(message):
    if not message.from_user:
        return False

    try:
        member = await bot.get_chat_member(
            message.chat.id,
            message.from_user.id
        )
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def get_target(message, args):
    if message.reply_to_message:
        target = message.reply_to_message.from_user

        if target:
            ensure_user(message.chat.id, target)
            return target.id, args

    if not args:
        return None, ""

    parts = args.split(maxsplit=1)
    identifier = parts[0]

    if not identifier.startswith("@") and not identifier.isdigit():
        return None, args

    row = find_user(message.chat.id, identifier)

    if not row:
        return None, args

    remaining = parts[1] if len(parts) > 1 else ""
    return row["user_id"], remaining


def parse_duration(value):
    value = value.lower().strip()

    match = re.fullmatch(
        r"(\d+)\s*(s|sec|m|min|h|hr|d|day|w|н|ч|м|д)",
        value
    )

    if not match:
        return None

    number = int(match.group(1))
    unit = match.group(2)

    if number <= 0:
        return None

    multipliers = {
        "s": 1,
        "sec": 1,
        "m": 60,
        "min": 60,
        "м": 60,
        "h": 3600,
        "hr": 3600,
        "ч": 3600,
        "d": 86400,
        "day": 86400,
        "д": 86400,
        "w": 604800,
        "н": 604800
    }

    seconds = number * multipliers[unit]

    if seconds < 30 or seconds > 366 * 86400:
        return None

    return seconds


def duration_text(seconds):
    if seconds % 86400 == 0:
        return f"{seconds // 86400} дн."
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч."
    if seconds % 60 == 0:
        return f"{seconds // 60} мин."
    return f"{seconds} сек."


def log_action(chat_id, user_id, admin_id, action, reason=""):
    db_execute("""
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
        now_text()
    ))


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


ALIASES = {
    "проф": "profile",
    "профиль": "profile",
    "мут": "mute",
    "размут": "unmute",
    "бан": "ban",
    "разбан": "unban",
    "пред": "warn",
    "предупреждение": "warn",
    "предлист": "warnlist",
    "кик": "kick",
    "топ": "top",
    "ранг": "rank"
}

COMMANDS = {
    "profile", "mute", "unmute", "ban",
    "unban", "warn", "warnlist", "kick",
    "top", "rank"
}


async def moderation_command(message, command, args):
    if not await is_admin(message):
        await message.reply("⛔ Эта команда доступна только администраторам.")
        return

    chat_id = message.chat.id
    admin_id = message.from_user.id

    target_id, remaining = await get_target(message, args)

    if command == "top":
        rows = conn.execute("""
        SELECT * FROM users
        WHERE chat_id = ?
        ORDER BY xp DESC, messages DESC
        LIMIT 10
        """, (chat_id,)).fetchall()

        if not rows:
            await message.reply("Пока никто не набрал статистику.")
            return

        result = ["🏆 ТОП УЧАСТНИКОВ\n"]

        for index, row in enumerate(rows, 1):
            result.append(
                f"{index}. {display_name(row)} — "
                f"{row['xp']} XP | {row['messages']} сообщений"
            )

        await message.reply("\n".join(result))
        return

    if command == "profile":
        target_id = message.from_user.id
        await message.reply(profile_text(chat_id, target_id))
        return

    if command == "rank":
        target_id = target_id or message.from_user.id
        row = get_user(chat_id, target_id)

        if not row:
            await message.reply("Участник ещё не имеет статистики.")
            return

        await message.reply(
            f"🏅 Ранг: {get_rank(row['xp'])}\n"
            f"Уровень: {get_level(row['xp'])}\n"
            f"Опыт: {row['xp']} XP"
        )
        return

    if not target_id:
        await message.reply(
            "❗ Ответь на сообщение участника "
            "или укажи его @username / ID."
        )
        return

    row = get_user(chat_id, target_id)
    name = display_name(row) if row else str(target_id)

    if command == "mute":
        duration = 3600
        reason = ""

        if remaining:
            parts = remaining.split(maxsplit=1)
            parsed = parse_duration(parts[0])

            if parsed is not None:
                duration = parsed
                reason = parts[1] if len(parts) > 1 else ""
            else:
                await message.reply(
                    "❗ Неверное время. Примеры: 30m, 2h, 1d."
                )
                return

        until = now() + timedelta(seconds=duration)

        try:
            await bot.restrict_chat_member(
                chat_id,
                target_id,
                permissions=MUTE_PERMISSIONS,
                until_date=until
            )

            log_action(
                chat_id, target_id, admin_id,
                "mute", reason
            )

            await message.reply(
                f"🔇 {name} получил мут на "
                f"{duration_text(duration)}."
            )
        except Exception as error:
            await message.reply(f"Не удалось выдать мут: {error}")

    elif command == "unmute":
        try:
            await bot.restrict_chat_member(
                chat_id,
                target_id,
                permissions=UNMUTE_PERMISSIONS
            )

            log_action(chat_id, target_id, admin_id, "unmute")
            await message.reply(f"🔊 {name} больше не в муте.")
        except Exception as error:
            await message.reply(f"Не удалось снять мут: {error}")

    elif command == "ban":
        reason = remaining or "Причина не указана"

        try:
            await bot.ban_chat_member(chat_id, target_id)

            log_action(
                chat_id, target_id, admin_id,
                "ban", reason
            )

            await message.reply(
                f"🔨 {name} забанен.\nПричина: {reason}"
            )
        except Exception as error:
            await message.reply(f"Не удалось выдать бан: {error}")

    elif command == "unban":
        try:
            await bot.unban_chat_member(
                chat_id,
                target_id,
                only_if_banned=True
            )

            log_action(chat_id, target_id, admin_id, "unban")
            await message.reply(f"✅ {name} разбанен.")
        except Exception as error:
            await message.reply(f"Не удалось снять бан: {error}")

    elif command == "kick":
        try:
            await bot.ban_chat_member(chat_id, target_id)
            await bot.unban_chat_member(
                chat_id,
                target_id,
                only_if_banned=True
            )

            log_action(chat_id, target_id, admin_id, "kick")
            await message.reply(f"👢 {name} исключён из чата.")
        except Exception as error:
            await message.reply(f"Не удалось исключить участника: {error}")

    elif command == "warn":
        reason = remaining or "Причина не указана"

        ensure_target = conn.execute("""
        SELECT user_id FROM users
        WHERE chat_id = ? AND user_id = ?
        """, (chat_id, target_id)).fetchone()

        if not ensure_target:
            await message.reply(
                "Сначала участник должен написать хотя бы одно сообщение."
            )
            return

        db_execute("""
        UPDATE users
        SET warns = warns + 1
        WHERE chat_id = ? AND user_id = ?
        """, (chat_id, target_id))

        row = get_user(chat_id, target_id)
        warns = row["warns"]

        log_action(
            chat_id, target_id, admin_id,
            "warn", reason
        )

        if warns >= 3:
            try:
                await bot.ban_chat_member(chat_id, target_id)

                log_action(
                    chat_id, target_id, admin_id,
                    "ban", "Автоматический бан за 3 предупреждения"
                )

                await message.reply(
                    f"🚫 {name} получил третье предупреждение "
                    f"и автоматически забанен.\n"
                    f"Причина последнего предупреждения: {reason}"
                )
            except Exception as error:
                await message.reply(
                    f"Получено 3/3 предупреждений, "
                    f"но бан не удался: {error}"
                )
        else:
            await message.reply(
                f"⚠️ Предупреждение для {name}.\n"
                f"Количество: {warns}/3\n"
                f"Причина: {reason}"
            )

    elif command == "warnlist":
        warnings = conn.execute("""
        SELECT * FROM moderation
        WHERE chat_id = ? AND user_id = ?
        AND action = 'warn'
        ORDER BY id DESC
        LIMIT 20
        """, (chat_id, target_id)).fetchall()

        if not warnings:
            await message.reply("У участника нет предупреждений.")
            return

        result = [f"⚠️ ПРЕДУПРЕЖДЕНИЯ: {name}\n"]

        for index, item in enumerate(warnings, 1):
            result.append(
                f"{index}. {item['reason'] or 'Без причины'}"
            )

        await message.reply("\n".join(result))


async def set_nickname(message):
    if not message.from_user:
        return

    nickname = message.text[len("+ник"):].strip()

    if not nickname:
        await message.reply(
            "Использование: +ник"
        )
        return

    if len(nickname) > 32:
        await message.reply("Ник не может быть длиннее 32 символов.")
        return

    ensure_user(message.chat.id, message.from_user)

    db_execute("""
    UPDATE users
    SET custom_nick = ?
    WHERE chat_id = ? AND user_id = ?
    """, (
        nickname,
        message.chat.id,
        message.from_user.id
    ))

    await message.reply(
        f"✨ Твой новый ник: {nickname}"
    )


def save_ai_message(chat_id, user_id, role, content):
    db_execute("""
    INSERT INTO ai_history (
        chat_id, user_id, role, content, created_at
    )
    VALUES (?, ?, ?, ?, ?)
    """, (
        chat_id,
        user_id,
        role,
        content[:6000],
        now_text()
    ))

    db_execute("""
    DELETE FROM ai_history
    WHERE chat_id = ? AND user_id = ?
    AND id NOT IN (
        SELECT id FROM ai_history
        WHERE chat_id = ? AND user_id = ?
        ORDER BY id DESC LIMIT 16
    )
    """, (chat_id, user_id, chat_id, user_id))


async def ask_groq(message, bot_username):
    if not message.text or not bot_username:
        return

    if not re.search(
        rf"@{re.escape(bot_username)}\b",
        message.text,
        re.IGNORECASE
    ):
        return

    question = re.sub(
        rf"@{re.escape(bot_username)}\b",
        "",
        message.text,
        flags=re.IGNORECASE
    ).strip()

    if not question:
        await message.reply("Что хотел спросить?")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    save_ai_message(chat_id, user_id, "user", question)

    history = conn.execute("""
    SELECT role, content FROM ai_history
    WHERE chat_id = ? AND user_id = ?
    ORDER BY id DESC LIMIT 12
    """, (chat_id, user_id)).fetchall()

    history = list(reversed(history))

    messages = [
        {
            "role": "system",
            "content": (
                "Ты — участник русскоязычного Telegram-сообщества. "
                "Общайся естественно, живо и неформально. "
                "Отвечай на русском языке, если собеседник "
                "не попросил другой язык. Не представляйся "
                "человеком. Не выдумывай факты. "
                "Не повторяй вопрос без необходимости."
            )
        }
    ]

    messages.extend([
        {
            "role": item["role"],
            "content": item["content"]
        }
        for item in history
    ])

    try:
        completion = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.8,
            max_tokens=900
        )

        answer = completion.choices[0].message.content

        if not answer:
            answer = "Не получилось сформировать ответ."

        save_ai_message(chat_id, user_id, "assistant", answer)

        await message.reply(
            answer[:4000],
            disable_web_page_preview=True
        )

    except Exception:
        logging.exception("Ошибка Groq API")
        await message.reply(
            "⚠️ Сейчас не получилось обратиться к нейросети."
        )


@router.message(CommandStart())
async def start_command(message: Message):
    await message.answer(
        "Привет! Я бот сообщества JoJo Overdrive.\n\n"
        "📊 /profile — твой профиль\n"
        "🏆 /top — рейтинг участников\n"
        "🎖 /rank — твой ранг\n\n"
        "Для смены ника напиши: +ник НовыйНик\n"
        "Чтобы обратиться к ИИ, упомяни меня через @username."
    )


@router.message()
async def all_messages(message: Message):
    if not message.from_user:
        return

    if message.from_user.is_bot:
        return

    chat_id = message.chat.id
    text = (message.text or message.caption or "").strip()

    # Сохраняем каждого участника отдельно в каждом чате.
    ensure_user(chat_id, message.from_user)

    # Считаем каждое новое сообщение.
    add_message(chat_id, message.from_user)

    # Новые участники.
    if message.new_chat_members:
        for member in message.new_chat_members:
            ensure_user(chat_id, member)

    if not text:
        return

    # Пользовательский ник.
    if text.lower().startswith("+ник"):
        await set_nickname(message)
        return

    # Определяем команду.
    parts = text.split(maxsplit=1)
    raw_command = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    if raw_command.startswith("/"):
        command = raw_command[1:].split("@")[0].lower()
    else:
        command = ALIASES.get(raw_command.lower(), "")

    if command in COMMANDS:
        if command == "profile":
            await message.reply(
                profile_text(chat_id, message.from_user.id)
            )
            return

        if command == "rank":
            row = get_user(chat_id, message.from_user.id)

            if not row:
                await message.reply("У тебя пока нет статистики.")
                return

            await message.reply(
                f"🏅 Твой ранг: {get_rank(row['xp'])}\n"
                f"Уровень: {get_level(row['xp'])}\n"
                f"Опыт: {row['xp']} XP"
            )
            return

        await moderation_command(message, command, args)
        return

    # Топ доступен всем.
    if command == "top":
        rows = conn.execute("""
        SELECT * FROM users
        WHERE chat_id = ?
        ORDER BY xp DESC, messages DESC
        LIMIT 10
        """, (chat_id,)).fetchall()

        if not rows:
            await message.reply("Пока нет статистики.")
            return

        result = ["🏆 ТОП УЧАСТНИКОВ\n"]

        for index, row in enumerate(rows, 1):
            result.append(
                f"{index}. {display_name(row)} — {row['xp']} XP"
            )

        await message.reply("\n".join(result))
        return

    # Проверяем упоминание бота.
    if not message.text:
        return

    try:
        me = await bot.get_me()
        await ask_groq(message, me.username)
    except Exception:
        logging.exception("Ошибка обработки упоминания бота")


async def main():
    logging.info("Запуск JoJo Overdrive Bot")

    me = await bot.get_me()

    logging.info("Бот авторизован: @%s", me.username)

    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен")
    finally:
        conn.close()