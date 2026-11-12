"""
Trigger-channel watcher: a honeypot-style channel where ANY message
gets deleted. A message that looks like scam/nitro-bait (4+ photos at
once, or a flagged keyword in any leetspeak/spaced-out/accented form)
gets its sender permanently jailed and every message they sent in that
channel in the last hour wiped; anything else just gets deleted with a
DM'd warning.

Depends on cogs/moderation.py being loaded first (reuses its jail
role, jail record storage, DM-embed builder, and registered mod/jail
channel settings) - set a jail channel there with ,set jail <id>
before this will actually be able to jail anyone.

Set the watched channel with the /set_trigger_channel slash command
(requires Manage Server). Setting it immediately posts the warning
message in that channel.

Data is stored at data/trigger_channels.json (guild_id -> channel_id),
next to moderation.py's other data files.
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.moderation import (
    _load_json, _save_json, _build_dm_embed, _dm_safely,
    get_mod_channel, get_jail_channel, set_jail_record,
    WARN_COLOR, JAIL_COLOR, DATA_DIR,
)
import os

TRIGGER_CHANNELS_FILE = os.path.join(DATA_DIR, "trigger_channels.json")

WARN_REASON = "automated action: dont send any messages in the trigger channel please."
TRIGGER_CHANNEL_NOTICE = "this is the trigger channel, do not send any messages here please, or you will be punished..."

TRIGGER_KEYWORDS = ("bio", "money", "nitro", "free", "status")
PHOTO_FLOOD_THRESHOLD = 4
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")

# Folds common evasion tricks (leetspeak digits/symbols, accents and
# other unicode confusables, spacing/punctuation used to break up a
# word) down to plain lowercase letters before matching. Not a full
# confusables database - covers the common cases, not every unicode
# lookalike in existence.
_LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "!": "i", "|": "l", "3": "e", "4": "a", "@": "a",
    "5": "s", "$": "s", "7": "t", "+": "t", "8": "b", "9": "g",
})


def _normalize_for_matching(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().translate(_LEET_MAP)
    return re.sub(r"[^a-z]", "", text)


def contains_trigger_keyword(text: str) -> bool:
    normalized = _normalize_for_matching(text or "")
    return any(keyword in normalized for keyword in TRIGGER_KEYWORDS)


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def get_trigger_channel(guild_id: int):
    return _load_json(TRIGGER_CHANNELS_FILE, {}).get(str(guild_id))


def set_trigger_channel_id(guild_id: int, channel_id: int):
    data = _load_json(TRIGGER_CHANNELS_FILE, {})
    data[str(guild_id)] = channel_id
    _save_json(TRIGGER_CHANNELS_FILE, data)


class TriggerChannel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------ setup

    @app_commands.command(name="set_trigger_channel", description="Set the channel this bot watches for scam/nitro-bait messages")
    @app_commands.describe(channel="The channel to watch")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_trigger_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        set_trigger_channel_id(interaction.guild_id, channel.id)
        try:
            await channel.send(TRIGGER_CHANNEL_NOTICE)
        except discord.HTTPException:
            pass
        await interaction.response.send_message(f"✅ Trigger channel set to {channel.mention}.", ephemeral=True)

    # ------------------------------------------------------------ watcher

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.id == self.bot.user.id:
            return
        trigger_channel_id = get_trigger_channel(message.guild.id)
        if not trigger_channel_id or message.channel.id != trigger_channel_id:
            return

        image_count = sum(1 for a in message.attachments if _is_image_attachment(a))
        is_scam = image_count >= PHOTO_FLOOD_THRESHOLD or contains_trigger_keyword(message.content)

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        if is_scam:
            reason = "Automated action: posted scam-pattern content (4+ photos at once, or a flagged keyword) in the trigger channel."
            jailed = await self._perma_jail(message.guild, message.author, reason)
            since = datetime.now(timezone.utc) - timedelta(hours=1)
            await self._delete_recent_messages(message.channel, message.author.id, since)
            if jailed:
                await self._log_to_mod_channel(
                    message.guild,
                    f"🔒 Permanently jailed {message.author.mention} - scam pattern detected in the trigger channel, "
                    f"deleted everything they'd sent there in the past hour.",
                )
            else:
                await self._log_to_mod_channel(
                    message.guild,
                    f"⚠️ {message.author.mention} tripped the scam-pattern check in the trigger channel, but I "
                    f"couldn't jail them - check that `,set jail <channel id>` is configured and my role is high "
                    f"enough.",
                )
        else:
            await self._auto_warn(message.guild, message.author, WARN_REASON)
            await self._log_to_mod_channel(
                message.guild, f"⚠️ Warned {message.author.mention} - sent a message in the trigger channel."
            )

    # ------------------------------------------------------------ actions

    async def _perma_jail(self, guild: discord.Guild, member: discord.Member, reason: str) -> bool:
        """Jails member permanently using moderation.py's own jail role
        and record storage, so ,unjail and the rest of that system
        still work on them normally. Returns True if the jail actually
        went through."""
        mod_cog = self.bot.get_cog("Moderation")
        jail_channel_id = get_jail_channel(guild.id)
        if mod_cog is None or not jail_channel_id:
            return False
        jail_channel = guild.get_channel(jail_channel_id)
        if not jail_channel:
            return False

        jail_role = await mod_cog._get_or_create_jail_role(guild)
        if jail_role in member.roles:
            return True  # already jailed - nothing more to do
        await mod_cog._lock_down_jail_role(guild, jail_channel)

        original_role_ids = [r.id for r in member.roles if not r.is_default()]
        try:
            await member.edit(roles=[jail_role], reason=reason)
        except discord.Forbidden:
            return False

        set_jail_record(guild.id, member.id, {
            "original_role_ids": original_role_ids,
            "unjail_at": None,  # permanent
            "reason": reason,
        })

        embed = _build_dm_embed(
            title="Jailed", color=JAIL_COLOR, guild=guild, moderator=guild.me, reason=reason,
            location_label="You have been jailed in", extra_fields=[("Duration", "permanent")],
        )
        await _dm_safely(member, embed)
        return True

    async def _auto_warn(self, guild: discord.Guild, member: discord.Member, reason: str):
        embed = _build_dm_embed(
            title="Warned", color=WARN_COLOR, guild=guild, moderator=guild.me, reason=reason,
            location_label="You have been warned in",
        )
        await _dm_safely(member, embed)

    async def _delete_recent_messages(self, channel: discord.TextChannel, user_id: int, since: datetime):
        try:
            await channel.purge(limit=500, after=since, check=lambda m: m.author.id == user_id)
        except discord.HTTPException:
            pass

    async def _log_to_mod_channel(self, guild: discord.Guild, text: str):
        mod_channel_id = get_mod_channel(guild.id)
        if not mod_channel_id:
            return
        channel = guild.get_channel(mod_channel_id)
        if channel:
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(TriggerChannel(bot))
