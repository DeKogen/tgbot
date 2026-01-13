#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram profile filter bot for daivinchik-style bots.
Reads incoming profile cards from a target bot, filters by keywords,
presses the appropriate button, and logs decisions to SQLite.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import ChatWriteForbiddenError, FloodWaitError
from telethon.tl import types

load_dotenv()

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION = os.getenv("TG_SESSION", "daivinchik_session")
TARGET_BOT = os.getenv("TARGET_BOT", "leomatchbot").lstrip("@")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

INCLUDE_KEYWORDS = os.getenv("INCLUDE_KEYWORDS", "")
EXCLUDE_KEYWORDS = os.getenv("EXCLUDE_KEYWORDS", "")
INCLUDE_MODE = os.getenv("INCLUDE_MODE", "any").lower()
MIN_TEXT_LEN = int(os.getenv("MIN_TEXT_LEN", "80"))

BTN_LIKE = os.getenv("BTN_LIKE", "")
BTN_SKIP = os.getenv("BTN_SKIP", "")
BTN_SLEEP = os.getenv("BTN_SLEEP", "")
PRESS_DELAY = float(os.getenv("PRESS_DELAY", "0.3"))
PENDING_TTL = float(os.getenv("PENDING_TTL", "30"))
AUTO_START = os.getenv("AUTO_START", "1").lower() in ("1", "true", "yes")
START_TEXT = os.getenv("START_TEXT", "/start").strip()
START_CLICK_TEXT = os.getenv("START_CLICK_TEXT", "🚀 Смотреть анкеты").strip()
START_DELAY = float(os.getenv("START_DELAY", "1.0"))

DUP_NUMERIC = os.getenv("DUP_NUMERIC", "0").lower() in ("1", "true", "yes")
DUP_DELAY = float(os.getenv("DUP_DELAY", "0.2"))

DB_PATH = Path(os.getenv("DB_PATH", "leomatch.db"))

ENABLE_DM_REPLY = os.getenv("ENABLE_DM_REPLY", "0").lower() in ("1", "true", "yes")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
log = logging.getLogger("profile_filter")

tele = TelegramClient(SESSION, API_ID, API_HASH)
PENDING_PROFILE = {"text": "", "ts": 0.0}
LAST_BUTTONS: List[Tuple[str, object, str]] = []
RESUME_BUTTON_HINTS = ("смотреть анкеты", "view profiles", "🚀")


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _parse_keywords(raw: str) -> List[str]:
    if not raw:
        return []
    parts = re.split(r"[,;\n]+", raw)
    cleaned = [p.strip().lower() for p in parts if p.strip()]
    return _dedupe(cleaned)


INCLUDE_LIST = _parse_keywords(INCLUDE_KEYWORDS)
EXCLUDE_LIST = _parse_keywords(EXCLUDE_KEYWORDS)
NEGATION_WORDS = {
    "no",
    "not",
    "never",
    "without",
    "dont",
    "don't",
    "doesnt",
    "doesn't",
    "wont",
    "won't",
    "не",
    "нет",
    "без",
    "никогда",
    "ни",
}
NEGATION_WINDOW = 3
WORD_RE = re.compile(r"[a-z0-9']+|[а-яё]+", re.IGNORECASE)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _store_pending(text: str) -> None:
    if not text.strip():
        return
    PENDING_PROFILE["text"] = text
    PENDING_PROFILE["ts"] = time.monotonic()


def _take_pending() -> Optional[str]:
    text = PENDING_PROFILE.get("text") or ""
    if not text:
        return None
    if time.monotonic() - PENDING_PROFILE["ts"] > PENDING_TTL:
        PENDING_PROFILE["text"] = ""
        return None
    PENDING_PROFILE["text"] = ""
    return text


def _find_matches(text: str, keywords: List[str]) -> List[str]:
    if not keywords:
        return []
    return [kw for kw in keywords if kw in text]


def _tokenize(text: str) -> List[Tuple[str, int, int]]:
    return [(m.group(0).lower(), m.start(), m.end()) for m in WORD_RE.finditer(text)]


def _is_negated(text: str, keyword: str, tokens: List[Tuple[str, int, int]]) -> bool:
    if not keyword:
        return False
    pattern = re.escape(keyword)
    for match in re.finditer(pattern, text):
        start, end = match.start(), match.end()
        first_idx = None
        last_idx = None
        for i, (_tok, tstart, tend) in enumerate(tokens):
            if tend <= start:
                continue
            if tstart >= end:
                if first_idx is None:
                    first_idx = i
                break
            if first_idx is None:
                first_idx = i
            last_idx = i
        if first_idx is None:
            continue
        if last_idx is None:
            last_idx = first_idx
        window_start = max(0, first_idx - NEGATION_WINDOW)
        for tok, _ts, _te in tokens[window_start:first_idx]:
            if tok in NEGATION_WORDS:
                return True
    return False


def _resume_button(buttons):
    for kind, _b, txt in buttons:
        lowered = (txt or "").strip().lower()
        if any(hint in lowered for hint in RESUME_BUTTON_HINTS):
            return kind, _b, txt
    return None


