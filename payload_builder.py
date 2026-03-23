"""payload_builder.py — Pure Raidbots Droptimizer payload construction.

All functions here are pure (no network I/O, no file I/O).  The only runtime
dependency is the standard library plus the dataclasses already on ``sys.path``.
This makes the module trivially testable with fixture data captured once from
live Raidbots responses.

Typical usage
-------------
::

    from payload_builder import (
        CharacterIdentity, SimTarget, StaticData, build_payload,
    )
    from droptimizer import fetch_static_data

    static   = fetch_static_data(session)
    identity = CharacterIdentity(
        name="Xiantus", realm="illidan", region="us",
        spec_label="Fire", simc_string=simc,
    )
    target = SimTarget(
        difficulty="raid-heroic", instance_id=-91,
        spec_id=63, loot_spec_id=63,
    )
    payload = build_payload(identity, target, character_data, static)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static lookup tables (moved from droptimizer.py)
# ---------------------------------------------------------------------------

# Difficulty string → Raidbots bonus ID / upgrade track metadata.
# Hero track  = raid-heroic, group 611, 6/6 bonusId 12798
# Myth track  = raid-mythic, group 612, 6/6 bonusId 12806
DIFFICULTY_MAP: dict[str, dict[str, Any]] = {
    "raid-heroic": {
        "upgradeLevel": 12798,
        "levelSelectorSequence": 611,
        "itemLevel": "Hero",
        "season": "mid1",
    },
    "raid-mythic": {
        "upgradeLevel": 12806,
        "levelSelectorSequence": 612,
        "itemLevel": "Myth",
        "season": "mid1",
    },
}

# Virtual instance ID → real raid instance IDs it aggregates.
# -91 = TWW Season 1 Raids (all three raid instances combined).
VIRTUAL_INSTANCES: dict[int, list[int]] = {
    -91: [1307, 1308, 1314],
}


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CharacterIdentity:
    """Who is being simulated and which spec string Raidbots expects."""
    name:        str
    realm:       str
    region:      str
    spec_label:  str   # e.g. "Fire" — used in reportName and payload["spec"]
    simc_string: str   # full SimC APL text


@dataclass(frozen=True)
class SimTarget:
    """What simulation to run."""
    difficulty:    str         # "raid-heroic" | "raid-mythic"
    instance_id:   int = -91   # -91 = Season 1 virtual instance
    spec_id:       int = 63
    loot_spec_id:  int = 63
    fight_style:   str = "Patchwerk"
    iterations:    str = "smart"
    crafted_stats: str = "36/49"


@dataclass(frozen=True)
class StaticData:
    """Pre-fetched Raidbots static data needed by build_payload."""
    encounter_items:  list
    instances:        list
    frontend_version: str


# ---------------------------------------------------------------------------
# Internal helpers (previously in droptimizer.py)
# ---------------------------------------------------------------------------

def get_slot_name(inventory_type: int) -> str | None:
    """Map WoW inventoryType integer to a Raidbots slot name string."""
    return {
        1: "head",      2: "neck",      3: "shoulder",  4: "shirt",
        5: "chest",     6: "waist",     7: "legs",      8: "feet",
        9: "wrist",     10: "hands",    11: "finger1",  12: "trinket1",
        13: "trinket1", 14: "back",     15: "main_hand", 16: "back",
        17: "main_hand", 20: "chest",   21: "main_hand", 22: "off_hand",
        23: "off_hand", 28: "ranged",
    }.get(inventory_type)


def _build_droptimizer_items(
    encounter_items: list,
    instance_data:   dict,
    difficulty:      str,
    character:       dict,
    upgrade_info:    dict,
    all_instances:   list,
) -> list:
    """Build the droptimizerItems array for a single difficulty/instance."""
    upgrade_bonus_id   = upgrade_info["upgradeLevel"]
    level_selector_seq = upgrade_info["levelSelectorSequence"]
    item_level_name    = upgrade_info["itemLevel"]
    season             = upgrade_info["season"]
    class_id           = character.get("class", 8)

    virtual_instance_id = instance_data["id"]
    enc_list = instance_data.get("encounters", [])
    if not enc_list and virtual_instance_id in VIRTUAL_INSTANCES:
        sub_ids = set(VIRTUAL_INSTANCES[virtual_instance_id])
        enc_list = [
            enc
            for inst in all_instances
            if inst["id"] in sub_ids
            for enc in inst.get("encounters", [])
        ]
    virtual_encounter_ids   = {e["id"] for e in enc_list}
    virtual_encounter_order = {e["id"]: i for i, e in enumerate(enc_list)}

    encounter_to_real_instance: dict[int, int] = {}
    for inst in all_instances:
        if inst["id"] < 0:
            continue
        for enc in inst.get("encounters", []):
            if enc["id"] in virtual_encounter_ids:
                encounter_to_real_instance[enc["id"]] = inst["id"]

    log.info(
        "Resolved %d encounters across real instances: %s",
        len(encounter_to_real_instance),
        sorted(set(encounter_to_real_instance.values())),
    )

    result = []

    for item in encounter_items:
        sources   = item.get("sources", [])
        item_class = item.get("itemClass")
        inv_type   = item.get("inventoryType")

        if item_class not in (2, 4):
            continue
        slot = get_slot_name(inv_type)
        if not slot:
            continue

        matching_sources = [
            s for s in sources
            if s.get("encounterId") in virtual_encounter_ids
        ]
        if not matching_sources:
            continue

        allowed_classes = item.get("allowableClasses")
        if allowed_classes and class_id not in allowed_classes:
            continue

        seen = set()
        for src in matching_sources:
            enc_id = src["encounterId"]
            key = (item["id"], enc_id, slot)
            if key in seen:
                continue
            seen.add(key)

            seq_offset   = virtual_encounter_order.get(enc_id, 0)
            real_inst_id = encounter_to_real_instance.get(enc_id, virtual_instance_id)

            bonus_lists = [4799, 4786, upgrade_bonus_id]
            socket_info = item.get("socketInfo", {})
            has_socket = (
                isinstance(socket_info, dict) and
                any(
                    isinstance(v, dict) and v.get("staticSlots", 0) > 0
                    for v in socket_info.values()
                )
            )
            if has_socket:
                bonus_lists = [13668] + bonus_lists

            enchant_id = 0
            for eq_item in character.get("items", {}).values():
                if not isinstance(eq_item, dict):
                    continue
                if get_slot_name(eq_item.get("inventoryType", 0)) == slot:
                    enchant_id = eq_item.get("enchant_id") or 0
                    break

            real_instance_obj = next(
                (i for i in all_instances if i["id"] == real_inst_id),
                {"id": real_inst_id},
            )
            encounter_obj = next(
                (
                    e for i in all_instances if i["id"] == real_inst_id
                    for e in i.get("encounters", []) if e["id"] == enc_id
                ),
                {"id": enc_id},
            )

            entry = {
                "id": (
                    f"{real_inst_id}/{enc_id}/{difficulty}/{item['id']}/"
                    f"{item.get('itemLevel', 276)}/{enchant_id}/{slot}///"
                ),
                "slot": slot,
                "item": {
                    **{k: v for k, v in item.items() if k != "sources"},
                    "bonusLists":   bonus_lists,
                    "bonus_id":     "/".join(str(b) for b in bonus_lists),
                    "enchant_id":   enchant_id,
                    "gem_id":       "",
                    "instanceId":   real_inst_id,
                    "encounterId":  enc_id,
                    "difficulty":   difficulty,
                    "offSpecItem":  False,
                    "upgrade": {
                        "group":    level_selector_seq,
                        "level":    6,
                        "max":      6,
                        "name":     item_level_name,
                        "fullName": f"{item_level_name} 6/6",
                        "bonusId":  upgrade_bonus_id,
                        "itemLevel": item.get("itemLevel", 276),
                        "seasonId": 34,
                    },
                    "instance":    real_instance_obj,
                    "encounter":   encounter_obj,
                    "overrides": {
                        "encounterId":                  enc_id,
                        "encounterSequenceOffset":      seq_offset,
                        "instanceId":                   real_inst_id,
                        "difficulty":                   difficulty,
                        "itemLevel":                    item_level_name,
                        "levelSelectorSequence":        level_selector_seq,
                        "season":                       season,
                        "levelSelectorSetUpgradeTrack": True,
                        "seasonId":                     34,
                        "disableWarforgeLevel":         True,
                        "enableSockets":                True,
                        "itemConversion":               {"id": 12, "minLevel": 220},
                        "instance":                     real_instance_obj,
                        "encounter":                    encounter_obj,
                        "encounterType":                "boss",
                        "encounterTypePlural":          "bosses",
                        "quality":                      4,
                    },
                    "socketInfo":    item.get("socketInfo", {}),
                    "tooltipParams": {"enchant": enchant_id},
                },
            }
            result.append(entry)

    log.info(
        "Built %d droptimizerItems for instance %s %s",
        len(result), virtual_instance_id, difficulty,
    )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_payload(
    identity: CharacterIdentity,
    target:   SimTarget,
    character: dict,
    static:   StaticData,
) -> dict:
    """Build a Raidbots Droptimizer submission payload.

    This is a **pure function** — it performs no I/O and always produces the
    same output for the same inputs.

    Args:
        identity:  Character name/realm/region/spec/simc.
        target:    Simulation parameters (difficulty, spec IDs, etc.).
        character: Raw ``/wowapi/character`` response from Raidbots.
        static:    Pre-fetched encounter items, instances, frontend version.

    Returns:
        A ``dict`` ready to POST to ``/sim``.
    """
    upgrade_info = DIFFICULTY_MAP.get(target.difficulty, DIFFICULTY_MAP["raid-heroic"])

    instance_data = next(
        (i for i in static.instances if i["id"] == target.instance_id), {}
    )

    droptimizer_items = _build_droptimizer_items(
        static.encounter_items, instance_data, target.difficulty,
        character, upgrade_info, static.instances,
    )

    class_id = character.get("class", 8)
    faction  = "alliance" if character.get("faction", 0) == 0 else "horde"

    # Strip None and non-dict slots — Raidbots backend 500s on nil entries.
    clean_items = {
        k: v for k, v in character.get("items", {}).items()
        if isinstance(v, dict)
    }
    character = {**character, "items": clean_items}

    diff_label  = "Heroic" if target.difficulty == "raid-heroic" else "Mythic"
    report_name = (
        f"Droptimizer \u2022 Season 1 Raids \u2022 "
        f"{diff_label} \u2022 {upgrade_info['itemLevel']} 6/6"
    )

    return {
        "type":             "droptimizer",
        "text":             identity.simc_string,
        "baseActorName":    identity.name,
        "spec":             identity.spec_label,
        "armory":           {
            "region": identity.region,
            "realm":  identity.realm,
            "name":   identity.name,
        },
        "character":        character,
        "reportName":       report_name,
        "frontendHost":     "www.raidbots.com",
        "frontendVersion":  static.frontend_version,
        "iterations":       target.iterations,
        "fightStyle":       target.fight_style,
        "fightLength":      300,
        "enemyCount":       1,
        "enemyType":        "FluffyPillow",
        "bloodlust":        True,
        "arcaneIntellect":  True,
        "fortitude":        True,
        "battleShout":      True,
        "mysticTouch":      True,
        "chaosBrand":       True,
        "markOfTheWild":    True,
        "skyfury":          True,
        "bleeding":         True,
        "reportDetails":    True,
        "ptr":              False,
        "simcVersion":      "weekly",
        # Legacy/optional sim toggles — required by Raidbots backend
        "aberration":                      False,
        "apl":                             "",
        "astralAntennaMissChance":         10,
        "attunedToTheAether":              False,
        "augmentation":                    "",
        "balefireBranchRngType":           "constant",
        "blueSilkenLining":                40,
        "cabalistsHymnalInParty":          0,
        "corruptingRageUptime":            80,
        "covenantChance":                  100,
        "cruciblePredation":               True,
        "crucibleSustenance":              True,
        "crucibleViolence":                True,
        "dawnDuskThreadLining":            100,
        "disableIqdExecute":               False,
        "email":                           "",
        "enableDominationShards":          False,
        "enableRuneWords":                 False,
        "essenceGorgerHighStat":           False,
        "flask":                           "",
        "food":                            "",
        "frontendVersion":                 "aa117406d3c58c9dc83a0df039513166f66a640a",
        "gearsets":                        [],
        "huntersMark":                     True,
        "iqdStatFailChance":               0,
        "loyalToTheEndAllies":             0,
        "nazjatar":                        False,
        "nyalotha":                        True,
        "ocularGlandUptime":               100,
        "ominousChromaticEssenceAllies":   "",
        "ominousChromaticEssencePersonal": "obsidian",
        "potion":                          "",
        "powerInfusion":                   False,
        "primalRitualShell":               "wind",
        "rubyWhelpShellTraining":          "",
        "sendEmail":                       False,
        "smartAggressive":                 False,
        "smartHighPrecision":              True,
        "soleahStatType":                  "haste",
        "stoneLegionHeraldryInParty":      0,
        "surgingVitality":                 0,
        "symbioticPresence":               22,
        "talentSets":                      [],
        "talents":                         None,
        "temporaryEnchant":                "",
        "unboundChangelingStatType":       "",
        "undulatingTides":                 100,
        "voidRitual":                      False,
        "whisperingIncarnateIconRoles":    "dps/heal/tank",
        "worldveinAllies":                 0,
        "droptimizer": {
            "equipped":           character.get("items", {}),
            "instance":           target.instance_id,
            "difficulty":         target.difficulty,
            "warforgeLevel":      0,
            "upgradeLevel":       upgrade_info["upgradeLevel"],
            "upgradeEquipped":    False,
            "gem":                None,
            "classId":            class_id,
            "specId":             target.spec_id,
            "lootSpecId":         target.loot_spec_id,
            "faction":            faction,
            "craftedStats":       target.crafted_stats,
            "offSpecItems":       False,
            "includeConversions": True,
        },
        "droptimizerItems": droptimizer_items,
        "simOptions": {
            "fightstyle": target.fight_style,
            "iterations": target.iterations,
        },
    }
