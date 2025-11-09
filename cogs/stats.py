import os
import re
import random
import nextcord
import configparser
from urllib.parse import urlparse
from nextcord.ext import commands
from utils.players import get_active
from utils.ini import (
    read_cfg, write_cfg, get_compat, getint_compat
)
from cogs.spells import _poly_active
from cogs.combat import _equipped_protection_bonus

def osr_mod(score: int) -> int:
    try:
        s = int(score)
    except Exception:
        s = 10
    if s <= 3:
        return -3
    if s <= 5:
        return -2
    if s <= 8:
        return -1
    if s <= 12:
        return 0
    if s <= 15:
        return +1
    if s <= 17:
        return +2
    return +3


LEVELS = list(range(1, 21))

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

CLERIC_SPELLS = {
    'spell1': [0,1,2,2,2,2,3,3,3,3,4,4,4,4,4,5,5,5,6,6],
    'spell2': [0,0,0,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5],
    'spell3': [0,0,0,0,0,1,2,2,2,2,3,3,3,4,4,4,4,4,4,5],
    'spell4': [0,0,0,0,0,0,0,1,2,2,2,2,3,3,3,3,3,4,4,4],
    'spell5': [0,0,0,0,0,0,0,0,0,1,2,2,2,2,3,3,3,3,3,3],
    'spell6': [0,0,0,0,0,0,0,0,0,0,0,1,2,2,2,2,2,3,3,3],
    'spell7': [0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,2,2,2,3]
}

CLERIC_XP = [0, 1500, 3000, 6000, 12000, 24000, 48000, 90000, 180000, 270000,
             360000, 450000, 540000, 630000, 720000, 810000, 900000, 990000,
             1080000, 1170000]

THIEF_SKILLS = {
    'openlock': [25,30,35,40,45,50,55,60,65,68,71,74,77,80,83,84,85,86,87,88],
    'removetrap': [20,25,30,35,40,45,50,55,60,63,66,69,72,75,78,79,80,81,82,83],
    'pickpocket': [30,35,40,45,50,55,60,65,70,74,78,82,86,90,94,95,96,97,98,99],
    'movesilently': [25,30,35,40,45,50,55,60,65,68,71,74,77,80,83,85,87,89,91,93],
    'climbwall': [80,81,82,83,84,85,86,87,88,89,90,91,92,93,94,95,96,97,98,99],
    'hide': [10,15,20,25,30,35,40,45,50,53,56,59,62,65,68,69,70,71,72,73],
    'listen': [30,34,38,42,46,50,54,58,62,65,68,71,74,77,80,83,86,89,92,95]
}

THIEF_XP = [0,1250,2500,5000,10000,20000,40000,75000,150000,225000,
            300000,375000,450000,525000,600000,675000,750000,825000,900000,975000]

FIGHTER_XP = [0,2000,4000,8000,16000,32000,64000,125000,250000,375000,
              500000,625000,750000,875000,1000000,1125000,1250000,1375000,1500000,1625000]

MAGIC_USER_SPELLS = {
    'spell1': [0,1,2,2,2,2,3,3,3,4,4,4,4,4,5,5,5,6,6,6],
    'spell2': [0,0,1,2,2,2,2,3,3,3,4,4,4,4,4,5,5,5,5,5],
    'spell3': [0,0,0,0,1,2,2,2,2,3,3,3,4,4,4,4,4,4,5,5],
    'spell4': [0,0,0,0,0,0,1,2,2,2,2,3,3,3,3,3,4,4,4,4],
    'spell5': [0,0,0,0,0,0,0,0,1,2,2,2,2,3,3,3,3,3,3,4],
    'spell6': [0,0,0,0,0,0,0,0,0,0,0,1,2,2,2,2,2,3,3,3,3],
    'spell7': [0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,2,2,2,2,3]
}

MAGIC_USER_XP = [0,2500,5000,10000,20000,40000,80000,160000,320000,480000,
                 640000,800000,960000,1120000,1280000,1440000,1600000,1760000,1920000,2080000]

def load_classes_ab(path="class.lst"):
    """
    Returns { 'fighter': {'ab':[...], ... } with AB parsed to list[int].
    """
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(path)

    out = {}
    for section in cfg.sections():
        sec = section.lower()
        out[sec] = {}
        for k, v in cfg.items(section):
            if k.lower() == "ab":
                try:
                    out[sec]["ab"] = [int(x) for x in v.split()]
                except Exception:
                    out[sec]["ab"] = []
            else:
                out[sec][k.lower()] = v
    return out

ALWAYS_LIST = {'skills', 'banned', 'banned_weapons'}


def load_races(file_path):
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(file_path)

    races = {}
    for section in cfg.sections():
        data = {}
        for key, value in cfg.items(section):
            val = value.strip()
            tokens = [t for t in re.split(r'[\,\s]+', val) if t]

            if key in ALWAYS_LIST:
                data[key] = tokens
                continue

            if len(tokens) > 1:
                try:
                    data[key] = [int(t) for t in tokens]
                except ValueError:
                    data[key] = tokens
            else:
                try:
                    data[key] = int(val)
                except ValueError:
                    data[key] = val
        races[section] = data
    return races


race_data = load_races('race.lst')


def ability_mod(val) -> int:
    """
    OSR/BX-style ability modifiers:
      3:-3, 4-5:-2, 6-8:-1, 9-12:0, 13-15:+1, 16-17:+2, 18:+3
    """
    try:
        s = int(val)
    except Exception:
        return 0
    if s <= 3:   return -3
    if s <= 5:   return -2
    if s <= 8:   return -1
    if s <= 12:  return  0
    if s <= 15:  return  1
    if s <= 17:  return  2
    return 3

