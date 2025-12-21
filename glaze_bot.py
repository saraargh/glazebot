ok wait you diff it. this is new 

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
        "test_channel_id": None,   # ✅ NEW (test-only channel)
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
# Embed styling
# =========================================================
GLAZE_YELLOW = discord.Color.from_rgb(255, 200, 64)
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


async def get_test_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    data, _ = await load_data()
    cid = data["config"].get("test_channel_id")
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
        view = ShareConfirmView(
            owner_id=self.owner_id,
            glaze_id=self.glaze_id,
            note=(self.note.value or "")
        )
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

        await interaction.response.edit_message(
            content="📣 Glaze shared in the server 🍯",
            view=None
        )


# =========================================================
# UI: Report view (mod message) with Delete & Scold
# =========================================================
class DeleteScoldView(discord.ui.View):
    def __init__(self, glaze_id: str):
        super().__init__(timeout=None)
        self.glaze_id = glaze_id

    @discord.ui.button(
        label="💥 Delete Glaze and Scold Glazer",
        style=discord.ButtonStyle.danger
    )
    async def delete_scold(self, interaction: discord.Interaction, button: discord.ui.Button):
        data, sha = await load_data()
        admin_roles = data["config"].get("admin_role_ids", [])

        if not is_admin(interaction, admin_roles):
            await interaction.response.send_message(
                "🚫 You don’t have permission to do that.",
                ephemeral=True
            )
            return

        glaze = next(
            (g for g in data["glazes"] if g["id"] == self.glaze_id),
            None
        )
        if not glaze or glaze.get("deleted"):
            await interaction.response.send_message(
                "😔 That glaze is already deleted or missing.",
                ephemeral=True
            )
            return

        glaze["deleted"] = True
        await save_data(data, sha, message="Delete glaze (mod action)")

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
        await interaction.response.edit_message(
            content="✅ Deleted and scolded.",
            view=self
        )


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
        g = next(
            (x for x in data["glazes"] if x["id"] == self.glaze_ids[self.index]),
            None
        )
        if not g or g.get("deleted"):
            return None
        return g

    async def _render(self, interaction: discord.Interaction):
        g = await self._get_current_glaze()
        if not g:
            await interaction.response.edit_message(
                content="😔 That glaze is no longer available.",
                embed=None,
                view=None
            )
            return

        created = parse_iso(g["created_at"]).astimezone(LONDON)
        received_str = created.strftime("%d %b %Y")

        embed = build_my_glaze_embed(
            self.index,
            len(self.glaze_ids),
            g["text"],
            received_str
        )

        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.glaze_ids) - 1

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        self.index -= 1
        await self._render(interaction)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        self.index += 1
        await self._render(interaction)

    @discord.ui.button(label="Say Thanks! 💐", style=discord.ButtonStyle.secondary)
    async def thanks_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        g = await self._get_current_glaze()
        if not g:
            await interaction.response.send_message(
                "😔 That glaze is no longer available.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ThanksModal(
                sender_id=int(g["sender_id"]),
                glaze_text=g["text"]
            )
        )

    @discord.ui.button(label="Report ⚠️", style=discord.ButtonStyle.secondary)
    async def report_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        g = await self._get_current_glaze()
        if not g:
            await interaction.response.send_message(
                "😔 That glaze is no longer available.",
                ephemeral=True
            )
            return
        await report_glaze(interaction, g["id"])

    @discord.ui.button(label="📣 Share", style=discord.ButtonStyle.secondary)
    async def share_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOUR_MENU, ephemeral=True)
            return
        g = await self._get_current_glaze()
        if not g:
            await interaction.response.send_message(
                "⚠️ This glaze can’t be shared.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ShareModal(
                owner_id=self.owner_id,
                glaze_id=g["id"]
            )
        )

# =========================================================
# Core actions (open glazes, DM mail, share, report)
# =========================================================
async def open_my_glazes(interaction: discord.Interaction):
    data, _ = await load_data()
    glz = [
        g for g in data["glazes"]
        if int(g["recipient_id"]) == interaction.user.id and not g.get("deleted")
    ]
    if not glz:
        await interaction.response.send_message(
            "😔 You don’t have any glazes yet…",
            ephemeral=True
        )
        return

    glz.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    ids = [g["id"] for g in glz]

    first = glz[0]
    created = parse_iso(first["created_at"]).astimezone(LONDON).strftime("%d %b %Y")
    embed = build_my_glaze_embed(0, len(ids), first["text"], created)

    view = MyGlazesView(owner_id=interaction.user.id, glaze_ids=ids)
    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True
    )


