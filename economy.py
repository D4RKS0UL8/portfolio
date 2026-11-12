import random
import time

import discord
from discord.ext import commands

from utils import database as db
from utils.helpers import error_embed


class Economy(commands.Cog):
    """Balance, daily/work income, profile stats, bios, and giving DTs to other members."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def help(self, ctx):
        if not ctx.guild:
            return
        embed = discord.Embed(title="Casino Commands", color=discord.Color.dark_red())
        embed.add_field(
            name="Available Commands",
            value="`.bj <amount>` - Play Blackjack\n`.cf <h/t> <amount>` - Coinflip\n`.bal` - Check balance\n`.stats [@user]` - View profile\n`.setbio <text>` - Set bio\n`.daily` - Claim daily\n`.work` - Work\n`.give <amount> @user` - Give DTs\n`.shop` - Open shop"
        )
        await ctx.send(embed=embed)

    @commands.command(aliases=['bal', 'b'])
    async def balance(self, ctx):
        if not ctx.guild:
            return
        streak = await db.get_streak(ctx.guild.id, ctx.author.id)
        bal = await db.get_bal(ctx.guild.id, ctx.author.id)
        embed = discord.Embed(title="Bank", description=f"Balance: **{bal} §**\nWin Streak: **{streak} 🔥**", color=discord.Color.green())
        await ctx.send(embed=embed)

    @commands.command()
    async def setbio(self, ctx, *, bio_text: str = None):
        if not ctx.guild:
            return
        if not bio_text or not bio_text.strip():
            return await ctx.send(embed=error_embed("Usage: `.setbio <biography>` max 105 characters"))
        bio_text = bio_text.strip()
        if len(bio_text) > 105:
            return await ctx.send(f"Error: shorten your bio by **{len(bio_text) - 105}** characters. Limit is 105.")
        await db.set_bio(ctx.guild.id, ctx.author.id, bio_text)
        await ctx.send(embed=discord.Embed(description="✅ Bio updated.", color=discord.Color.green()))

    @commands.command()
    async def stats(self, ctx, member: discord.Member = None):
        if not ctx.guild:
            # DM context: aggregate this user's data across every server file
            entries = await db.get_all_user_data(self.bot, ctx.author.id)
            if not entries:
                return await ctx.send("No data found for you in any server yet.")
            body = "; ".join(f"{name} - {bal}§ - {joined}" for name, bal, joined in entries)
            await ctx.send(f"[{body}]")
            return
        if member is None and ctx.message.reference:
            try:
                ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                member = ref_msg.author
            except Exception:
                pass
        user = member if member else ctx.author
        now = int(time.time())

        msgs_24h, msgs_7d, msgs_life = await db.get_message_counts(ctx.guild.id, user.id, now)

        bal = await db.get_bal(ctx.guild.id, user.id)
        bio = await db.get_bio(ctx.guild.id, user.id) or "*(Empty)*"
        joined_date = f"<t:{int(user.joined_at.timestamp())}:D>" if user.joined_at else "Unknown"

        embed = discord.Embed(title=f"Stats: {user.display_name}", color=discord.Color.blurple())
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Username", value=f"@{user.name}", inline=True)
        embed.add_field(name="DTs (§)", value=f"{bal} §", inline=True)
        embed.add_field(name="Joined Server", value=joined_date, inline=True)
        embed.add_field(name="Messages Sent", value=f"**24h:** {msgs_24h}\n**7 Days:** {msgs_7d}\n**Lifetime:** {msgs_life}", inline=False)
        embed.add_field(name="CBio", value=bio, inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def daily(self, ctx):
        if not ctx.guild:
            return
        user_id = ctx.author.id
        now = int(time.time())
        cooldown = 86400

        remaining = await db.get_cooldown_remaining(ctx.guild.id, user_id, "daily", cooldown, now)
        if remaining is not None:
            return await ctx.send(f"Error: wait {remaining // 3600}hrs for your next daily.")

        amount = int(random.triangular(50, 1000, 200))
        await db.update_bal(ctx.guild.id, user_id, amount)
        await db.set_cooldown(ctx.guild.id, user_id, "daily", now)
        await ctx.send(embed=discord.Embed(description=f"🎉 Claimed daily reward: **§{amount}**!", color=discord.Color.green()))

    @commands.command()
    async def work(self, ctx):
        if not ctx.guild:
            return
        user_id = ctx.author.id
        now = int(time.time())
        cooldown = 86400

        remaining = await db.get_cooldown_remaining(ctx.guild.id, user_id, "work", cooldown, now)
        if remaining is not None:
            return await ctx.send(f"Error: wait {remaining // 3600}hrs for your next shift.")

        amount = int(random.triangular(50, 1000, 200))
        flavor = random.choice([
            f"sold magic sand and got **§{amount}**",
            f"sold black market items and earned **§{amount}**",
            f"scammed a tourist for **§{amount}**"
        ])
        await db.update_bal(ctx.guild.id, user_id, amount)
        await db.set_cooldown(ctx.guild.id, user_id, "work", now)
        await ctx.send(embed=discord.Embed(description=flavor, color=discord.Color.green()))

    @commands.command()
    async def give(self, ctx, amount: str = None, target: str = None):
        if not ctx.guild or not amount or not target:
            return await ctx.send("Usage: `.give 500 @user`")
        try:
            amt = int(amount)
            if amt <= 0:
                return await ctx.send(embed=error_embed("Enter a positive amount!"))
        except ValueError:
            return await ctx.send("Usage: `.give 500 @user`")

        member = ctx.message.mentions[0] if ctx.message.mentions else None
        if not member:
            return await ctx.send(embed=error_embed("Could not find user!"))

        if await db.get_bal(ctx.guild.id, ctx.author.id) < amt:
            return await ctx.send("Error: You don't have enough DTs.")

        await db.update_bal(ctx.guild.id, ctx.author.id, -amt)
        await db.update_bal(ctx.guild.id, member.id, amt)
        await ctx.send(embed=discord.Embed(description=f"Gave §{amt} to @{member.name}!", color=discord.Color.green()))


async def setup(bot):
    await bot.add_cog(Economy(bot))
