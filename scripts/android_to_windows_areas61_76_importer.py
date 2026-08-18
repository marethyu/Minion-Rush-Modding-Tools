#!/usr/bin/env python3
"""Import Android Jelly Lab Areas 61-76 into Windows up23 using clone_last_area.py.

This converter targets the current cleaned ``clone_last_area.py`` and reuses its
validated Clara tree helpers plus the ``blibclara_library_editor.py`` codec. Older helper
APIs are intentionally unsupported. All 16 clone steps run in memory and the result
is encoded/verified once, avoiding 16 full-file decode/encode passes.  Android semantics are applied only after all Windows
Area/MapMission records have been structurally cloned.

Requirements:
    Current ``clone_last_area.py`` and ``blibclara_library_editor.py`` must be importable.
    Normally keep all three scripts in the same directory.

Usage:
    python android_to_windows_areas61_76_importer.py \
        android_maplib.blibclara windows_maplib_original.blibclara output.blibclara

Optional report:
    --report conversion_report.json

Tested well for maplib.blibclara from 5.7.0 Mega Mod v5 by Usto67

Policy retained from the tested converter:
  * structurally clone Windows Areas 60 -> 61 -> ... -> 76 using imported clone_last_area.py helpers
  * Android dual objectives become TargetA only
  * dual->single missions receive the most common stock-Windows 2-perk pair for TargetA
  * Android balthazarLair Location -> avl; Android prison Location -> fort
  * any other Android-only Location name -> fort
  * Android balthazarLair ForceLocation -> avl; Android prison ForceLocation -> fort
  * any other unknown ForceLocation -> fort
  * Android BAL background -> Windows AVL background; Android PRI background -> Windows FORT background
  * any other Android-only/unknown background -> Windows FORT background
  * Android Map_Area geometry libraries referenced by Areas 61-76 but absent in
    Windows are semantically transcoded into the Windows Clara schema and appended
    as new libraries (currently Map_Area_010 for Area 66)
  * any remaining unknown MapAreaDef -> Windows MapAreaDef_005
  * unknown MissionTarget -> Run_x_meters
  * unknown MissionAvoidingScope -> empty
  * unknown Perks/boosters are dropped; arrays are capped at 2
  * Android boosters are otherwise preserved exactly, including on dual->single missions
  * boosters are explicitly removed for MissionIds 863, 879, 891, 1030, 1053, 1072, 1090
"""
import argparse
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    import clone_last_area as cla
except ImportError as exc:  # clearer error than a later NameError
    raise SystemExit(
        "ERROR: clone_last_area.py is required and must be importable. "
        "Place it in the same directory as this converter."
    ) from exc


ANDROID_FIRST_AREA = 61
ANDROID_LAST_AREA = 76
WINDOWS_SAFE_AREADEF = "/Map_Area_005/MapAreaDef_005"
WINDOWS_SAFE_BACKGROUND = "/MapAreaBG_FORT/MapAreaBackgroundData_FORT"
WINDOWS_SAFE_TARGET = "/MapMissions/TargetDef/Run_x_meters"
WINDOWS_SAFE_LOCATION = "fort"
WINDOWS_SAFE_FORCE_LOCATION = "fort"

# Explicit semantic replacements requested for Android-only environments.
ANDROID_LOCATION_REMAP = {
    "balthazarLair": "avl",
    "prison": "fort",
}
ANDROID_FORCE_LOCATION_REMAP = {
    "balthazarLair": "avl",
    "prison": "fort",
}
BOOSTERS_REMOVED_MISSION_IDS = {863, 879, 891, 1030, 1053, 1072, 1090}

ANDROID_BACKGROUND_REMAP = {
    "/MapAreaBG_BAL/MapAreaBackgroundData_BAL": "/MapAreaBG_AVL/MapAreaBackgroundData_AVL",
    "/MapAreaBG_PRI/MapAreaBackgroundData_PRI": "/MapAreaBG_FORT/MapAreaBackgroundData_FORT",
}

AREA_IMPORT_FIELDS = (
    "mapAreaDefinition", "mapAreaBackgroundRef", "FruitData", "RequiredFruits",
    "RequiredFruitsForBonus1", "RequiredFruitsForBonus2", "BonusType1",
    "BonusType2", "CostumePriceRange",
)
MISSION_COMMON_FIELDS = (
    "MissionTargetVersion", "TargetValue1", "TargetValue2", "TargetValue3",
    "NumMissionItemsInPool", "DifficultyRate", "MissionAvoidingScope",
    "MaxDistBetweenMissionItems", "MinDistBetweenItems", "HasSpecificCostumes",
    "MissionCostumes", "boostersThreshold", "boostersAppearChance", "HasFreeTry",
    "FreeTryCostume", "FreeTryCostumeAmount", "HasLDTutorial",
    "PlayLDTutorialOnFirstTry", "TriggerLDTutorialFails", "HardLDPriorityMultiplier",
)
MISSION_REQUIRED_FIELDS = (
    "MissionId", "Location", "ForceLocation", "MissionTarget", "Perks", "boosters",
) + MISSION_COMMON_FIELDS


def _discover_last_area(full: dict[str, Any]) -> tuple[int, int]:
    entities, _ = cla._build_path_indexes(full)
    order = entities.get(cla.MAP_AREAS_ORDER)
    if order is None or order.get("class") != "MapAreasOrder":
        raise cla.CloneError(f"{cla.MAP_AREAS_ORDER} is missing or has the wrong class")
    elements = cla._get_property(order, "Areas").get("elements")
    if not isinstance(elements, list) or not elements or not isinstance(elements[-1], dict):
        raise cla.CloneError("MapAreasOrder.Areas is malformed or empty")
    source = cla._parse_ordered_area_path(elements[-1].get("value"))
    return source, source + 1


def _require_clone_api() -> None:
    required = (
        "CloneError", "MAP_AREAS_ROOT", "MAP_AREAS_ORDER", "MISSION_ROOT",
        "_build_path_indexes", "_get_property", "_get_single", "_set_single",
        "_find_entity_record", "_mission_id", "_all_mission_ids",
        "_collect_area_missions", "_parse_ordered_area_path", "clara",
    )
    missing = [name for name in required if not hasattr(cla, name)]
    if missing:
        raise SystemExit(
            "ERROR: current cleaned clone_last_area.py is required; missing API(s): " + ", ".join(missing)
        )
    codec_missing = [
        name for name in ("decode_file", "encode_manifest", "parse_schema", "flatten_entities", "CodecError")
        if not hasattr(cla.clara, name)
    ]
    if codec_missing:
        raise SystemExit(
            "ERROR: blibclara_library_editor.py is missing required API(s): " + ", ".join(codec_missing)
        )


