# bot_slash.py — CYAN Gambling Bot (Clean Standard UI)
# - GUI-only games inside /casino (Coinflip, Slots, Mines)
# - Mines difficulty picker (Easy/Normal/Hard)
# - Rewards dropdown inside GUI (no typing IDs)
# - Economy: /daily /balance /leaderboard /gift
# - Admin: /addreward /removereward /setinfochannel /postinfo /setstaffchannel /sync
# - Owner: /setcyan /resetcmds2 /backupdb
# - Persistence: set DB_PATH=/data/cyan_economy.db on Railway + mount /data volume
# - Instant guild sync via setup_hook()

import os
import sqlite3
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# =========================
# 1) CONFIG / ENV
# =========================
load_dotenv()

BOT_PREFIX   = os.getenv("BOT_PREFIX", "!")
DB           = os.getenv("DB_PATH", "cyan_economy.db")
MIN_BET      = int(os.getenv("MIN_BET", "10"))
MAX_BET      = int(os.getenv("MAX_BET", "100000"))
DAILY_AMOUNT = int(os.getenv("DAILY_AMOUNT", "50"))

# ensure folder exists (esp. when DB is /data/cyan_economy.db)
_db_dir = os.path.dirname(DB)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

GUILD_ID = "1431742078483828758"       # your Discord server ID for instant sync
OWNER_ID = 1269145029943758899         # your user ID (owner-only commands)

CYAN_COLOR = 0x00E6FF  # clean cyan color

# =========================
# 2) BOT INIT
# =========================
intents = discord.Intents.default()
intents.message_content = True  # optional; suppresses a warning
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)
db_lock = asyncio.Lock()

GUILD_INT = int(GUILD_ID)
GUILD_OBJ = discord.Object(id=GUILD_INT)

