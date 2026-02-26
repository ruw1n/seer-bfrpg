import os, re, json
import datetime
import random
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from nextcord.ext import commands, tasks
from utils.ini import read_cfg, write_cfg, get_compat
from pathlib import Path
import nextcord
from nextcord.ext import commands
from utils.players import (
    list_chars,
    set_active,
    link_existing,
    owns_char,
    get_active,
)

# Guild → welcome channel mapping (fill in with your IDs)
WELCOME_CHANNELS: dict[int, int] = {
    # guild_id: welcome_channel_id
    1438386489443745947: 1438568030610395206
}

BIRTHDAY_CHANNELS: dict[int, int] = {
    # guild_id: channel_id   (optional)
    # 1438386489443745947: 123456789012345678,
}

# Guild → Rules/reaction-role binding
REACTION_RULES = {
    1438386489443745947: {  # your server (guild) ID
        1438640654677966989: {  # first message
            "role_id": 1438672067745550488,  # “Adventurer” role
            "emoji": "✅",
        },
        1438966689621610596: {  # second message
            "role_id": 1438966207423184956,
            "emoji": "👋",
        },
    },
}



PLAYERS_JSON = os.path.join("data", "players.json")

def _players_json_path() -> Path:
    """
    Try hard to find data/players.json no matter where the bot is launched from.
    Order: cwd/data → this file's dir/data → parents' /data → absolute /data.
    """
    here = Path(__file__).resolve()
    candidates = [
        Path("data") / "players.json",
        Path.cwd() / "data" / "players.json",
        here.parent / "data" / "players.json",
        *[(p / "data" / "players.json") for p in here.parents[:3]],
        Path("/data/players.json"),
    ]
    seen = set()
    for p in candidates:
        try:
            rp = p.resolve()
        except Exception:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        if rp.exists():
            return rp
    return (Path.cwd() / "data" / "players.json").resolve()

