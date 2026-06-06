import re, time, asyncio
from datetime import datetime, timezone
from io import BytesIO
from aiogram import Router, Bot
from aiogram.types import (
    Message, BufferedInputFile, MessageEntity, InputMediaPhoto,
    ChatMemberUpdated, ReactionTypeEmoji, ChatPermissions,
)
from cachetools import TTLCache
from loguru import logger
from config import (
    ALLOWED_GROUP_IDS, DISABLED_THREADS, DISABLED_GENERAL_CHATS,
    RATE_LIMIT_SEC, MAX_URL_LEN,
)
import cache, security, screenshot, metadata, queue_manager

# aiogram 3.x: msg.reply* самі проставляють message_thread_id з вихідного
# повідомлення — вручну НЕ передаємо (інакше дубль kwarg → TypeError).
# bot.send_message тред НЕ проставляє сам → йому message_thread_id передаємо явно.

router = Router()
URL_RE = re.compile(r'https?://[^\s]+')

MAX_MSG_AGE = 60

# --- Анти-спам дублікатами: ескалація + реальний mute ---
# ПАМ'ЯТЬ: нічого на диск, усе в bounded TTLCache (maxsize + ttl → не росте).
# Активний бан зберігає САМ Telegram (restrictChatMember + until_date), у нас 0 байт.
DUP_WINDOW_SEC = 120        # вікно, де повтори ОДНОГО посилання = спам
STRIKE_DECAY_SEC = 120      # тиша 2 хв → лічильник попереджень обнуляється
BAN_SEC = 300               # mute 5 хв; Telegram сам знімає за until_date

_rate_store: TTLCache = TTLCache(maxsize=10_000, ttl=RATE_LIMIT_SEC)
_rate_notified: TTLCache = TTLCache(maxsize=10_000, ttl=RATE_LIMIT_SEC)
# (chat,user,url) → True: посилання вже обслужене цьому юзеру в цьому чаті (у вікні).
_dup_seen: TTLCache = TTLCache(maxsize=20_000, ttl=DUP_WINDOW_SEC)
# (chat,user) → strike: рівень ескалації.
_strikes: TTLCache = TTLCache(maxsize=10_000, ttl=STRIKE_DECAY_SEC)
# (chat,user) → message_id: одне ескалуюче повідомлення, редагуємо на місці.
_warn_msg: TTLCache = TTLCache(maxsize=10_000, ttl=STRIKE_DECAY_SEC)

def _rate_cooldown(user_id: int) -> int:
    """Залишок кулдауну в секундах (0 = можна). При 0 — фіксує поточний запит."""
    last = _rate_store.get(user_id)
    if last is not None:
        remaining = RATE_LIMIT_SEC - (time.monotonic() - last)
        if remaining > 0:
            return int(remaining) + 1
    _rate_store[user_id] = time.monotonic()
    return 0