_require_clone_api()


def _require_fields(fields: dict[str, Any], names: tuple[str, ...], label: str) -> None:
    missing = [name for name in names if name not in fields]
    if missing:
        raise cla.CloneError(f"{label} is missing required field(s): {', '.join(missing)}")


def _string_array_value(fields: dict[str, Any], name: str, label: str) -> list[str]:
    value = fields.get(name)
    values = value if isinstance(value, list) else [value]
    if any(not isinstance(item, str) for item in values):
        raise cla.CloneError(f"{label}.{name} must contain only string references")
    return [item for item in values if item]


def _named_value(fields: dict[str, Any], name: str, label: str) -> str:
    value = fields.get(name)
    if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"]:
        raise cla.CloneError(f"{label}.{name} must be a Clara name/value object")
    return value["name"]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _field_values(entity: dict[str, Any], name: str) -> list[Any]:
    elements = cla._get_property(entity, name).get("elements")
    if not isinstance(elements, list) or any(not isinstance(e, dict) for e in elements):
        raise cla.CloneError(f"{entity.get('name', '<unnamed>')}.{name}: malformed elements")
    return [e.get("value") for e in elements]


def _fields_dict(entity: dict[str, Any]) -> dict[str, Any]:
    properties = entity.get("properties")
    if not isinstance(properties, list):
        raise cla.CloneError(f"{entity.get('name', '<unnamed>')}: properties must be a list")
    out: dict[str, Any] = {}
    for index, prop in enumerate(properties):
        if not isinstance(prop, dict) or not isinstance(prop.get("name"), str) or not prop["name"]:
            raise cla.CloneError(f"{entity.get('name', '<unnamed>')}.properties[{index}] is malformed")
        name = prop["name"]
        if name in out:
            raise cla.CloneError(f"{entity.get('name', '<unnamed>')}: duplicate property {name!r}")
        elements = prop.get("elements")
        if not isinstance(elements, list) or any(not isinstance(e, dict) for e in elements):
            raise cla.CloneError(f"{entity.get('name', '<unnamed>')}.{name}: malformed elements")
        values = [e.get("value") for e in elements]
        out[name] = values[0] if len(values) == 1 else values
    return out


def _set_array_values(entity: dict[str, Any], name: str, values: list[str]) -> None:
    if len(values) > 2 or any(not isinstance(v, str) or not v for v in values):
        raise cla.CloneError(f"{entity.get('name', '<unnamed>')}.{name}: expected 0..2 non-empty strings")
    prop = cla._get_property(entity, name)
    elements = prop.get("elements")
    if not isinstance(elements, list) or any(not isinstance(e, dict) for e in elements):
        raise cla.CloneError(f"{entity.get('name', '<unnamed>')}.{name}: malformed elements")
    if prop.get("named_elements") is not False:
        raise cla.CloneError(f"{entity.get('name', '<unnamed>')}.{name}: expected unnamed elements")
    encoded = values or [""]
    template = copy.deepcopy(elements[0]) if elements else {"value": ""}
    new_elements = []
    for value in encoded:
        element = copy.deepcopy(template)
        element["value"] = value
        new_elements.append(element)
    prop["elements"] = new_elements


def _schema_alias_map(schema: Any, class_name: str, property_name: str) -> dict[str, int]:
    cls = schema.by_name.get(class_name)
    if cls is None:
        raise cla.CloneError(f"Windows schema has no class {class_name!r}")
    props = [p for p in cls.properties if p.name == property_name]
    if len(props) != 1:
        raise cla.CloneError(
            f"Windows schema {class_name}.{property_name}: expected one property, found {len(props)}"
        )
    param = schema.params[props[0].type_index]
    if param.type_code != 0x0004:
        raise cla.CloneError(f"Windows schema {class_name}.{property_name} is not id_name")
    aliases = tuple(param.aliases)
    if not aliases or len(aliases) != len(set(aliases)):
        raise cla.CloneError(f"Windows schema {class_name}.{property_name} has invalid aliases")
    return {name: index for index, name in enumerate(aliases)}


def _windows_force_map(entities: dict[str, dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for path, entity in entities.items():
        if entity.get("class") != "MapMission":
            continue
        value = cla._get_single(entity, "ForceLocation")
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("name"), str)
            or not value["name"]
            or not isinstance(value.get("value"), int)
            or isinstance(value.get("value"), bool)
        ):
            raise cla.CloneError(f"{path}.ForceLocation is malformed: {value!r}")
        previous = out.get(value["name"])
        if previous is not None and previous != value["value"]:
            raise cla.CloneError(
                f"ForceLocation {value['name']!r} has conflicting values {previous} and {value['value']}"
            )
        out[value["name"]] = value["value"]
    if "none" in out and out["none"] != 0xFFFFFFFF:
        raise cla.CloneError("Windows ForceLocation 'none' conflicts with 0xFFFFFFFF")
    out["none"] = 0xFFFFFFFF
    return out


def _known_string_refs(
    entities: dict[str, dict[str, Any]], property_name: str
) -> set[str]:
    out: set[str] = set()
    for path, entity in entities.items():
        if entity.get("class") != "MapMission":
            continue
        for value in _field_values(entity, property_name):
            if value == "":
                continue
            if not isinstance(value, str):
                raise cla.CloneError(
                    f"{path}.{property_name} contains non-string reference {value!r}"
                )
            out.add(value)
    return out


def _canonical_windows_perks_by_target(
    entities: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, str]]:
    counts: dict[str, dict[tuple[str, str], int]] = {}
    for path, entity in entities.items():
        if entity.get("class") != "MapMission":
            continue
        target = cla._get_single(entity, "MissionTarget")
        values = _field_values(entity, "Perks")
        if not isinstance(target, str) or any(not isinstance(v, str) for v in values):
            raise cla.CloneError(f"{path}: malformed MissionTarget/Perks")
        perks = tuple(v for v in values if v)
        if len(perks) != 2:
            continue
        pair = (perks[0], perks[1])
        bucket = counts.setdefault(target, {})
        bucket[pair] = bucket.get(pair, 0) + 1
    return {
        target: min(bucket, key=lambda pair: (-bucket[pair], pair))
        for target, bucket in counts.items()
    }


