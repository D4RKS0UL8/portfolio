import asyncio
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import imaging
from utils.helpers import error_embed


class Fun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Populates the font fallback list before the first render.
        # Also means `.reload fun` re-scans fonts/ as a side effect,
        # which is a handy bonus if you ever drop a file in by hand
        # instead of using .addfont.
        await asyncio.to_thread(imaging.discover_fonts)

    # ---------------------------------------------------- /fakemessage

    @app_commands.command(name="fakemessage", description="Render a joke Discord-style message image")
    @app_commands.describe(
        user="The user to impersonate (provides the default name/avatar)",
        message="The message text to display",
        display_name="Name shown above the message (overrides user's name if set)",
        avatar_url="Image URL for the avatar (overrides user's avatar if set)",
        name_color="Hex for the name, e.g. 5865F2 (defaults to white). Two hexes like FF6B6B-4ECDC4 make it a looping gradient",
        date="Timestamp beside the name - blank for right now, or e.g. 10/09/2026 22:29, 22:29, 2229, 10:29pm",
        reply="Show this as a reply to another typed-in message",
        reply_message="Short snippet of the message being replied to (required if reply=True)",
        reply_display_name="Name shown in the reply preview (required if reply=True)",
        reply_avatar_url="Small avatar shown in the reply preview (optional)",
        bg_color="Hex background, e.g. 2b2d31, or a horizontal gradient like ff0000-0000ff",
        third_gradient="Extra gradient stop: HEX, POS-HEX, HEX-INTENSITY, or POS-HEX-INTENSITY (e.g. 40-121212-70)"
    )
    async def fakemessage(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        message: str,
        display_name: Optional[str] = None,
        avatar_url: Optional[str] = None,
        name_color: Optional[str] = None,
        date: Optional[str] = None,
        reply: Optional[bool] = False,
        reply_message: Optional[str] = None,
        reply_display_name: Optional[str] = None,
        reply_avatar_url: Optional[str] = None,
        bg_color: Optional[str] = None,
        third_gradient: Optional[str] = None,
    ):
        if reply and not (reply_message and reply_display_name):
            return await interaction.response.send_message(
                "❌ `reply` is on, so `reply_message` and `reply_display_name` are both required.", ephemeral=True
            )

        # `avatar_decoration` is a Pycord addition, not present on
        # every discord.py-family fork/version - getattr() so this
        # degrades to "no decoration" instead of an AttributeError on
        # a library/version that doesn't have it. Nitro-less users, or
        # anyone with no decoration equipped, hit the `None` branch
        # too - that's the common case, not an error.
        decoration = getattr(user, "avatar_decoration", None)
        decoration_url = decoration.url if decoration else None

        await interaction.response.defer()
        buf, color_report = await imaging.render_fake_message(
            self.bot.session,
            name=display_name or user.display_name,
            avatar_url=avatar_url or user.display_avatar.url,
            decoration_url=decoration_url,
            message=message,
            name_color=name_color,
            date=date,
            reply=reply,
            reply_name=reply_display_name,
            reply_avatar_url=reply_avatar_url,
            reply_snippet=reply_message,
            bg_color=bg_color,
            third_gradient=third_gradient,
        )
        # Background hexes are always darkened before painting (see
        # darken_hex_color in utils/imaging.py) - surfaced here so
        # what you typed and what actually rendered aren't a mystery.
        content = None
        if color_report:
            lines = [f"`#{orig}` → `#{dark}` ({label})" for label, orig, dark in color_report]
            content = "🎨 Background darkened: " + ", ".join(lines)
        await interaction.followup.send(content=content, file=discord.File(buf, filename="fakemessage.png"))

    # -------------------------------------------------- font management
    # Owner-only: these write files into fonts/ on disk, so they get
    # the same trust level as the hot-swap cog panel rather than being
    # open to every server admin.

    @commands.command(name="addfont")
    @commands.is_owner()
    async def add_font(self, ctx):
        """Attach a .ttf/.otf file to the message invoking this
        command. Saved into fonts/ and hot-loaded into the fallback
        chain immediately - no restart needed."""
        if not ctx.message.attachments:
            return await ctx.send(embed=error_embed("Attach a `.ttf` or `.otf` file with this command."))
        attachment = ctx.message.attachments[0]
        if not attachment.filename.lower().endswith((".ttf", ".otf")):
            return await ctx.send(embed=error_embed("Only `.ttf` or `.otf` files are supported."))

        os.makedirs(imaging.FONTS_DIR, exist_ok=True)
        dest = os.path.join(imaging.FONTS_DIR, attachment.filename)
        await attachment.save(dest)
        await asyncio.to_thread(imaging.discover_fonts)

        loaded_names = {f.name for f in imaging.get_loaded_fonts()}
        if attachment.filename not in loaded_names:
            return await ctx.send(embed=error_embed(
                f"Saved **{attachment.filename}**, but couldn't read its character map - "
                f"it may not be a valid font file. It won't be used for rendering."
            ))
        await ctx.send(embed=discord.Embed(
            description=f"✅ Added **{attachment.filename}** ({len(imaging.get_loaded_fonts())} font(s) now loaded).",
            color=discord.Color.green()
        ))

    @commands.command(name="listfonts")
    @commands.is_owner()
    async def list_fonts(self, ctx):
        fonts = imaging.get_loaded_fonts()
        if not fonts:
            return await ctx.send(embed=error_embed("No fonts loaded - falling back to Pillow's built-in bitmap font."))
        lines = [f"`{f.name}` — {len(f.coverage)} glyphs" for f in fonts]
        embed = discord.Embed(
            title=f"Loaded Fonts ({len(fonts)})",
            description="\n".join(lines) + "\n\n*First font is tried first for every character; the rest fill in gaps in this order.*",
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="removefont")
    @commands.is_owner()
    async def remove_font(self, ctx, filename: str = None):
        if not filename:
            return await ctx.send(embed=error_embed("Usage: `.removefont <filename>` - see `.listfonts` for names."))
        path = os.path.join(imaging.FONTS_DIR, filename)
        if not os.path.isfile(path):
            return await ctx.send(embed=error_embed(f"No font named `{filename}` in the fonts folder."))
        await asyncio.to_thread(os.remove, path)
        await asyncio.to_thread(imaging.discover_fonts)
        await ctx.send(embed=discord.Embed(description=f"🗑️ Removed **{filename}**.", color=discord.Color.green()))

    @commands.command(name="refreshfonts")
    @commands.is_owner()
    async def refresh_fonts(self, ctx):
        """Re-scans the fonts/ folder - useful if a file was dropped in
        by hand instead of through .addfont."""
        await asyncio.to_thread(imaging.discover_fonts)
        fonts = imaging.get_loaded_fonts()
        await ctx.send(embed=discord.Embed(description=f"🔄 Reloaded fonts folder — {len(fonts)} font(s) loaded.", color=discord.Color.green()))


async def setup(bot):
    await bot.add_cog(Fun(bot))
