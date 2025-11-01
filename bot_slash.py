import os
import sqlite3
import asyncio
import random
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ------------ Config ------------
load_dotenv()
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")  # not really used for slash commands
DB = os.getenv("DB_PATH", "cyan_economy.db")
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "5"))
MIN_BET = int(os.getenv("MIN_BET", "10"))
MAX_BET = int(os.getenv("MAX_BET", "100000"))
DAILY_AMOUNT = int(os.getenv("DAILY_AMOUNT", "50"))
GUILD_ID = os.getenv("GUILD_ID")  # optional for instant guild sync

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)
db_lock = asyncio.Lock()


# ------------ DB helpers ------------
def init_db():
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users(
                     user_id INTEGER PRIMARY KEY,
                     balance INTEGER DEFAULT 0,
                     last_daily TEXT,
                     age_ok INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS transactions(
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER,
                     type TEXT,
                     amount INTEGER,
                     ts TEXT,
                     details TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS redeems(
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER,
                     amount INTEGER,
                     status TEXT,
                     ts TEXT,
                     reason TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS games(
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER,
                     game TEXT,
                     bet INTEGER,
                     result TEXT,
                     ts TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
                     key TEXT PRIMARY KEY,
                     value TEXT)""")
        conn.commit()


def setting_get(key:str, default=None):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=?", (key,))
        r = c.fetchone()
        return r[0] if r else default


def setting_set(key:str, value:str):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                  (key, value, value))
        conn.commit()


async def get_balance(user_id:int):
    async with db_lock:
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
            r = c.fetchone()
            return r[0] if r else 0


async def set_balance(user_id:int, new_bal:int):
    async with db_lock:
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO users(user_id,balance) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET balance=?",
                      (user_id, new_bal, new_bal))
            conn.commit()


async def add_transaction(user_id:int, ttype:str, amount:int, details:str=""):
    ts = datetime.now(timezone.utc).isoformat()
    async with db_lock:
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO transactions(user_id,type,amount,ts,details) VALUES(?,?,?,?,?)",
                      (user_id, ttype, amount, ts, details))
            conn.commit()


# ------------ Utilities ------------
def now_ts():
    return datetime.now(timezone.utc).isoformat()


def clamp_bet(bet:int):
    if bet < MIN_BET: return MIN_BET
    if bet > MAX_BET: return MAX_BET
    return bet


# ------------ Events ------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    init_db()
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await bot.tree.sync(guild=guild)
            print(f"Slash commands synced instantly to guild {GUILD_ID}")
        else:
            await bot.tree.sync()
            print("Slash commands synced globally (may take ~1 hour).")
    except Exception as e:
        print("Sync error:", e)


# ------------ Slash Commands ------------

@bot.tree.command(description="Show your CYAN balance")
async def balance(interaction: discord.Interaction):
    bal = await get_balance(interaction.user.id)
    await interaction.response.send_message(f"Your balance: **{bal} CYAN**", ephemeral=True)


@bot.tree.command(description="Claim your daily CYAN reward")
async def daily(interaction: discord.Interaction):
    async with db_lock:
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("SELECT last_daily, balance FROM users WHERE user_id=?", (interaction.user.id,))
            r = c.fetchone()
            now = datetime.now(timezone.utc)
            if r:
                last = r[0]
                bal = r[1]
                if last:
                    last_dt = datetime.fromisoformat(last)
                    if now - last_dt < timedelta(hours=24):
                        await interaction.response.send_message("You already claimed daily in the last 24h.", ephemeral=True)
                        return
            else:
                bal = 0
            bal += DAILY_AMOUNT
            c.execute("INSERT INTO users(user_id,balance,last_daily) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET balance=?, last_daily=?",
                      (interaction.user.id, bal, now.isoformat(), bal, now.isoformat()))
            conn.commit()
    await add_transaction(interaction.user.id, "daily", DAILY_AMOUNT, "claimed daily")
    await interaction.response.send_message(f"✅ Daily claimed: **{DAILY_AMOUNT} CYAN** — New balance: **{bal} CYAN**", ephemeral=True)


@bot.tree.command(description="Play a coinflip game")
async def coinflip(interaction: discord.Interaction, bet: int, choice: str):
    bet = clamp_bet(int(bet))
    choice = choice.lower()
    if choice not in ("heads", "tails", "h", "t"):
        await interaction.response.send_message("Choices: heads or tails.", ephemeral=True)
        return
    bal = await get_balance(interaction.user.id)
    if bet > bal:
        await interaction.response.send_message("Not enough CYAN.", ephemeral=True)
        return
    result = random.choice(["heads", "tails"])
    win = choice.startswith(result[0])
    if win:
        new_bal = bal + bet
        await add_transaction(interaction.user.id, "coinflip_win", bet, f"choice {choice} result {result}")
        msg = f"You won! Coin: **{result}**. +{bet} CYAN"
    else:
        new_bal = bal - bet
        await add_transaction(interaction.user.id, "coinflip_loss", -bet, f"choice {choice} result {result}")
        msg = f"You lost. Coin: **{result}**. -{bet} CYAN"
    await set_balance(interaction.user.id, new_bal)
    await interaction.response.send_message(f"{msg}\nBalance: **{new_bal} CYAN**")


SLOTS_SYMBOLS = ["🍒","🍋","🍊","⭐","7"]

@bot.tree.command(description="Spin slot machine")
async def slots(interaction: discord.Interaction, bet: int):
    bet = clamp_bet(int(bet))
    bal = await get_balance(interaction.user.id)
    if bet > bal:
        await interaction.response.send_message("Not enough CYAN.", ephemeral=True)
        return
    reel = [random.choice(SLOTS_SYMBOLS) for _ in range(3)]
    if len(set(reel)) == 1: multiplier = 10
    elif any(reel.count(s) == 2 for s in reel): multiplier = 2
    else: multiplier = 0
    if multiplier > 0:
        win = bet * multiplier
        new_bal = bal + win
        await add_transaction(interaction.user.id, "slots_win", win, f"{reel}")
        text = f"You won **{win} CYAN** — {' '.join(reel)}"
    else:
        new_bal = bal - bet
        await add_transaction(interaction.user.id, "slots_loss", -bet, f"{reel}")
        text = f"You lost **{bet} CYAN** — {' '.join(reel)}"
    await set_balance(interaction.user.id, new_bal)
    await interaction.response.send_message(f"{text}\nBalance: **{new_bal} CYAN**")


@bot.tree.command(description="Request CYAN redemption")
async def redeem(interaction: discord.Interaction, amount: int, reason: str = ""):
    amount = int(amount)
    bal = await get_balance(interaction.user.id)
    if amount <= 0 or amount > bal:
        await interaction.response.send_message("Invalid amount or insufficient funds.", ephemeral=True)
        return
    ts = now_ts()
    async with db_lock:
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO redeems(user_id, amount, status, ts, reason) VALUES(?,?,?,?,?)",
                      (interaction.user.id, amount, "pending", ts, reason))
            rid = c.lastrowid
            conn.commit()
    await add_transaction(interaction.user.id, "redeem_request", -amount, f"request id {rid} reason:{reason}")
    staff_channel_id = setting_get("staff_channel_id")
    if staff_channel_id:
        ch = interaction.guild.get_channel(int(staff_channel_id))
        if ch:
            embed = discord.Embed(title="Redeem Request", description=f"User: {interaction.user} ({interaction.user.id})\nAmount: {amount} CYAN\nID: {rid}\nReason: {reason}")
            await ch.send(embed=embed)
    await interaction.response.send_message(f"Redeem request `#{rid}` submitted for review.", ephemeral=True)


@bot.tree.command(description="Show top balances")
async def leaderboard(interaction: discord.Interaction, top: int = 10):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?", (top,))
        rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No balances yet.", ephemeral=True)
        return
    lines = []
    for i, (uid, bal) in enumerate(rows, start=1):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else str(uid)
        lines.append(f"{i}. {name} — {bal} CYAN")
    await interaction.response.send_message("**Top balances**\n" + "\n".join(lines))


# ------------ RUN ------------
def main():
    token = os.getenv("CYAN_TOKEN")
    if not token:
        raise RuntimeError("CYAN_TOKEN not set in env vars")
    bot.run(token)


if __name__ == "__main__":
    main()
