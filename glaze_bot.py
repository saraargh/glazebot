from __future__ import annotations

import os
import json
import uuid
import base64
import threading
import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

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

# Defaults (can be overridden by /controlpanel settings)
DEFAULT_DAILY_DROP_HOUR = 17
DEFAULT_DAILY_DROP_MINUTE = 0

MONTHLY_DROP_HOUR = 18
MONTHLY_DROP_MINUTE = 0

DAILY_PING_PREFIX = "🍯 A glaze has landed…"
MONTHLY_PING_PREFIX = "🍯 MONTHLY GLAZE RESULTS..."

FOOTER_TEXT = "Use /glaze to submit an anonymous glaze — remember to keep it SFW! ⚠️"
DROP_FOOTER_TEXT = "Use /help to learn how to send a glaze or say thank you!🍯"

SELF_GLAZE_ROAST = "🚫🚫 {user} only ugly people glaze themselves — try being nice to someone else!"
NOT_YOUR_MENU = "🍯 Hands off — this glaze menu isn’t yours!"

GLAZE_DISABLED_MSG = "🍯 Glaze has been switched off by the admins. Try again later."

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

        # ✅ restore approval buttons for pending approvals (so old messages still work after restart)
        try:
            data, _ = await load_data()
            for g in data.get("glazes", []):
                if g.get("approval_status") == "pending":
                    msg = g.get("approval_message") or {}
                    mid = msg.get("message_id")
                    if mid:
                        bot.add_view(ApprovalView(g["id"]), message_id=int(mid))
        except Exception as e:
            print("Approval restore error:", repr(e))

        glaze_scheduler.start()

    async def on_ready(self):
        print(f"🍯 Glaze online as {self.user}")

bot = GlazeBot()


# =========================================================
# GitHub JSON Store
# =========================================================
DEFAULT_DATA: Dict[str, Any] = {
    "config": {
        "drop_channel_id": None,
        "report_channel_id": None,
        "admin_role_ids": [],
        # NEW:
        # daily_drop_limit can be an int OR the literal string "all"
        "daily_drop_limit": 1,
        "daily_drop_hour": DEFAULT_DAILY_DROP_HOUR,
        "daily_drop_minute": DEFAULT_DAILY_DROP_MINUTE,
        "cooldown_hours": 12,
        "enabled": True,
        "approvals_enabled": False,
        "approval_channel_id": None,
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

    need_create = False
    created_data: Optional[Dict[str, Any]] = None

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
            created_data = _deepcopy(DEFAULT_DATA)
            _cached_data = created_data
            _cached_sha = None
            need_create = True

        else:
            raise RuntimeError(r.text)

    # IMPORTANT: save OUTSIDE the lock
    if need_create and created_data is not None:
        await save_data(created_data, None, "Create glaze_data.json")
        return created_data, None

    # fallback (shouldn't hit)
    return _cached_data or _deepcopy(DEFAULT_DATA), _cached_sha


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

def _get_cooldown_td(data: Dict[str, Any]) -> timedelta:
    try:
        hours = int(data.get("config", {}).get("cooldown_hours", 12))
    except Exception:
        hours = 12
    hours = max(1, min(168, hours))  # 1h to 7 days
    return timedelta(hours=hours)

def now_utc():
    return datetime.now(timezone.utc)

def iso_utc(dt):
    return dt.astimezone(timezone.utc).isoformat()

def parse_iso(s):
    return datetime.fromisoformat(s)

def month_key(dt):
    return dt.strftime("%Y-%m")


def _get_daily_drop_settings(data: Dict[str, Any]) -> Tuple[int, int, Union[int, str]]:
    cfg = data.get("config", {})
    hour = int(cfg.get("daily_drop_hour", DEFAULT_DAILY_DROP_HOUR) or DEFAULT_DAILY_DROP_HOUR)
    minute = int(cfg.get("daily_drop_minute", DEFAULT_DAILY_DROP_MINUTE) or DEFAULT_DAILY_DROP_MINUTE)
    limit = cfg.get("daily_drop_limit", 1)

    # Normalise limit
    if isinstance(limit, str):
        limit = limit.strip().lower()
        if limit != "all":
            # try cast
            try:
                limit = int(limit)
            except Exception:
                limit = 1
    elif isinstance(limit, (int, float)):
        limit = int(limit)
    else:
        limit = 1

    # clamp hour/minute
    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))

    if limit != "all":
        if limit < 1:
            limit = 1

    return hour, minute, limit


