# bot_slash.py — CYAN Gambling Bot (slash + GUI + global rewards + tickets + owner tools)
# - Tickets go into a chosen CATEGORY via /setticketcategory
# - GUI-only games (Coinflip, Slots) + Mines game
# - Global Rewards: /addreward /removereward /listrewards
# - /redeem -> staff Approve/Deny (with reason) -> auto ticket in category + Close button
# - Admin: /setinfochannel /postinfo /setstaffchannel /setticketcategory /sync
# - Owner: /setcyan, /resetcmds2
# - Data reset: DB is wiped on every restart AND on /sync
# - PUBLIC MODE: all messages are visible (no ephemeral responses)

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

# Rate-limit friendly sync guard
SYNC_COOLDOWN_SECS = int(os.getenv("SYNC_COOLDOWN_SECS", "300"))
SYNC_LOCK = asyncio.Lock()

# =========================
# 2) BOT INIT
# =========================
intents = discord.Intents.default()
intents.message_content = True  # optional
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)
db_lock = asyncio.Lock()

# For instant guild sync (used in setup_hook)
GUILD_INT = int(GUILD_ID)
GUILD_OBJ = discord.Object(id=GUILD_INT)

# =========================
# 3) DB + HELPERS
# =========================
def reset_db():
    """Delete the DB file for a clean, ephemeral economy."""
    try:
        if os.path.exists(DB):
            os.remove(DB)
            print("[DB] Removed existing DB for fresh start.")
    except Exception as e:
        print(f"[DB] Failed to remove DB: {e!r}")
    init_db()

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

def get_last_sync_ts() -> float:
    val = setting_get("last_sync_ts", "0")
    try: return float(val)
    except: return 0.0

def set_last_sync_ts(ts: float):
    setting_set("last_sync_ts", str(ts))

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
            "**Play (GUI):** `/casino` → Set Bet, Coinflip, Slots, Mines, Redeem\n"
            "**Economy:** `/daily`, `/balance`, `/leaderboard`\n"
            "**Rewards:** `/listrewards` then `/redeem`\n\n"
            "All payouts are **manual** and staff-reviewed."
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
            return await interaction.response.send_message("Admins only.")
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("UPDATE redeems SET status=? WHERE id=?", ("completed", self.redeem_id))
            conn.commit()
        await interaction.response.send_message("Ticket marked complete. Deleting in 3 seconds…")
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"Redeem #{self.redeem_id} completed")
        except:
            pass

class RedeemReviewView(discord.ui.View):
    """Staff Approve / Deny for a redeem request; on approve, opens a ticket channel under configured category."""
    def __init__(self, request_id: int, user_id: int, amount: int, reward_id: int, *, timeout: Optional[float] = 900):
        super().__init__(timeout=timeout)
        self.request_id = request_id
        self.user_id = user_id
        self.amount = amount
        self.reward_id = reward_id
    async def _ensure_admin(self, interaction: discord.Interaction) -> bool:
        if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Admins only.")
            return False
        return True
    async def _open_ticket(self, interaction: discord.Interaction, note: str):
        guild = interaction.guild
        member = guild.get_member(self.user_id) or await guild.fetch_member(self.user_id)

        # Resolve category if configured
        cat_id = setting_get("ticket_category_id")
        category = None
        if cat_id:
            try:
                category = guild.get_channel(int(cat_id))
                if not isinstance(category, discord.CategoryChannel):
                    category = None
            except:
                category = None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True, send_messages=True, read_message_history=True)
        }
        name = f"ticket-{member.name}-{self.request_id}".lower()[:95]
        ch = await guild.create_text_channel(
            name=name,
            overwrites=overwrites,
            category=category,  # None -> top level if not set
            reason=f"Redeem #{self.request_id} approved"
        )
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
                return await interaction.response.send_message("Already processed.")
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
            await interaction.response.send_message("Approved. Opening ticket…")
            await self._open_ticket(interaction, note)
        else:
            await interaction.response.send_message("Denied.")
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
            await interaction.response.send_message("Enter a valid number.")

