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

@bot.command()
async def hi(ctx, *, text: str):
    await ctx.send(text)
    await ctx.message.delete()

@bot.event
async def on_ready():
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game("hihi")
    )

    print(f"Logged in as {bot.user} ({bot.user.id})")


slur_list = ["nigger", "faggot", "nigga", "andre"]


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content = message.content.lower()

    for slur in slur_list:
        if slur in content:
            await message.delete()

            await message.author.timeout(
                datetime.timedelta(minutes=1),
                reason="Used a banned word"
            )

            await message.channel.send(
                f"{message.author.mention} dont say slurs"
            )

            break

    await bot.process_commands(message)


bot.run(os.getenv("TOKEN"))
