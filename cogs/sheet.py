import configparser
import os
import re
import random
import nextcord
import re
import glob
from nextcord.ext import commands
from pathlib import Path
from utils.ini import read_cfg, write_cfg, get_compat, getint_compat
from utils.players import add_char, set_active, get_active
from utils.players import remove_char, find_owner_by_char

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9 _-]+")

def _safe_char_basename(raw: str) -> str:
    """
    Convert a display name -> safe filename base in the current directory.
    Allows letters, digits, space, underscore, dash. Collapses whitespace to '_'.
    Strips leading/trailing dot/underscore/dash. Caps length to 64.
    """
    s = _SAFE_NAME_RE.sub("", str(raw))
    s = re.sub(r"\s+", "_", s).strip("._-")
    if not s:
        raise ValueError("Character name must contain letters or digits.")
    return s[:64]
    

def osr_modifier(score: int) -> int:
    s = max(1, int(score))
    if s <= 3:   return -3
    if s <= 5:   return -2
    if s <= 8:   return -1
    if s <= 12:  return 0
    if s <= 15:  return +1
    if s <= 17:  return +2
    return +3


calculate_modifier = osr_modifier


LOADOUTS = {
    "Fighter":     {"armor1": "ChainMail",    "armor2": "Shield",      "weapons": ["Spear"]},
    "Cleric":      {"armor1": "LeatherArmor", "armor2": "",            "weapons": ["Club", "Sling"]},
    "Druid":       {"armor1": "LeatherArmor", "armor2": "",            "weapons": ["Club", "Sling"]},
    "Thief":       {"armor1": "LeatherArmor", "armor2": "",            "weapons": ["Shortsword", "Shortbow"]},
    "Magic-User":  {"armor1": "",             "armor2": "",            "weapons": ["Dagger"]},
    "Illusionist": {"armor1": "",             "armor2": "",            "weapons": ["Dagger"]},
    "Necromancer": {"armor1": "",             "armor2": "",            "weapons": ["Dagger"]},
    "Spellcrafter":{"armor1": "",             "armor2": "",            "weapons": ["Dagger"]},
    "Fightermage": {"armor1": "LeatherArmor", "armor2": "",            "weapons": ["Longsword"]},
    "Magethief":   {"armor1": "LeatherArmor", "armor2": "",            "weapons": ["Dagger", "Shortbow"]},
    "Barbarian":   {"armor1": "LeatherArmor", "armor2": "",            "weapons": ["BattleAxe"]},
    "Ranger":      {"armor1": "LeatherArmor", "armor2": "",            "weapons": ["Shortsword", "Longbow"]},
    "Scout":       {"armor1": "LeatherArmor", "armor2": "",            "weapons": ["Dagger", "LightXbow"]},
    "Assassin":    {"armor1": "LeatherArmor", "armor2": "",            "weapons": ["Dagger"]},
    "Paladin":     {"armor1": "ChainMail",    "armor2": "Shield",      "weapons": ["Longsword"]},
}
LOADOUTS.update({
    "Fightermage": {"armor1": "LeatherArmor", "armor2": "", "weapons": ["Longsword"]},
    "Magethief":   {"armor1": "LeatherArmor", "armor2": "", "weapons": ["Dagger", "Shortbow"]},
})

ALWAYS_LIST = {"skills", "banned", "banned_weapons"}

def is_guild_owner():
    async def predicate(ctx):
        return ctx.guild is not None and ctx.author.id == ctx.guild.owner_id
    return commands.check(predicate)


def _roll_3d6() -> int:
    return sum(random.randint(1, 6) for _ in range(3))

def _roll_4d6_drop_lowest() -> int:
    rolls = [random.randint(1, 6) for _ in range(4)]
    rolls.remove(min(rolls))
    return sum(rolls)

def _hr_enabled_anywhere(channel, key: str, default: bool = True) -> bool:
    """
    Read a house-rule toggle set by the combat cog:
      section = <chan_base>:hr  (same convention as in combat.py)
    Falls back to `default` if missing.
    """
    try:
        cfg = _load_battles()
        sec = f"{_section_id(channel)}:hr"
        if not cfg.has_section(sec):
            return default
        return cfg.getboolean(sec, key, fallback=default)
    except Exception:
        return default


def standardize_class_name(char_class: str) -> str:
    m = (char_class or "").strip().lower()
    aliases = {
        "fighter": "Fighter",
        "cleric": "Cleric",
        "magic-user": "Magic-User",
        "mu": "Magic-User",
        "thief": "Thief",
        "fightermage": "Fightermage",
        "fighter/magic-user": "Fightermage",
        "fighter-mage": "Fightermage",
        "fm": "Fightermage",
        "magethief": "Magethief",
        "mage/thief": "Magethief",
        "illusionist/thief": "Magethief",
        "mage-thief": "Magethief",
        "mt": "Magethief",
    }
    return aliases.get(m, char_class.title())


def is_level_one(config: configparser.ConfigParser) -> bool:
    try:
        return int(config['cur'].get('level', 0)) == 1
    except Exception:
        return False


def validate_classes(path="class.lst"):
    cfg = configparser.ConfigParser(); cfg.optionxform = str; cfg.read(path)
    errors = []
    for sec in cfg.sections():

        xp_pairs = sorted(
            ((int(m.group(1)), int(v.strip()))
             for k, v in cfg.items(sec)
             if (m := re.fullmatch(r"xp(\d+)", k, flags=re.I)) and v.strip().lstrip("-").isdigit()),
            key=lambda kv: kv[0]
        )
        last = 0
        for lvl, req in xp_pairs:
            if req < last:
                errors.append(f"[{sec}] xp{lvl}={req} < previous ({last})")
            last = req


        for k, v in cfg.items(sec):
            vals = v.split()
            if k.lower() in {"ab","poi","wand","para","breath","spell",
                             "openlock","removetrap","pickpocket",
                             "movesilently","climbwall","hide","listen",
                             "spell1","spell2","spell3","spell4","spell5","spell6","spell7"}:
                if len(vals) != 20:
                    errors.append(f"[{sec}] {k} has {len(vals)} values (need 20)")
    return errors


def _resolve_char_ci(name: str):
    try:
        base = _safe_char_basename(name)
    except Exception:
        return None, None

    target = f"{base}.coe".lower()
    for fn in os.listdir("."):
        if fn.lower() == target:
            path = fn  
            try:
                cfg = read_cfg(path)
                real = get_compat(cfg, "info", "name", fallback=None)
                return (real or fn[:-4].replace("_", " ")), path
            except Exception:
                return fn[:-4].replace("_", " "), path
    return None, None


def _normalize_name(s: str) -> str:
    return "".join(ch.lower() for ch in str(s) if ch.isalnum())

def _ability_mod(v: int) -> int:
    return osr_modifier(v)

_STAT_ALIASES = {
    "str":"str","strength":"str",
    "dex":"dex","dexterity":"dex",
    "con":"con","constitution":"con",
    "int":"int","intelligence":"int",
    "wis":"wis","wisdom":"wis",
    "cha":"cha","charisma":"cha",
}

def _norm_stat_key(name: str) -> str | None:
    if not name: return None
    key = str(name).strip().lower()
    return _STAT_ALIASES.get(key)


def _load_item_index(path: str = "item.lst"):
    """
    Return (items, index) where:
      items: { "Longsword": {"price":"10","weight":"4", ...}, ... }
      index: { "longsword": "Longsword", "long sword": "Longsword", ... }
    Uses optional 'aliases=' in item.lst.
    """
    cfg = configparser.ConfigParser(); cfg.optionxform = str; cfg.read(path)
    items, index = {}, {}
    for section in cfg.sections():
        data = dict(cfg.items(section))
        items[section] = data
        index[_normalize_name(section)] = section
        aliases = re.split(r"[,\s]+", data.get("aliases", "").strip())
        for a in filter(None, aliases):
            index[_normalize_name(a)] = section
    return items, index


STARTER_KIT = [
    ("Torch", 6), ("Tinderbox", 1), ("Waterskin", 1), ("WinterBlanket", 1), ("Rations", 7),
    ("Chalk", 1), ("GrapplingHook", 1), ("HempRope", 1), ("Lantern", 1), ("Oil", 3), ("Arrow", 10), ("Bolt", 10), ("Bullet", 10),
    ("Bedroll", 1), ("GlassBottle", 1), ("IronSpikes", 12), ("10FtPole", 1), ("ScrollCase", 1), ("Mirror", 1),
]

CLASS_EXTRA_GEAR: dict[str, list[tuple[str, int]]] = {
    "fighter":      [("Whetstone", 1), ("Quiver", 1), ("Razor", 1)],
    "barbarian":    [("Whetstone", 1), ("Chisel", 1)],
    "ranger":       [("Quiver", 1), ("Hammock", 1)],
    "paladin":      [("WoodenStake", 1), ("HolySymbol", 1)],
    "cleric":       [("Bandages", 1), ("TravelAltar", 1), ("HolySymbol", 1), ("PrayerBook", 1)],
    "druid":        [("Incense", 1), ("Candles", 1), ("HolySymbol", 1), ("TarotCards", 1)],
    "thief":        [("Quiver", 1), ("ThievesTools", 1), ("BagofMarbles", 1), ("Crowbar", 1)],
    "scout":        [("Quiver", 1), ("ClimbingTools", 1)],
    "assassin":     [("Quiver", 1), ("DisguiseKit", 1)],
    "magic-user":   [("Quill", 1), ("InkJar", 1), ("Spellbook", 1), ("Candles", 1)],
    "illusionist":  [("Quill", 1), ("InkJar", 1), ("Spellbook", 1), ("Toy", 1)],
    "necromancer":  [("Quill", 1), ("InkJar", 1), ("Spellbook", 1), ("Hourglass", 1)],
    "spellcrafter": [("Quill", 1), ("InkJar", 1), ("Spellbook", 1), ("Acid", 1), ("Bucket", 1)],
    "fightermage":  [("Quill", 1), ("InkJar", 1), ("Spellbook", 1), ("Whetstone", 1), ("Quiver", 1)],
    "magethief":    [("Quill", 1), ("InkJar", 1), ("Spellbook", 1), ("ThievesTools", 1), ("Quiver", 1)],
}


def load_races(file_path):
    cfg = configparser.ConfigParser(); cfg.optionxform = str; cfg.read(file_path)
    races = {}
    for section in cfg.sections():
        data = {}
        for key, value in cfg.items(section):
            val = value.strip()
            tokens = [t for t in re.split(r"[,\s]+", val) if t]
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


def load_classes(file_path):
    config = configparser.ConfigParser(); config.optionxform = str; config.read(file_path)
    classes = {}
    for section in config.sections():
        classes[section] = {}
        for key, value in config[section].items():
            if " " in value:
                try:
                    classes[section][key] = [int(v) for v in value.split()]
                except ValueError:
                    classes[section][key] = value
            else:
                try:
                    classes[section][key] = int(value)
                except ValueError:
                    classes[section][key] = value
    return classes


class_data = load_classes('class.lst')
race_data  = load_races('race.lst')
# Canonical class names pulled from class.lst
VALID_CLASS_NAMES = {name.strip().lower() for name in class_data.keys()}


def get_race(races_cfg, race_name):
    for key in races_cfg:
        if key.lower() == race_name.lower():
            return races_cfg[key]
    raise ValueError(f"Race '{race_name}' not found!")


def roll_3d6() -> int:
    return sum(random.randint(1, 6) for _ in range(3))


def roll_stat() -> int:
    rolls = [random.randint(1, 6) for _ in range(4)]
    return sum(sorted(rolls)[1:])


def roll_dice(spec: str):
    m = re.fullmatch(r"\s*(\d+)d(\d+)\s*([+-]\s*\d+)?\s*", spec.strip().lower())
    if not m:
        raise ValueError(f"Bad dice spec: {spec}")
    n = int(m.group(1)); sides = int(m.group(2))
    flat = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    rolls = [random.randint(1, sides) for _ in range(n)]
    return sum(rolls), rolls, flat


def _load_class_cache(path: str = "class.lst"):
    cfg = configparser.ConfigParser(); cfg.optionxform = str; cfg.read(path)
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


def _safe_int(x, d=0):
    try: return int(str(x).strip())
    except: return d

def _get_pc_cfg(self, ctx, pc_name: str | None):
    if not pc_name:
        pc_name = get_active(ctx.author.id)
        if not pc_name:
            return (None, None, None)
    disp, path = _resolve_char_ci(pc_name)
    if not path:
        return (disp or pc_name, None, None)
    return (disp or pc_name, path, read_cfg(path))

def _retainer_cap(pc_cfg) -> int:
    return max(0, 4 + _safe_int(pc_cfg.get('stats','cha_modifier',fallback="0")))

def _count_retainers_for_pc(employer_char: str, employer_owner_id: str | int | None = None) -> int:
    cnt = 0
    owner_s = str(employer_owner_id) if employer_owner_id is not None else None
    for path in glob.glob("*.coe"):
        try:
            cfg = read_cfg(path)
            if not cfg.has_section('npc'):
                continue
            if cfg.get('npc', 'type', fallback='').strip().lower() != 'retainer':
                continue
            if cfg.get('npc', 'employer_char', fallback='').strip() != employer_char.strip():
                continue
            if owner_s is not None and cfg.get('npc', 'employer_owner_id', fallback='').strip() != owner_s:
                continue
            cnt += 1
        except Exception:
            pass
    return cnt


def _reaction_band(total: int):

    if total <= 2:  return ("refusal_hard","Refusal (ill words spread).", False)
    if total <= 5:  return ("refusal","Refusal.", False)
    if total <= 8:  return ("try_again","Reluctant: sweeten with ±N and reroll.", False)
    if total <= 11: return ("accept","Acceptance.", False)
    return ("accept","Acceptance — impressed! (+1 Loyalty)", True)


class SheetCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.races_cfg = load_races("race.lst")
        self.items, self.item_index = _load_item_index("item.lst")


    def _canon(self, name: str) -> str:
        return self.item_index.get(_normalize_name(name), name)

    def _get_char_race_class(self, cfg):
        race = get_compat(cfg, "info", "race", fallback="Human")
        cls  = get_compat(cfg, "info", "class", fallback="Fighter")
        return race.strip().lower(), cls.strip().lower()


    def _barbarian_unarmored_ac(self, level: int) -> int:
        if level <= 1: return 12
        if level == 2: return 13
        if level == 3: return 14
        if level == 4: return 16
        if level == 5: return 17
        if level == 6: return 18
        return 20

    def _truthy(self, v) -> bool:
        return str(v).strip().lower() in {"$true", "true", "1", "yes", "y"}

    def _item_lookup(self, name: str):
        if not name:
            return "", {}
        canon = self._canon(name)
        return canon, (self.items.get(canon, {}) or {})

    def _weight_thresholds(self, race_name: str, STR: int):
        s = max(3, min(int(STR or 10), 18))
        small = str(race_name).strip().lower() == "halfling"
        if s == 3:        med = (25, 60);  sml = (20, 40)
        elif s in (4, 5): med = (35, 90);  sml = (30, 60)
        elif 6 <= s <= 8: med = (50, 120); sml = (40, 80)
        elif 9 <= s <=12: med = (60, 150); sml = (50, 100)
        elif 13<= s <=15: med = (65, 165); sml = (55, 110)
        elif s in (16,17):med = (70, 180); sml = (60, 120)
        else:             med = (80, 195); sml = (65, 130)
        return sml if small else med

    def _armor_row_speeds(self, armor_name: str, armor_item: dict | None = None):
        name = (armor_name or "").lower()
        is_leather = "leather" in name
        is_metal   = any(w in name for w in ("mail", "plate", "metal"))
        magic = self._truthy((armor_item or {}).get("magic", "")) or name.startswith("magic ")
        if not armor_name:               return (40, 30)
        if is_leather and magic:         return (40, 30)
        if is_leather:                   return (30, 20)
        if is_metal and magic:           return (30, 20)
        if is_metal:                     return (20, 10)
        return (30, 20)

    def _recompute_eq_weight(self, cfg) -> float:
        total = 0.0
        a1 = get_compat(cfg, "eq", "armor1", fallback="").strip()
        a2 = get_compat(cfg, "eq", "armor2", fallback="").strip()
        for n in (a1, a2):
            if not n: continue
            _c, it = self._item_lookup(n)
            try:
                total += float((it.get("weight", "") or "0").strip())
            except Exception:
                pass
        if cfg.has_section("eq"):
            for k, v in cfg.items("eq"):
                if re.fullmatch(r"weapon\d+", k, flags=re.I):
                    nm = (v or "").strip()
                    if not nm: continue
                    _c, it = self._item_lookup(nm)
                    try:
                        total += float((it.get("weight", "") or "0").strip())
                    except Exception:
                        pass
                if re.fullmatch(r"carry\d+", k, flags=re.I):
                    nm = (v or "").strip()
                    if not nm: continue
                    _c, it = self._item_lookup(nm)
                    try:
                        total += float((it.get("weight", "") or "0").strip())
                    except Exception:
                        pass
        pp = getint_compat(cfg, "cur", "pp", fallback=0)
        gp = getint_compat(cfg, "cur", "gp", fallback=0)
        ep = getint_compat(cfg, "cur", "ep", fallback=0)
        sp = getint_compat(cfg, "cur", "sp", fallback=0)
        cp = getint_compat(cfg, "cur", "cp", fallback=0)
        coin_w = (pp + gp + ep + sp + cp) * 0.02
        return round(total + coin_w, 2)

    def _recompute_move(self, cfg) -> int:
        STR = getint_compat(cfg, "stats", "str", fallback=10)
        race_lc, class_lc = self._get_char_race_class(cfg)
        light_lim, heavy_lim = self._weight_thresholds(race_lc, STR)
        armor1 = ""; armor1_item = None
        try:
            if cfg.has_option("eq", "armor1"):
                armor1 = cfg.get("eq", "armor1").strip()
                if armor1:
                    _c, armor1_item = self._item_lookup(armor1)
        except Exception:
            pass
        light_spd, heavy_spd = self._armor_row_speeds(armor1, armor1_item)
        try:
            eqw = float(str(get_compat(cfg, "stats", "eq_weight", fallback="")).strip() or "0")
        except Exception:
            eqw = self._recompute_eq_weight(cfg)
        if eqw <= light_lim:      mv = light_spd + 10
        elif eqw <= heavy_lim:    mv = light_spd
        else:                     mv = heavy_spd
        if class_lc == "barbarian":
            mv += 5
        if not cfg.has_section("stats"): cfg.add_section("stats")
        cfg.set("stats", "move", str(mv))
        return mv

    def _bump_die(self, die: int) -> int:
        order = [4, 6, 8, 10, 12]
        try:
            i = order.index(die)
            return order[min(i + 1, len(order) - 1)]
        except ValueError:
            return min(max(die + 2, 4), 12)

    def _hit_die_for(self, char_class: str, race: str | None = None) -> int:
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
        if race_lc in {"half-ogre", "halfogre"}:
            die = self._bump_die(die)
        try:
            rinfo = {}
            races_cfg = getattr(self, "races_cfg", None)
            if races_cfg and race:
                # get_race already does a case-insensitive match
                rinfo = get_race(races_cfg, race)

            if "frail" in str(rinfo.get("skills", "")).lower():
                # Racial Frail cap: never better than d6, regardless of class HD
                die = min(die, 6)
        except Exception:
            pass
        return int(die)


    def calculate_hp(self, char_class: str, con_modifier, race: str | None = None) -> tuple[int, int]:
        cm = self._coerce_int(con_modifier, 0)
        max_die = self._hit_die_for(char_class, race)
        roll = random.randint(1, max_die)
        return max(1, roll + cm), roll

    def starting_hp(self, char_class: str, con_modifier, race: str | None = None) -> tuple[int, int]:
        cm = self._coerce_int(con_modifier, 0)
        max_die = self._hit_die_for(char_class, race)
        roll = max_die
        return max(1, roll + cm), roll

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


    def _hp_flat_after9(self, char_class: str) -> int:
        cls = (char_class or "").strip()
        sec = _CLASS_CACHE.get(cls, {}) or _CLASS_CACHE.get(cls.title(), {}) or _CLASS_CACHE.get(cls.lower(), {})
        try:
            hp20 = sec.get("hp20")
            if isinstance(hp20, int):
                return hp20
            if isinstance(hp20, str) and hp20.strip().lstrip("-").isdigit():
                return int(hp20.strip())
        except Exception:
            pass
        c = cls.lower()
        if c in {"fighter","paladin","ranger","barbarian"}: return 2
        if c in {"cleric","druid","scout"}: return 1
        return 1


    def _effective_max_hp(self, cfg) -> int:
        mhp = getint_compat(cfg, "max", "hp", fallback=0)
        lost = getint_compat(cfg, "cur", "neg_hp_loss_total", fallback=0)
        return max(0, mhp - max(0, lost))

    def _clamp_hp_to_cap(self, cfg, path: str) -> None:
        cap = self._effective_max_hp(cfg)
        cur = getint_compat(cfg, "cur", "hp", fallback=0)
        if cur > cap:
            if not cfg.has_section("cur"): cfg.add_section("cur")
            cfg["cur"]["hp"] = str(cap)
            write_cfg(path, cfg)


    def _stat_bounds(self, race_name: str, class_name: str) -> tuple[dict, dict]:
        """
        Returns (stat_min, stat_max) dicts for STR/DEX/CON/INT/WIS/CHA,
        combining race.lst and class.lst:
          - min = max(race_min, class_min, 3)
          - max = min(race_max, class_max, 18)
        """
        import re


        stats = ["str","dex","con","int","wis","cha"]
        stat_min = {s: 3 for s in stats}
        stat_max = {s: 18 for s in stats}


        try:
            rinfo = get_race(self.races_cfg, race_name)
        except Exception:
            rinfo = {}
        cinfo = class_data.get(standardize_class_name(class_name), {}) or {}

        def apply(src):
            for k, v in (src or {}).items():
                key = str(k).strip().lower()

                if key.endswith("min"):
                    base = key[:-3]
                    if base in stat_min:
                        try:
                            stat_min[base] = max(stat_min[base], int(v))
                        except Exception:
                            pass
                elif key.endswith("max"):
                    base = key[:-3]
                    if base in stat_max:
                        try:
                            stat_max[base] = min(stat_max[base], int(v))
                        except Exception:
                            pass

        apply(rinfo)
        apply(cinfo)


        for s in stats:
            if stat_min[s] > stat_max[s]:
                stat_min[s] = stat_max[s]

        return stat_min, stat_max


    @commands.command()
    async def charcreate(self, ctx, name: str, race: str, char_class: str, sex: str = None, *extras):
        """Create a new character and save to a .coe file."""
        char_class = standardize_class_name(char_class)
        char_name = name.strip()

        # Validate class against class.lst so typos like "Figher" are rejected
        if char_class.strip().lower() not in VALID_CLASS_NAMES:
            valid_list = ", ".join(sorted(class_data.keys()))
            await ctx.send(
                f"❌ Unknown class '{char_class}'. "
                f"Please choose one of: {valid_list}"
            )
            return

        existing_disp, existing_path = _resolve_char_ci(char_name)
        if existing_path:
            await ctx.send(f"A character with the name {existing_disp} already exists.")
            return

        file_name = f"{char_name.replace(' ', '_')}.coe"

        races_cfg = load_races("race.lst")
        race_lower = race.lower()
        if race_lower not in {r.lower() for r in races_cfg.keys()}:
            await ctx.send(f"Unknown race '{race}'. Please choose a valid race.")
            return

        stat_keys = ['str', 'dex', 'con', 'int', 'wis', 'cha']


        use_4d6 = False
        try:
            use_4d6 = self._hr_enabled(ctx.channel, "ability_4d6dl", default=True)
        except Exception:
            use_4d6 = _hr_enabled_anywhere(ctx.channel, "ability_4d6dl", default=True)

        stats = {k: (_roll_4d6_drop_lowest() if use_4d6 else _roll_3d6()) for k in stat_keys}

        char_race_data = get_race(races_cfg, race)
        smin, smax = self._stat_bounds(race, char_class)
        for s in stat_keys:
            stats[s] = max(smin[s], min(smax[s], stats[s]))

        stat_min, stat_max = {}, {}
        for k, v in char_race_data.items():
            if k.endswith('min'): stat_min[k[:-3]] = int(v)
            elif k.endswith('max'): stat_max[k[:-3]] = int(v)
        for s in stat_keys:
            stats[s] = max(stat_min.get(s, 3), min(stat_max.get(s, 18), stats[s]))

        modifiers = {f"{s}_modifier": calculate_modifier(score) for s, score in stats.items()}
        hp, roll = self.starting_hp(char_class, modifiers['con_modifier'], race)

        gold_rolls = [random.randint(1, 6) for _ in range(3)]
        starting_gp = sum(gold_rolls) * 10
        pp = ep = sp = cp = 0

        load = LOADOUTS.get(char_class, LOADOUTS["Fighter"])
        armor1 = load.get("armor1", "") or ""
        armor2 = load.get("armor2", "") or ""
        weapons = [w for w in load.get("weapons", []) if w]

        items_cfg = configparser.ConfigParser(); items_cfg.optionxform = str; items_cfg.read("item.lst")
        def _item_get(name, key, fallback=None):
            if not name or not items_cfg.has_section(name):
                return fallback
            return items_cfg.get(name, key, fallback=fallback)
        def _num(s, default=0.0):
            try: return float(str(s).strip())
            except Exception: return default
        def _int_or_none(s):
            if s is None: return None
            t = str(s).strip()
            if t == "": return None
            try: return int(t)
            except Exception: return None
        def _truthy(v) -> bool:
            return str(v).strip().lower() in {"$true","true","1","yes","y"}
        def _safe_int(x, d=None):
            try: return int(str(x).strip())
            except Exception: return d

        armor1_ac = _int_or_none(_item_get(armor1, "AC", None))
        shield_bonus = 0
        if armor2 and _truthy(_item_get(armor2, "armor2", "")):
            s_ac = _int_or_none(_item_get(armor2, "AC", None))
            if s_ac is not None:
                shield_bonus += s_ac

        is_barbarian = (char_class.strip().lower() == "barbarian")
        level = 1
        if is_barbarian and not armor1:
            base_ac = self._barbarian_unarmored_ac(level)
        else:
            base_ac = armor1_ac if armor1_ac is not None else 11

        # NEW: include Dex mod in starting AC
        dex_mod = modifiers.get("dex_modifier", 0)
        ac = base_ac + shield_bonus + dex_mod


        total_w = 0.0
        for n in filter(None, [armor1, armor2] + weapons):
            total_w += _num(_item_get(n, "weight", 0.0), 0.0)
        coin_weight = (pp + starting_gp + ep + sp + cp) * 0.02
        total_w += coin_weight
        eq_weight_str = f"{total_w:.2f}"

        def _skills_lower(v):
            """Normalize a race's skills entry (list or space-separated string) to a lowercase set."""
            if isinstance(v, (list, tuple, set)):
                return {str(x).strip().lower() for x in v}
            if v is None:
                return set()
            return {s.strip().lower() for s in str(v).split()}

        def _race_size(race_name: str, race_data: dict[str, object]) -> str:
            """
            Return 'small' | 'medium' | 'large' based on race skills or well-known names.
            Prefers the explicit Small/Large skill flags from race.lst.
            """
            name = str(race_name).strip().lower()
            sk = _skills_lower(race_data.get('skills', []))

            if 'small' in sk or name in {'halfling','goblin','kobold','gnome'}:
                return 'small'
            if 'large' in sk or name in {'half-ogre'}:
                return 'large'
            return 'medium'

        def _weight_thresholds_by_size(size: str, STR: int) -> tuple[int, int]:
            """
            Light/Heavy thresholds based on STR and size.
            - 'small' uses your existing small table values
            - 'medium' uses your existing medium table values
            - 'large' scales medium by ~25% and rounds to nearest 5
            """
            s = max(3, min(int(STR or 10), 18))

            if s == 3:         med = (25, 60);  sml = (20, 40)
            elif s in (4, 5):  med = (35, 90);  sml = (30, 60)
            elif 6 <= s <= 8:  med = (50, 120); sml = (40, 80)
            elif 9 <= s <= 12: med = (60, 150); sml = (50, 100)
            elif 13 <= s <= 15:med = (65, 165); sml = (55, 110)
            elif s in (16,17): med = (70, 180); sml = (60, 120)
            else:              med = (80, 195); sml = (65, 130)

            if size == 'small':
                return sml

            return sml if small else med

        def _armor_row_speeds(armor_name: str):
            name = (armor_name or "").lower()
            magic = _truthy(_item_get(armor_name, "magic", "")) or name.startswith("magic ")
            is_leather = "leather" in name
            is_metal   = any(w in name for w in ("mail", "plate", "metal"))
            if not armor_name:               return (40, 30)
            if is_leather and magic:         return (40, 30)
            if is_leather:                   return (30, 20)
            if is_metal and magic:           return (30, 20)
            if is_metal:                     return (20, 10)
            return (30, 20)

        STR = stats["str"]
        light_lim, heavy_lim = self._weight_thresholds(race, STR)
        light_spd, heavy_spd = self._armor_row_speeds(armor1)
        if total_w <= light_lim:  move = light_spd + 10
        elif total_w <= heavy_lim:move = light_spd
        else:                     move = heavy_spd

        config = configparser.ConfigParser()
        config['version'] = {'current': '08082018'}
        config['info'] = {
            'race': str(race or '').title(),
            'class': str(char_class or ''),
            'sex': str(sex or ''),
            'name': str(char_name or ''),
            'owner_id': str(ctx.author.id),
        }
        config['stats'] = {
            **{k: str(v) for k, v in stats.items()},
            **{k: str(v) for k, v in modifiers.items()},
            'ac': str(ac),
            'ab': '',
            'move': '0',
            'eq_weight': eq_weight_str
        }
        eq = {}
        if armor1: eq['armor1'] = armor1
        if armor2: eq['armor2'] = armor2
        eq['armor'] = str((1 if armor1 else 0) + (1 if armor2 else 0))
        for i, w in enumerate(weapons, start=1):
            eq[f'weapon{i}'] = w
        eq['weapon'] = str(len(weapons))
        eq['weight'] = eq_weight_str
        eq['coin_weight'] = f"{coin_weight:.2f}"
        config['eq'] = eq

        move = self._recompute_move(config)
        config['stats']['move'] = str(move)
        config['base'] = {**config['stats']}
        config['max'] = {'hp': str(hp)}
        config['cur'] = {
            'hp': str(hp), 'level': '1', 'xp': '0',
            'pp': '0', 'gp': str(starting_gp), 'ep': '0', 'sp': '0', 'cp': '0',
            'turn': ''
        }

        storage_items = [x for x in [armor1, armor2, *weapons] if x]
        inv_counts: dict[str, int] = {}

        for x in storage_items:
            canon = self._canon(x)
            inv_counts[canon.lower()] = inv_counts.get(canon.lower(), 0) + 1

        for item_name, qty in STARTER_KIT:
            canon = self._canon(item_name)
            inv_counts[canon.lower()] = inv_counts.get(canon.lower(), 0) + int(qty)

        # NEW: class-specific starter gear
        extra_gear = CLASS_EXTRA_GEAR.get(char_class.strip().lower(), [])
        for item_name, qty in extra_gear:
            canon = self._canon(item_name)
            inv_counts[canon.lower()] = inv_counts.get(canon.lower(), 0) + int(qty)


        item_sec = {}
        storage_items = [x for x in [armor1, armor2, *weapons] if x]

        inv_counts: dict[str, int] = {}

        # equipped/starting weapons+armor
        for x in storage_items:
            canon = self._canon(x)
            inv_counts[canon.lower()] = inv_counts.get(canon.lower(), 0) + 1

        # generic starter kit
        for item_name, qty in STARTER_KIT:
            canon = self._canon(item_name)
            inv_counts[canon.lower()] = inv_counts.get(canon.lower(), 0) + int(qty)

        # class-specific starter gear
        extra_gear = CLASS_EXTRA_GEAR.get(char_class.strip().lower(), [])
        for item_name, qty in extra_gear:
            canon = self._canon(item_name)
            inv_counts[canon.lower()] = inv_counts.get(canon.lower(), 0) + int(qty)

        # storage list must include *all* items we want to show in !bag
        storage_names = sorted({
            self._canon(k).strip()
            for k in [
                *storage_items,
                *(n for n, _ in STARTER_KIT),
                *(n for n, _ in extra_gear),
            ]
            if k
        })

        item_sec = {"storage": " ".join(storage_names)}
        for lower_key, cnt in inv_counts.items():
            item_sec[lower_key] = str(cnt)

        config["item"] = item_sec


        config['saves']          = {key: str(value) for key, value in char_race_data.get('saves', {}).items()}
        config['thief_mods']     = {key: str(value) for key, value in char_race_data.get('thief_mods', {}).items()}
        config['banned_weapons'] = {'list': ' '.join(char_race_data.get('banned_weapons', []))}

        class_sk = class_data.get(char_class, {}).get('skills', [])
        if isinstance(class_sk, str): class_sk = [class_sk]
        race_sk = char_race_data.get('skills', [])
        skills_all = sorted({*(race_sk or []), *(class_sk or [])})
        config['skills'] = {'list': ' '.join(skills_all)}


        extras = list(extras or [])
        def eat(flag, takes_val=False, aliases=()):
            for key in (flag, *aliases):
                if key in extras:
                    i = extras.index(key)
                    if takes_val:
                        val = extras[i+1] if i+1 < len(extras) else None
                        del extras[i:i+2]
                        return val
                    else:
                        del extras[i]
                        return True
            return None

        is_npc    = bool(eat("-npc", aliases=("-npc","-retainer","-retainer")))
        loy_str   = eat("-loyalty", True, aliases=("-morale","-morale","-loyalty"))
        share_str = eat("-share", True)
        employer  = eat("-employer", True)

        loyalty = None if loy_str is None else _safe_int(loy_str, None)
        share   = 15 if share_str is None else max(5, min(75, _safe_int(share_str, 15)))
        if not employer:
            employer = get_active(ctx.author.id) or ""

        npc_summary = None

        if is_npc:
            if not config.has_section('npc'):
                config.add_section('npc')
            config.set('npc','type','Retainer')
            config.set('npc','employer_char', employer)
            config.set('npc','employer_owner_id', str(ctx.author.id))
            config.set('npc','share_pct', str(share))
            if loyalty is not None:
                config.set('npc','loyalty', str(max(2, min(12, loyalty))))

            npc_summary = f"Retainer of **{employer or '(unset)'}** • Share: {share}%"
            if loyalty is not None:
                npc_summary += f" • Loyalty: {config.get('npc','loyalty',fallback='?')}"


        try:
            safe_base = _safe_char_basename(char_name)
            file_path = Path(f"{safe_base}.coe")
        except ValueError as e:
            await ctx.send(f"Invalid character name: {e}")
            return

        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(file_path), flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                config.write(f)
        except FileExistsError:
            await ctx.send(f"A character with the name {char_name} already exists.")
            return
        except OSError as e:
            await ctx.send(f"Failed to create character file: {e.strerror or e}")
            return

        char_id = safe_base
        try:
            add_char(ctx.author.id, char_id)
            set_active(ctx.author.id, char_id)
        except PermissionError:
            pass



        gp_line = f"🎲 Starting Gold: **{starting_gp} gp** *(rolled {gold_rolls[0]}+{gold_rolls[1]}+{gold_rolls[2]} × 10)*"
        gear_lines = []
        if armor1 or armor2:
            armor_list = ", ".join([x for x in [armor1, armor2] if x]) if (armor1 or armor2) else "—"
            gear_lines.append(f"**Armor**: {armor_list}")
        if weapons:
            gear_lines.append(f"**Weapons**: {', '.join(weapons)}")

        embed = nextcord.Embed(
            title=f"Character Created!",
            description=f"_Saved as **{char_name}**_",
            color=random.randint(0, 0xFFFFFF),
        )

        embed.add_field(name="**Character Info**", value=f"**Race:** {race.title()}\n**Class:** {char_class}", inline=False)
        embed.add_field(
            name="**Stats**",
            value='\n'.join(f"**{s.upper()}**: {stats[s]} (Mod: {modifiers[f'{s}_modifier']})" for s in ['str','dex','con','int','wis','cha']),
            inline=False
        )
        embed.add_field(name="**HP**", value=str(hp), inline=True)
        embed.add_field(name="**AC / Move**", value=f"{ac} / {move}", inline=True)
        embed.add_field(name="**Starting Gear**", value='\n'.join(gear_lines) or "—", inline=False)
        embed.add_field(name="**Coins**", value=gp_line, inline=False)


        if is_npc and npc_summary:
            embed.add_field(name="**NPC Tag**", value=npc_summary, inline=False)


        embed.set_footer(text=("House Rule: 4d6 drop lowest, `!hr` to change" if use_4d6 else "Classic: 3d6, `!hr` to change"))
        await ctx.send(embed=embed)


    def _hr_enabled(self, channel, key: str, default: bool = True) -> bool:
        """
        Per-channel toggle, stored under <chan_base>:hr
          heroic_called_shot = 1/0
          quarterstaff_defense = 1/0
        """
        try:
            cfg = _load_battles()
            sec = self._hr_section_id(channel)
            if not cfg.has_section(sec):
                cfg.add_section(sec)
            if not cfg.has_option(sec, key):
                cfg.set(sec, key, "1" if default else "0")
                _save_battles(cfg)
            return cfg.getboolean(sec, key)
        except Exception:
            return default

    @commands.command()
    async def reroll(self, ctx, *, char_name: str | None = None):
        """Reroll a character's stats and HP (only if level 1). If no name is given, uses your active character."""
        import random, nextcord


        if not char_name:
            char_name = get_active(ctx.author.id)
            if not char_name:
                await ctx.send("❌ No active character. Use `!char <name>` first or run `!reroll <name>`.")
                return

        disp, file_name = _resolve_char_ci(char_name)
        if not file_name:
            await ctx.send(f"Character '{disp or char_name}' does not exist.")
            return

        config = read_cfg(file_name)
        owner_id = get_compat(config, "info", "owner_id", fallback="")
        if owner_id and owner_id != str(ctx.author.id):
            await ctx.send(f"You do not own the character '{disp or char_name}'.")
            return

        if not is_level_one(config):
            await ctx.send(f"Character '{disp or char_name}' is above level 1 and cannot be rerolled.")
            return

        stat_keys = ['str', 'dex', 'con', 'int', 'wis', 'cha']
        stats = {stat: roll_stat() for stat in stat_keys}

        race = get_compat(config, "info", "race")
        char_class = standardize_class_name(get_compat(config, "info", "class"))


        smin, smax = self._stat_bounds(race, char_class)
        for stat in stat_keys:
            stats[stat] = max(smin[stat], min(smax[stat], stats[stat]))


        char_race_data = get_race(self.races_cfg, race)
        stat_min = {k[:-3].lower(): int(v) for k, v in char_race_data.items() if k.endswith('min')}
        stat_max = {k[:-3].lower(): int(v) for k, v in char_race_data.items() if k.endswith('max')}
        for stat in stat_keys:
            min_val = stat_min.get(stat, 3)
            max_val = stat_max.get(stat, 18)
            stats[stat] = max(min_val, min(max_val, stats[stat]))

        modifiers = {f"{stat}_modifier": calculate_modifier(stats[stat]) for stat in stat_keys}
        hp, roll = self.starting_hp(char_class, modifiers['con_modifier'], race)

        config['stats'].update(
            {**{stat: str(score) for stat, score in stats.items()},
             **{mod: str(value) for mod, value in modifiers.items()}}
        )
        config['cur']['hp'] = str(hp)
        config['max']['hp'] = str(hp)
        write_cfg(file_name, config)

        embed = nextcord.Embed(title=f"Character '{disp or char_name}' Rerolled!", color=random.randint(0, 0xFFFFFF))
        embed.add_field(
            name="**New Stats**",
            value='\n'.join(f"**{stat.upper()}**: {score} (Modifier: {modifiers[f'{stat}_modifier']})"
                            for stat, score in stats.items()),
            inline=False
        )
        embed.add_field(name="**HP**", value=f"{hp}", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def swap(self, ctx, *args):
        """
        Swap two ability scores for level 1 characters.
        Usage:
          • !swap STR DEX                    (uses your active character)
          • !swap <char_name> STR DEX        (explicit character)
        """
        import nextcord, random

        valid = {'str','dex','con','int','wis','cha'}


        if len(args) == 2:
            char_name = get_active(ctx.author.id)
            if not char_name:
                await ctx.send("❌ No active character. Use `!char <name>` first or run `!swap <name> str dex`.")
                return
            stat1, stat2 = args[0].lower(), args[1].lower()
        elif len(args) >= 3:
            char_name = str(args[0])
            stat1, stat2 = str(args[1]).lower(), str(args[2]).lower()
        else:
            await ctx.send("Error: Provide two stats to swap.\nExamples:\n• `!swap str dex`\n• `!swap <char_name> str dex`")
            return

        if stat1 not in valid or stat2 not in valid:
            await ctx.send("Invalid stats. Valid stats: str, dex, con, int, wis, cha.")
            return

        disp, path = _resolve_char_ci(char_name)
        if not path:
            await ctx.send(f"Character '{disp or char_name}' does not exist.")
            return

        config = read_cfg(path)
        if not is_level_one(config):
            await ctx.send(f"Character '{disp or char_name}' is above level 1 and cannot swap stats.")
            return

        level = getint_compat(config, "cur", "level", fallback=0)
        owner_id = get_compat(config, "info", "owner_id", fallback="")
        if level != 1:
            await ctx.send(f"Character '{disp or char_name}' must be level 1 to swap ability scores.")
            return
        if owner_id and owner_id != str(ctx.author.id):
            await ctx.send(f"You do not own the character '{disp or char_name}'.")
            return


        race = get_compat(config, "info", "race", fallback="Human")
        char_class = standardize_class_name(get_compat(config, "info", "class", fallback="Fighter"))
        smin, smax = self._stat_bounds(race, char_class)

        stats = config['stats']

        v1 = int(stats.get(stat1, '10') or 10)
        v2 = int(stats.get(stat2, '10') or 10)
        new1, new2 = v2, v1

        problems = []
        if not (smin[stat1] <= new1 <= smax[stat1]):
            problems.append(f"**{stat1.upper()}** would become **{new1}**, but allowed range is **{smin[stat1]}–{smax[stat1]}**.")
        if not (smin[stat2] <= new2 <= smax[stat2]):
            problems.append(f"**{stat2.upper()}** would become **{new2}**, but allowed range is **{smin[stat2]}–{smax[stat2]}**.")
        if problems:
            await ctx.send("❌ Swap would violate race/class limits:\n- " + "\n- ".join(problems))
            return


        old_con_val = int(stats.get('con', '10') or 10)
        old_con_mod = calculate_modifier(old_con_val)


        stats[stat1], stats[stat2] = stats.get(stat2, str(v2)), stats.get(stat1, str(v1))
        stats[f'{stat1}_modifier'] = str(calculate_modifier(int(stats[stat1])))
        stats[f'{stat2}_modifier'] = str(calculate_modifier(int(stats[stat2])))

        hp = getint_compat(config, "cur", "hp", fallback=0)
        mhp = getint_compat(config, "max", "hp", fallback=hp)
        new_con_val = int(stats.get('con', '10') or 10)
        new_con_mod = calculate_modifier(new_con_val)
        delta = new_con_mod - old_con_mod
        if delta != 0:
            hp = max(1, hp + delta)
            mhp = max(1, mhp + delta)
            config['cur']['hp'] = str(hp)
            config['max']['hp'] = str(mhp)

        write_cfg(path, config)

        embed = nextcord.Embed(title=f"Character '{disp or char_name}' Stats Swapped!", color=random.randint(0, 0xFFFFFF))
        embed.add_field(name="**Swapped Stats**", value=f"**{stat1.upper()}** ↔ **{stat2.upper()}**", inline=False)
        embed.add_field(
            name="**New Stats**",
            value='\n'.join(f"**{s.upper()}**: {stats.get(s, '—')} (Modifier: {stats.get(f'{s}_modifier', '0')})"
                            for s in ['str','dex','con','int','wis','cha']),
            inline=False
        )
        if delta != 0:
            sign = f"+{delta}" if delta > 0 else str(delta)
            embed.add_field(name="**HP Adjusted**", value=f"{hp}/{mhp} (CON mod {sign})", inline=False)
        else:
            embed.add_field(name="**HP**", value=f"{hp}/{mhp}", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def levelup(self, ctx, char_name: str):
        """Level up a character when they have enough XP."""
        disp_name = None; file_path = None
        try:
            disp_name, file_path = _resolve_char_ci(char_name)
        except Exception:
            pass
        if not file_path:
            target = f"{char_name.replace(' ', '_').lower()}.coe"
            for fn in os.listdir("."):
                if fn.lower() == target:
                    file_path = fn
                    try:
                        tmp = read_cfg(fn)
                        disp_name = get_compat(tmp, "info", "name", fallback=None) or fn[:-4].replace("_", " ")
                    except Exception:
                        disp_name = fn[:-4].replace("_", " ")
                    break
        if not file_path:
            await ctx.send(f"Character '{char_name}' does not exist.")
            return
        cfg = read_cfg(file_path)
        owner_id = get_compat(cfg, "info", "owner_id", fallback="")
        allowed = (not owner_id) or (owner_id == str(ctx.author.id))

        if not allowed:
            try:
                bcfg = _load_battles(); chan_id = _section_id(ctx.channel)
                if bcfg.has_section(chan_id):
                    dm_id = bcfg.get(chan_id, "DM", fallback="")
                    allowed = (str(ctx.author.id) == str(dm_id))
            except Exception:
                pass

        if not allowed:
            try:
                if ctx.guild and (ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator):
                    allowed = True
            except Exception:
                pass

        if not allowed:
            await ctx.send(f"❌ You do not own **{disp_name or char_name}**.")
            return
        char_name = disp_name or char_name
        for sec in ("cur", "max", "stats", "eq", "info"):
            if not cfg.has_section(sec):
                cfg.add_section(sec)
        old_ac = getint_compat(cfg, "stats", "ac", fallback=10)
        level = getint_compat(cfg, "cur", "level", fallback=1)
        new_level = level + 1
        cfg["cur"]["level"] = str(new_level)
        char_class = get_compat(cfg, "info", "class", fallback="Fighter")
        race       = get_compat(cfg, "info", "race",  fallback="Human")
        con_score  = getint_compat(cfg, "stats", "con", fallback=10)
        con_modifier = calculate_modifier(con_score)
        hp_gain, token = self.levelup_hp_one_level(char_class, con_modifier, race, entering_level=new_level)
        cur_hp_present = cfg.has_option("cur", "hp")
        max_hp_present = cfg.has_option("max", "hp")
        if not (cur_hp_present and max_hp_present):
            base_hp, _ = self.starting_hp(char_class, con_modifier, race)
            if not cfg.has_section("max"): cfg.add_section("max")
            if not cfg.has_section("cur"): cfg.add_section("cur")
            if not max_hp_present: cfg["max"]["hp"] = str(base_hp)
            if not cur_hp_present: cfg["cur"]["hp"] = str(base_hp)
        current_hp = getint_compat(cfg, "cur", "hp", fallback=getint_compat(cfg, "max", "hp", fallback=0))
        max_hp     = getint_compat(cfg, "max", "hp", fallback=current_hp)
        current_hp += hp_gain
        max_hp     += hp_gain
        cfg["cur"]["hp"] = str(current_hp)
        cfg["max"]["hp"] = str(max_hp)
        try:

            if hasattr(self, "_recalc_ac"):
                self._recalc_ac(cfg)
            elif hasattr(self, "_recompute_ac"):
                self._recompute_ac(cfg)
            else:

                cls_lc = get_compat(cfg, "info", "class", fallback="").strip().lower()
                a1 = get_compat(cfg, "eq", "armor1", fallback="").strip()
                if cls_lc == "barbarian" and not a1:

                    shield_bonus = 0
                    a2 = get_compat(cfg, "eq", "armor2", fallback="").strip()
                    if a2:
                        data2 = self.items.get(self._canon(a2), {})
                        def _truthy(v): return str(v).strip().lower() in {"$true","true","1","yes","y"}
                        if _truthy(data2.get("armor2", "")):
                            try:
                                shield_bonus = int(str(data2.get("AC", "0")).strip() or "0")
                            except Exception:
                                pass

                    # NEW: add Dex mod on top of barbarian table
                    try:
                        dex_mod = getint_compat(cfg, "stats", "dex_modifier", fallback=None)
                    except Exception:
                        dex_mod = None
                    if dex_mod is None:
                        try:
                            dex_score = getint_compat(cfg, "stats", "dex", fallback=10)
                            dex_mod = calculate_modifier(dex_score)
                        except Exception:
                            dex_mod = 0

                    cfg["stats"]["ac"] = str(self._barbarian_unarmored_ac(new_level) + shield_bonus + int(dex_mod or 0))

        except Exception:
            pass

        write_cfg(file_path, cfg)
        ac_after = getint_compat(cfg, "stats", "ac", fallback=old_ac)
        embed = nextcord.Embed(title=f"{char_name} has leveled up!", color=random.randint(0, 0xFFFFFF))
        embed.add_field(name="Level", value=str(new_level))
        embed.add_field(name="HP Gain", value=f"{hp_gain} (Roll: {token})")
        embed.add_field(name="Current / Max HP", value=f"{current_hp}/{max_hp}")
        if ac_after != old_ac:
            embed.add_field(name="AC", value=f"{old_ac} → **{ac_after}**", inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="additem")
    async def additem(self, ctx, who: str, *, item_and_qty: str):
        import re, secrets

        """DM/helper: Add items to a character's inventory (storage + counts)."""
        m = re.fullmatch(r'\s*(?:"([^"]+)"|\'([^\']+)\'|(.+?))(?:\s+(\d+))?\s*', item_and_qty)
        if not m:
            await ctx.send("❌ Couldn’t parse item and quantity. Try: `!additem testman rations 7`")
            return
        item_name = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        qty = int(m.group(4) or "1")
        if qty <= 0:
            await ctx.send("❌ Quantity must be a positive integer.")
            return

        char_name, path = _resolve_char_ci(who)
        if not path:
            await ctx.send(f"❌ Character '{who}' not found.")
            return

        cfg = read_cfg(path)
        owner_id = get_compat(cfg, "info", "owner_id", fallback="")
        if str(ctx.author.id) != str(owner_id) and not getattr(ctx.author.guild_permissions, "manage_guild", False):
            await ctx.send("❌ You must own this character or have Manage Server permission to add items.")
            return

        if not cfg.has_section("item"): cfg.add_section("item")
        canon = self._canon(item_name)
        canon_lc = canon.lower()

        spells_cog = self.bot.get_cog("Spells")
        looks_charged_by_name = canon_lc.startswith("wandof") or canon_lc.startswith("staffof")

        cog_says_charged = False
        it = None
        if spells_cog:
            canon_lookup, it = spells_cog._item_lookup(canon)
            cog_says_charged = spells_cog._item_has_charges(canon_lookup, it)
        else:
            canon_lookup = canon

        if cog_says_charged or looks_charged_by_name:
            made = []
            for _ in range(qty):
                if spells_cog:
                    tok = spells_cog._add_charged_item_instance(cfg, path, canon_lookup, it)
                else:

                    tok = f"{canon}@{secrets.token_hex(3)}"
                    storage = (get_compat(cfg, "item", "storage", fallback="") or "").split()
                    if tok not in storage:
                        storage.append(tok)
                    if not cfg.has_section("item"): cfg.add_section("item")
                    cfg.set("item", tok.lower(), "1")
                    cfg.set("item", canon_lc, "0")


                    mx = 20 if canon_lc.startswith("wandof") else 30
                    core = "".join(ch for ch in canon_lc if ch.isalnum())
                    suf  = tok.split("@", 1)[1].lower()
                    key  = f"{core}_{suf}"
                    if not cfg.has_section("charges"): cfg.add_section("charges")
                    cfg.set("charges", key, f"{mx}/{mx}")
                    cfg.set("item", "storage", " ".join(storage))
                    write_cfg(path, cfg)
                made.append(tok)

            await ctx.send(
                f"✅ Added **{len(made)}× {canon_lookup}** to **{char_name}** "
                f"(each tracked separately with its own charges)."
            )
            return


        lower_key = canon_lc
        try:
            cur_cnt = int(str(cfg.get("item", lower_key, fallback="0")).strip() or "0")
        except Exception:
            cur_cnt = 0
        cfg.set("item", lower_key, str(cur_cnt + qty))
        storage_line = cfg.get("item", "storage", fallback="")
        tokens = [t for t in storage_line.split() if t]
        seen_ci = {t.lower(): t for t in tokens}
        if canon_lc not in seen_ci:
            tokens.append(canon)
        cfg.set("item", "storage", " ".join(tokens))
        write_cfg(path, cfg)

        try:
            self._auto_seed_charges_on_add(cfg, path, canon, mode="new")
        except Exception:
            pass

        await ctx.send(f"✅ Added **{qty}× {canon}** to **{char_name}**’s inventory.")


    @commands.command(name="restore")
    async def restore_negative_level(self, ctx, target: str = None, levels: str = "1"):
        """Remove negative levels (e.g., Restoration)."""
        if not target:
            await ctx.send("Usage: `!restore <name> [count]`")
            return
        disp, path = _resolve_char_ci(target)
        pretty = disp or target
        if not path or not os.path.exists(path):
            await ctx.send(f"❌ Target **{pretty}** not found.")
            return
        try:
            n_levels = max(1, int(str(levels)))
        except Exception:
            n_levels = 1
        cfg = read_cfg(path)
        cur_lvls = getint_compat(cfg, "cur", "neg_levels", fallback=0)
        pool     = getint_compat(cfg, "cur", "neg_hp_loss_total", fallback=0)
        if cur_lvls <= 0:
            await ctx.send(f"✨ **{pretty}** has no negative levels.")
            return
        removed = 0
        heals = []
        for _ in range(n_levels):
            lvls_before = getint_compat(cfg, "cur", "neg_levels", fallback=0)
            pool_before = getint_compat(cfg, "cur", "neg_hp_loss_total", fallback=0)
            if lvls_before <= 0 or pool_before <= 0:
                break
            heal = int((pool_before / lvls_before) + 0.5)
            heal = max(0, heal)
            new_pool = max(0, pool_before - heal)
            new_lvls = max(0, lvls_before - 1)
            if not cfg.has_section("cur"): cfg.add_section("cur")
            cfg["cur"]["neg_levels"] = str(new_lvls)
            cfg["cur"]["neg_hp_loss_total"] = str(new_pool)
            write_cfg(path, cfg)
            cfg = read_cfg(path)
            cap = self._effective_max_hp(cfg)
            old_hp = getint_compat(cfg, "cur", "hp", fallback=0)
            new_hp = min(cap, old_hp + heal)
            cfg["cur"]["hp"] = str(new_hp)
            write_cfg(path, cfg)
            removed += 1
            heals.append(heal)
        title = f"✨ Restoration on {pretty}"
        embed = nextcord.Embed(title=title, color=random.randint(0, 0xFFFFFF))
        embed.add_field(name="Removed", value=f"{removed} negative level(s)", inline=True)
        if heals:
            embed.add_field(name="HP Restored", value=" → ".join(map(str, heals)) + f"  (total **{sum(heals)}**)", inline=True)
        cfg = read_cfg(path)
        mhp = getint_compat(cfg, "max", "hp", fallback=0)
        hp  = getint_compat(cfg, "cur", "hp", fallback=0)
        lv  = getint_compat(cfg, "cur", "neg_levels", fallback=0)
        pool= getint_compat(cfg, "cur", "neg_hp_loss_total", fallback=0)
        cap = self._effective_max_hp(cfg)
        embed.add_field(name="Now", value=f"HP {hp}/{mhp} (cap {cap}); Negative Levels: **{lv}**; Pool: **{pool}**", inline=False)
        try:
            bcfg = _load_battles()
            chan_id = str(ctx.channel.id)
            if bcfg.has_section(chan_id):
                msg_id = bcfg.getint(chan_id, "message_id", fallback=0)
                if msg_id:
                    block = _format_tracker_block(bcfg, chan_id)
                    msg = await ctx.channel.fetch_message(msg_id)
                    await msg.edit(content="**EVERYONE ROLL FOR INITIATIVE!**\n```text\n" + block + "\n```")
        except Exception:
            pass
        await ctx.send(embed=embed)


    @commands.command(name="dec")
    async def dec_stat(self, ctx, who: str = None, stat: str = None, points: int = 0):
        """
        Decrease an ability score and update its modifier (and HP if CON).
        Usage: !dec <name> <stat> <points>
               !dec (uses your active char) <stat> <points>
        Examples:
          !dec Holyman STR 4
          !dec STR 2             # uses your active character
        """
        import random, re


        if who and not stat and points == 0:
            toks = [t for t in re.split(r"\s+", who.strip()) if t]
            if len(toks) == 2 and _norm_stat_key(toks[0]) and toks[1].lstrip("+-").isdigit():
                stat = toks[0]; points = int(toks[1]); who = None

        if not stat or points is None or int(points) <= 0:
            await ctx.send("❌ Usage: `!dec <name> <stat> <points>` (e.g., `!dec Holyman STR 2`).")
            return
        points = int(points)
        skey = _norm_stat_key(stat)
        if not skey:
            await ctx.send(f"❌ Unknown ability: **{stat}**. Use STR/DEX/CON/INT/WIS/CHA.")
            return


        if not who:
            who = get_active(ctx.author.id)
            if not who:
                await ctx.send("❌ No active character. Use `!char <name>` or pass a name: `!dec <name> <stat> <points>`.")
                return

        disp, path = _resolve_char_ci(who)
        if not path:
            await ctx.send(f"❌ Character '{who}' not found.")
            return

        cfg = read_cfg(path)


        owner_id = get_compat(cfg, "info", "owner_id", fallback="")
        allowed = (not owner_id) or (owner_id == str(ctx.author.id))
        if not allowed:
            try:
                bcfg = _load_battles()
                chan_id = _section_id(ctx.channel)
                if bcfg.has_section(chan_id):
                    dm_id = bcfg.get(chan_id, "DM", fallback="")
                    allowed = (str(ctx.author.id) == str(dm_id))
            except Exception:
                pass
        if not allowed:
            await ctx.send(f"❌ You do not own **{disp}**.")
            return


        try:
            old_val = getint_compat(cfg, "stats", skey, fallback=None)
        except Exception:
            old_val = None
        if old_val is None:
            await ctx.send(f"❌ {disp} has no '{skey.upper()}' in [stats].")
            return

        old_mod = getint_compat(cfg, "stats", f"{skey}_modifier", fallback=_ability_mod(old_val))


        target = max(3, int(old_val) - points)
        if target == old_val:
            await ctx.send(f"ℹ️ {disp}’s {skey.upper()} is already at the minimum (3). No change.")
            return


        hp_note = None
        if skey == "con":
            try:
                delta_hp = self._apply_con_change_adjust_hp(cfg, old_val, target)
                if delta_hp != 0:
                    sign = "+" if delta_hp > 0 else ""
                    hp_note = f"Max HP {sign}{delta_hp} due to CON mod change."
            except TypeError:

                delta_hp = self._apply_con_change_adjust_hp(cfg, target - old_val)
                if delta_hp != 0:
                    sign = "+" if delta_hp > 0 else ""
                    hp_note = f"Max HP {sign}{delta_hp} due to CON mod change."


        if not cfg.has_section("stats"): cfg.add_section("stats")
        cfg["stats"][skey] = str(target)
        new_mod = _ability_mod(target)
        cfg["stats"][f"{skey}_modifier"] = str(new_mod)

        write_cfg(path, cfg)


        import nextcord
        embed = nextcord.Embed(
            title=f"📉 {disp}: {skey.upper()} decreased",
            color=random.randint(0, 0xFFFFFF)
        )
        embed.add_field(name=skey.upper(), value=f"{old_val} → **{target}**", inline=True)
        if new_mod != old_mod:
            sign = "+" if new_mod >= 0 else ""
            embed.add_field(name="Modifier", value=f"{old_mod:+d} → **{sign}{new_mod}**", inline=True)
        if hp_note:
            embed.add_field(name="HP", value=hp_note, inline=False)

        await ctx.send(embed=embed)


    @commands.command(name="inc")
    async def inc_stat(self, ctx, who: str = None, stat: str = None, points: int = 0):
        """
        Increase an ability score and update its modifier (and HP if CON).
        Usage: !inc <name> <stat> <points>
               !inc (active char) <stat> <points>
        """
        import random, re
        if who and not stat and points == 0:
            toks = [t for t in re.split(r"\s+", who.strip()) if t]
            if len(toks) == 2 and _norm_stat_key(toks[0]) and toks[1].lstrip("+-").isdigit():
                stat = toks[0]; points = int(toks[1]); who = None

        if not stat or points is None or int(points) <= 0:
            await ctx.send("❌ Usage: `!inc <name> <stat> <points>` (e.g., `!inc Holyman STR 2`).")
            return
        points = int(points)
        skey = _norm_stat_key(stat)
        if not skey:
            await ctx.send(f"❌ Unknown ability: **{stat}**. Use STR/DEX/CON/INT/WIS/CHA.")
            return

        if not who:
            who = get_active(ctx.author.id)
            if not who:
                await ctx.send("❌ No active character. Use `!char <name>` or pass a name: `!inc <name> <stat> <points>`.")
                return

        disp, path = _resolve_char_ci(who)
        if not path:
            await ctx.send(f"❌ Character '{who}' not found.")
            return

        cfg = read_cfg(path)

        owner_id = get_compat(cfg, "info", "owner_id", fallback="")
        allowed = (not owner_id) or (owner_id == str(ctx.author.id))
        if not allowed:
            try:
                bcfg = _load_battles(); chan_id = _section_id(ctx.channel)
                if bcfg.has_section(chan_id):
                    dm_id = bcfg.get(chan_id, "DM", fallback="")
                    allowed = (str(ctx.author.id) == str(dm_id))
            except Exception:
                pass
        if not allowed:
            await ctx.send(f"❌ You do not own **{disp}**.")
            return

        old_val = getint_compat(cfg, "stats", skey, fallback=None)
        if old_val is None:
            await ctx.send(f"❌ {disp} has no '{skey.upper()}' in [stats].")
            return
        old_mod = getint_compat(cfg, "stats", f"{skey}_modifier", fallback=_ability_mod(old_val))

        target = int(old_val) + points

        hp_note = None
        if skey == "con":
            try:
                delta_hp = self._apply_con_change_adjust_hp(cfg, old_val, target)
                if delta_hp != 0:
                    sign = "+" if delta_hp > 0 else ""
                    hp_note = f"Max HP {sign}{delta_hp} due to CON mod change."
            except TypeError:
                delta_hp = self._apply_con_change_adjust_hp(cfg, target - old_val)
                if delta_hp != 0:
                    sign = "+" if delta_hp > 0 else ""
                    hp_note = f"Max HP {sign}{delta_hp} due to CON mod change."

        if not cfg.has_section("stats"): cfg.add_section("stats")
        cfg["stats"][skey] = str(target)
        new_mod = _ability_mod(target)
        cfg["stats"][f"{skey}_modifier"] = str(new_mod)
        write_cfg(path, cfg)

        import nextcord
        embed = nextcord.Embed(
            title=f"📈 {disp}: {skey.upper()} increased",
            color=random.randint(0, 0xFFFFFF)
        )
        embed.add_field(name=skey.upper(), value=f"{old_val} → **{target}**", inline=True)
        if new_mod != old_mod:
            sign = "+" if new_mod >= 0 else ""
            embed.add_field(name="Modifier", value=f"{old_mod:+d} → **{sign}{new_mod}**", inline=True)
        if hp_note:
            embed.add_field(name="HP", value=hp_note, inline=False)

        await ctx.send(embed=embed)


    @commands.command(name="geasday")
    async def geas_apply_day(self, ctx, who: str):
        """
        Apply one day of Geas non-compliance:
          -2 to ALL ability scores (min 3), cumulative to -8 total.
        On first use, snapshots base stats under [geas].
        """
        import random, nextcord

        disp, path = _resolve_char_ci(who)
        if not path:
            await ctx.send(f"❌ Character '{who}' not found.")
            return
        cfg = read_cfg(path)


        owner_id = get_compat(cfg, "info", "owner_id", fallback="")
        allowed = (not owner_id) or (owner_id == str(ctx.author.id))
        if not allowed:
            try:
                bcfg = _load_battles(); chan_id = _section_id(ctx.channel)
                if bcfg.has_section(chan_id):
                    dm_id = bcfg.get(chan_id, "DM", fallback="")
                    allowed = (str(ctx.author.id) == str(dm_id))
            except Exception:
                pass
        if not allowed:
            await ctx.send(f"❌ You do not own **{disp}**.")
            return


        if not cfg.has_section("geas"):
            cfg.add_section("geas")
            for k in ("str","dex","con","int","wis","cha"):
                cfg["geas"][f"base_{k}"] = str(getint_compat(cfg, "stats", k, fallback=10))
            cfg["geas"]["applied"] = "0"

        applied = getint_compat(cfg, "geas", "applied", fallback=0)
        if applied >= 8:
            await ctx.send(f"ℹ️ **{disp}** is already at the maximum Geas penalty (−8).")
            return

        step = min(2, 8 - applied)
        changes = []
        hp_note = None

        for k in ("str","dex","con","int","wis","cha"):
            old = getint_compat(cfg, "stats", k, fallback=10)
            target = max(3, old - step)
            if k == "con":
                try:
                    self._apply_con_change_adjust_hp(cfg, old, target)
                except TypeError:
                    self._apply_con_change_adjust_hp(cfg, target - old)
            cfg["stats"][k] = str(target)
            cfg["stats"][f"{k}_modifier"] = str(_ability_mod(target))
            if target != old:
                changes.append(f"{k.upper()} {old}→**{target}**")

        cfg["geas"]["applied"] = str(applied + step)
        write_cfg(path, cfg)


        embed = nextcord.Embed(
            title=f"🧷 Geas penalty applied to {disp}",
            color=random.randint(0, 0xFFFFFF)
        )
        embed.add_field(name="This Day", value=f"−{step} to all abilities (min 3).", inline=False)
        if changes:
            embed.add_field(name="Changes", value=", ".join(changes), inline=False)
        embed.add_field(name="Total Penalty", value=f"−{applied + step} / −8", inline=True)

        await ctx.send(embed=embed)


    @commands.command(name="geasclear")
    async def geas_clear(self, ctx, who: str):
        """
        Restore stats to the snapshot saved by !geasday and clear the Geas penalty record.
        """
        import random, nextcord

        disp, path = _resolve_char_ci(who)
        if not path:
            await ctx.send(f"❌ Character '{who}' not found.")
            return
        cfg = read_cfg(path)

        if not cfg.has_section("geas"):
            await ctx.send(f"ℹ️ No Geas snapshot found for **{disp}**.")
            return


        owner_id = get_compat(cfg, "info", "owner_id", fallback="")
        allowed = (not owner_id) or (owner_id == str(ctx.author.id))
        if not allowed:
            try:
                bcfg = _load_battles(); chan_id = _section_id(ctx.channel)
                if bcfg.has_section(chan_id):
                    dm_id = bcfg.get(chan_id, "DM", fallback="")
                    allowed = (str(ctx.author.id) == str(dm_id))
            except Exception:
                pass
        if not allowed:
            await ctx.send(f"❌ You do not own **{disp}**.")
            return

        changes = []
        for k in ("str","dex","con","int","wis","cha"):
            base = getint_compat(cfg, "geas", f"base_{k}", fallback=None)
            cur  = getint_compat(cfg, "stats", k, fallback=None)
            if base is None or cur is None:
                continue
            if k == "con":
                try:
                    self._apply_con_change_adjust_hp(cfg, cur, base)
                except TypeError:
                    self._apply_con_change_adjust_hp(cfg, base - cur)
            cfg["stats"][k] = str(base)
            cfg["stats"][f"{k}_modifier"] = str(_ability_mod(base))
            if base != cur:
                changes.append(f"{k.upper()} {cur}→**{base}**")


        cfg.remove_section("geas")
        write_cfg(path, cfg)

        embed = nextcord.Embed(
            title=f"🧽 Geas penalties cleared for {disp}",
            color=random.randint(0, 0xFFFFFF)
        )
        if changes:
            embed.add_field(name="Restored", value=", ".join(changes), inline=False)
        await ctx.send(embed=embed)


    def _safe_canon(self, item_name: str, it: dict | None) -> str:
        """
        Stable display/key base for an item.
        - Prefer self._canon(item_name) if your cog has it
        - Else prefer catalog id/block/name
        - Else raw item_name
        """
        fn = getattr(self, "_canon", None)
        if callable(fn):
            try:
                v = str(fn(item_name) or "").strip()
                if v:
                    return v
            except Exception:
                pass
        if it:
            for k in ("charges_key", "id", "_id", "block", "name", "item_id"):
                v = str(it.get(k, "")).strip()
                if v:
                    return v
        return str(item_name or "")

    def _item_kind_with_charges(self, item_name: str, it: dict | None) -> str | None:
        """
        Returns 'wand' or 'staff' if this should track charges, else None.
        Uses either it['type'] or name heuristic (Wandof*/Staffof*).
        """
        ty = str((it or {}).get("type", "")).strip().lower()
        if ty in {"wand", "staff"}:
            return ty
        name_l = self._safe_canon(item_name, it).strip().lower()
        if name_l.startswith("wandof"):
            return "wand"
        if name_l.startswith("staffof"):
            return "staff"
        return None

    def _item_has_charges(self, item_name: str, it: dict | None) -> bool:
        return self._item_kind_with_charges(item_name, it) is not None

    def _charges_key(self, item_name: str, it: dict | None = None) -> str:

        base = self._safe_canon(item_name, it).lower()
        return "".join(ch for ch in base if ch.isalnum())

    def _charges_max_for_item(self, item_name: str, it: dict | None) -> int:
        kind = self._item_kind_with_charges(item_name, it)
        if kind == "wand":  return 20
        if kind == "staff": return 30

        try:
            return int((it or {}).get("max_charges", 0) or 0)
        except Exception:
            return 0

    def _get_item_charges(self, cfg, key: str, default_max: int | None = None) -> tuple[int | None, int | None]:
        """
        Reads [charges].<key> as 'cur/max' or legacy 'cur'.
        Returns (cur, max) where either may be None if unset.
        """
        import re
        raw = str(get_compat(cfg, "charges", key, fallback="")).strip()
        if raw:
            m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", raw)
            if m:
                return int(m.group(1)), int(m.group(2))
            try:
                cur = int(raw)
                return cur, (default_max or cur)
            except Exception:
                pass
        return None, default_max

    def _set_item_charges(self, cfg, path: str, key: str, cur: int, maxv: int | None = None) -> None:
        if not cfg.has_section("charges"):
            cfg.add_section("charges")
        if maxv is None:
            _, old_max = self._get_item_charges(cfg, key, default_max=None)
            maxv = old_max if old_max is not None else cur
        cfg.set("charges", key, f"{max(0, int(cur))}/{max(0, int(maxv))}")
        write_cfg(path, cfg)

    def _spend_item_charges(self, cfg, path: str, key: str, cost: int, default_max: int) -> tuple[bool, int, str]:
        """
        Try to spend 'cost' charges. Returns (ok, remaining_after, err_msg_if_any).
        """
        left, mx = self._get_item_charges(cfg, key, default_max)
        left = 0 if left is None else left
        mx = default_max if mx is None else mx
        if cost <= 0:
            return True, left, ""
        if left < cost:
            return False, left, f"❌ Not enough charges (has **{left}**, needs **{cost}**)."
        self._set_item_charges(cfg, path, key, left - cost, mx)
        return True, left - cost, ""

    def _auto_seed_charges_on_add(self, cfg, path: str, item_name: str, *, mode: str = "new") -> None:
        """
        Seed charges for newly added wand/staff if not already present.
        mode: 'new' -> full (20/30), 'found' -> roll (2d10/3d10)
        """
        _c, it = self._item_lookup(item_name)
        if not self._item_has_charges(item_name, it):
            return
        key = self._charges_key(item_name, it)
        if cfg.has_section("charges") and cfg.has_option("charges", key):
            return
        maxv = self._charges_max_for_item(item_name, it)
        if mode == "found":
            dice = "3d10" if self._item_kind_with_charges(item_name, it) == "staff" else "2d10"
            total, rolls, flat = roll_dice(dice)
            cur = max(1, total + flat)
        else:
            cur = maxv
        self._set_item_charges(cfg, path, key, cur, maxv)


    def _apply_con_change_adjust_hp(self, cfg, old_con: int, new_con: int):
        """Adjust max/current HP when CON modifier crosses a boundary."""
        old_mod = self._con_mod(old_con)
        new_mod = self._con_mod(new_con)
        if old_mod == new_mod:
            return 0
        hd_levels = self._levels_count_for_hp(cfg)
        delta_hp = (new_mod - old_mod) * hd_levels

        cur_hp = getint_compat(cfg, "cur", "hp", fallback=0)
        max_hp = getint_compat(cfg, "max", "hp", fallback=cur_hp)
        max_hp = max(1, max_hp + delta_hp)
        cur_hp = min(cur_hp, max_hp)

        if not cfg.has_section("cur"): cfg.add_section("cur")
        if not cfg.has_section("max"): cfg.add_section("max")
        cfg["cur"]["hp"] = str(cur_hp)
        cfg["max"]["hp"] = str(max_hp)
        return delta_hp

    def _con_mod(self, v: int) -> int:
        if v <= 3:  return -3
        if v <= 5:  return -2
        if v <= 8:  return -1
        if v <= 12: return 0
        if v <= 15: return +1
        if v <= 17: return +2
        return +3


    def _levels_count_for_hp(self, cfg) -> int:
        """How many HD-levels to apply CON bonus per die to.
           If you have a more precise table (e.g., stops at 9th), replace here."""
        return max(1, getint_compat(cfg, "cur", "level", fallback=1))

    @commands.command(name="hire")
    async def hire(self, ctx, *args):
        """
        Hire helpers.
          !hire retainer [pc:<name>] [±N]
          !hire specialist [type] [pc:<name>]
          !hire mercenaries [platoon|company] [type:<slug>] [pc:<name>] [size:<N>] [platoons:<N>] [-stronghold]

        Examples:
          !hire retainer
          !hire retainer pc:testman +2
          !hire specialist
          !hire specialist engineer
          !hire mercenaries
          !hire mercenaries platoon type:heavy_foot_human
          !hire mercenaries company type:medium_horseman_elf platoons:3 -stronghold
        """

        if not args:
            await ctx.send(
                "Usage:\n"
                "• `!hire retainer [pc:<name>] [±N]`\n"
                "• `!hire specialist [type] [pc:<name>]`\n"
                "• `!hire mercenaries [platoon|company] [type:<slug>] [pc:<name>] [size:<N>] [platoons:<N>] [-stronghold]`"
            )
            return

        sub = args[0].lower().strip()

        if sub in ("mercenary", "mercenaries", "mercs"):
            tokens = list(args[1:])

            def pop_token(prefixes):
                for i, t in enumerate(list(tokens)):
                    for p in prefixes:
                        if t.lower().startswith(p):
                            val = t.split(":", 1)[1].strip() if ":" in t else None
                            tokens.pop(i)
                            return val
                return None

            pc_name = pop_token(["pc:"])
            size_raw = pop_token(["size:"])     
            platoons_raw = pop_token(["platoons:", "platoon:", "pls:"])
            stronghold = False
            for t in list(tokens):
                if t.lower() in ("-stronghold", "-sh"):
                    stronghold = True
                    tokens.remove(t)

            type_raw = pop_token(["type:"])
            mode = tokens[0].lower().strip() if tokens else None

            disp, pc_path, pc_cfg = _get_pc_cfg(self, ctx, pc_name)
            employer_char = get_compat(pc_cfg, "info", "name", fallback=disp or "(Unknown)") if pc_cfg else "(None)"

            def norm_key(s: str):
                s = s.lower().replace("-", "_")
                s = re.sub(r"[^\w]+", "_", s)
                return re.sub(r"_+", "_", s).strip("_")

            def chunk_lines(lines, max_len=1024):
                chunks, buf = [], ""
                for line in lines:
                    line = line.rstrip()
                    if not buf:
                        buf = line
                    elif len(buf) + 1 + len(line) <= max_len:
                        buf += "\n" + line
                    else:
                        chunks.append(buf)
                        buf = line
                if buf:
                    chunks.append(buf)
                return chunks

            def add_lines_field_chunked(embed, title, lines):
                parts = chunk_lines(lines, max_len=1024)
                total = len(parts)
                for i, part in enumerate(parts, 1):
                    name = title if total == 1 else f"{title} [{i}/{total}]"
                    embed.add_field(name=name, value=part, inline=False)

            merc_types = {
                "light_foot_human": ("Light Foot, Human", "Human", 2, "Leather Armor, Shield, and Longsword", 8),
                "light_foot_elf": ("Light Foot, Elf", "Elf", 8, "Leather Armor, Shield, and Longsword", 8),
                "light_foot_orc": ("Light Foot, Orc", "Orc", 1, "Leather Armor and Spear", 7),
                "heavy_foot_human": ("Heavy Foot, Human", "Human", 3, "Chainmail, Shield, and Longsword", 8),
                "heavy_foot_dwarf": ("Heavy Foot, Dwarf", "Dwarf", 6, "Chainmail, Shield, and Shortsword", 9),
                "heavy_foot_orc": ("Heavy Foot, Orc", "Orc", 2, "Chainmail, Shield, and Shortsword", 8),
                "archer_human": ("Archer, Human", "Human", 5, "Leather Armor, Shortbow, and Shortsword", 8),
                "archer_elf": ("Archer, Elf", "Elf", 15, "Chainmail, Shortbow, and Shortsword", 8),
                "archer_orc": ("Archer, Orc", "Orc", 3, "Leather Armor, Shortbow, and Shortsword", 8),
                "crossbowman_human": ("Crossbowman, Human", "Human", 5, "Chainmail, Crossbow, and Shortsword", 8),
                "crossbowman_dwarf": ("Crossbowman, Dwarf", "Dwarf", 12, "Platemail, Crossbow, and Shortsword", 9),
                "longbowman_human": ("Longbowman, Human", "Human", 9, "Chainmail, Longbow, and Shortsword", 8),
                "longbowman_elf": ("Longbowman, Elf", "Elf", 20, "Chainmail, Longbow, and Longsword", 8),
                "light_horseman_human": ("Light Horseman, Human", "Human", 10, "Leather Armor, Shield, Lance, and Longsword", 8),
                "light_horseman_elf": ("Light Horseman, Elf", "Elf", 22, "Leather Armor, Lance, Shortbow, and Longsword", 8),
                "medium_horseman_human": ("Medium Horseman, Human", "Human", 15, "Chainmail, Shield, Lance, and Longsword", 8),
                "medium_horseman_elf": ("Medium Horseman, Elf", "Elf", 33, "Chainmail, Lance, Shortbow, and Longsword", 9),
                "heavy_horseman_human": ("Heavy Horseman, Human", "Human", 20, "Platemail, Shield, Lance, and Longsword", 8),
            }

            def _dedupe(seq):
                seen, out = set(), []
                for x in seq:
                    if not x or x in seen:
                        continue
                    seen.add(x)
                    out.append(x)
                return out

            def _lines(block):
                return [ln.strip() for ln in block.splitlines() if ln.strip()]

            def _first_tokens(block):
                out = []
                for ln in block.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    low = ln.lower()
                    if low in ("dwarf names", "meaning") or low.startswith("dwarf names"):
                        continue
                    name = ln.split()[0]
                    if name:
                        out.append(name)
                return out

            HUMAN_FEMALE_BLOCK = """Adela
Adelaide
Admiranda
Aeditha
Aelina
Agnys
Alainne
Alianore
Alison
Alyne
Alys
Amelia
Amice
Amphelice
Angelet
Anna
Annabel
Anne
Anthoinette
Anys
Aqua
Arabella
Arlette
Atilda
Aubrey
Audrye
Ava
Avelin
Avelyn
Averil
Ayleth
Baterich
Bathsua
Beatrix
Bellinda
Bertana
Berte
Bess
Brangwine
Braya
Brunhild
Bryde
Caesaria
Carmen
Casandra
Cecilia
Cecily
Celeste
Celestine
Celestria
Cenota
Charmaine
Chloe
Christabel
Cicely
Clara
Clarimond
Claudia
Clemence
Collys
Comet
Concessa
Constance
Cornelia
Crestian
Cristiana
Cwengyth
Cyndra
Cynewyn
Damaris
Dametta
Debra
Decima
Deloys
Denys
Diamanda
Dionisia
Dominy
Dorcas
Doris
Dorothe
Durilda
Dyana
Edelinne
Edithe
Eilonwy
Elaisse
Ele
Eleanor
Elewys
Elizabeth
Ellerete
Ellie
Elsebee
Elyn
Elynor
Elyzabeth
Emblyn
Emeline
Emeny
Emeria
Emery
Emilie
Emlinie
Emmet
Eschina
Eschiva
Esdeline
Esmenet
Estienne
Estrild
Ethelia
Eugenia
Eustacia
Eva
Eve
Evelyn
Felice
Florens
Frances
Francisca
Frideswide
Fridgia
Gaynor
Germainne
Gethrude
Gillian
Giselle
Glenda
Gloriana
Guinevere
Gylda
Helena
Helenor
Helvynya
Hester
Hilda
Hildegard
Hilith
Imedia
Isabella
Isemeine
Ismay
Ismenia
Isolde
Jaane
Jacquette
Jeanne
Jellion
Jemime
Jenet
Jenique
Jenyfer
Jessamine
Jessica
Jillian
Jocea
Jocelyn
Joleicia
Jolline
Josephine
Josian
Josiane
Joyse
Judithe
Judye
Juliana
Julyan
June
Justina
Katelyn
Kath
Katherine
Katrine
Kinborow
Latisha
Lauda
Leofwen
Leofwynn
Letita
Lettice
Linette
Linyeve
Liza
Lora
Maddeline
Maerwynn
Maisenta
Malin
Margarete
Margeria
Margry
Maria
Maronne
Marsilia
Martine
Mathild
Matilda
Melodie
Melusine
Meredithe
Merewyn
Merilda
Meryell
Millicent
Minerva
Mirabelle
Morgayne
Muriel
Murienne
Mydrede
Nastasia
Nena
Nesta
Nicholina
Nicia
Nicolaa
Nikita
Olyffe
Ophellia
Ottilia
Paige
Parnella
Patsy
Paula
Pelinne
Penelope
Petronilla
Placencia
Prudence
Pulmia
Purnell
Rebeccah
Rianna
Richenda
Ripley
Rosa
Rosalind
Rosamund
Rose
Roysia
Rychyld
Sabrina
Samantha
Sanche
Sandy
Sara
Sarra
Scarlet
Selphina
Sence
Serendipity
Shadi
Shari
Simone
Somerhild
Sreda
Stella
Susie
Sybell
Sylphie
Syndony
Sysley
Systeleley
Tansa
Temperance
Theda
Theresa
Thomasine
Thomasyn
Thora
Tiphina
Tristana
Ursula
Venus
Veronica
Vrsela
Wenyld
Willmott
Wulfhilda
Wynefreede
Yedythe
Ysabel
Ysmeina"""

            HUMAN_MALE_BLOCK = """Abel
Abelard
Abraham
Addison
Akintunde
Alaire
Albin
Aldebrand
Aldous
Aleyn
Alistair
Ancelot
Anselm
Anton
Aran
Arnald
Arnott
Arthur
Augustine
Aylmer
Baderon
Baldric
Bardolf
Barney
Bartholomew
Barton
Bayard
Bean
Beck
Belmont
Benedict
Beneger
Bernard
Berndan
Bertram
Bertrand
Blackburn
Blake
Blavier
Bouchard
Boville
Boyce
Boyle
Bran
Brice
Brien
Bruce
Bruno
Bryce
Cain
Cameron
Caplan
Carmine
Casim
Caspar
Ceadda
Chamberlain
Charlys
Chartain
Claudien
Clifton
Clive
Cole
Collins
Colson
Conphas
Cornell
Coster
Cutbert
Cuthbert
Cyriac
Daimbert
Dalmas
Daniel
Danyell
Dauid
Davyd
Dawson
Deitrich
Denston
Derwin
Deryk
Dick
Dietrich
Donner
Drake
Drew
Drystan
Eadbert
Ealdwine
Eckart
Eddie
Edmund
Edwyn
Eldred
Eleazar
Eliot
Emanuel
Emerick
Erasmus
Erik
Esmond
Esmour
Esperaunce
Etgar
Ethelbert
Ethelred
Eustace
Evans
Fawkes
Fernando
Fiebras
Flambard
Flansburgh
Folke
Fox
Foxe
Francis
Fred
Freddie
Frederick
Frederyk
Fulke
Galfrid
Ganelon
Gared
Gauwyn
Gembert
Geoffrey
Gerald
Gerbold
Gerhardt
Gerland
Goddard
Godebert
Godfrey
Gordon
Gregory
Grimbald
Gryffen
Guston
Gwayne
Gylbart
Gyles
Habreham
Hadrian
Haimirich
Halstein
Hamon
Harold
Heinlein
Hewrey
Hicks
Hogan
Humphrey
Indurain
Ingham
Ingram
Isleton
Ivan
Jakys
Jean
Jeger
Jenlyns
Johannes
Jonathas
Joseph
Josias
Joyce
Junk
Kain
Keane
Kennard
Kenrick
Kerrich
Khellus
Kimball
Kinnison
Kurtz
Ladislas
Lambert
Lars
Laurence
Laurentius
Leavold
Lefwyne
Lennard
Leopold
Littlejohn
Little Jon
Lloyd
Lodwicke
Lowell
Luke
Madison
Mainfroi
Mansel
Mathye
McNeal
Merlin
Morgant
Morys
Mueller
Myles
Nathaniel
Navarre
Neale
Neilson
Nelson
Noes
Noppo
Norman
Olyver
Orrick
Orwen
Osric
Oswyn
Owyne
Parnell
Patrick
Paul
Percival
Peter
Philippe
Piers
Powle
Radcliffe
Radolf
Raffe
Ralph
Randall
Randwulf
Rauffe
Raulin
Redwald
Reeve
Reginald
Reinholdt
Reynard
Reyner
Reynfred
Ricard
Richarde
Rickeman
Ridel
Robert
Robyn
Roger
Rolfe
Ronald
Roundelph
Rowland
Samson
Sandre
Selleck
Sevrin
Sighard
Sigurdh
Simond
Singleton
Solyeuse
Spencer
Spenser
Stanley
Stewart
Striker
Swift
Symon
Symond
Tak
Taran
Taylor
Templeton
Tensberger
Theodore
Thomas
Thrydwulf
Timothy
Tristan
Turstin
Ulric
Valentine
Valentyne
Vannes
Vector
Victor
Voyce
Vyncent
Wadard
Walter
Warin
Wauter
Werner
Wilfrid
Wilham
Willielmus
Wineburg
Wolfstan
Wyatt
Wymon
Wymond
Wystan
Ywain
Zacheus
Zell
Zerig
Ziggy"""

            ORC_BLOCK = """Achaios
Acis
Adonis
Aegipan
Ageia
Aigis
Aiguptos
Aigyptos
Aiolides
Aion
Aisa
Aisakos
Aithilla
Aithon
Aitne
Akakos
Alkmene
Alsnova
Ampelos
Anaxibia
Anius
Antigone
Apemosyne
Archedios
Argo
Arkeisios
Askalabos
Atropos
Atys
Augeias
Auson
Bacchus
Bakas
Bakis
Belos
Berekyntia
Bormos
Bromios
Brontes
Bukolos
Camers
Carna
Catillus
Charis
Chesias
Chryses
Ciden
Cinope
Cybele
Damia
Danae
Dardanos
Deianeira
Deidameia
Deimachos
Deimacos
Deimos
Dekelos
Delphos
Derkynos
Dies
Dodona
Dryope
Dwyvaer
Dysaules
Echetlos
Echo
Eidothea
Elatus
Elpenor
Enipeus
Epigonoi
Epione
Erato
Erebos
Euadne
Euchenor
Euenos
Eunmoss
Eunomos
Eupalamos
Euphorbos
Europe
Fames
Fauna
Foashpil
Galateia
Galeos
Glauke
Grups
Gyes
Gygas
Halia
Halisera
Helias
Helios
Hemithea
Hepaklos
Heppoko
Herios
Herkyna
Hippotes
Hopladamos
Huaina
Hylas
Iamos
Ianthe
Ilos
Inferi
Inuus
Iobes
Iphis
Irae
Irus
Isa
Ischys
Isyrion
Janus
Jupiter
Justitia
Kamerus
Kampe
Kapys
Kaukon
Kaukorn
Kaunos
Kelmis
Kephalos
Kilix
Klaros
Kleobis
Kranaos
Kyknos
Kyzikos
Laios
Lampetos
Laodameia
Laodike
Lapithes
Latinos
Latona
Lausus
Laverna
Leimone
Leipephile
Leuke
Leukippe
Leukon
Linos
Lityerses
Llawran
Lykeios
Lykomedes
Lykophron
Lykurgos
Lynkos
Lysippe
Machaon
Maiandros
Makaria
Marls
Mars
Mavors
Megareus
Melaineus
Melampus
Memphis
Menestheus
Merops
Mestor
Mestorle
Metaneira
Metis
Metope
Minos
Minyas
Misericordia
Miserikordi
Mnestra
Molossos
Morossos
Morpheus
Muilto
Mulciber
Musa
Mykenai
Myrine
Myrto
Naucitos
Nausithos
Nautes
Neaira
Neilos
Nemea
Nessos
Nireus
Nomios
Nyx
Ogaphos
Ogappon
Ogygos
Ogyugos
Oiax
Oibalos
Oinomaos
Ophis
Orthaia
Oxylos
Pallene
Pasiphae
Pedasos
Peirene
Pelias
Penates
Penia
Penthesileia
Peripanos
Persephone
Perseus
Phanes
Pheraia
Philyra
Phlegrai
Phobos
Phrasios
Phrixos
Phthonos
Pieria
Pisos
Pitane
Pittheus
Poine
Polybos
Polydamna
Polykaon
Polyxo
Portheus
Poteitei
Potitii
Priamos
Prokne
Prokris
Proteus
Prothoos
Raodameia
Reia
Reimonee
Remnus
Rhadamanthys
Rhadine
Rhakios
Rhea
Ruklegos
Salamis
Salios
Satiros
Satyros
Semele
Sibyl
Sikyon
Silvanus
Sinope
Sisyphos
Sithon
Sol
Sybaris
Syme
Talaos
Tantalos
Tatius
Telephassa
Tenes
Terkcion
Teukros
Thaeox
Thamyris
Thelxion
Theophane
Thespis
Thoas
Thyia
Tyche
Typhon
Uranos
Urans
Vesta
Zephyrus"""

            ELF_BLOCK = """Aelael
Aelar
Aelbel
Aelcalad
Aelcar
Aelel
Aeleri
Aeleryn
Aeloron
Aelquen
Aelril
Aelsyl
Aeltar
Aelthar
Aelther
Aeltir
Aelvael
Aelvil
Aelzor
Arael
Arar
Arbel
Arcalad
Arcar
Arel
Areri
Areryn
Arfael
Arfin
Argalad
Arhal
Arithil
Arlen
Arlorien
Armel
Armin
Armir
Arnaer
Arnim
Arnor
Aroron
Arquen
Arril
Arsyl
Artar
Arthar
Arther
Artir
Arvael
Arvil
Arzor
Belael
Belar
Belbel
Belcalad
Belcar
Belel
Beleri
Beleryn
Belmir
Belnaer
Belnim
Belnor
Beloron
Belquen
Belril
Belsyl
Beltar
Belthar
Belther
Beltir
Belvael
Belvil
Belzor
Caladael
Caladar
Caladbel
Caladcalad
Caladcar
Caladel
Caladeri
Caladeryn
Caladfael
Caladfin
Caladgalad
Caladhal
Caladithil
Caladlen
Caladlorien
Caladmel
Caladmin
Caladmir
Caladnaer
Caladnim
Caladnor
Caladoron
Caladquen
Caladril
Caladsyl
Caladtar
Caladthar
Caladther
Caladtir
Caladvael
Caladvil
Caladzor
Carael
Carar
Carbel
Carcalad
Carcar
Carel
Careri
Careryn
Carfael
Carfin
Cargalad
Carvil
Carzor
Elael
Elar
Elbel
Elcalad
Elcar
Elel
Eleri
Eleryn
Elfael
Elfin
Elgalad
Elhal
Elithil
Ellen
Ellorien
Elmel
Elmin
Elmir
Elnaer
Elnim
Elnor
Eloron
Elquen
Elril
Elsyl
Eltar
Elthar
Elther
Eltir
Elvael
Elvil
Elzor
Eriael
Ericalad
Erieryn
Erifael
Eriithil
Erilorien
Erisyl
Erithar
Erynael
Erynar
Erynbel
Eryncalad
Eryncar
Erynel
Eryneri
Eryneryn
Erynfael
Erynfin
Eryngalad
Erynhal
Erynithil
Erynlen
Erynlorien
Erynmel
Erynmin
Erynmir
Erynnaer
Erynnim
Erynnor
Erynoron
Erynquen
Erynril
Erynsyl
Eryntar
Erynthar
Erynther
Eryntir
Faelnor
Faeloron
Faelquen
Faelril
Faelsyl
Faeltar
Faelthar
Faelther
Faeltir
Faelvael
Faelvil
Faelzor
Finael
Finar
Finbel
Fincalad
Fincar
Finel
Fineri
Fineryn
Finfael
Finfin
Fingalad
Finhal
Finithil
Finlen
Finlorien
Finmel
Finmin
Finmir
Finnaer
Finnim
Finnor
Finoron
Finquen
Finril
Finsyl
Fintar
Finthar
Finther
Fintir
Finvael
Finvil
Finzor
Galadael
Galadar
Galadbel
Galadcalad
Galadcar
Galadel
Galaderi
Galaderyn
Galadfael
Galadfin
Galadgalad
Galadhal
Galadithil
Galadlen
Galadlorien
Galadmel
Galadmin
Galadmir
Galadnaer
Galadnim
Galadnor
Galadoron
Galadquen
Galadril
Galadsyl
Galadtar
Galadthar
Galadther
Galadtir
Galadvael
Galadvil
Galadzor
Halael
Halar
Halbel
Halcalad
Halcar
Halel
Haleri
Haleryn
Halfael
Halfin
Halgalad
Halhal
Halithil
Hallen
Hallorien
Halmel
Halmin
Halmir
Halnaer
Haltir
Halvael
Halvil
Halzor
Ithilael
Ithilar
Ithilbel
Ithilcalad
Ithilcar
Ithilel
Ithileri
Ithileryn
Ithilfael
Ithilfin
Ithilgalad
Ithilhal
Ithilithil
Ithillen
Ithillorien
Ithilmel
Ithilmin
Ithilmir
Ithilnaer
Ithilnim
Ithilnor
Ithiloron
Ithilquen
Ithilril
Ithilsyl
Ithiltar
Ithilthar
Ithilther
Ithiltir
Ithilvael
Ithilvil
Ithilzor
Lenael
Lenar
Lenbel
Lencalad
Lencar
Lenel
Leneri
Leneryn
Lenfael
Lenfin
Lengalad
Lenhal
Lenithil
Lenlen
Lenlorien
Lenmel
Lenmin
Lenmir
Lennaer
Lennim
Lennor
Lenoron
Lenquen
Lenril
Lensyl
Lentar
Lenthar
Lenther
Lentir
Lenvael
Lenvil
Lenzor
Lorienael
Lorienar
Lorienbel
Loriencalad
Loriencar
Lorienel
Lorieneri
Lorieneryn
Lorienfael
Lorienfin
Loriengalad
Lorienhal
Lorienithil
Lorienlen
Lorienlorien
Lorienmel
Lorienmin
Lorienmir
Loriennaer
Loriennim
Loriennor
Lorienoron
Lorienquen
Lorienril
Loriensyl
Lorientar
Lorienthar
Lorienther
Lorientir
Lorienvael
Lorienvil
Lorienzor
Melael
Melar
Melbel
Melcalad
Melcar
Melel
Meleri
Meleryn
Melfael
Melfin
Melgalad
Melhal
Melithil
Mellen
Mellorien
Melmel
Melmin
Melmir
Melnaer
Melnim
Melnor
Meloron
Melquen
Melril
Melsyl
Meltar
Melthar
Melther
Meltir
Melvael
Melvil
Melzor
Minael
Minar
Minbel
Mincalad
Mincar
Minel
Mineri
Mineryn
Minfael
Minfin
Mingalad
Minhal
Minithil
Minlen
Minlorien
Minmel
Minmin
Minmir
Minnaer
Minnim
Minnor
Minoron
Minquen
Minril
Minsyl
Mintar
Minthar
Minther
Mintir
Minvael
Minvil
Minzor
Mirael
Mirar
Mirbel
Mircalad
Mircar
Mirel
Mireri
Mireryn
Mirfael
Mirfin
Mirgalad
Mirhal
Mirithil
Mirlen
Mirlorien
Mirmel
Mirmin
Mirmir
Mirnaer
Mirnim
Mirnor
Miroron
Mirquen
Mirril
Mirsyl
Mirtar
Mirthar
Mirther
Mirtir
Mirvael
Mirvil
Mirzor
Naerael
Naerar
Naerbel
Naercalad
Naercar
Naerel
Naereri
Naereryn
Naerfael
Naerfin
Naergalad
Naerhal
Naerithil
Naerlen
Naerlorien
Naermel
Naermin
Naermir
Naernaer
Naernim
Naernor
Naeroron
Naerquen
Naerril
Naersyl
Nimlorien
Nimmel
Nimmin
Nimmir
Nimnaer
Nimnim
Nimnor
Nimoron
Nimquen
Nimril
Nimsyl
Nimtar
Nimthar
Nimther
Nimtir
Nimvael
Nimvil
Nimzor
Norael
Norar
Norbel
Norcalad
Norcar
Norel
Noreri
Noreryn
Norfael
Norfin
Norgalad
Norhal
Norithil
Norlen
Norlorien
Normel
Normin
Normir
Nornaer
Nornim
Nornor
Nororon
Norquen
Norril
Norsyl
Nortar
Northar
Norther
Nortir
Norvael
Queneryn
Quenfael
Quenfin
Quengalad
Quenhal
Quenithil
Quenlen
Quenlorien
Quenmel
Quenmin
Quenmir
Quennaer
Quennim
Quennor
Quenoron
Quenquen
Quenril
Quensyl
Quentar
Quenthar
Quenther
Quentir
Quenvael
Quenvil
Quenzor
Rilael
Rilar
Rilbel
Rilcalad
Rilcar
Rilel
Rileri
Rileryn
Rilfael
Rilfin
Rilgalad
Rilhal
Rilithil
Rillen
Rillorien
Rilmel
Rilmin
Rilmir
Sylar
Sylbel
Sylcalad
Sylcar
Sylel
Syleri
Syleryn
Sylfael
Sylfin
Sylgalad
Sylhal
Sylithil
Syllen
Syllorien
Sylmel
Sylmin
Sylmir
Sylnaer
Sylnim
Sylnor
Syloron
Sylquen
Sylril
Sylsyl
Syltar
Sylthar
Sylther
Syltir
Sylvael
Sylvil
Sylzor
Tarael
Tarar
Tarbel
Tarcalad
Tarcar
Tarel
Tareri
Tareryn
Tarfael
Tarfin
Targalad
Tharcalad
Tharcar
Tharel
Thareri
Thareryn
Tharfael
Tharfin
Thargalad
Tharhal
Tharithil
Tharlen
Tharlorien
Tharmel
Tharmin
Tharmir
Tharnaer
Tharnim
Tharnor
Tharoron
Tharquen
Tharril
Tharsyl
Thartar
Tharthar
Tharther
Thartir
Tharvael
Tharvil
Tharzor
Therael
Therar
Therbel
Thercalad
Thercar
Therel
Thereri
Thereryn
Therfael
Therfin
Thergalad
Therhal
Therithil
Therlen
Therlorien
Tircalad
Tircar
Tirel
Tireri
Tireryn
Tirfael
Tirfin
Tirgalad
Tirhal
Tirithil
Tirlen
Tirlorien
Tirmel
Tirmin
Tirmir
Tirnaer
Tirnim
Tirnor
Tiroron
Tirquen
Tirril
Tirsyl
Tirtar
Tirthar
Tirther
Tirtir
Tirvael
Tirvil
Tirzor
Vaelael
Vaelar
Vaelbel
Vaelcalad
Vaelcar
Vaelel
Vaeleri
Vaeleryn
Vaelfael
Vaelfin
Vaelgalad
Vaelhal
Vaelithil
Vaellen
Vaellorien
Vaelmel
Vaelmin
Vaelmir
Vaelnaer
Vaelnim
Vaelnor
Vaeloron
Vaelquen
Vaelril
Vaelsyl
Vaeltar
Vaelthar
Vaelther
Vaeltir
Vaelvael
Vaelvil
Vaelzor
Viltir
Vilvael
Vilvil
Vilzor
Zorael
Zorar
Zorbel
Zorcalad
Zorcar
Zorel
Zoreri
Zoreryn
Zorfael
Zorfin
Zorgalad
Zorhal
Zorithil
Zorlen
Zorlorien
Zormel
Zormin
Zormir
Zornaer
Zornim
Zornor
Zororon
Zorquen
Zorril
Zorsyl
Zortar
Zorthar
Zorther
Zortir
Zorvael
Zorvil
Zorzor"""

            DWARF_BLOCK = """Baldar
Baldir
Balin
Balkir
Balkor
Balmir
Balmor
Bjorn
Bodrek
Bodrik
Bodrim
Bolgar
Bolgrim
Bolgrin
Bolmar
Bolrik
Bolthor
Borin
Bormak
Bormek
Bormir
Braldik
Braldir
Bralmir
Bramdar
Bramdir
Bramnor
Bramrik
Brathar
Brathrik
Bravik
Bravmir
Brekmir
Brenrik
Brenvir
Brodin
Brodran
Brodrek
Brodrin
Brogin
Broldir
Brolmir
Brolrik
Brommir
Brondak
Brondar
Brondir
Brornak
Brundrik
Bruni
Brynjar
Dagmir
Dalkor
Dolvak
Dorak
Dorgrek
Dorgrim
Dorgun
Dornak
Dorvar
Drakar
Drakkar
Drakkun
Dralgin
Dralgor
Dralin
Dralmuk
Dravik
Dravor
Dravrin
Drek
Drekgar
Drekir
Drekk
Drekmar
Drekthar
Drekthor
Drekvar
Drellor
Drelvik
Dremor
Drokir
Drokmar
Dronak
Drovik
Drudvik
Drundor
Dulrik
Dundar
Dundor
Dundrik
Dunrik
Durak
Durgrim
Durin
Durn
Durnak
Durnik
Durnir
Durnok
Durnor
Dvalin
Eldrin
Faldor
Faldorn
Faldrek
Faldrin
Falthor
Fandrik
Farin
Fendrak
Fendrik
Frodrik
Frogrin
Froldar
Froldir
Froldor
Froldrak
Froldrik
Froldrin
Furdrin
Furgrim
Garrik
Gildrak
Gildrin
Gimli
Glorgrim
Gondor
Gordrik
Gorik
Gormak
Gorn
Gornik
Gothrak
Gothrek
Gralor
Gravmir
Gravorn
Grendor
Grendrik
Grimnir
Grimthar
Grimthor
Grolmir
Grom
Gromdar
Gromlor
Grommorn
Gromnir
Gromrik
Grundak
Grundal
Grundar
Grundin
Grundor
Grundrik
Grundrin
Grunrik
Guldar
Guldir
Guldrek
Gundal
Gundorin
Gundram
Gundrik
Gundril
Gundur
Haldor
Harmir
Harnok
Helgrim
Helgrin
Hjaldor
Hjalgar
Hjalgrim
Hjalrik
Hjorgrim
Hjorik
Horak
Horgrim
Horgrin
Horvak
Hraldik
Hraldin
Hraldor
Hralgar
Hralgorn
Hralmir
Hrodir
Hrodor
Hrogan
Hroldor
Hroldrin
Hromdar
Hromgar
Hromir
Hrothgar
Hrunar
Huldan
Huldar
Huldrek
Hulfran
Hulgrim
Jaldrik
Jalgrim
Jolgar
Jolgrim
Jolrik
Jondar
Jondrik
Jorgrim
Jorgrun
Jorik
Jorlin
Jormak
Jormar
Jornak
Jornik
Jornor
Jorvak
Jorvar
Jorvin
Juldar
Juldrek
Kaldur
Kalthor
Karn
Keldor
Keldrim
Khaldir
Khorgrim
Khorin
Khortek
Kolrik
Kormak
Kradmir
Kradorn
Kragmar
Kragor
Kranmir
Kranthor
Krogar
Kroldor
Kroldrin
Krolmir
Krolthor
Kromar
Krondar
Krondrin
Krumdor
Krumrik
Krunmir
Krunthor
Kundrak
Kundrik
Kurgan
Logrin
Magnar
Maldor
Moldar
Moldrin
Mordrin
Morthar
Mundar
Mundorin
Mundrik
Olfar
Orik
Ragnar
Ragnik
Rognir
Roknar
Rordain
Rordak
Rordrik
Rorgim
Rormir
Rudrin
Rulgar
Runrik
Rurik
Sindri
Skaldir
Skaldor
Skarn
Skarnak
Skarnor
Skondar
Skondrik
Skorgrim
Skorvik
Skroth
Sorin
Stenrik
Stoldrin
Tarkor
Tarkrin
Thalgrim
Thandor
Tharnor
Thodir
Thokar
Thokrin
Tholmir
Thorgal
Thorgim
Thraen
Thragnir
Thrain
Thraldir
Thralgon
Thralmir
Thravik
Thredor
Thredrin
Threknar
Threknor
Throdar
Throgar
Throgun
Throldan
Throldar
Throlmir
Thromm
Thromrik
Throndir
Thror
Throrin
Throruk
Thrukmar
Thrundak
Thrundor
Thrundrik
Thrundrin
Thuldar
Thuldor
Thuldrek
Thulgrim
Thundar
Thundrik
Thurnak
Tordak
Tordrin
Torgath
Torgim
Torgrim
Torgrin
Torgron
Torgun
Torlik
Torlin
Tormek
Tornir
Torvald
Torvan
Torvil
Torvin
Trelmir
Trondir
Turgald
Turgin
Uldar
Uldin
Uldrak
Uldrin
Ulfgar
Ulfrik
Ulgrin
Ulnir
Ulrik
Vagmir
Vagnar
Valkir
Vandrik
Varkun
Varmok
Varn
Varrik
Veknar
Veldrin
Voldrak
Voldur
Vondal
Vorgald
Vorgar
Vorgim
Vorgrim
Vorgund
Vorin
Vorn
Vornak
Vornir
Vradmir
Vrakdin
Vrakgrim
Vrakmir
Vraknor
Vraknorn
Vrumdar
Vrumir
Vuldar
Vulrik
Vundar
Vundorin
Vundrik
Vundrin
Zaldrek
Zalrik
Zalthir
Zalthor
Zarkir
Zarmir
Zarnak
Zarnir
Zarnok
Zarnor
Zarthak
Zarthrin
Zarvin
Zelmir
Zelthor
Zolgrim
Zolrik
Zordrin
Zorgim
Zorgrim
Zorvak
Zulgar
Zulgrim
Zulmar
Zulmorn
Zundar
Zundor
Zundrak
Zundrek
Zundrik
Zundrin"""

            human = _dedupe(_lines(HUMAN_FEMALE_BLOCK) + _lines(HUMAN_MALE_BLOCK))
            orc = _dedupe(_lines(ORC_BLOCK))
            elf = _dedupe(_first_tokens(ELF_BLOCK))
            dwarf = _dedupe(_first_tokens(DWARF_BLOCK))

            names = {"Human": human, "Elf": elf, "Dwarf": dwarf, "Orc": orc}

            def pick_name(race, used):
                pool = names.get(race, names["Human"])
                for _ in range(200):
                    n = random.choice(pool)
                    if n not in used:
                        used.add(n)
                        return n
                return random.choice(pool)

            def format_gp(n):
                return f"{n:,.0f} gp"


            type_key = None
            if type_raw:
                k = norm_key(type_raw)
                if k in merc_types:
                    type_key = k
            if not type_key:
                type_key = "light_foot_human"

            label, race, base_cost, equip, morale = merc_types[type_key]

            if mode not in ("platoon", "company"):
                about = (
                    "Mercenaries are hired warriors organized in **platoons** (32–48 Fighters; 2–4 squads led by corporals, "
                    "plus a **sergeant** and a **lieutenant**). Companies combine **2–5 platoons** and are led by a **captain** with a **first sergeant**.\n\n"
                    "Typical levels: Troopers **1st**; **10%** of corporals **2nd**; sergeants **50%** **2nd**; lieutenants **2nd**. "
                    "Captains **2nd–4th**; first sergeants **2nd–3rd**.\n\n"
                    "Mercenaries rarely enter dungeons; they’re for outdoor warfare and garrisons."
                )
                embed = nextcord.Embed(
                    title="⚔️ Hire Mercenaries",
                    description=f"Employer: **{employer_char}**\n\n{about}",
                    color=0x8B5CF6,
                )

                lines = [f"• **{lbl}** — {cost} gp/mo per level  *(slug: `{k}`)*"
                         for k, (lbl, _, cost, _, _) in merc_types.items()]
                add_lines_field_chunked(embed, "Common Types (monthly cost per **level**)", lines)

                embed.add_field(
                    name="Stronghold Discount",
                    value=("If housed in your stronghold: **–25% monthly**. "
                           "(Each merc needs **200 sq ft**; **Elves** need **500 sq ft** for the discount.)"),
                    inline=False,
                )
                embed.add_field(
                    name="Try",
                    value=(f"`!hire mercenaries platoon type:{type_key}`\n"
                           f"`!hire mercenaries company type:{type_key} platoons:3 -stronghold`"),
                    inline=False,
                )
                await ctx.send(embed=embed)
                return

            if mode == "platoon":
                used = set()
                troopers = max(32, min(48, int(size_raw))) if (size_raw and size_raw.isdigit()) else random.randint(32, 48)
                squads = random.randint(2, 4)
                corporals = squads
                corp_levels = [2 if random.random() < 0.10 else 1 for _ in range(corporals)]
                sgt_level = 2 if random.random() < 0.50 else 1
                ltn_level = 2

                corp_names = [pick_name(race, used) for _ in range(corporals)]
                sgt_name = pick_name(race, used)
                ltn_name = pick_name(race, used)

                total_personnel = troopers + corporals + 2
                total_cost = (troopers * base_cost) \
                           + sum(base_cost * lvl for lvl in corp_levels) \
                           + base_cost * sgt_level \
                           + base_cost * ltn_level
                sh_cost = int(total_cost * 0.75) if stronghold else None

                housing_sqft = None
                if stronghold:
                    per = 500 if race == "Elf" else 200
                    housing_sqft = total_personnel * per

                embed = nextcord.Embed(
                    title=f"⚔️ Mercenary Platoon — {label}",
                    description=f"Employer: **{employer_char}**",
                    color=0x10B981,
                )
                embed.add_field(
                    name="Composition",
                    value=(f"Troopers: **{troopers}**\n"
                           f"Squads: **{squads}** (Corporals: **{corporals}**)\n"
                           f"Leaders: **1 Sergeant** (L{sgt_level}), **1 Lieutenant** (L{ltn_level})\n"
                           f"Total Personnel: **{total_personnel}**"),
                    inline=False,
                )
                corp_lines = [f"{n} (L{lvl})" for n, lvl in zip(corp_names, corp_levels)]
                add_lines_field_chunked(
                    embed, "Leadership",
                    [f"Lieutenant {ltn_name} (Fighter {ltn_level})",
                     f"Sergeant {sgt_name} (Fighter {sgt_level})",
                     "Corporals: " + ", ".join(corp_lines)]
                )
                embed.add_field(name="Type Stats", value=f"**Equipment:** {equip}\n**Morale:** {morale}", inline=False)

                cost_lines = [f"Monthly Cost (level-adjusted): **{format_gp(total_cost)}**"]
                if sh_cost is not None:
                    cost_lines.append(f"Stronghold rate (–25%): **{format_gp(sh_cost)}**")
                    cost_lines.append(f"Housing needed for discount: **{housing_sqft:,} sq ft**")
                add_lines_field_chunked(embed, "Costs", cost_lines)

                embed.add_field(
                    name="GM Handling",
                    value=("Informational only; **no gold deducted** and **no NPC sheets created**. "
                           "Mercenaries generally **won’t** enter dungeons; they’re for outdoor battles and garrisons."),
                    inline=False,
                )
                await ctx.send(embed=embed)
                return

            if mode == "company":
                used = set()
                pcount = 2 if not platoons_raw else (max(2, min(5, int(platoons_raw))) if platoons_raw.isdigit() else 2)

                cpt_level = random.randint(2, 4)
                fs_level = random.randint(2, 3)
                cpt_name = pick_name(race, used)
                fs_name = pick_name(race, used)

                platoon_summaries = []
                company_total_personnel = 0
                company_total_cost = base_cost * cpt_level + base_cost * fs_level
                lieutenants = []

                for i in range(1, pcount + 1):
                    troopers = random.randint(32, 48)
                    squads = random.randint(2, 4)
                    corporals = squads
                    corp_levels = [2 if random.random() < 0.10 else 1 for _ in range(corporals)]
                    sgt_level = 2 if random.random() < 0.50 else 1
                    ltn_level = 2

                    corp_names = [pick_name(race, used) for _ in range(corporals)]
                    sgt_name = pick_name(race, used)
                    ltn_name = pick_name(race, used)

                    total_personnel = troopers + corporals + 2
                    cost = (troopers * base_cost) \
                         + sum(base_cost * lvl for lvl in corp_levels) \
                         + base_cost * sgt_level \
                         + base_cost * ltn_level

                    company_total_personnel += total_personnel
                    company_total_cost += cost
                    lieutenants.append(ltn_name)

                    platoon_summaries.append(
                        f"Platoon {i}: {troopers} troopers, {corporals} corporals, Sgt {sgt_name} (L{sgt_level}), Lt {ltn_name} (L{ltn_level})"
                    )

                sh_cost = int(company_total_cost * 0.75) if stronghold else None
                housing_sqft = None
                if stronghold:
                    per = 500 if race == "Elf" else 200
                    housing_sqft = (company_total_personnel + 2) * per 

                embed = nextcord.Embed(
                    title=f"🏰 Mercenary Company — {label}",
                    description=f"Employer: **{employer_char}**  • Platoons: **{pcount}**",
                    color=0xF59E0B,
                )
                embed.add_field(
                    name="Company Command",
                    value=(f"Captain {cpt_name} (Fighter {cpt_level})\n"
                           f"First Sergeant {fs_name} (Fighter {fs_level})\n"
                           f"Lieutenants: {', '.join(lieutenants)}"),
                    inline=False,
                )
                add_lines_field_chunked(embed, "Platoons", platoon_summaries)
                embed.add_field(name="Type Stats", value=f"**Equipment:** {equip}\n**Morale:** {morale}", inline=False)

                cost_lines = [f"Monthly Cost (level-adjusted): **{format_gp(company_total_cost)}**"]
                if sh_cost is not None:
                    cost_lines.append(f"Stronghold rate (–25%): **{format_gp(sh_cost)}**")
                    cost_lines.append(f"Housing needed for discount: **{housing_sqft:,} sq ft**")
                add_lines_field_chunked(embed, "Costs", cost_lines)

                embed.add_field(
                    name="GM Handling",
                    value=("Informational only; **no gold deducted** and **no NPC sheets created**. "
                           "Mercenaries rarely enter dungeons; they serve in outdoor engagements and as garrisons."),
                    inline=False,
                )
                await ctx.send(embed=embed)
                return

            await ctx.send("Usage: `!hire mercenaries [platoon|company] [type:<slug>] [pc:<name>] [size:<N>] [platoons:<N>] [-stronghold]`")
            return


        if sub == "specialist":
            tokens = list(args[1:])

            pc_name = None
            for i, t in enumerate(list(tokens)):
                if t.lower().startswith("pc:"):
                    pc_name = t.split(":", 1)[1].strip()
                    tokens.pop(i)
                    break

            disp, pc_path, pc_cfg = _get_pc_cfg(self, ctx, pc_name)
            employer_char = get_compat(pc_cfg, "info", "name", fallback=disp or "(Unknown)") if pc_cfg else "(None)"

            def rng(lo, hi):
                return f"{lo}–{hi} gp/month" if lo != hi else f"{lo} gp/month"

            specialists = {
                "alchemist": {
                    "aliases": ["alchemist", "alc"],
                    "cost": rng(1000, 1000),
                    "short": "Brews potions (with sample/formula), researches new ones at 2× time/cost, or assists MU research (+15% success).",
                    "long": (
                        "An alchemist can produce potions given materials and a sample or written formula, "
                        "in the same time and for the same cost as a Magic-User. They may research new potions "
                        "at twice the time and material cost of a Magic-User, or assist a MU creating certain magic items, "
                        "adding **+15%** to success chance."
                    ),
                },
                "animal trainer": {
                    "aliases": ["animal", "trainer", "animal-trainer", "animal_trainer"],
                    "cost": rng(250, 750),
                    "short": "Trains animals (normal → lower end; exotic/monstrous or multiple types → higher). Manages up to 5 at once.",
                    "long": (
                        "Required to train riding beasts or guard brutes. One trainer can manage up to **5** animals at a time. "
                        "Training duration is GM-determined and may take years. Cost depends on breadth (one type vs several) and "
                        "whether the animals are monstrous."
                    ),
                },
                "armorer": {
                    "aliases": ["armorer", "armourer", "weaponsmith", "weapon-smith"],
                    "cost": rng(100, 500),
                    "short": "Maintains gear for troops (about 1 per 50 Fighters). Includes 1d4 apprentices; experts aid magic projects.",
                    "long": (
                        "For maintaining arms and armor, assume roughly **1 armorer per 50 Fighters**. Pricing includes apprentices (**1d4**). "
                        "Specialist armorers/weaponsmiths who can assist with **magic arms/armor** command higher rates and seldom do routine upkeep."
                    ),
                },
                "engineer": {
                    "aliases": ["engineer", "eng"],
                    "cost": rng(750, 750),
                    "short": "Needed for fortresses, ships, and large mundane works. Big projects may need several.",
                    "long": (
                        "Any large construction (fortress, ship, siege works, etc.) requires an engineer. "
                        "The GM may require multiple engineers depending on project scale."
                    ),
                },
                "savant": {
                    "aliases": ["savant", "sage"],
                    "cost": rng(1500, 1500),
                    "short": "Scholar of obscure/ancient lore. Base cost maintains library; hard questions may add research/material fees.",
                    "long": (
                        "Savants are experts in obscure knowledge, often in narrow fields, but with broad access to facts. "
                        "The listed cost maintains their library and collections; **difficult questions** may require additional fees for materials or research."
                    ),
                },
                "ship": {
                    "aliases": ["ship", "ships", "crew", "ship's", "ship's crew", "sailors"],
                    "cost": None,
                    "short": "Crew costs per role: Captain 300, Navigator 200, Sailor 10, Rower 3 gp/month.",
                    "long": (
                        "**Monthly costs:** Captain **300 gp**, Navigator **200 gp**, Sailor **10 gp**, Rower **3 gp**.\n"
                        "Generally normal men, lightly armed. A PC may captain, but if inexperienced, regular sailors suffer **–2 Morale**."
                    ),
                },
                "captain": {
                    "aliases": ["captain", "skipper"],
                    "cost": rng(300, 300),
                    "short": "Ship captain.",
                    "long": "Captain of a vessel; usually a normal man; if a PC captains without experience, apply **–2 Morale** to regular sailors.",
                },
                "navigator": {
                    "aliases": ["navigator", "nav"],
                    "cost": rng(200, 200),
                    "short": "Plots courses out of sight of land.",
                    "long": "Required for voyages out of sight of land; ensures the vessel keeps to its intended course.",
                },
                "sailor": {
                    "aliases": ["sailor", "sailors", "seaman", "seamen"],
                    "cost": rng(10, 10),
                    "short": "General crew.",
                    "long": "General crew; typically normal men, unarmored, lightly armed.",
                },
                "rower": {
                    "aliases": ["rower", "rowers", "oarsman", "oarsmen"],
                    "cost": rng(3, 3),
                    "short": "Oarsman for galleys.",
                    "long": "Needed aboard galleys; typically normal men.",
                },
            }

            raw_key = " ".join(tokens).strip().lower()
            resolved = None
            if raw_key:
                for k, v in specialists.items():
                    if raw_key == k or raw_key in v["aliases"]:
                        resolved = k
                        break

            embed = nextcord.Embed(
                title="🧰 Hire a Specialist",
                description=f"Employer: **{employer_char}**  • Specialists are not limited by CHA.",
                color=0x3B82F6
            )

            def add_ship_table(e):
                e.add_field(
                    name="Ship’s Crew (monthly costs)",
                    value=(
                        "• **Captain** — 300 gp/month\n"
                        "• **Navigator** — 200 gp/month\n"
                        "• **Sailor** — 10 gp/month\n"
                        "• **Rower** — 3 gp/month"
                    ),
                    inline=False
                )
                e.add_field(
                    name="Notes",
                    value=(
                        "Generally normal men, unarmored and lightly armed. "
                        "A PC may captain, but if inexperienced, apply **–2 Morale** to regular sailors."
                    ),
                    inline=False
                )

            if not resolved:
                embed.add_field(name="Alchemist", value=f"{specialists['alchemist']['cost']} — {specialists['alchemist']['short']}", inline=False)
                embed.add_field(name="Animal Trainer", value=f"{specialists['animal trainer']['cost']} — {specialists['animal trainer']['short']}", inline=False)
                embed.add_field(name="Armorer / Weaponsmith", value=f"{specialists['armorer']['cost']} — {specialists['armorer']['short']}", inline=False)
                embed.add_field(name="Engineer", value=f"{specialists['engineer']['cost']} — {specialists['engineer']['short']}", inline=False)
                embed.add_field(name="Savant", value=f"{specialists['savant']['cost']} — {specialists['savant']['short']}", inline=False)
                add_ship_table(embed)
                embed.set_footer(text="Tip: Try e.g.  !hire specialist engineer   or   !hire specialist alchemist pc:<name>")
                await ctx.send(embed=embed)
                return

            spec = specialists[resolved]
            title_map = {
                "alchemist": "Alchemist",
                "animal trainer": "Animal Trainer",
                "armorer": "Armorer / Weaponsmith",
                "engineer": "Engineer",
                "savant": "Savant",
                "ship": "Ship’s Crew",
                "captain": "Ship Captain",
                "navigator": "Navigator",
                "sailor": "Sailor",
                "rower": "Rower",
            }
            embed = nextcord.Embed(
                title=f"🧰 Hire a Specialist: {title_map.get(resolved, resolved.title())}",
                description=f"Employer: **{employer_char}**",
                color=0x2FA84F
            )
            if resolved == "ship":
                add_ship_table(embed)
            else:
                if spec["cost"]:
                    embed.add_field(name="Monthly Cost", value=spec["cost"], inline=False)
                embed.add_field(name="What they do", value=spec["long"], inline=False)

            embed.add_field(
                name="GM Handling",
                value="This command **does not deduct gold** or create NPCs; treat these rates as guidance and let the GM finalize contracts and logistics.",
                inline=False
            )
            await ctx.send(embed=embed)
            return

        if sub != "retainer":
            await ctx.send(
                "Usage:\n"
                "• `!hire retainer [pc:<name>] [±N]`\n"
                "• `!hire specialist [type] [pc:<name>]`\n"
                "• `!hire mercenaries [platoon|company] [type:<slug>] [pc:<name>] [size:<N>] [platoons:<N>] [-stronghold]`"
            )
            return

        tokens = list(args[1:])
        offer_mod = 0

        for t in list(tokens):
            if re.fullmatch(r"[+-]\d+", t):
                offer_mod += int(t)
                tokens.remove(t)

        pc_name = None
        for i, t in enumerate(list(tokens)):
            if t.lower().startswith("pc:"):
                pc_name = t.split(":", 1)[1].strip()
                tokens.pop(i)
                break

        disp, pc_path, pc_cfg = _get_pc_cfg(self, ctx, pc_name)
        if not pc_cfg:
            await ctx.send("❌ No active/valid PC. Use `!char <name>` or pass `pc:<name>`.")
            return

        lvl_s = (
            get_compat(pc_cfg, "info", "level", fallback="") or
            get_compat(pc_cfg, "info", "lvl",   fallback="") or
            get_compat(pc_cfg, "cur",  "level", fallback="") or
            get_compat(pc_cfg, "cur",  "lvl",   fallback="")
        )
        pc_level = _safe_int(lvl_s)
        employer_char = get_compat(pc_cfg, "info", "name", fallback=disp or "(Unknown)")

        if pc_level <= 1:
            embed = nextcord.Embed(
                title="🧑‍🤝‍🧑 Hiring a Retainer",
                description=f"Employer: **{employer_char}**",
                color=0xCC3333
            )
            embed.add_field(
                name="Level Gate",
                value="**Level 1 characters cannot hire retainers.** Gain reputation first, then try again.",
                inline=False
            )
            embed.set_footer(text="Rule of thumb: highest retainer level allowed is ⌊PC level ÷ 2⌋.")
            await ctx.send(embed=embed)
            return

        cha_mod = _safe_int(pc_cfg.get('stats', 'cha_modifier', fallback="0"))
        have, cap = _count_retainers_for_pc(employer_char), _retainer_cap(pc_cfg)
        if have >= cap:
            await ctx.send(f"❌ Retainer limit for **{employer_char}** reached (**{have}/{cap}**).")
            return

        try:
            total, rolls, flat = roll_dice("2d6")
            d1, d2 = (rolls + [0, 0])[:2]
            base = total + flat
        except Exception:
            d1, d2 = random.randint(1, 6), random.randint(1, 6)
            base = d1 + d2

        adj = base + cha_mod + offer_mod
        band, note, bump = _reaction_band(adj)
        color = 0x2FA84F if band == "accept" else (0x3B82F6 if band == "try_again" else 0xCC3333)

        embed = nextcord.Embed(
            title="🧑‍🤝‍🧑 Hiring a Retainer",
            description=f"Employer: **{employer_char}**  • Cap: **{have}/{cap}**",
            color=color
        )
        mod_line = f"2d6 → {d1}+{d2} = **{base}**  + CHA({cha_mod:+d}) + OFFER({offer_mod:+d}) → **{adj}**"
        embed.add_field(name="Reaction", value=mod_line, inline=False)

        highest_allowed = pc_level // 2
        embed.add_field(name="Highest Retainer Level Allowed", value=f"**{highest_allowed}** (⌊{pc_level} ÷ 2⌋)", inline=False)

        if band == "try_again":
            embed.add_field(name="Result", value=f"**{note}**  (e.g., `!hire retainer +1`)", inline=False)
            await ctx.send(embed=embed)
            return

        if band.startswith("refusal"):
            embed.add_field(name="Result", value=f"**{note}**", inline=False)
            await ctx.send(embed=embed)
            return

        loyalty = max(2, min(12, 7 + cha_mod + (1 if bump else 0)))
        embed.add_field(name="Result", value=f"**{note}**", inline=False)
        embed.add_field(
            name="Next step",
            value=(
                "Pick the name/race/class for the retainer, then create them as an NPC.\n"
                f"**Example:** `!charcreate <Name> <Race> <Class> [sex] -npc -loyalty {loyalty}`\n"
                "_(Run this while your hiring PC is active so we tag the employer automatically.)_"
            ),
            inline=False
        )
        await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(SheetCog(bot))
