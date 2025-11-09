import json, os, re, random, math, time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import OrderedDict
from datetime import datetime, timezone
import nextcord
from nextcord.ext import commands

from utils.ini import read_cfg, write_cfg, get_compat, getint_compat
from utils.players import get_active


HERE = os.path.dirname(__file__)
RECIPES_PATH = os.path.abspath(os.path.join(HERE, "..", "data", "recipes.json"))

SPELL_LIST_CANDIDATES = ["data/spell.lst", "spell.lst"]

CRAFT_DATA_DIR = os.path.join("data", "crafting")


PAGE_SIZE_DEFAULT = 50
ARCANE_CLASSES = {"magic-user","spellcrafter","illusionist","necromancer","fightermage","magethief"}
VS_KEYS = {"dragon", "undead", "enchanted", "ooze", "lycanthrope"}
WEAPON_POWER_FILE = os.path.join("data", "weapon_powers.json")

_PLUS_RE = re.compile(r"\+(\d+)\b")

def _parsed_plus_from_name(name: str) -> int:
    """Pull +N from an item name like 'ChainMail+2' if present."""
    m = _PLUS_RE.search(str(name))
    return int(m.group(1)) if m else 0

def _candidate_paths(path: str) -> list[str]:

    here = os.path.dirname(__file__)
    paths = []
    if os.path.isabs(path):
        paths.append(path)
    else:
        paths.extend([
            path,
            os.path.join(here, path),
            os.path.join(here, "..", path),
            os.path.join(os.getcwd(), path),
            os.path.join(os.getcwd(), os.path.basename(path)),
        ])

    seen, out = set(), []
    for p in paths:
        q = os.path.abspath(p)
        if q not in seen:
            seen.add(q); out.append(q)
    return out

def _load_spell_lists() -> dict[str, dict[str, int]]:
    """
    Read spell.lst (INI-ish):
      [Magic-User]
      l0 = Foo Bar
      l1 = Baz
    → returns: {"Magic-User": {"foo":0, "bar":0, "baz":1}, ...}
    """
    import configparser
    paths = []
    for p in SPELL_LIST_CANDIDATES:
        paths.extend(_candidate_paths(p))
    found = None
    for p in paths:
        if os.path.exists(p):
            found = p; break
    if not found:
        print("[crafting] spell.lst not found (looked at):", paths)
        return {}

    cp = configparser.ConfigParser()
    cp.read(found, encoding="utf-8")

    out: dict[str, dict[str, int]] = {}
    for sec in cp.sections():
        class_name = sec.strip()
        class_map: dict[str, int] = {}
        for opt, val in cp.items(sec):
            m = re.fullmatch(r"[lL](\d+)", opt.strip())
            if not m:
                continue
            try:
                L = int(m.group(1))
            except Exception:
                continue

            for tok in (val or "").split():
                s = tok.strip()
                if not s:
                    continue
                class_map[s.lower()] = L
        out[class_name] = class_map
    return out

def _canon(cls: str) -> str:

    return Crafting._canon_class_name(self=None, s=cls)

