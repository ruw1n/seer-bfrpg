import os
import nextcord
from nextcord.ext import commands
from dotenv import load_dotenv, find_dotenv

# Load environment variables from .env file
env_path = find_dotenv()
if not env_path:
    raise RuntimeError("Couldn't find a .env file. Put one next to bot.py.")
load_dotenv(env_path)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing. Check .env contents and spelling.")

# Enable the necessary intents
intents = nextcord.Intents.default()
intents.message_content = True  # Enable message content intent

# Initialize the bot with intents
bot = commands.Bot(command_prefix="!", intents=intents)

# Load extensions (cogs)
initial_extensions = [
    "cogs.roll",
    "cogs.sheet",
    "cogs.stats",
    "cogs.players",
    "cogs.progression",
    "cogs.combat",
    "cogs.initiative",
    "cogs.spells",
    "cogs.crafting",
    "cogs.strongholds",
    "cogs.npc",
]

# Event when the bot is ready
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

# Load the cogs (extensions)
for ext in initial_extensions:
    try:
        bot.load_extension(ext)
        print(f"Loaded: {ext}")
    except Exception as e:
        print(f"Failed to load {ext}: {e}")


bot.run(TOKEN)

