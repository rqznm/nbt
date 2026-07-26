import os
import re
import asyncio
import datetime
import subprocess

import discord
import wikipedia
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Whole-word match only (so it doesn't trip on words like "snigger"/"sniggered"),
# with an optional trailing "s" to still catch plurals.
SLUR_PATTERN = re.compile(r"\b(nigger|nigga|faggot|fag)s?\b", re.IGNORECASE)


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
    await bot.tree.sync()
    print(f"Logged in as {bot.user} ({bot.user.id})")


def _fetch_wikipedia(word):
    wikipedia.set_lang("en")
    try:
        page = wikipedia.page(word, auto_suggest=True)
    except wikipedia.exceptions.DisambiguationError as e:
        return {"disambiguation": e.options[:5]}
    except Exception:
        return None

    intro = (page.summary or "").strip()
    if not intro:
        return None

    first_paragraph = re.split(r"\n\s*\n", intro)[0].strip()
    if not first_paragraph:
        return None

    return {"title": page.title, "text": first_paragraph, "url": page.url}


@bot.tree.command(name="define", description="Define a word using Wikipedia")
async def define(interaction: discord.Interaction, word: str):
    await interaction.response.defer()
    clean_word = word.strip()

    result = await asyncio.to_thread(_fetch_wikipedia, clean_word)

    if not result:
        await interaction.followup.send(f"Could not find a definition for `{clean_word}`.")
        return

    if "disambiguation" in result:
        await interaction.followup.send(
            f"**{clean_word}** could mean several things:\n"
            + ", ".join(result["disambiguation"])
        )
        return

    text = f"**{result['title']}**\n{result['text']}\n{result['url']}"
    if len(text) > 1900:
        text = text[:1897] + "..."

    await interaction.followup.send(text)


@bot.event
async def on_member_join(member):
    channel = bot.get_channel(1530727587989426336)
    if channel:
        await channel.send(
            f"welcome to necro's server {member.mention}"
        )


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if SLUR_PATTERN.search(message.content):
        await message.delete()
        if message.author.guild_permissions.moderate_members:
            return
        await message.author.timeout(
            datetime.timedelta(minutes=1),
            reason="Using prohibited language"
        )
        return
    if message.content.startswith("!"):
        text = message.content[1:]
        if text:
            await message.channel.send(text)
        await message.delete()
    await bot.process_commands(message)


bot.run(os.getenv("TOKEN"))