# =========================================================
# Embed styling
# =========================================================
GLAZE_YELLOW = discord.Color.from_rgb(255, 200, 64)


# =========================================================
# Embeds
# =========================================================
def build_daily_embed(recipient_display: str, text: str) -> discord.Embed:
    e = discord.Embed(
        title="🍯 GLAZEEEEE DROP",
        description=f"Today’s glaze is for **{recipient_display}**\n\n*“{text}”*",
        color=GLAZE_YELLOW
    )
    e.set_footer(text=DROP_FOOTER_TEXT)
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
        ),
        color=GLAZE_YELLOW
    )

    if MONTHLY_GIF_URL:
        e.set_image(url=MONTHLY_GIF_URL)

    e.set_footer(text=DROP_FOOTER_TEXT)
    return e


def build_my_glaze_embed(
    index: int,
    total: int,
    glaze_text: str,
    received_date_str: str
) -> discord.Embed:
    e = discord.Embed(
        title=f"🍯 Your Glaze ({index + 1} / {total})",
        description=f"*“{glaze_text}”*\n\n📅 Received: {received_date_str}",
        color=GLAZE_YELLOW
    )
    e.set_footer(text=FOOTER_TEXT)
    return e


def build_shared_embed(glaze_text: str, note: str) -> discord.Embed:
    e = discord.Embed(
        title="🍯 SHARED GLAZE",
        description=f"*“{glaze_text}”*",
        color=GLAZE_YELLOW
    )

    if note.strip():
        e.add_field(
            name="💬",
            value=f"“{note.strip()}”",
            inline=False
        )

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

async def get_approval_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    data, _ = await load_data()
    cid = data["config"].get("approval_channel_id")
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

###### UI APPROVAL########
def build_approval_embed(guild: discord.Guild, glaze: Dict[str, Any]) -> discord.Embed:
    recipient = guild.get_member(int(glaze["recipient_id"]))
    recipient_txt = recipient.mention if recipient else f"<@{int(glaze['recipient_id'])}>"

    e = discord.Embed(
        title="🛂 Glaze Approval Needed",
        description=f"**For:** {recipient_txt}\n\n**Glaze:**\n“{glaze['text']}”",
        color=GLAZE_YELLOW
    )
    e.set_footer(text=f"Glaze ID: {glaze['id']}")
    return e