class MinesView(discord.ui.View):
    """
    Simple Mines (5 tiles, 1 bomb).
    - Player stakes current bet (checked up-front).
    - Each safe reveal increases multiplier.
    - Cash Out to win bet * multiplier; bomb = lose bet.
    """
    MULTIPLIERS = [1.25, 1.55, 1.95, 2.50]  # after 1,2,3,4 safe picks

    def __init__(self, user_id: int, bet: int, *, timeout: Optional[float]=240):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.bet = clamp_bet(bet)
        self.revealed = 0
        self.game_over = False
        self.tiles = [0,1,2,3,4]
        self.bomb = random.choice(self.tiles)

        for idx in range(5):
            self.add_item(self._tile_button(idx))
        self.add_item(self._cashout_button())

    def _tile_button(self, idx:int):
        @discord.ui.button(label=f"Tile {idx+1}", style=discord.ButtonStyle.primary)
        async def tile(interaction: discord.Interaction, _btn: discord.ui.Button, _idx=idx):
            if not await self._guard(interaction): return
            if self.game_over:
                return await interaction.response.send_message("Game finished.")

            if self.revealed == 0:
                bal = await get_balance(self.user_id)
                if self.bet > bal:
                    return await interaction.response.send_message("Not enough CYAN for that bet.")

            if _idx == self.bomb:
                bal = await get_balance(self.user_id)
                await set_balance(self.user_id, bal - self.bet)
                await add_transaction(self.user_id, "mines_loss", -self.bet, f"bomb at tile { _idx+1 }")
                self.game_over = True
                for child in self.children:
                    if isinstance(child, discord.ui.Button):
                        child.disabled = True
                await interaction.response.edit_message(
                    content=f"💣 **BOOM!** You hit the bomb. Lost **-{self.bet} CYAN**.",
                    view=self
                )
                return
            else:
                self.revealed += 1
                for child in self.children:
                    if isinstance(child, discord.ui.Button) and child.label == f"Tile { _idx+1 }":
                        child.disabled = True
                        child.style = discord.ButtonStyle.success
                        child.emoji = "✅"
                if self.revealed >= 4:
                    win = int(self.bet * self.MULTIPLIERS[3])
                    bal = await get_balance(self.user_id)
                    await set_balance(self.user_id, bal + win)
                    await add_transaction(self.user_id, "mines_win", win, f"auto cashout {self.revealed} safe")
                    self.game_over = True
                    for child in self.children:
                        if isinstance(child, discord.ui.Button):
                            child.disabled = True
                    return await interaction.response.edit_message(
                        content=f"🏆 **All safe!** Auto cashout for **+{win} CYAN**.",
                        view=self
                    )
                mult = self.MULTIPLIERS[self.revealed-1]
                await interaction.response.edit_message(
                    content=(f"🟩 Safe! Revealed **{self.revealed}** tile(s). "
                             f"Current cashout: **{self.bet} × {mult:.2f} = {int(self.bet*mult)} CYAN**.\n"
                             f"Pick another tile or press **Cash Out**."),
                    view=self
                )
        return tile

    def _cashout_button(self):
        @discord.ui.button(label="Cash Out", style=discord.ButtonStyle.secondary, emoji="💰")
        async def cashout(interaction: discord.Interaction, _btn: discord.ui.Button):
            if not await self._guard(interaction): return
            if self.game_over:
                return await interaction.response.send_message("Game finished.")
            if self.revealed == 0:
                return await interaction.response.send_message("Reveal at least one tile before cashing out.")
            mult = self.MULTIPLIERS[self.revealed-1]
            win = int(self.bet * mult)
            bal = await get_balance(self.user_id)
            await set_balance(self.user_id, bal + win)
            await add_transaction(self.user_id, "mines_win", win, f"cashout at {self.revealed} safe")
            self.game_over = True
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            await interaction.response.edit_message(
                content=f"💰 **Cashed out!** **+{win} CYAN** (mult {mult:.2f}, safe {self.revealed}).",
                view=self
            )
        return cashout

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This Mines board isn't yours. Use `/casino`.")
            return False
        return True

