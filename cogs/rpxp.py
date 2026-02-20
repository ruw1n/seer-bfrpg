import json, os, re, time, datetime
import copy
from pathlib import Path
from zoneinfo import ZoneInfo

import nextcord
from nextcord.ext import commands
from utils.ini import read_cfg, get_compat
from utils.players import get_active

DATA_PATH = Path("data/rpxp.json")

def _iter_coe_files() -> list[Path]:
    paths: list[Path] = []
    # Most of your code assumes .coe live in the bot working dir
    paths += list(Path(".").glob("*.coe"))

    # Optional common folder; harmless if it doesn't exist
    if Path("chars").exists():
        paths += list(Path("chars").glob("*.coe"))

    # De-dupe
    seen = set()
    out = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out



def _atomic_write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _now_ts() -> int:
    return int(time.time())


def _week_id_chicago() -> str:
    tz = ZoneInfo("America/Chicago")
    d = datetime.datetime.now(tz).date()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _emoji_to_str(emoji) -> str:
    # payload.emoji can be PartialEmoji; message reactions can be str
    try:
        if isinstance(emoji, str):
            return emoji
        # PartialEmoji
        if getattr(emoji, "id", None):
            # custom emoji; use name:id form
            return f"{emoji.name}:{emoji.id}"
        return str(getattr(emoji, "name", emoji))
    except Exception:
        return str(emoji)


