import os
import datetime
import subprocess
import urllib.parse
import discord
import wikipedia
from discord.ext import commands
from dotenv import load_dotenv
load_dotenv()
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# NOTE: This is a Discord attachment CDN URL. These URLs contain ex/is/hm
# query params and expire (historically ~24h after being issued). Once it
# expires, this command will start posting a dead link. See the
# hypergamy_chart_file() alternative below for a version that never expires.
HYPERGAMY_CHART_URL = (
    "https://cdn.discordapp.com/attachments/1456410041883033601/"
    "1530737650598936686/Hypergamy-illustration.svg.webp"
    "?ex=6a66aa07&is=6a655887&hm=9c1135df22225a325ff27fae36b5fd31f47d2d04131213ea7c3852a5f6d83a1d"
)

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
@bot.tree.command(name="define", description="Define a word")
async def define(interaction: discord.Interaction, word: str):
    await interaction.response.defer()
    try:
        wikipedia.set_lang("en")
        result = wikipedia.summary(
            word,
            sentences=3
        )
        await interaction.followup.send(
            f"**{word.title()}**\n\n{result}"
        )
    except wikipedia.exceptions.DisambiguationError as e:
        await interaction.followup.send(
            f"Multiple results found:\n{', '.join(e.options[:5])}"
        )
    except wikipedia.exceptions.PageError:
        await interaction.followup.send(
            f"Could not find `{word}` on Wikipedia."
        )
    except Exception as e:
        await interaction.followup.send(
            f"Error: `{e}`"
        )
@bot.tree.command(name="search", description="Search")
async def search(interaction: discord.Interaction, query: str):
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.fr/search?q={encoded}"
    await interaction.response.send_message(url)
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
    content = message.content.lower()
    slur_list = [
        "nigger",
        "faggot",
        "nigga",
        "fag"
    ]
    for slur in slur_list:
        if slur in content:
            await message.delete()
            if message.author.guild_permissions.moderate_members:
                return
            await message.author.timeout(
                datetime.timedelta(minutes=1),
                reason="Using prohibited language"
            )
            return

    # Handle !hypergamy-chart before the generic "!" echo below, otherwise
    # it would just echo the literal text "hypergamy-chart" instead of
    # posting the image link.
    if content.strip() == "!hypergamy-chart":
        await message.channel.send(HYPERGAMY_CHART_URL)
        await message.delete()
        return

    if message.content.startswith("!"):
        text = message.content[1:]
        if text:
            await message.channel.send(text)
        await message.delete()
    await bot.process_commands(message)
bot.run(os.getenv("TOKEN"))
