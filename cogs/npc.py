import os, re, json, random, configparser
from datetime import datetime
from nextcord.ext import commands
import nextcord

MON_DIR = "monsters"  

def _ensure_mon_dir():
    os.makedirs(MON_DIR, exist_ok=True)


def _roll(spec: str) -> int:
    m = re.fullmatch(r"\s*(\d+)d(\d+)\s*([+-]\s*\d+)?\s*", spec.strip().lower())
    if not m: raise ValueError(f"Bad dice spec: {spec}")
    n, sides = int(m.group(1)), int(m.group(2))
    flat = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    return sum(random.randint(1, sides) for _ in range(n)) + flat

def _armor_ac_val(armor: str) -> int:
    a = (armor or "").strip().lower()
    if a in {"noarmor", "none", "unarmored"}: return 10
    if a in {"leather", "leatherarmor"}:      return 13  
    if a in {"chain", "chainmail"}:           return 15
    if a in {"plate", "platemail"}:           return 17
    return 10

def _xp_for_hd(hd: int) -> int:
    table = {
        0:10, 1:25, 2:75, 3:145, 4:240, 5:360, 6:500, 7:670, 8:875, 9:1075,
        10:1300, 11:1575, 12:1875, 13:2175, 14:2500, 15:2850, 16:3250,
        17:3600, 18:4000, 19:4500, 20:5250
    }
    hd_i = max(0, min(20, int(hd)))
    return table.get(hd_i, 5250)

def _weapon_pack(cls: str, ranged_ok: bool):
    cls = cls.title()
    if cls == "Fighter":
        melee = random.choice(["longsword", "mace", "spear", "battleaxe"])
        ranged = random.choice(["shortbow", "lightxbow"]) if ranged_ok else None
    elif cls == "Cleric":
        melee = random.choice(["mace", "warhammer", "staff"])
        ranged = None
    elif cls == "Thief":
        melee = random.choice(["shortsword", "dagger", "club"])
        ranged = "shortbow" if ranged_ok else None
    else:  
        melee = "dagger" if random.random() < 0.5 else "staff"
        ranged = None
    return melee, ranged

def _dmg_for(weap: str) -> str:
    w = (weap or "").lower()
    if "dagger" in w:     return "1d4"
    if "shortsword" in w: return "1d6"
    if "longsword" in w:  return "1d8"
    if "mace" in w:       return "1d6"
    if "spear" in w:      return "1d6"
    if "battleaxe" in w:  return "1d8"
    if "warhammer" in w:  return "1d6"
    if "club" in w:       return "1d4"
    if "staff" in w:      return "1d6"
    if "shortbow" in w:   return "1d6"
    if "xbow" in w:       return "1d8"
    return "1d4"


CLERIC_SLOTS = {
    1: [0,1,2,2,2,2,3,3,3,3,4,4,4,4,4,5,5,5,6,6],
    2: [0,0,0,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5],
    3: [0,0,0,0,0,1,2,2,2,2,3,3,3,4,4,4,4,4,4,5],
    4: [0,0,0,0,0,0,0,1,2,2,2,2,3,3,3,3,3,4,4,4],
    5: [0,0,0,0,0,0,0,0,0,1,2,2,2,2,3,3,3,3,3,3],
    6: [0,0,0,0,0,0,0,0,0,0,0,1,2,2,2,2,2,3,3,3],
    7: [0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,2,2,2,3],
}
MU_SLOTS = {
    1: [1,2,2,2,2,3,3,3,3,4,4,4,4,4,5,5,5,6,6,6],
    2: [0,0,1,2,2,2,2,3,3,3,4,4,4,4,4,5,5,5,5,5],
    3: [0,0,0,0,1,2,2,2,2,3,3,3,4,4,4,4,4,4,5,5],
    4: [0,0,0,0,0,0,1,2,2,2,2,3,3,3,3,3,4,4,4,4],
    5: [0,0,0,0,0,0,0,0,1,2,2,2,2,3,3,3,3,3,3,4],
    6: [0,0,0,0,0,0,0,0,0,0,1,2,2,2,2,2,3,3,3,3],
    7: [0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,2,2,2,3],
}

