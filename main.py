"""
Entry point. Run with `python main.py` from this directory.

What changed vs. the original single-file script:
  - The bot token is no longer hardcoded - it's read from the
    DISCORD_BOT_TOKEN environment variable (see .env.example). A
    hardcoded token in source is a real secret leak - anyone with the
    file (or a git history that ever contained it) can fully take over
    the bot - so this one wasn't optional.
  - All command/event logic now lives in cogs/, loaded dynamically from
    this file instead of being wired up inline.
  - One shared aiohttp.ClientSession lives on the bot for its whole
    lifetime (self.session), instead of the /fakemessage renderer
    opening a fresh one on every call.
"""
import asyncio
import os
import traceback

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

from utils import imaging

load_dotenv()

COGS_DIR = "cogs"


class DTManagerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix=self._get_prefix, intents=intents, help_command=None)

    @staticmethod
    async def _get_prefix(bot, message: discord.Message):
        # Accepts both ",command" and ", command" (one leading space)
        return [", ", ","]
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await asyncio.to_thread(imaging.discover_fonts)
        await self._load_all_cogs()

    async def _load_all_cogs(self):
        if not os.path.isdir(COGS_DIR):
            print(f"[startup] No '{COGS_DIR}/' directory found - nothing to load.")
            return
        for filename in sorted(os.listdir(COGS_DIR)):
            if not (filename.endswith(".py") and not filename.startswith("_")):
                continue
            ext = f"{COGS_DIR}.{filename[:-3]}"
            try:
                await self.load_extension(ext)
                print(f"[startup] loaded {ext}")
            except commands.ExtensionError:
                # Same philosophy as the hot-swap admin panel: one
                # broken file at boot shouldn't take the rest of the
                # bot down with it - log it and keep going.
                print(f"[startup] FAILED to load {ext}:")
                traceback.print_exc()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()


bot = DTManagerBot()


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and put your bot token in it, "
            "or export it as a real environment variable in however you run this in production."
        )
    bot.run(token)
