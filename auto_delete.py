import asyncio
import datetime
import logging
import time

import discord
from discord.ext import tasks

from settings_store import SettingsStore, duration_from_time_value


logger = logging.getLogger("auto_delete")

MAX_DELETES_PER_RUN = 200
MESSAGE_TRIGGER_COOLDOWN_SECONDS = 300


class AutoDeleteService:
    def __init__(self, bot: discord.Client, store: SettingsStore):
        self.bot = bot
        self.store = store
        self._last_message_cleanup: dict[int, float] = {}
        self._cleanup_locks: dict[int, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        if not self.cleanup_loop.is_running():
            self.cleanup_loop.start()

    def schedule_cleanup_for_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.author.id == 1083529323459379281:
            return

        rule = self._rule_for_channel(message.channel.id)
        if rule is None:
            return

        now = time.monotonic()
        last_run = self._last_message_cleanup.get(message.channel.id, 0)
        if now - last_run < MESSAGE_TRIGGER_COOLDOWN_SECONDS:
            return
        self._last_message_cleanup[message.channel.id] = now
        self.schedule_cleanup_rule(rule)

    def schedule_cleanup_rule(self, rule: dict) -> asyncio.Task | None:
        try:
            task = asyncio.create_task(self.cleanup_rule(dict(rule)))
        except RuntimeError:
            logger.exception("Could not schedule auto-delete cleanup.")
            return None

        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._log_task_exception)
        return task

    async def cleanup_all(self) -> None:
        for rule in self.store.auto_delete_rules():
            await self.cleanup_rule(rule)

    async def cleanup_rule(self, rule: dict) -> None:
        channel_id = int(rule.get("channel_id", 0))
        if not channel_id:
            return

        lock = self._cleanup_locks.setdefault(channel_id, asyncio.Lock())
        if lock.locked():
            logger.info("Auto-delete cleanup for %s is already running; skipping duplicate run.", channel_id)
            return

        async with lock:
            try:
                await self._cleanup_rule_unlocked(channel_id, rule)
            except discord.Forbidden:
                logger.warning("Missing permission to auto-delete in %s.", channel_id)
            except discord.HTTPException:
                logger.exception("Discord API error during auto-delete cleanup in %s.", channel_id)

    async def _cleanup_rule_unlocked(self, channel_id: int, rule: dict) -> None:

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

        time_value = rule.get("time")
        amount = rule.get("amount")
        deleted_count = 0

        if time_value:
            try:
                cutoff = datetime.datetime.now(datetime.timezone.utc) - duration_from_time_value(time_value)
            except ValueError:
                logger.warning("Skipping invalid auto-delete time value %r.", time_value)
            else:
                deleted = await channel.purge(
                    limit=MAX_DELETES_PER_RUN,
                    before=cutoff,
                    check=lambda message: not message.pinned,
                    bulk=True,
                    reason="Auto-delete time rule",
                )
                deleted_count += len(deleted)

        if amount:
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                amount = None

        if amount:
            seen_unpinned = 0
            remaining_deletes = MAX_DELETES_PER_RUN - deleted_count
            if remaining_deletes <= 0:
                return
            history_limit = amount + remaining_deletes - 1

            def should_delete_excess_message(message: discord.Message) -> bool:
                nonlocal seen_unpinned
                if message.pinned:
                    return False
                seen_unpinned += 1
                return seen_unpinned >= amount

            await channel.purge(
                limit=history_limit,
                check=should_delete_excess_message,
                bulk=True,
                reason="Auto-delete amount rule",
            )

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

    @staticmethod
    def _log_task_exception(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                "Background auto-delete cleanup failed.",
                exc_info=(type(exception), exception, exception.__traceback__),
            )