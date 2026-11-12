"""Owner-only hot-swap control panel.

Lets the bot owner load/reload/unload cogs from chat, with no restart,
and recover from a broken drop-in file without the bot going down.

A quick note on why `reload` is safe in the first place:
`bot.reload_extension()` prepares the *new* version of the module and
only swaps it in if that succeeds - if it raises, the old extension is
left exactly as it was, still fully loaded and working. So "the bot
keeps running when you drop in a broken file" is largely handled by
discord.py itself; this cog's job is to make sure whatever exception
comes back is actually caught and reported in chat instead of only
hitting the console (or, worse, going unhandled).
"""
import io
import os
import traceback

import discord
from discord.ext import commands

from utils.helpers import error_embed

COGS_DIR = "cogs"


def _extension_name(name: str) -> str:
    """Lets the owner type the short form ('casino') instead of the
    fully dotted import path ('cogs.casino'). Also strips a trailing
    '.py' so '.load moderation.py' works the same as '.load moderation'."""
    name = name.strip()
    if name.lower().endswith(".py"):
        name = name[:-3]
    if name.startswith(f"{COGS_DIR}."):
        return name
    return f"{COGS_DIR}.{name}"


def _discover_cog_files():
    """Every top-level .py file in cogs/ as a dotted extension name -
    used by the `cogs` status command below."""
    if not os.path.isdir(COGS_DIR):
        return []
    return [
        f"{COGS_DIR}.{filename[:-3]}"
        for filename in sorted(os.listdir(COGS_DIR))
        if filename.endswith(".py") and not filename.startswith("_")
    ]


