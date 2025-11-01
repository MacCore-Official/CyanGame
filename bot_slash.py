# bot_slash.py — CYAN Gambling Bot (slash + GUI + global rewards + tickets + owner tools)
# - Hardcoded guild sync for instant commands (GUILD_ID below)
# - Owner-only /setcyan (OWNER_ID below)
# - Global Rewards: /addreward /removereward /listrewards
# - /redeem (by reward ID) -> staff Approve/Deny with reason -> auto ticket with Close button
# - GUI panel: /casino (Set Bet, Coinflip Heads/Tails, Slots, Redeem modal, Refresh)
# - Admin helpers: /setinfochannel /postinfo /setstaffchannel /sync
# - Owner helper: /resetcmds (fix command signature mismatches fast)

import os
import sqlite3
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, List

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

# Hardcode guild for instant sync + your owner ID
GUILD_ID = "1431742078483828758"     # your server ID
OWNER_ID = 1269145029943758899       # your Discord user ID

# =========================
# 2) BOT INIT
# =========================
intents = discord.Intents.default()
intents.message_content = True  # optional; silences the warning for non-slash usage
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
                     amount INTEGER,      -- CYAN charged
                     status TEXT,         -- pending/approved/denied/completed
                     ts TEXT,
                     reason TEXT,
                     reward_id INTEGER,
                     ticket_channel_id INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS rewards(
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     cost_cyan INTEGER NOT NULL,
                     robux INTEGER NOT NULL)""")
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
            "**Play**: `/coinflip`, `/slots` (mines via GUI later)\n"
            "**Economy**: `/daily`, `/balance`, `/leaderboard`\n"
            "**Rewards**: `/listrewards`, then `/redeem` to request\n\n"
            "Open the GUI with **/casino**. All payouts are **manual** and staff-reviewed."
        ),
        color=0x18a558
    )
    e.set_footer(text=guild.name)
    return e

# Rewards helpers (GLOBAL)
def list_rewards() -> List[tuple]:
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT id, cost_cyan, robux FROM rewards ORDER BY cost_cyan ASC")
        return c.fetchall()

def add_reward(cost:int, robux:int) -> int:
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO rewards(cost_cyan, robux) VALUES(?,?)", (cost, robux))
        rid = c.lastrowid
        conn.commit()
        return rid

def remove_reward(rid:int) -> bool:
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM rewards WHERE id=?", (rid,))
        conn.commit()
        return c.rowcount > 0

# =========================
# 4) VIEWS (tickets, approvals, casino GUI)
# =========================
class ApprovalReasonModal(discord.ui.Modal, title="Approval Note / Instructions"):
    note = discord.ui.TextInput(
        label="Approval note (visible to user)",
        placeholder="e.g., We'll deliver within 24 hours.",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=300
    )
    def __init__(self, callback_on_submit):
        super().__init__()
        self.callback_on_submit = callback_on_submit
    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.callback_on_submit(interaction, self.note.value or "")

class DenyReasonModal(discord.ui.Modal, title="Denial Reason"):
    reason = discord.ui.TextInput(
        label="Reason (visible to user)",
        placeholder="e.g., Not enough balance / invalid request",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=300
    )
    def __init__(self, callback_on_submit):
        super().__init__()
        self.callback_on_submit = callback_on_submit
    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.callback_on_submit(interaction, self.reason.value)

class TicketCloseView(discord.ui.View):
    def __init__(self, user_id:int, redeem_id:int, *, timeout: Optional[float]=None):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.redeem_id = redeem_id
    async def _is_admin(self, interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator
    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._is_admin(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("UPDATE redeems SET status=? WHERE id=?", ("completed", self.redeem_id))
            conn.commit()
        await interaction.response.send_message("Ticket marked complete. Deleting in 3 seconds…", ephemeral=True)
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"Redeem #{self.redeem_id} completed")
        except:
            pass

class RedeemReviewView(discord.ui.View):
    """Staff Approve / Deny for a redeem request; on approve, opens a ticket channel."""
    def __init__(self, request_id: int, user_id: int, amount: int, reward_id: int, *, timeout: Optional[float] = 900):
        super().__init__(timeout=timeout)
        self.request_id = request_id
        self.user_id = user_id
        self.amount = amount
        self.reward_id = reward_id
    async def _ensure_admin(self, interaction: discord.Interaction) -> bool:
        if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return False
        return True
    async def _open_ticket(self, interaction: discord.Interaction, note: str):
        guild = interaction.guild
        member = guild.get_member(self.user_id) or await guild.fetch_member(self.user_id)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True, send_messages=True, read_message_history=True)
        }
        name = f"ticket-{member.name}-{self.request_id}".lower()[:95]
        ch = await guild.create_text_channel(name=name, overwrites=overwrites,
                                             reason=f"Redeem #{self.request_id} approved")
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("UPDATE redeems SET ticket_channel_id=? WHERE id=?", (ch.id, self.request_id))
            conn.commit()
        embed = discord.Embed(
            title=f"Redeem Ticket #{self.request_id}",
            description=(f"User: {member.mention}\n"
                         f"Amount charged: **{self.amount} CYAN**\n"
                         f"Reward ID: **{self.reward_id}**\n\n"
                         f"**Staff Note:** {note or 'No note'}"),
            color=0x18a558
        )
        await ch.send(content=member.mention, embed=embed,
                      view=TicketCloseView(user_id=self.user_id, redeem_id=self.request_id))
    async def _finalize(self, interaction: discord.Interaction, status: str, note: str):
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("SELECT status FROM redeems WHERE id=?", (self.request_id,))
            r = c.fetchone()
            if not r or r[0] != "pending":
                return await interaction.response.send_message("Already processed.", ephemeral=True)
            c.execute("UPDATE redeems SET status=?, reason=? WHERE id=?", (status, note, self.request_id))
            conn.commit()
        try:
            user = await bot.fetch_user(self.user_id)
            await user.send(f"Your redeem request #{self.request_id} was **{status.upper()}**.\nNote: {note or '—'}")
        except:
            pass
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except:
            pass
        if status == "approved":
            await interaction.response.send_message("Approved. Opening ticket…", ephemeral=True)
            await self._open_ticket(interaction, note)
        else:
            await interaction.response.send_message("Denied.", ephemeral=True)
    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ensure_admin(interaction): return
        async def _ok(ix: discord.Interaction, note: str):
            await self._finalize(ix, "approved", note)
        await interaction.response.send_modal(ApprovalReasonModal(_ok))
    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="🛑")
    async def deny_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ensure_admin(interaction): return
        async def _deny(ix: discord.Interaction, reason: str):
            await self._finalize(ix, "denied", reason)
        await interaction.response.send_modal(DenyReasonModal(_deny))

# ---- Casino GUI (player)
class BetModal(discord.ui.Modal, title="Set Bet"):
    bet = discord.ui.TextInput(label="Bet amount (CYAN)", placeholder="e.g. 100", required=True, min_length=1, max_length=10)
    def __init__(self, on_set):
        super().__init__()
        self.on_set = on_set
    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet_int = clamp_bet(int(self.bet.value))
            await self.on_set(interaction, bet_int)
        except:
            await interaction.response.send_message("Enter a valid number.", ephemeral=True)

class CasinoMenuView(discord.ui.View):
    def __init__(self, user_id: int, bet: Optional[int] = None, timeout: Optional[float] = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.bet = bet or MIN_BET
    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This panel belongs to someone else. Use `/casino`.", ephemeral=True)
            return False
        return True
    @discord.ui.button(label="Set Bet", style=discord.ButtonStyle.secondary, emoji="🧾")
    async def set_bet(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        async def apply_bet(ix: discord.Interaction, bet_val: int):
            self.bet = bet_val
            await ix.response.send_message(f"Bet set to **{self.bet} CYAN**.", ephemeral=True)
        await interaction.response.send_modal(BetModal(on_set=apply_bet))
    @discord.ui.button(label="Coinflip: Heads", style=discord.ButtonStyle.primary, emoji="🪙")
    async def coin_heads(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._do_coinflip(interaction, "heads")
    @discord.ui.button(label="Coinflip: Tails", style=discord.ButtonStyle.primary, emoji="🪙")
    async def coin_tails(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._do_coinflip(interaction, "tails")
    @discord.ui.button(label="Spin Slots", style=discord.ButtonStyle.success, emoji="🎰")
    async def slots(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._do_slots(interaction)
    @discord.ui.button(label="Redeem…", style=discord.ButtonStyle.secondary, emoji="📥")
    async def redeem_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        class RedeemModal(discord.ui.Modal, title="Redeem CYAN → Robux"):
            reward_id = discord.ui.TextInput(label="Reward ID (see /listrewards)", placeholder="e.g. 1", required=True)
            note = discord.ui.TextInput(label="Note (optional)", style=discord.TextStyle.paragraph, required=False, max_length=200)
            async def on_submit(self, ix: discord.Interaction):
                try:
                    rid = int(self.reward_id.value)
                except:
                    return await ix.response.send_message("Enter a valid reward ID.", ephemeral=True)
                with sqlite3.connect(DB) as conn:
                    c = conn.cursor()
                    c.execute("SELECT cost_cyan, robux FROM rewards WHERE id=?", (rid,))
                    row = c.fetchone()
                if not row:
                    return await ix.response.send_message("Reward not found.", ephemeral=True)
                cost, robux = row
                bal = await get_balance(ix.user.id)
                if cost > bal:
                    return await ix.response.send_message("Not enough CYAN for that reward.", ephemeral=True)
                await set_balance(ix.user.id, bal - cost)
                await add_transaction(ix.user.id, "redeem_request", -cost, f"reward_id {rid} robux {robux}")
                ts = now_ts()
                with sqlite3.connect(DB) as conn:
                    c = conn.cursor()
                    c.execute("INSERT INTO redeems(user_id, amount, status, ts, reason, reward_id, ticket_channel_id) VALUES(?,?,?,?,?,?,?)",
                              (ix.user.id, cost, "pending", ts, self.note.value or "", rid, None))
                    request_id = c.lastrowid
                    conn.commit()
                staff_channel_id = setting_get("staff_channel_id")
                if staff_channel_id:
                    ch = ix.guild.get_channel(int(staff_channel_id))
                    if ch:
                        embed = discord.Embed(
                            title="Redeem Request",
                            description=(f"User: {ix.user} ({ix.user.id})\n"
                                         f"Cost: **{cost} CYAN**\n"
                                         f"Reward ID: **{rid}** (Robux {robux})\n"
                                         f"ID: **{request_id}**\n"
                                         f"Note: {self.note.value or '—'}"),
                            color=0x18a558
                        )
                        await ch.send(embed=embed, view=RedeemReviewView(request_id=request_id, user_id=ix.user.id, amount=cost, reward_id=rid))
                await ix.response.send_message(f"Redeem request `#{request_id}` submitted.", ephemeral=True)
        await interaction.response.send_modal(RedeemModal())
    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        bal = await get_balance(self.user_id)
        e = discord.Embed(
            title="🎲 CYAN Casino",
            description=(f"**Balance:** `{bal} CYAN`\n"
                         f"**Bet:** `{self.bet} CYAN`\n\n"
                         "Use the buttons below to play."),
            color=0x18a558
        )
        await interaction.response.edit_message(embed=e, view=self)
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
        if len(set(reel)) == 1: mult = 10
        elif any(reel.count(s) == 2 for s in reel): mult = 2
        else: mult = 0
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
        gid = int(GUILD_ID)
        guild = discord.Object(id=gid)
        synced = await bot.tree.sync(guild=guild)
        print(f"[SYNC] Guild sync to {GUILD_ID}. Count={len(synced)}")
        print("[SYNC] Names:", [c.name for c in synced])
    except Exception as e:
        print("[SYNC] Error:", repr(e))

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
                    return await interaction.response.send_message("You already claimed in the last 24h.", ephemeral=True)
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
        return await interaction.response.send_message("Use heads/tails.", ephemeral=True)
    bal = await get_balance(interaction.user.id)
    if bet > bal:
        return await interaction.response.send_message("Not enough CYAN.", ephemeral=True)
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
        return await interaction.response.send_message("Not enough CYAN.", ephemeral=True)
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
        return await interaction.response.send_message("No balances yet.", ephemeral=True)
    lines = []
    for i, (uid, bal) in enumerate(rows, start=1):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else str(uid)
        lines.append(f"{i}. {name} — {bal} CYAN")
    await interaction.response.send_message("**Top balances**\n" + "\n".join(lines))

# ---- Rewards (GLOBAL)
@bot.tree.command(description="List available rewards (global) — ID, CYAN cost → Robux")
async def listrewards(interaction: discord.Interaction):
    rows = list_rewards()
    if not rows:
        return await interaction.response.send_message("No rewards configured yet.", ephemeral=True)
    msg = "**Rewards (Global):**\n" + "\n".join([f"ID `{rid}` — Cost **{cost} CYAN** → **{rbx} Robux**" for rid, cost, rbx in rows])
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(description="Admin: add a new reward (global)")
@app_commands.describe(cost_cyan="CYAN cost", robux="Robux delivered")
@app_commands.checks.has_permissions(manage_guild=True)
async def addreward(interaction: discord.Interaction, cost_cyan: int, robux: int):
    if cost_cyan <= 0 or robux <= 0:
        return await interaction.response.send_message("Values must be positive.", ephemeral=True)
    rid = add_reward(cost_cyan, robux)
    await interaction.response.send_message(f"✅ Added reward ID `{rid}` — **{cost_cyan} CYAN → {robux} Robux** (global)", ephemeral=True)

@bot.tree.command(description="Admin: remove a reward (global)")
@app_commands.describe(reward_id="ID from /listrewards")
@app_commands.checks.has_permissions(manage_guild=True)
async def removereward(interaction: discord.Interaction, reward_id: int):
    ok = remove_reward(reward_id)
    if ok:
        await interaction.response.send_message(f"🗑️ Removed reward `{reward_id}`.", ephemeral=True)
    else:
        await interaction.response.send_message("Reward not found.", ephemeral=True)

# ---- Staff/info channels
@bot.tree.command(description="Set info channel (help post)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setinfochannel(interaction: discord.Interaction, channel: discord.TextChannel):
    setting_set("info_channel_id", str(channel.id))
    await interaction.response.send_message(f"Info channel set to {channel.mention}.", ephemeral=True)

@bot.tree.command(description="Post the info panel")
@app_commands.checks.has_permissions(manage_guild=True)
async def postinfo(interaction: discord.Interaction):
    ch_id = setting_get("info_channel_id")
    if not ch_id:
        return await interaction.response.send_message("Set an info channel first with `/setinfochannel`.", ephemeral=True)
    ch = interaction.guild.get_channel(int(ch_id))
    if not ch:
        return await interaction.response.send_message("Saved channel not found.", ephemeral=True)
    msg = await ch.send(embed=info_embed(interaction.guild))
    try: await msg.pin()
    except: pass
    await interaction.response.send_message(f"Posted in {ch.mention}.", ephemeral=True)

@bot.tree.command(description="Set staff review channel (receives redeem requests)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setstaffchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    setting_set("staff_channel_id", str(channel.id))
    await interaction.response.send_message(f"Staff channel set to {channel.mention}.", ephemeral=True)

# ---- Redeem (by reward ID) — user
@bot.tree.command(description="Redeem a reward by ID")
@app_commands.describe(reward_id="Reward ID (see /listrewards)", note="Optional note for staff")
async def redeem(interaction: discord.Interaction, reward_id: int, note: str = ""):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT cost_cyan, robux FROM rewards WHERE id=?", (reward_id,))
        row = c.fetchone()
    if not row:
        return await interaction.response.send_message("Reward not found. Use /listrewards.", ephemeral=True)
    cost, robux = row
    bal = await get_balance(interaction.user.id)
    if cost > bal:
        return await interaction.response.send_message("Not enough CYAN for that reward.", ephemeral=True)
    await set_balance(interaction.user.id, bal - cost)
    await add_transaction(interaction.user.id, "redeem_request", -cost, f"reward_id {reward_id} robux {robux}")
    ts = now_ts()
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO redeems(user_id, amount, status, ts, reason, reward_id, ticket_channel_id) VALUES(?,?,?,?,?,?,?)",
                  (interaction.user.id, cost, "pending", ts, note or "", reward_id, None))
        request_id = c.lastrowid
        conn.commit()
    staff_channel_id = setting_get("staff_channel_id")
    if staff_channel_id:
        ch = interaction.guild.get_channel(int(staff_channel_id))
        if ch:
            embed = discord.Embed(
                title="Redeem Request",
                description=(f"User: {interaction.user} ({interaction.user.id})\n"
                             f"Cost: **{cost} CYAN**\n"
                             f"Reward ID: **{reward_id}** (Robux {robux})\n"
                             f"ID: **{request_id}**\n"
                             f"Note: {note or '—'}"),
                color=0x18a558
            )
            await ch.send(embed=embed, view=RedeemReviewView(request_id=request_id, user_id=interaction.user.id, amount=cost, reward_id=reward_id))
    await interaction.response.send_message(f"✅ Redeem request `#{request_id}` submitted. Staff will review.", ephemeral=True)

# ---- Owner-only
@bot.tree.command(description="Owner-only: set a user's CYAN balance")
@app_commands.describe(user="User to set", amount="New balance (>= 0)")
async def setcyan(interaction: discord.Interaction, user: discord.Member, amount: int):
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

# ---- GUI opener
@bot.tree.command(description="Open the CYAN casino panel")
async def casino(interaction: discord.Interaction):
    bal = await get_balance(interaction.user.id)
    view = CasinoMenuView(user_id=interaction.user.id, bet=MIN_BET)
    e = discord.Embed(
        title="🎲 CYAN Casino",
        description=(f"**Balance:** `{bal} CYAN`\n"
                     f"**Bet:** `{view.bet} CYAN`\n\n"
                     "Use the buttons below to play."),
        color=0x18a558
    )
    await interaction.response.send_message(embed=e, view=view, ephemeral=True)

# ---- Admin sync helper
@bot.tree.command(description="Force-sync slash commands (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    try:
        gid = int(GUILD_ID)
        synced = await bot.tree.sync(guild=discord.Object(id=gid))
        await interaction.response.send_message(f"Synced to guild {GUILD_ID} (count={len(synced)}).", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Sync error: {e!r}", ephemeral=True)

# ---- Owner reset (fix CommandSignatureMismatch instantly)
@bot.tree.command(description="Owner: reset guild slash commands")
async def resetcmds(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    gid = int(GUILD_ID)
    await bot.http.bulk_upsert_guild_commands(bot.application_id, gid, [])  # wipe live guild cmds
    synced = await bot.tree.sync(guild=discord.Object(id=gid))              # republish from code
    await interaction.response.send_message(f"Republished {len(synced)} commands.", ephemeral=True)

# =========================
# 7) RUN
# =========================
def main():
    token = os.getenv("CYAN_TOKEN") or os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("CYAN_TOKEN not set in environment")
    bot.run(token)

if __name__ == "__main__":
    main()