async def send_glaze_mail(interaction: discord.Interaction):
    data, _ = await load_data()
    glz = [
        g for g in data["glazes"]
        if int(g["recipient_id"]) == interaction.user.id and not g.get("deleted")
    ]
    if not glz:
        await interaction.response.send_message(
            "😔 You don’t have any glazes yet…",
            ephemeral=True
        )
        return

    glz.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    lines = ["💌 **Your Glaze Mail**\n"]
    for g in glz[:50]:
        dt = parse_iso(g["created_at"]).astimezone(LONDON).strftime("%d %b %Y")
        lines.append(f"• {dt} — “{g['text']}”")

    try:
        await interaction.user.send("\n".join(lines))
        await interaction.response.send_message(
            "💌 Glaze Mail complete — check your DMs!",
            ephemeral=True
        )
    except Exception:
        await interaction.response.send_message(
            "⚠️ I couldn’t DM you — please enable DMs.",
            ephemeral=True
        )


async def share_glaze(
    interaction: discord.Interaction,
    glaze_id: str,
    note: str
) -> Tuple[bool, str]:
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
        return False, "⚠️ Drop channel isn’t set."

    embed = build_shared_embed(glaze["text"], note)
    await ch.send(embed=embed)
    return True, "ok"


async def report_glaze(interaction: discord.Interaction, glaze_id: str):
    guild = await get_single_guild()
    if not guild:
        await interaction.response.send_message(
            "⚠️ Server not ready.",
            ephemeral=True
        )
        return

    report_ch = await get_report_channel(guild)
    if not report_ch:
        await interaction.response.send_message(
            "⚠️ Report channel isn’t set.",
            ephemeral=True
        )
        return

    data, sha = await load_data()
    glaze = next((g for g in data["glazes"] if g["id"] == glaze_id), None)
    if not glaze or glaze.get("deleted"):
        await interaction.response.send_message(
            "😔 That glaze is no longer available.",
            ephemeral=True
        )
        return

    glaze["reported"] = True
    await save_data(data, sha, message="Report glaze")

    content = (
        "⚠️ **GLAZE REPORTED**\n\n"
        f"Reported by: {interaction.user.mention}\n"
        f"Glaze was for: <@{int(glaze['recipient_id'])}>\n\n"
        f"Content:\n“{glaze['text']}”"
    )

    await report_ch.send(
        content,
        view=DeleteScoldView(glaze_id=glaze_id)
    )

    await interaction.response.send_message(
        "⚠️ Report sent to the mods.",
        ephemeral=True
    )


