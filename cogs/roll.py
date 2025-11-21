import os
import random
from nextcord.ext import commands
from utils.players import get_active
import nextcord
import re
import configparser

def load_class_list(filename):
    config = configparser.ConfigParser()
    config.read(filename)
    result = {}
    for section in config.sections():
        result[section] = {}
        for key in config[section]:
            result[section][key] = config[section][key]
    return result

def load_race_list(filename):
    config = configparser.ConfigParser()
    config.read(filename)
    result = {}
    for section in config.sections():
        result[section] = {}
        for key in config[section]:
            result[section][key] = config[section][key]
    return result

def read_cfg(filename):
    config = configparser.ConfigParser()
    config.read(filename)
    return config

def get_compat(config, section, key, fallback=None):
    try:
        return config.get(section, key)
    except (configparser.NoOptionError, configparser.NoSectionError):
        return fallback

def getint_compat(config, section, key, fallback=0):
    try:
        return config.getint(section, key)
    except (configparser.NoOptionError, configparser.NoSectionError, ValueError):
        return fallback

def _equipped_protection_bonus(cfg) -> int:
    """
    Read equipped items and return the highest Protection +N (Ring/Cloak/Belt/Pendant).
    Scans eq.* values like 'ring', 'ring2', 'cloak', 'belt', 'pendant', 'neck', 'amulet'.
    """
    try:
        if not cfg.has_section("eq"):
            return 0
    except Exception:
        return 0

    prot = 0
    try:
        candidates = []
        for opt, val in cfg.items("eq"):
            name = (val or "").strip()
            if not name:
                continue
            low = name.lower()
            if ("ofprotection" in low) and ("+" in low):
                m = re.search(r"(?:belt|cloak|ring|pendant)ofprotection\+([1-3])", low)
                if not m:
                    m = re.search(r"(?:amulet|neck)ofprotection\+([1-3])", low)
                if m:
                    try:
                        candidates.append(int(m.group(1)))
                    except Exception:
                        pass
        if candidates:
            prot = max(candidates)
    except Exception:
        prot = 0
    return max(0, int(prot or 0))

def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


_SKILL_ALIASES = {

    "openlock": "openlock",
    "openlocks": "openlock",
    "open": "openlock",
    "ol": "openlock",

    "removetrap": "removetrap",
    "remtrap": "removetrap",
    "rt": "removetrap",
    "removetrapp": "removetrap",

    "pickpocket": "pickpocket",
    "pickpockets": "pickpocket",
    "pp": "pickpocket",
    "pick": "pickpocket",

    "movesilently": "movesilently",
    "movesilent": "movesilently",
    "ms": "movesilently",
    "move": "movesilently",

    "climbwall": "climbwall",
    "climbwalls": "climbwall",
    "climb": "climbwall",
    "cw": "climbwall",

    "hide": "hide",
    "hideshadows": "hide",
    "hs": "hide",

    "listen": "listen",
    "hear": "listen",
    "ls": "listen",
}


_SKILL_LABEL = {
    "openlock": "Open Locks",
    "removetrap": "Remove Traps",
    "pickpocket": "Pick Pockets",
    "movesilently": "Move Silently",
    "climbwall": "Climb Walls",
    "hide": "Hide in Shadows",
    "listen": "Listen",
}


class_lst = load_class_list("class.lst")
race_lst = load_race_list("race.lst")


def roll_dice(spec: str):
    """
    Parse 'XdY' with optional +/- Z, e.g. '2d6+3', '1d8-1'.
    Returns (sum_of_rolls, individual_rolls, flat_modifier).
    """
    m = re.fullmatch(r"\s*(\d+)d(\d+)\s*([+-]\s*\d+)?\s*", spec.strip().lower())
    if not m:
        raise ValueError(f"Bad dice spec: {spec}")
    n = int(m.group(1))
    sides = int(m.group(2))
    flat = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    rolls = [random.randint(1, sides) for _ in range(n)]
    return sum(rolls), rolls, flat


def _norm_monster(name: str) -> str:
    return re.sub(r"[^\w]+", "", (name or "").strip().lower())

def _resolve_char_ci(name: str):
    """
    Case-insensitive, space/underscore-insensitive lookup of a creature file.
    Searches monsters/ (recursively) and the current dir. Supports .coe and .ini.
    Matches on filename OR [info] name in the file.
    Returns (display_name, absolute_path) or (None, None).
    """
    import os, re


    here = os.path.dirname(os.path.abspath(__file__))
    search_roots = [
        os.path.join(here, "monsters"),
        "monsters",
        here,
        ".",
        "/monsters",
    ]

    search_roots = [r for r in dict.fromkeys(search_roots) if os.path.isdir(r)]

    exts = (".coe", ".ini")

    def norm(s: str) -> str:

        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    target_key = norm(name)


    def display_from_path(p: str):
        try:
            cfg = read_cfg(p)
            real = get_compat(cfg, "info", "name", fallback=None)
        except Exception:
            real = None
        if real:
            return real
        return os.path.splitext(os.path.basename(p))[0].replace("_", " ")


    candidates = {
        f"{name}.coe", f"{name}.ini",
        f"{name.replace(' ', '_')}.coe", f"{name.replace(' ', '_')}.ini",
        f"{name.replace('_', ' ')}.coe", f"{name.replace('_', ' ')}.ini",
    }
    for root in search_roots:
        try:
            lowerdir = {fn.lower(): fn for fn in os.listdir(root)}
        except Exception:
            continue
        for cand in list(candidates):
            lc = cand.lower()
            if lc in lowerdir:
                p = os.path.abspath(os.path.join(root, lowerdir[lc]))
                return display_from_path(p), p


    for root in search_roots:
        try:
            for fn in os.listdir(root):
                _, ext = os.path.splitext(fn)
                if ext.lower() not in exts:
                    continue
                stem = os.path.splitext(fn)[0]
                if norm(stem) == target_key:
                    p = os.path.abspath(os.path.join(root, fn))
                    return display_from_path(p), p
        except Exception:
            pass


    for root in search_roots:
        try:
            for fn in os.listdir(root):
                _, ext = os.path.splitext(fn)
                if ext.lower() not in exts:
                    continue
                p = os.path.abspath(os.path.join(root, fn))
                try:
                    cfg = read_cfg(p)
                    real = get_compat(cfg, "info", "name", fallback="") or ""
                    if real and norm(real) == target_key:
                        return real, p
                except Exception:
                    continue
        except Exception:
            pass

    return None, None


def _read_appearing_from_cfg(cfg):

    for sec in ("base", "info", "stats"):
        try:
            v = (cfg.get(sec, "appearing", fallback="") or "").strip()
        except Exception:
            v = ""
        if v:
            return v
    return ""

def _roll_appearing(spec: str) -> tuple[int, str]:
    """
    Returns (count, pretty_breakdown). spec is either an int (e.g., "3" or "0")
    or a dice string like "1d4+1" / "2d6-1".
    """
    s = (spec or "").strip().lower()
    if not s:
        return (1, "default 1 (appearing missing)")

    if re.fullmatch(r"[+-]?\d+", s):
        n = int(s)
        return (n, f"{n}")

    m = re.fullmatch(r"\s*(\d+)d(\d+)\s*([+-]\s*\d+)?\s*", s)
    if m:
        total, rolls, flat = roll_dice(s)
        sign = f"{flat:+d}" if flat else ""
        return (max(0, total + flat), f"{m.group(1)}d{m.group(2)}{sign} [{', '.join(map(str, rolls))}]{(' ' + sign) if sign else ''} = {total + flat}")

    try:
        total, rolls, flat = roll_dice(s)
        return (max(0, total + flat), f"{s} [{', '.join(map(str, rolls))}] = {total + flat}")
    except Exception:
        return (1, f"unrecognized '{spec}', using 1")


_DUNGEON_COLS = {
    "L1":  [
        "GiantBee","Goblin","GreenJelly","Kobold","Adventurers","Bandits",
        "Orc","Stirge","Skeleton","SpittingCobra","GiantCrabSpider","Wolf"
    ],
    "L2":  [
        "GiantBombardierBeetle","GiantFly","Ghoul","Gnoll","GrayJelly","Hobgoblin",
        "LizardMan","Adventurers","PitViper","GiantBlackWidow","Troglodyte","Zombie"
    ],
    "L3":  [
        "GiantAnt","CarnivorousApe","GiantTigerBeetle","Bugbear","Doppleganger","Gargoyle",
        "GlassJelly","Wererat","Ogre","Shadow","TentacleWorm","Wight"
    ],
    "L45": [
        "CaveBear","GiantCaecilia","Cockatrice","Doppleganger","GrayJelly","Hellhound",
        "RustMonster","Werewolf","Minotaur","OchreJelly","Owlbear","Wraith"
    ],
    "L67": [
        "Basilisk","BlackJelly","GiantCaecilia","Deceiver","Hydra","RustMonster",
        "Weretiger","Mummy","Owlbear","GiantScorpion","Spectre","Troll"
    ],
    "L8p": [
        "GreaterBasilisk","Chimera","GreaterDeceiver","HillGiant","StoneGiant","Hydra",
        "BlackJelly","Wereboar","PurpleWorm","FlameSalamander","FrostSalamander","Vampire"
    ],
}

def _dungeon_bucket(avg_lvl: int) -> str | None:
    if avg_lvl <= 0:
        return None
    if avg_lvl == 1: return "L1"
    if avg_lvl == 2: return "L2"
    if avg_lvl == 3: return "L3"
    if 4 <= avg_lvl <= 5: return "L45"
    if 6 <= avg_lvl <= 7: return "L67"
    return "L8p"


