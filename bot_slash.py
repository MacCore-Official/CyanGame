# bot_slash.py  — FULL FIXED FILE (CYAN_TOKEN, slash + GUI + button approve)

import os
import sqlite3
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# =========================
# 1) CONFIG / ENV
# =========================
load_dotenv()
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
DB = os.getenv("DB_PATH", "cyan_economy.db")
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "5"))
MIN_BET = int(os.getenv("MIN_BET", "10"))
MAX_BET = int(os.getenv("MAX_BET", "100000"))
DAILY_AMOUNT = int(os.getenv("DAILY_AMOUNT", "50"))
GUILD_ID = os.getenv("GUILD_ID")  # optional for instant guild sync

# =========================
# 2) BOT INIT  (must be before any @bot.tree.command)
# =========================
# =========================
# 2) BOT INIT
# =========================
intents = discord.Intents.default()
intents.message_content = True  # enable message content intent

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)
db_lock = asyncio.Lock()



# =========================
# 3) DB + HELPERS
# =========================
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

async def get_balance(user_id:int) -> int:
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

def now_ts():
    return datetime.now(timezone.utc).isoformat()

def clamp_bet(bet:int) -> int:
    if bet < MIN_BET: return MIN_BET
    if bet > MAX_BET: return MAX_BET
    return bet

def info_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title="CYAN — Gambling Minigames & Rewards",
        description=(
            "**Play**: `/coinflip`, `/slots`, `/mines` (mines via GUI soon)\n"
            "**Economy**: `/daily`, `/balance`, `/leaderboard`\n"
            "**Redeem**: `/redeem` (staff review; manual payouts)\n\n"
            "Open the GUI with **/casino**.\n"
            "All payouts are **manual** and subject to server rules."
        ),
        color=0x18a558
    )
    e.set_footer(text=guild.name)
    return e


    # Only you can use this command
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ Owner only.", ephemeral=True)

    if amount < 0:
        return await interaction.response.send_message("Amount must be 0 or higher.", ephemeral=True)

    await set_balance(user.id, int(amount))
    await add_transaction(user.id, "owner_set", amount, f"set by {interaction.user.id}")

    await interaction.response.send_message(
        f"✅ Set **{user.display_name}** balance to **{amount} CYAN**.",
        ephemeral=True
    )

# =========================
# 4) BUTTON VIEWS (Admin approve + Casino GUI)
# =========================
class RedeemReviewView(discord.ui.View):
    """Staff-facing buttons to Approve/Deny a redeem request."""
    def __init__(self, request_id: int, user_id: int, amount: int, *, timeout: Optional[float] = 600):
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
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("SELECT status FROM redeems WHERE id=?", (self.request_id,))
            r = c.fetchone()
            if not r or r[0] != "pending":
                await interaction.response.send_message("Already processed.", ephemeral=True)
                return
            c.execute("UPDATE redeems SET status=?, reason=? WHERE id=?", (status, note, self.request_id))
            conn.commit()

        # DM user
        try:
            user = await bot.fetch_user(self.user_id)
            await user.send(f"Your redeem request #{self.request_id} for {self.amount} CYAN was **{status.upper()}**. Note: {note}")
        except:
            pass

        # Disable buttons
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        try:
            await interaction.message.edit(view=self)
        except:
            pass
        await interaction.response.send_message(f"Request #{self.request_id} {status}.", ephemeral=True)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._ensure_admin(interaction): return
        await self._mark("approved", interaction, "approved by button")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="🛑")
    async def deny_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._ensure_admin(interaction): return
        await self._mark("denied", interaction, "denied by button")


class BetModal(discord.ui.Modal, title="Set Bet"):
    bet = discord.ui.TextInput(label="Bet amount (CYAN)", placeholder="e.g. 100", required=True, min_length=1, max_length=10)

    def __init__(self, on_set):
        super().__init__()
        self.on_set = on_set  # callback(interaction, bet_int)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet_int = clamp_bet(int(self.bet.value))
            await self.on_set(interaction, bet_int)
        except:
            await interaction.response.send_message("Enter a valid number.", ephemeral=True)


