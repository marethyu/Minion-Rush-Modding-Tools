#!/usr/bin/env python3
"""
Minion Rush Savegame Editor
===========================

Editor for the Windows Store build of Despicable Me: Minion Rush whose native
manager serializer headers are listed below.  The program decodes a selected
save into a small two-section JSON document, applies only explicitly supported
edits, rebuilds all checksums/encryption/redundant copies, and verifies the
result before replacing the selected save.

USAGE
-----

SAVEGAME INPUT
~~~~~~~~~~~~~~

Both ``decode`` and ``encode`` accept an optional savegame path::

    python minion_rush_save_editor.py decode SAVEGAME
    python minion_rush_save_editor.py encode SAVEGAME

If ``SAVEGAME`` is omitted, the editor falls back to the exact Windows Store
package family below; it does NOT scan ``Packages`` for other candidate
installations::

    PACKAGE_FAMILY_NAME = "MinionRushModded_t5wpntz2y2kfm"

The fallback save path is exactly::

    %LOCALAPPDATA%\\Packages\\MinionRushModded_t5wpntz2y2kfm\\LocalState\\savegame

``decode`` always writes ``savegame.json`` beside this script.  Edit only the
``editable`` section, then use ``encode`` with the same save file (or omit the
path again to use the LocalState fallback).

``encode`` uses the selected save file as the authoritative lossless baseline.
After the rebuilt save has passed verification, the editor first preserves the
ORIGINAL selected file as ``<selected-save-name>.bak`` and only then replaces
the selected file with the verified encoded result.  This applies equally to an
explicitly supplied ``SAVEGAME`` and to the LocalState fallback.  If the backup
already exists, it is refreshed with the original bytes from the current encode.
For example::

    python minion_rush_save_editor.py encode C:\\path\\to\\my_save

creates/refreshes::

    C:\\path\\to\\my_save.bak

before replacing ``C:\\path\\to\\my_save``.  For the LocalState fallback the
backup remains ``savegame.bak`` exactly as before.

Validate an arbitrary save without modifying it::

    python minion_rush_save_editor.py verify SAVEGAME

Require a lossless decode/rebuild cycle::

    python minion_rush_save_editor.py roundtrip SAVEGAME OUTPUT

Copy only ``SaveVerifierMgr`` from one save to another::

    python minion_rush_save_editor.py transplant-saveverifier DONOR TARGET OUTPUT

``transplant-saveverifier`` writes the rebuilt candidate beside ``OUTPUT`` as a
temporary file, fully decodes and validates THAT EXACT FILE, confirms that only
``SaveVerifierMgr`` changed, and then renames that already-verified file onto
``OUTPUT`` with ``os.replace``.  It does not serialize or rewrite the save again
after verification, so the bytes installed at ``OUTPUT`` are the bytes that
were actually verified.

MapMgr decoding/editing requires the current Jelly Lab catalogue schema.  Put
``jelly_lab_catalog.json`` beside this script or pass::

    --jelly-lab-catalog PATH

CLEAN JSON
----------

The user-facing JSON contains exactly::

    {
      "editable": { ... },
      "readonly": { ... }
    }

The selected save is re-read as the authoritative binary baseline during
encoding.  Managers and fields that are not projected into clean JSON remain
losslessly preserved.  Editing ``readonly`` is rejected.

SAVEGAME FILE FORMAT
--------------------

Physical layer: ``jet::stream::RedundantStream``.  For redundancy count N, the
native writer stores::

    Header * 2
    (PayloadMarker + LogicalPayload + Header * 2) * (N - 1)
    Header * 5
    PayloadMarker + LogicalPayload

where::

    Header        = marker[0xB0] + u32 crc32(LogicalPayload) + u32 payload_length
    PayloadMarker = marker[0x90]

The reader derives N and payload length from the physical size, uses replica
consensus for header/marker data, validates payload CRC32, and selects a unique
majority logical payload.  It does not locate replicas by marker scanning.

Logical payload::

    u8  0xED
    u32 encryption_flag
    if encryption_flag == 0:
        plaintext
    else:
        u32 encrypted_section_length
        u32 plaintext_length
        XTEA ciphertext

XTEA uses 32 rounds, little-endian 64-bit blocks, and the 16-byte XOR-folded
key derived from the recovered build string ``ERROR: invalid stream``.  The
decrypted/plain logical data is::

    u32 crc32(RecordDB bytes)
    RecordDB

Clara RecordDB::

    u32 record_count
    repeat record_count times:
        u16 name_length
        byte name[name_length]        # UTF-8
        u8  type
        u32 auxiliary
        type-dependent payload

Recovered RecordDB types are: 0 skipped/raw bytes, 1 bool/u32-like, 2 64-bit
integer-like, 3 32-bit integer-like, 4 float32, 5 float64, 6 UTF-8 string,
7 blob, and 8 nested RecordDB. Types 1-5 are preserved in their exact wire form;
the editor does not expose their still-provisional semantic signedness/float meaning
as a generic editing interface.

SUPPORTED MANAGERS
------------------

Editable:

* ``Player``: banana_count, token_count, minion_launchers, free_revives,
  base_despicable_multiplier, and named Perk*/IBooster* counters.  Wallet edits
  also update the terminal protected wallet-shadow fields.
* ``OnlineInventoryMgr``: named inventory item counts.
* ``EvilMinionMgr``: evil_minion_timer_seconds.
* ``MapMgr``: ``selected.area`` / ``selected.level_in_area``.  Forward selection
  promotes skipped prerequisite levels to three fruits using catalogue
  ``target_value3`` data; selecting an existing unlocked level does not roll
  progression backward.  The one-past-final coordinate is the supported Jelly
  Lab finish sentinel.  Native boundary saves where the progression coordinate
  remains one level behind the scalar/LevelState frontier are recognized safely.

Read-only projections:

* ``PrizePodMgr``
* ``BonusUpgradeMgr``
* ``CostumeMgr``
* ``MapMgr`` detailed progression/area state
* ``AchievementsMgr``
* ``statistics``
* ``SaveVerifierMgr``

Recognized native blob headers (serializer, signature)::

    Player               0x1C  0x00BB0002
    OnlineInventoryMgr   0x1B  0x00AA0001
    EvilMinionMgr        0x02  0x00AA0012
    PrizePodMgr          0x18  0xF0B0F0C4
    BonusUpgradeMgr      0x02  0x00CDE000
    CostumeMgr           0x1B  0x000AB006
    AchievementsMgr      0x02  0x00AA0003
    MapMgr               0x15  0xB00B00AC
    statistics           0x05  0x00AA0006

UNSUPPORTED / PRESERVED
-----------------------

Any RecordDB manager not listed above is opaque: it is preserved from the
current binary baseline but is neither decoded into clean JSON nor editable.
Observed opaque managers in the regression saves include::

    JellyJobMgr, EnergyMgr, LeaderboardMgr, ConflictsMgr, InventionMgr,
    messagesMgr, OnlineMissionMgr, randomUsersMgr, onlinePlayerData,
    StoryEventMgr, MissionMgr, TauntsMgr, friendsMgr, RegionMgr, TutorialMgr,
    FacebookAtLaunchMgr, anticheatingManager, BappleMgrSaveable, DailyLoginMgr,
    ChallengeMgr, ELORankingMgr, RedeemCodeMgr

That list is observational, not exhaustive. Unknown serializer revisions/signatures
of listed native blob managers are preserved but not guessed. The editor does not
parse JPK/BLIBCLARA assets itself, does not expose arbitrary raw manager fields for
editing, and does not support save formats from other Minion Rush platforms/builds.

LIMITATIONS
-----------

* Encoding requires the selected save file because opaque/unexposed data comes
  from that baseline.  When no save path is supplied, the selected file is the
  LocalState fallback described above.
* ``readonly`` must still match that baseline.  If the game changes only an
  editable value after JSON was decoded, encoding can intentionally overwrite it
  with the value still present in the JSON.
* MapMgr is omitted from clean JSON when the current Jelly Lab catalogue is
  absent or invalid.  Forward skip requires ``target_value3`` for every skipped
  level.
* The recovered XTEA key and native manager headers are specific to the supported
  Windows Store build.
* ``transplant-saveverifier`` rebuilds the requested output but does not create
  an automatic backup of its target.
* Data types of various values is not fully understood yet. Currently, we know that
  tokens, bananas, multiplier and score are treaed as signed integers.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HEADER_MARKER_SIZE = 0xB0
HEADER_SIZE = HEADER_MARKER_SIZE + struct.calcsize("<II")
PAYLOAD_MARKER_SIZE = 0x90
NATIVE_BLOB_HEADER_SIZE = struct.calcsize("<II")
COUNTED_BLOB_PREFIX_SIZE = NATIVE_BLOB_HEADER_SIZE + struct.calcsize("<I")
SAVE_MAGIC = 0xED
MAX_U32 = 0xFFFFFFFF
DEFAULT_KEY_SOURCE = b"ERROR: invalid stream"
XTEA_DELTA = 0x9E3779B9
XTEA_ROUNDS = 32
WALLET_SHADOW_ROTATION = 10
WALLET_SHADOW_XOR = 0x48607922


# Known identifier universe recovered from the supplied up23 designlib.blibclara.
# Decode zero-fills these names even when the profile has never serialized them.
_PERK_BASES = (
    "PerkBananaMultiplier",
    "PerkBattleMultiplier",
    "PerkJumpsMultiplier",
    "PerkMeterMultiplier",
    "PerkMissesMultiplier",
    "PerkPickupsMultiplier",
    "PerkScoreMultiplierMultiplier",
    "PerkSecondMultiplier",
    "PerkSlidesMultiplier",
    "PerkSmashMultiplier",
    "PerkStarMultiplier",
    "PerkUsesMultiplier",
)
KNOWN_PERK_NAMES = tuple(
    f"{base}_x{multiplier}"
    for base in _PERK_BASES
    for multiplier in (2, 3, 4, 6)
)

_BOOSTER_POWERUPS = (
    "Fluffy",
    "FreezeRay",
    "MegaMinion",
    "Moon",
    "Rocket",
    "Shield",
    "Splitter",
    "Vacuum",
)
KNOWN_BOOSTER_NAMES = tuple(
    f"IBooster_{family}_{powerup}"
    for family in ("First3", "SW")
    for powerup in _BOOSTER_POWERUPS
)
KNOWN_PLAYER_COUNTER_NAMES = frozenset(KNOWN_PERK_NAMES + KNOWN_BOOSTER_NAMES)

# Selected known native BonusUpgradeMgr entries.  This catalogue is based on
# entries observed in savegame(20260810-154441).  The clean read-only view zero-fills
# these known entries if absent from another profile.  Serialized names not in
# this catalogue are still preserved and displayed as well.
KNOWN_BONUS_UPGRADE_NAMES = frozenset({
    "Bonus_BMX",
    "Bonus_Banana_Sundae",
    "Bonus_BlindBoxPerks",
    "Bonus_Cannon",
    "Bonus_Diving",
    "Bonus_EvilMinion",
    "Bonus_Flappy",
    "Bonus_Fluffy",
    "Bonus_FreezeRay",
    "Bonus_GOLDEN_Banana",
    "Bonus_GOLDEN_Shield",
    "Bonus_Jetski",
    "Bonus_Kite",
    "Bonus_LargeMinion",
    "Bonus_Magnet",
    "Bonus_Moon",
    "Bonus_Mower",
    "Bonus_Motocross",
    "Bonus_Rocket",
    "Bonus_Roller",
    "Bonus_Scooter",
    "Bonus_Shield",
    "Bonus_Skateboard",
    "Bonus_Sled",
    "Bonus_Snowboard",
    "Bonus_Soccer",
    "Bonus_Submarine",
    "Bonus_UFO",
})

# PrizePodMgr category ids are the game's BlindBoxType enum values.
# User-facing names below are verified against controlled purchases in this
# build.  Internal RED (3) and AutoDelivery (6) are intentionally omitted
# from the clean user-facing catalogue but are preserved losslessly if they
# occur in a save.
PRIZE_POD_CATEGORY_INFO = {
    0:  {"name": "golden", "internal_type": "Golden"},
    1:  {"name": "silver", "internal_type": "Silver"},
    2:  {"name": "perks", "internal_type": "Perk"},
    4:  {"name": "chinese", "internal_type": "Chinese"},
    5:  {"name": "haunted_hustle", "internal_type": "StoryEvent"},
    7:  {"name": "carnival", "internal_type": "Featuring"},
    8:  {"name": "copper", "internal_type": "Copper"},
    9:  {"name": "blue", "internal_type": "Blueprint"},
    10: {"name": "mega_perks", "internal_type": "MegaPerks"},
    11: {"name": "costume_improver", "internal_type": "GoldenTickets"},
    12: {"name": "trick_or_treat", "internal_type": "Ghost"},
}

# PrizePodMgr is strictly read-only in clean JSON.  Per-entry lottery-type
# words are parsed only as opaque structural data needed to locate entries.

KNOWN_ONLINE_INVENTORY_NAMES = frozenset({
    "1STAR_3Pack_Pod_Award_Definition",
    "1STAR_3Pack_Pod_Award_Definition_2",
    "1STAR_3Pack_Pod_Shop_Definition",
    "1STAR_3Pack_Pod_Shop_Definition_2",
    "2STARS_3Pack_Pod_Award_Definition",
    "2STARS_3Pack_Pod_Award_Definition_2",
    "2STARS_3Pack_Pod_Shop_Definition",
    "2STARS_3Pack_Pod_Shop_Definition_2",
    "2STARS_6Pack_Pod_Award_Definition",
    "2STARS_6Pack_Pod_Award_Definition_2",
    "2STARS_Pod_Shop_Definition",
    "2STARS_Pod_Shop_Definition_2",
    "up17_Powerup_BeachBall",
    "up17_Powerup_BoomRocket",
    "up17_Powerup_CookieBot",
    "up17_Powerup_EnergySoda",
    "up17_Powerup_FartGun",
    "up17_Powerup_FlameThrower",
    "up17_Powerup_JellyGun",
    "up17_Powerup_LipstickTaser",
    "up18_Powerup_BlastGun",
    "up18_Powerup_JellyPump",
    "up20_Currency_BluePrint",
    "up20_Powerup_LaserPoles",
    "up20_Powerup_ShrinkRay",
    "up27_Currency_Golden_Ticket_Baby",
    "up27_Currency_Golden_Ticket_Boxer",
    "up27_Currency_Golden_Ticket_Cupid",
    "up27_Currency_Golden_Ticket_Firefighter",
    "up27_Currency_Golden_Ticket_Ninja",
    "up27_Currency_Golden_Ticket_Referee",
    "up27_Currency_Golden_Ticket_Skater",
    "up27_Currency_Golden_Ticket_Tourist",
    "up28_Currency_Golden_Ticket_Ghost",
})


class SaveError(Exception):
    pass


# Native serializer headers for manager blobs this editor understands.  These
# are wire-format constants for the supported native build.
NATIVE_BLOB_HEADERS: dict[str, tuple[int, int]] = {
    "Player": (0x1C, 0x00BB0002),
    "OnlineInventoryMgr": (0x1B, 0x00AA0001),
    "EvilMinionMgr": (0x02, 0x00AA0012),
    "PrizePodMgr": (0x18, 0xF0B0F0C4),
    "BonusUpgradeMgr": (0x02, 0x00CDE000),
    "CostumeMgr": (0x1B, 0x000AB006),
    "AchievementsMgr": (0x02, 0x00AA0003),
    "MapMgr": (0x15, 0xB00B00AC),
    "statistics": (0x05, 0x00AA0006),
}

EDITABLE_MANAGERS = frozenset({"Player", "OnlineInventoryMgr", "EvilMinionMgr", "MapMgr"})
READONLY_MANAGERS = frozenset({
    "PrizePodMgr",
    "BonusUpgradeMgr",
    "CostumeMgr",
    "MapMgr",
    "AchievementsMgr",
    "statistics",
    "SaveVerifierMgr",
})



@dataclass(frozen=True)
class PlayerPrefix:
    token_offset: int
    token_count: int
    banana_offset: int
    banana_count: int
    launcher_offset: int
    minion_launchers: int
    revive_offset: int
    free_revives: int
    multiplier_offset: int
    base_despicable_multiplier: int
    counter_offset: int




def _parse_player_prefix(blob: bytes) -> PlayerPrefix:
    """Walk the fixed Player prefix in native serialization order.

    This is the prefix emitted before the two variable-length Perk/IBooster
    maps. Offsets returned here are derived from the serializer grammar rather
    than maintained as independent magic constants.
    """
    _require_native_blob(blob, "Player")
    r = Reader(blob, name="Player")
    r.u32()  # serializer version
    r.u32()  # signature
    r.u32()  # protected player state

    token_offset = r.tell()
    token_count = r.u32()
    banana_offset = r.tell()
    banana_count = r.u32()

    r.u16()
    r.u16()
    r.u32()
    r.u32()

    launcher_offset = r.tell()
    minion_launchers = r.u32()
    r.u8()
    r.u32()
    revive_offset = r.tell()
    free_revives = r.u32()

    r.read(32)  # four serialized 64-bit fields
    r.u8()
    multiplier_offset = r.tell()
    base_despicable_multiplier = r.u32()
    r.u32()
    r.u8()

    return PlayerPrefix(
        token_offset=token_offset,
        token_count=token_count,
        banana_offset=banana_offset,
        banana_count=banana_count,
        launcher_offset=launcher_offset,
        minion_launchers=minion_launchers,
        revive_offset=revive_offset,
        free_revives=free_revives,
        multiplier_offset=multiplier_offset,
        base_despicable_multiplier=base_despicable_multiplier,
        counter_offset=r.tell(),
    )



def _evil_minion_timer(blob: bytes) -> tuple[int, int]:
    """Return the timer field offset/value by walking EvilMinionMgr's prefix."""
    _require_native_blob(blob, "EvilMinionMgr", min_size=12)
    r = Reader(blob, name="EvilMinionMgr")
    r.u32()  # serializer version
    r.u32()  # signature
    offset = r.tell()
    return offset, r.u32()

def _native_blob_header(blob: bytes, manager: str) -> tuple[int, int] | None:
    if len(blob) < 8:
        return None
    actual = struct.unpack_from("<II", blob, 0)
    return actual if actual == NATIVE_BLOB_HEADERS[manager] else None


def _require_native_blob(blob: bytes, manager: str, *, min_size: int = 8) -> None:
    if len(blob) < min_size:
        raise SaveError(f"{manager} blob is too small: {len(blob)} bytes")
    actual = struct.unpack_from("<II", blob, 0)
    expected = NATIVE_BLOB_HEADERS[manager]
    if actual != expected:
        raise SaveError(
            f"unsupported {manager} serializer: "
            f"version=0x{actual[0]:X}, signature=0x{actual[1]:08X}; "
            f"expected version=0x{expected[0]:X}, signature=0x{expected[1]:08X}"
        )