class RPXPCog(commands.Cog):
    """
    Weekly RP XP conversion:
      - Track "RP points" for messages in flagged channels using anti-spam rules.
      - Convert points -> XP via !rpxp payout (admin).
      - Reset via !rpxp clear (admin) or auto-rollover on ISO week change (America/Chicago).
    """

    def __init__(self, bot):
        self.bot = bot
        self.data = self._load()

    # ---------- storage ----------
    def _load(self) -> dict:
        if DATA_PATH.exists():
            try:
                return json.loads(DATA_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"guilds": {}}

    def _save(self):
        _atomic_write_json(DATA_PATH, self.data)

    def _g(self, guild_id: int) -> dict:
        g = self.data["guilds"].setdefault(str(guild_id), {})
        g.setdefault("channels", [])  # flagged channels (ints)
        g.setdefault("week_id", _week_id_chicago())
        g.setdefault("users", {})     # user_id -> stats
        g.setdefault("chan_state", {})  # channel_id -> {last_author, last_ts}
        g.setdefault("msg_state", {})   # message_id -> {user_id, staff_bonus_applied, ts}

        g.setdefault("history", {})      # week_id -> archived snapshot
        g.setdefault("notify", {"channel_id": 0, "role_id": 0})  # rollover notices
        g.setdefault("pending_rollover", None)

        # Default settings (tweak with !rpxp set ...)
        g.setdefault("settings", {
            "min_chars": 80,
            "min_words": 15,

            "cooldown_sec": 600,           # only 1 eligible post / 10 min
            "partner_window_sec": 600,     # 10 min window
            "partner_bonus_milli": 250,    # +0.25 points

            # diminishing returns multipliers (in milli-points per post)
            # base post is 1.0 => 1000 milli
            "post_1_10_milli": 1000,       # 1.0
            "post_11_25_milli": 500,       # 0.5
            "post_26_plus_milli": 100,     # 0.1

            "weekly_point_cap_milli": 40000,  # 40.0 points max/week
            "xp_per_point": 25,               # 1 point => 25 XP
            "weekly_xp_cap": 1000,            # safety cap on payout

            "history_weeks_keep": 12,          # how many archived weeks to keep

            # staff reaction bonus (optional)
            "staff_bonus_enabled": False,
            "staff_required": False,          # if True: ONLY count posts that get staff reaction
            "staff_bonus_milli": 1000,        # +1.0 points when approved
            "staff_emojis": ["🔥", "⭐"],
            "staff_role_ids": [],             # roles allowed to approve
        })

        # auto-week rollover (archive outgoing week before clearing)
        cur_week = _week_id_chicago()
        if g.get("week_id") != cur_week:
            old_week = str(g.get("week_id") or "")
            try:
                self._archive_week(g, old_week, reason="rollover")
            except Exception:
                pass

            g["week_id"] = cur_week
            g["users"] = {}
            g["chan_state"] = {}
            g["msg_state"] = {}

            g["pending_rollover"] = {"old_week": old_week, "ts": _now_ts(), "notified": False}
            self._save()

        return g

    # ---------- archiving/notifications ----------
    def _prune_history(self, g: dict):
        hist = g.get("history", {})
        try:
            keep = int(g.get("settings", {}).get("history_weeks_keep", 12))
        except Exception:
            keep = 12
        if keep <= 0 or not isinstance(hist, dict):
            return
        if len(hist) <= keep:
            return
        # week_id is YYYY-W##, lexicographic sort works
        for wk in sorted(hist.keys())[:-keep]:
            hist.pop(wk, None)

    def _archive_week(
        self,
        g: dict,
        week_id: str,
        *,
        reason: str = "",
        actor_id: int | None = None,
        payout: dict | None = None,
    ):
        week_id = str(week_id or "").strip()
        if not week_id:
            return

        users = g.get("users") or {}
        msg_state = g.get("msg_state") or {}
        if not users and not msg_state:
            return  # nothing to archive

        hist = g.setdefault("history", {})
        entry = {
            "week_id": week_id,
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

        hist[week_id] = entry
        self._prune_history(g)

    def _format_week_lines(self, guild: nextcord.Guild, entry: dict, *, limit: int = 25) -> tuple[list[str], int, float]:
        """Return (lines, total_xp_est, total_points)."""
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

            active = get_active(uid) or "—"
            rows.append((pm, uid, pts, xp, posts, active))

        rows.sort(reverse=True, key=lambda r: r[0])

        lines: list[str] = []
        for i, (_, uid, pts, xp, posts, active) in enumerate(rows[: max(1, limit)], start=1):
            # keep compact; active char is informational only (may have changed since the week ended)
            lines.append(f"**{i}.** <@{uid}> — {pts:.2f} pts (~{xp} XP) — {posts} posts — active: {active}")

        return lines, int(total_xp), float(total_pm / 1000.0)

    async def _maybe_send_rollover_notice(self, guild: nextcord.Guild, g: dict):
        pr = g.get("pending_rollover")
        if not pr or pr.get("notified"):
            return

        old_week = str(pr.get("old_week") or "").strip()
        if not old_week:
            pr["notified"] = True
            self._save()
            return

        entry = (g.get("history") or {}).get(old_week)
        if not entry:
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

        # If there's nowhere safe to post, just mark as notified (data is still archived).
        if channel is None:
            pr["notified"] = True
            self._save()
            return

        lines, total_xp, total_pts = self._format_week_lines(guild, entry, limit=40)

        embed = nextcord.Embed(
            title=f"RPXP Week Archived — {old_week}",
            description=("\n".join(lines) if lines else "(no RP points recorded last week)"),
            color=nextcord.Color.blurple(),
        )
        embed.add_field(name="Totals (estimate)", value=f"{total_pts:.2f} pts → ~{total_xp} XP", inline=False)
        embed.set_footer(text="Use !rpxp prev (or !rpxp week <id>) to view archived weeks. Configure: !rpxp notify here / role @GM")

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
    def _is_flagged_channel(self, g: dict, channel: nextcord.abc.GuildChannel | nextcord.Thread) -> tuple[bool, int]:
        """
        Returns (is_flagged, effective_channel_id_used_for_state).
        If message is in a Thread, we treat the thread itself as the unit for partner bonus,
        but allow parent channel to be flagged.
        """
        flagged = set(int(x) for x in g.get("channels", []))

        # Thread: allow parent to be flagged, but state uses thread id (so scenes don’t bleed)
        if isinstance(channel, nextcord.Thread):
            if channel.id in flagged:
                return True, channel.id
            if channel.parent_id and channel.parent_id in flagged:
                return True, channel.id
            return False, channel.id

        return (channel.id in flagged), channel.id

    def _eligible_text_metrics(self, content: str) -> tuple[int, int, bool]:
        # strip urls, mentions-ish, and collapse whitespace
        s = re.sub(r"http\S+", "", content or "")
        s = re.sub(r"<@!?(\d+)>", "", s)
        s = re.sub(r"<#(\d+)>", "", s)
        s = re.sub(r"\s+", " ", s).strip()

        # require at least one letter to avoid pure emoji spam
        has_alpha = re.search(r"[A-Za-z]", s) is not None

        # count words roughly (letters + apostrophes)
        words = re.findall(r"[A-Za-z']+", s)
        return (len(s), len(words), has_alpha)

    def _post_award_milli(self, post_index_1_based: int, settings: dict) -> int:
        if post_index_1_based <= 10:
            return int(settings["post_1_10_milli"])
        if post_index_1_based <= 25:
            return int(settings["post_11_25_milli"])
        return int(settings["post_26_plus_milli"])

    def _award_points(
        self,
        g: dict,
        *,
        user_id: int,
        effective_chan_id: int,
        msg_id: int,
        created_ts: int,
        is_staff_approval_award: bool = False,
    ) -> tuple[bool, str, int]:
        """
        Adds points (milli) under the rules and caps.
        Returns (awarded, reason, milli_added).
        """

        settings = g["settings"]
        users = g["users"]
        u = users.setdefault(str(user_id), {"points_milli": 0, "posts_scored": 0, "last_scored_ts": 0})

        weekly_cap = int(settings["weekly_point_cap_milli"])
        if int(u["points_milli"]) >= weekly_cap:
            return (False, "weekly cap reached", 0)

        # Cooldown applies to normal message scoring, not staff approval bonus
        if not is_staff_approval_award:
            cooldown = int(settings["cooldown_sec"])
            last_ts = int(u.get("last_scored_ts", 0))
            if cooldown > 0 and (created_ts - last_ts) < cooldown:
                return (False, "cooldown", 0)

        # Determine base award for this post index
        post_index = int(u.get("posts_scored", 0)) + (0 if is_staff_approval_award else 1)
        if not is_staff_approval_award:
            base_milli = self._post_award_milli(post_index, settings)
        else:
            base_milli = int(settings["staff_bonus_milli"])

        add_milli = base_milli

        # Partner bonus: only on normal message scoring
        if not is_staff_approval_award:
            ps = g["chan_state"].setdefault(str(effective_chan_id), {"last_author": 0, "last_ts": 0})
            last_author = int(ps.get("last_author", 0))
            last_ts = int(ps.get("last_ts", 0))
            window = int(settings["partner_window_sec"])

            if window > 0 and last_author and last_author != user_id and (created_ts - last_ts) <= window:
                add_milli += int(settings["partner_bonus_milli"])

            # update channel state after scoring attempt
            ps["last_author"] = int(user_id)
            ps["last_ts"] = int(created_ts)

        # Apply cap
        before = int(u["points_milli"])
        after = min(weekly_cap, before + add_milli)
        added = after - before
        if added <= 0:
            return (False, "weekly cap reached", 0)

        u["points_milli"] = after
        if not is_staff_approval_award:
            u["posts_scored"] = int(u.get("posts_scored", 0)) + 1
            u["last_scored_ts"] = int(created_ts)

            # Track message so staff reactions can optionally grant bonus (or gate)
            g["msg_state"][str(msg_id)] = {
                "user_id": int(user_id),
                "staff_bonus_applied": False,
                "ts": int(created_ts),
            }

        return (True, "ok", added)

    # ---------- listeners ----------
    @commands.Cog.listener()
    async def on_message(self, message: nextcord.Message):
        if not message.guild or message.author.bot:
            return

        g = self._g(message.guild.id)
        await self._maybe_send_rollover_notice(message.guild, g)
        settings = g["settings"]

        # ignore command-like messages
        if message.content and message.content.strip().startswith("!"):
            return

        is_flagged, effective_chan_id = self._is_flagged_channel(g, message.channel)
        if not is_flagged:
            return

        # If staff_required is on, we do NOT award on message; we wait for approval reaction.
        if bool(settings.get("staff_required", False)):
            # Still record msg_state so approval can process later
            created_ts = int(message.created_at.timestamp())
            g["msg_state"][str(message.id)] = {
                "user_id": int(message.author.id),
                "staff_bonus_applied": False,
                "ts": int(created_ts),
                "pending_only": True,
            }
            self._save()
            return

        # content gate
        min_chars = int(settings["min_chars"])
        min_words = int(settings["min_words"])

        chars, words, has_alpha = self._eligible_text_metrics(message.content or "")
        if not has_alpha:
            return
        if not (chars >= min_chars or words >= min_words):
            return

        created_ts = int(message.created_at.timestamp())

        awarded, why, added = self._award_points(
            g,
            user_id=message.author.id,
            effective_chan_id=effective_chan_id,
            msg_id=message.id,
            created_ts=created_ts,
            is_staff_approval_award=False,
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

        # must be in a flagged channel
        flagged = set(int(x) for x in g.get("channels", []))
        if payload.channel_id not in flagged:
            # could still be a thread under a flagged parent; try to fetch channel
            try:
                ch = guild.get_channel(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
                if isinstance(ch, nextcord.Thread) and ch.parent_id and ch.parent_id in flagged:
                    pass
                else:
                    return
            except Exception:
                return

        # emoji must match
        emoji_str = _emoji_to_str(payload.emoji)
        allowed = set(str(x) for x in settings.get("staff_emojis", []))
        if emoji_str not in allowed:
            return

        # reactor must have staff role (if configured)
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

        # check msg_state
        ms = g["msg_state"].get(str(payload.message_id))
        if not ms:
            return
        if ms.get("staff_bonus_applied"):
            return

        # apply either:
        # - if staff_required: treat this approval as "the scoring event" (post rules apply except cooldown)
        # - else: grant bonus points (no cooldown)
        user_id = int(ms.get("user_id", 0))
        if not user_id:
            return

        created_ts = int(ms.get("ts", _now_ts()))
        # use channel id as state bucket for staff events (fine)
        effective_chan_id = int(payload.channel_id)

        if bool(settings.get("staff_required", False)):
            # fetch message to enforce content gate + diminishing/cooldown
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

            awarded, why, added = self._award_points(
                g,
                user_id=user_id,
                effective_chan_id=effective_chan_id,
                msg_id=payload.message_id,
                created_ts=created_ts,
                is_staff_approval_award=False,
            )
            if not awarded:
                return

            # and optionally also apply staff bonus on top (if enabled)
            if bool(settings.get("staff_bonus_enabled", False)):
                awarded2, why2, added2 = self._award_points(
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

        # staff bonus only
        awarded, why, added = self._award_points(
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
        """Show your current week RP points + estimated XP."""
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        u = g["users"].get(str(ctx.author.id), {"points_milli": 0, "posts_scored": 0})

        points = int(u.get("points_milli", 0)) / 1000.0
        posts = int(u.get("posts_scored", 0))

        s = g["settings"]
        xp_per_point = int(s["xp_per_point"])
        weekly_xp_cap = int(s["weekly_xp_cap"])

        est_xp = int((int(u.get("points_milli", 0)) * xp_per_point + 500) // 1000)
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
        """Flag THIS channel as RP-tracked."""
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
        """Remove RP tracking from THIS channel."""
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        cid = ctx.channel.id
        g["channels"] = [x for x in g["channels"] if int(x) != int(cid)]
        self._save()
        await ctx.send(f"🧹 RP tracking disabled here: <#{cid}>")

    @rpxp.command(name="channels")
    async def rpxp_channels(self, ctx: commands.Context):
        """List flagged channels."""
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        chans = [f"<#{int(x)}>" for x in g.get("channels", [])]
        await ctx.send("📌 RP-tracked channels:\n" + ("\n".join(chans) if chans else "(none)"))

    @rpxp.command(name="clear")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_clear(self, ctx: commands.Context):
        """Clear the current week's RP points (does NOT change flagged channels)."""
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
        """Show leaderboard for this week."""
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

    @rpxp.command(name="set")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_set(self, ctx: commands.Context, key: str, value: str):
        """
        Set a config value quickly.
        Examples:
          !rpxp set min_chars 80
          !rpxp set cooldown_sec 600
          !rpxp set xp_per_point 25
          !rpxp set weekly_xp_cap 1000
          !rpxp set staff_bonus_enabled true
          !rpxp set staff_required false
        """
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        s = g["settings"]
        k = (key or "").strip()

        if k not in s:
            await ctx.send(f"❌ Unknown key `{k}`.\nValid keys: " + ", ".join(sorted(s.keys())))
            return

        vraw = (value or "").strip().lower()
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

    @rpxp.command(name="staffrole")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_staffrole(self, ctx: commands.Context, action: str, role: nextcord.Role):
        """
        Manage staff roles allowed to approve RP posts.
        Usage:
          !rpxp staffrole add @Storyteller
          !rpxp staffrole remove @Storyteller
        """
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        s = g["settings"]
        role_ids = set(int(x) for x in s.get("staff_role_ids", []) if str(x).isdigit())

        act = (action or "").strip().lower()
        if act == "add":
            role_ids.add(role.id)
        elif act in {"remove", "del", "delete"}:
            role_ids.discard(role.id)
        else:
            await ctx.send("❌ action must be `add` or `remove`.")
            return

        s["staff_role_ids"] = sorted(role_ids)
        self._save()
        await ctx.send("✅ Staff roles now: " + (", ".join(f"<@&{rid}>" for rid in s["staff_role_ids"]) or "(none)"))

    @rpxp.command(name="payout")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_payout(self, ctx: commands.Context):
        """
        Convert everyone's points into XP on their ACTIVE character, then clear the week.
        """
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        if not g["users"]:
            await ctx.send("No RP points to payout this week.")
            return

        prog = self.bot.get_cog("ProgressionCog")
        if prog is None:
            await ctx.send("❌ ProgressionCog not loaded; can't grant XP.")
            return

        s = g["settings"]
        xp_per_point = int(s["xp_per_point"])
        weekly_xp_cap = int(s["weekly_xp_cap"])

        results = []
        skipped = []

        for uid_str, u in list(g["users"].items()):
            uid = int(uid_str)
            pm = int(u.get("points_milli", 0))
            if pm <= 0:
                continue

            xp = int((pm * xp_per_point + 500) // 1000)
            xp = min(xp, weekly_xp_cap)
            if xp <= 0:
                continue

            res = await prog.grant_xp_to_user_active(
                uid,
                xp,
                reason=f"Weekly RP XP payout ({g['week_id']})",
                apply_racial_bonus=True,
                actor_id=ctx.author.id,
            )
            if res.get("ok"):
                results.append(res)
            else:
                skipped.append((uid, res.get("err", "unknown error")))

        # archive + clear after payout
        payout_summary = {
            "ts": _now_ts(),
            "by": int(ctx.author.id),
            "results": [
                {"user_id": int(r.get("user_id", 0)), "char": r.get("char", ""), "grant": int(r.get("grant", 0)), "bonus": int(r.get("bonus", 0))}
                for r in results
            ],
            "skipped": [{"user_id": int(uid), "err": str(err)} for uid, err in skipped],
        }
        self._archive_week(g, g.get("week_id", ""), reason="payout", actor_id=ctx.author.id, payout=payout_summary)

        g["users"] = {}
        g["chan_state"] = {}
        g["msg_state"] = {}
        self._save()

        # report
        lines = []
        total = 0
        for r in sorted(results, key=lambda x: x.get("grant", 0), reverse=True)[:20]:
            total += int(r.get("grant", 0))
            lines.append(f"<@{r['user_id']}> → **{r['char']}**: +{r['grant']} XP" + (f" (+{r['bonus']} bonus)" if r.get("bonus", 0) else ""))

        embed = nextcord.Embed(
            title=f"Weekly RP XP Payout — {g['week_id']}",
            description=("\n".join(lines) if lines else "(no eligible payouts)"),
            color=nextcord.Color.green(),
        )
        embed.add_field(name="Total XP granted", value=str(total), inline=False)

        if skipped:
            preview = "\n".join([f"<@{uid}> — {err}" for uid, err in skipped[:10]])
            embed.add_field(name="Skipped (no active char / ownership / missing file)", value=preview, inline=False)

        await ctx.send(embed=embed)


    @rpxp.command(name="list")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_list(self, ctx: commands.Context, mode: str = ""):
        """
        List RPXP for every user with points, and which .coe will receive payout.
        mode:
          - default: show active character + points
          - "owned": also show owned character count (and names if short)
        """
        if not ctx.guild:
            return

        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)
        s = g["settings"]

        # Build owner_id -> [character names] from .coe files (for convenience)
        owned: dict[int, list[str]] = {}
        for p in _iter_coe_files():
            try:
                cfg = read_cfg(str(p))
                oid = get_compat(cfg, "info", "owner_id", fallback="")
                if not str(oid).isdigit():
                    continue
                oid_i = int(oid)

                # Prefer explicit name if present; else derive from filename
                nm = get_compat(cfg, "info", "name", fallback="") or ""
                nm = nm.strip()
                if not nm:
                    nm = p.stem.replace("_", " ").strip()

                owned.setdefault(oid_i, []).append(nm)
            except Exception:
                continue

        rows = []
        for uid_str, u in g.get("users", {}).items():
            try:
                uid = int(uid_str)
            except Exception:
                continue
            pm = int(u.get("points_milli", 0))
            if pm <= 0:
                continue

            pts = pm / 1000.0
            xp = int((pm * int(s["xp_per_point"]) + 500) // 1000)
            xp = min(xp, int(s["weekly_xp_cap"]))

            active = get_active(uid) or "—"
            # The file your system typically uses
            fn = f"{active.replace(' ', '_')}.coe" if active != "—" else ""
            exists = bool(fn and Path(fn).exists())

            rows.append((pm, uid, pts, xp, active, exists))

        rows.sort(reverse=True, key=lambda r: r[0])

        if not rows:
            await ctx.send("No RPXP points recorded for anyone this week.")
            return

        mode = (mode or "").strip().lower()
        lines = []
        for i, (_, uid, pts, xp, active, exists) in enumerate(rows[:40], start=1):
            marker = "✅" if exists else "⚠️"
            extra = ""
            if mode == "owned":
                chars = owned.get(uid, [])
                if chars:
                    # keep it short
                    if len(chars) <= 3:
                        extra = f" | owns: {', '.join(chars)}"
                    else:
                        extra = f" | owns: {len(chars)} chars"
            lines.append(f"**{i}.** <@{uid}> → {pts:.2f} pts (~{xp} XP){extra}")

        embed = nextcord.Embed(
            title=f"RPXP List — {g['week_id']}",
            description="\n".join(lines),
            color=nextcord.Color.blurple(),
        )
        if len(rows) > 40:
            embed.set_footer(text=f"+ {len(rows) - 40} more not shown (limit 40).")
        await ctx.send(embed=embed)



    @rpxp.command(name="history")
    @commands.has_permissions(manage_guild=True)
    async def rpxp_history(self, ctx: commands.Context):
        """List archived RPXP weeks (for manual payout / audit)."""
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)

        hist = g.get("history", {}) or {}
        if not hist:
            await ctx.send("No archived RPXP weeks yet.")
            return

        weeks = sorted(hist.keys(), reverse=True)
        lines = []
        for wk in weeks[:20]:
            e = hist.get(wk, {}) or {}
            ts = int(e.get("archived_ts", 0) or 0)
            paid_ts = int(e.get("paid_ts", 0) or 0)
            reason = (e.get("reason") or "").strip() or "archive"
            badge = "✅ paid" if paid_ts else "📦 archived"
            when = f"<t:{ts}:d>" if ts else "?"
            lines.append(f"• **{wk}** — {badge} — {reason} — {when}")

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
        """Show the most recently archived week."""
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)

        hist = g.get("history", {}) or {}
        if not hist:
            await ctx.send("No archived RPXP weeks yet.")
            return

        wk = sorted(hist.keys())[-1]
        entry = hist[wk]
        lines, total_xp, total_pts = self._format_week_lines(ctx.guild, entry, limit=40)

        ts = int(entry.get("archived_ts", 0) or 0)
        embed = nextcord.Embed(
            title=f"RPXP Archive — {wk}",
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
        """Show a specific archived week, e.g. !rpxp week 2026-W07"""
        if not ctx.guild:
            return
        g = self._g(ctx.guild.id)
        await self._maybe_send_rollover_notice(ctx.guild, g)

        wk = (week_id or "").strip()
        hist = g.get("history", {}) or {}
        entry = hist.get(wk)
        if not entry:
            avail = ", ".join(sorted(hist.keys(), reverse=True)[:10]) or "(none)"
            await ctx.send(f"❌ Archived week `{wk}` not found. Available: {avail}")
            return

        lines, total_xp, total_pts = self._format_week_lines(ctx.guild, entry, limit=40)
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
        """Configure rollover notifications."""
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

        # simulate a notice using the current week (if any data exists)
        entry = {
            "week_id": g.get("week_id", "?"),
            "archived_ts": _now_ts(),
            "reason": "test",
            "users": copy.deepcopy(g.get("users") or {}),
            "settings_snapshot": copy.deepcopy(g.get("settings") or {}),
        }
        lines, total_xp, total_pts = self._format_week_lines(ctx.guild, entry, limit=10)
        embed = nextcord.Embed(
            title=f"RPXP Notice Test — {g.get('week_id', '?')}",
            description="\n".join(lines) if lines else "(no points recorded this week yet)",
            color=nextcord.Color.blurple(),
        )
        embed.add_field(name="Totals (estimate)", value=f"{total_pts:.2f} pts → ~{total_xp} XP", inline=False)
        await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(RPXPCog(bot))
