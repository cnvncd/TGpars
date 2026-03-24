#!/usr/bin/env python3
# mirror_userbot.py — v4
"""
Зеркалирование Telegram-канала.
- Все кликабельные ссылки заменяются на MY_LINK (текст + entities + кнопки)
- Опросы копируются вручную через InputMediaPoll (работает даже с noforwards)
- Посты < 24ч: редактирование и удаление синхронизируются с источником
- Посты >= 24ч: игнорируются (не трогаем)
- Редакция НИКОГДА не дублируется как новый пост
"""

import os
import re
import logging
import asyncio
import sqlite3
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone, timedelta
from io import BytesIO

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaPoll,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
    DocumentAttributeAnimated,
    DocumentAttributeSticker,
    DocumentAttributeFilename,
    MessageEntityTextUrl,
    MessageEntityUrl,
    MessageEntityMention,
    MessageEntityMentionName,
    MessageEntityEmail,
    MessageEntityPhone,
    # для создания опроса вручную
    InputMediaPoll,
    Poll,
    PollAnswer,
    TextWithEntities,
)
from telethon.errors import RPCError, FloodWaitError

# ──────────────────────────────────────────────────────────────
# Конфиг
# ──────────────────────────────────────────────────────────────
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SOURCE_CHANNELS = os.getenv("SOURCE_CHANNELS", "")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "")
MY_LINK = os.getenv("MY_LINK", "https://t.me/mychannel")

if not (API_ID and API_HASH and SOURCE_CHANNELS and TARGET_CHANNEL):
    raise SystemExit("Не заданы API_ID/API_HASH/SOURCE_CHANNELS/TARGET_CHANNEL в .env")

SOURCE_CHANNELS = [s.strip() for s in SOURCE_CHANNELS.split(",") if s.strip()]


def _parse(s: str):
    s = s.strip()
    try:
        return int(s) if re.fullmatch(r"-?\d+", s) else s
    except Exception:
        return s


SOURCE_CHANNELS = [_parse(s) for s in SOURCE_CHANNELS]
TARGET_CHAT_ID = _parse(TARGET_CHANNEL)

try:
    ALBUM_DELAY = float(os.getenv("ALBUM_DELAY_SECONDS", "2"))
except Exception:
    ALBUM_DELAY = 2.0

# ──────────────────────────────────────────────────────────────
# Логирование
# ──────────────────────────────────────────────────────────────
logger = logging.getLogger("mirror")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
_fh = logging.FileHandler("log.txt", encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_sh)

# ──────────────────────────────────────────────────────────────
# БД
# ──────────────────────────────────────────────────────────────
DB_PATH = "mappings.db"

album_cache: Dict[int, List[Message]] = {}
album_timers: Dict[int, asyncio.Task] = {}

TARGET_ENTITY = None
TARGET_ENTITY_ID: Optional[int] = None


def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS mappings (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id TEXT    NOT NULL,
                source_msg_id  INTEGER NOT NULL,
                target_chat_id TEXT    NOT NULL,
                target_msg_id  INTEGER NOT NULL,
                grouped_id     INTEGER,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_chat_id, source_msg_id)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_src ON mappings(source_chat_id, source_msg_id)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_grp ON mappings(grouped_id)")


def save_mapping(sc, sm: int, tc, tm: int, gid: Optional[int]):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR REPLACE INTO mappings "
            "(source_chat_id,source_msg_id,target_chat_id,target_msg_id,grouped_id) "
            "VALUES(?,?,?,?,?)",
            (str(sc), sm, str(tc), tm, gid),
        )


def get_mapping(sc, sm: int) -> Optional[Tuple[str, int]]:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT target_chat_id,target_msg_id FROM mappings "
            "WHERE source_chat_id=? AND source_msg_id=?",
            (str(sc), sm),
        ).fetchone()
    return (row[0], int(row[1])) if row else None


def del_mapping(sc, sm: int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "DELETE FROM mappings WHERE source_chat_id=? AND source_msg_id=?",
            (str(sc), sm),
        )