SPELL_LISTS = {
    "Cleric": {
        "l1": "CureLightWounds CauseLightWounds DetectEvil DetectGood DetectMagic Light Darkness ProtectionFromEvil ProtectionFromGood PurifyFoodandWater RemoveFear CauseFear ResistCold Censure Command Sanctuary".split(),
        "l2": "Bless Bane CharmAnimal FindTraps HoldPerson ResistFire Silence15 SpeakwithAnimals SpiritualHammer GentleRepose RemoveParalysis RestoreHealth".split(),
        "l3": "ContinualLight ContinualDarkness CureBlindness CauseBlindness CureDisease CauseDisease GrowthofAnimals LocateObject RemoveCurse BestowCurse SpeakwithDead Striking CureDeafness Tongues WaterBreathing".split(),
        "l4": "AnimateDead CreateWater CureSeriousWounds CauseSeriousWounds DispelMagic NeutralizePoison PoisonTouch ProtectionEvil10 ProtectionGood10 SpeakwithPlants StickstoSnakes HoldMonster MagicMirror StoneShape".split(),
        "l5": "Commune CreateFood DispelEvil InsectPlague Quest RaiseDead TrueSeeing WallofFire PlaneShift RingofLesserHealing WallofStone Mummify".split(),
        "l6": "AnimateObjects BladeBarrier FindthePath Heal Harm Regenerate Restoration SpeakwithMonsters WordofRecall ControlUndead UndeathtoDeath".split(),
        "l7": "Anti-MagicShell ControlWeather Earthquake FireStorm HolyWord Resurrection RingofGreaterHealing WindWalk AstralProjection Gate".split(),
    },
    "Magic-User": {
        "l1": "ReadMagic CharmPerson DetectMagic FloatingDisc HoldPortal Light Darkness MagicMissile MagicMouth ProtectionFromEvil ProtectionFromGood ReadLanguages Shield Sleep Ventriloquism Alarm MageArmor Mount UnseenServant".split(),
        "l2": "ContinualLight ContinualDarkness DetectEvil DetectGood DetectInvisible Invisibility Knock Levitate LocateObject MindReading MirrorImage PhantasmalForce Web WizardLock AnalyzeMagic Familiar".split(),
        "l3": "Clairvoyance Darkvision DispelMagic Fireball Fly Haste Slow HoldPerson Invisible10 LightningBolt ProtectionEvil10 ProtectionGood10 ProtectionNormalMissiles WaterBreathing Clairaudience GaseousForm ImmunitytoNormalWeapons Tongues".split(),
        "l4": "CharmMonster Confusion DimensionDoor GrowthofPlants ShrinkPlants HallucinatoryTerrain IceStorm Massmorph PolymorphOther PolymorphSelf RemoveCurse BestowCurse WallofFire WizardEye ImprovedInvisibility MagicMirror Drainblade".split(),
        "l5": "AnimateDead Cloudkill ConjureElemental Feeblemind Restoremind HoldMonster MagicJar Passwall Telekinesis Teleport WallofStone PrivateSanctum Stoneskin".split(),
        "l6": "Anti-MagicShell DeathSpell Disintegrate FleshToStone StoneToFlesh Geas InvisibleStalker LowerWater ProjectedImage Reincarnate WallofIron Permanency RevealMagic".split(),
        "l7": "DelayedBlastFireball GreaterTeleport Longevity MassInvisibility PhaseDoor PowerWordStun Sword WychlampAura AstralProjection Gate".split(),
    },
}

def _choose_spells_for(cls: str, level: int):
    """
    Returns (display_names_list, counts_dict) or ([], {}) if no spells.
    display_names_list: unique, nice-cased names for the 'spells =' line
    counts_dict: {lowercased_name: prepared_count}
    """
    cls = cls.title()
    if level <= 0: return [], {}

    if cls == "Cleric":
        slots_tbl = CLERIC_SLOTS
    elif cls == "Magic-User":
        slots_tbl = MU_SLOTS
    else:
        return [], {}

    prepared_order = []
    counts = {}

    for slevel in range(1, 8):
        slots = slots_tbl.get(slevel, [0]*20)[max(0, min(19, level-1))]
        if slots <= 0:
            continue
        pool = SPELL_LISTS[cls].get(f"l{slevel}", [])
        if not pool:
            continue

        take_unique = min(slots, len(pool))
        uniques = random.sample(pool, take_unique) if take_unique > 0 else []
        for name in uniques:
            key = name.lower()
            counts[key] = counts.get(key, 0) + 1
            if name not in prepared_order:
                prepared_order.append(name)

        remaining = slots - take_unique
        for _ in range(remaining):
            name = random.choice(pool)
            key = name.lower()
            counts[key] = counts.get(key, 0) + 1
            if name not in prepared_order:
                prepared_order.append(name)

    return prepared_order, counts