def _remember_buttons(buttons) -> None:
    if not buttons or _resume_button(buttons):
        return
    LAST_BUTTONS.clear()
    for kind, _b, txt in buttons:
        if txt:
            LAST_BUTTONS.append((kind, None, txt))


def _has_reply_buttons(buttons) -> bool:
    return any(kind == "reply" for kind, _b, _txt in buttons)


def decide_action(text: str) -> Tuple[str, str, List[str], List[str]]:
    normalized = _normalize(text)
    if MIN_TEXT_LEN and len(normalized) < MIN_TEXT_LEN:
        return "skip", "too_short", [], []

    matched_excl = _find_matches(normalized, EXCLUDE_LIST)
    if matched_excl:
        tokens = _tokenize(normalized)
        unmatched_excl = [kw for kw in matched_excl if not _is_negated(normalized, kw, tokens)]
        if unmatched_excl:
            return "skip", "exclude:" + ",".join(unmatched_excl[:5]), [], unmatched_excl

    if INCLUDE_LIST:
        matched_incl = _find_matches(normalized, INCLUDE_LIST)
        if INCLUDE_MODE == "all":
            ok = len(set(matched_incl)) == len(INCLUDE_LIST)
        else:
            ok = bool(matched_incl)
        if ok:
            return "like", "include_match", matched_incl, matched_excl
        return "skip", "no_include", matched_incl, matched_excl

    return "like", "no_includes", [], matched_excl


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                source_bot TEXT,
                action TEXT,
                reason TEXT,
                matched_include TEXT,
                matched_exclude TEXT,
                text TEXT
            )
            """
        )


def log_decision(
    source_bot: str,
    text: str,
    action: str,
    reason: str,
    matched_include: List[str],
    matched_exclude: List[str],
) -> None:
    with db() as con:
        con.execute(
            """
            INSERT INTO decisions(ts, source_bot, action, reason, matched_include, matched_exclude, text)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                source_bot,
                action,
                reason,
                ",".join(matched_include),
                ",".join(matched_exclude),
                text,
            ),
        )


async def send_safely(peer, text: str) -> None:
    try:
        await tele.send_message(peer, text)
    except FloodWaitError as e:
        log.warning("FloodWait %ss - sleeping", e.seconds)
        await asyncio.sleep(int(e.seconds) + 1)
    except ChatWriteForbiddenError:
        log.warning("ChatWriteForbidden: %s", peer)
    except Exception as e:
        log.error("send_failed: %s", e)


async def send_with_typing(peer, text: str) -> None:
    async with tele.action(peer, "typing"):
        await asyncio.sleep(min(5.0, 0.6 + 0.04 * len(text)))
    await send_safely(peer, text)


def build_dm_reply(_text: str) -> Optional[str]:
    """
    Stub for future GPT-based DM replies.
    Return a string to reply, or None to skip.
    """
    return None


def _button_prefs() -> dict:
    heart_emojis = [
        "❤",
        "❤️",
        "♥",
        "💖",
        "💘",
        "💗",
        "💞",
        "💓",
        "💙",
        "💚",
        "💛",
        "🧡",
        "💜",
        "🖤",
        "🤍",
        "🤎",
    ]
    return {
        "like": [
            BTN_LIKE,
            "like",
            "invite",
            "match",
            "heart",
            "yes",
            *heart_emojis,
            "да",
            "лайк",
            "приглас",
            "серд",
        ],
        "skip": [
            BTN_SKIP,
            "skip",
            "next",
            "no",
            "pass",
            "👎",
            "далее",
            "пропуск",
            "нет",
            "след",
        ],
        "sleep": [
            BTN_SLEEP,
            "sleep",
            "pause",
            "zzz",
            "пауза",
            "сон",
        ],
    }


async def _iter_button_texts(markup):
    out = []
    if isinstance(markup, types.ReplyInlineMarkup):
        for row in (markup.rows or []):
            for b in getattr(row, "buttons", []) or []:
                txt = getattr(b, "text", None)
                if txt:
                    out.append(("inline", b, txt))
    elif isinstance(markup, types.ReplyKeyboardMarkup):
        for row in (markup.rows or []):
            for b in getattr(row, "buttons", []) or []:
                txt = getattr(b, "text", None)
                if txt:
                    out.append(("reply", b, txt))
    return out


async def _press(ev, kind: str, txt: str) -> bool:
    if kind == "inline":
        try:
            await ev.message.click(text=txt)
            return True
        except Exception:
            pass
    await tele.send_message(ev.chat_id, txt)
    return True