class CasinoMenuView(discord.ui.View):
    def __init__(self, user_id: int, bet: Optional[int] = None, timeout: Optional[float] = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.bet = bet or MIN_BET
    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This panel belongs to someone else. Use `/casino`.")
            return False
        return True
    @discord.ui.button(label="Set Bet", style=discord.ButtonStyle.secondary, emoji="🧾")
    async def set_bet(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        async def apply_bet(ix: discord.Interaction, bet_val: int):
            self.bet = bet_val
            await ix.response.send_message(f"Bet set to **{self.bet} CYAN**.")
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
    @discord.ui.button(label="Play Mines", style=discord.ButtonStyle.secondary, emoji="🧨")
    async def mines(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        view = MinesView(user_id=self.user_id, bet=self.bet)
        await interaction.response.send_message(
            content=(f"🧨 **Mines** started! Bet **{self.bet} CYAN**.\n"
                     f"Reveal tiles, then **Cash Out** before you hit the bomb."),
            view=view,
        )
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
                    return await ix.response.send_message("Enter a valid reward ID.")
                with sqlite3.connect(DB) as conn:
                    c = conn.cursor()
                    c.execute("SELECT cost_cyan, robux FROM rewards WHERE id=?", (rid,))
                    row = c.fetchone()
                if not row:
                    return await ix.response.send_message("Reward not found.")
                cost, robux = row
                bal = await get_balance(ix.user.id)
                if cost > bal:
                    return await ix.response.send_message("Not enough CYAN for that reward.")
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
                await ix.response.send_message(f"Redeem request `#{request_id}` submitted.")
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
            return await interaction.response.send_message("Not enough CYAN for that bet.")
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
        await interaction.response.send_message(f"{msg}\nBalance: **{new_bal} CYAN**")
    async def _do_slots(self, interaction: discord.Interaction):
        bal = await get_balance(self.user_id)
        bet = clamp_bet(self.bet)
        if bet > bal:
            return await interaction.response.send_message("Not enough CYAN for that bet.")
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
        await interaction.response.send_message(f"{text}\nBalance: **{new_bal} CYAN**")

# =========================
# 5) EVENTS — setup_hook (instant publish) + on_ready (db reset + init)
# =========================
@bot.event
async def setup_hook():
    # Make sure DB + tables exist BEFORE anything reads "settings"
    init_db()

    # Log local commands (from code)
    local_cmds = bot.tree.get_commands()
    print(f"[SETUP] Local commands: {len(local_cmds)} -> {[c.name for c in local_cmds]}")

    # Publish commands to your server instantly
    bot.tree.copy_global_to(guild=GUILD_OBJ)
    synced = await bot.tree.sync(guild=GUILD_OBJ)
    print(f"[SETUP] Synced {len(synced)} commands to guild {GUILD_ID} -> {[c.name for c in synced]}")


    # Debounce guild sync (avoid 429)
    async with SYNC_LOCK:
        from time import time
        now = time()
        last = get_last_sync_ts()
        if now - last < SYNC_COOLDOWN_SECS:
            wait_left = int(SYNC_COOLDOWN_SECS - (now - last))
            print(f"[SETUP] Skip sync (debounce {wait_left}s left).")
            return

        local_cmds = bot.tree.get_commands()
        print(f"[SETUP] Local commands: {len(local_cmds)} -> {[c.name for c in local_cmds]}")
        bot.tree.copy_global_to(guild=GUILD_OBJ)
        synced = await bot.tree.sync(guild=GUILD_OBJ)
        set_last_sync_ts(now)
        print(f"[SETUP] Synced {len(synced)} commands to guild {GUILD_ID} -> {[c.name for c in synced]}")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    # Always fresh DB on restart
    reset_db()

# =========================
# 6) SLASH COMMANDS (PUBLIC RESPONSES)
# =========================
@bot.tree.command(description="Show your CYAN balance")
async def balance(interaction: discord.Interaction):
    bal = await get_balance(interaction.user.id)
    await interaction.response.send_message(f"{interaction.user.mention} Your balance: **{bal} CYAN**")

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
                    return await interaction.response.send_message(f"{interaction.user.mention} You already claimed in the last 24h.")
            else:
                bal = 0
            bal += DAILY_AMOUNT
            c.execute("INSERT INTO users(user_id,balance,last_daily) VALUES(?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET balance=?, last_daily=?",
                      (interaction.user.id, bal, now.isoformat(), bal, now.isoformat()))
            conn.commit()
    await add_transaction(interaction.user.id, "daily", DAILY_AMOUNT, "claimed daily")
    await interaction.response.send_message(f"✅ {interaction.user.mention} Daily: **{DAILY_AMOUNT} CYAN** — New balance **{bal}**")

@bot.tree.command(description="Show leaderboard")
async def leaderboard(interaction: discord.Interaction, top: int = 10):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?", (top,))
        rows = c.fetchall()
    if not rows:
        return await interaction.response.send_message("No balances yet.")
    lines = []
    for i, (uid, bal) in enumerate(rows, start=1):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else f"<@{uid}>"
        lines.append(f"{i}. {name} — {bal} CYAN")
    await interaction.response.send_message("**Top balances**\n" + "\n".join(lines))

# ---- Rewards (GLOBAL)
@bot.tree.command(description="List available rewards (global) — ID, CYAN cost → Robux")
async def listrewards(interaction: discord.Interaction):
    rows = list_rewards()
    if not rows:
        return await interaction.response.send_message("No rewards configured yet.")
    msg = "**Rewards (Global):**\n" + "\n".join([f"ID `{rid}` — Cost **{cost} CYAN** → **{rbx} Robux**" for rid, cost, rbx in rows])
    await interaction.response.send_message(msg)

@bot.tree.command(description="Admin: add a new reward (global)")
@app_commands.describe(cost_cyan="CYAN cost", robux="Robux delivered")
@app_commands.checks.has_permissions(manage_guild=True)
async def addreward(interaction: discord.Interaction, cost_cyan: int, robux: int):
    if cost_cyan <= 0 or robux <= 0:
        return await interaction.response.send_message("Values must be positive.")
    rid = add_reward(cost_cyan, robux)
    await interaction.response.send_message(f"✅ Added reward ID `{rid}` — **{cost_cyan} CYAN → {robux} Robux** (global)")

@bot.tree.command(description="Admin: remove a reward (global)")
@app_commands.describe(reward_id="ID from /listrewards")
@app_commands.checks.has_permissions(manage_guild=True)
async def removereward(interaction: discord.Interaction, reward_id: int):
    ok = remove_reward(reward_id)
    if ok:
        await interaction.response.send_message(f"🗑️ Removed reward `{reward_id}`.")
    else:
        await interaction.response.send_message("Reward not found.")

# ---- Staff/info channels
@bot.tree.command(description="Set info channel (help post)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setinfochannel(interaction: discord.Interaction, channel: discord.TextChannel):
    setting_set("info_channel_id", str(channel.id))
    await interaction.response.send_message(f"Info channel set to {channel.mention}.")

@bot.tree.command(description="Post the info panel")
@app_commands.checks.has_permissions(manage_guild=True)
async def postinfo(interaction: discord.Interaction):
    ch_id = setting_get("info_channel_id")
    if not ch_id:
        return await interaction.response.send_message("Set an info channel first with `/setinfochannel`.")
    ch = interaction.guild.get_channel(int(ch_id))
    if not ch:
        return await interaction.response.send_message("Saved channel not found.")
    msg = await ch.send(embed=info_embed(interaction.guild))
    try: await msg.pin()
    except: pass
    await interaction.response.send_message(f"Posted in {ch.mention}.")

@bot.tree.command(description="Set staff review channel (receives redeem requests)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setstaffchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    setting_set("staff_channel_id", str(channel.id))
    await interaction.response.send_message(f"Staff channel set to {channel.mention}.")

@bot.tree.command(description="Set ticket category for redeem tickets")
@app_commands.checks.has_permissions(manage_guild=True)
async def setticketcategory(interaction: discord.Interaction, category: discord.CategoryChannel):
    setting_set("ticket_category_id", str(category.id))
    await interaction.response.send_message(f"Ticket category set to **{category.name}**.")

# ---- Redeem (by reward ID) — user
@bot.tree.command(description="Redeem a reward by ID")
@app_commands.describe(reward_id="Reward ID (see /listrewards)", note="Optional note for staff")
async def redeem(interaction: discord.Interaction, reward_id: int, note: str = ""):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT cost_cyan, robux FROM rewards WHERE id=?", (reward_id,))
        row = c.fetchone()
    if not row:
        return await interaction.response.send_message("Reward not found. Use /listrewards.")
    cost, robux = row
    bal = await get_balance(interaction.user.id)
    if cost > bal:
        return await interaction.response.send_message("Not enough CYAN for that reward.")
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
    await interaction.response.send_message(f"✅ Redeem request `#{request_id}` submitted. Staff will review.")

# ---- Owner-only
@bot.tree.command(description="Owner-only: set a user's CYAN balance")
@app_commands.describe(user="User to set", amount="New balance (>= 0)")
async def setcyan(interaction: discord.Interaction, user: discord.Member, amount: int):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ Owner only.")
    if amount < 0:
        return await interaction.response.send_message("Amount must be 0 or higher.")
    await set_balance(user.id, int(amount))
    await add_transaction(user.id, "owner_set", amount, f"set by {interaction.user.id}")
    await interaction.response.send_message(
        f"✅ Set **{user.display_name}** balance to **{amount} CYAN**."
    )

# ---- GUI opener
@bot.tree.command(description="Open the CYAN casino panel")
async def casino(interaction: discord.Interaction):
    bal = await get_balance(interaction.user.id)
    view = CasinoMenuView(user_id=interaction.user.id, bet=MIN_BET)
    e = discord.Embed(
        title="🎲 CYAN Casino",
        description=(f"**Player:** {interaction.user.mention}\n"
                     f"**Balance:** `{bal} CYAN`\n"
                     f"**Bet:** `{view.bet} CYAN`\n\n"
                     "Use the buttons below to play."),
        color=0x18a558
    )
    await interaction.response.send_message(embed=e, view=view)

# ---- Admin sync helper (also wipes DB to satisfy 'data gone on sync')
@bot.tree.command(description="Force-sync slash commands (admin) — also resets all data")
@app_commands.checks.has_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    async with SYNC_LOCK:
        from time import time
        now = time()
        last = get_last_sync_ts()
        if now - last < SYNC_COOLDOWN_SECS:
            wait_left = int(SYNC_COOLDOWN_SECS - (now - last))
            return await interaction.response.send_message(
                f"⏱️ Recently synced. Try again in ~{wait_left}s.")

        try:
            bot.tree.copy_global_to(guild=GUILD_OBJ)
            synced = await bot.tree.sync(guild=GUILD_OBJ)
            set_last_sync_ts(now)
            reset_db()
            await interaction.response.send_message(
                f"✅ Synced {len(synced)} commands to guild {GUILD_ID} and **reset all data**."
            )
        except Exception as e:
            await interaction.response.send_message(f"Sync error: {e!r}")

# ---- Owner reset (clears GUILD & GLOBAL, then republishes guild-only)
@bot.tree.command(description="Owner: reset guild & GLOBAL slash commands (also resets data)")
async def resetcmds2(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.")

    await bot.http.bulk_upsert_guild_commands(bot.application_id, GUILD_INT, [])
    await bot.http.bulk_upsert_global_commands(bot.application_id, [])

    bot.tree.copy_global_to(guild=GUILD_OBJ)
    synced = await bot.tree.sync(guild=GUILD_OBJ)

    reset_db()

    await interaction.response.send_message(
        f"Republished {len(synced)} commands and **reset all data**."
    )

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