def _choose_armor_and_shield(ptype: str, role: str, cls: str):
    ptype = ptype.lower(); role = role.lower(); cls = cls.title()
    if ptype == "bandits":
        armor = random.choice(["leather","leather","leather","chain"])
        shield = random.random() < 0.4
    elif ptype in {"buccaneers","pirates"}:
        armor = "leather"
        shield = False
    elif ptype == "merchants":
        if role.startswith("guard"):
            armor = "leather"; shield = (random.random() < 0.3)
        else:
            armor = "noarmor"; shield = False
    elif ptype == "nobles":
        if role.startswith("guard"):
            armor = "platemail"; shield = True
        elif role == "teamster":
            armor = "chain"; shield = False
        else:
            armor = "chain"; shield = True
    elif ptype == "pilgrims":
        if role.startswith("guard"):
            armor = "chain"; shield = False
        else:
            armor = "noarmor"; shield = False
    elif ptype == "adventurers":
        if cls in {"Fighter","Cleric"}:
            armor = "chain"; shield = (random.random() < 0.5)
        elif cls == "Thief":
            armor = "leather"; shield = False
        else:
            armor = "noarmor"; shield = False
    else:
        armor = "noarmor"; shield = False
    ac = _armor_ac_val(armor) + (1 if shield else 0)
    return ac, armor, shield

def _write_template(path: str, *, ac: int, hd: int, save_class: str,
                    melee: str, ranged: str|None, move: int = 30,
                    xp: int|None = None, type_str: str = "humanoid",
                    spell_block: tuple[list[str], dict[str,int]] | None = None):
    cp = configparser.ConfigParser()
    base = {
        "ac": str(int(ac)),
        "hd": str(int(hd)),
        "hpmod": "0",
        "attacks": "1",
        "move": str(int(move)),
        "saveas": f"{save_class} {int(hd)}",
        "xp": str(xp if xp is not None else _xp_for_hd(hd)),
        "type": type_str
    }
    names = [melee]
    if ranged: names.append(ranged)
    base["attacknames"] = " ".join(names)

    if spell_block:
        display_list, counts = spell_block
        if display_list:
            base["spells"] = " ".join(display_list)
            for disp in display_list:
                key = disp.lower()
                val = counts.get(key, 1)
                cp.setdefault("base", {})
                cp["base"][key] = str(int(val))

    cp["base"] = base
    cp["base"][melee] = _dmg_for(melee)
    if ranged:
        cp["base"][ranged] = _dmg_for(ranged)
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)


