import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game("Raising the dead...")
    )

    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(os.getenv("TOKEN"))
