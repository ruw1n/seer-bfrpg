import copy
import datetime
import json
import os
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, List, Dict, Any

import nextcord
from nextcord.ext import commands


def _data_path() -> Path:
    return Path(os.getenv("RPXP_DATA_PATH", "data/rpxp.json"))


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _now_ts() -> int:
    return int(time.time())


def _week_id_chicago() -> str:
    tz = ZoneInfo("America/Chicago")
    # Shift by +1 day so the "week" flips at Sunday 00:00 local time
    d = datetime.datetime.now(tz).date() + datetime.timedelta(days=1)
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _emoji_to_str(emoji) -> str:
    try:
        if isinstance(emoji, str):
            return emoji
        if getattr(emoji, "id", None):
            return f"{emoji.name}:{emoji.id}"
        return str(getattr(emoji, "name", emoji))
    except Exception:
        return str(emoji)


class RPXPCog(commands.Cog):
    """Standalone RPXP cog (no seer dependencies)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = self._load()

    # ---------- storage ----------
    def _load(self) -> dict:
        path = _data_path()
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"guilds": {}}

    def _save(self) -> None:
        _atomic_write_json(_data_path(), self.data)

    def _g(self, guild_id: int) -> dict:
        g = self.data["guilds"].setdefault(str(guild_id), {})

        g.setdefault("channels", [])
        g.setdefault("week_id", _week_id_chicago())
        g.setdefault("users", {})
        g.setdefault("chan_state", {})
        g.setdefault("msg_state", {})

        g.setdefault("history", {})
        g.setdefault("notify", {"channel_id": 0, "role_id": 0})
        g.setdefault("pending_rollover", None)

        g.setdefault("settings", {
            "min_chars": 80,
            "min_words": 15,

            "cooldown_sec": 600,
            "partner_window_sec": 600,
            "partner_bonus_milli": 250,

            "post_1_10_milli": 1000,
            "post_11_25_milli": 500,
            "post_26_plus_milli": 100,

            "weekly_point_cap_milli": 40000,
            "xp_per_point": 25,
            "weekly_xp_cap": 1000,

            "history_weeks_keep": 12,

            "length_bonus_enabled": False,
            "length_bonus_start_chars": 80,
            "length_bonus_chars_per_step": 80,
            "length_bonus_milli_per_step": 100,
            "length_bonus_cap_milli": 500,

            "staff_bonus_enabled": False,
            "staff_required": False,
            "staff_bonus_milli": 1000,
            "staff_emojis": ["🔥", "⭐"],
            "staff_role_ids": [],

            "ignore_prefixes": ["!", "/"],
        })

        cur_week = _week_id_chicago()
        if g.get("week_id") != cur_week:
            old_week = str(g.get("week_id") or "").strip().upper()

            archive_id = None
            try:
                archive_id = self._archive_week(g, old_week, reason="rollover")
            except Exception:
                archive_id = None

            g["week_id"] = cur_week
            g["users"] = {}
            g["chan_state"] = {}
            g["msg_state"] = {}

            g["pending_rollover"] = {
                "old_week": old_week,
                "archive_id": archive_id or "",
                "ts": _now_ts(),
                "notified": False,
            }
            self._save()

        return g

    # ---------- archiving ----------
    def _prune_history(self, g: dict) -> None:
        hist = g.get("history", {})
        try:
            keep = int(g.get("settings", {}).get("history_weeks_keep", 12))
        except Exception:
            keep = 12

        if keep <= 0 or not isinstance(hist, dict) or len(hist) <= keep:
            return

        items = []
        for k, e in hist.items():
            ts = int((e or {}).get("archived_ts", 0) or 0)
            items.append((ts, k))
        items.sort(reverse=True)
        keep_keys = {k for _, k in items[:keep]}
        for k in list(hist.keys()):
            if k not in keep_keys:
                hist.pop(k, None)

    def _archive_week(
        self,
        g: dict,
        week_id: str,
        *,
        reason: str = "",
        actor_id: Optional[int] = None,
        payout: Optional[dict] = None,
    ) -> Optional[str]:
        week_id = str(week_id or "").strip().upper()
        if not week_id:
            return None

        users = g.get("users") or {}
        msg_state = g.get("msg_state") or {}
        if not users and not msg_state:
            return None

        hist = g.setdefault("history", {})
        archive_key = week_id
        if archive_key in hist:
            n = 2
            while f"{week_id}#{n}" in hist:
                n += 1
            archive_key = f"{week_id}#{n}"

        entry: Dict[str, Any] = {
            "week_id": week_id,
            "archive_id": archive_key,
            "archived_ts": _now_ts(),
            "reason": reason or "",
            "actor_id": int(actor_id) if actor_id is not None else 0,
            "users": copy.deepcopy(users),
            "chan_state": copy.deepcopy(g.get("chan_state") or {}),
            "msg_state": copy.deepcopy(msg_state),
            "settings_snapshot": copy.deepcopy(g.get("settings") or {}),
        }
        if payout is not None:
            entry["payout"] = payout
            entry["paid_ts"] = _now_ts()

        hist[archive_key] = entry
        self._prune_history(g)
        return archive_key

    def _format_week_lines(self, entry: dict, *, limit: int = 25) -> Tuple[List[str], int, float]:
        users = entry.get("users", {}) or {}
        s = entry.get("settings_snapshot", {}) or {}
        xp_per_point = int(s.get("xp_per_point", 25))
        weekly_xp_cap = int(s.get("weekly_xp_cap", 1000))

        rows = []
        total_pm = 0
        total_xp = 0
        for uid_str, u in users.items():
            try:
                uid = int(uid_str)
            except Exception:
                continue
            pm = int(u.get("points_milli", 0))
            if pm <= 0:
                continue
            posts = int(u.get("posts_scored", 0))
            pts = pm / 1000.0
            xp = int((pm * xp_per_point + 500) // 1000)
            xp = min(xp, weekly_xp_cap)
            total_pm += pm
            total_xp += xp
            rows.append((pm, uid, pts, xp, posts))

        rows.sort(reverse=True, key=lambda r: r[0])

        lines: List[str] = []
        for i, (_, uid, pts, xp, posts) in enumerate(rows[: max(1, limit)], start=1):
            lines.append(f"**{i}.** <@{uid}> — {pts:.2f} pts (~{xp} XP) — {posts} posts")

        return lines, int(total_xp), float(total_pm / 1000.0)

    async def _maybe_send_rollover_notice(self, guild: nextcord.Guild, g: dict) -> None:
        pr = g.get("pending_rollover")
        if not pr or pr.get("notified"):
            return

        archive_id = str(pr.get("archive_id") or "").strip()
        old_week = str(pr.get("old_week") or "").strip().upper()

        hist = g.get("history") or {}
        entry = hist.get(archive_id) if archive_id else None
        if entry is None and old_week:
            entry = hist.get(old_week)

        if entry is None:
            pr["notified"] = True
            self._save()
            return

        notify = g.get("notify") or {}
        ch_id = int(notify.get("channel_id") or 0)
        role_id = int(notify.get("role_id") or 0)

        channel = None
        if ch_id:
            channel = guild.get_channel(ch_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(ch_id)
                except Exception:
                    channel = None
        if channel is None:
            channel = guild.system_channel
        if channel is None:
            pr["notified"] = True
            self._save()
            return

        lines, total_xp, total_pts = self._format_week_lines(entry, limit=40)
        wk_label = entry.get("week_id", old_week or "?")

        embed = nextcord.Embed(
            title=f"RPXP Week Archived — {wk_label}",
            description=("\n".join(lines) if lines else "(no RP points recorded last week)"),
            color=nextcord.Color.blurple(),
        )
        embed.add_field(name="Totals (estimate)", value=f"{total_pts:.2f} pts → ~{total_xp} XP", inline=False)
        embed.set_footer(text="View: !rpxp prev / !rpxp history / !rpxp week <id> • Configure: !rpxp notify here / role @GM")

        ping = f"<@&{role_id}> " if role_id else ""
        try:
            await channel.send(content=ping + "Week rollover detected — last week has been archived.", embed=embed)
        except Exception:
            pr["notified"] = True
            self._save()
            return

        pr["notified"] = True
        pr["notified_ts"] = _now_ts()
        self._save()

    # ---------- scoring ----------
    def _is_flagged_channel(self, g: dict, channel: nextcord.abc.GuildChannel | nextcord.Thread) -> Tuple[bool, int]:
        flagged = set(int(x) for x in g.get("channels", []))

        if isinstance(channel, nextcord.Thread):
            if channel.id in flagged:
                return True, channel.id
            if channel.parent_id and channel.parent_id in flagged:
                return True, channel.id
            return False, channel.id

        return (channel.id in flagged), channel.id

    def _eligible_text_metrics(self, content: str) -> Tuple[int, int, bool]:
        s = re.sub(r"http\S+", "", content or "")
        s = re.sub(r"<@!?(\d+)>", "", s)
        s = re.sub(r"<#(\d+)>", "", s)
        s = re.sub(r"\s+", " ", s).strip()

        has_alpha = re.search(r"[A-Za-z]", s) is not None
        words = re.findall(r"[A-Za-z']+", s)
        return (len(s), len(words), has_alpha)

    def _post_award_milli(self, post_index_1_based: int, settings: dict) -> int:
        if post_index_1_based <= 10:
            return int(settings["post_1_10_milli"])
        if post_index_1_based <= 25:
            return int(settings["post_11_25_milli"])
        return int(settings["post_26_plus_milli"])

    def _length_bonus_milli(self, chars: int, settings: dict) -> int:
        if not bool(settings.get("length_bonus_enabled", False)):
            return 0

        start_chars = max(0, int(settings.get("length_bonus_start_chars", settings.get("min_chars", 0))))
        chars_per_step = max(1, int(settings.get("length_bonus_chars_per_step", 80)))
        milli_per_step = max(0, int(settings.get("length_bonus_milli_per_step", 100)))
        cap_milli = max(0, int(settings.get("length_bonus_cap_milli", 500)))

        extra_chars = max(0, int(chars) - start_chars)
        bonus = (extra_chars // chars_per_step) * milli_per_step
        return min(cap_milli, bonus)
            
    def _award_points(
        self,
        g: dict,
        *,
        user_id: int,
        effective_chan_id: int,
        msg_id: int,
        created_ts: int,
        is_staff_approval_award: bool = False,
        chars: int = 0,
    ) -> Tuple[bool, str, int]:
        settings = g["settings"]
        users = g["users"]
        u = users.setdefault(str(user_id), {"points_milli": 0, "posts_scored": 0, "last_scored_ts": 0})

        weekly_cap = int(settings["weekly_point_cap_milli"])
        if int(u["points_milli"]) >= weekly_cap:
            return (False, "weekly cap reached", 0)

        if not is_staff_approval_award:
            cooldown = int(settings["cooldown_sec"])
            last_ts = int(u.get("last_scored_ts", 0))
            if cooldown > 0 and (created_ts - last_ts) < cooldown:
                return (False, "cooldown", 0)

        post_index = int(u.get("posts_scored", 0)) + (0 if is_staff_approval_award else 1)
        base_milli = int(settings["staff_bonus_milli"]) if is_staff_approval_award else self._post_award_milli(post_index, settings)
        add_milli = base_milli
        
        if not is_staff_approval_award:
            add_milli += self._length_bonus_milli(chars, settings)
            
        if not is_staff_approval_award:
            ps = g["chan_state"].setdefault(str(effective_chan_id), {"last_author": 0, "last_ts": 0})
            last_author = int(ps.get("last_author", 0))
            last_ts = int(ps.get("last_ts", 0))
            window = int(settings["partner_window_sec"])

            if window > 0 and last_author and last_author != user_id and (created_ts - last_ts) <= window:
                add_milli += int(settings["partner_bonus_milli"])

            ps["last_author"] = int(user_id)
            ps["last_ts"] = int(created_ts)

        before = int(u["points_milli"])
        after = min(weekly_cap, before + add_milli)
        added = after - before
        if added <= 0:
            return (False, "weekly cap reached", 0)

        u["points_milli"] = after
        if not is_staff_approval_award:
            u["posts_scored"] = int(u.get("posts_scored", 0)) + 1
            u["last_scored_ts"] = int(created_ts)
            g["msg_state"][str(msg_id)] = {
                "user_id": int(user_id),
                "staff_bonus_applied": False,
                "ts": int(created_ts),
            }

        return (True, "ok", added)

    def _is_ignored_message(self, settings: dict, content: str) -> bool:
        s = (content or "").lstrip()
        if not s:
            return True
        prefixes = settings.get("ignore_prefixes") or ["!", "/"]
        for p in prefixes:
            if p and s.startswith(str(p)):
                return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: nextcord.Message):
        if not message.guild or message.author.bot:
            return

        g = self._g(message.guild.id)
        await self._maybe_send_rollover_notice(message.guild, g)
        settings = g["settings"]

        if self._is_ignored_message(settings, message.content or ""):
            return

        is_flagged, effective_chan_id = self._is_flagged_channel(g, message.channel)
        if not is_flagged:
            return

        if bool(settings.get("staff_required", False)):
            created_ts = int(message.created_at.timestamp())
            g["msg_state"][str(message.id)] = {
                "user_id": int(message.author.id),
                "staff_bonus_applied": False,
                "ts": int(created_ts),
                "pending_only": True,
            }
            self._save()
            return

        chars, words, has_alpha = self._eligible_text_metrics(message.content or "")
        if not has_alpha:
            return
        if not (chars >= int(settings["min_chars"]) or words >= int(settings["min_words"])):
            return

        created_ts = int(message.created_at.timestamp())
        awarded, _, _ = self._award_points(
            g,
            user_id=message.author.id,
            effective_chan_id=effective_chan_id,
            msg_id=message.id,
            created_ts=created_ts,
            is_staff_approval_award=False,
            chars=chars,
        )
        if awarded:
            self._save()


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: nextcord.RawReactionActionEvent):
        if payload.guild_id is None or payload.user_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        g = self._g(guild.id)
        await self._maybe_send_rollover_notice(guild, g)
        settings = g["settings"]

        if not (bool(settings.get("staff_bonus_enabled", False)) or bool(settings.get("staff_required", False))):
            return

        flagged = set(int(x) for x in g.get("channels", []))
        in_scope = payload.channel_id in flagged
        if not in_scope:
            try:
                ch = guild.get_channel(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
                if isinstance(ch, nextcord.Thread) and ch.parent_id and ch.parent_id in flagged:
                    in_scope = True
            except Exception:
                in_scope = False
        if not in_scope:
            return

        emoji_str = _emoji_to_str(payload.emoji)
        allowed = set(str(x) for x in settings.get("staff_emojis", []))
        if emoji_str not in allowed:
            return

        member = payload.member
        if member is None:
            try:
                member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
            except Exception:
                return

        role_ids = set(int(x) for x in settings.get("staff_role_ids", []) if str(x).isdigit())
        if role_ids:
            if not any(r.id in role_ids for r in getattr(member, "roles", [])):
                return

        ms = g["msg_state"].get(str(payload.message_id))
        if not ms or ms.get("staff_bonus_applied"):
            return

        user_id = int(ms.get("user_id", 0))
        if not user_id:
            return

        created_ts = int(ms.get("ts", _now_ts()))
        effective_chan_id = int(payload.channel_id)

        if bool(settings.get("staff_required", False)):
            try:
                ch = guild.get_channel(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
                msg = await ch.fetch_message(payload.message_id)
            except Exception:
                return

            chars, words, has_alpha = self._eligible_text_metrics(msg.content or "")
            if not has_alpha:
                return
            if not (chars >= int(settings["min_chars"]) or words >= int(settings["min_words"])):
                return

            awarded, _, _ = self._award_points(
                g,
                user_id=user_id,
                effective_chan_id=effective_chan_id,
                msg_id=payload.message_id,
                created_ts=created_ts,
                is_staff_approval_award=False,
            )
            if not awarded:
                return

            if bool(settings.get("staff_bonus_enabled", False)):
                self._award_points(
                    g,
                    user_id=user_id,
                    effective_chan_id=effective_chan_id,
                    msg_id=payload.message_id,
                    created_ts=_now_ts(),
                    is_staff_approval_award=True,
                )

            ms["staff_bonus_applied"] = True
            self._save()
            return

        awarded, _, _ = self._award_points(
            g,
            user_id=user_id,
            effective_chan_id=effective_chan_id,
            msg_id=payload.message_id,
            created_ts=_now_ts(),
            is_staff_approval_award=True,
        )
        if awarded:
            ms["staff_bonus_applied"] = True
            self._save()

    # ---------- commands ----------
    @commands.group(name="rpxp", invoke_without_command=True)
    async def rpxp(self, ctx: commands.Context):
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        u = g["users"].get(str(ctx.author.id), {"points_milli": 0, "posts_scored": 0})

        pm = int(u.get("points_milli", 0))
        points = pm / 1000.0
        posts = int(u.get("posts_scored", 0))

        s = g["settings"]
        xp_per_point = int(s["xp_per_point"])
        weekly_xp_cap = int(s["weekly_xp_cap"])

        est_xp = int((pm * xp_per_point + 500) // 1000)
        est_xp = min(est_xp, weekly_xp_cap)

        embed = nextcord.Embed(
            title="Weekly RP XP Tracker",
            description=(
                f"Week: **{g['week_id']}**\n"
                f"User: {ctx.author.mention}\n\n"
                f"**RP Points:** {points:.2f}\n"
                f"**Scored Posts:** {posts}\n"
                f"**Estimated XP payout:** {est_xp} XP\n"
            ),
            color=nextcord.Color.blurple(),
        )
        embed.add_field(
            name="Rules (current)",
            value=(
                f"Min content: {s['min_chars']} chars OR {s['min_words']} words\n"
                f"Cooldown: {int(s['cooldown_sec'])//60} min\n"
                f"Diminishing returns: 1–10 full, 11–25 half, 26+ tiny\n"
                f"Partner bonus window: {int(s['partner_window_sec'])//60} min\n"
                f"Weekly cap: {int(s['weekly_point_cap_milli'])/1000:.1f} points → max {s['weekly_xp_cap']} XP\n"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @rpxp.command(name="flag")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_flag(self, ctx: commands.Context):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        cid = ctx.channel.id
        if cid not in g["channels"]:
            g["channels"].append(cid)
            self._save()
        await ctx.send(f"✅ RP tracking enabled here: <#{cid}>")

    @rpxp.command(name="unflag")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_unflag(self, ctx: commands.Context):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        cid = ctx.channel.id
        g["channels"] = [x for x in g["channels"] if int(x) != int(cid)]
        self._save()
        await ctx.send(f"🧹 RP tracking disabled here: <#{cid}>")

    @rpxp.command(name="channels")
    async def rpxp_channels(self, ctx: commands.Context):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        chans = [f"<#{int(x)}>" for x in g.get("channels", [])]
        await ctx.send("📌 RP-tracked channels:\n" + ("\n".join(chans) if chans else "(none)"))

    @rpxp.command(name="clear")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_clear(self, ctx: commands.Context):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        self._archive_week(g, g.get("week_id", ""), reason="clear", actor_id=ctx.author.id)
        g["users"] = {}
        g["chan_state"] = {}
        g["msg_state"] = {}
        self._save()
        await ctx.send(f"🧹 Archived + cleared RP tracking for week **{g['week_id']}**. (See: `!rpxp prev`)")

    @rpxp.command(name="top")
    async def rpxp_top(self, ctx: commands.Context, n: int = 10):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        n = max(1, min(25, int(n)))

        rows = []
        for uid, u in g["users"].items():
            rows.append((int(u.get("points_milli", 0)), int(uid), int(u.get("posts_scored", 0))))
        rows.sort(reverse=True)

        if not rows:
            await ctx.send("No RP points recorded yet this week.")
            return

        s = g["settings"]
        xp_per_point = int(s["xp_per_point"])
        weekly_xp_cap = int(s["weekly_xp_cap"])

        lines = []
        for i, (pm, uid, posts) in enumerate(rows[:n], start=1):
            pts = pm / 1000.0
            xp = int((pm * xp_per_point + 500) // 1000)
            xp = min(xp, weekly_xp_cap)
            lines.append(f"**{i}.** <@{uid}> — {pts:.2f} pts — ~{xp} XP — {posts} posts")

        embed = nextcord.Embed(
            title=f"RP Points Leaderboard — {g['week_id']}",
            description="\n".join(lines),
            color=nextcord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    @rpxp.command(name="list")
    async def rpxp_list(self, ctx: commands.Context):
        """Show everyone with RP points this week."""
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)

        rows = []
        for uid, u in g["users"].items():
            pm = int(u.get("points_milli", 0))
            if pm <= 0:
                continue
            rows.append((pm, int(uid), int(u.get("posts_scored", 0))))
        rows.sort(reverse=True)

        if not rows:
            await ctx.send("No RP points recorded yet this week.")
            return

        s = g["settings"]
        xp_per_point = int(s["xp_per_point"])
        weekly_xp_cap = int(s["weekly_xp_cap"])

        lines = []
        for i, (pm, uid, posts) in enumerate(rows, start=1):
            pts = pm / 1000.0
            xp = int((pm * xp_per_point + 500) // 1000)
            xp = min(xp, weekly_xp_cap)
            lines.append(f"**{i}.** <@{uid}> — {pts:.2f} pts — ~{xp} XP — {posts} posts")

        per_page = 20
        total_pages = (len(lines) + per_page - 1) // per_page

        for page in range(total_pages):
            page_lines = lines[page * per_page:(page + 1) * per_page]
            embed = nextcord.Embed(
                title=f"RPXP List — {g['week_id']}",
                description="\n".join(page_lines),
                color=nextcord.Color.blurple(),
            )
            embed.set_footer(text=f"Page {page + 1}/{total_pages} • {len(lines)} total users")
            await ctx.send(embed=embed)
            
    @rpxp.command(name="payout")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_payout(self, ctx: commands.Context):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)

        if not g["users"]:
            await ctx.send("No RP points to finalize this week.")
            return

        s = g["settings"]
        xp_per_point = int(s["xp_per_point"])
        weekly_xp_cap = int(s["weekly_xp_cap"])

        rows = []
        for uid_str, u in list(g["users"].items()):
            try:
                uid = int(uid_str)
            except Exception:
                continue

            pm = int(u.get("points_milli", 0))
            if pm <= 0:
                continue

            pts = pm / 1000.0
            posts = int(u.get("posts_scored", 0))

            xp = int((pm * xp_per_point + 500) // 1000)
            xp = min(xp, weekly_xp_cap)
            if xp <= 0:
                continue

            rows.append((xp, pm, uid, pts, posts))

        if not rows:
            await ctx.send("No RP points eligible for XP this week.")
            return

        rows.sort(reverse=True, key=lambda r: (r[0], r[1]))
        week_id = str(g.get("week_id", "?"))

        self._archive_week(g, week_id, reason="payout_report", actor_id=ctx.author.id)

        g["users"] = {}
        g["chan_state"] = {}
        g["msg_state"] = {}
        self._save()

        lines = []
        total_xp = 0
        total_pts = 0.0
        for i, (xp, _pm, uid, pts, posts) in enumerate(rows[:40], start=1):
            total_xp += int(xp)
            total_pts += float(pts)
            lines.append(f"**{i}.** <@{uid}> — {pts:.2f} pts → **{xp} XP** — {posts} posts")

        embed = nextcord.Embed(
            title=f"RPXP Finalized — {week_id}",
            description="\n".join(lines),
            color=nextcord.Color.green(),
        )
        embed.add_field(name="Totals", value=f"{total_pts:.2f} pts → {total_xp} XP (sum of per-user caps)", inline=False)

        footer = "This does NOT auto-award XP. Players may apply this XP to any character they choose. Archived: !rpxp prev / !rpxp history"
        if len(rows) > 40:
            footer += f" • +{len(rows)-40} more not shown"
        embed.set_footer(text=footer)

        await ctx.send(embed=embed)

    @rpxp.command(name="staffrole")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_staffrole(
        self,
        ctx: commands.Context,
        action: str = None,
        role: nextcord.Role = None,
    ):
        """Manage staff roles for RPXP approval.

        Show current roles: `!rpxp staffrole`
        Add a role: `!rpxp staffrole add @GM`
        Remove a role: `!rpxp staffrole remove @GM`
        """
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        s = g["settings"]
        role_ids = set(int(x) for x in s.get("staff_role_ids", []) if str(x).isdigit())

        if action is None:
            current = ", ".join(f"<@&{rid}>" for rid in sorted(role_ids)) or "(none)"
            await ctx.send(
                "🛡️ **RPXP staff roles**: " + current +
                "\nUse: `!rpxp staffrole add @Role` / `!rpxp staffrole remove @Role`"
            )
            return

        act = str(action).strip().lower()

        if act not in {"add", "remove", "del", "delete"}:
            await ctx.send("❌ Action must be `add` or `remove`.")
            return

        if role is None:
            await ctx.send(
                f"❌ Missing role for `{act}`.\n"
                "Use: `!rpxp staffrole add @Role` or `!rpxp staffrole remove @Role`"
            )
            return

        if act == "add":
            role_ids.add(role.id)
        else:
            role_ids.discard(role.id)

        s["staff_role_ids"] = sorted(role_ids)
        self._save()
        await ctx.send(
            "✅ Staff roles now: " +
            (", ".join(f"<@&{rid}>" for rid in s["staff_role_ids"]) or "(none)")
        )

    @rpxp.group(name="staffemoji", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def rpxp_staffemoji(self, ctx: commands.Context):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        emojis = g["settings"].get("staff_emojis") or []
        await ctx.send("⭐ **Staff approval emojis**: " + (" ".join(emojis) if emojis else "(none)") +
                       "\nUse: `!rpxp staffemoji add ⭐` / `!rpxp staffemoji remove ⭐`")

    @rpxp_staffemoji.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_staffemoji_add(self, ctx: commands.Context, emoji: str):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        s = g["settings"]
        lst = list(s.get("staff_emojis") or [])
        if emoji not in lst:
            lst.append(emoji)
            s["staff_emojis"] = lst
            self._save()
        await ctx.send("✅ Staff emojis: " + " ".join(s["staff_emojis"]))

    @rpxp_staffemoji.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_staffemoji_remove(self, ctx: commands.Context, emoji: str):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        s = g["settings"]
        s["staff_emojis"] = [e for e in (s.get("staff_emojis") or []) if e != emoji]
        self._save()
        await ctx.send("✅ Staff emojis: " + (" ".join(s["staff_emojis"]) if s["staff_emojis"] else "(none)"))

    @rpxp.group(name="prefixes", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def rpxp_prefixes(self, ctx: commands.Context):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        prefs = g["settings"].get("ignore_prefixes") or []
        await ctx.send("🚫 **Ignored prefixes**: " + (", ".join(prefs) if prefs else "(none)") +
                       "\nUse: `!rpxp prefixes add !` / `!rpxp prefixes remove !` / `!rpxp prefixes set !,/`")

    @rpxp_prefixes.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_prefixes_add(self, ctx: commands.Context, prefix: str):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        s = g["settings"]
        lst = list(s.get("ignore_prefixes") or [])
        p = (prefix or "")
        if p and p not in lst:
            lst.append(p)
            s["ignore_prefixes"] = lst
            self._save()
        await ctx.send("✅ Ignored prefixes: " + (", ".join(s["ignore_prefixes"]) if s["ignore_prefixes"] else "(none)"))

    @rpxp_prefixes.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_prefixes_remove(self, ctx: commands.Context, prefix: str):
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        s = g["settings"]
        p = (prefix or "")
        s["ignore_prefixes"] = [x for x in (s.get("ignore_prefixes") or []) if x != p]
        self._save()
        await ctx.send("✅ Ignored prefixes: " + (", ".join(s["ignore_prefixes"]) if s["ignore_prefixes"] else "(none)"))


    def _format_rpxp_set_keys(self, settings: dict) -> str:
        descriptions = {
            "cooldown_sec": "seconds between scored posts for the same user",
            "history_weeks_keep": "how many archived weeks to keep",
            "ignore_prefixes": "message prefixes ignored by RPXP scoring",
            "length_bonus_enabled": "whether longer posts get bonus RP points",
            "length_bonus_start_chars": "minimum chars before length bonus starts",
            "length_bonus_chars_per_step": "extra chars needed per bonus step",
            "length_bonus_milli_per_step": "bonus milli-points gained per length step",
            "length_bonus_cap_milli": "max milli-points a post can gain from length bonus",
            "min_chars": "minimum characters for a post to qualify",
            "min_words": "minimum words for a post to qualify",
            "partner_bonus_milli": "bonus milli-points for active back-and-forth scenes",
            "partner_window_sec": "seconds allowed for partner bonus to count",
            "post_1_10_milli": "milli-points awarded for posts 1 through 10",
            "post_11_25_milli": "milli-points awarded for posts 11 through 25",
            "post_26_plus_milli": "milli-points awarded for posts 26 and later",
            "staff_bonus_enabled": "whether staff reactions add a bonus award",
            "staff_bonus_milli": "bonus milli-points from valid staff approval",
            "staff_emojis": "reaction emojis that count as staff approval",
            "staff_required": "whether posts require staff approval before scoring",
            "staff_role_ids": "roles allowed to approve posts for staff scoring",
            "weekly_point_cap_milli": "max RP milli-points a user can earn per week",
            "weekly_xp_cap": "max XP a user can receive from RPXP in a week",
            "xp_per_point": "XP awarded per 1.0 RP point",
        }

        lines = ["**Valid RPXP set keys:**"]
        for k in sorted(settings.keys()):
            if isinstance(settings[k], list):
                typ = "list setting"
            elif isinstance(settings[k], bool):
                typ = "boolean"
            else:
                typ = "integer"

            desc = descriptions.get(k, "no description yet")
            lines.append(f"• `{k}` — {typ} — {desc}")

        lines.append("")
        lines.append("List settings are managed with:")
        lines.append("• `!rpxp prefixes ...`")
        lines.append("• `!rpxp staffemoji ...`")
        lines.append("• `!rpxp staffrole add/remove ...`")
        return "\n".join(lines)

    @rpxp.command(name="set")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_set(self, ctx: commands.Context, key: str = None, *, value: str = None):
        """Change RPXP settings.

        Use `!rpxp set list` to see valid keys.
        Example: `!rpxp set weekly_xp_cap 1500`
        """
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        s = g["settings"]

        if key is None or str(key).strip().lower() in {"list", "keys", "help"}:
            await ctx.send(
                self._format_rpxp_set_keys(s) +
                "\n\nExample: `!rpxp set weekly_xp_cap 1500`"
            )
            return

        k = str(key).strip()

        if k not in s:
            await ctx.send(
                f"❌ Unknown key `{k}`.\n\n" +
                self._format_rpxp_set_keys(s)
            )
            return

        if value is None:
            await ctx.send(
                f"❌ Missing value for `{k}`.\n\n" +
                self._format_rpxp_set_keys(s)
            )
            return

        if isinstance(s[k], list):
            await ctx.send(
                f"❌ `{k}` is a list setting.\n\n" +
                self._format_rpxp_set_keys(s)
            )
            return

        vraw = str(value).strip().lower()

        if isinstance(s[k], bool):
            if vraw in {"true", "1", "yes", "on"}:
                s[k] = True
            elif vraw in {"false", "0", "no", "off"}:
                s[k] = False
            else:
                await ctx.send("❌ Boolean value must be true/false.")
                return
        else:
            try:
                s[k] = int(value)
            except Exception:
                await ctx.send("❌ Value must be an integer.")
                return

        self._save()
        await ctx.send(f"✅ Set `{k}` = `{s[k]}`")

    @rpxp.command(name="history")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_history(self, ctx: commands.Context):
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)

        hist = g.get("history", {}) or {}
        if not hist:
            await ctx.send("No archived RPXP weeks yet.")
            return

        items = []
        for k, e in hist.items():
            ts = int((e or {}).get("archived_ts", 0) or 0)
            items.append((ts, k, e or {}))
        items.sort(reverse=True)

        lines = []
        for ts, k, e in items[:20]:
            paid_ts = int(e.get("paid_ts", 0) or 0)
            reason = (e.get("reason") or "").strip() or "archive"
            badge = "✅ paid" if paid_ts else "📦 archived"
            when = f"<t:{ts}:d>" if ts else "?"
            lines.append(f"• **{k}** — {badge} — {reason} — {when}")

        embed = nextcord.Embed(
            title="RPXP Archive History",
            description="\n".join(lines),
            color=nextcord.Color.blurple(),
        )
        embed.set_footer(text="View: !rpxp prev  |  !rpxp week 2026-W07  |  Configure notices: !rpxp notify here / role @GM")
        await ctx.send(embed=embed)

    @rpxp.command(name="prev")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_prev(self, ctx: commands.Context):
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)

        hist = g.get("history", {}) or {}
        if not hist:
            await ctx.send("No archived RPXP weeks yet.")
            return

        best_key = max(hist.keys(), key=lambda k: int((hist.get(k) or {}).get("archived_ts", 0) or 0))
        entry = hist[best_key]
        lines, total_xp, total_pts = self._format_week_lines(entry, limit=40)

        ts = int(entry.get("archived_ts", 0) or 0)
        embed = nextcord.Embed(
            title=f"RPXP Archive — {best_key}",
            description="\n".join(lines) if lines else "(no RP points recorded)",
            color=nextcord.Color.blurple(),
        )
        embed.add_field(name="Totals (estimate)", value=f"{total_pts:.2f} pts → ~{total_xp} XP", inline=False)
        if ts:
            embed.set_footer(text=f"Archived <t:{ts}:f> • Use !rpxp week <id> for older weeks")
        await ctx.send(embed=embed)

    @rpxp.command(name="week")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_week(self, ctx: commands.Context, week_id: str):
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)

        wk = (week_id or "").strip().upper()
        hist = g.get("history", {}) or {}
        candidates = [k for k in hist.keys() if k == wk or k.startswith(wk + "#")]
        entry = hist.get(wk) if wk in hist else (hist.get(sorted(candidates)[-1]) if candidates else None)
        if not entry:
            avail = ", ".join(sorted(hist.keys(), reverse=True)[:10]) or "(none)"
            await ctx.send(f"❌ Archived week `{wk}` not found. Available: {avail}")
            return

        lines, total_xp, total_pts = self._format_week_lines(entry, limit=40)
        ts = int(entry.get("archived_ts", 0) or 0)
        paid_ts = int(entry.get("paid_ts", 0) or 0)

        embed = nextcord.Embed(
            title=f"RPXP Archive — {wk}",
            description="\n".join(lines) if lines else "(no RP points recorded)",
            color=nextcord.Color.blurple(),
        )
        embed.add_field(name="Totals (estimate)", value=f"{total_pts:.2f} pts → ~{total_xp} XP", inline=False)

        meta = []
        if ts:
            meta.append(f"archived <t:{ts}:f>")
        if paid_ts:
            meta.append(f"paid <t:{paid_ts}:f>")
        if meta:
            embed.set_footer(text=" • ".join(meta))

        await ctx.send(embed=embed)

    @rpxp.group(name="notify", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def rpxp_notify(self, ctx: commands.Context):
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)

        n = g.get("notify", {}) or {}
        ch_id = int(n.get("channel_id") or 0)
        role_id = int(n.get("role_id") or 0)

        ch = f"<#{ch_id}>" if ch_id else "(not set)"
        role = f"<@&{role_id}>" if role_id else "(none)"

        await ctx.send(
            "🔔 **RPXP rollover notifications**\n"
            f"Channel: {ch}\n"
            f"Ping role: {role}\n\n"
            "Commands:\n"
            "• `!rpxp notify here` — set this channel\n"
            "• `!rpxp notify off` — disable\n"
            "• `!rpxp notify role @Role` — set ping role\n"
            "• `!rpxp notify norole` — clear ping role\n"
            "• `!rpxp notify test` — send a test notice"
        )

    @rpxp_notify.command(name="here")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_notify_here(self, ctx: commands.Context):
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        g.setdefault("notify", {})
        g["notify"]["channel_id"] = int(ctx.channel.id)
        self._save()
        await ctx.send(f"✅ RPXP rollover notifications will be sent in <#{ctx.channel.id}>.")

    @rpxp_notify.command(name="off")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_notify_off(self, ctx: commands.Context):
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        g.setdefault("notify", {})
        g["notify"]["channel_id"] = 0
        self._save()
        await ctx.send("🧹 RPXP rollover notifications disabled (channel cleared).")

    @rpxp_notify.command(name="role")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_notify_role(self, ctx: commands.Context, role: nextcord.Role):
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        g.setdefault("notify", {})
        g["notify"]["role_id"] = int(role.id)
        self._save()
        await ctx.send(f"✅ RPXP rollover notices will ping {role.mention}.")

    @rpxp_notify.command(name="norole")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_notify_norole(self, ctx: commands.Context):
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        g.setdefault("notify", {})
        g["notify"]["role_id"] = 0
        self._save()
        await ctx.send("🧹 RPXP rollover ping role cleared.")

    @rpxp_notify.command(name="test")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_notify_test(self, ctx: commands.Context):
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)

        entry = {
            "week_id": g.get("week_id", "?"),
            "archived_ts": _now_ts(),
            "reason": "test",
            "users": copy.deepcopy(g.get("users") or {}),
            "settings_snapshot": copy.deepcopy(g.get("settings") or {}),
        }
        lines, total_xp, total_pts = self._format_week_lines(entry, limit=10)
        embed = nextcord.Embed(
            title=f"RPXP Notice Test — {g.get('week_id', '?')}",
            description="\n".join(lines) if lines else "(no points recorded this week yet)",
            color=nextcord.Color.blurple(),
        )
        embed.add_field(name="Totals (estimate)", value=f"{total_pts:.2f} pts → ~{total_xp} XP", inline=False)
        await ctx.send(embed=embed)


def setup(bot: commands.Bot):
    bot.add_cog(RPXPCog(bot))