def _ensure_parent_dir(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    
def _load_players_registry() -> dict:
    try:
        with open(PLAYERS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

def _atomic_write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _norm_token(s: str) -> str:
    """Lowercase and collapse non-alphanumerics for tolerant matching."""
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def _lev(a: str, b: str) -> int:
    """Tiny, fast Levenshtein (edit distance)."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return max(la, lb)
    dp = list(range(lb + 1))
    for i in range(1, la + 1):
        prev = dp[0]
        dp[0] = i
        ca = a[i - 1]
        for j in range(1, lb + 1):
            temp = dp[j]
            cost = 0 if ca == b[j - 1] else 1
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + cost)
            prev = temp
    return dp[lb]


def _resolve_partial_any(candidates: list[str], token: str) -> tuple[str | None, list[str]]:
    """
    Resolve by exact/normalized → prefix → substring → tiny fuzzy (1–2 typos).
    Returns (resolved | None, suggestions_if_ambiguous).
    """
    tok = (token or "").strip()
    if not tok:
        return None, []
    tl = tok.lower()
    nt = _norm_token(tok)

    for n in candidates:
        if n.lower() == tl or _norm_token(n) == nt:
            return n, []

    prefix = [n for n in candidates if n.lower().startswith(tl) or _norm_token(n).startswith(nt)]
    if len(prefix) == 1:
        return prefix[0], []
    if len(prefix) > 1:
        return None, prefix[:8]

    subs = [n for n in candidates if (tl in n.lower()) or (nt and nt in _norm_token(n))]
    if len(subs) == 1:
        return subs[0], []
    if len(subs) > 1:
        return None, subs[:8]

    scored = []
    for n in candidates:
        ln = n.lower()
        nn = _norm_token(n)
        s1 = _lev(tl, ln[: len(tl)]) if ln else 99
        s2 = _lev(nt, nn[: len(nt)]) if nt else 99
        s3 = _lev(tl, ln)
        score = min(s1, s2, s3)
        scored.append((score, n))
    if not scored:
        return None, []
    scored.sort(key=lambda x: (x[0], len(x[1])))
    best = scored[0][0]
    threshold = 1 if len(tl) <= 4 else 2
    if best <= threshold:
        ties = [n for d, n in scored if d == best]
        if len(ties) == 1:
            return ties[0], []
        return None, ties[:8]
    return None, []

def _ct_now():
    now = datetime.datetime.now(datetime.timezone.utc)
    if ZoneInfo:
        return now.astimezone(ZoneInfo("America/Chicago"))
    return now

def _parse_mmdd(s: str) -> str | None:
    raw = (s or "").strip()
    if not raw:
        return None

    # normalize common Unicode dashes to ASCII hyphen
    raw = raw.translate(str.maketrans({
        "–": "-",  # en dash
        "—": "-",  # em dash
        "−": "-",  # minus sign
        "-": "-",  # non-breaking hyphen
        "‒": "-",  # figure dash
        "‐": "-",  # hyphen
    }))

    raw = raw.replace("/", "-")

    try:
        parts = raw.split("-")
        if len(parts) == 2:
            m = int(parts[0]); d = int(parts[1])
            datetime.date(2001, m, d)
            return f"{m:02d}-{d:02d}"
    except Exception:
        pass
    # allow "Feb 26" / "February 26"
    for fmt in ("%b %d", "%B %d"):
        try:
            dt = datetime.datetime.strptime(raw, fmt)
            return f"{dt.month:02d}-{dt.day:02d}"
        except Exception:
            continue
    return None

def _char_file_from_name(name: str) -> str:
    # simple local resolution (matches your zap() candidates idea)
    n = (name or "").strip()
    cands = [
        f"{n}.coe",
        f"{n.replace(' ', '_')}.coe",
        f"{n.lower().replace(' ', '_')}.coe",
    ]
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[1]  # default guess

def _inv_add_stack_cfg(cfg, token: str, qty: int = 1):
    import re
    token = re.sub(r"\s+", "", (token or "").strip())
    if not token:
        return
    if not cfg.has_section("item"):
        cfg.add_section("item")
    storage = (get_compat(cfg, "item", "storage", fallback="") or "").split()
    if token.lower() not in {t.lower() for t in storage}:
        storage.append(token)
        cfg.set("item", "storage", " ".join(storage))
    key = token.lower()
    try:
        cur = int(cfg.get("item", key, fallback="0") or "0")
    except Exception:
        cur = 0
    cfg.set("item", key, str(cur + max(1, int(qty))))
    

class Players(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


    @commands.Cog.listener()
    async def on_member_join(self, member: nextcord.Member):
        """
        Fires whenever someone joins a guild where this bot is present.
        Sends a welcome message to the configured welcome channel.
        """
        # Ignore bots if you don’t want to welcome them
        if member.bot:
            return

        guild = member.guild
        if guild is None:
            return

        # Look up the welcome channel for this guild
        chan_id = WELCOME_CHANNELS.get(guild.id)
        channel = None
        if chan_id:
            channel = guild.get_channel(chan_id)

        # If we didn't configure a channel, try to be smart:
        if channel is None:
            # Try a channel literally named 'welcome' or 'introductions'
            for ch in guild.text_channels:
                if ch.name.lower() in {"welcome", "introductions", "start-here"}:
                    channel = ch
                    break

        if channel is None:
            return  # nowhere to send it

        # Try to resolve the special channels by name so we can use proper mentions
        verify_chan = next((c for c in guild.text_channels if c.name.lower() in {"verify", "verification"}), None)
        rules_chan = next((c for c in guild.text_channels if c.name.lower() == "rules"), None)
        world_guide_chan = next((c for c in guild.text_channels if c.name.lower() == "world-guide"), None)
        bot_guide_chan = next((c for c in guild.text_channels if c.name.lower() == "bot-guide"), None)

        verify_mention = verify_chan.mention if verify_chan else "#verify"
        rules_mention = rules_chan.mention if rules_chan else "#rules"
        world_guide_mention = world_guide_chan.mention if world_guide_chan else "#world-guide"
        bot_guide_mention = bot_guide_chan.mention if bot_guide_chan else "#bot-guide"

        # Public welcome message
        await channel.send(
            f"🪙⚔️ Welcome, {member.mention}!\n"
            f"👉 **First, go to {verify_mention} and verify yourself** to unlock the server.\n"
            f"Then please read {rules_mention} and follow the steps to get started.\n"
            f"If you’d like to play, check {world_guide_mention} and {bot_guide_mention}."
        )



    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: nextcord.RawReactionActionEvent):
        """
        Give a role when a user reacts to one of the configured reaction-role messages.
        """
        # Only care about guild reactions
        if payload.guild_id is None:
            return

        guild_rules = REACTION_RULES.get(payload.guild_id)
        if not guild_rules:
            return

        # Look up config for THIS message
        msg_cfg = guild_rules.get(payload.message_id)
        if not msg_cfg:
            return  # this message isn't watched

        # Ignore bot reactions
        if self.bot.user and payload.user_id == self.bot.user.id:
            return

        emoji_str = str(payload.emoji)
        if emoji_str != msg_cfg.get("emoji"):
            return  # wrong emoji

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(msg_cfg.get("role_id"))
        if role is None:
            return

        # payload.member is usually set on add; fallback just in case
        member = payload.member or guild.get_member(payload.user_id)
        if member is None or member.bot:
            return

        try:
            if role not in member.roles:
                await member.add_roles(role, reason="Reaction role (joined via reaction).")
        except Exception:
            # optional: log here
            pass


    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: nextcord.RawReactionActionEvent):
        """
        Optionally remove the role if the user removes their reaction.
        """
        if payload.guild_id is None:
            return

        cfg = REACTION_RULES.get(payload.guild_id)
        if not cfg:
            return

        if payload.message_id != cfg.get("message_id"):
            return

        # Ignore bot users
        if payload.user_id == self.bot.user.id:
            return

        emoji_str = str(payload.emoji)
        if emoji_str != cfg.get("emoji"):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(cfg.get("role_id"))
        if role is None:
            return

        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return

        # Remove the role if they had it
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Removed rules reaction.")
        except Exception:
            pass
            
    @commands.command(name="char")
    async def char(self, ctx, *, char_name: str = None):
        """
        Set your active character.
        - `!char`              -> list your chars & show current active
        - `!char Testman`      -> make 'Testman' active (partial & fuzzy allowed)
        """
        user_id = ctx.author.id
        names = list_chars(user_id)
        current = get_active(user_id)

        if not char_name:
            if not names:
                await ctx.send(
                    "❌ You don’t have any linked characters yet.\n"
                    "Create one with `!charcreate ...` or claim an existing `.coe` using your flow."
                )
                return

            lines = []
            for n in names:
                badge = " ✅ (active)" if n == current else ""
                lines.append(f"• {n}{badge}")

            await ctx.send("**Your characters:**\n" + "\n".join(lines) + "\n\nUse `!char <name>` to switch.")
            return

        def norm_spaces(s: str) -> str:
            return re.sub(r"[\s_]+", " ", s).strip().lower()

        by_norm = {norm_spaces(n): n for n in names}
        target = by_norm.get(norm_spaces(char_name))

        if target is None:
            try:
                if owns_char(user_id, char_name):
                    link_existing(user_id, char_name)
                    names = list_chars(user_id)
                    by_norm = {norm_spaces(n): n for n in names}
                    target = by_norm.get(norm_spaces(char_name))
            except PermissionError:
                target = None

        if target is None and names:
            resolved, sugg = _resolve_partial_any(names, char_name)
            if resolved:
                target = resolved
            elif sugg:
                await ctx.send(
                    "⚠️ Ambiguous character — did you mean: "
                    + ", ".join(f"`{s}`" for s in sugg)
                    + " ?"
                )
                return

        if target is None:
            if names:
                await ctx.send(
                    f"❌ I couldn’t find **{char_name}** in your characters.\n"
                    f"Known: {', '.join(names)}"
                )
            else:
                await ctx.send("❌ You don’t have any linked characters yet.")
            return

        if not set_active(user_id, target):
            await ctx.send("❌ Couldn’t set active character (ownership or registry mismatch).")
            return

        if target.lower() != (char_name or "").strip().lower():
            await ctx.send(f"✅ Active character set to **{target}** (matched from `{char_name}`).")
        else:
            await ctx.send(f"✅ Active character set to **{target}**.")


    @commands.guild_only()
    @commands.command(name="zap")
    async def zap(self, ctx, *, char_name: str):
        """
        Server-owner only.
        Delete a character: removes its .coe file and unregisters it from /data/players.json.

        Usage:
          !zap <character name>

        Notes:
          • Partial / fuzzy name matching supported.
          • If it was the owner’s active char, we pick a new active (first remaining) or clear it.
        """
        if not ctx.guild or ctx.guild.owner_id != ctx.author.id:
            await ctx.send("⛔ Server-owner only command.")
            return

        reg = _load_players_registry()
        all_chars = []
        for _uid, entry in reg.items():
            for n in entry.get("characters", []):
                if n not in all_chars:
                    all_chars.append(n)

        if not all_chars:
            await ctx.send("⚠️ No registered characters found.")
            return

        resolved, sugg = _resolve_partial_any(all_chars, char_name)
        if not resolved:
            if sugg:
                await ctx.send(
                    "⚠️ Ambiguous character — did you mean: "
                    + ", ".join(f"`{s}`" for s in sugg)
                    + " ?"
                )
            else:
                await ctx.send(f"❌ Character **{char_name}** not found in registry.")
            return

        owner_id = None
        for uid, entry in reg.items():
            if resolved in entry.get("characters", []):
                owner_id = uid
                break

        if owner_id is None:
            await ctx.send(f"❌ Couldn’t determine owner for **{resolved}** (registry inconsistent).")
            return

        candidates = {
            f"{resolved}.coe",
            f"{resolved.replace(' ', '_')}.coe",
            f"{resolved.lower().replace(' ', '_')}.coe",
        }
        deleted_files = []
        missing_files = []
        for path in candidates:
            if os.path.exists(path):
                try:
                    os.remove(path)
                    deleted_files.append(path)
                except Exception as e:
                    await ctx.send(f"❌ Failed to delete `{path}`: {type(e).__name__}: {e}")
                    return
            else:
                missing_files.append(path)

        entry = reg.get(owner_id, {})
        chars = entry.get("characters", [])
        if resolved in chars:
            chars.remove(resolved)
        if entry.get("active") == resolved:
            entry["active"] = (chars[0] if chars else "")
        entry["characters"] = chars
        reg[owner_id] = entry

        try:
            _atomic_write_json(PLAYERS_JSON, reg)
        except Exception as e:
            await ctx.send(f"❌ Deleted file(s) but failed to update registry: {type(e).__name__}: {e}")
            return

        deleted_note = f"🗑️ Deleted: {', '.join(deleted_files)}" if deleted_files else "ℹ️ No .coe file found to delete."
        active_note = f"Active now: **{entry.get('active') or '—'}**" if str(owner_id) in reg else ""
        await ctx.send(
            f"✅ Zapped **{resolved}** (owner: `{owner_id}`).\n"
            f"{deleted_note}\n"
            f"📘 Registry updated. {active_note}"
        )


    @tasks.loop(minutes=10)
    async def bday_daily_check(self):
        now_ct = _ct_now()
        today = now_ct.strftime("%m-%d")
        if self._bday_last_mmdd == today:
            return
        self._bday_last_mmdd = today

        reg = _load_players_registry()
        if not reg:
            return

        # Find users whose birthday is today
        todays = []
        for uid, entry in reg.items():
            if (entry.get("birthday_md") or "") == today:
                todays.append(int(uid))

        if not todays:
            return

        # Announce + DM
        for guild in self.bot.guilds:
            chan_id = BIRTHDAY_CHANNELS.get(guild.id)
            channel = guild.get_channel(chan_id) if chan_id else None
            for uid in todays:
                member = guild.get_member(uid)
                if not member:
                    continue

                # DM reminder (best-effort)
                try:
                    await member.send("🎂 It’s your birthday today! Use `!bday claim` in the server to open your birthday gift.")
                except Exception:
                    pass

                # Public announce (optional)
                if channel:
                    try:
                        await channel.send(f"🎉 Happy birthday, {member.mention}! 🎂")
                    except Exception:
                        pass

    @bday_daily_check.before_loop
    async def _before_bday_daily_check(self):
        await self.bot.wait_until_ready()


    @commands.group(name="bday", aliases=["birthday"], invoke_without_command=True)
    async def bday(self, ctx):
        reg = _load_players_registry()
        entry = reg.get(str(ctx.author.id), {})
        md = entry.get("birthday_md")
        if md:
            await ctx.send(f"🎂 Your birthday is set to **{md}** (MM-DD). Use `!bday claim` on that day for your gift.")
        else:
            await ctx.send("🎂 You don’t have a birthday set. Use `!bday set MM-DD` (example: `!bday set 02-26`).")

    @bday.command(name="set")
    async def bday_set(self, ctx, *, when: str):
        md = _parse_mmdd(when)
        if not md:
            await ctx.send("❌ Couldn’t parse that date. Use `MM-DD` (example: `!bday set 02-26`).")
            return
        reg = _load_players_registry()
        entry = reg.get(str(ctx.author.id), {})
        entry["birthday_md"] = md
        reg[str(ctx.author.id)] = entry
        _atomic_write_json(PLAYERS_JSON, reg)
        await ctx.send(f"✅ Birthday set to **{md}**. I’ll DM you a reminder on the day.")

    @bday.command(name="clear")
    async def bday_clear(self, ctx):
        reg = _load_players_registry()
        entry = reg.get(str(ctx.author.id), {})
        entry.pop("birthday_md", None)
        reg[str(ctx.author.id)] = entry
        _atomic_write_json(PLAYERS_JSON, reg)
        await ctx.send("🧹 Cleared your birthday setting.")

    @bday.command(name="claim")
    async def bday_claim(self, ctx, *, char_name: str = ""):
        now_ct = _ct_now()
        today = now_ct.strftime("%m-%d")
        year = now_ct.year

        reg = _load_players_registry()
        entry = reg.get(str(ctx.author.id), {})
        md = entry.get("birthday_md")

        if not md:
            await ctx.send("❌ You don’t have a birthday set. Use `!bday set MM-DD` first.")
            return
        if md != today:
            await ctx.send(f"🎂 Your birthday is set to **{md}**, but today is **{today}** (America/Chicago).")
            return

        if int(entry.get("bday_last_claim_year") or 0) == year:
            await ctx.send(f"🎁 You already claimed your birthday gift for **{year}**.")
            return

        # Resolve character
        if not char_name.strip():
            char_name = get_active(ctx.author.id) or ""
        if not char_name:
            await ctx.send("❌ No active character. Use `!char <name>` or `!bday claim <char>`.")
            return

        path = _char_file_from_name(char_name)
        if not os.path.exists(path):
            await ctx.send(f"❌ Character file not found: `{path}`")
            return

        cfg = read_cfg(path)

        # Simple, safe gift table (tweak freely)
        roll = random.randint(1, 100)
        gp = 0
        items = []

        if roll <= 60:
            gp = random.randint(4, 12) * 100  # 400–1200 gp
        elif roll <= 90:
            items.append(("Healing", 2))
            gp = 250
        elif roll <= 99:
            items.append(("Speed", 2))
            gp = 500
        else:
            items.append(("RareVoucher", 1))

        if gp:
            if not cfg.has_section("cur"):
                cfg.add_section("cur")
            try:
                cur_gp = int(str(get_compat(cfg, "cur", "gp", fallback="0")) or "0")
            except Exception:
                cur_gp = 0
            cfg.set("cur", "gp", str(cur_gp + gp))

        for tok, qty in items:
            _inv_add_stack_cfg(cfg, tok, qty)

        write_cfg(path, cfg)

        entry["bday_last_claim_year"] = year
        reg[str(ctx.author.id)] = entry
        _atomic_write_json(PLAYERS_JSON, reg)

        lines = [f"🎲 d100 → **{roll:02d}**", f"👤 **{char_name}**"]
        if gp:
            lines.append(f"🪙 + **{gp} gp**")
        if items:
            lines.append("🎁 " + ", ".join(f"{t}×{q}" for t, q in items))
        await ctx.send("🎂 **Birthday Gift Claimed!**\n" + "\n".join(lines))

    @bday.command(name="upcoming")
    async def bday_upcoming(self, ctx, days: int = 30):
        days = max(1, min(int(days or 30), 365))
        now_ct = _now_ct_from_ctx(ctx)
        today = now_ct.date()

        reg = _load_players_registry()
        upcoming = []

        # Only list members in *this* guild who opted in
        for m in ctx.guild.members:
            if m.bot:
                continue
            entry = reg.get(str(m.id), {})
            md = entry.get("birthday_md")
            if not md:
                continue

            nxt = _next_occurrence(today, md)
            delta = (nxt - today).days
            if delta <= days:
                upcoming.append((delta, nxt, m.display_name, m.id))

        if not upcoming:
            await ctx.send(f"📅 No birthdays set in the next **{days}** days.\n(Players can opt in with `!bday set MM-DD`.)")
            return

        upcoming.sort(key=lambda t: (t[0], t[2].lower()))
        lines = []
        for delta, dt, name, _uid in upcoming[:40]:
            label = "today" if delta == 0 else f"in {delta}d"
            pretty = f"{dt.strftime('%b')} {dt.day}"
            lines.append(f"• **{pretty}** ({label}) — {name}")

        extra = len(upcoming) - min(len(upcoming), 40)
        more = f"\n… and **{extra}** more." if extra > 0 else ""

        await ctx.send(
            f"🎉 **Upcoming birthdays (next {days} days, America/Chicago):**\n"
            + "\n".join(lines)
            + more,
            allowed_mentions=nextcord.AllowedMentions.none(),
        )

def setup(bot):
    bot.add_cog(Players(bot))