def _norm_item_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())

def _str_item_override(cfg) -> tuple[int | None, str | None]:
    """
    Returns (override_mod, source_label) if a worn item sets the STR *modifier*.
    Priority: Girdle > Gauntlets. Returns (None, None) if no override.
    """
    hands = get_compat(cfg, "eq", "hands", fallback="")
    belt  = get_compat(cfg, "eq", "belt",  fallback="")

    hn = _norm_item_name(hands)
    bn = _norm_item_name(belt)


    if "girdleofgiantstrength" in bn:
        return 5, "Girdle of Giant Strength"
    if "gauntletsofogrepower" in hn:
        return 4, "Gauntlets of Ogre Power"
    return None, None

def _effective_str_mod(cfg) -> int:
    """Item overrides take precedence over the normal OSR STR mod."""
    base = _osr_mod_from_cfg(cfg, "str")
    ov, _ = _str_item_override(cfg)
    return ov if (ov is not None) else base


def _osr_mod_from_cfg(cfg, stat_key: str, *, fallback_score: int = 10) -> int:
    """
    Read stats.<stat_key> (e.g., 'str', 'dex') and return the OSR/BX modifier
    via ability_mod(). We DO NOT trust stats.<stat_key>_modifier (may be 5e-style).
    """
    score = getint_compat(cfg, "stats", stat_key, fallback=fallback_score)
    return ability_mod(score)

def _osr_str_dex_mods(cfg) -> tuple[int, int]:
    return _effective_str_mod(cfg), _osr_mod_from_cfg(cfg, "dex")


def update_progression(char_class, level, config):
    """Update the character's spells or skills based on their class and level."""
    level = getint_compat(config, "cur", "level", fallback=1)
    level_idx = max(0, level - 1)
    char_class_lower = char_class.lower()

    if char_class_lower == 'cleric':
        config['spells'] = {spell: progression[level_idx] for spell, progression in CLERIC_SPELLS.items()}

    elif char_class_lower == 'magic-user':
        config['spells'] = {spell: progression[level_idx] for spell, progression in MAGIC_USER_SPELLS.items()}

    elif char_class_lower == 'thief':
        config['skills'] = {skill: progression[level_idx] for skill, progression in THIEF_SKILLS.items()}


def _load_class_cache(path: str = "class.lst"):
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(path)

    cache = {}
    for section in cfg.sections():
        cache[section] = {}
        for k, v in cfg.items(section):
            v = v.strip()

            if v and " " in v:
                parts = v.split()
                if all(p.lstrip("-").isdigit() for p in parts):
                    cache[section][k] = [int(p) for p in parts]
                else:
                    cache[section][k] = v
            else:

                if v.lstrip("-").isdigit():
                    cache[section][k] = int(v)
                else:
                    cache[section][k] = v
    return cache

_CLASS_CACHE = _load_class_cache()

def _eq_get_weapons_from_cfg(cfg) -> list[str]:
    """Return equipped weapons in order (weapon1..weaponN), compacted."""
    names: list[str] = []
    try:
        cnt = getint_compat(cfg, "eq", "weapon", fallback=None)
    except Exception:
        cnt = None

    if cnt and cnt > 0:
        for i in range(1, cnt + 1):
            w = get_compat(cfg, "eq", f"weapon{i}", fallback="").strip()
            if w:
                names.append(w)
    else:

        if cfg.has_section("eq"):
            pairs = []
            for k, v in cfg.items("eq"):
                m = re.fullmatch(r"weapon(\d+)", k, flags=re.I)
                if m and v.strip():
                    pairs.append((int(m.group(1)), v.strip()))
            names = [v for _, v in sorted(pairs)]
    return names


def _weight_thresholds(race: str, STR: int) -> tuple[int, int]:
    """Return (light_limit, heavy_limit) by race & STR (Halfling uses small column)."""
    s = max(3, min(int(STR or 10), 18))
    small = str(race).strip().lower() == "halfling"

    if s == 3:        med = (25, 60);  sml = (20, 40)
    elif s in (4, 5): med = (35, 90);  sml = (30, 60)
    elif 6 <= s <= 8: med = (50, 120); sml = (40, 80)
    elif 9 <= s <= 12:med = (60, 150); sml = (50, 100)
    elif 13 <= s <= 15:med = (65, 165); sml = (55, 110)
    elif s in (16, 17):med = (70, 180); sml = (60, 120)
    else:              med = (80, 195); sml = (65, 130)

    return sml if small else med


def _eq_get_carry_from_cfg(cfg) -> list[str]:
    """Return carried items in order (carry1..carryN)."""
    names: list[str] = []
    try:
        cnt = getint_compat(cfg, "eq", "carry", fallback=None)
    except Exception:
        cnt = None

    if cnt and cnt > 0:
        for i in range(1, cnt + 1):
            v = get_compat(cfg, "eq", f"carry{i}", fallback="").strip()
            if v:
                names.append(v)
    else:

        if cfg.has_section("eq"):
            pairs = []
            for k, v in cfg.items("eq"):
                m = re.fullmatch(r"carry(\d+)", k, flags=re.I)
                if m and v.strip():
                    pairs.append((int(m.group(1)), v.strip()))
            names = [v for _, v in sorted(pairs)]
    return names


def _looks_like_url(u: str) -> bool:
    if not u:
        return False
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

class StatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.classes = load_classes_ab("class.lst")

    @commands.command(name="portrait")
    async def portrait(self, ctx, url: str = None):
        """
        Set or view your active character's portrait.
        Usage:
          !portrait <url>   -> sets portrait for your active character
          !portrait clear   -> removes portrait
          !portrait         -> shows current portrait
        """

        char_name = get_active(ctx.author.id)
        if not char_name:
            await ctx.send("❌ No active character. Use `!char <name>` first.")
            return

        path = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(path):
            await ctx.send(f"❌ Character file not found for **{char_name}**.")
            return

        cfg = read_cfg(path)
        owner_id = get_compat(cfg, "info", "owner_id", fallback="")
        if owner_id and owner_id != str(ctx.author.id):
            await ctx.send(f"❌ You do not own **{char_name}**.")
            return


        if url is None:
            current = (get_compat(cfg, "info", "portrait", fallback="") or "").strip()
            if not current:
                await ctx.send("ℹ️ No portrait set. Use `!portrait <image-url>` to set one.")
                return
            emb = nextcord.Embed(
                title=f"{char_name} — Portrait",
                color=random.randint(0, 0xFFFFFF)
            )
            emb.set_image(url=current)
            await ctx.send(embed=emb)
            return


        if url.lower() in {"clear", "remove", "none"}:
            if cfg.has_option("info", "portrait"):
                cfg.remove_option("info", "portrait")
                write_cfg(path, cfg)
                await ctx.send(f"🧹 Cleared portrait for **{char_name}**.")
            else:
                await ctx.send("ℹ️ No portrait to clear.")
            return


        if not _looks_like_url(url):
            await ctx.send("❌ That doesn’t look like a valid URL. Use http(s) links.")
            return

        if not cfg.has_section("info"):
            cfg.add_section("info")
        cfg.set("info", "portrait", url)
        write_cfg(path, cfg)

        emb = nextcord.Embed(
            title=f"{char_name} — Portrait set",
            description="This image will now appear in `!stats`, `!a`, `!cast`, `!lr`, etc.",
            color=random.randint(0, 0xFFFFFF)
        )
        try:
            emb.set_thumbnail(url=url)
        except Exception:
            pass
        await ctx.send(embed=emb)


    @commands.command()
    async def stats(self, ctx, char_name: str = None):
        """Display the stats, modifiers, and saving throw DCs for a character."""

        if not char_name:
            char_name = get_active(ctx.author.id)
            if not char_name:
                await ctx.send("❌ No active character. Use `!char <name>` or pass a name: `!stats <name>`.")
                return


        disp_name = None
        file_path = None
        try:

            disp_name, file_path = _resolve_char_ci(char_name)
        except Exception:
            pass

        if not file_path:

            import os
            target = f"{(char_name or '').replace(' ', '_').lower()}.coe"
            for fn in os.listdir("."):
                if fn.lower() == target:
                    file_path = fn

                    try:
                        cfg_tmp = read_cfg(fn)
                        disp_name = get_compat(cfg_tmp, "info", "name", fallback=None) or fn[:-4].replace("_", " ")
                    except Exception:
                        disp_name = fn[:-4].replace("_", " ")
                    break

        if not file_path:
            await ctx.send(f"❌ Character '{char_name}' does not exist.")
            return


        char_name = disp_name or char_name
        config = read_cfg(file_path)

        owner_id = get_compat(config, "info", "owner_id", fallback="")

        allowed = (owner_id == str(ctx.author.id)) or getattr(ctx.author.guild_permissions, "manage_guild", False)


        if not allowed:
            try:
                bcfg = _load_battles()
                for sec in bcfg.sections():
                    if bcfg.get(sec, "DM", fallback="") == str(ctx.author.id):
                        allowed = True
                        break
            except Exception:
                pass

        if not allowed:
            await ctx.send(f"❌ You do not own '{char_name}'.")
            return


        file_name = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(file_name):
            await ctx.send(f"❌ Character '{char_name}' does not exist.")
            return


        def _as_int(val, default=0):
            try:
                return int(val)
            except Exception:
                return default

        stats = {}
        if config.has_section('stats'):
            for k, v in config.items('stats'):
                stats[k] = _as_int(v, 0)


        hp  = getint_compat(config, "cur", "hp", fallback=0)
        mhp = getint_compat(config, "max", "hp", fallback=0)
        lvl = getint_compat(config, "cur", "level", fallback=1)
        neg_levels = getint_compat(config, "cur", "neg_levels", fallback=0)
        neg_pool   = getint_compat(config, "cur", "neg_hp_loss_total", fallback=0)
        eff_cap    = max(0, mhp - neg_pool)

        cur_con = getint_compat(config, "stats", "con", fallback=stats.get("con", 0))
        temp_con_loss = getint_compat(config, "cur", "con_loss_temp", fallback=0)
        perm_con_loss = getint_compat(config, "cur", "con_loss_perm", fallback=0)
        base_con = max(1, cur_con + temp_con_loss + perm_con_loss)

        def _con_mod(x: int) -> int:

            return osr_mod(int(x))


        try:
            hd_levels = self._levels_count_for_hp(config)
        except Exception:
            hd_levels = max(1, getint_compat(config, "cur", "level", fallback=1))


        base_mod = _con_mod(base_con)
        curr_mod = _con_mod(cur_con)
        con_hp_delta_abs = (base_mod - curr_mod) * hd_levels
        mhp_base = mhp + con_hp_delta_abs

        if hp > eff_cap:
            hp = eff_cap

        char_class = get_compat(config, "info", "class", fallback="Fighter")
        cur_xp  = getint_compat(config, "cur", "xp", fallback=0)
        next_req = _xp_needed_for_next_level(char_class, lvl)
        to_next = f"{next_req - cur_xp}" if next_req is not None else "—"

        ac   = getint_compat(config, "stats", "ac",   fallback=11)
        move = getint_compat(config, "stats", "move", fallback=20)
        try:
            if _poly_active(config):
                ac   = getint_compat(config, "poly", "ac",   fallback=ac)
                move = getint_compat(config, "poly", "move", fallback=move)
        except Exception:
            pass

        cls = get_compat(config, "info", "class", fallback="Fighter")
        race = get_compat(config, "info", "race")
        char_class = get_compat(config, "info", "class", fallback="Fighter")


        base_keys  = ["str", "dex", "con", "int", "wis", "cha"]
        nice_names = {"str":"STR","dex":"DEX","con":"CON","int":"INT","wis":"WIS","cha":"CHA"}

        def _signed(n: int) -> str:
            return f"+{n}" if n >= 0 else str(n)

        ability_lines = []
        eff_str_mod, src = _str_item_override(config)

        for key in base_keys:
            score = stats.get(key, 0)
            base_mod = ability_mod(score)

            if key == "str" and eff_str_mod is not None and eff_str_mod != base_mod:
                ability_lines.append(
                    f"STR: {score} ({_signed(base_mod)} → **{_signed(eff_str_mod)}** from {src})"
                )
            else:
                ability_lines.append(f"{nice_names[key]}: {score} ({_signed(base_mod)})")


        class_block = _CLASS_CACHE.get(cls, {})
        save_keys = {
            "poi": "Death Ray or Poison",
            "wand": "Magic Wands",
            "para": "Paralysis or Petrify",
            "breath": "Dragon Breath",
            "spell": "Spells"
        }
        save_lines = []
        for k, label in save_keys.items():
            arr = class_block.get(k, [])
            if isinstance(arr, list) and arr:
                idx = min(max(lvl - 1, 0), len(arr) - 1)
                dc = arr[idx]
                save_lines.append(f"• {label}: {dc}")
            else:
                save_lines.append(f"• {label}: N/A")


        ab_list = self.classes.get(char_class.lower(), {}).get("ab", [])
        ab = ab_list[min(lvl - 1, len(ab_list) - 1)] if ab_list else 0


        embed = nextcord.Embed(
            title=f"{char_name}",
            description=f"*Level {lvl} {race} {cls} {cur_xp}/{next_req}*",
            color=random.randint(0, 0xFFFFFF)
        )

        hp_notes = []

        if (temp_con_loss > 0 or perm_con_loss > 0) and mhp_base != mhp:
            hp_notes.append(f"CON base {mhp_base}")

        if neg_pool > 0:
            hp_notes.append(f"NL cap {eff_cap}")

        hp_tail = f" ({'; '.join(hp_notes)})" if hp_notes else ""
        embed.add_field(name="HP", value=f"{hp}/{mhp}{hp_tail}", inline=True)
        embed.add_field(name="AC", value=f"{ac}", inline=True)
        embed.add_field(name="Attack Bonus", value=f"+{ab}", inline=True)
        embed.add_field(name="Abilities", value="\n".join(ability_lines), inline=False)
        embed.add_field(name="Saving Throw DCs", value="\n".join(save_lines), inline=False)


        THIEF_CAPABLE = {"thief", "magethief"}
        if (char_class.lower() in THIEF_CAPABLE):

            class_block = _CLASS_CACHE.get("Thief", {}) or _CLASS_CACHE.get("thief", {})


            races_cfg = load_races("race.lst")
            race_data = {}
            if race:
                for sec_name, sec in races_cfg.items():
                    if sec_name.lower() == race.lower():
                        race_data = sec
                        break


            race_bonus_raw = {k.lower(): v for k, v in race_data.items()}
            race_bonus = {}
            for k in ("openlock","removetrap","pickpocket","movesilently","climb","hide","listen"):
                kk = "climbwall" if k == "climb" else k
                try:
                    race_bonus[kk] = int(race_bonus_raw.get(k, 0))
                except (TypeError, ValueError):
                    race_bonus[kk] = 0


            thief_labels = {
                "openlock":     "Open Locks",
                "removetrap":   "Remove Traps",
                "pickpocket":   "Pick Pockets",
                "movesilently": "Move Silently",
                "climbwall":    "Climb Walls",
                "hide":         "Hide in Shadows",
                "listen":       "Listen",
            }


            idx = max(0, min(lvl - 1, 19))
            thief_lines = []
            for key, label in thief_labels.items():
                arr = class_block.get(key, [])
                if isinstance(arr, list) and arr:
                    base = int(arr[min(idx, len(arr) - 1)])
                    bonus = int(race_bonus.get(key, 0))
                    pen_pct = 5 * neg_levels
                    total = base + bonus
                    eff_total = max(0, total - pen_pct)
                    bonus_str = f" ({bonus:+})" if bonus else ""
                    pen_str   = f" (−{pen_pct}% drain)" if pen_pct else ""
                    thief_lines.append(f"• {label}: {eff_total}%{bonus_str}{pen_str}")

            embed.add_field(
                name="Thief Skills (d100 ≤ target)",
                value="\n".join(thief_lines),
                inline=False
            )


        if char_class.lower() == "ranger":

            ranger_block = _CLASS_CACHE.get("Ranger", {}) or _CLASS_CACHE.get("ranger", {})


            races_cfg = load_races("race.lst")
            race_data = {}
            if race:
                for sec_name, sec in races_cfg.items():
                    if sec_name.lower() == race.lower():
                        race_data = sec
                        break


            race_bonus_raw = {k.lower(): v for k, v in race_data.items()}
            race_bonus = {}
            for k in ("movesilently","hide","tracking"):
                try:
                    race_bonus[k] = int(race_bonus_raw.get(k, 0))
                except (TypeError, ValueError):
                    race_bonus[k] = 0

            ranger_labels = {
                "movesilently": "Move Silently",
                "hide":         "Hide in Shadows",
                "tracking":     "Tracking",
            }

            idx = max(0, min(lvl - 1, 19))
            lines = []
            for key, label in ranger_labels.items():
                arr = ranger_block.get(key, [])
                if isinstance(arr, list) and arr:
                    base = int(arr[min(idx, len(arr) - 1)])
                    bonus = int(race_bonus.get(key, 0))
                    pen_pct = 5 * neg_levels
                    total = base + bonus
                    eff_total = max(0, total - pen_pct)
                    bonus_str = f" ({bonus:+})" if bonus else ""

                    if key == "hide" and (race or "").lower() == "halfling":
                        nat = 70
                        if nat > total:
                            lines.append(f"• {label}: {nat}% (using natural; table {total}%{bonus_str})")
                            continue

                    pen_str   = f" (−{pen_pct}% drain)" if pen_pct else ""
                    lines.append(f"• {label}: {eff_total}%{bonus_str}{pen_str}")
                else:
                    lines.append(f"• {label}: N/A")

            embed.add_field(
                name="Ranger Skills (d100 ≤ target)",
                value="\n".join(lines),
                inline=False
            )


        if char_class.lower() == "scout":
            scout_block = _CLASS_CACHE.get("Scout", {}) or _CLASS_CACHE.get("scout", {})


            races_cfg = load_races("race.lst")
            race_data = {}
            if race:
                for sec_name, sec in races_cfg.items():
                    if sec_name.lower() == race.lower():
                        race_data = sec
                        break
            race_bonus_raw = {k.lower(): v for k, v in race_data.items()}
            race_bonus = {}

            for k in ("openlock","movesilently","climbwall","climb","hide","listen","tracking"):
                kk = "climbwall" if k == "climb" else k
                try:
                    race_bonus[kk] = int(race_bonus_raw.get(k, 0))
                except (TypeError, ValueError):
                    race_bonus[kk] = 0

            scout_labels = {
                "openlock":     "Open Locks",
                "movesilently": "Move Silently",
                "climbwall":    "Climb Walls",
                "hide":         "Hide in Shadows",
                "listen":       "Listen",
                "tracking":     "Tracking",
            }

            idx = max(0, min(lvl - 1, 19))
            lines = []
            for key, label in scout_labels.items():
                arr = scout_block.get(key, [])
                if isinstance(arr, list) and arr:
                    base = int(arr[min(idx, len(arr) - 1)])
                    bonus = int(race_bonus.get(key, 0))
                    pen_pct = 5 * neg_levels
                    total = base + bonus
                    eff_total = max(0, total - pen_pct)
                    bonus_str = f" ({bonus:+})" if bonus else ""

                    if key == "hide" and (race or "").lower() == "halfling":
                        nat = 70
                        if nat > total:
                            lines.append(f"• {label}: {nat}% (using natural; table {total}%{bonus_str})")
                            continue

                    pen_str   = f" (−{pen_pct}% drain)" if pen_pct else ""
                    lines.append(f"• {label}: {eff_total}%{bonus_str}{pen_str}")
                else:
                    lines.append(f"• {label}: N/A")

            embed.add_field(
                name="Scout Skills (d100 ≤ target)",
                value="\n".join(lines),
                inline=False
            )


        if char_class.lower() == "assassin":
            assassin_block = _CLASS_CACHE.get("Assassin", {}) or _CLASS_CACHE.get("assassin", {})


            races_cfg = load_races("race.lst")
            race_data = {}
            if race:
                for sec_name, sec in races_cfg.items():
                    if sec_name.lower() == race.lower():
                        race_data = sec
                        break
            race_bonus_raw = {k.lower(): v for k, v in race_data.items()}
            race_bonus = {}
            for k in ("openlock","pickpocket","movesilently","climbwall","climb","hide","listen","poison"):
                kk = "climbwall" if k == "climb" else k
                try:
                    race_bonus[kk] = int(race_bonus_raw.get(k, 0))
                except (TypeError, ValueError):
                    race_bonus[kk] = 0

            assassin_labels = {
                "openlock":     "Open Locks",
                "pickpocket":   "Pick Pockets",
                "movesilently": "Move Silently",
                "climbwall":    "Climb Walls",
                "hide":         "Hide in Shadows",
                "listen":       "Listen",
                "poison":       "Poison",
            }

            idx = max(0, min(lvl - 1, 19))
            lines = []
            for key, label in assassin_labels.items():
                arr = assassin_block.get(key, [])
                if isinstance(arr, list) and arr:
                    base = int(arr[min(idx, len(arr) - 1)])
                    bonus = int(race_bonus.get(key, 0))
                    pen_pct = 5 * neg_levels
                    total = base + bonus
                    eff_total = max(0, total - pen_pct)
                    bonus_str = f" ({bonus:+})" if bonus else ""

                    if key == "hide" and (race or "").lower() == "halfling":
                        nat = 70
                        if nat > total:
                            lines.append(f"• {label}: {nat}% (using natural; table {total}%{bonus_str})")
                            continue

                    pen_str   = f" (−{pen_pct}% drain)" if pen_pct else ""
                    lines.append(f"• {label}: {eff_total}%{bonus_str}{pen_str}")
                else:
                    lines.append(f"• {label}: N/A")

            embed.add_field(
                name="Assassin Skills (d100 ≤ target)",
                value="\n".join(lines),
                inline=False
            )


        a1 = get_compat(config, "eq", "armor1", fallback="").strip()
        a2 = get_compat(config, "eq", "armor2", fallback="").strip()
        armor_list = [x for x in (a1, a2) if x]
        armor_text = ", ".join(armor_list) if armor_list else "—"

        weap_list = _eq_get_weapons_from_cfg(config)
        weap_text = ", ".join(weap_list) if weap_list else "—"
        carry_list = _eq_get_carry_from_cfg(config)
        carry_text = ", ".join(carry_list) if carry_list else "—"


        eq_weight   = self._eq_weight_cached(config)
        coin_weight = self._coin_weight_from_cfg(config)
        total_w     = eq_weight + coin_weight


        try:
            coin_weight = float(str(get_compat(config, "eq", "coin_weight", fallback="0")).strip() or "0")
        except Exception:
            coin_weight = 0.0


        STR_stat = getint_compat(config, "stats", "str", fallback=10)
        race_name = get_compat(config, "info", "race", fallback="Human")
        light_lim, heavy_lim = _weight_thresholds(race_name, STR_stat)
        if total_w <= light_lim:   load_label = "light"
        elif total_w <= heavy_lim: load_label = "heavy"
        else:                      load_label = "overencumbered"


        embed.add_field(
            name="Weight",
            value=f"{total_w:.1f} (**{load_label}** load)\n*(gear: {eq_weight:.1f} • coins: {coin_weight:.1f})*",
            inline=True
        )
        embed.add_field(name="Move", value=f"{move}", inline=True)


        portrait_url = (get_compat(config, "info", "portrait", fallback="") or "").strip()
        if portrait_url:
            try:
                embed.set_thumbnail(url=portrait_url)
            except Exception:

                pass


        owner_user = ctx.author


        try:
            if owner_id and str(owner_id).isdigit():
                uid = int(owner_id)
                if ctx.guild:
                    m = ctx.guild.get_member(uid)
                    if m:
                        owner_user = m
                    else:

                        owner_user = await self.bot.fetch_user(uid)
                else:
                    owner_user = await self.bot.fetch_user(uid)
        except Exception:
            pass

        owner_name = getattr(owner_user, "display_name", getattr(owner_user, "name", "Player"))


        try:
            embed.set_author(name=owner_name, icon_url=owner_user.display_avatar.url)
        except Exception:

            try:
                embed.set_author(name=owner_name, icon_url=owner_user.avatar.url)
            except Exception:
                embed.set_author(name=owner_name)


        if temp_con_loss > 0 or perm_con_loss > 0:
            con_lines = [f"**{base_con} → {cur_con}**  •  temp −{temp_con_loss}, perm −{perm_con_loss}"]
            if mhp_base != mhp:
                con_lines.append(f"Max HP: **{mhp_base} → {mhp}** (from CON)")
            embed.add_field(name="Constitution", value="\n".join(con_lines), inline=False)


        if neg_levels > 0:
            embed.add_field(
                name="Conditions",
                value=f"Negative Levels: **{neg_levels}**  •  d20 penalty **-{neg_levels}**  •  percentile **-{5*neg_levels}%**",
                inline=False
            )


        try:
            if _poly_active(config):
                form = get_compat(config, "poly", "form", fallback="?")
                naturals = []

                raw = (get_compat(config, "poly", "attacks", fallback="") or "").strip()
                if raw:
                    for seg in re.split(r"[|,]", raw):
                        seg = seg.strip()
                        if ":" in seg:
                            k, v = seg.split(":", 1)
                            naturals.append(f"{k.strip().capitalize()} {v.strip()}")
                else:
                    for k in ("bite","claw","hug","gore","slam","sting","horns"):
                        v = (get_compat(config, "poly", k, fallback="") or "").strip()
                        if v:
                            naturals.append(f"{k.capitalize()} {v}")

                extra = ("\n• " + ", ".join(naturals)) if naturals else ""
                embed.add_field(name="Polymorph", value=f"{form}{extra}", inline=False)
        except Exception:
            pass


        await ctx.send(embed=embed)


    @commands.command(name="givecoin", aliases=["gcoin","gcoins"])
    async def givecoin(self, ctx, who: str, *, amounts: str):
        """
        Give coins to another character.
        Usage:
          !givecoin <who> 3pp 1gp 14sp
          !givecoin <who> 250gp
          !givecoin <who> all
        Notes:
          • Normalizes denominations on both source and target
          • Updates [eq].coin_weight for both
        """
        import os, re

        def _resolve_char_ci(name: str):
            base = name.replace(" ", "_")
            target = f"{base}.coe".lower()
            for fn in os.listdir("."):
                if fn.lower() == target:
                    path = fn
                    try:
                        cfg2 = read_cfg(path)
                        real = get_compat(cfg2, "info", "name", fallback=None)
                        return (real or fn[:-4].replace("_"," ")), path
                    except Exception:
                        return fn[:-4].replace("_"," "), path
            return None, None

        def _parse_amounts(s: str) -> dict | str:
            """
            Returns dict {'pp':int,...} or the string 'ALL'.
            Accepts tokens like: 3pp, 10 gp, 250gp, 14sp 8cp
            """
            if not s or not s.strip():
                return {}
            if s.strip().lower() in {"all","*"}:
                return "ALL"
            pp=gp=ep=sp=cp=0
            for tok in re.findall(r"([+-]?\d+)\s*(pp|gp|ep|sp|cp)\b", s.lower()):
                n, d = tok
                try:
                    n = int(n)
                except Exception:
                    continue
                if d == "pp": pp += n
                elif d == "gp": gp += n
                elif d == "ep": ep += n
                elif d == "sp": sp += n
                elif d == "cp": cp += n

            for tok in re.findall(r"\b([+-]?\d+)(pp|gp|ep|sp|cp)\b", s.lower()):
                pass
            return {"pp":pp,"gp":gp,"ep":ep,"sp":sp,"cp":cp}


        src_name = get_active(ctx.author.id)
        if not src_name:
            return await ctx.send("❌ No active character. Use `!char <name>` first.")
        src_path = f"{src_name.replace(' ', '_')}.coe"
        if not os.path.exists(src_path):
            return await ctx.send(f"❌ Character file not found for **{src_name}**.")
        dst_name, dst_path = _resolve_char_ci(who)
        if not dst_path:
            return await ctx.send(f"❌ Character '{who}' not found.")

        src = read_cfg(src_path)
        dst = read_cfg(dst_path)

        owner_id = get_compat(src, "info", "owner_id", fallback="")
        if owner_id and str(owner_id) != str(ctx.author.id) and not getattr(ctx.author.guild_permissions, "manage_guild", False):
            return await ctx.send(f"❌ You must own **{src_name}** or have Manage Server to give coins.")

        spec = _parse_amounts(amounts)
        if not spec:
            return await ctx.send("❌ Couldn’t parse amounts. Try: `!givecoin testman 3pp 1gp 14sp` or `!givecoin testman all`.")


        def _wallet_cp(cfg):
            pp = getint_compat(cfg, "cur", "pp", fallback=0)
            gp = getint_compat(cfg, "cur", "gp", fallback=0)
            ep = getint_compat(cfg, "cur", "ep", fallback=0)
            sp = getint_compat(cfg, "cur", "sp", fallback=0)
            cp = getint_compat(cfg, "cur", "cp", fallback=0)
            return pp*1000 + gp*100 + ep*50 + sp*10 + cp

        src_cp = _wallet_cp(src)
        if spec == "ALL":
            move_cp = src_cp
        else:
            move_cp = (
                spec.get("pp",0)*1000 +
                spec.get("gp",0)*100 +
                spec.get("ep",0)*50 +
                spec.get("sp",0)*10 +
                spec.get("cp",0)
            )
            if move_cp <= 0:
                return await ctx.send("❌ Amount must be positive.")
            if move_cp > src_cp:
                short_gp = (move_cp - src_cp)/100.0
                return await ctx.send(f"❌ Not enough funds. Short by **{short_gp:.2f} gp**.")


        def _set_wallet_from_cp(cfg, total_cp):
            if total_cp < 0: total_cp = 0
            pp = total_cp // 1000; rem = total_cp % 1000
            gp = rem // 100;      rem = rem % 100
            ep = rem // 50;       rem = rem % 50
            sp = rem // 10;       cp = rem % 10
            if not cfg.has_section("cur"): cfg.add_section("cur")
            cfg.set("cur","pp",str(pp)); cfg.set("cur","gp",str(gp))
            cfg.set("cur","ep",str(ep)); cfg.set("cur","sp",str(sp)); cfg.set("cur","cp",str(cp))

            coin_w = (pp + gp + ep + sp + cp) * 0.02
            if not cfg.has_section("eq"): cfg.add_section("eq")
            cfg.set("eq","coin_weight", f"{coin_w:.2f}")
            return pp,gp,ep,sp,cp,coin_w

        new_src_total = src_cp - move_cp
        new_dst_total = _wallet_cp(dst) + move_cp

        s_pp,s_gp,s_ep,s_sp,s_cp,s_wt = _set_wallet_from_cp(src, new_src_total)
        d_pp,d_gp,d_ep,d_sp,d_cp,d_wt = _set_wallet_from_cp(dst, new_dst_total)


        try: self._recompute_eq_weight(src)
        except Exception: pass
        try: self._recompute_eq_weight(dst)
        except Exception: pass

        try: self._recompute_move(src)
        except Exception: pass
        try: self._recompute_move(dst)
        except Exception: pass

        write_cfg(src_path, src)
        write_cfg(dst_path, dst)

        def _fmt_wallet(pp,gp,ep,sp,cp):
            return f"pp:{pp} gp:{gp} ep:{ep} sp:{sp} cp:{cp}"

        amt_gp = move_cp/100.0
        await ctx.send(
            f"💰 **{src_name}** gives **{amt_gp:.2f} gp** to **{dst_name}**.\n"
            f"New wallets — {src_name}: {_fmt_wallet(s_pp,s_gp,s_ep,s_sp,s_cp)} • {dst_name}: {_fmt_wallet(d_pp,d_gp,d_ep,d_sp,d_cp)}"
        )


    @commands.command(name="coins")
    async def coins(self, ctx, *adjust):
        """
        Show and modify your coins.
        Usage:
          !coins                      -> show purse + totals
          !coins +30                  -> add 30 gp
          !coins -5                   -> remove 5 gp (clamped at 0)
          !coins +30pp / +30 pp       -> add 30 platinum
          !coins -12 sp / -12 silver  -> remove 12 silver
        Units: pp/platinum, gp/gold, ep/electrum, sp/silver, cp/copper
        """
        import re, random, nextcord


        char_name = get_active(ctx.author.id)
        if not char_name:
            await ctx.send("❌ No active character. Use `!char <name>` first.")
            return

        path = f"{char_name.replace(' ', '_')}.coe"
        try:
            cfg = read_cfg(path)
        except Exception:
            await ctx.send(f"❌ Couldn’t read **{char_name}**’s file.")
            return

        owner_id = get_compat(cfg, "info", "owner_id", fallback="")
        if owner_id and owner_id != str(ctx.author.id):
            await ctx.send(f"❌ You do not own **{char_name}**.")
            return


        if not cfg.has_section("cur"):
            cfg.add_section("cur")


        pp = getint_compat(cfg, "cur", "pp", fallback=0)
        gp = getint_compat(cfg, "cur", "gp", fallback=0)
        ep = getint_compat(cfg, "cur", "ep", fallback=0)
        sp = getint_compat(cfg, "cur", "sp", fallback=0)
        cp = getint_compat(cfg, "cur", "cp", fallback=0)


        change_note = None
        if adjust:
            raw = " ".join(adjust).strip().lower()
            m = re.fullmatch(r"\s*([+-])\s*(\d+)\s*([a-z]+)?\s*", raw)
            if not m:
                await ctx.send("❌ Bad format. Try `!coins`, `!coins +30`, `!coins -5 sp`, or `!coins +10pp`.")
                return

            sign, amt_s, unit = m.groups()
            amt = int(amt_s)
            unit = (unit or "gp").strip().lower()

            unit_map = {
                "pp": "pp", "platinum": "pp", "plat": "pp",
                "gp": "gp", "gold": "gp",
                "ep": "ep", "electrum": "ep",
                "sp": "sp", "silver": "sp",
                "cp": "cp", "copper": "cp",
            }
            key = unit_map.get(unit)
            if not key:
                await ctx.send("❌ Unknown coin type. Use pp/gp/ep/sp/cp (or platinum/gold/electrum/silver/copper).")
                return

            delta = amt if sign == "+" else -amt
            before = {"pp": pp, "gp": gp, "ep": ep, "sp": sp, "cp": cp}[key]
            after = max(0, before + delta)

            if key == "pp": pp = after
            elif key == "gp": gp = after
            elif key == "ep": ep = after
            elif key == "sp": sp = after
            else:            cp = after

            cfg.set("cur", "pp", str(pp))
            cfg.set("cur", "gp", str(gp))
            cfg.set("cur", "ep", str(ep))
            cfg.set("cur", "sp", str(sp))
            cfg.set("cur", "cp", str(cp))

            sym = "+" if delta >= 0 else "-"
            change_note = f"{sym}{abs(delta)} {key.upper()} (applied {after - before:+})"


        total_gp = (pp * 10) + gp + (ep * 0.5) + (sp * 0.1) + (cp * 0.01)
        total_coins = pp + gp + ep + sp + cp
        coin_weight = total_coins * 0.02


        if not cfg.has_section("eq"):
            cfg.add_section("eq")
        cfg.set("eq", "coin_weight", f"{coin_weight:.2f}")


        try: self._recompute_eq_weight(cfg)
        except Exception: pass
        try: self._recompute_move(cfg)
        except Exception: pass

        write_cfg(path, cfg)


        embed = nextcord.Embed(
            title=f"{char_name}'s Coinpurse",
            color=random.randint(0, 0xFFFFFF)
        )
        embed.description = "\n".join([
            f":pound: **{pp}** platinum",
            f":coin: **{gp}** gold",
            f":euro: **{ep}** electrum",
            f":dollar: **{sp}** silver",
            f":yen: **{cp}** copper",
        ])
        embed.add_field(name="Total Value", value=f":purse: **{total_gp:.2f}** GP", inline=True)
        embed.add_field(name="Coin Weight", value=f":moneybag: **{coin_weight:.2f}**", inline=True)

        if change_note:
            embed.set_footer(text=f"Adjusted: {change_note}")


        try:
            if _poly_active(cfg):
                form = get_compat(cfg, "poly", "form", fallback="?")
                naturals = []
                for k in ("bite","claw","hug","gore","slam","sting"):
                    v = (get_compat(cfg, "poly", k, fallback="") or "").strip()
                    if v:
                        naturals.append(f"{k.capitalize()} {v}")
                extra = ("\n• " + ", ".join(naturals)) if naturals else ""
                embed.add_field(name="Polymorph", value=f"{form}{extra}", inline=False)
        except Exception:
            pass

        await ctx.send(embed=embed)


    def _safe_float(self, v, default=0.0):
        try:
            return float(v)
        except Exception:
            return default

    def _eq_weight_cached(self, cfg) -> float:

        w = get_compat(cfg, "eq", "weight", fallback="0")
        return self._safe_float(w, 0.0)

    def _coin_weight_from_cfg(self, cfg) -> float:
        """Each coin weighs 0.02 lb."""
        pp = getint_compat(cfg, "cur", "pp", fallback=0)
        gp = getint_compat(cfg, "cur", "gp", fallback=0)
        ep = getint_compat(cfg, "cur", "ep", fallback=0)
        sp = getint_compat(cfg, "cur", "sp", fallback=0)
        cp = getint_compat(cfg, "cur", "cp", fallback=0)
        return (pp + gp + ep + sp + cp) * 0.02

    def _weights_snapshot(self, cfg):
        """
        Returns (eq_weight, coin_weight, total_weight),
        where total = equipped + coins.
        """
        eq_w   = self._eq_weight_cached(cfg)
        coin_w = self._coin_weight_from_cfg(cfg)
        return (eq_w, coin_w, eq_w + coin_w)

    def _fmt_num(self, value, places=1):

        from decimal import Decimal, ROUND_HALF_UP
        q = Decimal('1').scaleb(-places)
        d = Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP)
        s = format(d, 'f')
        if '.' in s:
            s = s.rstrip('0').rstrip('.')
        return s

    def _fmt_w(self, v) -> str:
        return self._fmt_num(v, 1)

    def _fmt_coin_w(self, v) -> str:
        return self._fmt_num(v, 1)


def setup(bot):
    bot.add_cog(StatsCog(bot))