def mapping_age_hours(sc, sm: int) -> Optional[float]:
    """Возвращает возраст маппинга в часах или None если нет."""
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT created_at FROM mappings WHERE source_chat_id=? AND source_msg_id=?",
            (str(sc), sm),
        ).fetchone()
    if not row:
        return None
    created = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600


# ──────────────────────────────────────────────────────────────
# Замена ВСЕГО кликабельного на MY_LINK
# ──────────────────────────────────────────────────────────────

# Регулярка: любая ссылка — http(s), www, домен.tld, email, упоминания, телефоны
_URL_RE = re.compile(
    r"(?:"
    r"https?://[^\s]+"  # http:// https://
    r"|www\.[^\s]+"  # www.
    r"|"
    r'(?<![=\'"@\w])'  # не внутри href="..." и не часть слова
    r"[a-zA-Z0-9][a-zA-Z0-9\-]*"  # начало домена
    r"\.[a-zA-Z]{2,}"  # TLD
    r"(?:\.[a-zA-Z]{2,})*"  # co.uk и т.д.
    r"(?:/[^\s]*)?"  # опциональный путь (убрал обязательный слэш)
    r")",
    re.IGNORECASE,
)

# Регулярка для email
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    re.IGNORECASE,
)

# Регулярка для упоминаний @username
_MENTION_RE = re.compile(
    r"@[a-zA-Z0-9_]{5,32}\b",
    re.IGNORECASE,
)

# Регулярка для телефонов (простая версия)
_PHONE_RE = re.compile(
    r"\+?\d[\d\s\-\(\)]{7,}\d",
    re.IGNORECASE,
)

# Типы entities которые кликабельны и должны быть заменены
_CLICKABLE_ENTITY_TYPES = (
    MessageEntityUrl,  # голая ссылка в тексте
    MessageEntityTextUrl,  # [текст](ссылка)
    MessageEntityMention,  # @username
    MessageEntityEmail,  # email
    MessageEntityPhone,  # телефон
)


def replace_links(text: Optional[str]) -> str:
    """Заменяет все URL, email, упоминания и телефоны в тексте на MY_LINK."""
    if not text:
        return ""
    # Заменяем в порядке: URL -> email -> упоминания -> телефоны
    text = _URL_RE.sub(MY_LINK, text)
    text = _EMAIL_RE.sub(MY_LINK, text)
    text = _MENTION_RE.sub(MY_LINK, text)
    text = _PHONE_RE.sub(MY_LINK, text)
    return text


def sanitize_entities(entities, text: str = "") -> Optional[list]:
    """
    Обрабатывает entities:
    - MessageEntityTextUrl → заменяем URL на MY_LINK
    - MessageEntityUrl → оставляем (текст уже заменён через replace_links)
    - MessageEntityMention / Email / Phone → убираем (были кликабельны)
    Форматирование (bold, italic, code и т.д.) сохраняем.
    """
    if not entities:
        return None
    result = []
    for e in entities:
        if isinstance(e, MessageEntityTextUrl):
            result.append(
                MessageEntityTextUrl(offset=e.offset, length=e.length, url=MY_LINK)
            )
        elif isinstance(
            e,
            (
                MessageEntityMention,
                MessageEntityEmail,
                MessageEntityPhone,
                MessageEntityMentionName,
            ),
        ):
            # убираем кликабельность — не добавляем в result
            pass
        else:
            result.append(e)
    return result or None


def has_clickable(text: Optional[str], entities=None) -> bool:
    if text and _URL_RE.search(text):
        return True
    if entities:
        return any(isinstance(e, _CLICKABLE_ENTITY_TYPES) for e in entities)
    return False


def convert_buttons(buttons):
    """Заменяет все URL в кнопках на MY_LINK."""
    if not buttons:
        return None
    rows = []
    for row in buttons:
        r = []
        for b in row:
            try:
                if getattr(b, "url", None):
                    r.append(Button.url(b.text or "link", MY_LINK))
                elif hasattr(b, "data"):
                    r.append(Button.inline(b.text or "btn", b.data))
                else:
                    r.append(Button.inline(b.text or "btn", b"x"))
            except Exception as ex:
                logger.debug(f"convert_buttons: {ex}")
        if r:
            rows.append(r)
    return rows or None


