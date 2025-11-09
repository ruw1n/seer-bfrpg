# Seer Bot — Quick Guide for Players & GMs
_Last updated: 2025-11-09_

This project includes material from the Basic Fantasy Role-Playing Game © Chris Gonnerman & contributors,
used under CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/). Changes were made.
Basic Fantasy RPG is not affiliated with or endorsing this project.

Seer is a Discord bot that automates a lot of Basic Fantasy RPG (BFRPG) play: character sheets, combat, spells,
inventory/coins, exploration, lair treasure, strongholds, and more. This guide is split into two parts: a **Player Guide**
and a **GM Guide**. It ends with a compact **Cheat Sheet** you can drop into a server info channel.

---

## Conventions (read me first)

- **Active character**: most commands act on the character you’ve set with `!char`. If a command says “ACTIVE”, that’s what it means.
- **Fuzzy names**: you can type partial names; Seer tries to resolve them (`wizard` will match “Wizard the Wise”). If ambiguous, it suggests options.
- **Dice notation**: `XdY±Z` is understood (e.g., `2d6+3`).
- **Targets**: many combat/spell commands accept one or more targets (e.g., `!a longsword go1`, `!cast sleep gn1 gn2`). Target tokens like `go1` are your initiative slots.
- **Options**: many commands take flags, e.g. `-i` for “ignore costs/slots” (simulation/test), or `±N` modifiers on saves/skills.
- **Instances**: some items/scrolls have instance IDs like `scroll:abc123` or `StaffofPower@76f2b0`. You can reference them directly.
- **Permissions**: some commands are **GM-only**; a few are **Server-owner only**. The bot also respects the character ownership registry.

---

# Player Guide

## 1) Character basics
- Create and switch:  
  `!charcreate` — guided character creator → saves a `.coe` file.  
  `!char` — shows your characters and current active one.  
  `!char Testman` — sets active character (partial/fuzzy allowed).
- Portrait (optional):  
  `!portrait` — view; `!portrait <url>` — set portrait.
- Leveling and XP:  
  `!xp` — show current XP and thresholds (or adjust with shorthand if allowed).  
  `!addxp <N> <reason>` — add XP to ACTIVE (no auto-level).  
  `!levelup` — level up when eligible.

## 2) Gear, inventory, coins
- View bags & equipment:  
  `!bag` — shows carried storage & weights, and coin weight;  
  `!eq` — shows equipped items by slot.
- Add/remove/equip:  
  `!additem <Name> [qty]` — add to inventory;  
  `!carry <Name> [qty]` — add a misc carried item;  
  `!uncarry <slot|name>` — remove carried item;  
  `!equip <item>` — equip armor/shield/weapon/wearables;  
  `!unequip <slot|name>` — unequip.
- Buying/selling & transferring:  
  `!shop [filter]` — browse a catalog; `!buy <Name> [qty]`; `!sell <Name> [qty]`  
  `!give <item> [qty] <target>` — give an item to another PC.
- Coins:  
  `!coins` — view/modify your coins;  
  `!givecoin <pp|gp|ep|sp|cp> <qty> <target>` — transfer coins.

## 3) Core rolls & exploration
- Dice & checks:  
  `!r 2d6+1` — general roller;  
  `!c str` — ability check (roll-under);  
  `!s listen +2 [target]` — thief skills with optional mod and target name;  
  `!save <poi|wand|para|breath|spell> [±N] [target]` — saving throw (aliases accepted).
- Travel & dungeon procedures:  
  `!forage` — forage (no travel delay);  
  `!hunt` — hunting (takes the day);  
  `!door` — secret door search;  
  `!trap` — trap search;  
  `!nt <N>` — exploration mode: advance N turns (1 turn = 10 minutes).

## 4) Combat loop (player view)
- Joining and seeing order:  
  The GM runs `!init` and invites players to `!join`. Use `!list` to repost the tracker.
- Your turn basics:  
  `!a <weapon> [target(s)]` — attack; supports options like `short|long`, `-b ±N` to-hit, and `-d X` extra damage.  
  `!defend` — forego attacks for +4 AC until your next turn.  
  `!escape` — try to escape a wrestling hold.
- Special monster interactions (you might be prompted): _breath, gaze, stench, spore, spit, spray, slam_, etc. The GM usually triggers these.
- Conditions: the GM may set statuses (`!status`) like blinded, paralyzed, etc.

## 5) Spells & magic items
- Preparation (Vancian classes):  
  `!prepare <Spell>` — prepare one instance (checks slots by level);  
  `!sb` — show prepared spells and “bubble” slots;  
  `!unprepare <Spell>` — remove prepared instance;  
  `!lr` — long rest resets slots and certain per-day trackers;
  `!spells` — show spells by class (e.g. `!spells magic-user`).
- Casting:  
  `!cast <Spell> [targets...] [-i]` — cast; `-i` = simulate (don’t spend slot).  
  `!item <Name> [targets...]` — use a magic item that replicates a spell or AoE.  
  Potions/scrolls/wands/staves: `!potion`, `!read`, `!wand`, `!staff`, `!givescroll`, `!scrolls`, `!prunescrolls`.
- Charges & daily uses:  
  `!charges` — view or edit wand/staff charges on your character.

## 6) Strongholds & followers (high level)
- `!followers` — roll followers for a PC with an appropriate stronghold.  
- `!sh`, `!shcalc`, `!shdig` — stronghold utilities.