# --- Модераторські примітиви (мʼяко падають, якщо прав/умов немає) ---
async def _react(bot: Bot, msg: Message, emoji: str):
    try:
        await bot.set_message_reaction(
            chat_id=msg.chat.id, message_id=msg.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as e:
        logger.info(f"react skipped chat={msg.chat.id}: {e}")

async def _delete(bot: Bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.info(f"delete skipped chat={chat_id} msg={message_id}: {e}")

async def _mute(bot: Bot, chat_id: int, user_id: int, seconds: int) -> bool:
    """Реальний mute в Telegram. until_date → Telegram сам знімає, нам зберігати нічого."""
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(time.time()) + seconds,
        )
        return True
    except Exception as e:
        logger.warning(f"mute failed chat={chat_id} user={user_id}: {e}")
        return False

async def _notice(bot: Bot, msg: Message, skey: tuple, text: str):
    """Одне ескалуюче повідомлення: редагуємо на місці, не плодимо нові."""
    mid = _warn_msg.get(skey)
    if mid:
        try:
            await bot.edit_message_text(text=text, chat_id=msg.chat.id, message_id=mid)
            return
        except Exception:
            pass  # повідомлення видалили/застаріло → надішлемо нове
    try:
        sent = await bot.send_message(
            chat_id=msg.chat.id, text=text,
            message_thread_id=msg.message_thread_id,  # у той самий топік
        )
        _warn_msg[skey] = sent.message_id
    except Exception as e:
        logger.warning(f"notice failed chat={msg.chat.id}: {e}")

async def _handle_duplicate_spam(bot: Bot, msg: Message, chat_id: int, user_id: int):
    """Ескалація на повтор того ж URL: 🗑+⏳ → 🗑+⚠️ → 🗑+🛑 → 🗑+🚫 mute 5 хв."""
    skey = (chat_id, user_id)
    strike = _strikes.get(skey, 0) + 1
    _strikes[skey] = strike
    logger.info(f"DUP_SPAM chat={chat_id} user={user_id} strike={strike}")

    name = (msg.from_user.first_name if msg.from_user else None) or "Користувач"

    await _delete(bot, chat_id, msg.message_id)

    if strike == 1:
        text = f"⏳ {name}, це посилання вже в обробці.\nНе дублюйте — результат буде нижче. 👇"
    elif strike == 2:
        text = f"⚠️ {name}, досить дублювати те саме посилання.\nЗупиніться, будь ласка."
    elif strike == 3:
        text = f"🛑 {name}, ОСТАННЄ попередження!\nЩе раз — і пауза на {BAN_SEC // 60} хв. 🔇"
    else:  # strike >= 4 → реальний mute
        if await _mute(bot, chat_id, user_id, BAN_SEC):
            text = (f"🚫 {name} — ПАУЗА {BAN_SEC // 60} хв.\n"
                    f"За спам одним посиланням. Повтори видаляються.\n"
                    f"Поверніться трохи згодом. ⏱")
        else:
            text = (f"🚫 {name}, спам зафіксовано.\n"
                    f"Припиніть — інакше повтори видалятимуться.")

    await _notice(bot, msg, skey, text)

# --- Привʼязка до груп + довіра адмінам ---
_admin_cache: TTLCache = TTLCache(maxsize=64, ttl=300)

async def _get_admin_ids(bot: Bot, chat_id: int) -> set[int]:
    cached = _admin_cache.get(chat_id)
    if cached is not None:
        return cached
    try:
        admins = await bot.get_chat_administrators(chat_id)
        ids = {a.user.id for a in admins if a.user}
        _admin_cache[chat_id] = ids  # кешуємо ТІЛЬКИ успіх
        return ids
    except Exception as e:
        logger.warning(f"get_chat_administrators failed chat={chat_id}: {e}")
        return set()

async def _is_trusted_sender(bot: Bot, msg: Message) -> bool:
    """True → адмін: бот його повідомлення пропускає (не обробляє ні з чим)."""
    if msg.sender_chat and msg.sender_chat.id == msg.chat.id:
        return True
    if msg.chat.type not in ("group", "supergroup"):
        return False
    if not msg.from_user:
        return False
    return msg.from_user.id in await _get_admin_ids(bot, msg.chat.id)

@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated, bot: Bot):
    chat = event.chat
    status = event.new_chat_member.status
    logger.info(f"MY_CHAT_MEMBER chat_id={chat.id} type={chat.type} title={chat.title!r} status={status}")

    if not ALLOWED_GROUP_IDS:
        return
    if chat.type == "private":
        return

    present = status in ("member", "administrator", "restricted")
    if present and chat.id not in ALLOWED_GROUP_IDS:
        logger.warning(f"LEAVE non-allowed chat_id={chat.id} (allowed={sorted(ALLOWED_GROUP_IDS)})")
        try:
            await bot.leave_chat(chat.id)
        except Exception as e:
            logger.error(f"leave_chat failed chat={chat.id}: {e}")

# --- Повідомлення ---
WARNING_INSTANT = (
    "\n"
    "🚨⚠️ СТОП! НЕ ПЕРЕХОДЬТЕ ЗА ПОСИЛАННЯМ! ⚠️🚨\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🛡 Готую безпечний перегляд сторінки.\n"
    "⏳ Зазвичай до 1–2 хвилин — не переходьте, дочекайтесь результату нижче. 👇"
)

DISCLAIMER = (
    "🚨 УВАГА! Не довіряйте незнайомим посиланням.\n"
    "⚠️ Ніколи не вводьте паролі та дані картки на невідомих сайтах.\n"
    "🔎 Безпечніше знайти цей товар через пошук Google."
)

# Стійкий анти-фішинг банер. Замінює статус «Готую перегляд» (НЕ видаляємо його),
# лишається над прев'ю. Статичний текст → parse_mode=HTML безпечний (немає вводу юзера).
ANTIPHISH_NOTICE = (
    "🛡 <b>БЕЗПЕЧНИЙ ПЕРЕГЛЯД готовий — дивись нижче</b> 👇\n\n"
    "🚨 <b>НЕ переходь за посиланням, поки не перевірив його.</b>\n"
    "<blockquote>Ознаки шахрайства:\n"
    "• домен із підміною (g00gle, olx-ua.com, дивні .top/.xyz)\n"
    "• просять логін, пароль, дані картки або код із SMS\n"
    "• тиснуть: «терміново», «оплати зараз», таймер зворотного відліку\n"
    "• ціна «занадто вигідна», передоплата/завдаток на картку</blockquote>\n"
    "✅ <b>Безпечно:</b> знайди той самий товар сам через Google або офіційний застосунок.\n"
    "🔐 Паролі та дані картки не вводь на незнайомому сайті — ніколи."
)