def _known_area_awards(entities: dict[str, dict[str, Any]]) -> tuple[set[str], set[str]]:
    bonus1: set[str] = set()
    bonus2: set[str] = set()
    for path, entity in entities.items():
        if entity.get("class") != "MapArea":
            continue
        for field, bucket in (("BonusType1", bonus1), ("BonusType2", bonus2)):
            value = cla._get_single(entity, field)
            if value == "":
                continue
            if not isinstance(value, str):
                raise cla.CloneError(f"{path}.{field} is not a string reference")
            bucket.add(value)
    return bonus1, bonus2


def _resolve_android_background(
    android_entities: dict[str, dict[str, Any]], ref: str
) -> str | None:
    entity = android_entities.get(ref)
    if entity is None:
        return None
    value = cla._get_single(entity, "mapAreaBackground")
    if not isinstance(value, dict):
        raise cla.CloneError(f"Android background {ref!r} has malformed value {value!r}")
    name = value.get("name")
    prefix = "MapAreaBackgroundData_"
    if not isinstance(name, str) or not name.startswith(prefix):
        raise cla.CloneError(f"Android background {ref!r} has invalid name {name!r}")
    return f"/MapAreaBG_{name[len(prefix):]}/{name}"


def _log(report: dict[str, Any], kind: str, **payload: Any) -> None:
    report["substitutions"].append({"kind": kind, **payload})


def _strip_element_provenance(value: Any) -> Any:
    """Drop Android raw/provenance fields so Windows encoding is semantic."""
    if isinstance(value, list):
        return [_strip_element_provenance(x) for x in value]
    if isinstance(value, dict):
        return {
            k: _strip_element_provenance(v)
            for k, v in value.items()
            if k not in {
                "raw_base64",
                "original_value",
                "original_name",
                "source_offset",
                "source_size",
            }
        }
    return value


def _transcode_entity_to_windows_schema(
    android_entity: dict[str, Any],
    windows_schema: Any,
) -> dict[str, Any]:
    """Rebuild one Android entity using the matching Windows class/property types."""
    class_name = android_entity.get("class")
    if not isinstance(class_name, str) or class_name not in windows_schema.by_name:
        raise cla.CloneError(
            f"geometry import: Android class {class_name!r} does not exist in Windows schema"
        )

    wclass = windows_schema.by_name[class_name]
    source_properties = android_entity.get("properties")
    if not isinstance(source_properties, list):
        raise cla.CloneError(f"geometry import: class {class_name} has malformed properties")
    src_props: dict[str, dict[str, Any]] = {}
    for index, prop in enumerate(source_properties):
        if not isinstance(prop, dict) or not isinstance(prop.get("name"), str):
            raise cla.CloneError(
                f"geometry import: class {class_name} property {index} is malformed"
            )
        if prop["name"] in src_props:
            raise cla.CloneError(
                f"geometry import: class {class_name} has duplicate property {prop['name']!r}"
            )
        src_props[prop["name"]] = prop
    expected_names = [p.name for p in wclass.properties]
    missing = [name for name in expected_names if name not in src_props]
    extra = sorted(set(src_props) - set(expected_names))
    if missing or extra:
        raise cla.CloneError(
            f"geometry import: class {class_name} property mismatch; "
            f"missing={missing}, extra={extra}"
        )

    out = _strip_element_provenance(copy.deepcopy(android_entity))
    out.pop("opaque", None)
    out.pop("opaque_body_base64", None)

    new_props: list[dict[str, Any]] = []
    for wpdef in wclass.properties:
        prop = _strip_element_provenance(copy.deepcopy(src_props[wpdef.name]))
        param = windows_schema.params[wpdef.type_index]
        prop["name"] = wpdef.name
        prop["type_index"] = wpdef.type_index
        prop["type_code"] = param.type_code
        prop["subtype"] = param.subtype
        elements = prop.get("elements")
        if not isinstance(elements, list) or any(not isinstance(e, dict) for e in elements):
            raise cla.CloneError(f"geometry import: {class_name}.{wpdef.name} has malformed elements")
        if not isinstance(prop.get("named_elements"), bool):
            raise cla.CloneError(f"geometry import: {class_name}.{wpdef.name} has invalid named_elements")
        new_props.append(prop)
    out["properties"] = new_props
    return out


def _transcode_folder_to_windows_schema(
    android_folder: dict[str, Any],
    windows_schema: Any,
) -> dict[str, Any]:
    """Recursively transcode all entity records in one Android Clara library."""
    if not isinstance(android_folder, dict) or not isinstance(android_folder.get("name"), str):
        raise cla.CloneError("geometry import: malformed Android folder")
    source_records = android_folder.get("records")
    if not isinstance(source_records, list):
        raise cla.CloneError(f"geometry import: folder {android_folder.get('name')!r} has malformed records")
    out = _strip_element_provenance(copy.deepcopy(android_folder))
    records: list[dict[str, Any]] = []
    for index, rec in enumerate(source_records):
        if not isinstance(rec, dict):
            raise cla.CloneError(
                f"geometry import: folder {android_folder.get('name')!r} record {index} is malformed"
            )
        kind = rec.get("kind")
        if kind == "folder":
            records.append({
                "kind": "folder",
                "folder": _transcode_folder_to_windows_schema(
                    rec.get("folder", {}), windows_schema
                ),
            })
        elif kind == "entity":
            records.append({
                "kind": "entity",
                "entity": _transcode_entity_to_windows_schema(
                    rec.get("entity", {}), windows_schema
                ),
            })
        elif kind in {"group", "multilayer"}:
            # These record types are schema-independent; none are present in
            # Map_Area_010, but preserving them makes the routine generic.
            records.append(_strip_element_provenance(copy.deepcopy(rec)))
        else:
            raise cla.CloneError(
                f"geometry import: unsupported record kind {kind!r} "
                f"in folder {android_folder.get('name')!r}"
            )
    out["records"] = records
    return out


def _find_library_by_root_or_name(
    full: dict[str, Any], name: str
) -> dict[str, Any] | None:
    libraries = full.get("libraries")
    if not isinstance(libraries, list):
        raise cla.CloneError("decoded Clara file has no valid libraries list")
    matches: list[dict[str, Any]] = []
    for index, library in enumerate(libraries):
        if not isinstance(library, dict):
            raise cla.CloneError(f"libraries[{index}] must be an object")
        root = library.get("root_folder")
        if not isinstance(root, dict):
            raise cla.CloneError(f"libraries[{index}].root_folder is malformed")
        if library.get("name") == name or root.get("name") == name:
            matches.append(library)
    if len(matches) > 1:
        raise cla.CloneError(f"multiple Clara libraries match root/name {name!r}")
    return matches[0] if matches else None