class Admin(commands.Cog):
    """Owner-only hot-swap control panel."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        # Applies to every command in *this* cog only. A failed check
        # raises CheckFailure, which the Events cog's on_command_error
        # already swallows silently - same "nothing happens" UX as any
        # other blocked command in this bot, no extra handling needed.
        return await self.bot.is_owner(ctx.author)

    # ---------------------------------------------------- error reporting

    async def _report(self, ctx, action: str, ext: str, error: commands.ExtensionError):
        """Turns whatever load/reload/unload raised into a chat-visible
        message. The friendly, well-known ExtensionError subtypes get a
        one-line explanation; anything else - most importantly
        ExtensionFailed, which wraps both a broken setup() call *and*
        import-time failures like a SyntaxError in the dropped-in file
        - gets the full traceback via _send_traceback."""
        if isinstance(error, commands.ExtensionNotFound):
            await ctx.send(embed=error_embed(f"No extension named `{ext}` could be found."))
            return
        if isinstance(error, commands.ExtensionAlreadyLoaded):
            await ctx.send(embed=error_embed(f"`{ext}` is already loaded."))
            return
        if isinstance(error, commands.ExtensionNotLoaded):
            await ctx.send(embed=error_embed(f"`{ext}` isn't currently loaded."))
            return
        if isinstance(error, commands.NoEntryPointError):
            await ctx.send(embed=error_embed(f"`{ext}` has no `async def setup(bot)` function."))
            return

        original = getattr(error, "original", error)
        await self._send_traceback(ctx, action, ext, original)

    async def _send_traceback(self, ctx, action: str, ext: str, original: BaseException):
        tb_text = "".join(traceback.format_exception(type(original), original, original.__traceback__))
        header = f"❌ **{action} failed for `{ext}`** — bot is still running.\n"
        block = f"```py\n{tb_text}\n```"

        try:
            if len(header) + len(block) <= 1900:
                await ctx.send(header + block)
            else:
                await ctx.send(
                    header + "Traceback attached (too long for a message).",
                    file=discord.File(io.BytesIO(tb_text.encode()), filename=f"{ext.replace('.', '_')}_traceback.txt")
                )
        except discord.HTTPException:
            # Last-resort fallback if even the report can't be sent
            # (missing permissions, etc.) - print so it's still visible
            # in process logs instead of vanishing entirely.
            print(f"[admin] {action} failed for {ext}:\n{tb_text}")

    # ---------------------------------------------------------- commands

    @commands.command(name="reload_utils")
    async def reload_utils_cmd(self, ctx):
        import importlib
        from utils import achievements, database, imaging, helpers
        
        try:
            importlib.reload(achievements)
            importlib.reload(database)
            importlib.reload(imaging)
            importlib.reload(helpers)
        except Exception as e:
            return await self._send_traceback(ctx, "Utils Reload", "utils", e)
            
        await ctx.send(embed=discord.Embed(
            description="✅ Reloaded `utils` modules.\n**Note:** You must now run `.refresh_all` so your cogs pick up the new memory addresses.", 
            color=discord.Color.green()
        ))
    @commands.command(name="load")
    async def load_cog(self, ctx, name: str = None):
        if not name:
            return await ctx.send(embed=error_embed("Usage: `.load <cog_name>`"))
        ext = _extension_name(name)
        try:
            await self.bot.load_extension(ext)
        except commands.ExtensionError as e:
            return await self._report(ctx, "Load", ext, e)
        except Exception as e:  # belt-and-braces: nothing should be able to crash this command
            return await self._send_traceback(ctx, "Load", ext, e)
        await ctx.send(embed=discord.Embed(description=f"✅ Loaded `{ext}`.", color=discord.Color.green()))

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_commands(self, ctx, scope: str = "guild"):
        """`.sync` -> sync to this guild only (near-instant).
        `.sync global` -> sync globally (can take up to an hour to show everywhere)."""
        if scope == "global":
            synced = await self.bot.tree.sync()
        else:
            synced = await self.bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"✅ Synced {len(synced)} command(s) ({scope}).")

    @commands.command(name="unload")
    async def unload_cog(self, ctx, name: str = None):
        if not name:
            return await ctx.send(embed=error_embed("Usage: `.unload <cog_name>`"))
        ext = _extension_name(name)
        if ext == __name__:
            return await ctx.send(embed=error_embed(
                "Refusing to unload the control panel cog itself - that would remove `load`/`reload` too "
                "and lock you out until a real restart. Use `reload` if you need to update this file."
            ))
        try:
            await self.bot.unload_extension(ext)
        except commands.ExtensionError as e:
            return await self._report(ctx, "Unload", ext, e)
        except Exception as e:
            return await self._send_traceback(ctx, "Unload", ext, e)
        await ctx.send(embed=discord.Embed(description=f"✅ Unloaded `{ext}`.", color=discord.Color.green()))

    @commands.command(name="reload")
    async def reload_cog(self, ctx, name: str = None):
        if not name:
            return await ctx.send(embed=error_embed("Usage: `.reload <cog_name>`"))
        ext = _extension_name(name)
        try:
            await self.bot.reload_extension(ext)
        except commands.ExtensionError as e:
            return await self._report(ctx, "Reload", ext, e)
        except Exception as e:
            return await self._send_traceback(ctx, "Reload", ext, e)
        await ctx.send(embed=discord.Embed(description=f"🔄 Reloaded `{ext}`.", color=discord.Color.green()))

    @commands.command(name="refresh_all")
    async def refresh_all(self, ctx):
        """Reloads every currently-loaded cog, one at a time - one
        broken file doesn't stop the others from being attempted - then
        re-syncs the slash-command tree so any /command signature
        changes actually reach Discord."""
        targets = list(self.bot.extensions.keys())
        succeeded, failed = [], []

        for ext in targets:
            try:
                await self.bot.reload_extension(ext)
                succeeded.append(ext)
            except commands.ExtensionError as e:
                failed.append(ext)
                await self._report(ctx, "Reload", ext, e)
            except Exception as e:
                failed.append(ext)
                await self._send_traceback(ctx, "Reload", ext, e)

        sync_note = ""
        try:
            await self.bot.tree.sync()
        except Exception as e:
            # Broad on purpose: sync() can raise more than just
            # HTTPException (rate limits, a missing application_id if
            # this somehow ran before login, etc.) and a failure here
            # is not fatal - the reload work above already succeeded
            # and was already reported, so this is just a best-effort
            # extra note, never a reason to blow up the whole command.
            sync_note = f"\n⚠️ Slash-command sync failed: {e}"

        embed = discord.Embed(
            title="Refresh All",
            description=f"✅ {len(succeeded)} reloaded, ❌ {len(failed)} failed.{sync_note}",
            color=discord.Color.green() if not failed else discord.Color.orange(),
        )
        if succeeded:
            embed.add_field(name="Reloaded", value="\n".join(f"`{e}`" for e in succeeded), inline=False)
        if failed:
            embed.add_field(name="Failed (traceback above)", value="\n".join(f"`{e}`" for e in failed), inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="cogs")
    async def list_cogs(self, ctx):
        """Bonus, not one of the four requested commands: shows what's
        loaded vs. what's sitting in cogs/ unloaded, since load/unload
        are hard to use blind from a phone otherwise. Remove this if
        you'd rather keep the panel to exactly load/reload/unload/refresh_all."""
        loaded = set(self.bot.extensions.keys())
        on_disk = set(_discover_cog_files())
        unloaded = sorted(on_disk - loaded)

        embed = discord.Embed(title="Cogs", color=discord.Color.blurple())
        embed.add_field(name=f"✅ Loaded ({len(loaded)})", value="\n".join(f"`{e}`" for e in sorted(loaded)) or "*none*", inline=False)
        embed.add_field(name=f"⬜ Available ({len(unloaded)})", value="\n".join(f"`{e}`" for e in unloaded) or "*none*", inline=False)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Admin(bot))
    await bot.add_cog(DMCommand(bot))