---

# GM Guide

## 1) Initiative & tracking
- Start & manage:  
  `!init` — start a tracker in the channel;  
  `!join` — players/PCs join;  
  `!list` — repost the pinned tracker;  
  `!p` / `!n` — go to previous/next actor;  
  `!end` — unpin + clear section + sweep leftovers.
- Status & timers:  
  `!status <name> <key>` — set/clear statuses (e.g., blinded, paralyzed);  
  `!ds <name> <status>` — **GM-only**: remove a status;  
  `!track <name> <label> <rounds>` — start a generic timer;  
  `!t` — ping exploration order.
- Healing & HP:  
  `!hp <±N>` — adjust ACTIVE PC HP or `!hp <name> <±N>` for any combatant;  
  `!heal <name>` — **GM-only**: refill to max HP;  
  `!rouse <name>` — rouse a sleeping/KO’d target to 1 HP (requires save).

## 2) Monsters & lairs
- Spawning & control:  
  `!mon <Type> [N]` — spawn N monsters from `monsters/<type>.ini` and auto-join;  
  `!remove <name1> [name2...]` — remove from tracker;  
  `!morale <name(s)>` — make a morale check;  
  `!msave <name> <poi|wand|para|breath|spell> [±N]` — **GM** roll saves for monsters;  
  `!mcast <name> <spell> [targets...]` / `!maddspell` — innate spells;  
  `!hd <name> <HD>` — set HD and adjust HP (supports `+/-` pips).
- Special abilities:  
  `!breath`, `!gaze`, `!stench`, `!spore`, `!spray`, `!thunderclap`, `!spin`, `!slam`, `!goo`, `!spit`, `!aoo` (Attacks of Opportunity), etc.  
  Use them with targets per the monster’s ability text; Seer handles saves, damage, and conditions when implemented.
- Visibility & forms:  
  `!invis <name> on|off` — **GM-only** invisibility in active battle;  
  `!unpoly <name>` — **GM-only**: remove polymorph overlays.

## 3) Treasure & loot
- Tracking per fight:  
  `!tally` — view/clear current XP & Treasure tally.  
  `!treasure <A..O> [count]` — generate lair hoards by type.  
  `!loot` — finalize all pending treasure at once.  
  `!treset` — reset treasure state for the channel.

## 4) Shops, crafting, downtime
- Shops: `!shop`, `!buy`, `!sell` with filters or categories.  
- Crafting: `!craft` — crafting browser & actions; `!craftdump` — generate a simplified craft catalog JSON (admin/helper use).
- Hiring: `!hire retainer [pc:<name>] [±N]` — resolves reactions and caps by CHA.

## 5) House rules & environment
- `!hr` — show/toggle house-rule switches for the channel (encumbrance, initiative extras, etc.).  
- `!nt <N>` — exploration-time advancement (turns).  
- `!traplist` — DM reference for common trap examples (sent via DM by default).

## 6) Admin/Safety
- `!ignite` — **GM-only**: ignite webs on a target.  
- `!unpoly`, `!invis` — **GM-only** battlefield effects.  
- `!zap <char>` — **Server-owner only**: delete a character `.coe` and unregister it. Use with care.

---

# Cheat Sheets

## Players — day-to-day
- Character: `!char`, `!charcreate`, `!levelup`, `!xp`, `!portrait`  
- Gear: `!bag`, `!eq`, `!equip`, `!unequip`, `!carry/!uncarry`, `!additem`, `!give`  
- Coins: `!coins`, `!givecoin`  
- Rolls: `!r 1d20+2`, `!c str`, `!s listen +2`, `!save breath +1`  
- Combat: `!a longsword go1`, `!defend`, `!escape`  
- Spells: `!prepare`, `!sb`, `!cast <spell> [targets] [-i]`, `!potion`, `!wand`, `!staff`, `!read`  
- Travel/Exploration: `!forage`, `!hunt`, `!door`, `!trap`, `!nt 6`

## GMs — round-to-round
- Tracker: `!init`, `!join`, `!list`, `!p`/`!n`, `!end`  
- Monsters: `!mon`, `!msave`, `!morale`, `!hd`, `!mcast`  
- Abilities: `!breath`, `!gaze`, `!stench`, `!spore`, `!spray`, `!slam`, `!spin`, `!thunderclap`  
- Status/HP: `!status`, `!ds`, `!hp`, `!heal`, `!rouse`, `!track`  
- Loot: `!treasure O 1`, `!tally`, `!loot`, `!treset`

---

# Tips & Notes

- **Research before fights**: `!info <Spell>` shows spellbook entries pulled from `spell.lst`.
- **Spell slots view**: `!sb` shows prepared spells and slot bubbles; `●` = available, `○` = spent.
- **Charges tracking**: `!charges` edits wand/staff charges; `!lr` resets per-day uses on many items and weapon “on‑hit” magics.
- **Weight & movement**: `!bag` shows gear vs coin weight and movement bands; STR and race affect thresholds.
- **Partial matching**: write _enough_ of a unique name, the bot figures out the rest. If ambiguous, it suggests options.
- **Simulation/testing**: add `-i` to many commands (e.g., `!cast ... -i`) to preview effects without spending resources.

---

If you need deeper mechanics (AC calculation quirks, stoneskin timers, special resistances, etc.), ask the bot `!help <command>` in‑server.