async def _show_notice(status):
    """Замість видалення статусу робимо його стійким анти-фішинг банером."""
    try:
        await status.edit_text(ANTIPHISH_NOTICE, parse_mode="HTML")
    except Exception as e:
        logger.info(f"notice edit skipped: {e}")


def build_message(meta: dict) -> tuple[str, list[MessageEntity]]:
    text = ""
    entities = []

    if meta.get("site_name"):
        text += f"🌐 {meta['site_name']}\n"

    if meta.get("title"):
        title = meta["title"]
        text += "📌 "
        start = len(text.encode("utf-16-le")) // 2
        text += title + "\n"
        end = len(text.encode("utf-16-le")) // 2 - 1
        entities.append(MessageEntity(type="code", offset=start, length=end - start))

    if meta.get("brand"):
        text += f"🏷 Бренд: {meta['brand']}\n"

    if meta.get("price"):
        price_str = f"💰 Ціна: {meta['price']}"
        start = len(text.encode("utf-16-le")) // 2
        text += price_str + "\n"
        end = len(text.encode("utf-16-le")) // 2 - 1
        entities.append(MessageEntity(type="bold", offset=start, length=end - start))

    if meta.get("rating"):
        text += f"{meta['rating']}\n"

    if meta.get("description"):
        desc = meta["description"].strip()
        if len(desc) > 300:
            desc = desc[:300].rsplit(" ", 1)[0] + "…"
        text += f"\n📝 {desc}\n"

    text += "\n"
    start = len(text.encode("utf-16-le")) // 2
    text += DISCLAIMER
    end = len(text.encode("utf-16-le")) // 2
    entities.append(MessageEntity(type="blockquote", offset=start, length=end - start))

    return text, entities

def build_disclaimer_only() -> tuple[str, list[MessageEntity]]:
    text = "ℹ️ Не вдалось отримати дані про сторінку.\n\n" + DISCLAIMER
    start = len("ℹ️ Не вдалось отримати дані про сторінку.\n\n".encode("utf-16-le")) // 2
    end = len(text.encode("utf-16-le")) // 2
    entities = [MessageEntity(type="blockquote", offset=start, length=end - start)]
    return text, entities

def trim_caption(text: str, entities: list) -> tuple[str, list]:
    if len(text) <= 1024:
        return text, entities
    text = text[:1021] + "…"
    limit = len(text.encode("utf-16-le")) // 2
    entities = [e for e in entities if e.offset + e.length <= limit]
    return text, entities

def merge_meta(httpx_meta: dict, browser_meta: dict) -> dict:
    result = {}
    h_title = httpx_meta.get("title") or ""
    b_title = browser_meta.get("title") or ""
    result["title"] = b_title if len(b_title) > len(h_title) else h_title
    h_desc = httpx_meta.get("description") or ""
    b_desc = browser_meta.get("description") or ""
    result["description"] = b_desc if len(b_desc) > len(h_desc) else h_desc
    for key in ("price", "brand", "rating"):
        result[key] = browser_meta.get(key) or httpx_meta.get(key)
    result["site_name"] = httpx_meta.get("site_name") or browser_meta.get("site_name")
    result["image"] = httpx_meta.get("image") or browser_meta.get("image")
    return result

def _format_warning(position: int) -> str:
    if position <= 1:
        return WARNING_INSTANT
    eta = position * 60
    return (
        "\n"
        "🚨⚠️ СТОП! НЕ ПЕРЕХОДЬТЕ ЗА ПОСИЛАННЯМ! ⚠️🚨\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🛡 Готую безпечний перегляд сторінки.\n"
        f"📊 Ваша позиція в черзі: {position}. Орієнтовний час: ~{eta} сек.\n"
        "⏳ Не переходьте, дочекайтесь результату нижче. 👇"
    )

def _utf16_len(s: str) -> int:
    """Довжина рядка в UTF-16 одиницях — Telegram рахує offset/length саме так."""
    return len(s.encode("utf-16-le")) // 2

