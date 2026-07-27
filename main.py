"""
Keeps a specific role positioned directly below the bot's own highest
role -- i.e. above every other role the bot is actually capable of
moving.

Discord's hierarchy rule: a bot can never place a role at or above its
own top role, only below it. So "above everything it can manage" means
locked in immediately beneath the bot's own top role.
"""

import asyncio
import logging

import discord
from discord.ext import commands, tasks

logger = logging.getLogger("necro_bot.role_enforcer")

TARGET_ROLE_ID = 1531106256977268976


class RoleEnforcerService:
    def __init__(self, bot: commands.Bot, target_role_id: int = TARGET_ROLE_ID):
        self.bot = bot
        self.target_role_id = target_role_id
        self._locks: dict[int, asyncio.Lock] = {}

        bot.add_listener(self._on_ready, "on_ready")
        bot.add_listener(self._on_guild_join, "on_guild_join")
        bot.add_listener(self._on_guild_role_create, "on_guild_role_create")
        bot.add_listener(self._on_guild_role_update, "on_guild_role_update")
        bot.add_listener(self._on_guild_role_delete, "on_guild_role_delete")

    def start(self) -> None:
        """Kick off the periodic safety-net resync (runs immediately, then every 10 min)."""
        self._periodic_resync.start()

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def enforce(self, guild: discord.Guild) -> None:
        lock = self._lock_for(guild.id)
        if lock.locked():
            return  # an enforcement pass is already running for this guild
        async with lock:
            me = guild.me
            if me is None:
                try:
                    me = await guild.fetch_member(self.bot.user.id)
                except discord.HTTPException:
                    return

            target = guild.get_role(self.target_role_id)
            if target is None:
                return  # this guild doesn't have that role

            bot_top = me.top_role
            if target.id == bot_top.id:
                return  # nothing to do

            if target.position >= bot_top.position:
                logger.warning(
                    "Can't manage %s in %s: it outranks or ties the bot's own "
                    "top role (%s). Move the bot's role higher in Server Settings.",
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
                logger.info("Moved %s to position %s in %s", target, desired_position, guild)
            except discord.Forbidden:
                logger.warning("Missing permissions to move %s in %s", target, guild)
            except discord.HTTPException as e:
                logger.warning("Failed to move %s in %s: %s", target, guild, e)

    async def _on_ready(self):
        for guild in self.bot.guilds:
            await self.enforce(guild)

    async def _on_guild_join(self, guild: discord.Guild):
        await self.enforce(guild)

    async def _on_guild_role_create(self, role: discord.Role):
        await self.enforce(role.guild)

    async def _on_guild_role_update(self, before: discord.Role, after: discord.Role):
        await self.enforce(after.guild)

    async def _on_guild_role_delete(self, role: discord.Role):
        await self.enforce(role.guild)

    @tasks.loop(minutes=10)
    async def _periodic_resync(self):
        for guild in self.bot.guilds:
            await self.enforce(guild)

    @_periodic_resync.before_loop
    async def _before_periodic_resync(self):
        await self.bot.wait_until_ready()