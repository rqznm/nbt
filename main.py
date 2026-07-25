import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game("hihi")
    )

    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if "nigger" in message.content.lower():
        await message.channel.send("yes")

    await bot.process_commands(message)


bot.run(os.getenv("TOKEN"))