def _required_android_geometry_roots(
    android_entities: dict[str, dict[str, Any]],
) -> set[str]:
    roots: set[str] = set()
    for area in range(ANDROID_FIRST_AREA, ANDROID_LAST_AREA + 1):
        apath = f"{cla.MAP_AREAS_ROOT}/MapArea{area:03d}"
        entity = android_entities.get(apath)
        if entity is None or entity.get("class") != "MapArea":
            raise cla.CloneError(f"Android input is missing MapArea {apath}")
        mapdef = cla._get_single(entity, "mapAreaDefinition")
        if not isinstance(mapdef, str) or not mapdef.startswith("/"):
            raise cla.CloneError(f"{apath}.mapAreaDefinition is invalid: {mapdef!r}")
        root = mapdef.split("/", 2)[1]
        if root.startswith("Map_Area_"):
            roots.add(root)
    return roots


def _iter_path_like_values(value: Any):
    if isinstance(value, str):
        if value.startswith("/"):
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_path_like_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_path_like_values(item)


def _import_required_android_geometry(
    full: dict[str, Any],
    android_full: dict[str, Any],
    android_entities: dict[str, dict[str, Any]],
    windows_schema: Any,
    report: dict[str, Any],
) -> None:
    """Append missing Android Map_Area libraries after semantic transcoding."""
    imported: list[dict[str, Any]] = []

    for root_name in sorted(_required_android_geometry_roots(android_entities)):
        if _find_library_by_root_or_name(full, root_name) is not None:
            continue
        alib = _find_library_by_root_or_name(android_full, root_name)
        if alib is None:
            raise cla.CloneError(
                f"geometry import: Android library/root {root_name!r} was not found"
            )

        root = alib.get("root_folder")
        if not isinstance(root, dict):
            raise cla.CloneError(f"geometry import: {root_name} has no root folder")

        transcoded_root = _transcode_folder_to_windows_schema(root, windows_schema)
        imported_lib = {
            "marker": alib.get("marker"),
            "version": alib.get("version"),
            "name": alib.get("name"),
            "root_folder": transcoded_root,
        }
        if imported_lib["marker"] != 0x1AAA or imported_lib["version"] != 12:
            raise cla.CloneError(
                f"geometry import: {root_name} has incompatible library marker/version"
            )

        entity_count = len(cla.clara.flatten_entities(transcoded_root))
        full["libraries"].append(imported_lib)
        imported.append({
            "library": root_name,
            "entity_count": entity_count,
        })

    # Verify every path-valued reference in the imported geometry resolves after
    # the append. For these Map_Area libraries, every slash-prefixed string is an
    # exact Clara entity reference.
    all_entities, _ = cla._build_path_indexes(full)
    unresolved: list[dict[str, str]] = []
    for info in imported:
        root_name = info["library"]
        prefix = f"/{root_name}"
        for path, ent in all_entities.items():
            if not (path == prefix or path.startswith(prefix + "/")):
                continue
            for prop in ent.get("properties", []):
                for el in prop.get("elements", []):
                    for ref in _iter_path_like_values(el.get("value")):
                        if ref not in all_entities:
                            unresolved.append({
                                "entity": path,
                                "property": prop.get("name", ""),
                                "reference": ref,
                            })
    if unresolved:
        sample = unresolved[:10]
        raise cla.CloneError(
            "geometry import: unresolved entity references after transplant: "
            + json.dumps(sample, ensure_ascii=False)
        )

    report["geometry_imports"] = imported
    report["geometry_unresolved_reference_count"] = 0


def _clone_last_area_in_memory(full: dict[str, Any]) -> dict[str, Any]:
    """Clone the current final Windows area once without serializing intermediate files."""
    entities, folders = cla._build_path_indexes(full)
    order = entities.get(cla.MAP_AREAS_ORDER)
    if order is None or order.get("class") != "MapAreasOrder":
        raise cla.CloneError(f"{cla.MAP_AREAS_ORDER} is missing or has the wrong class")
    area_elements = cla._get_property(order, "Areas").get("elements")
    if (
        not isinstance(area_elements, list)
        or not area_elements
        or any(not isinstance(e, dict) for e in area_elements)
    ):
        raise cla.CloneError("MapAreasOrder.Areas is malformed or empty")

    source_path = area_elements[-1].get("value")
    source_area = cla._parse_ordered_area_path(source_path)
    source_name = f"MapArea{source_area:03d}"
    expected_source_path = f"{cla.MAP_AREAS_ROOT}/{source_name}"
    if source_path != expected_source_path:
        raise cla.CloneError(
            f"last ordered area reference must be canonical: {source_path!r}; "
            f"expected {expected_source_path!r}"
        )
    new_area = source_area + 1
    new_name = f"MapArea{new_area:03d}"
    new_path = f"{cla.MAP_AREAS_ROOT}/{new_name}"
    if new_path in entities:
        raise cla.CloneError(f"Area {new_area} already exists")

    area_folder = folders.get(cla.MAP_AREAS_ROOT)
    mission_folder = folders.get(cla.MISSION_ROOT)
    if area_folder is None or mission_folder is None:
        raise cla.CloneError("required MapAreas or MapMissionDef folder is missing")
    source_area_record = cla._find_entity_record(area_folder, source_name)
    source_missions = cla._collect_area_missions(entities, source_area)
    if not source_missions:
        raise cla.CloneError(f"no MapMission records found for Area {source_area}")

    existing_ids = cla._all_mission_ids(entities)
    if not existing_ids:
        raise cla.CloneError("no MapMission MissionId values were found")
    if len(existing_ids) != len(set(existing_ids)):
        raise cla.CloneError("duplicate MissionId values before clone")
    first_id = max(existing_ids) + 1
    last_id = first_id + len(source_missions) - 1
    if last_id > 0x7FFFFFFF:
        raise cla.CloneError("new MissionId would exceed signed i32 range")

    mission_clones: list[tuple[dict[str, Any], int]] = []
    for index, (source_mission_path, _source_entity) in enumerate(source_missions):
        source_mission_name = source_mission_path.rsplit("/", 1)[-1]
        source_record = cla._find_entity_record(mission_folder, source_mission_name)
        new_id = first_id + index
        new_mission_name = f"Mission{new_area:03d}_{new_id:04d}"
        new_mission_path = f"{cla.MISSION_ROOT}/{new_mission_name}"
        if new_mission_path in entities:
            raise cla.CloneError(f"generated mission already exists: {new_mission_path}")
        clone = copy.deepcopy(source_record)
        clone["entity"]["name"] = new_mission_name
        cla._set_single(clone["entity"], "MissionId", new_id)
        mission_clones.append((clone, new_id))

    area_clone = copy.deepcopy(source_area_record)
    area_clone["entity"]["name"] = new_name
    area_folder["records"].append(area_clone)
    order_element = copy.deepcopy(area_elements[-1])
    order_element["value"] = new_path
    area_elements.append(order_element)
    cla._set_single(order, "TotalAreasExposed", len(area_elements))
    mission_folder["records"].extend(clone for clone, _ in mission_clones)

    return {
        "source_area": source_area,
        "created_area": new_area,
        "mission_count": len(mission_clones),
        "first_mission_id": mission_clones[0][1],
        "last_mission_id": mission_clones[-1][1],
    }


