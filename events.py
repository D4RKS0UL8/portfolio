import discord
from discord.ext import commands

from utils import achievements
from utils import database as db


class Events(commands.Cog):
    """Bot-wide listeners: presence/ready housekeeping, new-guild
    onboarding, per-message balance/achievement tracking, the
    DMs-only-allow-.stats check, and top-level command-error logging.
    """

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.change_presence(activity=discord.Game(name="Join My Server Pwease ^-^"))
        # Make sure every server we're already in has a data file + up to date name
        for guild in self.bot.guilds:
            await db.update_guild_meta(guild)
        await self.bot.tree.sync()
        print(f"Logged in as {self.bot.user}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        # New server added -> creates server_dbs/guild_<id>.sqlite immediately
        await db.update_guild_meta(guild)

        # Everyone already in the server gets "New Here" retroactively.
        # Granted silently per-member (no ping spam) - reported as one summary instead.
        eligible_members = [m for m in guild.members if not m.bot]
        await achievements.bulk_first_message_unlock(guild.id, [m.id for m in eligible_members])

        target_channel = guild.system_channel
        if target_channel is None or not target_channel.permissions_for(guild.me).send_messages:
            target_channel = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
                None
            )
        if target_channel:
            await target_channel.send(
                f"DT-Manager was Added successfully, the server logs have been written: "
                f"**{len(eligible_members)}** members have been given __1__ achievement"
            )

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        if message.guild:
            is_first_message = await db.record_message(message.guild.id, message.author.id)
            if is_first_message:
                await achievements.send_achievement_unlock(message.channel, message.author, "first_message")

    def bot_check(self, ctx):
        """Bot-wide check (discord.py auto-registers/deregisters this
        via bot.add_check/remove_check as this cog loads/unloads - see
        Cog._inject in discord.py's own source - so it's already
        hot-swap safe with no extra bookkeeping needed here).

        In DMs, only .stats is allowed to run. Everything else is
        silently blocked."""
        if ctx.guild is None:
            return ctx.command is not None and ctx.command.name == "stats"
        return True

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
            return  # e.g. a blocked prefix used in DMs - fail silently, no error spam
        print(f"Error in command '{ctx.command}': {error}")


async def setup(bot):
    await bot.add_cog(Events(bot))