_WILD = {
    "desert": {
        2:"DesertDragon", 3:"Hellhound", 4:"FireGiant", 5:"PurpleWorm",
        6:"GiantFly", 7:"GiantScorpion", 8:"Camel", 9:"GiantTarantula",
        10:"Merchants", 11:"Hawk", 12:"Bandits", 13:"Ogre", 14:"Griffon",
        15:"Gnoll", 16:"MountainDragon"
    },
    "grassland": {
        2:"PlainsDragon", 3:"Troll", 4:"GiantFly", 5:"GiantScorpion",
        6:"Bandits", 7:"Lion", 8:"WildBoar", 9:"Merchants",
        10:"Wolf", 11:"GiantBee", 12:"Gnoll", 13:"Goblin", 14:"BlinkDog",
        15:"DireWolf", 16:"HillGiant"
    },
    "jungle": {
        2:"ForestDragon", 3:"Bandits", 4:"Goblin", 5:"Hobgoblin",
        6:"GiantCentipede", 7:"Python", 8:"Elephant", 9:"Antelope",
        10:"Jaguar", 11:"Stirge", 12:"Gargoyle", 13:"GiantTigerBeetle",
        14:"Shadow", 15:"Merchants", 16:"Weretiger"
    },
    "mountain": {
        2:"IceDragon", 3:"HugeRoc", 4:"Deceiver", 5:"Werewolf",
        6:"MountainLion", 7:"Wolf", 8:"GiantCrabSpider", 9:"Hawk",
        10:"Orc", 11:"GiantBat", 12:"GiantTigerBeetle", 13:"GiantHawk",
        14:"Chimera", 15:"DireWolf", 16:"MountainDragon"
    },
    "ocean": {
        2:"SeaDragon", 3:"Hydra", 4:"SpermWhale", 5:"GiantCrocodile",
        6:"GiantCrab", 7:"KillerWhale", 8:"GiantOctopus", 9:"MakoShark",
        10:"Merchants", 11:"Bandits", 12:"BullShark", 13:"HugeRoc",
        14:"GreatWhiteShark", 15:"Mermaid", 16:"SeaSerpent"
    },
    "swamp": {
        2:"SwampDragon", 3:"Shadow", 4:"Troll", 5:"GiantDracolizard",
        6:"GiantCentipede", 7:"GiantLeech", 8:"LizardMan", 9:"Crocodile",
        10:"Stirge", 11:"Orc", 12:"GiantFrog", 13:"LizardMan",
        14:"BloodRose", 15:"HangmanTree", 16:"Basilisk"
    },
    "forest": {
        2:"ForestDragon", 3:"Alicorn", 4:"Treant", 5:"Orc",
        6:"WildBoar", 7:"BlackBear", 8:"GiantHawk", 9:"Antelope",
        10:"Wolf", 11:"Ogre", 12:"BrownBear", 13:"DireWolf",
        14:"HillGiant", 15:"Owlbear", 16:"Unicorn"
    },
}

_WILD_KEYS = {
    "desert":"desert","barren":"desert",
    "grassland":"grassland","plains":"grassland",
    "jungle":"jungle",
    "mountain":"mountain","mountains":"mountain","hill":"mountain","hills":"mountain",
    "ocean":"ocean","sea":"ocean",
    "swamp":"swamp","marsh":"swamp","bog":"swamp",
    "forest":"forest","woods":"forest"
}

def _dice_or_fallback(spec: str, lo: int = 1, hi: int = 6):
    """Use your roll_dice() if available; otherwise randint. Returns (total, rolls, flat)."""
    try:
        total, rolls, flat = roll_dice(spec)
        return total + flat, rolls, flat
    except Exception:
        m = re.fullmatch(r"(\d+)d(\d+)([+-]\d+)?", str(spec).strip())
        if m:
            nd = int(m.group(1))
            die = int(m.group(2))
            mod = int(m.group(3) or 0)
            rolls = [random.randint(1, die) for _ in range(nd)]
            return sum(rolls) + mod, rolls, mod
        r = random.randint(lo, hi)
        return r, [r], 0

_MEAT_YIELD = {
    "hare/rabbit": 1, "bird": 1, "duck": 1, "turkey": 2, "pheasant": 1,
    "goat": 3, "sheep": 4, "antelope": 4, "deer": 4, "boar": 6, "elk": 8, "moose": 12, "bison": 12,
    "fish": 1, "goose": 2, "wild pig": 6, "caribou": 6
}

_HUNT_ANIMALS = {
    "forest": [
        ("deer", "1d4"), ("boar", "1d3"), ("hare/rabbit", "2d6"),
        ("pheasant", "2d4"), ("turkey", "1d4")
    ],
    "grassland": [
        ("antelope", "1d6"), ("bison", "1d2"), ("hare/rabbit", "2d6"),
        ("bird", "2d6"), ("wild pig", "1d3")
    ],
    "hill": [
        ("goat", "1d4"), ("deer", "1d4"), ("boar", "1d3"), ("hare/rabbit", "2d6")
    ],
    "mountain": [
        ("goat", "1d4"), ("sheep", "1d4"), ("elk", "1d2"), ("bird", "2d6")
    ],
    "jungle": [
        ("boar", "1d3"), ("bird", "2d6"), ("turkey", "1d4"), ("fish", "2d6")
    ],
    "swamp": [
        ("duck", "2d6"), ("fish", "2d6"), ("boar", "1d2"), ("bird", "2d6")
    ],
    "desert": [
        ("bird", "2d4"), ("hare/rabbit", "1d6"), ("goat", "1d2") 
    ],
    "ocean": [
        ("fish", "3d6")  
    ],
}

def _pick_hunt_result(terr_key: str):
    rows = _HUNT_ANIMALS.get(terr_key, [])
    if not rows:
        return None
    animal, qty_spec = random.choice(rows)
    n, rolls, flat = _dice_or_fallback(qty_spec)
    yield_each = _MEAT_YIELD.get(animal, 1)
    return {
        "animal": animal,
        "count": max(1, int(n)),
        "qty_spec": qty_spec,
        "approx_person_days": max(1, int(n)) * yield_each,
        "rolls": rolls, "flat": flat
    }


_SPECIAL_HUMANS = {"bandits","adventurers","merchants","nobles"}

def _npcparty_note(label: str) -> str:
    key = (label or "").strip().lower()
    if key == "bandits":
        return "Humanoids encountered. For numbers and composition, run `!npcparty bandits`."
    if key == "adventurers":
        return "Adventuring party encountered. Use `!npcparty adventurers` to generate classes, levels, and headcount."
    if key == "merchants":
        return "Merchant caravan encountered. Use `!npcparty merchants` to generate guards, porters, and principals."
    if key == "nobles":
        return "Noble entourage encountered. Use `!npcparty nobles` to generate the retinue/bodyguards/attendants."
    return f"Humanoids encountered. Use `!npcparty {key}` to generate their party."

def _roll_special_humans(label: str) -> tuple[int, str, str]:

    total, rolls, flat = roll_dice("2d6")
    note = ""
    key = label.lower()
    if key == "merchants":
        note = "They might trade — consider using `!shop`."
    elif key == "adventurers":
        note = "Build as human party (mix of Fighters/Thieves/etc.)."
    else:
        note = "Humans/NPCs; build as you like (light armor, bows, etc.)."
    return (total + flat, f"2d6 [{', '.join(map(str, rolls))}] = {total + flat}", note)


