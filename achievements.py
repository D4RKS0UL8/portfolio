"""Achievement definitions + unlock/announce logic.

Split out from database.py because this is domain logic (what an
achievement *is*, when to announce it) layered on top of generic
per-guild storage, not storage itself.
"""
import asyncio
import time

import discord

from utils import database as db

# key -> (display name, flavor text shown in the unlock embed)
ACHIEVEMENTS = {
    "first_win":         ("First Win",      "the thrill of winning over and over again..."),
    "first_loss":        ("First Loss",     "you can feel the addiction... can't you?"),
    "natural_21_win":    ("Natural 21",     "the universe is on my side"),
    "natural_21_dealer": ("Dealer's 21",    "bad luck indeed"),
    "first_message":     ("New Here",       "i'm new here"),
}
TOTAL_ACHIEVEMENTS = 20  # denominator shown in unlock messages; room to add more later


def _unlock_achievement_sync(guild_id, user_id, key):
    conn, cursor = db.get_connection_sync(guild_id)
    cursor.execute(
        "INSERT OR IGNORE INTO achievements (user_id, achievement_key, unlocked_at) VALUES (?, ?, ?)",
        (user_id, key, int(time.time()))
    )
    newly_unlocked = cursor.rowcount > 0
    conn.commit()
    count = None
    if newly_unlocked:
        cursor.execute("SELECT COUNT(*) FROM achievements WHERE user_id = ?", (user_id,))
        count = cursor.fetchone()[0]
    conn.close()
    return newly_unlocked, count

async def unlock_achievement(guild_id, user_id, key):
    """Records an achievement for a user if they don't already have it.
    Safe to call unconditionally on every relevant event - it's a no-op
    if already unlocked. Returns (newly_unlocked: bool, total_count: int|None)."""
    return await asyncio.to_thread(_unlock_achievement_sync, guild_id, user_id, key)


async def send_achievement_unlock(channel, member, key):
    """Sends the achievement-unlock ping as its own separate message
    (never merged into a game-result embed), but only if this is the
    first time this user has earned this specific achievement."""
    newly_unlocked, count = await unlock_achievement(channel.guild.id, member.id, key)
    if not newly_unlocked:
        return
    name, flavor = ACHIEVEMENTS[key]
    embed = discord.Embed(title=f"🏆 {name}", description=f"*{flavor}*", color=discord.Color.gold())
    embed.set_thumbnail(url=member.display_avatar.url)
    await channel.send(
        content=f"Hey {member.mention}! New Achievement Unlocked: \u201c__{name}__\u201d\nAchievements unlocked: {count}/{TOTAL_ACHIEVEMENTS}",
        embed=embed
    )


def _bulk_first_message_unlock_sync(guild_id, user_ids):
    conn, cursor = db.get_connection_sync(guild_id)
    ts = int(time.time())
    cursor.executemany(
        "INSERT OR IGNORE INTO achievements (user_id, achievement_key, unlocked_at) VALUES (?, 'first_message', ?)",
        [(uid, ts) for uid in user_ids]
    )
    conn.commit()
    conn.close()

async def bulk_first_message_unlock(guild_id, user_ids):
    """Used by on_guild_join. The original called unlock_achievement()
    once per existing member - for a large server that's one sqlite
    connection opened and closed per member on a single gateway event.
    This grants the same achievement to the same set of people over one
    connection instead. Doesn't send individual announcement embeds,
    matching the original (on_guild_join only ever posted the one
    summary message, never per-member achievement pings)."""
    if not user_ids:
        return
    await asyncio.to_thread(_bulk_first_message_unlock_sync, guild_id, user_ids)