# Known achievements recovered from the supplied up23 designlib.blibclara.
KNOWN_ACHIEVEMENT_NAMES = (
    '002_dm_Despicable_you',
    '003_dm_HeadStart',
    '004_dm_TryAgain',
    '005_dm_rocketeer',
    '006_dm_ItsSoFluffy',
    '007_dm_MegaMinion',
    '008_dm_Forge_Ahead',
    '009_dm_Banana',
    '010_db_Vector',
    '011_dm_Meena',
    '012_dm_CantTouchThis',
    '013_dm_ColdAsIce',
    '014_dm_challenger',
    '015_dm_MinionSmash',
    '017_dm_Unstoppable',
    '018_dm_MarathonMinion',
    '020_dm_PowerHungry',
    '021_dm_Macho',
    '022_dm_EvilEye',
    '024_dm_BeachWalk',
    '025_dm_SandSlaughter',
    '026_dm_WaterPark',
    '027_dm_ShoppingMarathon',
    '029_dm_Veteran',
    '030_dm_Hoarder',
    '032_dm_Downtown',
    '033_dm_Hitchhiker',
    '034_dm_StarHunter',
    '035_dm_Expert',
    '036_dm_Unwanted',
    '037_dm_AmuWalk',
    '039_dm_SpreadMayhem',
    '040_dm_DefeatVillaintriloquist',
    '041_dm_DieByRocket',
    '042_dm_DieByBus',
    '043_dm_DieByTotem',
    '044_dm_DieByBarrel',
    '045_dm_DieByNewspaperStand',
    '046_dm_DieByBillboard',
    '047_dm_DieByRocksPile',
    '048_dm_UnlockedAllPowerUps',
    '049_dm_FortuneWheelSpin',
    '050_dm_CollectAllFruits_1_Area',
    '051_dm_Unlocked_5_Areas',
    '052_dm_Unlocked_10_Areas',
    '053_dm_Unlocked_15_Areas',
    '054_dm_Unlocked_20_Areas',
    '055_dm_Unlocked_25_Areas',
    '056_dm_EyeDrones',
    '057_dm_Occupied',
    '058_dm_Slingshot',
    '059_dm_PointyPlants',
    '060_dm_ExoticMarathon',
    '061_dm_IntruderAlert',
    '062_dm_LostBlizzard',
    '063_dm_LoveWings',
    '064_dm_BigHouse',
    '065_dm_LargePockets',
    '066_dm_ProRider',
    '067_dm_SMRecruit',
    '068_dm_SMAgent',
    '069_dm_SMSpecialist',
    '070_dm_DeploymentCosts',
    '071_dm_Slopestyle',
    '072_dm_Radical',
    '073_dm_BikingXtreme',
    '074_dm_ScooterMania',
    '075_dm_Jetskills',
)

@dataclass(frozen=True)
class RedundantStreamLayout:
    header_marker: bytes
    payload_marker: bytes
    payload_length: int
    redundancy: int
    valid_copy_offsets: tuple[int, ...]


@dataclass(frozen=True)
class RedundantPayload:
    payload: bytes
    valid_copies: int


class Reader:
    def __init__(self, data: bytes, *, name: str = "buffer") -> None:
        self.data = data
        self.pos = 0
        self.name = name

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def tell(self) -> int:
        return self.pos

    def require(self, count: int) -> None:
        if count < 0 or self.pos + count > len(self.data):
            raise SaveError(
                f"{self.name}: need {count} bytes at 0x{self.pos:X}, "
                f"but only {self.remaining()} remain"
            )

    def read(self, count: int) -> bytes:
        self.require(count)
        out = self.data[self.pos:self.pos + count]
        self.pos += count
        return out

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]


def _redundant_offsets(redundancy: int, payload_length: int) -> tuple[list[int], list[int], list[int], int]:
    """Return native RedundantStream header, marker, and payload offsets."""
    pos = 0
    headers: list[int] = []
    markers: list[int] = []
    payloads: list[int] = []

    def header() -> None:
        nonlocal pos
        headers.append(pos)
        pos += HEADER_SIZE

    for _ in range(2):
        header()
    for _ in range(redundancy - 1):
        markers.append(pos)
        pos += PAYLOAD_MARKER_SIZE
        payloads.append(pos)
        pos += payload_length
        header()
        header()
    for _ in range(5):
        header()
    markers.append(pos)
    pos += PAYLOAD_MARKER_SIZE
    payloads.append(pos)
    pos += payload_length
    return headers, markers, payloads, pos


def _unique_mode(values: list[Any]) -> tuple[Any, int] | None:
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    winners = [value for value, count in counts.items() if count == best]
    if len(winners) != 1:
        return None
    return winners[0], best


