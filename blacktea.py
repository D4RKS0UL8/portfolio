"""
.blacktea - a live, turn-based word game.

Flow: post a join embed and react ✅ to it -> wait 30s, collecting
whoever has the ✅ reaction at the deadline (adding *or removing* it
is honored, since we only look at the final state) -> shuffle into a
turn order, 2 lives each -> ping the current player with a random
3-letter combo -> they have 10s to reply with one real word containing
those letters (checked live against data/dictionary.txt, not a fixed
per-combo list - see data/combos.txt for why) -> correct answer:
react ✅ on their message, edit the prompt, move to the next player;
timeout: lose a life (countdown reactions appear in the last 3
seconds), eliminate at 0 -> last player standing wins.

Data files (both plain text, hot-reloaded on `.reload blacktea`):
  data/dictionary.txt   one valid word per line - what counts as "real"
  data/combos.txt        one 3-letter combo per line - what gets asked
See data/combo_word_preview.txt for a human-readable (not bot-read)
preview of example words per combo, useful for eyeballing combos.txt.
"""
import asyncio
import random
import traceback

import discord
from discord.ext import commands

from utils import wordgame
from utils.helpers import error_embed

DICTIONARY_PATH = "data/dictionary.txt"
COMBOS_PATH = "data/combos.txt"

JOIN_SECONDS = 30
TURN_SECONDS = 10
STARTING_LIVES = 2
MIN_PLAYERS = 2
COUNTDOWN_EMOJIS = ["3️⃣", "2️⃣", "1️⃣"]


