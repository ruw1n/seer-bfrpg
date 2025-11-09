# strongholds.py
import math, os, re, json, random, configparser
from pathlib import Path
from typing import Literal, Optional
import nextcord
from nextcord.ext import commands
from utils.players import get_active, list_chars  



MATERIALS = {
    "wood":      {1: 10},                   
    "brick":     {1: 20, 5: 50},
    "softstone": {1: 30, 5: 70, 10: 200},
    "hardstone": {1: 40, 5: 90, 10: 260, 15: 350},
}
HARDNESS = {"wood": 6, "brick": 8, "softstone": 12, "hardstone": 16}
MAX_HEIGHT = {1: 40, 5: 60, 10: 80, 15: 100}  
ROOF_MULT = {"thatch": 1, "wood": 2, "slate": 4} 
TYPE_MULT = {"castle": 1.0, "tower": 3.0, "temple": 1.0, "guildhouse": 2.0}


FOLLOWER_DICE = {
    "fighter": "3d6", "barbarian": "3d6", "paladin": "3d6", "ranger": "3d6",
    "magic-user": "1d8", "spellcrafter": "1d8", "illusionist": "1d8", "necromancer": "1d8",
    "fightermage": "1d8", "magethief": "1d8",
    "cleric": "2d8", "druid": "2d8",
    "thief": "2d6", "scout": "2d6", "assassin": "2d6",
}

DATA_FILE = Path("data/strongholds.json")



def roll(expr: str) -> int:
    m = re.fullmatch(r"(\d+)d(\d+)", expr)
    if not m: return 0
    n, s = int(m.group(1)), int(m.group(2))
    return sum(random.randint(1, s) for _ in range(n))

def _coe_path(name: str) -> Optional[Path]:
    stems = {name, name.replace(" ", "_"), name.lower().replace(" ", "_")}
    bases = [Path.cwd(), Path(__file__).resolve().parent, *Path(__file__).resolve().parents[:2]]
    for b in bases:
        for s in stems:
            p = (b / f"{s}.coe")
            if p.exists():
                return p
    return None

def read_coe(name: str) -> configparser.ConfigParser | None:
    path = _coe_path(name)
    if not path: return None
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    return cp

def get_class_level_from_coe(name: str) -> tuple[str, int]:
    cp = read_coe(name)
    if not cp: return "", 0
    cls = (cp.get("info", "class", fallback="") or "").strip()
    lvl = cp.getint("cur", "level", fallback=0)
    return cls, lvl

def class_family_dice(cls: str) -> str:
    k = (cls or "").strip().lower().replace("–", "-").replace("_", "-")
    for key in FOLLOWER_DICE:
        if key in k:
            return FOLLOWER_DICE[key]
    return ""

def _norm_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())

def _lev(a: str, b: str) -> int:
    if a == b: return 0
    la, lb = len(a), len(b)
    if la == 0 or lb == 0: return max(la, lb)
    dp = list(range(lb + 1))
    for i in range(1, la + 1):
        prev = dp[0]; dp[0] = i; ca = a[i-1]
        for j in range(1, lb + 1):
            temp = dp[j]; cost = 0 if ca == b[j-1] else 1
            dp[j] = min(dp[j] + 1, dp[j-1] + 1, prev + cost); prev = temp
    return dp[lb]

def _resolve_partial(items: list[str], token: str) -> tuple[Optional[str], list[str]]:
    tok = (token or "").strip()
    if not tok: return None, []
    tl = tok.lower(); nt = _norm_token(tok)

    for n in items:
        if n.lower() == tl or _norm_token(n) == nt: return n, []

    pre = [n for n in items if n.lower().startswith(tl) or _norm_token(n).startswith(nt)]
    if len(pre) == 1: return pre[0], []
    if len(pre) > 1:  return None, pre[:8]

    subs = [n for n in items if tl in n.lower() or (nt and nt in _norm_token(n))]
    if len(subs) == 1: return subs[0], []
    if len(subs) > 1:  return None, subs[:8]

    scored = []
    for n in items:
        ln = n.lower(); nn = _norm_token(n)
        s1 = _lev(tl, ln[:len(tl)]) if ln else 99
        s2 = _lev(nt, nn[:len(nt)]) if nt else 99
        s3 = _lev(tl, ln)
        scored.append((min(s1, s2, s3), n))
    if not scored: return None, []
    scored.sort(key=lambda x: (x[0], len(x[1])))
    best = scored[0][0]; thresh = 1 if len(tl) <= 4 else 2
    if best <= thresh:
        ties = [n for d, n in scored if d == best]
        return (ties[0], []) if len(ties) == 1 else (None, ties[:8])
    return None, []