class ApprovalView(discord.ui.View):
    def __init__(self, glaze_id: str):
        super().__init__(timeout=None)  # persistent-capable
        self.glaze_id = glaze_id

        # ✅ make button custom_ids unique per glaze
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.label == "✅ Approve":
                    child.custom_id = f"glaze_approve:{glaze_id}"
                elif child.label == "❌ Decline":
                    child.custom_id = f"glaze_decline:{glaze_id}"

    async def _admin_check(self, interaction: discord.Interaction) -> bool:
        data, _ = await load_data()
        admin_roles = data.get("config", {}).get("admin_role_ids", []) or []
        if not is_admin(interaction, admin_roles):
            await interaction.response.send_message("🚫 You don’t have permission to do that.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success)
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._admin_check(interaction):
            return

        data, sha = await load_data()
        g = next((x for x in data["glazes"] if x["id"] == self.glaze_id), None)
        if not g:
            await interaction.response.send_message("😔 That glaze no longer exists.", ephemeral=True)
            return

        if g.get("approval_status") != "pending":
            await interaction.response.send_message("ℹ️ This glaze was already processed.", ephemeral=True)
            return

        g["approved"] = True
        g["approval_status"] = "approved"
        g["approved_at"] = iso_utc(now_utc())
        g["approved_by"] = interaction.user.id

        await save_data(data, sha, message="Approve glaze")

        # disable buttons + mark message
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        await interaction.response.edit_message(content="✅ Approved.", view=self)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._admin_check(interaction):
            return

        data, sha = await load_data()
        g = next((x for x in data["glazes"] if x["id"] == self.glaze_id), None)
        if not g:
            await interaction.response.send_message("😔 That glaze no longer exists.", ephemeral=True)
            return

        if g.get("approval_status") != "pending":
            await interaction.response.send_message("ℹ️ This glaze was already processed.", ephemeral=True)
            return

        g["approved"] = False
        g["approval_status"] = "declined"
        g["declined_at"] = iso_utc(now_utc())
        g["declined_by"] = interaction.user.id
        g["deleted"] = True  # ensures it never appears anywhere

        await save_data(data, sha, message="Decline glaze")

        # DM the sender
        try:
            u = await bot.fetch_user(int(g["sender_id"]))
            await u.send(
                "⚠️ Your glaze was **flagged as inappropriate** and wasn’t approved.\n\n"
                "Please remember to be kind and keep glazes **SFW**. 🍯"
            )
        except Exception:
            pass

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        sender_txt = f"<@{int(g['sender_id'])}>"
        await interaction.response.edit_message(
            content=f"❌ Declined (sender notified). Sender was: {sender_txt}",
            view=self
        )

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
        if not g or g.get("deleted") or not g.get("approved"):
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
    glz = [g for g in data["glazes"] if int(g["recipient_id"]) == interaction.user.id and g.get("approved") and not g.get("deleted")]
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
    glz = [g for g in data["glazes"] if int(g["recipient_id"]) == interaction.user.id and g.get("approved") and not g.get("deleted")]
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
    admin_role="Add or remove an admin role (run multiple times)",
    daily_drop_limit='Daily drop limit: number (e.g. "3") or the literal string "all"',
    daily_drop_hour="Daily drop hour (0-23) London time",
    daily_drop_minute="Daily drop minute (0-59) London time",
    cooldown_hours="Cooldown between /glaze uses (hours)",
    glaze_enabled="Enable/disable Glaze submissions (True=on, False=off)",
    approval_channel="Channel where glazes go for approval",
    approvals_enabled="Require approval before glazes are accepted",
)
async def controlpanel(
    interaction: discord.Interaction,
    drop_channel: discord.TextChannel | None = None,
    report_channel: discord.TextChannel | None = None,
    admin_role: discord.Role | None = None,
    daily_drop_limit: str | None = None,
    daily_drop_hour: int | None = None,
    daily_drop_minute: int | None = None,
    cooldown_hours: int | None = None,
    glaze_enabled: bool | None = None,
    approval_channel: discord.TextChannel | None = None,
    approvals_enabled: bool | None = None,
):
    # MUST be non-ephemeral (interfering admins)
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("🚫 Admins only.")
        return

    data, sha = await load_data()
    changes: List[str] = []
    
    if approvals_enabled is not None:
        data["config"]["approvals_enabled"] = bool(approvals_enabled)
        changes.append(f"• Approvals → {'ON ✅' if approvals_enabled else 'OFF 📴'}")

    if approval_channel is not None:
        data["config"]["approval_channel_id"] = approval_channel.id
        changes.append(f"• Approval channel → {approval_channel.mention}")

    if cooldown_hours is not None:
        h = max(1, min(168, int(cooldown_hours)))
        data["config"]["cooldown_hours"] = h
        changes.append(f"• Cooldown → {h} hour(s)")

    if glaze_enabled is not None:
        data["config"]["enabled"] = bool(glaze_enabled)
        changes.append(f"• Glaze submissions → {'ON ✅' if glaze_enabled else 'OFF 📴'}")

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

    if daily_drop_limit is not None:
        val = daily_drop_limit.strip().lower()
        if val == "all":
            data["config"]["daily_drop_limit"] = "all"
            changes.append('• Daily drop limit → "all" (drops all undropped glazes)')
        else:
            try:
                n = int(val)
                if n < 1:
                    raise ValueError()
                data["config"]["daily_drop_limit"] = n
                changes.append(f"• Daily drop limit → {n}")
            except Exception:
                await interaction.response.send_message(
                    '🍯 Invalid daily_drop_limit. Use a number like "3" or the literal string "all".'
                )
                return

    if daily_drop_hour is not None:
        h = max(0, min(23, int(daily_drop_hour)))
        data["config"]["daily_drop_hour"] = h
        changes.append(f"• Daily drop hour → {h:02d} (London)")

    if daily_drop_minute is not None:
        m = max(0, min(59, int(daily_drop_minute)))
        data["config"]["daily_drop_minute"] = m
        changes.append(f"• Daily drop minute → {m:02d} (London)")

    if not changes:
        await interaction.response.send_message("🍯 Nothing changed — provide at least one option to update.")
        return

    await save_data(data, sha, message="Update Glaze controlpanel")

    hour, minute, limit = _get_daily_drop_settings(data)
    current_roles = ", ".join(f"<@&{r}>" for r in data["config"]["admin_role_ids"]) or "None"
    limit_str = "all" if limit == "all" else str(limit)
    enabled = bool(data["config"].get("enabled", True))

    await interaction.response.send_message(
        "🍯 **Glaze configuration updated**\n"
        + "\n".join(changes)
        + f"\n\n**Current settings:**"
        + f"\n• Admin roles: {current_roles}"
        + f"\n• Glaze submissions: {'ON ✅' if enabled else 'OFF 📴'}"
        + f"\n• Daily drop time: {hour:02d}:{minute:02d} (London)"
        + f"\n• Daily drop limit: {limit_str}"
        + "\n\n**Daily drop limit rules:**"
        + "\n• `1` → drops 1 glaze"
        + "\n• `N` → drops N glazes"
        + '\n• `"all"` → drops **all undropped glazes**'
    )


