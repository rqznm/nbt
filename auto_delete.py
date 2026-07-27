import datetime
import logging
import time

import discord
from discord.ext import tasks

from settings_store import SettingsStore, duration_from_time_value


logger = logging.getLogger("auto_delete")

MAX_DELETES_PER_RUN = 200
MESSAGE_TRIGGER_COOLDOWN_SECONDS = 60


class AutoDeleteService:
    def __init__(self, bot: discord.Client, store: SettingsStore):
        self.bot = bot
        self.store = store
        self._last_message_cleanup: dict[int, float] = {}

    def start(self) -> None:
        if not self.cleanup_loop.is_running():
            self.cleanup_loop.start()

    async def maybe_cleanup_for_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        rule = self._rule_for_channel(message.channel.id)
        if rule is None:
            return

        now = time.monotonic()
        last_run = self._last_message_cleanup.get(message.channel.id, 0)
        if now - last_run < MESSAGE_TRIGGER_COOLDOWN_SECONDS:
            return
        self._last_message_cleanup[message.channel.id] = now
        await self.cleanup_rule(rule)

    async def cleanup_all(self) -> None:
        for rule in self.store.auto_delete_rules():
            await self.cleanup_rule(rule)

    async def cleanup_rule(self, rule: dict) -> None:
        channel_id = int(rule.get("channel_id", 0))
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("Auto-delete channel %s is unavailable.", channel_id)
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.warning("Auto-delete target %s does not support message history.", channel_id)
            return

        candidates: dict[int, discord.Message] = {}
        time_value = rule.get("time")
        amount = rule.get("amount")

        if time_value:
            try:
                cutoff = datetime.datetime.now(datetime.timezone.utc) - duration_from_time_value(time_value)
            except ValueError:
                logger.warning("Skipping invalid auto-delete time value %r.", time_value)
            else:
                async for old_message in channel.history(before=cutoff, limit=MAX_DELETES_PER_RUN):
                    if not old_message.pinned:
                        candidates[old_message.id] = old_message
                    if len(candidates) >= MAX_DELETES_PER_RUN:
                        break

        if amount:
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                amount = None

        if amount:
            seen_unpinned = 0
            history_limit = amount + MAX_DELETES_PER_RUN
            async for history_message in channel.history(limit=history_limit):
                if history_message.pinned:
                    continue
                seen_unpinned += 1
                if seen_unpinned >= amount:
                    candidates[history_message.id] = history_message
                if len(candidates) >= MAX_DELETES_PER_RUN:
                    break

        for message in candidates.values():
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                logger.warning("Missing permission to auto-delete in %s.", channel_id)
                return
            except discord.HTTPException:
                logger.exception("Failed to auto-delete message %s in %s.", message.id, channel_id)

    def _rule_for_channel(self, channel_id: int) -> dict | None:
        for rule in self.store.auto_delete_rules():
            if int(rule.get("channel_id", 0)) == channel_id:
                return rule
        return None

    @tasks.loop(minutes=10)
    async def cleanup_loop(self) -> None:
        await self.cleanup_all()

    @cleanup_loop.before_loop
    async def before_cleanup_loop(self) -> None:
        await self.bot.wait_until_ready()