class Dice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="r", aliases=["roll"])
    async def roll(self, ctx, *pieces):
        """
        Roll dice with simple math.
        Examples:
          !r 2d6+3
          !r 15d6/2
          !r 6d6*2
          !r (1d8+5)*2
          !r 2d6 + 3          # spaced math is OK
          !r 2d6+3 1d4+2      # multiple expressions
        """
        import re, random

        if not pieces:
            await ctx.send("🎲 Usage: `!r 2d6+3`, `!r 15d6/2`, or multiple like `!r 2d6+3 1d4+2`")
            return

        embed = nextcord.Embed(title="🎲 Dice Roll Results", color=nextcord.Color.blurple())
        grand_total = 0


        if any(re.fullmatch(r'^[+\-*/()]+$', p) for p in pieces):
            expressions = [''.join(pieces)]
        else:

            expressions = list(pieces)

        dice_re = re.compile(r'(?i)(\d*)d(\d+)')

        def expand_and_eval(expr: str):
            """
            Replace dice with their rolled sums, then evaluate arithmetic safely.
            Returns (total:int, details_lines:list[str], display_expr:str)
            """
            expr = expr.strip()
            details = []


            def repl(m: re.Match) -> str:
                count = int(m.group(1)) if m.group(1) else 1
                sides = int(m.group(2))

                if count <= 0 or sides <= 0:
                    raise ValueError("Dice and sides must be positive.")

                rolls = [random.randint(1, sides) for _ in range(count)]

                def fmt(r):
                    if sides == 20 and r == 20: return "**20** 🎉"
                    if sides == 20 and r == 1:  return "**1** 💀"
                    return str(r)

                total = sum(rolls)
                details.append(f"{count}d{sides} → [{', '.join(fmt(r) for r in rolls)}] = {total}")
                return str(total)


            squashed = expr.replace(" ", "")
            numeric = dice_re.sub(repl, squashed)


            safe = numeric.replace('/', '//')
            if not re.fullmatch(r'[0-9+\-*/() ]+', safe):
                raise ValueError("Invalid characters (allowed: digits, d, + - * / and parentheses).")


            try:
                result = eval(safe, {"__builtins__": None}, {})
            except Exception:
                raise ValueError("Bad math expression.")

            if not isinstance(result, (int, float)):
                raise ValueError("Expression did not produce a number.")


            if isinstance(result, float):
                result = int(result)

            display_expr = safe.replace('//', ' // ')
            return int(result), details, display_expr

        for expr in expressions:
            try:
                total, details_lines, display_expr = expand_and_eval(expr)
                grand_total += total

                lines = []
                if details_lines:
                    lines.append("\n".join(details_lines))
                lines.append(f"Math: `{display_expr}`")
                lines.append(f"Total: **{total}**")

                embed.add_field(name=expr, value="\n".join(lines), inline=False)
            except ValueError as e:
                embed.add_field(name=expr, value=f"❌ {e}", inline=False)

        embed.set_footer(text=f"Grand Total of All Rolls: {grand_total}")
        await ctx.send(embed=embed)


    @commands.command(name="save")
    async def save(self, ctx, save_key: str = None, *args):
        """
        Roll a saving throw. Supports +/- modifiers and optional target name.

        Forms:
          !save poi
          !save poi +2
          !save poi -1
          !save poi 1
          !save poi Testman
          !save poi Testman +3
          !save poi +3 Testman
        """

        key_alias = {
            "poi":"poi","poison":"poi","death":"poi","deathray":"poi",
            "wand":"wand","wands":"wand",
            "para":"para","paralysis":"para","petrify":"para","paralyze":"para",
            "breath":"breath","dragon":"breath","dragonbreath":"breath",
            "spell":"spell","spells":"spell"
        }
        labels = {
            "poi": "Death Ray or Poison",
            "wand": "Magic Wands",
            "para": "Paralysis or Petrify",
            "breath": "Dragon Breath",
            "spell": "Spells"
        }

        if save_key is None or str(save_key).strip().lower() in {"?", "help"}:
            valid = ", ".join(sorted(set(key_alias.keys())))
            embed = nextcord.Embed(
                title="Saving Throws — Help",
                description=(
                    "Usage: `!save <type> [±mod] [Char Name]`\n"
                    "Examples: `!save poi`, `!save spell +2`, `!save breath Testman -1`\n\n"
                    f"Try one of: **{valid}**\n"
                    "Canonical keys: **poi**, **wand**, **para**, **breath**, **spell**"
                ),
                color=random.randint(0, 0xFFFFFF),
            )
            embed.add_field(name="poi",    value="Death Ray or Poison", inline=True)
            embed.add_field(name="wand",   value="Magic Wands", inline=True)
            embed.add_field(name="para",   value="Paralysis or Petrify", inline=True)
            embed.add_field(name="breath", value="Dragon Breath", inline=True)
            embed.add_field(name="spell",  value="Spells", inline=True)
            await ctx.send(embed=embed)
            return

        sk = key_alias.get((save_key or "").lower().strip())
        if sk not in labels:
            valid = ", ".join(sorted(set(key_alias.keys())))
            await ctx.send(f"❌ Unknown saving throw '{save_key}'. Try one of: {valid}")
            return

        def is_int(t: str) -> bool:
            return re.fullmatch(r"[+-]?\d+", t or "") is not None

        manual_mod = 0
        char_name = None
        toks = list(args)

        if not toks:
            pass
        elif len(toks) == 1 and is_int(toks[0]):
            manual_mod = int(toks[0])
        else:
            if is_int(toks[-1]):
                manual_mod = int(toks[-1])
                char_name = " ".join(toks[:-1]).strip() or None
            elif is_int(toks[0]):
                manual_mod = int(toks[0])
                char_name = " ".join(toks[1:]).strip() or None
            else:
                char_name = " ".join(toks).strip() or None

        if char_name is None:
            char_name = get_active(ctx.author.id)
            if not char_name:
                await ctx.send("❌ No active character. Use `!char <name>` or pass a name like `!save poi Testman`.")
                return

        coe = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(coe):
            await ctx.send(f"❌ Character '{char_name}' does not exist.")
            return

        config = read_cfg(coe)
        owner_id = get_compat(config, "info", "owner_id")
        if owner_id and owner_id != str(ctx.author.id):
            await ctx.send(f"❌ You do not own '{char_name}'.")
            return

        char_class = (get_compat(config, "info", "class", fallback="") or "").strip()
        race       = (get_compat(config, "info", "race", fallback="") or "").strip()
        level      = max(1, int(getint_compat(config, "cur", "level", fallback=1)))

        cp = configparser.ConfigParser()
        cp.optionxform = str
        cp.read("class.lst")

        class_sec = None
        for sec in cp.sections():
            if sec.lower() == char_class.lower():
                class_sec = sec
                break

        source_sec = None
        if class_sec and sk in cp[class_sec]:
            source_sec = class_sec
        else:
            for sec in cp.sections():
                if sk in cp[sec]:
                    source_sec = sec
                    break

        if not source_sec:
            await ctx.send("⚠️ Could not read saving throws from class.lst.")
            return

        try:
            vals = [int(x) for x in cp[source_sec][sk].split()]
        except Exception:
            await ctx.send("⚠️ Malformed saving throw table in class.lst.")
            return

        base_dc = vals[min(level - 1, len(vals) - 1)] if vals else 0

        race_bonus = 0
        if race:
            rp = configparser.ConfigParser()
            rp.optionxform = str
            rp.read("race.lst")
            race_sec = None
            for sec in rp.sections():
                if sec.lower() == race.lower():
                    race_sec = sec
                    break
            if race_sec and sk in rp[race_sec]:
                try:
                    race_bonus = int(rp[race_sec][sk])
                except ValueError:
                    race_bonus = 0

        prot_bonus = 0
        try:
            prot_bonus = _equipped_protection_bonus(config)
        except Exception:
            prot_bonus = 0

        roll = random.randint(1, 20)
        nl = max(0, getint_compat(config, "cur", "neg_levels", fallback=0))

        sick_mag_sheet = 0
        try:
            if getint_compat(config, "cur", "sick", fallback=0) > 0:
                raw = get_compat(config, "cur", "sick_pen", fallback="-2")
                try:
                    sick_mag_sheet = abs(int(str(raw).strip()))
                except Exception:
                    sick_mag_sheet = 2
        except Exception:
            pass

        sick_mag_battle = 0
        try:
            bcfg = _load_battles()
            if bcfg:
                for chan_id in bcfg.sections():
                    try:
                        names, _ = _parse_combatants(bcfg, chan_id)
                    except Exception:
                        continue
                    key = _find_ci_name(names, char_name) or None
                    if not key:
                        continue
                    try:
                        slot = _slot(key)
                    except Exception:
                        slot = key.replace(" ", "_")
                    active = max(
                        bcfg.getint(chan_id, f"{slot}.x_stenchn", fallback=0),
                        bcfg.getint(chan_id, f"{slot}.stn",       fallback=0),
                        bcfg.getint(chan_id, f"{slot}.stench",    fallback=0),
                        bcfg.getint(chan_id, f"{slot}.sick",      fallback=0),
                    )
                    if active > 0:
                        raw = (bcfg.get(chan_id, f"{slot}.sick_pen",   fallback="") or
                               bcfg.get(chan_id, f"{slot}.stench_pen", fallback="") or "-2")
                        try:
                            sick_mag_battle = abs(int(str(raw).strip()))
                        except Exception:
                            sick_mag_battle = 2
                    break
        except Exception:
            pass

        total_penalty = nl + sick_mag_sheet + sick_mag_battle

        total = roll + race_bonus + manual_mod + prot_bonus - total_penalty
        roll_display = "**20** 🎉" if roll == 20 else ("**1** 💀" if roll == 1 else str(roll))
        result = "✅ **SUCCESS**" if total >= base_dc else "❌ **FAIL**"

        parts = []
        if race_bonus:
            parts.append(f"{'+' if race_bonus>=0 else '−'}{abs(race_bonus)} race")
        if prot_bonus:
            parts.append(f"+{prot_bonus} protection")
        if manual_mod:
            parts.append(f"{'+' if manual_mod>=0 else '−'}{abs(manual_mod)} mod")
        if nl:
            parts.append(f"−{nl} drain")
        if sick_mag_sheet:
            parts.append(f"−{sick_mag_sheet} sick")
        if sick_mag_battle:
            parts.append(f"−{sick_mag_battle} sick (aura)")
        bonus_line = " + ".join(parts) if parts else "—"


        embed = nextcord.Embed(
            title=f"{char_name}'s Saving Throw: {labels[sk]}",
            color=random.randint(0x000000, 0xFFFFFF)
        )
        embed.add_field(name="Roll", value=roll_display, inline=True)
        embed.add_field(name="Bonuses", value=bonus_line, inline=True)
        embed.add_field(name="Total vs DC", value=f"**{total}** vs **{base_dc}**", inline=True)
        embed.add_field(name="Result", value=result, inline=False)
        await ctx.send(embed=embed)


    @commands.command(name="s")
    async def thief_skill(self, ctx, skill: str = None, *, tail: str = ""):
        """
        Skill check with optional manual modifier and/or target name.

        Examples:
          !s                      # ← shows skill list w/ blurbs (class-aware)
          !s ?                    # same as above
          !s hide
          !s movesilently +15
          !s tracking Testman
          !s openlock Testman -5
          !s move                 # alias for movesilently
          !s track +10
        """

        BLENDING_ITEMS = {"beltofblending","cloakofblending","pendantofblending","ringofblending"}
        STEALTH_ITEMS  = {"bootsofstealth","pendantofstealth","ringofstealth"}

        def _norm_item_name(s: str) -> str:
            import re
            return re.sub(r"[^a-z0-9]+", "", (s or "").lower()).split("@", 1)[0]

        def _equipped_item_set(_cfg) -> set[str]:
            """Collect normalized names of all equipped items (any eq.* slot)."""
            out = set()
            try:
                if _cfg.has_section("eq"):
                    for _, val in _cfg.items("eq"):
                        v = (val or "").strip()
                        if not v:
                            continue
                        base = v.split(" (", 1)[0].strip() 
                        out.add(_norm_item_name(base))
            except Exception:
                pass
            return out


        def _norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

        _SKILL_ALIASES = {
            "openlock":"openlock","openlocks":"openlock","open":"openlock","ol":"openlock",
            "removetrap":"removetrap","removetrapp":"removetrap","rt":"removetrap","trap":"removetrap",
            "pickpocket":"pickpocket","pp":"pickpocket","steal":"pickpocket",
            "movesilently":"movesilently","movesilent":"movesilently","move":"movesilently","ms":"movesilently","silent":"movesilently",
            "climbwall":"climbwall","climbwalls":"climbwall","climb":"climbwall","cw":"climbwall",
            "hide":"hide","hid":"hide",
            "listen":"listen","hear":"listen","lis":"listen",

            "tracking":"tracking","track":"tracking","tracks":"tracking","trk":"tracking",

            "poison":"poison","poisons":"poison","craftpoison":"poison","venom":"poison",

            "":"__help__", "?":"__help__", "help":"__help__", "list":"__help__",
        }
        _SKILL_LABEL = {
            "openlock":"Open Lock",
            "removetrap":"Remove Trap",
            "pickpocket":"Pick Pockets",
            "movesilently":"Move Silently",
            "climbwall":"Climb Walls",
            "hide":"Hide in Shadows",
            "listen":"Listen",
            "tracking":"Tracking",
            "poison":"Poison",
        }
        _SKILL_META = {
            "openlock":   {"desc":"Pick mechanical locks with tools.", "aliases":["openlock","open","ol"]},
            "removetrap": {"desc":"Find and disarm traps (mechanical).", "aliases":["removetrap","trap","rt"]},
            "pickpocket": {"desc":"Lift small items from pockets/pouches.", "aliases":["pickpocket","pp","steal"]},
            "movesilently":{"desc":"Move quietly to avoid being heard.", "aliases":["movesilently","move","ms","silent"]},
            "climbwall":  {"desc":"Scale rough/sheer surfaces.", "aliases":["climbwall","climb","cw"]},
            "hide":       {"desc":"Vanish into shadows/hide in plain sight.", "aliases":["hide","hid"]},
            "listen":     {"desc":"Hear noises through doors/walls.", "aliases":["listen","hear","lis"]},
            "tracking":   {"desc":"Follow tracks and signs in the wild.", "aliases":["tracking","track","trk"]},
            "poison":     {"desc":"Prepare/craft poisons (assassin).", "aliases":["poison","craftpoison","venom"]},
        }


        want_help = False
        if skill is None:
            want_help = True
        else:
            mapped = _SKILL_ALIASES.get(_norm(skill))
            if mapped == "__help__":
                want_help = True

        def _gating_for(char_class_lc: str, race_lc: str, canon_key: str) -> tuple[bool, str]:
            """Mirror your gating rules to mark which skills the current character can use."""
            is_thiefcap = char_class_lc in {"thief", "magethief"}
            is_ranger   = (char_class_lc == "ranger")
            is_scout    = (char_class_lc == "scout")
            is_assassin = (char_class_lc == "assassin")
            is_halfling = (race_lc == "halfling")
            allow_halfling_hide = is_halfling and canon_key == "hide"

            if canon_key == "tracking":
                ok = (is_ranger or is_scout)
                return ok, "Ranger/Scout only"
            if canon_key == "poison":
                ok = is_assassin
                return ok, "Assassin only"
            if canon_key in {"movesilently"}:
                ok = (is_thiefcap or is_ranger or is_scout or is_assassin)
                return ok, "Thief, Mage/Thief, Ranger, Scout, Assassin"
            if canon_key in {"hide"}:
                ok = (is_thiefcap or is_ranger or is_scout or is_assassin or allow_halfling_hide)
                return ok, "Thief, Mage/Thief, Ranger, Scout, Assassin; Halfling natural hide"
            if canon_key == "pickpocket":
                ok = (is_thiefcap or is_assassin)
                return ok, "Thief, Mage/Thief, Assassin"
            if canon_key in {"openlock","climbwall","listen"}:
                ok = (is_thiefcap or is_scout or is_assassin)
                return ok, "Thief, Mage/Thief, Scout, Assassin"
            if canon_key == "removetrap":
                ok = is_thiefcap
                return ok, "Thief, Mage/Thief"

            return is_thiefcap, "Thief, Mage/Thief"

        async def _send_skill_help():

            char_name = get_active(ctx.author.id)
            char_class_lc = ""
            race_lc = ""
            if char_name:
                coe = f"{char_name.replace(' ', '_')}.coe"
                if os.path.exists(coe):
                    cfg = read_cfg(coe)
                    char_class_lc = (get_compat(cfg, "info", "class", fallback="") or "").strip().lower()
                    race_lc       = (get_compat(cfg, "info", "race",  fallback="") or "").strip().lower()
                    eqset = _equipped_item_set(cfg)
                    has_blending = any(n in eqset for n in BLENDING_ITEMS)
                    has_stealth  = any(n in eqset for n in STEALTH_ITEMS)


            lines = []
            able_now = []
            for key in ["openlock","removetrap","pickpocket","movesilently","climbwall","hide","listen","tracking","poison"]:
                label = _SKILL_LABEL.get(key, key.title())
                meta  = _SKILL_META.get(key, {})
                desc  = meta.get("desc","")
                als   = ", ".join(meta.get("aliases", [])[:3])
                gate_ok, gate_note = _gating_for(char_class_lc, race_lc, key) if char_class_lc else (False, "")
                if key == "hide" and has_blending:
                    gate_ok = True
                    gate_note = "Any class (via *Blending* item)"
                if key == "movesilently" and has_stealth:
                    gate_ok = True
                    gate_note = "Any class (via *Stealth* item)"

                badge = "✅" if gate_ok else "—"
                if gate_ok:
                    able_now.append(label)
                alias_stub = (f" *(aliases: {als})*" if als else "")
                gate_stub  = (f"\n  _{gate_note}_") if gate_note else ""
                lines.append(f"**{label}** {badge}\n  {desc}{alias_stub}{gate_stub}")

            title = "Thief / Wilderness Skills"
            if char_class_lc:
                pretty_class = char_class_lc.title()
                pretty_name  = char_name
                title = f"{pretty_name} — Skills ({pretty_class})"

            tip = ("Use `!s <skill> [±mod] [Char Name]` e.g. `!s hide`, `!s movesilently +10`, "
                   "`!s tracking Testman`, `!s openlock -5`")

            import random, nextcord
            embed = nextcord.Embed(title=title, color=random.randint(0, 0xFFFFFF))


            embed.add_field(name="How to use", value=tip, inline=False)
            if able_now:
                you_can = ", ".join(able_now)

                if len(you_can) <= 1024:
                    embed.add_field(name="You can use", value=you_can, inline=False)
                else:

                    chunks = [you_can[i:i+1024] for i in range(0, len(you_can), 1024)]
                    for idx, ch in enumerate(chunks, start=1):
                        embed.add_field(name=f"You can use ({idx})", value=ch, inline=False)

            skills_text = "\n\n".join(lines)


            if len(skills_text) <= 4096:
                embed.description = skills_text
            else:

                def _split_para_chunks(text, max_len=1024):
                    out, cur = [], ""
                    for para in text.split("\n\n"):
                        if len(cur) + len(para) + (2 if cur else 0) > max_len:
                            if cur: out.append(cur)
                            cur = para
                        else:
                            cur = (cur + "\n\n" + para) if cur else para
                    if cur: out.append(cur)
                    return out

                for idx, chunk in enumerate(_split_para_chunks(skills_text), start=1):
                    embed.add_field(name=f"Skills ({idx})", value=chunk, inline=False)

            await ctx.send(embed=embed)


        if want_help:
            await _send_skill_help()
            return


        manual_mod = 0
        char_name = None
        tokens = [t for t in tail.split() if t]
        mod_idx = None
        for i in range(len(tokens) - 1, -1, -1):
            if re.fullmatch(r"[+-]?\d+", tokens[i]):
                mod_idx = i
                break
        if mod_idx is not None:
            try:
                manual_mod = int(tokens[mod_idx])
            except Exception:
                manual_mod = 0
            tokens.pop(mod_idx)
        if tokens:
            char_name = " ".join(tokens)


        if char_name is None:
            char_name = get_active(ctx.author.id)
            if not char_name:
                await ctx.send("❌ No active character. Use `!char <name>` or run `!s` to see skills.")
                return

        coe = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(coe):
            await ctx.send(f"❌ Character '{char_name}' does not exist.")
            return

        config = read_cfg(coe)
        owner_id = get_compat(config, "info", "owner_id")
        if owner_id and owner_id != str(ctx.author.id):
            await ctx.send(f"❌ You do not own '{char_name}'.")
            return

        char_class = (get_compat(config, "info", "class") or "").strip().lower()
        eqset = _equipped_item_set(config)
        has_blending = any(n in eqset for n in BLENDING_ITEMS)
        has_stealth  = any(n in eqset for n in STEALTH_ITEMS)

        level      = getint_compat(config, "cur", "level", fallback=1)
        race_raw   = get_compat(config, "info", "race", fallback="")
        race       = (race_raw or "").strip()
        is_halfling = (race.lower() == "halfling")
        neg_levels = getint_compat(config, "cur", "neg_levels", fallback=0)
        pen_pct = 5 * max(0, neg_levels)


        canon_key = _SKILL_ALIASES.get(_norm(skill))
        if not canon_key or canon_key == "__help__":
            await ctx.send("❌ Unknown skill. Try: openlock, removetrap, pickpocket, movesilently, climbwall, hide, listen, tracking, poison.\nTip: run `!s` to see the skill list.")
            return


        is_thiefcap = char_class in {"thief", "magethief"}
        is_ranger   = (char_class == "ranger")
        is_scout    = (char_class == "scout")
        is_assassin = (char_class == "assassin")
        allow_halfling_hide = is_halfling and canon_key == "hide"

        allowed = False
        reason = None

        if canon_key == "tracking":
            allowed = (is_ranger or is_scout)
            if not allowed: reason = "Only **Rangers** or **Scouts** can use **Tracking**."
        elif canon_key == "poison":
            allowed = is_assassin
            if not allowed: reason = "Only **Assassins** can use **Poison**."
        elif canon_key in {"movesilently", "hide"}:
            allowed = (is_thiefcap or is_ranger or is_scout or is_assassin or allow_halfling_hide)
            if not allowed: reason = "Only **Thief**, **Mage/Thief**, **Ranger**, **Scout**, or **Assassin** can use that skill (non-thief Halflings may **Hide** at 70%)."
        elif canon_key == "pickpocket":
            allowed = (is_thiefcap or is_assassin)
            if not allowed: reason = "Only **Thief**, **Mage/Thief**, or **Assassin** can use that skill."
        elif canon_key in {"openlock","climbwall","listen"}:
            allowed = (is_thiefcap or is_scout or is_assassin)
            if not allowed: reason = "Only **Thief**, **Mage/Thief**, **Scout**, or **Assassin** can use that skill."
        elif canon_key in {"removetrap"}:
            allowed = is_thiefcap
            if not allowed: reason = "Only **Thief** or **Mage/Thief** can use that skill."
        else:
            allowed = is_thiefcap
            if not allowed: reason = "Your class can't use that skill."

        if not allowed and canon_key == "hide" and has_blending:
            allowed, reason = True, None
        if not allowed and canon_key == "movesilently" and has_stealth:
            allowed, reason = True, None

        if not allowed:
            await ctx.send(f"❌ {reason}")
            return



        if allow_halfling_hide and not (is_thiefcap or is_ranger or is_scout or is_assassin):
            base_target = 70
            target = max(1, min(99, base_target + manual_mod - pen_pct))
            roll = random.randint(1, 100)
            success = roll <= target
            roll_face = "**01** 🎉" if roll == 1 else ("**100** 💀" if roll == 100 else f"{roll:02d}")

            breakdown = [f"Natural Halfling Hide: {base_target}%"]
            if manual_mod: breakdown.append(f"{'+' if manual_mod >= 0 else ''}{manual_mod}% manual")
            if pen_pct:    breakdown.append(f"−{pen_pct}% drain ({neg_levels} NL)")

            label = _SKILL_LABEL.get(canon_key, canon_key.title())
            embed = nextcord.Embed(title=f"{char_name} — {label}", color=random.randint(0, 0xFFFFFF))
            embed.add_field(name="Roll (d100)", value=roll_face, inline=True)
            embed.add_field(name="Target", value=f"{target}%", inline=True)
            embed.add_field(name="Breakdown", value="\n".join(breakdown), inline=False)
            embed.add_field(name="Result", value=("✅ **SUCCESS**" if success else "❌ **FAIL**"), inline=False)
            await ctx.send(embed=embed)
            return


        cp = configparser.ConfigParser(); cp.optionxform = str; cp.read("class.lst")

        class_sec = None
        for sec in cp.sections():
            if sec.lower() == char_class:
                class_sec = sec
                break


        skill_section = None
        if class_sec and canon_key in cp[class_sec]:
            skill_section = class_sec
        elif canon_key not in {"tracking", "poison"}:
            for sec in cp.sections():
                if sec.lower() == "thief" and canon_key in cp[sec]:
                    skill_section = sec
                    break


        if skill_section:
            try:
                vals = [int(x) for x in cp[skill_section][canon_key].split()]
            except Exception:
                await ctx.send("⚠️ Malformed skill table in class.lst.")
                return
            base = vals[min(max(level - 1, 0), len(vals) - 1)] if vals else 0
            source_note = f"{skill_section}"
        else:
            base = 0
            source_note = "—"


        bonus = 0
        if race:
            rp = configparser.ConfigParser(); rp.optionxform = str; rp.read("race.lst")
            race_sec = None
            for sec in rp.sections():
                if sec.lower() == race.lower():
                    race_sec = sec
                    break
            if race_sec:
                bonus_keys = [canon_key] + (["climb"] if canon_key == "climbwall" else [])
                for bk in bonus_keys:
                    if bk in rp[race_sec]:
                        try:
                            bonus = int(rp[race_sec][bk]); break
                        except ValueError:
                            pass

        base_with_bonus = max(1, min(99, base + bonus))
        final_target = base_with_bonus

        breakdown_lines = [f"{source_note}: {base}%"]
        if bonus: breakdown_lines.append(f"{'+' if bonus >= 0 else ''}{bonus}% race")

        item_floor = None
        item_label = None
        if canon_key == "hide" and has_blending:
            item_floor, item_label = 80, "Blending"
        elif canon_key == "movesilently" and has_stealth:
            item_floor, item_label = 90, "Stealth"

        if item_floor is not None:
            if base_with_bonus < item_floor:
                breakdown_lines.append(f"Item ({item_label}): {item_floor}% floor")
            else:
                breakdown_lines.append(f"Item ({item_label}): {item_floor}% floor (lower than table, ignored)")
            base_with_bonus = max(base_with_bonus, item_floor)

            final_target = base_with_bonus
            
        if is_halfling and canon_key == "hide":
            nat = 70
            breakdown_lines.append(f"Natural Halfling: {nat}%")
            if nat > final_target:
                final_target = nat
                breakdown_lines.append("Using **70%** (higher)")
            else:
                breakdown_lines.append("Using existing higher value")



        if manual_mod:
            breakdown_lines.append(f"{'+' if manual_mod >= 0 else ''}{manual_mod}% manual")


        if pen_pct:
            breakdown_lines.append(f"−{pen_pct}% drain ({neg_levels} NL)")

        target = max(1, min(99, final_target + manual_mod - pen_pct))


        roll = random.randint(1, 100)
        success = roll <= target
        roll_face = "**01** 🎉" if roll == 1 else ("**100** 💀" if roll == 100 else f"{roll:02d}")

        label = _SKILL_LABEL.get(canon_key, canon_key.title())
        embed = nextcord.Embed(title=f"{char_name} — {label}", color=random.randint(0x000000, 0xFFFFFF))
        embed.add_field(name="Roll (d100)", value=roll_face, inline=True)
        embed.add_field(name="Target", value=f"{target}%", inline=True)
        embed.add_field(name="Breakdown", value="\n".join(breakdown_lines), inline=False)
        embed.add_field(name="Result", value=("✅ **SUCCESS**" if success else "❌ **FAIL**"), inline=False)
        await ctx.send(embed=embed)


    @commands.command(name="c", aliases=["check","ability","abilitycheck"])
    async def ability_check(self, ctx, ability: str = None, *args):
        """
        Roll an Ability Check (BX-style roll-under on 1d20).
        • Succeeds if (d20 ± mod) <= Ability score.
        • Natural 1 = automatic success. Natural 20 = automatic failure.
        • Optional: pass a character name and/or a numeric modifier.

        Examples:
          !c str
          !c constitution
          !c str -5
          !c dex Testman +2
          !c wis +3 Testman
        """

        abil_alias = {
            "str":"str","strength":"str",
            "dex":"dex","dexterity":"dex",
            "con":"con","constitution":"con",
            "int":"int","intelligence":"int",
            "wis":"wis","wisdom":"wis",
            "cha":"cha","charisma":"cha",
        }
        labels = {
            "str":"Strength",
            "dex":"Dexterity",
            "con":"Constitution",
            "int":"Intelligence",
            "wis":"Wisdom",
            "cha":"Charisma",
        }

        if ability is None or str(ability).strip().lower() in {"?", "help"}:
            valid = ", ".join(sorted(set(abil_alias.keys())))
            embed = nextcord.Embed(
                title="Ability Checks — Help",
                description=(
                    "Usage: `!c <ability> [±mod] [Char Name]`\n"
                    "Examples: `!c str`, `!c wis +2`, `!c dex Testman -1`\n\n"
                    f"Try one of: **{valid}**\n"
                    "Canonical keys: **str**, **dex**, **con**, **int**, **wis**, **cha**"
                ),
                color=random.randint(0, 0xFFFFFF),
            )
            for k in ["str","dex","con","int","wis","cha"]:
                embed.add_field(name=k, value=labels[k], inline=True)
            await ctx.send(embed=embed)
            return

        ak = abil_alias.get((ability or "").strip().lower())
        if ak not in labels:
            valid = ", ".join(sorted(set(abil_alias.keys())))
            await ctx.send(f"❌ Unknown ability '{ability}'. Try one of: {valid}")
            return

        def is_int(t: str) -> bool:
            return re.fullmatch(r"[+-]?\d+", t or "") is not None

        manual_mod = 0
        char_name = None
        toks = list(args)

        if not toks:
            pass
        elif len(toks) == 1 and is_int(toks[0]):
            manual_mod = int(toks[0])
        else:
            if is_int(toks[-1]):
                manual_mod = int(toks[-1])
                char_name = " ".join(toks[:-1]).strip() or None
            elif is_int(toks[0]):
                manual_mod = int(toks[0])
                char_name = " ".join(toks[1:]).strip() or None
            else:
                char_name = " ".join(toks).strip() or None

        if char_name is None:
            char_name = get_active(ctx.author.id)
            if not char_name:
                await ctx.send("❌ No active character. Use `!char <name>` or pass a name like `!c str Testman`.")
                return

        coe = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(coe):
            await ctx.send(f"❌ Character '{char_name}' does not exist.")
            return

        config = read_cfg(coe)

        owner_id = get_compat(config, "info", "owner_id")
        if owner_id and owner_id != str(ctx.author.id):
            await ctx.send(f"❌ You do not own '{char_name}'.")
            return

        val_raw = None
        try:
            val_raw = self._gc(config, ak)
        except Exception:
            pass

        ability_score = None
        try:
            if val_raw is not None and str(val_raw).strip() != "":
                ability_score = int(str(val_raw).strip())
        except Exception:
            ability_score = None

        if ability_score is None:
            ability_score = (
                getint_compat(config, "stats", ak, fallback=None)
                or getint_compat(config, "base", ak, fallback=None)
                or 10
            )

        d20 = random.randint(1, 20)
        raw_display = "**20** 💀" if d20 == 20 else ("**1** 🎉" if d20 == 1 else str(d20))

        auto_success = (d20 == 1)
        auto_failure = (d20 == 20)
        effective = d20 + (manual_mod or 0)

        if auto_success:
            result = "✅ **SUCCESS** *(natural 1)*"
        elif auto_failure:
            result = "❌ **FAIL** *(natural 20)*"
        else:
            result = "✅ **SUCCESS**" if effective <= ability_score else "❌ **FAIL**"

        mod_line = ("—" if not manual_mod else f"{'+' if manual_mod>=0 else '−'}{abs(manual_mod)} mod")

        embed = nextcord.Embed(
            title=f"{char_name}'s Ability Check: {labels[ak]} (≤ {ability_score})",
            color=random.randint(0, 0xFFFFFF),
        )
        embed.add_field(name="Roll", value=raw_display, inline=True)
        embed.add_field(name="Modifier", value=mod_line, inline=True)
        if not (auto_success or auto_failure):
            embed.add_field(name="Effective vs Target", value=f"**{effective}** vs **{ability_score}**", inline=True)
        else:
            embed.add_field(name="Effective vs Target", value="—", inline=True)
        embed.add_field(name="Result", value=result, inline=False)

        if manual_mod:
            tip = "*(Negative mod lowers the roll = easier; positive raises the roll = harder.)*"
            embed.add_field(name="Note", value=tip, inline=False)

        await ctx.send(embed=embed)



    @commands.command(name="e", aliases=["encounter"])
    async def encounter(self, ctx, *args):
        """
        Usage:
          !e <monster>                      → roll its [base].appearing
          !e dungeon <level> [-f] [-p]      → wandering check; use -f to force encounter, -p to post publicly
          !e <terrain> [-f] [-p]            → desert/grassland/jungle/mountain/hill/ocean/swamp/forest
          !e town        [-p]               → 2d6 town RP prompt
        """
        import math

        if not args:
            await self._enc_send(ctx, content="❌ Usage: `!e <monster>` | `!e dungeon <avg-level> [-f] [-p]` | `!e <terrain> [-f] [-p]` | `!e town [-p]`", public=False)
            return


        force = any(str(a).lower() in {"-f", "-force"} for a in args)
        public = any(str(a).lower() in {"-p", "-public"} for a in args)
        args = tuple(a for a in args if str(a).lower() not in {"-f", "-force", "-p", "-public"})

        first = (args[0] or "").strip().lower()


        if first in {"town","city"}:
            await self._encounter_town(ctx, public=public)
            return


        if first == "dungeon":
            if len(args) < 2:
                await self._enc_send(ctx, content="⚠️ `!e dungeon` requires an **average party level**. Example: `!e dungeon 3`", public=public)
                return
            try:
                lvl = int(str(args[1]).strip())
            except Exception:
                await self._enc_send(ctx, content="⚠️ Average party level must be a number. Example: `!e dungeon 3`", public=public)
                return
            await self._encounter_dungeon(ctx, lvl, force=force, public=public)
            return


        terr_key = _WILD_KEYS.get(first)
        if terr_key:
            await self._encounter_wilderness(ctx, terr_key, force=force, public=public)
            return


        monster = " ".join(args).strip()
        await self._encounter_single_monster(ctx, monster, public=public)


    async def _encounter_single_monster(self, ctx, monster_name: str, public: bool = False):
        disp, path = _resolve_char_ci(monster_name)
        embed = nextcord.Embed(title=f"Encounter: {disp or monster_name}", color=random.randint(0, 0xFFFFFF))
        if not path:
            embed.description = "❌ Monster not found."
            await self._enc_send(ctx, embed=embed, public=public); return

        cfg = read_cfg(path)
        spec = _read_appearing_from_cfg(cfg) or "1"
        if str(spec).strip() == "0":
            embed.add_field(name="Appearing", value="0 → **Never appears randomly** (summoned/placed only).", inline=False)
            await self._enc_send(ctx, embed=embed, public=public); return

        n, pretty = _roll_appearing(spec)
        embed.add_field(name="Appearing", value=f"{pretty}", inline=False)
        embed.add_field(name="Result", value=f"👉 **{n} {disp}**", inline=False)
        await self._enc_send(ctx, embed=embed, public=public)


    async def _encounter_dungeon(self, ctx, avg_lvl: int, force: bool = False, public: bool = False):
        bucket = _dungeon_bucket(avg_lvl)
        if not bucket:
            await self._enc_send(ctx, content="⚠️ Average level must be ≥ 1. Example: `!e dungeon 3`", public=public)
            return


        if not force:
            s, rolls, flat = roll_dice("1d6")
            if s + flat != 1:
                embed = nextcord.Embed(
                    title=f"Dungeon Wandering Check (party level {avg_lvl})",
                    description=f"Rolled **{s + flat}** on 1d6 → **No encounter**.",
                    color=0x777777
                )
                await self._enc_send(ctx, embed=embed, public=public); return

        d12, r12, f12 = roll_dice("1d12")
        idx = (d12 + f12) - 1
        col = _DUNGEON_COLS[bucket]
        chosen = col[idx]

        shown_monster = chosen
        count_text = ""
        note = None

        if _norm_monster(chosen) in _SPECIAL_HUMANS:
            note = _npcparty_note(chosen)
            count_text = ""
        else:
            tries = 0
            while tries < 10:
                disp, path = _resolve_char_ci(chosen)
                if not path:
                    note = "Monster file not found; using **1**."
                    count_text = f"**1 {disp or chosen}**"
                    shown_monster = disp or chosen
                    break
                cfg = read_cfg(path)
                spec = _read_appearing_from_cfg(cfg) or "1"
                if str(spec).strip() == "0":
                    d12, r12, f12 = roll_dice("1d12")
                    idx = (d12 + f12) - 1
                    chosen = col[idx]
                    tries += 1
                    continue
                n, pretty = _roll_appearing(spec)
                shown_monster = disp or chosen
                count_text = f"**{n} {shown_monster}** ({pretty})"
                break
            else:
                note = "Encounter avoided: table repeatedly selected creatures with `appearing = 0`."

        forced_txt = " *(forced)*" if force else ""
        embed = nextcord.Embed(
            title=f"Dungeon Encounter (party level {avg_lvl})",
            color=random.randint(0, 0xFFFFFF)
        )
        embed.add_field(name="Wandering Check", value=f"1d6 → **1** (encounter!){forced_txt}", inline=False)
        embed.add_field(name="Table Roll", value=f"d12 → **{idx+1}** → **{shown_monster}**", inline=False)
        if count_text:
            embed.add_field(name="Appearing", value=count_text, inline=False)
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        await self._enc_send(ctx, embed=embed, public=public)


    async def _encounter_wilderness(self, ctx, terr_key: str, force: bool = False, public: bool = False):
        if not force:
            s, rolls, flat = roll_dice("1d6")
            if s + flat != 1:
                embed = nextcord.Embed(
                    title=f"Wilderness Wandering Check — {terr_key.title()}",
                    description=f"Rolled **{s + flat}** on 1d6 → **No encounter**.",
                    color=0x777777
                )
                await self._enc_send(ctx, embed=embed, public=public); return

        t, r2d8, f2d8 = roll_dice("2d8")
        picked = _WILD[terr_key].get(t + f2d8)
        if not picked:
            embed = nextcord.Embed(
                title=f"Wilderness Encounter — {terr_key.title()}",
                description=f"2d8 → **{t + f2d8}** but no mapping on table.",
                color=0xAA0000
            )
            await self._enc_send(ctx, embed=embed, public=public); return

        forced_txt = " *(forced)*" if force else ""
        embed = nextcord.Embed(
            title=f"Wilderness Encounter — {terr_key.title()}",
            color=random.randint(0, 0xFFFFFF)
        )
        embed.add_field(name="Wandering Check", value=f"1d6 → **1** (encounter!){forced_txt}", inline=False)
        embed.add_field(name="Table Roll", value=f"2d8 → **{t + f2d8}** → **{picked}**", inline=False)

        if _norm_monster(picked) in _SPECIAL_HUMANS:
            n, pretty, note = _roll_special_humans(picked)
            embed.add_field(name="Appearing", value=f"**{n} {picked}** ({pretty})", inline=False)
            if note:
                embed.add_field(name="Note", value=note, inline=False)
            await self._enc_send(ctx, embed=embed, public=public); return

        disp, path = _resolve_char_ci(picked)
        if not path:
            embed.add_field(name="Appearing", value="File not found → **1** by default.", inline=False)
            embed.add_field(name="Result", value=f"👉 **1 {disp or picked}**", inline=False)
            await self._enc_send(ctx, embed=embed, public=public); return

        cfg = read_cfg(path)
        spec = _read_appearing_from_cfg(cfg) or "1"
        if str(spec).strip() == "0":
            embed.add_field(
                name="Appearing",
                value="`appearing = 0` → **Never found randomly** (summoned/placed).",
                inline=False
            )
            await self._enc_send(ctx, embed=embed, public=public); return

        n, pretty = _roll_appearing(spec)
        embed.add_field(name="Appearing", value=f"{pretty}", inline=False)
        embed.add_field(name="Result", value=f"👉 **{n} {disp or picked}**", inline=False)
        await self._enc_send(ctx, embed=embed, public=public)


    async def _encounter_town(self, ctx, public: bool = False):
        roll, r, f = roll_dice("2d6")
        total = roll + f
        title = "Town Encounter"
        embed = nextcord.Embed(title=title, color=random.randint(0, 0xFFFFFF))
        embed.add_field(name="2d6", value=f"→ **{total}**", inline=False)

        def add(name, text): embed.add_field(name=name, value=text, inline=False)

        if total == 2:
            add("Nobles",
                "A noble entourage crosses your path. They might offer positions or a dangerous commission; "
                "with bad reputations, expect confrontation or an order to leave town. The watch can be summoned quickly.")
        elif total == 3:
            n, rolls, flat = roll_dice("1d6")
            add("Thieves",
                f"**{n+flat}** disguised townsfolk (thieves) shadow the party, seeking easy coin. "
                "They bail if watched closely or if a scuffle goes badly.")
        elif total == 4:
            n, r4, f4 = roll_dice("2d4")
            beggars = n + f4
            thieves = sum(1 for _ in range(beggars) if random.random() < 0.10)
            add("Beggars",
                (f"A lone beggar approaches; **{beggars}** more lurk nearby. "
                 f"If anyone gives, the rest swarm. Roughly **{thieves}** may be thieves scouting for a guild."))
        elif total == 5:
            n, r4, f4 = roll_dice("2d4")
            bullies = n + f4
            fighters = sum(1 for _ in range(bullies) if random.random() < 0.30)
            add("Bullies",
                (f"**{bullies}** young toughs (about **{fighters}** fighters) posture for a brawl. "
                 "Mostly unarmed, but a few have hidden blades. Consider a reaction roll for their leader’s mood."))
        elif total == 6:
            n, r6, f6 = roll_dice("2d6")
            add("Press Gang",
                (f"**{n+f6}** rough fighters fan out to conscript drunks and stragglers. "
                 "They prefer bludgeons and grapples; if the party loses, they awaken in a military camp."))
        elif total == 7:
            add("Merchants",
                "A merchant caravan makes an offer or needs escorts. Try `!shop` for wares or hook ideas.")
        elif total == 8:
            n, r, f = roll_dice("1d4+1")
            add("Priests",
                f"**{n}** pilgrims crusade through the streets, requesting tithes and preaching with fervor.")
        elif total == 9:
            n, r, f = roll_dice("2d6")
            add("Mercenaries",
                f"**{n+f}** sellswords on business. They might offer jobs—or rival contracts to complicate things.")
        elif total == 10:
            n, r, f = roll_dice("2d6")
            add("City Watch",
                f"**{n+f}** watchmen eye the party. They’ll demand explanations from suspicious types, "
                "but hesitate to escalate without cause.")
        elif total == 11:
            apprentices, r, f = roll_dice("1d4-1")
            apprentices = max(0, apprentices + f)
            add("Wizard",
                f"A lone wizard passes by{(' with **'+str(apprentices)+'** apprentice(s)' if apprentices else '')}. "
                "Temperament is uncertain—aid, curiosity, or ire?")
        else:
            n, r, f = roll_dice("1d8")
            add("Wererats",
                f"**{n+f}** wererats disguised as townsfolk. Cowardly; they won’t attack an equal or stronger party outright.")

        await self._enc_send(ctx, embed=embed, public=public)


    async def _enc_send(self, ctx, *, content: str | None = None, embed=None, public: bool = False):
        import nextcord
        try:
            if public:
                return await ctx.send(content=content, embed=embed)


            if embed and not embed.footer.text:
                try:
                    embed.set_footer(text=f"Private to {ctx.author.display_name}")
                except Exception:
                    pass

            if content and embed:
                return await ctx.author.send(content, embed=embed)
            elif embed:
                return await ctx.author.send(embed=embed)
            else:
                return await ctx.author.send(content)

        except nextcord.Forbidden:
            warn = "⚠️ I couldn't DM you. Enable DMs from server members or use `-p` to post publicly."
            if embed:
                await ctx.send(warn)
                return await ctx.send(embed=embed)
            else:
                return await ctx.send(warn + (f"\n{content}" if content else ""))



    @commands.command(name="forage")
    async def forage(self, ctx, *args):
        """
        Foraging (doesn't slow travel).
        RAW: For each *day of travel while foraging*, 1-in-6 chance to find enough food for **1d6 human-sized beings** (for one day).
        Usage:
          !forage
          !forage -d 3                 # roll 3 travel days
          !forage -d 4 -n 6            # also compute how many days this feeds 6 people
          !forage -dm                  # send by DM instead of public
        """

        public = True
        if any(a in {"-dm", "-dm", "-private"} for a in args):
            public = False

        def _pick_int(flag_short, flag_long, default=None):
            for i, a in enumerate(args):
                if str(a) in {flag_short, flag_long} and i + 1 < len(args):
                    try:
                        return int(str(args[i+1]))
                    except Exception:
                        return default
            if default is None and len(args) == 1:
                try:
                    return int(str(args[0]))
                except Exception:
                    pass
            return default

        days = _pick_int("-d", "-days", default=1)
        people = _pick_int("-n", "-people", default=None)
        days = max(1, int(days or 1))

        char_name = get_active(ctx.author.id)
        if not char_name:
            await ctx.send("❌ No active character. Use `!char <name>` first.")
            return

        path = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(path):
            await ctx.send(f"❌ Character file not found for **{char_name}**.")
            return
        cfg = read_cfg(path)  

        daily = []
        total_person_days = 0
        successes = 0
        d6_rolls = []
        for _ in range(days):
            r, rolls, flat = _dice_or_fallback("1d6")
            d6_rolls.append(r)
            if r == 1:
                successes += 1
                food, r2, f2 = _dice_or_fallback("1d6")
                total_person_days += food
                daily.append((True, r, food))
            else:
                daily.append((False, r, 0))

        title = "🌾 Foraging"
        embed = nextcord.Embed(title=title, color=random.randint(0, 0xFFFFFF))
        embed.add_field(name="Character", value=char_name, inline=True)
        embed.add_field(name="Days", value=str(days), inline=True)
        embed.add_field(name="Rule", value="Per day: 1 on 1d6 → **1d6 people fed** (for one day).", inline=False)

        if days <= 12:
            lines = []
            for i, (ok, roll, food) in enumerate(daily, 1):
                if ok:
                    lines.append(f"Day {i}: 1d6 → **{roll}** → success → **{food}** person-days")
                else:
                    lines.append(f"Day {i}: 1d6 → **{roll}** → no find")
            embed.add_field(name="Daily Rolls", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Daily Rolls", value=f"(rolled {days}× 1d6; successes: **{successes}**)", inline=False)

        embed.add_field(name="Successes", value=str(successes), inline=True)
        embed.add_field(name="Total Food", value=f"**{total_person_days}** person-days", inline=True)

        if people and people > 0:
            days_feed = total_person_days // people
            leftover = total_person_days % people
            embed.add_field(
                name=f"Feeds {people} people",
                value=(f"**{days_feed}** full day(s) " + (f"+ {leftover} extra person-day" if leftover else "")),
                inline=False
            )

        embed.set_footer(text="Foraging does not reduce travel speed. RAW: 1-in-6 per day; success yields 1d6 people-days of food.")
        await self._enc_send(ctx, embed=embed, public=public)


    @commands.command(name="hunt")
    async def hunt(self, ctx, *args):
        """
        Hunting (takes a full day; no travel/rest that day).
        RAW: While hunting, you have a 1-in-6 chance to encounter edible animals (if you can catch them).
             By default this ALSO runs a normal wilderness wandering encounter.

        Usage:
          !hunt forest                  # animals check + wandering encounter (default)
          !hunt grassland -no-wander   # animals check only (skip wandering)
          !hunt hills -dm               # DM instead of public

        Terrains: desert/grassland/jungle/mountain/hill/ocean/swamp/forest
        Flags:
          -no-wander | -nowander | -nw   disable the wandering encounter
          -dm | -dm | -private           send via DM instead of public
        """

        if not args:
            await self._enc_send(
                ctx,
                content="❌ Usage: `!hunt <terrain> [-dm|-private] [-no-wander]` (wandering check runs by default)",
                public=True
            )
            return

        public = True
        if any(a in {"-dm", "-dm", "-private"} for a in args):
            public = False

        disable_wander = any(a in {"-no-wander", "-nowander", "-nw"} for a in args)
        enable_wander  = any(a in {"-wander", "-w"} for a in args)
        also_wander = True
        if disable_wander:
            also_wander = False
        elif enable_wander:
            also_wander = True  

        terr_arg = next((a for a in args if not a.startswith("-")), None)
        if not terr_arg:
            await self._enc_send(ctx, content="⚠️ Specify a terrain, e.g., `!hunt forest`.", public=public)
            return

        first = terr_arg.strip().lower()
        terr_key = _WILD_KEYS.get(first, None)
        if not terr_key:
            await self._enc_send(
                ctx,
                content=("⚠️ Unknown terrain. Try one of: desert, grassland, jungle, mountain, hill, ocean, swamp, forest."),
                public=public
            )
            return

        char_name = get_active(ctx.author.id)
        if not char_name:
            await ctx.send("❌ No active character. Use `!char <name>` first.")
            return

        path = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(path):
            await ctx.send(f"❌ Character file not found for **{char_name}**.")
            return
        cfg = read_cfg(path)

        d6, rolls, flat = _dice_or_fallback("1d6")
        found = (d6 == 1)

        embed = nextcord.Embed(
            title=f"🏹 Hunting — {terr_key.title()}",
            color=(0x2ECC71 if found else 0x95A5A6)
        )
        embed.add_field(name="Character", value=char_name, inline=True)
        embed.add_field(name="Hunting Check", value=f"1d6 {rolls} → **{d6}** (need **1**)", inline=False)
        embed.add_field(name="RAW", value="Success gives an encounter with edible animals (if you can catch them).", inline=False)

        if found:
            pick = _pick_hunt_result(terr_key)
            if pick:
                animal = pick["animal"]
                count = pick["count"]
                qty_spec = pick["qty_spec"]
                approx = pick["approx_person_days"]
                embed.add_field(
                    name="Edible Animals Encountered",
                    value=f"**{count}× {animal}** ({qty_spec} → {count}).",
                    inline=False
                )
                embed.add_field(
                    name="Quick Meat Guideline",
                    value=(f"On a successful hunt, this could yield ~**{approx}** person-days of food "
                           f"(rule-of-thumb). GM adjudication overrides."),
                    inline=False
                )
                embed.add_field(
                    name="Next Step",
                    value=("Resolve the chase/combat normally. "
                           "This hunting roll is **in addition** to your regular wandering monster check."),
                    inline=False
                )
            else:
                embed.add_field(
                    name="Result",
                    value="✅ Animals encountered, but no table is defined for this terrain. (You can improvise.)",
                    inline=False
                )
        else:
            embed.add_field(name="Result", value="❌ No edible animals encountered today.", inline=False)

        if also_wander:
            embed.add_field(
                name="Wandering Check",
                value="Running a regular wilderness encounter as well (separate from the hunting animals).",
                inline=False
            )

        embed.set_footer(text="Hunting occupies the whole day. Animals check is separate from the normal wandering encounter.")
        await self._enc_send(ctx, embed=embed, public=public)

        if also_wander:
            try:
                await self._encounter_wilderness(ctx, terr_key, force=False, public=public)
            except Exception:
                await self._enc_send(
                    ctx,
                    content="(⚠️ Tried to run the wilderness wandering check too, but hit an error. You can still use `!e <terrain>` manually.)",
                    public=public
                )



    @commands.command(name="traplist", aliases=["traps"])
    async def traplist(self, ctx, *args):
        """
        GM reference: sends a DM (default) with common trap examples and quick rulings.
        Usage:
          !traplist            # DM (default)
          !traplist -p         # post publicly instead
        """


        public = any(a in {"-p", "-public"} for a in args)

        intro = nextcord.Embed(
            title="🧩 GM Trap Reference — Basics",
            color=random.randint(0, 0xFFFFFF)
        )
        intro.add_field(
            name="Design & Reliability",
            value=(
                "• Combine simple effects for deadlier traps (harder saves, more damage, multiple effects).\n"
                "• Traps need not be 100% reliable: you can roll per passerby (e.g., **1–2 on 1d6** to actually spring), "
                "or key them to **weight** so light PCs slip by and heavy ones don’t.\n"
            ),
            inline=False
        )
        intro.add_field(
            name="Running Traps With The Bot",
            value=(
                "• **Saves:** Have players `!save Spells|Poison|Petrify|Breath|DeathRay`\n"
                "• **Damage:** `!damage <target> <dice>` (e.g., `1d10`, `2d6`, `3d6`)\n"
                "• **Conditions:** `!status <target> blinded 1d8t` / `deafened 1d8t` / `prone`\n"
                "• **Wandering:** `!e dungeon <avg-lvl>` or `!e <terrain>`; use `-f` to force when the text says it attracts trouble.\n"
                "• **Timing:** When a trap says *arrive in 2d10 rounds*, just roll and use `!track`."
            ),
            inline=False
        )
        intro.set_footer(text="Tip: Keep trap notes short and mechanical so players stay in the fiction.")
        await self._enc_send(ctx, embed=intro, public=public)

        TRAPS = [
            ("Alarm",
             "Save vs **Spells** or be **deafened** for **1d8 turns**. Check wandering monsters immediately; if indicated, they arrive in **2d10 rounds**.",
             "GM: Run `!e` now; if an encounter triggers, roll 2d10 for arrival."),
            ("Arrow Trap",
             "Hidden crossbow attacks at **AB +1**; on hit **1d6+1** damage.",
             "GM: Roll to hit vs target AC; then `!damage`."),
            ("Chute",
             "Save vs **Death Ray** (add DEX bonus) or slide to a lower level. Usually little/no damage.",
             "GM: Move token; apply fall only if specified by map."),
            ("Falling Stones/Bricks",
             "Save vs **Paralysis/Petrify** (add DEX bonus) or take **1d10** damage.",
             "GM: Consider surprise or difficult footing after."),
            ("Flashing Light",
             "Save vs **Spells** or be **blinded** for **1d8 turns**.",
             "GM: Blind status automatically applies -2 on hit."),
            ("Monster-Attracting Spray",
             "Harmless but smelly. **Doubles wandering chance** for **1d6 hours** or until washed off.",
             "GM: Roll **two** wandering checks each interval; or occasionally `!e -f`."),
            ("Oil Slick",
             "Save vs **Death Ray** (add DEX bonus) or **fall prone**. Oil is **flammable**; torches may ignite.",
             "GM: If ignited, adjudicate fire damage (e.g., 1d6) and spread."),
            ("Pit Trap",
             "Save vs **Death Ray** (add DEX bonus) or fall; apply **falling damage**. Deadlier with spikes/acid/creatures/water.",
             "GM: Add spikes (extra damage), water (drowning clocks), or monsters."),
            ("Poison Dart",
             "Attacks at **AB +1** for **1d4** damage; victim must save vs **Poison** or **die**.",
             "GM: Resolve attack; on hit apply damage then poison save."),
            ("Poison Gas",
             "Fills room; everyone saves vs **Poison** or **die**. Sometimes **flammable**: on ignition **1d6** (save vs **Dragon Breath** to avoid).",
             "GM: Track area and ventilation; consider lingering hazard."),
            ("Poison Needle",
             "Tiny needle from a keyhole/aperture; victim saves vs **Poison** or **die**.",
             "GM: Common on locks; telegraph scratch marks for clues."),
            ("Portcullis",
             "Falling gate; triggering character saves vs **Death Ray** or takes **3d6** damage.",
             "GM: May also block retreat; consider STR checks to lift."),
            ("Rolling Boulder",
             "Save vs **Death Ray** (add DEX bonus) or take **2d6**. If no alcoves, may require outrunning.",
             "GM: Use contested move or chase rulings along a corridor."),
            ("Blade Trap",
             "Blade/spear pops from wall or ceiling; attacks at **AB +1** for **1d8**. Large blades can hit along a **10–20′ line**.",
             "GM: For lines, target each PC in path once."),
            ("Triggered Spell",
             "Upon activation, a spell fires (curse/illusion/wall of fire, etc.) targeting or centered on the trigger.",
             "GM: Use existing spell entries; run normal `!save`/effects."),
        ]

        per_page = 5
        total_pages = (len(TRAPS) + per_page - 1) // per_page

        for i in range(total_pages):
            start = i * per_page
            chunk = TRAPS[start:start+per_page]
            em = nextcord.Embed(
                title=f"🧩 GM Trap Reference — Examples ({i+1}/{total_pages})",
                color=random.randint(0, 0xFFFFFF)
            )
            for name, rules, tip in chunk:
                em.add_field(
                    name=f"• {name}",
                    value=f"{rules}\n*{tip}*",
                    inline=False
                )
            await self._enc_send(ctx, embed=em, public=public)

    @commands.command(name="chin", aliases=["chinchirorin"])
    async def chinchirorin(self, ctx, *flags):
        """
        Play a quick game of Chinchirorin (three-dice gambling).

        Default:
          !chin            → roll a hand and show your result (no House)
        Versus House:
          !chin -h
          !chin -house
          !chin house     → you vs the House

        Rules (this variant):
          • Each side rolls 3d6 until they get a scoring combo:
              – 4-5-6      → automatic win
              – 1-2-3      → automatic loss
              – triples    → strong hand (Triple 6 is best, Triple 1 is weakest)
              – pair+single→ “Point N” where N is the odd die (Point 6 is best)
            Hands with all different dice that are not 4-5-6 or 1-2-3 are “no combo”
            and get re-rolled automatically (up to a few tries).
          • Highest rank wins; ties are possible when playing vs the House.
        """

        # Check if we are playing vs House
        vs_house = any(str(f).lower() in ("-h", "--house", "house") for f in flags)

        def _chin_rank(rolls):
            """
            Given a list of three ints [d1, d2, d3], return (rank, label)
            or (None, 'No combo') if it’s not a scoring hand.

            Rank ordering (higher is better):
              4-5-6            → top
              triples          → next (Triple 6 best)
              point N          → next (Point 6 best)
              1-2-3            → worst scoring hand
            """
            dice = sorted(int(r) for r in rolls)

            # 4-5-6 auto win
            if dice == [4, 5, 6]:
                return 30, "4-5-6 (Automatic Win!)"

            # 1-2-3 auto loss
            if dice == [1, 2, 3]:
                return 1, "1-2-3 (Automatic Loss)"

            # Triples
            if len(set(dice)) == 1:
                val = dice[0]
                rank = 20 + val          # Triple 6 = 26, Triple 1 = 21
                return rank, f"Triple {val}"

            # Pair + single → Point N (N is the odd die)
            if len(set(dice)) == 2:
                # [a, a, b] or [a, b, b]
                if dice[0] == dice[1]:
                    point = dice[2]
                else:
                    point = dice[0]
                rank = 10 + point         # Point 6 = 16, Point 1 = 11
                return rank, f"Point {point}"

            # All different, but not 4-5-6 / 1-2-3 → no combo
            return None, "No combo (re-roll)"

        def _play_side(label: str):
            """
            Roll up to a few times until we get a scoring combo.
            Returns (rank, label_text, history_lines).
            """
            history = []
            final_rank = None
            final_label = None

            for attempt in range(1, 6):  # up to 5 attempts to avoid degenerate loops
                total, rolls, flat = roll_dice("3d6")
                combo_rank, combo_label = _chin_rank(rolls)

                rolls_txt = ", ".join(str(r) for r in rolls)
                history.append(f"Roll {attempt}: [{rolls_txt}] → {combo_label}")

                if combo_rank is not None:
                    final_rank = combo_rank
                    final_label = combo_label
                    break

            if final_rank is None:
                final_label = "No scoring combo after several tries."
            return final_rank, final_label, history

        # Always roll for the player
        p_rank, p_label, p_hist = _play_side("You")

        if vs_house:
            # Player vs House mode
            h_rank, h_label, h_hist = _play_side("House")

            # Decide outcome
            if p_rank is None and h_rank is None:
                outcome = "Both sides failed to make a hand — it's a wash."
            elif p_rank is None:
                outcome = "❌ You failed to make a hand; the House wins by default."
            elif h_rank is None:
                outcome = "✅ The House failed to make a hand; you win by default."
            else:
                if p_rank > h_rank:
                    outcome = "✅ **You win!**"
                elif p_rank < h_rank:
                    outcome = "❌ **The House wins.**"
                else:
                    outcome = "🤝 **Tie!** Same hand."

            embed = nextcord.Embed(
                title="🎲 Chinchirorin — You vs the House",
                color=random.randint(0, 0xFFFFFF),
            )

            embed.add_field(
                name="Your Hand",
                value="\n".join(p_hist) + (f"\n\nFinal: **{p_label}**" if p_label else ""),
                inline=False,
            )
            embed.add_field(
                name="House Hand",
                value="\n".join(h_hist) + (f"\n\nFinal: **{h_label}**" if h_label else ""),
                inline=False,
            )

            embed.add_field(name="Outcome", value=outcome, inline=False)
            embed.set_footer(text="House rules: 4-5-6>Triples>Points>1-2-3. No combo = re-roll.")
            await ctx.send(embed=embed)
        else:
            # Solo mode (for comparing with other players)
            embed = nextcord.Embed(
                title="🎲 Chinchirorin — Your Hand",
                color=random.randint(0, 0xFFFFFF),
            )
            embed.add_field(
                name="Rolls",
                value="\n".join(p_hist),
                inline=False,
            )
            embed.add_field(
                name="Final Hand",
                value=f"**{p_label}**",
                inline=False,
            )
            embed.set_footer(text="Rules: 4-5-6>Triples>Points>1-2-3. Use !chin -h to play vs the House.")
            await ctx.send(embed=embed)

    @commands.command(name="br")
    async def scene_break(self, ctx, count: int = 1):
        """
        Drop a visual scene break bar for roleplay.
        Usage:
          !br
          !br 3
        """
        # Try to delete the invoking message (!br / !br 3)
        try:
            await ctx.message.delete()
        except (nextcord.Forbidden, nextcord.HTTPException):
            # No perms or other issue — just ignore and move on
            pass

        count = max(1, min(int(count), 5))
        separator = "```\u200b```"
        await ctx.send("\n".join(separator for _ in range(count)))

def setup(bot):
    bot.add_cog(Dice(bot))
