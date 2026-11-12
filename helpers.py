"""Small, stateless helpers used by more than one cog.

Nothing here touches the database or the network, so none of it needs
to be async - these are just plain functions, unchanged from the
original script.
"""
import discord


def error_embed(text: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {text}", color=discord.Color.red())


def check_bet(amount_str, balance):
    if not amount_str:
        return None
    amt_lower = amount_str.lower()
    if amt_lower in ['all', 'max']:
        return balance
    if amt_lower == 'half':
        return balance // 2
    try:
        amt = int(amount_str)
        if amt <= 0:
            return None
        if amt > balance:
            return False
        return amt
    except ValueError:
        return None
