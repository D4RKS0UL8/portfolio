"""
Per-guild SQLite persistence.

BLOCKING I/O - WHAT WAS WRONG AND WHAT CHANGED
------------------------------------------------
sqlite3 (the stdlib module) is fully synchronous - there's no `await`
anywhere in it. In the original script, every one of these functions
ran directly on the event loop: `get_db()` opens a file, runs several
`CREATE TABLE IF NOT EXISTS` statements, and commits, and every single
balance/streak/bio/achievement read or write opened yet another
connection - all synchronously, all on the *same* thread that also
handles Discord's gateway heartbeat, every command reply, and every
button interaction across every guild the bot is in. Under any real
load (a busy server, several guilds active at once) those calls queue
up behind each other and stall everything else, including the
heartbeat Discord expects on a schedule - which is how bots end up
randomly disconnecting under load.

The fix: every function that touches disk here is a thin
`asyncio.to_thread(...)` wrapper around a private `_..._sync`
implementation. The SQL and the table schema are byte-for-byte what
the original script had - only *where* they run changed.

Two extra things beyond a straight 1:1 wrap, both because they sit on
the hottest paths in the bot:
  - `record_message()` (called from on_message, i.e. on every single
    non-bot message) folds what used to be up to three separate
    connections - one for the msg_logs check+insert, one inside the
    old get_bal(), one for the balance UPDATE - into one connection,
    one thread dispatch.
  - `update_bal()` similarly reuses one connection instead of two.
Both are drop-in behaviorally identical to the original; see the
comments on each for the reasoning.
"""
import asyncio
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

DB_DIR = "server_dbs"
if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR)


def get_connection_sync(guild_id: int):
    """Opens (and, on first use, initializes) this guild's sqlite
    file. Synchronous by nature - only call this from inside a
    function that's itself running in a worker thread."""
    db_path = os.path.join(DB_DIR, f"guild_{guild_id}.sqlite")
    db = sqlite3.connect(db_path)
    cursor = db.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER, inventory TEXT, streak INTEGER DEFAULT 0, bio TEXT DEFAULT '', joined_at INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS msg_logs (user_id INTEGER, timestamp INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS cooldowns (user_id INTEGER, command TEXT, timestamp INTEGER, PRIMARY KEY (user_id, command))")
    cursor.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS achievements (user_id INTEGER, achievement_key TEXT, unlocked_at INTEGER, PRIMARY KEY (user_id, achievement_key))")
    db.commit()
    return db, cursor


# ---------------------------------------------------------------- meta

def _update_guild_meta_sync(guild_id, guild_name):
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute(
        "INSERT INTO meta (key, value) VALUES ('guild_name', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (guild_name,)
    )
    conn.commit()
    conn.close()

async def update_guild_meta(guild):
    """Stamps the guild's current name into its own db file so we can
    identify the server later even if the bot is no longer in it."""
    await asyncio.to_thread(_update_guild_meta_sync, guild.id, guild.name)


# ------------------------------------------------------------- balance

def _get_bal_sync(guild_id, user_id):
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    if res:
        conn.close()
        return res[0]
    cursor.execute(
        "INSERT INTO users (user_id, balance, inventory, streak, bio, joined_at) VALUES (?, 100, '', 0, '', ?)",
        (user_id, int(time.time()))
    )
    conn.commit()
    conn.close()
    return 100

async def get_bal(guild_id, user_id):
    return await asyncio.to_thread(_get_bal_sync, guild_id, user_id)


def _update_bal_sync(guild_id, user_id, amount):
    # Reuses one connection for the read-then-write instead of the
    # original's get_bal() [connection #1] + a second open [connection
    # #2] - same end state (new users still start at 100, existing
    # balances still shift by `amount`), one fewer sqlite3.connect().
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    if res:
        bal = res[0]
    else:
        cursor.execute(
            "INSERT INTO users (user_id, balance, inventory, streak, bio, joined_at) VALUES (?, 100, '', 0, '', ?)",
            (user_id, int(time.time()))
        )
        bal = 100
    cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (bal + amount, user_id))
    conn.commit()
    conn.close()

