# bot_slash.py — CYAN Gambling Bot (Clean Standard UI, PUBLIC)
# - GUI-only games inside /casino (Coinflip, Slots, Mines w/ Cashout, Tower, Roulette)
# - Mines difficulty picker (Easy/Normal/Hard) + Cash Out at any time
# - Tower game (5 rows): pick a safe tile each row, Cash Out anytime
# - Roulette (Red/Black/Green/Exact Number)
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
# 4) VIEWS (Tickets, Approvals, GUI, Games)
# =========================
def casino_embed(user: discord.User, balance: int, bet: int) -> discord.Embed:
    e = discord.Embed(
        title="CYAN Casino",
        description=(
            "Play via buttons below.\n"
            "Games: Coinflip · Slots · Mines · Tower · Roulette\n"
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
    """Staff Approve / Deny for a redeem request; on approve, opens a ticket channel."""
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

# ---- Mines (Difficulty Picker + Game + Cashout)
DIFFS: Dict[str, Tuple[int,int]] = {
    "easy":   (3, 2),  # (mines_count, full_clear_multiplier)
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
        self.safe_total = total - mines_count

        # 5x5 button grid
        for idx in range(total):
            btn = self._make_tile(idx)
            btn.row = idx // size
            self.add_item(btn)

        # Cash Out button
        self.add_item(self._cashout_button())

    def _payout_now(self) -> int:
        # Linear scaling so full clear == bet * multiplier
        # payout = bet * (1 + progress * (multiplier - 1))
        progress = len(self.revealed) / self.safe_total if self.safe_total else 0
        return int(self.bet * (1 + progress * (self.multiplier - 1)))

    def _make_tile(self, idx: int) -> discord.ui.Button:
        b = discord.ui.Button(
            emoji="⬛",
            style=discord.ButtonStyle.secondary,
            custom_id=f"mine_{idx}"
        )

        async def on_click(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("This game isn't yours. Use `/casino`.")
            if not self.alive:
                return await interaction.response.send_message("Game over. Open `/casino` to start again.")

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
                        if child.custom_id and child.custom_id.startswith("mine_"):
                            if int(child.custom_id.split("_")[1]) in self.mines:
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

            safe_left = self.safe_total - len(self.revealed)
            if safe_left == 0:
                # cleared → win full multiplier
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
            return await interaction.response.edit_message(
                content=f"🧨 **Mines** — Safes found: **{len(self.revealed)}/{self.safe_total}** · Potential cashout: **{self._payout_now()} CYAN**",
                view=self
            )

        b.callback = on_click
        return b

    def _cashout_button(self) -> discord.ui.Button:
        b = discord.ui.Button(label="Cash Out", style=discord.ButtonStyle.primary, emoji="💵", custom_id="mines_cashout")

        async def on_click(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("This panel belongs to someone else. Use `/casino`.")
            if not self.alive:
                return await interaction.response.send_message("Game already ended.")
            self.alive = False
            payout = self._payout_now()
            bal = await get_balance(self.user_id)
            await set_balance(self.user_id, bal + payout)
            await add_transaction(self.user_id, "mines_cashout", payout, f"revealed {len(self.revealed)}/{self.safe_total}, mult={self.multiplier}")
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            await interaction.response.edit_message(
                content=f"💵 **Cashed Out** for **+{payout} CYAN**. (Bet {self.bet}, progress {len(self.revealed)}/{self.safe_total})",
                view=self
            )

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
            content=f"🧨 **Mines** — Difficulty: **{key.title()}** · Mines: **{mines}** · Full Clear: **x{mult}**\n"
                    f"Click safe tiles. Cash Out anytime!",
            embed=None,
            view=view
        )

    @discord.ui.button(label="Easy", style=discord.ButtonStyle.success, emoji="🟢")
    async def easy(self, interaction: discord.Interaction, _):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("Use your own panel.")
        await self._start(interaction, "easy")

    @discord.ui.button(label="Normal", style=discord.ButtonStyle.primary, emoji="🔵")
    async def normal(self, interaction: discord.Interaction, _):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("Use your own panel.")
        await self._start(interaction, "normal")

    @discord.ui.button(label="Hard", style=discord.ButtonStyle.danger, emoji="🔴")
    async def hard(self, interaction: discord.Interaction, _):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("Use your own panel.")
        await self._start(interaction, "hard")

# ---- Tower (5 rows, 3 tiles each row, one bomb per row). Cash Out anytime.
class TowerView(discord.ui.View):
    def __init__(self, user_id: int, bet: int, rows: int = 5, choices_per_row: int = 3, timeout: Optional[float] = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.bet = clamp_bet(bet)
        self.rows = rows
        self.choices = choices_per_row
        self.current_row = 0
        self.alive = True
        # Pre-generate bomb positions per row
        self.bombs = [random.randint(0, self.choices - 1) for _ in range(self.rows)]
        # Progress-based multiplier (linear → full clear x4)
        self.full_mult = 4
        self._render_row()

        # Cash Out
        self.add_item(self._cashout_button())

    def _payout_now(self) -> int:
        progress = self.current_row / self.rows if self.rows else 0
        return int(self.bet * (1 + progress * (self.full_mult - 1)))

    def _render_row(self):
        # Clear old row buttons (keep cashout at end)
        to_remove = [ch for ch in self.children if isinstance(ch, discord.ui.Button) and ch.custom_id and ch.custom_id.startswith("tower_")]
        for ch in to_remove:
            self.remove_item(ch)
        if self.current_row >= self.rows:
            return
        for i in range(self.choices):
            b = discord.ui.Button(
                label=f"Row {self.current_row+1} • Pick {i+1}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"tower_{self.current_row}_{i}"
            )
            async def on_click(interaction: discord.Interaction, row=self.current_row, pick=i):
                if interaction.user.id != self.user_id:
                    return await interaction.response.send_message("This panel belongs to someone else. Use `/casino`.")
                if not self.alive:
                    return await interaction.response.send_message("Game over. Open `/casino` to start again.")
                bomb = self.bombs[row]
                # Disable row buttons
                for child in self.children:
                    if isinstance(child, discord.ui.Button) and child.custom_id and child.custom_id.startswith(f"tower_{row}_"):
                        child.disabled = True
                        if child.custom_id.endswith(f"_{bomb}"):
                            child.style = discord.ButtonStyle.danger
                            child.emoji = "💣"
                        elif child.custom_id.endswith(f"_{pick}") and pick != bomb:
                            child.style = discord.ButtonStyle.success
                            child.emoji = "✅"
                if pick == bomb:
                    # Lose
                    self.alive = False
                    bal = await get_balance(self.user_id)
                    loss = min(self.bet, bal)
                    await set_balance(self.user_id, bal - loss)
                    await add_transaction(self.user_id, "tower_loss", -loss, f"row {row+1}")
                    # Disable everything
                    for ch in self.children:
                        if isinstance(ch, discord.ui.Button):
                            ch.disabled = True
                    return await interaction.response.edit_message(content=f"💥 **Tower** — Hit a bomb at row {row+1}! **-{loss} CYAN**", view=self)
                else:
                    # Advance
                    self.current_row += 1
                    if self.current_row >= self.rows:
                        # Full clear win
                        self.alive = False
                        win = self.bet * self.full_mult
                        bal = await get_balance(self.user_id)
                        await set_balance(self.user_id, bal + win)
                        await add_transaction(self.user_id, "tower_win", win, f"rows={self.rows} mult={self.full_mult}")
                        for ch in self.children:
                            if isinstance(ch, discord.ui.Button):
                                ch.disabled = True
                        return await interaction.response.edit_message(content=f"🎉 **Tower** — Reached the top! **+{win} CYAN** (x{self.full_mult})", view=self)
                    else:
                        # Render next row
                        self._render_row()
                        return await interaction.response.edit_message(
                            content=f"🧱 **Tower** — Progress: **{self.current_row}/{self.rows}** · Potential cashout: **{self._payout_now()} CYAN**",
                            view=self
                        )
            b.callback = on_click
            # Insert before cashout button (which is last)
            self.children.insert(len(self.children), b)

    def _cashout_button(self) -> discord.ui.Button:
        b = discord.ui.Button(label="Cash Out", style=discord.ButtonStyle.primary, emoji="💵", custom_id="tower_cashout")
        async def on_click(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("This panel belongs to someone else. Use `/casino`.")
            if not self.alive:
                return await interaction.response.send_message("Game already ended.")
            self.alive = False
            payout = self._payout_now()
            bal = await get_balance(self.user_id)
            await set_balance(self.user_id, bal + payout)
            await add_transaction(self.user_id, "tower_cashout", payout, f"progress {self.current_row}/{self.rows}")
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            await interaction.response.edit_message(content=f"💵 **Tower Cashout** — **+{payout} CYAN** (Bet {self.bet}, progress {self.current_row}/{self.rows})", view=self)
        b.callback = on_click
        return b

# ---- Roulette (Red/Black/Green/Number)
ROULETTE_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
ROULETTE_BLACK = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}

class RouletteView(discord.ui.View):
    def __init__(self, user_id: int, bet: int, timeout: Optional[float] = 180):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.bet = clamp_bet(bet)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This panel belongs to someone else. Use `/casino`.")
            return False
        return True

    @discord.ui.button(label="Bet Red", style=discord.ButtonStyle.danger, emoji="🟥")
    async def bet_red(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._spin(interaction, kind="red")

    @discord.ui.button(label="Bet Black", style=discord.ButtonStyle.secondary, emoji="⬛")
    async def bet_black(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._spin(interaction, kind="black")

    @discord.ui.button(label="Bet Green (0)", style=discord.ButtonStyle.success, emoji="🟩")
    async def bet_green(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        await self._spin(interaction, kind="green")

    @discord.ui.button(label="Bet Exact Number", style=discord.ButtonStyle.primary, emoji="🎯")
    async def bet_number(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        modal = RouletteNumberModal(on_submit=self._spin_number)
        await interaction.response.send_modal(modal)

    async def _spin_number(self, interaction: discord.Interaction, number_text: str):
        try:
            num = int(number_text)
            if num < 0 or num > 36:
                raise ValueError
        except:
            return await interaction.response.send_message("Enter a valid number from 0 to 36.")
        await self._spin(interaction, kind="number", number=num)

    async def _spin(self, interaction: discord.Interaction, kind: str, number: Optional[int] = None):
        bal = await get_balance(self.user_id)
        bet_amt = clamp_bet(self.bet)
        if bet_amt > bal:
            return await interaction.response.send_message("Not enough CYAN for that bet.")
        # Spin wheel 0..36
        result = random.randint(0, 36)
        color = "green" if result == 0 else ("red" if result in ROULETTE_RED else "black")
        win_mult = 0
        label = ""

        if kind == "red":
            win_mult = 2 if color == "red" else 0
            label = "Red"
        elif kind == "black":
            win_mult = 2 if color == "black" else 0
            label = "Black"
        elif kind == "green":
            win_mult = 14 if result == 0 else 0
            label = "Green (0)"
        elif kind == "number":
            label = f"Number {number}"
            win_mult = 36 if result == number else 0

        if win_mult:
            win = bet_amt * win_mult
            new_bal = bal + win
            await add_transaction(self.user_id, "roulette_win", win, f"{label} vs {result} ({color})")
            await set_balance(self.user_id, new_bal)
            return await interaction.response.send_message(
                f"🎡 **Roulette** — Bet **{label}**. Result: **{result} {color}** → **+{win} CYAN**\nBalance: **{new_bal}**"
            )
        else:
            new_bal = bal - bet_amt
            await add_transaction(self.user_id, "roulette_loss", -bet_amt, f"{label} vs {result} ({color})")
            await set_balance(self.user_id, new_bal)
            return await interaction.response.send_message(
                f"🎡 **Roulette** — Bet **{label}**. Result: **{result} {color}** → **-{bet_amt} CYAN**\nBalance: **{new_bal}**"
            )

class RouletteNumberModal(discord.ui.Modal, title="Exact Number Bet"):
    number = discord.ui.TextInput(label="Number (0-36)", placeholder="e.g. 17", required=True, max_length=2)
    def __init__(self, on_submit):
        super().__init__()
        self._on_submit = on_submit
    async def on_submit(self, interaction: discord.Interaction):
        await self._on_submit(interaction, str(self.number.value))

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
            return await interaction.response.send_message("Reward not found.")
        cost, robux = row
        bal = await get_balance(interaction.user.id)
        if cost > bal:
            return await interaction.response.send_message("Not enough CYAN for that reward.")

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

        await interaction.response.send_message(f"✅ Redeem request `#{request_id}` submitted. Staff will review.")

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
            await interaction.response.send_message("Enter a valid number.")

class CasinoMenuView(discord.ui.View):
    def __init__(self, user_id: int, bet: Optional[int] = None, timeout: Optional[float] = 600):
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
            view=view
        )

    @discord.ui.button(label="Tower", style=discord.ButtonStyle.secondary, emoji="🗼")
    async def tower(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        view = TowerView(user_id=self.user_id, bet=self.bet)
        await interaction.response.send_message(
            f"🗼 **Tower** — Reach the top (5 rows). Cash Out anytime. Potential full clear: x{view.full_mult}.",
            view=view
        )

    @discord.ui.button(label="Roulette", style=discord.ButtonStyle.secondary, emoji="🎡")
    async def roulette(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        view = RouletteView(user_id=self.user_id, bet=self.bet)
        await interaction.response.send_message(
            "🎡 **Roulette** — Red/Black (x2), Green 0 (x14), Exact Number (x36). Spin below:",
            view=view
        )

    @discord.ui.button(label="Rewards", style=discord.ButtonStyle.secondary, emoji="🎁")
    async def rewards(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        rows = list_rewards()
        if not rows:
            return await interaction.response.send_message("No rewards configured yet. Ask staff to add rewards.")
        await interaction.response.send_message("Select a reward to redeem:", view=RewardsView(rows))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard(interaction): return
        bal = await get_balance(self.user_id)
        await interaction.response.edit_message(embed=casino_embed(interaction.user, bal, self.bet), view=self)

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
# 6) SLASH COMMANDS (PUBLIC OUTPUTS)
# =========================
@bot.tree.command(description="Show your CYAN balance")
async def balance(interaction: discord.Interaction):
    bal = await get_balance(interaction.user.id)
    await interaction.response.send_message(f"{interaction.user.mention} balance: **{bal} CYAN**")

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
                    return await interaction.response.send_message(f"{interaction.user.mention} already claimed in the last 24h.")
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
        name = member.display_name if member else str(uid)
        lines.append(f"{i}. {name} — {bal} CYAN")
    await interaction.response.send_message("**Top balances**\n" + "\n".join(lines))

# Gift (player → player)
@bot.tree.command(description="Gift CYAN to another user")
@app_commands.describe(user="Recipient", amount="Amount of CYAN to send (≥ 1)")
async def gift(interaction: discord.Interaction, user: discord.Member, amount: int):
    if user.id == interaction.user.id:
        return await interaction.response.send_message("You can't gift yourself.")
    if amount <= 0:
        return await interaction.response.send_message("Amount must be at least 1.")
    sender_bal = await get_balance(interaction.user.id)
    if amount > sender_bal:
        return await interaction.response.send_message("Not enough CYAN.")
    await set_balance(interaction.user.id, sender_bal - amount)
    recv_bal = await get_balance(user.id)
    await set_balance(user.id, recv_bal + amount)
    await add_transaction(interaction.user.id, "gift_send", -amount, f"to {user.id}")
    await add_transaction(user.id, "gift_recv", amount, f"from {interaction.user.id}")
    await interaction.response.send_message(f"🎁 {interaction.user.mention} sent **{amount} CYAN** to **{user.display_name}**.")

# Rewards (Admin)
@bot.tree.command(description="Admin: add a new reward (global)")
@app_commands.describe(cost_cyan="CYAN cost", robux="Robux delivered")
@app_commands.checks.has_permissions(manage_guild=True)
async def addreward(interaction: discord.Interaction, cost_cyan: int, robux: int):
    if cost_cyan <= 0 or robux <= 0:
        return await interaction.response.send_message("Values must be positive.")
    rid = add_reward(cost_cyan, robux)
    await interaction.response.send_message(f"✅ Added reward ID `{rid}` — **{cost_cyan} CYAN → {robux} Robux** (global)")

@bot.tree.command(description="Admin: remove a reward (global)")
@app_commands.describe(reward_id="ID to remove")
@app_commands.checks.has_permissions(manage_guild=True)
async def removereward(interaction: discord.Interaction, reward_id: int):
    ok = remove_reward(reward_id)
    if ok:
        await interaction.response.send_message(f"🗑️ Removed reward `{reward_id}`.")
    else:
        await interaction.response.send_message("Reward not found.")

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

# Owner-only
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

@bot.tree.command(description="Owner: download the database file")
async def backupdb(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.")
    try:
        await interaction.response.send_message(
            content="Here’s the current DB file.",
            file=discord.File(DB, filename=os.path.basename(DB))
        )
    except Exception as e:
        await interaction.response.send_message(f"Backup failed: {e!r}")

# Casino opener
@bot.tree.command(description="Open the CYAN casino panel")
async def casino(interaction: discord.Interaction):
    bal = await get_balance(interaction.user.id)
    view = CasinoMenuView(user_id=interaction.user.id, bet=MIN_BET)
    e = casino_embed(interaction.user, bal, view.bet)
    await interaction.response.send_message(embed=e, view=view)  # PUBLIC

# Admin sync helper
@bot.tree.command(description="Force-sync slash commands (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    try:
        synced = await bot.tree.sync(guild=GUILD_OBJ)
        await interaction.response.send_message(f"Synced to guild {GUILD_ID} (count={len(synced)}).")
    except Exception as e:
        await interaction.response.send_message(f"Sync error: {e!r}")

# Owner reset (fallback if ever needed)
@bot.tree.command(description="Owner: reset guild slash commands")
async def resetcmds2(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.")
    await bot.http.bulk_upsert_guild_commands(bot.application_id, GUILD_INT, [])  # wipe live guild cmds
    synced = await bot.tree.sync(guild=GUILD_OBJ)                                 # republish from code
    await interaction.response.send_message(f"Republished {len(synced)} commands.")

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