# ──────────────────────────────────────────────────────────────
# Тип медиа
# ──────────────────────────────────────────────────────────────


def media_kind(msg: Message) -> str:
    if isinstance(getattr(msg, "media", None), MessageMediaPoll) or getattr(
        msg, "poll", None
    ):
        return "poll"
    if not msg.media:
        return "text"
    m = msg.media
    if isinstance(m, MessageMediaPhoto):
        return "photo"
    if isinstance(m, MessageMediaDocument):
        doc = m.document
        if not doc:
            return "document"
        at = {type(a): a for a in doc.attributes}
        if DocumentAttributeSticker in at:
            return "sticker"
        aud = at.get(DocumentAttributeAudio)
        if aud:
            return "voice" if aud.voice else "audio"
        vid = at.get(DocumentAttributeVideo)
        if vid:
            return "video_note" if vid.round_message else "video"
        if DocumentAttributeAnimated in at:
            return "gif"
        return "document"
    return "other"


def orig_filename(msg: Message) -> Optional[str]:
    if not isinstance(getattr(msg, "media", None), MessageMediaDocument):
        return None
    for a in msg.media.document.attributes:
        if isinstance(a, DocumentAttributeFilename):
            return a.file_name
    return None


# ──────────────────────────────────────────────────────────────
# Retry
# ──────────────────────────────────────────────────────────────


async def retry(fn, retries: int = 3):
    for i in range(retries):
        try:
            return await fn()
        except FloodWaitError as e:
            if i < retries - 1:
                logger.warning(f"FloodWait {e.seconds}s")
                await asyncio.sleep(e.seconds + 5)
            else:
                raise
        except Exception as e:
            if i < retries - 1:
                logger.warning(f"retry {i + 1}: {e}")
                await asyncio.sleep(2)
            else:
                raise
    return None


# ──────────────────────────────────────────────────────────────
# Опрос: создаём вручную через InputMediaPoll
# Работает даже с noforwards-каналами
# ──────────────────────────────────────────────────────────────


