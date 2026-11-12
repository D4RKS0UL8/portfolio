"""
Moderation cog: ban, timeout, warn, jail/unjail, and role management.

Requires your bot's command_prefix to include "," (these are all
comma-prefixed text commands, not slash commands) and these intents:
    intents.members = True
    intents.message_content = True

Drop this file at cogs/moderation.py and make sure your bot loads it
the same way it loads cogs/fun.py (extension name "cogs.moderation").

Data is stored as JSON under data/ next to wherever the bot is run
from: data/mod_channels.json, data/jail_channels.json, data/jails.json,
data/bans.json.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

DATA_DIR = "data"
MOD_CHANNELS_FILE = os.path.join(DATA_DIR, "mod_channels.json")
JAIL_CHANNELS_FILE = os.path.join(DATA_DIR, "jail_channels.json")
JAILS_FILE = os.path.join(DATA_DIR, "jails.json")
BANS_FILE = os.path.join(DATA_DIR, "bans.json")

JAIL_ROLE_NAME = "jail"
JAIL_ROLE_COLOR = discord.Color.red()

WARN_COLOR = discord.Color.gold()
JAIL_COLOR = discord.Color.red()
UNJAIL_COLOR = discord.Color.green()
BAN_COLOR = discord.Color.red()
TIMEOUT_COLOR = discord.Color.orange()
KICK_COLOR = discord.Color.dark_orange()

DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DURATION_RE = re.compile(r'^(\d+)([smhd])$', re.IGNORECASE)

# A role ID is always a Discord snowflake (17-19 digits in practice).
# Anything numeric shorter than that is read as a rank number instead
# - see _resolve_role.
SNOWFLAKE_MIN_DIGITS = 15

ROLES_PER_PAGE = 10


# --------------------------------------------------------------- storage

def _load_json(path, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # atomic on POSIX - never leaves a half-written file


def get_mod_channel(guild_id: int):
    return _load_json(MOD_CHANNELS_FILE, {}).get(str(guild_id))


def set_mod_channel(guild_id: int, channel_id: int):
    data = _load_json(MOD_CHANNELS_FILE, {})
    data[str(guild_id)] = channel_id
    _save_json(MOD_CHANNELS_FILE, data)


def get_jail_channel(guild_id: int):
    return _load_json(JAIL_CHANNELS_FILE, {}).get(str(guild_id))


def set_jail_channel(guild_id: int, channel_id: int):
    data = _load_json(JAIL_CHANNELS_FILE, {})
    data[str(guild_id)] = channel_id
    _save_json(JAIL_CHANNELS_FILE, data)


def _jail_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def get_jail_record(guild_id: int, user_id: int):
    return _load_json(JAILS_FILE, {}).get(_jail_key(guild_id, user_id))


def set_jail_record(guild_id: int, user_id: int, record: dict):
    data = _load_json(JAILS_FILE, {})
    data[_jail_key(guild_id, user_id)] = record
    _save_json(JAILS_FILE, data)


def delete_jail_record(guild_id: int, user_id: int):
    data = _load_json(JAILS_FILE, {})
    data.pop(_jail_key(guild_id, user_id), None)
    _save_json(JAILS_FILE, data)


def get_ban_record(guild_id: int, user_id: int):
    return _load_json(BANS_FILE, {}).get(_jail_key(guild_id, user_id))


def set_ban_record(guild_id: int, user_id: int, record: dict):
    data = _load_json(BANS_FILE, {})
    data[_jail_key(guild_id, user_id)] = record
    _save_json(BANS_FILE, data)


def delete_ban_record(guild_id: int, user_id: int):
    data = _load_json(BANS_FILE, {})
    data.pop(_jail_key(guild_id, user_id), None)
    _save_json(BANS_FILE, data)


# ------------------------------------------------------------- parsing

def parse_duration_and_reason(argstring: str):
    """Splits "<duration> <reason...>" into (seconds_or_None, reason).
    The first whitespace-separated token is checked against
    DURATION_RE ("<number><s|m|h|d>"); if it matches, that token is
    consumed as the duration and everything after it is the reason.
    If it does NOT match - including something that merely looks
    duration-ish, like "1ds" - duration comes back None (meaning
    permanent) and the *entire* original string (the bad token
    included) becomes the reason, rather than silently dropping it.
    """
    argstring = (argstring or "").strip()
    if not argstring:
        return None, "No reason provided"

    parts = argstring.split(maxsplit=1)
    m = DURATION_RE.match(parts[0])
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        seconds = amount * DURATION_UNITS[unit]
        reason = parts[1].strip() if len(parts) > 1 else "No reason provided"
        return seconds, reason

    return None, argstring


def format_duration(seconds):
    if seconds is None:
        return "permanent"
    units = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
    for suffix, size in units:
        if seconds % size == 0:
            return f"{seconds // size}{suffix}"
    return f"{seconds}s"


# ------------------------------------------------------------- resolving

async def resolve_member(ctx: commands.Context, raw: str):
    """Resolves a user ID, a @mention, or a username/display name to a
    Member of ctx.guild. Returns None (and lets the caller report it)
    if nothing matches."""
    raw = raw.strip().strip("<@!>")
    if raw.isdigit():
        member = ctx.guild.get_member(int(raw))
        if member:
            return member
        try:
            return await ctx.guild.fetch_member(int(raw))
        except discord.HTTPException:
            return None
    lowered = raw.lower()
    for member in ctx.guild.members:
        if member.name.lower() == lowered or member.display_name.lower() == lowered:
            return member
    return None


def _resolve_role(guild: discord.Guild, raw: str):
    """Resolves a role by ID, by rank number (1 = guild's highest role,
    below @everyone - matches the order Discord's own role list shows
    top to bottom), or by name (case-insensitive). Returns None if
    nothing matches."""
    raw = raw.strip().strip("<@&>")
    ranked = [r for r in sorted(guild.roles, key=lambda r: r.position, reverse=True) if not r.is_default()]

    if raw.isdigit():
        if len(raw) >= SNOWFLAKE_MIN_DIGITS:
            role = guild.get_role(int(raw))
            if role:
                return role
        else:
            rank = int(raw)
            if 1 <= rank <= len(ranked):
                return ranked[rank - 1]
        return None

    lowered = raw.lower()
    for role in ranked:
        if role.name.lower() == lowered:
            return role
    return None


# --------------------------------------------------------------- embeds

def _build_dm_embed(*, title: str, color: discord.Color, guild: discord.Guild,
                     moderator: discord.Member, reason: str, location_label: str = "Server",
                     extra_fields=None):
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name=location_label, value=guild.name, inline=False)
    if extra_fields:
        for name, value in extra_fields:
            embed.add_field(name=name, value=value, inline=False)
    embed.add_field(name="Moderator", value=moderator.display_name, inline=False)
    embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


async def _dm_safely(member: discord.Member, embed: discord.Embed) -> bool:
    try:
        await member.send(embed=embed)
        return True
    except discord.HTTPException:
        return False  # DMs closed - the mod action still goes through, just silently skips the DM


# -------------------------------------------------------------- role list view

class RoleListView(discord.ui.View):
    def __init__(self, guild: discord.Guild, pages: list):
        super().__init__(timeout=120)
        self.guild = guild
        self.pages = pages
        self.index = 0
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_button.disabled = self.index == 0
        self.next_button.disabled = self.index >= len(self.pages) - 1

    def current_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"Roles - {self.guild.name}",
            description=self.pages[self.index],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Page {self.index + 1}/{len(self.pages)}")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)


# ----------------------------------------------------------------- cog

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.unjail_loop.start()
        self.unban_loop.start()

    def cog_unload(self):
        self.unjail_loop.cancel()
        self.unban_loop.cancel()

    # ---- shared helper: reminds the moderator if they're not in the registered mod-cmds channel
    async def _remind_if_wrong_channel(self, ctx: commands.Context):
        mod_channel_id = get_mod_channel(ctx.guild.id)
        if mod_channel_id and ctx.channel.id != mod_channel_id:
            await ctx.send(f"ℹ️ Reminder: <#{mod_channel_id}> is the registered channel for mod commands.")

    # ------------------------------------------------------------ setup

    @commands.command(name="set")
    @commands.has_permissions(manage_guild=True)
    async def set_cmd(self, ctx: commands.Context, kind: str, *, value: str = None):
        """,set mod cmds  -> registers the current channel for mod command replies
        ,set jail <channel id>  -> sets the jail channel"""
        kind = (kind or "").lower()
        if kind == "mod" and (value or "").lower().strip() == "cmds":
            set_mod_channel(ctx.guild.id, ctx.channel.id)
            return await ctx.send(f"✅ Mod commands are now registered to {ctx.channel.mention}.")

        if kind == "jail":
            if not value or not value.strip().isdigit():
                return await ctx.send("❌ Usage: `,set jail <jail channel numerical id>`")
            channel = ctx.guild.get_channel(int(value.strip()))
            if not channel:
                return await ctx.send("❌ Couldn't find a channel with that ID in this server.")
            set_jail_channel(ctx.guild.id, channel.id)
            await self._lock_down_jail_role(ctx.guild, channel)
            return await ctx.send(f"✅ Jail channel set to {channel.mention}.")

        await ctx.send("❌ Usage: `,set mod cmds` or `,set jail <channel id>`")

    async def _get_or_create_jail_role(self, guild: discord.Guild) -> discord.Role:
        role = discord.utils.get(guild.roles, name=JAIL_ROLE_NAME)
        if role:
            return role
        return await guild.create_role(
            name=JAIL_ROLE_NAME, color=JAIL_ROLE_COLOR, permissions=discord.Permissions.none(),
            reason="Auto-created for the jail system",
        )

    async def _lock_down_jail_role(self, guild: discord.Guild, jail_channel: discord.abc.GuildChannel):
        """Denies the jail role view access on every channel except the
        jail channel, so a jailed member really can only see that one
        channel regardless of how @everyone's permissions are set up."""
        role = await self._get_or_create_jail_role(guild)
        for channel in guild.channels:
            try:
                if channel.id == jail_channel.id:
                    await channel.set_permissions(role, view_channel=True, send_messages=True)
                else:
                    await channel.set_permissions(role, view_channel=False)
            except discord.Forbidden:
                continue  # missing perms on that one channel - skip it rather than fail the whole command

    # ------------------------------------------------------------ ban

    @commands.command(name="ban")
    @commands.has_permissions(ban_members=True)
    async def ban_cmd(self, ctx: commands.Context, user: str, *, args: str = None):
        """,ban <member> [duration] [reason] - duration is optional;
        omit it (or give something that doesn't parse as a duration)
        for a permanent ban, same fallback rule as jail."""
        member = await resolve_member(ctx, user)
        if not member:
            return await ctx.send("❌ Couldn't find that user in this server.")
        seconds, reason = parse_duration_and_reason(args)

        embed = _build_dm_embed(
            title="Banned", color=BAN_COLOR, guild=ctx.guild, moderator=ctx.author, reason=reason,
            location_label="You have been banned from", extra_fields=[("Duration", format_duration(seconds))],
        )
        # DM before the ban goes through - safest point to still reach them.
        dmed = await _dm_safely(member, embed)

        try:
            await ctx.guild.ban(member, reason=f"{reason} (by {ctx.author})")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to ban that user (check role position).")

        if seconds:
            unban_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            set_ban_record(ctx.guild.id, member.id, {"unban_at": unban_at, "reason": reason})

        note = "" if dmed else " (couldn't DM them - their DMs are likely closed)"
        await ctx.send(f"🔨 Banned **{member}**{note} - {format_duration(seconds)} - {reason}")
        await self._remind_if_wrong_channel(ctx)

    @tasks.loop(seconds=30)
    async def unban_loop(self):
        data = _load_json(BANS_FILE, {})
        now = datetime.now(timezone.utc)
        for key, record in list(data.items()):
            if datetime.fromisoformat(record["unban_at"]) > now:
                continue
            guild_id_s, user_id_s = key.split(":", 1)
            guild = self.bot.get_guild(int(guild_id_s))
            if not guild:
                continue
            try:
                await guild.unban(discord.Object(id=int(user_id_s)), reason="Temporary ban expired")
            except discord.HTTPException:
                pass  # already unbanned/not banned - fine, still clear the record below
            delete_ban_record(int(guild_id_s), int(user_id_s))

    @unban_loop.before_loop
    async def _before_unban_loop(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ timeout

    @commands.command(name="timeout")
    @commands.has_permissions(moderate_members=True)
    async def timeout_cmd(self, ctx: commands.Context, user: str, *, args: str = None):
        member = await resolve_member(ctx, user)
        if not member:
            return await ctx.send("❌ Couldn't find that user in this server.")
        seconds, reason = parse_duration_and_reason(args)
        if seconds is None:
            return await ctx.send("❌ Timeout needs a duration (Discord doesn't support permanent timeouts) - e.g. `,timeout @user 1h spamming`")
        seconds = min(seconds, 28 * 86400)  # Discord's own hard cap

        embed = _build_dm_embed(
            title="Timed Out", color=TIMEOUT_COLOR, guild=ctx.guild, moderator=ctx.author, reason=reason,
            location_label="You have been timed out in", extra_fields=[("Duration", format_duration(seconds))],
        )
        dmed = await _dm_safely(member, embed)

        try:
            await member.timeout(timedelta(seconds=seconds), reason=f"{reason} (by {ctx.author})")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to time out that user (check role position).")
        note = "" if dmed else " (couldn't DM them - their DMs are likely closed)"
        await ctx.send(f"⏱️ Timed out **{member}**{note} for {format_duration(seconds)} - {reason}")
        await self._remind_if_wrong_channel(ctx)

    # ------------------------------------------------------------ kick

    @commands.command(name="kick")
    @commands.has_permissions(kick_members=True)
    async def kick_cmd(self, ctx: commands.Context, user: str, *, reason: str = None):
        member = await resolve_member(ctx, user)
        if not member:
            return await ctx.send("❌ Couldn't find that user in this server.")
        reason = reason or "No reason provided"

        embed = _build_dm_embed(
            title="Kicked", color=KICK_COLOR, guild=ctx.guild, moderator=ctx.author, reason=reason,
            location_label="You have been kicked from",
        )
        dmed = await _dm_safely(member, embed)

        try:
            await member.kick(reason=f"{reason} (by {ctx.author})")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to kick that user (check role position).")
        note = "" if dmed else " (couldn't DM them - their DMs are likely closed)"
        await ctx.send(f"👢 Kicked **{member}**{note} - {reason}")
        await self._remind_if_wrong_channel(ctx)

    # ------------------------------------------------------------ warn

    @commands.command(name="warn")
    @commands.has_permissions(kick_members=True)
    async def warn_cmd(self, ctx: commands.Context, user: str, *, reason: str = None):
        member = await resolve_member(ctx, user)
        if not member:
            return await ctx.send("❌ Couldn't find that user in this server.")
        reason = reason or "No reason provided"

        embed = _build_dm_embed(
            title="Warned", color=WARN_COLOR, guild=ctx.guild, moderator=ctx.author, reason=reason,
            location_label="You have been warned in",
        )
        dmed = await _dm_safely(member, embed)

        try:
            await ctx.message.add_reaction("👍")
        except discord.HTTPException:
            pass
        note = "" if dmed else " (couldn't DM them - their DMs are likely closed)"
        await ctx.send(f"⚠️ Warned **{member}**{note} - {reason}")
        await self._remind_if_wrong_channel(ctx)

    # ------------------------------------------------------------ jail / unjail

    @commands.command(name="jail")
    @commands.has_permissions(kick_members=True)
    async def jail_cmd(self, ctx: commands.Context, user: str, *, args: str = None):
        jail_channel_id = get_jail_channel(ctx.guild.id)
        if not jail_channel_id:
            return await ctx.send("❌ No jail channel set yet - run `,set jail <channel id>` first.")
        jail_channel = ctx.guild.get_channel(jail_channel_id)
        if not jail_channel:
            return await ctx.send("❌ The registered jail channel no longer exists - run `,set jail` again.")

        member = await resolve_member(ctx, user)
        if not member:
            return await ctx.send("❌ Couldn't find that user in this server.")

        seconds, reason = parse_duration_and_reason(args)
        jail_role = await self._get_or_create_jail_role(ctx.guild)
        await self._lock_down_jail_role(ctx.guild, jail_channel)  # keep it current, cheap to re-run

        if jail_role in member.roles:
            return await ctx.send(f"❌ **{member}** is already jailed.")

        original_role_ids = [r.id for r in member.roles if not r.is_default()]
        try:
            await member.edit(roles=[jail_role], reason=f"Jailed by {ctx.author}: {reason}")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to edit that member's roles (check role position).")

        unjail_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat() if seconds else None
        set_jail_record(ctx.guild.id, member.id, {
            "original_role_ids": original_role_ids,
            "unjail_at": unjail_at,
            "reason": reason,
        })

        embed = _build_dm_embed(
            title="Jailed", color=JAIL_COLOR, guild=ctx.guild, moderator=ctx.author, reason=reason,
            location_label="You have been jailed in", extra_fields=[("Duration", format_duration(seconds))],
        )
        dmed = await _dm_safely(member, embed)

        try:
            await ctx.message.add_reaction("👍")
        except discord.HTTPException:
            pass
        note = "" if dmed else " (couldn't DM them - their DMs are likely closed)"
        await ctx.send(f"🔒 Jailed **{member}**{note} - {format_duration(seconds)} - {reason}")
        await self._remind_if_wrong_channel(ctx)

    @commands.command(name="unjail")
    @commands.has_permissions(kick_members=True)
    async def unjail_cmd(self, ctx: commands.Context, user: str, *, reason: str = None):
        member = await resolve_member(ctx, user)
        if not member:
            return await ctx.send("❌ Couldn't find that user in this server.")
        await self._do_unjail(ctx.guild, member, reason or "Manually lifted", moderator=ctx.author)
        await ctx.send(f"🔓 Unjailed **{member}**.")
        await self._remind_if_wrong_channel(ctx)

    async def _do_unjail(self, guild: discord.Guild, member: discord.Member, reason: str, moderator=None):
        record = get_jail_record(guild.id, member.id)
        jail_role = discord.utils.get(guild.roles, name=JAIL_ROLE_NAME)

        restored_roles = []
        if record:
            for role_id in record.get("original_role_ids", []):
                role = guild.get_role(role_id)
                if role:
                    restored_roles.append(role)
        try:
            await member.edit(roles=restored_roles, reason="Unjailed")
        except discord.Forbidden:
            pass  # best-effort - still clear the jail record below so it doesn't loop forever

        delete_jail_record(guild.id, member.id)

        embed = _build_dm_embed(
            title="Lifted Timeout", color=UNJAIL_COLOR, guild=guild,
            moderator=moderator or guild.me, reason=reason, location_label="You are no longer jailed in",
        )
        await _dm_safely(member, embed)

    @tasks.loop(seconds=30)
    async def unjail_loop(self):
        data = _load_json(JAILS_FILE, {})
        now = datetime.now(timezone.utc)
        for key, record in list(data.items()):
            unjail_at = record.get("unjail_at")
            if not unjail_at:
                continue  # permanent
            if datetime.fromisoformat(unjail_at) > now:
                continue
            guild_id_s, user_id_s = key.split(":", 1)
            guild = self.bot.get_guild(int(guild_id_s))
            if not guild:
                continue
            member = guild.get_member(int(user_id_s))
            if not member:
                delete_jail_record(int(guild_id_s), int(user_id_s))
                continue
            await self._do_unjail(guild, member, "Jail duration expired")

    @unjail_loop.before_loop
    async def _before_unjail_loop(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ roles

    @commands.group(name="r", invoke_without_command=True)
    async def role_group(self, ctx: commands.Context):
        await ctx.send("Usage: `,r create <name> <color>` / `,r list` / `,r add <member> <role>` / `,r remove <member> <role>`")

    @role_group.command(name="create")
    @commands.has_permissions(manage_roles=True)
    async def role_create(self, ctx: commands.Context, name: str, color: str):
        hex_str = color.strip().lstrip("#")
        try:
            color_int = int(hex_str, 16)
        except ValueError:
            return await ctx.send("❌ That's not a valid hex color, e.g. `,r create Vip FF6B6B`")
        role = await ctx.guild.create_role(
            name=name, color=discord.Color(color_int), permissions=discord.Permissions.none(),
            reason=f"Created by {ctx.author}",
        )
        await ctx.send(f"✅ Created {role.mention}.", allowed_mentions=discord.AllowedMentions.none())

    @role_group.command(name="list")
    async def role_list(self, ctx: commands.Context):
        ranked = [r for r in sorted(ctx.guild.roles, key=lambda r: r.position, reverse=True) if not r.is_default()]
        if not ranked:
            return await ctx.send("This server has no roles besides @everyone.")

        lines = [f"`{i + 1}.` {role.mention}" for i, role in enumerate(ranked)]
        pages = [
            "\n".join(lines[i:i + ROLES_PER_PAGE])
            for i in range(0, len(lines), ROLES_PER_PAGE)
        ]
        view = RoleListView(ctx.guild, pages)
        await ctx.send(embed=view.current_embed(), view=view, allowed_mentions=discord.AllowedMentions.none())

    @role_group.command(name="add")
    @commands.has_permissions(manage_roles=True)
    async def role_add(self, ctx: commands.Context, user: str, *, role_ref: str):
        member = await resolve_member(ctx, user)
        if not member:
            return await ctx.send("❌ Couldn't find that user in this server.")
        role = _resolve_role(ctx.guild, role_ref)
        if not role:
            return await ctx.send("❌ Couldn't find that role (tried ID, rank number, and name).")
        try:
            await member.add_roles(role, reason=f"Added by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to add that role (check role position).")
        await ctx.send(f"✅ Added {role.mention} to **{member}**.", allowed_mentions=discord.AllowedMentions.none())

    @role_group.command(name="remove")
    @commands.has_permissions(manage_roles=True)
    async def role_remove(self, ctx: commands.Context, user: str, *, role_ref: str):
        member = await resolve_member(ctx, user)
        if not member:
            return await ctx.send("❌ Couldn't find that user in this server.")
        role = _resolve_role(ctx.guild, role_ref)
        if not role:
            return await ctx.send("❌ Couldn't find that role (tried ID, rank number, and name).")
        try:
            await member.remove_roles(role, reason=f"Removed by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to remove that role (check role position).")
        await ctx.send(f"✅ Removed {role.mention} from **{member}**.", allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
