# Telegram Profile Filter Bot

Filters profile cards from a target bot (e.g. @leomatchbot), presses the right button, and logs decisions to SQLite.

## Requirements
- Python 3.10+
- Telegram API ID and hash

## Setup
1. Create and activate a venv:
   - `python -m venv .venv`
   - `source .venv/bin/activate`
2. Install deps: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill values:
   - `cp .env.example .env`
   - Set `TG_API_ID`, `TG_API_HASH`, and (optionally) `TG_SESSION`.
   - Adjust `TARGET_BOT`, keywords, and button labels if needed.
4. (Optional) Disable auto-start by setting `AUTO_START=0` if you want to manually trigger `/start`.
5. Run: `python bot.py`
6. Log in to Telegram when prompted and keep the session file alongside the script.

## Keyword filtering
- `INCLUDE_KEYWORDS` and `EXCLUDE_KEYWORDS` accept comma/semicolon/newline lists.
- Matching is case-insensitive and works for English and Russian keywords.
- If `INCLUDE_KEYWORDS` is empty, everything passes unless excluded.

## Buttons
- If button text is not standard, set `BTN_LIKE` / `BTN_SKIP` to exact labels.
- If matching fails, the bot falls back to button order for 2-3 button layouts.

## DM replies (optional)
`ENABLE_DM_REPLY=0` by default. The `build_dm_reply` stub is ready for ChatGPT integration.

## Data
Decisions are logged to `leomatch.db` (configurable via `DB_PATH`).