def _sender_prefix(msg: Message) -> tuple[str, list[MessageEntity]]:
    """Атрибуція відправника В ТІЛІ картки. Переживає видалення вихідного
    повідомлення (раніше відправник був видимий лише в reply-цитаті)."""
    user = msg.from_user
    if not user:
        return "", []
    label = "👤 Надіслав: "
    if user.username:
        return f"{label}@{user.username}\n\n", []
    name = (user.full_name or "Користувач").strip() or "Користувач"
    ent = MessageEntity(
        type="text_mention",
        offset=_utf16_len(label),
        length=_utf16_len(name),
        user=user,
    )
    return f"{label}{name}\n\n", [ent]

def _with_sender(msg: Message, text: str, entities: list) -> tuple[str, list]:
    """Дописує атрибуцію відправника на початок картки, зсуваючи offset'и."""
    prefix, prefix_ents = _sender_prefix(msg)
    if not prefix:
        return text, entities
    shift = _utf16_len(prefix)
    shifted = [e.model_copy(update={"offset": e.offset + shift}) for e in entities]
    return prefix + text, prefix_ents + shifted

async def _send_from_cache(msg: Message, url: str, entry: dict):
    kind = entry.get("kind")
    meta = entry.get("meta") or {}

    if meta and meta.get("title"):
        msg_text, msg_entities = build_message(meta)
    else:
        msg_text, msg_entities = build_disclaimer_only()
    msg_text, msg_entities = _with_sender(msg, msg_text, msg_entities)
    cap_text, cap_entities = trim_caption(msg_text, msg_entities)

    if kind == "photo":
        await msg.reply_photo(photo=entry["file_id"], caption=cap_text, caption_entities=cap_entities)
    elif kind == "media_group":
        media = []
        for i, fid in enumerate(entry["file_ids"]):
            if i == 0:
                media.append(InputMediaPhoto(media=fid, caption=cap_text, caption_entities=cap_entities))
            else:
                media.append(InputMediaPhoto(media=fid))
        await msg.reply_media_group(media=media)
    elif kind == "text":
        await msg.reply(text=msg_text, entities=msg_entities)

def _thread_disabled(chat_id: int, thread_id) -> bool:
    """Цей топік ЦІЄЇ групи в denylist? General = повідомлення без топіка (thread_id is None)."""
    if thread_id is None:
        return chat_id in DISABLED_GENERAL_CHATS
    return (chat_id, thread_id) in DISABLED_THREADS