class NPC(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="npcparty")
    @commands.has_permissions(manage_guild=True)
    async def npcparty(self, ctx, party_type: str = None, *opts):
        """
        Generate modular monster templates in /monsters, then spawn with !mon:
          !npcparty bandits -name Blackfang
          !npcparty merchants -sea -name Seafoam
          !npcparty adventurers -avg 4 -evil -name RedHand
          !npcparty nobles -name Barons_Retinue
          !npcparty pilgrims -name Wayfarers
          !npcparty disband <Slug>
        Flags:
          -name <Slug>     custom slug (default: <type>_YYYYmmddHHMMSS)
          -avg <N>         avg level for adventurers (default 2)
          -evil            allow humanoid replacements (orcs/hobgobs/gnolls) [flavor only]
          -nonhuman <Race> force a non-human race flavor (Elf/Dwarf/Halfling/etc.)
          -sea             sea-going merchants
          -seed <N>        deterministic RNG
        """
        _ensure_mon_dir()

        if party_type is None or party_type.lower() in {"help","-h","-help"}:
            em = nextcord.Embed(
                title="🧭 NPC Party Generator",
                description="Creates **monster-style** templates in `/monsters` (no PC sections). Then use `!mon <name> [count]`.",
                color=0x3B82F6
            )
            em.add_field(name="Types",
                         value="`adventurers`, `bandits`, `buccaneers`, `pirates`, `merchants`, `nobles`, `pilgrims`",
                         inline=False)
            em.add_field(
                name="Examples",
                value=("```text\n"
                       "!npcparty bandits -name Blackfang\n"
                       "!npcparty adventurers -avg 5 -evil -name RedHand\n"
                       "!npcparty merchants -sea -name Seafoam\n"
                       "!npcparty nobles -name Barons_Retinue\n"
                       "```"),
                inline=False
            )
            await ctx.send(embed=em)
            return

        if party_type.lower() == "disband":
            slug = (opts[0] if opts else "").strip()
            if not slug:
                await ctx.send("Usage: `!npcparty disband <Slug>`")
                return
            mani = os.path.join(MON_DIR, f"enc_{slug}.json")
            if not os.path.exists(mani):
                await ctx.send(f"❌ No manifest found for **{slug}**.")
                return
            with open(mani, "r", encoding="utf-8") as f:
                j = json.load(f)
            removed = 0
            for fn in j.get("files", []):
                p = os.path.join(MON_DIR, fn)
                if os.path.exists(p):
                    try: os.remove(p); removed += 1
                    except Exception: pass
            try: os.remove(mani)
            except Exception: pass
            await ctx.send(f"🗑️ Disbanded **{slug}**. Deleted {removed} templates.")
            return

        flags = {"-evil": False, "-sea": False}
        kw = {"-avg": None, "-name": None, "-seed": None, "-nonhuman": None}
        i = 0; tokens = list(opts)
        while i < len(tokens):
            t = tokens[i]
            if t in flags:
                flags[t] = True; i += 1; continue
            if t in kw and i+1 < len(tokens):
                kw[t] = tokens[i+1]; i += 2; continue
            i += 1

        if kw["-seed"] is not None:
            try: random.seed(int(str(kw["-seed"])))
            except Exception: pass

        slug = (kw["-name"] or f"{party_type}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}").replace(" ","_")
        avg = int(kw["-avg"] or 2)
        sea = bool(flags["-sea"])
        ptype = party_type.strip().lower()

        counts: dict[str, tuple[str,int,int]] = {}  

        def inc(key: str, cls: str, hd: int):
            if key in counts:
                ccls, chd, n = counts[key]
                counts[key] = (ccls, chd, n+1)
            else:
                counts[key] = (cls, hd, 1)

        if ptype == "adventurers":
            def lvl_knob():
                v = max(1, int(random.gauss(avg, 0.8)))
                if random.random() < 0.25: v = max(1, v-1)
                return v
            n_f = max(1, _roll("1d3"))
            n_t = max(1, _roll("1d2"))
            n_c = max(1, _roll("1d2"))
            n_m = max(0, _roll("1d2")-1)

            lf = lvl_knob()
            lt = lvl_knob()
            lc = lvl_knob()
            lm = lvl_knob() if n_m > 0 else None

            for _ in range(n_f): inc(f"fighter_l{lf}", "Fighter", lf)
            for _ in range(n_t): inc(f"thief_l{lt}",   "Thief",   lt)
            for _ in range(n_c): inc(f"cleric_l{lc}",  "Cleric",  lc)
            for _ in range(n_m): inc(f"mu_l{lm}",      "Magic-User", lm)

        elif ptype == "bandits":
            nf = _roll("2d12")
            nt = _roll("1d6")
            for _ in range(nf): inc("bandit", "Fighter", 1)
            for _ in range(nt): inc("thief",  "Thief",   1)
            lead_lv = _roll("1d4")+1
            inc("lieutenant", random.choice(["Fighter","Thief"]), lead_lv)
            if (nf + nt) >= 11 and random.random() < 0.5:
                inc("second", "Fighter" if counts["lieutenant"][0] == "Thief" else "Thief", _roll("1d4")+1)

        elif ptype in {"buccaneers","pirates"}:
            core = _roll("3d8")
            for _ in range(core): inc("sailor", "Fighter", 1)
            inc("captain", "Fighter", _roll("1d4")+2)
            nm = _roll("1d3")
            mate_hd = random.randint(2,5)
            for _ in range(nm): inc(f"mate_l{mate_hd}", "Fighter", mate_hd)

        elif ptype == "merchants":
            if sea:
                for _ in range(_roll("2d8")+8): inc("crew", "Fighter", 1)
                for _ in range(_roll("1d4")+2): inc("guard", "Fighter", 1)
                for _ in range(_roll("1d4")):    inc("guard_l2", "Fighter", 2)
                inc("captain", "Fighter", random.randint(2,4))
            else:
                if random.randint(0,1) == 0:
                    for _ in range(_roll("1d4")+1): inc("merchant", "Thief", 1)
                else:
                    inc("merchant", "Thief", 1)
                for _ in range(_roll("2d4")): inc("guard", "Fighter", 1)
                for _ in range(_roll("1d4")): inc("guard_l2", "Fighter", 2)

        elif ptype == "nobles":
            inc("noble", random.choice(["Fighter","Magic-User","Cleric","Thief"]), max(1, _roll("2d4")-1))
            if random.random() < 0.5:
                inc("spouse", random.choice(["Fighter","Magic-User","Cleric","Thief"]), max(1, _roll("2d4")-1))
            ng = max(2, _roll("1d4")+1)
            guard_hd = random.randint(1,4)
            for _ in range(ng): inc(f"guard_l{guard_hd}", "Fighter", guard_hd)
            inc("teamster", "Fighter", 1)

        elif ptype == "pilgrims":
            ncler = _roll("1d4")
            if ncler > 0:
                cler_hd = random.randint(1,4)
                for _ in range(ncler): inc(f"cleric_l{cler_hd}", "Cleric", cler_hd)
            for _ in range(_roll("3d6")): inc("pilgrim", "Thief", 1)  
            ng = _roll("1d6")
            if ng > 0:
                guard_hd = random.randint(1,4)
                for _ in range(ng): inc(f"guard_l{guard_hd}", "Fighter", guard_hd)
            ns = _roll("1d4")
            if ns > 0:
                scout_hd = random.randint(1,4)
                for _ in range(ns): inc(f"scout_l{scout_hd}", "Thief", scout_hd)
            if random.random() < 0.5:
                inc("acolyte_mu", "Magic-User", random.randint(1,4))

        else:
            await ctx.send("Types: adventurers, bandits, buccaneers, pirates, merchants, nobles, pilgrims")
            return

        files = []
        for key, (cls, hd, n) in counts.items():
            ac, armor, shield = _choose_armor_and_shield(ptype, key, cls)
            ranged_ok = not (cls in {"Cleric","Magic-User"})
            melee, ranged = _weapon_pack(cls, ranged_ok=ranged_ok)

            spell_block = None
            if cls in {"Cleric", "Magic-User"}:
                disp, cnts = _choose_spells_for(cls, hd)
                if disp:
                    spell_block = (disp, cnts)

            filebase = f"{slug}_{key}".lower()
            path = os.path.join(MON_DIR, f"{filebase}.ini")
            k = 2
            while os.path.exists(path):
                filebase = f"{slug}_{key}_{k}".lower()
                path = os.path.join(MON_DIR, f"{filebase}.ini")
                k += 1
            _write_template(
                path,
                ac=ac,
                hd=hd,
                save_class=cls,
                melee=melee,
                ranged=ranged,
                move=30,
                xp=_xp_for_hd(hd),
                type_str="humanoid",
                spell_block=spell_block
            )
            files.append((filebase, n))

        mani = {"slug": slug, "type": ptype, "files": [f"{fb}.ini" for (fb, _n) in files]}
        with open(os.path.join(MON_DIR, f"enc_{slug}.json"), "w", encoding="utf-8") as f:
            json.dump(mani, f, indent=2)

        total = sum(n for _fb, n in files)
        em = nextcord.Embed(
            title=f"🎲 NPC party created: {slug}",
            description=f"Type: **{party_type}** • members: **{total}**" + (" • mode: **sea**" if sea else ""),
            color=0x33AA77
        )
        lines = [f"!mon {fb} {n}" if n > 1 else f"!mon {fb}" for (fb, n) in files]
        em.add_field(name="Spawn into this battle", value=f"```text\n" + "\n".join(lines) + "\n```", inline=False)
        em.add_field(name="Control in combat", value="Use `!slam <Attacker> <Target> [attackname]`", inline=False)
        em.set_footer(text="Clean up later: !npcparty disband " + slug)
        await ctx.send(embed=em)

def setup(bot):
    bot.add_cog(NPC(bot))