# ---------------------------------------------------------------- DM command
# Separate cog so it has its own check independent of owner-only Admin.

ALLOWED_DM_ROLE_ID = 1480218428987146270

class DMCommand(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _has_dm_role(self, user: discord.User) -> bool:
        """Check if the user has the allowed role in any mutual guild."""
        for guild in self.bot.guilds:
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except (discord.NotFound, discord.HTTPException):
                    continue
            if any(r.id == ALLOWED_DM_ROLE_ID for r in member.roles):
                return True
        return False

    @commands.command(name="dmsend")
    async def dm_cmd(self, ctx: commands.Context, user_id: str = None, *, message: str = ""):
        # Must be used in DMs with the bot
        if not isinstance(ctx.channel, discord.DMChannel):
            return

        # Only users with the allowed role
        if not await self._has_dm_role(ctx.author):
            await ctx.send(embed=error_embed("You don't have permission to use this command."))
            return

        if not user_id or not user_id.isdigit():
            return await ctx.send(embed=error_embed("Usage: `,dm <user_id> <message>`"))

        # Must have a message or an attachment
        if not message and not ctx.message.attachments:
            return await ctx.send(embed=error_embed("Provide a message or attach a file."))

        try:
            target = await self.bot.fetch_user(int(user_id))
        except discord.NotFound:
            return await ctx.send(embed=error_embed(f"No user found with ID `{user_id}`."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Failed to fetch user: {e}"))

        # Build the footer line Discord renders as small subtext
        footer = f"-# sent from {ctx.author.name}"
        full_message = f"{message}\n{footer}".strip() if message else footer

        # Re-upload any attachments so they land in the target's DMs
        files = []
        for attachment in ctx.message.attachments:
            try:
                files.append(await attachment.to_file())
            except discord.HTTPException:
                pass

        try:
            await target.send(content=full_message, files=files if files else None)
        except discord.Forbidden:
            return await ctx.send(embed=error_embed(f"Can't DM **{target}** — their DMs are closed."))
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed(f"Failed to send: {e}"))

        await ctx.send(embed=discord.Embed(
            description=f"✅ Sent to **{target}** (`{target.id}`).",
            color=discord.Color.green(),
        ))



