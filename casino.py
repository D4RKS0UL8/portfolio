import random

import discord
from discord.ext import commands

from utils import achievements
from utils import database as db
from utils.helpers import check_bet, error_embed

suits = ['♠️', '♥️', '♦️', '♣️']
ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']


def draw_card():
    return f"[{random.choice(ranks)}{random.choice(suits)}]"


def calc_score(hand):
    score = 0
    aces = 0
    for card in hand:
        rank = card[1:-3]
        if rank in ['J', 'Q', 'K']:
            score += 10
        elif rank == 'A':
            aces += 1
            score += 11
        else:
            score += int(rank)
    while score > 21 and aces:
        score -= 10
        aces -= 1
    return score


class BlackjackView(discord.ui.View):
    """Was defined fresh inside the `.bj` command on every call in the
    original (closing over `ctx` directly). Promoted to a module-level
    class - proper cog structure, and stops re-defining the class on
    every single invocation - so `ctx` is now passed explicitly and
    stored as `self.ctx`. generate_embed() is `async def` now because
    it needs to await get_streak/get_bal; every call site below awaits
    it accordingly.
    """

    def __init__(self, ctx, p_hand, d_hand, bet_amt):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.p_hand = p_hand
        self.d_hand = d_hand
        self.bet = bet_amt

    async def generate_embed(self, status="playing"):
        p_score = calc_score(self.p_hand)
        d_score = calc_score(self.d_hand)

        embed = discord.Embed(title="Blackjack", color=discord.Color.blurple())

        if status == "playing":
            embed.description = f"**Your Hand:** {', '.join(self.p_hand)} = **{p_score}**\n**Dealer Hand:** {self.d_hand[0]}, [??] = **?**\n\nBet: **{self.bet} §**"
        else:
            embed.description = f"**Your Hand:** {', '.join(self.p_hand)} = **{p_score}**\n**Dealer Hand:** {', '.join(self.d_hand)} = **{d_score}**\n\nBet: **{self.bet} §**"

            if status == "win":
                streak = await db.get_streak(self.ctx.guild.id, self.ctx.author.id)
                bonus_pct = 0.10 + ((streak - 1) * 0.05) if streak > 0 else 0
                bonus_amt = int(self.bet * bonus_pct)
                total_win = (self.bet * 2) + bonus_amt

                embed.color = discord.Color.green()
                bal = await db.get_bal(self.ctx.guild.id, self.ctx.author.id)
                embed.description += f"\n\n**You won!** (+{total_win - self.bet} § incl. {int(bonus_pct*100)}% streak bonus)\nStreak: **{streak} 🔥**\nTotal Balance: **{bal} §**"
            elif status == "lose":
                embed.color = discord.Color.red()
                bal = await db.get_bal(self.ctx.guild.id, self.ctx.author.id)
                embed.description += f"\n\n**You lost!** (Streak reset to 0)\nTotal Balance: **{bal} §**"
            elif status == "push":
                embed.color = discord.Color.gold()
                bal = await db.get_bal(self.ctx.guild.id, self.ctx.author.id)
                embed.description += f"\n\n**Push! Bet returned.**\nTotal Balance: **{bal} §**"

        return embed

    async def check_win(self, interaction):
        p_score = calc_score(self.p_hand)
        if p_score == 21:
            await self.process_win()
            await self.end_game(interaction, "win")
            return True
        elif p_score > 21:
            await self.process_loss()
            await self.end_game(interaction, "lose")
            return True
        return False

    async def process_win(self):
        streak = await db.get_streak(self.ctx.guild.id, self.ctx.author.id)
        bonus_pct = 0.10 + (streak * 0.05)
        bonus_amt = int(self.bet * bonus_pct)
        await db.update_bal(self.ctx.guild.id, self.ctx.author.id, (self.bet * 2) + bonus_amt)
        await db.set_streak(self.ctx.guild.id, self.ctx.author.id, streak + 1)
        await achievements.send_achievement_unlock(self.ctx.channel, self.ctx.author, "first_win")

    async def process_loss(self):
        await db.set_streak(self.ctx.guild.id, self.ctx.author.id, 0)
        await achievements.send_achievement_unlock(self.ctx.channel, self.ctx.author, "first_loss")

    async def end_game(self, interaction, status):
        for child in self.children:
            child.disabled = True

        if status == "lose" and calc_score(self.d_hand) == 21:
            await db.add_inventory(self.ctx.guild.id, self.ctx.author.id, f"Lost {self.bet} DTs to Dealer's 21")

        await interaction.response.edit_message(embed=await self.generate_embed(status), view=self)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.blurple)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("Not your game.", ephemeral=True)

        self.p_hand.append(draw_card())
        if await self.check_win(interaction):
            return
        await interaction.response.edit_message(embed=await self.generate_embed(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.gray)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("Not your game.", ephemeral=True)

        d_score = calc_score(self.d_hand)
        while d_score < 17:
            self.d_hand.append(draw_card())
            d_score = calc_score(self.d_hand)

        p_score = calc_score(self.p_hand)

        if d_score > 21 or p_score > d_score:
            await self.process_win()
            await self.end_game(interaction, "win")
        elif d_score > p_score:
            await self.process_loss()
            await self.end_game(interaction, "lose")
        else:
            await db.update_bal(self.ctx.guild.id, self.ctx.author.id, self.bet)
            await self.end_game(interaction, "push")


class Casino(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(aliases=['cf'])
    async def coinflip(self, ctx, choice: str = None, amount: str = None):
        if not ctx.guild or not choice or not amount:
            return await ctx.send(embed=error_embed("Usage: `.cf <h/t> <amount>`"))
        bal = await db.get_bal(ctx.guild.id, ctx.author.id)
        bet = check_bet(amount, bal)
        if not bet or bet is False:
            return await ctx.send(embed=error_embed("Invalid bet or insufficient funds."))

        win = (choice.lower() in ['h', 'heads']) == random.choice([True, False])
        await db.update_bal(ctx.guild.id, ctx.author.id, bet if win else -bet)
        await ctx.send(f"Coinflip result: {'Won' if win else 'Lost'} §{bet}!")

    @commands.command(aliases=['bj'])
    async def blackjack(self, ctx, amount: str = None):
        if not ctx.guild or not amount:
            return await ctx.send(embed=error_embed("Usage: `.bj <amount>`"))
        bal = await db.get_bal(ctx.guild.id, ctx.author.id)
        bet = check_bet(amount, bal)
        if not bet or bet is False:
            return await ctx.send(embed=error_embed("Invalid bet or insufficient funds."))

        await db.update_bal(ctx.guild.id, ctx.author.id, -bet)
        player_hand = [draw_card(), draw_card()]
        dealer_hand = [draw_card(), draw_card()]

        view = BlackjackView(ctx, player_hand, dealer_hand, bet)
        msg = await ctx.send(embed=await view.generate_embed(), view=view)

        if calc_score(player_hand) == 21:
            await view.process_win()
            for child in view.children:
                child.disabled = True
            await msg.edit(embed=await view.generate_embed("win"), view=view)


async def setup(bot):
    await bot.add_cog(Casino(bot))