def _clone_windows_to_76_in_memory(base_full: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Clone 60->61->...->76 before any Android semantic edits."""
    full = copy.deepcopy(base_full)
    steps = []
    for expected_area in range(ANDROID_FIRST_AREA, ANDROID_LAST_AREA + 1):
        step = _clone_last_area_in_memory(full)
        if step["created_area"] != expected_area:
            raise cla.CloneError(
                f"clone sequence mismatch: expected Area {expected_area}, "
                f"created Area {step['created_area']}"
            )
        steps.append(step)
    report["structural_clone_steps"] = steps
    return full


def _convert(
    android_path: Path,
    windows_path: Path,
    output_path: Path,
    report_path: Path | None = None,
) -> Path:
    android_path = android_path.resolve()
    windows_path = windows_path.resolve()
    output_path = output_path.resolve()
    if report_path is not None:
        report_path = report_path.resolve()

    if not android_path.is_file() or not windows_path.is_file():
        raise cla.CloneError("both Android and Windows input maplibs must exist")
    protected = {android_path, windows_path}
    if output_path in protected:
        raise cla.CloneError("refusing to overwrite either input file")
    if report_path is not None and report_path in protected | {output_path}:
        raise cla.CloneError("report path must differ from both inputs and the output maplib")

    adata = android_path.read_bytes()
    base_data = windows_path.read_bytes()
    afull = cla.clara.decode_file(adata, android_path.name)
    base_full = cla.clara.decode_file(base_data, windows_path.name)
    aent, _ = cla._build_path_indexes(afull)
    base_ent, _ = cla._build_path_indexes(base_full)
    wschema = cla.clara.parse_schema(base_data)

    # Require the exact structural starting point expected by clone_last_area workflow.
    source_area, next_area = _discover_last_area(base_full)
    if source_area != 60 or next_area != 61:
        raise cla.CloneError(
            f"expected original Windows maplib ending at Area 60; found last Area {source_area}"
        )
    base_ids = cla._all_mission_ids(base_ent)
    if sorted(base_ids) != list(range(1, 859)):
        raise cla.CloneError("expected original Windows MissionIds 1..858")

    android_missions: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    all_android_ids: list[int] = []
    for area in range(ANDROID_FIRST_AREA, ANDROID_LAST_AREA + 1):
        apath = f"{cla.MAP_AREAS_ROOT}/MapArea{area:03d}"
        if apath not in aent:
            raise cla.CloneError(f"Android input is missing {apath}")
        missions = cla._collect_area_missions(aent, area)
        if len(missions) != 15:
            raise cla.CloneError(f"Android Area {area} has {len(missions)} missions; expected 15")
        android_missions[area] = missions
        all_android_ids.extend(cla._mission_id(ent) for _, ent in missions)
    if all_android_ids != list(range(859, 1099)):
        raise cla.CloneError("Android Areas 61-76 must have consecutive MissionIds 859..1098")

    # Conversion catalogs are derived from the untouched 60-area Windows base,
    # never from repeated clones (which would skew frequency-based perk selection).
    location_map = _schema_alias_map(wschema, "MapMission", "Location")
    force_map = _windows_force_map(base_ent)
    price_map = _schema_alias_map(wschema, "MapArea", "CostumePriceRange")
    known_targets = {p for p, e in base_ent.items() if e.get("class") == "MapTarget"}
    known_avoiding = _known_string_refs(base_ent, "MissionAvoidingScope")
    known_perks = _known_string_refs(base_ent, "Perks")
    known_boosters = _known_string_refs(base_ent, "boosters")
    canonical_perks = _canonical_windows_perks_by_target(base_ent)
    known_bonus1, known_bonus2 = _known_area_awards(base_ent)
    area60_path = f"{cla.MAP_AREAS_ROOT}/MapArea060"
    area60 = base_ent.get(area60_path)
    if area60 is None or area60.get("class") != "MapArea":
        raise cla.CloneError(f"Windows base is missing {area60_path}")
    area60_fields = _fields_dict(area60)
    _require_fields(
        area60_fields,
        ("FruitData", "BonusType1", "BonusType2", "CostumePriceRange"),
        "Windows Area 60",
    )
    for field in ("FruitData", "BonusType1", "BonusType2"):
        if not isinstance(area60_fields[field], str):
            raise cla.CloneError(f"Windows Area 60.{field} must be a string")
    area60_price_name = _named_value(area60_fields, "CostumePriceRange", "Windows Area 60")

    for required_path in (WINDOWS_SAFE_AREADEF, WINDOWS_SAFE_BACKGROUND, WINDOWS_SAFE_TARGET):
        if required_path not in base_ent:
            raise cla.CloneError(f"Windows base does not define required fallback entity {required_path!r}")
    if area60_fields["FruitData"] not in base_ent:
        raise cla.CloneError("Windows Area 60 FruitData reference does not resolve")

    if WINDOWS_SAFE_FORCE_LOCATION not in force_map:
        raise cla.CloneError(f"Windows base does not define ForceLocation {WINDOWS_SAFE_FORCE_LOCATION!r}")
    if WINDOWS_SAFE_LOCATION not in location_map:
        raise cla.CloneError(f"Windows base does not define Location {WINDOWS_SAFE_LOCATION!r}")
    for android_name, windows_name in ANDROID_LOCATION_REMAP.items():
        if windows_name not in location_map:
            raise cla.CloneError(
                f"Windows base does not define Location {windows_name!r} required for Android {android_name!r}"
            )
    for android_name, windows_name in ANDROID_FORCE_LOCATION_REMAP.items():
        if windows_name not in force_map:
            raise cla.CloneError(
                f"Windows base does not define ForceLocation {windows_name!r} required for Android {android_name!r}"
            )
    for android_bg, windows_bg in ANDROID_BACKGROUND_REMAP.items():
        if windows_bg not in base_ent:
            raise cla.CloneError(
                f"Windows base does not define background {windows_bg!r} required for Android {android_bg!r}"
            )

    report: dict[str, Any] = {
        "converter": "android-to-windows-areas61-76-importer-v3",
        "android_source": str(android_path),
        "android_sha256": hashlib.sha256(adata).hexdigest(),
        "windows_source": str(windows_path),
        "windows_sha256": hashlib.sha256(base_data).hexdigest(),
        "structural_engine": "current cleaned clone_last_area.py helpers + blibclara_library_editor.py",
        "areas_imported": list(range(61, 77)),
        "missions_imported": 240,
        "policy": {
            "structure": "clone Area 60->61->...->76 in memory before applying Android semantics",
            "dual_targets": "TargetDualDef -> TargetA; discard TargetB and Target2Value1/2/3",
            "dual_target_perks": "canonical most-common stock Windows two-perk pair for retained TargetA",
            "android_location_remap": ANDROID_LOCATION_REMAP,
            "unknown_location": WINDOWS_SAFE_LOCATION,
            "android_force_location_remap": ANDROID_FORCE_LOCATION_REMAP,
            "unknown_force_location": WINDOWS_SAFE_FORCE_LOCATION,
            "geometry": "semantically import missing Android Map_Area libraries into Windows schema; reuse Windows geometry when already present",
            "unknown_map_area_definition_after_geometry_import": WINDOWS_SAFE_AREADEF,
            "android_background_remap": ANDROID_BACKGROUND_REMAP,
            "unknown_background": WINDOWS_SAFE_BACKGROUND,
            "unknown_target": WINDOWS_SAFE_TARGET,
            "unknown_avoiding_scope": "",
            "unknown_perk_or_booster": "drop; preserve at most two known entries",
            "windows_only_fields": "inherited from cloned Windows Area-60 mission templates",
        },
        "substitutions": [],
        "dual_target_conversions": [],
        "dual_perk_reassignments": [],
        "areas": {},
        "geometry_imports": [],
    }

    full = _clone_windows_to_76_in_memory(base_full, report)
    _import_required_android_geometry(full, afull, aent, wschema, report)
    went, _ = cla._build_path_indexes(full)

    # Verify structural cloning produced the exact required numbering layout.
    order = went[cla.MAP_AREAS_ORDER]
    ordered = _field_values(order, "Areas")
    expected_order = [f"{cla.MAP_AREAS_ROOT}/MapArea{area:03d}" for area in range(1, 77)]
    if ordered != expected_order:
        raise cla.CloneError("structural clone stage did not produce exact ordered Areas 1..76")
    if cla._get_single(order, "TotalAreasExposed") != 76:
        raise cla.CloneError("structural clone stage did not set TotalAreasExposed=76")
    ids = sorted(cla._all_mission_ids(went))
    if ids != list(range(1, 1099)):
        raise cla.CloneError("structural clone stage did not produce MissionIds 1..1098")

    # Edit the already-cloned MapArea records in place.
    for area in range(ANDROID_FIRST_AREA, ANDROID_LAST_AREA + 1):
        apath = f"{cla.MAP_AREAS_ROOT}/MapArea{area:03d}"
        source_area_entity = aent.get(apath)
        target = went.get(apath)
        if source_area_entity is None or source_area_entity.get("class") != "MapArea":
            raise cla.CloneError(f"Android input is missing MapArea {apath}")
        if target is None or target.get("class") != "MapArea":
            raise cla.CloneError(f"cloned Windows map is missing MapArea {apath}")
        afields = _fields_dict(source_area_entity)
        _require_fields(afields, AREA_IMPORT_FIELDS, f"Android Area {area}")

        mapdef = afields["mapAreaDefinition"]
        if not isinstance(mapdef, str) or not mapdef:
            raise cla.CloneError(f"Android Area {area}.mapAreaDefinition must be a non-empty string")
        if mapdef not in went:
            _log(report, "mapAreaDefinition", area=area, android_value=mapdef,
                 windows_value=WINDOWS_SAFE_AREADEF)
            mapdef = WINDOWS_SAFE_AREADEF
        cla._set_single(target, "mapAreaDefinition", mapdef)

        bg_ref = afields["mapAreaBackgroundRef"]
        if not isinstance(bg_ref, str) or not bg_ref:
            raise cla.CloneError(f"Android Area {area}.mapAreaBackgroundRef must be a non-empty string")
        bg = _resolve_android_background(aent, bg_ref)
        if bg in ANDROID_BACKGROUND_REMAP:
            mapped_bg = ANDROID_BACKGROUND_REMAP[bg]
            _log(report, "background", area=area, android_value=bg_ref,
                 resolved_android_background=bg, windows_value=mapped_bg)
            bg = mapped_bg
        elif bg not in went:
            _log(report, "background", area=area, android_value=bg_ref,
                 resolved_android_background=bg, windows_value=WINDOWS_SAFE_BACKGROUND)
            bg = WINDOWS_SAFE_BACKGROUND
        cla._set_single(target, "mapAreaBackgroundData", bg)

        fruit = afields["FruitData"]
        if not isinstance(fruit, str) or not fruit:
            raise cla.CloneError(f"Android Area {area}.FruitData must be a non-empty string")
        if fruit not in went:
            _log(report, "FruitData", area=area, android_value=fruit,
                 windows_value=area60_fields["FruitData"])
            fruit = area60_fields["FruitData"]
        cla._set_single(target, "FruitData", fruit)

        for field in ("RequiredFruits", "RequiredFruitsForBonus1", "RequiredFruitsForBonus2"):
            cla._set_single(target, field, afields[field])

        b1 = afields["BonusType1"]
        if not isinstance(b1, str):
            raise cla.CloneError(f"Android Area {area}.BonusType1 must be a string")
        if b1 not in known_bonus1:
            _log(report, "BonusType1", area=area, android_value=b1,
                 windows_value=area60_fields["BonusType1"])
            b1 = area60_fields["BonusType1"]
        cla._set_single(target, "BonusType1", b1)

        b2 = afields["BonusType2"]
        if not isinstance(b2, str):
            raise cla.CloneError(f"Android Area {area}.BonusType2 must be a string")
        if b2 not in known_bonus2:
            _log(report, "BonusType2", area=area, android_value=b2,
                 windows_value=area60_fields["BonusType2"])
            b2 = area60_fields["BonusType2"]
        cla._set_single(target, "BonusType2", b2)

        price = afields["CostumePriceRange"]
        pname = _named_value(afields, "CostumePriceRange", f"Android Area {area}")
        if pname not in price_map:
            fallback = area60_price_name
            _log(report, "CostumePriceRange", area=area, android_value=price,
                 windows_value=fallback)
            pname = fallback
        cla._set_single(target, "CostumePriceRange", {"id": price_map[pname], "name": pname})

        report["areas"][str(area)] = {
            "mapAreaDefinition": mapdef,
            "mapAreaBackgroundData": bg,
            "FruitData": fruit,
            "BonusType1": b1,
            "BonusType2": b2,
            "CostumePriceRange": pname,
        }

    # Edit already-cloned mission records in place; no mission records are appended here.
    for area in range(ANDROID_FIRST_AREA, ANDROID_LAST_AREA + 1):
        dest_missions = cla._collect_area_missions(went, area)
        src_missions = android_missions[area]
        if len(dest_missions) != len(src_missions):
            raise cla.CloneError(f"Area {area}: cloned/Android mission-count mismatch")

        for ordinal, ((_, src_ent), (_, dest)) in enumerate(zip(src_missions, dest_missions)):
            sf = _fields_dict(src_ent)
            label = f"Android Area {area} Level {ordinal + 1}"
            _require_fields(sf, MISSION_REQUIRED_FIELDS, label)
            expected_mid = cla._mission_id(src_ent)
            actual_mid = cla._mission_id(dest)
            if actual_mid != expected_mid:
                raise cla.CloneError(
                    f"Area {area} Level {ordinal+1}: clone MissionId {actual_mid} != Android {expected_mid}"
                )

            # Location: symbolic name only; Android numeric IDs are never reused.
            aloc = sf["Location"]
            lname = _named_value(sf, "Location", label)
            if lname in ANDROID_LOCATION_REMAP:
                mapped_name = ANDROID_LOCATION_REMAP[lname]
                _log(report, "Location", area=area, level_number=ordinal+1,
                     mission_id=expected_mid, android_value=aloc,
                     windows_value=mapped_name)
                lname = mapped_name
            elif lname not in location_map:
                _log(report, "Location", area=area, level_number=ordinal+1,
                     mission_id=expected_mid, android_value=aloc,
                     windows_value=WINDOWS_SAFE_LOCATION)
                lname = WINDOWS_SAFE_LOCATION
            cla._set_single(dest, "Location", {"id": location_map[lname], "name": lname})

            # ForceLocation: map Android-only environments by symbolic name.
            aforce = sf["ForceLocation"]
            fname = _named_value(sf, "ForceLocation", label)
            if fname in ANDROID_FORCE_LOCATION_REMAP:
                mapped_name = ANDROID_FORCE_LOCATION_REMAP[fname]
                _log(report, "ForceLocation", area=area, level_number=ordinal+1,
                     mission_id=expected_mid, android_value=aforce,
                     windows_value=mapped_name)
                fname = mapped_name
            elif fname not in force_map:
                _log(report, "ForceLocation", area=area, level_number=ordinal+1,
                     mission_id=expected_mid, android_value=aforce,
                     windows_value=WINDOWS_SAFE_FORCE_LOCATION)
                fname = WINDOWS_SAFE_FORCE_LOCATION
            cla._set_single(dest, "ForceLocation", {"value": force_map[fname], "name": fname})

            atarget = sf["MissionTarget"]
            if not isinstance(atarget, str) or not atarget:
                raise cla.CloneError(f"{label}.MissionTarget must be a non-empty string")
            wtarget = atarget
            is_dual = False
            if isinstance(atarget, str) and "/TargetDualDef/" in atarget and atarget in aent:
                is_dual = True
                dual_entity = aent[atarget]
                dual = _fields_dict(dual_entity)
                _require_fields(dual, ("TargetA", "TargetB"), f"Android dual target {atarget}")
                target_a = dual["TargetA"]
                target_b = dual["TargetB"]
                if not isinstance(target_a, str) or not isinstance(target_b, str):
                    raise cla.CloneError(f"Android dual target {atarget} has non-string TargetA/TargetB")
                wtarget = target_a if target_a in known_targets else WINDOWS_SAFE_TARGET
                report["dual_target_conversions"].append({
                    "area": area,
                    "level_number": ordinal + 1,
                    "mission_id": expected_mid,
                    "android_dual_target": atarget,
                    "windows_target": wtarget,
                    "discarded_target_b": target_b,
                    "target2_values": [
                        sf.get("Target2Value1", 0),
                        sf.get("Target2Value2", 0),
                        sf.get("Target2Value3", 0),
                    ],
                })
            elif atarget not in known_targets:
                _log(report, "MissionTarget", area=area, level_number=ordinal+1,
                     mission_id=expected_mid, android_value=atarget,
                     windows_value=WINDOWS_SAFE_TARGET)
                wtarget = WINDOWS_SAFE_TARGET
            cla._set_single(dest, "MissionTarget", wtarget)

            for field in MISSION_COMMON_FIELDS:
                value = cla._get_single(src_ent, field)
                if field == "MissionAvoidingScope":
                    if not isinstance(value, str):
                        raise cla.CloneError(f"{label}.MissionAvoidingScope must be a string")
                    if value and value not in known_avoiding:
                        _log(report, field, area=area, level_number=ordinal+1,
                             mission_id=expected_mid, android_value=value, windows_value="")
                        value = ""
                cla._set_single(dest, field, value)

            android_perks = _string_array_value(sf, "Perks", label)
            accepted_perks = [v for v in android_perks if v in known_perks]
            unknown_perks = [v for v in android_perks if v not in known_perks]
            if unknown_perks:
                _log(report, "Perks", area=area, level_number=ordinal+1,
                     mission_id=expected_mid, android_unknown_values=unknown_perks,
                     action="dropped")

            if is_dual:
                pair = canonical_perks.get(wtarget)
                if pair is None:
                    raise cla.CloneError(
                        f"MissionId {expected_mid}: no stock Windows two-perk pair for TargetA {wtarget!r}"
                    )
                final_perks = list(pair)
                report["dual_perk_reassignments"].append({
                    "area": area,
                    "level_number": ordinal + 1,
                    "mission_id": expected_mid,
                    "windows_target_a": wtarget,
                    "android_perks": android_perks,
                    "windows_perks": final_perks,
                    "changed": android_perks != final_perks,
                })
            else:
                final_perks = accepted_perks[:2]
                if len(accepted_perks) > 2:
                    _log(report, "Perks_cap", area=area, level_number=ordinal+1,
                         mission_id=expected_mid, android_values=accepted_perks,
                         windows_values=final_perks)
            _set_array_values(dest, "Perks", final_perks)

            android_boosters = _string_array_value(sf, "boosters", label)
            accepted_boosters = [v for v in android_boosters if v in known_boosters]
            unknown_boosters = [v for v in android_boosters if v not in known_boosters]
            if unknown_boosters:
                _log(report, "boosters", area=area, level_number=ordinal+1,
                     mission_id=expected_mid, android_unknown_values=unknown_boosters,
                     action="dropped")
            if len(accepted_boosters) > 2:
                _log(report, "boosters_cap", area=area, level_number=ordinal+1,
                     mission_id=expected_mid, android_values=accepted_boosters,
                     windows_values=accepted_boosters[:2])

            final_boosters = accepted_boosters[:2]
            if expected_mid in BOOSTERS_REMOVED_MISSION_IDS:
                if final_boosters:
                    _log(report, "boosters_removed", area=area, level_number=ordinal+1,
                         mission_id=expected_mid, android_values=final_boosters,
                         windows_values=[])
                final_boosters = []
            _set_array_values(dest, "boosters", final_boosters)

    rebuilt = cla.clara.encode_manifest(base_data, full)

    # Fresh decode and strict conversion-policy verification.
    check = cla.clara.decode_file(rebuilt, output_path.name)
    cent, _ = cla._build_path_indexes(check)
    order2 = cent.get(cla.MAP_AREAS_ORDER)
    if order2 is None or order2.get("class") != "MapAreasOrder":
        raise cla.CloneError("verification failed: MapAreasOrder is missing")
    order_vals = _field_values(order2, "Areas")
    if order_vals != expected_order:
        raise cla.CloneError("verification failed: ordered area list is not exactly Areas 1..76")
    if cla._get_single(order2, "TotalAreasExposed") != 76:
        raise cla.CloneError("verification failed: TotalAreasExposed != 76")
    mids = sorted(cla._all_mission_ids(cent))
    if mids != list(range(1, 1099)):
        raise cla.CloneError("verification failed: MissionIds are not exactly 1..1098")

    for area in range(ANDROID_FIRST_AREA, ANDROID_LAST_AREA + 1):
        apath = f"{cla.MAP_AREAS_ROOT}/MapArea{area:03d}"
        area_entity = cent.get(apath)
        if area_entity is None or area_entity.get("class") != "MapArea":
            raise cla.CloneError(f"verification failed: missing MapArea {apath}")
        af = _fields_dict(area_entity)
        _require_fields(af, ("mapAreaDefinition", "mapAreaBackgroundData", "FruitData"), f"Area {area}")
        for field in ("mapAreaDefinition", "mapAreaBackgroundData", "FruitData"):
            if not isinstance(af[field], str) or af[field] not in cent:
                raise cla.CloneError(
                    f"verification failed: Area {area} {field} reference does not exist: {af[field]!r}"
                )
        missions = cla._collect_area_missions(cent, area)
        if len(missions) != 15:
            raise cla.CloneError(f"verification failed: Area {area} has {len(missions)} missions")
        for _, mission in missions:
            mf = _fields_dict(mission)
            _require_fields(
                mf,
                ("MissionId", "MissionTarget", "Location", "ForceLocation", "MissionAvoidingScope", "Perks", "boosters"),
                f"verified Area {area} mission",
            )
            mid = cla._mission_id(mission)
            target = mf["MissionTarget"]
            if not isinstance(target, str) or target not in known_targets:
                raise cla.CloneError(f"verification failed: MissionId {mid} has unknown target")
            loc = mf["Location"]
            if not isinstance(loc, dict) or location_map.get(loc.get("name")) != loc.get("id"):
                raise cla.CloneError(f"verification failed: MissionId {mid} has bad Location")
            force = mf["ForceLocation"]
            if not isinstance(force, dict) or force_map.get(force.get("name")) != force.get("value"):
                raise cla.CloneError(f"verification failed: MissionId {mid} has bad ForceLocation")
            avoiding = mf["MissionAvoidingScope"]
            if not isinstance(avoiding, str) or (avoiding and avoiding not in known_avoiding):
                raise cla.CloneError(f"verification failed: MissionId {mid} has unknown avoid scope")
            for field, known in (("Perks", known_perks), ("boosters", known_boosters)):
                values = _string_array_value(mf, field, f"verified MissionId {mid}")
                if len(values) > 2 or any(value not in known for value in values):
                    raise cla.CloneError(f"verification failed: MissionId {mid} incompatible {field}")

    report["output"] = str(output_path)
    report["output_sha256"] = hashlib.sha256(rebuilt).hexdigest()
    report["output_size"] = len(rebuilt)
    report["summary"] = {
        "ordered_areas": 76,
        "total_map_missions": 1098,
        "dual_targets_converted_to_target_a": len(report["dual_target_conversions"]),
        "dual_perk_pairs_reassigned": len(report["dual_perk_reassignments"]),
        "dual_perk_pairs_actually_changed": sum(
            1 for x in report["dual_perk_reassignments"] if x["changed"]
        ),
        "geometry_libraries_imported": len(report["geometry_imports"]),
        "geometry_entities_imported": sum(x["entity_count"] for x in report["geometry_imports"]),
        "boosters_explicitly_removed": sum(
            1 for x in report["substitutions"] if x.get("kind") == "boosters_removed"
        ),
        "other_substitutions": sum(
            1 for x in report["substitutions"] if x.get("kind") != "boosters_removed"
        ),
    }

    report_bytes = (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write(output_path, rebuilt)
    if report_path is not None:
        _atomic_write(report_path, report_bytes)

    print("OK: imported Android Areas 61-76 + required Android map geometry")
    print(f"Output: {output_path}")
    print(f"SHA-256: {report['output_sha256']}")
    print("Structural creation: 16 in-memory clone steps")
    print(
        "Geometry libraries imported: "
        f"{len(report['geometry_imports'])} "
        f"({sum(x['entity_count'] for x in report['geometry_imports'])} entities)"
    )
    print(f"Dual targets -> TargetA: {len(report['dual_target_conversions'])}")
    print(
        f"Dual perk pairs reassigned: {len(report['dual_perk_reassignments'])} "
        f"({sum(1 for x in report['dual_perk_reassignments'] if x['changed'])} changed)"
    )
    print(f"Other substitutions: {report['summary']['other_substitutions']}")
    if report_path is not None:
        print(f"Report: {report_path}")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import Android Jelly Lab Areas 61-76 into original Windows up23 maplib, "
            "using the current clone_last_area.py helpers and semantically importing required Android map geometry"
        )
    )
    parser.add_argument("android_maplib", type=Path)
    parser.add_argument("windows_maplib", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    try:
        _convert(args.android_maplib, args.windows_maplib, args.output, args.report)
        return 0
    except (cla.CloneError, cla.clara.CodecError, OSError, ValueError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