# =========================
# 3) DB + HELPERS
# =========================
def init_db():
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users(
                     user_id INTEGER PRIMARY KEY,
                     balance INTEGER DEFAULT 0,
                     last_daily TEXT)""")
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

def setting_get(key: str, default=None):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=?", (key,))
        r = c.fetchone()
        return r[0] if r else default

def setting_set(key: str, value: str):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                  (key, value, value))
        conn.commit()

async def get_balance(user_id: int) -> int:
    async with db_lock:
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
            r = c.fetchone()
            return r[0] if r else 0

async def set_balance(user_id: int, new_bal: int):
    async with db_lock:
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO users(user_id,balance) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET balance=?",
                      (user_id, new_bal, new_bal))
            conn.commit()

async def add_transaction(user_id: int, ttype: str, amount: int, details: str = ""):
    ts = datetime.now(timezone.utc).isoformat()
    async with db_lock:
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO transactions(user_id,type,amount,ts,details) VALUES(?,?,?,?,?)",
                      (user_id, ttype, amount, ts, details))
            conn.commit()

def clamp_bet(bet: int) -> int:
    if bet < MIN_BET: return MIN_BET
    if bet > MAX_BET: return MAX_BET
    return bet

# Rewards helpers (GLOBAL)
def list_rewards() -> List[Tuple[int,int,int]]:
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("SELECT id, cost_cyan, robux FROM rewards ORDER BY cost_cyan ASC")
        return c.fetchall()

def add_reward(cost: int, robux: int) -> int:
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO rewards(cost_cyan, robux) VALUES(?,?)", (cost, robux))
        rid = c.lastrowid
        conn.commit()
        return rid

def remove_reward(rid: int) -> bool:
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM rewards WHERE id=?", (rid,))
        conn.commit()
        return c.rowcount > 0

# =========================
# 4) VIEWS (Tickets, Approvals, GUI, Mines, Rewards)
# =========================
def casino_embed(user: discord.User, balance: int, bet: int) -> discord.Embed:
    e = discord.Embed(
        title="CYAN Casino",
        description=(
            "Play via buttons below.\n"
            "Games: Coinflip · Slots · Mines\n"
            "Use Rewards to redeem Robux."
        ),
        color=CYAN_COLOR
    )
    e.add_field(name="Balance", value=f"`{balance} CYAN`", inline=True)
    e.add_field(name="Bet", value=f"`{bet} CYAN`", inline=True)
    e.set_footer(text=f"Player: {user}", icon_url=user.display_avatar.url if hasattr(user.display_avatar, 'url') else discord.Embed.Empty)
    return e

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
            color=CYAN_COLOR
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

# ---- Mines (Difficulty Picker + Game)
DIFFS: Dict[str, Tuple[int,int]] = {
    "easy":   (3, 2),  # mines, multiplier
    "normal": (5, 3),
    "hard":   (8, 5),
}

class MinesView(discord.ui.View):
    def __init__(self, user_id: int, bet: int, mines_count: int, multiplier: int, size: int = 5, timeout: Optional[float] = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.bet = clamp_bet(bet)
        self.size = size
        self.multiplier = multiplier
        total = size * size
        mines_count = min(max(1, mines_count), total - 1)
        self.mines = set(random.sample(range(total), mines_count))
        self.revealed: set[int] = set()
        self.alive = True

        # 5x5 button grid
        for idx in range(total):
            btn = self._make_tile(idx)
            btn.row = idx // size
            self.add_item(btn)

    def _make_tile(self, idx: int) -> discord.ui.Button:
        # Use emoji-only to avoid "missing label" errors
        b = discord.ui.Button(
            emoji="⬛",
            style=discord.ButtonStyle.secondary,
            custom_id=f"mine_{idx}"
        )

        async def on_click(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("This game isn't yours. Use `/casino`.", ephemeral=True)
            if not self.alive:
                return await interaction.response.send_message("Game over. Open `/casino` to start again.", ephemeral=True)

            # locate the live button
            button = None
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.custom_id == b.custom_id:
                    button = child
                    break

            if idx in self.revealed:
                return await interaction.response.defer()

            if idx in self.mines:
                # lose bet (clamped to balance)
                self.alive = False
                bal = await get_balance(self.user_id)
                loss = min(self.bet, bal)
                await set_balance(self.user_id, bal - loss)
                await add_transaction(self.user_id, "mines_loss", -loss, f"hit {idx} mines={len(self.mines)}")

                for i, child in enumerate(self.children):
                    if isinstance(child, discord.ui.Button):
                        child.disabled = True
                        if i in self.mines:
                            child.style = discord.ButtonStyle.danger
                            child.emoji = "💣"
                return await interaction.response.edit_message(
                    content=f"💥 You hit a mine! **-{loss} CYAN**",
                    view=self
                )

            # safe
            self.revealed.add(idx)
            if button:
                button.style = discord.ButtonStyle.success
                button.emoji = "✅"
                button.disabled = True

            safe_left = self.size * self.size - len(self.mines) - len(self.revealed)
            if safe_left == 0:
                # cleared → win
                self.alive = False
                win = self.bet * self.multiplier
                bal = await get_balance(self.user_id)
                await set_balance(self.user_id, bal + win)
                await add_transaction(self.user_id, "mines_win", win, f"cleared mines={len(self.mines)} mult={self.multiplier}")
                for child in self.children:
                    if isinstance(child, discord.ui.Button):
                        child.disabled = True
                return await interaction.response.edit_message(
                    content=f"🎉 Cleared the board! **+{win} CYAN** (x{self.multiplier})",
                    view=self
                )
            return await interaction.response.edit_message(view=self)

        b.callback = on_click
        return b

class MinesDifficultyView(discord.ui.View):
    def __init__(self, user_id:int, bet:int, *, timeout: Optional[float]=120):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.bet = bet

    async def _start(self, interaction: discord.Interaction, key: str):
        mines, mult = DIFFS[key]
        view = MinesView(user_id=self.user_id, bet=self.bet, mines_count=mines, multiplier=mult)
        await interaction.response.edit_message(
            content=f"🧨 **Mines** — Difficulty: **{key.title()}** · Mines: **{mines}** · Payout: **x{mult}**\n"
                    f"Click safe tiles. Clear all safes to win!",
            embed=None,
            view=view
        )

    @discord.ui.button(label="Easy", style=discord.ButtonStyle.success, emoji="🟢")
    async def easy(self, interaction: discord.Interaction, _):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("Use your own panel.", ephemeral=True)
        await self._start(interaction, "easy")

    @discord.ui.button(label="Normal", style=discord.ButtonStyle.primary, emoji="🔵")
    async def normal(self, interaction: discord.Interaction, _):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("Use your own panel.", ephemeral=True)
        await self._start(interaction, "normal")

    @discord.ui.button(label="Hard", style=discord.ButtonStyle.danger, emoji="🔴")
    async def hard(self, interaction: discord.Interaction, _):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("Use your own panel.", ephemeral=True)
        await self._start(interaction, "hard")

# ---- Rewards: Select menu inside GUI
class RewardSelect(discord.ui.Select):
    def __init__(self, rows: List[Tuple[int,int,int]]):
        opts = []
        for rid, cost, robux in rows[:25]:  # max 25 options
            opts.append(discord.SelectOption(
                label=f"{robux} Robux",
                description=f"Costs {cost} CYAN",
                value=str(rid),
                emoji="🎁"
            ))
        super().__init__(placeholder="Choose a reward to redeem…", options=opts, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        rid = int(self.values[0])
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("SELECT cost_cyan, robux FROM rewards WHERE id=?", (rid,))
            row = c.fetchone()
        if not row:
            return await interaction.response.send_message("Reward not found.", ephemeral=True)
        cost, robux = row
        bal = await get_balance(interaction.user.id)
        if cost > bal:
            return await interaction.response.send_message("Not enough CYAN for that reward.", ephemeral=True)

        # charge + record request
        await set_balance(interaction.user.id, bal - cost)
        await add_transaction(interaction.user.id, "redeem_request", -cost, f"reward_id {rid} robux {robux}")
        ts = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO redeems(user_id, amount, status, ts, reason, reward_id, ticket_channel_id) VALUES(?,?,?,?,?,?,?)",
                      (interaction.user.id, cost, "pending", ts, "", rid, None))
            request_id = c.lastrowid
            conn.commit()

        staff_channel_id = setting_get("staff_channel_id")
        if staff_channel_id:
            ch = interaction.guild.get_channel(int(staff_channel_id))
            if ch:
                embed = discord.Embed(
                    title="Redeem Request",
                    description=(f"User: {interaction.user} ({interaction.user.id})\n"
                                 f"Cost: **{cost} CYAN** · Reward: **{robux} Robux** (ID {rid})\n"
                                 f"Request ID: **{request_id}**"),
                    color=CYAN_COLOR
                )
                await ch.send(embed=embed, view=RedeemReviewView(request_id=request_id, user_id=interaction.user.id, amount=cost, reward_id=rid))

        await interaction.response.send_message(f"✅ Redeem request `#{request_id}` submitted. Staff will review.", ephemeral=True)

class RewardsView(discord.ui.View):
    def __init__(self, rows: List[Tuple[int,int,int]], *, timeout: Optional[float]=120):
        super().__init__(timeout=timeout)
        if rows:
            self.add_item(RewardSelect(rows))

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
            bal = await get_balance(self.user_id)
            await ix.response.edit_message(embed=casino_embed(ix.user, bal, self.bet), view=self)
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

    @discord.ui.button(label="Mines", style=discord.ButtonStyle.secondary, emoji="🧨")
    async def mines(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        view = MinesDifficultyView(user_id=self.user_id, bet=self.bet)
        await interaction.response.send_message(
            "Pick a difficulty to start Mines:",
            view=view,
            ephemeral=True
        )

    @discord.ui.button(label="Rewards", style=discord.ButtonStyle.secondary, emoji="🎁")
    async def rewards(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        rows = list_rewards()
        if not rows:
            return await interaction.response.send_message("No rewards configured yet. Ask staff to add rewards.", ephemeral=True)
        await interaction.response.send_message("Select a reward to redeem:", view=RewardsView(rows), ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        bal = await get_balance(self.user_id)
        await interaction.response.edit_message(embed=casino_embed(interaction.user, bal, self.bet), view=self)

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
# 5) EVENTS — setup_hook (instant publish) + on_ready (login msg)
# =========================
@bot.event
async def setup_hook():
    init_db()
    local_cmds = bot.tree.get_commands()
    print(f"[SETUP] Local commands: {len(local_cmds)} -> {[c.name for c in local_cmds]}")
    bot.tree.copy_global_to(guild=GUILD_OBJ)
    synced = await bot.tree.sync(guild=GUILD_OBJ)
    print(f"[SETUP] Synced {len(synced)} commands to guild {GUILD_ID} -> {[c.name for c in synced]}")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")

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

# Gift (player → player)
@bot.tree.command(description="Gift CYAN to another user")
@app_commands.describe(user="Recipient", amount="Amount of CYAN to send (≥ 1)")
async def gift(interaction: discord.Interaction, user: discord.Member, amount: int):
    if user.id == interaction.user.id:
        return await interaction.response.send_message("You can't gift yourself.", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
    sender_bal = await get_balance(interaction.user.id)
    if amount > sender_bal:
        return await interaction.response.send_message("Not enough CYAN.", ephemeral=True)
    await set_balance(interaction.user.id, sender_bal - amount)
    recv_bal = await get_balance(user.id)
    await set_balance(user.id, recv_bal + amount)
    await add_transaction(interaction.user.id, "gift_send", -amount, f"to {user.id}")
    await add_transaction(user.id, "gift_recv", amount, f"from {interaction.user.id}")
    await interaction.response.send_message(f"🎁 Sent **{amount} CYAN** to **{user.display_name}**.", ephemeral=True)

# Rewards (Admin)
@bot.tree.command(description="Admin: add a new reward (global)")
@app_commands.describe(cost_cyan="CYAN cost", robux="Robux delivered")
@app_commands.checks.has_permissions(manage_guild=True)
async def addreward(interaction: discord.Interaction, cost_cyan: int, robux: int):
    if cost_cyan <= 0 or robux <= 0:
        return await interaction.response.send_message("Values must be positive.", ephemeral=True)
    rid = add_reward(cost_cyan, robux)
    await interaction.response.send_message(f"✅ Added reward ID `{rid}` — **{cost_cyan} CYAN → {robux} Robux** (global)", ephemeral=True)

@bot.tree.command(description="Admin: remove a reward (global)")
@app_commands.describe(reward_id="ID to remove")
@app_commands.checks.has_permissions(manage_guild=True)
async def removereward(interaction: discord.Interaction, reward_id: int):
    ok = remove_reward(reward_id)
    if ok:
        await interaction.response.send_message(f"🗑️ Removed reward `{reward_id}`.", ephemeral=True)
    else:
        await interaction.response.send_message("Reward not found.", ephemeral=True)

# Info / Staff Channels
def info_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title="CYAN — Gambling Minigames & Rewards",
        description=(
            "Open **/casino** and use buttons to play.\n"
            "Economy: `/daily`, `/balance`, `/leaderboard`, `/gift`\n"
            "Rewards: Press **Rewards** in `/casino` to pick from the list.\n\n"
            "All payouts are manual and staff-reviewed."
        ),
        color=CYAN_COLOR
    )
    e.set_footer(text=guild.name)
    return e

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

# Owner-only
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

@bot.tree.command(description="Owner: download the database file")
async def backupdb(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    try:
        await interaction.response.send_message(
            content="Here’s the current DB file.",
            file=discord.File(DB, filename=os.path.basename(DB))
        )
    except Exception as e:
        await interaction.response.send_message(f"Backup failed: {e!r}", ephemeral=True)

# Casino opener
@bot.tree.command(description="Open the CYAN casino panel")
async def casino(interaction: discord.Interaction):
    bal = await get_balance(interaction.user.id)
    view = CasinoMenuView(user_id=interaction.user.id, bet=MIN_BET)
    e = casino_embed(interaction.user, bal, view.bet)
    await interaction.response.send_message(embed=e, view=view, ephemeral=True)

# Admin sync helper
@bot.tree.command(description="Force-sync slash commands (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    try:
        synced = await bot.tree.sync(guild=GUILD_OBJ)
        await interaction.response.send_message(f"Synced to guild {GUILD_ID} (count={len(synced)}).", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Sync error: {e!r}", ephemeral=True)

# Owner reset (fallback if ever needed)
@bot.tree.command(description="Owner: reset guild slash commands")
async def resetcmds2(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    await bot.http.bulk_upsert_guild_commands(bot.application_id, GUILD_INT, [])  # wipe live guild cmds
    synced = await bot.tree.sync(guild=GUILD_OBJ)                                 # republish from code
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