# =========================================================
# Commands
# =========================================================
@bot.tree.command(name="controlpanel", description="Configure Glaze settings")
@app_commands.describe(
    drop_channel="Channel for daily & monthly glazes",
    report_channel="Channel for reported glazes",
    test_channel="Channel for test drops ONLY",
    admin_role="Add or remove an admin role"
)
async def controlpanel(
    interaction: discord.Interaction,
    drop_channel: discord.TextChannel | None = None,
    report_channel: discord.TextChannel | None = None,
    test_channel: discord.TextChannel | None = None,
    admin_role: discord.Role | None = None
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("🚫 Admins only.")
        return

    data, sha = await load_data()
    changes = []

    if drop_channel:
        data["config"]["drop_channel_id"] = drop_channel.id
        changes.append(f"• Drop channel → {drop_channel.mention}")

    if report_channel:
        data["config"]["report_channel_id"] = report_channel.id
        changes.append(f"• Report channel → {report_channel.mention}")

    if test_channel:
        data["config"]["test_channel_id"] = test_channel.id
        changes.append(f"• Test channel → {test_channel.mention}")

    if admin_role:
        roles = set(data["config"].get("admin_role_ids", []))
        if admin_role.id in roles:
            roles.remove(admin_role.id)
            changes.append(f"• Admin role removed → {admin_role.mention}")
        else:
            roles.add(admin_role.id)
            changes.append(f"• Admin role added → {admin_role.mention}")
        data["config"]["admin_role_ids"] = list(roles)

    if not changes:
        await interaction.response.send_message("🍯 Nothing changed.")
        return

    await save_data(data, sha, message="Update controlpanel")
    await interaction.response.send_message(
        "🍯 **Glaze configuration updated**\n" + "\n".join(changes)
    )


@bot.tree.command(name="testdrop", description="Test a glaze drop (FAKE, admin only)")
@app_commands.choices(
    kind=[
        app_commands.Choice(name="Daily Glaze", value="daily"),
        app_commands.Choice(name="Monthly Glaze", value="monthly")
    ]
)
async def testdrop(interaction: discord.Interaction, kind: app_commands.Choice[str]):
    data, _ = await load_data()
    admin_roles = data["config"].get("admin_role_ids", [])

    if not is_admin(interaction, admin_roles):
        await interaction.response.send_message("🚫 No permission.")
        return

    guild = await get_single_guild()
    if not guild:
        await interaction.response.send_message("⚠️ Server not ready.")
        return

    test_ch = await get_test_channel(guild)
    if not test_ch:
        await interaction.response.send_message(
            "⚠️ Test channel not set.",
            ephemeral=True
        )
        return

    fake_user = interaction.user

    if kind.value == "daily":
        await test_ch.send(
            f"🍯 **(TEST)** A glaze has landed…\n{fake_user.mention}"
        )
        await test_ch.send(
            embed=build_daily_embed(
                fake_user.display_name,
                "This is a **test glaze** — no real data was used 🍯"
            )
        )

    else:
        mk = datetime.now(timezone.utc).strftime("%Y-%m")
        await test_ch.send(
            "🍯🍯🍯 **(TEST)** MONTHLY GLAZE RESULTS 🍯🍯🍯\n@everyone"
        )
        await test_ch.send(
            embed=build_monthly_embed(
                mk,
                fake_user.mention,
                99
            )
        )

    await interaction.response.send_message(
        "🍯 Test drop sent to test channel."
    )


# =========================================================
# Monthly winner calculation
# =========================================================
def compute_month_winner(
    data: Dict[str, Any],
    month_key_str: str
) -> Optional[Tuple[int, int]]:
    month_glazes = [
        g for g in data["glazes"]
        if g.get("month_key") == month_key_str and not g.get("deleted")
    ]
    if not month_glazes:
        return None

    counts: Dict[int, int] = {}
    for g in month_glazes:
        rid = int(g["recipient_id"])
        counts[rid] = counts.get(rid, 0) + 1

    winner = max(counts.items(), key=lambda x: x[1])
    return winner[0], winner[1]


# =========================================================
# Scheduler (UNCHANGED)
# =========================================================
@tasks.loop(minutes=1)
async def glaze_scheduler():
    try:
        guild = await get_single_guild()
        if not guild:
            return

        data, sha = await load_data()
        drop_ch = await get_drop_channel(guild)
        if not drop_ch:
            return

        now_ldn = datetime.now(tz=LONDON)
        today = now_ldn.strftime("%Y-%m-%d")

        if (
            now_ldn.hour == DAILY_DROP_HOUR
            and now_ldn.minute == DAILY_DROP_MINUTE
            and data["meta"].get("last_daily_drop_date") != today
        ):
            pending = [
                g for g in data["glazes"]
                if not g.get("deleted") and not g.get("dropped_at")
            ]
            pending.sort(key=lambda x: x.get("created_at", ""))

            if pending:
                g = pending[0]
                member = guild.get_member(int(g["recipient_id"]))
                if member:
                    await drop_ch.send(
                        f"{DAILY_PING_PREFIX}\n{member.mention}"
                    )
                    await drop_ch.send(
                        embed=build_daily_embed(
                            member.display_name,
                            g["text"]
                        )
                    )
                g["dropped_at"] = iso_utc(now_utc())

            data["meta"]["last_daily_drop_date"] = today
            await save_data(data, sha, message="Daily glaze drop")

    except Exception as e:
        print("Scheduler error:", repr(e))


# =========================================================
# Run
# =========================================================
if not TOKEN:
    raise RuntimeError("TOKEN env var is missing")

bot.run(TOKEN)