async def press_choice(ev, want: str, buttons_override=None) -> bool:
    if buttons_override is not None:
        buttons = buttons_override
    else:
        markup = getattr(ev, "reply_markup", None) or getattr(ev.message, "reply_markup", None)
        buttons = await _iter_button_texts(markup)
    if not buttons:
        return False

    prefs = _button_prefs().get(want, [])
    target_exact = BTN_LIKE if want == "like" else BTN_SKIP if want == "skip" else BTN_SLEEP

    async def _dup_numeric() -> None:
        if not DUP_NUMERIC:
            return
        try:
            if want == "like":
                await tele.send_message(ev.chat_id, "2")
            elif want == "skip":
                await tele.send_message(ev.chat_id, "1")
        except Exception:
            pass

    if target_exact:
        for kind, _b, txt in buttons:
            if txt.strip() == target_exact:
                await _press(ev, kind, txt)
                await asyncio.sleep(DUP_DELAY)
                await _dup_numeric()
                return True

    lowers = [p.lower() for p in prefs if p]
    for kind, _b, txt in buttons:
        if any(p in txt.lower() for p in lowers):
            await _press(ev, kind, txt)
            await asyncio.sleep(DUP_DELAY)
            await _dup_numeric()
            return True

    flat = [b for b in buttons]
    if want in ("like", "skip") and len(flat) in (2, 3):
        idx = 0 if want == "like" else 1
        kind, _b, txt = flat[idx]
        await _press(ev, kind, txt)
        await asyncio.sleep(DUP_DELAY)
        await _dup_numeric()
        return True

    log.warning("No matching button for action=%s; buttons=%s", want, [txt for _k, _b, txt in buttons])
    return False


async def _is_target_bot(ev) -> bool:
    if ev.chat and getattr(ev.chat, "username", None):
        return ev.chat.username.lower() == TARGET_BOT.lower()
    try:
        ent = await ev.get_chat()
        return (getattr(ent, "username", "") or "").lower() == TARGET_BOT.lower()
    except Exception:
        return False


async def handle_profile_event(ev) -> bool:
    if not await _is_target_bot(ev):
        return False
    text = ev.raw_text or ""
    markup = getattr(ev, "reply_markup", None) or getattr(ev.message, "reply_markup", None)
    has_text = bool(text.strip())
    has_markup = bool(markup)
    log.debug("target_event: text=%s markup=%s msg_id=%s", has_text, has_markup, getattr(ev.message, "id", None))
    if has_markup:
        buttons = await _iter_button_texts(markup)
        resume_button = _resume_button(buttons)
        if resume_button:
            kind, _b, txt = resume_button
            await _press(ev, kind, txt)
            return True
        log.debug("buttons=%s", [txt for _k, _b, txt in buttons])
        _remember_buttons(buttons)
    if has_text and not has_markup:
        if LAST_BUTTONS and _has_reply_buttons(LAST_BUTTONS):
            action, reason, matched_incl, matched_excl = decide_action(text)
            log_decision(TARGET_BOT, text, action, reason, matched_incl, matched_excl)
            await asyncio.sleep(PRESS_DELAY)
            await press_choice(ev, action, buttons_override=LAST_BUTTONS)
            return True
        _store_pending(text)
        return True
    if has_markup and not has_text:
        pending = _take_pending()
        if not pending:
            return True
        text = pending
        has_text = True
    if not has_markup or not text.strip():
        return True
    if has_text and has_markup:
        PENDING_PROFILE["text"] = ""
    action, reason, matched_incl, matched_excl = decide_action(text)
    log_decision(TARGET_BOT, text, action, reason, matched_incl, matched_excl)
    await asyncio.sleep(PRESS_DELAY)
    await press_choice(ev, action)
    return True


@tele.on(events.NewMessage(incoming=True))
async def on_message(ev: events.NewMessage.Event) -> None:
    if await handle_profile_event(ev):
        return

    if not ENABLE_DM_REPLY:
        return

    if ev.is_private and not ev.out:
        try:
            uname = (getattr(ev.chat, "username", "") or "").lower()
        except Exception:
            uname = ""
        if uname == TARGET_BOT.lower():
            return
        try:
            sender = await ev.get_sender()
            if getattr(sender, "bot", False):
                return
        except Exception:
            pass

        reply = build_dm_reply(ev.raw_text or "")
        if reply:
            await send_with_typing(ev.sender_id, reply)


@tele.on(events.MessageEdited(incoming=True))
async def on_message_edited(ev: events.MessageEdited.Event) -> None:
    await handle_profile_event(ev)


async def main() -> None:
    if not API_ID or not API_HASH:
        raise RuntimeError("Missing TG_API_ID / TG_API_HASH")
    init_db()
    await tele.start()
    me = await tele.get_me()
    log.info("Started as @%s (%s)", getattr(me, "username", None), me.id)
    log.info("Target bot: @%s", TARGET_BOT)
    if AUTO_START:
        await asyncio.sleep(START_DELAY)
        if START_TEXT:
            log.info("Auto-start: sending %s", START_TEXT)
            await send_safely(TARGET_BOT, START_TEXT)
        if START_CLICK_TEXT:
            await asyncio.sleep(START_DELAY)
            log.info("Auto-start: sending %s", START_CLICK_TEXT)
            await send_safely(TARGET_BOT, START_CLICK_TEXT)
    await tele.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by Ctrl+C")
