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
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
DB = os.getenv("DB_PATH", "cyan_economy.db")
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "5"))
MIN_BET = int(os.getenv("MIN_BET", "10"))
MAX_BET = int(os.getenv("MAX_BET", "100000"))
DAILY_AMOUNT = int(os.getenv("DAILY_AMOUNT", "50"))
GUILD_ID = os.getenv("GUILD_ID")

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

def info_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title="CYAN — Gambling Minigames & Rewards",
        description=(
            "**Play**: `/coinflip`, `/slots`, `/mines`\n"
            "**Economy**: `/daily`, `/balance`, `/leaderboard`\n"
            "**Redeem**: `/redeem` (staff will review)\n\n"
            "All payouts are **manual** and subject to server rules."
        ),
        color=0x18a558
    )
    e.set_footer(text=guild.name)
    return e

# ------------ Button View for Approvals ------------
class RedeemReviewView(discord.ui.View):
    def __init__(self, request_id: int, user_id: int, amount: int, *, timeout: float | None = 600):
        super().__init__(timeout=timeout)
        self.request_id = request_id
        self.user_id = user_id
        self.amount = amount

    async def _ensure_admin(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return False
        return True

    async def _mark(self, status: str, interaction: discord.Interaction, note: str):
        # check pending
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("SELECT status FROM redeems WHERE id=?", (self.request_id,))
            r = c.fetchone()
            if not r or r[0] != "pending":
                await interaction.response.send_message("Already processed.", ephemeral=True)
                return
            c.execute("UPDATE redeems SET status=?, reason=? WHERE id=?", (status, note, self.request_id))
            conn.commit()

        # notify user
        try:
            user = await bot.fetch_user(self.user_id)
            await user.send(f"Your redeem request #{self.request_id} for {self.amount} CYAN was **{status.upper()}**. Note: {note}")
        except:
            pass

        # disable buttons
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        await interaction.message.edit(view=self)
        await interaction.response.send_message(f"Request #{self.request_id} {status}.", ephemeral=True)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_admin(interaction): return
        await self._mark("approved", interaction, "approved by button")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="🛑")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_admin(interaction): return
        await self._mark("denied", interaction, "denied by button")

# ------------ Events ------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    init_db()
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await bot.tree.sync(guild=guild)
            print(f"Slash commands synced to guild {GUILD_ID}")
        else:
            await bot.tree.sync()
            print("Slash commands globally synced (may take ~1 hour).")
    except Exception as e:
        print("Sync error:", e)

# ------------ Slash Commands ------------
@bot.tree.command(description="Show your CYAN balance")
async def balance(interaction: discord.Interaction):
    bal = await get_balance(interaction.user.id)
    await interaction.response.send_message(f"Your balance: **{bal} CYAN**", ephemeral=True)

@bot.tree.command(description="Claim daily CYAN")
async def daily(interaction: discord.Interaction):
    async with db_lock:
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("SELECT last_daily, balance FROM users WHERE user_id=?", (interaction.user.id,))
            r = c.fetchone()
            now = datetime.now(timezone.utc)
            if r:
                last = r[0]; bal = r[1]
                if last and now - datetime.fromisoformat(last) < timedelta(hours=24):
                    await interaction.response.send_message("You already claimed in the last 24h.", ephemeral=True)
                    return
            else:
                bal = 0
            bal += DAILY_AMOUNT
            c.execute("INSERT INTO users(user_id,balance,last_daily) VALUES(?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET balance=?, last_daily=?",
                      (interaction.user.id, bal, now.isoformat(), bal, now.isoformat()))
            conn.commit()
    await add_transaction(interaction.user.id, "daily", DAILY_AMOUNT, "claimed daily")
    await interaction.response.send_message(f"✅ Daily: **{DAILY_AMOUNT} CYAN** — New balance **{bal}**", ephemeral=True)

@bot.tree.command(description="Flip a coin for CYAN")
@app_commands.describe(bet="Amount to bet", choice="heads or tails")
async def coinflip(interaction: discord.Interaction, bet: int, choice: str):
    bet = clamp_bet(int(bet))
    choice = choice.lower()
    if choice not in ("heads", "tails", "h", "t"):
        await interaction.response.send_message("Use heads/tails.", ephemeral=True); return
    bal = await get_balance(interaction.user.id)
    if bet > bal:
        await interaction.response.send_message("Not enough CYAN.", ephemeral=True); return
    result = random.choice(["heads","tails"])
    win = choice.startswith(result[0])
    if win:
        new_bal = bal + bet
        await add_transaction(interaction.user.id, "coinflip_win", bet, f"choice {choice} result {result}")
        msg = f"You won. Coin: **{result}**. +{bet} CYAN"
    else:
        new_bal = bal - bet
        await add_transaction(interaction.user.id, "coinflip_loss", -bet, f"choice {choice} result {result}")
        msg = f"You lost. Coin: **{result}**. -{bet} CYAN"
    await set_balance(interaction.user.id, new_bal)
    await interaction.response.send_message(f"{msg}\nBalance: **{new_bal} CYAN**")