@router.message()
async def handle(msg: Message, bot: Bot):
    age = (datetime.now(timezone.utc) - msg.date).total_seconds()
    if age > MAX_MSG_AGE:
        logger.info(f"SKIP stale msg age={age:.0f}s chat={msg.chat.id}")
        return

    if ALLOWED_GROUP_IDS and msg.chat.id not in ALLOWED_GROUP_IDS:
        if msg.chat.type in ("group", "supergroup", "channel"):
            logger.warning(f"Message in non-allowed chat {msg.chat.id} — leaving")
            try:
                await bot.leave_chat(msg.chat.id)
            except Exception as e:
                logger.warning(f"leave_chat failed: {e}")
        return

    # Denylist топіків (пара група+топік) — ПОВЕРХ фільтра групи. Стоїть ПІСЛЯ
    # нього, щоб із чужих груп бот усе одно виходив. У вимкнених топіках бот мовчить.
    if _thread_disabled(msg.chat.id, msg.message_thread_id):
        return

    text = msg.text or msg.caption or ""
    urls = URL_RE.findall(text)
    if not urls:
        return

    # Адміни — повз усе (ліміт/дедуп/видалення/мьют їх не стосуються).
    if await _is_trusted_sender(bot, msg):
        uid = msg.from_user.id if msg.from_user else "anon"
        logger.info(f"SKIP trusted sender user={uid} chat={msg.chat.id}")
        return

    url = urls[0]
    # Аномально довгі URL не обробляємо (cache/regex hygiene, анти-abuse).
    if len(url) > MAX_URL_LEN:
        logger.info(f"SKIP overly long URL len={len(url)} chat={msg.chat.id}")
        return

    chat_id = msg.chat.id
    user_id = msg.from_user.id if msg.from_user else None

    if user_id is not None:
        # Повтор тієї ж посилання цим юзером → ескалація (видалення + попередження/мьют).
        if (chat_id, user_id, url) in _dup_seen:
            _dup_seen[(chat_id, user_id, url)] = True  # тримаємо вікно живим, поки спамлять
            await _handle_duplicate_spam(bot, msg, chat_id, user_id)
            return

        # Загальний пейсинг РІЗНИХ посилань. Дублі сюди не доходять.
        cooldown = _rate_cooldown(user_id)
        if cooldown:
            logger.info(f"RATE_LIMIT user={user_id} cooldown={cooldown}s")
            if user_id not in _rate_notified:
                _rate_notified[user_id] = True
                await msg.reply(f"⏳ Зачекайте {cooldown} сек. перед наступним запитом.")
            return

        _dup_seen[(chat_id, user_id, url)] = True  # приймаємо в роботу

    screenshot.log_ram("Start request")

    if not security.is_safe(url):
        await msg.reply("🚫 Посилання веде на недоступний ресурс.")
        return

    entry = cache.get(url)
    if entry:
        kind = entry.get("kind")
        if kind == "failure":
            await msg.reply(
                f"🚫 Сторінка недоступна.\n"
                f"Причина: {entry.get('failure_reason', 'unknown')}\n"
                f"Спробуйте через декілька хвилин."
            )
            return
        await _send_from_cache(msg, url, entry)
        return

    dest_key = (chat_id, msg.message_thread_id, url)
    try:
        future, position, is_duplicate = await queue_manager.enqueue(dest_key, url)
    except queue_manager.QueueFull:
        if user_id is not None:
            _dup_seen.pop((chat_id, user_id, url), None)
        await msg.reply(
            "⚠️ Бот зараз перевантажений (черга заповнена).\n"
            "Будь ласка, спробуйте через хвилину."
        )
        return

    if is_duplicate:
        logger.info(f"INFLIGHT dup url={url} chat={chat_id} thread={msg.message_thread_id} — react 👀")
        await _react(bot, msg, "👀")
        return

    status = await msg.reply(_format_warning(position))
    start = time.monotonic()

    httpx_task = asyncio.create_task(metadata.fetch(url))

    try:
        parts, browser_meta = await future
    except Exception as e:
        logger.error(f"FAIL url-task error={type(e).__name__}")
        cache.save_failure(url, type(e).__name__)
        await status.edit_text(
            "❌ Не вдалось зробити безпечне прев'ю.\n"
            "🚨 Тим більше не переходь за посиланням — спробуй пізніше "
            "або знайди товар через Google."
        )
        return

    httpx_meta = await httpx_task

    meta = merge_meta(httpx_meta, browser_meta)
    logger.info(f"Final meta: title={meta.get('title')} price={meta.get('price')}")

    elapsed = time.monotonic() - start

    if meta and meta.get("title"):
        msg_text, msg_entities = build_message(meta)
    else:
        msg_text, msg_entities = build_disclaimer_only()
    msg_text, msg_entities = _with_sender(msg, msg_text, msg_entities)

    try:
        if parts:
            cap_text, cap_entities = trim_caption(msg_text, msg_entities)

            if len(parts) == 1:
                sent = await msg.reply_photo(
                    photo=BufferedInputFile(parts[0], filename="preview.png"),
                    caption=cap_text,
                    caption_entities=cap_entities,
                )
                if sent.photo:
                    cache.save_photo(url, sent.photo[-1].file_id, meta)
            else:
                media = []
                for i, part in enumerate(parts):
                    if i == 0:
                        media.append(InputMediaPhoto(
                            media=BufferedInputFile(part, filename=f"part_{i+1}.png"),
                            caption=cap_text,
                            caption_entities=cap_entities,
                        ))
                    else:
                        media.append(InputMediaPhoto(
                            media=BufferedInputFile(part, filename=f"part_{i+1}.png"),
                        ))
                sent_list = await msg.reply_media_group(media=media)
                if sent_list:
                    file_ids = [s.photo[-1].file_id for s in sent_list if s.photo]
                    if file_ids:
                        cache.save_media_group(url, file_ids, meta)

            logger.info(f"OK+photo parts={len(parts)} time={elapsed:.1f}s")
        else:
            await msg.reply(text=msg_text, entities=msg_entities)
            if meta and meta.get("title"):
                cache.save_text_only(url, meta)
            else:
                cache.save_failure(url, "empty result")
            logger.info(f"OK+text time={elapsed:.1f}s")

    except Exception as e:
        logger.error(f"FAIL send error={type(e).__name__}")
        cache.save_failure(url, type(e).__name__)
        await status.edit_text(
            "❌ Не вдалось зробити безпечне прев'ю.\n"
            "🚨 Тим більше не переходь за посиланням — спробуй пізніше "
            "або знайди товар через Google."
        )
        return

    # Статус «Готую перегляд» не видаляємо, а лишаємо як стійкий анти-фішинг банер
    # над прев'ю (максимально броске й корисне нагадування).
    await _show_notice(status)
