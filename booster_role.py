"""
Booster Role cog  —  ,br

Server boosters can create and customise a personal decorative role.
Moderators with manage_roles can also interact with any booster role.

Subcommands
-----------
,br create               Create your booster role (white, named role#N)
,br name <name>          Rename your role
,br rename <name>        Alias for name
,br color <hex> [hex2]   Set a solid color or a two-stop gradient color*
,br icon <emoji>         Set a custom emoji as the role icon
,br delete               Delete your booster role (asks for confirmation)
,br share <user>         Toggle whether another member can wear your role

* Discord doesn't support true gradient role colors. The bot sets the
  role color to the first hex and sends an informational note.
"""

import json
import os
import re
import asyncio
from datetime import timezone, datetime

import discord
from discord.ext import commands

from utils.helpers import error_embed

DATA_DIR = "data"
BR_FILE = os.path.join(DATA_DIR, "booster_roles.json")  # {guild_id: {user_id: role_id}}


# ---------------------------------------------------------------- storage

def _load() -> dict:
    if not os.path.exists(BR_FILE):
        return {}
    try:
        with open(BR_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BR_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def get_br_role_id(guild_id: int, user_id: int) -> int | None:
    data = _load()
    v = data.get(_key(guild_id, user_id))
    return int(v) if v else None


def set_br_role_id(guild_id: int, user_id: int, role_id: int):
    data = _load()
    data[_key(guild_id, user_id)] = role_id
    _save(data)


def clear_br_role(guild_id: int, user_id: int):
    data = _load()
    data.pop(_key(guild_id, user_id), None)
    _save(data)


def get_sharers(guild_id: int, user_id: int) -> list[int]:
    """Members the booster has shared their role with."""
    data = _load()
    return data.get(f"{guild_id}:{user_id}:sharers", [])


def set_sharers(guild_id: int, user_id: int, sharers: list[int]):
    data = _load()
    data[f"{guild_id}:{user_id}:sharers"] = sharers
    _save(data)


# ---------------------------------------------------------------- helpers

def _parse_hex(text: str) -> discord.Color | None:
    text = text.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return None
    return discord.Color(int(text, 16))


def _next_role_number(guild: discord.Guild, existing_ids: set[int]) -> int:
    """Picks the next available role#N by scanning what's already in use."""
    used = set()
    for role in guild.roles:
        if role.id in existing_ids:
            m = re.fullmatch(r"role#(\d+)", role.name)
            if m:
                used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return n


def _all_br_role_ids(guild_id: int) -> set[int]:
    data = _load()
    ids = set()
    for k, v in data.items():
        if k.startswith(f"{guild_id}:") and not k.endswith(":sharers"):
            try:
                ids.add(int(v))
            except (ValueError, TypeError):
                pass
    return ids


async def _get_owner_role(ctx: commands.Context) -> discord.Role | None:
    """Returns the booster role the invoking member owns, or None."""
    role_id = get_br_role_id(ctx.guild.id, ctx.author.id)
    if not role_id:
        return None
    return ctx.guild.get_role(role_id)


def _is_booster(member: discord.Member) -> bool:
    return member.premium_since is not None


def _has_manage_roles(member: discord.Member) -> bool:
    return member.guild_permissions.manage_roles


async def _require_booster_and_role(ctx: commands.Context):
    """
    Returns (member, role) if the author is a booster with an existing
    booster role, otherwise sends an error and returns (None, None).
    """
    if not _is_booster(ctx.author) and not _has_manage_roles(ctx.author):
        await ctx.send(embed=error_embed("You need to be a server booster to use booster role commands."))
        return None, None
    role = await _get_owner_role(ctx)
    if role is None:
        await ctx.send(embed=error_embed("You don't have a booster role yet. Use `,br create` to make one."))
        return None, None
    return ctx.author, role


async def _find_member(ctx: commands.Context, target: str) -> discord.Member | None:
    target = target.strip()
    # mention
    m = re.fullmatch(r"<@!?(\d+)>", target)
    if m:
        uid = int(m.group(1))
        member = ctx.guild.get_member(uid)
        if not member:
            try:
                member = await ctx.guild.fetch_member(uid)
            except discord.NotFound:
                pass
        return member
    # raw id
    if target.isdigit():
        try:
            return ctx.guild.get_member(int(target)) or await ctx.guild.fetch_member(int(target))
        except discord.NotFound:
            return None
    # username / display name
    t = target.lower()
    return discord.utils.find(
        lambda m: m.name.lower() == t or m.display_name.lower() == t,
        ctx.guild.members,
    )


# ---------------------------------------------------------------- cog

class BoosterRole(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- group dispatcher

    @commands.group(name="br", invoke_without_command=True)
    async def br(self, ctx: commands.Context):
        help_text = (
            "**Booster Role commands** (server boosters only)\n"
            "`,br create` — create your personal role\n"
            "`,br name <name>` — rename your role\n"
            "`,br color <hex> [hex2]` — set role color\n"
            "`,br icon <emoji>` — set role icon (custom emoji)\n"
            "`,br delete` — delete your role\n"
            "`,br share <user>` — toggle sharing your role with someone\n"
        )
        await ctx.send(embed=discord.Embed(description=help_text, color=discord.Color.blurple()))

    # ---------- create

    @br.command(name="create")
    async def br_create(self, ctx: commands.Context):
        if not _is_booster(ctx.author) and not _has_manage_roles(ctx.author):
            return await ctx.send(embed=error_embed("You need to be a server booster to create a booster role."))

        existing_id = get_br_role_id(ctx.guild.id, ctx.author.id)
        if existing_id and ctx.guild.get_role(existing_id):
            return await ctx.send(embed=error_embed("You already have a booster role. Use `,br delete` first if you want to start fresh."))

        all_ids = _all_br_role_ids(ctx.guild.id)
        n = _next_role_number(ctx.guild, all_ids)
        role_name = f"role#{n}"

        # Position: just above the booster's highest non-admin role, but
        # below the bot's top role so the bot can manage it.
        bot_top = ctx.guild.me.top_role.position - 1

        try:
            role = await ctx.guild.create_role(
                name=role_name,
                color=discord.Color(0xFFFFFF),
                hoist=False,
                mentionable=False,
                reason=f"Booster role created by {ctx.author.name}",
            )
            # Move role below the bot's highest role
            await role.edit(position=max(1, bot_top))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I don't have permission to create roles."))

        # Give the role only to the booster
        await ctx.author.add_roles(role, reason="Booster role created")
        set_br_role_id(ctx.guild.id, ctx.author.id, role.id)

        embed = discord.Embed(
            description=f"✅ Created your booster role **{role.mention}**!\nCustomise it with `,br name`, `,br color`, `,br icon`.",
            color=discord.Color(0xFFFFFF),
        )
        await ctx.send(embed=embed)

    # ---------- name / rename

    @br.command(name="name", aliases=["rename"])
    async def br_name(self, ctx: commands.Context, *, new_name: str = None):
        if not new_name:
            return await ctx.send(embed=error_embed("Usage: `,br name <new name>`"))
        _, role = await _require_booster_and_role(ctx)
        if role is None:
            return
        if len(new_name) > 100:
            return await ctx.send(embed=error_embed("Role names can't be longer than 100 characters."))
        try:
            await role.edit(name=new_name, reason=f"Booster role renamed by {ctx.author.name}")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I don't have permission to edit that role."))
        await ctx.send(embed=discord.Embed(description=f"✅ Renamed your role to **{new_name}**.", color=role.color))

    # ---------- color

    @br.command(name="color", aliases=["colour"])
    async def br_color(self, ctx: commands.Context, hex1: str = None, hex2: str = None):
        if not hex1:
            return await ctx.send(embed=error_embed("Usage: `,br color <hex>` or `,br color <hex1> <hex2>`"))
        _, role = await _require_booster_and_role(ctx)
        if role is None:
            return

        color = _parse_hex(hex1)
        if color is None:
            return await ctx.send(embed=error_embed(f"`{hex1}` isn't a valid 6-digit hex color."))

        try:
            await role.edit(color=color, reason=f"Booster role color changed by {ctx.author.name}")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I don't have permission to edit that role."))

        if hex2:
            color2 = _parse_hex(hex2)
            if color2 is None:
                return await ctx.send(embed=error_embed(f"`{hex2}` isn't a valid 6-digit hex color."))
            embed = discord.Embed(
                description=(
                    f"✅ Role color set to **#{hex1.upper()}**.\n"
                    f"*(Discord doesn't support gradient role colors — the second color `#{hex2.upper()}` can't be applied natively. "
                    f"Your role uses the first color.)*"
                ),
                color=color,
            )
        else:
            embed = discord.Embed(
                description=f"✅ Role color set to **#{hex1.strip('#').upper()}**.",
                color=color,
            )
        await ctx.send(embed=embed)

    # ---------- icon

    @br.command(name="icon")
    async def br_icon(self, ctx: commands.Context, emoji: str = None):
        if not emoji:
            return await ctx.send(embed=error_embed("Usage: `,br icon <custom emoji>`\nSend a custom emoji from this server."))
        _, role = await _require_booster_and_role(ctx)
        if role is None:
            return

        # Parse custom emoji  <:name:id>  or  <a:name:id>
        m = re.fullmatch(r"<a?:(\w+):(\d+)>", emoji.strip())
        if not m:
            return await ctx.send(embed=error_embed(
                "Only custom server emojis are supported for role icons (e.g. `<:cool:123456789>`).\n"
                "Unicode emojis aren't accepted by Discord's role icon API."
            ))

        emoji_id = int(m.group(2))
        animated = emoji.startswith("<a:")
        guild_emoji = discord.utils.get(ctx.guild.emojis, id=emoji_id)
        if not guild_emoji:
            return await ctx.send(embed=error_embed("That emoji isn't from this server. Role icons must be server emojis."))

        # Fetch the emoji image bytes
        asset = guild_emoji.url
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(asset) as resp:
                if resp.status != 200:
                    return await ctx.send(embed=error_embed("Couldn't fetch the emoji image."))
                image_bytes = await resp.read()

        try:
            await role.edit(display_icon=image_bytes, reason=f"Booster role icon set by {ctx.author.name}")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I don't have permission to edit that role, or this server doesn't have the role icons feature (requires level 2 boost)."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Discord rejected the icon: {e}"))

        await ctx.send(embed=discord.Embed(
            description=f"✅ Role icon set to {emoji}.",
            color=role.color,
        ))

    # ---------- delete

    @br.command(name="delete")
    async def br_delete(self, ctx: commands.Context):
        _, role = await _require_booster_and_role(ctx)
        if role is None:
            return

        prompt = await ctx.send(
            "are you sure you want to delete your booster role? "
            "you can make another one and start fresh though."
        )

        def check(m: discord.Message) -> bool:
            return (
                m.author.id == ctx.author.id
                and m.channel.id == ctx.channel.id
                and m.reference is not None
                and m.reference.message_id == prompt.id
                and m.content.strip().lower() == "yes"
            )

        try:
            await self.bot.wait_for("message", check=check, timeout=30.0)
        except asyncio.TimeoutError:
            return await ctx.send(embed=discord.Embed(
                description="❌ Deletion cancelled (timed out).", color=discord.Color.red()
            ))

        # Remove the role from everyone who has it
        sharers = get_sharers(ctx.guild.id, ctx.author.id)
        members_to_strip = [ctx.guild.get_member(uid) for uid in [ctx.author.id] + sharers]
        for member in members_to_strip:
            if member and role in member.roles:
                try:
                    await member.remove_roles(role)
                except discord.HTTPException:
                    pass

        try:
            await role.delete(reason=f"Booster role deleted by {ctx.author.name}")
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I don't have permission to delete that role."))

        clear_br_role(ctx.guild.id, ctx.author.id)
        set_sharers(ctx.guild.id, ctx.author.id, [])

        await ctx.send(embed=discord.Embed(
            description="✅ Your booster role has been deleted. Use `,br create` whenever you want a new one.",
            color=discord.Color.green(),
        ))

    # ---------- share

    @br.command(name="share")
    async def br_share(self, ctx: commands.Context, *, target: str = None):
        if not target:
            return await ctx.send(embed=error_embed("Usage: `,br share <@mention or username or id>`"))
        _, role = await _require_booster_and_role(ctx)
        if role is None:
            return

        member = await _find_member(ctx, target)
        if not member:
            return await ctx.send(embed=error_embed(f"Couldn't find a member matching `{target}`."))
        if member.id == ctx.author.id:
            return await ctx.send(embed=error_embed("You already have your own role — you can't share it with yourself."))

        sharers = get_sharers(ctx.guild.id, ctx.author.id)

        if member.id in sharers:
            # Unshare
            sharers.remove(member.id)
            set_sharers(ctx.guild.id, ctx.author.id, sharers)
            try:
                await member.remove_roles(role, reason=f"Booster role unshared by {ctx.author.name}")
            except discord.HTTPException:
                pass
            await ctx.send(embed=discord.Embed(
                description=f"✅ Removed your booster role from **{member.display_name}**.",
                color=discord.Color.orange(),
            ))
        else:
            # Share
            sharers.append(member.id)
            set_sharers(ctx.guild.id, ctx.author.id, sharers)
            try:
                await member.add_roles(role, reason=f"Booster role shared by {ctx.author.name}")
            except discord.Forbidden:
                return await ctx.send(embed=error_embed("I don't have permission to assign that role."))
            await ctx.send(embed=discord.Embed(
                description=f"✅ **{member.display_name}** can now wear your booster role {role.mention}.",
                color=role.color,
            ))

    # ---------- guard: handle subcommands typed incorrectly

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=error_embed("You don't have permission to do that."))
        elif isinstance(error, commands.CommandInvokeError):
            raise error.original


async def setup(bot: commands.Bot):
    await bot.add_cog(BoosterRole(bot))
