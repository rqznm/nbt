import os
import re
import datetime
import subprocess
import urllib.parse

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

SLUR_PATTERN = re.compile(r"\b(nigger|nigga|faggot|fag)s?\b", re.IGNORECASE)

WIKIPEDIA_SEARCH_URL = "https://en.wikipedia.org/w/rest.php/v1/search/page"
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
WIKIPEDIA_HEADERS = {"User-Agent": "DiscordWikipediaBot/1.0"}


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


async def _fetch_wikipedia(session, query):
    try:
        async with session.get(
            WIKIPEDIA_SEARCH_URL, params={"q": query, "limit": 1}
        ) as resp:
            if resp.status != 200:
                return None
            search_data = await resp.json()
    except Exception:
        return None

    pages = search_data.get("pages") or []
    if not pages:
        return None
    key = pages[0].get("key") or pages[0].get("title")
    if not key:
        return None

    url = WIKIPEDIA_SUMMARY_URL.format(urllib.parse.quote(key))
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            summary = await resp.json()
    except Exception:
        return None

    if summary.get("type") == "disambiguation":
        return {"disambiguation": True}

    extract = (summary.get("extract") or "").strip()
    page_url = (summary.get("content_urls") or {}).get("desktop", {}).get("page")
    if not extract or not page_url:
        return None

    return {"title": summary.get("title") or key, "text": extract, "url": page_url}


@bot.tree.command(name="wikipedia", description="Look up a Wikipedia article")
async def wikipedia(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    clean_query = query.strip()

    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout, headers=WIKIPEDIA_HEADERS) as session:
        result = await _fetch_wikipedia(session, clean_query)

    if not result:
        await interaction.followup.send(f"Could not find a Wikipedia article for `{clean_query}`.")
        return

    if "disambiguation" in result:
        await interaction.followup.send(
            f"**{clean_query}** could mean several things on Wikipedia — try being more specific."
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