@bot.tree.command(name="glaze", description="Send an anonymous glaze to someone.")
@app_commands.describe(member="Who are you glazing?", message="Write something nice (keep it SFW!)")
async def glaze_cmd(interaction: discord.Interaction, member: discord.Member, message: str):
    guild = await get_single_guild()
    if not guild:
        await interaction.response.send_message("⚠️ Server not ready.", ephemeral=True)
        return

    # ✅ ack instantly so GitHub work doesn't time out the interaction
    await interaction.response.defer(ephemeral=True)

    if member.id == interaction.user.id:
        await interaction.followup.send(
            SELF_GLAZE_ROAST.format(user=interaction.user.mention),
            ephemeral=False
        )
        return

    text = message.strip()
    if len(text) < 10:
        await interaction.followup.send("🍯 Make it a bit longer — at least 10 characters.", ephemeral=True)
        return
    if len(text) > 500:
        await interaction.followup.send("🍯 Keep it under 500 characters please.", ephemeral=True)
        return

    global _cached_data, _cached_sha
    _cached_data = None
    _cached_sha = None

    data, sha = await load_data()

    if not bool(data.get("config", {}).get("enabled", True)):
        await interaction.followup.send(GLAZE_DISABLED_MSG, ephemeral=True)
        return

    cd = _get_cooldown_td(data)
    last = data["cooldowns"].get(str(interaction.user.id))
    if last:
        diff = now_utc() - parse_iso(last)
        if diff < cd:
            await interaction.followup.send("⏳ You’re on cooldown — try again later.", ephemeral=True)
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
        "reported": False,
        "approved": True,
        "approval_status": "approved",
        "approval_message": None
    }

    data["glazes"].append(glaze)
    data["cooldowns"][str(interaction.user.id)] = iso_utc(created)

    approvals_on = bool(data.get("config", {}).get("approvals_enabled", False))

    if approvals_on:
        glaze["approved"] = False
        glaze["approval_status"] = "pending"

        await save_data(data, sha, message="Add glaze (pending approval)")

        approval_ch = await get_approval_channel(guild)
        if approval_ch:
            msg = await approval_ch.send(
                embed=build_approval_embed(guild, glaze),
                view=ApprovalView(glaze_id=g_id)
            )

            _cached_data = None
            _cached_sha = None
            data2, sha2 = await load_data()
            g2 = next((x for x in data2["glazes"] if x["id"] == g_id), None)
            if g2:
                g2["approval_message"] = {
                    "channel_id": approval_ch.id,
                    "message_id": msg.id
                }
                await save_data(data2, sha2, message="Store approval message ids")
    else:
        await save_data(data, sha, message="Add glaze")

    await interaction.followup.send("✅ Your glaze has been submitted! 🍯", ephemeral=True)

