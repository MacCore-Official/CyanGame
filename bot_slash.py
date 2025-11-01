# ✅ IMPORTS FIRSTs
import os
import sqlite3
import asyncio
import random
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ✅ CONFIG + DB + HELPERS HERE

# ✅ THEN paste the GUI code (BetModal, CasinoMenuView, etc)

# ===================== CASINO GUI =====================

class BetModal(discord.ui.Modal, title="Set Bet"):
    bet = discord.ui.TextInput(label="Bet amount (CYAN)", placeholder="e.g. 100", required=True, min_length=1, max_length=10)

    def __init__(self, on_set):
        super().__init__()
        self.on_set = on_set  # callback (interaction, bet_int)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet_int = clamp_bet(int(str(self.bet)))
            await self.on_set(interaction, bet_int)
        except:
            await interaction.response.send_message("Enter a valid number.", ephemeral=True)


class CasinoMenuView(discord.ui.View):
    def __init__(self, user_id: int, bet: int | None = None, timeout: float | None = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.bet = bet or MIN_BET

    # ----- utility -----
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
                f"**Bet:** `{self.bet} CYAN`  *(click **Set Bet** to change)*\n\n"
                "Play with the buttons below."
            ),
            color=0x18a558
        )
        e.set_footer(text="Use /casino again if this times out.")
        await interaction.message.edit(embed=e, view=self)

    # ----- buttons -----
    @discord.ui.button(label="Set Bet", style=discord.ButtonStyle.secondary, emoji="🧾")
    async def set_bet(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction): return

        async def apply_bet(ix: discord.Interaction, bet_val: int):
            self.bet = bet_val
            await ix.response.send_message(f"Bet set to **{self.bet} CYAN**.", ephemeral=True)
            await self._refresh_embed(ix)

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
                # reuse your /redeem logic (inline here)
                try:
                    amt = int(str(self.amount))
                except:
                    return await ix.response.send_message("Enter a valid number.", ephemeral=True)
                bal = await get_balance(ix.user.id)
                if amt <= 0 or amt > bal:
                    return await ix.response.send_message("Invalid amount or insufficient funds.", ephemeral=True)
                ts = now_ts()
                with sqlite3.connect(DB) as conn:
                    c = conn.cursor()
                    c.execute("INSERT INTO redeems(user_id, amount, status, ts, reason) VALUES(?,?,?,?,?)",
                              (ix.user.id, amt, "pending", ts, str(self.reason)))
                    rid = c.lastrowid
                    conn.commit()
                await add_transaction(ix.user.id, "redeem_request", -amt, f"request id {rid} reason:{str(self.reason)}")
                staff_channel_id = setting_get("staff_channel_id")
                if staff_channel_id:
                    ch = ix.guild.get_channel(int(staff_channel_id))
                    if ch:
                        embed = discord.Embed(
                            title="Redeem Request",
                            description=f"User: {ix.user} ({ix.user.id})\nAmount: {amt} CYAN\nID: {rid}\nReason: {str(self.reason)}",
                            color=0x18a558
                        )
                        view = RedeemReviewView(request_id=rid, user_id=ix.user.id, amount=amt)
                        await ch.send(embed=embed, view=view)
                await ix.response.send_message(f"Redeem request `#{rid}` submitted.", ephemeral=True)

        await interaction.response.send_modal(RedeemModal())

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self._refresh_embed(interaction)

    # ----- game logic (uses your helpers) -----
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
        await self._refresh_embed(interaction)

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
        await self._refresh_embed(interaction)


# Slash command to open the GUI
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
