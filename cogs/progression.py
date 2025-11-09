import os, json, re, datetime, random, configparser
from pathlib import Path
import nextcord
from nextcord.ext import commands
from decimal import Decimal, ROUND_HALF_UP
from utils.ini import read_cfg, write_cfg, get_compat, getint_compat
from utils.players import get_active

DATA_DIR = Path("data/xp")
DATA_DIR.mkdir(parents=True, exist_ok=True)



def _load_class_xp(path: str = "class.lst"):
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(path)
    xp_cache = {}
    for section in cfg.sections():
        table = {}
        for k, v in cfg.items(section):
            m = re.fullmatch(r"xp(\d+)", k, flags=re.I)
            if m and v.strip().lstrip("-").isdigit():
                lvl = int(m.group(1))
                table[lvl] = int(v.strip())
        if table:
            xp_cache[section] = dict(sorted(table.items())) 
    return xp_cache

_CLASS_XP = _load_class_xp()

def _xp_needed_for_next_level(char_class: str, current_level: int) -> int | None:
    table = _CLASS_XP.get(char_class, {})
    return table.get(current_level + 1)

def _max_level_for_xp(char_class: str, xp_total: int) -> int:
    """
    Return the highest level the XP qualifies for, based on class.lst.
    Level 1 requires 0 XP; xp2 is threshold for reaching level 2, etc.
    """
    table = _CLASS_XP.get(char_class, {})
    level = 1
    for lvl, req in sorted(table.items()): 
        if xp_total >= req:
            level = max(level, lvl)
        else:
            break
    return level

def _xp_log_path(char_name: str) -> Path:
    slug = char_name.replace(" ", "_")
    return DATA_DIR / f"{slug}.json"

def _read_xp_log(char_name: str) -> list[dict]:
    p = _xp_log_path(char_name)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []

def _append_xp_log(char_name: str, *, when: datetime.datetime, amount: int,
                   who_id: int, reason: str, before_xp: int, after_xp: int,
                   before_lvl: int, after_lvl: int):
    p = _xp_log_path(char_name)
    log = _read_xp_log(char_name)
    log.append({
        "ts": when.isoformat(timespec="seconds") + "Z",
        "amount": amount,
        "by": str(who_id),
        "reason": reason,
        "before_xp": before_xp,
        "after_xp": after_xp,
        "before_lvl": before_lvl,
        "after_lvl": after_lvl,
    })
    p.write_text(json.dumps(log, indent=2), encoding="utf-8")



class ProgressionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    
    def _get_char_race_class(self, cfg):
        race = (get_compat(cfg, "info", "race", fallback="Human") or "").strip().lower()
        cls  = (get_compat(cfg, "info", "class", fallback="Fighter") or "").strip().lower()
        return race, cls

    def _fmt_num(self, value, places=1):
        q = Decimal('1').scaleb(-places)
        d = Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP)
        s = format(d, 'f')
        if '.' in s:
            s = s.rstrip('0').rstrip('.')
        return s

    def _human_xp_bonus(self, base_xp: int, race: str | None) -> int:
        """+10% XP for humans. Integer math, round half up."""
        if race and race.strip().lower() == "human" and base_xp > 0:
            return (base_xp + 5) // 10
        return 0


    def _bump_die(self, die: int) -> int:
        """Bump one die size: 4→6→8→10→12 (12 stays 12)."""
        order = [4, 6, 8, 10, 12]
        try:
            i = order.index(die)
            return order[min(i + 1, len(order) - 1)]
        except ValueError:
            return min(max(die + 2, 4), 12)

    def _hit_die_for(self, char_class: str, race: str | None = None) -> int:
        """
        Determine the class hit die, then apply race adjustments:
          - Half-Ogre: bump one size (e.g., d8→d10, d10→d12)
          - Races with 'Frail' in skills: cap at d6 (Elf, Gnome in your data)
        Prefers class.lst hp9=1dX when available; falls back to a mapping.
        """
        cls_lc = (char_class or "").strip().lower()
        race_lc = (race or "").strip().lower()

        base_die = None
        try:
            if hasattr(self, "_class_cp"):
                sec = None
                for s in self._class_cp.sections():
                    if s.lower() == cls_lc:
                        sec = s; break
                if sec and self._class_cp.has_option(sec, "hp9"):
                    hp9 = self._class_cp.get(sec, "hp9").strip()
                    m = re.search(r"1d(\d+)", hp9, flags=re.I)
                    if m:
                        base_die = int(m.group(1))
        except Exception:
            pass

        if not base_die:
            fallback = {
                "fighter": 8, "barbarian": 10, "paladin": 8, "ranger": 8,
                "cleric": 6, "druid": 6,
                "thief": 4, "assassin": 4, "scout": 6,
                "magic-user": 4, "illusionist": 4, "necromancer": 4, "spellcrafter": 4,
                "fightermage": 6, "magethief": 4,  
            }
            base_die = fallback.get(cls_lc, 6)

        die = base_die

        if race_lc == "half-ogre" or race_lc == "halfogre":
            die = self._bump_die(die)

        try:
            if hasattr(self, "races"):
                rinfo = self.races.get(race_lc, {})
                if "frail" in str(rinfo.get("skills", "")).lower():
                    die = min(die, 6)
        except Exception:
            pass

        return int(die)

    def _coerce_int(self, v, default: int = 0) -> int:
        """Coerce possibly-string modifiers like '+2'/' -1 ' into int."""
        try:
            return int(v)
        except Exception:
            try:
                s = str(v).strip()
                if not s:
                    return default
                m = re.match(r"^([+-]?)\s*(\d+)$", s)
                if m:
                    sign = -1 if m.group(1) == "-" else 1
                    return sign * int(m.group(2))
            except Exception:
                pass
            return default

    def _racial_xp_bonus(self, base_amount: int, race: str) -> tuple[int, int]:
        """
        Returns (bonus_amount, percent_used) for a race-based XP bonus.
        Floors the bonus to an integer. No bonus if base_amount <= 0.
        Order of precedence:
          1) If race.lst has xpbonus=<int>, use that percent
          2) Otherwise fall back to hard-coded mapping below
        """
        try:
            amt = int(base_amount)
        except Exception:
            return (0, 0)
        if amt <= 0:
            return (0, 0)

        rnorm = (race or "").strip().lower()
        rnorm = rnorm.replace("_", " ").replace("-", " ")
        rnorm = " ".join(rnorm.split())

        pct = None
        try:
            if hasattr(self, "_race_cp"):
                rp = self._race_cp
            else:
                rp = configparser.ConfigParser()
                rp.optionxform = str
                rp.read("race.lst")
                self._race_cp = rp

            sec = None
            for s in self._race_cp.sections():
                s_norm = " ".join(s.strip().lower().replace("_"," ").replace("-"," ").split())
                if s_norm == rnorm:
                    sec = s
                    break
            if sec and self._race_cp.has_option(sec, "xpbonus"):
                try:
                    pct = int(str(self._race_cp.get(sec, "xpbonus")).strip())
                except Exception:
                    pct = None
        except Exception:
            pct = None

        if pct is None:
            mapping = {
                "human": 10,
                "half elf": 5,
                "half ogre": 5,
                "half orc": 5,
            }
            pct = mapping.get(rnorm, 0)

        bonus = (amt * pct) // 100
        return (bonus, pct)

    def _ensure_class_cp(self):
        if not hasattr(self, "_class_cp"):
            cp = configparser.ConfigParser()
            cp.optionxform = str
            cp.read("class.lst")
            self._class_cp = cp
        return self._class_cp

    def _coerce_int(self, v, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            try:
                s = str(v).strip()
                if not s:
                    return default
                m = re.match(r"^([+-]?)\s*(\d+)$", s)
                if m:
                    sign = -1 if m.group(1) == "-" else 1
                    return sign * int(m.group(2))
            except Exception:
                pass
            return default

    def _bump_die(self, die: int) -> int:
        order = [4, 6, 8, 10, 12]
        try:
            i = order.index(die)
            return order[min(i + 1, len(order) - 1)]
        except ValueError:
            return min(max(int(die) + 2, 4), 12)

    def _hit_die_for(self, char_class: str, race: str | None = None) -> int:
        """Base class HD (hp9) with Half-Ogre bump and Frail cap."""
        cls_lc = (char_class or "").strip().lower()
        race_lc = (race or "").strip().lower()

        base_die = None
        try:
            cp = self._ensure_class_cp()
            sec = next((s for s in cp.sections() if s.lower() == cls_lc), None)
            if sec and cp.has_option(sec, "hp9"):
                m = re.search(r"1d(\d+)", cp.get(sec, "hp9"), flags=re.I)
                if m:
                    base_die = int(m.group(1))
        except Exception:
            pass

        if not base_die:
            fallback = {
                "fighter": 8, "barbarian": 10, "paladin": 8, "ranger": 8,
                "cleric": 6, "druid": 6,
                "thief": 4, "assassin": 4, "scout": 6,
                "magic-user": 4, "illusionist": 4, "necromancer": 4, "spellcrafter": 4,
                "fightermage": 6, "magethief": 4,
            }
            base_die = fallback.get(cls_lc, 6)

        die = base_die

        if race_lc in {"half-ogre", "halfogre"}:
            die = self._bump_die(die)

        try:
            if hasattr(self, "races"):
                rinfo = self.races.get(race_lc, {})
                if "frail" in str(rinfo.get("skills", "")).lower():
                    die = min(die, 6)
        except Exception:
            pass

        return int(die)

    def _hp_flat_after9(self, char_class: str) -> int:
        """
        Flat HP gain per level at 10+ from class.lst hp20.
        If missing, use a sane heuristic from the class's base die.
        """
        try:
            cp = self._ensure_class_cp()
            sec = next((s for s in cp.sections() if s.lower() == (char_class or "").strip().lower()), None)
            if sec and cp.has_option(sec, "hp20"):
                txt = cp.get(sec, "hp20").strip()
                m = re.search(r"-?\d+", txt)
                if m:
                    return max(0, int(m.group(0)))
        except Exception:
            pass

        die = self._hit_die_for(char_class, race=None)
        if die >= 10: return 3
        if die >= 8:  return 2
        return 1

    def levelup_hp_one_level(self, char_class: str, con_modifier, race: str | None = None, *, entering_level: int | None = None) -> tuple[int, str]:
        if entering_level is not None and entering_level >= 10:
            flat = self._hp_flat_after9(char_class)
            return (int(flat), f"flat+{flat}")
        cm = self._coerce_int(con_modifier, 0)
        max_die = self._hit_die_for(char_class, race)
        roll = random.randint(1, max_die)
        return (max(1, roll + cm), str(roll))

    def _coerce_int(self, v, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            try:
                s = str(v).strip()
                if not s:
                    return default
                m = re.match(r"^([+-]?)\s*(\d+)$", s)
                if m:
                    sign = -1 if m.group(1) == "-" else 1
                    return sign * int(m.group(2))
            except Exception:
                pass
            return default





    @commands.command(name="xp")
    async def xp(self, ctx, *, arg: str = ""):
        """
        Show current XP/thresholds, or adjust XP with shorthand:
          • !xp               -> show summary + recent log
          • !xp +350 Goblin camp   -> add 350 XP (racial/human bonus applies)
          • !xp -200 Cursed drain  -> subtract 200 XP (NO racial/human bonus)
        """
        s = (arg or "").strip()
        if s:
            m = re.match(r"^([+-])\s*(\d+)(?:\s+(.*))?$", s)
            if m:
                sign, num, rsn = m.group(1), int(m.group(2)), (m.group(3) or "").strip()
                if sign == "+":
                    await self._apply_xp_delta(ctx, num, reason=rsn, apply_racial_bonus=True)
                else:
                    await self._apply_xp_delta(ctx, -num, reason=rsn, apply_racial_bonus=False)
                return

        char_name = get_active(ctx.author.id)
        if not char_name:
            await ctx.send("❌ No active character. Use `!setactive <name>` first.")
            return

        file_name = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(file_name):
            await ctx.send(f"❌ Character '{char_name}' does not exist.")
            return

        cfg = read_cfg(file_name)
        owner_id = get_compat(cfg, "info", "owner_id")
        if owner_id != str(ctx.author.id):
            await ctx.send(f"❌ You do not own '{char_name}'.")
            return

        char_class = get_compat(cfg, "info", "class", fallback="Fighter")
        cur_xp  = getint_compat(cfg, "cur", "xp", fallback=0)
        level   = getint_compat(cfg, "cur", "level", fallback=1)

        next_req = _xp_needed_for_next_level(char_class, level)

        embed = nextcord.Embed(
            title=f"{char_name} — XP Summary",
            description=f"Class: {char_class}\nLevel: {level}\nXP: {cur_xp}",
            color=nextcord.Color.blurple()
        )
        if next_req is not None:
            embed.add_field(name="Next Level At", value=str(next_req), inline=True)
            embed.add_field(name="XP To Next", value=str(max(0, next_req - cur_xp)), inline=True)
        else:
            embed.add_field(name="Next Level At", value="(max table reached)", inline=True)

        log = _read_xp_log(char_name)
        if log:
            last = log[-10:]
            lines = []
            for e in reversed(last):
                ts   = e.get("ts", "?").replace("T", " ")
                amt  = e.get("amount", 0)
                rsn  = e.get("reason", "")
                lines.append(f"{ts} • {amt:+} XP" + (f" • {rsn}" if rsn else ""))
            embed.add_field(name="Recent XP Events", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Recent XP Events", value="(no entries yet)", inline=False)

        await ctx.send(embed=embed)

    async def _apply_xp_delta(self, ctx, base_amount: int, *, reason: str = "", apply_racial_bonus: bool = True):
        """
        Core XP adjuster used by !addxp and !xp +/-.
        - Positive amounts may apply racial/human bonuses (if enabled).
        - Negative amounts NEVER apply bonuses.
        - No auto-level; just updates XP and logs.
        """
        import datetime

        char_name = get_active(ctx.author.id)
        if not char_name:
            await ctx.send("❌ No active character. Use `!setactive <name>` first.")
            return

        file_name = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(file_name):
            await ctx.send(f"❌ Character '{char_name}' does not exist.")
            return

        cfg = read_cfg(file_name)

        owner_id = get_compat(cfg, "info", "owner_id")
        if owner_id != str(ctx.author.id):
            await ctx.send(f"❌ You do not own '{char_name}'.")
            return

        char_class = get_compat(cfg, "info", "class", fallback="Fighter").strip()
        race       = get_compat(cfg, "info", "race",  fallback="Human").strip()

        bonus = pct = 0
        if apply_racial_bonus and base_amount > 0:
            bonus, pct = self._racial_xp_bonus(base_amount, race)
        grant = base_amount + bonus

        before_xp  = getint_compat(cfg, "cur", "xp", fallback=0)
        before_lvl = getint_compat(cfg, "cur", "level", fallback=1)

        new_xp = max(0, before_xp + grant)

        eligible_lvl = _max_level_for_xp(char_class, new_xp)

        cfg.setdefault("cur", {})
        cfg["cur"]["xp"] = str(new_xp)
        write_cfg(file_name, cfg)

        log_reason = reason or (f"Base {base_amount} + racial bonus {bonus} ({pct}%)" if bonus else f"{base_amount}")
        _append_xp_log(
            char_name,
            when=datetime.datetime.utcnow(),
            amount=grant,
            who_id=ctx.author.id,
            reason=log_reason,
            before_xp=before_xp,
            after_xp=new_xp,
            before_lvl=before_lvl,
            after_lvl=before_lvl
        )

        title = f"{char_name} {'gained' if grant >= 0 else 'lost'} {abs(grant)} XP"
        if bonus and grant > 0:
            title += f"  (+{bonus} racial bonus)"

        desc_lines = [
            f"Class: {char_class}",
            f"Level: {before_lvl}",
            f"XP: {new_xp}",
        ]

        next_req = _xp_needed_for_next_level(char_class, max(before_lvl, 1))
        if next_req is None:
            desc_lines.append("Next Level at: — (max level)")
        else:
            to_next = max(0, next_req - new_xp)
            desc_lines.append(f"Next Level at: {next_req} (need {to_next} more)")

        embed = nextcord.Embed(
            title=title,
            description="\n".join(desc_lines),
            color=random.randint(0, 0xFFFFFF)
        )

        if eligible_lvl > before_lvl:
            if eligible_lvl == before_lvl + 1:
                note = f"XP qualifies for **Level {eligible_lvl}**."
            else:
                note = f"XP qualifies for **Level {before_lvl+1}–{eligible_lvl}** (multiple levels available)."
            embed.add_field(
                name="Level Up Available",
                value=f"{note}\nUse `!levelup {char_name}` to advance.",
                inline=False
            )

        should_be_lvl = _max_level_for_xp(char_class, new_xp)
        if should_be_lvl < before_lvl:
            embed.add_field(
                name="Note",
                value=("XP is below the minimum for your current level, "
                       "but levels are not reduced automatically."),
                inline=False
            )

        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)

        await ctx.send(embed=embed)


    @commands.command(name="addxp")
    async def addxp(self, ctx, amount: int, *, reason: str = ""):
        """Add XP to your ACTIVE character (no auto-level)."""
        await self._apply_xp_delta(ctx, amount, reason=reason, apply_racial_bonus=True)

def setup(bot):
    bot.add_cog(ProgressionCog(bot))