@bot.tree.command(name="myglaze", description="See your glazes (buttons + DM option).")
async def myglaze_cmd(interaction: discord.Interaction):
    data, _ = await load_data()
    glz = [g for g in data["glazes"] if int(g["recipient_id"]) == interaction.user.id and g.get("approved") and not g.get("deleted")]
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
        if not g.get("deleted") and g.get("approved"):
            sid = int(g["sender_id"])
            sender_counts[sid] = sender_counts.get(sid, 0) + 1

    if sender_counts:
        sorted_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        sender_lines = [f"**{i}.** <@{uid}>" for i, (uid, cnt) in enumerate(sorted_senders, start=1)]
    else:
        sender_lines = ["No glazes sent yet 🍯"]

    embed = discord.Embed(title="🍯 Glaze Leaderboard")
    embed.add_field(name="🏆 Most Glazed (Monthly Wins)", value="\n".join(monthly_lines), inline=False)
    embed.add_field(name="🍯 Top Glazers (Most Sent)", value="\n".join(sender_lines), inline=False)
    embed.set_footer(text=FOOTER_TEXT)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="randomdrop", description="Drop one random pending glaze right now (Glaze admins only).")
async def randomdrop_cmd(interaction: discord.Interaction):
    data, sha = await load_data()
    
    if not bool(data.get("config", {}).get("enabled", True)):
        await interaction.response.send_message("🍯 Glaze is switched off right now.", ephemeral=True)
        return
    
    admin_roles = data["config"].get("admin_role_ids", [])

    if not is_admin(interaction, admin_roles):
        await interaction.response.send_message("🚫 You don’t have permission to do that.", ephemeral=True)
        return

    guild = await get_single_guild()
    if not guild:
        await interaction.response.send_message("⚠️ Server not ready.", ephemeral=True)
        return

    drop_ch = await get_drop_channel(guild)
    if not drop_ch:
        await interaction.response.send_message("⚠️ Drop channel isn’t set. Ask an admin to run /controlpanel.", ephemeral=True)
        return

    pending = [g for g in data["glazes"] if g.get("approved") and not g.get("deleted") and not g.get("dropped_at")]
    if not pending:
        await interaction.response.send_message("🍯 No pending glazes to drop.", ephemeral=True)
        return

    g = random.choice(pending)
    recipient = guild.get_member(int(g["recipient_id"]))

    # post publicly
    if recipient:
        await drop_ch.send(f"{DAILY_PING_PREFIX}\n{recipient.mention}")
        await drop_ch.send(embed=build_daily_embed(recipient.display_name, g["text"]))
    else:
        await drop_ch.send(f"{DAILY_PING_PREFIX}\n<@{int(g['recipient_id'])}>")
        await drop_ch.send(embed=build_daily_embed("Someone", g["text"]))

    # mark as dropped (REAL drop) — but DO NOT touch last_daily_drop_date
    g["dropped_at"] = iso_utc(now_utc())
    await save_data(data, sha, message="Random glaze drop (admin)")

    await interaction.response.send_message("🍯 Random glaze dropped.", ephemeral=True)


# =========================================================
# Monthly winner calculation
# =========================================================
def compute_month_winner(data: Dict[str, Any], month_key_str: str) -> Optional[Tuple[int, int]]:
    month_glazes = [g for g in data["glazes"] if g.get("month_key") == month_key_str and g.get("approved") and not g.get("deleted")]
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