SLOTS_SYMBOLS = ["🍒","🍋","🍊","⭐","7"]

@bot.tree.command(description="Spin slots for CYAN")
@app_commands.describe(bet="Amount to bet")
async def slots(interaction: discord.Interaction, bet: int):
    bet = clamp_bet(int(bet))
    bal = await get_balance(interaction.user.id)
    if bet > bal:
        await interaction.response.send_message("Not enough CYAN.", ephemeral=True); return
    reel = [random.choice(SLOTS_SYMBOLS) for _ in range(3)]
    if len(set(reel)) == 1: multiplier = 10
    elif any(reel.count(s) == 2 for s in reel): multiplier = 2
    else: multiplier = 0
    if multiplier > 0:
        win = bet * multiplier; new_bal = bal + win
        await add_transaction(interaction.user.id, "slots_win", win, f"{reel}")
        text = f"You won **{win} CYAN** — {' '.join(reel)}"
    else:
        new_bal = bal - bet
        await add_transaction(interaction.user.id, "slots_loss", -bet, f"{reel}")
        text = f"You lost **{bet} CYAN** — {' '.join(reel)}"
    await set_balance(interaction.user.id, new_bal)
    await interaction.response.send_message(f"{text}\nBalance: **{new_bal} CYAN**")

@bot.tree.command(description="Show leaderboard")
async def leaderboard(interaction: discord.Interaction, top: int = 10):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?", (top,))
        rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No balances yet.", ephemeral=True); return
    lines = []
    for i, (uid, bal) in enumerate(rows, start=1):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else str(uid)
        lines.append(f"{i}. {name} — {bal} CYAN")
    await interaction.response.send_message("**Top balances**\n" + "\n".join(lines))

# ------------ Info panel ------------
@bot.tree.command(description="Set info channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setinfochannel(interaction: discord.Interaction, channel: discord.TextChannel):
    setting_set("info_channel_id", str(channel.id))
    await interaction.response.send_message(f"Info channel set to {channel.mention}.", ephemeral=True)

@bot.tree.command(description="Post the info panel")
@app_commands.checks.has_permissions(manage_guild=True)
async def postinfo(interaction: discord.Interaction):
    ch_id = setting_get("info_channel_id")
    if not ch_id:
        await interaction.response.send_message("Set an info channel first with `/setinfochannel`.", ephemeral=True); return
    ch = interaction.guild.get_channel(int(ch_id))
    if not ch:
        await interaction.response.send_message("Saved channel not found.", ephemeral=True); return
    msg = await ch.send(embed=info_embed(interaction.guild))
    try: await msg.pin()
    except: pass
    await interaction.response.send_message(f"Posted in {ch.mention}.", ephemeral=True)

# ------------ Staff / Redeem ------------
@bot.tree.command(description="Set staff review channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setstaffchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    setting_set("staff_channel_id", str(channel.id))
    await interaction.response.send_message(f"Staff channel set to {channel.mention}.", ephemeral=True)

@bot.tree.command(description="Redeem CYAN (sends for staff approval)")
@app_commands.describe(amount="Amount to redeem", reason="Optional note")
async def redeem(interaction: discord.Interaction, amount: int, reason: str = ""):
    amount = int(amount)
    bal = await get_balance(interaction.user.id)
    if amount <= 0 or amount > bal:
        await interaction.response.send_message("Invalid amount or insufficient funds.", ephemeral=True); return
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
            embed = discord.Embed(
                title="Redeem Request",
                description=f"User: {interaction.user} ({interaction.user.id})\nAmount: {amount} CYAN\nID: {rid}\nReason: {reason}",
                color=0x18a558
            )
            view = RedeemReviewView(request_id=rid, user_id=interaction.user.id, amount=amount)
            await ch.send(embed=embed, view=view)

    await interaction.response.send_message(f"Redeem request `#{rid}` submitted. Staff will review.", ephemeral=True)

# ------------ Run ------------
def main():
    token = os.getenv("CYAN_TOKEN")
    if not token:
        raise RuntimeError("CYAN_TOKEN not set in environment")
    bot.run(token)

if __name__ == "__main__":
    main()
