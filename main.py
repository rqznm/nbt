import os
import datetime

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
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game("am i even fucking working? (2)")
    )

    print(f"Logged in as {bot.user} ({bot.user.id})")


slur_list = ["nigger", "faggot", "nigga", "andre"]


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content = message.content.lower()

    if content:
        await message.channel.send(message.content)
        await message.delete()

    for slur in slur_list:
        if slur in content:
            await message.author.timeout(
                datetime.timedelta(minutes=1),
            break


bot.run(os.getenv("TOKEN"))
