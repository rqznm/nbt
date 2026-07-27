import asyncio
import logging
import os
import subprocess
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from auto_delete import AutoDeleteService
from auto_responder import send_auto_response
from blacklisted_words import delete_if_blacklisted
from member_counter import MemberCounterService
from settings_channel_guard import delete_non_bot_settings_message
from settings_panel import SettingsPanel
from settings_store import SettingsStore
from temp_voice import TempVoiceService
from welcome import send_welcome

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("necro_bot")

TARGET_ROLE_ID = 1531106256977268976


class RoleEnforcerService:
    """Keeps TARGET_ROLE_ID pinned directly below the bot's own top role --
    i.e. above every other role the bot is actually capable of moving.
    A bot can never place a role at or above its own top role, so
    "above everything it can manage" means immediately beneath it."""

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


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
settings_store = SettingsStore()
auto_delete_service = AutoDeleteService(bot, settings_store)
member_counter_service = MemberCounterService(bot, settings_store)
temp_voice_service = TempVoiceService(bot)
role_enforcer_service = RoleEnforcerService(bot)
settings_panel: SettingsPanel | None = None
bootstrapped = False


@bot.event
async def on_ready():
    global bootstrapped, settings_panel
    try:
        commits = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            text=True,
        ).strip()
    except Exception:
        commits = "?"
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(f"version {commits}"),
    )
    if not bootstrapped:
        settings_panel = SettingsPanel(
            bot,
            settings_store,
            auto_delete_service,
            member_counter_service,
        )
        bot.add_view(settings_panel.view)
        await bot.load_extension("commands")
        await bot.tree.sync()
        auto_delete_service.start()
        member_counter_service.start()
        role_enforcer_service.start()
        await settings_panel.ensure_panel()
        await member_counter_service.update_all()
        await temp_voice_service.start()
        bootstrapped = True
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id)


@bot.event
async def on_member_join(member: discord.Member):
    await send_welcome(member, settings_store)
    await member_counter_service.update_guild(member.guild)


@bot.event
async def on_member_remove(member: discord.Member):
    await member_counter_service.update_guild(member.guild)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    await temp_voice_service.on_voice_state_update(member, before, after)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if await delete_non_bot_settings_message(message):
        return
    if await delete_if_blacklisted(message, settings_store):
        return
    await send_auto_response(message, settings_store)
    auto_delete_service.schedule_cleanup_for_message(message)
    await bot.process_commands(message)


def main():
    token = os.getenv("TOKEN")
    if not token:
        raise RuntimeError("TOKEN is not set.")
    bot.run(token)


if __name__ == "__main__":
    main()