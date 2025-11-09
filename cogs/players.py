import os, re, json
from pathlib import Path
from nextcord.ext import commands
from utils.players import (
    list_chars,
    set_active,
    link_existing,
    owns_char,
    get_active,
)

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


class Players(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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

def setup(bot):
    bot.add_cog(Players(bot))

