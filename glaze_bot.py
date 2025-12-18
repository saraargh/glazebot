# =========================
# GLAZE BOT — PART 1 OF 2
# =========================

from __future__ import annotations

import os
import json
import uuid
import base64
import threading
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import discord
from discord import app_commands
from discord.ext import tasks
from flask import Flask
from zoneinfo import ZoneInfo


# =========================================================
# Flask keep-alive (Render)
# =========================================================
app = Flask("glaze")

@app.get("/")
def home():
    return "🍯 Glaze is alive"

def _run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

threading.Thread(target=_run_flask, daemon=True).start()


# =========================================================
# Config
# =========================================================
TOKEN = os.getenv("TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_FILE = os.getenv("GLAZE_GITHUB_FILE", "glaze_data.json")

API_BASE = "https://api.github.com"
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

LONDON = ZoneInfo("Europe/London")

DAILY_DROP_HOUR = 17
DAILY_DROP_MINUTE = 0

MONTHLY_DROP_HOUR = 18
MONTHLY_DROP_MINUTE = 0

DAILY_PING_PREFIX = "🍯 A glaze has landed…"
MONTHLY_PING_PREFIX = "🍯 MONTHLY GLAZE RESULTS..."

FOOTER_TEXT = "Use /glaze to submit an anonymous glaze — remember to keep it SFW! ⚠️"
SELF_GLAZE_ROAST = "🚫🚫 {user} only ugly people glaze themselves — try being nice to someone else!"
NOT_YOUR_MENU = "🍯 Hands off — this glaze menu isn’t yours!"

MONTHLY_GIF_URL = "https://cdn.discordapp.com/attachments/1450977394948051015/IMG_5594.gif"

LOCK_GUILD_ID = int(os.getenv("GUILD_ID", "0"))


# =========================================================
# Discord client
# =========================================================
intents = discord.Intents.default()
intents.members = True

class GlazeBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        glaze_scheduler.start()

    async def on_ready(self):
        print(f"🍯 Glaze online as {self.user}")

bot = GlazeBot()


# =========================================================
# GitHub JSON Store
# =========================================================
DEFAULT_DATA = {
    "config": {
        "drop_channel_id": None,
        "report_channel_id": None,
        "admin_role_ids": []
    },
    "meta": {
        "last_daily_drop_date": None,
        "last_monthly_announce": {}
    },
    "cooldowns": {},
    "glazes": [],
    "wins": {}
}

_store_lock = asyncio.Lock()
_cached_data: Optional[Dict[str, Any]] = None
_cached_sha: Optional[str] = None


def _github_enabled() -> bool:
    return bool(GITHUB_REPO and GITHUB_TOKEN)


def _deepcopy(data):
    return json.loads(json.dumps(data))


def _merge_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    merged = _deepcopy(DEFAULT_DATA)
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


async def load_data() -> Tuple[Dict[str, Any], Optional[str]]:
    global _cached_data, _cached_sha

    async with _store_lock:
        if _cached_data is not None:
            return _cached_data, _cached_sha

        if not _github_enabled():
            _cached_data = _deepcopy(DEFAULT_DATA)
            return _cached_data, None

        url = f"{API_BASE}/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        r = await asyncio.to_thread(requests.get, url, headers=HEADERS)

        if r.status_code == 200:
            payload = r.json()
            raw = base64.b64decode(payload["content"]).decode()
            _cached_sha = payload["sha"]
            _cached_data = _merge_defaults(json.loads(raw))
            return _cached_data, _cached_sha

        if r.status_code == 404:
            _cached_data = _deepcopy(DEFAULT_DATA)
            await save_data(_cached_data, None, "Create glaze_data.json")
            return _cached_data, None

        raise RuntimeError(r.text)


async def save_data(data: Dict[str, Any], sha: Optional[str], message: str):
    global _cached_data, _cached_sha

    async with _store_lock:
        if not _github_enabled():
            _cached_data = _deepcopy(data)
            _cached_sha = sha
            return

        payload = {
            "message": message,
            "content": base64.b64encode(json.dumps(data, indent=2).encode()).decode()
        }
        if sha:
            payload["sha"] = sha

        url = f"{API_BASE}/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        r = await asyncio.to_thread(requests.put, url, headers=HEADERS, json=payload)

        if r.status_code not in (200, 201):
            raise RuntimeError(r.text)

        res = r.json()
        _cached_sha = res["content"]["sha"]
        _cached_data = _deepcopy(data)


# =========================================================
# Helpers
# =========================================================
def now_utc():
    return datetime.now(timezone.utc)

def iso_utc(dt):
    return dt.astimezone(timezone.utc).isoformat()

def parse_iso(s):
    return datetime.fromisoformat(s)

def month_key(dt):
    return dt.strftime("%Y-%m")


# =========================================================
# UI + MODALS CONTINUE IN PART 2
# =========================================================
# =========================
# GLAZE BOT — PART 2 OF 2
# (Paste directly under PART 1)
# =========================


# =========================================================
# Embeds
# =========================================================
def build_daily_embed(recipient_display: str, text: str) -> discord.Embed:
    e = discord.Embed(
        title="🍯 GLAZEEEEE DROP",
        description=f"Today’s glaze is for **{recipient_display}**\n\n*“{text}”*"
    )
    e.set_footer(text=FOOTER_TEXT)
    return e

def build_monthly_embed(month_key_str: str, winner_mention: str, count: int) -> discord.Embed:
    # month_key_str like "2025-12"
    dt = datetime.strptime(month_key_str + "-01", "%Y-%m-%d")
    pretty = dt.strftime("%B %Y")
    e = discord.Embed(
        title="🍯 MOST GLAZED",
        description=(
            f"The Landing Strip’s most glazed member for **{pretty}** "
            f"is {winner_mention} with a total of **{count} glazes** — yayyyy 🎉🎉"
        )
    )
    if MONTHLY_GIF_URL:
        e.set_image(url=MONTHLY_GIF_URL)
    e.set_footer(text=FOOTER_TEXT)
    return e

def build_my_glaze_embed(index: int, total: int, glaze_text: str, received_date_str: str) -> discord.Embed:
    e = discord.Embed(
        title=f"🍯 Your Glaze ({index + 1} / {total})",
        description=f"*“{glaze_text}”*\n\n📅 Received: {received_date_str}"
    )
    e.set_footer(text=FOOTER_TEXT)
    return e

def build_shared_embed(glaze_text: str, note: str) -> discord.Embed:
    e = discord.Embed(
        title="🍯 SHARED GLAZE",
        description=f"*“{glaze_text}”*"
    )
    if note.strip():
        e.add_field(name="💬", value=f"“{note.strip()}”", inline=False)
    e.set_footer(text="Shared via /myglaze 🍯")
    return e


# =========================================================
# Permissions + Guild helpers
# =========================================================
async def get_single_guild() -> Optional[discord.Guild]:
    if not bot.guilds:
        return None
    if LOCK_GUILD_ID:
        return discord.utils.get(bot.guilds, id=LOCK_GUILD_ID)
    return bot.guilds[0]

def is_admin(interaction: discord.Interaction, admin_role_ids: List[int]) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    member: discord.Member = interaction.user
    if member.guild_permissions.administrator:
        return True
    roles = {r.id for r in member.roles}
    return any(rid in roles for rid in admin_role_ids)

async def get_drop_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    data, _ = await load_data()
    cid = data["config"].get("drop_channel_id")
    if not cid:
        return None
    ch = guild.get_channel(int(cid))
    return ch if isinstance(ch, discord.TextChannel) else None

async def get_report_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    data, _ = await load_data()
    cid = data["config"].get("report_channel_id")
    if not cid:
        return None
    ch = guild.get_channel(int(cid))
    return ch if isinstance(ch, discord.TextChannel) else None


# =========================================================
# UI: /myglaze hub view
# =========================================================
class MyGlazeHubView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id

    @discord.ui.button(label="🍯 My Glazes", style=discord.ButtonStyle.secondary)
    async def my_glazes(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        await open_my_glazes(interaction)

    @discord.ui.button(label="💌 DM Me", style=discord.ButtonStyle.secondary)
    async def dm_me(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        await send_glaze_mail(interaction)


# =========================================================
# UI: Say Thanks modal
# =========================================================
class ThanksModal(discord.ui.Modal, title="Say Thanks! 💐"):
    message = discord.ui.TextInput(
        label="Write a thank-you message (optional)",
        required=False,
        max_length=500,
        style=discord.TextStyle.paragraph
    )

    def __init__(self, sender_id: int, glaze_text: str):
        super().__init__()
        self.sender_id = sender_id
        self.glaze_text = glaze_text

    async def on_submit(self, interaction: discord.Interaction):
        thank_text = (self.message.value or "").strip()

        try:
            u = await bot.fetch_user(self.sender_id)

            dm = (
                "💐 Someone wants to thank you for your glaze!\n\n"
                f"🍯 **Your glaze:**\n“{self.glaze_text}”\n\n"
            )
            if thank_text:
                dm += f"💬 **Their message:**\n“{thank_text}”"

            await u.send(dm)
        except Exception:
            pass

        await interaction.response.send_message("💐 Thanks sent!", ephemeral=True)


# =========================================================
# UI: Share modal -> confirmation
# =========================================================
class ShareModal(discord.ui.Modal, title="Share this glaze 🍯"):
    note = discord.ui.TextInput(
        label="Add a message (optional)",
        required=False,
        max_length=200,
        placeholder="e.g. This made my day 💛"
    )

    def __init__(self, owner_id: int, glaze_id: str):
        super().__init__()
        self.owner_id = owner_id
        self.glaze_id = glaze_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        view = ShareConfirmView(owner_id=self.owner_id, glaze_id=self.glaze_id, note=(self.note.value or ""))
        await interaction.response.send_message(
            "📣 Share this glaze in the server?\nOnce shared, it can’t be undone.",
            view=view,
            ephemeral=True
        )

class ShareConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, glaze_id: str, note: str):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.glaze_id = glaze_id
        self.note = note

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        await interaction.response.edit_message(content="❌ Share cancelled.", view=None)

    @discord.ui.button(label="📣 Share", style=discord.ButtonStyle.primary)
    async def share(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return

        ok, msg = await share_glaze(interaction, self.glaze_id, self.note)
        if not ok:
            await interaction.response.edit_message(content=msg, view=None)
            return

        await interaction.response.edit_message(content="📣 Glaze shared in the server 🍯", view=None)


# =========================================================
# UI: Report view (mod message) with Delete & Scold
# =========================================================
class DeleteScoldView(discord.ui.View):
    def __init__(self, glaze_id: str):
        super().__init__(timeout=None)
        self.glaze_id = glaze_id

    @discord.ui.button(label="💥 Delete Glaze and Scold Glazer", style=discord.ButtonStyle.danger)
    async def delete_scold(self, interaction: discord.Interaction, button: discord.ui.Button):
        data, sha = await load_data()
        admin_roles = data["config"].get("admin_role_ids", [])
        if not is_admin(interaction, admin_roles):
            await interaction.response.send_message("🚫 You don’t have permission to do that.", ephemeral=True)
            return

        glaze = next((g for g in data["glazes"] if g["id"] == self.glaze_id), None)
        if not glaze or glaze.get("deleted"):
            await interaction.response.send_message("😔 That glaze is already deleted or missing.", ephemeral=True)
            return

        glaze["deleted"] = True
        await save_data(data, sha, message="Delete glaze (mod action)")

        # scold DM includes the reported glaze text
        try:
            u = await bot.fetch_user(int(glaze["sender_id"]))
            await u.send(
                "⚠️ **Your glaze was reported and removed**\n\n"
                "🍯 **Reported glaze:**\n"
                f"“{glaze['text']}”\n\n"
                "Please remember to keep glazes kind and SFW."
            )
        except Exception:
            pass

        button.disabled = True
        await interaction.response.edit_message(content="✅ Deleted and scolded.", view=self)


# =========================================================
# UI: My glazes paginated view
# =========================================================
class MyGlazesView(discord.ui.View):
    def __init__(self, owner_id: int, glaze_ids: List[str]):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.glaze_ids = glaze_ids
        self.index = 0

        self.prev_btn.disabled = True
        self.next_btn.disabled = len(glaze_ids) <= 1

    async def _get_current_glaze(self) -> Optional[Dict[str, Any]]:
        data, _ = await load_data()
        g = next((x for x in data["glazes"] if x["id"] == self.glaze_ids[self.index]), None)
        if not g or g.get("deleted"):
            return None
        return g

    async def _render(self, interaction: discord.Interaction):
        g = await self._get_current_glaze()
        if not g:
            await interaction.response.edit_message(content="😔 That glaze is no longer available.", embed=None, view=None)
            return

        created = parse_iso(g["created_at"]).astimezone(LONDON)
        received_str = created.strftime("%d %b %Y")

        embed = build_my_glaze_embed(self.index, len(self.glaze_ids), g["text"], received_str)

        self.prev_btn.disabled = (self.index == 0)
        self.next_btn.disabled = (self.index >= len(self.glaze_ids) - 1)

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        self.index = max(0, self.index - 1)
        await self._render(interaction)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        self.index = min(len(self.glaze_ids) - 1, self.index + 1)
        await self._render(interaction)

    @discord.ui.button(label="Say Thanks! 💐", style=discord.ButtonStyle.secondary)
    async def thanks_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        g = await self._get_current_glaze()
        if not g:
            await interaction.response.send_message("😔 That glaze is no longer available.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ThanksModal(sender_id=int(g["sender_id"]), glaze_text=g["text"])
        )

    @discord.ui.button(label="Report ⚠️", style=discord.ButtonStyle.secondary)
    async def report_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        g = await self._get_current_glaze()
        if not g:
            await interaction.response.send_message("😔 That glaze is no longer available.", ephemeral=True)
            return
        await report_glaze(interaction, g["id"])

    @discord.ui.button(label="📣 Share", style=discord.ButtonStyle.secondary)
    async def share_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        g = await self._get_current_glaze()
        if not g:
            await interaction.response.send_message("⚠️ This glaze can’t be shared.", ephemeral=True)
            return
        await interaction.response.send_modal(ShareModal(owner_id=self.owner_id, glaze_id=g["id"]))


# =========================================================
# Core actions (open glazes, DM mail, share, report)
# =========================================================
async def open_my_glazes(interaction: discord.Interaction):
    data, _ = await load_data()
    glz = [g for g in data["glazes"] if int(g["recipient_id"]) == interaction.user.id and not g.get("deleted")]
    if not glz:
        await interaction.response.send_message("😔 You don’t have any glazes yet…", ephemeral=True)
        return

    glz.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    ids = [g["id"] for g in glz]

    first = glz[0]
    created = parse_iso(first["created_at"]).astimezone(LONDON).strftime("%d %b %Y")
    embed = build_my_glaze_embed(0, len(ids), first["text"], created)

    view = MyGlazesView(owner_id=interaction.user.id, glaze_ids=ids)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def send_glaze_mail(interaction: discord.Interaction):
    data, _ = await load_data()
    glz = [g for g in data["glazes"] if int(g["recipient_id"]) == interaction.user.id and not g.get("deleted")]
    if not glz:
        await interaction.response.send_message("😔 You don’t have any glazes yet…", ephemeral=True)
        return

    glz.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    lines = ["💌 **Your Glaze Mail**\n"]
    for g in glz[:50]:
        dt = parse_iso(g["created_at"]).astimezone(LONDON).strftime("%d %b %Y")
        lines.append(f"• {dt} — “{g['text']}”")

    try:
        await interaction.user.send("\n".join(lines))
        await interaction.response.send_message("💌 Glaze Mail complete — check your DMs!", ephemeral=True)
    except Exception:
        await interaction.response.send_message("⚠️ I couldn’t DM you — please enable DMs to receive Glaze Mail.", ephemeral=True)

async def share_glaze(interaction: discord.Interaction, glaze_id: str, note: str) -> Tuple[bool, str]:
    guild = await get_single_guild()
    if not guild:
        return False, "⚠️ Server not ready."

    data, _ = await load_data()
    glaze = next((g for g in data["glazes"] if g["id"] == glaze_id), None)
    if not glaze or glaze.get("deleted"):
        return False, "⚠️ This glaze can’t be shared."

    if interaction.user.id != int(glaze["recipient_id"]):
        return False, NOT_YOUR_MENU

    ch = await get_drop_channel(guild)
    if not ch:
        return False, "⚠️ Drop channel isn’t set. Ask an admin to run /controlpanel."

    embed = build_shared_embed(glaze["text"], note)
    await ch.send(embed=embed)
    return True, "ok"

async def report_glaze(interaction: discord.Interaction, glaze_id: str):
    guild = await get_single_guild()
    if not guild:
        await interaction.response.send_message("⚠️ Server not ready.", ephemeral=True)
        return

    report_ch = await get_report_channel(guild)
    if not report_ch:
        await interaction.response.send_message("⚠️ Report channel isn’t set. Ask an admin to run /controlpanel.", ephemeral=True)
        return

    data, sha = await load_data()
    glaze = next((g for g in data["glazes"] if g["id"] == glaze_id), None)
    if not glaze or glaze.get("deleted"):
        await interaction.response.send_message("😔 That glaze is no longer available.", ephemeral=True)
        return

    glaze["reported"] = True
    await save_data(data, sha, message="Report glaze")

    reporter = interaction.user.mention
    recipient_mention = f"<@{int(glaze['recipient_id'])}>"

    content = (
        "⚠️ **GLAZE REPORTED**\n\n"
        f"Reported by: {reporter}\n"
        f"Glaze was for: {recipient_mention}\n"
        f"Glaze ID: `{glaze_id}`\n\n"
        f"Content:\n“{glaze['text']}”"
    )

    await report_ch.send(content, view=DeleteScoldView(glaze_id=glaze_id))
    await interaction.response.send_message("⚠️ Report sent to the mods. Thank you.", ephemeral=True)


# =========================================================
# Commands
# =========================================================
@bot.tree.command(name="controlpanel", description="Configure Glaze settings")
@app_commands.describe(
    drop_channel="Channel for daily & monthly glazes",
    report_channel="Channel for reported glazes",
    admin_role="Add or remove an admin role (run multiple times)"
)
async def controlpanel(
    interaction: discord.Interaction,
    drop_channel: discord.TextChannel | None = None,
    report_channel: discord.TextChannel | None = None,
    admin_role: discord.Role | None = None
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("🚫 Admins only.")
        return

    data, sha = await load_data()
    changes = []

    if drop_channel is not None:
        data["config"]["drop_channel_id"] = drop_channel.id
        changes.append(f"• Drop channel → {drop_channel.mention}")

    if report_channel is not None:
        data["config"]["report_channel_id"] = report_channel.id
        changes.append(f"• Report channel → {report_channel.mention}")

    if admin_role is not None:
        role_ids = set(data["config"].get("admin_role_ids", []))
        if admin_role.id in role_ids:
            role_ids.remove(admin_role.id)
            changes.append(f"• Admin role removed → {admin_role.mention}")
        else:
            role_ids.add(admin_role.id)
            changes.append(f"• Admin role added → {admin_role.mention}")
        data["config"]["admin_role_ids"] = list(role_ids)

    if not changes:
        await interaction.response.send_message("🍯 Nothing changed — provide at least one option to update.")
        return

    await save_data(data, sha, message="Update Glaze controlpanel")
    current_roles = ", ".join(f"<@&{r}>" for r in data["config"]["admin_role_ids"]) or "None"
    await interaction.response.send_message(
        "🍯 **Glaze configuration updated**\n"
        + "\n".join(changes)
        + f"\n• Current admin roles: {current_roles}"
    )

@bot.tree.command(name="glaze", description="Send an anonymous glaze to someone (once every 24h).")
@app_commands.describe(member="Who are you glazing?", message="Write something nice (keep it SFW!)")
async def glaze_cmd(interaction: discord.Interaction, member: discord.Member, message: str):
    guild = await get_single_guild()
    if not guild:
        await interaction.response.send_message("⚠️ Server not ready.", ephemeral=True)
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message(SELF_GLAZE_ROAST.format(user=interaction.user.mention), ephemeral=False)
        return

    text = message.strip()
    if len(text) < 10:
        await interaction.response.send_message("🍯 Make it a bit longer — at least 10 characters.", ephemeral=True)
        return
    if len(text) > 500:
        await interaction.response.send_message("🍯 Keep it under 500 characters please.", ephemeral=True)
        return

    # IMPORTANT: always read latest JSON, not stale cache
    # (invalidate cache first so manual JSON edits take effect immediately)
    global _cached_data, _cached_sha
    _cached_data = None
    _cached_sha = None

    data, sha = await load_data()

    last = data["cooldowns"].get(str(interaction.user.id))
    if last:
        diff = now_utc() - parse_iso(last)
        if diff < timedelta(hours=24):
            await interaction.response.send_message("⏳ You can only glaze once every 24 hours.", ephemeral=True)
            return

    g_id = str(uuid.uuid4())
    created = now_utc()
    glaze = {
        "id": g_id,
        "sender_id": interaction.user.id,
        "recipient_id": member.id,
        "text": text,
        "created_at": iso_utc(created),
        "month_key": month_key(created),
        "dropped_at": None,
        "deleted": False,
        "reported": False
    }

    data["glazes"].append(glaze)
    data["cooldowns"][str(interaction.user.id)] = iso_utc(created)

    await save_data(data, sha, message="Add glaze")

    await interaction.response.send_message("✅ Your glaze has been submitted! 🍯", ephemeral=True)

@bot.tree.command(name="myglaze", description="See your glazes (buttons + DM option).")
async def myglaze_cmd(interaction: discord.Interaction):
    data, _ = await load_data()
    glz = [g for g in data["glazes"] if int(g["recipient_id"]) == interaction.user.id and not g.get("deleted")]
    if not glz:
        await interaction.response.send_message("😔 You don’t have any glazes yet… but your time will come 🍯", ephemeral=True)
        return
    await interaction.response.send_message("🍯 Your glaze menu:", view=MyGlazeHubView(owner_id=interaction.user.id), ephemeral=True)

@bot.tree.command(name="glazeleaderboard", description="Monthly winners + top glazers")
async def glazeleaderboard_cmd(interaction: discord.Interaction):
    data, _ = await load_data()

    wins = data.get("wins", {})
    if wins:
        sorted_wins = sorted(((int(uid), cnt) for uid, cnt in wins.items()), key=lambda x: x[1], reverse=True)[:5]
        monthly_lines = [f"**{i}.** <@{uid}> — **{cnt}** win(s)" for i, (uid, cnt) in enumerate(sorted_wins, start=1)]
    else:
        monthly_lines = ["No monthly winners yet 🍯"]

    sender_counts: Dict[int, int] = {}
    for g in data.get("glazes", []):
        if not g.get("deleted"):
            sid = int(g["sender_id"])
            sender_counts[sid] = sender_counts.get(sid, 0) + 1

    if sender_counts:
        sorted_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        sender_lines = [f"**{i}.** <@{uid}> — **{cnt}** glazes sent" for i, (uid, cnt) in enumerate(sorted_senders, start=1)]
    else:
        sender_lines = ["No glazes sent yet 🍯"]

    embed = discord.Embed(title="🍯 Glaze Leaderboard")
    embed.add_field(name="🏆 Most Glazed (Monthly Wins)", value="\n".join(monthly_lines), inline=False)
    embed.add_field(name="🍯 Top Glazers (Most Sent)", value="\n".join(sender_lines), inline=False)
    embed.set_footer(text=FOOTER_TEXT)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="testdrop", description="Force a Glaze drop for testing (admin only)")
@app_commands.describe(kind="Which drop to test")
@app_commands.choices(
    kind=[
        app_commands.Choice(name="Daily Glaze", value="daily"),
        app_commands.Choice(name="Monthly Glaze", value="monthly")
    ]
)
async def testdrop(interaction: discord.Interaction, kind: app_commands.Choice[str]):
    data, sha = await load_data()

    admin_roles = data["config"].get("admin_role_ids", [])
    if not is_admin(interaction, admin_roles):
        await interaction.response.send_message("🚫 You don’t have permission to do that.")
        return

    guild = await get_single_guild()
    if not guild:
        await interaction.response.send_message("⚠️ Server not ready.")
        return

    drop_channel = interaction.channel
    if not isinstance(drop_channel, discord.TextChannel):
        await interaction.response.send_message("⚠️ Run this in a text channel.")
        return

    if kind.value == "daily":
        pending = [g for g in data["glazes"] if not g.get("deleted") and not g.get("dropped_at")]
        pending.sort(key=lambda x: x.get("created_at", ""))

        if not pending:
            await interaction.response.send_message("🍯 No pending glazes to drop.")
            return

        g = pending[0]
        member = guild.get_member(int(g["recipient_id"]))

        if member:
            await drop_channel.send(f"🍯 **(Test Drop)** A glaze has landed…\n{member.mention}")
            await drop_channel.send(embed=build_daily_embed(member.display_name, g["text"]))
        else:
            await drop_channel.send("🍯 **(Test Drop)** A glaze landed, but the member is no longer here.")

        g["dropped_at"] = iso_utc(now_utc())
        data["meta"]["last_daily_drop_date"] = datetime.now(tz=LONDON).strftime("%Y-%m-%d")
        await save_data(data, sha, message="Test daily glaze drop")

        await interaction.response.send_message("🍯 Daily test drop complete.")

    else:
        mk = datetime.now(timezone.utc).strftime("%Y-%m")
        winner = compute_month_winner(data, mk)
        if not winner:
            await interaction.response.send_message("🍯 No glazes available for this month.")
            return

        winner_id, count = winner
        await drop_channel.send("🍯🍯🍯 **(Test Drop)** MONTHLY GLAZE RESULTS 🍯🍯🍯\n@everyone")
        await drop_channel.send(embed=build_monthly_embed(mk, f"<@{winner_id}>", count))

        data["meta"]["last_monthly_announce"][mk] = iso_utc(now_utc())
        data["wins"][str(winner_id)] = int(data["wins"].get(str(winner_id), 0)) + 1
        await save_data(data, sha, message="Test monthly glaze drop")

        await interaction.response.send_message("🍯 Monthly test drop complete.")


# =========================================================
# Monthly winner calculation
# =========================================================
def compute_month_winner(data: Dict[str, Any], month_key_str: str) -> Optional[Tuple[int, int]]:
    month_glazes = [g for g in data["glazes"] if g.get("month_key") == month_key_str and not g.get("deleted")]
    if not month_glazes:
        return None

    month_glazes.sort(key=lambda x: x.get("created_at", ""))

    counts: Dict[int, int] = {}
    for g in month_glazes:
        rid = int(g["recipient_id"])
        counts[rid] = counts.get(rid, 0) + 1

    best = max(counts.values())
    tied = [rid for rid, c in counts.items() if c == best]
    if len(tied) == 1:
        return tied[0], best

    nth_time: Dict[int, str] = {}
    running: Dict[int, int] = {rid: 0 for rid in tied}
    for g in month_glazes:
        rid = int(g["recipient_id"])
        if rid not in running:
            continue
        running[rid] += 1
        if running[rid] == best and rid not in nth_time:
            nth_time[rid] = g["created_at"]

    tied.sort(key=lambda rid: nth_time.get(rid, "9999-99-99"))
    return tied[0], best


# =========================================================
# Scheduler
# =========================================================
def is_last_day_of_month_london(dt: datetime) -> bool:
    tomorrow = (dt + timedelta(days=1)).date()
    return tomorrow.day == 1

@tasks.loop(minutes=1)
async def glaze_scheduler():
    try:
        guild = await get_single_guild()
        if not guild:
            return

        data, sha = await load_data()
        drop_cid = data.get("config", {}).get("drop_channel_id")
        if not drop_cid:
            return

        drop_ch = guild.get_channel(int(drop_cid))
        if not isinstance(drop_ch, discord.TextChannel):
            return

        now_ldn = datetime.now(tz=LONDON)
        today_str = now_ldn.strftime("%Y-%m-%d")

        # daily drop
        if now_ldn.hour == DAILY_DROP_HOUR and now_ldn.minute == DAILY_DROP_MINUTE:
            if data["meta"].get("last_daily_drop_date") != today_str:
                pending = [g for g in data["glazes"] if not g.get("deleted") and not g.get("dropped_at")]
                pending.sort(key=lambda x: x.get("created_at", ""))

                if pending:
                    g = pending[0]
                    recipient = guild.get_member(int(g["recipient_id"]))
                    if recipient:
                        await drop_ch.send(f"{DAILY_PING_PREFIX}\n{recipient.mention}")
                        await drop_ch.send(embed=build_daily_embed(recipient.display_name, g["text"]))
                    g["dropped_at"] = iso_utc(now_utc())

                data["meta"]["last_daily_drop_date"] = today_str
                await save_data(data, sha, message="Daily glaze drop")

        # monthly drop
        if is_last_day_of_month_london(now_ldn) and now_ldn.hour == MONTHLY_DROP_HOUR and now_ldn.minute == MONTHLY_DROP_MINUTE:
            mk = datetime.now(timezone.utc).strftime("%Y-%m")
            already = data["meta"].get("last_monthly_announce", {}).get(mk)
            if not already:
                winner = compute_month_winner(data, mk)
                if winner:
                    winner_id, count = winner
                    await drop_ch.send(f"{MONTHLY_PING_PREFIX}\n@everyone")
                    await drop_ch.send(embed=build_monthly_embed(mk, f"<@{winner_id}>", count))

                    data["meta"]["last_monthly_announce"][mk] = iso_utc(now_utc())
                    data["wins"][str(winner_id)] = int(data["wins"].get(str(winner_id), 0)) + 1
                    await save_data(data, sha, message=f"Monthly most glazed {mk}")

    except Exception as e:
        print("Scheduler error:", repr(e))


# =========================================================
# Run
# =========================================================
if not TOKEN:
    raise RuntimeError("TOKEN env var is missing")

bot.run(TOKEN)