async def send_poll(client: TelegramClient, msg: Message, target) -> Optional[Message]:
    """
    Создаёт копию опроса через InputMediaPoll.
    Не требует пересылки — работает с защищёнными каналами.
    """
    try:
        media = msg.media
        if not isinstance(media, MessageMediaPoll):
            return None

        src_poll = media.poll

        # Создаём новый Poll с теми же параметрами
        # closed=False чтобы новый опрос был открыт
        new_poll = Poll(
            id=0,  # сервер присвоит новый id
            question=TextWithEntities(
                text=src_poll.question.text
                if hasattr(src_poll.question, "text")
                else str(src_poll.question),
                entities=list(getattr(src_poll.question, "entities", None) or []),
            ),
            answers=[
                PollAnswer(
                    text=TextWithEntities(
                        text=a.text.text if hasattr(a.text, "text") else str(a.text),
                        entities=list(getattr(a.text, "entities", None) or []),
                    ),
                    option=a.option,
                )
                for a in src_poll.answers
            ],
            closed=False,
            public_voters=src_poll.public_voters,
            multiple_choice=src_poll.multiple_choice,
            quiz=src_poll.quiz,
        )

        # Правильные ответы и объяснение (для викторин)
        correct_answers = None
        solution = None
        solution_ents = None
        if src_poll.quiz and media.results:
            correct_answers = getattr(media.results, "correct_answers", None)
            solution = getattr(media.results, "solution", None)
            solution_ents = getattr(media.results, "solution_entities", None)

        input_poll = InputMediaPoll(
            poll=new_poll,
            correct_answers=correct_answers,
            solution=solution,
            solution_entities=solution_ents,
        )

        buttons = convert_buttons(getattr(msg, "buttons", None))

        async def do():
            return await client.send_file(
                entity=target,
                file=input_poll,
                buttons=buttons,
            )

        result = await retry(do)
        logger.info(f"[poll] создан вручную → {getattr(result, 'id', '?')}")
        return result

    except Exception as e:
        logger.error(f"send_poll: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# Отправка медиа
# ──────────────────────────────────────────────────────────────


async def send_media_ref(
    client, msg, target, cap, cap_ents, buttons, kind
) -> Optional[Message]:
    """Отправляет через media-ссылку (без скачивания). Сохраняет все атрибуты."""
    try:
        kw = dict(
            entity=target,
            file=msg.media,
            caption=cap or None,
            formatting_entities=cap_ents,
            buttons=buttons,
            force_document=False,
        )
        if kind == "video_note":
            kw["video_note"] = True
            kw["caption"] = None
            kw["formatting_entities"] = None
        elif kind == "voice":
            kw["voice_note"] = True
        return await retry(lambda: client.send_file(**kw))
    except Exception as e:
        logger.warning(f"send_media_ref({kind}): {e}")
        return None


async def send_media_bytes(
    client, msg, target, cap, cap_ents, buttons, kind
) -> Optional[Message]:
    """Fallback: скачивает байты и отправляет с правильным расширением."""
    ext = {
        "photo": ".jpg",
        "voice": ".ogg",
        "video_note": ".mp4",
        "gif": ".mp4",
        "video": ".mp4",
        "audio": ".mp3",
        "sticker": ".webp",
    }
    try:
        data = await client.download_media(msg, bytes)
        if not data:
            return None
        buf = BytesIO(data)
        suf = ext.get(kind) or (
            "." + (orig_filename(msg) or "file.bin").rsplit(".", 1)[-1]
        )
        buf.name = f"media{suf}"

        kw = dict(
            entity=target,
            file=buf,
            caption=cap or None,
            formatting_entities=cap_ents,
            buttons=buttons,
            force_document=False,
        )
        if kind == "video_note":
            kw["video_note"] = True
            kw["caption"] = None
            kw["formatting_entities"] = None
        elif kind == "voice":
            kw["voice_note"] = True
        return await retry(lambda: client.send_file(**kw))
    except Exception as e:
        logger.error(f"send_media_bytes({kind}): {e}")
        return None


# ──────────────────────────────────────────────────────────────
# Альбомы
# ──────────────────────────────────────────────────────────────


async def process_album(client, grouped_id: int, target):
    msgs = album_cache.get(grouped_id)
    if not msgs:
        return
    logger.info(f"Альбом {grouped_id}: {len(msgs)} частей")
    try:
        msgs.sort(key=lambda m: m.id)
        cap_msg = next((m for m in msgs if m.message), None)
        raw_cap = cap_msg.message if cap_msg else None
        new_cap = replace_links(raw_cap) if raw_cap else None
        new_ents = (
            sanitize_entities(list(cap_msg.entities or []), new_cap or "")
            if cap_msg
            else None
        )
        buttons = convert_buttons(
            getattr(cap_msg, "buttons", None) if cap_msg else None
        )
        tgt_id = getattr(target, "id", str(target))

        src_chat = await msgs[0].get_chat()
        is_protected = bool(
            getattr(src_chat, "noforwards", False)
            or getattr(src_chat, "protected", False)
        )

        target_msgs = None

        # Попытка 1: media-объекты (быстро, без скачивания)
        if not is_protected:
            media_objs = [m.media for m in msgs if m.media]
            try:
                target_msgs = await retry(
                    lambda: client.send_file(
                        entity=target,
                        file=media_objs,
                        caption=new_cap,
                        formatting_entities=new_ents,
                        buttons=buttons,
                    )
                )
            except Exception as e:
                logger.warning(f"Альбом media_objs: {e}")
                target_msgs = None

        # Попытка 2: байты с правильными именами
        if not target_msgs:
            ext_map = {"photo": ".jpg", "video": ".mp4", "gif": ".mp4", "audio": ".mp3"}
            bufs = []
            for m in msgs:
                if not m.media:
                    continue
                k = media_kind(m)
                try:
                    data = await client.download_media(m, bytes)
                    if not data:
                        continue
                    buf = BytesIO(data)
                    suf = ext_map.get(k) or (
                        "." + (orig_filename(m) or "file.bin").rsplit(".", 1)[-1]
                    )
                    buf.name = f"media{suf}"
                    bufs.append(buf)
                except Exception as e:
                    logger.warning(f"Альбом download {m.id}: {e}")
            if bufs:
                try:
                    target_msgs = await retry(
                        lambda: client.send_file(
                            entity=target,
                            file=bufs,
                            caption=new_cap,
                            formatting_entities=new_ents,
                            buttons=buttons,
                        )
                    )
                except Exception as e:
                    logger.error(f"Альбом bytes: {e}")

        if not target_msgs:
            logger.error(f"Альбом {grouped_id} не отправлен")
            return

        tgt_list = target_msgs if isinstance(target_msgs, list) else [target_msgs]
        for i, sm in enumerate(msgs):
            tid = tgt_list[i].id if i < len(tgt_list) else tgt_list[-1].id
            save_mapping(sm.chat_id, sm.id, tgt_id, tid, grouped_id)
        logger.info(f"Альбом {grouped_id} → {len(tgt_list)} сообщений")

    except Exception as e:
        logger.exception(f"process_album {grouped_id}: {e}")
    finally:
        album_cache.pop(grouped_id, None)
        album_timers.pop(grouped_id, None)


# ──────────────────────────────────────────────────────────────
# Копирование одного сообщения
# ──────────────────────────────────────────────────────────────


async def copy_message(client, msg: Message, target) -> Optional[Message]:
    try:
        if getattr(msg, "action", None) is not None:
            return None

        # Альбом
        gid = getattr(msg, "grouped_id", None)
        if gid:
            album_cache.setdefault(gid, []).append(msg)
            if gid in album_timers:
                album_timers[gid].cancel()

            async def _later():
                await asyncio.sleep(ALBUM_DELAY)
                await process_album(client, gid, target)

            album_timers[gid] = asyncio.create_task(_later())
            logger.info(f"Альбом {gid}: {len(album_cache[gid])} частей")
            return None

        kind = media_kind(msg)
        raw_text = msg.message or msg.text or ""
        ents = list(msg.entities or [])
        need_repl = has_clickable(raw_text, ents)
        buttons = convert_buttons(getattr(msg, "buttons", None))
        tgt_id = getattr(target, "id", str(target))

        src_chat = await msg.get_chat()
        is_protected = bool(
            getattr(src_chat, "noforwards", False)
            or getattr(src_chat, "protected", False)
        )

        out: Optional[Message] = None

        # ── Опрос: создаём вручную ────────────────────────────────────────
        if kind == "poll":
            out = await send_poll(client, msg, target)
            if out:
                save_mapping(msg.chat_id, msg.id, tgt_id, out.id, None)
            else:
                logger.error(f"[poll] {msg.id} не скопирован")
            return out

        # ── Стикер ───────────────────────────────────────────────────────
        if kind == "sticker":
            try:
                out = await retry(lambda: msg.copy_to(target))
            except Exception as e:
                logger.warning(f"[sticker] copy_to: {e}")
            if out:
                save_mapping(msg.chat_id, msg.id, tgt_id, out.id, None)
            return out

        # ── video_note / voice: только через media-ссылку ────────────────
        if kind in ("video_note", "voice"):
            cap = replace_links(raw_text) if raw_text and kind == "voice" else None
            ce = sanitize_entities(ents, cap or "") if cap else None
            out = await send_media_ref(client, msg, target, cap, ce, buttons, kind)
            if not out:
                out = await send_media_bytes(
                    client, msg, target, cap, ce, buttons, kind
                )
            if out:
                save_mapping(msg.chat_id, msg.id, tgt_id, out.id, None)
                logger.info(f"[{kind}] {msg.id} → {out.id}")
            return out

        # ── Не-защищённый: copy_to ────────────────────────────────────────
        if not is_protected:
            try:
                out = await retry(lambda: msg.copy_to(target))
                if out and (need_repl or buttons):
                    nt = replace_links(raw_text)
                    ne = sanitize_entities(ents, nt)
                    try:
                        await client.edit_message(
                            entity=target,
                            message=out.id,
                            text=nt or (None if msg.media else " "),
                            formatting_entities=ne,
                            buttons=buttons,
                        )
                    except Exception as e:
                        logger.warning(f"edit после copy_to: {e}")
            except Exception as e:
                logger.warning(f"copy_to ({kind}): {e} → fallback")
                out = None

        # ── Fallback ─────────────────────────────────────────────────────
        if not out:
            cap = replace_links(raw_text) if raw_text else None
            ce = sanitize_entities(ents, cap or "")
            if msg.media:
                out = await send_media_ref(client, msg, target, cap, ce, buttons, kind)
                if not out:
                    out = await send_media_bytes(
                        client, msg, target, cap, ce, buttons, kind
                    )
            else:
                nt = replace_links(raw_text) or " "
                ne = sanitize_entities(ents, nt)
                try:
                    out = await retry(
                        lambda: client.send_message(
                            entity=target,
                            message=nt,
                            formatting_entities=ne,
                            buttons=buttons,
                        )
                    )
                except Exception as e:
                    logger.error(f"send_message: {e}")

        if out:
            save_mapping(msg.chat_id, msg.id, tgt_id, out.id, None)
            logger.info(f"[{kind}] {msg.chat_id}:{msg.id} → {tgt_id}:{out.id}")
        return out

    except Exception as e:
        logger.exception(f"copy_message: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# Обработчики событий
# ──────────────────────────────────────────────────────────────


async def handle_new(client, event):
    msg = event.message
    if getattr(msg, "action", None) is not None:
        return
    chat = await msg.get_chat()
    logger.info(f"NEW {getattr(chat, 'id', '')}:{msg.id}")
    try:
        r = await copy_message(client, msg, TARGET_ENTITY or TARGET_CHAT_ID)
        if r:
            logger.info(f"  → {TARGET_CHANNEL}:{r.id}")
    except Exception as e:
        logger.exception(f"handle_new: {e}")


async def handle_edit(client, event):
    """
    Редактирование.
    - Нет маппинга → пост не видели → копируем как новый
    - Маппинг есть, пост < 24ч → редактируем (или пересоздаём если медиа)
    - Маппинг есть, пост >= 24ч → игнорируем
    - НИКОГДА не создаём дубль если маппинг уже есть
    """
    msg = event.message
    if getattr(msg, "action", None) is not None:
        return
    chat = await msg.get_chat()
    # Используем msg.chat_id вместо chat.id для консистентности
    chat_id = msg.chat_id
    logger.info(f"EDIT {chat_id}:{msg.id}")
    try:
        age = mapping_age_hours(chat_id, msg.id)
        logger.info(f"  mapping age: {age} hours" if age else "  mapping not found")

        if age is None:
            # Нет маппинга — значит пост не был скопирован (упал при старте или старый)
            # Копируем как новый
            logger.info(f"  нет маппинга → копируем как новый")
            await handle_new(client, event)
            return

        if age >= 24:
            # Пост старше 24ч — не трогаем
            logger.info(f"  возраст {age:.1f}ч >= 24ч → пропускаем")
            return

        # Пост < 24ч — синхронизируем
        logger.info(f"  возраст {age:.1f}ч < 24ч → синхронизируем")
        mapping = get_mapping(chat_id, msg.id)
        tgt_chat, tgt_mid = mapping
        tgt = TARGET_ENTITY or tgt_chat

        if msg.media:
            # Медиа изменилось — удаляем старое, отправляем новое
            logger.info(f"  медиа изменилось → удаляем {tgt_mid} и пересоздаём")
            try:
                await client.delete_messages(entity=tgt, message_ids=tgt_mid)
            except Exception as e:
                logger.warning(f"  delete old: {e}")
            del_mapping(chat_id, msg.id)
            r = await copy_message(client, msg, TARGET_ENTITY or TARGET_CHAT_ID)
            logger.info(f"  пересоздано → {r.id if r else 'FAIL'}")
            return

        # Только текст/кнопки — редактируем на месте
        logger.info(f"  редактируем текст в сообщении {tgt_mid}")
        nt = replace_links(msg.message or msg.text or "")
        ne = sanitize_entities(list(msg.entities or []), nt)
        nb = convert_buttons(getattr(msg, "buttons", None))
        try:
            await client.edit_message(
                entity=tgt,
                message=tgt_mid,
                text=nt or " ",
                formatting_entities=ne,
                buttons=nb,
            )
            logger.info(f"  ✓ отредактировано → {tgt_mid}")
        except Exception as e:
            logger.warning(f"  edit_message failed: {e} — пересоздаю")
            try:
                await client.delete_messages(entity=tgt, message_ids=tgt_mid)
                del_mapping(chat_id, msg.id)
            except Exception:
                pass
            r = await copy_message(client, msg, TARGET_ENTITY or TARGET_CHAT_ID)
            logger.info(f"  пересоздано → {r.id if r else 'FAIL'}")

    except Exception as e:
        logger.exception(f"handle_edit: {e}")


async def handle_delete(client, event):
    """
    Удаление.
    - Маппинг есть и пост < 24ч → удаляем в целевом
    - Маппинг есть и пост >= 24ч → игнорируем
    - Нет маппинга → ничего не делаем
    """
    try:
        # Получаем chat_id напрямую из события
        chat_id = event.chat_id
        if not chat_id:
            return
        logger.info(f"DELETE event in chat {chat_id}, ids: {event.deleted_ids}")
        for mid in event.deleted_ids:
            age = mapping_age_hours(chat_id, mid)
            if age is None:
                continue  # нет маппинга
            if age >= 24:
                logger.info(
                    f"DELETE {chat_id}:{mid} возраст {age:.1f}ч >= 24ч → пропускаем"
                )
                continue
            mapping = get_mapping(chat_id, mid)
            if not mapping:
                continue
            tgt_chat, tgt_mid = mapping
            try:
                await client.delete_messages(
                    entity=TARGET_ENTITY or tgt_chat,
                    message_ids=tgt_mid,
                )
                del_mapping(chat_id, mid)
                logger.info(f"DELETE {chat_id}:{mid} → {tgt_chat}:{tgt_mid}")
            except Exception as e:
                logger.error(f"delete {mid}: {e}")
    except Exception as e:
        logger.exception(f"handle_delete: {e}")


# ──────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────


async def main():
    init_db()
    client = TelegramClient("mirror_session", API_ID, API_HASH, auto_reconnect=True)
    await client.start()
    me = await client.get_me()
    logger.info(f"Запущен как {me.first_name} (@{me.username})")

    global TARGET_ENTITY, TARGET_ENTITY_ID
    for ident in (TARGET_CHAT_ID, TARGET_CHANNEL):
        try:
            TARGET_ENTITY = await client.get_entity(ident)
            TARGET_ENTITY_ID = TARGET_ENTITY.id
            logger.info(f"Цель: {TARGET_ENTITY_ID} ({TARGET_CHANNEL})")
            break
        except Exception as e:
            logger.warning(f"get_entity({ident}): {e}")

    @client.on(events.NewMessage(chats=SOURCE_CHANNELS))
    async def _new(ev):
        asyncio.create_task(handle_new(client, ev))

    @client.on(events.MessageEdited(chats=SOURCE_CHANNELS))
    async def _edit(ev):
        asyncio.create_task(handle_edit(client, ev))

    @client.on(events.MessageDeleted(chats=SOURCE_CHANNELS))
    async def _del(ev):
        asyncio.create_task(handle_delete(client, ev))

    logger.info(f"Слушаю: {SOURCE_CHANNELS} → {TARGET_CHANNEL}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлено")
    except Exception as e:
        logger.exception(f"Критическая ошибка: {e}")
