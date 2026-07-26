import os
import datetime
import subprocess

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    try:
        commits = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            text=True
        ).strip()
    except Exception:
        commits = "?"

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(f"version {commits}")
    )

    print(f"Logged in as {bot.user} ({bot.user.id})")

@bot.event
async def on_member_join(member):
    channel = bot.get_channel(1530727587989426336)

    if channel:
        await channel.send(f"welcome to necro's server {member.mention}")
        member.kick(1140192603221020725)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content = message.content.lower()

    slur_list = ["nigger", "faggot", "nigga", "fag"]

    for slur in slur_list:
        if slur in content:
            await message.delete()

            if message.author.guild_permissions.moderate_members:
                return

            await message.author.timeout(datetime.timedelta(minutes=1))
            return

    if message.content.startswith("!"):
        text = message.content[1:]
        
        if text:
            await message.channel.send(text)

        await message.delete()

    await bot.process_commands(message)

bot.run(os.getenv("TOKEN"))
