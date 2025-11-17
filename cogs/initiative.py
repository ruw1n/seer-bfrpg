from __future__ import annotations
import os
import random
import re
import configparser
import nextcord
import json
import sys
import time
import copy
from nextcord.ext import commands
from typing import Dict, List, Tuple, Optional
from utils.players import get_active
from utils.ini import read_cfg, get_compat, getint_compat, write_cfg
from pathlib import Path

MONSTER_DIR = "./monsters"

_X_SKIP_GENERIC = {
    "x_fastvenom", "x_slowvenom",
    "x_heatmetal", "x_chillmetal",
    "x_swarm", "x_swarm_torch",
    "x_constrict", "x_holdbite", "x_leech", "x_entangle",
    "x_swallow", "x_tentacles", "x_dissolve",
    "x_rotgrub", "x_rotgrub_burn", "x_rotgrub_notice",
}

def _sanity_patch_builtins() -> None:
    """If any built-in rows are wrong (from stale code), fix them in-place."""
    want_f1 = {"poi":12, "wand":13, "para":14, "breath":15, "spell":17}
    bad = False
    cur = globals().get("_DEFAULT_F1", {})
    if tuple(cur.get(k) for k in ("poi","wand","para","breath","spell")) !=       tuple(want_f1[k] for k in ("poi","wand","para","breath","spell")):
        globals()["_DEFAULT_F1"] = want_f1.copy()
        bad = True
    frow = globals().get("_BUILTIN_L1", {}).get("fighter", {})
    if tuple(frow.get(k) for k in ("poi","wand","para","breath","spell")) !=       tuple(want_f1[k] for k in ("poi","wand","para","breath","spell")):
        _BUILTIN_L1["fighter"] = want_f1.copy()
        bad = True
    if bad:

        pass

def _rotgrubs_badge(cfg, chan_id: str, slot: str) -> str:
    try:
        rem = cfg.getint(chan_id, f"{slot}.x_rotgrub", fallback=0)
    except Exception:
        rem = 0
    if rem <= 0:
        return ""
    code = (cfg.get(chan_id, f"{slot}.x_rotgrub_code", fallback="disease") or "disease").strip()
    return f" • [{code} {rem}]"

SAVE_KEYS = ("poi", "wand", "para", "breath", "spell")

_BUILTIN_L1 = {
    "fighter":   {"poi": 12, "wand": 13, "para": 14, "breath": 15, "spell": 17},
    "cleric":    {"poi": 11, "wand": 12, "para": 14, "breath": 16, "spell": 15},
    "magicuser": {"poi": 13, "wand": 14, "para": 13, "breath": 16, "spell": 15},
}
_DEFAULT_F1 = _BUILTIN_L1["fighter"]

_CLASS_SAVES: Optional[Dict[str, Dict[str, List[int]]]] = None
_CLASS_SAVES_SOURCE: Optional[str] = None
_CLASS_SAVES_OVERRIDE_PATH: Optional[str] = None
_CLASS_LST_ENV = "SEER_CLASS_LST"

_SAVE_KEY_CANON = {
    "poi": "poi", "poison": "poi", "death": "poi",
    "wand": "wand", "wands": "wand",
    "para": "para", "paralyze": "para", "paralysis": "para", "petrify": "para",
    "breath": "breath", "dragonbreath": "breath",
    "spell": "spell", "spells": "spell"
}

def _canon_vs(v: str) -> str:
    k = (str(v) or "").lower().replace(" ", "")
    return _SAVE_KEY_CANON.get(k, k)

def _norm_class_name(name: str) -> str:
    n = (name or "").strip().lower().replace("_", " ").replace("-", " ")

    exact = {
        "fighter": "fighter",
        "fightermage": "fightermage", "fighter mage": "fightermage",
        "magic user": "magic-user", "magic-user": "magic-user", "mu": "magic-user", "mage": "magic-user", "wizard": "magic-user",
        "cleric": "cleric", "druid": "druid", "thief": "thief", "assassin": "assassin",
        "ranger": "ranger", "paladin": "paladin", "barbarian": "barbarian",
        "illusionist": "illusionist", "necromancer": "necromancer", "spellcrafter": "spellcrafter",
        "nm": "nm", "normal man": "nm", "normalman": "nm",
    }
    return exact.get(n, n)

def _section_for_class(klass: str) -> str | None:
    """Return the exact class section (no prefix/fuzzy)."""
    k = _norm_class_name(klass)

    by_norm = { _norm_class_name(sec): sec for sec in _CLASS_SAVES.keys() }
    return by_norm.get(k)

def _load_class_saves_from_cache() -> dict[str, dict[str, list[int]]]:
    out = {}
    for sec, kv in _CLASS_CACHE.items():
        block = {}
        for key in ("poi", "wand", "para", "breath", "spell"):
            src = "spells" if key == "spell" and "spells" in kv else key
            vals = kv.get(src, [])
            block[key] = vals if isinstance(vals, list) else []
        out[sec] = block
    return out

def _find_class_lst_with_traces() -> Tuple[Optional[str], List[str]]:
    """Find class.lst using override, env, CWD, data, and file dir."""
    tried: List[str] = []

    if _CLASS_SAVES_OVERRIDE_PATH:
        p = os.path.abspath(_CLASS_SAVES_OVERRIDE_PATH)
        tried.append(p)
        if os.path.exists(p): return p, tried

    envp = os.environ.get(_CLASS_LST_ENV)
    if envp:
        p = os.path.abspath(envp)
        tried.append(p)
        if os.path.exists(p): return p, tried

    here = os.path.dirname(__file__)
    for p in [
        os.path.join(os.getcwd(), "class.lst"),
        os.path.join(os.getcwd(), "data", "class.lst"),
        os.path.join(here, "class.lst"),
        "class.lst",
    ]:
        tried.append(p)
        if os.path.exists(p): return p, tried
    return None, tried

def load_class_saves(path: Optional[str]) -> Dict[str, Dict[str, List[int]]]:
    """Parse class.lst into {class: {poi|wand|para|breath|spell: [20 ints]}}."""
    if not path: return {}
    cfg = configparser.ConfigParser()
    cfg.optionxform = str.lower
    with open(path, "r", encoding="utf-8") as f:
        cfg.read_file(f)
    out: Dict[str, Dict[str, List[int]]] = {}
    for sect in cfg.sections():
        vals = cfg[sect]
        row: Dict[str, List[int]] = {}
        for src in SAVE_KEYS:
            if src in vals:
                nums = [int(x) for x in re.findall(r"-?\d+", vals[src])]
                if not nums: continue
                if len(nums) < 20: nums = nums + [nums[-1]] * (20 - len(nums))
                row[src] = nums[:20]
        if row:
            out[_norm_class_name(sect)] = row
    return out

def _ensure_tables_loaded(force: bool=False) -> Optional[str]:
    """Load or reload tables. Returns the source path used (or None for builtin)."""
    global _CLASS_SAVES, _CLASS_SAVES_SOURCE
    if _CLASS_SAVES is not None and not force:
        return _CLASS_SAVES_SOURCE
    path, _tried = _find_class_lst_with_traces()
    tables = load_class_saves(path) if path else {}
    if tables:
        _CLASS_SAVES = tables
        _CLASS_SAVES_SOURCE = os.path.abspath(path) if path else None
    else:
        _CLASS_SAVES = {}
        _CLASS_SAVES_SOURCE = None
    return _CLASS_SAVES_SOURCE

def _get_f1_row() -> Dict[str, int]:
    """Fighter 1 row, from file if present; else builtin."""
    _ensure_tables_loaded()
    if _CLASS_SAVES and "fighter" in _CLASS_SAVES:
        row = _CLASS_SAVES["fighter"]
        return {k: int(row[k][0]) for k in SAVE_KEYS if k in row}
    return dict(_DEFAULT_F1)

def _parse_saveas(self, s: str) -> tuple[str, int]:
    """
    Accepts:
      • 'Fighter 5', 'fighter 05'
      • 'NM', 'Normal Man'
      • 'Fighter' (defaults to level 1)
    Returns (klass, level). Level may be ignored by special classes like NM.
    """
    t = (str(s) or "").strip()
    if not t:
        return "", 0
    norm = t.lower().replace("_", " ").replace("-", " ").strip()
    if norm in {"nm", "normal man", "normalman"}:
        return "NM", 1

    m = re.match(r"\s*([A-Za-z][A-Za-z\-\s]+?)\s+(\d+)\s*$", t)
    if m:
        return m.group(1).strip(), int(m.group(2))

    return t.title(), 1

def _get_saveas_from_cfg(t_cfg) -> Optional[str]:
    """Try to read 'saveas' from [base]/[stats]/[info] (case-insensitive)."""
    for sec in ("base","stats","info"):
        try:
            try:
                v = get_compat(t_cfg, sec, "saveas", fallback=None)
            except Exception:
                v = t_cfg.get(sec, "saveas", fallback=None) if hasattr(t_cfg, "get") else None
            if v:
                s = str(v).strip()
                if s:
                    return s
        except Exception:
            pass
    return None

def _norm_spell_key(name: str) -> str:
    """Lowercase + strip non-alnum to match 'protectionfromevil' style keys."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())

def _get_first_spells_section(cfg) -> str | None:
    """
    Return the name of the first section that contains a 'spells' key.
    Prefers [base], then scans the rest.
    """
    try_order = ["base", "stats", "spells", "info"]
    for sec in try_order:
        try:
            if cfg.has_section(sec):
                val = (get_compat(cfg, sec, "spells", fallback="") or "").strip()
                if val:
                    return sec
        except Exception:
            pass

    try:
        for sec in cfg.sections():
            try:
                val = (get_compat(cfg, sec, "spells", fallback="") or "").strip()
                if val:
                    return sec
            except Exception:
                pass
    except Exception:
        pass
    return None

def _apply_mitigation(raw, weapon_name="", weapon_type="", t_cfg=None, is_magical=None,
                      chan_id=None, target_name=None):
    import re, math, os

    def _tokset(s):
        return {x.strip().lower() for x in re.split(r"[,\s]+", str(s or "")) if x.strip()}

    def _merge_keys(cfg, section, *keys):
        out = set()
        for k in keys:
            out |= _tokset(get_compat(cfg, section, k, fallback=""))
        return out

    imm   = _merge_keys(t_cfg,"base","immune","immunity","immune_types")   | _merge_keys(t_cfg,"stats","immune","immunity","immune_types")
    res   = _merge_keys(t_cfg,"base","resist","resistance","resist_types") | _merge_keys(t_cfg,"stats","resist","resistance","resist_types")
    weak  = (_merge_keys(t_cfg,"base","weak","weakness","weak_types","vulnerable","vulnerability","vuln")
           | _merge_keys(t_cfg,"stats","weak","weakness","weak_types","vulnerable","vulnerability","vuln")
           | _merge_keys(t_cfg,"info","weak","weakness","weak_types","vulnerable","vulnerability","vuln"))
    absorb = (_merge_keys(t_cfg,"base","absorb","absorb_types")
           |  _merge_keys(t_cfg,"stats","absorb","absorb_types")
           |  _merge_keys(t_cfg,"info","absorb","absorb_types"))

    try:
        mtype = (get_compat(t_cfg, "info", "monster_type", fallback="")
                 or get_compat(t_cfg, "info", "type", fallback="")).strip().lower()
        if mtype:
            for cand in (f"{mtype}.ini", os.path.join("mon", f"{mtype}.ini"), os.path.join("monsters", f"{mtype}.ini")):
                if os.path.exists(cand):
                    base_cfg = read_cfg(cand)
                    imm   |= _merge_keys(base_cfg,"base","immune","immunity","immune_types")   | _merge_keys(base_cfg,"stats","immune","immunity","immune_types")
                    res   |= _merge_keys(base_cfg,"base","resist","resistance","resist_types") | _merge_keys(base_cfg,"stats","resist","resistance","resist_types")
                    weak  |= (_merge_keys(base_cfg,"base","weak","weakness","weak_types","vulnerable","vulnerability","vuln")
                           |  _merge_keys(base_cfg,"stats","weak","weakness","weak_types","vulnerable","vulnerability","vuln")
                           |  _merge_keys(base_cfg,"info","weak","weakness","weak_types","vulnerable","vulnerability","vuln"))
                    absorb |= (_merge_keys(base_cfg,"base","absorb","absorb_types")
                            |   _merge_keys(base_cfg,"stats","absorb","absorb_types")
                            |   _merge_keys(base_cfg,"info","absorb","absorb_types"))
                    break
    except Exception:
        pass

    def _norm_tok(t):
        t = (t or "").lower()
        if t in {"elec","electricity","lightning"}: return "electric"
        if t in {"acidic"}: return "acid"
        return t

    wt = (weapon_type or "").lower()
    wtokens = {_norm_tok(x) for x in _tokset(wt)}
    absorb  = {_norm_tok(x) for x in absorb}
    imm     = {_norm_tok(x) for x in imm}
    res     = {_norm_tok(x) for x in res}
    weak    = {_norm_tok(x) for x in weak}

    if is_magical is None:
        is_magical = wt.startswith("mag") or ("magical" in wt)
    if not is_magical:
        if wt in {"force", "holy", "fire", "cold", "electric", "magical"}:
            is_magical = True

    try:

        cand_name = (target_name
                     or get_compat(t_cfg, "info", "name", fallback="")
                     or "")
        bcfg = _load_battles()
        if bcfg:
            sections = [chan_id] if chan_id and bcfg.has_section(chan_id) else bcfg.sections()
            for sec in sections:
                try:
                    names, _ = _parse_combatants(bcfg, sec)
                    key = _find_ci_name(names, cand_name) or cand_name
                    if key in names:
                        s = _slot(key) if '_slot' in globals() else key.replace(" ", "_")
                        inw_left = bcfg.getint(sec, f"{s}.inw", fallback=0)
                        if inw_left > 0:
                            if (not is_magical) and (weapon_name not in {"Oil", "Holy Water"}):
                                return 0, "immune (nonmagical)"
                        break
                except Exception:
                    continue
    except Exception:
        pass

    fos = getint_compat(t_cfg, "base", "fossilized", fallback=0) or getint_compat(t_cfg, "stats", "fossilized", fallback=0)
    if fos:
        wn = (weapon_name or "").lower()

        if (("arrow" in wn) or ("bolt" in wn) or ("bullet" in wn) or wn in {"shortbow","longbow","sling","lightxbow","heavyxbow"}) and not is_magical:
            return 0, "immune (normal missiles)"

        if "slashing" in wtokens:
            return (1 if raw > 0 else 0), "fossilized bones (slashing → 1)"

        PHYS = {"slashing","piercing","bludgeoning"}
        if wtokens.intersection(PHYS):
            return math.floor(raw / 2), "resists weapons"

    match_abs = absorb.intersection(wtokens)
    if match_abs and is_magical:
        key = next(iter(match_abs))
        heal = max(0, raw // 3)
        if heal > 0:
            return -heal, f"absorbs {key} (magical) → heals {heal}"
        else:
            return 0, f"absorbs {key} (magical)"

    if "nonmagical" in imm and not is_magical and weapon_name not in {"Oil", "Holy Water"}:
        return 0, "immune (nonmagical)"

    match_imm = imm.intersection(wtokens)
    if match_imm:
        key = next(iter(match_imm))
        return 0, f"immune ({key})"

    match_res = res.intersection(wtokens)
    if match_res:
        key = next(iter(match_res))
        return math.floor(raw/2), f"resists {key}"

    match_weak = weak.intersection(wtokens)
    if match_weak:
        key = next(iter(match_weak))
        return raw * 2, f"weak to {key}"

    return raw, ""

def _parse_init_bonus_from_tpl(tpl) -> int:
    """
    Supports 'init' or 'initiative' keys with values like '+2', '2', or '-1'.
    Returns 0 if missing/invalid.
    """
    raw = str(tpl.get("init", "") or tpl.get("initiative", "")).strip()
    if not raw:
        return 0
    m = re.match(r"^[+\-]?\d+$", raw)
    if not m:
        return 0
    try:
        return int(raw)
    except Exception:
        return 0

def _parse_skills_from_tpl(tpl) -> str:
    """
    Accepts 'skills' (and a couple synonyms) as a space- or comma-separated list.
    Returns a normalized 'Name Name2 Name3' string (space-separated).
    """
    val = (
        str(tpl.get("skills", "") or tpl.get("skill", "") or tpl.get("abilities", "") or tpl.get("traits", "")).strip()
    )
    if not val:
        return ""
    toks = [t for t in re.split(r"[,\s]+", val) if t]
    return " ".join(toks)

def _find_spell_count_in_cfg(cfg, spell_key: str, prefer_section: str | None = None) -> int | None:
    """
    Look for the integer uses for a spell key (already normalized).
    Try the preferred section first, then scan all sections.
    """
    if prefer_section:
        try:
            v = getint_compat(cfg, prefer_section, spell_key, fallback=None)
            if v is not None and int(v) > 0:
                return int(v)
        except Exception:
            pass
    try:
        for sec in cfg.sections():
            try:
                v = getint_compat(cfg, sec, spell_key, fallback=None)
                if v is not None and int(v) > 0:
                    return int(v)
            except Exception:
                pass
    except Exception:
        pass
    return None

def _is_undead_cfg(cfg, pretty=""):
    try:
        cls = (get_compat(cfg, "info", "class", fallback="") or "").strip().lower()
        if cls == "monster":
            t = (get_compat(cfg, "monster", "type", fallback="") or "").strip().lower()
            if "undead" in t:
                return True
        race = (get_compat(cfg, "info", "race", fallback="") or "").strip().lower()
        if "undead" in race:
            return True
    except Exception:
        pass
    return False

def _parse_spells_from_cfg_any(cfg) -> list[tuple[str, int]]:
    """
    Extract [(SpellName, uses)] from any config.
    Strategy:
      • Find a section that has 'spells = ...'
      • Split the list, then for each spell read a lower-case key for its uses.
      • If no count key is present, default to 1.
    """
    sec = _get_first_spells_section(cfg)
    if not sec:
        return []

    spells_raw = (get_compat(cfg, sec, "spells", fallback="") or "").strip()
    if not spells_raw:
        return []

    names = [nm for nm in re.split(r"[,\s]+", spells_raw) if nm]
    out: list[tuple[str, int]] = []
    for disp in names:
        key = _norm_spell_key(disp)
        uses = _find_spell_count_in_cfg(cfg, key, prefer_section=sec)
        out.append((disp, (uses if uses is not None else 1)))
    return out

def _ci_find_file(basename_with_ext: str) -> str | None:
    """Case-insensitive file find in CWD."""
    want = basename_with_ext.lower()
    for fn in os.listdir("."):
        if fn.lower() == want:
            return fn
    return None

def _ci_find_monster_ini(monster_type: str) -> str | None:
    """Case-insensitive find in MONSTER_DIR."""
    if not os.path.isdir(MONSTER_DIR):
        return None
    want = f"{monster_type}.ini".lower()
    for fn in os.listdir(MONSTER_DIR):
        if fn.lower() == want:
            return os.path.join(MONSTER_DIR, fn)
    return None

def _monster_source_files(name: str) -> tuple[str | None, str | None, str | None]:
    """
    Returns (coe_path, base_ini_path, monster_type)
    - coe_path: the instance file if present
    - base_ini_path: monsters/<monster_type>.ini if resolvable
    - monster_type: string name if resolvable
    """

    coe = _ci_find_file(f"{name.replace(' ', '_')}.coe")
    mtype = None
    if coe:
        try:
            cfg = read_cfg(coe)
            mtype = (get_compat(cfg, "info", "monster_type", fallback="") or "").strip()
        except Exception:
            mtype = None

    guess_type = (mtype or name or "").replace(" ", "").lower()
    base_ini = _ci_find_monster_ini(guess_type) if guess_type else None
    return coe, base_ini, (mtype or guess_type or None)

def _parse_attacks_from_cfg_any(cfg, section: str, prefix: str = "") -> list[tuple[str, str]]:
    """
    Extract (attack_name, dice) pairs from a config.
    Supports:
      - [stats] attacknames = bite claw ; and atk_bite = 2d4
      - [base]  attacknames = bite claw ; and bite = 2d4
    'prefix' lets you pass 'atk_' for .coe [stats] style.
    """
    names_raw = (get_compat(cfg, section, "attacknames", fallback="") or "").strip()
    if not names_raw:
        return []

    names = re.split(r"[,\s]+", names_raw.strip())
    pairs = []
    for nm in names:
        if not nm:
            continue

        dice = (get_compat(cfg, section, nm, fallback="") or "").strip()
        if not dice:
            dice = (get_compat(cfg, section, f"{prefix}{nm}", fallback="") or "").strip()

        pairs.append((nm, dice if dice else "—"))
    return pairs

def _monster_profile(name: str) -> dict:
    """
    Returns:
      {
        'display': str,
        'attacks_per_turn': int|None,
        'attacks': [(name,dice)],
        'special': str|None,
        'ac': int|None,
        'move': int|None,
        'source': str,
        'monster_type': str,
        'spells': [(name, uses)]
      }
    """
    coe, base_ini, mtype = _monster_source_files(name)
    out = {
        "display": name,
        "attacks_per_turn": None,
        "attacks": [],
        "special": None,
        "ac": None,
        "move": None,
        "source": "",
        "monster_type": mtype or "",
        "spells": []
    }

    if coe:
        try:
            icfg = read_cfg(coe)
            inst_pairs = _parse_attacks_from_cfg_any(icfg, "stats", prefix="atk_")
            if inst_pairs:
                out["attacks"] = inst_pairs or out["attacks"]

            inst_spells = _parse_spells_from_cfg_any(icfg)
            if inst_spells:
                out["spells"] = inst_spells
            out["source"] += os.path.basename(coe)
        except Exception:
            pass

    if base_ini:
        try:
            mcfg = read_cfg(base_ini)
            if out["attacks_per_turn"] is None:
                out["attacks_per_turn"] = getint_compat(mcfg, "base", "attacks", fallback=None)
            base_pairs = _parse_attacks_from_cfg_any(mcfg, "base", prefix="")
            if not out["attacks"]:
                out["attacks"] = base_pairs or []
            sp = (get_compat(mcfg, "base", "special", fallback="") or "").strip()
            out["special"] = sp if sp else None
            out["ac"] = getint_compat(mcfg, "base", "ac", fallback=None)
            out["move"] = getint_compat(mcfg, "base", "move", fallback=None)

            if not out["spells"]:
                base_spells = _parse_spells_from_cfg_any(mcfg)
                if base_spells:
                    out["spells"] = base_spells
            out["source"] += (" | " if out["source"] else "") + os.path.basename(base_ini)
        except Exception:
            pass

    if out["attacks_per_turn"] is None:
        out["attacks_per_turn"] = max(1, len(out["attacks"])) if out["attacks"] else None

    return out

async def _maybe_dm_monster_attacks(self, ctx, cfg, chan_id: str, who: str):
    """
    If enabled and 'who' is a monster, DM the DM an embed with
    # attacks/turn, attacks & dice, and specials (if any).
    """
    try:
        if cfg.getint(chan_id, "attacks_hint", fallback=1) != 1:
            return
    except Exception:

        pass

    if not _is_monster(who):
        return

    async def _get_user_by_id(uid: str):
        if not uid or not str(uid).isdigit():
            return None
        u = ctx.bot.get_user(int(uid))
        if u is None:
            try:
                u = await ctx.bot.fetch_user(int(uid))
            except Exception:
                return None
        return u

    user = None
    try:
        slot = _choose_slot_for_effects(cfg, chan_id, who)
        summoner_name = cfg.get(chan_id, f"{slot}.minion_by", fallback="").strip()
        if summoner_name:
            disp, path = _resolve_char_ci_local(summoner_name)
            if path:
                pcfg = read_cfg(path)
                owner_id = (get_compat(pcfg, "info", "owner_id", fallback="") or "").strip()
                if not owner_id:
                    legacy = (get_compat(pcfg, "info", "owner", fallback="") or "")
                    m = re.search(r"(\d{15,25})", legacy)
                    owner_id = m.group(1) if m else ""
                user = await _get_user_by_id(owner_id)
    except Exception:
        pass

    if user is None:
        try:
            coe, _base_ini, _mtype = _monster_source_files(who)
            if coe:
                icfg = read_cfg(coe)
                owner_id = (get_compat(icfg, "info", "owner_id", fallback="") or "").strip()
                if not owner_id:
                    legacy = (get_compat(icfg, "info", "owner", fallback="") or "")
                    m = re.search(r"(\d{15,25})", legacy)
                    owner_id = m.group(1) if m else ""
                user = await _get_user_by_id(owner_id)
        except Exception:
            pass

    if user is None:
        dm_id = cfg.get(chan_id, "DM", fallback="")
        user = await _get_user_by_id(str(dm_id))
        if user is None:
            return

    prof = _monster_profile(who)

    atk_lines = []
    for nm, dice in (prof["attacks"] or []):
        atk_lines.append(f"• **{nm}** — `{dice}`")

    spl_lines = []
    for sname, uses in (prof.get("spells") or []):
        spl_lines.append(f"• **{sname}** ×{uses}")

    special_txt = prof["special"]
    footer_hint = "Use `!remindattacks off` to disable."
    if special_txt:

        if re.search(r"\boil\b", special_txt, flags=re.I):
            special_txt += "\n*(Try `!slam <target> oil` to use the beetle’s oil.)*"

    title = f"Monster Turn: {who}"
    embed = nextcord.Embed(title=title, color=random.randint(0, 0xFFFFFF))
    if prof["attacks_per_turn"] is not None:
        embed.add_field(name="Attacks per Turn", value=str(prof["attacks_per_turn"]), inline=True)
    if prof["ac"] is not None:
        embed.add_field(name="AC", value=str(prof["ac"]), inline=True)
    if prof["move"] is not None:
        embed.add_field(name="Move", value=str(prof["move"]), inline=True)

    if atk_lines:
        embed.add_field(name="Attacks", value="\n".join(atk_lines), inline=False)
    else:
        embed.add_field(name="Attacks", value="_No named attacks found._", inline=False)

    if spl_lines:

        text = "\n".join(spl_lines)
        if len(text) > 1000:
            text = text[:990].rstrip() + "\n…"
        embed.add_field(name="Spells (per day)", value=text, inline=False)

    if special_txt:
        embed.add_field(name="Special", value=special_txt, inline=False)

    if prof["source"]:
        embed.set_footer(text=f"Source: {prof['source']} • {footer_hint}")
    else:
        embed.set_footer(text=footer_hint)

    try:
        await user.send(embed=embed)

    except Exception:

        pass

def _ss_state(bcfg, chan, slot):
    gi = lambda k: bcfg.getint(chan, k, fallback=0)
    pool = max(
        gi(f"{slot}.stonehp"),
        gi(f"{slot}.stone_pool"),
        gi(f"{slot}.x_stonehp"),
        gi(f"{slot}.x_stone_pool"),
    )
    dur = max(
        gi(f"{slot}.stone"),
        gi(f"{slot}.stonerounds"),
        gi(f"{slot}.stonedur"),
        gi(f"{slot}.x_stone"),
        gi(f"{slot}.x_stoneskin"),
    )
    return dur, pool

def _ss_normalize(bcfg, chan, slot):
    dur, pool = _ss_state(bcfg, chan, slot)
    if pool <= 0:
        nukes = (
            f"{slot}.stone", f"{slot}.stonerounds", f"{slot}.stonedur",
            f"{slot}.stonehp", f"{slot}.stone_pool",
            f"{slot}.x_stone", f"{slot}.x_stonehp", f"{slot}.x_stone_pool",
            f"{slot}.x_stoneskin",
        )
        for opt in nukes:
            if bcfg.has_option(chan, opt):
                bcfg.remove_option(chan, opt)
        for base in (f"{slot}.x_stone", f"{slot}.x_stoneskin"):
            for suf in ("", "_code", "_label", "_emoji", "_by"):
                opt = f"{base}{suf}"
                if bcfg.has_option(chan, opt):
                    bcfg.remove_option(chan, opt)


BATTLE_FILE = "battle.lst"

def normalize_name(s) -> str:
    if not isinstance(s, str):
        return ""
    return "".join(ch.lower() for ch in s if ch.isalnum())

LAIR_TYPES = set("ABCDEFGHIJKLMNO")
INDIVIDUAL_TYPES = set("PQRSTUV")

def _inc_cfg_int(cfg, chan_id: str, key: str, delta: int):
    """Increment an int option in the battle config (never below 0)."""
    try:
        cur = cfg.getint(chan_id, key, fallback=0)
    except Exception:
        cur = 0
    try:
        d = int(delta)
    except Exception:
        d = 0
    cfg.set(chan_id, key, str(max(0, cur + d)))

def _ensure_section(cfg, chan_id):
    if not cfg.has_section(chan_id):
        cfg.add_section(chan_id)
        cfg.set(chan_id, "list", "")
        cfg.set(chan_id, "message_id", "0")
        cfg.set(chan_id, "round", "0")
        cfg.set(chan_id, "turn", "")
        cfg.set(chan_id, "turn_e", "")
        cfg.set(chan_id, "DM", "")
        cfg.set(chan_id, "join_seq", "0")
        cfg.set(chan_id, "etime_rounds", "0")

def _hmcm_badges(cfg, chan_id: str, slot: str, actor_name: str) -> str:
    """
    Return HM/CM badges for a given slot.
    Shows 'current stage' for the actor whose turn it is (stage-1, clamped to 1),
    and the stored 'next stage' for everyone else.
    """
    turn_name = cfg.get(chan_id, "turn", fallback="")
    out = ""
    for key, tag in (("x_heatmetal", "HM"), ("x_chillmetal", "CM")):
        try:
            stg = cfg.getint(chan_id, f"{slot}.{key}", fallback=0)
        except Exception:
            stg = 0
        if stg > 0:
            show = max(1, stg - 1) if actor_name == turn_name else stg
            out += f" • [{tag} {show}/7]"
    return out

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



def _apply_mitigation(raw, weapon_name="", weapon_type="", t_cfg=None, is_magical=None,
                      chan_id=None, target_name=None):
    """
    Apply target-based damage mitigation and return (final_damage, note).

    Inputs
    - raw (int): incoming damage before mitigation
    - weapon_name (str): display name like "Longsword", "Oil", "Holy Water"
    - weapon_type (str): tokens describing damage type(s): e.g. "slashing", "fire", "holy"
                         multiple tokens allowed, separated by space/comma
    - t_cfg: config for the target (expects helpers like get_compat/read_cfg to exist)
    - is_magical (bool|None): if None, inferred from weapon_type tokens
    - chan_id (str|None): battle channel id for initiative-based INW rule
    - target_name (str|None): target display name for INW lookup

    Returns
    - (int, str): (final damage after mitigation, short reason string)
    """

    def _tokset(s):
                                            
        return {x.strip().lower() for x in re.split(r"[,\s]+", str(s or "")) if x.strip()}

    def _merge_keys(cfg, section, *keys):
        out = set()
        for k in keys:
            out |= _tokset(get_compat(cfg, section, k, fallback=""))
        return out

    def _norm_tok(t):
        t = (t or "").lower()
        if t in {"elec", "electricity", "lightning"}: return "electric"
        if t in {"acidic"}: return "acid"
        return t

    PHYS = {"slashing", "piercing", "bludgeoning"}
    MAGICAL_TYPE_HINTS = {"force", "holy", "electric", "magical"}

    imm   = _merge_keys(t_cfg, "base", "immune","immunity","immune_types")   | _merge_keys(t_cfg,"stats","immune","immunity","immune_types")
    res   = _merge_keys(t_cfg, "base", "resist","resistance","resist_types") | _merge_keys(t_cfg,"stats","resist","resistance","resist_types")
    weak  = (_merge_keys(t_cfg,"base","weak","weakness","weak_types","vulnerable","vulnerability","vuln")
          |  _merge_keys(t_cfg,"stats","weak","weakness","weak_types","vulnerable","vulnerability","vuln")
          |  _merge_keys(t_cfg,"info","weak","weakness","weak_types","vulnerable","vulnerability","vuln"))
    absorb = (_merge_keys(t_cfg,"base","absorb","absorb_types")
           |  _merge_keys(t_cfg,"stats","absorb","absorb_types")
           |  _merge_keys(t_cfg,"info","absorb","absorb_types"))

    reduce1 = (_merge_keys(t_cfg, "base", "reduce1", "reduce", "reduce_types")
            |  _merge_keys(t_cfg, "stats","reduce1", "reduce", "reduce_types")
            |  _merge_keys(t_cfg, "info", "reduce1", "reduce", "reduce_types"))

    try:
        mtype = (get_compat(t_cfg, "info", "monster_type", fallback="")
                 or get_compat(t_cfg, "info", "type", fallback="")).strip().lower()
        if mtype:
            for cand in (f"{mtype}.ini",
                         os.path.join("mon", f"{mtype}.ini"),
                         os.path.join("monsters", f"{mtype}.ini")):
                if os.path.exists(cand):
                    base_cfg = read_cfg(cand)
                    imm   |= _merge_keys(base_cfg,"base","immune","immunity","immune_types")   | _merge_keys(base_cfg,"stats","immune","immunity","immune_types")
                    res   |= _merge_keys(base_cfg,"base","resist","resistance","resist_types") | _merge_keys(base_cfg,"stats","resist","resistance","resist_types")
                    weak  |= (_merge_keys(base_cfg,"base","weak","weakness","weak_types","vulnerable","vulnerability","vuln")
                           |  _merge_keys(base_cfg,"stats","weak","weakness","weak_types","vulnerable","vulnerability","vuln")
                           |  _merge_keys(base_cfg,"info","weak","weakness","weak_types","vulnerable","vulnerability","vuln"))
                    absorb |= (_merge_keys(base_cfg,"base","absorb","absorb_types")
                            |   _merge_keys(base_cfg,"stats","absorb","absorb_types")
                            |   _merge_keys(base_cfg,"info","absorb","absorb_types"))
                    break
    except Exception:
        pass

    wtokens = {_norm_tok(x) for x in _tokset(weapon_type)}
    absorb  = {_norm_tok(x) for x in absorb}
    imm     = {_norm_tok(x) for x in imm}
    res     = {_norm_tok(x) for x in res}
    weak    = {_norm_tok(x) for x in weak}
    reduce1 = {_norm_tok(x) for x in reduce1}

    if is_magical is None:
        wt = (weapon_type or "").lower()
        is_magical = wt.startswith("mag") or ("magical" in wt)
    if not is_magical:
                                                                                            
        if wtokens & MAGICAL_TYPE_HINTS:
            is_magical = True


    def _has_eq_resist(cfg, elem: str) -> bool:
        try:
            if cfg.has_section("eq"):
                for _k, _v in cfg.items("eq"):
                    nm = (str(_v) or "").lower().replace(" ", "")
                    if elem == "fire" and "fireresistance" in nm:
                        return True
                    if elem == "cold" and "coldresistance" in nm:
                        return True
        except Exception:
            pass
        return False

    def _has_timer_resist(elem: str) -> bool:
        try:
            if not (chan_id and target_name):
                return False
            bcfg = _load_battles()
            if not bcfg or not bcfg.has_section(chan_id):
                return False
            names, _ = _parse_combatants(bcfg, chan_id)
            key = _find_ci_name(names, target_name) or target_name
            try:
                s = _slot(key)
            except Exception:
                s = key.replace(" ", "_")
            for suff in (f"{s}.x_{elem}resistance", f"{s}.x_resist{elem}"):
                try:
                    if bcfg.getint(chan_id, suff, fallback=0) > 0:
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False


    _virt_res = set()
    if "fire" in wtokens and (_has_eq_resist(t_cfg, "fire") or _has_timer_resist("fire")):
        if not is_magical:
            return 0, "immune (normal fire)"
        _virt_res.add("fire")
    if "cold" in wtokens and (_has_eq_resist(t_cfg, "cold") or _has_timer_resist("cold")):
        if not is_magical:
            return 0, "immune (normal cold)"
        _virt_res.add("cold")


    if _virt_res:
        res |= _virt_res

    


    try:
        if chan_id and target_name:
            bcfg = _load_battles()
        else:
            bcfg = None
    except Exception:
        bcfg = None

    def _slot_for_target(bcfg, sec, name):
        try:
            names, _ = _parse_combatants(bcfg, sec)
            key = _find_ci_name(names, name) or name
            return _slot(key) if '_slot' in globals() else key.replace(" ", "_")
        except Exception:
            return (name or "").replace(" ", "_")

    if bcfg:
        secs = [chan_id] if bcfg.has_section(chan_id or "") else []
    else:
        secs = []

    for sec in secs:
        s = _slot_for_target(bcfg, sec, target_name or "")
        if "fire" in wtokens:
            if not is_magical:
                return 0, "immune (normal fire)"
            pool = bcfg.getint(sec, f"{s}.pff_pool", fallback=0)
            if pool > 0:
                left = max(0, pool - int(raw))
                bcfg.set(sec, f"{s}.pff_pool", str(left))
                if left <= 0:
                    for k in (f"{s}.pff_pool", f"{s}.pff_self"):
                        if bcfg.has_option(sec, k):
                            bcfg.remove_option(sec, k)
                _save_battles(bcfg)
                return 0, f"Protection from Fire absorbs ({left} left)"
        if ("electric" in wtokens) or ("lightning" in wtokens):
            if not is_magical:
                return 0, "immune (normal lightning)"
            pool = bcfg.getint(sec, f"{s}.pfl_pool", fallback=0)
            if pool > 0:
                left = max(0, pool - int(raw))
                bcfg.set(sec, f"{s}.pfl_pool", str(left))
                if left <= 0:
                    for k in (f"{s}.pfl_pool", f"{s}.pfl_self"):
                        if bcfg.has_option(sec, k):
                            bcfg.remove_option(sec, k)
                _save_battles(bcfg)
                return 0, f"Protection from Lightning absorbs ({left} left)"
                                                                                         
                                    
    try:
        cand_name = (target_name
                     or get_compat(t_cfg, "info", "name", fallback="")
                     or "")
        bcfg = _load_battles()
        if bcfg:
            sections = [chan_id] if chan_id and bcfg.has_section(chan_id) else bcfg.sections()
            for sec in sections:
                try:
                    names, _ = _parse_combatants(bcfg, sec)
                    key = _find_ci_name(names, cand_name) or cand_name
                    if key in names:
                        s = _slot(key) if '_slot' in globals() else key.replace(" ", "_")
                        inw_left = bcfg.getint(sec, f"{s}.inw", fallback=0)
                        if inw_left > 0:
                                                                      
                            wname = (weapon_name or "").lower()
                            if (not is_magical) and (wname not in {"oil", "holy water"}):
                                return 0, "immune (nonmagical)"
                        break
                except Exception:
                    continue
    except Exception:
        pass

    fos = getint_compat(t_cfg, "base", "fossilized", fallback=0) or getint_compat(t_cfg, "stats", "fossilized", fallback=0)
    if fos:
        wn = (weapon_name or "").lower()
                                                                         
        if (("arrow" in wn) or ("bolt" in wn) or ("bullet" in wn)
            or wn in {"shortbow", "longbow", "sling", "lightxbow", "heavyxbow"}) and not is_magical:
            return 0, "immune (normal missiles)"

                                                                       
        if "slashing" in wtokens:
            return (1 if raw > 0 else 0), "fossilized bones (slashing → 1)"

                                                                                
        if wtokens & PHYS:
            return math.floor(raw / 2), "resists weapons"

                                                                      
    match_abs = absorb & wtokens
    if match_abs and is_magical:
        key = next(iter(match_abs))
        heal = max(0, raw // 3)
        if heal > 0:
            return -heal, f"absorbs {key} (magical) → heals {heal}"
        else:
            return 0, f"absorbs {key} (magical)"

    if "nonmagical" in imm and not is_magical:
        wname = (weapon_name or "").lower()
        if wname not in {"oil", "holy water"}:
            return 0, "immune (nonmagical)"

    match_imm = imm & wtokens
    if match_imm:
        key = next(iter(match_imm))
        return 0, f"immune ({key})"

    if reduce1:
                                              
        is_weaponish = bool(wtokens & PHYS)

                                                                                 
        wname_raw  = (weapon_name or "").lower().strip()
        wname_base = re.sub(r"\s*\+.*$", "", wname_raw)                                     
        wname_norm = re.sub(r"[^a-z0-9]+", "", wname_base)                              
        reduce1_norm = {re.sub(r"[^a-z0-9]+","", x) for x in reduce1}

        is_bow   = ("bow" in wname_raw) and ("xbow" not in wname_raw and "crossbow" not in wname_raw)
        is_xbow  = ("xbow" in wname_raw) or ("crossbow" in wname_raw)
        is_sling = ("sling" in wname_raw)

        name_match = (
            (wname_norm in reduce1_norm) or
            ("bow" in reduce1 and is_bow) or
            (("xbow" in reduce1 or "crossbow" in reduce1) and is_xbow) or
            ("sling" in reduce1 and is_sling)
        )

        match_reduce = (
            ("any" in reduce1) or ("all" in reduce1) or
            (("weapon" in reduce1) or ("physical" in reduce1)) and is_weaponish or
            ("nonmagical" in reduce1 and not is_magical) or
            ("magical" in reduce1 and is_magical) or
            bool(reduce1 & wtokens) or
            name_match                                                              
        )

        if match_reduce:
            return (1 if raw > 0 else 0), "reduced to 1"


    match_res = res & wtokens
    if match_res:
        key = next(iter(match_res))
        return math.floor(raw / 2), f"resists {key}"

    match_weak = weak & wtokens
    if match_weak:
        key = next(iter(match_weak))
        return raw * 2, f"weak to {key}"

    return raw, ""


def _get_map(cfg, chan_id, key: str) -> dict:
    raw = cfg.get(chan_id, key, fallback="{}")
    try:
        return json.loads(raw) or {}
    except Exception:
        return {}
def _roll_one_lair_preview(code: str) -> list[str]:
    """
    Non-mutating: roll one lair hoard of the given type and return display lines.
    Uses LAIR_TABLE (A..O); falls back to _LAIR (A..I) if present there.
    """
    code = (code or "").strip().upper()
    if code not in LAIR_TABLE and code not in _LAIR:
        return [f"❌ Lair type **{code}** is not implemented."]

    r = _roll_lair_once(code)
    lines = [f"🏴 Lair **{code}** (preview):"]

    for cur in ("cp","sp","ep","gp","pp"):
        if r.get(cur, 0):
            lines.append(f"• {cur.upper()}: +{r[cur]}")
    if r.get("gems"):       lines.append(f"• Gems: +{r['gems']}")
    if r.get("jewelry"):    lines.append(f"• Jewelry: +{r['jewelry']}")
    if r.get("magic_any"):  lines.append(f"• Magic (any): +{r['magic_any']}")
    if r.get("magic_wa"):   lines.append(f"• Magic (weapon/armor): +{r['magic_wa']}")
    if r.get("magic_xw"):   lines.append(f"• Magic (except weapons): +{r['magic_xw']}")
    if r.get("potions"):    lines.append(f"• Potions: +{r['potions']}")
    if r.get("scrolls"):    lines.append(f"• Scrolls: +{r['scrolls']}")

    return lines if len(lines) > 1 else [f"🏴 Lair **{code}** (preview): nothing this time."]

def _choose_slot_for_effects(cfg, chan_id, name):
    cands = []
    try:
        cands.append(_slot(name))
    except Exception:
        pass
    cands.append(name.replace(" ", "_"))

    base = cands[0]
    disp = cfg.get(chan_id, f"{base}.disp", fallback="")
    if disp and disp != name:
        cands.append(disp.replace(" ", "_"))

    prefer_prefixes = ("paralyzed","blind","cc","ck","light","darkness","mi",
                       "magwep","ghh","sph","shl","hyp","cl","cn","db","inw","pnm","x_", "pet",
                       "cc_blind_pending","cs_blind_pending","sp_blind_pending","sp_cnf_pending",
                       "x_heatmetal","x_chillmetal")
    section_items = list(cfg.items(chan_id))
    for s in cands:
        for opt, _ in section_items:
            if not opt.startswith(f"{s}."):
                continue
            suf = opt.split(".", 1)[1]
            if any(suf.startswith(p) for p in prefer_prefixes):
                return s
    return cands[0]

def _cleanup_bad_disease_disp(bcfg, chan_id):
    try:
        names, _ = _parse_combatants(bcfg, chan_id)
        changed = []
        for nm in names or []:
            try:
                slot = _slot(nm) if '_slot' in globals() else nm.replace(" ", "_")
                opt = f"{slot}.disp"
                if bcfg.has_option(chan_id, opt) and bcfg.get(chan_id, opt).strip().upper() == "DIS":
                    bcfg.remove_option(chan_id, opt)
                    changed.append(nm)
            except Exception:
                continue
        if changed:
            _save_battles(bcfg)
        return changed
    except Exception:
        return []

def _norm_type_token(t: str | None) -> str:
    t = (t or "").strip().lower()
    if t in {"elec","electricity","lightning"}: return "electric"
    if t in {"acidic"}: return "acid"
    return t

def _collect_absorb_types(t_cfg) -> set[str]:
    def _tokset(s):
        return {x.strip().lower() for x in re.split(r"[,\s]+", str(s or "")) if x.strip()}

    def _merge_keys(cfg, section, *keys):
        out = set()
        for k in keys:
            out |= _tokset(get_compat(cfg, section, k, fallback=""))
        return out

    absorb = (_merge_keys(t_cfg, "base","absorb","absorb_types")
           |  _merge_keys(t_cfg, "stats","absorb","absorb_types")
           |  _merge_keys(t_cfg, "info","absorb","absorb_types"))

    try:
        mtype = (get_compat(t_cfg, "info", "monster_type", fallback="")
                 or get_compat(t_cfg, "info", "type", fallback="")).strip().lower()
        if mtype:
            for cand in (f"{mtype}.ini", os.path.join("mon", f"{mtype}.ini"), os.path.join("monsters", f"{mtype}.ini")):
                if os.path.exists(cand):
                    base_cfg = read_cfg(cand)
                    absorb |= (_merge_keys(base_cfg,"base","absorb","absorb_types")
                            |   _merge_keys(base_cfg,"stats","absorb","absorb_types")
                            |   _merge_keys(base_cfg,"info","absorb","absorb_types"))
                    break
    except Exception:
        pass

    return {_norm_type_token(x) for x in absorb if x}

def _set_map(cfg, chan_id, key: str, data: dict) -> None:
    cfg.set(chan_id, key, json.dumps(data))

def _inc(cfg, chan_id, key: str, by: int = 1) -> None:
    val = cfg.getint(chan_id, key, fallback=0)
    cfg.set(chan_id, key, str(max(0, val + by)))

def _add_coins(cfg, chan_id, *, cp=0, sp=0, ep=0, gp=0, pp=0):
    if cp: _inc(cfg, chan_id, "tre_cp", cp); _inc(cfg, chan_id, "new_cp", cp)
    if sp: _inc(cfg, chan_id, "tre_sp", sp); _inc(cfg, chan_id, "new_sp", sp)
    if ep: _inc(cfg, chan_id, "tre_ep", ep); _inc(cfg, chan_id, "new_ep", ep)
    if gp: _inc(cfg, chan_id, "tre_gp", gp); _inc(cfg, chan_id, "new_gp", gp)
    if pp: _inc(cfg, chan_id, "tre_pp", pp); _inc(cfg, chan_id, "new_pp", pp)

def _add_misc(cfg, chan_id, *, gems=0, jewelry=0, magic_any=0, magic_wa=0, magic_xw=0, potions=0, scrolls=0):
    if gems:      _inc(cfg, chan_id, "tre_gems", gems);       _inc(cfg, chan_id, "new_gems", gems)
    if jewelry:   _inc(cfg, chan_id, "tre_jewelry", jewelry);  _inc(cfg, chan_id, "new_jewelry", jewelry)
    if magic_any: _inc(cfg, chan_id, "tre_magic_any", magic_any); _inc(cfg, chan_id, "new_magic_any", magic_any)
    if magic_wa:  _inc(cfg, chan_id, "tre_magic_wa", magic_wa);   _inc(cfg, chan_id, "new_magic_wa", magic_wa)
    if magic_xw:  _inc(cfg, chan_id, "tre_magic_xw", magic_xw);   _inc(cfg, chan_id, "new_magic_xw", magic_xw)
    if potions:   _inc(cfg, chan_id, "tre_potions", potions);     _inc(cfg, chan_id, "new_potions", potions)
    if scrolls:   _inc(cfg, chan_id, "tre_scrolls", scrolls);     _inc(cfg, chan_id, "new_scrolls", scrolls)

def _clear_x_effect(cfg, chan_id: str, slot: str, base_key: str):
    """Remove a custom x_* effect key and its metadata (label/emoji/code/by)."""
    key = f"{slot}.{base_key}"
    if cfg.has_option(chan_id, key):
        cfg.remove_option(chan_id, key)
    for suf in ("_label","_emoji","_code","_by"):
        mkey = f"{slot}.{base_key}{suf}"
        if cfg.has_option(chan_id, mkey):
            cfg.remove_option(chan_id, mkey)

def _advance_exploration_x_effects(cfg, chan_id: str, rounds: int = 60) -> dict[str, list[str]]:
    """
    Decrement custom x_* effects by `rounds`. When any hits 0 or below, remove the key and metadata.
    Returns { name: [expired labels...] } for a pretty 'Expired effects' line.
    """
    expired: dict[str, list[str]] = {}
    names, _ = _parse_combatants(cfg, chan_id)
    for name in names:
        slot = _choose_slot_for_effects(cfg, chan_id, name)

        for opt_key, _ in list(cfg.items(chan_id)):
            if not opt_key.startswith(f"{slot}.x_"):
                continue
            base_key = opt_key.split(".", 1)[1]
            if any(base_key.endswith(suf) for suf in ("_label","_emoji","_code","_by")):
                continue
            left = cfg.getint(chan_id, opt_key, fallback=0)
            if left <= 0:
                continue
            new_left = max(0, left - rounds)
            if new_left <= 0:

                label = cfg.get(chan_id, f"{slot}.{base_key}_label",
                                fallback=base_key[2:].replace("_", " ").title())
                expired.setdefault(name, []).append(label)
                _clear_x_effect(cfg, chan_id, slot, base_key)
            else:
                cfg.set(chan_id, opt_key, str(new_left))
    _save_battles(cfg)
    return expired

def _perm_codes_for_slot(cfg, chan_id: str, slot: str) -> set[str]:
    codes = set()
    for key, code in (
        ("x_detectmagic_perm",        "DM"),
        ("x_protectionfromevil_perm", "PF"),
        ("x_readlanguages_perm",      "RL"),
        ("x_readmagic_perm",          "RM"),
        ("x_detectinvisible_perm",    "DI"),
        ("x_fly_perm",                "FL"),
    ):
        try:
            if cfg.getint(chan_id, f"{slot}.{key}", fallback=0) > 0:
                codes.add(code)
        except Exception:
            pass
    return codes

def _d(n): return random.randint(1, n)
def _d100(): return random.randint(1, 100)

def _dice_sum(spec: str) -> int:
    """
    Supports: 'XdY', 'XdYxK' or 'KxXdY' (multipliers), and plain ints.
    Examples: 3d6, 4d6x10, 10x3d8, 250
    """
    s = str(spec).strip().lower()
    if not s:
        return 0

    m = re.fullmatch(r"(?:(\d+)x)?\s*(\d+)d(\d+)\s*(?:x(\d+))?", s)
    if m:
        left_mul  = int(m.group(1) or 1)
        n         = int(m.group(2))
        d         = int(m.group(3))
        right_mul = int(m.group(4) or 1)
        return left_mul * sum(random.randint(1, d) for _ in range(n)) * right_mul

    try:
        return int(s)
    except Exception:
        return 0

def _move_rates_for_char(char_name: str) -> tuple[int, int]:
    """
    Return (base_move, turn_move) for a character name.
    Falls back to 30' base if not found. Turn move = base * 3.
    """
    path = f"{char_name.replace(' ', '_')}.coe"
    base = 30
    if os.path.exists(path):
        cfg = read_cfg(path)

        try:
            base = getint_compat(cfg, "stats", "move", fallback=None)
            if base is None:
                base = getint_compat(cfg, "base", "move", fallback=30)
        except Exception:
            base = 30
    return base, base * 3

def _2d8() -> int:    return _d(8) + _d(8)
def _chunk_send_lines(lines, max_chunk=25):
    """Yield chunks of lines for multiple sends."""
    cur = []
    for s in lines:
        cur.append(s)
        if len(cur) >= max_chunk:
            yield cur
            cur = []
    if cur: yield cur

def _get_map(cfg, chan_id, key: str) -> dict:
    raw = cfg.get(chan_id, key, fallback="{}")
    try: return json.loads(raw) or {}
    except: return {}
def _set_map(cfg, chan_id, key: str, data: dict): cfg.set(chan_id, key, json.dumps(data))

def _gen_gems(count: int) -> tuple[list[str], int]:
    """
    Roll 'count' gems using BFRPG table.
    Returns (lines, total_gp_value).
    """
    lines, total_gp = [], 0

    def _tier():
        r = _d100()
        if   r <= 20:  return ("Ornamental",     10,  "1d10")
        elif r <= 45:  return ("Semiprecious",   50,  "1d8")
        elif r <= 75:  return ("Fancy",         100,  "1d6")
        elif r <= 95:  return ("Precious",      500,  "1d4")
        else:          return ("Gem",          1000,  "1d2")

    GEM_NAMES = (
        ("Alexandrite",5),("Amethyst",7),("Aventurine",8),("Chlorastrolite",10),("Diamond",10),
        ("Emerald",3),("Fire Opal",5),("Fluorospar",9),("Garnet",6),("Heliotrope",5),
        ("Malachite",10),("Rhodonite",10),("Ruby",3),("Sapphire",4),("Topaz",5)
    )

    _names_flat = [name for name, w in GEM_NAMES for _ in range(w)] or ["Gemstone"]

    for _ in range(max(0, count)):
        tier, base_gp, _stack = _tier()
        name = random.choice(_names_flat)
        total_gp += base_gp
        lines.append(f"{tier} {name} — {base_gp} gp")

    return lines, total_gp

def _is_perm(cfg, chan_id: str, slot: str, base_key: str) -> bool:
    """
    True if this effect should be treated as permanent and must not be
    decremented or incremented by n/p/nt.

    Rules supported:
    - Named flags: light_perm / darkness_perm / blind_perm
    - Custom x_*: <slot>.x_foo_perm flag
    - Custom x_*: <slot>.x_foo_code in {"perm","permanent"}
    - Negative sentinel: <slot>.<base_key> < 0
    """

    PERM_MAP = {"light": "light_perm", "darkness": "darkness_perm", "blind": "blind_perm"}
    perm_flag = PERM_MAP.get(base_key)
    if perm_flag and cfg.getint(chan_id, f"{slot}.{perm_flag}", fallback=0) > 0:
        return True

    if base_key.startswith("x_"):
        if cfg.getint(chan_id, f"{slot}.{base_key}_perm", fallback=0) > 0:
            return True
        code = cfg.get(chan_id, f"{slot}.{base_key}_code", fallback="").strip().lower()
        if code in {"perm", "permanent"}:
            return True

    if cfg.has_option(chan_id, f"{slot}.{base_key}"):
        if cfg.getint(chan_id, f"{slot}.{base_key}", fallback=0) < 0:
            return True

    return False

def _apply_stoneskin_absorb(cfg, chan_id: str, target_name: str, dmg: int) -> tuple[int, int, int]:
    """
    If target has Stoneskin (duration>0 and pool>0), absorb up to <pool> damage.
    Returns (absorbed, remaining_damage, ss_hp_left). Clears SS if pool reaches 0.
    """
    if dmg <= 0:
        return (0, 0, 0)

    slot = _choose_slot_for_effects(cfg, chan_id, target_name)
    dur = cfg.getint(chan_id, f"{slot}.stone",   fallback=0)
    hp  = cfg.getint(chan_id, f"{slot}.stonehp", fallback=0)
    if dur <= 0 or hp <= 0:
        return (0, dmg, max(0, hp))

    absorbed = min(hp, dmg)
    hp_left  = hp - absorbed

    if hp_left <= 0:

        if cfg.has_option(chan_id, f"{slot}.stone"):   cfg.remove_option(chan_id, f"{slot}.stone")
        if cfg.has_option(chan_id, f"{slot}.stonehp"): cfg.remove_option(chan_id, f"{slot}.stonehp")
        if cfg.has_option(chan_id, f"{slot}.stone_by"): cfg.remove_option(chan_id, f"{slot}.stone_by")
    else:
        cfg.set(chan_id, f"{slot}.stonehp", str(hp_left))

    _save_battles(cfg)
    return (absorbed, dmg - absorbed, max(0, hp_left))

def _gen_jewelry(count: int) -> tuple[list[str], int]:
    """
    Each jewelry item is worth 2d8×100 gp.
    Returns (lines, total_gp_value).
    """
    lines, total_gp = [], 0

    J = [
        "Anklet","Belt","Bowl","Bracelet","Brooch","Buckle","Chain","Choker","Circlet","Clasp",
        "Comb","Crown","Cup","Earring","Flagon","Goblet","Knife","Letter Opener","Locket","Medal",
        "Necklace","Plate","Pin","Scepter","Statuette","Tiara"
    ]
    for _ in range(max(0, count)):
        kind = random.choice(J)
        val = _2d8() * 100
        total_gp += val
        lines.append(f"{kind} — {val} gp")
    return lines, total_gp

_GEM_BASE = [
    (1, 20, ("Ornamental", 10,  "1d10")),
    (21,45, ("Semiprecious", 50, "1d8")),
    (46,75, ("Fancy",       100, "1d6")),
    (76,95, ("Precious",    500, "1d4")),
    (96,99, ("Gem",        1000, "1d2")),
    (100,100, ("Gem",      1000, "1d2")),
]
_GEM_TYPES = [
    (1,5,"Alexandrite"), (6,12,"Amethyst"), (13,20,"Aventurine"),
    (21,30,"Chlorastrolite"), (31,40,"Diamond"), (41,43,"Emerald"),
    (44,48,"Fire Opal"), (49,57,"Fluorospar"), (58,63,"Garnet"),
    (64,68,"Heliotrope"), (69,78,"Malachite"), (79,88,"Rhodonite"),
    (89,91,"Ruby"), (92,95,"Sapphire"), (96,100,"Topaz"),
]
def _pick_percent(table):
    """
    Roll d% on a table of rows that are either:
      (lo, hi, val)   -> inclusive range
      (cutoff, val)   -> '≤ cutoff' single-threshold rows
    """
    r = _d100()
    for row in table:
        if len(row) == 3:
            lo, hi, val = row
            if lo <= r <= hi:
                return val
        elif len(row) == 2:
            cutoff, val = row
            if r <= cutoff:
                return val
    last = table[-1]
    return last[-1] if isinstance(last, (tuple, list)) else last

_JEWELRY_TYPES = [
    (1,6,"Anklet"), (7,12,"Belt"), (13,14,"Bowl"), (15,21,"Bracelet"),
    (22,27,"Brooch"), (28,32,"Buckle"), (33,37,"Chain"), (38,40,"Choker"),
    (41,42,"Circlet"), (43,47,"Clasp"), (48,51,"Comb"), (52,52,"Crown"),
    (53,55,"Cup"), (56,62,"Earring"), (63,65,"Flagon"), (66,68,"Goblet"),
    (69,73,"Knife"), (74,77,"Letter Opener"), (78,80,"Locket"), (81,82,"Medal"),
    (83,89,"Necklace"), (90,90,"Plate"), (91,95,"Pin"), (96,96,"Scepter"),
    (97,99,"Statuette"), (100,100,"Tiara"),
]

_POTIONS = [
    (1,3,"Clairaudience"), (4,6,"Clairvoyance"), (7,8,"Cold Resistance"),
    (9,11,"Control Animal"), (12,13,"Control Dragon"), (14,16,"Control Giant"),
    (17,19,"Control Human"), (20,22,"Control Plant"), (23,25,"Control Undead"),
    (26,32,"Delusion"), (33,35,"Diminution"), (36,39,"Fire Resistance"),
    (40,43,"Flying"), (44,47,"Gaseous Form"), (48,51,"Giant Strength"),
    (52,55,"Growth"), (56,59,"Healing"), (60,63,"Heroism"),
    (64,68,"Invisibility"), (69,72,"Invulnerability"), (73,76,"Levitation"),
    (77,80,"Longevity"), (81,84,"Mind Reading"), (85,86,"Poison"),
    (87,89,"Polymorph Self"), (90,97,"Speed"), (98,100,"Treasure Finding"),
]

_WEAPON_TYPES = [
    (1,2,"GreatAxe"), (3,9,"BattleAxe"), (10,11,"HandAxe"),
    (12,19,"Shortbow"), (20,27,"Shortbow"),
    (28,31,"Longbow"), (32,35,"Longbow"),
    (36,43,"LightXbow"), (44,47,"HeavyXbow"),
    (48,59,"Dagger"), (60,65,"Shortsword"), (66,79,"Longsword"),
    (80,81,"Longsword"), (82,83,"Greatsword"),
    (84,86,"Warhammer"), (87,94,"Mace"),
    (95,95,"Maul"), (96,96,"Polearm"),
    (97,97,"Sling"), (98,100,"Spear"),
]
_MELEE_BONUS = [
    (1,40, "+1"),
    (41,50,"+2"),
    (51,55,"+3"),
    (56,57,"+4"),
    (58,   "+5"),
    (59,75,"+1, +2 vs. Special Enemy"),
    (76,85,"+1, +3 vs. Special Enemy"),
    (86,95,"Roll Again + Special Ability"),
    (96,98,"Cursed, -1"),
    (99,100,"Cursed, -2"),
]
_MISSILE_BONUS = [
    (1,46,"+1"), (47,58,"+2"), (59,64,"+3"),
    (65,82,"+1, +2 vs. Special Enemy"),
    (83,94,"+1, +3 vs. Special Enemy"),
    (95,98,"Cursed, -1"), (99,100,"Cursed, -2"),
]
_ENEMIES = {1:"Dragons",2:"Enchanted",3:"Lycanthropes",4:"Regenerators",5:"Spell Users",6:"Undead"}
_ABILITIES = {
    1:"Casts Light on Command", 2:"Casts Light on Command", 3:"Casts Light on Command",
    4:"Casts Light on Command", 5:"Casts Light on Command", 6:"Casts Light on Command",
    7:"Casts Light on Command", 8:"Casts Light on Command", 9:"Casts Light on Command",
    10:"Charm Person", 11:"Charm Person", 12:"Drains Energy",
    13:"Flames on Command", 14:"Flames on Command", 15:"Flames on Command", 16:"Flames on Command",
    17:"Locate Objects", 18:"Locate Objects", 19:"Locate Objects",
    20:"Wishes",
}
def _is_missile_weapon(wt: str) -> bool:
    wt = wt.lower()
    return any(x in wt for x in ("bow","arrow","quarrel","sling"))

_LAIR = {
    "A": {
        "coins": {
            "cp": (50, "5d6", 100), "sp": (60, "5d6", 100), "ep": (40, "5d4", 100),
            "gp": (70, "10d6", 100), "pp": (50, "1d10", 100),
        },
        "gems":     (50, "6d6"),
        "jewelry":  (50, "6d6"),
        "magic":    {"any": (30, "3")}
    },
    "B": {
        "coins": {
            "cp": (75, "5d10",100), "sp": (50, "5d6",100), "ep": (50, "5d4",100),
            "gp": (50, "3d6", 100),
        },
        "gems":     (25, "1d6"),
        "jewelry":  (25, "1d6"),
        "magic":    {"wa": (10, "1")}
    },
    "C": {
        "coins": {
            "cp": (60, "6d6",100), "sp": (60, "5d4",100), "ep": (30, "2d6",100),
        },
        "gems":     (25, "1d4"),
        "jewelry":  (25, "1d4"),
        "magic":    {"any": (15, "1d2")}
    },
    "D": {
        "coins": {
            "cp": (30, "4d6",100), "sp": (45, "6d6",100), "gp": (90, "5d8",100),
        },
        "gems":     (30, "1d8"),
        "jewelry":  (30, "1d8"),
        "magic":    {"any": (20, "1d2"), "potions": (100, "1")}
    },
    "E": {
        "coins": {
            "cp": (30, "2d8",100), "sp": (60, "6d10",100), "ep": (50, "3d8",100),
            "gp": (50, "4d10",100),
        },
        "gems":     (10, "1d10"),
        "jewelry":  (10, "1d10"),
        "magic":    {"any": (30, "1d4"), "scrolls": (100, "1")}
    },
    "F": {
        "coins": {
            "sp": (40, "3d8",100), "ep": (50, "4d8",100), "gp": (85, "6d10",100),
            "pp": (70, "2d8",100),
        },
        "gems":     (20, "2d12"),
        "jewelry":  (10, "1d12"),
        "magic":    {"xw": (35, "1d4"), "potions": (100, "1"), "scrolls": (100, "1")}
    },
    "G": {
        "coins": {
            "gp": (90, "4d6x10",100), "pp": (75, "5d8",100),
        },
        "gems":     (25, "3d6"),
        "jewelry":  (25, "1d10"),
        "magic":    {"any": (50, "1d4"), "scrolls": (100, "1")}
    },

    "H": {
        "coins": {
            "cp": (100, "8d10",100), "sp": (100, "6d10x10",100),
            "ep": (100, "3d10x10",100), "gp": (100, "5d8x10",100),
            "pp": (100, "9d8",100),
        },
        "gems":     (100, "1d100"),
        "jewelry":  (100, "10d4"),
        "magic":    {"any": (100, "1d4"), "potions": (100, "1"), "scrolls": (100, "1")}
    },
    "I": {
        "coins": { "pp": (80, "3d10",100) },
        "gems":  (50, "2d6"),
        "jewelry": (50, "2d6"),
        "magic": { "any": (15, "1") }
    },
}

def _roll_one_lair_and_apply(cfg, chan_id: str, code: str) -> list[str]:
    """
    Resolve ONE lair hoard and apply results to tallies/pools.
    Uses LAIR_TABLE (A..O). Falls back to _LAIR (A..I) if needed.
    """
    code = (code or "").strip().upper()
    row = LAIR_TABLE.get(code) or _LAIR.get(code)
    if not row:
        return [f"❌ Lair type **{code}** is not implemented."]

    out = [f"🏴 Lair **{code}**:"]

    for cur, spec in (row.get("coins") or {}).items():
        try:
            chance, dice, mult = int(spec[0]), str(spec[1]), int(spec[2])
        except Exception:
            continue
        if _d100() <= chance:
            amt = _dice_sum(dice) * mult
            if amt:
                _inc_cfg_int(cfg, chan_id, f"tre_{cur}", amt)
                _inc_cfg_int(cfg, chan_id, f"new_{cur}", amt)
                out.append(f"• {cur.upper()}: +{amt}")

    if "gems" in row:
        ch, di = row["gems"]
        if int(ch) > 0 and _d100() <= int(ch):
            n = _dice_sum(di)
            if n:
                _inc_cfg_int(cfg, chan_id, "tre_gems", n)
                _inc_cfg_int(cfg, chan_id, "new_gems", n)
                out.append(f"• Gems: +{n}")

    if "jewelry" in row:
        ch, di = row["jewelry"]
        if int(ch) > 0 and _d100() <= int(ch):
            n = _dice_sum(di)
            if n:
                _inc_cfg_int(cfg, chan_id, "tre_jewelry", n)
                _inc_cfg_int(cfg, chan_id, "new_jewelry", n)
                out.append(f"• Jewelry: +{n}")

    magic = row.get("magic", {})
    def _add_pool(key: str, n: int, label: str):
        if n <= 0:
            return
        _inc_cfg_int(cfg, chan_id, key, n)
        _inc_cfg_int(cfg, chan_id, key.replace("tre_", "new_"), n)
        out.append(f"• Magic ({label}): +{n}")

    if magic:
        chance = int(magic.get("chance", 0))
        if _d100() <= chance:

            if "any" in magic:
                _add_pool("tre_magic_any", _dice_sum(magic["any"]), "any")

            if magic.get("weapon_or_armor"):
                _add_pool("tre_magic_wa", _dice_sum(magic["weapon_or_armor"]), "weapon/armor")

            if magic.get("except_weapons"):
                die = magic.get("any", "1")
                _add_pool("tre_magic_xw", _dice_sum(die), "except weapons")

            _add_pool("tre_potions", int(magic.get("potions_bonus", 0)), "potions")
            _add_pool("tre_scrolls", int(magic.get("scrolls_bonus", 0)), "scrolls")

    if "potions_only" in row:
        ch, di = row["potions_only"]
        if _d100() <= int(ch):
            _add_pool("tre_potions", _dice_sum(di), "potions")

    if "scrolls_only" in row:
        ch, di = row["scrolls_only"]
        if _d100() <= int(ch):
            _add_pool("tre_scrolls", _dice_sum(di), "scrolls")

    try:
        opt = f"tre_lair_bonus_{code}"
        bonus_spec = cfg.get(chan_id, opt, fallback="").strip()
        if bonus_spec:

            spec = bonus_spec.replace("×", "x").replace("*", "x").replace(" ", "")
            try:
                amt = _dice_sum(spec)
            except Exception:
                amt = 0
            if amt and amt > 0:
                _inc_cfg_int(cfg, chan_id, "tre_gp", amt)
                _inc_cfg_int(cfg, chan_id, "new_gp", amt)
                out.append(f"• Bonus GP: +{amt} (from {bonus_spec})")

            if cfg.has_option(chan_id, opt):
                cfg.remove_option(chan_id, opt)
    except Exception:
        pass

    return out if len(out) > 1 else [f"🏴 Lair **{code}**: nothing this time."]

def _monster_has_regen(att_cfg, att_path):
    """
    True if monster lists 'regen' / 'regeneration' in special (instance or template).
    """
    import os
    try:
        s1 = " ".join([
            (get_compat(att_cfg, "base", "special", fallback="") or ""),
            (get_compat(att_cfg, "info", "special", fallback="") or "")
        ]).lower()
        if ("regen" in s1) or ("regeneration" in s1):
            return True

        mtype = (get_compat(att_cfg, "info", "monster_type", fallback="") or "").strip().lower()
        for p in (f"monsters/{mtype}.ini", f"{mtype}.ini"):
            if os.path.exists(p):
                mcfg = read_cfg(p)
                s2 = " ".join([
                    (get_compat(mcfg, "base", "special", fallback="") or ""),
                    (get_compat(mcfg, "info", "special", fallback="") or "")
                ]).lower()
                if ("regen" in s2) or ("regeneration" in s2):
                    return True
    except Exception:
        pass
    return False

def _pc_has_regen_item(pcfg) -> bool:
    """True if the PC has Ring/Pendant of Regeneration equipped."""
    try:
        for k in ("ring", "ring2", "neck", "pendant", "amulet"):
            v = (get_compat(pcfg, "eq", k, fallback="") or "").strip().lower()
            nm = re.sub(r"[^a-z0-9]+", "", v)
            if "ringofregeneration" in nm or "pendantofregeneration" in nm:
                return True
    except Exception:
        pass
    return False
    
_ARMOR_TYPES = [
    (1,9,"Leather Armor"),
    (10,28,"Chain Mail"),
    (29,43,"Plate Mail"),
    (44,100,"Shield"),
]
_ARMOR_BONUS = [
    (1,50,"+1"), (51,80,"+2"), (81,90,"+3"),
    (91,95,"Cursed*"), (96,100,"Cursed, AC 11**"),
]

_WSR = [
    (1,8,"Rod of Cancellation"),
    (9,13,"Snake Staff"), (14,17,"Staff of Commanding"), (18,28,"Staff of Healing"),
    (29,30,"Staff of Power"), (31,34,"Staff of Striking"), (35,35,"Staff of Wizardry"),
    (36,40,"Wand of Cold"), (41,45,"Wand of Enemy Detection"), (46,50,"Wand of Fear"),
    (51,55,"Wand of Fireballs"), (56,60,"Wand of Illusion"), (61,65,"Wand of Lightning Bolts"),
    (66,73,"Wand of Magic Detection"), (74,79,"Wand of Paralysis"),
    (80,84,"Wand of Polymorph"), (85,92,"Wand of Secret Door Detection"),
    (93,100,"Wand of Trap Detection"),
]

_RARE = [
    (1,5,"Bag of Devouring"), (6,20,"Bag of Holding"),
    (21,32,"Boots of Traveling and Leaping"), (33,47,"Broom of Flying"),
    (48,57,"Device of Summoning Elementals"), (58,59,"Efreeti Bottle"),
    (60,64,"Flying Carpet"), (65,81,"Gauntlets of Ogre Power"),
    (82,86,"Girdle of Giant Strength"), (87,88,"Mirror of Imprisonment"),
    (89,100,"Rope of Climbing"),
]

def _owner_mention(name: str, cfg=None, chan_id: str | None = None) -> str:
    """
    Return '<@id>' for this PC's owner, or ''.
    Tries display override first, then canonical name. Skips monsters.
    """
    candidates = [name]
    try:
        if cfg is not None and chan_id is not None and cfg.has_section(chan_id):
            slot = _choose_slot_for_effects(cfg, chan_id, name)
            disp = cfg.get(chan_id, f"{slot}.disp", fallback="")
            if disp and disp not in candidates:
                candidates.insert(0, disp)
    except Exception:
        pass

    for nm in candidates:
        disp, path = _resolve_char_ci_local(nm)
        if not path:
            continue
        pcfg = read_cfg(path)
        cls = (get_compat(pcfg, "info", "class", fallback="") or "").strip().lower()
        if cls == "monster":
            return ""
        owner = (get_compat(pcfg, "info", "owner_id", fallback="") or "").strip()
        if owner.isdigit():
            return f"<@{owner}>"
        legacy = (get_compat(pcfg, "info", "owner", fallback="") or "")
        m = re.search(r"(\d{15,25})", legacy)
        if m:
            return f"<@{m.group(1)}>"
    return ""

def _roll_individual(code: str) -> dict:
    """Return {'cp','sp','ep','gp','pp','gems','jewelry','magic_any','potions','scrolls'} for one monster."""
    code = (code or "").strip().upper()
    out = dict(cp=0, sp=0, ep=0, gp=0, pp=0, gems=0, jewelry=0, magic_any=0, potions=0, scrolls=0)

    if code == "P":
        out["cp"] = _dice_sum("3d8")
    elif code == "Q":
        out["sp"] = _dice_sum("3d6")
    elif code == "R":
        out["ep"] = _dice_sum("2d6")
    elif code == "S":
        out["gp"] = _dice_sum("2d4")
    elif code == "T":
        out["pp"] = _dice_sum("1d6")
    elif code == "U":
        if random.randint(1,100) <= 50: out["cp"] += _dice_sum("1d20")
        if random.randint(1,100) <= 50: out["sp"] += _dice_sum("1d20")
        if random.randint(1,100) <= 25: out["gp"] += _dice_sum("1d20")
        if random.randint(1,100) <= 5:  out["gems"] += _dice_sum("1d4")
        if random.randint(1,100) <= 5:  out["jewelry"] += _dice_sum("1d4")
        if random.randint(1,100) <= 2:  out["magic_any"] += 1
    elif code == "V":
        if random.randint(1,100) <= 25: out["sp"] += _dice_sum("1d20")
        if random.randint(1,100) <= 25: out["ep"] += _dice_sum("1d20")
        if random.randint(1,100) <= 50: out["gp"] += _dice_sum("1d20")
        if random.randint(1,100) <= 25: out["pp"] += _dice_sum("1d20")
        if random.randint(1,100) <= 10: out["gems"] += _dice_sum("1d4")
        if random.randint(1,100) <= 10: out["jewelry"] += _dice_sum("1d4")
        if random.randint(1,100) <= 5:  out["magic_any"] += 1

    return out

LAIR_TABLE = {

    "A": {
        "coins": {"cp":(50,"5d6",100), "sp":(60,"5d6",100), "ep":(40,"5d4",100), "gp":(70,"10d6",100), "pp":(50,"1d10",100)},
        "gems": (50,"1d10"), "jewelry": (50,"6d6"),
        "magic": {"chance":30, "any":"3"}
    },
    "B": {
        "coins": {"cp":(75,"5d10",100), "sp":(50,"5d6",100), "ep":(50,"5d4",100), "gp":(50,"3d6",100), "pp":(0,"0",1)},
        "gems": (25,"1d6"), "jewelry": (25,"1d6"),
        "magic": {"chance":10, "weapon_or_armor":"1"}
    },
    "C": {
        "coins": {"cp":(60,"6d6",100), "sp":(60,"5d4",100), "ep":(30,"2d6",100), "gp":(0,"0",1), "pp":(0,"0",1)},
        "gems": (25,"1d4"), "jewelry": (25,"1d4"),
        "magic": {"chance":15, "any":"1d2"}
    },
    "D": {
        "coins": {"cp":(30,"4d6",100), "sp":(45,"6d6",100), "ep":(0,"0",1), "gp":(90,"5d8",100), "pp":(0,"0",1)},
        "gems": (30,"1d8"), "jewelry": (30,"1d8"),
        "magic": {"chance":20, "any":"1d2", "potions_bonus":1}
    },
    "E": {
        "coins": {"cp":(30,"2d8",100), "sp":(60,"6d10",100), "ep":(50,"3d8",100), "gp":(50,"4d10",100), "pp":(0,"0",1)},
        "gems": (10,"1d10"), "jewelry": (10,"1d10"),
        "magic": {"chance":30, "any":"1d4", "scrolls_bonus":1}
    },
    "F": {
        "coins": {"cp":(0,"0",1), "sp":(40,"3d8",100), "ep":(50,"4d8",100), "gp":(85,"6d10",100), "pp":(70,"2d8",100)},
        "gems": (20,"2d12"), "jewelry": (10,"1d12"),
        "magic": {"chance":35, "any":"1d4", "except_weapons":True, "potions_bonus":1, "scrolls_bonus":1}
    },
    "G": {
        "coins": {"cp":(0,"0",1), "sp":(0,"0",1), "ep":(0,"0",1), "gp":(90,"4d6",10), "pp":(75,"5d8",100)},
        "gems": (25,"3d6"), "jewelry": (25,"1d10"),
        "magic": {"chance":50, "any":"1d4", "scrolls_bonus":1}
    },
    "H": {
        "coins": {"cp":(100,"8d10",100), "sp":(100,"6d10",10), "ep":(100,"3d10",10), "gp":(100,"5d8",10), "pp":(100,"9d8",100)},
        "gems": (100,"1d100"), "jewelry": (100,"10d4"),
        "magic": {"chance":100, "any":"1d4", "potions_bonus":1, "scrolls_bonus":1}

    },
    "I": {
        "coins": {"cp":(0,"0",1), "sp":(0,"0",1), "ep":(0,"0",1), "gp":(0,"0",1), "pp":(80,"3d10",100)},
        "gems": (50,"2d6"), "jewelry": (50,"2d6"),
        "magic": {"chance":15, "any":"1"}
    },
    "J": {
        "coins": {"cp":(45,"3d8",1), "sp":(45,"1d8",1), "ep":(0,"0",1), "gp":(0,"0",1), "pp":(0,"0",1)},
        "gems": (0,"0"), "jewelry": (0,"0")
    },
    "K": {
        "coins": {"cp":(0,"0",1), "sp":(90,"2d10",1), "ep":(35,"1d8",1), "gp":(0,"0",1), "pp":(0,"0",1)},
        "gems": (0,"0"), "jewelry": (0,"0")
    },
    "L": {
        "coins": {"cp":(0,"0",1), "sp":(0,"0",1), "ep":(0,"0",1), "gp":(0,"0",1), "pp":(0,"0",1)},
        "gems": (50,"1d4"), "jewelry": (0,"0")
    },
    "M": {
        "coins": {"cp":(0,"0",1), "sp":(0,"0",1), "ep":(0,"0",1), "gp":(90,"4d10",1), "pp":(90,"2d8",10)},
        "gems": (55,"5d4"), "jewelry": (45,"2d6")
    },
    "N": {
        "coins": {"cp":(0,"0",1), "sp":(0,"0",1), "ep":(0,"0",1), "gp":(0,"0",1), "pp":(0,"0",1)},

        "potions_only": (40,"2d4")
    },
    "O": {
        "coins": {"cp":(0,"0",1), "sp":(0,"0",1), "ep":(0,"0",1), "gp":(0,"0",1), "pp":(0,"0",1)},

        "scrolls_only": (50,"1d4")
    },
}

def _roll_lair_once(code: str) -> dict:
    """Return totals for ONE lair roll of this type; same keys as _roll_individual plus jewelry + magic splits."""
    code = (code or "").strip().upper()
    cfg = LAIR_TABLE.get(code)
    out = dict(cp=0, sp=0, ep=0, gp=0, pp=0,
               gems=0, jewelry=0,
               magic_any=0, magic_wa=0, magic_xw=0,
               potions=0, scrolls=0)
    if not cfg:
        return out

    coins = cfg.get("coins", {})
    for k in ("cp","sp","ep","gp","pp"):
        if k in coins:
            chance, dice, mult = coins[k]
            if random.randint(1,100) <= int(chance):
                out[k] += _dice_sum(dice) * int(mult)

    if "gems" in cfg:
        chance, dice = cfg["gems"]
        if int(chance) > 0 and random.randint(1,100) <= int(chance):
            out["gems"] += _dice_sum(dice)
    if "jewelry" in cfg:
        chance, dice = cfg["jewelry"]
        if int(chance) > 0 and random.randint(1,100) <= int(chance):
            out["jewelry"] += _dice_sum(dice)

    if "magic" in cfg:
        m = cfg["magic"]
        if random.randint(1,100) <= int(m.get("chance", 0)):
            if "any" in m:
                out["magic_any"] += _dice_sum(m["any"])
            if m.get("weapon_or_armor"):
                out["magic_wa"] += _dice_sum(m["weapon_or_armor"])
            if m.get("except_weapons"):
                out["magic_xw"] += _dice_sum(m.get("any","1"))

            out["potions"] += int(m.get("potions_bonus", 0))
            out["scrolls"] += int(m.get("scrolls_bonus", 0))

    if "potions_only" in cfg:
        chance, dice = cfg["potions_only"]
        if random.randint(1,100) <= int(chance):
            out["potions"] += _dice_sum(dice)
    if "scrolls_only" in cfg:
        chance, dice = cfg["scrolls_only"]
        if random.randint(1,100) <= int(chance):
            out["scrolls"] += _dice_sum(dice)

    return out

def _load_battles():
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(BATTLE_FILE)
    return cfg

def _save_battles(cfg):
    with open(BATTLE_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)

def _section_id(channel):
    return str(channel.id)

def _parse_combatants(cfg, chan_id):
    names_line = cfg.get(chan_id, "list", fallback="")
    names = [n for n in names_line.split() if n]
    scores = {}
    for n in names:
        try:
            scores[n] = int(cfg.get(chan_id, n, fallback="0"))
        except ValueError:
            scores[n] = 0
    return names, scores

def _write_combatants(cfg, chan_id, names, scores):
    cfg.set(chan_id, "list", " ".join(names))
    for n in names:
        cfg.set(chan_id, n, str(scores.get(n, 0)))

def _sorted_names(names, scores):
    return sorted(names, key=lambda n: (-scores.get(n, 0), n.lower()))

def _apply_queued_blind_after_paralysis(cfg, chan_id: str, slot: str) -> tuple[int, str, str]:
    """
    If a queued blind (from Color Cloud, Color Spray, or Scintillating Pattern) exists and paralysis is gone,
    apply it now. Returns (applied_rounds, source_key, by_name).
    """
    cc_pend = cfg.getint(chan_id, f"{slot}.cc_blind_pending", fallback=0)
    cs_pend = cfg.getint(chan_id, f"{slot}.cs_blind_pending", fallback=0)
    sp_pend = cfg.getint(chan_id, f"{slot}.sp_blind_pending", fallback=0)

    src, pend, by = ("", 0, "")
    if sp_pend > 0:
        src, pend = ("scintillating", sp_pend)
        by = cfg.get(chan_id, f"{slot}.sp_blind_by", fallback="")
    elif cc_pend > 0:
        src, pend = ("colorcloud", cc_pend)
        by = cfg.get(chan_id, f"{slot}.cc_blind_by", fallback="")
    elif cs_pend > 0:
        src, pend = ("colorspray", cs_pend)
        by = cfg.get(chan_id, f"{slot}.cs_blind_by", fallback="")

    if pend <= 0:
        return (0, "", "")

    for k in (f"{slot}.cs_blind_pending", f"{slot}.cs_blind_by",
              f"{slot}.cc_blind_pending", f"{slot}.cc_blind_by",
              f"{slot}.sp_blind_pending", f"{slot}.sp_blind_by"):
        if cfg.has_option(chan_id, k):
            cfg.remove_option(chan_id, k)

    cur_bl = cfg.getint(chan_id, f"{slot}.blind", fallback=0)
    new_bl = max(cur_bl, pend)
    cfg.set(chan_id, f"{slot}.blind", str(new_bl))
    cfg.set(chan_id, f"{slot}.blind_src", src)
    if by:
        cfg.set(chan_id, f"{slot}.blind_by", by)

    return (new_bl, src, by)

def _apply_queued_confusion_after_blind(cfg, chan_id: str, name_key: str) -> tuple[int, str | None]:
    """
    If BLIND is gone and we have queued Confusion (from Scintillating Pattern), apply it now.
    Returns (applied_rounds, note_for_channel_or_None).
    """
    slot = _choose_slot_for_effects(cfg, chan_id, name_key)

    bl_left = cfg.getint(chan_id, f"{slot}.blind", fallback=0)
    bl_perm = cfg.getint(chan_id, f"{slot}.blind_perm", fallback=0)
    if bl_left > 0 or bl_perm > 0:
        return (0, None)

    pend = cfg.getint(chan_id, f"{slot}.sp_cnf_pending", fallback=0)
    if pend <= 0:
        return (0, None)

    if cfg.has_option(chan_id, f"{slot}.sp_cnf_pending"): cfg.remove_option(chan_id, f"{slot}.sp_cnf_pending")
    by = cfg.get(chan_id, f"{slot}.sp_cnf_by", fallback="")
    if cfg.has_option(chan_id, f"{slot}.sp_cnf_by"): cfg.remove_option(chan_id, f"{slot}.sp_cnf_by")

    prev = cfg.getint(chan_id, f"{slot}.cn", fallback=0)
    newv = max(prev, pend)
    cfg.set(chan_id, f"{slot}.cn", str(newv))
    if by: cfg.set(chan_id, f"{slot}.cn_by", by)
    _save_battles(cfg)
    return (newv, f"{name_key} becomes **CONFUSED** for **{newv}** rounds (Scintillating Pattern).")

def _tick_paralyze_on_turn(cfg, chan_id: str, name_key: str) -> tuple[bool, str | None]:
    """
    Decrement <slot>.paralyzed when this creature's turn begins.
    Returns (still_paralyzed, status_text_for_channel_or_None).
    """

    def _choose_slot_for_effects(cfg, chan_id, name):
        cands = []
        try:
            cands.append(_slot(name))
        except Exception:
            pass
        cands.append(str(name).replace(" ", "_"))
        prefer = ("cn","paralyzed","blind",
          "cc_blind_pending","cs_blind_pending","sp_blind_pending","sp_cnf_pending",
          "cc","magwep","ghh","magearmor","boneskin","sph","shl","chl","cr","gcr","light","darkness")

        for s in cands:
            if any(cfg.has_option(chan_id, f"{s}.{k}") for k in prefer):
                return s
        return cands[0]

    slot = _choose_slot_for_effects(cfg, chan_id, name_key)

    opt = f"{slot}.paralyzed"
    if not cfg.has_option(chan_id, opt):
        return (False, None)

    n = cfg.getint(chan_id, opt, fallback=0)
    n = max(0, n - 1)

    if n <= 0:
        cfg.remove_option(chan_id, opt)
        by_opt = f"{slot}.paralyzed_by"
        if cfg.has_option(chan_id, by_opt):
            cfg.remove_option(chan_id, by_opt)

        applied, src, _by = _apply_queued_blind_after_paralysis(cfg, chan_id, slot)

        msg = f"{name_key} is no longer **PARALYZED**."
        if applied > 0:
            src_label = ("Color Cloud" if src == "colorcloud"
                         else "Color Spray" if src == "colorspray"
                         else "Scintillating Pattern" if src == "scintillating"
                         else "magic")
            msg = (f"{name_key} shakes off **UNCONSCIOUS** and is now **BLINDED** "
                   f"for {applied} rounds ({src_label}).")

        _save_battles(cfg)

        sleep_left = cfg.getint(chan_id, f"{slot}.sleep", fallback=0)
        if sleep_left > 0:
            sleep_left -= 1
            if sleep_left <= 0:
                if cfg.has_option(chan_id, f"{slot}.sleep"):
                    cfg.remove_option(chan_id, f"{slot}.sleep")
                if cfg.has_option(chan_id, f"{slot}.sleep_by"):
                    cfg.remove_option(chan_id, f"{slot}.sleep_by")

        return (False, msg)
    else:
        cfg.set(chan_id, opt, str(n))
        _save_battles(cfg)
        return (True, f"{name_key} is **PARALYZED** ({n} rds remain).")

def _tick_stoneskin_on_turn(cfg, chan_id: str, name_key: str) -> tuple[bool, str | None]:
    """
    Decrement <slot>.stone (rounds). If duration hits 0 OR pool is 0, clear both and report.
    Returns (still_active, note_text_or_None).
    """
    slot = _choose_slot_for_effects(cfg, chan_id, name_key)
    dur_key = f"{slot}.stone"
    hp_key  = f"{slot}.stonehp"

    if not (cfg.has_option(chan_id, dur_key) or cfg.has_option(chan_id, hp_key)):
        return (False, None)

    dur = cfg.getint(chan_id, dur_key, fallback=0)
    hp  = cfg.getint(chan_id, hp_key,  fallback=0)

    if dur > 0:
        dur = max(0, dur - 1)
        cfg.set(chan_id, dur_key, str(dur))

    if dur <= 0 or hp <= 0:
        if cfg.has_option(chan_id, dur_key): cfg.remove_option(chan_id, dur_key)
        if cfg.has_option(chan_id, hp_key):  cfg.remove_option(chan_id, hp_key)

        by_key = f"{slot}.stone_by"
        if cfg.has_option(chan_id, by_key): cfg.remove_option(chan_id, by_key)
        _save_battles(cfg)
        return (False, f"{name_key}’s **Stoneskin** crumbles.")
    else:
        _save_battles(cfg)
        return (True, None)

def _tick_mirror_on_turn(cfg, chan_id: str, name_key: str) -> tuple[bool, str | None]:
    """
    Decrement <slot>.mi (rounds). When it reaches 0, also clear <slot>.mi_images.
    Returns (still_active, note_text_or_None).
    """

    slot = _choose_slot_for_effects(cfg, chan_id, name_key)

    key = f"{slot}.mi"
    imgs_key = f"{slot}.mi_images"

    if not cfg.has_option(chan_id, key):
        return (False, None)

    n = cfg.getint(chan_id, key, fallback=0)
    n = max(0, n - 1)
    if n <= 0:
        cfg.remove_option(chan_id, key)
        cleared = cfg.getint(chan_id, imgs_key, fallback=0)
        if cfg.has_option(chan_id, imgs_key):
            cfg.remove_option(chan_id, imgs_key)
        by_opt = f"{slot}.mi_by"
        if cfg.has_option(chan_id, by_opt):
            cfg.remove_option(chan_id, by_opt)
        _save_battles(cfg)
        msg = f"{name_key}’s **Mirror Image** ends."
        if cleared:
            msg += f" ({cleared} remaining figment{'s' if cleared != 1 else ''} disperse.)"
        return (False, msg)
    else:
        cfg.set(chan_id, key, str(n))
        _save_battles(cfg)
        imgs = cfg.getint(chan_id, imgs_key, fallback=0)
        return (True, f"{name_key} — **Mirror Image** ({n} rds remain; images: {imgs}).")

def _char_snapshot(name):
    """Return (hp, mhp, ac, owner_id) from <name>.coe, with safe fallbacks."""
    path = f"{name.replace(' ', '_')}.coe"
    if not os.path.exists(path):
        return (None, None, 11, None)
    cfg = read_cfg(path)
    hp  = getint_compat(cfg, "cur", "hp", fallback=None)
    mhp = getint_compat(cfg, "max", "hp", fallback=None)

    try:
        ac_raw = get_compat(cfg, "stats", "ac", fallback="")
        ac = int(ac_raw) if str(ac_raw).strip() != "" else 11
    except Exception:
        ac = 11
    owner = get_compat(cfg, "info", "owner_id", fallback=None)
    return (hp, mhp, ac, owner)

def _dex_mod_from_char(char_name: str) -> int:
    path = f"{char_name.replace(' ', '_')}.coe"
    if not os.path.exists(path):
        return 0
    cfg = read_cfg(path)
    try:
        if cfg.has_option("stats", "dex_modifier"):
            return int(cfg.get("stats", "dex_modifier"))
    except Exception:
        pass
    dex = getint_compat(cfg, "stats", "dex", fallback=10)
    return (dex - 10) // 2

def _find_ci_name(names: list[str], query: str | None) -> str | None:
    """Return the exact entry from `names` that equals `query` case-insensitively."""
    if not query:
        return None
    q = str(query).strip().lower()
    for n in names:
        if n.lower() == q:
            return n
    return None
    
def _find_ci_or_partial_name(names: list[str], token: str) -> tuple[str | None, list[str]]:
    """
    Returns (resolved_name, ambiguous_matches).
    Exact (case-insensitive) match wins.
    Else try prefix, then substring matches (case-insensitive, space/_ normalized).
    If more than one remains, return (None, candidates).
    """
    def norm(s: str) -> str:
        return (s or "").strip().lower().replace("_", " ")
    q = norm(token)

    for n in names:
        if norm(n) == q:
            return n, []

    pref = [n for n in names if norm(n).startswith(q)]
    if len(pref) == 1:
        return pref[0], []

    sub = [n for n in names if q in norm(n)]
    if len(sub) == 1:
        return sub[0], []

    cand = []
    seen = set()
    for n in pref + sub:
        if n not in seen:
            seen.add(n); cand.append(n)
    return None, cand
    

def _purge_zero_x_effects(cfg, chan_id: str):
    """
    Safety sweep: remove any <slot>.x_* keys that are <= 0 and their metadata.
    (Prevents stale tags like [HA] after expiry.)
    """
    changed = False
    try:
        names, _ = _parse_combatants(cfg, chan_id)
    except Exception:
        names = []

    for nm in names:
        slot = _choose_slot_for_effects(cfg, chan_id, nm)

        for opt_key, _ in list(cfg.items(chan_id)):
            if not opt_key.startswith(f"{slot}.x_"):
                continue
            base_key = opt_key.split(".", 1)[1]

            if any(base_key.endswith(suf) for suf in ("_label","_emoji","_code","_by")):
                continue
            left = max(0, cfg.getint(chan_id, opt_key, fallback=0))
            if left <= 0:

                _clear_x_effect(cfg, chan_id, slot, base_key)
                changed = True
    if changed:
        _save_battles(cfg)

def _slot(name: str) -> str:
    return name.replace(" ", "_")

def _is_tracker_message(msg: nextcord.Message) -> bool:
    try:
        return (msg.author.bot and (msg.content or "").startswith("**EVERYONE ROLL FOR INITIATIVE!**"))
    except Exception:
        return False

def _sorted_names(names, scores, cfg=None, chan_id=None):
    """
    Sort initiative: higher init first, then higher DEX mod, then earlier join order.
    """
    def keyfunc(n):
        base = -scores.get(n, 0)
        dex  = 0
        join = 10**9
        if cfg and chan_id:
            s = _slot(n)
            try:
                dex = -int(cfg.get(chan_id, f"{s}.dex", fallback="0"))
            except Exception:
                dex = 0
            try:
                join = int(cfg.get(chan_id, f"{s}.join", fallback=str(10**9)))
            except Exception:
                join = 10**9
        return (base, dex, join)
    return sorted(names, key=keyfunc)

def _tick_status_counter(cfg, chan_id: str, name_key: str, key: str, label: str) -> tuple[bool, str | None]:
    """
    Decrement <slot>.<key> when this creature's turn begins.
    Returns (still_active, note_text_or_None).
    """
    try:
        slot = _slot(name_key)
    except Exception:
        slot = name_key.replace(" ", "_")

    if _is_perm(cfg, chan_id, slot, key):
        return (True, None)

    opt = f"{slot}.{key}"
    if not cfg.has_option(chan_id, opt):
        return (False, None)

    n = cfg.getint(chan_id, opt, fallback=0)
    n = max(0, n - 1)
    if n <= 0:
        cfg.remove_option(chan_id, opt)
        by_opt = f"{slot}.{key}_by"
        if cfg.has_option(chan_id, by_opt):
            cfg.remove_option(chan_id, by_opt)
        _save_battles(cfg)
        return (False, f"{name_key}’s **{label}** has ended.")
    else:
        cfg.set(chan_id, opt, str(n))
        _save_battles(cfg)
        return (True, None)

def _exploration_next(cfg, chan_id: str) -> tuple[str, bool]:
    """
    Returns (next_name, wraps_to_top).
    Uses current initiative order. Advances from cfg[turn_e] if present,
    otherwise starts at the top without counting it as a wrap.
    """
    names, scores = _parse_combatants(cfg, chan_id)
    if not names:
        return ("", False)

    ordered = _sorted_names(names, scores, cfg, chan_id)
    if not ordered:
        return ("", False)

    cur_e = (cfg.get(chan_id, "turn_e", fallback="") or "").strip()
    if not cur_e or cur_e not in ordered:

        return (ordered[0], False)

    i = ordered.index(cur_e)
    next_i = (i + 1) % len(ordered)
    return (ordered[next_i], next_i == 0)

MONSTER_DIRS = ["monsters", "."]
SAVE_KEYS = ("poi", "wand", "para", "breath", "spell")

def _life_bar(cur: int | None, mx: int | None, width: int = 10, style: str = "block") -> str:
    if cur is None or mx is None or mx <= 0:

        if style == "ascii":
            return "[" + ("?" * width) + "]"
        return "[" + ("?" * width) + "]"
    filled = max(0, min(width, round(width * (cur / mx))))
    if style == "ascii":
        full, empty = "#", "-"
    else:
        full, empty = "█", "░"
    return "[" + (full * filled) + (empty * (width - filled)) + "]"

def _is_monster(char_name: str) -> bool:
    """True if the .coe marks this as a Monster."""
    path = f"{char_name.replace(' ', '_')}.coe"
    if not os.path.exists(path):
        return False
    cfg = read_cfg(path)
    cls = get_compat(cfg, "info", "class", fallback="").strip().lower()
    return cls == "monster"

def _load_monster_template(mon_name: str) -> dict | None:
    """Read monsters/<name>.ini or <name>.ini [base] block -> dict of strings/ints."""
    fn = None
    lc = re.sub(r"\s+", "", mon_name).lower()
    for d in MONSTER_DIRS:
        cand = os.path.join(d, f"{lc}.ini")
        if os.path.exists(cand):
            fn = cand
            break
    if not fn:
        return None

    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(fn)
    if not cp.has_section("base"):
        return None
    out = {}
    for k, v in cp.items("base"):
        v = v.strip()
        if v.lstrip("-").isdigit():
            out[k.lower()] = int(v)
        else:
            out[k.lower()] = v
    return out

from typing import Dict, List, Tuple, Optional

_SAVE_KEY_CANON = {
    "death": "death", "poison": "death", "poi": "death",
    "wand": "wands", "wands": "wands",
    "para": "para", "paralysis": "para", "petrify": "para",
    "breath": "breath", "dragon": "breath",
    "spell": "spells", "spells": "spells",
}

def _find_class_lst() -> Optional[str]:
    """
    Heuristic search for class.lst next to this file / cwd / ./data
    """
    here = os.path.dirname(__file__)
    candidates = [
        os.path.join(here, "class.lst"),
        os.path.join(os.getcwd(), "class.lst"),
        os.path.join(os.getcwd(), "data", "class.lst"),
        "class.lst",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

def load_class_saves(path: Optional[str] = None) -> Dict[str, Dict[str, List[int]]]:
    """
    Parse class.lst and return:
        { normalized_class_name: { "death"|"wands"|"para"|"breath"|"spells": [20 ints] } }
    Only sections that actually have save lines are included.
    """
    if path is None:
        path = _find_class_lst()
    if not path:
        return {}

    cfg = configparser.ConfigParser()

    cfg.optionxform = str.lower
    with open(path, "r", encoding="utf-8") as f:
        cfg.read_file(f)

    out: Dict[str, Dict[str, List[int]]] = {}
    for sect in cfg.sections():
        vals = cfg[sect]
        row = {}

        mapping = {
            "poi": "death",
            "wand": "wands",
            "para": "para",
            "breath": "breath",
            "spell": "spells",
        }
        for src, dst in mapping.items():
            if src in vals:
                nums = [int(x) for x in re.findall(r"-?\d+", vals[src])]
                if not nums:
                    continue

                if len(nums) < 20:
                    nums = nums + [nums[-1]] * (20 - len(nums))
                row[dst] = nums[:20]

        if row:
            nname = _norm_class_name(sect)
            out[nname] = row

    return out

def _parse_saveas(s: str) -> Tuple[str, int]:
    """
    Accepts forms like:
      'Fighter 3', 'cleric   1', 'Magic-User 7', 'MU 2', 'Wizard 5'
    Returns (normalized_class_name, level_int)
    """
    if not s:
        return ("fighter", 1)
    parts = re.findall(r"[A-Za-z\-]+|\d+", s)
    if not parts:
        return ("fighter", 1)

    cls_tokens = []
    lvl = None
    for tok in parts:
        if re.fullmatch(r"\d+", tok):
            lvl = int(tok)
        else:
            cls_tokens.append(tok)
    cls = " ".join(cls_tokens) if cls_tokens else "fighter"
    return (_norm_class_name(cls), max(1, min(20, lvl if lvl is not None else 1)))

def _class_save_target(class_name: str, level: int, vs: str) -> int:
    """
    Look up class/level/vs in loaded tables, with L1 built-in fallback.
    """
    _ensure_tables_loaded()
    vs = _canon_vs(vs)
    cls = _norm_class_name(class_name)

    if _CLASS_SAVES and cls in _CLASS_SAVES and vs in _CLASS_SAVES[cls]:
        arr = _CLASS_SAVES[cls][vs]
        return arr[max(1, min(20, int(level))) - 1]

    if int(level) == 1 and cls in _BUILTIN_L1 and vs in _BUILTIN_L1[cls]:
        return _BUILTIN_L1[cls][vs]

    return _DEFAULT_F1.get(vs, 14)

def _get_saveas_from_cfg(t_cfg) -> Optional[str]:
    """
    Try to pull a 'saveas' directive from the target's config (case-insensitive, multiple sections).
    """
    for sec in ("base", "stats", "info"):
        try:

            try:
                v = get_compat(t_cfg, sec, "saveas", fallback=None)
            except Exception:
                v = t_cfg.get(sec, "saveas", fallback=None) if hasattr(t_cfg, "get") else None
            if v:
                s = str(v).strip()
                if s:
                    return s
        except Exception:
            pass
    return None

def _sorted_entries(cfg, chan_id: str):
    """
    Return list of dicts sorted by: init DESC, dex DESC, join ASC.
    Each entry: {"name","disp","init","dex","join"}
    """
    names, scores = _parse_combatants(cfg, chan_id)
    out = []
    for n in names:
        s = _slot(n)
        out.append({
            "name": n,
            "disp": cfg.get(chan_id, f"{s}.disp", fallback=n),
            "init": cfg.getint(chan_id, n, fallback=0),
            "dex":  cfg.getint(chan_id, f"{s}.dex", fallback=0),
            "join": cfg.getint(chan_id, f"{s}.join", fallback=0),
        })
    out.sort(key=lambda e: (-e["init"], -e["dex"], e["join"]))
    return out

def _format_tracker_block(cfg, chan_id: str):
    if not cfg.has_section(chan_id):
        return "Initiative: — (round 0)\n(no combatants yet)"
    ents = _sorted_entries(cfg, chan_id)
    lines = []
    entries   = _sorted_entries(cfg, chan_id)
    round_no  = cfg.getint(chan_id, "round", fallback=0)
    turn_name = cfg.get(chan_id, "turn", fallback="")

    current_init = "-"
    if entries and turn_name:
        for e in entries:
            if e["name"] == turn_name:
                current_init = e.get("init", e.get("score", "-"))
                break

    header = f"Initiative: {current_init} (round {round_no})"
    if not entries:
        return header + "\n(no combatants yet)"

    def _find_file_ci(nm: str) -> str | None:
        """Find '<name>.coe' case-insensitively."""
        base = (nm or "").replace(" ", "_").lower()
        target = f"{base}.coe"
        for fn in os.listdir("."):
            if fn.lower() == target:
                return fn
        return None

    lines = [header]
    for ent in entries:
        name = ent["name"]
        init_val = ent.get("init", ent.get("score", "-"))

        slot = _choose_slot_for_effects(cfg, chan_id, name)
        perm_codes = _perm_codes_for_slot(cfg, chan_id, slot)
        disp = cfg.get(chan_id, f"{slot}.disp", fallback=name)

        if cfg.has_option(chan_id, f"{slot}.acpen"):
            disp = f"{disp} (–2 AC)"

        prefix = "> " if name == turn_name else "  "

        path = _find_file_ci(disp) or _find_file_ci(name)

        pcfg = None
        if path:
            try:
                pcfg = read_cfg(path)
            except Exception:
                pcfg = None

        tail = ""
        if path:
            try:
                pcfg = read_cfg(path)
                cls = (get_compat(pcfg, "info", "class", fallback="") or "").strip().lower()
                cur_hp = getint_compat(pcfg, "cur", "hp", fallback=None)
                max_hp = getint_compat(pcfg, "max", "hp", fallback=cur_hp)

                if cls == "monster":

                    tail = f" • HP {_life_bar(cur_hp, max_hp, width=10)}"
                else:

                    raw_ac = get_compat(pcfg, "stats", "ac", fallback="")
                    try:
                        ac = int(raw_ac) if str(raw_ac).strip() != "" else 10
                    except Exception:
                        ac = 10
                    tail = f" • HP {cur_hp}/{max_hp} • AC {ac}"
            except Exception:
                pass

        par = cfg.getint(chan_id, f"{slot}.paralyzed", fallback=0)
        if par > 0:
            ccq = cfg.getint(chan_id, f"{slot}.cc_blind_pending", fallback=0)
            csq = cfg.getint(chan_id, f"{slot}.cs_blind_pending", fallback=0)
            spq = cfg.getint(chan_id, f"{slot}.sp_blind_pending", fallback=0)
            pend = max(ccq, csq, spq)
            tail += f" • [PAR {par} → BL {pend}]" if pend > 0 else f" • [PAR {par}]"

        pet = cfg.getint(chan_id, f"{slot}.pet", fallback=0)
        pet_perm = cfg.getint(chan_id, f"{slot}.pet_perm", fallback=0)
        if pet > 0 or pet_perm:
            tail += " • [PET –]"

        for key, code in (
            ("x_detectmagic_perm",        "DM"),
            ("x_protectionfromevil_perm", "PF"),
            ("x_readlanguages_perm",      "RL"),
            ("x_readmagic_perm",          "RM"),
            ("x_detectinvisible_perm",    "DI"),
            ("x_fly_perm",                "FL"),
        ):
            if cfg.getint(chan_id, f"{slot}.{key}", fallback=0) > 0:
                tail += f" • [{code} –]"

        try:
            sh = cfg.getint(chan_id, f"{slot}.shield", fallback=0)
        except Exception:
            sh = 0
        if sh and sh > 0:
            tail += f" • [SH {sh}]"

        try:
            mi_imgs = cfg.getint(chan_id, f"{slot}.mi_images", fallback=0)
        except Exception:
            mi_imgs = 0
        if mi_imgs > 0:
            tail += f" • [MI {mi_imgs}]"

        try:
            mw = cfg.getint(chan_id, f"{slot}.magwep", fallback=0)
        except Exception:
            mw = 0
        if mw and mw > 0:
            tail += f" • [MW {mw}]"

        try:
            nl = getint_compat(pcfg, "cur", "neg_levels", fallback=0)
        except Exception:
            nl = 0
        if nl > 0:
            tail += f" • [NL {nl}]"

        try:
            dis = getint_compat(pcfg, "cur", "disease", fallback=0)
        except Exception:
            dis = 0
        if dis > 0:
            tail += " • [DIS]"

        try:
            sph = cfg.getint(chan_id, f"{slot}.sph", fallback=0)
        except Exception:
            sph = 0
        if sph > 0:
            try:
                bon = cfg.getint(chan_id, f"{slot}.sph_bonus", fallback=0)
            except Exception:
                bon = 0
            tail += f" • [SW {sph}{('+'+str(bon)) if bon else ''}]"

        try:
            swd = cfg.getint(chan_id, f"{slot}.swd", fallback=0)
        except Exception:
            swd = 0
        if swd > 0:
            tail += f" • [SWD {swd}]"

        try:
            ghh = cfg.getint(chan_id, f"{slot}.ghh", fallback=0)
        except Exception:
            ghh = 0
        if ghh > 0:
            tail += f" • [GH {ghh}]"

        try:
            gf = cfg.getint(chan_id, f"{slot}.ghf", fallback=0)
        except Exception:
            gf = 0
        if gf:
            tail += " • [GF]"

        try:
            ma = cfg.getint(chan_id, f"{slot}.magearmor", fallback=0)
        except Exception:
            ma = 0
        if ma > 0:
            tail += f" • [MA {ma}]"

        try:
            bs = cfg.getint(chan_id, f"{slot}.boneskin", fallback=0)
        except Exception:
            bs = 0
        if bs > 0:
            tail += f" • [BS {bs}]"

        try:
            if pcfg and _pc_has_regen_item(pcfg):
                tail += " • [RG –]"
        except Exception:
            pass


        try:
            sl = cfg.getint(chan_id, f"{slot}.shl", fallback=0)
        except Exception:
            sl = 0
        if sl > 0:
            try:
                slb = cfg.getint(chan_id, f"{slot}.shl_dmg", fallback=1)
            except Exception:
                slb = 1

            tail += f" • [SL {sl}+1]"

        now_r = cfg.getint(chan_id, "round", fallback=0)

        ko_left  = cfg.getint(chan_id, f"{slot}.ko", fallback=0)
        ko_ready = cfg.getint(chan_id, f"{slot}.ko_ready", fallback=0)

        if ko_left > 0:

            if ko_ready and now_r < ko_ready:
                tail +=f" [KO {ko_ready - now_r}]"
            else:
                tail +=" [KO ✓]"

        bl_left = cfg.getint(chan_id, f"{slot}.blind", fallback=0)
        bl_perm = cfg.getint(chan_id, f"{slot}.blind_perm", fallback=0)
        if bl_left > 0 or bl_perm:
            bl_src = (cfg.get(chan_id, f"{slot}.blind_src", fallback="") or "").lower()
            src_code = ("C"  if bl_src == "colorspray" else
                        "CC" if bl_src == "colorcloud" else
                        "SP" if bl_src == "scintillating"  else
                        "L"  if bl_src == "light" else
                        "CL" if bl_src == "clight" else
                        "D"  if bl_src == "darkness" else
                        "CD" if bl_src == "cdarkness" else "")
            amt = "–" if bl_perm else str(bl_left)
            tail += f" • [BL {src_code+' ' if src_code else ''}{amt}]"

        lt = cfg.getint(chan_id, f"{slot}.light", fallback=0)
        lt_perm = cfg.getint(chan_id, f"{slot}.light_perm", fallback=0)
        if lt > 0 or lt_perm:
            tail += f" • [LT {'–' if lt_perm else lt}]"

        dk = cfg.getint(chan_id, f"{slot}.darkness", fallback=0)
        dk_perm = cfg.getint(chan_id, f"{slot}.darkness_perm", fallback=0)
        if dk > 0 or dk_perm:
            tail += f" • [DK {'–' if dk_perm else dk}]"

        heldby = (cfg.get(chan_id, f"{slot}.heldby", fallback="") or "").strip()
        if heldby:
            tail += " • [HELD]"

        try:
            web_left = cfg.getint(chan_id, f"{slot}.web", fallback=0)
        except Exception:
            web_left = 0
        if web_left > 0:
            st  = (cfg.get(chan_id, f"{slot}.web_state",  fallback="E") or "E").upper()
            ign =  cfg.get(chan_id, f"{slot}.webignite",   fallback="") == "1"
            stc = "T" if st == "T" else "E"
            tail += f" • [WEB {stc} {web_left}{'🔥' if ign else ''}]"

        if cfg.has_option(chan_id, f"{slot}.x_heatmetal"):
            stg = cfg.getint(chan_id, f"{slot}.x_heatmetal", fallback=0)

            show = max(1, stg - 1) if name == turn_name else stg
            tail += f" • [HM {show}/7]"

        if cfg.has_option(chan_id, f"{slot}.x_chillmetal"):
            stg = cfg.getint(chan_id, f"{slot}.x_chillmetal", fallback=0)
            show = max(1, stg - 1) if name == turn_name else stg
            tail += f" • [CM {show}/7]"

        try:
            sv = cfg.getint(chan_id, f"{slot}.x_slowvenom", fallback=0)
        except Exception:
            sv = 0
        if sv > 0:
            tail += f" • [SV {sv}]"

        try:
            fv = cfg.getint(chan_id, f"{slot}.x_fastvenom", fallback=0)
        except Exception:
            fv = 0
        if fv > 0:
            tail += f" • [FV {fv}]"

        cc_left = cfg.getint(chan_id, f"{slot}.cc", fallback=0)
        if cc_left > 0:
            tail += f" • [CC {cc_left}]"

        ck_left = cfg.getint(chan_id, f"{slot}.ck", fallback=0)
        if ck_left > 0:
            tail += f" • [CK {ck_left}]"

        sc_left = cfg.getint(chan_id, f"{slot}.sc", fallback=0)
        if sc_left > 0:
            tail += f" • [SC {sc_left}]"

        try:
            chl = cfg.getint(chan_id, f"{slot}.chl", fallback=0)
        except Exception:
            chl = 0
        if chl > 0:
            tail += f" • [CHL {chl}]"

        try:
            cr = cfg.getint(chan_id, f"{slot}.cr", fallback=0)
        except Exception:
            cr = 0
        if cr > 0:
            tail += f" • [CR {cr}]"

        try:
            mg = cfg.getint(chan_id, f"{slot}.maggots", fallback=0)
        except Exception:
            mg = 0
        if mg > 0:
            tail += f" • [MG {mg}]"

        try:
            stn = cfg.getint(chan_id, f"{slot}.stench", fallback=0)
        except Exception:
            stn = 0
        if stn > 0:
            tail += f" • [STN {stn}]"

        try:
            hyp = cfg.getint(chan_id, f"{slot}.hyp", fallback=-999)
        except Exception:
            hyp = -999
        if hyp != -999:
            if hyp > 0:
                tail += f" • [HYP {hyp}]"
            else:
                tail += " • [HYP]"

        fr = cfg.getint(chan_id, f"{slot}.fear", fallback=0)
        if fr > 0:
            tail += f" • [FR {fr}]"

        try:
            inv_perm = cfg.getint(chan_id, f"{slot}.inv_perm", fallback=0)
        except Exception:
            inv_perm = 0
        inv_left = cfg.getint(chan_id, f"{slot}.inv", fallback=0)
        if inv_perm:
            tail += " • [INV –]"
        elif inv_left > 0:
            tail += f" • [INV {inv_left}]"

        ps_left = cfg.getint(chan_id, f"{slot}.ps", fallback=0)
        if ps_left > 0:
            tail += f" • [PS {ps_left}]"

        try:
            cur_perm = cfg.getint(chan_id, f"{slot}.curse_perm", fallback=0)
            cur_left = cfg.getint(chan_id, f"{slot}.curse",      fallback=0)
        except Exception:
            cur_perm = cur_left = 0
        if cur_perm or cur_left > 0:
            tail += " • [BC –]"

        try:
            fb_perm = cfg.getint(chan_id, f"{slot}.feeble_perm", fallback=0)
            fb_left = cfg.getint(chan_id, f"{slot}.feeble",      fallback=0)
        except Exception:
            fb_perm = fb_left = 0
        if fb_perm or fb_left > 0:
            tail += " • [FB –]"

        try:
            if pcfg and str(get_compat(pcfg, "poly", "active", fallback="0")).strip() == "1":
                knd = (get_compat(pcfg, "poly", "kind", fallback="") or "").strip().lower()
                if knd == "other":
                    form = (get_compat(pcfg, "poly", "form", fallback="") or "").strip()
                    tail += f" • [PO –{(' '+form) if form else ''}]"

        except Exception:
            pass

        try:
            cl_left = cfg.getint(chan_id, f"{slot}.cl", fallback=0)
        except Exception:
            cl_left = 0
        if cl_left > 0:
            try:
                cl_bolts = cfg.getint(chan_id, f"{slot}.cl_bolts", fallback=0)
            except Exception:
                cl_bolts = 0
            try:
                cl_die = (cfg.get(chan_id, f"{slot}.cl_die", fallback="3d6") or "3d6").strip()
            except Exception:
                cl_die = "3d6"

            if cl_die == "3d8":
                tail += f" • [CL {cl_left}/{cl_bolts} 3d8]"
            else:
                tail += f" • [CL {cl_left}/{cl_bolts}]"

        _ss_normalize(cfg, chan_id, slot)
        ss_dur, ss_hp = _ss_state(cfg, chan_id, slot)
        if ss_hp > 0:
            tail += f" • [SS {ss_hp}]"

        try:
            cn_perm = cfg.getint(chan_id, f"{slot}.cn_perm", fallback=0)
        except Exception:
            cn_perm = 0
        cn_left = cfg.getint(chan_id, f"{slot}.cn", fallback=0)
        if cn_perm:
            tail += " • [CN –]"
        elif cn_left > 0:
            tail += f" • [CN {cn_left}]"

        db_left = cfg.getint(chan_id, f"{slot}.db", fallback=0)
        if db_left > 0:
            tail += f" • [DB {db_left}]"

        inw = cfg.getint(chan_id, f"{slot}.inw", fallback=0)
        if inw > 0:
            tail += f" • [INW {inw}]"

        pnm = cfg.getint(chan_id, f"{slot}.pnm", fallback=0)
        if pnm > 0:
            tail += f" • [PNM {pnm}]"

        gas_left = cfg.getint(chan_id, f"{slot}.gas", fallback=0)
        if gas_left > 0:
            tail += f" • [GAS {gas_left}]"

        tail += _rotgrubs_badge(cfg, chan_id, slot)

        try:
            for opt_key, _val in list(cfg.items(chan_id)):
                if not opt_key.startswith(f"{slot}.x_"):
                    continue
                base_key = opt_key.split(".", 1)[1]
                if any(base_key.endswith(suf) for suf in ("_label","_emoji","_code","_by","_perm")):
                    continue

                if base_key in _X_SKIP_GENERIC:
                    continue

                raw_left = cfg.getint(chan_id, opt_key, fallback=0)
                left     = max(0, raw_left)

                code  = (cfg.get(chan_id, f"{slot}.{base_key}_code",  fallback="") or "").strip()
                label = (cfg.get(chan_id, f"{slot}.{base_key}_label", fallback="") or "").strip()

                display_code = "" if code.lower() == "perm" else code

                tag = (display_code or label or base_key[2:4].upper()).upper()

                if tag in perm_codes:
                    continue

                is_perm_flag = cfg.getint(chan_id, f"{slot}.{base_key}_perm", fallback=0) > 0
                is_perm      = (raw_left < 0) or is_perm_flag or (code.lower() == "perm")

                if is_perm:
                    tail += f" • [{tag} –]"
                elif left > 0:
                    tail += f" • [{tag} {left}]"
                else:
                    tail += f" • [{tag}]"
        except Exception:
            pass

        try:
            eff_str = getint_compat(pcfg, "stats", "str", fallback=None)
            str_temp = getint_compat(pcfg, "cur", "str_loss_temp", fallback=0)
        except Exception:
            eff_str = None
            str_temp = 0

        if str_temp > 0:
            tail += f" • [STR–{str_temp}]"
        if eff_str is not None and eff_str <= 2:
            tail += " • [COLLAPSED]"

        lines.append(f"{prefix}{str(init_val):>2}: {disp}{tail}")

    return "\n".join(lines)

def _add_oil_burn(cfg, chan_id: str, name_key: str, stacks: int = 1):
    """
    Queue burning-oil damage for the initiative entry 'name_key'.
    Stores a small integer counter at <slot>.oil (1d8 fire per stack at start of that entry's next turn).
    """
    s = _slot(name_key)
    key = f"{s}.oil"
    try:
        cur = cfg.getint(chan_id, key, fallback=0)
    except Exception:
        cur = 0
    cfg.set(chan_id, key, str(cur + max(1, stacks)))
    _save_battles(cfg)

def _resolve_char_ci_local(name: str):
    base = name.replace(" ", "_")
    want = f"{base}.coe".lower()
    for fn in os.listdir("."):
        if fn.lower() == want:
            path = fn
            try:
                cfg = read_cfg(path)
                real = get_compat(cfg, "info", "name", fallback=None)
                return (real or fn[:-4].replace("_", " ")), path
            except Exception:
                return fn[:-4].replace("_", " "), path
    return None, None

def _add_maggots(cfg, chan_id: str, name_key: str, stacks: int = 1, die: str = "1d3"):
    """
    Start/refresh maggots on 'name_key'.
    'stacks' here means *rounds remaining* (e.g., caster level).
    Deals <die> once per target at the start of their turn until it reaches 0,
    or until burned off by start-of-turn fire.
    """
    s = _slot(name_key)
    k = f"{s}.maggots"
    try:
        cur = cfg.getint(chan_id, k, fallback=0)
    except Exception:
        cur = 0

    cfg.set(chan_id, k, str(max(cur, max(1, stacks))))
    if die:
        cfg.set(chan_id, f"{s}.maggots_die", str(die))
    _save_battles(cfg)

def _defense_sets(t_cfg):
    """Read resist/reduce1/immune token sets (case-insensitive) from [stats] or [base]."""
    def _split(v):
        return {t for t in re.split(r"[,\s]+", str(v or "").lower().strip()) if t}
    resist  = _split(get_compat(t_cfg, "stats", "resist",  fallback="")) | _split(get_compat(t_cfg, "base", "resist",  fallback=""))
    reduce1 = _split(get_compat(t_cfg, "stats", "reduce1", fallback="")) | _split(get_compat(t_cfg, "base", "reduce1", fallback=""))
    immune  = _split(get_compat(t_cfg, "stats", "immune",  fallback="")) | _split(get_compat(t_cfg, "base", "immune",  fallback=""))
    return resist, reduce1, immune

def _mitigate_fire(dmg: int, t_cfg, *, weapon_name="Fire", chan_id=None, target_name=None):
    """
    Fire mitigation shim for initiative ticks.
    Delegates to combat engine's _apply_mitigation so absorb=fire heals 1/3, etc.
    """
    if dmg <= 0:
        return 0, ""
    final, note = _apply_mitigation(
        dmg,
        weapon_name=weapon_name,
        weapon_type="fire",
        t_cfg=t_cfg,
        chan_id=chan_id,
        target_name=target_name,
    )
    return final, note

def _is_tracker_message(msg) -> bool:
    try:
        txt = (msg.content or "").strip()
        if not txt:
            return False
        if not txt.startswith("**EVERYONE ROLL FOR INITIATIVE!**"):
            return False

        return "```text" in txt and "```" in txt
    except Exception:
        return False


def _resist_protection_tags(cfg, chan_id: str, slot: str) -> str:
    """Shows legacy non-x_* keys for cold/fire/lightning so they appear in UIs."""
    bits = []

    rc = cfg.getint(chan_id, f"{slot}.rc", fallback=0)
    if rc > 0:
        bits.append(f"[RC {rc}]")

    rf = cfg.getint(chan_id, f"{slot}.rf", fallback=0)
    if rf > 0:
        bits.append(f"[RF {rf}]")

    pfi = cfg.getint(chan_id, f"{slot}.pfi", fallback=0) or cfg.getint(chan_id, f"{slot}.pfire", fallback=0)
    if pfi > 0:
        bits.append(f"[FI {pfi}]")

    pl = cfg.getint(chan_id, f"{slot}.pl", fallback=0) or cfg.getint(chan_id, f"{slot}.plight", fallback=0)
    if pl > 0:
        bits.append(f"[PL {pl}]")

    return (" • " + " • ".join(bits)) if bits else ""


class Initiative(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _update_tracker_message(self, ctx, cfg=None, chan_id=None):
        """
        Update the single pinned initiative tracker in this channel.

        Pinning rules (Option B):
          • If message_id is valid → edit only (never pin).
          • If message_id is invalid → try to find a pinned tracker and edit it, normalize message_id.
          • Only if none found → create + pin one tracker and unpin other duplicate trackers.
        """
        try:
            if cfg is None:
                cfg = _load_battles()
            if chan_id is None:
                try:
                    chan_id = _section_id(ctx.channel)
                except Exception:
                    chan_id = str(ctx.channel.id)

            if not cfg.has_section(chan_id):
                return

            block = _format_tracker_block(cfg, chan_id)
            content = "**EVERYONE ROLL FOR INITIATIVE!**\n```text\n" + block + "\n```"

            msg_id = cfg.getint(chan_id, "message_id", fallback=0)

            if msg_id:
                try:
                    msg = await ctx.channel.fetch_message(msg_id)
                    await msg.edit(content=content)
                    return
                except Exception:

                    pass

            try:
                pins = await ctx.channel.pins()
                candidate = None
                for pm in pins:

                    if _is_tracker_message(pm):
                        candidate = pm
                        break

                if candidate:
                    await candidate.edit(content=content)
                    cfg.set(chan_id, "message_id", str(candidate.id))
                    _save_battles(cfg)

                    try:
                        for other in pins:
                            if other.id != candidate.id and _is_tracker_message(other):
                                try:
                                    await other.unpin(reason="Duplicate initiative tracker")
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    return
            except Exception:

                pass

            new_msg = await ctx.send(content)
            try:
                await new_msg.pin(reason="Initiative tracker")
            except Exception:
                pass

            cfg.set(chan_id, "message_id", str(new_msg.id))
            _save_battles(cfg)

            try:
                pins = await ctx.channel.pins()
                for pm in pins:
                    if pm.id != new_msg.id and _is_tracker_message(pm):
                        try:
                            await pm.unpin(reason="Replacing duplicate tracker")
                        except Exception:
                            pass
            except Exception:
                pass

        except Exception:

            pass

    def _chan_id(ctx):
        try:
            return _section_id(ctx.channel)
        except Exception:
            return str(ctx.channel.id)

    def _slot_safe(name: str) -> str:
        try:
            return _slot(name)
        except Exception:
            return name.replace(" ", "_")

    @staticmethod
    def _ci_key(cfg, chan_id: str, display_name: str) -> str:
        names, _ = _parse_combatants(cfg, chan_id)
        return _find_ci_name(names, (display_name or "").strip()) or display_name

    def _tick_start_of_turn(self, cfg, chan_id: str, actor_name: str) -> bool:
        """
        Start-of-turn housekeeping for a single actor.
        Decrements per-round timers and clears one-round AC flags.
        Returns True if anything changed (so caller can refresh tracker).
        """
        try:
            if not cfg.has_section(chan_id):
                return False

            try:
                names, _ = _parse_combatants(cfg, chan_id)
            except Exception:
                names = []
            key = _find_ci_name(names, (actor_name or "").strip()) or (actor_name or "").strip()

            candidates = []
            try:
                candidates.append(_slot(key))
            except Exception:
                pass
            candidates.append(key.replace(" ", "_"))

            timer_tags = (
               "magwep","ghh","magearmor","boneskin","sph","swd","shl","chl","cr","gcr","light","darkness",
               "blind","paralyzed","cc","cc_blind_pending","cs_blind_pending","sp_blind_pending","sp_cnf_pending",
               "ck","hyp","cl","db","inw","pnm","gas","ps","sc"
             )

            slot = None
            for s in candidates:
                if any(cfg.has_option(chan_id, f"{s}.{t}") for t in timer_tags):
                    slot = s
                    break
            slot = slot or candidates[0]

            changed = False

            for opt in (f"{slot}.acpen", f"{slot}.acbuf"):
                if cfg.has_option(chan_id, opt):
                    cfg.remove_option(chan_id, opt)
                    changed = True

            def dec(base: str, extra_clear: tuple[str, ...] = (), perm_flag: str | None = None) -> None:
                nonlocal changed
                key_base = f"{slot}.{base}"

                if _is_perm(cfg, chan_id, slot, base):
                    return

                if perm_flag and cfg.getint(chan_id, f"{slot}.{perm_flag}", fallback=0) > 0:
                    return

                left = cfg.getint(chan_id, key_base, fallback=0)
                if left > 0:
                    left = max(0, left - 1)
                    cfg.set(chan_id, key_base, str(left))
                    changed = True
                    if left == 0:
                        for opt in extra_clear:
                            if cfg.has_option(chan_id, opt):
                                cfg.remove_option(chan_id, opt)

                        if perm_flag:
                            opt = f"{slot}.{perm_flag}"
                            if cfg.has_option(chan_id, opt):
                                cfg.remove_option(chan_id, opt)

            dec("magwep",   (f"{slot}.magwep_name", f"{slot}.magwep_disp"))
            dec("ghh")
            dec("magearmor", (f"{slot}.magearmor_by",))
            dec("boneskin",  (f"{slot}.boneskin_by",))
            dec("sph",       (f"{slot}.sph_bonus",))
            dec("swd"),
            dec("shl",       (f"{slot}.shl_hit", f"{slot}.shl_dmg", f"{slot}.shl_die"))
            dec("light",     (f"{slot}.light_by", f"{slot}.light_level"),       "light_perm")
            dec("darkness",  (f"{slot}.dark_by",  f"{slot}.dark_level"),        "darkness_perm")
            dec("blind",     (f"{slot}.blind_src", f"{slot}.blind_by", f"{slot}.blind_level"), "blind_perm")
            dec("cc",        (f"{slot}.cc_by", f"{slot}.cc_level"))
            dec("ck",        (f"{slot}.ck_by", f"{slot}.ck_level"))
            dec("cl", (f"{slot}.cl_by", f"{slot}.cl_die", f"{slot}.cl_bolts", f"{slot}.cl_last_round"))
            dec("cn", (f"{slot}.cn_by", f"{slot}.cn_last", f"{slot}.cn_tar", f"{slot}.cn_tar_by"))
            dec("db")
            dec("inw")
            dec("pnm")
            dec("fear", (f"{slot}.fear_by", f"{slot}.fear_src"))
            dec("inv")
            dec("gas", (f"{slot}.gas_by", f"{slot}.gas_ac_hint"))
            dec("sc", (f"{slot}.sc_by", f"{slot}.sc_level"))
            dec("chl", (f"{slot}.chl_charges",))
            dec("cr")
            dec("gcr")
            dec("stench")

            try:

                for opt_key, _val in list(cfg.items(chan_id)):
                    if not opt_key.startswith(f"{slot}.x_"):
                        continue
                    base_key = opt_key.split(".", 1)[1]

                    if base_key in _X_SKIP_GENERIC:
                        continue

                    if any(base_key.endswith(suf) for suf in ("_label","_emoji","_code","_by")):
                        continue

                    if _is_perm(cfg, chan_id, slot, base_key):
                        continue

                    left = cfg.getint(chan_id, opt_key, fallback=0)
                    if left <= 0:
                        continue
                    new_left = left - 1
                    changed = True
                    if new_left <= 0:
                        if cfg.has_option(chan_id, opt_key):
                            cfg.remove_option(chan_id, opt_key)
                        for suf in ("_label","_emoji","_code","_by"):
                            mkey = f"{slot}.{base_key}{suf}"
                            if cfg.has_option(chan_id, mkey):
                                cfg.remove_option(chan_id, mkey)
                    else:
                        cfg.set(chan_id, opt_key, str(new_left))

            except Exception:
                pass

            try:
                hyp_key = f"{slot}.hyp"
                if cfg.has_option(chan_id, hyp_key):
                    hyp_left = cfg.getint(chan_id, hyp_key, fallback=0)
                    if hyp_left > 0:
                        newv = hyp_left - 1
                        if newv <= 0:

                            cfg.remove_option(chan_id, hyp_key)
                            if cfg.has_option(chan_id, f"{slot}.hyp_by"):
                                cfg.remove_option(chan_id, f"{slot}.hyp_by")
                            changed = True
                        else:
                            cfg.set(chan_id, hyp_key, str(newv))
                            changed = True

            except Exception:
                pass

            try:
                par_left = cfg.getint(chan_id, f"{slot}.paralyzed", fallback=0)
                if par_left <= 0:
                    applied, _src, _by = _apply_queued_blind_after_paralysis(cfg, chan_id, slot)
                    if applied > 0:
                        changed = True
            except Exception:
                pass

            if changed:
                _save_battles(cfg)
            return changed
        except Exception:
            return False

    async def _process_start_of_turn_effects(self, ctx, cfg, chan_id: str, turn_name: str) -> bool:
        """
        Apply start-of-turn effects to the current turn holder:
          • Spiritual Hammer tick (existing behavior)
          • Burning Oil (existing behavior; now also flags if fire hit)
          • Maggots: deal damage per stack at start of the victim's turn; burn off if fire hit
        Returns True if the turn holder (and a MONSTER) died and was removed.
        """
        global os
        global nextcord
        global random

        if not turn_name:
            return False

        next_after = ""
        try:
            names, _ = _parse_combatants(cfg, chan_id)
            key = _find_ci_name(names, turn_name) or turn_name
            if key in names and len(names) > 1:
                idx = names.index(key)
                next_after = names[(idx + 1) % len(names)]
        except Exception:
            next_after = ""

        slot = _slot(turn_name)

        died_monster_overall = False
        any_effect_applied = False
        fire_hit_this_start = False
        acid_hit_this_start = False

        try:
            sph_val = cfg.getint(chan_id, f"{slot}.sph", fallback=0)
        except Exception:
            sph_val = 0
        if sph_val > 0:
            newv = sph_val - 1
            cfg.set(chan_id, f"{slot}.sph", str(newv))
            if newv <= 0:
                for k in ("sph", "sph_bonus"):
                    opt = f"{slot}.{k}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)
            _save_battles(cfg)

        oil_key = f"{slot}.oil"
        if cfg.has_option(chan_id, oil_key):
            any_effect_applied = True
            stacks = cfg.getint(chan_id, oil_key, fallback=0)

            cfg.remove_option(chan_id, oil_key)
            _save_battles(cfg)

            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)
            if not path or not os.path.exists(path):
                await ctx.send(f"🔥 {disp}: start-of-turn oil could not find a character file.")
            else:
                t_cfg = read_cfg(path)

                rolls = [random.randint(1, 8) for _ in range(max(1, stacks))]
                raw_total = sum(rolls)
                final_total, note = _apply_mitigation(
                    raw_total,
                    weapon_name="Oil",
                    weapon_type="fire",
                    t_cfg=t_cfg,
                    chan_id=chan_id,
                    target_name=disp,
                )

                old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                mhp    = getint_compat(t_cfg, "max", "hp", fallback=max(1, old_hp))
                new_hp = (max(0, old_hp - final_total) if final_total >= 0 else min(mhp, old_hp - final_total))
                if not t_cfg.has_section("cur"):
                    t_cfg.add_section("cur")
                t_cfg["cur"]["hp"] = str(new_hp)
                write_cfg(path, t_cfg)

                if final_total > 0:
                    fire_hit_this_start = True

                is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                segs = ", ".join(str(r) for r in rolls)
                desc = f"1d8 × {max(1, stacks)}: [{segs}] = **{raw_total}**"

                if note:
                    if final_total < 0 and "heals" in note.lower():
                        desc += f"  *({note})*"
                    else:
                        desc += f" → **{final_total}**  *({note})*"
                else:
                    amt_txt = (f"**heals {abs(final_total)}**" if final_total < 0 else f"**{final_total}**")
                    desc += f" → {amt_txt}"

                if is_mon:
                    mhp2 = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                    before = _life_bar(old_hp, mhp2, width=10)
                    after  = _life_bar(new_hp, mhp2, width=10)
                    desc += f"\nHP {before} → **{after}**"
                else:
                    desc += f"\nHP {old_hp} → **{new_hp}**"

                if new_hp <= 0:
                    desc += "  ☠️ **DEAD!**"

                await ctx.send(embed=nextcord.Embed(
                    title=f"🔥 Burning Oil: {disp} takes damage",
                    description=desc,
                    color=random.randint(0, 0xFFFFFF)
                ))

                if new_hp <= 0 and is_mon:
                    died_monster_overall = True
                    try:
                        names, scores = _parse_combatants(cfg, chan_id)
                        key = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                        if key in names:
                            names = [n for n in names if n != key]
                            if cfg.has_option(chan_id, key): cfg.remove_option(chan_id, key)
                            s = _slot(key)
                            for suf in (".dex", ".join", ".disp", ".acpen", ".oil"):
                                opt = f"{s}{suf}"
                                if cfg.has_option(chan_id, opt):
                                    cfg.remove_option(chan_id, opt)
                            _write_combatants(cfg, chan_id, names, scores); _save_battles(cfg)
                        try: os.remove(os.path.abspath(path))
                        except Exception: pass
                    except Exception:
                        pass

        for key, label, emoji, wtype in (
            ("x_heatmetal",  "Heat Metal",  "♨️", "fire"),
            ("x_chillmetal", "Chill Metal", "❄️", "cold"),
        ):
            try:
                stage = cfg.getint(chan_id, f"{slot}.{key}", fallback=0)
            except Exception:
                stage = 0

            if stage > 0:
                any_effect_applied = True
                disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
                _, path = _resolve_char_ci_local(turn_name)

                dice = None
                if stage in (2, 3):
                    dice = "1d4"
                elif stage in (4, 5):
                    dice = "2d4"
                elif stage == 6:
                    dice = "1d4"

                if not path or not os.path.exists(path):
                    await ctx.send(f"{emoji} {disp}: {label.lower()} tick but no character file was found.")
                else:
                    t_cfg = read_cfg(path)

                    total_applied = 0
                    rolls = []
                    note = ""
                    if dice:
                        s, rolls, flat = roll_dice(dice)
                        raw = s + flat
                        total_applied, note = _apply_mitigation(
                            raw,
                            weapon_name=label,
                            weapon_type=wtype,
                            is_magical=True,
                            t_cfg=t_cfg,
                            chan_id=chan_id,
                            target_name=disp,
                        )

                        old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                        mhp    = getint_compat(t_cfg, "max", "hp", fallback=max(1, old_hp))
                        new_hp = (max(0, old_hp - total_applied) if total_applied >= 0 else min(mhp, old_hp - total_applied))
                        if not t_cfg.has_section("cur"):
                            t_cfg.add_section("cur")
                        t_cfg["cur"]["hp"] = str(new_hp)
                        write_cfg(path, t_cfg)

                        if wtype == "fire" and total_applied > 0:
                            fire_hit_this_start = True

                        is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                        if is_mon:
                            mhp2 = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                            before = _life_bar(old_hp, mhp2, width=10)
                            after  = _life_bar(new_hp, mhp2, width=10)
                            hp_line = f"{before} → **{after}**"
                        else:
                            hp_line = f"{old_hp} → **{new_hp}**"

                        dmg_line = f"{dice} [{', '.join(str(r) for r in rolls)}]"
                        if note:
                            if total_applied < 0 and "heals" in note.lower():
                                dmg_line += f"  *({note})*"
                            else:
                                dmg_line += f" → **{total_applied}**  *({note})*"
                        else:
                            amt_txt = (f"**heals {abs(total_applied)}**" if total_applied < 0 else f"**{total_applied}**")
                            dmg_line += f" → {amt_txt}"

                        rem_after = max(0, 7 - stage)
                        desc = f"{dmg_line}\nHP {hp_line}\n_(round {stage}/7; {rem_after} remaining)_"
                        if new_hp <= 0:
                            desc += "  ☠️ **DEAD!**"

                        await ctx.send(embed=nextcord.Embed(
                            title=f"{emoji} {label}: {disp} takes damage",
                            description=desc,
                            color=random.randint(0, 0xFFFFFF)
                        ))

                        if new_hp <= 0 and is_mon:
                            died_monster_overall = True
                            try:
                                names, scores = _parse_combatants(cfg, chan_id)
                                keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                                if keyname in names:
                                    names = [n for n in names if n != keyname]
                                    if cfg.has_option(chan_id, keyname):
                                        cfg.remove_option(chan_id, keyname)
                                    s = _slot(keyname)
                                    for suf in (".dex",".join",".disp",".acpen",".oil",
                                                ".x_swarm",".x_swarm_code",".x_swarm_label",".x_swarm_emoji",".x_swarm_stat",".x_swarm_torch",
                                                ".maggots",".maggots_die",".x_maggots",".x_maggots_dmg",".x_maggots_label",".x_maggots_emoji",".x_maggots_code",".x_maggots_by",
                                                ".x_tentacles",".x_tentacles_dmg",".x_tentacles_label",".x_tentacles_emoji",".x_tentacles_code",".x_tentacles_by",
                                                ".heldby",
                                                ".x_heatmetal",".x_chillmetal"):
                                        opt = f"{s}{suf}"
                                        if cfg.has_option(chan_id, opt):
                                            cfg.remove_option(chan_id, opt)
                                    _write_combatants(cfg, chan_id, names, scores); _save_battles(cfg)
                                try: os.remove(os.path.abspath(path))
                                except Exception: pass
                            except Exception:
                                pass

                    next_stage = stage + 1
                    if next_stage <= 7:
                        cfg.set(chan_id, f"{slot}.{key}", str(next_stage))
                        _save_battles(cfg)

                        if not dice:
                            rem_after = max(0, 7 - stage)
                            await ctx.send(embed=nextcord.Embed(
                                title=f"{emoji} {label}: {disp}",
                                description=f"(no damage this round — stage {stage}/7; **{rem_after}** remaining)",
                                color=random.randint(0, 0xFFFFFF)
                            ))
                    else:

                        if cfg.has_option(chan_id, f"{slot}.{key}"):
                            cfg.remove_option(chan_id, f"{slot}.{key}")
                        _save_battles(cfg)
                        await ctx.send(embed=nextcord.Embed(
                            title=f"{emoji} {label}: {disp}",
                            description="Effect fades.",
                            color=random.randint(0, 0xFFFFFF)
                        ))

        try:
            x_dis = cfg.get(chan_id, f"{slot}.x_dissolve", fallback="")
        except Exception:
            x_dis = ""
        if str(x_dis).strip() != "":
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)

            die = (cfg.get(chan_id, f"{slot}.x_dissolve_dice", fallback="1d6") or "1d6").strip()
            if not path or not os.path.exists(path):
                await ctx.send(f"🧪 {disp}: acid tick but no character file was found.")
            else:
                t_cfg = read_cfg(path)

                s, rolls, flat = roll_dice(die)
                raw = s + flat
                final, note = _apply_mitigation(raw, weapon_name="Dissolving Acid", weapon_type="acid", t_cfg=t_cfg)

                if final > 0:
                    acid_hit_this_start = True
                old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                new_hp = max(0, old_hp - final)
                if not t_cfg.has_section("cur"):
                    t_cfg.add_section("cur")
                t_cfg["cur"]["hp"] = str(new_hp)
                write_cfg(path, t_cfg)

                is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                dmg_line = f"{die} [{', '.join(str(r) for r in rolls)}]"
                dmg_line += (f" → **{final}** ({note})" if note else f" → **{final}**")
                if is_mon:
                    mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                    before = _life_bar(old_hp, mhp, width=10)
                    after  = _life_bar(new_hp, mhp, width=10)
                    hp_line = f"{before} → **{after}**"
                else:
                    hp_line = f"{old_hp} → **{new_hp}**"

                await ctx.send(embed=nextcord.Embed(
                    title=f"🧪 Dissolving Acid: {disp} takes damage",
                    description=dmg_line + f"\nHP {hp_line}" + ("  ☠️ **DEAD!**" if new_hp <= 0 else ""),
                    color=random.randint(0, 0xFFFFFF)
                ))

                if new_hp <= 0 and is_mon:
                    died_monster_overall = True
                    try:
                        names, scores = _parse_combatants(cfg, chan_id)
                        keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                        if keyname in names:
                            names = [n for n in names if n != keyname]
                            if cfg.has_option(chan_id, keyname):
                                cfg.remove_option(chan_id, keyname)
                            s_dead = _slot(keyname)
                            for suf in (".dex",".join",".disp",".acpen",".oil",
                                        ".x_dissolve",".x_dissolve_dice",".x_dissolve_label",".x_dissolve_emoji",".x_dissolve_code",".x_dissolve_by"):
                                opt = f"{s_dead}{suf}"
                                if cfg.has_option(chan_id, opt):
                                    cfg.remove_option(chan_id, opt)
                            _write_combatants(cfg, chan_id, names, scores); _save_battles(cfg)
                        try: os.remove(os.path.abspath(path))
                        except Exception: pass
                    except Exception:
                        pass

                try:
                    rounds_left = int(str(x_dis).strip())
                except Exception:
                    rounds_left = None
                if isinstance(rounds_left, int) and rounds_left >= 0:
                    rem = max(0, rounds_left - 1)
                    if rem > 0:
                        cfg.set(chan_id, f"{slot}.x_dissolve", str(rem))
                    else:
                        for suf in ("", "_dice", "_label", "_emoji", "_code", "_by"):
                            opt = f"{slot}.x_dissolve{suf}"
                            if cfg.has_option(chan_id, opt):
                                cfg.remove_option(chan_id, opt)
                    _save_battles(cfg)

        try:
            fv = cfg.getint(chan_id, f"{slot}.x_fastvenom", fallback=0)
        except Exception:
            fv = 0
        if fv > 0:
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)
            die = (cfg.get(chan_id, f"{slot}.x_fastvenom_dice", fallback="1d6") or "1d6").strip()

            if self._poison_immune(read_cfg(path)) if path and os.path.exists(path) else False or               "undead" in (get_compat(read_cfg(path), "info", "type", fallback="").lower() if path and os.path.exists(path) else ""):
                for suf in ("", "_dice", "_label"):
                    opt = f"{slot}.x_fastvenom{suf}"
                    if cfg.has_option(chan_id, opt): cfg.remove_option(chan_id, opt)
                _save_battles(cfg)
                await ctx.send(embed=nextcord.Embed(
                    title=f"🧪 Venom: {disp}",
                    description="No effect (poison immune/undead).",
                    color=random.randint(0, 0xFFFFFF)
                ))
            else:
                if not path or not os.path.exists(path):
                    await ctx.send(f"🧪 {disp}: venom tick but no character file was found.")
                else:
                    t_cfg = read_cfg(path)
                    s, rolls, flat = roll_dice(die)
                    raw = s + flat

                    final, note = _apply_mitigation(raw, weapon_name="Venom", weapon_type="internal", t_cfg=t_cfg)
                    old = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    new = max(0, old - final)
                    if not t_cfg.has_section("cur"): t_cfg.add_section("cur")
                    t_cfg["cur"]["hp"] = str(new); write_cfg(path, t_cfg)

                    is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                    dmg_line = f"{die} [{', '.join(str(r) for r in rolls)}]" + (f" → **{final}** ({note})" if note else f" → **{final}**")
                    if is_mon:
                        mhp = getint_compat(t_cfg, "max", "hp", fallback=old)
                        before = _life_bar(old, mhp, width=10); after = _life_bar(new, mhp, width=10)
                        hp_line = f"{before} → **{after}**"
                    else:
                        hp_line = f"{old} → **{new}**"

                    rem = max(0, fv - 1)
                    if rem > 0:
                        cfg.set(chan_id, f"{slot}.x_fastvenom", str(rem))
                    else:
                        for suf in ("", "_dice", "_label"):
                            opt = f"{slot}.x_fastvenom{suf}"
                            if cfg.has_option(chan_id, opt): cfg.remove_option(chan_id, opt)
                    _save_battles(cfg)

                    await ctx.send(embed=nextcord.Embed(
                        title=f"🧪 Venom: {disp} takes damage",
                        description=f"{dmg_line}\nHP {hp_line}\n_(fast venom: {rem} round{'s' if rem!=1 else ''} left)_",
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    if new <= 0 and is_mon:
                        died_monster_overall = True
                        try:
                            names, scores = _parse_combatants(cfg, chan_id)
                            keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                            if keyname in names:
                                names = [n for n in names if n != keyname]
                                if cfg.has_option(chan_id, keyname): cfg.remove_option(chan_id, keyname)
                                s_dead = _slot(keyname)
                                for suf in (".dex",".join",".disp",".acpen",".oil",".x_fastvenom",".x_fastvenom_dice",".x_fastvenom_label"):
                                    opt = f"{s_dead}{suf}"
                                    if cfg.has_option(chan_id, opt): cfg.remove_option(chan_id, opt)
                                _write_combatants(cfg, chan_id, names, scores); _save_battles(cfg)
                            try: os.remove(os.path.abspath(path))
                            except Exception: pass
                        except Exception:
                            pass

        try:
            sp_left = cfg.getint(chan_id, f"{slot}.x_spore", fallback=0)
        except Exception:
            sp_left = 0

        if sp_left > 0:
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)

            die = (cfg.get(chan_id, f"{slot}.x_spore_dice", fallback="1d8") or "1d8").strip()

            if not path or not os.path.exists(path):
                await ctx.send(f"☣️ {disp}: spores tick but no character file was found.")
            else:
                t_cfg = read_cfg(path)

                is_undead = "undead" in (get_compat(t_cfg, "info", "type", fallback="").lower())
                if is_undead or (self._poison_immune(t_cfg) if hasattr(self, "_poison_immune") else False):

                    for suf in ("", "_dice", "_label", "_emoji", "_code", "_by"):
                        opt = f"{slot}.x_spore{suf}"
                        if cfg.has_option(chan_id, opt):
                            cfg.remove_option(chan_id, opt)
                    _save_battles(cfg)

                    await ctx.send(embed=nextcord.Embed(
                        title=f"☣️ Toxic Spores: {disp}",
                        description="No further effect (poison immune/undead). Spores cleared.",
                        color=random.randint(0, 0xFFFFFF)
                    ))
                else:

                    s, rolls, flat = roll_dice(die)
                    raw = s + flat
                    final, note = _apply_mitigation(raw, weapon_name="Toxic Spores", weapon_type="poison", t_cfg=t_cfg)

                    old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    new_hp = max(0, old_hp - final)
                    if not t_cfg.has_section("cur"):
                        t_cfg.add_section("cur")
                    t_cfg["cur"]["hp"] = str(new_hp)
                    write_cfg(path, t_cfg)

                    rem = max(0, sp_left - 1)
                    if rem > 0:
                        cfg.set(chan_id, f"{slot}.x_spore", str(rem))
                    else:
                        for suf in ("", "_dice", "_label", "_emoji", "_code", "_by"):
                            opt = f"{slot}.x_spore{suf}"
                            if cfg.has_option(chan_id, opt):
                                cfg.remove_option(chan_id, opt)
                    _save_battles(cfg)

                    is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                    dmg_line = f"{die} [{', '.join(str(r) for r in rolls)}]"
                    if note:
                        dmg_line += f" → **{final}** ({note})"
                    else:
                        dmg_line += f" → **{final}**"
                    if is_mon:
                        mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                        before = _life_bar(old_hp, mhp, width=10)
                        after  = _life_bar(new_hp, mhp, width=10)
                        hp_line = f"{before} → **{after}**"
                    else:
                        hp_line = f"{old_hp} → **{new_hp}**"

                    txt_tail = (f"\n_(spores: {rem} round{'s' if rem!=1 else ''} left)_" if rem else "\n_effect ends._")
                    await ctx.send(embed=nextcord.Embed(
                        title=f"☣️ Toxic Spores: {disp} takes damage",
                        description=f"{dmg_line}\nHP {hp_line}{txt_tail}" + ("  ☠️ **DEAD!**" if new_hp <= 0 else ""),
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    if new_hp <= 0 and is_mon:
                        died_monster_overall = True
                        try:
                            names, scores = _parse_combatants(cfg, chan_id)
                            keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                            if keyname in names:
                                names = [n for n in names if n != keyname]
                                if cfg.has_option(chan_id, keyname):
                                    cfg.remove_option(chan_id, keyname)
                                s_dead = _slot(keyname)
                                for suf in (".dex",".join",".disp",".acpen",".oil",
                                            ".x_spore",".x_spore_dice",".x_spore_label",".x_spore_emoji",".x_spore_code",".x_spore_by"):
                                    opt = f"{s_dead}{suf}"
                                    if cfg.has_option(chan_id, opt):
                                        cfg.remove_option(chan_id, opt)
                                _write_combatants(cfg, chan_id, names, scores); _save_battles(cfg)
                            try: os.remove(os.path.abspath(path))
                            except Exception: pass
                        except Exception:
                            pass

        try:
            dance_left = cfg.getint(chan_id, f"{slot}.x_dance", fallback=0)
        except Exception:
            dance_left = 0

        if dance_left > 0:
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            kind = (cfg.get(chan_id, f"{slot}.x_dance_kind", fallback="primary") or "primary").lower()
            code = (cfg.get(chan_id, f"{slot}.x_dance_code", fallback="") or "").lower()

            _, path = _resolve_char_ci_local(turn_name)

            rem = max(0, dance_left - 1)
            if rem > 0:
                cfg.set(chan_id, f"{slot}.x_dance", str(rem))
            else:

                for suf in ("", "_label", "_code", "_kind"):
                    opt = f"{slot}.x_dance{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)
                _save_battles(cfg)
            _save_battles(cfg)

            if kind == "primary":
                if not path or not os.path.exists(path):
                    await ctx.send(f"🕷️ {disp}: dance poison tick but no character file was found.")
                else:
                    t_cfg = read_cfg(path)
                    ok, roll, dc, _ = self._roll_save(t_cfg, vs="poi", penalty=0)
                    if dc is None:
                        ok = False

                    if ok:
                        await ctx.send(embed=nextcord.Embed(
                            title=f"🕷️ Dance Poison: {disp}",
                            description=f"Save vs Poison: {roll} vs {dc} → ✅ **RESISTED** (no damage)."
                                        + (f"\n_{rem} round{'s' if rem!=1 else ''} remaining._" if rem else "\n_effect ends._"),
                            color=random.randint(0, 0xFFFFFF)
                        ))
                    else:
                        s, rolls, flat = roll_dice("1d4")
                        raw = s + flat
                        final, note = _apply_mitigation(raw, weapon_name="Dance Poison", weapon_type="internal", t_cfg=t_cfg)

                        old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                        new_hp = max(0, old_hp - final)
                        if not t_cfg.has_section("cur"):
                            t_cfg.add_section("cur")
                        t_cfg["cur"]["hp"] = str(new_hp)
                        write_cfg(path, t_cfg)

                        is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                        dmg_line = f"1d4 [{', '.join(str(r) for r in rolls)}]" + (f" → **{final}** ({note})" if note else f" → **{final}**")
                        if is_mon:
                            mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                            before = _life_bar(old_hp, mhp, width=10); after = _life_bar(new_hp, mhp, width=10)
                            hp_line = f"{before} → **{after}**"
                        else:
                            hp_line = f"{old_hp} → **{new_hp}**"

                        await ctx.send(embed=nextcord.Embed(
                            title=f"🕷️ Dance Poison: {disp} takes damage",
                            description=(dmg_line + f"\nHP {hp_line}\n"
                                         + (f"_{rem} round{'s' if rem!=1 else ''} remaining._" if rem else "_effect ends._")),
                            color=random.randint(0, 0xFFFFFF)
                        ))

                        if new_hp <= 0 and is_mon:
                            died_monster_overall = True
                            try:
                                names, scores = _parse_combatants(cfg, chan_id)
                                keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                                if keyname in names:
                                    names = [n for n in names if n != keyname]
                                    if cfg.has_option(chan_id, keyname):
                                        cfg.remove_option(chan_id, keyname)
                                    s_dead = _slot(keyname)
                                    for suf in (".dex",".join",".disp",".acpen",".oil",
                                                ".x_dance",".x_dance_label",".x_dance_code",".x_dance_kind"):
                                        opt = f"{s_dead}{suf}"
                                        if cfg.has_option(chan_id, opt):
                                            cfg.remove_option(chan_id, opt)
                                    _write_combatants(cfg, chan_id, names, scores); _save_battles(cfg)
                                try: os.remove(os.path.abspath(path))
                                except Exception: pass
                            except Exception:
                                pass
            else:

                await ctx.send(embed=nextcord.Embed(
                    title=f"🕺 Dancing: {disp}",
                    description=(f"(secondary) — **{rem}** round{'s' if rem!=1 else ''} remaining."
                                 if rem else "Effect ends."),
                    color=random.randint(0, 0xFFFFFF)
                ))

        try:
            x_cons = cfg.get(chan_id, f"{slot}.x_constrict", fallback="")
        except Exception:
            x_cons = ""
        if str(x_cons).strip() != "":
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)

            heldby = (cfg.get(chan_id, f"{slot}.heldby", fallback="") or "").strip()
            if not heldby:

                for suf in ("", "_label", "_emoji", "_code", "_by", "_dice"):
                    opt = f"{slot}.x_constrict{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)
                _save_battles(cfg)
            else:

                die = (cfg.get(chan_id, f"{slot}.x_constrict_dice", fallback="2d4") or "2d4").strip()
                if not path or not os.path.exists(path):
                    await ctx.send(f"🐍 {disp}: constriction tick but no character file was found.")
                else:
                    t_cfg = read_cfg(path)
                    s, rolls, flat = roll_dice(die)
                    raw = s + flat
                    final, note = _apply_mitigation(raw, weapon_name="Constriction", weapon_type="bludgeoning", t_cfg=t_cfg)

                    old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    new_hp = max(0, old_hp - final)
                    if not t_cfg.has_section("cur"):
                        t_cfg.add_section("cur")
                    t_cfg["cur"]["hp"] = str(new_hp)
                    write_cfg(path, t_cfg)

                    is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                    if is_mon:
                        mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                        before = _life_bar(old_hp, mhp, width=10)
                        after  = _life_bar(new_hp, mhp, width=10)
                        hp_line = f"{before} → **{after}**"
                    else:
                        hp_line = f"{old_hp} → **{new_hp}**"

                    dmg_line = f"{die} [{', '.join(str(r) for r in rolls)}]"
                    dmg_line += (f" → **{final}** ({note})" if note else f" → **{final}**")

                    await ctx.send(embed=nextcord.Embed(
                        title=f"🐍 Constriction: {disp} is squeezed",
                        description=f"{dmg_line}\nHP {hp_line}",
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    if new_hp <= 0 and is_mon:
                        died_monster_overall = True
                        try:
                            names, scores = _parse_combatants(cfg, chan_id)
                            keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                            if keyname in names:
                                names = [n for n in names if n != keyname]
                                if cfg.has_option(chan_id, keyname):
                                    cfg.remove_option(chan_id, keyname)
                                s_dead = _slot(keyname)

                                for suf in (".dex",".join",".disp",".acpen",".oil",".heldby",
                                            ".x_constrict",".x_constrict_label",".x_constrict_emoji",".x_constrict_code",".x_constrict_by",".x_constrict_dice"):
                                    opt = f"{s_dead}{suf}"
                                    if cfg.has_option(chan_id, opt):
                                        cfg.remove_option(chan_id, opt)
                                _write_combatants(cfg, chan_id, names, scores)
                                _save_battles(cfg)

                            try:
                                if heldby:
                                    s_h = _slot(heldby)
                                    if cfg.get(chan_id, f"{s_h}.holds", fallback="") == keyname:
                                        cfg.remove_option(chan_id, f"{s_h}.holds"); _save_battles(cfg)
                            except Exception:
                                pass

                            try: os.remove(os.path.abspath(path))
                            except Exception: pass
                        except Exception:
                            pass

        try:
            x_hold = cfg.get(chan_id, f"{slot}.x_holdbite", fallback="")
        except Exception:
            x_hold = ""
        if str(x_hold).strip() != "":
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)

            heldby = (cfg.get(chan_id, f"{slot}.heldby", fallback="") or "").strip()
            if not heldby:

                for suf in ("", "_label", "_emoji", "_code", "_by", "_dice"):
                    opt = f"{slot}.x_holdbite{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)
                _save_battles(cfg)
            else:
                die = (cfg.get(chan_id, f"{slot}.x_holdbite_dice", fallback="1d4") or "1d4").strip()
                if not path or not os.path.exists(path):
                    await ctx.send(f"🐕 {disp}: hold tick but no character file was found.")
                else:
                    t_cfg = read_cfg(path)

                    s, rolls, flat = roll_dice(die)
                    raw = s + flat
                    final, note = _apply_mitigation(raw, weapon_name="Worry", weapon_type="piercing", t_cfg=t_cfg)

                    old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    new_hp = max(0, old_hp - final)
                    if not t_cfg.has_section("cur"):
                        t_cfg.add_section("cur")
                    t_cfg["cur"]["hp"] = str(new_hp)
                    write_cfg(path, t_cfg)

                    is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                    if is_mon:
                        mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                        before = _life_bar(old_hp, mhp, width=10)
                        after  = _life_bar(new_hp, mhp, width=10)
                        hp_line = f"{before} → **{after}**"
                    else:
                        hp_line = f"{old_hp} → **{new_hp}**"

                    dmg_line = f"{die} [{', '.join(str(r) for r in rolls)}]"
                    dmg_line += (f" → **{final}** ({note})" if note else f" → **{final}**")

                    await ctx.send(embed=nextcord.Embed(
                        title=f"🐕 Hold: {disp} takes damage",
                        description=f"{dmg_line}\nHP {hp_line}" + ("  ☠️ **DEAD!**" if new_hp <= 0 else ""),
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    if new_hp <= 0 and is_mon:
                        died_monster_overall = True
                        try:
                            names, scores = _parse_combatants(cfg, chan_id)
                            keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                            if keyname in names:
                                names = [n for n in names if n != keyname]
                                if cfg.has_option(chan_id, keyname):
                                    cfg.remove_option(chan_id, keyname)
                                s_dead = _slot(keyname)
                                for suf in (".dex",".join",".disp",".acpen",".oil",".heldby",
                                            ".x_holdbite",".x_holdbite_label",".x_holdbite_emoji",".x_holdbite_code",".x_holdbite_by",".x_holdbite_dice"):
                                    opt = f"{s_dead}{suf}"
                                    if cfg.has_option(chan_id, opt):
                                        cfg.remove_option(chan_id, opt)
                                _write_combatants(cfg, chan_id, names, scores)
                                _save_battles(cfg)

                            try: os.remove(os.path.abspath(path))
                            except Exception: pass

                            try:
                                if heldby:
                                    s_h = _slot(heldby)
                                    if cfg.get(chan_id, f"{s_h}.holds", fallback="") == keyname:
                                        cfg.remove_option(chan_id, f"{s_h}.holds"); _save_battles(cfg)
                            except Exception:
                                pass
                        except Exception:
                            pass

        try:
            x_leech = cfg.get(chan_id, f"{slot}.x_leech", fallback="")
        except Exception:
            x_leech = ""
        if str(x_leech).strip() != "":
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)

            heldby = (cfg.get(chan_id, f"{slot}.heldby", fallback="") or "").strip()
            if not heldby:

                for suf in ("", "_label", "_emoji", "_code", "_by", "_dice"):
                    opt = f"{slot}.x_leech{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)
                _save_battles(cfg)
            else:
                die = (cfg.get(chan_id, f"{slot}.x_leech_dice", fallback="1d6") or "1d6").strip()
                if not path or not os.path.exists(path):
                    await ctx.send(f"🩸 {disp}: leech drain tick but no character file was found.")
                else:
                    t_cfg = read_cfg(path)
                    s, rolls, flat = roll_dice(die)
                    raw = s + flat

                    final, note = _apply_mitigation(raw, weapon_name="Leech Drain", weapon_type="piercing", t_cfg=t_cfg)

                    old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    new_hp = max(0, old_hp - final)
                    if not t_cfg.has_section("cur"):
                        t_cfg.add_section("cur")
                    t_cfg["cur"]["hp"] = str(new_hp)
                    write_cfg(path, t_cfg)

                    is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                    dmg_line = f"{die} [{', '.join(str(r) for r in rolls)}]"
                    dmg_line += (f" → **{final}** ({note})" if note else f" → **{final}**")
                    if is_mon:
                        mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                        before = _life_bar(old_hp, mhp, width=10)
                        after  = _life_bar(new_hp, mhp, width=10)
                        hp_line = f"{before} → **{after}**"
                    else:
                        hp_line = f"{old_hp} → **{new_hp}**"
                    if new_hp <= 0:
                        hp_line += "  ☠️ **DEAD!**"

                    await ctx.send(embed=nextcord.Embed(
                        title=f"🩸 Leech Drain: {disp} loses blood",
                        description=f"{dmg_line}\nHP {hp_line}",
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    if new_hp <= 0 and is_mon:
                        died_monster_overall = True
                        try:
                            names, scores = _parse_combatants(cfg, chan_id)
                            keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                            if keyname in names:
                                names = [n for n in names if n != keyname]
                                if cfg.has_option(chan_id, keyname):
                                    cfg.remove_option(chan_id, keyname)
                                s_dead = _slot(keyname)
                                for suf in (".dex",".join",".disp",".acpen",".oil",".heldby",
                                            ".x_leech",".x_leech_label",".x_leech_emoji",".x_leech_code",".x_leech_by",".x_leech_dice"):
                                    opt = f"{s_dead}{suf}"
                                    if cfg.has_option(chan_id, opt):
                                        cfg.remove_option(chan_id, opt)
                                _write_combatants(cfg, chan_id, names, scores)
                                _save_battles(cfg)
                            try: os.remove(os.path.abspath(path))
                            except Exception: pass
                        except Exception:
                            pass

        try:
            x_vine = cfg.get(chan_id, f"{slot}.x_entangle", fallback="")
        except Exception:
            x_vine = ""
        if str(x_vine).strip() != "":
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)

            heldby = (cfg.get(chan_id, f"{slot}.heldby", fallback="") or "").strip()
            if not heldby:

                for suf in ("", "_label", "_emoji", "_code", "_by", "_dice"):
                    opt = f"{slot}.x_entangle{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)
                _save_battles(cfg)
            else:

                die = (cfg.get(chan_id, f"{slot}.x_entangle_dice", fallback="1d8") or "1d8").strip()
                if not path or not os.path.exists(path):
                    await ctx.send(f"🌿 {disp}: entangle tick but no character file was found.")
                else:
                    t_cfg = read_cfg(path)
                    s, rolls, flat = roll_dice(die)
                    raw = s + flat

                    final, note = _apply_mitigation(raw, weapon_name="Vines", weapon_type="bludgeoning", t_cfg=t_cfg)

                    old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    new_hp = max(0, old_hp - final)
                    if not t_cfg.has_section("cur"):
                        t_cfg.add_section("cur")
                    t_cfg["cur"]["hp"] = str(new_hp)
                    write_cfg(path, t_cfg)

                    is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                    if is_mon:
                        mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                        before = _life_bar(old_hp, mhp, width=10)
                        after  = _life_bar(new_hp, mhp, width=10)
                        hp_line = f"{before} → **{after}**"
                    else:
                        hp_line = f"{old_hp} → **{new_hp}**"

                    dmg_line = f"{die} [{', '.join(str(r) for r in rolls)}]"
                    dmg_line += (f" → **{final}** ({note})" if note else f" → **{final}**")

                    await ctx.send(embed=nextcord.Embed(
                        title=f"🌿 Entangled: {disp} takes damage",
                        description=f"{dmg_line}\nHP {hp_line}",
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    if new_hp <= 0 and is_mon:
                        died_monster_overall = True
                        try:
                            names, scores = _parse_combatants(cfg, chan_id)
                            keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                            if keyname in names:
                                names = [n for n in names if n != keyname]
                                if cfg.has_option(chan_id, keyname):
                                    cfg.remove_option(chan_id, keyname)
                                s_dead = _slot(keyname)
                                for suf in (".dex",".join",".disp",".acpen",".oil",".heldby",
                                            ".x_entangle",".x_entangle_label",".x_entangle_emoji",".x_entangle_code",".x_entangle_by",".x_entangle_dice"):
                                    opt = f"{s_dead}{suf}"
                                    if cfg.has_option(chan_id, opt):
                                        cfg.remove_option(chan_id, opt)
                                _write_combatants(cfg, chan_id, names, scores)
                                _save_battles(cfg)

                            try:
                                if heldby:
                                    s_h = _slot(heldby)
                                    if cfg.get(chan_id, f"{s_h}.holds", fallback="") == keyname:
                                        cfg.remove_option(chan_id, f"{s_h}.holds"); _save_battles(cfg)
                            except Exception:
                                pass

                            try: os.remove(os.path.abspath(path))
                            except Exception: pass
                        except Exception:
                            pass

        try:
            x_sw = cfg.get(chan_id, f"{slot}.x_swallow", fallback="")
        except Exception:
            x_sw = ""
        if str(x_sw).strip() != "":
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)

            heldby = (cfg.get(chan_id, f"{slot}.heldby", fallback="") or "").strip()
            if not heldby:

                for suf in ("", "_label", "_code", "_by", "_dice"):
                    opt = f"{slot}.x_swallow{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)
                _save_battles(cfg)

            else:
                die = (cfg.get(chan_id, f"{slot}.x_swallow_dice", fallback="1d8") or "1d8").strip()
                if not path or not os.path.exists(path):
                    await ctx.send(f"🫗 {disp}: swallowed tick but no character file was found.")
                else:
                    t_cfg = read_cfg(path)

                    s, rolls, flat = roll_dice(die)
                    raw = s + flat
                    final, note = _apply_mitigation(raw, weapon_name="Swallowed", weapon_type="internal", t_cfg=t_cfg)

                    old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    new_hp = max(0, old_hp - final)
                    if not t_cfg.has_section("cur"):
                        t_cfg.add_section("cur")
                    t_cfg["cur"]["hp"] = str(new_hp)
                    write_cfg(path, t_cfg)

                    is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                    dmg_line = f"{die} [{', '.join(str(r) for r in rolls)}]"
                    if note:
                        dmg_line += f" → **{final}** ({note})"
                    else:
                        dmg_line += f" → **{final}**"

                    if is_mon:
                        mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                        before = _life_bar(old_hp, mhp, width=10)
                        after  = _life_bar(new_hp, mhp, width=10)
                        hp_line = f"{before} → **{after}**"
                    else:
                        hp_line = f"{old_hp} → **{new_hp}**"

                    await ctx.send(embed=nextcord.Embed(
                        title=f"🫗 Swallowed: {disp} takes damage",
                        description=f"{dmg_line}\nHP {hp_line}" + ("  ☠️ **DEAD!**" if new_hp <= 0 else ""),
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    if new_hp <= 0 and is_mon:
                        died_monster_overall = True
                        try:
                            names, scores = _parse_combatants(cfg, chan_id)
                            keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                            if keyname in names:
                                names = [n for n in names if n != keyname]
                                if cfg.has_option(chan_id, keyname):
                                    cfg.remove_option(chan_id, keyname)
                                s_dead = _slot(keyname)
                                for suf in (".dex",".join",".disp",".acpen",".oil",".heldby",
                                            ".x_swallow",".x_swallow_label",".x_swallow_code",".x_swallow_by",".x_swallow_dice"):
                                    opt = f"{s_dead}{suf}"
                                    if cfg.has_option(chan_id, opt):
                                        cfg.remove_option(chan_id, opt)
                                _write_combatants(cfg, chan_id, names, scores)
                                _save_battles(cfg)
                            try: os.remove(os.path.abspath(path))
                            except Exception: pass
                        except Exception:
                            pass

        mg_native = cfg.getint(chan_id, f"{slot}.maggots", fallback=0)
        mg_x      = cfg.getint(chan_id, f"{slot}.x_maggots", fallback=0)
        use_x     = (mg_native <= 0 and mg_x > 0)

        mg  = mg_native if mg_native > 0 else mg_x
        die = (cfg.get(chan_id, f"{slot}.maggots_die",   fallback="") or "").strip()
        if not die:
            die = (cfg.get(chan_id, f"{slot}.x_maggots_dmg", fallback="1d3") or "1d3").strip()

        if mg > 0:
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)

            if fire_hit_this_start:
                if use_x:
                    for suf in ("", "_dmg", "_label", "_emoji", "_code", "_by"):
                        opt = f"{slot}.x_maggots{suf}"
                        if cfg.has_option(chan_id, opt):
                            cfg.remove_option(chan_id, opt)
                else:
                    if cfg.has_option(chan_id, f"{slot}.maggots"):
                        cfg.remove_option(chan_id, f"{slot}.maggots")
                    if cfg.has_option(chan_id, f"{slot}.maggots_die"):
                        cfg.remove_option(chan_id, f"{slot}.maggots_die")
                _save_battles(cfg)

                await ctx.send(embed=nextcord.Embed(
                    title=f"🔥 {disp}: Maggots burned off",
                    description="Start-of-turn fire **scorches away the maggots**.",
                    color=random.randint(0, 0xFFFFFF)
                ))

            else:

                if not path or not os.path.exists(path):
                    await ctx.send(f"🪱 {disp}: maggots could not find a character file.")
                else:
                    t_cfg = read_cfg(path)

                    try:
                        r = _dice_sum(die)
                    except Exception:
                        r = random.randint(1, 3) if die.strip().lower() == "1d3" else 1
                    total = max(0, r)

                    old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    new_hp = max(0, old_hp - total)
                    if not t_cfg.has_section("cur"):
                        t_cfg.add_section("cur")
                    t_cfg["cur"]["hp"] = str(new_hp)
                    write_cfg(path, t_cfg)

                    rem = max(0, mg - 1)
                    if use_x:
                        if rem > 0:
                            cfg.set(chan_id, f"{slot}.x_maggots", str(rem))
                        else:
                            for suf in ("", "_dmg", "_label", "_emoji", "_code", "_by"):
                                opt = f"{slot}.x_maggots{suf}"
                                if cfg.has_option(chan_id, opt):
                                    cfg.remove_option(chan_id, opt)
                    else:
                        if rem > 0:
                            cfg.set(chan_id, f"{slot}.maggots", str(rem))
                        else:
                            if cfg.has_option(chan_id, f"{slot}.maggots"):
                                cfg.remove_option(chan_id, f"{slot}.maggots")
                            if cfg.has_option(chan_id, f"{slot}.maggots_die"):
                                cfg.remove_option(chan_id, f"{slot}.maggots_die")
                    _save_battles(cfg)

                    is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                    desc = f"{die}: **{total}**  _(maggots: {rem} round{'s' if rem != 1 else ''} left)_"
                    if is_mon:
                        mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                        before = _life_bar(old_hp, mhp, width=10)
                        after  = _life_bar(new_hp, mhp, width=10)
                        desc += f"\nHP {before} → **{after}**"
                    else:
                        desc += f"\nHP {old_hp} → **{new_hp}**"
                    if new_hp <= 0:
                        desc += "  ☠️ **DEAD!**"

                    await ctx.send(embed=nextcord.Embed(
                        title=f"🪱 Maggots: {disp} takes damage",
                        description=desc,
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    if new_hp <= 0 and is_mon:
                        died_monster_overall = True
                        try:
                            names, scores = _parse_combatants(cfg, chan_id)
                            key = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                            if key in names:
                                names = [n for n in names if n != key]
                                if cfg.has_option(chan_id, key):
                                    cfg.remove_option(chan_id, key)
                                s = _slot(key)
                                for suf in (".dex",".join",".disp",".acpen",".oil",".maggots",".maggots_die",
                                            ".x_maggots",".x_maggots_dmg",".x_maggots_label",".x_maggots_emoji",".x_maggots_code",".x_maggots_by"):
                                    opt = f"{s}{suf}"
                                    if cfg.has_option(chan_id, opt):
                                        cfg.remove_option(chan_id, opt)
                                _write_combatants(cfg, chan_id, names, scores)
                                _save_battles(cfg)
                            try:
                                os.remove(os.path.abspath(path))
                            except Exception:
                                pass
                        except Exception:
                            pass

        try:
            rg_rounds = cfg.getint(chan_id, f"{slot}.x_rotgrub", fallback=0)
        except Exception:
            rg_rounds = 0

        if rg_rounds > 0:
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)

            try:
                rg_burn = cfg.getint(chan_id, f"{slot}.x_rotgrub_burn", fallback=0)
            except Exception:
                rg_burn = 0

            noticed = (cfg.get(chan_id, f"{slot}.x_rotgrub_notice", fallback="0") == "1")

            rem = max(0, rg_rounds - 1)
            if rg_burn > 0:
                rg_burn = max(0, rg_burn - 1)

            if rem <= 0:

                if not path or not os.path.exists(path):

                    await ctx.send(embed=nextcord.Embed(
                        title=f"🪱 Rot Grubs: {disp}",
                        description="The grubs reach the heart. ☠️ **DEAD!**",
                        color=random.randint(0, 0xFFFFFF)
                    ))
                else:
                    t_cfg = read_cfg(path)
                    old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    if not t_cfg.has_section("cur"):
                        t_cfg.add_section("cur")
                    t_cfg["cur"]["hp"] = "0"
                    write_cfg(path, t_cfg)

                    is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                    hp_line = f"{old_hp} → **0**" if not is_mon else ""
                    await ctx.send(embed=nextcord.Embed(
                        title=f"🪱 Rot Grubs: {disp}",
                        description=("The grubs reach the heart. "
                                     "☠️ **DEAD!**" + (f"\nHP {hp_line}" if hp_line else "")),
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    if is_mon:
                        died_monster_overall = True
                        try:
                            names, scores = _parse_combatants(cfg, chan_id)
                            keyname = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                            if keyname in names:
                                names = [n for n in names if n != keyname]
                                if cfg.has_option(chan_id, keyname):
                                    cfg.remove_option(chan_id, keyname)
                                s_dead = _slot(keyname)
                                for suf in (".dex",".join",".disp",".acpen",".oil",
                                            ".x_rotgrub",".x_rotgrub_burn",".x_rotgrub_label",".x_rotgrub_emoji",".x_rotgrub_code",".x_rotgrub_by",".x_rotgrub_notice"):
                                    opt = f"{s_dead}{suf}"
                                    if cfg.has_option(chan_id, opt):
                                        cfg.remove_option(chan_id, opt)
                                _write_combatants(cfg, chan_id, names, scores); _save_battles(cfg)
                            try: os.remove(os.path.abspath(path))
                            except Exception: pass
                        except Exception:
                            pass

                for suf in (".x_rotgrub",".x_rotgrub_burn",".x_rotgrub_label",".x_rotgrub_emoji",".x_rotgrub_code",".x_rotgrub_by",".x_rotgrub_notice"):
                    opt = f"{slot}{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)
                _save_battles(cfg)

            else:

                cfg.set(chan_id, f"{slot}.x_rotgrub", str(rem))
                if rg_burn > 0:
                    cfg.set(chan_id, f"{slot}.x_rotgrub_burn", str(rg_burn))
                else:
                    if cfg.has_option(chan_id, f"{slot}.x_rotgrub_burn"):
                        cfg.remove_option(chan_id, f"{slot}.x_rotgrub_burn")
                _save_battles(cfg)

                burn_txt = (f" — burn/cut window: **{rg_burn}** round{'s' if rg_burn!=1 else ''} left" if rg_burn > 0 else
                            " — **too deep for burn/cut** (only *Cure Disease* can save now)")
                note_txt = ("**(noticed)**" if noticed else "_(unnoticed)_")
                await ctx.send(embed=nextcord.Embed(
                    title=f"🪱 Rot Grubs: {disp}",
                    description=f"{note_txt} — **{rem}** round{'s' if rem!=1 else ''} until death{burn_txt}.",
                    color=random.randint(0, 0xFFFFFF)
                ))

        try:
            torch_on = cfg.getint(chan_id, f"{slot}.x_swarm_torch", fallback=0)
        except Exception:
            torch_on = 0
        if torch_on > 0:
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)
            if not path or not os.path.exists(path):
                await ctx.send(f"🕯️ {disp}: torch ward tick but no character file found.")
            else:
                t_cfg = read_cfg(path)
                roll = random.randint(1, 4)
                old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                new_hp = max(0, old_hp - roll)
                if not t_cfg.has_section("cur"):
                    t_cfg.add_section("cur")
                t_cfg["cur"]["hp"] = str(new_hp)
                write_cfg(path, t_cfg)

                is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                if is_mon:
                    mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                    before = _life_bar(old_hp, mhp, width=10)
                    after  = _life_bar(new_hp, mhp, width=10)
                    hp_line = f"{before} → **{after}**"
                else:
                    hp_line = f"{old_hp} → **{new_hp}**"

                msg = nextcord.Embed(
                    title=f"🕯️ Torch Ward: {disp} takes damage",
                    description=f"1d4 → **{roll}**\nHP {hp_line}" + ("  ☠️ **DEAD!**" if new_hp<=0 else ""),
                    color=random.randint(0, 0xFFFFFF)
                )
                await ctx.send(embed=msg)

                if new_hp <= 0 and is_mon:
                    died_monster_overall = True
                    try:
                        names, scores = _parse_combatants(cfg, chan_id)
                        key = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                        if key in names:
                            names = [n for n in names if n != key]
                            if cfg.has_option(chan_id, key):
                                cfg.remove_option(chan_id, key)
                            s = _slot(key)
                            for suf in (".dex",".join",".disp",".acpen",".oil",".x_swarm",".x_swarm_code",".x_swarm_label",".x_swarm_emoji",".x_swarm_stat",".x_swarm_torch"):
                                opt = f"{s}{suf}"
                                if cfg.has_option(chan_id, opt):
                                    cfg.remove_option(chan_id, opt)
                            _write_combatants(cfg, chan_id, names, scores)
                            _save_battles(cfg)
                        try: os.remove(os.path.abspath(path))
                        except Exception: pass
                    except Exception:
                        pass

        try:
            dur_left = cfg.getint(chan_id, f"{slot}.x_swarm", fallback=0)
        except Exception:
            dur_left = 0
        if dur_left > 0:
            any_effect_applied = True
            rem = max(0, dur_left - 1)
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            if rem > 0:
                cfg.set(chan_id, f"{slot}.x_swarm", str(rem)); _save_battles(cfg)
                await ctx.send(embed=nextcord.Embed(
                    title=f"🪰 Insect Swarm: {disp}",
                    description=f"**{rem}** round{'s' if rem!=1 else ''} remaining.",
                    color=random.randint(0, 0xFFFFFF)
                ))
            else:

                for suf in (".x_swarm",".x_swarm_code",".x_swarm_label",".x_swarm_emoji",".x_swarm_stat",".x_swarm_torch"):
                    if cfg.has_option(chan_id, f"{slot}{suf}"):
                        cfg.remove_option(chan_id, f"{slot}{suf}")
                _save_battles(cfg)

                await ctx.send(embed=nextcord.Embed(
                    title=f"🪰 Insect Swarm: {disp} disperses",
                    description="Duration ended.",
                    color=random.randint(0, 0xFFFFFF)
                ))

                died_monster_overall = True
                try:
                    names, scores = _parse_combatants(cfg, chan_id)
                    key = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                    if key in names:
                        names = [n for n in names if n != key]
                        if cfg.has_option(chan_id, key): cfg.remove_option(chan_id, key)
                        s = _slot(key)
                        for suf in (".dex",".join",".disp",".acpen",".oil"):
                            opt = f"{s}{suf}"
                            if cfg.has_option(chan_id, opt): cfg.remove_option(chan_id, opt)
                        _write_combatants(cfg, chan_id, names, scores); _save_battles(cfg)
                    try:
                        _, path = _resolve_char_ci_local(turn_name)
                        if path and os.path.exists(path): os.remove(os.path.abspath(path))
                    except Exception:
                        pass
                except Exception:
                    pass

        bt_left = cfg.getint(chan_id, f"{slot}.x_tentacles", fallback=0)
        if bt_left > 0:
            any_effect_applied = True
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)

            heldby = (cfg.get(chan_id, f"{slot}.heldby", fallback="") or "").lower()
            still_held = ("tentacles" in heldby)

            rem = max(0, bt_left - 1)
            if rem > 0:
                cfg.set(chan_id, f"{slot}.x_tentacles", str(rem))
            else:

                for suf in ("", "_dmg", "_label", "_emoji", "_code", "_by"):
                    opt = f"{slot}.x_tentacles{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)
                hb = (cfg.get(chan_id, f"{slot}.heldby", fallback="") or "").lower()
                if "tentacles" in hb and cfg.has_option(chan_id, f"{slot}.heldby"):
                    cfg.remove_option(chan_id, f"{slot}.heldby")
            _save_battles(cfg)

            if not still_held:
                await ctx.send(embed=nextcord.Embed(
                    title=f"🐙 Black Tentacles: {disp}",
                    description=f"(not held) — **{rem}** round{'s' if rem!=1 else ''} remaining in the area.",
                    color=random.randint(0, 0xFFFFFF)
                ))
            else:

                if not path or not os.path.exists(path):
                    await ctx.send(f"🐙 {disp}: tentacles constrict but no character file was found.")
                else:
                    t_cfg = read_cfg(path)

                    s, rolls, flat = roll_dice("1d6")
                    raw = s + flat
                    final, note = _apply_mitigation(raw, weapon_name="Black Tentacles", weapon_type="magical", t_cfg=t_cfg)

                    old_hp = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    new_hp = max(0, old_hp - final)
                    if not t_cfg.has_section("cur"):
                        t_cfg.add_section("cur")
                    t_cfg["cur"]["hp"] = str(new_hp)
                    write_cfg(path, t_cfg)

                    is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                    dmg_line = f"1d6 [{', '.join(str(r) for r in rolls)}]"
                    dmg_line += (f" → **{final}** ({note})" if note else f" → **{final}**")
                    if is_mon:
                        mhp = getint_compat(t_cfg, "max", "hp", fallback=old_hp)
                        before = _life_bar(old_hp, mhp, width=10)
                        after  = _life_bar(new_hp, mhp, width=10)
                        hp_line = f"{before} → **{after}**"
                    else:
                        hp_line = f"{old_hp} → **{new_hp}**"
                    if new_hp <= 0:
                        hp_line += "  ☠️ **DEAD!**"

                    await ctx.send(embed=nextcord.Embed(
                        title=f"🐙 Black Tentacles: {disp} is constricted",
                        description=(dmg_line + f"\nHP {hp_line}\n"
                                     f"_({rem} round{'s' if rem!=1 else ''} of tentacles remain.)_"),
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    if new_hp <= 0 and is_mon:
                        died_monster_overall = True
                        try:
                            names, scores = _parse_combatants(cfg, chan_id)
                            key = disp if disp in names else (turn_name if turn_name in names else (_find_ci_name(names, turn_name) or turn_name))
                            if key in names:
                                names = [n for n in names if n != key]
                                if cfg.has_option(chan_id, key):
                                    cfg.remove_option(chan_id, key)
                                s = _slot(key)
                                for suf in (".dex",".join",".disp",".acpen",".oil",
                                            ".x_tentacles",".x_tentacles_dmg",".x_tentacles_label",".x_tentacles_emoji",".x_tentacles_code",".x_tentacles_by",
                                            ".heldby"):
                                    opt = f"{s}{suf}"
                                    if cfg.has_option(chan_id, opt):
                                        cfg.remove_option(chan_id, opt)
                                _write_combatants(cfg, chan_id, names, scores)
                                _save_battles(cfg)
                            try: os.remove(os.path.abspath(path))
                            except Exception: pass
                        except Exception:
                            pass

        try:
            disp = cfg.get(chan_id, f"{slot}.disp", fallback=turn_name)
            _, path = _resolve_char_ci_local(turn_name)
            if path and os.path.exists(path):
                t_cfg = read_cfg(path)
                is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                if is_mon and _monster_has_regen(t_cfg, path):

                    cur = getint_compat(t_cfg, "cur", "hp", fallback=0)
                    mx  = getint_compat(t_cfg, "max", "hp", fallback=max(1, cur))

                    try:
                        suppress = cfg.getint(chan_id, f"{slot}.x_regen_block", fallback=0)
                    except Exception:
                        suppress = 0

                    if (suppress > 0) or fire_hit_this_start or acid_hit_this_start:

                        if suppress > 0:
                            if suppress > 1:
                                cfg.set(chan_id, f"{slot}.x_regen_block", str(suppress - 1))
                            else:
                                if cfg.has_option(chan_id, f"{slot}.x_regen_block"):
                                    cfg.remove_option(chan_id, f"{slot}.x_regen_block")
                            _save_battles(cfg)
                    else:

                        if 0 < cur < mx:
                            if not t_cfg.has_section("cur"):
                                t_cfg.add_section("cur")
                            t_cfg["cur"]["hp"] = str(min(mx, cur + 1))
                            write_cfg(path, t_cfg)
                            any_effect_applied = True
                            
                elif (not is_mon) and _pc_has_regen_item(t_cfg):
                    cur = getint_compat(t_cfg, "cur", "hp",  fallback=0)
                    mx  = getint_compat(t_cfg, "max", "hp", fallback=max(1, cur))

                    if not t_cfg.has_section("cur"):
                        t_cfg.add_section("cur")
                    cap = getint_compat(t_cfg, "cur", "regen_cap_hp", fallback=cur)
                    if not t_cfg.has_option("cur", "regen_cap_hp"):
                        t_cfg["cur"]["regen_cap_hp"] = str(cur)
                        cap = cur

                    try:
                        suppress = cfg.getint(chan_id, f"{slot}.x_regen_block", fallback=0)
                    except Exception:
                        suppress = 0

                    fire_hit = False
                    acid_hit = False
                    try:
                        fire_hit = bool(fire_hit_this_start)
                    except NameError:
                        pass
                    try:
                        acid_hit = bool(acid_hit_this_start)
                    except NameError:
                        pass

                    if (suppress > 0) or fire_hit or acid_hit:
                        if suppress > 0:
                            if suppress > 1:
                                cfg.set(chan_id, f"{slot}.x_regen_block", str(suppress - 1))
                            else:
                                if cfg.has_option(chan_id, f"{slot}.x_regen_block"):
                                    cfg.remove_option(chan_id, f"{slot}.x_regen_block")
                            _save_battles(cfg)
                    else:
                        if 0 < cur < mx and cur < cap:
                            t_cfg["cur"]["hp"] = str(min(cap, cur + 1))
                            write_cfg(path, t_cfg)
                            any_effect_applied = True

                    try:
                        new_cur = getint_compat(t_cfg, "cur", "hp", fallback=cur)
                    except Exception:
                        new_cur = cur
                    if new_cur > cap:
                        t_cfg["cur"]["regen_cap_hp"] = str(new_cur)
                        write_cfg(path, t_cfg)
            
        except Exception:
            pass



        if any_effect_applied:
            try:
                msg_id = cfg.getint(chan_id, "message_id", fallback=0)
                if msg_id:
                    block = _format_tracker_block(cfg, chan_id)
                    content = "**EVERYONE ROLL FOR INITIATIVE!**\n```text\n" + block + "\n```"
                    msg = await ctx.channel.fetch_message(msg_id)
                    await msg.edit(content=content)
            except Exception:
                pass

        return died_monster_overall

    async def _process_start_of_turn_web_bridge(self, ctx, cfg, chan_id: str, who_name: str) -> bool:
        """
        Ask the Spells cog to apply start-of-turn Web effects to 'who_name'.
        Return True if they died and were removed (same contract as oil).
        """
        try:
            spells = self.bot.get_cog("SpellsCog") or self.bot.get_cog("Spells")
            if not spells or not hasattr(spells, "_apply_start_of_turn_web"):
                return False

            _, who_path = _resolve_char_ci_local(who_name)
            if not who_path:
                return False
            return await spells._apply_start_of_turn_web(ctx, cfg, chan_id, who_name, who_path)
        except Exception:
            return False

    async def roll_gems(self, ctx, qty: str = "all"):
        cfg = _load_battles(); chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id): await ctx.send("❌ No initiative running here."); return
        have = cfg.getint(chan_id, "tre_gems", fallback=0)
        if have <= 0: await ctx.send("ℹ️ No gems pending."); return
        times = have if str(qty).lower() in {"","all","*"} else max(0,int(str(qty)))
        if times <= 0: await ctx.send("❌ Quantity must be a positive number or 'all'."); return
        times = min(times, have)

        gem_lines, gem_gp = _gen_gems(times)
        if gem_gp:
            _inc_cfg_int(cfg, chan_id, "tre_gp", gem_gp)
            _inc_cfg_int(cfg, chan_id, "new_gp", gem_gp)

        cfg.set(chan_id, "tre_gems", str(have - times)); _save_battles(cfg)

        out = [f"• {s.replace(' — ', ' — **') + '**'}" if ' gp' in s else f"• {s}" for s in gem_lines]
        footer = f"\n**→ Added {gem_gp} gp to Coins.**\n*Remaining gems:* {cfg.getint(chan_id,'tre_gems',fallback=0)}"
        for chunk in _chunk_send_lines(out, 20):
            await ctx.send("💎 **Gems**\n" + "\n".join(chunk) + footer)

    async def roll_jewelry(self, ctx, qty: str = "all"):
        cfg = _load_battles(); chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id): await ctx.send("❌ No initiative running here."); return
        have = cfg.getint(chan_id, "tre_jewelry", fallback=0)
        if have <= 0: await ctx.send("ℹ️ No jewelry pending."); return
        times = have if str(qty).lower() in {"","all","*"} else max(0,int(str(qty)))
        if times <= 0: await ctx.send("❌ Quantity must be a positive number or 'all'."); return
        times = min(times, have)

        jew_lines, jew_gp = _gen_jewelry(times)
        if jew_gp:
            _inc_cfg_int(cfg, chan_id, "tre_gp", jew_gp)
            _inc_cfg_int(cfg, chan_id, "new_gp", jew_gp)

        cfg.set(chan_id, "tre_jewelry", str(have - times)); _save_battles(cfg)

        out = [f"• {s.replace(' — ', ' — **') + '**'}" if ' gp' in s else f"• {s}" for s in jew_lines]
        footer = f"\n**→ Added {jew_gp} gp to Coins.**\n*Remaining jewelry:* {cfg.getint(chan_id,'tre_jewelry',fallback=0)}"
        for chunk in _chunk_send_lines(out, 20):
            await ctx.send("👑 **Jewelry**\n" + "\n".join(chunk) + footer)

    async def roll_potions(self, ctx, qty: str = "all"):
        cfg = _load_battles(); chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id): await ctx.send("❌ No initiative running here."); return
        have = cfg.getint(chan_id, "tre_potions", fallback=0)
        if have <= 0: await ctx.send("ℹ️ No potions pending."); return
        times = have if str(qty).lower() in {"","all","*"} else max(0,int(str(qty)))
        if times <= 0: await ctx.send("❌ Quantity must be a positive number or 'all'."); return
        times = min(times, have)

        lines = [f"• { _pick_percent(_POTIONS) }" for _ in range(times)]
        cfg.set(chan_id, "tre_potions", str(have - times)); _save_battles(cfg)
        for chunk in _chunk_send_lines(lines, 25):
            await ctx.send("🧪 **Potions**\n" + "\n".join(chunk) + f"\n*Remaining potions:* {cfg.getint(chan_id,'tre_potions',fallback=0)}")

    async def roll_scrolls(self, ctx, qty: str = "all"):
        """
        Rolls from the long scroll table. Spell scrolls pick a class and random spells (by name) from self._spells_by_class.
        Maps add lair seeds; Map to 1d4 Magic Items increments tre_magic_any.
        """
        cfg = _load_battles(); chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id): await ctx.send("❌ No initiative running here."); return
        have = cfg.getint(chan_id, "tre_scrolls", fallback=0)
        if have <= 0: await ctx.send("ℹ️ No scrolls pending."); return
        times = have if str(qty).lower() in {"","all","*"} else max(0,int(str(qty)))
        if times <= 0: await ctx.send("❌ Quantity must be a positive number or 'all'."); return
        times = min(times, have)

        lines = []
        def pick_spells(for_class: str, nsp: int) -> list[str]:
            return self._pick_spells(for_class, nsp)

        for _ in range(times):
            r = _d100()
            if 1 <= r <= 9:
                nsp = 1 if r<=3 else (2 if r<=6 else (3 if r<=8 else 4))
                cls = random.choice(["Cleric","Druid"])
                names = pick_spells(cls, nsp)
                lines.append(f"• **Spell Scroll** ({cls}) — {', '.join(names)}")
            elif 10 <= r <= 35:

                nsp = 1 if r<=15 else (2 if r<=20 else (3 if r<=25 else (4 if r<=29 else (5 if r<=32 else (6 if r<=34 else 7)))))
                cls = random.choice(["Magic-User","Illusionist","Necromancer","Spellcrafter"])
                names = pick_spells(cls, nsp)
                lines.append(f"• **Spell Scroll** ({cls}) — {', '.join(names)}")
            elif 36 <= r <= 40:
                lines.append("• **Cursed Scroll**")
            elif 41 <= r <= 46:
                lines.append("• **Protection from Elementals**")
            elif 47 <= r <= 56:
                lines.append("• **Protection from Lycanthropes**")
            elif 57 <= r <= 61:
                lines.append("• **Protection from Magic**")
            elif 62 <= r <= 75:
                lines.append("• **Protection from Undead**")
            elif 76 <= r <= 85:
                lines.append("• **Map to Treasure Type A**")
                lairs = _get_map(cfg, chan_id, "tre_lair_counts"); lairs["A"] = lairs.get("A",0)+1; _set_map(cfg, chan_id, "tre_lair_counts", lairs)
            elif 86 <= r <= 89:
                lines.append("• **Map to Treasure Type E**")
                lairs = _get_map(cfg, chan_id, "tre_lair_counts"); lairs["E"] = lairs.get("E",0)+1; _set_map(cfg, chan_id, "tre_lair_counts", lairs)
            elif 90 <= r <= 92:
                lines.append("• **Map to Treasure Type G**")
                lairs = _get_map(cfg, chan_id, "tre_lair_counts"); lairs["G"] = lairs.get("G",0)+1; _set_map(cfg, chan_id, "tre_lair_counts", lairs)
            else:
                k = _dice_sum("1d4")
                _inc(cfg, chan_id, "tre_magic_any", k)
                lines.append(f"• **Map to 1d4 Magic Items** (added **{k}** magic-any to tally)")

        cfg.set(chan_id, "tre_scrolls", str(have - times)); _save_battles(cfg)
        for chunk in _chunk_send_lines(lines, 12):
            await ctx.send("📜 **Scrolls**\n" + "\n".join(chunk) + f"\n*Remaining scrolls:* {cfg.getint(chan_id,'tre_scrolls',fallback=0)}")

    async def roll_magic_items(self, ctx, qty: str = "all", *flags):
        """
        Roll specific magic items, consuming the magic pools:
          tre_magic_any (Any: weapon/armor/potion/scroll/wand-rod-staff/rare)
          tre_magic_wa  (Weapon or Armor only)
          tre_magic_xw  (Anything except weapons; armor allowed)

        Usage:
          !magicitem           -> consume all pools
          !magicitem all -any -> consume only 'any' pool
          !magicitem 3 -wa    -> roll 3 items from weapon/armor pool
          !magicitem 5 -xw    -> roll 5 items from 'except weapons' pool
        """
        cfg = _load_battles(); chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id): await ctx.send("❌ No initiative running here."); return

        use_any = "-any" in flags
        use_wa  = "-wa"  in flags
        use_xw  = "-xw"  in flags
        if not (use_any or use_wa or use_xw):
            use_any = use_wa = use_xw = True

        pool_any = cfg.getint(chan_id, "tre_magic_any", fallback=0) if use_any else 0
        pool_wa  = cfg.getint(chan_id, "tre_magic_wa", fallback=0) if use_wa else 0
        pool_xw  = cfg.getint(chan_id, "tre_magic_xw", fallback=0) if use_xw else 0
        have_total = pool_any + pool_wa + pool_xw
        if have_total <= 0:
            await ctx.send("ℹ️ No magic items pending in the selected pools."); return

        want = have_total if str(qty).lower() in {"","all","*"} else max(0, int(str(qty)))
        if want <= 0: await ctx.send("❌ Quantity must be a positive number or 'all'."); return
        want = min(want, have_total)

        def pick_cat_from_any():
            return random.choice(["weapon","armor","potion","scroll","wsr","rare","misc"])
        def pick_cat_from_wa():
            return random.choice(["weapon","armor"])
        def pick_cat_from_xw():
            return random.choice(["armor","potion","scroll","wsr","rare","misc"])

        _MISC_SUBTABLE = [(57, 1), (100, 2)]

        _MISC_EFFECTS_1 = [
            (1, 1,   ("Blasting", "G")),
            (2, 5,   ("Blending", "F")),
            (6, 13,  ("Cold Resistance", "F")),
            (14, 17, ("Comprehension", "E")),
            (18, 22, ("Control Animal", "C")),
            (23, 29, ("Control Human", "C")),
            (30, 35, ("Control Plant", "C")),
            (36, 37, ("Courage", "G")),
            (38, 40, ("Deception", "F")),
            (41, 52, ("Delusion", "A")),
            (53, 55, ("Djinni Summoning", "C")),
            (56, 56, ("Doom", "G")),
            (57, 67, ("Fire Resistance", "F")),
            (68, 80, ("Invisibility", "F")),
            (81, 85, ("Levitation", "B")),
            (86, 95, ("Mind Reading", "C")),
            (96, 97, ("Panic", "G")),
            (98, 100, ("Penetrating Vision", "D")),
        ]
        _MISC_EFFECTS_2 = [
            (1, 7,   ("Protection +1", "F")),
            (8, 10,  ("Protection +2", "F")),
            (11, 11, ("Protection +3", "F")),
            (12, 14, ("Protection from Energy Drain", "F")),
            (15, 20, ("Protection from Scrying", "F")),
            (21, 23, ("Regeneration", "C")),
            (24, 29, ("Scrying", "H")),
            (30, 32, ("Scrying, Superior", "H")),
            (33, 39, ("Speed", "B")),
            (40, 42, ("Spell Storing", "C")),
            (43, 50, ("Spell Turning", "F")),
            (51, 69, ("Stealth", "B")),
            (70, 72, ("Telekinesis", "C")),
            (73, 74, ("Telepathy", "C")),
            (75, 76, ("Teleportation", "C")),
            (77, 78, ("True Seeing", "D")),
            (79, 88, ("Water Walking", "B")),
            (89, 99, ("Weakness", "C")),
            (100,100,("Wishes", "C")),
        ]
        _MISC_FORMS = {
          "A":[(1,2,"Bell"),(3,5,"Belt"),(6,13,"Boots"),(14,15,"Bowl"),(16,28,"Cloak"),
               (29,31,"Orb"),(32,33,"Drums"),(34,38,"Helm"),(39,43,"Horn"),(44,46,"Lens"),
               (47,49,"Mirror"),(50,67,"Pendant"),(68,100,"Ring")],
          "B":[(1,25,"Boots"),(26,50,"Pendant"),(51,100,"Ring")],
          "C":[(1,40,"Pendant"),(41,100,"Ring")],
          "D":[(1,17,"Lens"),(18,21,"Mirror"),(22,50,"Pendant"),(51,100,"Ring")],
          "E":[(1,40,"Helm"),(41,80,"Pendant"),(81,100,"Ring")],
          "F":[(1,7,"Belt"),(8,38,"Cloak"),(39,50,"Pendant"),(51,100,"Ring")],
          "G":[(1,17,"Bell"),(18,50,"Drums"),(51,100,"Horn")],
          "H":[(1,17,"Bowl"),(18,67,"Orb")]
        }

        def _pick_percent2(table):
            r = _d100()
            for lo, hi, val in table:
                if lo <= r <= hi:
                    return val
            return table[-1][-1]

        def roll_misc():
            sub = _pick_percent(_MISC_SUBTABLE)  
            effects = _MISC_EFFECTS_1 if sub == 1 else _MISC_EFFECTS_2
            (effect, form_col) = _pick_percent2(effects)
            form = _pick_percent2(_MISC_FORMS[form_col])
            return "Miscellaneous", f"{form} of {effect}"
        def roll_weapon():
            wt = _pick_percent(_WEAPON_TYPES)
            table = _MISSILE_BONUS if _is_missile_weapon(wt) else _MELEE_BONUS
            bonus = _pick_percent(table)
            desc = f"{wt} {bonus}"
            special_note = None
            if "Roll Again + Special Ability" in bonus:
                bonus2 = _pick_percent(table)

                if "Roll Again" in bonus2: bonus2 = "+1"
                abil = _ABILITIES[_d(20)]
                desc = f"{wt} {bonus2}"
                special_note = f"Special: {abil}"
            if "Special Enemy" in bonus:
                foe = _ENEMIES[_d(6)]
                desc = f"{wt} {bonus} ({foe})"
            if "Cursed" in bonus:
                special_note = None
            return "Weapon", desc if not special_note else f"{desc} — {special_note}"

        def roll_armor():
            at = _pick_percent(_ARMOR_TYPES)
            ab = _pick_percent(_ARMOR_BONUS)
            if ab == "Cursed*":

                roll2 = _pick_percent([t for t in _ARMOR_BONUS if t[2] in {"+1","+2","+3"}])
                desc = f"{at} {roll2.replace('+','-')}"
            else:
                desc = f"{at} {ab}"
            return "Armor", desc

        def roll_wsr():
            return "Wand/Staff/Rod", _pick_percent(_WSR)

        def roll_rare():
            return "Rare", _pick_percent(_RARE)

        def roll_potion():
            return "Potion", _pick_percent(_POTIONS)

        def roll_scroll():

            if _d(2) == 1:
                grp = random.choice(["Magic-User","Illusionist","Necromancer","Spellcrafter"])
            else:
                grp = random.choice(["Cleric","Druid"])
            names = self._pick_spells(grp, 1)
            spell = names[0] if names else "(no spells found)"
            return "Scroll", f"Spell Scroll ({grp}) — {spell}"

        lines = []

        def take_from(poolname, count, picker):
            nonlocal lines
            for _ in range(count):
                kind = picker()
                if kind == "weapon":
                    k, text = roll_weapon()
                elif kind == "armor":
                    k, text = roll_armor()
                elif kind == "wsr":
                    k, text = roll_wsr()
                elif kind == "rare":
                    k, text = roll_rare()
                elif kind == "potion":
                    k, text = roll_potion()
                elif kind == "misc":
                    k, text = roll_misc()
    
                else:
                    k, text = roll_scroll()
                lines.append(f"• **{k}** — {text}")

        left_any, left_wa, left_xw = pool_any, pool_wa, pool_xw
        want_left = want

        take_a = min(want_left, left_any); want_left -= take_a
        take_w = min(want_left, left_wa);  want_left -= take_w
        take_x = min(want_left, left_xw);  want_left -= take_x

        if take_a: take_from("any", take_a, pick_cat_from_any)
        if take_w: take_from("wa",  take_w, pick_cat_from_wa)
        if take_x: take_from("xw",  take_x, pick_cat_from_xw)

        if use_any: cfg.set(chan_id, "tre_magic_any", str(pool_any - take_a))
        if use_wa:  cfg.set(chan_id, "tre_magic_wa",  str(pool_wa  - take_w))
        if use_xw:  cfg.set(chan_id, "tre_magic_xw",  str(pool_xw  - take_x))
        _save_battles(cfg)

        for chunk in _chunk_send_lines(lines, 12):
            await ctx.send("✨ **Magic Items**\n" + "\n".join(chunk) +
                           f"\n*Remaining:* any={cfg.getint(chan_id,'tre_magic_any',fallback=0)} "
                           f"wa={cfg.getint(chan_id,'tre_magic_wa',fallback=0)} "
                           f"xw={cfg.getint(chan_id,'tre_magic_xw',fallback=0)}")

    @commands.command(name="remindattacks")
    async def remind_attacks(self, ctx, mode: str | None = None):
        """
        Toggle or show the 'DM monster attacks at start of monster's turn' feature
        for the current battle channel. Usage:
          !remindattacks          -> shows current setting
          !remindattacks on       -> enables
          !remindattacks off      -> disables
          !remindattacks toggle   -> toggles
        """
        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here. Use `!init` first.")
            return

        cur = cfg.getint(chan_id, "attacks_hint", fallback=1)

        if mode is None:
            await ctx.send(f"🔔 Monster attack reminders are **{'ON' if cur else 'OFF'}** for this battle.")
            return

        m = (mode or "").strip().lower()
        if m in ("on", "enable", "enabled"):
            newv = 1
        elif m in ("off", "disable", "disabled"):
            newv = 0
        elif m in ("toggle", "switch"):
            newv = 0 if cur else 1
        else:
            await ctx.send("Usage: `!remindattacks [on|off|toggle]`")
            return

        cfg.set(chan_id, "attacks_hint", str(newv))
        _save_battles(cfg)
        await ctx.send(f"✅ Monster attack reminders **{'enabled' if newv else 'disabled'}** for this battle.")

    @commands.command(name="init")
    async def start_battle(self, ctx):
        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)

        if cfg.has_section(chan_id):

            if not cfg.has_option(chan_id, "attacks_hint"):
                cfg.set(chan_id, "attacks_hint", "1")
                _save_battles(cfg)

            dm_id = cfg.get(chan_id, "DM", fallback="")
            round_no = cfg.getint(chan_id, "round", fallback=0)
            turn = cfg.get(chan_id, "turn", fallback="") or "—"
            await ctx.send(
                f"❌ A battle is already running here (GM: {f'<@{dm_id}>' if dm_id else 'unknown'}, "
                f"round {round_no}, turn **{turn}**). Use `!list` or `!end`."
            )
            return

        cfg.add_section(chan_id)
        cfg.set(chan_id, "DM", str(ctx.author.id))
        cfg.set(chan_id, "round", "1")
        cfg.set(chan_id, "turn", "")
        cfg.set(chan_id, "list", "")
        cfg.set(chan_id, "join_seq", "0")
        cfg.set(chan_id, "message_id", "0")
        cfg.set(chan_id, "attacks_hint", "1")
        _save_battles(cfg)

        block = _format_tracker_block(cfg, chan_id)
        msg = await ctx.send("**EVERYONE ROLL FOR INITIATIVE!**\n```text\n" + block + "\n```")
        try:
            await msg.pin()
        except Exception:
            pass

        cfg.set(chan_id, "message_id", str(msg.id))
        _save_battles(cfg)
        await self._update_tracker_message(ctx, cfg, chan_id)

    @commands.command(name="join")
    async def join_initiative(self, ctx, *, name: str | None = None):

        if name is None:
            char_name = get_active(ctx.author.id)
            if not char_name:
                await ctx.send("❌ No active character. Use `!char <name>` or pass a name.")
                return
            join_name = char_name
        else:
            join_name = name

        coe = f"{join_name.replace(' ', '_')}.coe"
        is_halfling = False
        if os.path.exists(coe):
            config = read_cfg(coe)
            owner_id = get_compat(config, "info", "owner_id", fallback="")
            if owner_id and owner_id != str(ctx.author.id):
                await ctx.send(f"❌ You do not own '{join_name}'.")
                return
            race_raw = get_compat(config, "info", "race", fallback="")
            is_halfling = (race_raw.strip().lower() == "halfling")

        dex_mod = _dex_mod_from_char(join_name)
        d6 = random.randint(1, 6)
        total = d6 + dex_mod + (1 if is_halfling else 0)

        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)

        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here. Use `!init` first.")
            return

        names, scores = _parse_combatants(cfg, chan_id)
        if join_name not in names:
            names.append(join_name)
        scores[join_name] = total
        _write_combatants(cfg, chan_id, names, scores)

        s = _slot(join_name)
        cfg.set(chan_id, f"{s}.dex", str(dex_mod))
        seq = cfg.getint(chan_id, "join_seq", fallback=0) + 1
        cfg.set(chan_id, "join_seq", str(seq))
        cfg.set(chan_id, f"{s}.join", str(seq))
        _save_battles(cfg)

        await self._update_tracker_message(ctx, cfg, chan_id)

        parts = []
        if dex_mod:
            parts.append(f"+ {dex_mod}")
        if is_halfling:
            parts.append("+ 1 (Halfling)")
        mod_txt = (" " + " ".join(parts)) if parts else ""

        await ctx.send(f"📝 **{join_name}** joins initiative: 1d6 = {d6}{mod_txt} → **{total}**")

    @commands.command(name="list")
    async def show_initiative(self, ctx):
        """Repost the current pinned initiative block to the channel."""
        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("No initiative running here.")
            return

        block = _format_tracker_block(cfg, chan_id)

        await ctx.send("\n```text\n" + block + "\n```")

    @commands.command(name="leave")
    async def leave_initiative(self, ctx, *, name: str | None = None):
        if not name:
            char_name = get_active(ctx.author.id)
            name = char_name if char_name else ctx.author.display_name

        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("No initiative running here.")
            return

        names, scores = _parse_combatants(cfg, chan_id)
        if name not in names:
            await ctx.send(f"{name} wasn’t in initiative.")
            return

        names = [n for n in names if n != name]
        if cfg.has_option(chan_id, name):
            cfg.remove_option(chan_id, name)

        s = _slot(name)
        for suf in (".dex", ".join", ".disp"):
            opt = f"{s}{suf}"
            if cfg.has_option(chan_id, opt):
                cfg.remove_option(chan_id, opt)

        _write_combatants(cfg, chan_id, names, scores)

        cur_turn = cfg.get(chan_id, "turn", fallback="")
        if cur_turn == name:
            ents = _sorted_entries(cfg, chan_id)
            cfg.set(chan_id, "turn", ents[0]["name"] if ents else "")

        _save_battles(cfg)
        await self._update_tracker_message(ctx, cfg, chan_id)
        await ctx.send(f"↩️ **{name}** removed from initiative.")

    def _quick_status_tail(self, cfg, chan_id: str, who: str) -> str:
        """Lightweight status-tail builder used in !n/!p announcements."""
        try:
            names, _ = _parse_combatants(cfg, chan_id)
            key = _find_ci_name(names, who) or who
            try:
                slot = _slot(key)
            except Exception:
                slot = key.replace(" ", "_")
        except Exception:
            slot = who.replace(" ", "_")

        def geti(tag, default=0):
            opt = f"{slot}.{tag}"
            try:
                return cfg.getint(chan_id, opt, fallback=default)
            except Exception:
                return default

        tags = []
        def add(tag, code):
            v = geti(tag, 0)
            if v and v > 0:
                tags.append(f"{code} {v}")

        if geti("pet", 0) or geti("pet_perm", 0):
            tags.append("PET –")

        add("db",        "DB")
        add("inw",       "INW")
        add("magwep",    "MAG")
        add("magearmor", "MA")
        add("boneskin",  "BS")
        add("shield",    "SHD")
        add("blind",     "BLIND")
        add("paralyzed", "PAR")
        add("hyp",       "HYP")
        add("ghh",       "GHH")
        add("shl",       "SHL")
        add("light",     "LIGHT")
        add("darkness",  "DARK")
        add("cc",        "CC")
        add("ck",        "CK")
        add("cn",        "CN")
        add("sph",       "SPH")
        add("pnm",       "PNM")
        add("fear",      "FR")
        add("gas", "GAS")
        add("ps",        "PS")
        add("swd", "SWD")
        add("chl", "CHL")
        add("cr",  "CR")
        add("gcr", "GCR")
        add("stench", "STN")
        mgi = geti("maggots", 0)
        if mgi and mgi > 0:
            tags.append(f"MG×{mgi}")

        try:
            turn_name = cfg.get(chan_id, "turn", fallback="")
        except Exception:
            turn_name = ""
        try:
            hm = geti("x_heatmetal", 0)
        except Exception:
            hm = 0
        try:
            cm = geti("x_chillmetal", 0)
        except Exception:
            cm = 0
        if hm and hm > 0:
            show = max(1, hm - 1) if who == turn_name else hm
            tags.append(f"HM {show}/7")
        if cm and cm > 0:
            show = max(1, cm - 1) if who == turn_name else cm
            tags.append(f"CM {show}/7")

        mi = geti("mi_images", 0)
        if mi and mi > 0:
            tags.append(f"MI×{mi}")

        sv = geti("x_slowvenom", 0)
        if sv and sv > 0:
            tags.append(f"SV {sv}")

        rc = geti("rc", 0)
        if rc > 0: tags.append(f"RC {rc}")
        rf = geti("rf", 0)
        if rf > 0: tags.append(f"RF {rf}")

        pfi = geti("pfi", 0) or geti("pfire", 0)
        if pfi > 0: tags.append(f"FI {pfi}")  

        pl = geti("pl", 0) or geti("plight", 0)
        if pl > 0: tags.append(f"PL {pl}")

        try:
            try:
                perm_codes = _perm_codes_for_slot(cfg, chan_id, slot)
            except Exception:
                perm_codes = set()

            for opt_key, _ in list(cfg.items(chan_id)):
                if not opt_key.startswith(f"{slot}.x_"):
                    continue
                base_key = opt_key.split(".", 1)[1]
                if any(base_key.endswith(suf) for suf in ("_label","_emoji","_code","_by","_perm")):
                    continue

                raw_left = geti(base_key, 0)
                left     = max(0, raw_left)

                code  = (cfg.get(chan_id, f"{slot}.{base_key}_code",  fallback="") or "").strip()
                label = (cfg.get(chan_id, f"{slot}.{base_key}_label", fallback="") or "").strip()
                display_code = "" if code.lower() == "perm" else code
                tag = (display_code or label or base_key[2:4].upper()).upper()

                if tag in perm_codes:
                    continue

                is_perm_flag = cfg.getint(chan_id, f"{slot}.{base_key}_perm", fallback=0) > 0
                is_perm      = (raw_left < 0) or is_perm_flag or (code.lower() == "perm")

                if is_perm:
                    tags.append(f"{tag} –")
                elif left > 0:
                    tags.append(f"{tag} {left}")
                else:
                    tags.append(f"{tag}")
        except Exception:
            pass


        return (" • [" + "] [".join(tags) + "]") if tags else ""

    def _status_tail_for_actor(self, cfg, chan_id: str, name: str) -> str:
        """
        Build the same compact status tags shown in the tracker line,
        so we can append them to the 'current turn' message.
        """

        slot = _choose_slot_for_effects(cfg, chan_id, name)
        perm_codes = _perm_codes_for_slot(cfg, chan_id, slot)
        disp = cfg.get(chan_id, f"{slot}.disp", fallback=name)

        def _find_file_ci(nm: str) -> str | None:
            base = (nm or "").replace(" ", "_").lower()
            want = f"{base}.coe"
            for fn in os.listdir("."):
                if fn.lower() == want:
                    return fn
            return None

        path = _find_file_ci(disp) or _find_file_ci(name)
        pcfg = None
        if path:
            try:
                pcfg = read_cfg(path)
            except Exception:
                pcfg = None

        tail = ""

        par = cfg.getint(chan_id, f"{slot}.paralyzed", fallback=0)
        if par > 0:
            ccq = cfg.getint(chan_id, f"{slot}.cc_blind_pending", fallback=0)
            csq = cfg.getint(chan_id, f"{slot}.cs_blind_pending", fallback=0)
            spq = cfg.getint(chan_id, f"{slot}.sp_blind_pending", fallback=0)
            pend = max(ccq, csq, spq)
            tail += f" • [PAR {par} → BL {pend}]" if pend > 0 else f" • [PAR {par}]"

        sh  = cfg.getint(chan_id, f"{slot}.shield",    fallback=0)
        mi  = cfg.getint(chan_id, f"{slot}.mi_images", fallback=0)
        mw  = cfg.getint(chan_id, f"{slot}.magwep",    fallback=0)
        if sh > 0: tail += f" • [SH {sh}]"
        if mi > 0: tail += f" • [MI {mi}]"
        if mw > 0: tail += f" • [MW {mw}]"

        pet = cfg.getint(chan_id, f"{slot}.pet", fallback=0)
        pet_perm = cfg.getint(chan_id, f"{slot}.pet_perm", fallback=0)
        if pet > 0 or pet_perm:
            tail += " • [PET –]"

        for key, code in (
            ("x_detectmagic_perm",        "DM"),
            ("x_protectionfromevil_perm", "PF"),
            ("x_readlanguages_perm",      "RL"),
            ("x_readmagic_perm",          "RM"),
            ("x_detectinvisible_perm",    "DI"),
            ("x_fly_perm",                "FL"),
        ):
            if cfg.getint(chan_id, f"{slot}.{key}", fallback=0) > 0:
                tail += f" • [{code} –]"

        try:
            nl = getint_compat(pcfg, "cur", "neg_levels", fallback=0) if pcfg else 0
        except Exception:
            nl = 0
        if nl > 0:
            tail += f" • [NL {nl}]"

        try:
            dis = getint_compat(pcfg, "cur", "disease", fallback=0) if pcfg else 0
        except Exception:
            dis = 0
        if dis > 0:
            tail += " • [DIS]"

        try:
            sw = cfg.getint(chan_id, f"{slot}.sph", fallback=0)
        except Exception:
            sw = 0
        if sw > 0:
            try:
                bon = cfg.getint(chan_id, f"{slot}.sph_bonus", fallback=0)
            except Exception:
                bon = 0
            tail += f" • [SW {sw}{('+'+str(bon)) if bon else ''}]"

        try:
            swd = cfg.getint(chan_id, f"{slot}.swd", fallback=0)
        except Exception:
            swd = 0
        if swd > 0:
            tail += f" • [SWD {swd}]"

        ghh = cfg.getint(chan_id, f"{slot}.ghh", fallback=0)
        if ghh > 0:
            tail += f" • [GH {ghh}]"
        gf  = cfg.getint(chan_id, f"{slot}.ghf", fallback=0)
        if gf:
            tail += " • [GF]"

        ma = cfg.getint(chan_id, f"{slot}.magearmor", fallback=0)
        if ma > 0:
            tail += f" • [MA {ma}]"
        bs = cfg.getint(chan_id, f"{slot}.boneskin", fallback=0)
        if bs > 0:
            tail += f" • [BS {bs}]"

        try:
            if pcfg and _pc_has_regen_item(pcfg):
                tail += " • [RG –]"
        except Exception:
            pass


        try:
            fv = cfg.getint(chan_id, f"{slot}.x_fastvenom", fallback=0)
        except Exception:
            fv = 0
        if fv > 0:
            tail += f" • [FV {fv}]"

        sl = cfg.getint(chan_id, f"{slot}.shl", fallback=0)
        if sl > 0:
            tail += f" • [SL {sl}+1]"

        now_r = cfg.getint(chan_id, "round", fallback=0)

        ko_left  = cfg.getint(chan_id, f"{slot}.ko", fallback=0)
        ko_ready = cfg.getint(chan_id, f"{slot}.ko_ready", fallback=0)

        if ko_left > 0:

            if ko_ready and now_r < ko_ready:
                tail +=f" [KO {ko_ready - now_r}]"
            else:
                tail +=" [KO ✓]"

        bl_left = cfg.getint(chan_id, f"{slot}.blind", fallback=0)
        bl_perm = cfg.getint(chan_id, f"{slot}.blind_perm", fallback=0)
        if bl_left > 0 or bl_perm:
            bl_src = (cfg.get(chan_id, f"{slot}.blind_src", fallback="") or "").lower()
            src_code = ("C"  if bl_src == "colorspray" else
                        "CC" if bl_src == "colorcloud" else
                        "L"  if bl_src == "light" else
                        "CL" if bl_src == "clight" else
                        "D"  if bl_src == "darkness" else
                        "CD" if bl_src == "cdarkness" else "")
            amt = "–" if bl_perm else str(bl_left)
            tail += f" • [BL {src_code+' ' if src_code else ''}{amt}]"

        lt = cfg.getint(chan_id, f"{slot}.light", fallback=0)
        lt_perm = cfg.getint(chan_id, f"{slot}.light_perm", fallback=0)
        if lt > 0 or lt_perm:
            tail += f" • [LT {'–' if lt_perm else lt}]"

        dk = cfg.getint(chan_id, f"{slot}.darkness", fallback=0)
        dk_perm = cfg.getint(chan_id, f"{slot}.darkness_perm", fallback=0)
        if dk > 0 or dk_perm:
            tail += f" • [DK {'–' if dk_perm else dk}]"

        heldby = (cfg.get(chan_id, f"{slot}.heldby", fallback="") or "").strip()
        if heldby:
            tail += " • [HELD]"

        try:
            web_left = cfg.getint(chan_id, f"{slot}.web", fallback=0)
        except Exception:
            web_left = 0
        if web_left > 0:
            st  = (cfg.get(chan_id, f"{slot}.web_state",  fallback="E") or "E").upper()
            ign =  cfg.get(chan_id, f"{slot}.webignite",   fallback="") == "1"
            stc = "T" if st == "T" else "E"
            tail += f" • [WEB {stc} {web_left}{'🔥' if ign else ''}]"

        try:
            slot_for_badges = _choose_slot_for_effects(cfg, chan_id, name)
        except Exception:
            slot_for_badges = (name or "").replace(" ", "_")

        tail += _hmcm_badges(cfg, chan_id, slot_for_badges, name)

        cc_left = cfg.getint(chan_id, f"{slot}.cc", fallback=0)
        if cc_left > 0:
            tail += f" • [CC {cc_left}]"

        ck_left = cfg.getint(chan_id, f"{slot}.ck", fallback=0)
        if ck_left > 0:
            tail += f" • [CK {ck_left}]"

        sc_left = cfg.getint(chan_id, f"{slot}.sc", fallback=0)
        if sc_left > 0:
            tail += f" • [SC {sc_left}]"

        try:
            chl = cfg.getint(chan_id, f"{slot}.chl", fallback=0)
        except Exception:
            chl = 0
        if chl > 0:
            tail += f" • [CHL {chl}]"

        try:
            cr = cfg.getint(chan_id, f"{slot}.cr", fallback=0)
        except Exception:
            cr = 0
        if cr > 0:
            tail += f" • [CR {cr}]"

        try:
            mg = cfg.getint(chan_id, f"{slot}.maggots", fallback=0)
        except Exception:
            mg = 0
        if mg > 0:
            tail += f" • [MG {mg}]"

        try:
            stn = cfg.getint(chan_id, f"{slot}.stench", fallback=0)
        except Exception:
            stn = 0
        if stn > 0:
            tail += f" • [STN {stn}]"

        fr = cfg.getint(chan_id, f"{slot}.fear", fallback=0)
        if fr > 0:
            tail += f" • [FR {fr}]"

        try:
            hyp = cfg.getint(chan_id, f"{slot}.hyp", fallback=-999)
        except Exception:
            hyp = -999
        if hyp != -999:
            tail += f" • [HYP {hyp}]" if hyp > 0 else " • [HYP]"

        try:
            inv_perm = cfg.getint(chan_id, f"{slot}.inv_perm", fallback=0)
        except Exception:
            inv_perm = 0
        inv_left = cfg.getint(chan_id, f"{slot}.inv", fallback=0)
        if inv_perm:
            tail += " • [INV –]"
        elif inv_left > 0:
            tail += f" • [INV {inv_left}]"

        ps_left = cfg.getint(chan_id, f"{slot}.ps", fallback=0)
        if ps_left > 0:
            tail += f" • [PS {ps_left}]"

        try:
            cur_perm = cfg.getint(chan_id, f"{slot}.curse_perm", fallback=0)
            cur_left = cfg.getint(chan_id, f"{slot}.curse",      fallback=0)
        except Exception:
            cur_perm = cur_left = 0
        if cur_perm or cur_left > 0:
            tail += " • [BC –]"

        try:
            fb_perm = cfg.getint(chan_id, f"{slot}.feeble_perm", fallback=0)
            fb_left = cfg.getint(chan_id, f"{slot}.feeble",      fallback=0)
        except Exception:
            fb_perm = fb_left = 0
        if fb_perm or fb_left > 0:
            tail += " • [FB –]"

        try:
            if pcfg and str(get_compat(pcfg, "poly", "active", fallback="0")).strip() == "1":
                knd = (get_compat(pcfg, "poly", "kind", fallback="") or "").strip().lower()
                if knd == "other":
                    form = (get_compat(pcfg, "poly", "form", fallback="") or "").strip()
                    tail += f" • [PO –{(' '+form) if form else ''}]"

        except Exception:
            pass

        cl_left = cfg.getint(chan_id, f"{slot}.cl", fallback=0)
        if cl_left > 0:
            cl_bolts = cfg.getint(chan_id, f"{slot}.cl_bolts", fallback=0)
            cl_die   = (cfg.get(chan_id, f"{slot}.cl_die", fallback="3d6") or "3d6").strip()
            tail += (f" • [CL {cl_left}/{cl_bolts} 3d8]" if cl_die == "3d8"
                     else f" • [CL {cl_left}/{cl_bolts}]")

        try:
            rk, pk, _ = _stone_keys(slot)
        except Exception:
            rk = f"{slot}.ss_rounds"
            pk = f"{slot}.ss_pool"

        def _ri(opt: str, d: int = 0) -> int:
            try:
                return int(str(cfg.get(chan_id, opt, fallback=str(d))).strip() or d)
            except Exception:
                return d

        _ss_normalize(cfg, chan, slot)
        ss_dur, ss_hp = _ss_state(cfg, chan, slot)
        if ss_hp > 0:
            tail += f" • [SS {ss_hp}]"

        try:
            cn_perm = cfg.getint(chan_id, f"{slot}.cn_perm", fallback=0)
        except Exception:
            cn_perm = 0
        cn_left = cfg.getint(chan_id, f"{slot}.cn", fallback=0)
        if cn_perm:
            tail += " • [CN –]"
        elif cn_left > 0:
            tail += f" • [CN {cn_left}]"

        db_left = cfg.getint(chan_id, f"{slot}.db", fallback=0)
        if db_left > 0:
            tail += f" • [DB {db_left}]"

        inw = cfg.getint(chan_id, f"{slot}.inw", fallback=0)
        if inw > 0:
            tail += f" • [INW {inw}]"

        pnm = cfg.getint(chan_id, f"{slot}.pnm", fallback=0)
        if pnm > 0:
            tail += f" • [PNM {pnm}]"

        gas_left = cfg.getint(chan_id, f"{slot}.gas", fallback=0)
        if gas_left > 0:
            tail += f" • [GAS {gas_left}]"

        tail += _rotgrubs_badge(cfg, chan_id, slot)

        tail += _resist_protection_tags(cfg, chan_id, slot)


        try:
            for opt_key, _val in list(cfg.items(chan_id)):
                if not opt_key.startswith(f"{slot}.x_"):
                    continue
                base_key = opt_key.split(".", 1)[1]
                if any(base_key.endswith(suf) for suf in ("_label","_emoji","_code","_by","_perm")):
                    continue

                if any(base_key.endswith(suf) for suf in ("_label","_emoji","_code","_by","_perm")):
                    continue

                if base_key in _X_SKIP_GENERIC:
                    continue

                raw_left = cfg.getint(chan_id, opt_key, fallback=0)
                left     = max(0, raw_left)

                code  = (cfg.get(chan_id, f"{slot}.{base_key}_code",  fallback="") or "").strip()
                label = (cfg.get(chan_id, f"{slot}.{base_key}_label", fallback="") or "").strip()

                display_code = "" if code.lower() == "perm" else code
                tag = (display_code or label or base_key[2:4].upper()).upper()

                if tag in perm_codes:
                    continue

                is_perm_flag = cfg.getint(chan_id, f"{slot}.{base_key}_perm", fallback=0) > 0
                is_perm      = (raw_left < 0) or is_perm_flag or (code.lower() == "perm")

                if is_perm:
                    tail += f" • [{tag} –]"
                elif left > 0:
                    tail += f" • [{tag} {left}]"
                else:
                    tail += f" • [{tag}]"
        except Exception:
            pass




        try:
            eff_str = getint_compat(pcfg, "stats", "str", fallback=None)
            str_temp = getint_compat(pcfg, "cur", "str_loss_temp", fallback=0)
        except Exception:
            eff_str = None
            str_temp = 0

        if str_temp > 0:
            tail += f" • [STR–{str_temp}]"
        if eff_str is not None and eff_str <= 2:
            tail += " • [COLLAPSED]"

        return tail

    @commands.command(name="n")
    async def next_turn(self, ctx):
        cfg = _load_battles()
        chan_id = str(ctx.channel.id)
        if not cfg.has_section(chan_id):
            await ctx.send("No initiative running here.")
            return

        names, scores = _parse_combatants(cfg, chan_id)
        if not names:
            await ctx.send("No combatants yet.")
            return

        dm_id = cfg.get(chan_id, "DM", fallback="")
        ordered = _sorted_names(names, scores, cfg, chan_id)

        cur = (cfg.get(chan_id, "turn", fallback="") or "").strip()
        round_num = cfg.getint(chan_id, "round", fallback=0)

        def _announce(who: str, rn: int):
            ini_score = scores.get(who, 0)
            hp, mhp, ac, owner = _char_snapshot(who)
            if _is_monster(who):
                hp_disp = _life_bar(hp, mhp, width=10)
                stat_line = f"{who} • HP {hp_disp}"
            else:
                hp_txt = f"{hp}/{mhp}" if (hp is not None and mhp is not None) else "?"
                stat_line = f"{who} • HP {hp_txt} • AC {ac}"

            tail = ""
            try:
                tail = self._status_tail_for_actor(cfg, chan_id, who)
            except Exception:
                tail = ""

            if not tail:

                tail = self._quick_status_tail(cfg, chan_id, who)

            stat_line += tail

            mention = f"<@{owner}>" if owner and str(owner).isdigit() else ""
            header = f"**Initiative {ini_score} (round {rn})**: {who} {mention}".strip()
            body   = f"```ini\n{stat_line}\n```"
            return header, body

        if round_num == 0 or not cur:
            top = ordered[0]
            cfg.set(chan_id, "turn", top)
            cfg.set(chan_id, "round", "1")
            _save_battles(cfg)

            changed = self._tick_start_of_turn(cfg, chan_id, top)
            if changed:
                await self._update_tracker_message(ctx, cfg, chan_id)

            await _maybe_dm_monster_attacks(self, ctx, cfg, chan_id, top)

            header, body = _announce(top, 1)
            await ctx.send(f"{header}\n{body}")
            return

        _, _, _, owner = _char_snapshot(cur)
        if str(ctx.author.id) != str(dm_id) and str(ctx.author.id) != str(owner):
            await ctx.send("❌ Only the DM or the current character's owner can advance the turn.")
            return

        try:
            i = ordered.index(cur)
        except ValueError:
            i = -1
        next_i = (i + 1) % len(ordered)
        next_name = ordered[next_i]
        wrapped_now = (next_i == 0)
        if wrapped_now:
            round_num += 1

        cfg.set(chan_id, "round", str(round_num))
        cfg.set(chan_id, "turn", next_name)

        if wrapped_now and (round_num % 60 == 0):
            await ctx.send("⌛ **A full turn passes.**")
            try:
                await self._apply_turn_disease_local(ctx, cfg, chan_id)
                await self._apply_turn_strength_recovery(ctx, cfg, chan_id)
                await self._update_tracker_message(ctx, cfg, chan_id)
            except Exception:
                pass

        _save_battles(cfg)

        changed = self._tick_start_of_turn(cfg, chan_id, next_name)
        if changed:
            await self._update_tracker_message(ctx, cfg, chan_id)

        while True:

            notes = []
            still_par, note_par = _tick_paralyze_on_turn(cfg, chan_id, next_name)
            if note_par: notes.append(note_par)
            still_cnf, note_cnf = self._tick_confusion_on_turn(cfg, chan_id, next_name)
            if note_cnf: notes.append(note_cnf)
            still_maze, note_maze = self._tick_maze_on_turn(cfg, chan_id, next_name)
            if note_maze:
                notes.append(note_maze)
            _active, note_sh = _tick_status_counter(cfg, chan_id, next_name, "shield", "Shield")
            if note_sh: notes.append(note_sh)

            _active, note_fear = _tick_status_counter(cfg, chan_id, next_name, "fear", "Frightened")
            if note_fear: notes.append(note_fear)
            _active, note_ps = _tick_status_counter(cfg, chan_id, next_name, "ps", "Polymorph Self")
            if note_ps: notes.append(note_ps)
            _active, note_ss = _tick_stoneskin_on_turn(cfg, chan_id, next_name)
            if note_ss: notes.append(note_ss)

            _, note_mi = _tick_mirror_on_turn(cfg, chan_id, next_name)
            if note_mi: notes.append(note_mi)
            if notes:
                await self._update_tracker_message(ctx, cfg, chan_id)
                for nline in notes:
                    await ctx.send(nline)

            applied_cnf, note_sp = _apply_queued_confusion_after_blind(cfg, chan_id, next_name)
            if note_sp:
                await ctx.send(note_sp)
            if applied_cnf > 0:
                await self._update_tracker_message(ctx, cfg, chan_id)

            died = await self._process_start_of_turn_effects(ctx, cfg, chan_id, next_name)
            if not died:
                died = await self._process_start_of_turn_web_bridge(ctx, cfg, chan_id, next_name)

            if not died:
                await self._update_tracker_message(ctx, cfg, chan_id)

                await _maybe_dm_monster_attacks(self, ctx, cfg, chan_id, next_name)

                header, body = _announce(next_name, round_num)
                await ctx.send(f"{header}\n{body}")
                break

            names2, scores2 = _parse_combatants(cfg, chan_id)
            if not names2:
                cfg.set(chan_id, "turn", "")
                _save_battles(cfg)
                await self._update_tracker_message(ctx, cfg, chan_id)
                await ctx.send("No combatants remain.")
                return

            ordered_prev = _sorted_names(names, scores, cfg, chan_id)
            try:
                prev_idx = ordered_prev.index(next_name)
            except ValueError:
                prev_idx = -1
            was_last = (prev_idx == len(ordered_prev) - 1)

            ordered2 = _sorted_names(names2, scores2, cfg, chan_id)
            if was_last:
                round_num += 1
                cfg.set(chan_id, "round", str(round_num))
                pick_idx = 0
            else:
                pick_idx = min(max(prev_idx, 0), len(ordered2) - 1)

            next_name = ordered2[pick_idx]
            cfg.set(chan_id, "turn", next_name)
            _save_battles(cfg)

            names, scores = names2, scores2

    @commands.command(name="p")
    async def previous_turn(self, ctx):
        """
        Go back one actor in initiative.
        Restores one round to per-round timers that were decremented on !n
        for the *current* actor, then moves the turn pointer back.
        """
        cfg = _load_battles()
        chan_id = str(ctx.channel.id)
        if not cfg.has_section(chan_id):
            await ctx.send("No initiative running here.")
            return

        names, scores = _parse_combatants(cfg, chan_id)
        if not names:
            await ctx.send("No combatants yet.")
            return

        dm_id = cfg.get(chan_id, "DM", fallback="")
        ordered = _sorted_names(names, scores, cfg, chan_id)

        cur = (cfg.get(chan_id, "turn", fallback="") or "").strip()
        round_num = cfg.getint(chan_id, "round", fallback=0)

        if round_num == 0 or not cur:
            await ctx.send("❌ You can’t go back — the round hasn’t started yet.")
            return

        _, _, _, owner = _char_snapshot(cur)
        if str(ctx.author.id) != str(dm_id) and str(ctx.author.id) != str(owner):
            await ctx.send("❌ Only the DM or the current character's owner can rewind the turn.")
            return

        try:
            i = ordered.index(cur)
        except ValueError:
            i = 0

        wrapped_back = (i == 0)

        if wrapped_back and round_num <= 1:
            await ctx.send("⛔ You’re at the first actor of round 1 — can’t go back further.")
            return

        try:

            try:
                ini_names, _ = _parse_combatants(cfg, chan_id)
            except Exception:
                ini_names = []
            key = _find_ci_name(ini_names, cur) or cur
            try:
                slot = _slot(key)
            except Exception:
                slot = key.replace(" ", "_")

            def _inc(tag: str):
                if _is_perm(cfg, chan_id, slot, tag):
                    return
                opt = f"{slot}.{tag}"
                if cfg.has_option(chan_id, opt):
                    cfg.set(chan_id, opt, str(cfg.getint(chan_id, opt, fallback=0) + 1))

            for tag in ("magwep","ghh","magearmor","boneskin","sph","swd","shl","light","darkness",
            "blind","cc","ck","cl","cn","db","inw","pnm","fear","inv","gas","sc",
            "chl","cr","gcr","stench","ps","stone"):
                _inc(tag)

        except Exception:
            pass

        prev_i = (i - 1) % len(ordered)
        prev_name = ordered[prev_i]

        if wrapped_back:
            round_num = max(1, round_num - 1)

        cfg.set(chan_id, "round", str(round_num))
        cfg.set(chan_id, "turn", prev_name)
        _save_battles(cfg)

        try:
            await self._update_tracker_message(ctx, cfg, chan_id)
        except Exception:
            pass

        def _announce(who: str, rn: int):
            ini_score = scores.get(who, 0)
            hp, mhp, ac, owner_id = _char_snapshot(who)
            if _is_monster(who):
                hp_disp = _life_bar(hp, mhp, width=10)
                stat_line = f"{who} • HP {hp_disp}"
            else:
                hp_txt = f"{hp}/{mhp}" if (hp is not None and mhp is not None) else "?"
                stat_line = f"{who} • HP {hp_txt} • AC {ac}"

            tail = ""
            try:
                tail = self._status_tail_for_actor(cfg, chan_id, who)
            except Exception:
                tail = ""

            if not tail:

                tail = self._quick_status_tail(cfg, chan_id, who)

            stat_line += tail

            mention = f"<@{owner_id}>" if owner_id and str(owner_id).isdigit() else ""
            header = f"**Initiative {ini_score} (round {rn})**: {who} {mention}".strip()
            body   = f"```ini\n{stat_line}\n```"
            return header, body

        header, body = _announce(prev_name, round_num)
        await ctx.send(f"⏪ **Rewound turn.**\n{header}\n{body}")

    @commands.command(name="end")
    async def end_battle(self, ctx):
        """Unpin tracker, clear this channel's section, and sweep leftover monster files."""
        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("No initiative running here.")
            return

        msg_id = cfg.getint(chan_id, "message_id", fallback=0)
        if msg_id:
            try:
                msg = await ctx.channel.fetch_message(msg_id)
                await msg.unpin(reason="End battle")
            except Exception:
                pass

        names, _ = _parse_combatants(cfg, chan_id)
        listed_monsters = set(n for n in names if _is_monster(n))

        tagged_monsters = set()
        for fn in os.listdir("."):
            if not fn.lower().endswith(".coe"):
                continue
            try:
                cp = read_cfg(fn)
                cls = str(get_compat(cp, "info", "class", fallback="")).strip().lower()
                bch = str(get_compat(cp, "info", "battle_chan", fallback="")).strip()
                if cls == "monster" and bch == chan_id:

                    disp = get_compat(cp, "info", "name", fallback=fn[:-4].replace("_", " "))
                    tagged_monsters.add(disp)
            except Exception:
                continue

        to_delete_names = listed_monsters | tagged_monsters

        cfg.remove_section(chan_id)
        _save_battles(cfg)

        deleted = []
        for name in sorted(to_delete_names):
            path = f"{name.replace(' ', '_')}.coe"
            if os.path.exists(path):
                try:
                    os.remove(path)
                    deleted.append(name)
                except Exception:
                    pass

        if deleted:
            await ctx.send("🧹 Initiative cleared. Removed monsters: " + ", ".join(f"**{n}**" for n in deleted))
        else:
            await ctx.send("🧹 Initiative cleared.")

    async def _refresh_initiative_if_listed(self, ctx, char_name: str):
        """If this channel has a battle and the char is listed, refresh the pinned tracker."""
        cfg_b = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg_b.has_section(chan_id):
            return
        names, _ = _parse_combatants(cfg_b, chan_id)
        if char_name in names:
            await self._update_tracker_message(ctx, cfg_b, chan_id)

    @commands.command(name="heal")
    async def restore_hp(self, ctx, *who_parts: str, **_kw):
        """
        Refill a character's HP to max. DM/Owner-only.
        Usage: !heal Testman
        """

        chan_id = _section_id(ctx.channel)

        raw  = (getattr(ctx.message, "content", "") or "").strip().lower()
        pref = (ctx.prefix or "").lower()
        if raw.startswith(f"{pref}cast "):
            return

        who = _kw.get("who") or " ".join(who_parts).strip()
        if not who:
            await ctx.send("❌ Usage: `!heal <name>`")
            return

        def _resolve_char_ci(name: str):
            base = name.replace(" ", "_")
            target = f"{base}.coe".lower()
            for fn in os.listdir("."):
                if fn.lower() == target:
                    path = fn
                    try:
                        cfg2 = read_cfg(path)
                        real = get_compat(cfg2, "info", "name", fallback=None)
                        return (real or fn[:-4].replace("_", " ")), path
                    except Exception:
                        return fn[:-4].replace("_", " "), path
            return None, None

        try:
            disp_name, path = _resolve_char_ci(who)
        except Exception:
            disp_name, path = who, None

        if not path or not os.path.exists(path):
            await ctx.send(f"❌ Character '{who}' does not exist.")
            return

        cfg = read_cfg(path)

        owner_id = str(get_compat(cfg, "info", "owner_id", fallback="") or "").strip()

        is_dm = False

        dm_id = cfg.get(chan_id, "DM", fallback="")
        is_dm = (str(ctx.author.id) == str(dm_id))
        if not is_dm:
            try:
                if getattr(ctx, "guild", None) is None:
                    is_dm = True
                elif ctx.author.guild_permissions.administrator:
                    is_dm = True
                else:
                    role_names = {r.name.lower() for r in getattr(ctx.author, "roles", [])}
                    if any(n in role_names for n in ("dm", "dungeon master", "game master", "gm")):
                        is_dm = True
            except Exception:
                pass

        if owner_id and (owner_id != str(ctx.author.id)) and not is_dm:
            await ctx.send(f"❌ Only the character’s owner or a DM can heal **{disp_name}**.")
            return

        old_hp = getint_compat(cfg, "cur", "hp", fallback=0)
        max_hp = getint_compat(cfg, "max", "hp", fallback=old_hp)
        if not cfg.has_section("cur"):
            cfg.add_section("cur")
        cfg["cur"]["hp"] = str(max_hp)
        write_cfg(path, cfg)

        await ctx.send(f"✅ **{disp_name}** healed to **{max_hp} HP** (was {old_hp}).")

        await self._refresh_initiative_if_listed(ctx, disp_name)

    @commands.command(name="hp")
    async def adjust_hp(self, ctx, amt: str = None):
        """
        Adjust your ACTIVE character's HP, or show it with no args.
        Examples:
          !hp                 (show current / max)
          !hp +5              (heal 5)
          !hp -3              (take 3 damage)
          !hp =12             (set to 12)
          !hp 12              (set to 12)
          !hp +1d6+3          (heal 1d6+3)
          !hp -1d4            (take 1d4)
          !hp =2d8+1          (set to 2d8+1)
          !hp 1d10+2          (set to 1d10+2)
        """

        char_name = get_active(ctx.author.id)
        if not char_name:
            await ctx.send("❌ No active character. Use `!char <name>` first.")
            return

        file_name = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(file_name):
            await ctx.send(f"❌ Character file not found for **{char_name}**.")
            return

        cfg = read_cfg(file_name)
        owner_id = get_compat(cfg, "info", "owner_id", fallback="")
        if owner_id and owner_id != str(ctx.author.id):
            await ctx.send(f"❌ You do not own **{char_name}**.")
            return

        cur_hp = getint_compat(cfg, "cur", "hp", fallback=0)
        max_hp = getint_compat(cfg, "max", "hp", fallback=cur_hp)

        if amt is None:
            await ctx.send(f"❤️ **{char_name}** HP **{cur_hp} / {max_hp}**")
            return

        s = amt.strip().replace(" ", "")

        dice_re = re.compile(r'(?i)(\d*)d(\d+)')

        def _eval_dice_math(expr: str):
            """
            Expand all NdM into summed rolls, then eval + - * // and parentheses.
            Returns (value:int, breakdown:str) or raises ValueError.
            """
            details = []

            def repl(m: re.Match) -> str:
                count = int(m.group(1)) if m.group(1) else 1
                sides = int(m.group(2))
                if count <= 0 or sides <= 0:
                    raise ValueError("Dice and sides must be positive.")
                rolls = [random.randint(1, sides) for _ in range(count)]
                total = sum(rolls)
                details.append(f"{count}d{sides} → [{', '.join(str(r) for r in rolls)}] = {total}")
                return str(total)

            squashed = expr.replace(" ", "")
            numeric = dice_re.sub(repl, squashed)

            safe = numeric.replace('/', '//')
            if not re.fullmatch(r'[0-9+\-*/()]+', safe):
                raise ValueError("Invalid characters (allowed: digits, d, + - * / and parentheses).")

            try:
                result = eval(safe, {"__builtins__": None}, {})
            except Exception:
                raise ValueError("Bad math expression.")

            if isinstance(result, float):
                result = int(result)
            if not isinstance(result, int):
                raise ValueError("Expression did not produce a number.")

            return result, ("; ".join(details) if details else None), safe.replace('//', ' // ')

        try:
            if s.startswith(("+", "-")):

                sign = 1 if s[0] == "+" else -1
                expr = s[1:]
                if expr == "" or expr == "+" or expr == "-":
                    raise ValueError
                if "d" in expr or re.search(r'[+*/()-]', expr):
                    val, breakdown, shown = _eval_dice_math(expr)
                    delta = sign * val
                    new_hp = cur_hp + delta
                    note = f" (applied {delta:+}; {breakdown}; math: `{shown}`)" if breakdown else f" (applied {delta:+})"
                else:
                    delta = sign * int(expr)
                    new_hp = cur_hp + delta
                    note = f" (applied {delta:+})"

            elif s.startswith("="):

                expr = s[1:]
                if "d" in expr or re.search(r'[+*/()-]', expr):
                    val, breakdown, shown = _eval_dice_math(expr)
                    new_hp = val
                    note = f" (set via {breakdown}; math: `{shown}`)" if breakdown else ""
                else:
                    new_hp = int(expr)
                    note = ""

            else:

                if "d" in s or re.search(r'[+*/()-]', s):
                    val, breakdown, shown = _eval_dice_math(s)
                    new_hp = val
                    note = f" (set via {breakdown}; math: `{shown}`)" if breakdown else ""
                else:
                    new_hp = int(s)
                    note = ""

        except ValueError:
            await ctx.send("❌ Invalid amount. Use `+N`, `-N`, `=N`, `N`, or dice like `+1d6+3`, `-1d4`, `=2d8+1`, `1d10+2`.")
            return

        new_hp = max(0, min(max_hp, new_hp))

        if not cfg.has_section("cur"):
            cfg.add_section("cur")
        cfg["cur"]["hp"] = str(new_hp)
        write_cfg(file_name, cfg)

        arrow = "→"
        await ctx.send(f"❤️ **{char_name}** HP {cur_hp} {arrow} **{new_hp}** / {max_hp}{note}")

        try:
            await self._refresh_initiative_if_listed(ctx, char_name)
        except Exception:
            pass

    @commands.command(name="mon")
    async def spawn_monsters(self, ctx, mon_name: str, *args):
        """
        DM: Spawn N monsters from <name>.ini, create .coe files, add to initiative (1d6 each).
        Usage:
          !mon <name> [count] [flags...]

        Flags (orderless; -x or -x both OK):
          -nolair   -> don’t queue lair hoards (A..O)
          -noind    -> don’t roll individual treasure (P..V)
          -noloot   -> no treasure handling at all (implies both of the above)
          -1hp       -> spawn with 1 HP (keeps full max HP)
        """

        count = None
        raw_flags = []
        for tok in args:
            t = str(tok).strip()
            if count is None and re.fullmatch(r"\d+", t):
                count = int(t)
            else:
                raw_flags.append(t)
        if count is None:
            count = 1
        if count <= 0 or count > 50:
            await ctx.send("❌ Count must be between 1 and 50.")
            return

        fset = {re.sub(r"^-?", "-", f.strip().lower()) for f in raw_flags}
        want_lair  = "-noloot" not in fset and "-nolair" not in fset
        want_indiv = "-noloot" not in fset and not ({"-noind", "-no-ind", "-noindividual"} & fset)
        spawn_at_1hp = any(f in fset for f in ("-1hp", "-1hp", "-onehp", "-hp1"))

        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here. Use `!init` first.")
            return

        dm_id = cfg.get(chan_id, "DM", fallback="")
        if str(ctx.author.id) != str(dm_id):
            await ctx.send("❌ Only the DM can use `!mon` in this battle.")
            return

        tpl = _load_monster_template(mon_name)
        if not tpl:
            await ctx.send(f"❌ Monster template not found for '{mon_name}'. Put '{mon_name.lower()}.ini' in ./monsters or root.")
            return

        ac     = int(tpl.get("ac", 10))

        def _parse_hd_value(raw) -> float:
            from fractions import Fraction
            s = str(raw).strip().lower().replace(" ", "")
            s = (s.replace("½","1/2")
                   .replace("¼","1/4")
                   .replace("⅛","1/8"))
            try:
                if "/" in s:
                    return float(Fraction(s))
                return float(s)
            except Exception:
                try:
                    return float(int(s))
                except Exception:
                    return 1.0

        hd_raw = tpl.get("hd", 1)
        hd_val = _parse_hd_value(hd_raw)

        hpmod  = int(tpl.get("hpmod", 0))
        damage = str(tpl.get("damage", "1d6"))
        move   = int(tpl.get("move", 30))
        saveas = str(tpl.get("saveas", "Fighter 1"))
        resist  = str(tpl.get("resist",  "")).strip()
        reduce1 = str(tpl.get("reduce1", "")).strip()
        immune  = str(tpl.get("immune",  "")).strip()
        init_bonus = _parse_init_bonus_from_tpl(tpl)
        skills_str = _parse_skills_from_tpl(tpl)

        try:
            xp_each = int(str(tpl.get("xp", 0)).strip() or "0")
        except Exception:
            xp_each = 0
        xp_added = 0

        SAVE_KEYS = ("poi", "wand", "para", "breath", "spell")

        def _nm_saves() -> dict[str, str]:

            return {k: str(_class_save_target("Fighter", 1, k) + 1) for k in SAVE_KEYS}

        saveas_raw = str(tpl.get("saveas", "Fighter 1")).strip()
        sa = saveas_raw.lower()

        if sa in {"nm", "normalman", "normal man", "normal"}:
            saves_out = _nm_saves()
        else:
            m = re.match(r"([A-Za-z\-]+)\s+(\d+)", saveas_raw)
            if m:
                save_class = m.group(1)
                save_level = max(1, min(20, int(m.group(2))))
            else:
                save_class, save_level = "Fighter", 1

            saves_out = {k: str(_class_save_target(save_class, save_level, k)) for k in SAVE_KEYS}

        prefix = re.sub(r"[^A-Za-z]", "", mon_name).upper()[:2] or "MO"
        existing = {fn[:-4] for fn in os.listdir(".") if fn.lower().endswith(".coe")}
        created = []

        names, scores = _parse_combatants(cfg, chan_id)
        join_seq = cfg.getint(chan_id, "join_seq", fallback=0)

        for _ in range(count):
            i = 1
            while f"{prefix}{i}" in existing:
                i += 1
            mon = f"{prefix}{i}"
            existing.add(mon)

            def _roll_hp_from_hd(hd_val: float, hpmod: int) -> int:
                import math, random
                hp = 0

                full = int(hd_val) if hd_val >= 1 else 0
                if full > 0:
                    hp += sum(random.randint(1, 8) for _ in range(full))

                frac = round(hd_val - full, 3)
                if full == 0:

                    if abs(frac - 0.5) < 1e-6:
                        hp += random.randint(1, 4)
                    elif abs(frac - 0.25) < 1e-6:
                        hp += random.randint(1, 2)
                    elif abs(frac - 0.125) < 1e-6:
                        hp += 1
                    else:

                        hp += 1
                else:

                    if abs(frac - 0.5) < 1e-6:
                        hp += random.randint(1, 4)

                hp += int(hpmod)
                return max(1, hp)

            hp = _roll_hp_from_hd(hd_val, hpmod)

            coe = configparser.ConfigParser()
            coe.optionxform = str
            coe["version"] = {"current": "08082018"}
            coe["info"] = {
                "race": "Monster", "class": "Monster", "sex": "",
                "name": mon, "owner_id": str(dm_id),
                "monster_type": mon_name, "battle_chan": chan_id,
            }
            if skills_str:
                coe["info"]["skills"] = skills_str
            coe["max"] = {"hp": str(hp)}
            coe["cur"] = {
                "hp": str(1 if spawn_at_1hp else hp),
                "level": str(max(1, int(hd_val))),
                "xp": "0", "gp": "0", "pp": "0", "ep": "0", "sp": "0", "turn": ""
            }

            stats = {
                "ac": str(ac),
                "ab": "",
                "move": str(move),
                "type": str(tpl.get("type", "")).strip(),
                "resist": resist,
                "reduce1": reduce1,
                "immune": immune,

                "hd": str(hd_val),
            }

            attacknames_raw = str(tpl.get("attacknames", "")).strip().lower()
            atk_pref_list = sorted(k[4:] for k in tpl.keys() if k.lower().startswith("atk_"))
            attack_list = [a for a in re.split(r"\s+", attacknames_raw) if a] if attacknames_raw else atk_pref_list
            if attack_list:
                stats["attacknames"] = " ".join(attack_list)
                first_spec = None
                for an in attack_list:
                    spec = (str(tpl.get(f"atk_{an}") or tpl.get(an) or
                                tpl.get(f"dmg_{an}") or tpl.get(f"{an}_dmg") or "")).strip()
                    if spec:
                        stats[f"atk_{an}"] = spec
                        if first_spec is None:
                            first_spec = spec
                    for key in (f"effect_{an}", f"{an}_effect", f"type_{an}"):
                        v = str(tpl.get(key, "")).strip()
                        if v:
                            stats[key] = v
                stats["damage"] = first_spec or damage or "1d6"
            else:
                stats["damage"] = damage or "1d6"

            coe["stats"] = stats
            coe["base"]  = dict(stats)

            coe["base"]  = dict(stats)
            coe["saves"] = saves_out
            coe["thief_mods"] = {}
            coe["banned_weapons"] = {"list": ""}
            coe["skills"] = {"list": skills_str}

            def _alias_key(s: str) -> str:
                return "".join(ch.lower() for ch in str(s) if ch.isalnum())

            spells_line = str(tpl.get("spells", "")).strip()
            if spells_line:

                mon_magic = {}
                mon_left  = {}
                listed = []

                for tok in re.split(r"\s+", spells_line):
                    if not tok:
                        continue
                    key = _alias_key(tok)

                    raw_ct = (tpl.get(tok, None) or
                              tpl.get(tok.lower(), None) or
                              tpl.get(key, None))
                    try:
                        count = int(str(raw_ct).strip()) if raw_ct is not None else 1
                    except Exception:
                        count = 1

                    listed.append(key)
                    mon_magic[f"{key}_total"] = str(max(0, count))
                    mon_left[f"{key}_left"]   = str(max(0, count))

                cl_raw = tpl.get("casterlevel", None) or tpl.get("cl", None)
                try:
                    caster_level = int(str(cl_raw).strip()) if cl_raw is not None else int(hd_val)
                except Exception:
                    caster_level = int(hd_val)

                mon_magic["list"] = " ".join(listed)
                mon_magic["caster_level"] = str(caster_level)
                mon_magic["source"] = str(tpl.get("name", mon_name)).strip() or mon_name

                coe["mon_spells"] = mon_magic
                coe["mon_left"]   = mon_left

            with open(f"{mon}.coe", "w", encoding="utf-8") as f:
                coe.write(f)

            d6 = random.randint(1, 6)
            ini_total = d6 + init_bonus
            if mon not in names:
                names.append(mon)
            scores[mon] = ini_total

            s = _slot(mon)

            for opt_key, _ in list(cfg.items(chan_id)):
                if opt_key.startswith(f"{s}."):
                  cfg.remove_option(chan_id, opt_key)

                if cfg.has_option(chan_id, mon):
                 cfg.remove_option(chan_id, mon)
            cfg.set(chan_id, f"{s}.dex", "0")
            join_seq += 1
            cfg.set(chan_id, "join_seq", str(join_seq))
            cfg.set(chan_id, f"{s}.join", str(join_seq))
            cfg.set(chan_id, f"{s}.disp", mon)

            created.append((mon, d6, hp))
            xp_added += max(0, xp_each)

        _write_combatants(cfg, chan_id, names, scores)

        old_tally = cfg.getint(chan_id, "xp_tally", fallback=0)
        new_tally = old_tally + xp_added
        cfg.set(chan_id, "xp_tally", str(new_tally))

        tre_raw = str(tpl.get("treasure", "")).strip().upper()
        if tre_raw and tre_raw not in {"NONE", "-", "—"} and (want_lair or want_indiv):
            codes = [c for c in re.findall(r"[A-Z]", tre_raw)]
            lair_letters  = [c for c in codes if c in LAIR_TYPES]
            indiv_letters = [c for c in codes if c in INDIVIDUAL_TYPES]

            if want_lair and lair_letters:
                lair_map = _get_map(cfg, chan_id, "tre_lair_counts")
                for L in lair_letters:
                    lair_map[L] = int(lair_map.get(L, 0)) + count
                _set_map(cfg, chan_id, "tre_lair_counts", lair_map)

                bonus_spec_raw = (str(tpl.get("gold", "")).strip()
                                  or str(tpl.get("lair_gold", "")).strip()
                                  or str(tpl.get("extra_gold", "")).strip())
                if bonus_spec_raw:

                    spec_norm = bonus_spec_raw.replace("×", "x").replace("*", "x").replace(" ", "")

                    for L in set(lair_letters):
                        opt = f"tre_lair_bonus_{L}"
                        if not cfg.has_option(chan_id, opt) or not cfg.get(chan_id, opt, fallback="").strip():
                            cfg.set(chan_id, opt, spec_norm)

            if want_indiv and indiv_letters:
                coins = dict(cp=0, sp=0, ep=0, gp=0, pp=0)
                misc  = dict(gems=0, jewelry=0, magic_any=0, potions=0, scrolls=0)
                for _ in range(count):
                    for code in indiv_letters:
                        r = _roll_individual(code)
                        for k in coins: coins[k] += r.get(k, 0)
                        for k in misc:  misc[k]  += r.get(k, 0)
                _add_coins(cfg, chan_id, **coins)
                _add_misc(cfg, chan_id, **misc)

        _save_battles(cfg)
        await self._update_tracker_message(ctx, cfg, chan_id)

        def _fmt_pm(n): return f"+{n}" if n >= 0 else str(n)
        lines = []
        for (mon, ini, _hp) in created:

            shown = f"1d6 = {ini - init_bonus}"
            if init_bonus:
                shown += f" {_fmt_pm(init_bonus)}"
            shown += f" → **{ini}**"
            lines.append(f"🧟 **{mon}** joins initiative! ({shown})")
        await ctx.send("\n".join(lines))

    @commands.command(name="tally")
    async def tally_xp(self, ctx, *args):
        """
        Show or clear the current XP & Treasure tally for this battle.
          !tally         -> show
          !tally clear   -> show then reset all tallies
        """
        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here. Use `!init` first."); return

        xp  = cfg.getint(chan_id, "xp_tally", fallback=0)
        cp  = cfg.getint(chan_id, "tre_cp", fallback=0)
        sp  = cfg.getint(chan_id, "tre_sp", fallback=0)
        ep  = cfg.getint(chan_id, "tre_ep", fallback=0)
        gp  = cfg.getint(chan_id, "tre_gp", fallback=0)
        pp  = cfg.getint(chan_id, "tre_pp", fallback=0)
        gems    = cfg.getint(chan_id, "tre_gems", fallback=0)
        jewelry = cfg.getint(chan_id, "tre_jewelry", fallback=0)
        magic_any = cfg.getint(chan_id, "tre_magic_any", fallback=0)
        magic_wa  = cfg.getint(chan_id, "tre_magic_wa", fallback=0)
        magic_xw  = cfg.getint(chan_id, "tre_magic_xw", fallback=0)
        potions   = cfg.getint(chan_id, "tre_potions", fallback=0)
        scrolls   = cfg.getint(chan_id, "tre_scrolls", fallback=0)
        lair_map  = _get_map(cfg, chan_id, "tre_lair_counts")

        coin_line = f"CP:{cp}  SP:{sp}  EP:{ep}  GP:{gp}  PP:{pp}"
        misc_line = []
        if gems:     misc_line.append(f"Gems:{gems}")
        if jewelry:  misc_line.append(f"Jewelry:{jewelry}")
        if magic_any or magic_wa or magic_xw:
            parts = []
            if magic_any: parts.append(str(magic_any))
            if magic_wa:  parts.append(f"WA:{magic_wa}")
            if magic_xw:  parts.append(f"XW:{magic_xw}")
            misc_line.append("Magic:" + "/".join(parts))
        if potions: misc_line.append(f"Potions:{potions}")
        if scrolls: misc_line.append(f"Scrolls:{scrolls}")
        misc_line = "  •  ".join(misc_line) if misc_line else "—"

        lair_line = ", ".join(f"{k}:{v}" for k,v in sorted(lair_map.items()) if v) or "—"

        msg = (
            f"📊 **XP Tally:** {xp}\n"
            f"💰 **Coins:** {coin_line}\n"
            f"💎 **Other:** {misc_line}\n"
            f"🏴 **Lair seeds (pending):** {lair_line}"
        )

        if args and str(args[0]).lower() in {"clear","reset"}:
            await ctx.send(msg + "\n*(reset to zero)*")

            for k in ("xp_tally","tre_cp","tre_sp","tre_ep","tre_gp","tre_pp",
                      "tre_gems","tre_jewelry","tre_magic_any","tre_magic_wa","tre_magic_xw",
                      "tre_potions","tre_scrolls"):
                cfg.set(chan_id, k, "0")
            _set_map(cfg, chan_id, "tre_lair_counts", {})
            _save_battles(cfg)
            return

        new_keys = ("new_cp","new_sp","new_ep","new_gp","new_pp",
                    "new_gems","new_jewelry","new_magic_any","new_magic_wa","new_magic_xw",
                    "new_potions","new_scrolls")
        new_vals = {k: cfg.getint(chan_id, k, fallback=0) for k in new_keys}

        recent_lines = []
        coin_bits = [f"{k[4:].upper()}:+{v}" for k,v in new_vals.items() if k.startswith("new_") and k[4:] in ("cp","sp","ep","gp","pp") and v]
        if coin_bits:
            recent_lines.append("💰 " + "  ".join(coin_bits))

        pool_bits = []
        for k,label in (("new_gems","Gems"),("new_jewelry","Jewelry"),("new_magic_any","Magic(any)"),
                        ("new_magic_wa","Magic(W/A)"),("new_magic_xw","Magic(XW)"),
                        ("new_potions","Potions"),("new_scrolls","Scrolls")):
            v = new_vals.get(k,0)
            if v: pool_bits.append(f"{label}:+{v}")
        if pool_bits:
            recent_lines.append("📦 " + "  ·  ".join(pool_bits))

        msg2 = msg
        if recent_lines:
            msg2 += "\n\n🆕 **Since last tally:**\n" + "\n".join(recent_lines)

            for k in new_keys:
                cfg.set(chan_id, k, "0")
            _save_battles(cfg)

        await ctx.send(msg)

    @commands.command(name="lair")
    async def lair_manage(self, ctx, *args):
        """
        GM-only lair tools.

        Usage:
          !lair list
          !lair clear
          !lair remove <letters...>         (e.g., !lair remove b k)
          !lair <LETTER>                    (e.g., !lair A) -> roll ONE hoard of that type, then zero that letter

        Notes:
          • This only affects the queued lair *seeds* stored in tre_lair_counts.
          • Rolling (!lair <LETTER>) applies the hoard and then clears that letter to 0 (your house rule).
        """
        import re

        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here."); return

        dm_id = cfg.get(chan_id, "DM", fallback="")
        if str(ctx.author.id) != str(dm_id):
            await ctx.send("❌ Only the DM can use `!lair`."); return

        lairs = _get_map(cfg, chan_id, "tre_lair_counts")

        def _fmt_map(d: dict) -> str:
            items = [f"{k}:{int(v)}" for k, v in sorted(d.items()) if int(v or 0) > 0]
            return ", ".join(items) if items else "—"

        if not args or (len(args) == 1 and str(args[0]).lower() in {"list","ls","show"}):
            await ctx.send(f"🏴 **Pending lairs:** { _fmt_map(lairs) }")
            return

        first = str(args[0]).lower()

        if first in {"clear","reset"}:
            if not lairs:
                await ctx.send("Nothing to clear.")
                return
            _set_map(cfg, chan_id, "tre_lair_counts", {})

            for L in LAIR_TYPES:
                opt = f"tre_lair_bonus_{L}"
                if cfg.has_option(chan_id, opt):
                    cfg.remove_option(chan_id, opt)
            _save_battles(cfg)

            _save_battles(cfg)
            await ctx.send("🧹 Cleared all pending lairs.")
            return

        if first in {"remove","rm","del"}:
            if len(args) < 2:
                await ctx.send("Usage: `!lair remove <letters...>` e.g., `!lair remove b k`")
                return

            codes = []
            for token in args[1:]:
                for c in re.findall(r"[A-Za-z]", str(token)):
                    codes.append(c.upper())

            if not codes:
                await ctx.send("No valid lair letters found.")
                return

            removed, missing = [], []
            for c in codes:
                v = int(lairs.get(c, 0) or 0)
                if v > 0:
                    lairs[c] = 0
                    removed.append(f"{c} (was {v})")
                else:

                    if c not in missing:
                        missing.append(c)

            for c in codes:
                opt = f"tre_lair_bonus_{c}"
                if cfg.has_option(chan_id, opt):
                    cfg.remove_option(chan_id, opt)

            _set_map(cfg, chan_id, "tre_lair_counts", lairs)
            _save_battles(cfg)

            parts = []
            if removed: parts.append("Removed: " + ", ".join(removed))
            if missing: parts.append("Not pending: " + ", ".join(missing))
            if not parts: parts.append("No changes.")
            parts.append(f"Now pending: { _fmt_map(lairs) }")

            await ctx.send("🏴 " + "  •  ".join(parts))
            return

        code = str(args[0]).strip().upper()
        pending = int(lairs.get(code, 0) or 0)
        if pending <= 0:
            await ctx.send(f"ℹ️ No pending lairs of type **{code}**.")
            return

        lines = _roll_one_lair_and_apply(cfg, chan_id, code)
        lairs[code] = 0
        _set_map(cfg, chan_id, "tre_lair_counts", lairs)
        _save_battles(cfg)

        for chunk in _chunk_send_lines(lines, 20):
            await ctx.send("\n".join(chunk))

    @commands.command(name="loot")
    async def loot_finalize(self, ctx):
        """
        Finalize all pending treasure at once:
          • For each lair type with count>0, resolve ONE hoard (house rule) and clear its count.
          • Drain all item pools (gems, jewelry, potions, scrolls, magic any/wa/xw) to zero and list results.
        Coins are added to the running coin tallies; we do not zero coin totals here.
        """
        cfg = _load_battles(); chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here."); return

        lairs = _get_map(cfg, chan_id, "tre_lair_counts")
        lair_report = []
        if lairs:
            for code, cnt in sorted(lairs.items()):
                try: n = int(cnt or 0)
                except: n = 0
                if n > 0:
                    lair_report.extend(_roll_one_lair_and_apply(cfg, chan_id, code))

                    lairs[code] = 0
            _set_map(cfg, chan_id, "tre_lair_counts", lairs)

        gem_n = cfg.getint(chan_id, "tre_gems", fallback=0)
        gem_lines = []
        if gem_n:
            rolled_lines, gem_gp_total = _gen_gems(gem_n)
            gem_lines = [f"• {s.replace(' — ', ' — **') + '**'}" if ' gp' in s else f"• {s}" for s in rolled_lines]
            if gem_gp_total:
                _inc_cfg_int(cfg, chan_id, "tre_gp", gem_gp_total)
                gem_lines.append(f"**→ Added {gem_gp_total} gp to Coins.**")
            cfg.set(chan_id, "tre_gems", "0")

        jew_n = cfg.getint(chan_id, "tre_jewelry", fallback=0)
        jew_lines = []
        if jew_n:
            rolled_lines, jew_gp_total = _gen_jewelry(jew_n)
            jew_lines = [f"• {s.replace(' — ', ' — **') + '**'}" if ' gp' in s else f"• {s}" for s in rolled_lines]
            if jew_gp_total:
                _inc_cfg_int(cfg, chan_id, "tre_gp", jew_gp_total)
                jew_lines.append(f"**→ Added {jew_gp_total} gp to Coins.**")
            cfg.set(chan_id, "tre_jewelry", "0")

        pot_n = cfg.getint(chan_id, "tre_potions", fallback=0)
        pot_table = _POTIONS

        pot_lines = [f"• { _pick_percent(pot_table) }" for _ in range(pot_n)]

        if pot_n: cfg.set(chan_id, "tre_potions", "0")

        scr_n = cfg.getint(chan_id, "tre_scrolls", fallback=0)
        scr_lines = []
        def pick_spells(for_class: str, nsp: int) -> list[str]:
            return self._pick_spells(for_class, nsp)

        for _ in range(scr_n):
            r = _d100()
            if 1 <= r <= 9:
                nsp = 1 if r<=3 else (2 if r<=6 else (3 if r<=8 else 4))
                cls = random.choice(["Cleric","Druid"])
                scr_lines.append(f"• **Spell Scroll** ({cls}) — {', '.join(pick_spells(cls, nsp))}")
            elif 10 <= r <= 35:
                nsp = 1 if r<=15 else (2 if r<=20 else (3 if r<=25 else (4 if r<=29 else (5 if r<=32 else (6 if r<=34 else 7)))))
                cls = random.choice(["Magic-User","Illusionist","Necromancer","Spellcrafter"])
                scr_lines.append(f"• Spell Scroll ({cls}) — {', '.join(pick_spells(cls, nsp))}")
            elif 36 <= r <= 40:  scr_lines.append("• Cursed Scroll")
            elif 41 <= r <= 46: scr_lines.append("• Protection from Elementals")
            elif 47 <= r <= 56: scr_lines.append("• Protection from Lycanthropes")
            elif 57 <= r <= 61: scr_lines.append("• Protection from Magic")
            elif 62 <= r <= 75: scr_lines.append("• Protection from Undead")
            elif 76 <= r <= 85: scr_lines.append("• Map to Treasure Type A")
            elif 86 <= r <= 89: scr_lines.append("• Map to Treasure Type E")
            elif 90 <= r <= 92: scr_lines.append("• Map to Treasure Type G")
            else:
                k = _dice_sum("1d4")
                _inc_cfg_int(cfg, chan_id, "tre_magic_any", k)
                scr_lines.append(f"• **Map to 1d4 Magic Items** (added **{k}** magic-any to tally)")
        if scr_n: cfg.set(chan_id, "tre_scrolls", "0")

        any_n = cfg.getint(chan_id, "tre_magic_any", fallback=0)
        wa_n  = cfg.getint(chan_id, "tre_magic_wa",  fallback=0)
        xw_n  = cfg.getint(chan_id, "tre_magic_xw",  fallback=0)

        _WEAPON_TYPES = [
            (1,2,"GreatAxe"), (3,9,"BattleAxe"), (10,11,"HandAxe"),
            (12,19,"Shortbow"), (20,27,"Shortbow"),
            (28,31,"Longbow"), (32,35,"Longbow"),
            (36,43,"LightXbow"), (44,47,"HeavyXbow"),
            (48,59,"Dagger"), (60,65,"Shortsword"), (66,79,"Longsword"),
            (80,81,"Longsword"), (82,83,"Greatsword"),
            (84,86,"Warhammer"), (87,94,"Mace"),
            (95,95,"Maul"), (96,96,"Polearm"),
            (97,97,"Sling"), (98,100,"Spear"),
        ]
        _MELEE_BONUS = [
            (1,40, "+1"), (41,50,"+2"), (51,55,"+3"), (56,57,"+4"),
            (58,"+5"), (59,75,"+1, +2 vs. Special Enemy"), (76,85,"+1, +3 vs. Special Enemy"),
            (86,95,"Roll Again + Special Ability"), (96,98,"Cursed, -1"), (99,100,"Cursed, -2"),
        ]
        _MISSILE_BONUS = [
            (1,46,"+1"), (47,58,"+2"), (59,64,"+3"),
            (65,82,"+1, +2 vs. Special Enemy"), (83,94,"+1, +3 vs. Special Enemy"),
            (95,98,"Cursed, -1"), (99,100,"Cursed, -2"),
        ]
        _ENEMIES = {1:"Dragons",2:"Enchanted",3:"Lycanthropes",4:"Regenerators",5:"Spell Users",6:"Undead"}
        _ABILITIES = {**{i:"Casts Light on Command" for i in range(1,10)}, 10:"Charm Person",11:"Charm Person",
                      12:"Drains Energy",13:"Flames on Command",14:"Flames on Command",
                      15:"Flames on Command",16:"Flames on Command",
                      17:"Locate Objects",18:"Locate Objects",19:"Locate Objects",20:"Wishes"}

        magic_lines = []

        def roll_weapon():
            wt = _pick_percent(_WEAPON_TYPES)
            table = _MISSILE_BONUS if _is_missile_weapon(wt) else _MELEE_BONUS
            bonus = _pick_percent(table)
            desc = f"{wt} {bonus}"
            if "Roll Again + Special Ability" in bonus:
                bonus2 = _pick_percent(table)
                if "Roll Again" in bonus2: bonus2 = "+1"
                abil = _ABILITIES[_d(20)]
                desc = f"{wt} {bonus2} — Special: {abil}"
            if "Special Enemy" in bonus:
                foe = _ENEMIES[_d(6)]
                desc = f"{wt} {bonus} ({foe})"
            return "Weapon", desc

        _ARMOR_TYPES = [(1,9,"Leather Armor"),(10,28,"Chain Mail"),(29,43,"Plate Mail"),(44,100,"Shield")]
        _ARMOR_BONUS = [(1,50,"+1"),(51,80,"+2"),(81,90,"+3"),(91,95,"Cursed*"),(96,100,"Cursed, AC 11**")]

        def roll_armor():
            at = _pick_percent(_ARMOR_TYPES)
            ab = _pick_percent(_ARMOR_BONUS)
            if ab == "Cursed*":

                roll2 = _pick_percent([(1,50,"+1"),(51,80,"+2"),(81,90,"+3")])
                ab = roll2.replace("+","-")
            return "Armor", f"{at} {ab}"

        _WSR = [
            (1,8,"Rod of Cancellation"), (9,13,"Snake Staff"), (14,17,"Staff of Commanding"),
            (18,28,"Staff of Healing"), (29,30,"Staff of Power"), (31,34,"Staff of Striking"),
            (35,35,"Staff of Wizardry"), (36,40,"Wand of Cold"), (41,45,"Wand of Enemy Detection"),
            (46,50,"Wand of Fear"), (51,55,"Wand of Fireballs"), (56,60,"Wand of Illusion"),
            (61,65,"Wand of Lightning Bolts"), (66,73,"Wand of Magic Detection"),
            (74,79,"Wand of Paralysis"), (80,84,"Wand of Polymorph"),
            (85,92,"Wand of Secret Door Detection"), (93,100,"Wand of Trap Detection"),
        ]
        _RARE = [
            (1,5,"Bag of Devouring"), (6,20,"Bag of Holding"), (21,32,"Boots of Traveling and Leaping"),
            (33,47,"Broom of Flying"), (48,57,"Device of Summoning Elementals"),
            (58,59,"Efreeti Bottle"), (60,64,"Flying Carpet"),
            (65,81,"Gauntlets of Ogre Power"), (82,86,"Girdle of Giant Strength"),
            (87,88,"Mirror of Imprisonment"), (89,100,"Rope of Climbing"),
        ]

        _MISC_SUBTABLE = [(57, 1), (100, 2)]

        _MISC_EFFECTS_1 = [
            (1, 1,   ("Blasting", "G")),
            (2, 5,   ("Blending", "F")),
            (6, 13,  ("Cold Resistance", "F")),
            (14, 17, ("Comprehension", "E")),
            (18, 22, ("Control Animal", "C")),
            (23, 29, ("Control Human", "C")),
            (30, 35, ("Control Plant", "C")),
            (36, 37, ("Courage", "G")),
            (38, 40, ("Deception", "F")),
            (41, 52, ("Delusion", "A")),
            (53, 55, ("Djinni Summoning", "C")),
            (56, 56, ("Doom", "G")),
            (57, 67, ("Fire Resistance", "F")),
            (68, 80, ("Invisibility", "F")),
            (81, 85, ("Levitation", "B")),
            (86, 95, ("Mind Reading", "C")),
            (96, 97, ("Panic", "G")),
            (98, 100, ("Penetrating Vision", "D")),
        ]
        _MISC_EFFECTS_2 = [
            (1, 7,   ("Protection +1", "F")),
            (8, 10,  ("Protection +2", "F")),
            (11, 11, ("Protection +3", "F")),
            (12, 14, ("Protection from Energy Drain", "F")),
            (15, 20, ("Protection from Scrying", "F")),
            (21, 23, ("Regeneration", "C")),
            (24, 29, ("Scrying", "H")),
            (30, 32, ("Scrying, Superior", "H")),
            (33, 39, ("Speed", "B")),
            (40, 42, ("Spell Storing", "C")),
            (43, 50, ("Spell Turning", "F")),
            (51, 69, ("Stealth", "B")),
            (70, 72, ("Telekinesis", "C")),
            (73, 74, ("Telepathy", "C")),
            (75, 76, ("Teleportation", "C")),
            (77, 78, ("True Seeing", "D")),
            (79, 88, ("Water Walking", "B")),
            (89, 99, ("Weakness", "C")),
            (100,100,("Wishes", "C")),
        ]

        _MISC_FORMS = {
          "A":[(1,2,"Bell"),(3,5,"Belt"),(6,13,"Boots"),(14,15,"Bowl"),(16,28,"Cloak"),
               (29,31,"Orb"),(32,33,"Drums"),(34,38,"Helm"),(39,43,"Horn"),(44,46,"Lens"),
               (47,49,"Mirror"),(50,67,"Pendant"),(68,100,"Ring")],
          "B":[(1,25,"Boots"),(26,50,"Pendant"),(51,100,"Ring")],
          "C":[(1,40,"Pendant"),(41,100,"Ring")],
          "D":[(1,17,"Lens"),(18,21,"Mirror"),(22,50,"Pendant"),(51,100,"Ring")],
          "E":[(1,40,"Helm"),(41,80,"Pendant"),(81,100,"Ring")],
          "F":[(1,7,"Belt"),(8,38,"Cloak"),(39,50,"Pendant"),(51,100,"Ring")],
          "G":[(1,17,"Bell"),(18,50,"Drums"),(51,100,"Horn")],
          "H":[(1,17,"Bowl"),(18,67,"Orb")]
        }

        def _pick_percent2(table):
            r = _d100()
            for row in table:
                lo, hi, val = row
                if lo <= r <= hi: return val
            return table[-1][-1]

        def roll_misc():
            sub = _pick_percent(_MISC_SUBTABLE)  
            eff, col = _pick_percent2(_MISC_EFFECTS_1 if sub == 1 else _MISC_EFFECTS_2)
            form = _pick_percent2(_MISC_FORMS[col])
            return "Miscellaneous", f"{form} of {eff}"

        def roll_wsr():  return "Wand/Staff/Rod", _pick_percent(_WSR)
        def roll_rare(): return "Rare", _pick_percent(_RARE)
        def roll_potion():
            return "Potion", _pick_percent(pot_table)

        def pick_cat_from_any():
            return random.choice(["weapon","armor","potion","scroll","wsr","rare","misc"])
        def pick_cat_from_xw():
            return random.choice(["armor","potion","scroll","wsr","rare","misc"])        

        def roll_scroll_simple():
            grp = random.choice(["Magic-User","Illusionist","Necromancer","Spellcrafter","Cleric","Druid"])
            names = self._pick_spells(grp, 1)
            spell = names[0] if names else "(no spells found)"
            return "Scroll", f"Spell Scroll ({grp}) — {spell}"

        def take_from_pool(n, picker):
            for _ in range(n):
                kind, text = picker()
                magic_lines.append(f"• **{kind}** — {text}")

        def cat_any():
            return random.choice(["weapon","armor","potion","scroll","wsr","rare"])
        def cat_xw():
            return random.choice(["armor","potion","scroll","wsr","rare"])

        for _ in range(any_n):
            c = cat_any()
            if   c=="weapon": take_from_pool(1, roll_weapon)
            elif c=="armor":  take_from_pool(1, roll_armor)
            elif c=="potion": take_from_pool(1, roll_potion)
            elif c=="scroll": take_from_pool(1, roll_scroll_simple)
            elif c=="wsr":    take_from_pool(1, roll_wsr)
            else:             take_from_pool(1, roll_rare)
        for _ in range(wa_n):
            if _d(2)==1: take_from_pool(1, roll_weapon)
            else:        take_from_pool(1, roll_armor)
        for _ in range(xw_n):
            c = cat_xw()
            if   c=="armor":  take_from_pool(1, roll_armor)
            elif c=="potion": take_from_pool(1, roll_potion)
            elif c=="scroll": take_from_pool(1, roll_scroll_simple)
            elif c=="wsr":    take_from_pool(1, roll_wsr)
            else:             take_from_pool(1, roll_rare)

        if any_n: cfg.set(chan_id, "tre_magic_any", "0")
        if wa_n:  cfg.set(chan_id, "tre_magic_wa", "0")
        if xw_n:  cfg.set(chan_id, "tre_magic_xw", "0")

        _save_battles(cfg)

        if lair_report:
            for chunk in _chunk_send_lines(lair_report, 20):
                await ctx.send("\n".join(chunk))

        new_keys = ("new_cp","new_sp","new_ep","new_gp","new_pp",
                    "new_gems","new_jewelry","new_magic_any","new_magic_wa","new_magic_xw",
                    "new_potions","new_scrolls")
        new_vals = {k: cfg.getint(chan_id, k, fallback=0) for k in new_keys}
        coin_bits = [f"{k[4:].upper()}:+{v}" for k,v in new_vals.items()
                     if k[4:] in ("cp","sp","ep","gp","pp") and v]
        pool_bits = []
        for k,label in (("new_gems","Gems"),("new_jewelry","Jewelry"),("new_magic_any","Magic(any)"),
                        ("new_magic_wa","Magic(W/A)"),("new_magic_xw","Magic(XW)"),
                        ("new_potions","Potions"),("new_scrolls","Scrolls")):
            v = new_vals.get(k,0)
            if v: pool_bits.append(f"{label}:+{v}")
        if coin_bits or pool_bits:
            lines = []
            if coin_bits: lines.append("💰 " + "  ".join(coin_bits))
            if pool_bits: lines.append("📦 " + "  ·  ".join(pool_bits))
            await ctx.send("🆕 **Since last tally:**\n" + "\n".join(lines))
            for k in new_keys: cfg.set(chan_id, k, "0")
            _save_battles(cfg)
        if gem_lines:
            for chunk in _chunk_send_lines(gem_lines, 20):
                await ctx.send("💎 **Gems**\n" + "\n".join(chunk))
        if jew_lines:
            for chunk in _chunk_send_lines(jew_lines, 20):
                await ctx.send("👑 **Jewelry**\n" + "\n".join(chunk))
        if pot_lines:
            for chunk in _chunk_send_lines(pot_lines, 25):
                await ctx.send("🧪 **Potions**\n" + "\n".join(chunk))
        if scr_lines:
            for chunk in _chunk_send_lines(scr_lines, 10):
                await ctx.send("📜 **Scrolls**\n" + "\n".join(chunk))
        if magic_lines:
            for chunk in _chunk_send_lines(magic_lines, 12):
                await ctx.send("✨ **Magic Items**\n" + "\n".join(chunk))

        coins_line = (f"🪙 **Coins (totals so far)** — "
                      f"CP {cfg.getint(chan_id,'tre_cp',fallback=0)}, "
                      f"SP {cfg.getint(chan_id,'tre_sp',fallback=0)}, "
                      f"EP {cfg.getint(chan_id,'tre_ep',fallback=0)}, "
                      f"GP {cfg.getint(chan_id,'tre_gp',fallback=0)}, "
                      f"PP {cfg.getint(chan_id,'tre_pp',fallback=0)}")
        await ctx.send(coins_line)

    @commands.command(name="morale")
    async def morale(self, ctx, *args):
        """
        Make a Morale check for one or more monsters.
        Usage:
          !morale GO1
          !morale go1 bu2
          !morale go1 -2   (apply -2 adjustment to the roll)
        Rules: 2d6 <= Morale passes. Morale 12 never fails.
        """
        if not args:
            await ctx.send("Usage: `!morale <monster ...> [±mod]` e.g., `!morale go1 -2`")
            return

        mod = 0
        if re.fullmatch(r"[+-]?\d+", str(args[-1])):
            try:
                mod = int(args[-1])
                names = args[:-1]
            except Exception:
                names = args
        else:
            names = args

        if not names:
            await ctx.send("Please name at least one monster, e.g. `!morale go1`.")
            return

        lines = []
        for raw in names:
            disp, path = _resolve_char_ci_local(raw)
            pretty = disp or raw
            if not path or not os.path.exists(path):
                lines.append(f"{pretty}: *(not found)*")
                continue

            cfg = read_cfg(path)
            cls = (get_compat(cfg, "info", "class", fallback="") or "").strip().lower()
            if cls != "monster":
                lines.append(f"{pretty}: not a monster.")
                continue

            morale = None
            try:
                m1 = str(get_compat(cfg, "stats", "morale", fallback="")).strip()
                m2 = str(get_compat(cfg, "base",  "morale", fallback="")).strip()
                mtxt = m1 or m2
                if mtxt and mtxt.lstrip("-").isdigit():
                    morale = int(mtxt)
            except Exception:
                pass

            if morale is None:
                mtype = get_compat(cfg, "info", "monster_type", fallback="")
                tpl = _load_monster_template(mtype) if mtype else None
                if tpl and "morale" in tpl:
                    try:
                        morale = int(tpl["morale"])
                    except Exception:
                        pass

            if morale is None:
                morale = 7

            morale_mod_bb = 0
            bb_bits = []
            try:
                bcfg_bb = _load_battles()
                ch_bb = _section_id(ctx.channel)
                if bcfg_bb and bcfg_bb.has_section(ch_bb):
                    names_bb, _ = _parse_combatants(bcfg_bb, ch_bb)
                    key_m = _find_ci_name(names_bb, pretty) or pretty
                    try: slot_m = _slot(key_m)
                    except Exception: slot_m = key_m.replace(" ", "_")

                    if bcfg_bb.getint(ch_bb, f"{slot_m}.x_bless", fallback=0) > 0 and                       bcfg_bb.getint(ch_bb, f"{slot_m}.morale_bless", fallback=0) > 0:
                        morale_mod_bb -= 1
                        bb_bits.append("Bless: −1")

                    if bcfg_bb.getint(ch_bb, f"{slot_m}.x_bane", fallback=0) > 0 and                       bcfg_bb.getint(ch_bb, f"{slot_m}.morale_bane",  fallback=0) > 0:
                        morale_mod_bb += 1
                        bb_bits.append("Bane: +1")
            except Exception:
                pass

            if morale >= 12:
                lines.append(f"**{pretty}**: Morale **12** → ✅ *stands and fights* (never fails).")
                continue

            r1 = random.randint(1, 6)
            r2 = random.randint(1, 6)
            eff_mod = mod + morale_mod_bb

            parts = []
            if mod:
                parts.append(f"{'+' if mod > 0 else '–'} {abs(mod)}")
            if morale_mod_bb:

                label = "; ".join(bb_bits) if bb_bits else "Bless/Bane"
                parts.append(f"{'+' if morale_mod_bb > 0 else '–'} {abs(morale_mod_bb)} ({label})")
            mod_txt = (" " + " ".join(parts)) if parts else ""

            total = r1 + r2 + eff_mod
            result = "✅ **STAND & FIGHT**" if total <= morale else "❌ **LOST NERVE**"

            lines.append(
                f"**{pretty}**: 2d6 [{r1}, {r2}]{mod_txt} = **{total}** vs Morale **{morale}** → {result}"
            )

        embed = nextcord.Embed(
            title="Morale Check",
            description="\n".join(lines),
            color=random.randint(0, 0xFFFFFF)
        )

        await ctx.send(embed=embed)

    @commands.command(name="remove", aliases=["rm", "kick"])
    async def remove_from_initiative(self, ctx, *, args: str):
        """
        GM-only: remove one or more names from this channel's initiative.
        Usage:
          !remove GO3
          !remove go1 go2
          !remove go*                 (prefix wildcard)
          !remove go1 -delete        (also delete monster file)
          !remove go1 go2 -d          (same)

        Notes:
          - If you remove the current *turn* holder, turn advances to the top of the
            remaining order (round number unchanged).
          - -delete / -d will delete .coe files only for Monsters.
        """
        import re, os

        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here.")
            return

        dm_id = cfg.get(chan_id, "DM", fallback="")
        if str(ctx.author.id) != str(dm_id):
            await ctx.send("❌ Only the DM can use `!remove` in this battle.")
            return

        if not args or not args.strip():
            await ctx.send("Usage: `!remove <name ...> [-delete]`  (e.g., `!remove GO3`)")
            return

        tokens = [t for t in re.split(r"\s+", args.strip()) if t]

        want_delete = True
        raw_names   = [t for t in tokens if t.lower() not in ("-d", "-delete", "-del")]

        if not raw_names:
            await ctx.send("Please name at least one combatant to remove.")
            return

        names, scores = _parse_combatants(cfg, chan_id)
        cur_turn = (cfg.get(chan_id, "turn", fallback="") or "").strip()

        removed = []
        missing = []
        deleted_files = []

        def _expand_token(tok: str) -> list[str]:
            q = tok.strip().lower()
            out = []
            if q.endswith("*"):
                pref = q[:-1]
                for n in names:
                    if n.lower().startswith(pref):
                        out.append(n)
            else:
                found = _find_ci_name(names, tok)
                if found:
                    out.append(found)
            return out

        to_remove = []
        seen = set()
        for tok in raw_names:
            hits = _expand_token(tok)
            if not hits:
                missing.append(tok)
            for h in hits:
                if h not in seen:
                    seen.add(h)
                    to_remove.append(h)

        if not to_remove and missing:
            await ctx.send("Nothing matched: " + ", ".join(f"`{m}`" for m in missing))
            return

        for key in to_remove:
            if key not in names:
                continue
            names = [n for n in names if n != key]
            if cfg.has_option(chan_id, key):
                cfg.remove_option(chan_id, key)

            s = _slot(key)
            for suf in (".dex", ".join", ".disp", ".acpen", ".oil", ".holds", ".heldby"):
                opt = f"{s}{suf}"
                if cfg.has_option(chan_id, opt):
                    cfg.remove_option(chan_id, opt)

            removed.append(key)

            if want_delete:
                disp, path = _resolve_char_ci_local(key)
                if path and os.path.exists(path):
                    try:
                        pcfg = read_cfg(path)
                        cls = (get_compat(pcfg, "info", "class", fallback="") or "").strip().lower()
                        if cls == "monster":
                            try:
                                os.remove(os.path.abspath(path))
                                deleted_files.append(disp or key)
                            except Exception:
                                pass
                    except Exception:
                        pass

        _write_combatants(cfg, chan_id, names, scores)

        if cur_turn in removed:
            if names:
                ordered = _sorted_names(names, scores, cfg, chan_id)
                cfg.set(chan_id, "turn", ordered[0] if ordered else "")
            else:
                cfg.set(chan_id, "turn", "")

        _save_battles(cfg)
        await self._update_tracker_message(ctx, cfg, chan_id)

        parts = []
        if removed:
            parts.append("Removed: " + ", ".join(f"**{n}**" for n in removed))
        if deleted_files:
            parts.append("Deleted files: " + ", ".join(f"**{n}**" for n in deleted_files))
        if missing:
            parts.append("Not found: " + ", ".join(f"`{m}`" for m in missing))
        await ctx.send("🧽 " + ("; ".join(parts) if parts else "No changes."))

    def _eq_get_weapons(self, cfg) -> list[str]:
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

    @commands.command(name="magicweapon")
    async def magicweapon(self, ctx, *weapon_words):
        """Paladin: bless one equipped weapon to count as magical for 1 turn (60 rounds)."""

        char_name = get_active(ctx.author.id)
        if not char_name:
            await ctx.send("❌ No active character. Use `!char <name>` first.")
            return

        coe = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(coe):
            await ctx.send(f"❌ Character file not found for **{char_name}**.")
            return
        cfg = read_cfg(coe)
        if get_compat(cfg, "info", "owner_id", fallback="") not in {"", str(ctx.author.id)}:
            await ctx.send(f"❌ You do not own **{char_name}**.")
            return

        cls = (get_compat(cfg, "info", "class", fallback="") or "").strip().lower()
        if cls != "paladin":
            await ctx.send("❌ Magic Weapon is a Paladin ability.")
            return

        bcfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not bcfg.has_section(chan_id):
            await ctx.send("❌ Not currently in combat. Start initiative first.")
            return

        want = " ".join(weapon_words).strip()
        if not want:
            await ctx.send("Usage: `!magicweapon <equipped-weapon>`")
            return

        equipped = self._eq_get_weapons(cfg)
        equipped_norm = {normalize_name(w): w for w in equipped}
        key = normalize_name(want)
        if key not in equipped_norm:
            pretty = " / ".join(equipped) or "none"
            await ctx.send(f"❌ That weapon isn’t equipped. Currently equipped: `{pretty}`")
            return

        try:
            slot_me = _slot(char_name)
        except Exception:
            slot_me = char_name.replace(" ", "_")

        prev_rounds = bcfg.getint(chan_id, f"{slot_me}.magwep", fallback=0)
        prev_disp   = bcfg.get(chan_id,  f"{slot_me}.magwep_disp", fallback="")
        prev_name   = bcfg.get(chan_id,  f"{slot_me}.magwep_name", fallback="")

        bcfg.set(chan_id, f"{slot_me}.magwep", "60")
        bcfg.set(chan_id, f"{slot_me}.magwep_name", key)
        bcfg.set(chan_id, f"{slot_me}.magwep_disp", equipped_norm[key])
        _save_battles(bcfg)

        title = f"🕯️ {char_name} invokes Magic Weapon!"
        embed = nextcord.Embed(title=title, color=random.randint(0, 0xFFFFFF))
        embed.add_field(name="Weapon", value=equipped_norm[key], inline=True)
        embed.add_field(name="Duration", value="**60 rounds** (1 turn)", inline=True)

        status_lines = []
        if prev_rounds > 0:
            if prev_name == key:
                status_lines.append(f"Refreshed blessing on **{prev_disp}** *(was {prev_rounds} rounds left)*.")
            else:
                status_lines.append(f"Replaced blessing on **{prev_disp}** *(had {prev_rounds} rounds left)*.")
        else:
            status_lines.append("Blessing applied.")
        embed.add_field(name="Status", value="\n".join(status_lines), inline=False)

        embed.add_field(
            name="Effects",
            value=(
                "• Counts as **magical** for overcoming *nonmagical* immunity/resistance.\n"
                "• Applies only while wielding this weapon.\n"
            ),
            inline=False
        )

        embed.set_footer(text="Tracker tag: [MW 60]")

        try:
            msg_id = bcfg.getint(chan_id, "message_id", fallback=0)
            if msg_id:
                block = _format_tracker_block(bcfg, chan_id)
                msg = await ctx.channel.fetch_message(msg_id)
                await msg.edit(content="**EVERYONE ROLL FOR INITIATIVE!**\n```text\n" + block + "\n```")
        except Exception:
            pass

        await ctx.send(embed=embed)

    @commands.command(name="classes")
    async def list_classes(self,ctx):
        await ctx.send(f"**Classes**: Fighter, Barbarian, Ranger, Paladin, Cleric, Druid, Thief, Scout, Assassin, Magic-User, Illusionist, Necromancer, Spellcrafter, Fightermage, Magethief")

    @commands.command(name="races")
    async def list_races(self,ctx):
        await ctx.send(f"**Races**: Human, Elf, Half-Elf, Half-Ogre, Half-Orc, Halfling, Bugbear, Caveman, Gnoll, Goblin, Hobgoblin, Kobold, Lizardman, Orc, Dwarf, Gnome")

    @commands.command(name="loh")
    async def lay_on_hands(self, ctx, *args):
        """
        Paladin: Lay on Hands.
          !loh                    -> heal self
          !loh <name>             -> heal target
          !loh <name> disease     -> cure disease (L7+)
          !loh <name> poison      -> neutralize poison (L11+)

        Heals 2 + CHA mod (min 0). Uses/day = (level + 1)//2.
        Tracks uses in cur.loh_used (reset this on !lr command).
        """

        def ability_mod(score: int) -> int:
            return (score - 10) // 2

        char_name = get_active(ctx.author.id)
        if not char_name:
            await ctx.send("❌ No active character. Use `!char <name>` first.")
            return

        coe = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(coe):
            await ctx.send(f"❌ Character file not found for **{char_name}**.")
            return
        cfg = read_cfg(coe)
        if get_compat(cfg, "info", "owner_id", fallback="") not in {"", str(ctx.author.id)}:
            await ctx.send(f"❌ You do not own **{char_name}**.")
            return

        cls = (get_compat(cfg, "info", "class", fallback="") or "").strip().lower()
        if cls != "paladin":
            await ctx.send("❌ Lay on Hands is a Paladin ability.")
            return

        mode = "heal"
        target_tokens = list(args)
        if target_tokens and str(target_tokens[-1]).lower() in {"disease", "poison"}:
            mode = str(target_tokens.pop()).lower()
        target_name = " ".join(target_tokens).strip() or char_name

        if target_name == char_name:
            tgt_disp, tgt_path = (char_name, coe)
            tcfg = cfg
            same_file = True
        else:
            tgt_disp, tgt_path = _resolve_char_ci_local(target_name)
            if not tgt_path:
                await ctx.send(f"❌ Target **{target_name}** not found.")
                return
            tcfg = read_cfg(tgt_path)
            same_file = False
        pretty = tgt_disp or target_name

        level = getint_compat(cfg, "cur", "level", fallback=1)
        uses_max = max(1, (level + 1) // 2)
        used = getint_compat(cfg, "cur", "loh_used", fallback=0)
        if used >= uses_max:
            await ctx.send(f"⛔ No Lay on Hands uses left ({used}/{uses_max} today).")
            return

        if mode == "disease" and level < 7:
            await ctx.send("⛔ Cure disease requires Paladin level **7+**.")
            return
        if mode == "poison" and level < 11:
            await ctx.send("⛔ Neutralize poison requires Paladin level **11+**.")
            return

        effect_text = ""
        changed_anything = False

        if mode == "heal":
            CHA = getint_compat(cfg, "stats", "cha", fallback=10)
            cha_mod = getint_compat(cfg, "stats", "cha_modifier", fallback=ability_mod(CHA))
            heal = max(0, 2 + cha_mod)

            cur_hp = getint_compat(tcfg, "cur", "hp", fallback=None)
            max_hp = getint_compat(tcfg, "max", "hp", fallback=cur_hp if cur_hp is not None else 0)
            if cur_hp is None:
                await ctx.send("⚠️ Target has no HP field; cannot heal.")
                return

            new_hp = min(max_hp, cur_hp + heal)

            tcfg.setdefault("cur", {})
            tcfg["cur"]["hp"] = str(new_hp)
            if not same_file:
                write_cfg(tgt_path, tcfg)

            if _is_monster(tgt_path):
                before = _life_bar(cur_hp, max_hp, width=10)
                after  = _life_bar(new_hp, max_hp, width=10)
                effect_text = f"💠 **Heal** {pretty}: {before} → **{after}** (+{new_hp - cur_hp})"
            else:
                effect_text = f"💠 **Heal** {pretty}: {cur_hp} → **{new_hp}** (+{new_hp - cur_hp})"

            changed_anything = (new_hp != cur_hp)

        elif mode == "disease":

            bcfg = _load_battles()
            chan_id = _section_id(ctx.channel)
            removed = False

            if bcfg.has_section(chan_id):
                names, _ = _parse_combatants(bcfg, chan_id)
                key = _find_ci_name(names, pretty) or pretty
                try:
                    slot = _slot(key)
                except Exception:
                    slot = key.replace(" ", "_")
                for opt in (f"{slot}.diseased", f"{slot}.disease", f"{slot}.ill"):
                    if bcfg.has_option(chan_id, opt):
                        bcfg.remove_option(chan_id, opt)
                        removed = True
                _save_battles(bcfg)

            for sec, opt in (("cur","diseased"), ("cur","disease"), ("status","disease")):
                try:
                    if tcfg.has_option(sec, opt):
                        tcfg.remove_option(sec, opt)
                        removed = True
                except Exception:
                    pass
            write_cfg(tgt_path, tcfg)

            effect_text = f"🧿 **Cure disease** on {pretty}: " + ("success." if removed else "no disease detected; cleansed anyway.")
            changed_anything = True

        elif mode == "poison":
            bcfg = _load_battles()
            chan_id = _section_id(ctx.channel)
            removed = False

            if bcfg.has_section(chan_id):
                names, _ = _parse_combatants(bcfg, chan_id)
                key = _find_ci_name(names, pretty) or pretty
                try:
                    slot = _slot(key)
                except Exception:
                    slot = key.replace(" ", "_")
                for opt in (f"{slot}.poisoned", f"{slot}.poison", f"{slot}.stench", f"{slot}.stench_by"):
                    if bcfg.has_option(chan_id, opt):
                        bcfg.remove_option(chan_id, opt)
                        removed = True

                for opt in (f"{slot}.x_sick", f"{slot}.x_sick_label", f"{slot}.x_sick_code", f"{slot}.x_sick_by"):
                    if bcfg.has_option(chan_id, opt):
                        bcfg.remove_option(chan_id, opt)
                        removed = True

                _save_battles(bcfg)

            for opt in (f"{slot}.poisoned", f"{slot}.poison", f"{slot}.stench", f"{slot}.stench_by",
                        f"{slot}.x_sick", f"{slot}.x_sick_label", f"{slot}.x_sick_code", f"{slot}.x_sick_by"):
                if bcfg.has_option(chan_id, opt):
                    bcfg.remove_option(chan_id, opt)
                    removed = True

            for sec, opt in (("cur","poisoned"), ("cur","poison"), ("status","poison"),
                             ("cur","stench"), ("status","stench"), ("cur","sickened"), ("status","sickened")):
                try:
                    if tcfg.has_option(sec, opt):
                        tcfg.remove_option(sec, opt)
                        removed = True
                except Exception:
                    pass

            write_cfg(tgt_path, tcfg)

            effect_text = f"☠️ **Neutralize poison** on {pretty}: " + ("success." if removed else "no poison detected; purged anyway.")
            changed_anything = True

        used += 1
        cfg.setdefault("cur", {})
        cfg["cur"]["loh_used"] = str(used)
        write_cfg(coe, cfg)

        if same_file:

            write_cfg(coe, cfg)
        else:

            write_cfg(coe, cfg)

        embed = nextcord.Embed(
            title=f"{char_name} uses Lay on Hands",
            description=effect_text,
            color=random.randint(0, 0xFFFFFF)
        )
        embed.add_field(name="Mode", value=mode.title(), inline=True)
        embed.add_field(name="Uses", value=f"{used}/{uses_max} today", inline=True)
        await ctx.send(embed=embed)

        try:
            bcfg2 = _load_battles()
            if bcfg2.has_section(_section_id(ctx.channel)):
                msg_id = bcfg2.getint(_section_id(ctx.channel), "message_id", fallback=0)
                if msg_id:
                    block = _format_tracker_block(bcfg2, _section_id(ctx.channel))
                    msg = await ctx.channel.fetch_message(msg_id)
                    await msg.edit(content="**EVERYONE ROLL FOR INITIATIVE!**\n```text\n" + block + "\n```")
        except Exception:
            pass

    @commands.command(name="nt")
    async def exploration_turn(self, ctx, turns: int = 1):
        """
        Exploration mode: advance time by N turns (N >= 1, 1 turn == 60 rounds).
        Decrements active conditions for ALL relevant slots by 60/turn (not just listed combatants).
        Applies queued BLIND after PARALYZED ends.
        GM-only. Do NOT use during combat round-wraps (that's already accounted for).
        """
        import random, nextcord

        try:
            turns = int(turns)
        except Exception:
            turns = 1
        if turns <= 0:
            await ctx.send("Turns must be ≥ 1.")
            return

        ROUNDS_PER_TURN = 60
        TOTAL_ROUNDS    = ROUNDS_PER_TURN * turns

        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here. Use `!battle` first.")
            return

        EFFECTS = [
            ("magwep",    "Magic Weapon",      ("magwep_name","magwep_disp"), "🕯️"),
            ("ghh",       "Ghoulish Hands",    (),                             "🧟‍♂️"),
            ("shield",    "Shield",            (),                             "🛡️"),
            ("magearmor", "Mage Armor",        ("magearmor_by",),              "🧱"),
            ("boneskin",  "Boneskin",          ("boneskin_by",),               "🦴"),
            ("sph",       "Spiritual Hammer",  ("sph_bonus",),                 "🔨"),
            ("shl",       "Shillelagh",        ("shl_hit","shl_dmg","shl_die"),"🪵"),
            ("light",     "Light",             ("light_by","light_level"),     "🌟"),
            ("darkness",  "Darkness",          ("dark_by","dark_level"),       "🌑"),
            ("blind",     "Blind",             ("blind_src","blind_by","blind_level"), "🙈"),
            ("paralyzed", "Unconscious/Paralyzed", (),                         "😴"),
            ("cc",        "Color Cloud",       ("cc_by","cc_level"),           "🌫️"),
            ("ck",        "Cloudkill",         ("ck_by","ck_level"),           "☣️"),
            ("hyp",       "Hypnotic Pattern",  ("hyp_by",),                    "🌀"),
            ("cl",        "Call Lightning",    ("cl_by","cl_die","cl_bolts","cl_last_round"), "⚡"),
            ("cn",        "Confusion",         ("cn_by","cn_last","cn_tar","cn_tar_by"),      "😵‍💫"),
            ("db",        "Drainblade",        ("db_by",),                     "🩸"),
            ("inw",       "Immunity to Normal Weapons", ("inw_by",),           "💎"),
            ("pnm",       "Protection from Normal Missiles", ("pnm_by",),      "🚀"),
            ("fear",      "Fear",              ("fear_by","fear_src"),         "😱"),
            ("inv",       "Invisibility",      ("inv_by",),                    "🫥"),
            ("mi",        "Mirror Image",      ("mi_images","mi_by"),          "🪞"),
            ("gas",       "Gaseous Form",      ("gas_by","gas_ac_hint"),       "☁️"),
            ("ps",        "Polymorph (Self)",  ("ps_form","ps_by"),            "🦎"),
            ("stone",     "Stoneskin",         ("stone_by"),                   "🪨"),
            ("swd", "Sword",           (),              "🗡️"),
            ("sc", "Stinking Cloud", ("sc_by","sc_level"), "🤢"),
            ("chl", "Chill (follow-up)", ("chl_charges",), "🥶"),
            ("cr", "Chill Ray", (), "❄️"),
            ("gcr", "Greater Chill Ray", (), "🧊"),
            ("stench", "Stench", (), "🤢"),

        ]
        EFFECT_KEYS = {k for (k, _, _, _) in EFFECTS}

        PERM_FLAG = {"light": "light_perm", "darkness": "darkness_perm", "blind": "blind_perm"}

        slots_to_tick = set()
        slot_label = {}

        for opt_key, _val in cfg.items(chan_id):
            if "." not in opt_key:
                continue
            s, tail = opt_key.split(".", 1)
            if tail in EFFECT_KEYS or tail.startswith("x_"):
                slots_to_tick.add(s)

        names, _scores = _parse_combatants(cfg, chan_id)
        for nm in names:
            try:
                slots_to_tick.add(_slot(nm))
            except Exception:
                slots_to_tick.add(nm.replace(" ", "_"))

        for s in slots_to_tick:
            slot_label[s] = cfg.get(chan_id, f"{s}.disp", fallback=s.replace("_", " "))

        expired_by_label: dict[str, set[str]] = {}
        changed = False

        expl_clock = cfg.getint(chan_id, "expl_clock", fallback=0) + TOTAL_ROUNDS
        cfg.set(chan_id, "expl_clock", str(expl_clock))
        cfg.set(chan_id, "etime_rounds", str(expl_clock))
        changed = True

        for _ in range(turns):
            for s in sorted(slots_to_tick):
                show = slot_label.get(s, s.replace("_", " "))
                par_ended_now = False

                for base_key, pretty, extra_suffixes, emoji in EFFECTS:
                    opt = f"{s}.{base_key}"
                    left = cfg.getint(chan_id, opt, fallback=0)
                    if left <= 0:
                        continue

                    perm = PERM_FLAG.get(base_key)
                    if perm and cfg.getint(chan_id, f"{s}.{perm}", fallback=0) > 0:
                        continue

                    new_left = max(0, left - ROUNDS_PER_TURN)
                    if new_left != left:
                        cfg.set(chan_id, opt, str(new_left))
                        changed = True

                    if base_key == "paralyzed" and left > 0 and new_left == 0:
                        par_ended_now = True

                    if new_left == 0:

                        for suf in extra_suffixes:
                            eopt = f"{s}.{suf}"
                            if cfg.has_option(chan_id, eopt):
                                cfg.remove_option(chan_id, eopt)
                        expired_by_label.setdefault(show, set()).add(f"{emoji} {pretty}")

                    if base_key == "blind" and left > 0 and new_left == 0:

                        applied, note_cnf = _apply_queued_confusion_after_blind(cfg, chan_id, slot_label.get(s, s.replace("_", " ")))
                        if applied > 0:
                            changed = True
                            expired_by_label.setdefault(slot_label.get(s, s.replace("_"," ")), set()).add(f"😵‍💫 Confusion {applied} (from Scintillating Pattern)")

                if par_ended_now:
                    applied, src, _by = _apply_queued_blind_after_paralysis(cfg, chan_id, s)
                    if applied > 0:
                        changed = True
                        label_src = "Color Cloud" if src == "colorcloud" else "Color Spray"
                        expired_by_label.setdefault(show, set()).add(f"🙈 Blind {applied} (from {label_src})")

            for s in sorted(slots_to_tick):
                show = slot_label.get(s, s.replace("_", " "))
                for opt_key, _val in list(cfg.items(chan_id)):
                    if not opt_key.startswith(f"{s}.x_"):
                        continue

                    base_key = opt_key.split(".", 1)[1]

                    if any(base_key.endswith(suf) for suf in (
                        "_label", "_emoji", "_code", "_by", "_dice", "_die", "_src", "_level", "_name"
                    )):
                        continue

                    val = (cfg.get(chan_id, opt_key, fallback="0") or "0").strip()
                    try:
                        left = int(val)
                    except ValueError:

                        continue

                    if left < 0:
                        continue
                    if left <= 0:
                        continue

                    new_left = max(0, left - ROUNDS_PER_TURN)
                    if new_left != left:
                        cfg.set(chan_id, opt_key, str(new_left))
                        changed = True
                    if new_left == 0:
                        label = cfg.get(chan_id, f"{s}.{base_key}_label", fallback=base_key[2:].capitalize())
                        emoji = cfg.get(chan_id, f"{s}.{base_key}_emoji", fallback="⏱️")
                        for suf in ("_label","_emoji","_code","_by"):
                            mkey = f"{s}.{base_key}{suf}"
                            if cfg.has_option(chan_id, mkey):
                                cfg.remove_option(chan_id, mkey)
                        expired_by_label.setdefault(show, set()).add(f"{emoji} {label}")

            for s in sorted(slots_to_tick):

                left_r = cfg.getint(chan_id, f"{s}.x_slowvenom", fallback=0)
                if left_r <= 0:
                    continue

                show = slot_label.get(s, s.replace("_", " "))
                die  = (cfg.get(chan_id, f"{s}.x_slowvenom_dice", fallback="1d6") or "1d6").strip()
                _, path = _resolve_char_ci_local(show)
                if not path or not os.path.exists(path):
                    await ctx.send(f"🧪 {show}: slow venom tick but no character file was found.")
                    continue

                t_cfg = read_cfg(path)

                if self._poison_immune(t_cfg) or _is_undead_cfg(t_cfg, show):
                    for suf in ("", "_dice", "_label"):
                        opt = f"{s}.x_slowvenom{suf}"
                        if cfg.has_option(chan_id, opt):
                            cfg.remove_option(chan_id, opt)
                    _save_battles(cfg)
                    await ctx.send(embed=nextcord.Embed(
                        title=f"🧪 Venom: {show}",
                        description="No effect (poison immune/undead).",
                        color=random.randint(0, 0xFFFFFF)
                    ))

                    expired_by_label.setdefault(show, set()).add("🧪 SLOW VENOM")
                    changed = True
                    continue

                sv_ok, sv_roll, sv_dc, _ = self._roll_save(t_cfg, vs="poi", penalty=0)
                if sv_dc is None:
                    sv_ok = False

                rem_r = max(0, left_r - ROUNDS_PER_TURN)
                if rem_r > 0:
                    cfg.set(chan_id, f"{s}.x_slowvenom", str(rem_r))
                else:

                    for suf in ("", "_dice", "_label"):
                        opt = f"{s}.x_slowvenom{suf}"
                        if cfg.has_option(chan_id, opt):
                            cfg.remove_option(chan_id, opt)
                    expired_by_label.setdefault(show, set()).add("🧪 SLOW VENOM")
                _save_battles(cfg)
                changed = True

                if sv_ok:
                    turns_hint = rem_r // 60
                    desc = (f"Save vs Poison: **{sv_roll}** vs **{sv_dc}** → ✅ **RESISTED**\n"
                            f"_(slow venom: {rem_r} rounds"
                            f"{(' ≈ ' + str(turns_hint) + ' turn' + ('' if turns_hint==1 else 's')) if rem_r else ''} left)_")
                    await ctx.send(embed=nextcord.Embed(
                        title=f"🧪 Venom (slow): {show} resists",
                        description=desc,
                        color=random.randint(0, 0xFFFFFF)
                    ))
                    continue

                ssum, rolls, flat = roll_dice(die)
                raw = ssum + flat
                final, note = _apply_mitigation(raw, weapon_name="Venom", weapon_type="internal", t_cfg=t_cfg)

                old = getint_compat(t_cfg, "cur", "hp", fallback=0)
                new = max(0, old - final)
                if not t_cfg.has_section("cur"): t_cfg.add_section("cur")
                t_cfg["cur"]["hp"] = str(new); write_cfg(path, t_cfg)

                is_mon = (get_compat(t_cfg, "info", "class", fallback="").strip().lower() == "monster")
                dmg_line = f"{die} [{', '.join(str(r) for r in rolls)}]" + (f" → **{final}** ({note})" if note else f" → **{final}**")
                if is_mon:
                    mhp = getint_compat(t_cfg, "max", "hp", fallback=old)
                    before = _life_bar(old, mhp, width=10); after = _life_bar(new, mhp, width=10)
                    hp_line = f"{before} → **{after}**"
                else:
                    hp_line = f"{old} → **{new}**"

                turns_hint = rem_r // 60
                await ctx.send(embed=nextcord.Embed(
                    title=f"🧪 Venom (slow): {show} takes damage",
                    description=(f"Save vs Poison: **{sv_roll}** vs **{sv_dc}** → ❌ **FAIL**\n"
                                 f"{dmg_line}\nHP {hp_line}\n"
                                 f"_(slow venom: {rem_r} rounds"
                                 f"{(' ≈ ' + str(turns_hint) + ' turn' + ('' if turns_hint==1 else 's')) if rem_r else ''} left)_"),
                    color=random.randint(0, 0xFFFFFF)
                ))

                if new <= 0 and is_mon:
                    try:
                        names, scores = _parse_combatants(cfg, chan_id)
                        keyname = show if show in names else (_find_ci_name(names, show) or show)
                        if keyname in names:
                            names = [n for n in names if n != keyname]
                            if cfg.has_option(chan_id, keyname): cfg.remove_option(chan_id, keyname)
                            s_dead = _slot(keyname)
                            for suf in (".dex",".join",".disp",".acpen",".oil",
                                        ".x_slowvenom",".x_slowvenom_dice",".x_slowvenom_label"):
                                opt = f"{s_dead}{suf}"
                                if cfg.has_option(chan_id, opt): cfg.remove_option(chan_id, opt)
                            _write_combatants(cfg, chan_id, names, scores); _save_battles(cfg)
                        try: os.remove(os.path.abspath(path))
                        except Exception: pass
                    except Exception:
                        pass

        if changed:
            _save_battles(cfg)

        try:
            _purge_zero_x_effects(cfg, chan_id)
        except Exception:
            pass

        try:
            await self._update_tracker_message(ctx, cfg, chan_id)
        except Exception:
            pass

        try:
            await self._apply_turn_disease_local(ctx, cfg, chan_id)
        except Exception:
            pass

        try:
            await self._apply_turn_strength_recovery(ctx, cfg, chan_id)
        except Exception:
            pass

        embed = nextcord.Embed(
            title="⏳ Exploration Time",
            description=(
                f"**{turns}** exploration turn{'s' if turns!=1 else ''} passed "
                f"(**{TOTAL_ROUNDS} rounds**)."
            ),
            color=random.randint(0, 0xFFFFFF),
        )
        embed.add_field(
            name="Exploration clock",
            value=f"{expl_clock} rounds (≈ {expl_clock // 60} turns)",
            inline=False,
        )
        if not expired_by_label:
            embed.add_field(name="Expired effects", value="None this time.", inline=False)
        else:
            lines = [f"**{actor}**: " + ", ".join(sorted(fx))
                     for actor, fx in sorted(expired_by_label.items())]
            embed.add_field(name="Expired effects", value="\n".join(lines), inline=False)

        await ctx.send(embed=embed)

    def _get_expl_clock(self, cfg, chan_id: str) -> int:

        if cfg.has_option(chan_id, "expl_clock"):
            return cfg.getint(chan_id, "expl_clock", fallback=0)
        if cfg.has_option(chan_id, "etime_rounds"):
            return cfg.getint(chan_id, "etime_rounds", fallback=0)
        return cfg.getint(chan_id, "round", fallback=0)

    def _tick_confusion_on_turn(self, cfg, chan_id: str, name_key: str) -> tuple[bool, str | None]:
        """
        Decrement <slot>.cn when this creature's turn begins.
        Returns (still_confused, status_text_for_channel_or_None).
        Retaliation (cn_retaliate_by) overrides the d10 roll once, then clears.
        NEW: if <slot>.cn_perm > 0, Confusion is permanent — do NOT decrement .cn.
        """
        import random

        def _choose_slot_for_effects(cfg, chan_id, name):
            cands = []
            try:
                cands.append(_slot(name))
            except Exception:
                pass
            cands.append(str(name).replace(" ", "_"))
            prefer = ("cn","paralyzed","blind","cc_blind_pending","cs_blind_pending","cc",
                      "magwep","ghh","magearmor","boneskin","sph","shl","chl","light","darkness")
            for s in cands:
                if any(cfg.has_option(chan_id, f"{s}.{k}") for k in prefer):
                    return s
            return cands[0]

        s = _choose_slot_for_effects(cfg, chan_id, name_key)

        left = cfg.getint(chan_id, f"{s}.cn", fallback=0)
        if left <= 0:
            legacy = cfg.getint(chan_id, f"{s}.cnf", fallback=0)
            if legacy > 0:
                left = legacy
                cfg.set(chan_id, f"{s}.cn", str(legacy))
                cfg.remove_option(chan_id, f"{s}.cnf")
                _save_battles(cfg)

        par = cfg.getint(chan_id, f"{s}.para", fallback=0)
        if par > 0:

            return (True, None)

        is_perm = cfg.getint(chan_id, f"{s}.cn_perm", fallback=0) > 0

        if left <= 0 and not is_perm:
            return (False, None)

        if is_perm and left <= 0:
            cfg.set(chan_id, f"{s}.cn", "1")
            _save_battles(cfg)
            left = 1

        if not is_perm:
            left = max(0, left - 1)
            if left == 0:
                cfg.remove_option(chan_id, f"{s}.cn")
                if cfg.has_option(chan_id, f"{s}.cn_by"): cfg.remove_option(chan_id, f"{s}.cn_by")
            else:
                cfg.set(chan_id, f"{s}.cn", str(left))

        ret_by = (cfg.get(chan_id, f"{s}.cn_retaliate_by", fallback="") or "").strip()
        ret_rd = cfg.getint(chan_id, f"{s}.cn_retaliate_round", fallback=-1)
        if ret_by:
            if cfg.has_option(chan_id, f"{s}.cn_retaliate_by"): cfg.remove_option(chan_id, f"{s}.cn_retaliate_by")
            if cfg.has_option(chan_id, f"{s}.cn_retaliate_round"): cfg.remove_option(chan_id, f"{s}.cn_retaliate_round")
            _save_battles(cfg)
            cur_rd = cfg.getint(chan_id, "round", fallback=0)
            when = " (from last turn)" if (ret_rd >= 0 and ret_rd < cur_rd) else ""
            return ((left > 0) or is_perm,
                    f"{name_key} is **CONFUSED** — was attacked by **{ret_by}**{when} and will **RETALIATE** now (overrides d10).")

        d = random.randint(1, 10)
        if d == 1:
            txt = "acts normally."
        elif d == 2:
            txt = "moves toward the **caster** and attacks if possible."
        elif 3 <= d <= 5:
            txt = "does nothing except babble."
        elif 6 <= d <= 7:
            txt = "moves swiftly away from the **caster**."
        else:
            txt = "attacks the **nearest** creature (friend or foe)."

        _save_battles(cfg)
        return ((left > 0) or is_perm,
                f"{name_key} is **CONFUSED** — d10 **{d}** → {txt} (DM adjudicates; if impossible, shift down the table).")

    def _spell_pool(self, for_class: str) -> list[str]:
        """
        Returns a deduped, sorted pool of spell names for the given class.
        Looks for _spells_by_class on the Spells cog; empty list if unavailable.
        """
        spells_cog = self.bot.get_cog("SpellsCog") or self.bot.get_cog("Spells")
        by_class = getattr(spells_cog, "_spells_by_class", None) if spells_cog else None

        pool: list[str] = []
        if isinstance(by_class, dict):
            by_lvl = by_class.get(for_class, {})
            if isinstance(by_lvl, dict):
                for names in by_lvl.values():
                    pool.extend(names)
        return sorted(set(pool))

    def _pick_spells(self, for_class: str, nsp: int) -> list[str]:
        pool = self._spell_pool(for_class)
        if not pool:
            return [f"(no spells found)"]
        import random
        pool = pool[:]
        random.shuffle(pool)
        return pool[:max(1, nsp)]

    @commands.command(name="treasure")
    async def treasure_preview(self, ctx, *args):
        """
        Generate one or more lair hoards by type (A..O).

        DEFAULT (no flags): dry-run = roll actual items (potions/scrolls/magic) WITHOUT changing tallies.
        Flags:
          -preview            : preview bullets only (no state change, no item rolls)
          -apply | -a         : apply tallies (no item rolls)
          -roll | -r | -gen  : apply + roll items (consumes tallies like !loot)

        Examples:
          !treasure e
          !treasure e x3 -preview
          !treasure e g -apply
          !treasure e g -roll
        """
        
        toks = [str(a).strip() for a in args if str(a).strip()]
        do_roll    = any(t in {"-roll", "-r", "-gen", "-generate"} for t in toks)
        do_apply   = do_roll or any(t in {"-apply", "-a"} for t in toks)
        do_preview = any(t in {"-preview", "-p"} for t in toks)
        toks = [t for t in toks if t not in {
            "-roll","-r","-gen","-generate","-apply","-a","-preview","-p"
        }]

        s = " ".join(toks).replace(",", " ").lower()
        parts = re.split(r"\s+", s) if s else []
        codes, times = [], 1
        for p in parts:
            m = re.fullmatch(r"x?(\d+)", p)
            if m:
                times = max(1, int(m.group(1))); continue
            for ch in p:
                if ch.isalpha():
                    codes.append(ch.upper())

        if not codes:
            await ctx.send("Usage: `!treasure <A..O> [xN] [-preview|-apply|-roll]`")
            return

        def preview_lines():
            out = []
            for code in codes:
                for _ in range(times):
                    out.extend(_roll_one_lair_preview(code))
            return out

        if do_preview and not do_apply:
            lines = preview_lines()
            for chunk in _chunk_send_lines(lines, 12):
                await ctx.send("\n".join(chunk))
            return

        bcfg = _load_battles()
        chan_id = _section_id(ctx.channel)

        if do_apply:
            if not (bcfg and bcfg.has_section(chan_id)):
                await ctx.send("❌ No initiative running here. Use `!init` first, or omit `-apply`.")
                return

            for code in codes:
                for _ in range(times):
                    _roll_one_lair_and_apply(bcfg, chan_id, code)
            _save_battles(bcfg)

            await ctx.send("🏴 **Applied:** " + ", ".join(f"{c}×{times}" for c in codes))

            if do_roll:
                try:    await self.roll_potions(ctx, "all")
                except Exception:  pass
                try:    await self.roll_scrolls(ctx, "all")
                except Exception:  pass
                try:    await self.roll_magic_items(ctx, "all")
                except Exception:  pass
            return

        had_section = bcfg.has_section(chan_id)
        if not had_section:
            bcfg.add_section(chan_id)

        before = {}
        if had_section:
            for k, v in bcfg.items(chan_id):
                before[k] = v

        try:
            preview = []
            total_coins = dict(cp=0, sp=0, ep=0, gp=0, pp=0)
            for code in codes:
                for _ in range(times):
                    r = _roll_lair_once(code)  
                    for k in ("cp","sp","ep","gp","pp"):
                        total_coins[k] += r.get(k, 0)

                    c = r
                    if sum(c.get(k,0) for k in ("cp","sp","ep","gp","pp")):
                        if c.get("cp"): preview.append(f"• CP: +{c['cp']}")
                        if c.get("sp"): preview.append(f"• SP: +{c['sp']}")
                        if c.get("ep"): preview.append(f"• EP: +{c['ep']}")
                        if c.get("gp"): preview.append(f"• GP: +{c['gp']}")
                        if c.get("pp"): preview.append(f"• PP: +{c['pp']}")
                    if c.get("gems"):    preview.append(f"• Gems: +{c['gems']}")
                    if c.get("jewelry"): preview.append(f"• Jewelry: +{c['jewelry']}")
                    if c.get("magic_any"): preview.append(f"• Magic (any): +{c['magic_any']}")
                    if c.get("magic_wa"):  preview.append(f"• Magic (weapons/armor): +{c['magic_wa']}")
                    if c.get("magic_xw"):  preview.append(f"• Magic (except weapons): +{c['magic_xw']}")
                    if c.get("potions"): preview.append(f"• Potions: +{c['potions']}")
                    if c.get("scrolls"): preview.append(f"• Scrolls: +{c['scrolls']}")

                    _add_coins(bcfg, chan_id,
                               cp=c.get("cp",0), sp=c.get("sp",0), ep=c.get("ep",0),
                               gp=c.get("gp",0), pp=c.get("pp",0))
                    _add_misc(bcfg, chan_id,
                              gems=c.get("gems",0), jewelry=c.get("jewelry",0),
                              magic_any=c.get("magic_any",0),
                              magic_wa=c.get("magic_wa",0),
                              magic_xw=c.get("magic_xw",0),
                              potions=c.get("potions",0), scrolls=c.get("scrolls",0))

            _save_battles(bcfg)

            if preview:
                await ctx.send("🏴 Lair " + ", ".join(codes) + " (preview):\n" + "\n".join(preview) +
                               "\n_Note: dry-run only — tallies not changed._")

            try:    await self.roll_potions(ctx, "all")
            except Exception:  pass
            try:    await self.roll_scrolls(ctx, "all")
            except Exception:  pass
            try:    await self.roll_magic_items(ctx, "all")
            except Exception:  pass

        finally:
            if had_section:
                for k in list(bcfg[chan_id].keys()):
                    try: bcfg.remove_option(chan_id, k)
                    except Exception: pass
                for k, v in before.items():
                    bcfg.set(chan_id, k, v)
            else:
                try: bcfg.remove_section(chan_id)
                except Exception: pass
            _save_battles(bcfg)




    async def _announce_exploration(self, ctx, cfg, chan_id: str, who: str):
        """Pretty ping for exploration turns: movement + tips."""
        base_mv, turn_mv = _move_rates_for_char(who)
        et = cfg.getint(chan_id, "etime_rounds", fallback=0)

        _, _, _, owner = _char_snapshot(who)
        mention = self._owner_mention(who_name)

        desc_lines = [
            f"Movement this turn: **{turn_mv}′** (3× base {base_mv}′).",
            f"Exploration clock: **{et}** rounds (≈ {et // 60} turn{'s' if (et // 60) != 1 else ''}).",
            "",
            "Things you can do now:",
            "• `!door` to search for secret doors",
            "• `!trap` to search for traps",
            "• `!s listen` (or another thief skill)",
            "• RP: search for something else, map the area, devise a trap, set night watch, wait in ambush, etc.",
        ]
        embed = nextcord.Embed(
            title=f"🧭 Exploration Turn: {who}",
            description="\n".join(desc_lines),
            color=random.randint(0, 0xFFFFFF)
        )

        if mention:
            await ctx.send(
                mention,
                embed=embed,
                allowed_mentions=nextcord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        else:
            await ctx.send(embed=embed)

    def _format_turns_from_rounds(self, rounds: int) -> str:
        if rounds <= 0:
            return "≈ 0 turns"
        return f"≈ {rounds // 60} turns" if rounds % 60 == 0 else f"≈ {rounds / 60:.1f} turns"

    def _feet(self, n: int | None) -> str:
        try:
            n = int(n)
        except Exception:
            n = 0
        return f"{n}′"

    def _display_name_for(self, cfg, chan_id: str, name: str) -> str:
        try:
            slot = _choose_slot_for_effects(cfg, chan_id, name)
        except Exception:
            slot = name.replace(" ", "_")
        return cfg.get(chan_id, f"{slot}.disp", fallback=name) or name

    def _exploration_embed(self, cfg, chan_id: str, who_name: str) -> nextcord.Embed:
        disp = self._display_name_for(cfg, chan_id, who_name)

        base_mv, turn_mv = _move_rates_for_char(disp)
        clock = self._get_expl_clock(cfg, chan_id)

        lines = [
            f"Movement this turn: {self._feet(turn_mv)} (3× base {self._feet(base_mv)}).",
            f"Exploration clock: {clock} rounds ({self._format_turns_from_rounds(clock)}).",
            "",
            "Things you can do now:",
            "• `!door` to search for secret doors",
            "• `!trap` to search for traps",
            "• `!s listen` (or another thief skill)",
            "• RP: search for something else, map the area, devise a trap, set night watch, wait in ambush, etc.",
        ]

        return nextcord.Embed(
            title=f"🧭 Exploration Turn: {disp}",
            description="\n".join(lines),
            color=random.randint(0, 0xFFFFFF),
        )

    async def _announce_exploration(self, ctx, cfg, chan_id: str, who_name: str):
        try:
            await ctx.send(embed=self._exploration_embed(cfg, chan_id, who_name))
        except Exception:
            await ctx.send(f"🧭 Exploration Turn: **{who_name}**")

    @commands.command(name="t")
    async def exploration_step(self, ctx):
        """
        Exploration order ping:
          - Announces who's up next for exploration with a mention.
          - Posts an exploration embed (movement, clock, actions).
          - On wrap to the top, runs 'nt' (or fallback) *then* shows the updated clock.
        """
        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here. Use `!init` first.")
            return

        names, _ = _parse_combatants(cfg, chan_id)
        if not names:
            await ctx.send("ℹ️ No combatants yet.")
            return

        next_name, wraps = _exploration_next(cfg, chan_id)
        if not next_name:
            await ctx.send("ℹ️ No one to ping.")
            return

        cfg.set(chan_id, "turn_e", next_name)
        _save_battles(cfg)

        if wraps:
            try:
                nt_cmd = self.bot.get_command("nt")
                if nt_cmd:
                    await ctx.invoke(nt_cmd)
                else:
                    old = cfg.getint(chan_id, "round", fallback=0)
                    cfg.set(chan_id, "round", str(old + 60))
                    _save_battles(cfg)
                    await ctx.send("⌛ **A full exploration turn passes.**")
                    try:    await self._apply_turn_disease_local(ctx, cfg, chan_id)
                    except: pass
                    try:    await self._apply_turn_strength_recovery(ctx, cfg, chan_id)
                    except: pass
                    try:    await self._update_tracker_message(ctx, cfg, chan_id)
                    except: pass
            except Exception:
                pass

            cfg = _load_battles()

            try:
                _purge_zero_x_effects(cfg, chan_id)
                _save_battles(cfg)
                await self._update_tracker_message(ctx, cfg, chan_id)
            except Exception:
                pass

        try:
            base_mv, turn_mv = _move_rates_for_char(next_name)
        except Exception:
            base_mv, turn_mv = 30, 90

        exp_rounds = self._get_expl_clock(cfg, chan_id)

        emb = nextcord.Embed(
            title=f"🧭 Exploration Turn: {next_name}",
            color=nextcord.Color.blurple()
        )
        emb.add_field(name="Movement this turn", value=f"**{turn_mv}**′ (3× base {base_mv}′)", inline=False)
        emb.add_field(name="Exploration clock",
            value=f"{exp_rounds} rounds ({self._format_turns_from_rounds(exp_rounds)})",
            inline=False)
        emb.add_field(
            name="Things you can do now",
            value=(
                "• `!door` to search for secret doors\n"
                "• `!trap` to search for traps\n"
                "• `!s` listen (or another thief skill)\n"
                "• RP: search for something else, map the area, devise a trap, set night watch, wait in ambush, etc."
            ),
            inline=False
        )

        mention = _owner_mention(next_name, cfg, chan_id)
        content = f"{mention}" if mention else None

        await ctx.send(
            content=content,
            embed=emb,
            allowed_mentions=nextcord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    @commands.command(name="treset")
    async def exploration_reset(self, ctx):
        cfg = _load_battles(); chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here."); return
        cfg.set(chan_id, "turn_e", "")
        _save_battles(cfg)
        await ctx.send("🔄 Exploration order cursor reset to the top.")

    async def _apply_turn_disease_local(self, ctx, bcfg, chan_id: str):
        """
        One 10-minute turn passes: for all *listed* combatants that are diseased,
        apply –1 CON (temp), then a Save vs Death to end the disease on success.
        Uses the combat cog's helpers for the actual CON/HP math & saving throw.
        """
        try:
            names, _ = _parse_combatants(bcfg, chan_id)
        except Exception:
            names = []

        if not names:
            return

        combat = (self.bot.get_cog("Combat")
                  or self.bot.get_cog("SpellsCog")
                  or self.bot.get_cog("Spells"))
        if not combat:
            await ctx.send("⚠️ Disease system unavailable (Combat/Spells cog not loaded).")
            return

        lines = []
        for name in names:
            disp, path = _resolve_char_ci_local(name)
            if not path or not os.path.exists(path):
                continue
            cfg = read_cfg(path)
            if getint_compat(cfg, "cur", "disease", fallback=0) <= 0:
                continue

            try:
                hp_delta = combat._apply_conloss_points(cfg, 1)
                write_cfg(path, cfg)
            except Exception:
                hp_delta = 0

            try:
                cur_con, temp, perm, peak, base = combat._get_con_state(cfg)
            except Exception:
                cur_con, temp = None, getint_compat(cfg, "cur", "con_loss_temp", fallback=0)

            line = f"**{disp}**: −1 CON (temp → {temp}; cur CON {cur_con})"
            if hp_delta:
                max_hp = getint_compat(cfg, "max", "hp", fallback=0)
                line += f"; Max HP {'+' if hp_delta>0 else ''}{hp_delta}, now {max_hp}"

            try:
                ok, roll, dc, _ = combat._roll_save(cfg, vs="poi", penalty=0)
            except Exception:
                ok, roll, dc = False, "—", "—"

            if ok:
                if not cfg.has_section("cur"):
                    cfg.add_section("cur")
                cfg["cur"]["disease"] = "0"
                write_cfg(path, cfg)
                line += f"\n→ Save vs Death: {roll} vs {dc} **SUCCESS** — disease **cured**."
            else:
                line += f"\n→ Save vs Death: {roll} vs {dc} **FAIL** — disease **persists**."

            lines.append(line)

        if lines:
            emb = nextcord.Embed(
                title="🦠 Disease Progression",
                description="\n\n".join(lines),
                color=0x7f1d1d
            )
            await ctx.send(embed=emb)

    @commands.command(name="track", aliases=["tr"])
    async def track_timer(self, ctx, *, expr: str = ""):
        """
        Start a generic timer on a combatant's slot.
        Usage:
          !track <name> [target] <dur>
        Examples:
          !track detectinvisible go1 1t/lvl
          !track holdportal door 5t
          !track torch 1h
          !track detectinvisible 600
        """
        import re, os

        if not expr or not expr.strip():
            await ctx.send("Usage: `!track <name> [target] <dur>` — e.g., `!track detectinvisible go1 1t/lvl` or `!track torch 1h`")
            return

        def _norm_tag(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

        def _title_from_tag(tag: str) -> str:
            parts = re.findall(r"[a-zA-Z]+", tag)
            return " ".join(p.capitalize() for p in parts) or tag.capitalize()

        def _default_code(label: str) -> str:
            words = [w for w in re.findall(r"[A-Za-z]+", label)]
            if len(words) >= 2:
                code = "".join(w[0] for w in words[:3]).upper()
            else:
                code = (re.sub(r"[^A-Za-z]", "", label).upper()[:3] or "FX")
            return code

        def _parse_dur_to_rounds(tok: str, caster_level: int) -> int | None:
            if not tok:
                return None
            s = tok.strip().lower()
            m = re.match(r"^\s*(\d+)\s*([rth])(?:\s*(?:/|per\s*)(?:lvl|level))?\s*$", s)
            if not m:
                m2 = re.match(r"^\s*(\d+)\s*(rounds?|turns?|hours?)\s*(?:/(?:lvl|level)|\s*per\s*(?:lvl|level))?\s*$", s)
                if not m2:
                    if s.isdigit():
                        return int(s)
                    return None
                n = int(m2.group(1))
                unit = m2.group(2)
                per_lvl = bool(re.search(r"(?:/|per)\s*(?:lvl|level)", s))
                base = n if unit.startswith("round") else (n*60 if unit.startswith("turn") else n*360)
                return base * (caster_level if per_lvl else 1)

            n = int(m.group(1))
            unit_ch = m.group(2)
            per_lvl = bool(re.search(r"(?:/|per)\s*(?:lvl|level)\s*$", s))
            base = n if unit_ch == "r" else (n*60 if unit_ch == "t" else n*360)
            return base * (caster_level if per_lvl else 1)

        char_name = get_active(ctx.author.id)
        if not char_name:
            await ctx.send("❌ No active character. Use `!char <name>` first.")
            return

        char_path = f"{char_name.replace(' ', '_')}.coe"
        if not os.path.exists(char_path):
            await ctx.send(f"❌ Character file not found for **{char_name}**.")
            return
        ccfg = read_cfg(char_path)
        caster_level = getint_compat(ccfg, "cur", "level", fallback=1)

        toks = [t for t in re.split(r"\s+", expr.strip()) if t]
        if not toks:
            await ctx.send("Usage: `!track <name> [target] <dur>`")
            return

        name_tok = toks[0]
        tag = _norm_tag(name_tok)
        if not tag:
            await ctx.send("Please provide a valid name for the timer.")
            return

        dur_tok = toks[-1] if len(toks) >= 2 else ""
        target_guess = None
        if len(toks) >= 3:
            target_guess = toks[1]
        elif len(toks) == 2:
            if re.search(r"\d|[rth]($|\W)|round|turn|hour", toks[1].lower()):
                target_guess = None
            else:
                target_guess = toks[1]

        bcfg = _load_battles()
        chan_id = str(ctx.channel.id)
        if not bcfg.has_section(chan_id):
            await ctx.send("❌ No initiative here. Use `!init` first.")
            return

        target_name = target_guess or char_name
        names, _ = _parse_combatants(bcfg, chan_id)
        name_key = _find_ci_name(names, target_name) or target_name
        try:
            slot = _slot(name_key)
        except Exception:
            slot = name_key.replace(" ", "_")

        rounds = _parse_dur_to_rounds(dur_tok, caster_level) if dur_tok else None
        if rounds is None:
            await ctx.send("❌ Couldn’t parse duration. Try `120`, `10r`, `1t`, `1t/lvl`, or `1h`.")
            return
        rounds = max(0, int(rounds))

        label = _title_from_tag(tag)
        code  = _default_code(label)
        default_emoji = {
            "detectinvisible": "🔍",
            "holdportal": "🚪",
            "detectmagic": "🔍",
        }.get(tag, "⏱️")

        bcfg.set(chan_id, f"{slot}.x_{tag}", str(rounds))
        bcfg.set(chan_id, f"{slot}.x_{tag}_label", label)
        bcfg.set(chan_id, f"{slot}.x_{tag}_code", code)
        bcfg.set(chan_id, f"{slot}.x_{tag}_emoji", default_emoji)
        bcfg.set(chan_id, f"{slot}.x_{tag}_by", char_name)
        _save_battles(bcfg)

        try:
            await self._update_tracker_message(ctx, bcfg, chan_id)
        except Exception:
            pass

        await ctx.send(f"🧭 Tracking **{label}** on **{bcfg.get(chan_id, f'{slot}.disp', fallback=name_key)}**: **{rounds} rounds** ({code}).")

    def _dance_clear_flags(self, cfg, chan_id: str, slot: str):
        for suf in ("", "_label", "_code", "_kind"):
            opt = f"{slot}.x_dance{suf}"
            if cfg.has_option(chan_id, opt):
                cfg.remove_option(chan_id, opt)
        _save_battles(cfg)

    @commands.command(name="ds")
    async def destatus(self, ctx, who: str, tag: str):
        """
        GM-only: remove a status from a combatant.
        Examples:
          !ds wizard inv
          !ds wizard ss
          !ds goblin mi
        """
        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here."); return

        dm_id = cfg.get(chan_id, "DM", fallback="")
        if str(ctx.author.id) != str(dm_id):
            await ctx.send("❌ Only the DM can use !ds here."); return

        names, _ = _parse_combatants(cfg, chan_id)
        key  = _find_ci_name(names, who) or who
        slot = _choose_slot_for_effects(cfg, chan_id, key)
        code = (tag or "").strip().lower()

        CLEAR_MAP = {

          "mi": ["mi","mi_images","mi_by"],
          "mirror": ["mi","mi_images","mi_by"], "mirrorimage":["mi","mi_images","mi_by"],
          "inv": ["inv","inv_perm"], "invis":["inv","inv_perm"], "invisible":["inv","inv_perm"],
          "shield": ["shield","shield_by"], "sh":["shield","shield_by"],
          "ma": ["magearmor","magearmor_by"], "magearmor":["magearmor","magearmor_by"],
          "bs": ["boneskin","boneskin_by"], "boneskin":["boneskin","boneskin_by"],
          "mw": ["magwep","magwep_name","magwep_disp"], "magwep":["magwep","magwep_name","magwep_disp"],

          "blind": ["blind","blind_by","blind_src","blind_perm","blind_level"],
          "bl": ["blind","blind_by","blind_src","blind_perm","blind_level"],
          "light": ["light","light_perm","light_by","light_level"], "lt":["light","light_perm","light_by","light_level"],
          "dark": ["darkness","darkness_perm","dark_by","dark_level"], "dk":["darkness","darkness_perm","dark_by","dark_level"],

          "par": ["paralyzed","paralyzed_by","cc_blind_pending","cs_blind_pending","cc_blind_by","cs_blind_by"],
          "paralyzed": ["paralyzed","paralyzed_by","cc_blind_pending","cs_blind_pending","cc_blind_by","cs_blind_by"],
          "fr": ["fear","fear_by","fear_src"], "fear":["fear","fear_by","fear_src"],
          "hyp": ["hyp","hyp_by"],

          "cc": ["cc","cc_by","cc_level"], "colorspray":["cc","cc_by","cc_level"],
          "ck": ["ck","ck_by","ck_level"],
          "sc": ["sc","sc_by","sc_level"],

          "sph": ["sph","sph_bonus"], "sw":["sph","sph_bonus"], "spiritualhammer":["sph","sph_bonus"],
          "shl": ["shl","shl_hit","shl_dmg","shl_die"], "sl":["shl","shl_hit","shl_dmg","shl_die"], "shillelagh":["shl","shl_hit","shl_dmg","shl_die"],
          "ghh": ["ghh","ghf"], "gh":["ghh","ghf"],
          "swd": ["swd", "swd_bonus"],
          "chl": ["chl","chl_charges"], "chill": ["chl","chl_charges"],
          "cr": ["cr"],
          "gcr": ["gcr"],

          "db": ["db"], "drainblade":["db"],
          "inw": ["inw"], "immunitynormalweapons":["inw"],
          "pnm": ["pnm"],
          "gas": ["gas","gas_by","gas_ac_hint"],

          "re": ["x_rayexhaustion"], "ray": ["x_rayexhaustion"], "rayexhaustion": ["x_rayexhaustion"],

          "pet": ["pet", "pet_perm", "pet_by"],

          "ps":   ["ps", "ps_by", "ps_form", "ps_src"],
          "poly": ["poly", "polyself", "polyself_by", "polyform", "ps", "ps_by", "ps_form", "ps_src"],

          "cl": ["cl","cl_by","cl_bolts","cl_die","cl_last_round"],
          "cn": ["cn","cn_by","cn_last","cn_tar","cn_tar_by"],

          "bc": ["curse","curse_perm"], "curse": ["curse","curse_perm"],
          "fb": ["feeble","feeble_perm"], "feeble":["feeble","feeble_perm"], "feeblemind":["feeble","feeble_perm"],

          "ss": ["stone","stonehp","stone_by"],
          "stone": ["stone","stonehp","stone_by"], "stoneskin": ["stone","stonehp","stone_by"],

          "gs": ["gs", "gs_bonus", "x_giantstrength", "x_giantstrength_label", "x_giantstrength_code", "x_giantstrength_by"],
          "giant": ["gs", "gs_bonus", "x_giantstrength", "x_giantstrength_label", "x_giantstrength_code", "x_giantstrength_by"],
          "giantstrength": ["gs", "gs_bonus", "x_giantstrength", "x_giantstrength_label", "x_giantstrength_code", "x_giantstrength_by"],

          "gr": ["growth", "growth_bonus", "x_growth", "x_growth_label", "x_growth_code", "x_growth_by"],
          "growth": ["growth", "growth_bonus", "x_growth", "x_growth_label", "x_growth_code", "x_growth_by"],

          "hero": ["heroism", "atkmod_heroism", "x_heroism", "x_heroism_label", "x_heroism_code", "x_heroism_by"],
          "heroism": ["heroism", "atkmod_heroism", "x_heroism", "x_heroism_label", "x_heroism_code", "x_heroism_by"],

          "inw": ["inw", "pot_ac"],
          "invulnerability": ["inw", "pot_ac"],

          "web": ["web","web_state","web_canbrk","webignite"],
          "wb":  ["web","web_state","web_canbrk","webignite"],

          "pet": ["pet", "pet_perm", "pet_by"],
          "petrify": ["pet", "pet_perm", "pet_by"],
          "petrification": ["pet", "pet_perm", "pet_by"],

          "bless": ["x_bless", "bless_hit"],
          "bane":  ["x_bane",  "bane_hit"],

          "bk": ["barkskin","barkskin_by","x_barkskin"],
          "bark": ["barkskin","barkskin_by","x_barkskin"],
          "barkskin": ["barkskin","barkskin_by","x_barkskin"],

          "goa": ["x_growanimal", "growanimal"],
          "growanimal": ["x_growanimal", "growanimal"],
          "growthofanimals": ["x_growanimal", "growanimal"],

          "enhwep":  ["enhwep","enh_wep","enhwep_bonus","enh_wep_bonus","enhwep_name","enh_wep_name"],
          "weakwep": ["weakwep","weak_wep","weakwep_bonus","weak_wep_bonus","weakwep_name","weak_wep_name"],

          "enharmor":  ["enharmor","enh_armor","enharmor_bonus"],
          "weakarmor": ["weakarmor","weak_armor"],

          "stench": ["stench_pen","stench_label","stench_emoji","x_stenchn","stn","stench","sick"],
          "sick":   ["stench_pen","stench_label","stench_emoji","x_stenchn","stn","stench","sick"],

          "gazereflection": ["x_gazereflection"],
          "grf": ["x_gazereflection"],

          "oil": ["oil"],

          "coil":    ["x_constrict"],
          "constrict":["x_constrict"],
          "vine":    ["x_entangle"],
          "entangle":["x_entangle"],
          "gullet":  ["x_swallow"],
          "swallow": ["x_swallow"],
          "ro":       ["x_rotgrub", "x_rotgrub_burn"],
          "rotgrub":  ["x_rotgrub", "x_rotgrub_burn"],
          "grubs":    ["x_rotgrub", "x_rotgrub_burn"],

          "spore":  ["x_spores","x_spores_dice","x_spores_label","x_spores_emoji","x_spores_code","x_spores_by"],
          "spores": ["x_spores","x_spores_dice","x_spores_label","x_spores_emoji","x_spores_code","x_spores_by"],
          "sporecd":["x_sporecd"],

          "acid":     ["x_dissolve","x_dissolve_dice","x_dissolve_label","x_dissolve_emoji","x_dissolve_code","x_dissolve_by"],
          "dissolve": ["x_dissolve","x_dissolve_dice","x_dissolve_label","x_dissolve_emoji","x_dissolve_code","x_dissolve_by"],
          "da":       ["x_dissolve","x_dissolve_dice","x_dissolve_label","x_dissolve_emoji","x_dissolve_code","x_dissolve_by"],

          "venom":     ["x_fastvenom","x_fastvenom_dice","x_fastvenom_label"],
          "fv":        ["x_fastvenom","x_fastvenom_dice","x_fastvenom_label"],
          "fastvenom": ["x_fastvenom","x_fastvenom_dice","x_fastvenom_label"],

          "hold":  ["x_holdbite","x_holdbite_dice","x_holdbite_label","x_holdbite_emoji","x_holdbite_code","x_holdbite_by"],
          "worry": ["x_holdbite","x_holdbite_dice","x_holdbite_label","x_holdbite_emoji","x_holdbite_code","x_holdbite_by"],

          "leech": ["x_leech","x_leech_dice","x_leech_label","x_leech_emoji","x_leech_code","x_leech_by"],

          "tentacles": ["x_tentacles","x_tentacles_dmg","x_tentacles_label","x_tentacles_emoji","x_tentacles_code","x_tentacles_by"],
          "bt":        ["x_tentacles","x_tentacles_dmg","x_tentacles_label","x_tentacles_emoji","x_tentacles_code","x_tentacles_by"],

          "swarm": ["x_swarm","x_swarm_code","x_swarm_label","x_swarm_emoji","x_swarm_stat","x_swarm_torch"],
          "torch": ["x_swarm_torch"],
          "ward":  ["x_swarm_torch"],

          "hm":         ["x_heatmetal","x_heatmetal_label","x_heatmetal_code","x_heatmetal_emoji"],
          "heatmetal":  ["x_heatmetal","x_heatmetal_label","x_heatmetal_code","x_heatmetal_emoji"],
          "cm":         ["x_chillmetal","x_chillmetal_label","x_chillmetal_code","x_chillmetal_emoji"],
          "chillmetal": ["x_chillmetal","x_chillmetal_label","x_chillmetal_code","x_chillmetal_emoji"],

          "ro":       ["x_rotgrub","x_rotgrub_burn","x_rotgrub_label","x_rotgrub_emoji","x_rotgrub_code","x_rotgrub_by"],
          "rotgrub":  ["x_rotgrub","x_rotgrub_burn","x_rotgrub_label","x_rotgrub_emoji","x_rotgrub_code","x_rotgrub_by"],
          "grubs":    ["x_rotgrub","x_rotgrub_burn","x_rotgrub_label","x_rotgrub_emoji","x_rotgrub_code","x_rotgrub_by"],

          "mg":       ["maggots","maggots_die","x_maggots","x_maggots_dmg","x_maggots_label","x_maggots_emoji","x_maggots_code","x_maggots_by"],
          "maggots":  ["maggots","maggots_die","x_maggots","x_maggots_dmg","x_maggots_label","x_maggots_emoji","x_maggots_code","x_maggots_by"],

          "coil":      ["x_constrict","x_constrict_dice","x_constrict_label","x_constrict_emoji","x_constrict_code","x_constrict_by"],
          "constrict": ["x_constrict","x_constrict_dice","x_constrict_label","x_constrict_emoji","x_constrict_code","x_constrict_by"],
          "vine":      ["x_entangle","x_entangle_dice","x_entangle_label","x_entangle_emoji","x_entangle_code","x_entangle_by"],
          "entangle":  ["x_entangle","x_entangle_dice","x_entangle_label","x_entangle_emoji","x_entangle_code","x_entangle_by"],

          "gullet":  ["x_swallow","x_swallow_dice","x_swallow_label","x_swallow_emoji","x_swallow_code","x_swallow_by"],
          "swallow": ["x_swallow","x_swallow_dice","x_swallow_label","x_swallow_emoji","x_swallow_code","x_swallow_by"],

          "maze": ["x_maze","x_maze_label","x_maze_emoji","x_maze_code","x_maze_by"],
          "mz":   ["x_maze","x_maze_label","x_maze_emoji","x_maze_code","x_maze_by"],

          "sick":   ["x_sick","x_sick_label","x_sick_emoji","x_sick_code","x_sick_by","stench_pen","stench_label","stench_emoji","x_stenchn","stn","stench","sick"],
          "sicken": ["x_sick","x_sick_label","x_sick_emoji","x_sick_code","x_sick_by","stench_pen","stench_label","stench_emoji","x_stenchn","stn","stench","sick"],

          "slow":  ["x_slw"],
          "sg":    ["x_gslwcd","x_gslwcd_label","x_gslwcd_code"],

          "ts": ["x_trueseeing"], "trueseeing": ["x_trueseeing"],

        }

        removed = []

        keys = CLEAR_MAP.get(code)
        if keys:
            for base in keys:
                opt = f"{slot}.{base}"
                if cfg.has_option(chan_id, opt):
                    cfg.remove_option(chan_id, opt); removed.append(base)

        def _clear_x_effect_local(base_key: str):

            try:
                _clear_x_effect(cfg, chan_id, slot, base_key)
            except Exception:

                for suf in ("","_label","_emoji","_code","_by"):
                    opt = f"{slot}.{base_key}{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)

        if not keys:
            if code.startswith("x_"):
                base_key = code

                base_key = base_key.split(".")[-1]
                if cfg.has_option(chan_id, f"{slot}.{base_key}") or cfg.has_option(chan_id, f"{slot}.{base_key}_code"):
                    _clear_x_effect_local(base_key); removed.append(base_key)
            else:

                for opt_key, _ in list(cfg.items(chan_id)):
                    if not opt_key.startswith(f"{slot}.x_"): continue
                    base_key = opt_key.split(".", 1)[1]

                    if base_key in _X_SKIP_GENERIC:
                        continue

                    if any(base_key.endswith(suf) for suf in ("_label","_emoji","_code","_by")):
                        continue
                    code_opt  = cfg.get(chan_id, f"{slot}.{base_key}_code",  fallback="").strip().lower()
                    label_opt = cfg.get(chan_id, f"{slot}.{base_key}_label", fallback="").strip().lower()
                    if code == code_opt or (code and code in label_opt):
                        _clear_x_effect_local(base_key); removed.append(base_key)

            for ak in ("detectmagic","protectionfromevil","readlanguages","readmagic","detectinvisible","fly"):
                for suf in ("_perm","_perm_by"):
                    opt = f"{slot}.x_{ak}{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)

        for b in list(removed):
            if b.startswith("x_control"):
                uses_opt = f"{slot}.{b[2:]}_uses"
                if cfg.has_option(chan_id, uses_opt):
                    cfg.remove_option(chan_id, uses_opt)

        if any(b in {"x_constrict", "x_entangle", "x_swallow", "x_holdbite", "x_leech", "x_tentacles"} for b in removed):
            try:

                holder = cfg.get(chan_id, f"{slot}.heldby", fallback="").strip()
                if holder:
                    s_holder = _slot(holder)
                    if cfg.has_option(chan_id, f"{s_holder}.holds"):
                        cfg.remove_option(chan_id, f"{s_holder}.holds")
                    cfg.remove_option(chan_id, f"{slot}.heldby")

                victim = cfg.get(chan_id, f"{slot}.holds", fallback="").strip()
                if victim:
                    s_victim = _slot(victim)
                    if cfg.has_option(chan_id, f"{s_victim}.heldby"):
                        cfg.remove_option(chan_id, f"{s_victim}.heldby")
                    cfg.remove_option(chan_id, f"{slot}.holds")
            except Exception:
                pass

        did_poly_cleanup = False
        if removed:

            poly_hit = (code in {"ps", "poly", "alter", "alterself"}) or any(
                k in removed for k in ("ps", "ps_form", "ps_by", "ps_src", "poly", "polyself", "polyform")
            )

            if poly_hit:

                try:
                    disp_ci, path_ci = self._resolve_char_ci(key)
                except Exception:
                    disp_ci, path_ci = key, None

                if path_ci:
                    try:
                        pcfg = read_cfg(path_ci)
                        if _poly_active(pcfg):
                            _poly_clear_overlay(pcfg)
                            write_cfg(path_ci, pcfg)
                            did_poly_cleanup = True
                    except Exception:
                        pass

                try:
                    _clear_battle_poly_flags(cfg, chan_id, key)
                except Exception:
                    pass

        did_re_restore = False
        try:

            ray_cleared = (code in {"re","ray","rayexhaustion"}) or any(
                k == "x_rayexhaustion" or k.endswith("x_rayexhaustion") for k in removed
            )
            if ray_cleared:

                try:
                    disp_ci, path_ci = self._resolve_char_ci(key)
                except Exception:
                    disp_ci, path_ci = key, None

                if path_ci:
                    cfg_ci = read_cfg(path_ci)
                    effsec = "effects"
                    changed = False

                    def _restore_one(stat_key: str):
                        nonlocal changed
                        d_opt = f"rayexhaustion_{stat_key}_delta"
                        if cfg_ci.has_option(effsec, d_opt):
                            amt = getint_compat(cfg_ci, effsec, d_opt, fallback=0)
                            if amt:
                                cur = getint_compat(cfg_ci, "stats", stat_key, fallback=None)
                                if cur is not None:
                                    new_val = int(cur) + int(amt)
                                    if not cfg_ci.has_section("stats"): cfg_ci.add_section("stats")
                                    cfg_ci["stats"][stat_key] = str(new_val)

                                    try:
                                        new_mod = _ability_mod(new_val)
                                    except Exception:
                                        new_mod = getint_compat(cfg_ci, "stats", f"{stat_key}_modifier", fallback=0)
                                    cfg_ci["stats"][f"{stat_key}_modifier"] = str(new_mod)
                                    changed = True

                            cfg_ci.remove_option(effsec, d_opt)

                    if cfg_ci.has_section(effsec):
                        _restore_one("str")
                        _restore_one("dex")

                        if not list(cfg_ci.items(effsec)):
                            cfg_ci.remove_section(effsec)

                    if changed:
                        write_cfg(path_ci, cfg_ci)
                        did_re_restore = True
        except Exception:
            pass

        if removed:
            _save_battles(cfg)
            await self._update_tracker_message(ctx, cfg, chan_id)
            pretty = ", ".join(sorted(set(removed)))
            extra_bits = []
            if did_poly_cleanup:
                extra_bits.append("restored original form")
            if did_re_restore:
                extra_bits.append("restored STR/DEX")
            extra_txt = (" and " + " & ".join(extra_bits)) if extra_bits else ""
            await ctx.send(f"🧽 Cleared **{tag.upper()}** from **{key}** ({pretty}){extra_txt}.")
        else:
            await ctx.send(f"ℹ️ No matching status **{tag}** found on **{key}**.")

    @commands.command(name="status")
    async def set_status(self, ctx, who: str, effect: str, amount: str):
        """
        Set or clear a status on a combatant.
        Examples:
          !status wizard fly perm
          !status testy blind 30r
          !status thog paralysis 5t
          !status testman blind 1d8t  # supports dice
          Units: r=rounds, t=turns(60r), m=minutes(6r), h=hours(360r), d=days(8640r) • Also supports dice: XdY[±Z] before the unit.
          Use 'off' or '0' to clear an effect.
        """
        cfg = _load_battles()
        chan_id = _section_id(ctx.channel)
        if not cfg.has_section(chan_id):
            await ctx.send("❌ No initiative running here. Use `!init` first.")
            return

        dm_id = cfg.get(chan_id, "DM", fallback="")
        is_dm = (str(ctx.author.id) == str(dm_id))

        target_key = self._ci_key(cfg, chan_id, who) or who
        disp, path = _resolve_char_ci_local(target_key)
        owner_ok = False
        if is_dm:
            owner_ok = True
        else:
            if path:
                pcfg = read_cfg(path)
                owner_id = (get_compat(pcfg, "info", "owner_id", fallback="") or "").strip()
                cls = (get_compat(pcfg, "info", "class", fallback="") or "").strip().lower()

                if cls == "monster":
                    owner_ok = False
                else:
                    owner_ok = (owner_id and owner_id == str(ctx.author.id))
        if not owner_ok:
            await ctx.send("❌ Only the DM or the character’s owner can change statuses.")
            return

        e = "".join(ch for ch in effect.lower() if ch.isalnum())

        aliases = {
            "fl":"fly", "inv":"invisibility", "dk":"darkness", "dark":"darkness",
            "lt":"light", "bl":"blind", "par":"paralyzed", "paralysis":"paralyzed",
            "dm":"detectmagic", "pf":"protectionfromevil", "pfe":"protectionfromevil",
            "rl":"readlanguages", "rm":"readmagic", "di":"detectinvisible",
            "blinded":"blind",
            "invis":"invisibility",
            "invisible":"invisibility",
            "df":"deaf",
            "deafened":"deaf",
            "deafen":"deaf",
            "spray":"marked",
            "scent":"marked",
            "scented":"marked",
            "pr":"prone",
        }
        e = aliases.get(e, e)

        SIMPLE = {
            "blind": ("blind", "blind_perm"),
            "invisibility": ("inv", "inv_perm"),
            "light": ("light", "light_perm"),
            "darkness": ("darkness", "darkness_perm"),
            "paralyzed": ("paralyzed", None),

        }

        PERMA_TAGS = {
            "fly": "x_fly_perm",
            "detectmagic": "x_detectmagic_perm",
            "protectionfromevil": "x_protectionfromevil_perm",
            "readlanguages": "x_readlanguages_perm",
            "readmagic": "x_readmagic_perm",
            "detectinvisible": "x_detectinvisible_perm",
            "deaf": "x_deaf_perm",
        }

        X_META = {
            "fly": ("x_fly", "FL", "Fly"),
            "detectmagic": ("x_detectmagic", "DM", "Detect Magic"),
            "protectionfromevil": ("x_protectionfromevil", "PF", "Protection from Evil"),
            "readlanguages": ("x_readlanguages", "RL", "Read Languages"),
            "readmagic": ("x_readmagic", "RM", "Read Magic"),
            "detectinvisible": ("x_detectinvisible", "DI", "Detect Invisible"),
            "deaf": ("x_deaf", "DF", "Deaf"),
            "prone": ("x_prone", "PR", "Prone"),
            "marked": ("x_marked", "MS", "Monster-Scented"),
        }

        def parse_amount(s: str):
            s = s.strip().lower()
            if s in {"perm","permanent","perma","∞","inf"}:
                return "perm"
            if s in {"off","none","clear","0"}:
                return "off"
            m = re.fullmatch(r"(\d+d\d+(?:[+-]\d+)?)\s*([rtmhd]?)", s)
            if m:
                dice = m.group(1)
                unit = (m.group(2) or "r")
                try:
                    total, rolls, flat = roll_dice(dice)
                    base = total + flat
                except Exception:
                    base = int(dice.split("d")[0])
                factor = {"r":1, "t":60, "m":6, "h":360, "d":8640}[unit]
                return max(0, int(base)) * factor
            m = re.fullmatch(r"(\d+)\s*([rtmhd]?)", s)
            if m:
                n = int(m.group(1))
                u = (m.group(2) or "r")
                factor = {"r":1, "t":60, "m":6, "h":360, "d":8640}[u]
                return n * factor
            return None

        amt = parse_amount(amount)
        if amt is None:
            await ctx.send("❌ Bad duration. Use `perm`, `off`, or `<N|XdY[±Z]><r|t|m|h|d>` (e.g., `30r`, `5t`, `10m`, `1d8t`).")
            return

        slot = _choose_slot_for_effects(cfg, chan_id, target_key)

        changed = False
        notes = []

        def seti(opt, val):
            nonlocal changed
            cfg.set(chan_id, opt, str(int(val)))
            changed = True

        def cleari(opt):
            nonlocal changed
            if cfg.has_option(chan_id, opt):
                cfg.remove_option(chan_id, opt)
                changed = True

        def clear_x_bundle(base_key: str):
            cleari(f"{slot}.{base_key}")
            for suf in ("_label","_emoji","_code","_by"):
                cleari(f"{slot}.{base_key}{suf}")

        if amt == "off":

            if e in SIMPLE:
                tkey, pkey = SIMPLE[e]
                cleari(f"{slot}.{tkey}")
                if pkey: cleari(f"{slot}.{pkey}")
            if e in PERMA_TAGS:
                cleari(f"{slot}.{PERMA_TAGS[e]}")

            if e in X_META:
                base, _, _ = X_META[e]
                clear_x_bundle(base)
            _save_battles(cfg)
            await self._update_tracker_message(ctx, cfg, chan_id)
            await ctx.send(f"🧹 Cleared **{e}** on **{target_key}**.")
            return

        if amt == "perm":

            if e in PERMA_TAGS:
                seti(f"{slot}.{PERMA_TAGS[e]}", 1)

                if e in X_META:
                    base, _, _ = X_META[e]
                    clear_x_bundle(base)
                notes.append(f"{e} → **permanent**")
            elif e in SIMPLE and SIMPLE[e][1]:

                _t, p = SIMPLE[e]
                seti(f"{slot}.{p}", 1)

                cleari(f"{slot}.{_t}")
                notes.append(f"{e} → **permanent**")
            else:
                await ctx.send(f"⚠️ `{e}` doesn’t support a permanent flag here.")
                return
        else:

            rounds = int(amt)
            if e in SIMPLE:
                tkey, pkey = SIMPLE[e]

                if pkey: cleari(f"{slot}.{pkey}")
                seti(f"{slot}.{tkey}", rounds)
                notes.append(f"{e} → **{rounds} rds**")
            elif e in X_META:
                base, code, label = X_META[e]
                seti(f"{slot}.{base}", rounds)
                cfg.set(chan_id, f"{slot}.{base}_code", code)
                cfg.set(chan_id, f"{slot}.{base}_label", label)

                changed = True
                notes.append(f"{e} → **{rounds} rds**")
            else:

                base = f"x_{e}"
                seti(f"{slot}.{base}", rounds)

                cfg.set(chan_id, f"{slot}.{base}_code", e[:2].upper() or "FX")
                cfg.set(chan_id, f"{slot}.{base}_label", e.title())
                changed = True
                notes.append(f"{e} → **{rounds} rds**")

        if changed:
            _save_battles(cfg)
            await self._update_tracker_message(ctx, cfg, chan_id)

        who_disp = cfg.get(chan_id, f"{slot}.disp", fallback=target_key)
        await ctx.send(f"✅ Set status for **{who_disp}**: " + "; ".join(notes))

    def _maze_is_active(self, cfg, chan_id: str, name_key: str) -> tuple[bool, str, int]:
        """Returns (active, slot, until_round)."""
        try:
            slot = _slot(name_key)
        except Exception:
            slot = name_key.replace(" ", "_")
        cur_rd = cfg.getint(chan_id, "round", fallback=0)
        until = cfg.getint(chan_id, f"{slot}.maze_until", fallback=0)
        return (until > 0 and cur_rd < until, slot, until)

    def _clear_maze(self, cfg, chan_id: str, slot: str):

        for key in (f"{slot}.maze_until", f"{slot}.maze_by"):
            if cfg.has_option(chan_id, key):
                cfg.remove_option(chan_id, key)

        try:
            if '_clear_x_effect' in globals():
                _clear_x_effect(cfg, chan_id, slot, "x_maze")
            else:

                for suf in ("", "_code", "_label", "_emoji", "_by"):
                    opt = f"{slot}.x_maze{suf}"
                    if cfg.has_option(chan_id, opt):
                        cfg.remove_option(chan_id, opt)
        except Exception:

            for suf in ("", "_code", "_label", "_emoji", "_by"):
                opt = f"{slot}.x_maze{suf}"
                if cfg.has_option(chan_id, opt):
                    cfg.remove_option(chan_id, opt)

        _save_battles(cfg)

    def _is_minotaur(self, tgt_cfg) -> bool:

        for sec in ("info", "base", "monster", "stats"):
            try:
                v = (tgt_cfg.get(sec, "type", fallback="") or "").strip().lower()
                if v == "minotaur":
                    return True
            except Exception:
                pass
        return False

    def _tick_maze_on_turn(self, cfg, chan_id: str, name_key: str) -> tuple[bool, str | None]:
        """
        Start-of-turn Maze logic for 'name_key'.
        Returns (still_in_maze, note_for_channel_or_None).
        If they escape or duration expires, returns False.
        """
        active, slot, until = self._maze_is_active(cfg, chan_id, name_key)
        cur_rd = cfg.getint(chan_id, "round", fallback=0)

        if not active and until > 0 and cur_rd >= until:
            self._clear_maze(cfg, chan_id, slot)
            return (False, f"🌀 **{name_key}** returns as **Maze** ends.")

        if not active:
            return (False, None)

        disp, path = _resolve_char_ci_local(name_key)
        if not path:

            return (True, None)

        def_cfg = read_cfg(path)
        ok, roll, dc, _pen = self._roll_save(def_cfg, vs="spl", penalty=0)

        if ok:
            self._clear_maze(cfg, chan_id, slot)
            return (False, f"🌀 **Maze**: {name_key} rolls **{roll}** vs **{dc}** → **ESCAPES** and reappears.")
        else:
            return (True, f"🌀 **Maze**: {name_key} rolls **{roll}** vs **{dc}** → **still lost** (turn skipped).")

    def _roll_save(self, t_cfg, vs: str = "para", penalty: int = 0) -> tuple[bool, int, int, int]:
        """
        Return (success, d20, target, penalty_applied).
        'penalty' lowers the roll (i.e., makes the save harder).
        """
        target = self._get_save_target(t_cfg, vs)
        d20 = random.randint(1, 20)
        effective = d20 - (penalty or 0)
        success = (effective >= target)
        return success, d20, target, (penalty or 0)

    def _get_save_target(self, t_cfg, vs: str = "para") -> int:
        """
        1) Check the target file's explicit saves in [stats]/[saves] for keys like:
           sv_para, save_para, para, paralysis (and the other types).
        2) If not present, honor 'saveas = <Class> <Level>' (e.g., Cleric 1, Magic-User 1).
        3) Otherwise, fall back to Fighter 1 style defaults.
        """
        vs = _canon_vs(vs)

        keys = [f"sv_{vs}", f"save_{vs}", vs]
        if vs == "para":
            keys += ["paralyze", "paralysis"]
        for sec in ("stats", "saves"):
            for k in keys:
                try:
                    raw = get_compat(t_cfg, sec, k, fallback=None)
                except Exception:
                    raw = None
                if raw is None:
                    continue
                s = str(raw).strip()
                if not s:
                    continue
                try:
                    return int(s)
                except Exception:
                    pass

        saveas = _get_saveas_from_cfg(t_cfg)
        if saveas:
            cls, lvl = _parse_saveas(saveas)
            return _class_save_target(cls, lvl, vs)

        return _DEFAULT_F1.get(vs, 14)

    async def _apply_turn_strength_recovery(self, ctx, bcfg, chan_id: str):
        """
        For everyone listed, recover 1 point of STR if they have temp loss.
        Also lift 'collapsed' if they reach STR 3+.
        Robust: falls back to manual decrement if _recover_one_point_of_str or
        your resolver are missing.
        """

        names, _ = _parse_combatants(bcfg, chan_id)
        if not names:
            return

        lines = []
        changed_any = False

        for nm in names:

            disp = nm
            path = None
            try:
                disp, path = _resolve_char_ci_local(nm)
            except Exception:
                try:
                    disp, path = _resolve_char_ci(nm)
                except Exception:
                    path = None
            if not path or not os.path.exists(path):
                continue

            pcfg = read_cfg(path)

            prev_temp = getint_compat(pcfg, "cur", "str_loss_temp", fallback=0)

            used_helper = False
            new_eff = getint_compat(pcfg, "stats", "str", fallback=None)
            new_temp = prev_temp
            stood = False

            try:
                new_eff, new_temp, stood = self._recover_one_point_of_str(pcfg)
                used_helper = True
                write_cfg(path, pcfg)
            except Exception:

                prev_eff = getint_compat(pcfg, "stats", "str", fallback=None)
                base_str = getint_compat(pcfg, "base",  "str", fallback=prev_eff if prev_eff is not None else 10)
                perm_loss = (
                    getint_compat(pcfg, "cur", "str_loss_perm", fallback=
                    getint_compat(pcfg, "cur", "str_perm_loss", fallback=0))
                )
                if prev_temp > 0:
                    if not pcfg.has_section("cur"):
                        pcfg.add_section("cur")
                    new_temp = prev_temp - 1
                    pcfg.set("cur", "str_loss_temp", str(new_temp))
                else:
                    new_temp = prev_temp

                new_eff = max(1, base_str - int(perm_loss) - int(new_temp))
                if not pcfg.has_section("stats"):
                    pcfg.add_section("stats")
                pcfg.set("stats", "str", str(new_eff))

                stood = (prev_eff is not None and prev_eff < 3 and new_eff >= 3)

                write_cfg(path, pcfg)

            recovered_one = (new_temp < prev_temp)

            if recovered_one or stood:
                label = disp or nm
                msg = f"• {label}: STR +1 (now {new_eff})" if recovered_one else f"• {label}: STR unchanged (now {new_eff})"

                if stood:
                    try:
                        try:
                            s = _slot(nm)
                        except Exception:
                            s = nm.replace(" ", "_")
                        opt = f"{s}.str_collapsed"
                        if bcfg.has_option(chan_id, opt):
                            bcfg.remove_option(chan_id, opt)
                            changed_any = True
                        msg += " — no longer **Collapsed**."
                    except Exception:
                        pass

                if recovered_one and not used_helper:
                    msg += " *(manual STR temp –1)*"

                lines.append(msg)

        if changed_any:
            _save_battles(bcfg)

        if lines:
            await ctx.send("💪 **Strength recovers (1 per turn):**\n" + "\n".join(lines))

    def _poison_immune(self, t_cfg) -> bool:
        """True if target has 'immune: poison' in [stats] or [base]."""
        import re
        def _split(v):
            return {t for t in re.split(r"[,\s]+", str(v or "").lower().strip()) if t}
        im_stats = _split(get_compat(t_cfg, "stats", "immune",  fallback=""))
        im_base  = _split(get_compat(t_cfg, "base",  "immune",  fallback=""))
        return "poison" in (im_stats | im_base)

def setup(bot):
    bot.add_cog(Initiative(bot))