##### help command ######
@bot.tree.command(name="help", description="How Glaze works 🍯")
@app_commands.describe(admin="Show admin-only help (Glaze admins only)")
async def help_cmd(interaction: discord.Interaction, admin: bool | None = False):
    data, _ = await load_data()
    hour, minute, limit = _get_daily_drop_settings(data)
    cd_hours = int(_get_cooldown_td(data).total_seconds() // 3600)
    limit_str = "all" if limit == "all" else str(limit)

    embed = discord.Embed(
        title="🍯 Glaze Help",
        description=(
            "**Glaze lets you send anonymous kindness to other members.**\n\n"
            f"Daily drops happen at **{hour:02d}:{minute:02d} London time**.\n"
            f"Daily drop limit is currently **{limit_str}**."
        ),
        color=GLAZE_YELLOW
    )

    embed.add_field(
        name="✨ Commands",
        value=(
            "`/glaze <member> <message>`\n"
            "Send an anonymous glaze\n\n"
            "`/myglaze`\n"
            "View glazes you’ve received (buttons + DM option)\n\n"
            "`/glazeleaderboard`\n"
            "See monthly winners & top glazers"
        ),
        inline=False
    )

    embed.add_field(
        name="🕒 Rules",
        value=(
            f"• One glaze every **{cd_hours} hours**\n"
            "• Anonymous by default\n"
            "• Must be **kind & SFW**\n"
            "• Reported glazes may be removed"
        ),
        inline=False
    )

    embed.add_field(
        name="🍯 Drops",
        value=(
            "• Daily Drop posts glazes publicly with a ping + embed\n"
            "• Monthly Drop announces the most glazed member 🎉\n\n"
            "**Daily drop limit rules:**\n"
            "• `1` → drops 1 glaze\n"
            "• `N` → drops N glazes\n"
            "• `\"all\"` → drops **all undropped glazes**"
        ),
        inline=False
    )

    embed.set_footer(text=FOOTER_TEXT)

    if admin:
        admin_roles = data["config"].get("admin_role_ids", [])
        if not is_admin(interaction, admin_roles):
            await interaction.response.send_message("🍯 That section is for Glaze admins only.", ephemeral=True)
            return

        admin_embed = discord.Embed(
            title="🔒 Glaze Admin Help",
            description="Admin-only tools & moderation controls.",
            color=GLAZE_YELLOW
        )

        admin_embed.add_field(
            name="🛠️ Admin Commands",
            value=(
                "`/controlpanel`\n"
                "Set drop channel, report channel, Glaze admins, daily drop limit & daily drop time\n\n"
                "`/randomdrop`\n"
                "Drops **one random pending glaze** right now (real drop)\n"
                "✅ Marks it as dropped\n"
                "❌ Does not affect the 5pm daily-drop tracker"
            ),
            inline=False
        )

        admin_embed.add_field(
            name="⚠️ Moderation",
            value=(
                "• Reported glazes appear in the report channel\n"
                "• Mods can delete glazes & DM the sender\n"
                "• Deleted glazes never appear again"
            ),
            inline=False
        )

        admin_embed.set_footer(text=FOOTER_TEXT)
        await interaction.response.send_message(embeds=[embed, admin_embed])
        return

    await interaction.response.send_message(embed=embed)

###forcewinner####
@bot.tree.command(
    name="force_winner",
    description="Force-calculate & post a monthly winner (Glaze admins only)."
)
@app_commands.describe(
    month="Month to tally in YYYY-MM (blank = last month)",
    override="Post even if this month was already announced"
)
async def force_winner_cmd(
    interaction: discord.Interaction,
    month: str | None = None,
    override: bool | None = False
):
    # ---- admin check (Glaze admins)
    data, _ = await load_data()
    admin_roles = data.get("config", {}).get("admin_role_ids", []) or []
    if not is_admin(interaction, admin_roles):
        await interaction.response.send_message("🚫 You don’t have permission to do that.", ephemeral=True)
        return

    guild = await get_single_guild()
    if not guild:
        await interaction.response.send_message("⚠️ Server not ready.", ephemeral=True)
        return

    drop_ch = await get_drop_channel(guild)
    if not drop_ch:
        await interaction.response.send_message(
            "⚠️ Drop channel isn’t set. Ask an admin to run /controlpanel.",
            ephemeral=True
        )
        return

    # IMPORTANT: always read latest JSON (not stale cache)
    global _cached_data, _cached_sha
    _cached_data = None
    _cached_sha = None

    data, sha = await load_data()

    # ---- pick month (default = last month, London time)
    now_ldn = datetime.now(tz=LONDON)
    if month and month.strip():
        mk = month.strip()
        try:
            datetime.strptime(mk + "-01", "%Y-%m-%d")
        except Exception:
            await interaction.response.send_message(
                "🍯 Invalid month. Use **YYYY-MM** (e.g. `2026-01`).",
                ephemeral=True
            )
            return
    else:
        # last month (London)
        prev_month_dt = (now_ldn.replace(day=1) - timedelta(days=1))
        mk = prev_month_dt.strftime("%Y-%m")

    # ---- already announced?
    last_map = data.get("meta", {}).get("last_monthly_announce", {})
    if not isinstance(last_map, dict):
        data.setdefault("meta", {})["last_monthly_announce"] = {}
        last_map = data["meta"]["last_monthly_announce"]

    if last_map.get(mk) and not override:
        await interaction.response.send_message(
            f"🍯 Monthly winner for **{mk}** was already announced.\n"
            f"Run again with `override: True` to post it again.",
            ephemeral=True
        )
        return

    # ---- compute winner for that month
    winner = compute_month_winner(data, mk)
    if not winner:
        await interaction.response.send_message(
            f"🍯 No glazes found for **{mk}** — nothing to tally.",
            ephemeral=True
        )
        return

    winner_id, count = winner

    # ---- post publicly
    await drop_ch.send(f"{MONTHLY_PING_PREFIX}\n@everyone")
    await drop_ch.send(embed=build_monthly_embed(mk, f"<@{winner_id}>", count))

    # ---- update JSON markers + leaderboard
    data.setdefault("wins", {})
    data["wins"][str(winner_id)] = int(data["wins"].get(str(winner_id), 0)) + 1
    data["meta"]["last_monthly_announce"][mk] = iso_utc(now_utc())

    await save_data(data, sha, message=f"Force monthly most glazed {mk}")

    await interaction.response.send_message(
        f"✅ Posted monthly winner for **{mk}**.\nWinner: <@{winner_id}> with **{count}** glazes.",
        ephemeral=True
    )

# =========================================================
# Scheduler
# =========================================================
def is_last_day_of_month_london(dt: datetime) -> bool:
    tomorrow = (dt + timedelta(days=1)).date()
    return tomorrow.day == 1


async def _drop_one_glaze(drop_ch: discord.TextChannel, guild: discord.Guild, glaze: Dict[str, Any]) -> None:
    recipient = guild.get_member(int(glaze["recipient_id"]))
    if recipient:
        await drop_ch.send(f"{DAILY_PING_PREFIX}\n{recipient.mention}")
        await drop_ch.send(embed=build_daily_embed(recipient.display_name, glaze["text"]))
    else:
        await drop_ch.send(f"{DAILY_PING_PREFIX}\n<@{int(glaze['recipient_id'])}>")
        await drop_ch.send(embed=build_daily_embed("Someone", glaze["text"]))


@tasks.loop(minutes=1)
async def glaze_scheduler():
    try:
        guild = await get_single_guild()
        if not guild:
            return

        data, sha = await load_data()
        
        if not bool(data.get("config", {}).get("enabled", True)):
            return
        
        drop_cid = data.get("config", {}).get("drop_channel_id")
        if not drop_cid:
            return

        drop_ch = guild.get_channel(int(drop_cid))
        if not isinstance(drop_ch, discord.TextChannel):
            return

        now_ldn = datetime.now(tz=LONDON)
        today_str = now_ldn.strftime("%Y-%m-%d")

        # daily drop (uses controlpanel settings)
        hour, minute, limit = _get_daily_drop_settings(data)

        if now_ldn.hour == hour and now_ldn.minute == minute:
            if data["meta"].get("last_daily_drop_date") != today_str:
                pending = [g for g in data["glazes"] if g.get("approved") and not g.get("deleted") and not g.get("dropped_at")]
                pending.sort(key=lambda x: x.get("created_at", ""))  # oldest first

                if pending:
                    if limit == "all":
                        to_drop = pending[:]  # ALL undropped glazes
                    else:
                        to_drop = pending[: min(int(limit), len(pending))]

                    for g in to_drop:
                        await _drop_one_glaze(drop_ch, guild, g)
                        g["dropped_at"] = iso_utc(now_utc())

                # mark today's daily drop as done (ONLY the scheduler does this)
                data["meta"]["last_daily_drop_date"] = today_str
                await save_data(data, sha, message="Daily glaze drop")

        # monthly drop
        if is_last_day_of_month_london(now_ldn) and now_ldn.hour == MONTHLY_DROP_HOUR and now_ldn.minute == MONTHLY_DROP_MINUTE:
            mk = now_ldn.strftime("%Y-%m")
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