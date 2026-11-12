import discord
from discord.ext import commands

from utils import database as db

# Roles granted by the shop (must sit BELOW the bot's own top role in
# Server Settings > Roles, or add_roles() below will fail)
MEDIA_ROLE_ID = 1539860544960598136       # "Image Perms" purchase grants this
STREAMING_ROLE_ID = 1539860372243349624   # "Video Perms" purchase grants this


class ShopView(discord.ui.View):
    """Promoted to module level for the same reason as BlackjackView in
    casino.py - `ctx` is passed in explicitly instead of via closure."""

    def __init__(self, ctx):
        super().__init__(timeout=60)
        self.ctx = ctx

    @discord.ui.button(label="Image Perms (§50,000)", style=discord.ButtonStyle.green)
    async def image_perms(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("Not your shop menu.", ephemeral=True)
        if await db.get_bal(self.ctx.guild.id, interaction.user.id) < 50000:
            return await interaction.response.send_message("❌ Not enough DTs!", ephemeral=True)
        await db.update_bal(self.ctx.guild.id, interaction.user.id, -50000)
        await db.add_inventory(self.ctx.guild.id, interaction.user.id, "Image Perms")
        await self.grant_role(interaction, MEDIA_ROLE_ID, "Image Perms")

    @discord.ui.button(label="Video Perms (§50,000)", style=discord.ButtonStyle.green)
    async def video_perms(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("Not your shop menu.", ephemeral=True)
        if await db.get_bal(self.ctx.guild.id, interaction.user.id) < 50000:
            return await interaction.response.send_message("❌ Not enough DTs!", ephemeral=True)
        await db.update_bal(self.ctx.guild.id, interaction.user.id, -50000)
        await db.add_inventory(self.ctx.guild.id, interaction.user.id, "Video Perms")
        await self.grant_role(interaction, STREAMING_ROLE_ID, "Video Perms")

    async def grant_role(self, interaction, role_id, item_name):
        role = self.ctx.guild.get_role(role_id)
        if role is None:
            return await interaction.response.send_message(
                f"✅ Bought **{item_name}**! ⚠️ Couldn't find that role (bad ID or it was deleted) - ask an admin to check it.",
                ephemeral=True
            )
        try:
            await interaction.user.add_roles(role, reason=f"Purchased {item_name}")
            await interaction.response.send_message(f"✅ Bought **{item_name}**! You've been given {role.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"✅ Bought **{item_name}**! ⚠️ I don't have permission to assign that role - my top role needs to be above it, and I need Manage Roles.",
                ephemeral=True
            )
        except discord.HTTPException:
            await interaction.response.send_message(f"✅ Bought **{item_name}**! ⚠️ Something went wrong assigning the role, contact an admin.", ephemeral=True)


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def shop(self, ctx):
        if not ctx.guild:
            return
        embed = discord.Embed(title="Casino Shop", description="Click a button to purchase perms!", color=discord.Color.gold())
        embed.add_field(name="🖼️ Image Perms", value="**§50,000**", inline=False)
        embed.add_field(name="🎥 Video Perms", value="**§50,000**", inline=False)
        await ctx.send(embed=embed, view=ShopView(ctx))


async def setup(bot):
    await bot.add_cog(Shop(bot))
