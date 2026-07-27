import logging
import os
import subprocess

import discord
from discord.ext import commands
from dotenv import load_dotenv

from auto_delete import AutoDeleteService
from blacklisted_words import delete_if_blacklisted
from member_counter import MemberCounterService
from settings_channel_guard import delete_non_bot_settings_message
from settings_panel import SettingsPanel
from settings_store import SettingsStore
from welcome import send_welcome


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("necro_bot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
settings_store = SettingsStore()
auto_delete_service = AutoDeleteService(bot, settings_store)
member_counter_service = MemberCounterService(bot, settings_store)
settings_panel = SettingsPanel(
    bot,
    settings_store,
    auto_delete_service,
    member_counter_service,
)
bot.add_view(settings_panel.view)


@bot.event
async def on_ready():
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
    await bot.tree.sync()
    auto_delete_service.start()
    member_counter_service.start()
    await settings_panel.ensure_panel()
    await member_counter_service.update_all()
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id)


@bot.event
async def on_member_join(member: discord.Member):
    await send_welcome(member, settings_store)
    await member_counter_service.update_guild(member.guild)


@bot.event
async def on_member_remove(member: discord.Member):
    await member_counter_service.update_guild(member.guild)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if await delete_non_bot_settings_message(message):
        return

    if await delete_if_blacklisted(message, settings_store):
        return

    await auto_delete_service.maybe_cleanup_for_message(message)
    await bot.process_commands(message)


token = os.getenv("TOKEN")
if not token:
    raise RuntimeError("TOKEN is not set.")

bot.run(token)