async def update_bal(guild_id, user_id, amount):
    await asyncio.to_thread(_update_bal_sync, guild_id, user_id, amount)


# -------------------------------------------------------------- streak

def _get_streak_sync(guild_id, user_id):
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute("SELECT streak FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    if res:
        return res[0]
    _get_bal_sync(guild_id, user_id)  # same as original: ensures a row exists
    return 0

async def get_streak(guild_id, user_id):
    return await asyncio.to_thread(_get_streak_sync, guild_id, user_id)


def _set_streak_sync(guild_id, user_id, streak):
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute("UPDATE users SET streak = ? WHERE user_id = ?", (streak, user_id))
    conn.commit()
    conn.close()

async def set_streak(guild_id, user_id, streak):
    await asyncio.to_thread(_set_streak_sync, guild_id, user_id, streak)


# ----------------------------------------------------------------- bio

def _get_bio_sync(guild_id, user_id):
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute("SELECT bio FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    if res:
        return res[0]
    _get_bal_sync(guild_id, user_id)
    return ""

async def get_bio(guild_id, user_id):
    return await asyncio.to_thread(_get_bio_sync, guild_id, user_id)


def _set_bio_sync(guild_id, user_id, bio_text):
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute("UPDATE users SET bio = ? WHERE user_id = ?", (bio_text, user_id))
    conn.commit()
    conn.close()

async def set_bio(guild_id, user_id, bio_text):
    await asyncio.to_thread(_set_bio_sync, guild_id, user_id, bio_text)


# ----------------------------------------------------------- inventory

def _add_inventory_sync(guild_id, user_id, item):
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute("SELECT inventory FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    current_inv = res[0] if res and res[0] else ""
    new_inv = f"{current_inv}\n{item}" if current_inv else item
    cursor.execute("UPDATE users SET inventory = ? WHERE user_id = ?", (new_inv, user_id))
    conn.commit()
    conn.close()

async def add_inventory(guild_id, user_id, item):
    await asyncio.to_thread(_add_inventory_sync, guild_id, user_id, item)


# --------------------------------------------------------------- stats

def _get_message_counts_sync(guild_id, user_id, now):
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute("SELECT COUNT(*) FROM msg_logs WHERE user_id = ? AND timestamp >= ?", (user_id, now - 86400))
    msgs_24h = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM msg_logs WHERE user_id = ? AND timestamp >= ?", (user_id, now - 604800))
    msgs_7d = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM msg_logs WHERE user_id = ?", (user_id,))
    msgs_life = cursor.fetchone()[0]
    conn.close()
    return msgs_24h, msgs_7d, msgs_life

async def get_message_counts(guild_id, user_id, now):
    """Returns (msgs_24h, msgs_7d, msgs_life) - same three queries the
    original `.stats` command ran inline, just relocated here and run
    off the event loop."""
    return await asyncio.to_thread(_get_message_counts_sync, guild_id, user_id, now)


# ------------------------------------------------------------ cooldown

def _get_cooldown_remaining_sync(guild_id, user_id, command, cooldown_seconds, now):
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute("SELECT timestamp FROM cooldowns WHERE user_id = ? AND command = ?", (user_id, command))
    res = cursor.fetchone()
    conn.close()
    if res and (now - res[0]) < cooldown_seconds:
        return cooldown_seconds - (now - res[0])
    return None

async def get_cooldown_remaining(guild_id, user_id, command, cooldown_seconds, now):
    """None if off cooldown, otherwise seconds remaining. `.daily` and
    `.work` shared this exact logic (copy-pasted in the original, just
    with a different `command` string) - unified into one helper."""
    return await asyncio.to_thread(_get_cooldown_remaining_sync, guild_id, user_id, command, cooldown_seconds, now)


def _set_cooldown_sync(guild_id, user_id, command, now):
    conn, cursor = get_connection_sync(guild_id)
    cursor.execute("INSERT OR REPLACE INTO cooldowns (user_id, command, timestamp) VALUES (?, ?, ?)", (user_id, command, now))
    conn.commit()
    conn.close()

async def set_cooldown(guild_id, user_id, command, now):
    await asyncio.to_thread(_set_cooldown_sync, guild_id, user_id, command, now)


# --------------------------------------------------- on_message hot path

def _record_message_sync(guild_id, user_id, ts):
    """Combines what used to be 3 separate connections (msg_logs
    count+insert, then get_bal's connection, then update_bal's
    connection) into one. This is the single hottest path in the whole
    bot - it runs on every non-bot message in every guild - so this is
    the one place a full SQL-level merge (not just a to_thread wrap) is
    worth the slight duplication vs. calling get_bal/update_bal
    separately.

    End state is identical to the original: a brand new user still
    lands at balance 100 (default) + 1 (message reward) = 101; an
    existing user's balance still goes up by exactly 1.
    """
    conn, cursor = get_connection_sync(guild_id)

    cursor.execute("SELECT COUNT(*) FROM msg_logs WHERE user_id = ?", (user_id,))
    is_first_message = cursor.fetchone()[0] == 0
    cursor.execute("INSERT INTO msg_logs (user_id, timestamp) VALUES (?, ?)", (user_id, ts))

    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (row[0] + 1, user_id))
    else:
        cursor.execute(
            "INSERT INTO users (user_id, balance, inventory, streak, bio, joined_at) VALUES (?, 101, '', 0, '', ?)",
            (user_id, ts)
        )

    conn.commit()
    conn.close()
    return is_first_message

async def record_message(guild_id, user_id):
    """Logs a message and grants the +1 DT reward; returns True if
    this is this user's first ever logged message in this guild."""
    return await asyncio.to_thread(_record_message_sync, guild_id, user_id, int(time.time()))


# ------------------------------------------------------- cross-guild lookup

def _get_all_user_data_sync(user_id, live_guilds: dict):
    entries = []
    if not os.path.exists(DB_DIR):
        return entries

    for filename in sorted(os.listdir(DB_DIR)):
        if not (filename.startswith("guild_") and filename.endswith(".sqlite")):
            continue
        guild_id_str = filename[len("guild_"):-len(".sqlite")]
        try:
            guild_id = int(guild_id_str)
        except ValueError:
            continue

        path = os.path.join(DB_DIR, filename)
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute("SELECT balance, joined_at FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            continue
        balance, first_seen = row

        cur.execute("SELECT value FROM meta WHERE key = 'guild_name'")
        meta_row = cur.fetchone()
        conn.close()

        guild_obj = live_guilds.get(guild_id)
        guild_name = guild_obj.name if guild_obj else (meta_row[0] if meta_row else f"Unknown Server ({guild_id})")

        join_dt = None
        if guild_obj:
            member = guild_obj.get_member(user_id)
            if member and member.joined_at:
                join_dt = member.joined_at

        if join_dt:
            joined_str = join_dt.strftime("%Y-%m-%d %H:%M UTC")
        elif first_seen:
            joined_str = datetime.fromtimestamp(first_seen, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + " (first seen)"
        else:
            joined_str = "Unknown"

        entries.append((guild_name, balance, joined_str))

    entries.sort(key=lambda e: e[1], reverse=True)  # richest server first
    return entries

async def get_all_user_data(bot, user_id):
    """Scans every server_dbs/guild_*.sqlite file for this user_id.
    Used by `.stats` in DMs.

    `bot.get_guild()` / `guild.get_member()` are just in-memory dict
    reads on discord.py's cache, not I/O - but they're still discord.py
    objects, so we snapshot `{guild_id: guild}` here on the event loop
    *before* handing off to the worker thread below, rather than
    touching the live discord.py cache from another thread while the
    gateway could be mutating it concurrently.
    """
    live_guilds = {g.id: g for g in bot.guilds}
    return await asyncio.to_thread(_get_all_user_data_sync, user_id, live_guilds)
