#!/usr/bin/env python3
"""Build the Minion Rush Jelly Lab MapMgr catalogue from a data-mandatory JPK.

USAGE
-----

Extract from an explicitly supplied data-mandatory JPK::

    python extract_jelly_lab_catalog.py DATA_MANDATORY_JPK

If ``DATA_MANDATORY_JPK`` is omitted, the extractor automatically selects the
newest suitable Windows data-mandatory JPK from::

    %LOCALAPPDATA%\\Packages\\GAMELOFTSA.DespicableMeMinionRush_0pp20fcewvvtj\\LocalState\\dlcs

The existing ``--data-mandatory-jpk PATH`` form is retained for compatibility.
``--dlcs-dir PATH`` can still override the directory searched when no JPK is
supplied.  ``--output PATH`` can still override the output file.

Default output::

    jelly_lab_catalog.json beside this script

Dependencies imported directly from beside this script:

    jpk.py
    blibclara_editor.py

Run this file directly so Python places its directory on the import path and
resolves those sibling modules normally.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Optional

import blibclara_editor
import jpk

CATALOG_FORMAT = "minion_rush_jelly_lab_catalog_minimal_v3"
PACKAGE_FAMILY_NAME = "GAMELOFTSA.DespicableMeMinionRush_0pp20fcewvvtj"
JELLY_MAP_AREAS_ORDER = "/MapLevelDef/MapSystem/MapAreasOrder"
JELLY_MAPLIB_ENTRY = "maplib.blibclara"
JELLY_MISSION_ROOT = "/MapMissions/MapMissionDef"


class CatalogError(Exception):
    pass


def _default_dlcs_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "Packages" / PACKAGE_FAMILY_NAME / "LocalState" / "dlcs"


def _jpk_version_key(path: Path) -> tuple[int, int, int, int]:
    match = re.search(
        r"dlc_v(\d+)_data_mandatory_(?:w8wp8|windows)_up(\d+)_data_(\d+)",
        path.name,
        flags=re.IGNORECASE,
    )
    if match:
        return (*map(int, match.groups()), path.stat().st_mtime_ns)

    numbers = [int(x) for x in re.findall(r"\d+", path.name)]
    major, update, data = (numbers + [0, 0, 0])[:3]
    return major, update, data, path.stat().st_mtime_ns


def _find_latest_data_mandatory_jpk(dlcs_dir: Path) -> Path:
    if not dlcs_dir.is_dir():
        raise FileNotFoundError(f"Minion Rush DLC directory not found: {dlcs_dir}")

    candidates = [
        path
        for path in dlcs_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".jpk"
        and "data_mandatory" in path.name.lower()
    ]
    if not candidates:
        raise FileNotFoundError(f"no data-mandatory .jpk found in {dlcs_dir}")

    canonical_re = re.compile(
        r"^dlc_v\d+_data_mandatory_(?:w8wp8|windows)_up\d+_data_\d+\.jpk$",
        flags=re.IGNORECASE,
    )
    canonical = [path for path in candidates if canonical_re.fullmatch(path.name)]
    preferred = canonical or [
        path for path in candidates if "w8wp8" in path.name.lower()
    ] or candidates
    return max(preferred, key=_jpk_version_key)


def _join_clara_path(parent: str, name: Any) -> str:
    child = str(name if name is not None else "").strip("/")
    if not child:
        return parent.rstrip("/")
    if not parent or parent == "/":
        return "/" + child
    return parent.rstrip("/") + "/" + child


def _clara_build_indexes(full: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entities: dict[str, dict[str, Any]] = {}

    def walk(folder: dict[str, Any], path: str) -> None:
        for record in folder.get("records", []):
            if not isinstance(record, dict):
                continue
            child_folder = record.get("folder")
            if record.get("kind") == "folder" and isinstance(child_folder, dict):
                walk(child_folder, _join_clara_path(path, child_folder.get("name")))
                continue

            entity = record.get("entity")
            if record.get("kind") != "entity" or not isinstance(entity, dict):
                continue
            entity_path = _join_clara_path(path, entity.get("name"))
            if entity_path in entities:
                raise CatalogError(f"duplicate Clara entity path: {entity_path}")
            entities[entity_path] = entity

    for library in full.get("libraries", []):
        root = library.get("root_folder") if isinstance(library, dict) else None
        if isinstance(root, dict):
            walk(root, _join_clara_path("", root.get("name")))
    return entities


def _clara_simplify_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_clara_simplify_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("class"), str) and isinstance(value.get("properties"), list):
        return {
            "class": value["class"],
            "name": value.get("name", ""),
            "fields": _clara_fields(value),
        }

    ignored = {
        "raw_base64",
        "original_value",
        "original_name",
        "source_offset",
        "source_size",
        "preamble_hex",
        "opaque_body_base64",
    }
    return {
        key: _clara_simplify_value(item)
        for key, item in value.items()
        if key not in ignored
    }


def _clara_simplify_property(prop: dict[str, Any]) -> Any:
    values = [
        _clara_simplify_value(element.get("value"))
        for element in prop.get("elements", [])
    ]
    if not values:
        return []
    return values[0] if len(values) == 1 else values


def _clara_fields(entity: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(entity, dict):
        return {}
    return {
        prop["name"]: _clara_simplify_property(prop)
        for prop in entity.get("properties", [])
        if isinstance(prop, dict) and isinstance(prop.get("name"), str)
    }


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return []


def _require_u32(value: Any, message: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > 0xFFFFFFFF
    ):
        raise CatalogError(message)
    return value


def _collect_area_missions(
    entities: dict[str, dict[str, Any]], area_number: int
) -> list[tuple[int, str, dict[str, Any]]]:
    prefix = f"Mission{area_number:03d}_"
    root_prefix = JELLY_MISSION_ROOT + "/"
    missions: list[tuple[int, str, dict[str, Any]]] = []

    for path, entity in entities.items():
        if not path.startswith(root_prefix) or entity.get("class") != "MapMission":
            continue
        name = path.rsplit("/", 1)[-1]
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix):]
        if suffix.isdigit():
            missions.append((int(suffix), path, entity))

    missions.sort(key=lambda row: row[0])
    return missions


def build_catalogue(jpk_path: Path) -> dict[str, Any]:
    """Extract the compact per-area and per-level facts required by the save editor."""
    try:
        with jpk._open_zip(jpk_path) as archive:
            entry = jpk._require_unique_entry(archive, JELLY_MAPLIB_ENTRY)
            maplib_data = archive.read(entry)
    except (jpk.JPKError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise CatalogError(
            f"cannot read {JELLY_MAPLIB_ENTRY} from {jpk_path}: {exc}"
        ) from exc

    try:
        full = blibclara_editor.decode_file(maplib_data, JELLY_MAPLIB_ENTRY)
    except blibclara_editor.CodecError as exc:
        raise CatalogError(f"cannot decode {JELLY_MAPLIB_ENTRY}: {exc}") from exc

    entities = _clara_build_indexes(full)
    order_entity = entities.get(JELLY_MAP_AREAS_ORDER)
    if not isinstance(order_entity, dict):
        raise CatalogError(f"{JELLY_MAPLIB_ENTRY}: MapAreasOrder entity is missing")

    area_paths = _as_string_list(_clara_fields(order_entity).get("Areas"))
    if not area_paths:
        raise CatalogError(
            f"{JELLY_MAPLIB_ENTRY}: MapAreasOrder.Areas is empty or malformed"
        )

    areas: dict[str, dict[str, Any]] = {}
    level_defaults: dict[str, dict[str, Any]] = {}

    for area_path in area_paths:
        area_entity = entities.get(area_path)
        if not isinstance(area_entity, dict) or area_entity.get("class") != "MapArea":
            raise CatalogError(
                f"{JELLY_MAPLIB_ENTRY}: invalid MapArea reference {area_path!r}"
            )

        match = re.fullmatch(r".*/MapArea(\d+)", area_path)
        if match is None:
            raise CatalogError(
                f"{JELLY_MAPLIB_ENTRY}: malformed MapArea reference {area_path!r}"
            )
        area_number = int(match.group(1))
        if area_number <= 0:
            raise CatalogError(f"invalid Jelly Lab area number {area_number}")
        if str(area_number) in areas:
            raise CatalogError(f"duplicate Jelly Lab area number {area_number}")

        area_fields = _clara_fields(area_entity)
        max_fruits = _require_u32(
            area_fields.get("RequiredFruitsForBonus2"),
            f"Area {area_number}: RequiredFruitsForBonus2 is missing or malformed",
        )

        missions = _collect_area_missions(entities, area_number)
        if not missions:
            raise CatalogError(f"Area {area_number}: no MapMission entries found")
        mission_suffixes = [suffix for suffix, _, _ in missions]
        if len(set(mission_suffixes)) != len(mission_suffixes):
            raise CatalogError(f"Area {area_number}: duplicate MapMission id")

        level_count = len(missions)
        expected_max_fruits = 3 * level_count
        if max_fruits != expected_max_fruits:
            raise CatalogError(
                f"Area {area_number}: RequiredFruitsForBonus2={max_fruits}, "
                f"but {level_count} levels imply {expected_max_fruits} collectible fruits"
            )

        # Mission suffixes are global mission IDs; level_in_area is the sorted
        # position within this area's missions, not the suffix itself.
        for level_number, (mission_suffix, _, mission_entity) in enumerate(missions, 1):
            mission_fields = _clara_fields(mission_entity)
            mission_id = _require_u32(
                mission_fields.get("MissionId"),
                f"Area {area_number} level {level_number}: MissionId is missing or malformed",
            )
            if mission_id != mission_suffix:
                raise CatalogError(
                    f"Area {area_number} level {level_number}: mission suffix "
                    f"{mission_suffix} does not match MissionId {mission_id}"
                )

            target_values = {
                output_name: _require_u32(
                    mission_fields.get(source_name),
                    f"Area {area_number} level {level_number}: "
                    f"{source_name} is missing or malformed",
                )
                for source_name, output_name in (
                    ("TargetValue1", "target_value1"),
                    ("TargetValue2", "target_value2"),
                    ("TargetValue3", "target_value3"),
                )
            }
            if not (
                target_values["target_value1"]
                <= target_values["target_value2"]
                <= target_values["target_value3"]
            ):
                raise CatalogError(
                    f"Area {area_number} level {level_number}: mission target "
                    "thresholds are not nondecreasing"
                )

            force_location_raw = mission_fields.get("ForceLocation")
            force_location = (
                force_location_raw.get("value")
                if isinstance(force_location_raw, dict)
                else force_location_raw
            )
            force_location = _require_u32(
                force_location,
                f"Area {area_number} level {level_number}: "
                "ForceLocation.value is missing or malformed",
            )

            level_defaults[f"{area_number}:{level_number}"] = {
                **target_values,
                "force_location": force_location,
            }

        rewards: dict[str, str] = {}
        for key, field_name in (("0", "BonusType1"), ("1", "BonusType2")):
            reward_path = area_fields.get(field_name)
            if not isinstance(reward_path, str) or not reward_path:
                raise CatalogError(
                    f"Area {area_number}: {field_name} is missing or malformed"
                )
            reward_name = reward_path.rsplit("/", 1)[-1]
            if not reward_name:
                raise CatalogError(
                    f"Area {area_number}: {field_name} has an empty reward name"
                )
            rewards[key] = reward_name

        areas[str(area_number)] = {
            "max_fruits": max_fruits,
            "level_count": level_count,
            "rewards": rewards,
        }

    area_numbers = sorted(map(int, areas))
    if area_numbers != list(range(1, len(area_numbers) + 1)):
        raise CatalogError(
            f"Jelly Lab areas must be contiguous from 1; found {area_numbers}"
        )

    expected_total_levels = sum(area["level_count"] for area in areas.values())
    if len(level_defaults) != expected_total_levels:
        raise CatalogError(
            "internal catalogue mismatch: "
            f"{len(level_defaults)} level defaults for {expected_total_levels} levels"
        )

    return {
        "format": CATALOG_FORMAT,
        "areas": areas,
        "level_defaults": level_defaults,
    }


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract minimal Minion Rush Jelly Lab MapMgr catalogue from an optional "
            "data-mandatory JPK; if omitted, auto-select one from LocalState\\dlcs"
        )
    )
    parser.add_argument(
        "data_mandatory_jpk_input",
        nargs="?",
        type=Path,
        metavar="DATA_MANDATORY_JPK",
        help=(
            "data-mandatory JPK to extract; if omitted, auto-select the newest suitable "
            "Windows archive from LocalState\\dlcs"
        ),
    )
    parser.add_argument(
        "--dlcs-dir",
        type=Path,
        default=None,
        help=(
            "Minion Rush LocalState\\dlcs directory to search when no JPK is supplied. "
            "Default: %%LOCALAPPDATA%%\\Packages\\"
            f"{PACKAGE_FAMILY_NAME}\\LocalState\\dlcs"
        ),
    )
    parser.add_argument(
        "--data-mandatory-jpk",
        dest="data_mandatory_jpk_option",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "compatibility form for supplying the exact data-mandatory JPK; do not use "
            "together with positional DATA_MANDATORY_JPK"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output JSON; default: jelly_lab_catalog.json beside this script",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if (
            args.data_mandatory_jpk_input is not None
            and args.data_mandatory_jpk_option is not None
        ):
            raise CatalogError(
                "supply the data-mandatory JPK either positionally or with "
                "--data-mandatory-jpk, not both"
            )

        supplied_jpk = (
            args.data_mandatory_jpk_input
            if args.data_mandatory_jpk_input is not None
            else args.data_mandatory_jpk_option
        )

        if supplied_jpk is not None:
            jpk_path = supplied_jpk
            if not jpk_path.is_file():
                raise FileNotFoundError(f"data-mandatory JPK not found: {jpk_path}")
        else:
            dlcs_dir = args.dlcs_dir or _default_dlcs_dir()
            jpk_path = _find_latest_data_mandatory_jpk(dlcs_dir)

        output = args.output or Path(__file__).resolve().with_name("jelly_lab_catalog.json")
        catalog = build_catalogue(jpk_path)
        _write_json(output, catalog)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "data_mandatory_jpk": str(jpk_path),
                    "areas": len(catalog["areas"]),
                    "levels": len(catalog["level_defaults"]),
                    "level_defaults": len(catalog["level_defaults"]),
                    "format": CATALOG_FORMAT,
                },
                indent=2,
            )
        )
        return 0
    except (OSError, CatalogError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