def sections_rect(side_ft: int, height_ft: int, thickness_ft: int, entrances_sections: int = 0) -> int:
    courses = max(1, height_ft // 10)
    perim = 4 * side_ft
    base = (perim // 10) * courses
    if thickness_ft >= 5:
        per_course = 2 * (thickness_ft // 5)  
        base -= per_course * courses
    base = max(0, base - int(entrances_sections or 0))
    return base

def sections_round(diam_ft: int, height_ft: int, thickness_ft: int) -> int:
    courses = max(1, height_ft // 10)
    perim = 3 * diam_ft 
    base = (perim // 10) * courses
    return base

def floor_squares_rect(inner_side_ft: int) -> int:
    return max(0, (inner_side_ft // 10) * (inner_side_ft // 10))

def floor_squares_round(inner_diam_ft: int) -> int:
    return max(0, int(round(3 * (inner_diam_ft ** 2) / 400)))



def wall_cost(material: str, thickness_ft: int) -> int:
    material = material.lower()
    if material not in MATERIALS or thickness_ft not in MATERIALS[material]:
        raise ValueError("Unsupported material/thickness")
    return MATERIALS[material][thickness_ft]

def roof_cost_for_area_10sq(roof_kind: Literal["thatch","wood","slate"], area_10sq: int) -> int:
    base = MATERIALS["wood"][1]  
    mult = ROOF_MULT[roof_kind]
    return int(area_10sq * base * mult)

def apply_height_engineering(cost_gp: int, portion_height_ft: int) -> int:
    steps = max(0, portion_height_ft // 10)
    return int(round(cost_gp * (1 + 0.10 * steps)))

def worker_days_for_cost(total_cost_gp: int) -> int:
    return int(total_cost_gp)

def build_time_days(worker_days: int, workers: int) -> int:
    if workers <= 0: workers = 1
    ideal = math.ceil(worker_days / workers)
    floor_cap = math.ceil(math.sqrt(max(1, worker_days)))
    return max(ideal, floor_cap)

def cargo_tons_for_construction_cost(wood_or_stone_cost_gp: int) -> float:
    return wood_or_stone_cost_gp / 5.0



def _load_state() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state(state: dict):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)

def _user_bucket(state: dict, guild_id: str, user_id: str) -> dict:
    state.setdefault(guild_id, {})
    state[guild_id].setdefault(user_id, {"current": "", "plans": {}})
    return state[guild_id][user_id]

def _plan_names(state: dict, guild_id: str, user_id: str) -> list[str]:
    b = _user_bucket(state, guild_id, user_id)
    return list(b.get("plans", {}).keys())

def _get_plan(state: dict, guild_id: str, user_id: str, name: str) -> Optional[dict]:
    b = _user_bucket(state, guild_id, user_id)
    return b["plans"].get(name)

def _set_plan(state: dict, guild_id: str, user_id: str, name: str, plan: dict):
    b = _user_bucket(state, guild_id, user_id)
    b["plans"][name] = plan
    if not b["current"]:
        b["current"] = name

def _select_plan(state: dict, guild_id: str, user_id: str, name: str) -> bool:
    b = _user_bucket(state, guild_id, user_id)
    if name in b["plans"]:
        b["current"] = name
        return True
    return False

def _current_plan(state: dict, guild_id: str, user_id: str) -> tuple[Optional[str], Optional[dict]]:
    b = _user_bucket(state, guild_id, user_id)
    cur = b.get("current") or ""
    if cur and cur in b["plans"]:
        return cur, b["plans"][cur]
    return None, None



def _calc_plan(plan: dict):
    """
    Plan schema:
      {
        "name": str,
        "type": "castle|tower|temple|guildhouse",
        "remote": float,
        "roof": "thatch|wood|slate|none",
        "parapet": bool,
        "floors": [ { "shape":"rect","side":50,"height":20,"thick":10,"mat":"hardstone","entrances":2 },
                    { "shape":"round","diam":40,"height":10,"thick":5,"mat":"hardstone","entrances":0 }, ... ],
        "windows": { "1": 4, "2": 6, ... },  # cosmetic
        "income": { "type": "taxes|tuition|tithe|business", "gpw": 0 },
        "upkeep_gpw": 0,
        "engines": { "Ballista A": {"ac": 20}, ... }
      }
    """
    type_mult = TYPE_MULT.get(plan.get("type","castle"), 1.0)
    remote_mult = float(plan.get("remote", 1.0))
    floors = list(plan.get("floors", []))
    parapet = bool(plan.get("parapet", False))
    roof_kind = plan.get("roof", None)
    notes = []
    total_base = 0
    total_eng  = 0
    total_floor_area_10sq = 0

    if not floors:
        return {
            "base_cost_gp": 0, "eng_cost_gp": 0, "final_cost_gp": 0,
            "floor_area_10sq": 0, "roof_area_10sq": 0, "roof_cost_gp": 0,
            "notes": ["No floors added."]
        }

    top_shape = floors[-1].get("shape","rect")
    top_thick = int(floors[-1].get("thick", 1))
    top_mat   = floors[-1].get("mat","hardstone").lower()

    for i, fl in enumerate(floors, start=1):
        shp   = fl.get("shape","rect")
        h     = int(fl.get("height", 10))
        thick = int(fl.get("thick", 1))
        mat   = str(fl.get("mat","hardstone")).lower()
        ent   = int(fl.get("entrances", 0))
        if thick not in (1,5,10,15): raise ValueError("thickness must be 1/5/10/15")
        if mat not in MATERIALS: raise ValueError(f"bad material: {mat}")
        if h > MAX_HEIGHT[thick]:
            notes.append(f"⚠️ Floor {i}: {thick}’ wall over max height ({h}>{MAX_HEIGHT[thick]}).")

        if shp == "rect":
            side = int(fl.get("side", 30))
            wall_secs = sections_rect(side, h, thick, ent)
            gp_per_sec = wall_cost(mat, thick)
            cost_walls = wall_secs * gp_per_sec
            inner = max(0, side - 2*thick)
            floor_sq = floor_squares_rect(inner)
        elif shp == "round":
            diam = int(fl.get("diam", 30))
            wall_secs = sections_round(diam, h, thick)
            gp_per_sec = wall_cost(mat, thick)
            cost_walls = wall_secs * gp_per_sec
            inner = max(0, diam - 2*thick)
            floor_sq = floor_squares_round(inner)
        else:
            raise ValueError("shape must be rect|round")

        cost_floor = floor_sq * MATERIALS["wood"][1]
        portion_cost = cost_walls + cost_floor
        portion_eng  = apply_height_engineering(portion_cost, h)

        total_base += portion_cost
        total_eng  += portion_eng
        total_floor_area_10sq += floor_sq


    roof_area_10sq = 0
    roof_cost = 0
    if top_shape == "rect":
        top_side = int(floors[-1].get("side", 30))
        top_inner = max(0, top_side - 2*top_thick)
        roof_area_10sq = floor_squares_rect(top_inner) if roof_kind else 0
        if parapet:
            top_h = int(floors[-1].get("height", 10))
            perim_sections = (4 * top_side) // 10  #
            para_cost_per_sec = wall_cost(top_mat, 1) * 0.5
            para_cost = int(round(perim_sections * para_cost_per_sec))
            total_base += para_cost
            total_eng  += apply_height_engineering(para_cost, max(0, top_h // 2))  
    else:  
        top_diam = int(floors[-1].get("diam", 30))
        top_inner = max(0, top_diam - 2*top_thick)
        roof_area_10sq = floor_squares_round(top_inner) if roof_kind else 0
        if parapet:
            perim_sections = (3 * top_diam) // 10
            para_cost_per_sec = wall_cost(top_mat, 1) * 0.5
            para_cost = int(round(perim_sections * para_cost_per_sec))
            top_h = int(floors[-1].get("height", 10))
            total_base += para_cost
            total_eng  += apply_height_engineering(para_cost, max(0, top_h // 2))

    if roof_kind:
        roof_kind = roof_kind.lower()
        if roof_kind not in ROOF_MULT:
            raise ValueError("roof must be thatch|wood|slate|none")
        cost_r = roof_cost_for_area_10sq(roof_kind, roof_area_10sq)
        top_h = int(floors[-1].get("height", 10))
        roof_cost = cost_r
        total_base += cost_r
        total_eng  += apply_height_engineering(cost_r, top_h)

    final_cost = int(round(total_eng * type_mult * remote_mult))
    return {
        "base_cost_gp": total_base,
        "eng_cost_gp": total_eng,
        "final_cost_gp": final_cost,
        "floor_area_10sq": total_floor_area_10sq,
        "roof_area_10sq": roof_area_10sq,
        "roof_cost_gp": roof_cost,
        "notes": notes,
    }



def parse_keyvals(s: str) -> dict:
    out = {}
    toks = re.findall(r'(\w+)\s*=\s*("[^"]+"|\S+)', s)
    for k, v in toks:
        v = v.strip().strip('"')
        if "," in v:
            parts = [p.strip() for p in v.split(",")]
            out[k.lower()] = parts
        else:
            out[k.lower()] = v
    return out



class Strongholds(commands.Cog):
    def __init__(self, bot):
        self.bot = bot



    @commands.command(name="followers")
    async def followers(self, ctx, *, char_name: Optional[str] = None):
        """
        Roll followers for a PC who builds a class-appropriate stronghold.
        Usage:
          !followers               -> uses your active PC
          !followers <PC Name>     -> explicitly name a PC (partial/fuzzy OK)
        """
        name = None
        if char_name:

            reg = list_chars(ctx.author.id)
            if reg:
                found, sugg = _resolve_partial(reg, char_name)
                if found: name = found
                elif sugg:
                    await ctx.send("⚠️ Ambiguous PC — did you mean: " + ", ".join(f"`{s}`" for s in sugg) + " ?")
                    return
                else:

                    name = char_name
            else:
                name = char_name
        else:
            name = get_active(ctx.author.id) or ""

        if not name:
            await ctx.send("❌ No active character. Use `!char <name>` or pass a name: `!followers <PC>`.")
            return

        cls, lvl = get_class_level_from_coe(name)
        if not cls or lvl < 9:
            await ctx.send(f"❌ **{name}** must be level 9+ and have a readable `.coe` (class/level) to attract followers.")
            return

        dice = class_family_dice(cls)
        if not dice:
            await ctx.send(f"⚠️ Class **{cls}** not mapped to follower rules; specify manually.")
            return

        num = roll(dice)
        await ctx.send(
            f"🏰 **{name}** ({cls} {lvl}) stronghold followers: **{num}** (rolled {dice}).\n"
            "Followers are 1st-level of the same class and generally don’t leave the stronghold."
        )



    @commands.command(name="shcalc")
    async def shcalc(self, ctx, *, args: str = ""):
        """
        One-shot stronghold calculator for square/rect keeps (book math).

        Usage:
          • Single floor (explicit):
              !shcalc side=50 height=20 thick=10 mat=hardstone roof=slate type=castle remote=1.0 workers=120
          • Multi-floor (CSV lists, all lists the SAME length):
              !shcalc side=50,50,40,40 height=20,10,10,10 thick=10,10,5,5 mat=hardstone
          • Optional gate deduction per floor (10' sections removed):
              !shcalc side=50,50 height=20,10 thick=10,10 entrances=2,0
          • Positional shortcut (single floor):  !shcalc 50 20 10   ← side height thick

        Notes:
          • thick ∈ {1,5,10,15}
          • roof ∈ {thatch,wood,slate,none}
          • type ∈ {castle,tower,temple,guildhouse}
        """
        if not args.strip():
            await ctx.send("Usage: `!shcalc side=50 height=20 thick=10 [...]` • CSV lists allowed • or positional: `!shcalc 50 20 10` (side height thick)")
            return

        kv = parse_keyvals(args)

        if not kv:
            nums = re.findall(r"-?\d+", args)
            if len(nums) == 3:
                kv = {"side": nums[0], "height": nums[1], "thick": nums[2]}
            else:
                await ctx.send("❌ Provide `side=, height=, thick=` (or `!shcalc <side> <height> <thick>`).")
                return

        def as_int_list(key, required=True):
            v = kv.get(key)
            if v is None:
                if required: raise ValueError(f"Missing {key}")
                return []
            if isinstance(v, list): return [int(x) for x in v]
            return [int(v)]

        try:
            sides   = as_int_list("side")
            heights = as_int_list("height")
            thick   = as_int_list("thick")
        except Exception as e:
            await ctx.send(f"❌ {e}")
            return

        mats    = kv.get("mat")
        entrances = kv.get("entrances", None)
        roof = kv.get("roof", "slate").lower()
        if roof == "none": roof = None
        typ = kv.get("type", "castle").lower()
        remote = float(kv.get("remote", 1.0))
        workers = int(kv.get("workers", 100))

        if not isinstance(mats, list): mats = [mats] if mats else []
        if len(mats) == 1 and len(sides) > 1:
            mats = mats * len(sides)
        if entrances is not None and not isinstance(entrances, list):
            entrances = [entrances]
        if isinstance(entrances, list):
            entrances = [int(x) for x in entrances]
            if len(entrances) == 1 and len(sides) > 1:
                entrances = entrances * len(sides)

        n = len(sides)
        if not (len(heights)==len(thick)==n and (not mats or len(mats)==n)):
            await ctx.send("❌ side/height/thick/mat lists must be same length.")
            return
        if roof and roof not in ROOF_MULT:
            await ctx.send("❌ roof must be thatch|wood|slate|none.")
            return
        if typ not in TYPE_MULT:
            await ctx.send("❌ type must be castle|tower|temple|guildhouse.")
            return
        if any(t not in (1,5,10,15) for t in thick):
            await ctx.send("❌ thick must be one of 1, 5, 10, 15.")
            return

        floors = []
        for i in range(n):
            floors.append({
                "shape":"rect","side":int(sides[i]),"height":int(heights[i]),
                "thick":int(thick[i]),"mat":str((mats[i] if mats else "hardstone")).lower(),
                "entrances": int(entrances[i] if entrances else 0)
            })
        plan = {
            "name":"_temp","type":typ,"remote":remote,"roof":roof or None,
            "parapet": False, "floors": floors
        }
        try:
            result = _calc_plan(plan)
        except Exception as e:
            await ctx.send(f"❌ Calc error: {type(e).__name__}: {e}")
            return

        wood_or_stone_base = result["base_cost_gp"]
        worker_days = worker_days_for_cost(result["final_cost_gp"])
        time_days = build_time_days(worker_days, workers)
        tons = cargo_tons_for_construction_cost(wood_or_stone_base)

        max_h = max(heights) if heights else 0
        warn = []
        if max_h > 40: warn.append("Needs **solid foundation** (>40’).")
        if max_h > 60: warn.append("Must rest on **bedrock** (>60’).")
        if typ == "guildhouse":
            warn.append("Guildhouses in cities: **exterior 1’ walls** but **×2 cost** for traps/passages.")
        if typ == "tower":
            warn.append("Magic-User towers **×3 cost** for research fittings.")

        embed = nextcord.Embed(title="🏰 Stronghold Cost Estimate", color=0x8c7b5a)
        embed.add_field(
            name="Inputs",
            value=(
                f"Floors: **{n}**  •  Type: **{typ}**  •  Remote: **×{remote}**\n"
                f"Sides: `{sides}` ft\nHeights: `{heights}` ft\nThickness: `{thick}` ft\n"
                f"Materials: `{mats}`\nEntrances(-10' sections): `{entrances or [0]*n}`\n"
                f"Roof: **{roof or 'none'}** (base squares: {result['roof_area_10sq']})"
            ),
            inline=False
        )
        embed.add_field(
            name="Costs",
            value=(
                f"Base materials (pre-engineering): **{result['base_cost_gp']:,} gp**\n"
                f"+ Engineering per 10′ height portions → **{result['eng_cost_gp']:,} gp**\n"
                f"Type/Remote multipliers → **{result['final_cost_gp']:,} gp**"
            ),
            inline=False
        )
        embed.add_field(
            name="Time & Logistics",
            value=(
                f"Worker-days: **{worker_days:,}** (1 per gp)\n"
                f"Workers: **{workers}** → Time: **{time_days} days** (~{round(time_days/140,2)} yrs @ 140 d/yr)\n"
                f"Cargo (materials only): **{tons:.1f} tons** (1 ton / 5 gp of wood/stone)"
            ),
            inline=False
        )
        if result["notes"] or warn:
            embed.add_field(name="Notes", value="\n".join([*warn, *result["notes"]]) or "—", inline=False)
        await ctx.send(embed=embed)


    @commands.command(name="shdig")
    async def shdig(self, ctx, material: Literal["earth","softstone","hardstone"], cubes_5ft: int, workers: int = 50):
        """
        Dungeon excavation time. Usage: !shdig <earth|softstone|hardstone> <5ft_cubes> [workers]
        """
        per_cube_days = {"earth": 5, "softstone": 10, "hardstone": 20}[material]
        days_one_worker = per_cube_days * cubes_5ft
        time_days = build_time_days(days_one_worker, workers)
        await ctx.send(
            f"⛏️ Digging **{cubes_5ft}** cubes of **{material}** with **{workers}** workers:\n"
            f"Worker-days: **{days_one_worker:,}** → Time: **{time_days} days** "
            f"(~{round(time_days/140,2)} yrs @ 140 d/yr)\n"
            f"{'Earth needs supports; double time for unskilled miners.' if material=='earth' else 'Double time for unskilled miners.'}"
        )


    @commands.group(name="sh", invoke_without_command=True)
    async def sh(self, ctx):
        state = _load_state()
        names = _plan_names(state, str(ctx.guild.id), str(ctx.author.id)) if ctx.guild else []
        cur, _ = _current_plan(state, str(ctx.guild.id), str(ctx.author.id)) if ctx.guild else (None, None)
        if not names:
            await ctx.send("🏗️ Stronghold planner.\nStart with `!sh new <name> [type=castle|tower|temple|guildhouse] [remote=1.0] [roof=slate|wood|thatch|none]`. `!sh guide` for more info.")
            return
        await ctx.send("🏗️ Your plans: " + ", ".join(f"**{n}**" + (" ✅" if n==cur else "") for n in names))

    @sh.command(name="new")
    async def sh_new(self, ctx, *, args: str):
        """
        Create a new plan. Example:
          !sh new MyKeep type=castle remote=1.0 roof=slate
        """
        kv = parse_keyvals(args)

        m = re.match(r'\s*"([^"]+)"\s*(.*)$', args.strip())
        if m:
            name = m.group(1)
            rest = m.group(2)
            kv.update(parse_keyvals(rest))
        else:
            pieces = [p for p in re.split(r"\s+", args.strip()) if p]
            name = ""
            for p in pieces:
                if "=" not in p:
                    name = p; break
            if not name:
                await ctx.send("❌ Provide a plan name. Example: `!sh new MyKeep type=castle`")
                return
        typ = (kv.get("type") or "castle").lower()
        remote = float(kv.get("remote", 1.0))
        roof = kv.get("roof", "slate").lower()
        if roof == "none": roof = None
        if typ not in TYPE_MULT:
            await ctx.send("❌ type must be castle|tower|temple|guildhouse.")
            return
        state = _load_state()
        b = _user_bucket(state, str(ctx.guild.id), str(ctx.author.id))
        if name in b["plans"]:
            await ctx.send("❌ A plan with that name already exists. Use `!sh select \"Name\"` or choose another name.")
            return
        plan = {
            "name": name, "type": typ, "remote": remote, "roof": roof,
            "parapet": False, "floors": [], "windows": {}, "income": {"type":"","gpw":0},
            "upkeep_gpw": 0, "engines": {}
        }
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), name, plan)
        _select_plan(state, str(ctx.guild.id), str(ctx.author.id), name)
        _save_state(state)
        await ctx.send(f"✅ Created and selected plan **{name}** (type **{typ}**, remote ×{remote}, roof **{roof or 'none'}**).")

    @sh.command(name="select")
    async def sh_select(self, ctx, *, name: str):
        state = _load_state()
        names = _plan_names(state, str(ctx.guild.id), str(ctx.author.id))
        found, sugg = _resolve_partial(names, name)
        if not found:
            if sugg:
                await ctx.send("⚠️ Ambiguous — did you mean: " + ", ".join(f"`{s}`" for s in sugg) + " ?")
            else:
                await ctx.send("❌ No such plan.")
            return
        _select_plan(state, str(ctx.guild.id), str(ctx.author.id), found)
        _save_state(state)
        await ctx.send(f"✅ Selected plan **{found}**.")

    @sh.command(name="add-rect")
    async def sh_add_rect(self, ctx, *, args: str):
        """
        Add a rectangular floor.
        Example: !sh add-rect side=50 height=20 thick=10 mat=hardstone entrances=2 repeat=2
        """
        kv = parse_keyvals(args)
        try:
            side = int(kv["side"]); height = int(kv["height"]); thick = int(kv["thick"])
            mat = kv.get("mat","hardstone").lower()
            ent = int(kv.get("entrances", 0))
            rep = int(kv.get("repeat", 1))
        except Exception:
            await ctx.send("❌ Required: side=, height=, thick= (1|5|10|15). Optional: mat=, entrances=, repeat=")
            return
        if mat not in MATERIALS: await ctx.send("❌ mat must be wood|brick|softstone|hardstone."); return
        if thick not in (1,5,10,15): await ctx.send("❌ thick must be 1|5|10|15."); return
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan: await ctx.send("❌ No current plan. Use `!sh new ...`"); return
        for _ in range(max(1,rep)):
            plan["floors"].append({"shape":"rect","side":side,"height":height,"thick":thick,"mat":mat,"entrances":ent})
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), name, plan); _save_state(state)
        await ctx.send(f"🧱 Added **{rep}** rectangular floor(s) to **{name}**.")

    @sh.command(name="add-round")
    async def sh_add_round(self, ctx, *, args: str):
        """
        Add a round floor.
        Example: !sh add-round diam=40 height=10 thick=5 mat=hardstone repeat=3
        """
        kv = parse_keyvals(args)
        try:
            diam = int(kv["diam"]); height = int(kv["height"]); thick = int(kv["thick"])
            mat = kv.get("mat","hardstone").lower()
            rep = int(kv.get("repeat", 1))
        except Exception:
            await ctx.send("❌ Required: diam=, height=, thick= (1|5|10|15). Optional: mat=, repeat=")
            return
        if mat not in MATERIALS: await ctx.send("❌ mat must be wood|brick|softstone|hardstone."); return
        if thick not in (1,5,10,15): await ctx.send("❌ thick must be 1|5|10|15."); return
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan: await ctx.send("❌ No current plan. Use `!sh new ...`"); return
        for _ in range(max(1,rep)):
            plan["floors"].append({"shape":"round","diam":diam,"height":height,"thick":thick,"mat":mat,"entrances":0})
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), name, plan); _save_state(state)
        await ctx.send(f"🗼 Added **{rep}** round floor(s) to **{name}**.")

    @sh.command(name="roof")
    async def sh_roof(self, ctx, kind: str):
        """
        Set roof kind: thatch|wood|slate|none
        """
        k = kind.lower()
        if k == "none": k = None
        elif k not in ROOF_MULT:
            await ctx.send("❌ roof must be thatch|wood|slate|none.")
            return
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan: await ctx.send("❌ No current plan."); return
        plan["roof"] = k
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), name, plan); _save_state(state)
        await ctx.send(f"🏚️ Roof set to **{kind}** for **{name}**.")

    @sh.command(name="parapet")
    async def sh_parapet(self, ctx, toggle: Literal["on","off"]):
        """
        Toggle top parapet (1' thick, 5' high, half-cost).
        """
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan: await ctx.send("❌ No current plan."); return
        plan["parapet"] = (toggle == "on")
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), name, plan); _save_state(state)
        await ctx.send(f"🛡️ Parapet **{toggle}** on **{name}**.")

    @sh.command(name="gate")
    async def sh_gate(self, ctx, floor_index: int, sections: int):
        """
        Gatehouse entrance deduction: subtract N ten-foot sections on a given floor.
          !sh gate <floor_index> <sections>
        """
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan or not plan.get("floors"):
            await ctx.send("❌ No floors to edit."); return
        i = max(1, min(floor_index, len(plan["floors"]))) - 1
        plan["floors"][i]["entrances"] = max(0, int(sections))
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), name, plan); _save_state(state)
        await ctx.send(f"🚪 Floor **{floor_index}** entrance deduction set to **{sections}** for **{name}**.")

    @sh.command(name="window")
    async def sh_window(self, ctx, floor_index: int, count: int):
        """
        Track windows (cosmetic only, cost is already included by rules).
        """
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan: await ctx.send("❌ No current plan."); return
        plan["windows"][str(max(1,floor_index))] = max(0, int(count))
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), name, plan); _save_state(state)
        await ctx.send(f"🪟 Tracked **{count}** window(s) on floor **{floor_index}** of **{name}**.")

    @sh.command(name="income")
    async def sh_income(self, ctx, income_type: str, gp_per_week: int):
        """
        Set income stream (gp/week). Types: taxes|tuition|tithe|business|custom
        """
        income_type = income_type.lower()
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan: await ctx.send("❌ No current plan."); return
        plan["income"] = {"type": income_type, "gpw": max(0, int(gp_per_week))}
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), name, plan); _save_state(state)
        await ctx.send(f"💰 Income set to **{gp_per_week} gp/week** ({income_type}) for **{name}**.")

    @sh.command(name="upkeep")
    async def sh_upkeep(self, ctx, gp_per_week: int):
        """
        Set upkeep (gp/week).
        """
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan: await ctx.send("❌ No current plan."); return
        plan["upkeep_gpw"] = max(0, int(gp_per_week))
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), name, plan); _save_state(state)
        await ctx.send(f"🧾 Upkeep set to **{gp_per_week} gp/week** for **{name}**.")

    @sh.command(name="engine")
    async def sh_engine(self, ctx, action: Literal["new","hit","reset","status"], *, name: str):
        """
        Siege engine AC tracker per BFRPG rule (start at AC 20, -1 each shot to min 11).
          !sh engine new "Ballista A"
          !sh engine hit "Ballista A"
          !sh engine reset "Ballista A"
          !sh engine status "Ballista A"
        """
        state = _load_state()
        plan_name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan: await ctx.send("❌ No current plan."); return
        engines = plan.setdefault("engines", {})
        found_key, _ = _resolve_partial(list(engines.keys()), name) if engines else (None, [])
        key = found_key or name
        if action == "new":
            engines[key] = {"ac": 20}
            await ctx.send(f"🎯 Engine **{key}** created at **AC 20**.")
        elif action == "hit":
            if key not in engines: engines[key] = {"ac": 20}
            engines[key]["ac"] = max(11, engines[key]["ac"] - 1)
            await ctx.send(f"🎯 Engine **{key}** now **AC {engines[key]['ac']}** (min 11).")
        elif action == "reset":
            if key in engines:
                engines[key]["ac"] = 20
                await ctx.send(f"🔄 Engine **{key}** reset to **AC 20**.")
            else:
                await ctx.send("❌ No such engine.")
        else:
            if key in engines:
                await ctx.send(f"📊 Engine **{key}** current **AC {engines[key]['ac']}**.")
            else:
                await ctx.send("❌ No such engine.")
        plan["engines"] = engines
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), plan_name, plan); _save_state(state)

    @sh.command(name="summary")
    async def sh_summary(self, ctx, workers: int = 100):
        """
        Show plan summary, costs, time/logistics, follower housing, and warnings.
        """
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan:
            await ctx.send("❌ No current plan."); return
        try:
            result = _calc_plan(plan)
        except Exception as e:
            await ctx.send(f"❌ Calc error: {type(e).__name__}: {e}")
            return

        base = result["base_cost_gp"]
        eng  = result["eng_cost_gp"]
        final = result["final_cost_gp"]
        floor_10sq = result["floor_area_10sq"]
        worker_days = worker_days_for_cost(final)
        time_days = build_time_days(worker_days, workers)
        tons = cargo_tons_for_construction_cost(base)

        max_h = max((int(f.get("height",10)) for f in plan.get("floors",[])), default=0)
        warn = []
        if max_h > 40: warn.append("Needs **solid foundation** (>40’).")
        if max_h > 60: warn.append("Must rest on **bedrock** (>60’).")
        if plan.get("type") == "guildhouse":
            warn.append("Guildhouses in cities: **exterior 1’ walls** but **×2 cost** for traps/passages.")
        if plan.get("type") == "tower":
            warn.append("Magic-User towers **×3 cost** for research fittings.")
        warn.extend(result.get("notes", []))

        followers_space_req = 0

        total_sqft = floor_10sq * 100
        cap_followers = total_sqft // 200

        income = plan.get("income", {"type":"","gpw":0})
        upkeep = int(plan.get("upkeep_gpw", 0))
        net_gpw = int(income.get("gpw",0)) - upkeep

        combos = []
        for f in plan.get("floors", []):
            mat = f.get("mat","hardstone").lower(); t = int(f.get("thick",1))
            if (mat,t) not in combos: combos.append((mat,t))
        combos = combos[:3]
        hp_lines = []
        for (mat,t) in combos:
            try:
                gp = wall_cost(mat,t)
                hp = gp
                hard = HARDNESS.get(mat, 0)
                hp_lines.append(f"{mat}/{t}′ walls: **HP {hp}** per section, **Hardness {hard}**")
            except Exception:
                pass

        embed = nextcord.Embed(title=f"🏰 {name} — Stronghold Summary", color=0x8c7b5a)
        embed.add_field(
            name="Plan",
            value=(
                f"Type: **{plan.get('type','castle')}**  •  Remote: **×{plan.get('remote',1.0)}**  •  "
                f"Roof: **{plan.get('roof') or 'none'}**  •  Parapet: **{'on' if plan.get('parapet') else 'off'}**\n"
                f"Floors: **{len(plan.get('floors',[]))}**  •  Floor area: **{floor_10sq} × 10′ squares**"
            ),
            inline=False
        )
        embed.add_field(
            name="Costs",
            value=(
                f"Base (materials): **{base:,} gp**\n"
                f"+ Engineering (per 10′): **{eng:,} gp**\n"
                f"= **{final:,} gp** (after type/remote multipliers)"
            ),
            inline=False
        )
        embed.add_field(
            name="Time & Logistics",
            value=(
                f"Worker-days: **{worker_days:,}**  |  Workers: **{workers}** → **{time_days} days** "
                f"(~{round(time_days/140,2)} yrs @ 140 d/yr)\n"
                f"Cargo (materials): **{tons:.1f} tons** (1 ton / 5 gp)"
            ),
            inline=False
        )
        embed.add_field(
            name="Followers & Space",
            value=(
                f"Total floor area: **{total_sqft:,} sq ft**  •  "
                f"Capacity for followers (200 sq ft each): **≈ {cap_followers}**"
            ),
            inline=False
        )
        if income.get("gpw",0) or upkeep:
            embed.add_field(
                name="Income & Upkeep",
                value=f"Income: **{income.get('gpw',0)} gp/wk** ({income.get('type') or '—'})  •  "
                      f"Upkeep: **{upkeep} gp/wk**  •  Net: **{net_gpw} gp/wk**",
                inline=False
            )
        if hp_lines:
            embed.add_field(name="Siege Helpers", value="\n".join(hp_lines), inline=False)
        if plan.get("engines"):
            engines = ", ".join(f"{k}: AC {v.get('ac',20)}" for k,v in plan["engines"].items())
            embed.add_field(name="Engines (AC tracker)", value=engines, inline=False)
        if warn:
            embed.add_field(name="Notes / Warnings", value="\n".join(warn), inline=False)
        await ctx.send(embed=embed)

    @sh.command(name="delete")
    async def sh_delete(self, ctx, *, name: Optional[str] = None):
        """
        Delete a plan (partial name ok). If no name, deletes the current plan.
        """
        state = _load_state()
        b = _user_bucket(state, str(ctx.guild.id), str(ctx.author.id))
        if not b["plans"]:
            await ctx.send("⚠️ You have no plans.")
            return
        if not name:
            cur = b.get("current")
            if not cur:
                await ctx.send("⚠️ No current plan selected.")
                return
            del b["plans"][cur]
            b["current"] = next(iter(b["plans"])) if b["plans"] else ""
            _save_state(state)
            await ctx.send(f"🗑️ Deleted plan **{cur}**.")
            return
        found, sugg = _resolve_partial(list(b["plans"].keys()), name)
        if not found:
            if sugg:
                await ctx.send("⚠️ Ambiguous — did you mean: " + ", ".join(f"`{s}`" for s in sugg) + " ?")
            else:
                await ctx.send("❌ No such plan.")
            return
        del b["plans"][found]
        if b.get("current") == found:
            b["current"] = next(iter(b["plans"])) if b["plans"] else ""
        _save_state(state)
        await ctx.send(f"🗑️ Deleted plan **{found}**.")



    def _taper_thickness_schedule(self, total_height_ft: int) -> list[int]:
        """
        Returns a list of per-10' course thicknesses (in feet) from bottom to top.
        Rule-of-thumb, aligned with BFRPG max heights and the example:
          • <=40' total: all 1'
          • 40'-60': bottom 20' at 5', rest 1'
          • 60'-80': bottom 20' at 10', next 20' at 5', rest 1'
          • 80'-100': bottom 20' at 15', next 20' at 10', next 20' at 5', rest 1'
        """
        courses = max(1, total_height_ft // 10)
        sched = [1] * courses
        if total_height_ft > 80:
            for i in range(min(2, courses)): sched[i] = 15
            for i in range(2, min(4, courses)): sched[i] = 10
            for i in range(4, min(6, courses)): sched[i] = 5
        elif total_height_ft > 60:
            for i in range(min(2, courses)): sched[i] = 10
            for i in range(2, min(4, courses)): sched[i] = 5
        elif total_height_ft > 40:
            for i in range(min(2, courses)): sched[i] = 5
        return sched

    def _build_approx_plan(self, *, shape: str, footprint: int, height: int,
                           mat: str, roof: Optional[str], typ: str, remote: float,
                           parapet: bool, entrances_sections: int, name: str) -> dict:
        schedule = self._taper_thickness_schedule(height)
        floors = []
        for thick in schedule:
            if shape == "rect":
                floors.append({"shape":"rect","side":footprint,"height":10,"thick":thick,"mat":mat,"entrances":entrances_sections})
            else:
                floors.append({"shape":"round","diam":footprint,"height":10,"thick":thick,"mat":mat,"entrances":0})
        return {
            "name": name, "type": typ, "remote": remote, "roof": roof,
            "parapet": parapet, "floors": floors, "windows": {}, "income": {"type":"","gpw":0},
            "upkeep_gpw": 0, "engines": {}
        }

    def _format_rom_embed(self, title: str, plan: dict, result: dict, workers: int) -> nextcord.Embed:
        base = result["base_cost_gp"]; eng = result["eng_cost_gp"]; final = result["final_cost_gp"]
        floor_10sq = result["floor_area_10sq"]
        worker_days = worker_days_for_cost(final); time_days = build_time_days(worker_days, workers)
        tons = cargo_tons_for_construction_cost(base)
        total_sqft = floor_10sq * 100; cap_followers = total_sqft // 200
        warn = []
        max_h = max((int(f.get("height",10)) for f in plan.get("floors",[])), default=0)
        if max_h > 40: warn.append("Needs **solid foundation** (>40’).")
        if max_h > 60: warn.append("Must rest on **bedrock** (>60’).")
        if plan.get("type") == "guildhouse":
            warn.append("Guildhouses in cities: **exterior 1’ walls** but **×2 cost** for traps/passages.")
        if plan.get("type") == "tower":
            warn.append("Magic-User towers **×3 cost** for research fittings.")
        warn.extend(result.get("notes", []))

        embed = nextcord.Embed(title=title, color=0x8c7b5a)
        shape = plan["floors"][0]["shape"] if plan.get("floors") else "rect"
        foot = plan["floors"][0]["side"] if shape=="rect" else plan["floors"][0]["diam"]
        embed.add_field(
            name="Inputs",
            value=(
                f"Shape: **{shape}**  •  Footprint: **{foot} ft**  •  Height: **{sum(f['height'] for f in plan['floors'])} ft**\n"
                f"Material: **{plan['floors'][0]['mat']}**  •  Roof: **{plan.get('roof') or 'none'}**\n"
                f"Type: **{plan.get('type','castle')}**  •  Remote: **×{plan.get('remote',1.0)}**  •  Parapet: **{'on' if plan.get('parapet') else 'off'}**\n"
                f"Floors: **{len(plan.get('floors',[]))}**  •  Floor area: **{floor_10sq} × 10′ squares**"
            ),
            inline=False
        )
        embed.add_field(
            name="Costs",
            value=(
                f"Base (materials): **{base:,} gp**\n"
                f"+ Engineering (per 10′): **{eng:,} gp**\n"
                f"= **{final:,} gp** (after type/remote multipliers)"
            ),
            inline=False
        )
        embed.add_field(
            name="Time & Logistics",
            value=(
                f"Worker-days: **{worker_days:,}**  |  Workers: **{workers}** → **{time_days} days** "
                f"(~{round(time_days/140,2)} yrs @ 140 d/yr)\n"
                f"Cargo (materials): **{tons:.1f} tons** (1 ton / 5 gp)"
            ),
            inline=False
        )
        embed.add_field(
            name="Followers & Space",
            value=f"Total floor area: **{total_sqft:,} sq ft**  •  Capacity (200 sq ft each): **≈ {cap_followers}**",
            inline=False
        )
        if warn: embed.add_field(name="Notes / Warnings", value="\n".join(warn), inline=False)
        return embed


    @sh.command(name="approx")
    async def sh_approx(self, ctx, *, args: str):
        """
        Rough ballpark from minimal inputs with a smart taper.

        Examples:
          !sh approx rect side=50 height=60 mat=hardstone roof=slate type=castle remote=1.0 parapet=on entrances=2 [save="Name"]
          !sh approx round diam=40 height=80 mat=hardstone roof=wood type=tower
          !sh approx shape=rect side=30 height=60 mat=hardstone roof=slate
        """
        kv = parse_keyvals(args)

        shape_token = (kv.get("shape") or "").lower()
        if not shape_token:
            pieces = [p for p in re.split(r"\s+", args.strip()) if p]
            if pieces and "=" not in pieces[0]:
                shape_token = pieces[0].lower()

        aliases = {
            "rect": {"rect","rec","r","square","sq"},
            "round": {"round","rnd","circle","circ","c","tower","roundtower"}
        }
        shape = None
        for canon, pool in aliases.items():
            if shape_token in pool:
                shape = canon
                break
        if not shape:
            await ctx.send("❌ Specify shape: `rect` or `round` (e.g., `!sh approx rect side=50 height=60 ...`).")
            return

        mat = (kv.get("mat") or "hardstone").lower()
        typ = (kv.get("type") or "castle").lower()
        remote = float(kv.get("remote", 1.0))
        roof = kv.get("roof", "slate").lower()
        roof = None if roof == "none" else roof
        parapet = str(kv.get("parapet","off")).lower() in ("on","true","yes","1")
        entrances = int(kv.get("entrances", 0))
        save_name = kv.get("save")


        try:
            height = int(kv["height"])
        except Exception:
            await ctx.send("❌ Provide `height=<ft>`.")
            return

        fp_val = kv.get("side") if shape == "rect" else kv.get("diam")
        fp_val = fp_val or kv.get("size") or kv.get("footprint")
        if fp_val is None:
            await ctx.send(f"❌ Provide `{'side' if shape=='rect' else 'diam'}=<ft>` (or `size=`/`footprint=`).")
            return
        try:
            footprint = int(fp_val)
        except Exception:
            await ctx.send("❌ Footprint must be an integer (feet).")
            return

        if mat not in MATERIALS:
            await ctx.send("❌ mat must be wood|brick|softstone|hardstone.")
            return
        if roof and roof not in ROOF_MULT:
            await ctx.send("❌ roof must be thatch|wood|slate|none.")
            return
        if typ not in TYPE_MULT:
            await ctx.send("❌ type must be castle|tower|temple|guildhouse.")
            return

        tmp_name = save_name or "_approx"
        plan = self._build_approx_plan(shape=shape, footprint=footprint, height=height, mat=mat,
                                       roof=roof, typ=typ, remote=remote, parapet=parapet,
                                       entrances_sections=entrances, name=tmp_name)
        try:
            result = _calc_plan(plan)
        except Exception as e:
            await ctx.send(f"❌ Calc error: {type(e).__name__}: {e}")
            return

        embed = self._format_rom_embed("🏰 ROM Stronghold Estimate", plan, result, workers=int(kv.get("workers",100)))
        await ctx.send(embed=embed)

        if save_name:
            state = _load_state()
            _set_plan(state, str(ctx.guild.id), str(ctx.author.id), save_name, plan)
            _select_plan(state, str(ctx.guild.id), str(ctx.author.id), save_name)
            _save_state(state)
            await ctx.send(f"💾 Saved and selected plan **{save_name}**.")




    @sh.command(name="budget")
    async def sh_budget(self, ctx, *, args: str):
        """
        Find the best area under a budget (tries reasonable sizes/heights).

        Examples:
          !sh budget rect budget=40000 side_min=30 side_max=80 step=10 mat=hardstone roof=slate type=castle remote=1.0 [save="Name"]
          !sh budget round budget=60000 diam_min=30 diam_max=60 step=10 mat=hardstone type=tower roof=wood
          !sh budget shape=rect budget=50000 side_min=40 side_max=60
        """
        kv = parse_keyvals(args)


        shape_token = (kv.get("shape") or "").lower()
        if not shape_token:
            pieces = [p for p in re.split(r"\s+", args.strip()) if p]
            if pieces and "=" not in pieces[0]:
                shape_token = pieces[0].lower()
        aliases = {
            "rect": {"rect","rec","r","square","sq"},
            "round": {"round","rnd","circle","circ","c","tower","roundtower"}
        }
        shape = None
        for canon, pool in aliases.items():
            if shape_token in pool:
                shape = canon
                break
        if not shape:
            await ctx.send("❌ First specify shape (e.g., `!sh budget rect budget=40000 ...`) or pass `shape=rect`.")
            return

        try:
            budget = int(kv["budget"])
        except Exception:
            await ctx.send("❌ Provide budget=<gp>.")
            return

        mat = (kv.get("mat") or "hardstone").lower()
        typ = (kv.get("type") or "castle").lower()
        remote = float(kv.get("remote", 1.0))
        roof = kv.get("roof", "slate").lower()
        roof = None if roof == "none" else roof
        parapet = str(kv.get("parapet","off")).lower() in ("on","true","yes","1")
        save_name = kv.get("save")
        step = int(kv.get("step", 10))
        if mat not in MATERIALS: await ctx.send("❌ mat must be wood|brick|softstone|hardstone."); return
        if roof and roof not in ROOF_MULT: await ctx.send("❌ roof must be thatch|wood|slate|none."); return
        if typ not in TYPE_MULT: await ctx.send("❌ type must be castle|tower|temple|guildhouse."); return

        if shape == "rect":
            mn = int(kv.get("side_min", 30)); mx = int(kv.get("side_max", 80))
            sizes = list(range(mn, mx+1, step))
        else:
            mn = int(kv.get("diam_min", 30)); mx = int(kv.get("diam_max", 80))
            sizes = list(range(mn, mx+1, step))

        best = None
        for footprint in sizes:
            for height in range(10, 101, 10):
                plan = self._build_approx_plan(shape=shape, footprint=footprint, height=height, mat=mat,
                                               roof=roof, typ=typ, remote=remote, parapet=parapet,
                                               entrances_sections=int(kv.get("entrances",0)), name="_budget")
                try:
                    res = _calc_plan(plan)
                except Exception:
                    continue
                if res["final_cost_gp"] <= budget:
                    area = res["floor_area_10sq"]
                    if (best is None) or (area > best["res"]["floor_area_10sq"]):
                        best = {"plan": plan, "res": res}

        if not best:
            await ctx.send("😬 No design fit under that budget with the given constraints.")
            return

        embed = self._format_rom_embed("📐 Best Fit Under Budget", best["plan"], best["res"], workers=int(kv.get("workers",100)))
        b = best["res"]["final_cost_gp"]
        embed.add_field(name="Budget", value=f"Target ≤ **{budget:,} gp**  •  Selected: **{b:,} gp**  •  Slack: **{budget - b:,} gp**", inline=False)
        await ctx.send(embed=embed)

        if save_name:
            state = _load_state()
            best["plan"]["name"] = save_name
            _set_plan(state, str(ctx.guild.id), str(ctx.author.id), save_name, best["plan"])
            _select_plan(state, str(ctx.guild.id), str(ctx.author.id), save_name)
            _save_state(state)
            await ctx.send(f"💾 Saved and selected plan **{save_name}**.")




    @sh.command(name="roi")
    async def sh_roi(self, ctx):
        """
        Show breakeven time for the current plan based on Income & Upkeep.
          !sh roi
        """
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan: await ctx.send("❌ No current plan."); return

        res = _calc_plan(plan)
        final = res["final_cost_gp"]
        income = int(plan.get("income",{}).get("gpw",0))
        upkeep = int(plan.get("upkeep_gpw",0))
        net = income - upkeep
        if net <= 0:
            await ctx.send(f"📉 Breakeven: **never** (net ≤ 0 gp/wk). Final cost **{final:,} gp**, net **{net} gp/wk**.")
            return
        weeks = math.ceil(final / net)
        years = round(weeks / (140/7), 2)
        await ctx.send(f"📈 Breakeven in **{weeks} weeks** (~**{years} years**). (Final **{final:,} gp** / Net **{net} gp/wk**)")



    @sh.command(name="logistics")
    async def sh_logistics(self, ctx, *, args: str = ""):
        """
        Estimate hauling effort for materials on the current plan:
          !sh logistics wag_cap=2.0 trip_days=4 teams=10
          !sh logistics ship_cap=50 ships=1 port_trip_days=10

        Defaults: wag_cap=2 tons, teams=5, trip_days=4, ship_cap=0 (unused), ships=0.
        """
        kv = parse_keyvals(args or "")
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan: await ctx.send("❌ No current plan."); return

        res = _calc_plan(plan)
        tons = cargo_tons_for_construction_cost(res["base_cost_gp"])
        wag_cap = float(kv.get("wag_cap", 2.0))
        teams = int(kv.get("teams", 5))
        trip_days = int(kv.get("trip_days", 4))
        ship_cap = float(kv.get("ship_cap", 0.0))
        ships = int(kv.get("ships", 0))
        port_trip_days = int(kv.get("port_trip_days", trip_days))

        wagon_trips = math.ceil(tons / max(0.0001, wag_cap))
        wagon_rounds = math.ceil(wagon_trips / max(1, teams))
        wagon_days = wagon_rounds * trip_days

        ship_trips = math.ceil(tons / max(0.0001, ship_cap)) if ship_cap > 0 and ships > 0 else 0
        ship_rounds = math.ceil(ship_trips / max(1, ships)) if ship_trips else 0
        ship_days = ship_rounds * port_trip_days if ship_trips else 0

        msg = (
            f"🛻 Materials: **{tons:.1f} tons**\n"
            f"Wagons: **{teams}** teams @ **{wag_cap} t** each → **{wagon_trips}** trips → **{wagon_rounds}** rounds → **{wagon_days}** days\n"
        )
        if ship_trips:
            msg += f"⛴️ Ships: **{ships}** @ **{ship_cap} t** each → **{ship_trips}** trips → **{ship_rounds}** rounds → **{ship_days}** days\n"
        await ctx.send(msg)



    @sh.command(name="taper")
    async def sh_taper(self, ctx):
        """
        Apply rule-of-thumb taper to the current plan based on total height.
        Keeps shape/footprints; only adjusts thickness per 10' course.
        """
        state = _load_state()
        name, plan = _current_plan(state, str(ctx.guild.id), str(ctx.author.id))
        if not plan or not plan.get("floors"):
            await ctx.send("❌ No current plan."); return

        total_h = sum(int(f.get("height",10)) for f in plan["floors"])
        schedule = self._taper_thickness_schedule(total_h)


        first = plan["floors"][0]
        mat = first.get("mat","hardstone")
        if first.get("shape") == "rect":
            side = first.get("side", 30)
            floors = [{"shape":"rect","side":side,"height":10,"thick":t,"mat":mat,"entrances":0} for t in schedule]
        else:
            diam = first.get("diam", 30)
            floors = [{"shape":"round","diam":diam,"height":10,"thick":t,"mat":mat,"entrances":0} for t in schedule]

        plan["floors"] = floors
        _set_plan(state, str(ctx.guild.id), str(ctx.author.id), name, plan); _save_state(state)
        await ctx.send(f"🪚 Applied taper to **{name}**.")


    @sh.command(name="guide")
    async def sh_guide(self, ctx):
        """
        Simple, friendly guide + cheat sheet.
        """
        e = nextcord.Embed(
            title="🏗️ Strongholds — Simple Guide",
            color=0x8c7b5a,
            description="Figure out costs fast, or build and save a plan. Your plans live in `data/strongholds.json`."
        )


        e.add_field(
            name="Start here (copy these):",
            value=(
                "• Quick idea (auto wall thickness):\n"
                "  `!sh approx rect side=40 height=50 mat=hardstone roof=slate type=castle`\n"
                "• Fit a budget:\n"
                "  `!sh budget rect budget=40000 side_min=30 side_max=60 step=10 mat=hardstone`\n"
                "• One-shot math (single floor):\n"
                "  `!shcalc 40 20 10`  *(= side 40, height 20, thick 10)*"
            ),
            inline=False
        )


        e.add_field(
            name="Build a plan in 5 steps:",
            value=(
                "1) New plan → `!sh new MyKeep type=castle roof=slate`\n"
                "2) Add floors → `!sh add-rect side=40 height=20 thick=10 mat=hardstone`\n"
                "3) Options → `!sh parapet on` • `!sh gate 1 2` • `!sh window 2 4`\n"
                "4) Summary → `!sh summary`  (costs, time, warnings)\n"
                "5) Money math → `!sh income taxes 200` • `!sh upkeep 50` • `!sh roi`"
            ),
            inline=False
        )


        e.add_field(
            name="Cheat sheet: materials & roofs",
            value=(
                "**Materials (`mat=`)**\n"
                "• `wood` — cheapest; thin walls (1′)\n"
                "• `brick` — medium; 1′ or 5′ walls\n"
                "• `softstone` — thicker allowed (1′/5′/10′)\n"
                "• `hardstone` — strongest (1′/5′/10′/15′)\n"
                "\n"
                "**Roofs (`roof=`)**\n"
                "• `thatch` — cheapest\n"
                "• `wood` — medium\n"
                "• `slate` — priciest\n"
                "_Tip: `roof=none` turns the roof off._"
            ),
            inline=False
        )


        e.add_field(
            name="Cheat sheet: shapes, types, thickness",
            value=(
                "**Shapes**\n"
                "• Rectangle: `rect` (also `rec`, `r`, `square`, `sq`)\n"
                "• Round: `round` (also `rnd`, `circle`, `tower`)\n"
                "\n"
                "**Types (`type=`)**\n"
                "• `castle` • `temple` • `guildhouse` (×2 cost) • `tower` (×3 cost)\n"
                "\n"
                "**Wall thickness (`thick=`)**\n"
                "• Use 1, 5, 10, or 15 (feet). Taller builds need thicker walls.\n"
                "_`!sh approx` picks a smart taper for you._"
            ),
            inline=False
        )


        e.add_field(
            name="Extras & gotchas",
            value=(
                "**Extras**\n"
                "• Parapet: `!sh parapet on|off` (adds a low wall on top)\n"
                "• Entrances: `!sh gate <floor#> <sections>` (subtracts 10′ wall sections)\n"
                "• Windows: `!sh window <floor#> <count>` (cosmetic)\n"
                "• Remote multiplier: add `remote=1.0` (1.0 = normal; higher = harder to build)\n"
                "\n"
                "**Common mistakes**\n"
                "• Missing shape on `approx`/`budget` → start with `rect` or `round` (or `shape=rect`).\n"
                "• CSV list lengths must match in `!shcalc`.\n"
                "• `thick` must be one of **1/5/10/15**.\n"
                "• Roof must be `thatch`, `wood`, `slate`, or `none`."
            ),
            inline=False
        )


        e.add_field(
            name="`!shcalc` quick tips",
            value=(
                "• Single floor explicit: `!shcalc side=50 height=20 thick=10 mat=hardstone roof=slate`\n"
                "• Multi-floor lists (same length):\n"
                "  `!shcalc side=50,50,40 height=20,10,10 thick=10,10,5 mat=hardstone`\n"
                "• Positional: `!shcalc <side> <height> <thick>`"
            ),
            inline=False
        )

        await ctx.send(embed=e)


    @sh.command(name="examples")
    async def sh_examples(self, ctx):
        """
        Show a few presets with one-click Save buttons.
        """

        cog = self
        author_id = ctx.author.id

        class ExamplesView(nextcord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)

            async def _save_plan(self, interaction: nextcord.Interaction, plan: dict, save_name: str):
                if interaction.user.id != author_id:
                    await interaction.response.send_message("This menu belongs to someone else.", ephemeral=True)
                    return
                state = _load_state()
                gid = str(interaction.guild.id)
                uid = str(interaction.user.id)
                _set_plan(state, gid, uid, save_name, plan)
                _select_plan(state, gid, uid, save_name)
                _save_state(state)
                await interaction.response.send_message(f"💾 Saved and selected plan **{save_name}**.", ephemeral=True)

            @nextcord.ui.button(label="Rect Keep 50×50, h60 (castle)", style=nextcord.ButtonStyle.primary)
            async def btn_keep(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
                plan = cog._build_approx_plan(
                    shape="rect", footprint=50, height=60, mat="hardstone",
                    roof="slate", typ="castle", remote=1.0, parapet=True,
                    entrances_sections=2, name="Rect Keep 50x50 h60"
                )
                await self._save_plan(interaction, plan, "Rect Keep 50x50 h60")

            @nextcord.ui.button(label="Round Tower Ø40, h80 (tower)", style=nextcord.ButtonStyle.primary)
            async def btn_tower(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
                plan = cog._build_approx_plan(
                    shape="round", footprint=40, height=80, mat="hardstone",
                    roof="wood", typ="tower", remote=1.0, parapet=False,
                    entrances_sections=0, name="Round Tower d40 h80"
                )
                await self._save_plan(interaction, plan, "Round Tower d40 h80")

            @nextcord.ui.button(label="Guildhouse 60×60, h40 (city)", style=nextcord.ButtonStyle.secondary)
            async def btn_guild(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
                plan = cog._build_approx_plan(
                    shape="rect", footprint=60, height=40, mat="brick",
                    roof="slate", typ="guildhouse", remote=1.0, parapet=False,
                    entrances_sections=2, name="Guildhouse 60x60 h40"
                )
                await self._save_plan(interaction, plan, "Guildhouse 60x60 h40")

            @nextcord.ui.button(label="Temple 70×70, h30", style=nextcord.ButtonStyle.secondary)
            async def btn_temple(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
                plan = cog._build_approx_plan(
                    shape="rect", footprint=70, height=30, mat="softstone",
                    roof="wood", typ="temple", remote=1.0, parapet=True,
                    entrances_sections=2, name="Temple 70x70 h30"
                )
                await self._save_plan(interaction, plan, "Temple 70x70 h30")

        e = nextcord.Embed(
            title="🏰 Stronghold Examples",
            color=0x8c7b5a,
            description=(
                "Click a button to **save & select** a ready-made plan (you can tweak it after with `!sh add-rect`, `!sh roof`, etc.)."
            ),
        )
        e.add_field(
            name="What you get:",
            value=(
                "• **Rect Keep 50×50, h60** — hardstone, slate roof, parapet on, 2 entrances, type *castle*\n"
                "• **Round Tower Ø40, h80** — hardstone, wood roof, type *tower*\n"
                "• **Guildhouse 60×60, h40** — brick, slate roof, 2 entrances, type *guildhouse* (city)\n"
                "• **Temple 70×70, h30** — softstone, wood roof, parapet on, type *temple*"
            ),
            inline=False
        )
        await ctx.send(embed=e, view=ExamplesView())



def setup(bot):
    bot.add_cog(Strongholds(bot))

