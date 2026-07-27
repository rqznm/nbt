"""
Discord bot that automatically keeps a specific role positioned directly
below the bot's own highest role -- i.e. above every other role the bot
is actually capable of reordering.

Discord's hierarchy rule: a bot can only move roles that sit BELOW its
own top role. It can never place any role at or above its own top role.
So "above every role it's able to" means: immediately below the bot's
own top role.

Setup:
    1. pip install -U discord.py
    2. In the Discord Developer Portal, no privileged intents are
       required for this script.
    3. Invite the bot with the "bot" and "applications.commands" scopes
       and the "Manage Roles" permission.
    4. In the server's role list, drag the BOT'S OWN role above
       TARGET_ROLE_ID (a bot can't manage a role that outranks it).
    5. Set the DISCORD_TOKEN environment variable and run this file.
"""

import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("role-enforcer")

TARGET_ROLE_ID = 1531106256977268976  # the role to keep on top
TOKEN = os.environ["DISCORD_TOKEN"]   # set this in your environment

intents = discord.Intents.default()
intents.guilds = True  # role create/update/delete events

bot = commands.Bot(command_prefix="!", intents=intents)

# One lock per guild so overlapping events can't race each other
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(guild_id: int) -> asyncio.Lock:
    return _locks.setdefault(guild_id, asyncio.Lock())


async def enforce_top_position(guild: discord.Guild) -> None:
    """Move TARGET_ROLE_ID directly below the bot's top role, if needed."""
    lock = _lock_for(guild.id)
    if lock.locked():
        return  # an enforcement pass is already running for this guild
    async with lock:
        me = guild.me
        if me is None:
            try:
                me = await guild.fetch_member(bot.user.id)
            except discord.HTTPException:
                return

        target = guild.get_role(TARGET_ROLE_ID)
        if target is None:
            return  # this guild doesn't have that role

        bot_top = me.top_role

        if target.id == bot_top.id:
            return  # nothing to do

        if target.position >= bot_top.position:
            log.warning(
                "Can't manage %s in %s: it outranks or ties the bot's own "
                "top role (%s). Move the bot's role higher.",
                target, guild, bot_top,
            )
            return

        desired_position = bot_top.position - 1
        if target.position == desired_position:
            return  # already correctly placed

        try:
            await target.edit(
                position=desired_position,
                reason="Auto-enforcing configured top role position",
            )
            log.info("Moved %s to position %s in %s", target, desired_position, guild)
        except discord.Forbidden:
            log.warning("Missing permissions to move %s in %s", target, guild)
        except discord.HTTPException as e:
            log.warning("Failed to move %s in %s: %s", target, guild, e)


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    try:
        await bot.tree.sync()
    except discord.HTTPException as e:
        log.warning("Slash command sync failed: %s", e)
    for guild in bot.guilds:
        await enforce_top_position(guild)


@bot.event
async def on_guild_join(guild: discord.Guild):
    await enforce_top_position(guild)


@bot.event
async def on_guild_role_create(role: discord.Role):
    await enforce_top_position(role.guild)


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    await enforce_top_position(after.guild)


@bot.event
async def on_guild_role_delete(role: discord.Role):
    await enforce_top_position(role.guild)


@tasks.loop(minutes=10)
async def periodic_resync():
    """Safety net in case a gateway event is ever missed."""
    for guild in bot.guilds:
        await enforce_top_position(guild)


@periodic_resync.before_loop
async def before_periodic_resync():
    await bot.wait_until_ready()


periodic_resync.start()


@bot.tree.command(name="fixroles", description="Re-sync the enforced role position now")
@app_commands.checks.has_permissions(manage_roles=True)
async def fixroles(interaction: discord.Interaction):
    await enforce_top_position(interaction.guild)
    await interaction.response.send_message("Role position re-synced.", ephemeral=True)


bot.run(TOKEN)