def _parse_redundant_stream_layout(data: bytes) -> RedundantStreamLayout:
    """Recover RedundantStream layout from structure and replica consensus.

    The native writer emits, for redundancy N >= 1::

        header * 2
        (payload_marker + payload + header * 2) * (N - 1)
        header * 5
        payload_marker + payload

    A header is ``marker[0xB0] + crc32 + payload_length`` and a payload marker is
    0x90 bytes.  N and payload length are solved from the physical file size; no
    marker scanning or first-replica trust is used.  Canonical header fields and
    marker bytes come from replica consensus, so a damaged first header or first
    payload marker does not defeat the redundancy layer.
    """
    numerator = len(data) - 5 * HEADER_SIZE
    if numerator <= 0:
        raise SaveError("file is too small to contain a native RedundantStream wrapper")

    candidates: list[tuple[tuple[int, int, int], RedundantStreamLayout]] = []
    divisors: set[int] = set()
    limit = math.isqrt(numerator)
    for divisor in range(1, limit + 1):
        if numerator % divisor == 0:
            divisors.add(divisor)
            divisors.add(numerator // divisor)

    for redundancy in sorted(d for d in divisors if d >= 1):
        unit = numerator // redundancy
        payload_length = unit - 2 * HEADER_SIZE - PAYLOAD_MARKER_SIZE
        if not 0 < payload_length <= MAX_U32:
            continue

        header_offsets, marker_offsets, copy_offsets, end = _redundant_offsets(
            redundancy, payload_length
        )
        if end != len(data):
            continue

        header_records = [
            (
                data[offset:offset + HEADER_MARKER_SIZE],
                *struct.unpack_from("<II", data, offset + HEADER_MARKER_SIZE),
            )
            for offset in header_offsets
        ]
        header_mode = _unique_mode(header_records)
        if header_mode is None:
            continue
        (header_marker, stored_crc32, stored_length), matching_headers = header_mode
        if stored_length != payload_length or matching_headers <= len(header_offsets) // 2:
            continue

        crc_valid_indexes = [
            index
            for index, offset in enumerate(copy_offsets)
            if zlib.crc32(data[offset:offset + payload_length]) & MAX_U32 == stored_crc32
        ]
        if not crc_valid_indexes:
            continue

        payload_mode = _unique_mode([
            data[marker_offsets[index]:marker_offsets[index] + PAYLOAD_MARKER_SIZE]
            for index in crc_valid_indexes
        ])
        if payload_mode is None:
            continue
        payload_marker, matching_markers = payload_mode
        valid_copy_offsets = tuple(
            copy_offsets[index]
            for index in crc_valid_indexes
            if data[marker_offsets[index]:marker_offsets[index] + PAYLOAD_MARKER_SIZE]
            == payload_marker
        )
        if not valid_copy_offsets:
            continue

        layout = RedundantStreamLayout(
            header_marker=header_marker,
            payload_marker=payload_marker,
            payload_length=payload_length,
            redundancy=redundancy,
            valid_copy_offsets=valid_copy_offsets,
        )
        score = (matching_headers, len(valid_copy_offsets), matching_markers)
        candidates.append((score, layout))

    if not candidates:
        raise SaveError("file does not match a recoverable native RedundantStream layout")
    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        raise SaveError("RedundantStream layout is ambiguous")
    return candidates[0][1]


def extract_redundant_payload(data: bytes) -> RedundantPayload:
    """Return the majority CRC-valid logical payload from RedundantStream."""
    layout = _parse_redundant_stream_layout(data)
    groups: dict[bytes, list[int]] = {}
    for offset in layout.valid_copy_offsets:
        payload = data[offset:offset + layout.payload_length]
        groups.setdefault(payload, []).append(offset)
    best = max(len(offsets) for offsets in groups.values())
    winners = [(payload, offsets) for payload, offsets in groups.items() if len(offsets) == best]
    if len(winners) != 1:
        raise SaveError("RedundantStream payload replicas disagree without a unique majority")
    payload, offsets = winners[0]
    return RedundantPayload(payload=payload, valid_copies=len(offsets))

def derive_xtea_key(source: bytes) -> bytes:
    """
    Reproduce FUN_00503110:
        key = 16 zero bytes
        key[i & 15] ^= source[i]
    """
    key = bytearray(16)
    for i, byte in enumerate(source):
        key[i & 0x0F] ^= byte
    return bytes(key)


DEFAULT_XTEA_KEY = derive_xtea_key(DEFAULT_KEY_SOURCE)


def xtea_decrypt_block(block: bytes, key: bytes) -> bytes:
    if len(block) != 8:
        raise ValueError("XTEA block must be exactly 8 bytes")
    if len(key) != 16:
        raise ValueError("XTEA key must be exactly 16 bytes")

    v0, v1 = struct.unpack("<2I", block)
    k = struct.unpack("<4I", key)
    total = (XTEA_DELTA * XTEA_ROUNDS) & MAX_U32

    for _ in range(XTEA_ROUNDS):
        v1 = (
            v1
            - (
                (((v0 << 4) ^ (v0 >> 5)) + v0)
                ^ ((total + k[(total >> 11) & 3]) & MAX_U32)
            )
        ) & MAX_U32
        total = (total - XTEA_DELTA) & MAX_U32
        v0 = (
            v0
            - (
                (((v1 << 4) ^ (v1 >> 5)) + v1)
                ^ ((total + k[total & 3]) & MAX_U32)
            )
        ) & MAX_U32

    return struct.pack("<2I", v0, v1)


def xtea_decrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % 8:
        raise SaveError(
            f"encrypted XTEA area has length {len(data)}, not a multiple of 8"
        )
    return b"".join(
        xtea_decrypt_block(data[offset:offset + 8], key)
        for offset in range(0, len(data), 8)
    )


def unwrap_inner_payload(payload: bytes, key: bytes) -> bytes:
    """Validate/decrypt the logical payload and return its plaintext bytes."""
    r = Reader(payload, name="logical payload")
    marker = r.u8()
    if marker != SAVE_MAGIC:
        raise SaveError(
            f"unexpected logical-stream marker 0x{marker:02X}; expected 0x{SAVE_MAGIC:02X}"
        )

    encryption_flag = r.u32()
    if encryption_flag == 0:
        return r.read(r.remaining())

    section_length = r.u32()
    if section_length < 4 or section_length != r.remaining():
        raise SaveError(
            f"encrypted section length {section_length} does not match "
            f"the {r.remaining()} bytes remaining in the logical payload"
        )
    plaintext_length = r.u32()
    ciphertext = r.read(section_length - 4)
    decrypted = xtea_decrypt(ciphertext, key)
    if plaintext_length > len(decrypted):
        raise SaveError(
            f"decrypted buffer is {len(decrypted)} bytes, but header requests {plaintext_length}"
        )
    padding = decrypted[plaintext_length:]
    if not 1 <= len(padding) <= 8 or any(padding):
        raise SaveError("encrypted payload has invalid native zero padding")
    return decrypted[:plaintext_length]


def decode_text(raw: bytes) -> str:
    """Decode native length-prefixed strings from the supported build."""
    return raw.decode("utf-8")


def read_record_string(r: Reader) -> str:
    length = r.u16()
    if length == 0:
        return ""
    return decode_text(r.read(length))


def parse_record_value(
    r: Reader,
    *,
    depth: int,
    max_depth: int,
) -> dict[str, Any]:
    if depth > max_depth:
        raise SaveError(f"RecordDB nesting exceeds maximum depth {max_depth}")

    value_offset = r.tell()
    type_id = r.u8()
    aux = r.u32()

    result: dict[str, Any] = {"_type": type_id, "_aux": aux}

    if type_id == 0:
        # Decompiler seeks forward by aux bytes.
        result["data"] = r.read(aux)

    elif type_id in (1, 2, 3, 4, 5):
        size = {1: 4, 2: 8, 3: 4, 4: 4, 5: 8}[type_id]
        result["data"] = r.read(size)

    elif type_id == 6:
        result["value"] = read_record_string(r)

    elif type_id == 7:
        result["data"] = r.read(aux)

    elif type_id == 8:
        result["value"] = parse_record_db(
            r,
            depth=depth + 1,
            max_depth=max_depth,
        )

    else:
        raise SaveError(
            f"unknown RecordDB type {type_id} at offset 0x{value_offset:X}"
        )

    return result


def parse_record_db(
    r: Reader,
    *,
    depth: int = 0,
    max_depth: int = 64,
) -> dict[str, Any]:
    start = r.tell()
    count = r.u32()
    # Even an empty-name record needs u16 name length + u8 type + u32 aux.
    max_possible = r.remaining() // 7
    if count > max_possible:
        raise SaveError(
            f"RecordDB count {count} cannot fit in {r.remaining()} remaining bytes "
            f"at offset 0x{start:X}"
        )

    records: dict[str, Any] = {}
    duplicates: dict[str, list[Any]] = {}

    for index in range(count):
        name = read_record_string(r)
        value = parse_record_value(
            r,
            depth=depth,
            max_depth=max_depth,
        )
        value["_record_index"] = index

        if name in records:
            duplicates.setdefault(name, [records[name]]).append(value)
        else:
            records[name] = value

    out: dict[str, Any] = {"_record_count": count, "records": records}
    if duplicates:
        out["_duplicates"] = duplicates
    return out


def split_and_validate_recorddb_plaintext(plaintext: bytes) -> tuple[bytes, int]:
    """
    Decrypted save data begins with:
        u32 stored_crc32
        u8  recorddb_bytes[...]

    The checksum is CRC32 over all bytes after the checksum field.
    """
    if len(plaintext) < 8:
        raise SaveError("decrypted buffer is too small for checksum + RecordDB")

    stored_crc = struct.unpack_from("<I", plaintext, 0)[0]
    recorddb = plaintext[4:]
    actual_crc = zlib.crc32(recorddb) & MAX_U32
    if actual_crc != stored_crc:
        raise SaveError(
            "inner RecordDB CRC32 mismatch: "
            f"stored=0x{stored_crc:08X}, actual=0x{actual_crc:08X}; "
            "the key may be wrong or the file may be damaged"
        )
    return recorddb, stored_crc


def _record_blob(records: dict[str, Any], name: str) -> bytes | None:
    """Return a RecordDB blob, or None when the record is absent/invalid."""
    record = records.get(name)
    if not isinstance(record, dict) or record.get("_type") != 7:
        return None
    data = record.get("data")
    return data if isinstance(data, bytes) else None


def _parse_prize_pod_manager(prize_raw: bytes) -> dict[str, Any]:
    """Parse PrizePodMgr's categorized pending pod/reward collections."""
    if len(prize_raw) < 12:
        return {}
    try:
        version, signature, category_count = struct.unpack_from("<III", prize_raw, 0)
    except struct.error:
        return {}
    if (version, signature) != NATIVE_BLOB_HEADERS["PrizePodMgr"]:
        return {}

    pos = COUNTED_BLOB_PREFIX_SIZE
    if category_count > (len(prize_raw) - pos) // 8:
        return {}
    categories: dict[str, dict[str, Any]] = {}
    try:
        for _ in range(category_count):
            if pos + 8 > len(prize_raw):
                return {}
            category_id, entry_count = struct.unpack_from("<II", prize_raw, pos)
            pos += 8
            if entry_count > (len(prize_raw) - pos) // 7:
                return {}
            entries: list[dict[str, Any]] = []
            for index in range(entry_count):
                if pos + 2 > len(prize_raw):
                    return {}
                name_len = struct.unpack_from("<H", prize_raw, pos)[0]
                pos += 2
                if name_len == 0 or pos + name_len + 4 > len(prize_raw):
                    return {}
                name = decode_text(prize_raw[pos:pos + name_len])
                pos += name_len
                pos += 4  # BlindBoxLotteryType; read-only projection does not expose it.
                entries.append({"index": index, "prize": name})
            categories[str(category_id)] = {"entries": entries}
    except (struct.error, UnicodeError):
        return {}
    return {"categories": categories}


# PrizePodMgr intentionally has no clean-JSON writer.

def _read_named_u32_map(
    data: bytes,
    count_offset: int,
    *,
    expected_prefix: str,
    label: str,
) -> tuple[list[tuple[str, int]], int]:
    """Read a counted u16-string -> u32 map and return entries plus end offset."""
    if count_offset + 4 > len(data):
        raise SaveError(f"{label} is truncated before its count")
    count = struct.unpack_from("<I", data, count_offset)[0]
    pos = count_offset + 4
    if count > (len(data) - pos) // 7:
        raise SaveError(f"{label} count {count} cannot fit in the remaining blob")
    entries: list[tuple[str, int]] = []
    seen: set[str] = set()
    for index in range(count):
        if pos + 2 > len(data):
            raise SaveError(f"{label} entry {index} is truncated before name length")
        name_len = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if name_len == 0 or pos + name_len + 4 > len(data):
            raise SaveError(f"{label} entry {index} has invalid name/value bounds")
        try:
            name = data[pos:pos + name_len].decode("utf-8")
        except UnicodeError as exc:
            raise SaveError(f"{label} entry {index} has invalid UTF-8 name") from exc
        pos += name_len
        if not name.startswith(expected_prefix):
            raise SaveError(
                f"{label} entry {index} has unexpected name {name!r}; "
                f"expected prefix {expected_prefix!r}"
            )
        if name in seen:
            raise SaveError(f"{label} contains duplicate name {name!r}")
        seen.add(name)
        value = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        entries.append((name, value))
    return entries, pos


def _encode_named_u32_map(entries: list[tuple[str, int]], *, label: str) -> bytes:
    out = bytearray(struct.pack("<I", len(entries)))
    for name, value in entries:
        raw = name.encode("utf-8")
        if not raw or len(raw) > 0xFFFF:
            raise SaveError(f"{label} name {name!r} has invalid encoded length")
        out += struct.pack("<H", len(raw)) + raw
        out += struct.pack("<I", checked_u32(value, f"{label}.{name}"))
    return bytes(out)


def _parse_player_counter_collections(
    player_raw: bytes, prefix: PlayerPrefix | None = None
) -> tuple[list[tuple[str, int]], list[tuple[str, int]], int]:
    """Parse the two consecutive Player counter maps.

    Confirmed layout in the supplied saves::

        fixed Player prefix, then u32 perk_count
        repeated perk_count: u16 name_len, name, u32 value
        then: u32 booster_count
        repeated booster_count: u16 name_len, name, u32 value
        then: remaining Player fields
    """
    if prefix is None:
        prefix = _parse_player_prefix(player_raw)
    perks, after_perks = _read_named_u32_map(
        player_raw, prefix.counter_offset, expected_prefix="Perk", label="Player Perk map"
    )
    boosters, after_boosters = _read_named_u32_map(
        player_raw, after_perks, expected_prefix="IBooster", label="Player IBooster map"
    )
    return perks, boosters, after_boosters


def _rebuild_player_perk_counters(player_raw: bytes, requested: dict[str, Any]) -> tuple[bytes, dict[str, int]]:
    """Patch existing counters and materialize nonzero known absent counters."""
    if not isinstance(requested, dict):
        raise SaveError("editable.Player.perk_counters must be an object")

    prefix = _parse_player_prefix(player_raw)
    perks, boosters, tail_offset = _parse_player_counter_collections(player_raw, prefix)
    serialized_names = {name for name, _ in perks + boosters}
    expected = serialized_names | KNOWN_PLAYER_COUNTER_NAMES
    supplied = set(requested)
    if supplied != expected:
        missing = sorted(expected - supplied)
        unknown = sorted(supplied - expected)
        raise SaveError(
            "perk/booster names must match the known designlib universe plus serialized Player entries; "
            f"missing={missing}, unknown={unknown}"
        )

    old_perk_names = {name for name, _ in perks}
    old_booster_names = {name for name, _ in boosters}
    new_perks = [
        (name, checked_u32(requested[name], f"editable.Player.perk_counters.{name}"))
        for name, _ in perks
    ]
    new_boosters = [
        (name, checked_u32(requested[name], f"editable.Player.perk_counters.{name}"))
        for name, _ in boosters
    ]

    added_perks = 0
    added_boosters = 0
    for name in sorted(KNOWN_PERK_NAMES):
        if name not in old_perk_names:
            value = checked_u32(requested[name], f"editable.Player.perk_counters.{name}")
            if value != 0:
                new_perks.append((name, value))
                added_perks += 1
    for name in sorted(KNOWN_BOOSTER_NAMES):
        if name not in old_booster_names:
            value = checked_u32(requested[name], f"editable.Player.perk_counters.{name}")
            if value != 0:
                new_boosters.append((name, value))
                added_boosters += 1

    rebuilt = (
        player_raw[:prefix.counter_offset]
        + _encode_named_u32_map(new_perks, label="Player.perk_counters")
        + _encode_named_u32_map(new_boosters, label="Player.perk_counters")
        + player_raw[tail_offset:]
    )
    return rebuilt, {
        "serialized_before": len(perks) + len(boosters),
        "serialized_after": len(new_perks) + len(new_boosters),
        "added_perks": added_perks,
        "added_boosters": added_boosters,
    }


def _online_inventory_layout(inventory_raw: bytes) -> tuple[list[tuple[str, int]], int]:
    _require_native_blob(
        inventory_raw, "OnlineInventoryMgr", min_size=COUNTED_BLOB_PREFIX_SIZE
    )
    entries, end = _read_named_u32_map(
        inventory_raw, NATIVE_BLOB_HEADER_SIZE, expected_prefix="", label="OnlineInventoryMgr items"
    )
    return entries, end


def _online_inventory_item_is_hidden(name: str) -> bool:
    # Preserve serializer-only/obsolete entries without exposing them in clean JSON.
    # Any unrecognized simple up28 currency is treated the same way.
    return name.endswith("_unused") or (
        name.startswith("up28_Currency_") and name not in KNOWN_ONLINE_INVENTORY_NAMES
    )


def _rebuild_online_inventory_items(inventory_raw: bytes, requested: dict[str, Any]) -> tuple[bytes, dict[str, int]]:
    """Patch existing inventory quantities and materialize known absent nonzero items."""
    if not isinstance(requested, dict):
        raise SaveError("editable.OnlineInventoryMgr.items must be an object")
    entries, tail_offset = _online_inventory_layout(inventory_raw)
    serialized_names = {name for name, _ in entries}
    visible_serialized_names = {name for name in serialized_names if not _online_inventory_item_is_hidden(name)}
    expected = visible_serialized_names | KNOWN_ONLINE_INVENTORY_NAMES
    supplied = set(requested)
    if supplied != expected:
        missing = sorted(expected - supplied)
        unknown = sorted(supplied - expected)
        raise SaveError(
            "OnlineInventoryMgr item names must match the known designlib upXX_/xSTAR(S)_ "
            f"universe plus serialized entries; missing={missing}, unknown={unknown}"
        )

    rebuilt_entries = []
    for name, original_value in entries:
        if _online_inventory_item_is_hidden(name):
            rebuilt_entries.append((name, original_value))
        else:
            rebuilt_entries.append(
                (name, checked_u32(requested[name], f"editable.OnlineInventoryMgr.items.{name}"))
            )
    added = 0
    for name in sorted(KNOWN_ONLINE_INVENTORY_NAMES - serialized_names):
        value = checked_u32(requested[name], f"editable.OnlineInventoryMgr.items.{name}")
        if value != 0:
            rebuilt_entries.append((name, value))
            added += 1

    rebuilt = (
        inventory_raw[:NATIVE_BLOB_HEADER_SIZE]
        + _encode_named_u32_map(rebuilt_entries, label="OnlineInventoryMgr.items")
        + inventory_raw[tail_offset:]
    )
    return rebuilt, {
        "serialized_before": len(entries),
        "serialized_after": len(rebuilt_entries),
        "added": added,
    }


def _read_u16_string(reader: Reader, *, allow_empty: bool = False) -> str:
    length = reader.u16()
    if length == 0:
        if allow_empty:
            return ""
        raise SaveError(f"{reader.name}: empty string is not valid here")
    return decode_text(reader.read(length))


# Purchasable/displayed costume catalogue for this Windows/up23 build, ordered
# by the numeric prefix used in the internal entity names.  The two special
# transformation-only entries 16_MinionCostume_Jelly and
# 27_MinionCostume_EvilMinion are intentionally excluded.  This leaves 60
# costumes.  Note that the exact internal names for 46 and 47 include the
# ``_None`` suffix.
KNOWN_COSTUME_NAMES: tuple[str, ...] = (
    "00_MinionCostume_None",
    "01_MinionCostume_Mage",
    "02_MinionCostume_Maid",
    "03_MinionCostume_Knight",
    "04_MinionCostume_Ninja",
    "05_MinionCostume_Vampirion",
    "06_MinionCostume_ElMariachi",
    "07_MinionCostume_Alarm",
    "08_MinionCostume_Firefighter",
    "09_MinionCostume_Vacationer",
    "10_MinionCostume_Surfer",
    "11_MinionCostume_Dance",
    "12_MinionCostume_Golfer",
    "13_MinionCostume_LeCook",
    "14_MinionCostume_Grandpa",
    "15_MinionCostume_Snorkeler",
    "17_MinionCostume_Girl",
    "18_MinionCostume_Reveler",
    "19_MinionCostume_Dad",
    "20_MinionCostume_Mom",
    "21_MinionCostume_Worker",
    "22_MinionCostume_Baby",
    "23_MinionCostume_Referee",
    "24_MinionCostume_Singer",
    "25_MinionCostume_Hunter",
    "26_MinionCostume_TortillaChipHat",
    "28_MinionCostume_SnowBoarder",
    "29_MinionCostume_Ballerina",
    "30_MinionCostume_Disguised",
    "31_MinionCostume_Santa",
    "32_MinionCostume_Football",
    "33_MinionCostume_Jogger",
    "34_MinionCostume_Cupid",
    "35_MinionCostume_Astronaut",
    "36_MinionCostume_Starfish",
    "37_MinionCostume_Tourist",
    "38_MinionCostume_Jar",
    "39_MinionCostume_Lucy",
    "40_MinionCostume_Lifeguard",
    "41_MinionCostume_Boxer",
    "42_LC_KHA_001_MinionCostume_costume01",
    "43_LC_KHA_002_MinionCostume_costume02",
    "44_MinionCostume_Frank",
    "45_LC_CHD_001_MinionCostume_Fuwa",
    "46_MinionCostume_Carl_None",
    "47_MinionCostume_Jerry_None",
    "48_MinionCostume_Disco",
    "49_MinionCostume_Skater",
    "50_MinionCostume_Carl_SantaHat",
    "51_MinionCostume_Jerry_SantaHat",
    "52_MinionCostume_Carl_CarnivalHat",
    "53_MinionCostume_Jerry_CarnivalHat",
    "54_MinionCostume_Soccer",
    "55_MinionCostume_Cleopatra",
    "56_MinionCostume_Athenian",
    "57_MinionCostume_Hazmat",
    "58_MinionCostume_Carl_BeehiveHat",
    "59_MinionCostume_Jerry_BeekeeperHat",
    "60_MinionCostume_Bride_Of_Frankenstein",
    "61_MinionCostume_Ghost",
)


def _read_costume_count(r: Reader, label: str, *, min_item_size: int = 1) -> int:
    count = r.u32()
    if count > r.remaining() // min_item_size:
        raise SaveError(f"CostumeMgr: {label} count {count} cannot fit in the remaining blob")
    return count


def _parse_costume_entry(r: Reader) -> tuple[str, int]:
    """Consume exactly one native CostumeMgr entry.

    This mirrors FUN_00b9ac90/FUN_00b94e90.  Only the first state word is
    projected publicly, but every following native collection/field is consumed
    structurally so the next entry boundary is known rather than searched for.
    """
    internal_name = _read_u16_string(r)
    state_word = r.u32()
    r.u32()  # second protected state/level word

    # Native nested protected map: two scalar words, then counted triples of u32.
    r.u32()
    r.u32()
    triple_count = _read_costume_count(r, "protected-map", min_item_size=12)
    for _ in range(triple_count):
        r.u32()
        r.u32()
        r.u32()

    raw_u32_count = _read_costume_count(r, "u32-list", min_item_size=4)
    r.read(raw_u32_count * 4)

    first_string_count = _read_costume_count(r, "first string-list", min_item_size=2)
    for _ in range(first_string_count):
        _read_u16_string(r, allow_empty=True)

    second_string_count = _read_costume_count(r, "second string-list", min_item_size=2)
    for _ in range(second_string_count):
        _read_u16_string(r, allow_empty=True)

    _read_u16_string(r, allow_empty=True)
    _read_u16_string(r, allow_empty=True)

    # Native fixed tail: u8, u32, u8, u8, u8.
    r.u8()
    r.u32()
    r.u8()
    r.u8()
    r.u8()
    return internal_name, state_word


def _parse_costume_manager(costume_raw: bytes) -> dict[str, Any]:
    """Parse CostumeMgr structurally without next-name scanning."""
    if len(costume_raw) < 12:
        return {}
    r = Reader(costume_raw, name="CostumeMgr")
    try:
        version = r.u32()
        signature = r.u32()
        if (version, signature) != NATIVE_BLOB_HEADERS["CostumeMgr"]:
            return {}
        r.u32()  # manager state
        equipped_costume = _read_u16_string(r)
        previous_costume = _read_u16_string(r, allow_empty=True)
        auxiliary_header_costumes = [_read_u16_string(r, allow_empty=True) for _ in range(4)]
        r.u32()  # global progression mirror
        count = _read_costume_count(r, "costume", min_item_size=47)

        entry_state_words: dict[str, int] = {}
        for _ in range(count):
            internal_name, state_word = _parse_costume_entry(r)
            if internal_name in entry_state_words:
                raise SaveError(f"CostumeMgr: duplicate costume {internal_name!r}")
            entry_state_words[internal_name] = state_word
        if r.remaining() != 0:
            raise SaveError(f"CostumeMgr: {r.remaining()} trailing bytes after declared entries")
        return {
            "equipped_costume": equipped_costume,
            "previous_costume": previous_costume,
            "auxiliary_header_costumes": auxiliary_header_costumes,
            "entry_state_words": dict(sorted(entry_state_words.items())),
        }
    except (SaveError, struct.error, UnicodeError):
        return {}


def _expand_costume_catalog(decoded: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return all known costumes with uniform lock/level state.

    CostumeMgr serializes only a subset of the global costume universe.  A
    serialized state word of 0xFFFFFFFF means locked/unavailable; any other
    word is a zero-based owned level.  Known costumes absent from the manager's
    serialized table are shown as locked rather than omitted.
    """
    raw_states = decoded.get("entry_state_words")
    if not isinstance(raw_states, dict):
        raw_states = {}

    costumes: dict[str, dict[str, Any]] = {}
    for name in KNOWN_COSTUME_NAMES:
        raw = raw_states.get(name)
        if isinstance(raw, int) and raw != 0xFFFFFFFF:
            costumes[name] = {
                "is_unlocked": True,
                "level": raw + 1,
            }
        else:
            costumes[name] = {
                "is_unlocked": False,
                "level": None,
            }
    return costumes


def _parse_bonus_upgrade_entries(bonus_raw: bytes) -> dict[str, Any]:
    """Parse BonusUpgradeMgr's named upgrade-level collection."""
    if len(bonus_raw) < 12:
        return {}
    version, signature, count = struct.unpack_from("<III", bonus_raw, 0)
    if (version, signature) != NATIVE_BLOB_HEADERS["BonusUpgradeMgr"]:
        return {}
    pos = COUNTED_BLOB_PREFIX_SIZE
    if count > (len(bonus_raw) - pos) // 11:
        return {}
    entries: dict[str, dict[str, int]] = {}
    try:
        for _ in range(count):
            if pos + 2 > len(bonus_raw):
                return {}
            name_len = struct.unpack_from("<H", bonus_raw, pos)[0]
            pos += 2
            if name_len == 0 or pos + name_len + 8 > len(bonus_raw):
                return {}
            name = decode_text(bonus_raw[pos:pos + name_len])
            pos += name_len
            upgrade_level, _reserved = struct.unpack_from("<II", bonus_raw, pos)
            pos += 8
            entries[name] = {"upgrade_level": upgrade_level}
    except (struct.error, UnicodeError):
        return {}
    return {"entries": dict(sorted(entries.items()))}


def _parse_achievement_flags(achievements_raw: bytes) -> dict[str, Any]:
    """Parse AchievementsMgr's named reward/completion flag collection."""
    if len(achievements_raw) < 12:
        return {}
    version, signature, count = struct.unpack_from("<III", achievements_raw, 0)
    if (version, signature) != NATIVE_BLOB_HEADERS["AchievementsMgr"]:
        return {}
    pos = COUNTED_BLOB_PREFIX_SIZE
    if count > (len(achievements_raw) - pos) // 5:
        return {}
    entries: dict[str, dict[str, bool]] = {}
    try:
        for _ in range(count):
            if pos + 2 > len(achievements_raw):
                return {}
            name_len = struct.unpack_from("<H", achievements_raw, pos)[0]
            pos += 2
            if name_len == 0 or pos + name_len + 2 > len(achievements_raw):
                return {}
            name = decode_text(achievements_raw[pos:pos + name_len])
            pos += name_len
            entries[name] = {
                "reward_collected": achievements_raw[pos] != 0,
                "completed": achievements_raw[pos + 1] != 0,
            }
            pos += 2
    except (struct.error, UnicodeError):
        return {}
    return {"entries": dict(sorted(entries.items()))}


# Known native Statistics names.  The main catalogue is recovered from the
# executable's contiguous Statistics-name static-initializer block; it is
# unioned with names observed in actual ``statistic_id`` positions in supplied
# saves to retain serialized spelling variants.  This is deliberately not
# derived from a save's name table, which is shared with scope names.  Missing
# primary integer values project as zero, while any future locally observed
# statistic name is automatically unioned into the clean view.
KNOWN_STATISTIC_NAMES: frozenset[str] = frozenset({
    'BananaMultiplierBananas',
    'BananaSplitterCollected',
    'BarrelHitCount',
    'BeatBestOwnDistance',
    'BeatDistanceCount',
    'BeatFriendsScoreCount',
    'BillboardHitCount',
    'BusHitCount',
    'CarHitCount',
    'ChallengeFriendsCount',
    'CheckLeaderboardsCount',
    'CostumeRunDistance',
    'DespicableActionsCount',
    'DivingCollected',
    'DivingPlayCount',
    'FlappyCollected',
    'FlappyRideCount',
    'FluffyCollected',
    'FluffyRideCount',
    'FreezeRayCollected',
    'GameplayTimeMillis',
    'GhostHittedMC',
    'GruRocketRideCount',
    'JellyJobStamps',
    'JetskiCollected',
    'JetskiRideCount',
    'JumpOverSnowBomb',
    'KiteCollected',
    'KiteRideCount',
    'LargeMinionCount',
    'MCHittedGhost',
    'MagnetCollected',
    'MaxUpgradeDiving',
    'MaxUpgradeKite',
    'MaxUpgradeSoccer',
    'MegaMinionCollected',
    'MinionTransform',
    'MoonCollected',
    'MoonRideCount',
    'NearMissSnowBoard',
    'NewspaperStandHitCount',
    'NumAreasInWhichCollectedAllFruits',
    'NumAreasUnlocked',
    'NumPowerupsUnlocked',
    'NumberOfFriends',
    'ObjDestroyedOnContact',
    'PlayXRuns',
    'RocketCollected',
    'RocketHitCount',
    'RocksPileHitCount',
    'RollUnderFireHydrant',
    'RollUnderXmasLights',
    'RollerCollected',
    'RollerRideCount',
    'ScooterCollected',
    'ScooterRideCount',
    'ShieldCollected',
    'ShowcaseLocationMinMissionNumber',
    'SkateboardCollected',
    'SkateboardRideCount',
    'SledCollected',
    'SledRideCount',
    'SnowboardCollected',
    'SoccerCollected',
    'SoccerPlayCount',
    'SpinWheelCount',
    'SstoryEventStarsCollected',
    'SubmarineCollected',
    'SubmarineRideCount',
    'TotemHitCount',
    'UFOCollected',
    'UFORideCount',
    'UnlockedDiving',
    'UnlockedKite',
    'UnlockedSoccer',
    'WeeklyContestPrizeCount',
    'aiShoot',
    'bananas',
    'bapplesCount',
    'beatScoreMultiplier',
    'bestScore',
    'bestScoreAmongFriends',
    'bmxCollected',
    'bmxRideCount',
    'cactusHitCount',
    'challengesWonCount',
    'climbedObstacleCount',
    'completedMissionsCount',
    'costumeReviveCount',
    'costumesBuyCount',
    'defeatMacho',
    'defeatMeena',
    'defeatVector',
    'defeatVillaintriloquist',
    'despicableMultiplier',
    'distance',
    'distanceExtra',
    'energyUnitsSpent',
    'evilEyesCount',
    'evilMinionCount',
    'eyeDroneHitCount',
    'fireLipsCount',
    'freeReviveCount',
    'freezeRayObjDestroyed',
    'freezedObjects',
    'jetskiRideCount',
    'jumpCount',
    'jumpOverFireTonguesCount',
    'jumpOverObstaclesCount',
    'jumpOverSandCastlesCount',
    'jumpOverWetfloorSignsCount',
    'jumpsStartedCount',
    'magnetizedBananas',
    'maxBananasCollectedInRun',
    'maxReachedScoreMultiplier',
    'maxUpgradeAll',
    'maxUpgradeBmx',
    'maxUpgradeBonusCannon',
    'maxUpgradeFlappy',
    'maxUpgradeMotocross',
    'maxUpgradePowerUpBananaMulti',
    'maxUpgradePowerUpEvilMinion',
    'maxUpgradePowerUpMagnet',
    'maxUpgradePowerUpShield',
    'maxUpgradePowerUpWeapon',
    'maxUpgradeScooter',
    'maxUpgradeSkateboard',
    'maxUpgradeSnowboard',
    'maxUpgradeUFO',
    'maxUpgradeVehicleFluffy',
    'maxUpgradeVehicleJetski',
    'maxUpgradeVehicleLargeMinion',
    'maxUpgradeVehicleMoon',
    'maxUpgradeVehicleMower',
    'maxUpgradeVehicleRocket',
    'maxUpgradeVehicleRoller',
    'maxUpgradeVehicleSled',
    'maxUpgradeVehicleSubmarine',
    'motocrossCollected',
    'motocrossRideCount',
    'mowerCollected',
    'mowerRideCount',
    'multiplierCountValue',
    'nearMiss',
    'nearMissAdvertisingPanelsCount',
    'nearMissRollingCagesCount',
    'nearMissSurfBoardsCount',
    'obstacleCollision',
    'pickUpsCount',
    'pitFallCount',
    'playerRecordedShoot',
    'puzzlePiecesWonCount',
    'randomShoot',
    'residentialEnterCount',
    'reviveCount',
    'ribbonHitCount',
    'rollCount',
    'rollUnderFireBowlsCount',
    'rollUnderHammocksCount',
    'rollUnderObstaclesCount',
    'rollUnderRedTapesCount',
    'rollsStartedCount',
    'runDistanceNoBananas',
    'runDistanceNoDespicableActions',
    'runDistanceNoPowerups',
    'sandCastleHitCount',
    'scooterRideCount',
    'score',
    'seasonalItemCollected',
    'seasonalItemCollectedInOneSeasonalMinigame',
    'secreatAreaCount',
    'shopSpentBananas',
    'slowerCollision',
    'snowboardRideCount',
    'speederCollision',
    'starsCount',
    'switchReviveCount',
    'timeElapsed',
    'tobbogansPassedCount',
    'toiletCabinHitCount',
    'tokens',
    'track_GPTime',
    'track_GPTotalTime',
    'tracking_raceId',
    'tracking_rev_p_d',
    'tracking_rev_p_nd',
    'turnsCount',
    'unlockedBonusCannon',
    'unlockedPowerUpBananaMulti',
    'unlockedPowerUpEvilMinion',
    'unlockedPowerUpMagnet',
    'unlockedPowerUpShield',
    'unlockedPowerUpWeapon',
    'unlockedVehicleBmx',
    'unlockedVehicleFlappy',
    'unlockedVehicleFluffy',
    'unlockedVehicleJetski',
    'unlockedVehicleLargeMinion',
    'unlockedVehicleMoon',
    'unlockedVehicleMotocross',
    'unlockedVehicleMower',
    'unlockedVehicleRocket',
    'unlockedVehicleRoller',
    'unlockedVehicleScooter',
    'unlockedVehicleSkateboard',
    'unlockedVehicleSled',
    'unlockedVehicleSnowboard',
    'unlockedVehicleSubmarine',
    'unlockedVehicleUFO',
    'upgradesCount',
    'usedCannon',
    'usedClawSaver',
    'usedJumpersCount',
    'usedSpeederCount',
})


def _parse_statistics(statistics_raw: bytes) -> dict[str, Any]:
    """Parse the native Statistics hierarchy into only the data used by clean JSON."""
    try:
        r = Reader(statistics_raw, name="statistics")
        version = r.u32()
        signature = r.u32()
        if (version, signature) != NATIVE_BLOB_HEADERS["statistics"]:
            return {}

        best_meters = r.u32()
        header_len = r.u16()
        if header_len > r.remaining():
            return {}
        decode_text(r.read(header_len))  # validate native UTF-8 header string
        r.u32()  # header state
        last_run_score = r.u32()
        last_run_bananas = r.u32()
        last_run_distance = r.u32()

        name_count = r.u32()
        if name_count > r.remaining() // 6:
            return {}
        id_to_name: dict[int, str] = {}
        seen_names: set[str] = set()
        for _ in range(name_count):
            name_len = r.u16()
            if name_len > r.remaining() - 4:
                return {}
            name = decode_text(r.read(name_len))
            identifier = r.u32()
            if identifier in id_to_name or name in seen_names:
                return {}
            id_to_name[identifier] = name
            seen_names.add(name)

        outer_count = r.u32()
        if outer_count > r.remaining() // 8:
            return {}

        observed_names: set[str] = set()
        scope_values: dict[tuple[int, int, str | None], dict[str, int]] = {}

        for _ in range(outer_count):
            outer_key = r.i32()
            vector_count = r.u32()
            if vector_count > r.remaining() // 4:
                return {}
            for vector_index in range(vector_count):
                scope_count = r.u32()
                if scope_count > r.remaining() // 8:
                    return {}
                for _ in range(scope_count):
                    scope_id = r.i32()
                    scope_name = id_to_name.get(scope_id)
                    statistic_count = r.u32()
                    if statistic_count > r.remaining() // 8:
                        return {}

                    primary: dict[str, int] = {}
                    for _ in range(statistic_count):
                        statistic_id = r.i32()
                        statistic_name = id_to_name.get(statistic_id)
                        if statistic_name is not None:
                            observed_names.add(statistic_name)
                        leaf_count = r.u32()
                        if leaf_count > r.remaining() // 12:
                            return {}

                        first_primary: int | None = None
                        for _ in range(leaf_count):
                            leaf_key = r.u32()
                            integer_raw = r.u32()
                            r.u32()  # protected float bits are not part of clean JSON
                            if leaf_key == 0 and first_primary is None:
                                first_primary = struct.unpack("<i", struct.pack("<I", integer_raw))[0]
                        if statistic_name is not None and first_primary is not None:
                            primary[statistic_name] = first_primary

                    scope_values.setdefault((outer_key, vector_index, scope_name), primary)

        if r.remaining() != 0:
            return {}
        return {
            "best_meters": best_meters,
            "last_run_score": last_run_score,
            "last_run_bananas": last_run_bananas,
            "last_run_distance": last_run_distance,
            "observed_names": observed_names,
            "scope_values": scope_values,
        }
    except (SaveError, struct.error, UnicodeError, OverflowError):
        return {}


def _decode_statistics_summary(exact: dict[str, Any]) -> dict[str, Any]:
    """Build the clean statistics projection from parsed native values."""
    scopes = exact.get("scope_values")
    if not isinstance(scopes, dict):
        return {}
    last_values = dict(scopes.get((-1, 0, "none"), {}))
    cumulative_values = dict(scopes.get((-1, 1, "none"), {}))

    for name, value in (
        ("score", exact["last_run_score"]),
        ("bananas", exact["last_run_bananas"]),
        ("distance", exact["last_run_distance"]),
    ):
        last_values.setdefault(name, int(value))

    observed = exact.get("observed_names")
    known_names = set(KNOWN_STATISTIC_NAMES)
    if isinstance(observed, set):
        known_names.update(name for name in observed if isinstance(name, str))

    def normalized(values: dict[str, int]) -> dict[str, int]:
        result = {name: int(values.get(name, 0)) for name in ("score", "bananas", "distance")}
        for name in sorted(known_names | set(values)):
            result.setdefault(name, int(values.get(name, 0)))
        return result

    personal_best: dict[str, int] = {"distance": int(exact["best_meters"])}
    if "bestScore" in cumulative_values:
        personal_best["score"] = int(cumulative_values["bestScore"])
    if "maxBananasCollectedInRun" in cumulative_values:
        personal_best["bananas"] = int(cumulative_values["maxBananasCollectedInRun"])

    return {
        "personal_best": personal_best,
        "last_run": normalized(last_values),
        "cumulative": normalized(cumulative_values),
    }


# MapMgr needs a separate Jelly Lab catalogue because mission definitions are not
# embedded in savegame.  Only the current minimal catalogue schema is supported.
JELLY_DEFAULT_CATALOG_NAME = "jelly_lab_catalog.json"


def _default_jelly_catalog_path() -> Path:
    return Path(__file__).resolve().parent / JELLY_DEFAULT_CATALOG_NAME


def _load_jelly_lab_catalogue(args: argparse.Namespace) -> dict[str, Any] | None:
    catalog_arg = getattr(args, "jelly_lab_catalog", None)
    catalog_path = Path(catalog_arg) if catalog_arg is not None else _default_jelly_catalog_path()
    try:
        obj = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None

    raw_areas = obj.get("areas")
    raw_defaults = obj.get("level_defaults")
    if not isinstance(raw_areas, dict) or not raw_areas or not isinstance(raw_defaults, dict):
        return None

    areas: dict[str, dict[str, Any]] = {}
    for area_name, raw_area in raw_areas.items():
        if not isinstance(area_name, str) or not area_name.isdigit() or not isinstance(raw_area, dict):
            return None
        max_fruits = raw_area.get("max_fruits")
        level_count = raw_area.get("level_count")
        rewards = raw_area.get("rewards")
        if (
            not isinstance(max_fruits, int)
            or isinstance(max_fruits, bool)
            or max_fruits < 0
            or not isinstance(level_count, int)
            or isinstance(level_count, bool)
            or level_count < 1
            or not isinstance(rewards, dict)
        ):
            return None
        clean_rewards: dict[str, str] = {}
        for key, name in rewards.items():
            if not isinstance(key, str) or not key.isdigit() or not isinstance(name, str) or not name:
                return None
            clean_rewards[key] = name
        areas[area_name] = {
            "max_fruits": max_fruits,
            "level_count": level_count,
            "rewards": clean_rewards,
        }

    level_defaults: dict[str, dict[str, Any]] = {}
    for key, raw_hint in raw_defaults.items():
        if not isinstance(key, str) or not re.fullmatch(r"[1-9]\d*:[1-9]\d*", key):
            return None
        if not isinstance(raw_hint, dict):
            return None
        area_text, level_text = key.split(":", 1)
        area_number, level_number = int(area_text), int(level_text)
        area = areas.get(area_text)
        if not isinstance(area, dict) or level_number > area["level_count"]:
            return None

        # Only fields consumed by the editor are retained. Other catalogue
        # metadata may be present, but it is not part of the save writer.
        hint: dict[str, int] = {}
        for field_name in ("force_location", "target_value3"):
            value = raw_hint.get(field_name)
            if value is None:
                continue
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= MAX_U32
            ):
                return None
            hint[field_name] = value
        level_defaults[key] = hint

    area_numbers = sorted(int(name) for name in areas)
    if area_numbers != list(range(1, len(area_numbers) + 1)):
        return None
    return {"areas": areas, "level_defaults": level_defaults}


def _parse_map_level_state(r: Reader) -> dict[str, Any]:
    """Parse one MapMgr level-state object in the exact native wire order.

    The native in-memory object is larger and contains duplicated/obfuscated
    integer storage.  The save stream contains only the recovered logical u32
    values.  Two fields are ordinary u16-length-prefixed record strings, so the
    serialized record is 70 bytes only when both strings are empty.
    """
    area_index = r.u32()
    local_level_index = r.u32()
    unknown_10 = r.u32()
    protected_14 = r.u32()
    unknown_24 = r.u32()
    reference_28 = read_record_string(r)
    flag_2c = r.u8()
    flag_2d = r.u8()
    fruits = r.u32()
    unknown_34 = r.u32()
    unknown_38 = r.u32()
    protected_41 = r.u32()
    protected_51 = r.u32()
    protected_61 = r.u32()
    flag_71 = r.u8()
    flag_72 = r.u8()
    unknown_74 = r.u32()
    reference_78 = read_record_string(r)
    flag_7c = r.u8()
    protected_7d = r.u32()
    protected_8d = r.u32()
    unknown_3c = r.u32()
    flag_40 = r.u8()

    return {
        "area": area_index + 1,
        "level_in_area": local_level_index + 1,
        "fruits": fruits,
        "native": {
            "area_index": area_index,
            "level_index": local_level_index,
            "unknown_10": unknown_10,
            "protected_14": protected_14,
            "unknown_24": unknown_24,
            "reference_28": reference_28,
            "flag_2c": flag_2c,
            "flag_2d": flag_2d,
            "fruits": fruits,
            "unknown_34": unknown_34,
            "unknown_38": unknown_38,
            "protected_41": protected_41,
            "protected_51": protected_51,
            "protected_61": protected_61,
            "flag_71": flag_71,
            "flag_72": flag_72,
            "unknown_74": unknown_74,
            "reference_78": reference_78,
            "flag_7c": flag_7c,
            "protected_7d": protected_7d,
            "protected_8d": protected_8d,
            "unknown_3c": unknown_3c,
            "flag_40": flag_40,
        },
    }


def _parse_map_level_state_records(r: Reader, count: int) -> list[dict[str, Any]]:
    if count < 0:
        raise SaveError("MapMgr level-state count is negative")
    # 70 bytes is the minimum record size: both native strings have zero length.
    if count > max(0, (r.remaining() - 4) // 70):
        raise SaveError(
            f"MapMgr level-state count {count} cannot fit in {r.remaining()} remaining bytes"
        )
    return [_parse_map_level_state(r) for _ in range(count)]


def _parse_map_areas(r: Reader) -> tuple[int, dict[str, dict[str, Any]]]:
    area_count = r.u32()
    if area_count > r.remaining() // 13:
        raise SaveError(f"MapMgr area count {area_count} cannot fit in the remaining blob")

    native_areas: dict[str, dict[str, Any]] = {}
    for area_index in range(area_count):
        state = r.u8()
        fruits = r.u32()

        map_a_count = r.u32()
        if map_a_count > r.remaining() // 5:
            raise SaveError(
                f"MapMgr area {area_index + 1} map_a count {map_a_count} "
                "cannot fit in the remaining blob"
            )
        map_a: dict[str, int] = {}
        for _ in range(map_a_count):
            key = r.u32()
            map_a[str(key)] = r.u8()

        map_b_count = r.u32()
        if map_b_count > r.remaining() // 6:
            raise SaveError(
                f"MapMgr area {area_index + 1} map_b count {map_b_count} "
                "cannot fit in the remaining blob"
            )
        map_b: dict[str, str] = {}
        for _ in range(map_b_count):
            key = r.u32()
            map_b[str(key)] = read_record_string(r)

        native_areas[str(area_index + 1)] = {
            "state": state,
            "fruits": fruits,
            "map_a": map_a,
            "map_b": map_b,
        }
    return area_count, native_areas


def _parse_map_tail(r: Reader) -> dict[str, Any]:
    """Parse the complete MapMgr tail in native serialization order."""
    index_count = r.u32()
    if index_count > r.remaining() // 4:
        raise SaveError(f"MapMgr tail index count {index_count} cannot fit in the remaining blob")
    indexes = [r.u32() for _ in range(index_count)]

    pair_34_38 = (r.u32(), r.u32())
    fields_2c_30 = (r.u32(), r.u32())
    pair_3c_40 = (r.u32(), r.u32())
    field_44 = r.u32()
    field_48 = r.u32()
    field_4c = r.u32()
    protected_50 = r.u32()

    protected_vector_count = r.u32()
    if protected_vector_count > r.remaining() // 4:
        raise SaveError(
            f"MapMgr tail protected-vector count {protected_vector_count} "
            "cannot fit in the remaining blob"
        )
    protected_vector = [r.u32() for _ in range(protected_vector_count)]

    field_6c = r.u8()
    field_70 = r.u32()
    field_74 = r.u32()
    reference_90 = read_record_string(r)
    field_78 = r.u32()
    field_7c = r.u32()
    field_84 = r.u32()
    field_8c = r.u32()

    return {
        "index_count": index_count,
        "indexes": indexes,
        "pair_34_38": list(pair_34_38),
        "fields_2c_30": list(fields_2c_30),
        "pair_3c_40": list(pair_3c_40),
        "field_44": field_44,
        "field_48": field_48,
        "field_4c": field_4c,
        "protected_50": protected_50,
        "protected_vector_count": protected_vector_count,
        "protected_vector": protected_vector,
        "field_6c": field_6c,
        "field_70": field_70,
        "field_74": field_74,
        "reference_90": reference_90,
        "field_78": field_78,
        "field_7c": field_7c,
        "field_84": field_84,
        "field_8c": field_8c,
    }


def _parse_map_manager(blob: bytes, jelly_catalogue: dict[str, Any]) -> dict[str, Any] | None:
    """Decode MapMgr using the exact native serialization grammar.

    The clean JSON projection stays compact/read-only; this routine retains the
    richer native structure internally so future RE can name additional fields
    without reparsing opaque byte slices.
    """
    if len(blob) < 12:
        return None
    catalogue_areas = jelly_catalogue.get("areas")
    if not isinstance(catalogue_areas, dict) or not catalogue_areas:
        return None

    r = Reader(blob, name="MapMgr")
    try:
        version = r.u32()
        signature = r.u32()
        if (version, signature) != NATIVE_BLOB_HEADERS["MapMgr"]:
            return None

        level_state_count = r.u32()
        level_rows = _parse_map_level_state_records(r, level_state_count)
        area_count, native_areas = _parse_map_areas(r)
        tail = _parse_map_tail(r)
        if r.remaining() != 0:
            raise SaveError(f"MapMgr parser left {r.remaining()} trailing bytes")
    except SaveError:
        return None

    level_fruits_by_area: dict[int, dict[int, int]] = {}
    for row in level_rows:
        level_fruits_by_area.setdefault(row["area"], {})[row["level_in_area"]] = row["fruits"]

    areas: dict[str, dict[str, Any]] = {}
    all_area_names = set(catalogue_areas) | set(native_areas)
    for area_name in sorted(all_area_names, key=lambda x: int(x) if str(x).isdigit() else str(x)):
        cat = catalogue_areas.get(area_name)
        native = native_areas.get(area_name)
        if isinstance(native, dict):
            state = int(native.get("state", 0))
            fruits = int(native.get("fruits", 0))
            map_a = copy.deepcopy(native.get("map_a", {}))
            map_b = copy.deepcopy(native.get("map_b", {}))
        else:
            state = 0
            fruits = 0
            map_a = {}
            map_b = {}

        area: dict[str, Any] = {
            "state": state,
            "fruits": fruits,
            "map_a": map_a,
            "map_b": map_b,
        }
        if isinstance(cat, dict):
            max_fruits = cat.get("max_fruits")
            if isinstance(max_fruits, int):
                area["all_fruits_collected"] = fruits == max_fruits

            level_count = cat.get("level_count")
            if isinstance(level_count, int) and level_count >= 0:
                saved_level_fruits = level_fruits_by_area.get(int(area_name), {})
                area["levels"] = {
                    str(level_number): int(saved_level_fruits.get(level_number, 0))
                    for level_number in range(1, level_count + 1)
                }

            raw_rewards = cat.get("rewards")
            if isinstance(raw_rewards, dict):
                rewards: list[dict[str, Any]] = []
                for key_text, name in sorted(
                    raw_rewards.items(),
                    key=lambda item: int(item[0]) if str(item[0]).isdigit() else str(item[0]),
                ):
                    if not isinstance(key_text, str) or not key_text.isdigit() or not isinstance(name, str):
                        continue
                    key = int(key_text)
                    rewards.append({
                        "key": key,
                        "name": name,
                        "is_collected": map_b.get(str(key)) == name,
                    })
                area["rewards"] = rewards
        areas[str(area_name)] = area

    # These three coordinate pairs are structurally confirmed.  The semantic
    # names are retained from controlled-save comparisons made in earlier RE.
    selected_area, selected_local = tail["pair_34_38"]
    progression_area, progression_local = tail["fields_2c_30"]
    last_area, last_local = tail["pair_3c_40"]

    # field_4c is the native scalar serialized after the three coordinate pairs.
    # It matched the header collection count in every regression save and is the
    # stronger semantic candidate for current progression than the header count.
    current_progression_level = int(tail["field_4c"])

    decoded: dict[str, Any] = {
        "current_progression_level": current_progression_level,
        "area_count": area_count,
        "areas": areas,
        "serialized_level_states": level_rows,
        "native_tail": tail,
    }

    decoded["selected"] = {"area": selected_area + 1, "level_in_area": selected_local + 1}
    decoded["progression"] = {"area": progression_area + 1, "level_in_area": progression_local + 1}
    decoded["last_played"] = {"area": last_area + 1, "level_in_area": last_local + 1}
    return decoded


def add_decoded_fields(parsed: dict[str, Any], jelly_catalogue: dict[str, Any] | None = None) -> None:
    """Attach compact manager-specific decodes using internal record names."""
    records = parsed.get("records")
    if not isinstance(records, dict):
        return

    # Manager-specific views are derived caches. Clear them before reparsing so
    # an invalid/unsupported replacement blob cannot inherit stale semantics
    # from an earlier decode pass.
    for manager_name in NATIVE_BLOB_HEADERS:
        record = records.get(manager_name)
        if isinstance(record, dict):
            record.pop("decoded", None)

    player_raw = _record_blob(records, "Player")
    player = records.get("Player")
    if (
        player_raw is not None
        and isinstance(player, dict)
        and _native_blob_header(player_raw, "Player") is not None
    ):
        try:
            prefix = _parse_player_prefix(player_raw)
            perks, boosters, _ = _parse_player_counter_collections(player_raw, prefix)
        except SaveError:
            prefix = None
            perks, boosters = [], []
        if prefix is not None:
            d: dict[str, Any] = {
                "token_count": prefix.token_count,
                "banana_count": prefix.banana_count,
                "minion_launchers": prefix.minion_launchers,
                "free_revives": prefix.free_revives,
                "base_despicable_multiplier": prefix.base_despicable_multiplier,
            }
            counters = dict(perks + boosters)
            if counters:
                d["perk_counters"] = dict(sorted(counters.items()))
            player["decoded"] = d

    inventory_raw = _record_blob(records, "OnlineInventoryMgr")
    inventory = records.get("OnlineInventoryMgr")
    if inventory_raw is not None and isinstance(inventory, dict):
        try:
            entries, _ = _online_inventory_layout(inventory_raw)
        except SaveError:
            entries = []
        inventory["decoded"] = {"items": dict(sorted(entries))}

    prize_raw = _record_blob(records, "PrizePodMgr")
    prize_mgr = records.get("PrizePodMgr")
    if prize_raw is not None and isinstance(prize_mgr, dict):
        d = _parse_prize_pod_manager(prize_raw)
        if d:
            prize_mgr["decoded"] = d

    bonus_raw = _record_blob(records, "BonusUpgradeMgr")
    bonus = records.get("BonusUpgradeMgr")
    if bonus_raw is not None and isinstance(bonus, dict):
        d = _parse_bonus_upgrade_entries(bonus_raw)
        if d:
            bonus["decoded"] = d

    costume_raw = _record_blob(records, "CostumeMgr")
    costume = records.get("CostumeMgr")
    if costume_raw is not None and isinstance(costume, dict):
        d = _parse_costume_manager(costume_raw)
        if d:
            costume["decoded"] = d

    map_raw = _record_blob(records, "MapMgr")
    map_record = records.get("MapMgr")
    if jelly_catalogue is not None and map_raw is not None and isinstance(map_record, dict):
        d = _parse_map_manager(map_raw, jelly_catalogue)
        if d:
            map_record["decoded"] = d

    achievements_raw = _record_blob(records, "AchievementsMgr")
    achievements = records.get("AchievementsMgr")
    if achievements_raw is not None and isinstance(achievements, dict):
        d = _parse_achievement_flags(achievements_raw)
        if d:
            # Entries already use the serialized/internal achievement names.
            achievements["decoded"] = d


    statistics_raw = _record_blob(records, "statistics")
    statistics = records.get("statistics")
    if statistics_raw is not None and isinstance(statistics, dict):
        # Statistics decoding is exact-only. Recognized native records are parsed
        # structurally; unknown serializer revisions remain preserved internally.
        exact_statistics = _parse_statistics(statistics_raw)
        if exact_statistics:
            statistics["decoded"] = _decode_statistics_summary(exact_statistics)

    evil_raw = _record_blob(records, "EvilMinionMgr")
    evil = records.get("EvilMinionMgr")
    if (
        evil_raw is not None
        and isinstance(evil, dict)
        and _native_blob_header(evil_raw, "EvilMinionMgr") is not None
    ):
        try:
            _, timer = _evil_minion_timer(evil_raw)
        except SaveError:
            pass
        else:
            evil["decoded"] = {"evil_minion_timer_seconds": timer}


def xtea_encrypt_block(block: bytes, key: bytes) -> bytes:
    if len(block) != 8:
        raise ValueError("XTEA block must be exactly 8 bytes")
    if len(key) != 16:
        raise ValueError("XTEA key must be exactly 16 bytes")

    v0, v1 = struct.unpack("<2I", block)
    k = struct.unpack("<4I", key)
    total = 0
    for _ in range(XTEA_ROUNDS):
        v0 = (
            v0
            + ((((v1 << 4) ^ (v1 >> 5)) + v1) ^ ((total + k[total & 3]) & MAX_U32))
        ) & MAX_U32
        total = (total + XTEA_DELTA) & MAX_U32
        v1 = (
            v1
            + ((((v0 << 4) ^ (v0 >> 5)) + v0) ^ ((total + k[(total >> 11) & 3]) & MAX_U32))
        ) & MAX_U32
    return struct.pack("<2I", v0, v1)

def xtea_encrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % 8:
        raise SaveError('XTEA plaintext must be padded to a multiple of 8 bytes')
    return b"".join(
        xtea_encrypt_block(data[offset:offset + 8], key)
        for offset in range(0, len(data), 8)
    )

def checked_u8(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SaveError(f'{name} must be an integer')
    if not 0 <= value <= 0xFF:
        raise SaveError(f'{name} must be in the range 0..255')
    return value

def checked_u32(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SaveError(f'{name} must be an integer')
    if not 0 <= value <= MAX_U32:
        raise SaveError(f'{name} must be in the range 0..{MAX_U32}')
    return value

def rol32(value: int, count: int) -> int:
    count &= 31
    value &= MAX_U32
    return (value << count | value >> (32 - count & 31)) & MAX_U32

def wallet_shadow_encode(value: int) -> int:
    return rol32(checked_u32(value, "wallet shadow value"), WALLET_SHADOW_ROTATION) ^ WALLET_SHADOW_XOR

def _player_wallet_shadow_offset(player_raw: bytes) -> int:
    """Return the exact wallet-shadow flag offset from the native serializer.

    FUN_007cf710 writes the validity byte and two shadow u32 values as the final
    nine bytes of Player, immediately before returning.
    """
    if len(player_raw) < 17:
        raise SaveError("Player blob is too small for the wallet shadow")
    _require_native_blob(player_raw, "Player", min_size=17)
    return len(player_raw) - 9


def patch_wallet_shadow(blob: bytearray, old_tokens: int, old_bananas: int, new_tokens: int, new_bananas: int) -> dict[str, Any]:
    """Patch Player's native terminal wallet-shadow fields structurally."""
    offset = _player_wallet_shadow_offset(bytes(blob))
    valid_flag = blob[offset]
    old_token_shadow, old_banana_shadow = struct.unpack_from("<II", blob, offset + 1)
    expected_token_shadow = wallet_shadow_encode(old_tokens)
    expected_banana_shadow = wallet_shadow_encode(old_bananas)
    if valid_flag != 1:
        raise SaveError(
            f"Player wallet shadow valid flag is {valid_flag}, expected 1 at 0x{offset:X}"
        )
    if (old_token_shadow, old_banana_shadow) != (expected_token_shadow, expected_banana_shadow):
        raise SaveError(
            "Player terminal wallet shadow does not match ordinary wallet values: "
            f"stored=(0x{old_token_shadow:08X},0x{old_banana_shadow:08X}), "
            f"expected=(0x{expected_token_shadow:08X},0x{expected_banana_shadow:08X})"
        )
    new_token_shadow = wallet_shadow_encode(new_tokens)
    new_banana_shadow = wallet_shadow_encode(new_bananas)
    struct.pack_into("<BII", blob, offset, 1, new_token_shadow, new_banana_shadow)
    return {
        "offset": offset,
        "old_token_shadow": f"0x{old_token_shadow:08X}",
        "old_banana_shadow": f"0x{old_banana_shadow:08X}",
        "new_token_shadow": f"0x{new_token_shadow:08X}",
        "new_banana_shadow": f"0x{new_banana_shadow:08X}",
    }


def decode_blob(record: dict[str, Any], name: str) -> bytearray:
    if record.get('_type') != 7 or not isinstance(record.get('data'), bytes):
        raise SaveError(f'record {name!r} is not a binary blob')
    return bytearray(record['data'])

def patch_u32(blob: bytearray, offset: int, value: Any, field: str) -> None:
    value = checked_u32(value, field)
    if offset + 4 > len(blob):
        raise SaveError(f'cannot write {field}: blob is {len(blob)} bytes, offset is 0x{offset:X}')
    struct.pack_into('<I', blob, offset, value)


def _map_coordinate_checked(
    value: Any,
    *,
    field: str,
    catalogue_areas: dict[str, Any],
    allow_final_one_past: bool = False,
) -> tuple[int, int]:
    if not isinstance(value, dict):
        raise SaveError(f"{field} must be an object")
    extra = set(value) - {"area", "level_in_area"}
    if extra:
        raise SaveError(f"{field} has unsupported fields: " + ", ".join(sorted(extra)))
    area = value.get("area")
    level = value.get("level_in_area")
    if not isinstance(area, int) or isinstance(area, bool) or area < 1:
        raise SaveError(f"{field}.area must be a positive integer")
    if not isinstance(level, int) or isinstance(level, bool) or level < 1:
        raise SaveError(f"{field}.level_in_area must be a positive integer")
    cat = catalogue_areas.get(str(area))
    if not isinstance(cat, dict):
        raise SaveError(f"{field} references unknown Jelly Lab area {area}")
    level_count = cat.get("level_count")
    if not isinstance(level_count, int) or level_count < 1:
        raise SaveError(f"Jelly Lab catalogue area {area} has invalid level_count")
    if level > level_count:
        if allow_final_one_past:
            numeric_areas = sorted(
                int(k) for k in catalogue_areas
                if isinstance(k, str) and k.isdigit()
            )
            final_area = numeric_areas[-1] if numeric_areas else None
            if area == final_area and level == level_count + 1:
                return area, level
        raise SaveError(
            f"{field} references level {level} outside area {area}'s catalogue range"
        )
    return area, level


def _map_catalogue_progression(
    jelly_catalogue: dict[str, Any],
) -> tuple[list[tuple[int, int]], dict[tuple[int, int], int]]:
    catalogue_areas = jelly_catalogue.get("areas")
    if not isinstance(catalogue_areas, dict) or not catalogue_areas:
        raise SaveError("MapMgr editing requires a valid Jelly Lab catalogue")

    coordinates: list[tuple[int, int]] = []
    expected_area = 1
    for area_name in sorted(catalogue_areas, key=lambda x: int(x) if str(x).isdigit() else 1 << 30):
        if not isinstance(area_name, str) or not area_name.isdigit():
            raise SaveError("Jelly Lab catalogue area keys must be decimal integers")
        area = int(area_name)
        if area != expected_area:
            raise SaveError(
                f"Jelly Lab catalogue areas are not contiguous at area {expected_area}"
            )
        cat = catalogue_areas[area_name]
        level_count = cat.get("level_count") if isinstance(cat, dict) else None
        if not isinstance(level_count, int) or level_count < 1:
            raise SaveError(f"Jelly Lab catalogue area {area} has invalid level_count")
        for level in range(1, level_count + 1):
            coordinates.append((area, level))
        expected_area += 1

    coordinate_to_global = {
        coordinate: index + 1 for index, coordinate in enumerate(coordinates)
    }
    return coordinates, coordinate_to_global


def _encode_map_level_state(native: dict[str, Any]) -> bytes:
    if not isinstance(native, dict):
        raise SaveError("MapMgr level state is missing native fields")
    out = bytearray()
    out += struct.pack(
        "<IIIII",
        checked_u32(native.get("area_index"), "MapMgr.level.area_index"),
        checked_u32(native.get("level_index"), "MapMgr.level.level_index"),
        checked_u32(native.get("unknown_10"), "MapMgr.level.unknown_10"),
        checked_u32(native.get("protected_14"), "MapMgr.level.best_progress"),
        checked_u32(native.get("unknown_24"), "MapMgr.level.unknown_24"),
    )
    out += encode_record_string(native.get("reference_28", ""))
    out += struct.pack(
        "<BBIIIIII",
        checked_u8(native.get("flag_2c"), "MapMgr.level.flag_2c"),
        checked_u8(native.get("flag_2d"), "MapMgr.level.flag_2d"),
        checked_u32(native.get("fruits"), "MapMgr.level.fruits"),
        checked_u32(native.get("unknown_34"), "MapMgr.level.previous_fruits"),
        checked_u32(native.get("unknown_38"), "MapMgr.level.attempt_count"),
        checked_u32(native.get("protected_41"), "MapMgr.level.previous_best"),
        checked_u32(native.get("protected_51"), "MapMgr.level.latest_run_score"),
        checked_u32(native.get("protected_61"), "MapMgr.level.latest_attempt_progress"),
    )
    out += struct.pack(
        "<BBI",
        checked_u8(native.get("flag_71"), "MapMgr.level.flag_71"),
        checked_u8(native.get("flag_72"), "MapMgr.level.flag_72"),
        checked_u32(native.get("unknown_74"), "MapMgr.level.unknown_74"),
    )
    out += encode_record_string(native.get("reference_78", ""))
    out += struct.pack(
        "<BIIIB",
        checked_u8(native.get("flag_7c"), "MapMgr.level.flag_7c"),
        checked_u32(native.get("protected_7d"), "MapMgr.level.protected_7d"),
        checked_u32(native.get("protected_8d"), "MapMgr.level.protected_8d"),
        checked_u32(native.get("unknown_3c"), "MapMgr.level.zero_fruit_attempt_count"),
        checked_u8(native.get("flag_40"), "MapMgr.level.flag_40"),
    )
    return bytes(out)


def _new_map_level_state(
    area: int, level: int, *, unknown_10: int = 0xFFFFFFFF
) -> dict[str, Any]:
    # Native fresh/unplayed frontier state recovered from controlled saves and
    # the FUN_007a21e0/FUN_007a2660 construction path.
    return {
        "area_index": area - 1,
        "level_index": level - 1,
        "unknown_10": checked_u32(unknown_10, "MapMgr.fresh_level.unknown_10"),
        "protected_14": 0,
        "unknown_24": 0,
        "reference_28": "",
        "flag_2c": 1,
        "flag_2d": 1,
        "fruits": 0,
        "unknown_34": 0,
        "unknown_38": 0,
        "protected_41": 0,
        "protected_51": 0,
        "protected_61": 0,
        "flag_71": 0,
        "flag_72": 0,
        "unknown_74": 0,
        "reference_78": "",
        "flag_7c": 0,
        "protected_7d": 0,
        "protected_8d": 0,
        "unknown_3c": 0,
        "flag_40": 0,
    }


def _map_level_default_hint(
    jelly_catalogue: dict[str, Any], area: int, level: int
) -> dict[str, Any]:
    defaults = jelly_catalogue.get("level_defaults")
    if not isinstance(defaults, dict):
        return {}
    hint = defaults.get(f"{area}:{level}")
    return hint if isinstance(hint, dict) else {}


def _map_target_value3_checked(
    jelly_catalogue: dict[str, Any], area: int, level: int
) -> int:
    hint = _map_level_default_hint(jelly_catalogue, area, level)
    value = hint.get("target_value3")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > MAX_U32
    ):
        raise SaveError(
            "MapMgr forward skip requires target_value3 for every skipped "
            f"level; catalogue is missing Area {area} Level {level}."
        )
    return value

def _map_initializer_unknown_10(
    jelly_catalogue: dict[str, Any], area: int, level: int
) -> int:
    hint = _map_level_default_hint(jelly_catalogue, area, level)
    force_location = hint.get("force_location")
    if (
        isinstance(force_location, int)
        and not isinstance(force_location, bool)
        and 0 <= force_location <= MAX_U32
        and force_location != MAX_U32
    ):
        return force_location
    return MAX_U32


def _complete_map_level_state(
    native: dict[str, Any], *, target_value3: int
) -> dict[str, Any]:
    """Fabricate one three-fruit successful attempt for a skipped prerequisite.

    Existing history is preserved where possible.  If the level already has all
    three fruits, nothing is changed.  Otherwise the synthetic result records the
    pre-skip fruit count/best progress, increments attempt count once, writes
    TargetValue3 as the latest objective progress, raises best progress to at
    least TargetValue3, and sets fruits to 3.  No genuine run occurred, so the
    latest-run score is written as 0.
    """
    if not isinstance(native, dict):
        raise SaveError("MapMgr skipped level is missing native state")
    old_fruits = checked_u32(native.get("fruits"), "MapMgr.skipped_level.fruits")
    if old_fruits >= 3:
        return {
            "changed": False,
            "old_fruits": old_fruits,
            "new_fruits": old_fruits,
            "fruit_delta": 0,
        }

    old_best = checked_u32(
        native.get("protected_14"), "MapMgr.skipped_level.best_progress"
    )
    old_attempts = checked_u32(
        native.get("unknown_38"), "MapMgr.skipped_level.attempt_count"
    )
    new_best = max(old_best, target_value3)
    fruit_delta = 3 - old_fruits

    native["flag_2c"] = 1
    native["flag_2d"] = 1
    native["unknown_34"] = old_fruits             # previous_fruits
    native["unknown_38"] = checked_u32(
        old_attempts + 1, "MapMgr.skipped_level.attempt_count"
    )
    native["protected_41"] = old_best             # previous_best_progress
    native["protected_51"] = 0                    # no real run score exists
    native["protected_61"] = target_value3        # latest_attempt_progress
    native["protected_14"] = new_best             # best_objective_progress
    native["fruits"] = 3

    return {
        "changed": True,
        "old_fruits": old_fruits,
        "new_fruits": 3,
        "fruit_delta": fruit_delta,
        "old_best_progress": old_best,
        "new_best_progress": new_best,
        "target_value3": target_value3,
        "old_attempt_count": old_attempts,
        "new_attempt_count": old_attempts + 1,
    }

def _encode_map_tail(tail: dict[str, Any]) -> bytes:
    if not isinstance(tail, dict):
        raise SaveError("MapMgr native tail is missing")
    indexes = tail.get("indexes")
    protected_vector = tail.get("protected_vector")
    if not isinstance(indexes, list) or not isinstance(protected_vector, list):
        raise SaveError("MapMgr native tail vectors are invalid")
    out = bytearray(struct.pack("<I", len(indexes)))
    for value in indexes:
        out += struct.pack("<I", checked_u32(value, "MapMgr.tail.index"))
    for key in ("pair_34_38", "fields_2c_30", "pair_3c_40"):
        pair = tail.get(key)
        if not isinstance(pair, list) or len(pair) != 2:
            raise SaveError(f"MapMgr tail {key} is invalid")
        out += struct.pack(
            "<II",
            checked_u32(pair[0], f"MapMgr.tail.{key}[0]"),
            checked_u32(pair[1], f"MapMgr.tail.{key}[1]"),
        )
    for key in ("field_44", "field_48", "field_4c", "protected_50"):
        out += struct.pack("<I", checked_u32(tail.get(key), f"MapMgr.tail.{key}"))
    out += struct.pack("<I", len(protected_vector))
    for value in protected_vector:
        out += struct.pack("<I", checked_u32(value, "MapMgr.tail.protected_vector"))
    out += struct.pack("<B", checked_u8(tail.get("field_6c"), "MapMgr.tail.field_6c"))
    out += struct.pack(
        "<II",
        checked_u32(tail.get("field_70"), "MapMgr.tail.field_70"),
        checked_u32(tail.get("field_74"), "MapMgr.tail.field_74"),
    )
    out += encode_record_string(tail.get("reference_90", ""))
    for key in ("field_78", "field_7c", "field_84", "field_8c"):
        out += struct.pack("<I", checked_u32(tail.get(key), f"MapMgr.tail.{key}"))
    return bytes(out)


def _encode_map_manager(decoded: dict[str, Any]) -> bytes:
    states = decoded.get("serialized_level_states")
    native_areas = decoded.get("areas")
    tail = decoded.get("native_tail")
    area_count = decoded.get("area_count")
    if not isinstance(states, list) or not isinstance(native_areas, dict):
        raise SaveError("MapMgr decoded structure is incomplete")
    if not isinstance(area_count, int) or area_count < 0:
        raise SaveError("MapMgr area_count is invalid")

    version, signature = NATIVE_BLOB_HEADERS["MapMgr"]
    out = bytearray(struct.pack("<III", version, signature, len(states)))
    for row in states:
        if not isinstance(row, dict):
            raise SaveError("MapMgr contains an invalid level-state row")
        out += _encode_map_level_state(row.get("native"))

    out += struct.pack("<I", area_count)
    for area in range(1, area_count + 1):
        native = native_areas.get(str(area))
        if not isinstance(native, dict):
            raise SaveError(f"MapMgr decoded area {area} is missing")
        map_a = native.get("map_a")
        map_b = native.get("map_b")
        if not isinstance(map_a, dict) or not isinstance(map_b, dict):
            raise SaveError(f"MapMgr decoded area {area} maps are invalid")
        out += struct.pack(
            "<BI",
            checked_u8(native.get("state"), f"MapMgr.area.{area}.state"),
            checked_u32(native.get("fruits"), f"MapMgr.area.{area}.fruits"),
        )
        out += struct.pack("<I", len(map_a))
        for key, value in map_a.items():
            try:
                numeric_key = int(key)
            except (TypeError, ValueError) as exc:
                raise SaveError(f"MapMgr area {area} map_a key is invalid") from exc
            out += struct.pack(
                "<IB",
                checked_u32(numeric_key, f"MapMgr.area.{area}.map_a.key"),
                checked_u8(value, f"MapMgr.area.{area}.map_a.value"),
            )
        out += struct.pack("<I", len(map_b))
        for key, value in map_b.items():
            try:
                numeric_key = int(key)
            except (TypeError, ValueError) as exc:
                raise SaveError(f"MapMgr area {area} map_b key is invalid") from exc
            out += struct.pack("<I", checked_u32(numeric_key, f"MapMgr.area.{area}.map_b.key"))
            out += encode_record_string(value)

    out += _encode_map_tail(tail)
    return bytes(out)


def _rebuild_map_manager_selected(
    blob: bytes,
    requested: Any,
    jelly_catalogue: dict[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    if not isinstance(requested, dict):
        raise SaveError("editable.MapMgr must be an object containing 'selected'")
    if set(requested) != {"selected"}:
        raise SaveError("editable.MapMgr must contain only 'selected'")
    selected_request = requested["selected"]
    selected_field = "editable.MapMgr.selected"

    catalogue_areas = jelly_catalogue.get("areas")
    if not isinstance(catalogue_areas, dict) or not catalogue_areas:
        raise SaveError("MapMgr selected requires a valid Jelly Lab catalogue")

    requested_area, requested_level = _map_coordinate_checked(
        selected_request,
        field=selected_field,
        catalogue_areas=catalogue_areas,
        allow_final_one_past=True,
    )
    progression_order, coordinate_to_global = _map_catalogue_progression(jelly_catalogue)
    final_area, final_level = progression_order[-1]
    finish_sentinel = (
        requested_area == final_area and requested_level == final_level + 1
    )
    if finish_sentinel:
        target_area, target_level = final_area, final_level
        target_global = len(progression_order)
    else:
        target_area, target_level = requested_area, requested_level
        target_global = coordinate_to_global[(target_area, target_level)]

    current = _parse_map_manager(blob, jelly_catalogue)
    if not isinstance(current, dict):
        raise SaveError("could not decode MapMgr for selected editing")
    decoded = copy.deepcopy(current)

    current_progression = current.get("progression")
    if not isinstance(current_progression, dict):
        raise SaveError("MapMgr has no usable progression coordinate")
    current_coord = (
        current_progression.get("area"), current_progression.get("level_in_area")
    )
    progression_coordinate_global = coordinate_to_global.get(current_coord)
    if not isinstance(progression_coordinate_global, int):
        raise SaveError("MapMgr progression coordinate is outside the Jelly Lab catalogue")

    states = decoded.get("serialized_level_states")
    tail = decoded.get("native_tail")
    if not isinstance(states, list) or not isinstance(tail, dict):
        raise SaveError("MapMgr decoded state is incomplete")

    # Two native layouts are accepted.  Most saves serialize the progression
    # coordinate at the current frontier, so coordinate/global scalar/LevelState
    # count all agree.  A controlled boundary save instead keeps the coordinate
    # on the immediately previous completed level while the scalar and contiguous
    # LevelState prefix have already advanced to the next frontier.  Treat that
    # one-level lag as a valid native representation, not as corruption.
    header_count = len(states)
    progression_scalar = current.get("current_progression_level")
    lagged_frontier = (
        isinstance(progression_scalar, int)
        and 1 <= progression_scalar <= len(progression_order)
        and header_count == progression_scalar
        and progression_coordinate_global + 1 == progression_scalar
    )
    effective_current_global = (
        progression_scalar if lagged_frontier else progression_coordinate_global
    )
    effective_current_coord = progression_order[effective_current_global - 1]

    old_selected = copy.deepcopy(current.get("selected"))
    requested_view = {"area": requested_area, "level_in_area": requested_level}
    target_view = {"area": target_area, "level_in_area": target_level}
    report: dict[str, Any] = {
        "requested_target": copy.deepcopy(requested_view),
        "target": copy.deepcopy(target_view),
        "finish_sentinel": finish_sentinel,
        "target_global_level": target_global,
        "old_progression": copy.deepcopy(current_progression),
        "old_progression_coordinate_global_level": progression_coordinate_global,
        "old_progression_global_level": effective_current_global,
        "native_progression_coordinate_lagged_one": lagged_frontier,
        "old_selected": old_selected,
        "added_level_states": [],
        "area_state_changes": [],
        "area_jelly_machine_changes": [],
    }

    # A target strictly behind the effective frontier is selection-only.  It never
    # rolls progression, areas, fruit totals, rewards, or LevelState history backward.
    if target_global < effective_current_global:
        target_exists = any(
            isinstance(row, dict)
            and row.get("area") == target_area
            and row.get("level_in_area") == target_level
            for row in states
        )
        if not target_exists:
            raise SaveError(
                "MapMgr selection target is behind progression but has no serialized LevelState"
            )
        if old_selected == target_view:
            report["mode"] = "no_change"
            report["new_selected"] = copy.deepcopy(old_selected)
            return blob, report
        tail["pair_34_38"] = [target_area - 1, target_level - 1]
        report["mode"] = "select_existing"
        report["new_selected"] = copy.deepcopy(target_view)
        rebuilt = _encode_map_manager(decoded)
        return rebuilt, report

    # In the one-level-lag native representation, selecting the already-current
    # frontier is still selection-only.  Do not rewrite the progression coordinate
    # merely to canonicalize an otherwise valid save.
    if lagged_frontier and target_global == effective_current_global:
        target_exists = any(
            isinstance(row, dict)
            and row.get("area") == target_area
            and row.get("level_in_area") == target_level
            for row in states
        )
        if not target_exists:
            raise SaveError(
                "MapMgr current frontier has no serialized LevelState"
            )
        if old_selected == target_view:
            report["mode"] = "no_change"
            report["new_selected"] = copy.deepcopy(old_selected)
            return blob, report
        tail["pair_34_38"] = [target_area - 1, target_level - 1]
        report["mode"] = "select_current_progression"
        report["new_selected"] = copy.deepcopy(target_view)
        rebuilt = _encode_map_manager(decoded)
        return rebuilt, report

    # Forward skipping relies on the observed/native invariant that the saved
    # LevelState set is exactly the contiguous prefix through the effective
    # current frontier.  Accept the canonical coordinate layout and the observed
    # one-level-lag boundary layout above; reject every other disagreement.
    if (
        not isinstance(progression_scalar, int)
        or not 1 <= progression_scalar <= len(progression_order)
        or header_count != progression_scalar
        or progression_coordinate_global not in {
            progression_scalar, progression_scalar - 1
        }
    ):
        raise SaveError(
            "MapMgr forward skip refused: progression coordinate, header level-state count, "
            "and native progression scalar are not synchronized"
        )
    existing_coordinates: set[tuple[int, int]] = set()
    for row in states:
        if not isinstance(row, dict):
            raise SaveError("MapMgr contains an invalid existing LevelState")
        coord = (row.get("area"), row.get("level_in_area"))
        if not all(isinstance(v, int) for v in coord):
            raise SaveError("MapMgr contains a LevelState with invalid coordinates")
        if coord in existing_coordinates:
            raise SaveError(f"MapMgr contains duplicate LevelState coordinate {coord}")
        existing_coordinates.add(coord)
    expected_prefix = set(progression_order[:effective_current_global])
    if existing_coordinates != expected_prefix:
        raise SaveError(
            "MapMgr forward skip refused: existing LevelStates are not the expected contiguous progression prefix"
        )

    # Build an index over the existing contiguous prefix.  Forward skipping now
    # fabricates a fully completed prerequisite chain: every level traversed from
    # the current frontier through the level immediately before the target gets
    # all three fruits and TargetValue3 objective progress.  Older levels that
    # were already behind progression are left untouched.  The target itself
    # remains a fresh, unattempted frontier with zero fruits/progress.
    coordinate_rows: dict[tuple[int, int], dict[str, Any]] = {}
    for row in states:
        coordinate_rows[(row["area"], row["level_in_area"])] = row

    # The skipped prerequisites are exactly the levels traversed by this forward
    # operation: current progression through target-1.  Do not retroactively
    # change unrelated older levels that the player naturally passed with only
    # one or two fruits.
    if finish_sentinel:
        # One-past-final means there is no fresh target beyond the catalogue.
        # Every level from the current frontier through the real final level is
        # therefore a skipped prerequisite and must be completed to 3 fruits.
        prerequisite_globals = list(range(effective_current_global, target_global + 1))
    else:
        prerequisite_globals = (
            list(range(effective_current_global, target_global))
            if target_global > effective_current_global
            else []
        )

    # Validate all required target_value3 metadata before mutating anything.
    prerequisite_targets: dict[tuple[int, int], int] = {}
    for global_level in prerequisite_globals:
        area, level = progression_order[global_level - 1]
        prerequisite_targets[(area, level)] = _map_target_value3_checked(
            jelly_catalogue, area, level
        )

    report["promoted_skipped_levels"] = []
    report["area_fruit_changes"] = []
    area_fruit_deltas: dict[int, int] = {}

    # Promote every traversed prerequisite to all three fruits.
    for global_level in prerequisite_globals:
        area, level = progression_order[global_level - 1]
        coord = (area, level)
        row = coordinate_rows.get(coord)
        if row is None:
            native = _new_map_level_state(
                area, level,
                unknown_10=_map_initializer_unknown_10(jelly_catalogue, area, level),
            )
            row = {
                "area": area,
                "level_in_area": level,
                "fruits": 0,
                "native": native,
            }
            states.append(row)
            coordinate_rows[coord] = row
            report["added_level_states"].append({
                "global_level": global_level,
                "area": area,
                "level_in_area": level,
                "role": "skipped_prerequisite",
            })

        native = row.get("native")
        change = _complete_map_level_state(
            native, target_value3=prerequisite_targets[coord]
        )
        row["fruits"] = native.get("fruits") if isinstance(native, dict) else row.get("fruits")
        if change.get("changed"):
            area_fruit_deltas[area] = area_fruit_deltas.get(area, 0) + int(change.get("fruit_delta", 0))
            report["promoted_skipped_levels"].append({
                "global_level": global_level,
                "area": area,
                "level_in_area": level,
                **change,
            })

    # A genuinely forward target is created as a fresh unattempted frontier.
    # If target equals current progression (repair mode), that frontier already
    # exists and is preserved exactly.
    target_coord = (target_area, target_level)
    if finish_sentinel:
        # The promotion loop above creates the real final LevelState when it did
        # not exist yet. Never serialize the synthetic one-past-end coordinate.
        if target_coord not in coordinate_rows:
            raise SaveError("MapMgr finish sentinel did not materialize the real final LevelState")
    elif target_global > effective_current_global:
        if target_coord in coordinate_rows:
            raise SaveError("MapMgr forward skip target unexpectedly already exists")
        target_native = _new_map_level_state(
            target_area, target_level,
            unknown_10=_map_initializer_unknown_10(
                jelly_catalogue, target_area, target_level
            ),
        )
        states.append({
            "area": target_area,
            "level_in_area": target_level,
            "fruits": 0,
            "native": target_native,
        })
        coordinate_rows[target_coord] = states[-1]
        report["added_level_states"].append({
            "global_level": target_global,
            "area": target_area,
            "level_in_area": target_level,
            "role": "fresh_target",
        })
    elif target_coord not in coordinate_rows:
        raise SaveError("MapMgr current progression target has no serialized LevelState")

    areas = decoded.get("areas")
    if not isinstance(areas, dict):
        raise SaveError("MapMgr decoded areas are missing")

    # Area aggregate fruit counts must match the fabricated three-fruit LevelState
    # promotions.  Reward maps remain untouched: skipping unlocks progression but
    # does not pretend that area reward UI/claims were naturally processed.
    for area, delta in sorted(area_fruit_deltas.items()):
        native_area = areas.get(str(area))
        if not isinstance(native_area, dict):
            raise SaveError(f"MapMgr native area {area} is missing")
        old_fruits = checked_u32(native_area.get("fruits"), f"MapMgr.area.{area}.fruits")
        new_fruits = checked_u32(old_fruits + delta, f"MapMgr.area.{area}.fruits")
        cat_area = catalogue_areas.get(str(area))
        max_fruits = cat_area.get("max_fruits") if isinstance(cat_area, dict) else None
        if isinstance(max_fruits, int) and new_fruits > max_fruits:
            raise SaveError(
                f"MapMgr skip would raise Area {area} fruits above catalogue maximum"
            )
        native_area["fruits"] = new_fruits
        report["area_fruit_changes"].append({
            "area": area, "old": old_fruits, "new": new_fruits, "delta": delta
        })

    # All areas strictly before the target area are crossed/open.  For the
    # one-past-final finish sentinel, also mark the real final area crossed/open.
    # Controlled normal-area and final-area transition saves show that crossing
    # a Jelly Machine/area boundary requires TWO native updates on every crossed
    # area: state -> 1 and map_a[0] -> 1.  Preserve all other map_a/map_b entries.
    area_state_stop = target_area + 1 if finish_sentinel else target_area
    for area in range(1, area_state_stop):
        native_area = areas.get(str(area))
        if not isinstance(native_area, dict):
            raise SaveError(f"MapMgr native area {area} is missing")

        old_state = native_area.get("state")
        if old_state != 1:
            native_area["state"] = 1
            report["area_state_changes"].append({"area": area, "old": old_state, "new": 1})

        # Populate the Jelly Machine transition marker only for areas actually
        # crossed by this forward skip.  Do not retroactively repair unrelated
        # historical areas during a no-op/current-selection encode.  The finish
        # sentinel is the one exception: when already sitting on the final real
        # level it still unlocks the final Jelly Machine.
        crossed_by_forward_skip = (
            target_global > effective_current_global
            and area >= effective_current_coord[0]
        )
        unlock_final_machine_now = finish_sentinel and area == final_area
        if crossed_by_forward_skip or unlock_final_machine_now:
            map_a = native_area.get("map_a")
            if not isinstance(map_a, dict):
                raise SaveError(f"MapMgr native area {area}.map_a is missing")
            old_machine_state = map_a.get("0")
            if old_machine_state != 1:
                map_a["0"] = 1
                change = {
                    "area": area,
                    "map_a_key": 0,
                    "old": old_machine_state,
                    "new": 1,
                }
                report["area_jelly_machine_changes"].append(change)
                if unlock_final_machine_now:
                    report["final_jelly_machine_change"] = copy.deepcopy(change)

    tail["pair_34_38"] = [target_area - 1, target_level - 1]  # selected
    tail["fields_2c_30"] = [target_area - 1, target_level - 1]  # progression
    tail["field_4c"] = target_global
    decoded["current_progression_level"] = target_global
    decoded["selected"] = copy.deepcopy(target_view)
    decoded["progression"] = copy.deepcopy(target_view)

    if finish_sentinel:
        report["mode"] = "finish_jelly_lab_three_fruits"
    elif target_global > effective_current_global:
        report["mode"] = "skip_forward_three_fruit_prerequisites"
    elif report["promoted_skipped_levels"] or report["area_state_changes"]:
        report["mode"] = "repair_current_progression"
    elif old_selected != target_view:
        report["mode"] = "select_current_progression"
    else:
        report["mode"] = "no_change"
    report["new_selected"] = copy.deepcopy(target_view)
    report["new_progression"] = copy.deepcopy(target_view)
    report["new_progression_global_level"] = target_global
    report["last_played_preserved"] = copy.deepcopy(current.get("last_played"))

    rebuilt = _encode_map_manager(decoded)
    reparsed = _parse_map_manager(rebuilt, jelly_catalogue)
    if not isinstance(reparsed, dict):
        raise SaveError("MapMgr selected rebuild failed its own native parse check")
    reparsed_states = reparsed.get("serialized_level_states")
    if not isinstance(reparsed_states, list) or len(reparsed_states) != target_global:
        raise SaveError("MapMgr selected rebuild produced the wrong LevelState count")
    if reparsed.get("progression") != target_view or reparsed.get("selected") != target_view:
        raise SaveError("MapMgr selected rebuild produced the wrong target coordinate")
    return rebuilt, report


def apply_editable(
    records_db: dict[str, Any],
    editable: dict[str, Any],
    jelly_catalogue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(editable, dict):
        raise SaveError("editable must be an object")
    records = records_db.get("records")
    if not isinstance(records, dict):
        raise SaveError("RecordDB root is missing records")
    unknown = set(editable) - EDITABLE_MANAGERS
    if unknown:
        raise SaveError("unsupported editable managers: " + ", ".join(sorted(unknown)))

    report: dict[str, Any] = {}

    player_req = editable.get("Player")
    if player_req is not None:
        if not isinstance(player_req, dict):
            raise SaveError("editable.Player must be an object")
        player = records.get("Player")
        if not isinstance(player, dict):
            raise SaveError("save has no Player record")
        blob = decode_blob(player, "Player")
        prefix = _parse_player_prefix(bytes(blob))

        allowed_player = {
            "banana_count", "token_count", "minion_launchers", "free_revives",
            "base_despicable_multiplier", "perk_counters"
        }
        extra = set(player_req) - allowed_player
        if extra:
            raise SaveError("unsupported editable.Player fields: " + ", ".join(sorted(extra)))

        old_tokens = prefix.token_count
        old_bananas = prefix.banana_count
        new_tokens = checked_u32(player_req.get("token_count", old_tokens), "editable.Player.token_count")
        new_bananas = checked_u32(player_req.get("banana_count", old_bananas), "editable.Player.banana_count")
        wallet = patch_wallet_shadow(blob, old_tokens, old_bananas, new_tokens, new_bananas)
        patch_u32(blob, prefix.token_offset, new_tokens, "token_count")
        patch_u32(blob, prefix.banana_offset, new_bananas, "banana_count")

        old_launchers = prefix.minion_launchers
        old_revives = prefix.free_revives
        new_launchers = checked_u32(
            player_req.get("minion_launchers", old_launchers),
            "editable.Player.minion_launchers",
        )
        new_revives = checked_u32(
            player_req.get("free_revives", old_revives),
            "editable.Player.free_revives",
        )
        patch_u32(blob, prefix.launcher_offset, new_launchers, "minion_launchers")
        patch_u32(blob, prefix.revive_offset, new_revives, "free_revives")

        old_multiplier: int | None = None
        new_multiplier: int | None = None
        if "base_despicable_multiplier" in player_req:
            old_multiplier = prefix.base_despicable_multiplier
            new_multiplier = checked_u32(
                player_req["base_despicable_multiplier"],
                "editable.Player.base_despicable_multiplier",
            )
            struct.pack_into("<I", blob, prefix.multiplier_offset, new_multiplier)

        player_report: dict[str, Any] = {
            "banana_count": {"old": old_bananas, "new": new_bananas},
            "token_count": {"old": old_tokens, "new": new_tokens},
            "minion_launchers": {"old": old_launchers, "new": new_launchers},
            "free_revives": {"old": old_revives, "new": new_revives},
            "wallet_shadow": wallet,
        }
        if old_multiplier is not None and new_multiplier is not None:
            player_report["base_despicable_multiplier"] = {
                "old": old_multiplier, "new": new_multiplier
            }
        if "perk_counters" in player_req:
            rebuilt_player, counter_report = _rebuild_player_perk_counters(
                bytes(blob), player_req["perk_counters"]
            )
            blob = bytearray(rebuilt_player)
            player_report["perk_counters"] = counter_report
        player["data"] = bytes(blob)
        player["_aux"] = len(blob)
        report["Player"] = player_report

    inventory_req = editable.get("OnlineInventoryMgr")
    if inventory_req is not None:
        if not isinstance(inventory_req, dict) or set(inventory_req) != {"items"}:
            raise SaveError("editable.OnlineInventoryMgr must contain only 'items'")
        record = records.get("OnlineInventoryMgr")
        if not isinstance(record, dict):
            raise SaveError("save has no OnlineInventoryMgr record")
        blob = decode_blob(record, "OnlineInventoryMgr")
        rebuilt_inventory, inventory_report = _rebuild_online_inventory_items(
            bytes(blob), inventory_req["items"]
        )
        blob = bytearray(rebuilt_inventory)
        record["data"] = bytes(blob)
        record["_aux"] = len(blob)
        report["OnlineInventoryMgr"] = inventory_report

    evil_req = editable.get("EvilMinionMgr")
    if evil_req is not None:
        if not isinstance(evil_req, dict) or set(evil_req) != {"evil_minion_timer_seconds"}:
            raise SaveError(
                "editable.EvilMinionMgr must contain only evil_minion_timer_seconds"
            )
        record = records.get("EvilMinionMgr")
        if not isinstance(record, dict):
            raise SaveError("save has no EvilMinionMgr record")
        blob = decode_blob(record, "EvilMinionMgr")
        timer_offset, old = _evil_minion_timer(bytes(blob))
        new = checked_u32(
            evil_req["evil_minion_timer_seconds"],
            "editable.EvilMinionMgr.evil_minion_timer_seconds",
        )
        patch_u32(blob, timer_offset, new, "evil_minion_timer_seconds")
        record["data"] = bytes(blob)
        record["_aux"] = len(blob)
        report["EvilMinionMgr"] = {
            "evil_minion_timer_seconds": {"old": old, "new": new}
        }


    map_req = editable.get("MapMgr")
    if map_req is not None:
        if jelly_catalogue is None:
            raise SaveError("editable.MapMgr requires a valid Jelly Lab catalogue")
        record = records.get("MapMgr")
        if not isinstance(record, dict):
            raise SaveError("save has no MapMgr record")
        blob = bytes(decode_blob(record, "MapMgr"))
        rebuilt_map, map_report = _rebuild_map_manager_selected(blob, map_req, jelly_catalogue)
        if rebuilt_map != blob:
            record["data"] = rebuilt_map
            record["_aux"] = len(rebuilt_map)
        report["MapMgr"] = map_report

    return report


def encode_record_string(value: str) -> bytes:
    if not isinstance(value, str):
        raise SaveError('record name/string value must be a string')
    raw = value.encode('utf-8')
    if len(raw) > 65535:
        raise SaveError('record name/string exceeds 65535 encoded bytes')
    return struct.pack('<H', len(raw)) + raw

def encode_record_value(record: dict[str, Any]) -> bytes:
    if not isinstance(record, dict):
        raise SaveError('each record value must be an object')
    try:
        type_id = int(record['_type'])
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError('record is missing a valid _type') from exc
    body: bytes
    aux: int
    if type_id == 0:
        body = record.get("data", b"")
        if not isinstance(body, bytes):
            raise SaveError("type-0 record is missing raw bytes")
        aux = len(body)
    elif type_id in (1, 2, 3, 4, 5):
        expected_size = {1: 4, 2: 8, 3: 4, 4: 4, 5: 8}[type_id]
        body = record.get("data")
        if not isinstance(body, bytes) or len(body) != expected_size:
            raise SaveError(f"type-{type_id} record must contain {expected_size} raw bytes")
        aux = checked_u32(record.get("_aux", 0), f"type-{type_id} _aux")
    elif type_id == 6:
        body = encode_record_string(record.get('value', ''))
        aux = checked_u32(record.get('_aux', 0), 'type-6 _aux')
    elif type_id == 7:
        body = bytes(decode_blob(record, '<blob>'))
        aux = len(body)
    elif type_id == 8:
        nested = record.get('value')
        if not isinstance(nested, dict):
            raise SaveError("type-8 record is missing nested 'value' RecordDB")
        body = encode_record_db(nested)
        aux = checked_u32(record.get('_aux', 0), 'type-8 _aux')
    else:
        raise SaveError(f'unsupported RecordDB type {type_id}')
    return struct.pack('<BI', type_id, aux) + body

def ordered_entries(database: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    records = database.get('records')
    if not isinstance(records, dict):
        raise SaveError("RecordDB object is missing a 'records' object")
    entries: list[tuple[str, dict[str, Any]]] = []
    duplicate_names: set[str] = set()
    duplicates = database.get('_duplicates', {})
    if isinstance(duplicates, dict):
        for name, group in duplicates.items():
            if not isinstance(group, list):
                raise SaveError(f'duplicate group {name!r} must be a list')
            duplicate_names.add(name)
            for record in group:
                if not isinstance(record, dict):
                    raise SaveError(f'duplicate record {name!r} must be an object')
                entries.append((name, record))
    for name, record in records.items():
        if not isinstance(record, dict):
            raise SaveError(f'record {name!r} must be an object')
        if name not in duplicate_names:
            entries.append((name, record))

    def sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
        index = item[1].get('_record_index')
        return (index if isinstance(index, int) else 1 << 60, item[0])
    entries.sort(key=sort_key)
    return entries

def encode_record_db(database: dict[str, Any]) -> bytes:
    entries = ordered_entries(database)
    out = bytearray(struct.pack('<I', len(entries)))
    for name, record in entries:
        out += encode_record_string(name)
        out += encode_record_value(record)
    return bytes(out)

def rewrite_template(template: bytes, logical_payload: bytes) -> tuple[bytes, int, int]:
    """Rebuild RedundantStream using the template's native replica markers."""
    layout = _parse_redundant_stream_layout(template)
    new_crc = zlib.crc32(logical_payload) & MAX_U32
    header = layout.header_marker + struct.pack("<II", new_crc, len(logical_payload))

    out = bytearray()
    out += header * 2
    for _ in range(layout.redundancy - 1):
        out += layout.payload_marker
        out += logical_payload
        out += header * 2
    out += header * 5
    out += layout.payload_marker
    out += logical_payload
    return bytes(out), layout.redundancy, 2 * layout.redundancy + 5

# Intentionally hardcoded: this editor targets this exact Windows Store package.
# Do not replace this with wildcard package discovery.
PACKAGE_FAMILY_NAME = "MinionRushModded_t5wpntz2y2kfm"
DEFAULT_CLEAN_JSON_NAME = "savegame.json"


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _game_localstate_dir() -> Path:
    """Return the LocalState directory for the supported Windows Store package."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise SaveError("LOCALAPPDATA environment variable is not set")
    return Path(local_app_data) / "Packages" / PACKAGE_FAMILY_NAME / "LocalState"

def _installed_save_path() -> Path:
    return _game_localstate_dir() / "savegame"


def _backup_path_for_save(savegame: Path) -> Path:
    """Return the backup path associated with the selected save file."""
    return savegame.with_name(savegame.name + ".bak")


def _selected_save_path(args: argparse.Namespace) -> Path:
    """Use an explicit SAVEGAME argument, otherwise the supported LocalState save."""
    savegame = getattr(args, "savegame", None)
    return savegame if savegame is not None else _installed_save_path()


def _default_clean_json_path() -> Path:
    return _script_dir() / DEFAULT_CLEAN_JSON_NAME


def _decode_save(path: Path, args: argparse.Namespace) -> tuple[dict[str, Any], RedundantPayload, bytes, int]:
    physical = path.read_bytes()
    redundant = extract_redundant_payload(physical)
    plaintext = unwrap_inner_payload(redundant.payload, DEFAULT_XTEA_KEY)
    recorddb_bytes, inner_crc32 = split_and_validate_recorddb_plaintext(plaintext)
    rr = Reader(recorddb_bytes, name="decrypted RecordDB")
    parsed = parse_record_db(rr, max_depth=getattr(args, "max_depth", 64))
    remaining = rr.remaining()
    if remaining != 0:
        raise SaveError(f"RecordDB parser left {remaining} trailing bytes")
    jelly_catalogue = _load_jelly_lab_catalogue(args)
    add_decoded_fields(parsed, jelly_catalogue)
    return parsed, redundant, recorddb_bytes, inner_crc32


def _json_dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path, *, what: str = "JSON") -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SaveError(f"invalid {what}: {exc}") from exc
    if not isinstance(obj, dict):
        raise SaveError(f"{what} root must be an object")
    return obj


def _clean_player_editable(records: dict[str, Any]) -> dict[str, Any] | None:
    player = records.get("Player")
    if not isinstance(player, dict) or not isinstance(player.get("decoded"), dict):
        return None
    d = player["decoded"]
    out = {
        key: d[key]
        for key in (
            "banana_count", "token_count", "minion_launchers",
            "free_revives", "base_despicable_multiplier",
        )
        if key in d
    }
    counters = {name: 0 for name in KNOWN_PLAYER_COUNTER_NAMES}
    if isinstance(d.get("perk_counters"), dict):
        counters.update(d["perk_counters"])
    out["perk_counters"] = dict(sorted(counters.items()))
    return out


def _clean_inventory_editable(records: dict[str, Any]) -> dict[str, Any] | None:
    inventory = records.get("OnlineInventoryMgr")
    if not isinstance(inventory, dict) or not isinstance(inventory.get("decoded"), dict):
        return None
    raw_items = inventory["decoded"].get("items")
    if not isinstance(raw_items, dict):
        return None
    items = {name: 0 for name in KNOWN_ONLINE_INVENTORY_NAMES}
    items.update(
        {name: value for name, value in raw_items.items() if not _online_inventory_item_is_hidden(name)}
    )
    return {"items": dict(sorted(items.items()))}


def _clean_evil_editable(records: dict[str, Any]) -> dict[str, Any] | None:
    evil = records.get("EvilMinionMgr")
    if not isinstance(evil, dict) or not isinstance(evil.get("decoded"), dict):
        return None
    timer = evil["decoded"].get("evil_minion_timer_seconds")
    if not isinstance(timer, int):
        return None
    return {"evil_minion_timer_seconds": timer}


def _clean_map_editable(records: dict[str, Any]) -> dict[str, Any] | None:
    map_record = records.get("MapMgr")
    if not isinstance(map_record, dict) or not isinstance(map_record.get("decoded"), dict):
        return None
    selected = map_record["decoded"].get("selected")
    if not isinstance(selected, dict):
        return None
    area = selected.get("area")
    level = selected.get("level_in_area")
    if not isinstance(area, int) or not isinstance(level, int):
        return None
    return {"selected": {"area": area, "level_in_area": level}}


def _clean_map_readonly(records: dict[str, Any]) -> dict[str, Any] | None:
    map_record = records.get("MapMgr")
    if not isinstance(map_record, dict) or not isinstance(map_record.get("decoded"), dict):
        return None
    d = map_record["decoded"]
    out: dict[str, Any] = {}
    for key in (
        "current_progression_level",
        "progression",
        "last_played",
    ):
        if key in d:
            out[key] = copy.deepcopy(d[key])

    raw_areas = d.get("areas")
    if isinstance(raw_areas, dict):
        clean_areas: dict[str, dict[str, Any]] = {}
        for area_name, raw_area in raw_areas.items():
            if not isinstance(raw_area, dict):
                continue
            clean_area = {
                key: copy.deepcopy(value)
                for key, value in raw_area.items()
                if key not in ("map_a", "map_b")
            }
            clean_areas[str(area_name)] = clean_area
        out["areas"] = clean_areas
    return out or None


def _extract_editable(parsed: dict[str, Any]) -> dict[str, Any]:
    records = parsed.get("records")
    if not isinstance(records, dict):
        return {}

    # Keep the exact manager order requested by the user.
    editable: dict[str, Any] = {}
    evil = _clean_evil_editable(records)
    if evil is not None:
        editable["EvilMinionMgr"] = evil
    inventory = _clean_inventory_editable(records)
    if inventory is not None:
        editable["OnlineInventoryMgr"] = inventory
    player = _clean_player_editable(records)
    if player is not None:
        editable["Player"] = player
    map_editable = _clean_map_editable(records)
    if map_editable is not None:
        editable["MapMgr"] = map_editable
    return editable


def _clean_prize_readonly(records: dict[str, Any]) -> dict[str, Any] | None:
    prize_mgr = records.get("PrizePodMgr")
    if not isinstance(prize_mgr, dict) or not isinstance(prize_mgr.get("decoded"), dict):
        return None
    d = prize_mgr["decoded"]
    raw_categories = d.get("categories")
    if not isinstance(raw_categories, dict):
        return None

    categories: dict[str, Any] = {}
    total_pod_count = 0
    named_counts: dict[str, int] = {}
    for category_id in sorted(PRIZE_POD_CATEGORY_INFO):
        info = PRIZE_POD_CATEGORY_INFO[category_id]
        raw_category = raw_categories.get(str(category_id))
        entries: list[dict[str, Any]] = []
        if isinstance(raw_category, dict):
            raw_entries = raw_category.get("entries")
            if isinstance(raw_entries, list):
                for raw_entry in raw_entries:
                    if not isinstance(raw_entry, dict):
                        continue
                    entry: dict[str, Any] = {}
                    index = raw_entry.get("index")
                    prize = raw_entry.get("prize")
                    if isinstance(index, int):
                        entry["index"] = index
                    if isinstance(prize, str):
                        entry["prize"] = prize
                    entries.append(entry)
        count = len(entries)
        total_pod_count += count
        named_counts[info["name"]] = count
        categories[str(category_id)] = {
            "category_name": info["name"],
            "pod_count": count,
            "entries": entries,
        }

    return {
        "total_pod_count": total_pod_count,
        "golden_pod_count": named_counts.get("golden", 0),
        "silver_pod_count": named_counts.get("silver", 0),
        "perks_pod_count": named_counts.get("perks", 0),
        "chinese_pod_count": named_counts.get("chinese", 0),
        "haunted_hustle_pod_count": named_counts.get("haunted_hustle", 0),
        "carnival_pod_count": named_counts.get("carnival", 0),
        "copper_pod_count": named_counts.get("copper", 0),
        "blue_pod_count": named_counts.get("blue", 0),
        "mega_perks_pod_count": named_counts.get("mega_perks", 0),
        "costume_improver_pod_count": named_counts.get("costume_improver", 0),
        "trick_or_treat_pod_count": named_counts.get("trick_or_treat", 0),
        "categories": categories,
    }


def _clean_bonus_readonly(records: dict[str, Any]) -> dict[str, Any] | None:
    bonus = records.get("BonusUpgradeMgr")
    if not isinstance(bonus, dict) or not isinstance(bonus.get("decoded"), dict):
        return None
    entries = bonus["decoded"].get("entries")
    if not isinstance(entries, dict):
        return None
    clean_entries: dict[str, dict[str, int]] = {
        name: {"upgrade_level": 0}
        for name in KNOWN_BONUS_UPGRADE_NAMES
    }
    for name, entry in sorted(entries.items()):
        if not isinstance(entry, dict):
            continue
        level = entry.get("upgrade_level")
        if isinstance(level, int):
            clean_entries[name] = {"upgrade_level": level}
    return {"entries": dict(sorted(clean_entries.items()))}


def _clean_costume_readonly(records: dict[str, Any]) -> dict[str, Any] | None:
    costume = records.get("CostumeMgr")
    if not isinstance(costume, dict) or not isinstance(costume.get("decoded"), dict):
        return None
    d = costume["decoded"]
    out: dict[str, Any] = {}
    for key in (
        "equipped_costume",
        "previous_costume",
        "auxiliary_header_costumes",
    ):
        if key in d:
            out[key] = copy.deepcopy(d[key])
    costumes = _expand_costume_catalog(d)
    out["unlocked_costume_count"] = sum(
        1
        for entry in costumes.values()
        if isinstance(entry, dict) and entry.get("is_unlocked") is True
    )
    out["costumes"] = costumes
    return out


def _clean_achievements_readonly(records: dict[str, Any]) -> dict[str, Any] | None:
    achievements = records.get("AchievementsMgr")
    if not isinstance(achievements, dict) or not isinstance(achievements.get("decoded"), dict):
        return None
    raw_entries = achievements["decoded"].get("entries")
    if not isinstance(raw_entries, dict):
        return None
    entries = {
        name: {"reward_collected": False, "completed": False}
        for name in KNOWN_ACHIEVEMENT_NAMES
    }
    for name, raw_entry in raw_entries.items():
        if name not in entries or not isinstance(raw_entry, dict):
            continue
        entries[name] = {
            "reward_collected": bool(raw_entry.get("reward_collected", False)),
            "completed": bool(raw_entry.get("completed", False)),
        }
    sorted_entries = dict(sorted(entries.items()))
    return {
        "completed_count": sum(1 for entry in sorted_entries.values() if entry["completed"]),
        "entries": sorted_entries,
    }


def _clean_statistics_readonly(records: dict[str, Any]) -> dict[str, Any] | None:
    statistics = records.get("statistics")
    if not isinstance(statistics, dict) or not isinstance(statistics.get("decoded"), dict):
        return None
    stats_decoded = statistics["decoded"]
    clean_statistics: dict[str, Any] = {}
    for key in ("personal_best", "last_run", "cumulative"):
        value = stats_decoded.get(key)
        if isinstance(value, dict):
            clean_statistics[key] = copy.deepcopy(value)
    return clean_statistics or None


def _clean_saveverifier_readonly(records: dict[str, Any]) -> dict[str, Any] | None:
    record = records.get("SaveVerifierMgr")
    if not isinstance(record, dict):
        return None
    if record.get("_type") != 6 or not isinstance(record.get("value"), str):
        return None
    return {"value": record["value"]}


def _extract_readonly(parsed: dict[str, Any]) -> dict[str, Any]:
    records = parsed.get("records")
    if not isinstance(records, dict):
        return {}
    readonly: dict[str, Any] = {}
    projectors = (
        ("PrizePodMgr", _clean_prize_readonly),
        ("BonusUpgradeMgr", _clean_bonus_readonly),
        ("CostumeMgr", _clean_costume_readonly),
        ("MapMgr", _clean_map_readonly),
        ("AchievementsMgr", _clean_achievements_readonly),
        ("statistics", _clean_statistics_readonly),
        ("SaveVerifierMgr", _clean_saveverifier_readonly),
    )
    for name, projector in projectors:
        value = projector(records)
        if value is not None:
            readonly[name] = value
    return readonly


def make_clean_document(parsed: dict[str, Any]) -> dict[str, Any]:
    """Build the two-section user-facing projection."""
    return {
        "editable": _extract_editable(parsed),
        "readonly": _extract_readonly(parsed),
    }


def _template_encryption_flag(template: bytes) -> int:
    """Recover the encryption flag directly from the original physical save."""
    redundant = extract_redundant_payload(template)
    r = Reader(redundant.payload, name="template logical payload")
    marker = r.u8()
    if marker != SAVE_MAGIC:
        raise SaveError(
            f"unexpected logical-stream marker 0x{marker:02X}; expected 0x{SAVE_MAGIC:02X}"
        )
    return r.u32()


def merge_clean_document(clean: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Return a writable copy of the original save's RecordDB baseline."""
    if not isinstance(clean, dict):
        raise SaveError("clean JSON root must be an object")
    extra = set(clean) - {"editable", "readonly"}
    if extra:
        raise SaveError(
            "clean JSON may contain only top-level 'editable' and 'readonly'; unsupported: "
            + ", ".join(sorted(extra))
        )
    if set(clean) != {"editable", "readonly"}:
        missing = {"editable", "readonly"} - set(clean)
        raise SaveError("clean JSON is missing: " + ", ".join(sorted(missing)))
    editable = clean.get("editable")
    readonly = clean.get("readonly")
    if not isinstance(editable, dict):
        raise SaveError("editable must be an object")
    if not isinstance(readonly, dict):
        raise SaveError("readonly must be an object")

    unknown_editable = set(editable) - EDITABLE_MANAGERS
    if unknown_editable:
        raise SaveError(
            "unsupported editable managers: " + ", ".join(sorted(unknown_editable))
        )
    unknown_readonly = set(readonly) - READONLY_MANAGERS
    if unknown_readonly:
        raise SaveError(
            "unsupported readonly managers: " + ", ".join(sorted(unknown_readonly))
        )
    baseline_readonly = _extract_readonly(baseline)
    if readonly != baseline_readonly:
        raise SaveError("readonly section was modified or no longer matches the current savegame")
    return copy.deepcopy(baseline)


def _build_logical_payload_for_flag(recorddb: bytes, key: bytes, encryption_flag: int) -> bytes:
    inner_crc = zlib.crc32(recorddb) & MAX_U32
    plaintext = struct.pack("<I", inner_crc) + recorddb
    if encryption_flag == 0:
        return struct.pack("<BI", SAVE_MAGIC, 0) + plaintext
    # The game always emits at least one zero-padding byte; when the plaintext
    # is already 8-byte aligned it emits a full extra XTEA block of zeros.
    padded_length = ((len(plaintext) // 8) + 1) * 8
    padded = plaintext + b"\x00" * (padded_length - len(plaintext))
    ciphertext = xtea_encrypt(padded, key)
    section_length = 4 + len(ciphertext)
    return struct.pack("<BI", SAVE_MAGIC, encryption_flag) + struct.pack("<II", section_length, len(plaintext)) + ciphertext




def _verify_clean_projection(
    requested: dict[str, Any],
    actual: dict[str, Any],
    *,
    patch_report: dict[str, Any] | None = None,
) -> None:
    requested_editable = copy.deepcopy(requested.get("editable", {}))
    actual_editable = copy.deepcopy(actual.get("editable", {}))

    # A finish sentinel is an editor command, not a serializable map coordinate.
    # The requested one-past-final coordinate (final area, final level + 1) is
    # intentionally canonicalized by the MapMgr writer to the real final level.
    # Compare against that canonical target during post-encode verification.
    map_report = patch_report.get("MapMgr") if isinstance(patch_report, dict) else None
    if isinstance(map_report, dict) and map_report.get("finish_sentinel") is True:
        target = map_report.get("target")
        if (
            isinstance(target, dict)
            and isinstance(requested_editable, dict)
            and isinstance(requested_editable.get("MapMgr"), dict)
        ):
            requested_editable["MapMgr"] = {"selected": copy.deepcopy(target)}

    if requested_editable != actual_editable:
        raise SaveError("post-encode verification: editable values differ after rebuild")

    requested_readonly = copy.deepcopy(requested.get("readonly", {}))
    actual_readonly = copy.deepcopy(actual.get("readonly", {}))
    # MapMgr readonly state is expected to change as a consequence of selected.
    if isinstance(requested_editable, dict) and "MapMgr" in requested_editable:
        if isinstance(requested_readonly, dict):
            requested_readonly.pop("MapMgr", None)
        if isinstance(actual_readonly, dict):
            actual_readonly.pop("MapMgr", None)
    if requested_readonly != actual_readonly:
        raise SaveError("post-encode verification: readonly values unexpectedly changed")


def command_decode(args: argparse.Namespace) -> None:
    savegame = _selected_save_path(args)
    output = _default_clean_json_path()
    parsed, _, _, _ = _decode_save(savegame, args)
    clean = make_clean_document(parsed)
    _json_dump(output, clean)
    print(json.dumps({
        "input": str(savegame),
        "output": str(output),
        "record_count": parsed.get("_record_count"),
    }, indent=2))


def command_verify(args: argparse.Namespace) -> None:
    parsed, redundant, recorddb, inner_crc32 = _decode_save(args.savegame, args)
    report: dict[str, Any] = {
        "verified": True,
        "file": str(args.savegame),
        "physical_size": args.savegame.stat().st_size,
        "valid_redundant_copies": redundant.valid_copies,
        "record_count": parsed.get("_record_count"),
        "recorddb_bytes": len(recorddb),
        "bytes_remaining": 0,
        "inner_crc32": f"{inner_crc32:08x}",
    }
    records = parsed.get("records")
    if isinstance(records, dict):
        map_record = records.get("MapMgr")
        if isinstance(map_record, dict) and isinstance(map_record.get("decoded"), dict):
            d = map_record["decoded"]
            map_summary = {
                key: copy.deepcopy(d[key])
                for key in (
                    "current_progression_level",
                    "area_count",
                    "selected",
                    "progression",
                    "last_played",
                )
                if key in d
            }
            if map_summary:
                report["MapMgr"] = map_summary
    print(json.dumps(report, indent=2))


def _encode_document(
    clean: dict[str, Any],
    baseline: dict[str, Any],
    template: bytes,
    args: argparse.Namespace,
) -> tuple[bytes, dict[str, Any]]:
    document = merge_clean_document(clean, baseline)
    jelly_catalogue = _load_jelly_lab_catalogue(args)
    patch_report = apply_editable(
        document, clean.get("editable", {}), jelly_catalogue=jelly_catalogue
    )
    recorddb = encode_record_db(document)
    key = DEFAULT_XTEA_KEY
    logical_payload = _build_logical_payload_for_flag(
        recorddb, key, _template_encryption_flag(template)
    )
    output, payload_copies, headers = rewrite_template(template, logical_payload)
    return output, {
        "recorddb_length": len(recorddb),
        "logical_payload_length": len(logical_payload),
        "redundant_payload_copies_replaced": payload_copies,
        "header_checksums_updated": headers,
        "output_size": len(output),
        "output_crc32": f"{zlib.crc32(output) & MAX_U32:08x}",
        "patch_report": patch_report,
    }


def command_encode(args: argparse.Namespace) -> None:
    using_localstate_fallback = getattr(args, "savegame", None) is None
    savegame = _selected_save_path(args)
    json_file = _default_clean_json_path()
    backup = _backup_path_for_save(savegame)
    clean = _load_json(json_file, what="clean JSON")

    # The selected save is the authoritative lossless baseline.
    baseline, _, _, _ = _decode_save(savegame, args)
    original = savegame.read_bytes()
    output, report = _encode_document(clean, baseline, original, args)

    temp = savegame.with_name(savegame.name + ".editor.verify.tmp")
    try:
        temp.write_bytes(output)
        verify_args = argparse.Namespace(**vars(args))
        verify_args.savegame = temp
        parsed_after, _, _, _ = _decode_save(temp, verify_args)
        _verify_clean_projection(
            clean,
            make_clean_document(parsed_after),
            patch_report=report.get("patch_report"),
        )

        # Refresh the selected save's .bak on every successful encode so it
        # always contains the immediately previous save.  Write through a
        # temporary file first, then atomically replace an existing backup.
        backup_existed = backup.exists()
        backup_temp = backup.with_name(backup.name + ".editor.tmp")
        try:
            backup_temp.write_bytes(original)
            os.replace(backup_temp, backup)
        finally:
            try:
                backup_temp.unlink()
            except FileNotFoundError:
                pass
        backup_created = not backup_existed
        backup_overwritten = backup_existed
        backup_updated = True
        os.replace(temp, savegame)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass

    print(json.dumps({
        "clean_json": str(json_file),
        "savegame": str(savegame),
        "backup": str(backup),
        "backup_created": backup_created,
        "backup_overwritten": backup_overwritten,
        "backup_updated": backup_updated,
        "backup_contains_original_selected_save": True,
        **report,
        "baseline_source": (
            "installed_savegame" if using_localstate_fallback else "supplied_savegame"
        ),
        "post_encode_verified": True,
        "selected_savegame_overwritten": True,
        "installed_save_overwritten": using_localstate_fallback,
    }, indent=2))


def command_roundtrip(args: argparse.Namespace) -> None:
    parsed, _, recorddb, _ = _decode_save(args.savegame, args)
    original = args.savegame.read_bytes()
    clean = make_clean_document(parsed)
    output, report = _encode_document(clean, parsed, original, args)
    args.output.write_bytes(output)
    identical = output == original
    print(json.dumps({
        "input": str(args.savegame),
        "output": str(args.output),
        "byte_identical": identical,
        "input_size": len(original),
        "output_size": len(output),
        **report,
    }, indent=2))
    if not identical:
        raise SaveError("round-trip output is not byte-identical to input")


def _saveverifier_record(database: dict[str, Any], *, label: str) -> dict[str, Any]:
    records = database.get("records")
    if not isinstance(records, dict):
        raise SaveError(f"{label} save has no RecordDB records")
    record = records.get("SaveVerifierMgr")
    if not isinstance(record, dict):
        raise SaveError(f"{label} save has no SaveVerifierMgr record")
    if record.get("_type") != 6 or not isinstance(record.get("value"), str):
        raise SaveError(
            f"{label} SaveVerifierMgr is not the expected type-6 string record"
        )
    return record


def _non_saveverifier_record_fingerprint(database: dict[str, Any]) -> list[tuple[str, bytes]]:
    """Return exact serialized value bytes for every non-verifier record."""
    return [
        (name, encode_record_value(record))
        for name, record in ordered_entries(database)
        if name != "SaveVerifierMgr"
    ]


def command_transplant_saveverifier(args: argparse.Namespace) -> None:
    donor, _, _, _ = _decode_save(args.donor, args)
    target, _, _, _ = _decode_save(args.target, args)
    donor_record = _saveverifier_record(donor, label="donor")
    target_record = _saveverifier_record(target, label="target")

    old_value = target_record["value"]
    new_value = donor_record["value"]
    before_non_verifier = _non_saveverifier_record_fingerprint(target)

    rebuilt_db = copy.deepcopy(target)
    rebuilt_record = _saveverifier_record(rebuilt_db, label="target")
    # The RecordDB key/order belong to the target.  Transplant the complete
    # serialized verifier value from the donor: type, aux word, and string.
    rebuilt_record["_type"] = donor_record["_type"]
    rebuilt_record["_aux"] = donor_record.get("_aux", 0)
    rebuilt_record["value"] = donor_record["value"]

    recorddb = encode_record_db(rebuilt_db)
    target_physical = args.target.read_bytes()
    logical_payload = _build_logical_payload_for_flag(
        recorddb, DEFAULT_XTEA_KEY, _template_encryption_flag(target_physical)
    )
    output, payload_copies, headers = rewrite_template(target_physical, logical_payload)

    temp = Path(str(args.out) + ".verify.tmp")
    try:
        temp.write_bytes(output)
        verify_args = argparse.Namespace(**vars(args))
        verify_args.savegame = temp
        parsed_after, redundant_after, recorddb_after, inner_crc32_after = _decode_save(
            temp, verify_args
        )
        after_record = _saveverifier_record(parsed_after, label="output")
        if after_record.get("_type") != donor_record.get("_type"):
            raise SaveError("post-transplant verification: SaveVerifierMgr type differs from donor")
        if after_record.get("_aux") != donor_record.get("_aux"):
            raise SaveError("post-transplant verification: SaveVerifierMgr aux differs from donor")
        if after_record.get("value") != donor_record.get("value"):
            raise SaveError("post-transplant verification: SaveVerifierMgr value differs from donor")
        after_non_verifier = _non_saveverifier_record_fingerprint(parsed_after)
        if after_non_verifier != before_non_verifier:
            raise SaveError(
                "post-transplant verification: a non-SaveVerifierMgr RecordDB entry changed"
            )
        os.replace(temp, args.out)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass

    print(json.dumps({
        "donor": str(args.donor),
        "target": str(args.target),
        "output": str(args.out),
        "SaveVerifierMgr": {
            "old": old_value,
            "new": new_value,
            "donor_aux": donor_record.get("_aux"),
        },
        "record_count": parsed_after.get("_record_count"),
        "recorddb_length": len(recorddb_after),
        "logical_payload_length": len(logical_payload),
        "valid_redundant_copies": redundant_after.valid_copies,
        "inner_crc32": f"{inner_crc32_after:08x}",
        "redundant_payload_copies_replaced": payload_copies,
        "header_checksums_updated": headers,
        "non_SaveVerifierMgr_records_unchanged": True,
        "post_transplant_verified": True,
    }, indent=2))


def _add_decode_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--max-depth",
        type=int,
        default=64,
        help="maximum nested RecordDB depth (default: 64)",
    )
    p.add_argument(
        "--jelly-lab-catalog",
        type=Path,
        default=None,
        help=(
            "current Jelly Lab catalogue JSON; default: jelly_lab_catalog.json "
            "beside the save editor; forward skip requires target_value3 metadata"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minion Rush savegame editor")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "decode",
        help=(
            "decode SAVEGAME to savegame.json beside this script; "
            "default: LocalState\\savegame"
        ),
    )
    p.add_argument(
        "savegame",
        nargs="?",
        type=Path,
        help="save file to decode (default: supported package LocalState\\savegame)",
    )
    _add_decode_options(p)
    p.set_defaults(func=command_decode)

    p = sub.add_parser(
        "encode",
        help=(
            "encode savegame.json using SAVEGAME as baseline; preserve the original "
            "as SAVEGAME.bak, then overwrite SAVEGAME with the verified result; "
            "default: LocalState\\savegame"
        ),
    )
    p.add_argument(
        "savegame",
        nargs="?",
        type=Path,
        help="save file to use as baseline/target; original is backed up as SAVEGAME.bak before replacement (default: supported package LocalState\\savegame)",
    )
    _add_decode_options(p)
    p.set_defaults(func=command_encode)

    p = sub.add_parser("verify", help="validate an arbitrary save without modifying it")
    p.add_argument("savegame", type=Path)
    _add_decode_options(p)
    p.set_defaults(func=command_verify)

    p = sub.add_parser(
        "roundtrip",
        help="decode and rebuild a save, requiring byte-identical output",
    )
    p.add_argument("savegame", type=Path)
    p.add_argument("output", type=Path)
    _add_decode_options(p)
    p.set_defaults(func=command_roundtrip)

    p = sub.add_parser(
        "transplant-saveverifier",
        help="copy SaveVerifierMgr from DONOR into TARGET and rebuild TARGET",
    )
    p.add_argument("donor", type=Path, help="save whose SaveVerifierMgr should be copied")
    p.add_argument("target", type=Path, help="save whose other contents should be preserved")
    p.add_argument("out", type=Path, help="rebuilt target save")
    _add_decode_options(p)
    p.set_defaults(func=command_transplant_saveverifier)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
        return 0
    except (OSError, SaveError, ValueError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