def _spell_level_in_list(spell_lists: dict[str, dict[str, int]], cls_name: str, spell: str) -> Optional[int]:
    """
    Return level if 'spell' is on cls_name’s list, else None.
    cls_name can be 'Magic-User', 'Illusionist', etc. (canon names).
    """
    if not spell_lists:
        return None
    cls = _canon(cls_name)
    table = spell_lists.get(cls) or {}
    return table.get(spell.strip().lower())


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[crafting] recipes.json not found at {path}")
        return {}
    except json.JSONDecodeError as e:
        print(f"[crafting] JSON syntax error in {path}: line {e.lineno}, col {e.colno}: {e.msg}")
        return {}
    except Exception as e:
        print(f"[crafting] error reading {path}: {e}")
        return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def _format_eta(started_ts: int, days: int) -> Tuple[str, str]:
    """
    Return (absolute_when_str_utc, relative_str).
    Example: ("2025-10-18 15:30 UTC", "in 2d 6h") or ("2025-10-12 09:12 UTC", "ready")
    """
    try:
        days = int(days)
    except Exception:
        days = 0
    eta_ts = int(started_ts or 0) + max(0, days) * 86400
    now = int(time.time())
    remaining = eta_ts - now

    when_str = datetime.fromtimestamp(eta_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if remaining <= 0:
        rel = "ready"
    else:
        d = remaining // 86400
        h = (remaining % 86400) // 3600
        m = (remaining % 3600) // 60
        if d:
            rel = f"in {d}d {h}h"
        elif h:
            rel = f"in {h}h {m}m"
        else:
            rel = f"in {max(1, m)}m"
    return when_str, rel


def _resolve_char_ci(name: str) -> Tuple[Optional[str], Optional[str]]:
    base = name.replace(" ", "_")
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

def _fmt_armor_row(name: str, rec: dict) -> str:
    eff = _weapon_effective_plus(rec)
    disp = _clean_recipe_key_for_display(name)
    return f"• {disp} (effective +{eff:g})"

def _prime_for_class(cls_lc: str) -> str:
    arcane = {"magic-user","mage","illusionist","necromancer","spellcrafter","fightermage","magethief"}

    divine = {"cleric","druid","paladin"}
    return "wis" if cls_lc in divine else "int"

def _class_group(cls_lc: str) -> str:
    if cls_lc in {"cleric","druid","paladin"}:
        return "divine"
    return "arcane"


def _is_spellcrafter(cls_lc: str) -> bool:
    return cls_lc == "spellcrafter"

def _mod(stat_score: int) -> int:

    s = max(1, int(stat_score))
    if s <= 3:   return -3
    if s <= 5:   return -2
    if s <= 8:   return -1
    if s <= 12:  return 0
    if s <= 15:  return +1
    if s <= 17:  return +2
    return +3

def _read_char_snapshot(char_file: str) -> dict:
    cfg = read_cfg(char_file)
    name  = get_compat(cfg, "info", "name", fallback=char_file[:-4].replace("_"," "))
    cls   = get_compat(cfg, "info", "class", fallback="Fighter")
    lvl   = getint_compat(cfg, "cur", "level", fallback=1)
    gp    = getint_compat(cfg, "cur", "gp", fallback=0)
    pp    = getint_compat(cfg, "cur", "pp", fallback=0)
    ep    = getint_compat(cfg, "cur", "ep", fallback=0)
    sp    = getint_compat(cfg, "cur", "sp", fallback=0)
    cp    = getint_compat(cfg, "cur", "cp", fallback=0)
    stats = {k: getint_compat(cfg, "stats", k, fallback=10) for k in ("str","dex","con","int","wis","cha")}
    return {
        "name": name, "class": cls, "level": lvl, "stats": stats,
        "coins": {"pp":pp,"gp":gp,"ep":ep,"sp":sp,"cp":cp},
        "cfg": cfg
    }

def _coins_to_cp(wallet: dict) -> int:
    return wallet["pp"]*1000 + wallet["gp"]*100 + wallet["ep"]*50 + wallet["sp"]*10 + wallet["cp"]

def _cp_to_wallet(cp_total: int) -> dict:
    new_pp = cp_total // 1000; rem = cp_total % 1000
    new_gp = rem // 100;       rem = rem % 100
    new_ep = rem // 50;        rem = rem % 50
    new_sp = rem // 10;        new_cp = rem % 10
    return {"pp":new_pp,"gp":new_gp,"ep":new_ep,"sp":new_sp,"cp":new_cp}

def _deduct_gp(cfg, path: str, gp_amount: float) -> bool:

    want_cp = int(round(gp_amount * 100))
    wallet = {
        "pp": getint_compat(cfg, "cur", "pp", fallback=0),
        "gp": getint_compat(cfg, "cur", "gp", fallback=0),
        "ep": getint_compat(cfg, "cur", "ep", fallback=0),
        "sp": getint_compat(cfg, "cur", "sp", fallback=0),
        "cp": getint_compat(cfg, "cur", "cp", fallback=0),
    }
    total_cp = _coins_to_cp(wallet)
    if total_cp < want_cp:
        return False
    new_wallet = _cp_to_wallet(total_cp - want_cp)
    if not cfg.has_section("cur"): cfg.add_section("cur")
    for k,v in new_wallet.items():
        cfg.set("cur", k, str(v))
    write_cfg(path, cfg)
    return True

def _weapon_effective_plus(rec: dict) -> float:
    """
    Compute 'effective +' for weapons/armor from recipe meta.
    """
    meta = rec.get("weapon_meta") or {}
    a = 0; b = 0
    try: a = int(meta.get("base_plus", rec.get("base_plus", 0)) or 0)
    except: pass
    try: b = int(meta.get("vs_plus",   rec.get("vs_bonus", 0))  or 0)
    except: pass
    large, small = (b, a) if b >= a else (a, b)
    return float(small + large / 2.0)

def _fmt_scroll_row(name: str, rec: dict) -> str:
    eff = (rec.get("effects") or [{}])[0]
    try:
        L = int(eff.get("level", 0))
    except:
        L = eff.get("level", "?")
    cls = (eff.get("class") or "").strip() or "?"
    disp = _clean_recipe_key_for_display(name)
    return f"• {disp}  *(L{L}, {cls})*"

def _fmt_weapon_row(name: str, rec: dict) -> str:
    eff = _weapon_effective_plus(rec)
    disp = _clean_recipe_key_for_display(name)
    return f"• {disp} (effective +{eff:g})"

def _add_inventory_items(char_file: str, item_name: str, qty: int) -> None:
    cfg = read_cfg(char_file)
    if not cfg.has_section("item"): cfg.add_section("item")
    lower_key = item_name.lower()
    try:
        cur_cnt = int(str(cfg.get("item", lower_key, fallback="0")).strip() or "0")
    except Exception:
        cur_cnt = 0
    cfg.set("item", lower_key, str(cur_cnt + qty))
    storage_line = cfg.get("item", "storage", fallback="")
    tokens = [t for t in storage_line.split() if t]
    if item_name.lower() not in {t.lower() for t in tokens}:
        tokens.append(item_name)
    cfg.set("item", "storage", " ".join(tokens))
    write_cfg(char_file, cfg)

def _xp_award(cfg, path: str, gp_spent: float, *, enabled: bool = True) -> Optional[int]:
    if not enabled: return None
    xp = int(round(gp_spent / 10.0))
    if xp <= 0: return 0

    old = getint_compat(cfg, "cur", "xp", fallback=0)
    if not cfg.has_section("cur"): cfg.add_section("cur")
    cfg.set("cur", "xp", str(old + xp))
    write_cfg(path, cfg)
    return xp

def _clean_recipe_key_for_display(name: str) -> str:

    s = str(name).strip()
    s = re.sub(r"^\s*Scroll:\s*[\[\']\s*(.+?)\s*[\]\']\s*$", r"Scroll: \1", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s


@dataclass
class Quote:
    item_key: str
    cat: str
    base_chance: int
    adj_minus: int
    adj_plus: int
    final_chance: int
    days: int
    cost_gp: float
    notes: List[str] = field(default_factory=list)

class Crafting(commands.Cog):
    """
    !craft list [filter]
    !craft quote <item> [-doses N] [-safe]   # safe = double cost/time for +25% chance (weapons/permanent)
    !craft start <item> [-doses N] [-safe] [-y]
    !craft status
    !craft cancel <project_id>
    !craft resolve <project_id>                # GM-only: performs the secret roll & finishes on success
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recipes = _load_json(RECIPES_PATH)
        self.spell_lists = _load_spell_lists()


    def _iter_recipes_filtered(self, *, mode: str = "", text_filter: str = ""):
        """
        Iterate (name, recipe) from self.recipes with optional mode filtering:
          mode in {"", "scrolls", "weapons", "armor"}
        """
        want_t = ""
        if mode == "scrolls":
            want_t = "spell_scroll"
        elif mode == "weapons":
            want_t = "weapon"
        elif mode == "armor":
            want_t = "armor"

        for name, rec in (self.recipes or {}).items():
            t = (rec.get("type") or "").strip().lower()
            if want_t and t != want_t:
                continue
            if text_filter and text_filter.lower() not in str(name).lower():
                continue
            yield name, rec


    def _iter_staff_items(self):
        """
        Iterate (canon_name, item_def) for any item whose canonical name looks like a staff.
        We don’t rely on the item's 'type' field; names drive staff detection.
        """
        sc = self._spells()
        if not sc or not hasattr(sc, "iter_item_defs"):
            return
        for canon, it in sc.iter_item_defs():
            nm = str(canon or it.get("name", "")).strip()
            nml = nm.lower()
            if nml.startswith("staffof") or nml.startswith("staff of "):
                yield (canon or nm), (it or {})

    def _infer_levels_for_staff(self, it: dict) -> list[int]:
        """
        Infer spell-effect 'levels' from an item definition for costing/time purposes.
        We scan common keywords (light, charmperson, locateobject, flame, drain, wish).
        Users can override/extend in data/weapon_powers.json (same map you already use).
        """
        levels: list[int] = []
        lvl_map = self._weapon_power_levels()


        special = str(it.get("special", "") or "").lower()
        if special:
            for key, L in lvl_map.items():
                if key in special:
                    levels.append(int(L))


        for key, L in lvl_map.items():
            try:
                v = it.get(key, None)
                if isinstance(v, (int, float)) and int(v) > 0:
                    levels.append(int(L))
                elif isinstance(v, str):
                    vs = v.strip().lower()
                    if vs and vs not in {"0", "false", "no"}:
                        levels.append(int(L))
            except Exception:
                pass


        out = []
        seen = set()
        for L in levels:
            if L not in seen:
                out.append(L); seen.add(L)
        return out

    def _build_dynamic_staff_recipe(self, item_key: str) -> dict | None:
        """
        Synthesize a recipe for a 'Staffof…' item discovered via the Spells cog, even if
        the item DB calls it a bludgeoning weapon. We treat it as a charged staff.
        """
        sc = self._spells()
        if not sc:
            return None
        canon, it = sc._item_lookup(item_key)
        if not it:
            return None

        name = canon or item_key
        nml = name.lower()
        if not (nml.startswith("staffof") or nml.startswith("staff of ")):
            return None


        rec: dict = {
            "type": "staff",
            "gives": name,
            "charges": {"max": 30},


            "min_level": {"default": 9, "spellcrafter": 7},
        }


        levels = self._infer_levels_for_staff(it)
        if levels:
            rec["effects"] = [{"level": int(L)} for L in levels]
        else:

            rec["equivalent_level"] = 4

        return rec


    def _clean_spell(self, s: str) -> str:

        s = str(s).strip()
        s = re.sub(r"^[\[\(\"\']+|[\]\)\"\']+$", "", s)
        return s.strip()

    def _spell_info_map(self) -> dict:
        """
        Build {SpellName: {'L': level_int, 'side': 'arcane'|'divine'}}
        Prefers arcane if the spell exists for multiple classes.
        """
        index = self._all_spells_index() or {}
        info = {}
        for clsname, rows in index.items():
            side = "arcane" if self._is_spell_class_arcane(clsname) else "divine"
            for nm, L in rows:
                nm = self._clean_spell(nm)
                if nm not in info:
                    info[nm] = {"L": int(L), "side": side}
                else:

                    if info[nm]["side"] != "arcane" and side == "arcane":
                        info[nm] = {"L": int(L), "side": side}

                    elif side == info[nm]["side"]:
                        info[nm]["L"] = min(info[nm]["L"], int(L))
        return info


    def _is_spell_class_arcane(self, clsname: str) -> bool:

        return self._is_arcane_class(clsname)


    def _iter_magic_items(self):
        """
        Yield (name, type, base_plus, vs_plus) for all weapons/armor that are actually magical.
        type ∈ {'weapon','armor'}
        """
        sc = self.bot.get_cog("Spells")
        if not sc or not hasattr(sc, "iter_item_defs"):
            return
        for canon, it in sc.iter_item_defs():
            t = str(it.get("type","") or "").strip().lower()
            is_armor = t in {"armor","shield"}
            is_weapon = ("plus" in it) and not is_armor
            if not (is_armor or is_weapon):
                continue
            try:
                base_plus = int(str(it.get("plus","0")) or "0")
            except Exception:
                base_plus = 0
            vs_plus = 0
            for k in VS_KEYS:
                try:
                    vs_plus = max(vs_plus, int(str(it.get(k,"0")) or "0"))
                except Exception:
                    pass
            if base_plus <= 0 and vs_plus <= 0:
                continue
            yield canon, ("armor" if is_armor else "weapon"), base_plus, vs_plus

    def _effective_bonus_label(self, name: str) -> str:
        """
        Return a human label like '(effective +2.5)' for a weapon/armor by looking it up.
        """
        sc = self.bot.get_cog("Spells")
        if not sc or not hasattr(sc, "_item_lookup"):
            return ""
        canon, it = sc._item_lookup(name)
        if not it:
            return ""
        try:
            a = int(str(it.get("plus","0")) or "0")
        except Exception:
            a = 0
        b = 0
        for k in VS_KEYS:
            try:
                b = max(b, int(str(it.get(k,"0")) or "0"))
            except Exception:
                pass
        if a <= 0 and b <= 0:
            return ""
        eff = a + (b/2.0 if b>0 else 0)
        return f"(effective +{eff:g})"


    def _render_paginated_embed(self, *, title: str, subtitle: str, items: list[str], page: int, per_page: int = 50):
        total = len(items)
        pages = max(1, math.ceil(total / per_page))
        page = min(max(1, page), pages)
        start = (page-1)*per_page
        chunk = items[start:start+per_page]

        emb = nextcord.Embed(
            title=title,
            description=f"Filter: **{subtitle or 'All'}**\nPage {page} / {pages} • Results: {total}\n"
                        f"*Tip*\ntry !craft list scrolls, !craft list weapons, or !craft list armor.\n"
                        f"Use `-p N` to navigate.",
            color=random.randint(0, 0xFFFFFF)
        )
        if chunk:

            block = "\n".join(chunk)

            if len(block) <= 1000:
                emb.add_field(name="Results", value=block, inline=False)
            else:
                mid = len(chunk)//2
                emb.add_field(name="Results", value="\n".join(chunk[:mid]), inline=False)
                emb.add_field(name="Results (cont.)", value="\n".join(chunk[mid:]), inline=False)
        else:
            emb.add_field(name="Results", value="*No matching items.*", inline=False)
        return emb


    def _min_level_for(self, recipe: dict, cls_lc: str) -> int:
        is_sc = _is_spellcrafter(cls_lc)
        if "min_level" in recipe:
            if is_sc and "spellcrafter" in recipe["min_level"]:
                return int(recipe["min_level"]["spellcrafter"])
            return int(recipe["min_level"].get("default", 9))

        t = recipe.get("type","")
        if t == "spell_scroll": return 1 if is_sc else 1
        if t in {"potion","scroll","single_use"}: return 3 if is_sc else 7
        return 7 if is_sc else 9

    def _base_success(self, level: int, prime_mod: int) -> int:


        return max(0, min(100, 15 + 5*level + prime_mod))

    def _prime_score(self, stats: dict, cls_lc: str) -> int:
        key = _prime_for_class(cls_lc)
        return int(stats.get(key, 10))

    def _charges_adjust(self, levels: List[int], charges: dict) -> Tuple[int, float, int, List[str]]:
        """
        Return (extra_days, extra_cost_gp, chance_penalty, notes) from the 'charges' table & recharging options.
        We assume levels represent each distinct effect's spell level (used for other penalties elsewhere).
        """
        notes = []
        if not charges: return (0, 0.0, 0, notes)
        maxc = int(charges.get("max", 0))
        if maxc <= 0: return (0,0.0,0,notes)


        if   2 <= maxc <= 3:  cost_per = 150; per_day = 1; chance_pen = 5
        elif 4 <= maxc <= 7:  cost_per = 125; per_day = 2; chance_pen = 10
        elif 8 <= maxc <= 20: cost_per = 100; per_day = 3; chance_pen = 20
        elif 21<= maxc <= 30: cost_per = 75;  per_day = 4; chance_pen = 30
        else:                 cost_per = 100; per_day = 3; chance_pen = 20


        charge_count_for_cost = max(0, maxc - 1)
        extra_cost = cost_per * charge_count_for_cost
        extra_days = charge_count_for_cost
        notes.append(f"Charges: {maxc} (cost +{extra_cost} gp, time +{extra_days}d, chance −{chance_pen}%)")


        if charges.get("rechargeable", False):
            extra_cost *= 2
            extra_days *= 2
            chance_pen *= 2
            notes.append("Rechargeable: ×2 cost/time, chance penalty doubled.")


        sr = charges.get("self_recharge")
        if sr:

            if sr == "1_per_day":
                extra_cost *= 3; extra_days *= 2; chance_pen += 10
                notes.append("Self-recharge 1/day: ×3 cost, ×2 time, −10% chance.")
            elif sr == "all_per_day":
                extra_cost *= 5; extra_days *= 3; chance_pen += 30
                notes.append("Self-recharge all/day: ×5 cost, ×3 time, −30% chance.")
            elif sr == "all_per_week":
                extra_cost *= 4; extra_days *= 2; chance_pen += 20
                notes.append("Self-recharge all/week: ×4 cost, ×2 time, −20% chance.")

        return (int(extra_days), float(extra_cost), int(chance_pen), notes)


    async def _send_large(self, ctx, lines: list[str], *, max_len: int = 1900) -> None:
        """
        Sends a list of lines in multiple messages, respecting Discord's 2000 char limit.
        """
        block: list[str] = []
        cur = 0
        for ln in lines:
            add = len(ln) + 1
            if block and cur + add > max_len:
                await ctx.send("\n".join(block))
                block, cur = [], 0
            block.append(ln)
            cur += add
        if block:
            await ctx.send("\n".join(block))

    def _embed_lines(self, title: str, subtitle: str, lines: list[str], *, color=None):
        import random, nextcord
        color = color or random.randint(0, 0xFFFFFF)
        embed = nextcord.Embed(title=title, description=subtitle, color=color)

        block, cur = [], 0
        for ln in lines:
            add = len(ln) + 1
            if block and cur + add > 1000:
                embed.add_field(name="Results", value="\n".join(block), inline=False)
                block, cur = [], 0
            block.append(ln)
            cur += add
        if block:
            embed.add_field(name="Results", value="\n".join(block), inline=False)
        return embed

    def _quote_for(self, who_cfg: dict, recipe_key: str, *, doses: int = 1, safe_boost: bool = False, scroll_class_forced: Optional[str] = None) -> Quote:

        """Compute cost, time, and success chance per BF RPG."""
        rec = self.recipes.get(recipe_key)
        if not rec:
            raise ValueError("Unknown craftable item.")
        cls_lc = get_compat(who_cfg["cfg"], "info", "class", fallback="Fighter").strip().lower()
        lvl = who_cfg["level"]
        prime_score = self._prime_score(who_cfg["stats"], cls_lc)
        base = self._base_success(lvl, prime_score)

        t = (rec.get("type") or "").strip().lower()
        effects = rec.get("effects", []) or []
        spell_levels = [int(e.get("level", 0)) for e in effects] or []

        is_sc = _is_spellcrafter(cls_lc)


        spells_cog = self._spells()
        prof = spells_cog._caster_profile(cls_lc, ctx.channel) if spells_cog else None
        if not prof:
            raise PermissionError("Only spellcasters may craft magic items.")

        def _effect_side(e: dict) -> str:
            c = (e.get("class") or "").strip().lower()
            if c in {"cleric","druid","paladin","divine"}: return "divine"
            if c in {"magic-user","magicuser","illusionist","necromancer","spellcrafter","arcane"}: return "arcane"

            return "divine" if (prof and not prof.get("book_based", True)) else "arcane"

        is_divine_caster = bool(prof and not prof.get("book_based", True))


        if t == "spell_scroll":
            if not is_sc:
                crafter_canon = self._canon_class_name(get_compat(who_cfg["cfg"], "info", "class", fallback="Fighter"))
                crafter_side  = self._class_side(crafter_canon)
                for eff in effects:
                    spell = (eff.get("spell") or "").strip()

                    L_on_my_list = _spell_level_in_list(self.spell_lists, crafter_canon, spell)
                    if L_on_my_list is not None:
                        if crafter_side == "arcane":
                            knows = self._pc_knows_spell(who_cfg["cfg"], who_cfg["class"], spell)
                            if not knows:
                                raise PermissionError(f"Must **know** {spell} to craft this scroll (add to your spellbook with `!sb add`).")
                        else:
                            if not is_divine_caster:
                                raise PermissionError("Only divine casters may craft divine scrolls.")
                            maxL = self._max_spell_level_for_class(who_cfg['cfg'], who_cfg['class'])
                            if maxL is not None and int(L_on_my_list) > maxL:
                                raise PermissionError(f"Must be able to pray for **{spell}** (L{L_on_my_list}); highest available is L{maxL}.")
                        continue


                    L = int(eff.get("level") or 0)
                    side = _effect_side(eff)
                    if side == "arcane":
                        knows = self._pc_knows_spell(who_cfg["cfg"], who_cfg["class"], spell)
                        if not knows:
                            raise PermissionError(f"Must **know** {spell} to craft this scroll (add to your spellbook with `!sb add`).")
                    else:
                        if not is_divine_caster:
                            raise PermissionError("Only divine casters may craft divine scrolls.")
                        maxL = self._max_spell_level_for_class(who_cfg['cfg'], who_cfg['class'])
                        if maxL is not None and L > maxL:
                            raise PermissionError(f"Must be able to pray for **{spell}** (L{L}); highest available is L{maxL}.")
        else:
            for eff in effects:
                L = int(eff.get("level") or 0)
                if _effect_side(eff) == "divine":
                    if not (is_sc or is_divine_caster):
                        raise PermissionError("Only divine casters may craft items imbued with divine spells.")
                    if (not is_sc):
                        maxL = self._max_spell_level_for_class(who_cfg['cfg'], who_cfg['class'])
                        if maxL is not None and L > maxL:
                            raise PermissionError(f"Item imbues L{L} divine magic; highest you can pray for is L{maxL}.")


        min_lvl = self._min_level_for(rec, cls_lc)
        if lvl < min_lvl:
            raise PermissionError(f"Requires level {min_lvl}+ for {rec.get('type','item')}.")


        notes, days, cost, minus, plus = [], 0, 0.0, 0, 0


        if t == "spell_scroll":

            spell = (effects[0].get("spell") if effects else "") or recipe_key
            spell = str(spell).strip()
            if not spell:
                raise ValueError("Scroll recipe is missing a spell name.")

            crafter_canon = self._canon_class_name(get_compat(who_cfg["cfg"], "info", "class", fallback="Fighter"))
            is_sc = _is_spellcrafter(cls_lc)


            if is_sc:
                if not scroll_class_forced:

                    opts = []
                    for cls_name, table in (self.spell_lists or {}).items():
                        L = table.get(spell.lower())
                        if isinstance(L, int):
                            opts.append(f"{cls_name} L{L}")
                    opt_str = ", ".join(opts) if opts else "Magic-User, Illusionist, Necromancer, Cleric, Druid, Paladin"
                    raise PermissionError(
                        "Spellcrafter must choose a target class for **all** scrolls. "
                        f"Use `-as <class>` (e.g., {opt_str})."
                    )
                target_class = self._canon_class_name(scroll_class_forced)
                L = _spell_level_in_list(self.spell_lists, target_class, spell)
                if L is None:
                    raise PermissionError(f"**{spell}** is not on the **{target_class}** spell list in spell.lst.")
                scroll_class = target_class

            else:

                L = _spell_level_in_list(self.spell_lists, crafter_canon, spell)
                if L is None:
                    raise PermissionError(
                        f"As a **{crafter_canon}**, you can only craft scrolls on the **{crafter_canon}** list. "
                        f"**{spell}** isn’t on it."
                    )
                scroll_class = crafter_canon


            minus += 10 * L
            days = max(1, L)
            cost = 50 * L
            notes.append(f"Spell scroll **{spell}** ({scroll_class} L{L}): −{10*L}% chance, {days}d, {cost} gp.")


        elif t in {"potion","single_use"}:
            if effects:
                if not spell_levels: raise ValueError("Single-use needs a spell level.")
                L = max(spell_levels)
                minus += 10 * L
                days = 7 + L
                cost = 50 * L * days
                notes.append(f"Single-use (spell L{L}): −{10*L}% chance, {days}d, {cost} gp.")
                if rec.get("batchable", False) and doses > 1:
                    extra = doses - 1
                    minus += 5 * extra
                    days += extra
                    cost = 50 * L * days
                    notes.append(f"Batch {doses} dose(s): −{5*extra}% chance, +{extra}d, total cost {cost} gp.")
            else:

                ov = rec.get("override")
                if isinstance(ov, dict):

                    days = int(str(ov.get("days", 0)))
                    cost = float(str(ov.get("cost_gp", 0)))
                    minus += int(str(ov.get("chance_minus", 0)))


                    c_pretty = int(cost) if cost.is_integer() else cost
                    notes.append(f"Override: {days}d, {c_pretty} gp, −{int(ov.get('chance_minus', 0))}% chance.")
                else:

                    L = int(rec.get("equivalent_level", 0))
                    if L <= 0:
                        raise ValueError("Single-use (non-spell) needs equivalent_level or override.")
                    minus += 10 * L
                    days = (7 + L) * 2
                    cost = 50 * L * days
                    notes.append(f"Single-use (non-spell, L{L}): −{10*L}% chance, {days}d, {int(cost)} gp.")


        elif t in {"weapon","armor"}:
            meta = rec.get("weapon_meta") or {}
            a = int(meta.get("base_plus", 0))
            b = int(meta.get("vs_plus", 0))
            if a <= 0 and b <= 0:
                raise ValueError("No positive magical bonus found on this item.")


            large, small = (b, a) if b >= a else (a, b)
            eff = small + (large / 2.0)


            cost = 1000.0 * eff
            days = int(7 + 2 * eff)
            minus += int(round(10 * eff))

            notes.append(f"Enchant bonus {a}" + (f", +{b} vs. special" if b > 0 else "") +
                         f" → effective {eff:g}: −{int(round(10*eff))}% chance, {days}d, {int(cost)} gp.")


            extra_lvls = [int(e.get("level", 0)) for e in (rec.get("effects") or []) if int(e.get("level", 0)) > 0]
            if not extra_lvls and rec.get("feature_levels"):
                extra_lvls = [int(x) for x in rec.get("feature_levels") if int(x) > 0]

            for L in extra_lvls:
                cost += 500 * L
                days += 5 + 2 * L
                minus += 5 * L
            if extra_lvls:
                notes.append(f"Extra powers total L{sum(extra_lvls)}: −{5*sum(extra_lvls)}% chance, +{5*len(extra_lvls) + 2*sum(extra_lvls)}d, +{500*sum(extra_lvls)} gp.")


            if safe_boost:
                plus += 25
                cost *= 2
                days = int(math.ceil(days * 2))
                notes.append("Safety boost: +25% chance, ×2 cost/time.")


        elif t in {"wand","staff","rare","ring","misc"}:

            levels = [int(e.get("level", 0)) for e in (rec.get("effects") or []) if "level" in e]
            if not levels:
                for f in rec.get("features") or []:
                    if (f.get("kind") or "").lower() == "spell_effect" and "level" in f:
                        levels.append(int(f["level"]))
            if not levels and rec.get("equivalent_level"):
                levels = [int(rec["equivalent_level"])]


            base_cost = float(rec.get("base_item_cost_gp", 0.0))
            hq_mult   = float(rec.get("hq_multiplier", 0.0))
            if not levels and not rec.get("override") and not rec.get("equivalent_level") and not (base_cost and hq_mult):
                raise ValueError(f"Recipe '{recipe_key}' has no levels/equiv/override/base cost; cannot quote.")


            cost = 0.0; days = 0; minus = 0
            for L in (levels or [0]):
                cost  += 500 * L
                days  += 5 + 2 * L
                minus += 5 * L
            if levels:
                notes.append(f"Spell effects total L{sum(levels)}: −{5*sum(levels)}% chance, {days}d, {int(cost)} gp.")


            ch_days, ch_cost, ch_pen, ch_notes = self._charges_adjust(levels, rec.get("charges", {}))
            days += ch_days; cost += ch_cost; minus += ch_pen; notes += ch_notes


            if base_cost and hq_mult:
                cost += base_cost * hq_mult
                notes.append(f"High-quality base: +{int(base_cost*hq_mult)} gp (×{int(hq_mult)}).")


            if rec.get("override"):
                ov = rec["override"]
                days = int(ov.get("days", days))
                cost = float(ov.get("cost_gp", cost))
                minus += int(ov.get("chance_minus", 0))
                notes.append("Recipe override applied.")


            if rec.get("any_class", False):
                cost += 1000 * max(1, len(effects) or 1)
                notes.append(f"Usable by any class: +{1000*max(1,len(effects) or 1)} gp.")


            if safe_boost:
                plus += 25
                cost *= 2
                days = int(math.ceil(days * 2))
                notes.append("Safety boost: +25% chance, ×2 cost/time.")

        else:
            raise ValueError(f"Unsupported recipe type '{t}' yet.")


        if _is_spellcrafter(cls_lc):
            plus += 25
            if lvl >= 6:
                days = max(1, math.ceil(days / 2))
                notes.append("Spellcrafter 6+: time halved.")
            if lvl >= 9:
                cost = round(cost * 0.75, 2)
                notes.append("Spellcrafter 9+: cost −25%.")

        final = max(1, min(99, base - minus + plus))
        return Quote(recipe_key, t, base, minus, plus, final, int(days), float(cost), notes)


    @commands.command(name="craft")
    async def craft_entry(self, ctx, subcmd: str = None, *args):
        import re, os, time, random
        if not subcmd:
            await ctx.send(
                "Usage:\n"
                "• `!craft list [filter]`\n"
                "• `!craft quote <item> [-doses N] [-safe]`\n"
                "• `!craft start <item> [-doses N] [-safe] [-finish \"note\"] [-as \"class\" (if Spellcrafter)] [-y]`\n"
                "• `!craft status`\n"
                "• `!craft due <project_id> <finish-note>`\n"
                "• `!craft cancel <project_id>`\n"
                "• `!craft resolve <project_id>` (GM-only)\n"
            )
            return

        sub = subcmd.lower()

        if sub == "list":
            import random, nextcord, re, math


            raw = [str(a) for a in args if str(a).strip()]
            page, parts = self._parse_page_flag(raw)
            tokens = [p for p in parts if p]


            TYPE_ALIAS = {
                "scroll": "spell_scroll", "scrolls": "spell_scroll",
                "weapon": "weapon", "weapons": "weapon",
                "armor": "armor", "armour": "armor",
                "wand": "wand", "wands": "wand",
                "staff": "staff", "staves": "staff",
                "potion": "potion", "potions": "potion",
                "rare": "rare", "rares": "rare",
                "ring": "ring", "rings": "ring",
                "misc": "misc",
            }
            want_type = ""
            if tokens and tokens[0].lower() in TYPE_ALIAS:
                want_type = TYPE_ALIAS[tokens[0].lower()]
                tokens = tokens[1:]


            want_scroll_level = None
            want_scroll_class = ""
            for t in list(tokens):
                m = re.fullmatch(r"[lL](\d+)", t)
                if m:
                    want_scroll_level = int(m.group(1))
                    tokens.remove(t)
                    break


            flt = " ".join(tokens).strip().lower()


            catalog = self._recipes_catalog()
            if not catalog:
                tried = "\n".join("• " + p for p in _candidate_paths(RECIPES_PATH))
                return await ctx.send("⚠️ No recipes found. I looked for **recipes.json** at:\n" + tried)


            if (not want_type) and (not flt) and (want_scroll_level is None) and (not want_scroll_class):

                total = len(catalog)
                emb = nextcord.Embed(
                    title="🧪 Crafting Browser",
                    description=(
                        f"You’ve got **{total}** craftable recipes loaded.\n"
                        f"Use the quick filters or search by name to narrow things down."
                    ),
                    color=random.randint(0, 0xFFFFFF),
                )
                emb.add_field(
                    name="Quick filters",
                    value=(
                        "`!craft list scrolls` • spell scrolls\n"
                        "`!craft list wands` • charged wands\n"
                        "`!craft list staff` • all staves (incl. Staffof…)\n"
                        "`!craft list potion` • magic potions (incl. Healing)\n"
                        "`!craft list weapons` • enchanted weapons\n"
                        "`!craft list armor` • enchanted armor\n"
                        "`!craft list rare` • rare/misc magic"
                    ),
                    inline=False,
                )
                emb.add_field(
                    name="Scroll drill-downs",
                    value=(
                        "`!craft list scroll L1`\n"
                        "`!craft list scroll L3 arcane`\n"
                        "`!craft list scroll L2 divine`"
                    ),
                    inline=False,
                )
                emb.add_field(
                    name="Search by name",
                    value=(
                        "`!craft list fire`  • partial match\n"
                        "`!craft list \"hand axe\"`  • quoted phrase\n"
                        "`!craft list staff power`  • multiple keywords"
                    ),
                    inline=False,
                )
                emb.add_field(
                    name="Next steps",
                    value=(
                        "`!craft quote <item>` — see time, cost, chance\n"
                        "`!craft start <item> -y` — pay & begin the project\n"
                        "Use `-p N` to change pages when a list is long."
                    ),
                    inline=False,
                )
                return await ctx.send(embed=emb)


            rows = [r for r in catalog if not want_type or r["type"] == want_type]


            if want_type == "spell_scroll":
                if want_scroll_level is not None:
                    rows = [r for r in rows if int(r.get("spell_level", -1)) == want_scroll_level]
                if want_scroll_class:
                    if want_scroll_class == "divine":
                        rows = [r for r in rows if (r.get("spell_class","") or "").lower() != "arcane"]
                    else:
                        rows = [r for r in rows if (r.get("spell_class","") or "").lower() == want_scroll_class]


            if flt:
                rows = [r for r in rows if flt in r["name"].lower()]


            def _fmt_scroll(r):
                meta = []
                lvl = r.get("spell_level", None)
                disp_cls = r.get("display_class", "") or r.get("spell_class", "")
                if isinstance(lvl, int):
                    meta.append(f"L{lvl}")
                if disp_cls:
                    meta.append(disp_cls if disp_cls not in {"arcane","divine"} else disp_cls)
                tail = f"  *({', '.join(meta)})*" if meta else ""
                return f"• {_clean_recipe_key_for_display(r['name'])}{tail}"


            def _fmt_weapon_or_armor(r):

                return f"• {r['name']}"

            def _fmt_generic(r):
                return f"• {r['name']} ({r['type']})"

            lines = []
            for r in rows:
                t = r["type"]
                if t == "spell_scroll":
                    lines.append(_fmt_scroll(r))
                elif t in {"weapon","armor"}:
                    lines.append(_fmt_weapon_or_armor(r))
                else:
                    lines.append(_fmt_generic(r))

            lines.sort(key=str.lower)


            PAGE = PAGE_SIZE_DEFAULT
            total = len(lines)
            pages = max(1, math.ceil(total / PAGE))
            page = max(1, min(page, pages))
            start = (page - 1) * PAGE
            chunk = lines[start:start + PAGE]


            def _add_block(embed, title, out_lines):
                if not out_lines:
                    return
                block, used, head = [], 0, title
                for ln in out_lines:
                    add = len(ln) + 1
                    if block and used + add > 1000:
                        embed.add_field(name=head, value="\n".join(block), inline=False)
                        block, used = [], 0
                        head = f"{title} (cont.)"
                    block.append(ln); used += add
                if block:
                    embed.add_field(name=head, value="\n".join(block), inline=False)

            title = (
                "📜 Craftable Scrolls" if want_type == "spell_scroll" else
                "🛠️ Craftable Weapons" if want_type == "weapon" else
                "🛡️ Craftable Armor"   if want_type == "armor" else
                "🧪 Craftable Items"
            )
            pretty = (tokens[0] if tokens else "All") if want_type else (flt or "All")
            desc = f"Filter: **{pretty if pretty else 'All'}**\nPage {page} / {pages} • Results: {total}\nUse `-p N` to navigate."
            embed = nextcord.Embed(title=title, description=desc, color=random.randint(0, 0xFFFFFF))
            _add_block(embed, "Results", chunk or ["*No matching items.*"])
            if not self.recipes:
                embed.add_field(
                    name="⚠ Data problem",
                    value=f"Couldn’t load recipes from `{RECIPES_PATH}` (syntax or path). "
                          f"Run `!craft validate` for exact line/col.",
                    inline=False
                )

            return await ctx.send(embed=embed)


        if sub == "status":
            ufile = os.path.join(CRAFT_DATA_DIR, f"{ctx.author.id}.json")
            projects = _load_json(ufile)
            if not projects:
                await ctx.send("No active crafting projects.")
                return

            lines = []
            for pid, p in projects.items():
                when_str, rel = _format_eta(p.get("started_ts", 0), int(p.get("days", 0)))
                fin = f" • finish: {p.get('finish')}" if p.get("finish") else ""

                lines.append(
                    f"`{pid}` • {p['item']} • days: {p['days']} • chance: {p['final_chance']}% • char: {p['owner_char']}{fin}"
                )

                lines.append(f"↳ ready ~ **{when_str}** ({rel})")

            await ctx.send("**Your crafting projects:**\n" + "\n".join(lines))
            return


        if sub in {"quote","start"}:
            if not args:
                await ctx.send(f"Usage: `!craft {sub} <item name> [-doses N] [-safe] [-finish \"note\"] [-y]`")
                return


            parts = list(args)
            doses = 1
            safe = False
            confirm = False
            finish_note = ""
            as_class = None


            if parts and parts[0].startswith(("'", '"')):
                raw = " ".join(parts)
                m = re.match(r'\s*["\'](.+?)["\']\s*(.*)$', raw)
                if m:
                    item = m.group(1); tail = m.group(2).strip().split()
                    parts = [item] + tail
            item = parts[0]


            i = 1
            while i < len(parts):
                tok = str(parts[i])
                low = tok.lower()

                if low == "-safe":
                    safe = True

                elif low == "-as" and (i + 1) < len(parts):
                    as_class = parts[i + 1]; i += 1
                elif low.startswith("-as="):
                    as_class = tok.split("=", 1)[1].strip()

                elif low == "-as" and (i + 1) < len(parts):
                    as_class = parts[i + 1]; i += 1
                elif low.startswith("-as="):
                    as_class = tok.split("=", 1)[1].strip()


                elif low in {"-y", "-yes", "-confirm"}:
                    confirm = True

                elif low.startswith("-doses="):
                    try: doses = max(1, int(low.split("=",1)[1]))
                    except Exception: pass

                elif low == "-doses" and (i + 1) < len(parts):
                    try: doses = max(1, int(parts[i+1])); i += 1
                    except Exception: pass

                elif low.startswith("-finish="):
                    finish_note = tok.split("=", 1)[1].strip().strip('"').strip("'")

                elif low == "-finish" and (i + 1) < len(parts):

                    j = i + 1
                    buff = []
                    while j < len(parts):
                        nxt = parts[j]
                        if nxt.startswith("-") or nxt in {"-y", "-yes", "-confirm"}:
                            break
                        buff.append(nxt)
                        j += 1
                    finish_note = " ".join(buff).strip().strip('"').strip("'")
                    i = j - 1

                i += 1


            active = get_active(ctx.author.id)
            if not active:
                await ctx.send("❌ No active character. Use `!char <name>` first.")
                return
            disp, path = _resolve_char_ci(active)
            if not path:
                await ctx.send(f"❌ Character file not found for **{active}**.")
                return
            who = _read_char_snapshot(path)


            key = None
            lower = {k.lower(): k for k in self.recipes.keys()}
            key = lower.get(item.lower())
            if not key:
                norm = lambda s: re.sub(r"[\s:_]+","", s).lower()
                idx = {norm(k): k for k in self.recipes.keys()}
                key = idx.get(norm(item))
            dyn_rec = None
            if not key:
                dyn_rec = (
                    self._build_dynamic_weapon_or_armor_recipe(item)
                    or self._build_dynamic_staff_recipe(item)
                    or self._build_dynamic_scroll_recipe(item)
                )
                if dyn_rec:
                    self.recipes[item] = dyn_rec
                    key = item


            if not key:
                await ctx.send(f"❌ Unknown craftable item: **{item}**.\nUse `!craft list <filter>`.")
                return


            resolved_cls = None
            rec = self.recipes.get(key, {}) or {}
            if (rec.get("type") or "").strip().lower() == "spell_scroll":
                pc_canon = self._canon_class_name(get_compat(who["cfg"], "info", "class", fallback="Fighter"))
                if pc_canon == "Spellcrafter":

                    resolved_cls = self._canon_class_name(as_class or "")
                else:
                    resolved_cls = pc_canon


            try:
                q = self._quote_for(
                    who, key, doses=doses, safe_boost=safe,
                    scroll_class_forced=as_class
                )

            except PermissionError as e:
                await ctx.send(f"❌ {e}")
                return
            except Exception as e:
                await ctx.send(f"❌ Couldn’t compute quote: `{type(e).__name__}: {e}`")
                return


            lines = [
                f"**{key}**  *(type: {q.cat})*",
                f"Chance: **{q.final_chance}%**  (base {q.base_chance}%  −{q.adj_minus}%  +{q.adj_plus}%)",
                f"Time: **{q.days} day(s)**",
                f"Cost: **{q.cost_gp:.2f} gp**",
            ]
            if q.notes:
                lines.append("Notes:\n- " + "\n- ".join(q.notes))

            if sub == "quote":
                await ctx.send("\n".join(lines))
                return


            if not confirm:
                extra = f"\nFinish: **{finish_note}**" if finish_note else ""
                await ctx.send("\n".join(lines) + extra + "\n\nRe-run with `-y` to begin crafting and pay the cost.")
                return


            if not _deduct_gp(who["cfg"], path, q.cost_gp):
                await ctx.send(f"❌ Not enough funds. Need **{q.cost_gp:.2f} gp**.")
                return


            os.makedirs(CRAFT_DATA_DIR, exist_ok=True)
            ufile = os.path.join(CRAFT_DATA_DIR, f"{ctx.author.id}.json")
            projects = _load_json(ufile)
            now = int(time.time())
            pid = str(now)
            projects[pid] = {
                "item": key,
                "type": q.cat,
                "doses": doses,
                "safe": bool(safe),
                "days": q.days,
                "cost_gp": q.cost_gp,
                "final_chance": q.final_chance,
                "started_ts": now,
                "owner_char": who["name"],
                "char_file": path,
                "class": get_compat(who["cfg"], "info", "class", fallback="Fighter"),
                "level": who["level"],
                "notes": q.notes,
                "finish": finish_note,
                "scroll_class": (resolved_cls or as_class or ""),

            }
            _save_json(ufile, projects)


            before_xp = getint_compat(who["cfg"], "cur", "xp", fallback=0)
            xp = _xp_award(who["cfg"], path, q.cost_gp, enabled=True)
            xp_line = f"  • XP **+{xp}**" if xp else ""


            try:
                import json, datetime
                from pathlib import Path
                logdir = Path("data/xp")
                logdir.mkdir(parents=True, exist_ok=True)
                p = logdir / f"{who['name'].replace(' ', '_')}.json"
                try:
                    log = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
                except Exception:
                    log = []
                log.append({
                    "ts": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "amount": int(xp or 0),
                    "by": str(ctx.author.id),
                    "reason": f"Crafting {key} (cost {q.cost_gp:.2f} gp)",
                    "before_xp": int(before_xp),
                    "after_xp": int(before_xp + (xp or 0)),
                    "before_lvl": int(who["level"]),
                    "after_lvl": int(who["level"]),
                })
                p.write_text(json.dumps(log, indent=2), encoding="utf-8")
            except Exception:
                pass

            fin_line = f"\nFinish: **{finish_note}**" if finish_note else ""

            when_str, rel = _format_eta(now, q.days)

            await ctx.send(
                f"🧪 Crafting begins for **{key}** (Project `{pid}`)\n"
                f"Time **{q.days}** day(s) • Cost **{q.cost_gp:.2f} gp** • Chance **{q.final_chance}%**{xp_line}{fin_line}\n"
                f"Ready ~ **{when_str}** ({rel})\n"
                f"*GM will resolve the result when downtime elapses.*"
            )
            return

        if sub == "status":
            ufile = os.path.join(CRAFT_DATA_DIR, f"{ctx.author.id}.json")
            projects = _load_json(ufile)
            if not projects:
                await ctx.send("No active crafting projects.")
                return

            lines = []
            for pid, p in projects.items():
                when_str, rel = _format_eta(p.get("started_ts", 0), int(p.get("days", 0)))
                fin = f" • finish: {p.get('finish')}" if p.get("finish") else ""

                lines.append(
                    f"`{pid}` • {p['item']} • days: {p['days']} • chance: {p['final_chance']}% • char: {p['owner_char']}{fin}"
                )

                lines.append(f"↳ ready ~ **{when_str}** ({rel})")

            await ctx.send("**Your crafting projects:**\n" + "\n".join(lines))
            return

        if sub == "due":

            if len(args) < 2:
                return await ctx.send("Usage: `!craft due <project_id> <finish-note>`  (ex: `!craft due 1760717145 Month 3, Day 12`)")
            pid, finish_note = args[0], " ".join(args[1:])

            found = None
            for fn in os.listdir(CRAFT_DATA_DIR):
                if not fn.endswith(".json"):
                    continue
                fp = os.path.join(CRAFT_DATA_DIR, fn)
                data = _load_json(fp)
                if pid in data:
                    found = (fp, data, fn)
                    break
            if not found:
                return await ctx.send("❌ Project not found.")

            fp, data, fn = found
            owner_uid = fn[:-5]
            if str(ctx.author.id) != owner_uid and not getattr(ctx.author.guild_permissions, "manage_guild", False):
                return await ctx.send("❌ Only the project owner or GM may set the finish note.")

            data[pid]["finish"] = finish_note.strip()
            _save_json(fp, data)
            return await ctx.send(f"📅 Project `{pid}` finish set to: **{finish_note.strip()}**")

        if sub == "cancel":
            if not args:
                await ctx.send("Usage: `!craft cancel <project_id>`")
                return
            pid = args[0]
            ufile = os.path.join(CRAFT_DATA_DIR, f"{ctx.author.id}.json")
            projects = _load_json(ufile)
            if pid not in projects:
                await ctx.send("❌ Unknown project id.")
                return
            proj = projects.pop(pid)
            _save_json(ufile, projects)
            await ctx.send(f"🗑️ Canceled project `{pid}` (**{proj['item']}**). *Costs are not refunded.*")
            return

        if sub == "resolve":

            if not getattr(ctx.author.guild_permissions, "manage_guild", False):
                await ctx.send("❌ Only the GM (Manage Server) can resolve crafting.")
                return
            if not args:
                await ctx.send("Usage: `!craft resolve <project_id>`")
                return
            pid = args[0]

            found = None
            for fn in os.listdir(CRAFT_DATA_DIR):
                if not fn.endswith(".json"): continue
                fp = os.path.join(CRAFT_DATA_DIR, fn)
                data = _load_json(fp)
                if pid in data:
                    found = (fp, data); break
            if not found:
                await ctx.send("❌ Project not found.")
                return
            fp, data = found
            p = data[pid]


            roll = random.randint(1,100)
            success = (roll <= int(p["final_chance"]))
            await ctx.send(
                f"🎲 Craft resolve `{pid}` • {p['item']} • chance {p['final_chance']}% • **roll {roll}** → "
                + ("**SUCCESS**" if success else "**FAIL**")
            )

            if success:
                if p["type"] == "spell_scroll":
                    rec = self.recipes.get(p["item"], {}) or {}
                    effects = rec.get("effects", []) or []
                    spells = [e.get("spell") for e in effects if e.get("spell")] or [p["item"]]

                    stored = (p.get("scroll_class") or "").strip()
                    rec_cls_raw = ((effects[0].get("class") if effects else "") or "").strip()


                    if stored and stored.lower() not in {"arcane","divine"}:
                        scroll_class = self._canon_class_name(stored)


                    elif rec_cls_raw and rec_cls_raw.lower() not in {"arcane","divine"}:
                        scroll_class = self._canon_class_name(rec_cls_raw)


                    else:
                        scroll_class, _L = self._pick_scroll_class_for_spell(
                            spells[0], read_cfg(p["char_file"]), override=(stored or None)
                        )


                    token = self._create_spell_scroll_instance(
                        p["char_file"], scroll_class, spells, label=f"Crafted {p['item']}", carry=False
                    )
                    await ctx.send(
                        f"✅ Added **{token}** to **{p['owner_char']}** containing: {', '.join(spells)} *(class: {scroll_class})*."
                    )


                else:
                    qty = int(p.get("doses", 1))
                    item_token = self._result_item_token(p["item"])

                    spells_cog = self.bot.get_cog("Spells")
                    looks_charged = item_token.lower().startswith(("wandof", "staffof"))


                    rec = self.recipes.get(p["item"], {}) or {}
                    recipe_charges_max = None
                    try:
                        recipe_charges_max = int((rec.get("charges") or {}).get("max") or 0) or None
                    except Exception:
                        recipe_charges_max = None

                    if spells_cog:
                        canon, it = spells_cog._item_lookup(item_token)
                        is_charged = spells_cog._item_has_charges(canon, it) or looks_charged

                        if is_charged:
                            cfg2 = read_cfg(p["char_file"])
                            made = []
                            for _ in range(qty):
                                tok = spells_cog._add_charged_item_instance(cfg2, p["char_file"], canon, it)

                                if recipe_charges_max is not None:
                                    ch_key = spells_cog._charges_key(tok, it)
                                    spells_cog._set_item_charges(cfg2, p["char_file"], ch_key, recipe_charges_max, recipe_charges_max)
                                made.append(tok)
                            await ctx.send(
                                f"✅ Added **{len(made)}× {canon}** to **{p['owner_char']}** (per-item charges tracked)."
                            )
                        else:
                            _add_inventory_items(p["char_file"], canon or item_token, qty)
                            await ctx.send(f"✅ Added **{qty}× {canon or item_token}** to **{p['owner_char']}**’s inventory.")
                    else:

                        if looks_charged:
                            made = []
                            for _ in range(qty):
                                tok = self._add_charged_instance_local(
                                    p["char_file"],
                                    item_token,
                                    charges_max=recipe_charges_max
                                )
                                made.append(tok)
                            await ctx.send(
                                f"✅ Added **{len(made)}× {item_token}** to **{p['owner_char']}** (per-item charges tracked)."
                            )
                        else:
                            _add_inventory_items(p["char_file"], item_token, qty)
                            await ctx.send(f"✅ Added **{qty}× {item_token}** to **{p['owner_char']}**’s inventory.")


            del data[pid]
            _save_json(fp, data)
            return

        if sub == "due":

            if len(args) < 2:
                return await ctx.send("Usage: `!craft due <project_id> <finish-note>`  (ex: `!craft due 1760717145 Month 3, Day 12`)")
            pid, finish_note = args[0], " ".join(args[1:])

            found = None
            for fn in os.listdir(CRAFT_DATA_DIR):
                if not fn.endswith(".json"):
                    continue
                fp = os.path.join(CRAFT_DATA_DIR, fn)
                data = _load_json(fp)
                if pid in data:
                    found = (fp, data, fn)
                    break
            if not found:
                return await ctx.send("❌ Project not found.")

            fp, data, fn = found
            owner_uid = fn[:-5]
            if str(ctx.author.id) != owner_uid and not getattr(ctx.author.guild_permissions, "manage_guild", False):
                return await ctx.send("❌ Only the project owner or GM may set the finish note.")

            data[pid]["finish"] = finish_note.strip()
            _save_json(fp, data)
            return await ctx.send(f"📅 Project `{pid}` finish set to: **{finish_note.strip()}**")

        if sub == "cancel":
            if not args:
                await ctx.send("Usage: `!craft cancel <project_id>`")
                return
            pid = args[0]
            ufile = os.path.join(CRAFT_DATA_DIR, f"{ctx.author.id}.json")
            projects = _load_json(ufile)
            if pid not in projects:
                await ctx.send("❌ Unknown project id.")
                return
            proj = projects.pop(pid)
            _save_json(ufile, projects)
            await ctx.send(f"🗑️ Canceled project `{pid}` (**{proj['item']}**). *Costs are not refunded.*")
            return


            if subcmd == "validate":
                path = RECIPES_PATH
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        json.load(f)
                    return await ctx.send(f"✅ `{path}` parses OK.")
                except json.JSONDecodeError as e:

                    with open(path, "r", encoding="utf-8") as f:
                        lines = f.read().splitlines()
                    i = e.lineno
                    lo, hi = max(1, i-3), min(len(lines), i+3)
                    excerpt = "\n".join(f"{j:>5}: {lines[j-1]}" for j in range(lo, hi+1))
                    return await ctx.send(
                        f"❌ JSON error in `{path}` at line {e.lineno}, col {e.colno}: {e.msg}\n"
                        f"```text\n{excerpt}\n```"
                    )


        await ctx.send("❌ Unknown subcommand. Try `!craft`.")
        return

    def _create_spell_scroll_instance(self, char_path: str, cls: str, spells: list[str], label: str = "", carry: bool = False) -> str:
        cfg = read_cfg(char_path)
        sid = str(self._alloc_scroll_id(cfg))
        rec = {"sid": sid, "class": cls, "spells": spells, "spent": [0]*len(spells), "readmagic": 0, "label": label}
        self._write_scroll_rec(cfg, rec)

        if not cfg.has_section("item"): cfg.add_section("item")
        token = f"SpellScroll@{sid}"
        storage = (get_compat(cfg, "item", "storage", fallback="") or "").split()
        if token not in storage:
            storage.append(token)
        cfg.set("item", "storage", " ".join(storage))
        cfg.set("item", token.lower(), "1")

        if carry:
            if not cfg.has_section("eq"): cfg.add_section("eq")
            placed = False
            for i in range(1, 9):
                if not (get_compat(cfg, "eq", f"carry{i}", fallback="") or "").strip():
                    cfg.set("eq", f"carry{i}", token)
                    cfg.set("eq", f"carry{i}_qty", "1")
                    placed = True
                    break


        try: self._recalc_carry_weight(cfg)
        except Exception: pass

        write_cfg(char_path, cfg)
        return token


    def _result_item_token(self, recipe_key: str) -> str:
        rec = self.recipes.get(recipe_key, {}) or {}

        name = rec.get("gives") or rec.get("item_name")
        if name:
            return str(name).strip()


        t = (rec.get("type") or "").strip().lower()
        if t == "potion" and ": " in recipe_key:
            return recipe_key.split(": ", 1)[1].strip()


        return recipe_key


    def _norm(self, s: str) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "", str(s).lower())

    def _scroll_id_from_token(self, token: str) -> str | None:

        if "@" in token:
            base, sid = token.split("@", 1)
            if self._norm(base) in {"spellscroll", "scrollspell", "spellscrolls"} and sid.strip():
                return sid.strip()
        return None

    def _item_lookup_strip_instance(self, name: str):
        """Like _item_lookup, but ignores '@<id>' suffix for DB lookups and returns (canon, item, sid)."""
        base = name.split("@", 1)[0]
        canon, item = self._item_lookup(base)
        sid = self._scroll_id_from_token(name)
        return canon, item, sid

    def _scroll_section_name(self, sid: str) -> str:
        return f"scroll:{sid}"

    def _ensure_scroll_index(self, cfg):
        if not cfg.has_section("scrollindex"):
            cfg.add_section("scrollindex")
            cfg.set("scrollindex", "next_id", "1")
        try:
            return int(cfg.get("scrollindex", "next_id"))
        except Exception:
            cfg.set("scrollindex", "next_id", "1")
            return 1

    def _alloc_scroll_id(self, cfg) -> int:
        nxt = self._ensure_scroll_index(cfg)
        cfg.set("scrollindex", "next_id", str(nxt + 1))
        return nxt

    def _read_scroll_rec(self, cfg, sid: str) -> dict | None:
        sec = self._scroll_section_name(sid)
        if not cfg.has_section(sec): return None
        cls = (get_compat(cfg, sec, "class", fallback="") or "").strip()
        spells = (get_compat(cfg, sec, "spells", fallback="") or "").strip()
        spent  = (get_compat(cfg, sec, "spent",  fallback="") or "").strip()
        readm  = (get_compat(cfg, sec, "readmagic", fallback="0") or "0").strip()
        label  = (get_compat(cfg, sec, "label", fallback="") or "").strip()

        spell_list = [s for s in spells.split("|") if s]
        spent_list = [int(x) if str(x).strip().isdigit() else 0 for x in spent.split(",") if str(x).strip()!=""]
        while len(spent_list) < len(spell_list): spent_list.append(0)
        if len(spent_list) > len(spell_list): spent_list = spent_list[:len(spell_list)]
        return {
            "sid": sid,
            "class": cls,
            "spells": spell_list,
            "spent": spent_list,
            "readmagic": 1 if readm == "1" else 0,
            "label": label,
        }

    def _write_scroll_rec(self, cfg, rec: dict) -> None:
        sec = self._scroll_section_name(rec["sid"])
        if not cfg.has_section(sec): cfg.add_section(sec)
        cfg.set(sec, "class", str(rec.get("class","")))
        cfg.set(sec, "spells", "|".join(rec.get("spells", [])))
        cfg.set(sec, "spent",  ",".join(str(int(x)) for x in rec.get("spent", [])))
        cfg.set(sec, "readmagic", "1" if rec.get("readmagic") else "0")
        if rec.get("label"):
            cfg.set(sec, "label", rec["label"])
        else:
            try: cfg.remove_option(sec, "label")
            except Exception: pass

    def _delete_scroll_rec(self, cfg, sid: str) -> None:
        sec = self._scroll_section_name(sid)
        try:
            cfg.remove_section(sec)
        except Exception:
            pass

    def _class_name(self, cfg) -> str:
        return (get_compat(cfg, "info", "class", fallback="") or "").strip()

    def _is_arcane_class(self, clsname: str) -> bool:
        return self._norm(clsname) in ARCANE_CLASSES


    def _max_spell_level_for_class(self, cfg, clsname: str) -> int | None:
        """
        Return the highest spell level the caster can cast *right now*.
        Prefer the live [slots] totals in the character file, else fall back
        to a simple heuristic.
        """
        try:
            max_lvl = 0
            for L in range(1, 8):
                tot = getint_compat(cfg, "slots", f"l{L}_total", fallback=0)
                if (tot or 0) > 0:
                    max_lvl = L
            if max_lvl > 0:
                return max_lvl
        except Exception:
            pass


        lvl = getint_compat(cfg, "cur", "level", fallback=1)
        if self._is_arcane_class(clsname):
            return min(7, max(1, int(lvl) // 2))
        return min(7, max(1, (int(lvl) + 1) // 2))


    def _find_spell_level(self, scroll_class: str, spell_name: str) -> int | None:
        try:
            cls = self._norm(scroll_class)
            sn  = self._norm(spell_name)
            v = self.spell_levels.get(cls, {}).get(sn, None)
            if isinstance(v, (list, tuple)):

                return int(v[0])
            return int(v) if v is not None else None
        except Exception:
            return None


    def _pc_knows_spell(self, caster_cfg, caster_class: str, spell_name: str) -> bool | None:
        """
        True if we can tell the PC knows this spell (arcane spellbook or legacy [book]),
        False if we can tell they do not, None if indeterminable.
        """
        try:
            want = self._norm(spell_name)


            book = get_compat(caster_cfg, "book", "spells", fallback="")
            if book:
                have = {self._norm(s) for s in re.split(r"[\s,]+", str(book)) if s}
                return want in have


            names = set()
            for L in range(0, 10):
                raw = get_compat(caster_cfg, "spellbook", f"l{L}", fallback="")
                if raw:
                    for n in re.split(r"[\s,]+", str(raw)):
                        if n:
                            names.add(self._norm(n))
            if names:
                return want in names


            return None
        except Exception:
            return None


    def _pick_carried_scroll_with_spell(self, cfg, want_cls: str, want_spell: str, require_readmagic: bool, carried_only=True) -> tuple[str, dict] | None:
        """
        Return (token, rec) for a carried SpellScroll that contains an unspent copy of want_spell and class matches.
        If multiple candidates, picks the first. You can extend to prefer specific IDs with -s.
        """
        want_spell_n = self._norm(want_spell)
        want_cls_n = self._norm(want_cls)

        tokens = []
        try:
            for _idx, slot_key, nm, q in self._iter_carried_slots(cfg):
                sid = self._scroll_id_from_token(nm)
                if not sid: continue
                rec = self._read_scroll_rec(cfg, sid)
                if not rec: continue
                if self._norm(rec["class"]) != want_cls_n: continue
                if require_readmagic and self._is_arcane_class(rec["class"]) and not rec.get("readmagic"):
                    continue

                for s, used in zip(rec["spells"], rec["spent"]):
                    if not used and self._norm(s) == want_spell_n:
                        tokens.append((nm, rec))
                        break
        except Exception:
            pass
        if tokens:
            return tokens[0]

        return None

    def _mark_scroll_spell_spent_and_cleanup(self, cfg, token: str, spell_name: str) -> bool:
        """
        Mark one copy of spell_name as spent on the given token's scroll.
        If all spells are spent afterwards, remove the scroll item from inventory (consume one)
        and delete its [scroll:<id>] section. Recalculates carry weight.
        """
        sid = self._scroll_id_from_token(token)
        if not sid: return False
        rec = self._read_scroll_rec(cfg, sid)
        if not rec: return False
        sn = self._norm(spell_name)


        for i, (s, used) in enumerate(zip(rec["spells"], rec["spent"])):
            if not used and self._norm(s) == sn:
                rec["spent"][i] = 1
                break
        else:
            return False


        self._write_scroll_rec(cfg, rec)


        if all(int(x) == 1 for x in rec["spent"]):

            try:
                self._consume_one_anywhere_and_recalc(cfg, f"SpellScroll@{sid}")
            except Exception:

                pass
            self._delete_scroll_rec(cfg, sid)
        return True


    def _add_charged_instance_local(self, char_path: str, canon_name: str, charges_max: int | None = None) -> str:
        """Create canon@<id> with qty=1 and seed [charges] like additem’s fallback."""
        import secrets
        cfg = read_cfg(char_path)

        token = f"{canon_name}@{secrets.token_hex(3)}"

        storage = (get_compat(cfg, "item", "storage", fallback="") or "").split()
        if token not in storage:
            storage.append(token)
        if not cfg.has_section("item"):
            cfg.add_section("item")


        cfg.set("item", token.lower(), "1")
        cfg.set("item", canon_name.lower(), "0")


        if charges_max is None:
            charges_max = 30 if canon_name.lower().startswith("staffof") else 20


        core = "".join(ch for ch in canon_name.lower() if ch.isalnum())
        suf  = token.split("@", 1)[1].lower()
        ch_key = f"{core}_{suf}"

        if not cfg.has_section("charges"):
            cfg.add_section("charges")
        cfg.set("charges", ch_key, f"{charges_max}/{charges_max}")

        cfg.set("item", "storage", " ".join(storage))
        write_cfg(char_path, cfg)
        return token

    def _weapon_power_levels(self) -> dict:

        defaults = {"light":1, "charmperson":1, "locateobject":2, "flame":2, "drain":7, "wish":7}
        user = _load_json(WEAPON_POWER_FILE)
        if not isinstance(user, dict):
            user = {}
        for k, v in user.items():
            try:
                defaults[str(k).lower()] = int(v)
            except Exception:
                pass
        return defaults


    def _build_dynamic_weapon_or_armor_recipe(self, item_key: str) -> Optional[dict]:
        """
        Inspect the item DB via Spells cog and synthesize a recipe for weapons/armor.
        Returns a *recipe dict* or None if not recognized/craftable.
        """
        spells_cog = self._spells()
        if not spells_cog:
            return None

        canon, it = spells_cog._item_lookup(item_key)
        if not it:
            return None

        t = (it.get("type") or "").strip().lower()
        is_armor = t in {"armor", "shield"}
        is_weapon = ("plus" in it) and not is_armor

        if not (is_armor or is_weapon):
            return None


        try:
            base_plus = int(str(it.get("plus", "0")).strip() or "0")
        except Exception:
            base_plus = 0


        vs_plus = 0
        for k in VS_KEYS:
            try:
                vs_plus = max(vs_plus, int(str(it.get(k, "0")).strip() or "0"))
            except Exception:
                pass


        if base_plus <= 0 and vs_plus <= 0:
            return None

        rec_type = "armor" if is_armor else "weapon"
        rec = {
            "type": rec_type,
            "min_level": {"default": 9, "spellcrafter": 7},
            "weapon_meta": {"base_plus": base_plus, "vs_plus": vs_plus},
            "gives": canon or item_key
        }


        special = (it.get("special") or "").strip().lower()
        if special:
            lvl_map = self._weapon_power_levels()
            lvl = lvl_map.get(special)
            if isinstance(lvl, int) and lvl > 0:
                rec["effects"] = [{"level": lvl}]

                if special in {"charmperson","locateobject","light"}:
                    rec["effects"][0]["spell"] = special.title().replace("object","Object").replace("person","Person")

        return rec


    def _parse_page_flag(self, parts: list[str], default: int = 1) -> tuple[int, list[str]]:
        """
        Supports: -p N  and  -page N  and  -page=N
        Returns (page_number, remaining_tokens)
        """
        page = default
        out: list[str] = []
        i = 0
        while i < len(parts):
            t = str(parts[i]).strip().lower()
            if t in ("-p", "-page") and i + 1 < len(parts):
                try:
                    page = max(1, int(parts[i + 1]))
                    i += 1
                except Exception:
                    pass
            elif t.startswith("-page="):
                try:
                    page = max(1, int(t.split("=", 1)[1]))
                except Exception:
                    pass
            else:
                out.append(parts[i])
            i += 1
        return page, out


    def _recipes_catalog(self) -> list[dict]:
        """
        Normalize recipes.json into a simple list we can filter & sort.
        Each row looks like:
          {"name":..., "type":..., "spell_level":int|0, "spell_class":str|"",
           "effective":float|0.0, "rec": <original recipe dict>}
        """
        cat = []
        recs = self.recipes or {}
        for name, rec in recs.items():
            if not isinstance(rec, dict):
                continue
            t = (rec.get("type") or "").strip().lower()
            row = {"name": name, "type": t, "rec": rec, "spell_level": 0, "spell_class": "", "effective": 0.0}

            if t == "spell_scroll":
                effs = rec.get("effects") or []

                spell_nm = ""
                if effs and effs[0].get("spell"):
                    spell_nm = str(effs[0]["spell"]).strip()
                elif name.lower().startswith("scroll"):

                    parts = name.split(":", 1)
                    if len(parts) == 2:
                        spell_nm = parts[1].strip()


                pairs = self._classes_and_levels_for_spell(spell_nm) if spell_nm else []

                if not pairs:
                    lvl = 0
                    try:
                        lvl = max(int(e.get("level") or 0) for e in effs) if effs else 0
                    except Exception:
                        lvl = 0
                    cls_raw = (effs[0].get("class", "") if effs else "") or ""
                    row["spell_level"]  = int(lvl)
                    row["spell_class"]  = str(cls_raw).strip().lower() or ""
                    row["display_class"] = self._canon_class_name(cls_raw) if cls_raw else ""
                    row["class_levels"] = []
                else:

                    order = {"Magic-User":0,"Illusionist":1,"Necromancer":2,"Spellcrafter":3,
                             "Cleric":4,"Druid":5,"Paladin":6}
                    pairs.sort(key=lambda t2: (int(t2[1]), order.get(self._canon_class_name(t2[0]), 99)))
                    disp_cls, disp_lvl = pairs[0]
                    row["display_class"] = self._canon_class_name(disp_cls)
                    row["spell_level"]   = int(disp_lvl)
                    row["spell_class"]   = "divine" if self._class_side(disp_cls) == "divine" else "arcane"
                    row["class_levels"]  = [(self._canon_class_name(c), int(L)) for (c, L) in pairs]


            elif t in {"weapon", "armor"}:
                wm = rec.get("weapon_meta") or {}
                def _ival(v):
                    try: return int(str(v).strip())
                    except Exception: return 0
                base = _ival(wm.get("base_plus"))
                vs   = _ival(wm.get("vs_plus"))
                row["effective"] = float(base + (vs/2.0 if vs > 0 else 0.0))

            cat.append(row)
        return cat


    def _embed_add_block(self, embed, header: str, lines: list[str]) -> None:
        """
        Discord embed field value is capped at 1024 chars.
        This helper chunks lines across multiple fields with “(cont.)” headers.
        """
        if not lines:
            embed.add_field(name=header, value="*No matching items.*", inline=False)
            return

        cur: list[str] = []
        cur_len = 0
        part = 0
        for ln in lines:
            add_len = len(ln) + 1
            if cur and (cur_len + add_len) > 1000:
                title = header if part == 0 else f"{header} (cont. {part})"
                embed.add_field(name=title, value="\n".join(cur), inline=False)
                cur, cur_len = [], 0
                part += 1
            cur.append(ln)
            cur_len += add_len
        if cur:
            title = header if part == 0 else f"{header} (cont. {part})"
            embed.add_field(name=title, value="\n".join(cur), inline=False)


    def _all_spells_index(self):
        """
        Return mapping {ClassName: [(spell_name, level_int), ...]} using Spells cog.
        Tries a few shapes; tweak if your Spells cog exposes a different API.
        """
        sc = self._spells()
        out = {}
        if not sc:
            return out


        if hasattr(sc, "spell_levels"):
            for clsname, table in getattr(sc, "spell_levels").items():
                rows = []
                for spell, lv in table.items():
                    L = int(lv[0] if isinstance(lv, (list,tuple)) else lv)
                    rows.append((spell, L))
                rows.sort(key=lambda r: (r[1], r[0].lower()))
                out[clsname] = rows
            return out


        if hasattr(sc, "all_spells_by_class"):
            return sc.all_spells_by_class()

        return out

    def _build_dynamic_scroll_recipe(self, item_key: str) -> Optional[dict]:
        m = re.match(r"^\s*scroll[:\s]+(.+)$", item_key, re.I)
        if not m:
            return None
        spell_name = m.group(1).strip()

        index = self._all_spells_index()
        if not index:
            return None


        pick = None
        pick_cls = None
        for clsname, rows in index.items():
            for nm, L in rows:
                if nm.lower() == spell_name.lower():
                    if pick is None or self._is_spell_class_arcane(clsname):
                        pick = (nm, int(L))
                        pick_cls = clsname
                        if self._is_spell_class_arcane(clsname):
                            break
            if pick and self._is_spell_class_arcane(clsname):
                break

        if not pick:
            return None

        nm, L = pick

        return {
            "type": "spell_scroll",
            "effects": [{"spell": nm, "level": int(L), "class": pick_cls}],
            "min_level": {"default": 1, "spellcrafter": 1}
        }


    def _spells(self):
        return self.bot.get_cog("Spells") or self.bot.get_cog("SpellsCog")


    def _craftable_catalog(self) -> list[tuple[str, str]]:
        """
        Build the unified catalog used by *all* list modes, but only from recipes.json.
        Returns a list of (name, type) where type ∈ {'potion','spell_scroll','wand','staff',
        'rare','weapon','armor','ring','misc'}.
        """
        out: list[tuple[str, str]] = []
        for k, rec in (self.recipes or {}).items():
            t = str(rec.get("type", "") or "").strip().lower()
            out.append((k, t))
        out.sort(key=lambda t: (t[1], t[0].lower()))
        return out


    @commands.command(name="craftdump")
    @commands.has_guild_permissions(manage_guild=True)
    async def craft_dump(self, ctx):
        """
        Write a simplified catalog to data/craft_catalog.json and upload it.
        """
        out = []
        for name, rec in (self.recipes or {}).items():
            entry = {"name": _clean_recipe_key_for_display(name), "type": rec.get("type")}
            t = (rec.get("type") or "").lower()
            if t == "spell_scroll":
                e = (rec.get("effects") or [{}])[0]
                entry["level"] = e.get("level")
                entry["class"] = e.get("class")
            elif t in {"weapon","armor"}:
                meta = rec.get("weapon_meta") or {}
                entry["base_plus"] = meta.get("base_plus", rec.get("base_plus"))
                entry["vs_plus"]   = meta.get("vs_plus",   rec.get("vs_bonus"))
                entry["effective"] = _weapon_effective_plus(rec)
            else:

                for k in ("min_level","charges","override","features"):
                    if k in rec: entry[k] = rec[k]
            out.append(entry)

        os.makedirs("data", exist_ok=True)
        path = os.path.join("data","craft_catalog.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        try:
            await ctx.send(file=nextcord.File(path, filename="craft_catalog.json"))
        except Exception:
            await ctx.send("Wrote data/craft_catalog.json")

    def _read_override(self, rec: dict):
        """Return (days, cost_gp, chance_minus, notes) from a recipe 'override' dict, or (None, None, None, None)."""
        ov = rec.get("override")
        if not isinstance(ov, dict):
            return None, None, None, None
        try:
            days = int(str(ov.get("days") or 0))
        except Exception:
            days = 0
        try:
            cost_gp = float(str(ov.get("cost_gp") or 0))
        except Exception:
            cost_gp = 0.0
        try:
            ch_minus = int(str(ov.get("chance_minus") or 0))
        except Exception:
            ch_minus = 0
        note = "Override: days={d}, cost={c} gp, chance −{m}%".format(d=days, c=int(cost_gp) if cost_gp.is_integer() else cost_gp, m=ch_minus)
        return days, cost_gp, ch_minus, note


    def _has_timer_code(self, ctx, who_name: str, code: str) -> bool:
        bcfg = _load_battles()
        chan = str(ctx.channel.id)
        if not bcfg or not bcfg.has_section(chan):
            return False
        slot = self._slot(who_name)
        if slot <= 0:
            return False


        if code == "BR" and bcfg.getint(chan, f"{slot}.x_break", fallback=0) > 0:
            return True


        for opt, val in bcfg.items(chan):
            if not opt.startswith(f"{slot}.x_") or not opt.endswith("_code"):
                continue
            if (val or "").strip().upper() == code:
                rounds_key = opt[:-5]
                if bcfg.getint(chan, rounds_key, fallback=0) > 0:
                    return True
        return False

    def _break_ok(self, ctx, who_name: str) -> bool:
        return self._has_timer_code(ctx, who_name, "BR")


    def _canon_class_name(self, s: str) -> str:
        x = (s or "").strip().lower()
        m = {
            "mu":"Magic-User","magicuser":"Magic-User","magic-user":"Magic-User","mage":"Magic-User","arcane":"arcane",
            "ill":"Illusionist","illusionist":"Illusionist",
            "nec":"Necromancer","necromancer":"Necromancer",
            "sc":"Spellcrafter","spellcrafter":"Spellcrafter",
            "cl":"Cleric","cleric":"Cleric",
            "dr":"Druid","druid":"Druid",
            "pal":"Paladin","paladin":"Paladin",
            "divine":"divine",
        }
        return m.get(x, s.strip())

    def _class_side(self, clsname: str) -> str:
        c = (clsname or "").strip().lower()
        if c in {"cleric","druid","paladin"}: return "divine"
        return "arcane"

    def _classes_and_levels_for_spell(self, spell_name: str) -> list[tuple[str,int]]:
        rows = []
        index = self._all_spells_index() or {}
        want = self._norm(spell_name)
        for clsname, pairs in index.items():
            for nm, L in pairs:
                if self._norm(nm) == want:
                    rows.append((clsname, int(L)))
        return rows

    def _pick_scroll_class_for_spell(self, spell_name: str, caster_cfg, override: str | None = None) -> tuple[str,int]:
        cand = self._classes_and_levels_for_spell(spell_name)
        if not cand:
            return ("Magic-User", max(1, self._find_spell_level("Magic-User", spell_name) or 1))


        canon: dict[str,int] = {}
        for cls, L in cand:
            c = self._canon_class_name(cls)
            try:
                L = int(L)
            except Exception:
                L = 0
            canon[c] = min(L, canon.get(c, 99))

        items = list(canon.items())

        def _pick_lowest(cands: list[tuple[str,int]], priority: list[str]) -> tuple[str,int]:
            order = {name:i for i, name in enumerate(priority)}
            return sorted(cands, key=lambda t: (int(t[1]), order.get(t[0], 999)))[0]


        arc_items  = [(c, L) for c, L in items if self._class_side(c) == "arcane"]
        arc_specs  = [(c, L) for c, L in arc_items if c not in {"Magic-User", "Spellcrafter"}]


        if override and override not in {"arcane", "divine"}:
            ov = self._canon_class_name(override)
            if ov in canon:
                return (ov, canon[ov])


        if override == "arcane" and arc_items:
            return _pick_lowest(arc_specs or arc_items, ["Illusionist", "Necromancer", "Magic-User", "Spellcrafter"])


        if override == "divine":
            div_items = [(c, L) for c, L in items if self._class_side(c) == "divine"]
            if div_items:
                return _pick_lowest(div_items, ["Cleric", "Druid", "Paladin"])


        my_cls  = (get_compat(caster_cfg, "info", "class", fallback="") or "").strip()
        my_side = self._class_side(my_cls)
        mine = [(c, L) for c, L in items if self._class_side(c) == my_side]
        if mine:
            if my_side == "divine":
                return _pick_lowest(mine, ["Cleric", "Druid", "Paladin"])
            else:
                return _pick_lowest(mine, ["Illusionist", "Necromancer", "Magic-User", "Spellcrafter"])


        if arc_specs:
            return _pick_lowest(arc_specs, ["Illusionist", "Necromancer"])
        return _pick_lowest(items, ["Magic-User", "Illusionist", "Necromancer", "Spellcrafter", "Cleric", "Druid", "Paladin"])


def setup(bot):
    bot.add_cog(Crafting(bot))
