import logging

import discord
from discord.ext import tasks

from settings_store import SettingsStore


logger = logging.getLogger("member_counter")


class MemberCounterService:
    def __init__(self, bot: discord.Client, store: SettingsStore):
        self.bot = bot
        self.store = store

    def start(self) -> None:
        if not self.update_loop.is_running():
            self.update_loop.start()

    async def update_all(self) -> None:
        for guild in self.bot.guilds:
            await self.update_guild(guild)

    async def update_guild(self, guild: discord.Guild) -> None:
        settings = self.store.member_counter()
        category_id = settings.get("category_id")
        template = settings.get("name_template") or "members: {counter}!"
        if not category_id:
            return
        if "{counter}" not in template:
            logger.warning("Member counter template is missing {counter}.")
            return

        category = guild.get_channel(int(category_id))
        if category is None:
            try:
                fetched = await guild.fetch_channel(int(category_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("Member counter category %s is unavailable.", category_id)
                return
            category = fetched

        if not isinstance(category, discord.CategoryChannel):
            logger.warning("Member counter target %s is not a category.", category_id)
            return

        member_count = guild.member_count
        if member_count is None:
            member_count = len(guild.members)
        next_name = template.replace("{counter}", str(member_count))[:100]
        if category.name == next_name:
            return

        try:
            await category.edit(name=next_name, reason="Updating member counter")
        except discord.Forbidden:
            logger.warning("Missing permission to rename member counter category %s.", category_id)
        except discord.HTTPException:
            logger.exception("Failed to rename member counter category %s.", category_id)

    @tasks.loop(minutes=10)
    async def update_loop(self) -> None:
        await self.update_all()

    @update_loop.before_loop
    async def before_update_loop(self) -> None:
        await self.bot.wait_until_ready()