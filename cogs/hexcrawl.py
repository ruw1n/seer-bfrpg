import random
import nextcord
from nextcord.ext import commands
import configparser

HEXCRAWL_FILE = "hexcrawl_state.lst"

def _hexes_per_day(move_ft: int) -> int:
    return max(1, move_ft // 10)

def _travel_mode_note(mode_key: str) -> str:
    if mode_key == "road":
        return "Marked roads are fastest, traveling at 3x speed."
    if mode_key == "offroad":
        return "Off-road travel is slower, traveling at 1x speed."
    if mode_key == "difficult":
        return "Difficult terrain is slowest, traveling at 0.5x speed."
    return "Use the terrain and action rules for the day’s movement."
    
def _hx_load_state():
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(HEXCRAWL_FILE)
    return cfg

def _hx_save_state(cfg):
    with open(HEXCRAWL_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)

def _hx_set_weather(chan_id: str, *, season: str, roll: int, desc: str, vis: int):
    cfg = _hx_load_state()
    if not cfg.has_section(chan_id):
        cfg.add_section(chan_id)
    cfg.set(chan_id, "weather_season", season)
    cfg.set(chan_id, "weather_roll", str(roll))
    cfg.set(chan_id, "weather_desc", desc)
    cfg.set(chan_id, "weather_vis", str(vis))
    _hx_save_state(cfg)

def _hx_get_weather(chan_id: str):
    cfg = _hx_load_state()
    if not cfg.has_section(chan_id):
        return None

    desc = cfg.get(chan_id, "weather_desc", fallback="").strip()
    if not desc:
        return None

    return {
        "season": cfg.get(chan_id, "weather_season", fallback=""),
        "roll": cfg.getint(chan_id, "weather_roll", fallback=0),
        "desc": desc,
        "vis": cfg.getint(chan_id, "weather_vis", fallback=0),
    }
    
LOST_TABLE = {
    1: "60° clockwise",
    2: "120° clockwise",
    3: "Opposite direction — turned around",
    4: "Opposite direction — turned around",
    5: "120° counterclockwise",
    6: "60° counterclockwise",
}

WEATHER_TABLES = {
    "summer": [
        ("Hot, clear", 0),
        ("Sweltering", 0),
        ("Overcast, muggy", 0),
        ("Stormy, thunder", 0),
        ("Gentle rain", 0),
        ("Baking, dry", 0),
        ("Low cloud, mist", -1),
        ("Warm wind", 0),
    ],
    "autumn": [
        ("Balmy, clement", 0),
        ("Frosty, chill", 0),
        ("Rolling fog", -2),
        ("Bracing wind", 0),
        ("Cloudy, misty", -1),
        ("Driving rain", -1),
        ("Brooding clouds", 0),
        ("Drizzle, damp", 0),
    ],
    "winter": [
        ("Clear, cold", 0),
        ("Frigid, icy", 0),
        ("Light snow", -1),
        ("Snow storm", -2),
        ("Frigid mist", -1),
        ("Freezing rain", -1),
        ("Bitter, silent", 0),
        ("Relentless wind", 0),
    ],
    "spring": [
        ("Clement, cheery", 0),
        ("Brisk, clear", 0),
        ("Windy, cloudy", 0),
        ("Warm, fresh", 0),
        ("Pouring rain", -1),
        ("Chilly, damp", 0),
        ("Gloomy", 0),
        ("Chill mist", -1),
    ],
}

EVENT_TABLES = {
    "road": [
        "Uneventful", "Uneventful", "Weather change", "Weather change",
        "Mishap / Hazard", "Spoor", "Spoor",
        "Encounter", "Encounter", "Encounter", "Encounter", "Location",
    ],
    "offroad": [
        "Uneventful", "Uneventful", "Weather change", "Weather change",
        "Mishap / Hazard", "Spoor",
        "Encounter", "Encounter", "Encounter",
        "Location", "Location", "Location",
    ],
    "difficult": [
        "Uneventful", "Uneventful", "Weather change", "Weather change",
        "Mishap / Hazard", "Mishap / Hazard", "Mishap / Hazard",
        "Spoor", "Encounter", "Encounter", "Location", "Location",
    ],
    "camp": [
        "Uneventful", "Uneventful", "Uneventful", "Uneventful", "Uneventful", "Uneventful",
        "Weather change", "Weather change",
        "Spoor", "Spoor",
        "Encounter", "Encounter",
    ],
}

LOCATION_TABLE = {
    1: "Lesser stone (d6-1 Viz)",
    2: "Strange tree (Boon/Hex)",
    3: "Tomb (dungeon d3 rooms)",
    4: "Shrine (Boon/Hex)",
    5: "Strange waters (Boon/Hex)",
    6: "Monument (Boon)",
    7: "Mysterious ruin (Hex)",
    8: "Minor lair (dungeon d6 rooms)",
}

MISHAP_TABLE = {
    1: "Lame horse / Rolled ankle — lose 10' movement for d3 days.",
    2: "Lost rations — lose d3 days of rations.",
    3: "Lost rations — lose d3 days of rations.",
    4: "Leaking water skin — lose 1 day of water.",
    5: "Leaking water skin — lose 1 day of water.",
    6: "Clumsy — take d6 damage.",
}

TERRAIN_ALIASES = {
    "forest": "forest",
    "woods": "forest",
    "wood": "forest",
    "grassland": "grassland",
    "plains": "grassland",
    "desert": "desert",
    "jungle": "jungle",
    "mountain": "mountain",
    "mountains": "mountain",
    "hill": "mountain",
    "hills": "mountain",
    "swamp": "swamp",
    "marsh": "swamp",
    "ocean": "ocean",
    "sea": "ocean",
}

MODE_ALIASES = {
    "road": "road",
    "onroad": "road",
    "on-road": "road",
    "offroad": "offroad",
    "off-road": "offroad",
    "wild": "offroad",
    "wilderness": "offroad",
    "difficult": "difficult",
    "rough": "difficult",
    "camp": "camp",
    "camping": "camp",
}

SEASON_ALIASES = {
    "spring": "spring",
    "summer": "summer",
    "autumn": "autumn",
    "fall": "autumn",
    "winter": "winter",
}

REWARD_HELP = [
    ("map", "25 XP per hex mapped"),
    ("clear", "100 XP per hex cleared of monsters"),
    ("donate", "1 XP per 1 gp donated"),
    ("ring6", "additional 600 XP, +5% cost match"),
    ("ring12", "additional 1200 XP, +5% cost match"),
    ("resourcepath", "+1% grant per hex to exploitable resources"),
    ("ruins", "500 XP per dungeon level, +1% grant per level"),
    ("ruinspath", "+1% grant per hex to ruins/dungeons"),
    ("tradepath", "+1% grant per hex to other towns"),
    ("domain", "10 XP × families"),
    ("death", "100 XP × dead PC level"),
    ("build", "table note only; no numeric XP/% in pasted chart"),
]

def _d(n: int) -> int:
    return random.randint(1, n)

def _season_key(raw: str | None) -> str | None:
    if not raw:
        return None
    return SEASON_ALIASES.get(raw.strip().lower())

def _mode_key(raw: str | None) -> str | None:
    if not raw:
        return None
    return MODE_ALIASES.get(raw.strip().lower())

def _terrain_key(raw: str | None) -> str | None:
    if not raw:
        return None
    return TERRAIN_ALIASES.get(raw.strip().lower())

def _vis_text(mod: int) -> str:
    if mod == 0:
        return "No visibility penalty."
    word = "Mist" if mod == -1 else "Fog" if mod == -2 else "Poor visibility"
    return f"{word}: {mod} to Wisdom checks to avoid getting lost."

def _format_pct(pct: float) -> str:
    if abs(pct - int(pct)) < 1e-9:
        return f"+{int(pct)}%"
    return f"+{pct:g}%"

class Hexcrawl(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _roll_weather(self, season: str):
        r = _d(8)
        desc, vis = WEATHER_TABLES[season][r - 1]
        return r, desc, vis

    def _roll_location(self):
        r = _d(8)
        result = LOCATION_TABLE[r]
        extras = []

        if "Boon/Hex" in result:
            bx = _d(6)
            if bx <= 2:
                extras.append(f"Boon/Hex d6 → **{bx}** → **Hex**: -1 to saving throws for 1 day.")
            else:
                extras.append(f"Boon/Hex d6 → **{bx}** → **Boon**: +1 to all saving throws for 1 day.")
        elif result.endswith("(Boon)"):
            extras.append("Automatic **Boon**: +1 to all saving throws for 1 day.")
        elif result.endswith("(Hex)"):
            extras.append("Automatic **Hex**: -1 to saving throws for 1 day.")
        elif "dungeon d3 rooms" in result:
            rooms = _d(3)
            extras.append(f"Dungeon size: d3 → **{rooms}** room(s).")
        elif "dungeon d6 rooms" in result:
            rooms = _d(6)
            extras.append(f"Dungeon size: d6 → **{rooms}** room(s).")

        return r, result, extras

    def _roll_mishap(self):
        r = _d(6)
        text = MISHAP_TABLE[r]
        extras = []
        if r == 1:
            dur = _d(3)
            extras.append(f"Duration: d3 → **{dur}** day(s).")
        elif r in {2, 3}:
            lost = _d(3)
            extras.append(f"Rations lost: d3 → **{lost}** day(s).")
        elif r == 6:
            dmg = _d(6)
            extras.append(f"Damage: d6 → **{dmg}**.")
        return r, text, extras

    @commands.command(name="weather")
    async def weather(self, ctx, season: str = None):
        raw = (season or "").strip().lower()

        if raw in {"current", "now", "last"}:
            wx = _hx_get_weather(str(ctx.channel.id))
            if not wx:
                await ctx.send("❌ No saved weather for this channel yet.")
                return

            embed = nextcord.Embed(
                title="🌦️ Current Weather",
                color=random.randint(0, 0xFFFFFF),
            )
            embed.add_field(name="Season", value=wx["season"].title(), inline=True)
            embed.add_field(name="d8", value=f"**{wx['roll']}**", inline=True)
            embed.add_field(name="Result", value=wx["desc"], inline=False)
            embed.add_field(name="Visibility", value=_vis_text(wx["vis"]), inline=False)
            await ctx.send(embed=embed)
            return

        key = _season_key(season)
        if not key:
            await ctx.send("❌ Usage: `!weather <spring|summer|autumn|winter>` or `!weather current`")
            return

        r, desc, vis = self._roll_weather(key)
        _hx_set_weather(str(ctx.channel.id), season=key, roll=r, desc=desc, vis=vis)

        embed = nextcord.Embed(
            title=f"🌦️ Weather — {key.title()}",
            color=random.randint(0, 0xFFFFFF),
        )
        embed.add_field(name="d8", value=f"**{r}**", inline=True)
        embed.add_field(name="Result", value=desc, inline=True)
        embed.add_field(name="Visibility", value=_vis_text(vis), inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="event")
    async def event(self, ctx, mode: str = None, season: str = None, terrain: str = None):
        """
        !event <road|offroad|difficult|camp> [season] [terrain]

        Examples:
          !event offroad autumn forest
          !event road summer
          !event camp winter swamp
        """
        mode_key = _mode_key(mode)
        saved_wx = _hx_get_weather(str(ctx.channel.id))
        season_key = _season_key(season) or (saved_wx["season"] if saved_wx else None)
        terr_key = _terrain_key(terrain)

        if not mode_key:
            await ctx.send("❌ Usage: `!event <road|offroad|difficult|camp> [season] [terrain]`")
            return

        r = _d(12)
        outcome = EVENT_TABLES[mode_key][r - 1]

        embed = nextcord.Embed(
            title=f"🧭 Hex Event — {mode_key.title()}",
            color=random.randint(0, 0xFFFFFF),
        )
        embed.add_field(name="d12", value=f"**{r}**", inline=True)
        embed.add_field(name="Outcome", value=outcome, inline=True)

        if outcome == "Weather change":
            if season_key:
                wr, desc, vis = self._roll_weather(season_key)
                embed.add_field(
                    name="Weather",
                    value=f"{season_key.title()} d8 → **{wr}** → **{desc}**\n{_vis_text(vis)}",
                    inline=False,
                )
                _hx_set_weather(str(ctx.channel.id), season=season_key, roll=wr, desc=desc, vis=vis)
            else:
                embed.add_field(
                    name="Weather",
                    value="No season is set for this channel yet. Use `!weather <season>` first, or pass a season into `!event`.",
                    inline=False,
                )

        elif outcome == "Location":
            lr, loc, extras = self._roll_location()
            val = f"d8 → **{lr}** → **{loc}**"
            if extras:
                val += "\n" + "\n".join(extras)
            embed.add_field(name="Location", value=val, inline=False)

        elif outcome == "Mishap / Hazard":
            mr, mishap, extras = self._roll_mishap()
            val = f"d6 → **{mr}** → {mishap}"
            if extras:
                val += "\n" + "\n".join(extras)
            embed.add_field(name="Mishap", value=val, inline=False)

        elif outcome == "Spoor":
            if terr_key:
                embed.add_field(
                    name="Spoor",
                    value=f"Roll your **{terr_key}** encounter table, but treat the result as signs only: tracks, droppings, markings, scraps, distant cries.",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Spoor",
                    value="Roll the appropriate encounter table, but reveal only signs of the creature.",
                    inline=False,
                )

        elif outcome == "Encounter":
            if terr_key:
                embed.add_field(
                    name="Encounter",
                    value=f"Use your normal **{terr_key}** encounter table now. Run `!e {terr_key}` for a normal wandering check, or `!e {terr_key} -f` to force one.",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Encounter",
                    value="Use the appropriate encounter table for location, activity, and time of day.",
                    inline=False,
                )

        await ctx.send(embed=embed)


    @commands.command(name="hexrewards")
    async def hexrewards(self, ctx, reward_key: str = None, amount: str = None, *args):
        """
        !hexrewards table
        !hexrewards map 3
        !hexrewards clear 2 -apply @user1 @user2
        !hexrewards ruins 4
        !hexrewards domain 28
        """
        key = (reward_key or "").strip().lower()

        if key in {"", "help", "table", "list", "?"}:
            embed = nextcord.Embed(
                title="🏰 Hex Rewards",
                description="Use `!hexrewards <key> <amount> [-apply @users...]`",
                color=random.randint(0, 0xFFFFFF),
            )
            for k, text in REWARD_HELP:
                embed.add_field(name=k, value=text, inline=False)
            embed.set_footer(text="Without -apply this is calculator-only. With -apply, XP is granted to mentioned users' active PCs.")
            await ctx.send(embed=embed)
            return

        apply = any(str(a).lower() == "-apply" for a in args)
        mentions = list(dict.fromkeys(ctx.message.mentions))
        target_users = mentions or ([ctx.author] if apply else [])

        if key == "build":
            embed = nextcord.Embed(
                title="🏰 Hex Rewards — Build",
                description="Your table says building a wilderness stronghold maintains Cleared Hex status in a 2-hex radius, but no numeric XP or grant % is listed.",
                color=random.randint(0, 0xFFFFFF),
            )
            await ctx.send(embed=embed)
            return

        if amount is None:
            await ctx.send("❌ Usage: `!hexrewards <key> <amount> [-apply @users...]` or `!hexrewards table`")
            return

        try:
            n = int(amount)
        except ValueError:
            await ctx.send("❌ Amount must be a whole number.")
            return

        xp_each = 0
        grant_pct = 0.0
        label = ""
        note = None

        if key == "map":
            label = f"Map {n} hex(es)"
            xp_each = 25 * n
        elif key == "clear":
            label = f"Clear {n} hex(es)"
            xp_each = 100 * n
        elif key == "donate":
            label = f"Donate {n} gp"
            xp_each = n
        elif key in {"ring6", "clear6"}:
            label = "Clear 6 hexes around town"
            xp_each = 600
            grant_pct = 5.0
        elif key in {"ring12", "clear12"}:
            label = "Clear 12 hexes around town"
            xp_each = 1200
            grant_pct = 5.0
        elif key == "resourcepath":
            label = f"Resource path ({n} hexes)"
            grant_pct = 1.0 * n
        elif key == "ruins":
            label = f"Historical ruins / dungeon ({n} level(s))"
            xp_each = 500 * n
            grant_pct = 1.0 * n
        elif key == "ruinspath":
            label = f"Path to ruins / dungeons ({n} hexes)"
            grant_pct = 1.0 * n
        elif key == "tradepath":
            label = f"Trade path ({n} hexes)"
            grant_pct = 1.0 * n
        elif key == "domain":
            label = f"Mitigate negative Domain Event ({n} families)"
            xp_each = 10 * n
        elif key == "death":
            label = f"Death of PC (level {n})"
            xp_each = 100 * n
            note = "Your pasted chart says 'see note below', so add your campaign-specific death rule on top."
        else:
            await ctx.send("❌ Unknown reward key. Use `!hexrewards table`.")
            return

        embed = nextcord.Embed(
            title="🏰 Hex Rewards",
            description=label,
            color=random.randint(0, 0xFFFFFF),
        )
        embed.add_field(name="XP (to each PC)", value=str(xp_each) if xp_each else "—", inline=True)
        embed.add_field(name="Stronghold Grant", value=_format_pct(grant_pct) if grant_pct else "—", inline=True)

        if note:
            embed.add_field(name="Note", value=note, inline=False)

        if apply and xp_each <= 0:
            embed.add_field(name="Apply", value="No XP to apply for this reward; this result is grant/track only.", inline=False)
            await ctx.send(embed=embed)
            return

        if apply:
            if not getattr(ctx.author.guild_permissions, "manage_guild", False):
                await ctx.send("❌ `-apply` requires **Manage Server**.")
                return

            prog = self.bot.get_cog("ProgressionCog") or self.bot.get_cog("Progression")
            if not prog or not hasattr(prog, "grant_xp_to_user_active"):
                embed.add_field(name="Apply", value="Progression cog not found; calculator result only.", inline=False)
                await ctx.send(embed=embed)
                return

            results = []
            for user in target_users:
                try:
                    res = await prog.grant_xp_to_user_active(
                        user.id,
                        xp_each,
                        reason=f"Hex reward: {label}",
                        apply_racial_bonus=True,
                        actor_id=ctx.author.id,
                    )
                    if res.get("ok"):
                        results.append(f"✅ {user.display_name} → **{res['grant']} XP** to **{res['char']}**")
                    else:
                        results.append(f"⚠️ {user.display_name} → {res.get('err', 'could not apply')}")
                except Exception as e:
                    results.append(f"⚠️ {user.display_name} → {type(e).__name__}: {e}")

            embed.add_field(
                name="Applied",
                value="\n".join(results) if results else "No targets.",
                inline=False,
            )

        await ctx.send(embed=embed)

    @commands.command(name="lost")
    async def lost(self, ctx):
        """
        Resolve the direction after the party has become lost.
        """
        r = _d(6)
        result = LOST_TABLE[r]

        embed = nextcord.Embed(
            title="🧭 Lost!",
            color=random.randint(0, 0xFFFFFF),
        )
        embed.add_field(name="d6", value=f"**{r}**", inline=True)
        embed.add_field(name="Direction", value=result, inline=True)
        embed.add_field(
            name="Procedure",
            value="The party travels 1 day in the wrong direction before noticing the error.",
            inline=False,
        )
        await ctx.send(embed=embed)


    @commands.command(name="camp")
    async def camp(self, ctx, terrain: str = None):
        terr_key = _terrain_key(terrain)
        if not terr_key:
            await ctx.send("❌ Usage: `!camp <terrain>`")
            return

        wx = _hx_get_weather(str(ctx.channel.id))

        pub = nextcord.Embed(
            title="🏕️ Camp",
            color=random.randint(0, 0xFFFFFF),
        )
        if wx:
            pub.add_field(
                name="Weather",
                value=f"{wx['desc']} • {_vis_text(wx['vis'])}",
                inline=False,
            )

        pub.add_field(
            name="Supplies",
            value="Consume 1 day of food per character and check water supply.",
            inline=False,
        )
        pub.add_field(
            name="Night",
            value="Three night watches will be rolled and sent to the GM by DM.",
            inline=False,
        )
        await ctx.send(embed=pub)

        try:
            await ctx.author.send(
                embed=nextcord.Embed(
                    title=f"🌙 Camp Watches — {terr_key.title()}",
                    description=(
                        "Running **3 wilderness wandering checks** for the night:\n"
                        "• First watch\n"
                        "• Second watch\n"
                        "• Third watch"
                    ),
                    color=random.randint(0, 0xFFFFFF),
                )
            )
        except nextcord.Forbidden:
            await ctx.send("⚠️ I couldn't DM you the night watches, so I stopped before revealing anything publicly.")
            return

        dice = self.bot.get_cog("Dice")
        if not dice or not hasattr(dice, "_encounter_wilderness"):
            await ctx.send("⚠️ Dice cog encounter helper not found.")
            return

        watch_labels = ["First Watch", "Second Watch", "Third Watch"]
        for label in watch_labels:
            try:
                await ctx.author.send(f"**{label}**")
                await dice._encounter_wilderness(ctx, terr_key, force=False, public=False)
            except nextcord.Forbidden:
                await ctx.send("⚠️ A watch result could not be DM'd, so I stopped instead of posting it publicly.")
                return
    @commands.command(name="mishap")
    async def mishap(self, ctx):
        mr, mishap, extras = self._roll_mishap()
        val = f"d6 → **{mr}** → {mishap}"
        if extras:
            val += "\n" + "\n".join(extras)

        embed = nextcord.Embed(
            title="⚠️ Mishap / Hazard",
            description=val,
            color=random.randint(0, 0xFFFFFF),
        )
        await ctx.send(embed=embed)
        

    @commands.command(name="hexday")
    async def hexday(self, ctx, slowest_move: str = None, mode: str = "offroad", people: str = None):
        """
        !hexday <slowest_move> [road|offroad|difficult] [people]
        Example:
          !hexday 30 road 5
          !hexday 20 difficult
        """
        try:
            move_ft = int(slowest_move)
        except Exception:
            await ctx.send("❌ Usage: `!hexday <slowest_move> [road|offroad|difficult] [people]`")
            return

        mode_key = _mode_key(mode)
        if mode_key not in {"road", "offroad", "difficult"}:
            await ctx.send("❌ Travel mode must be `road`, `offroad`, or `difficult`.")
            return

        party_size = None
        if people is not None:
            try:
                party_size = max(1, int(people))
            except Exception:
                party_size = None

        wx = _hx_get_weather(str(ctx.channel.id))
        base_hexes = _hexes_per_day(move_ft)

        embed = nextcord.Embed(
            title="🧭 Hex Day",
            color=random.randint(0, 0xFFFFFF),
        )

        if wx:
            embed.add_field(
                name="Weather",
                value=f"{wx['season'].title()} d8 → **{wx['roll']}** → **{wx['desc']}**\n{_vis_text(wx['vis'])}",
                inline=False,
            )
        else:
            embed.add_field(
                name="Weather",
                value="No weather saved yet. Run `!weather <season>` first.",
                inline=False,
            )

        embed.add_field(
            name="Movement",
            value=f"Slowest speed: **{move_ft}'**\nBase clear/open pace: **{base_hexes} hex(es)/day**",
            inline=False,
        )

        embed.add_field(
            name="Mode",
            value=_travel_mode_note(mode_key),
            inline=False,
        )

        steps = [
            "1. Start with `!weather <season>` if today’s weather is not already set.",
            "2. Determine whether the party is Traveling, Exploring (events), or Camping/Hunting/Foraging.",
            f"3. For each travel hex, use `!event {mode_key} <season> <terrain>` and roll encounters as needed.",
            "4. If off-road / trackless, check whether the party becomes lost. Apply visibility penalties to the Wisdom check.",
            "5. If the party chooses, run `!forage` or `!hunt <terrain>`.",
            "6. Resolve any `!mishap`, `!lost`, locations, or spoor results.",
            "7. Track food and water for the day.",
            "8. End with `!camp <terrain>`.",
        ]
        embed.add_field(name="Day Structure", value="\n".join(steps), inline=False)

        if party_size:
            embed.add_field(
                name="Supplies Reminder",
                value=f"Party size: **{party_size}**\nNeed **{party_size}** waterskins worth of water today.\nFood depends on whether you track food by day or by the ODT's 7-days-per-ration rule.",
                inline=False,
            )

        await ctx.send(embed=embed)

def setup(bot):
    bot.add_cog(Hexcrawl(bot))