class CasinoMenuView(discord.ui.View):
    """Player-facing casino GUI: set bet, coinflip (heads/tails), slots spin, redeem, refresh."""
    def __init__(self, user_id: int, bet: Optional[int] = None, timeout: Optional[float] = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.bet = bet or MIN_BET

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This panel belongs to someone else. Use `/casino`.", ephemeral=True)
            return False
        return True

    async def _refresh_embed(self, interaction: discord.Interaction):
        bal = await get_balance(self.user_id)
        e = discord.Embed(
            title="🎲 CYAN Casino",
            description=(
                f"**Balance:** `{bal} CYAN`\n"
                f"**Bet:** `{self.bet} CYAN`  *(use **Set Bet**)*\n\n"
                "Play with the buttons below."
            ),
            color=0x18a558
        )
        e.set_footer(text="Use /casino again if this panel times out.")
        # For component interactions, edit the original ephemeral message:
        await interaction.response.edit_message(embed=e, view=self)

    # ---------- Buttons ----------
    @discord.ui.button(label="Set Bet", style=discord.ButtonStyle.secondary, emoji="🧾")
    async def set_bet(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction): return

        async def apply_bet(ix: discord.Interaction, bet_val: int):
            self.bet = bet_val
            await ix.response.send_message(f"Bet set to **{self.bet} CYAN**.", ephemeral=True)
            # Cannot edit here (modal response already used); user can hit Refresh

        await interaction.response.send_modal(BetModal(on_set=apply_bet))

    @discord.ui.button(label="Coinflip: Heads", style=discord.ButtonStyle.primary, emoji="🪙")
    async def coin_heads(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._do_coinflip(interaction, "heads")

    @discord.ui.button(label="Coinflip: Tails", style=discord.ButtonStyle.primary, emoji="🪙")
    async def coin_tails(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._do_coinflip(interaction, "tails")

    @discord.ui.button(label="Spin Slots", style=discord.ButtonStyle.success, emoji="🎰")
    async def slots(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._do_slots(interaction)

    @discord.ui.button(label="Redeem…", style=discord.ButtonStyle.secondary, emoji="📥")
    async def redeem_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction): return

        class RedeemModal(discord.ui.Modal, title="Redeem CYAN"):
            amount = discord.ui.TextInput(label="Amount", placeholder="e.g. 500", required=True)
            reason = discord.ui.TextInput(label="Reason (optional)", style=discord.TextStyle.paragraph, required=False, max_length=200)

            async def on_submit(self, ix: discord.Interaction):
                try:
                    amt = int(self.amount.value)
                except:
                    return await ix.response.send_message("Enter a valid number.", ephemeral=True)
                bal = await get_balance(ix.user.id)
                if amt <= 0 or amt > bal:
                    return await ix.response.send_message("Invalid amount or insufficient funds.", ephemeral=True)
                ts = now_ts()
                with sqlite3.connect(DB) as conn:
                    c = conn.cursor()
                    c.execute("INSERT INTO redeems(user_id, amount, status, ts, reason) VALUES(?,?,?,?,?)",
                              (ix.user.id, amt, "pending", ts, self.reason.value or ""))
                    rid = c.lastrowid
                    conn.commit()
                await add_transaction(ix.user.id, "redeem_request", -amt, f"request id {rid} reason:{self.reason.value or ''}")
                staff_channel_id = setting_get("staff_channel_id")
                if staff_channel_id:
                    ch = ix.guild.get_channel(int(staff_channel_id))
                    if ch:
                        embed = discord.Embed(
                            title="Redeem Request",
                            description=f"User: {ix.user} ({ix.user.id})\nAmount: {amt} CYAN\nID: {rid}\nReason: {self.reason.value or ''}",
                            color=0x18a558
                        )
                        view = RedeemReviewView(request_id=rid, user_id=ix.user.id, amount=amt)
                        await ch.send(embed=embed, view=view)
                await ix.response.send_message(f"Redeem request `#{rid}` submitted.", ephemeral=True)

        await interaction.response.send_modal(RedeemModal())

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._refresh_embed(interaction)

    # ---------- Game logic ----------
    async def _do_coinflip(self, interaction: discord.Interaction, choice: str):
        bal = await get_balance(self.user_id)
        bet = clamp_bet(self.bet)
        if bet > bal:
            return await interaction.response.send_message("Not enough CYAN for that bet.", ephemeral=True)
        result = random.choice(["heads", "tails"])
        win = (choice == result)
        if win:
            new_bal = bal + bet
            await add_transaction(self.user_id, "coinflip_win", bet, f"choice {choice} result {result}")
            msg = f"🪙 **Coinflip** — You chose **{choice}**. Coin: **{result}**. You **won +{bet}**."
        else:
            new_bal = bal - bet
            await add_transaction(self.user_id, "coinflip_loss", -bet, f"choice {choice} result {result}")
            msg = f"🪙 **Coinflip** — You chose **{choice}**. Coin: **{result}**. You **lost -{bet}**."
        await set_balance(self.user_id, new_bal)
        await interaction.response.send_message(f"{msg}\nBalance: **{new_bal} CYAN**", ephemeral=True)

    async def _do_slots(self, interaction: discord.Interaction):
        bal = await get_balance(self.user_id)
        bet = clamp_bet(self.bet)
        if bet > bal:
            return await interaction.response.send_message("Not enough CYAN for that bet.", ephemeral=True)
        symbols = ["🍒","🍋","🍊","⭐","7"]
        reel = [random.choice(symbols) for _ in range(3)]
        if len(set(reel)) == 1:
            mult = 10
        elif any(reel.count(s) == 2 for s in reel):
            mult = 2
        else:
            mult = 0
        if mult:
            win = bet * mult
            new_bal = bal + win
            await add_transaction(self.user_id, "slots_win", win, f"{reel}")
            text = f"🎰 **Slots** — {' '.join(reel)} → **+{win} CYAN**"
        else:
            new_bal = bal - bet
            await add_transaction(self.user_id, "slots_loss", -bet, f"{reel}")
            text = f"🎰 **Slots** — {' '.join(reel)} → **-{bet} CYAN**"
        await set_balance(self.user_id, new_bal)
        await interaction.response.send_message(f"{text}\nBalance: **{new_bal} CYAN**", ephemeral=True)

# =========================
# 5) EVENTS
# =========================
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

# =========================
# 6) SLASH COMMANDS
# =========================
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

# Info panel + staff channel
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

# CASINO GUI opener
@bot.tree.command(description="Open the CYAN casino panel")
async def casino(interaction: discord.Interaction):
    bal = await get_balance(interaction.user.id)
    view = CasinoMenuView(user_id=interaction.user.id, bet=MIN_BET)
    e = discord.Embed(
        title="🎲 CYAN Casino",
        description=(
            f"**Balance:** `{bal} CYAN`\n"
            f"**Bet:** `{view.bet} CYAN`\n\n"
            "Use the buttons below to play."
        ),
        color=0x18a558
    )
    await interaction.response.send_message(embed=e, view=view, ephemeral=True)
# Add under other slash commands in bot_slash.py

@bot.tree.command(description="Owner-only: set a user's CYAN balance")
@app_commands.describe(user="User to set", amount="New balance (>= 0)")
async def setcyan(interaction: discord.Interaction, user: discord.Member, amount: int):
    # Allow only the specific owner user ID to run this
    OWNER_ID = 1269145029943758899
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ Owner only.", ephemeral=True)

    if amount < 0:
        return await interaction.response.send_message("Amount must be 0 or higher.", ephemeral=True)

    await set_balance(user.id, int(amount))
    await add_transaction(user.id, "owner_set", amount, f"set by {interaction.user.id}")
    await interaction.response.send_message(
        f"✅ Set **{user.display_name}** balance to **{amount} CYAN**.",
        ephemeral=True
    )
@bot.tree.command(description="Ping")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)

@bot.tree.command(description="List registered commands (debug)")
async def listcmds(interaction: discord.Interaction):
    names = [f"/{c.name}" for c in bot.tree.get_commands(guild=interaction.guild)]
    if not names:
        names = [f"/{c.name}" for c in bot.tree.get_commands()]
    await interaction.response.send_message("Commands I have: " + ", ".join(sorted(names)) or "(none)", ephemeral=True)


# =========================
# 7) RUN
# =========================
def main():
    token = os.getenv("CYAN_TOKEN")
    if not token:
        raise RuntimeError("CYAN_TOKEN not set in environment")
    bot.run(token)

if __name__ == "__main__":
    main()
