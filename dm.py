import discord
from discord.ext import commands

OWNER_NAMES = {"d4rks0ul_8", "spiritual_og"}


def _sender_label(user) -> str:
    name = user.name
    if name in OWNER_NAMES:
        return name
    return name


class DM(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="dm")
    async def dm_member(self, ctx, user_id: int = None, *, message: str = ""):

        # ── 1. Must be a DM ─────────────────────────────────────────────────
        if ctx.guild is not None:
            return await ctx.send("❌ This command only works in DMs with the bot.")

        # ── 2. Permission check ─────────────────────────────────────────────
        invoker = ctx.author
        has_permission = False
        checked_guilds = []

        for guild in self.bot.guilds:
            member = guild.get_member(invoker.id)
            if member is None:
                # Try fetching — cache might not have them
                try:
                    member = await guild.fetch_member(invoker.id)
                except (discord.NotFound, discord.HTTPException):
                    checked_guilds.append(f"{guild.name} (not found)")
                    continue

            perms = member.guild_permissions
            checked_guilds.append(
                f"{guild.name} (admin={perms.administrator}, manage_guild={perms.manage_guild})"
            )
            if perms.administrator or perms.manage_guild:
                has_permission = True
                break

        if not has_permission:
            debug = "\n".join(checked_guilds) if checked_guilds else "No mutual servers found."
            return await ctx.send(
                f"❌ No permission found. Checked:\n```\n{debug}\n```"
            )

        # ── 3. Argument validation ──────────────────────────────────────────
        if user_id is None:
            return await ctx.send("❌ Usage: `.dm <user_id> <message>`")

        if not message and not ctx.message.attachments:
            return await ctx.send("❌ Provide a message or attach a file.")

        # ── 4. Resolve target ───────────────────────────────────────────────
        try:
            target = await self.bot.fetch_user(user_id)
        except discord.NotFound:
            return await ctx.send(f"❌ No user found with ID `{user_id}`.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed to fetch user: {e}")

        # ── 5. Build message ────────────────────────────────────────────────
        footer = f"-# sent from {_sender_label(invoker)}"
        body = f"{message}\n{footer}" if message else footer

        # ── 6. Attachments ──────────────────────────────────────────────────
        files = []
        for attachment in ctx.message.attachments:
            try:
                files.append(await attachment.to_file())
            except discord.HTTPException:
                pass

        # ── 7. Send ─────────────────────────────────────────────────────────
        try:
            if files:
                await target.send(content=body, files=files)
            else:
                await target.send(content=body)
        except discord.Forbidden:
            return await ctx.send(
                f"❌ Can't DM **{target}** — their DMs are closed or they blocked the bot."
            )
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Failed to send DM: {e}")

        await ctx.send(f"✅ DM sent to **{target}** (`{target.id}`).")


async def setup(bot):
    await bot.add_cog(DM(bot))