class BlackteaGame:
    """One in-progress game, scoped to a single channel. Holds all
    mutable state itself so the cog only needs a dict of these keyed
    by channel id - nothing here is shared across games."""

    def __init__(self, cog: "Blacktea", ctx: commands.Context):
        self.cog = cog
        self.bot = cog.bot
        self.ctx = ctx
        self.channel = ctx.channel

        self.participants: list = []
        self.lives: dict = {}
        self.used_words: set = set()
        self.order_index = -1
        self.last_combo = None

    # ------------------------------------------------------------ public

    async def run(self):
        if not await self._collect_players():
            return
        await self._play()

    # --------------------------------------------------------- join phase

    async def _collect_players(self) -> bool:
        embed = discord.Embed(
            title="🍵 Blacktea",
            description=(
                f"React with ✅ to join! Change your mind? Just remove your "
                f"reaction any time before the timer ends.\n\n"
                f"**Starting in {JOIN_SECONDS} seconds.**"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="How to play",
            value=(
                f"Each round you'll get pinged with **3 letters**. Reply with "
                f"**one real word** that contains those letters *in that "
                f"order* somewhere in it - within **{TURN_SECONDS} seconds**.\n"
                f"- Everyone starts with **{STARTING_LIVES} lives** - miss the "
                f"timer and you lose one.\n"
                f"- Each word can only be used **once** for the whole game.\n"
                f"- Last player standing wins!"
            ),
            inline=False,
        )
        join_msg = await self.channel.send(embed=embed)
        try:
            await join_msg.add_reaction("✅")
        except discord.HTTPException:
            pass

        await asyncio.sleep(JOIN_SECONDS)

        try:
            join_msg = await self.channel.fetch_message(join_msg.id)
        except discord.HTTPException:
            pass
        reaction = discord.utils.get(join_msg.reactions, emoji="✅")

        participants = []
        if reaction is not None:
            async for user in reaction.users():
                if not user.bot and user not in participants:
                    participants.append(user)

        if len(participants) < MIN_PLAYERS:
            await self.channel.send(embed=error_embed(
                f"Not enough players joined (need at least {MIN_PLAYERS}). Game cancelled."
            ))
            return False

        random.shuffle(participants)
        self.participants = participants
        self.lives = {p.id: STARTING_LIVES for p in participants}

        order_text = " → ".join(p.mention for p in participants)
        await self.channel.send(embed=discord.Embed(
            title="Let's go! 🍵",
            description=f"**Turn order:** {order_text}",
            color=discord.Color.green(),
        ))
        return True

    # ------------------------------------------------------------- rounds

    async def _play(self):
        while self._alive_count() > 1:
            player = self._next_alive_player()
            if player is None:
                break
            await self._take_turn(player)

        survivors = [p for p in self.participants if self.lives[p.id] > 0]
        if survivors:
            await self.channel.send(embed=discord.Embed(
                title="🏆 Game over!",
                description=f"{survivors[0].mention} wins the last cup of Blacktea!",
                color=discord.Color.gold(),
            ))
        else:
            await self.channel.send(embed=discord.Embed(
                description="Game over - nobody's left standing!",
                color=discord.Color.red(),
            ))

    def _alive_count(self) -> int:
        return sum(1 for p in self.participants if self.lives[p.id] > 0)

    def _next_alive_player(self):
        n = len(self.participants)
        for _ in range(n):
            self.order_index = (self.order_index + 1) % n
            p = self.participants[self.order_index]
            if self.lives[p.id] > 0:
                return p
        return None

    def _pick_combo(self) -> str:
        combos = self.cog.combos
        choice = random.choice(combos)
        if len(combos) > 1:
            while choice == self.last_combo:
                choice = random.choice(combos)
        self.last_combo = choice
        return choice

    async def _take_turn(self, player):
        combo = self._pick_combo()
        ping_msg = await self.channel.send(
            content=player.mention,
            embed=discord.Embed(
                description=f"🍵 Type a word containing the letters: **{combo}**.",
                color=discord.Color.blurple(),
            ),
        )

        def check(m: discord.Message) -> bool:
            if m.channel.id != self.channel.id or m.author.id != player.id:
                return False
            return wordgame.check_answer(m.content, combo, self.cog.dictionary, self.used_words) is not None

        countdown_task = asyncio.create_task(self._add_countdown_reactions(ping_msg))
        try:
            answer_msg = await self.bot.wait_for("message", check=check, timeout=TURN_SECONDS)
        except asyncio.TimeoutError:
            answer_msg = None
        finally:
            countdown_task.cancel()

        if answer_msg is not None:
            await self._handle_correct(player, ping_msg, answer_msg, combo)
        else:
            await self._handle_timeout(player, ping_msg)

    async def _add_countdown_reactions(self, msg: discord.Message):
        """Runs alongside the wait_for above; cancelled the instant a
        valid answer (or the full timeout) resolves the turn, so this
        only ever gets partway through if the player is still
        thinking with 3 seconds left."""
        try:
            await asyncio.sleep(TURN_SECONDS - len(COUNTDOWN_EMOJIS))
            for emoji in COUNTDOWN_EMOJIS:
                await msg.add_reaction(emoji)
                await asyncio.sleep(1)
        except (asyncio.CancelledError, discord.HTTPException):
            pass

    async def _handle_correct(self, player, ping_msg, answer_msg, combo):
        word = wordgame.check_answer(answer_msg.content, combo, self.cog.dictionary, self.used_words)
        self.used_words.add(word)
        try:
            await answer_msg.add_reaction("✅")
        except discord.HTTPException:
            pass
        try:
            await ping_msg.edit(
                content=None,
                embed=discord.Embed(
                    description=(
                        f"🍵 ~~Type a word containing the letters: **{combo}**.~~\n"
                        f"✅ {player.mention} answered **{answer_msg.content.strip()}**!"
                    ),
                    color=discord.Color.green(),
                ),
            )
        except discord.HTTPException:
            pass

    async def _handle_timeout(self, player, ping_msg):
        self.lives[player.id] -= 1
        remaining = self.lives[player.id]
        try:
            if remaining > 0:
                life_word = "life" if remaining == 1 else "lives"
                await ping_msg.edit(
                    content=None,
                    embed=discord.Embed(
                        description=f"⏰ Time's up! {player.mention} has **{remaining}** {life_word} remaining.",
                        color=discord.Color.orange(),
                    ),
                )
            else:
                await ping_msg.edit(
                    content=None,
                    embed=discord.Embed(
                        description=f"💀 Time's up! {player.mention} is **eliminated**.",
                        color=discord.Color.red(),
                    ),
                )
        except discord.HTTPException:
            pass


class Blacktea(commands.Cog):
    """A live turn-based word game, `.blacktea`. See the module
    docstring above for the full flow."""

    def __init__(self, bot):
        self.bot = bot
        self.active_games: dict = {}   # channel_id -> BlackteaGame
        self.dictionary: set = set()
        self.combos: list = []

    async def cog_load(self):
        # Off the event loop: dictionary.txt is a few MB. Also means
        # `.reload blacktea` picks up edits to either data file
        # without a full bot restart.
        self.dictionary = await asyncio.to_thread(wordgame.load_dictionary, DICTIONARY_PATH)
        self.combos = await asyncio.to_thread(wordgame.load_combos, COMBOS_PATH)
        print(f"[blacktea] loaded {len(self.dictionary)} dictionary words, {len(self.combos)} combos")

    @commands.command(name="blacktea")
    async def blacktea(self, ctx: commands.Context):
        if not ctx.guild:
            return
        if ctx.channel.id in self.active_games:
            return await ctx.send(embed=error_embed("A game is already running in this channel."))
        if not self.combos:
            return await ctx.send(embed=error_embed(
                f"No combos loaded ({COMBOS_PATH} is empty or missing) - can't start a game."
            ))
        if not self.dictionary:
            return await ctx.send(embed=error_embed(
                f"No dictionary loaded ({DICTIONARY_PATH} is empty or missing) - can't validate answers."
            ))

        game = BlackteaGame(self, ctx)
        self.active_games[ctx.channel.id] = game
        try:
            await game.run()
        except Exception:
            # Same "never leave the bot/channel hanging" philosophy as
            # the admin hot-swap panel - a bug here shouldn't strand
            # players mid-game with no explanation.
            traceback.print_exc()
            try:
                await ctx.send(embed=error_embed("Something went wrong and the game had to stop early. Sorry about that!"))
            except discord.HTTPException:
                pass
        finally:
            self.active_games.pop(ctx.channel.id, None)


async def setup(bot):
    await bot.add_cog(Blacktea(bot))
