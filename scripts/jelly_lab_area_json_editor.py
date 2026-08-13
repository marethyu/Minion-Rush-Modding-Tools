#!/usr/bin/env python3
"""
Minimal Jelly Lab selected-area JSON editor for Minion Rush maplib.blibclara.

This is a parameter-editing layer over blibclara_editor. The decoded JSON is
intentionally minimal and contains exactly two top-level keys::

    {
      "area": { ...all MapArea properties... },
      "levels": [
        {
          "level_number": 1,
          "mission": {
            "mandatory": { ...required editing fields... },
            "optional": { ...all other MapMission properties... }
          }
        }
      ]
    }

Everything inside ``area`` and both ``levels[].mission.mandatory`` and
``levels[].mission.optional`` is editable. The labels are organizational only;
they do not change the underlying Clara property names.
``levels[].level_number`` and the number/order of levels are structural and
read-only. No paths, catalogs, source metadata, geometry, target definitions,
or other auxiliary objects are emitted.

Name-only enum fields
---------------------
``area.CostumePriceRange``, ``mission.Location``, and
``mission.ForceLocation`` are emitted as name strings only. During encode the
editor resolves the corresponding numeric ``id`` or ``value`` automatically
from the input maplib/schema. For example::

    "CostumePriceRange": "Expensive"
    "Location": "macho",
    "ForceLocation": "none"


Mission field labels
--------------------
The JSON groups MapMission properties into two editable objects. ``mandatory``
contains Location, ForceLocation, MissionTarget, MissionAvoidingScope, Perks,
boosters, TargetValue1, TargetValue2, and TargetValue3. Every other MapMission
property is emitted under ``optional``. The encoder requires this labeled form exactly.

Perks / boosters
-----------------
``mission.Perks`` and ``mission.boosters`` are always JSON arrays with a hard
maximum of two entries. ``[]`` means none. The game UI has been observed to
consume at most two entries, so 3+ entries are rejected.

Area numbers are discovered from /MapLevelDef/MapSystem/MapAreasOrder/Areas;
there is no hard-coded Area 60 limit.

Typical workflow
----------------
Decode Area 61::

    python jelly_lab_area_json_editor.py decode maplib.blibclara 61 area61.json

Edit area61.json, then encode it back. Because the minimal JSON intentionally
does not contain an area number, provide the selected area again on encode::

    python jelly_lab_area_json_editor.py encode maplib.blibclara 61 area61.json maplib_mod.blibclara

Validate every listed area::

    python jelly_lab_area_json_editor.py check maplib.blibclara

This remains a parameter editor, not a structural map editor: it does not add
or remove areas, levels, MapPoints, or MapPath milestones.
"""

import argparse
import copy
import hashlib
import json
import math
import re
import struct
import sys
from pathlib import Path
from typing import Any

import blibclara_editor


MAX_MISSION_ARRAY_ITEMS = 2
CAPPED_MISSION_ARRAY_FIELDS = frozenset({"Perks", "boosters"})
MANDATORY_MISSION_FIELDS = frozenset({
    "Location",
    "ForceLocation",
    "MissionTarget",
    "MissionAvoidingScope",
    "Perks",
    "boosters",
    "TargetValue1",
    "TargetValue2",
    "TargetValue3",
})
MAP_AREAS_ROOT = "/MapLevelDef/MapSystem/MapAreas"
MAP_AREAS_ORDER = "/MapLevelDef/MapSystem/MapAreasOrder"
MISSION_ROOT = "/MapMissions/MapMissionDef"


class AreaEditError(RuntimeError):
    pass



def build_entity_index(full: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return mutable entities indexed by validated absolute Clara path."""
    libraries = full.get("libraries")
    if not isinstance(libraries, list):
        raise AreaEditError("decoded Clara manifest has no libraries list")

    entities: dict[str, dict[str, Any]] = {}
    folder_paths: set[str] = set()

    def walk(folder: dict[str, Any], path: str) -> None:
        if path in folder_paths:
            raise AreaEditError(f"duplicate folder path {path}")
        folder_paths.add(path)

        records = folder.get("records")
        if not isinstance(records, list):
            raise AreaEditError(f"folder {path}: records must be a list")
        for index, rec in enumerate(records):
            if not isinstance(rec, dict):
                raise AreaEditError(f"folder {path}: record {index} must be an object")
            kind = rec.get("kind")
            if kind == "folder":
                child = rec.get("folder")
                if not isinstance(child, dict):
                    raise AreaEditError(f"folder {path}: record {index} has malformed child folder")
                child_name = child.get("name")
                if not isinstance(child_name, str) or not child_name:
                    raise AreaEditError(f"folder {path}: record {index} has invalid child folder name")
                walk(child, path.rstrip("/") + "/" + child_name)
            elif kind == "entity":
                entity = rec.get("entity")
                if not isinstance(entity, dict):
                    raise AreaEditError(f"folder {path}: record {index} has malformed entity")
                name = entity.get("name")
                if not isinstance(name, str) or not name:
                    raise AreaEditError(f"folder {path}: record {index} has invalid entity name")
                entity_path = path.rstrip("/") + "/" + name
                if entity_path in entities:
                    raise AreaEditError(f"duplicate entity path {entity_path}")
                entities[entity_path] = entity
            elif kind not in {"group", "multilayer"}:
                raise AreaEditError(f"folder {path}: unsupported record kind {kind!r}")

    for library_index, library in enumerate(libraries):
        if not isinstance(library, dict):
            raise AreaEditError(f"libraries[{library_index}] must be an object")
        root = library.get("root_folder")
        if not isinstance(root, dict):
            raise AreaEditError(f"libraries[{library_index}].root_folder must be an object")
        root_name = root.get("name")
        if not isinstance(root_name, str) or not root_name:
            raise AreaEditError(f"libraries[{library_index}].root_folder has invalid name")
        walk(root, "/" + root_name)

    return entities


def simplify_value(value: Any) -> Any:
    """Turn recursively decoded Clara values into clean JSON values."""
    if isinstance(value, list):
        return [simplify_value(x) for x in value]
    if isinstance(value, dict):
        if isinstance(value.get("class"), str) and (
            value.get("opaque") is True or isinstance(value.get("properties"), list)
        ):
            return simplify_entity(value)
        # enum/reference support objects and other ordinary dictionaries
        return {
            k: simplify_value(v)
            for k, v in value.items()
            if k not in {
                "raw_base64", "original_value", "original_name",
                "source_offset", "source_size", "preamble_hex",
                "opaque_body_base64",
            }
        }
    return value


def simplify_property(prop: dict[str, Any]) -> Any:
    elements = prop.get("elements")
    if not isinstance(elements, list):
        raise AreaEditError(f"property {prop.get('name')!r}: elements must be a list")
    if not all(isinstance(element, dict) for element in elements):
        raise AreaEditError(f"property {prop.get('name')!r}: every element must be an object")
    values = [simplify_value(element.get("value")) for element in elements]
    if not values:
        return []
    return values[0] if len(values) == 1 else values


def simplify_fields(entity: dict[str, Any]) -> dict[str, Any]:
    properties = entity.get("properties")
    if not isinstance(properties, list):
        raise AreaEditError(f"entity {entity.get('name')!r}: properties must be a list")
    fields: dict[str, Any] = {}
    for index, prop in enumerate(properties):
        if not isinstance(prop, dict):
            raise AreaEditError(f"entity {entity.get('name')!r}: property {index} must be an object")
        name = prop.get("name")
        if not isinstance(name, str) or not name:
            raise AreaEditError(f"entity {entity.get('name')!r}: property {index} has invalid name")
        if name in fields:
            raise AreaEditError(f"entity {entity.get('name')!r}: duplicate property {name!r}")
        fields[name] = simplify_property(prop)
    return fields


def simplify_resizable_reference_property(prop: dict[str, Any]) -> list[Any]:
    """Return a resizable reference property as a JSON list.

    Stock maplib uses one empty-string element to represent an empty Perks or
    boosters list. Expose that as [] while preserving all non-empty elements.
    """
    elements = prop.get("elements")
    if not isinstance(elements, list) or not all(isinstance(element, dict) for element in elements):
        raise AreaEditError(f"property {prop.get('name')!r}: malformed elements")
    values = [simplify_value(element.get("value")) for element in elements]
    return [] if values == [""] else values


def simplify_area_fields(entity: dict[str, Any]) -> dict[str, Any]:
    """Return editable MapArea fields with CostumePriceRange as a name only."""
    fields = simplify_fields(entity)
    price_range = fields.get("CostumePriceRange")
    if not isinstance(price_range, dict) or not isinstance(price_range.get("name"), str):
        raise AreaEditError("MapArea.CostumePriceRange is not a valid id/name value")
    fields["CostumePriceRange"] = price_range["name"]
    return fields


def simplify_mission_fields(entity: dict[str, Any]) -> dict[str, Any]:
    """Return the flat editable MapMission field mapping used internally."""
    fields = simplify_fields(entity)
    missing = sorted(MANDATORY_MISSION_FIELDS - set(fields))
    if missing:
        raise AreaEditError(
            "MapMission schema is missing mandatory field(s): " + ", ".join(missing)
        )

    for name in CAPPED_MISSION_ARRAY_FIELDS:
        fields[name] = simplify_resizable_reference_property(get_property(entity, name))

    for name in ("Location", "ForceLocation"):
        value = fields[name]
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise AreaEditError(f"MapMission.{name} is not a valid named Clara value")
        fields[name] = value["name"]
    return fields


def label_mission_fields(fields: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Group current MapMission fields under mandatory/optional labels."""
    return {
        "mandatory": {name: value for name, value in fields.items() if name in MANDATORY_MISSION_FIELDS},
        "optional": {name: value for name, value in fields.items() if name not in MANDATORY_MISSION_FIELDS},
    }


def unlabel_mission_fields(mission: Any, label: str) -> dict[str, Any]:
    """Validate and flatten the current mandatory/optional mission representation."""
    if not isinstance(mission, dict):
        raise AreaEditError(f"{label} must be an object")
    if set(mission) != {"mandatory", "optional"}:
        raise AreaEditError(f"{label} must contain exactly 'mandatory' and 'optional'")

    mandatory = mission["mandatory"]
    optional = mission["optional"]
    if not isinstance(mandatory, dict):
        raise AreaEditError(f"{label}.mandatory must be an object")
    if not isinstance(optional, dict):
        raise AreaEditError(f"{label}.optional must be an object")

    mandatory_names = set(mandatory)
    missing = sorted(MANDATORY_MISSION_FIELDS - mandatory_names)
    misplaced_optional = sorted(mandatory_names - MANDATORY_MISSION_FIELDS)
    misplaced_mandatory = sorted(set(optional) & MANDATORY_MISSION_FIELDS)
    if missing:
        raise AreaEditError(f"{label}.mandatory is missing: {', '.join(missing)}")
    if misplaced_optional:
        raise AreaEditError(
            f"{label}.mandatory contains field(s) that must be optional: "
            + ", ".join(misplaced_optional)
        )
    if misplaced_mandatory:
        raise AreaEditError(
            f"{label}.optional contains mandatory field(s): "
            + ", ".join(misplaced_mandatory)
        )

    return {**mandatory, **optional}


def simplify_entity(entity: dict[str, Any]) -> dict[str, Any]:
    if entity.get("opaque") is True:
        # The codec can preserve such a value, but it is not safely editable without
        # its raw body.  Keep only the structural signal in this friendly JSON.
        return {
            "class": entity.get("class"),
            "name": entity.get("name", ""),
            "opaque": True,
        }
    return {
        "class": entity.get("class"),
        "name": entity.get("name", ""),
        "fields": simplify_fields(entity),
    }



def get_property(entity: dict[str, Any], name: str) -> dict[str, Any]:
    for prop in entity.get("properties", []):
        if prop.get("name") == name:
            return prop
    raise AreaEditError(
        f"entity {entity.get('name')!r} class {entity.get('class')!r} has no property {name!r}"
    )


def get_field(entity: dict[str, Any], name: str) -> Any:
    return simplify_property(get_property(entity, name))


def set_nested_entity_from_simple(original: dict[str, Any], desired: dict[str, Any], label: str) -> dict[str, Any]:
    if original.get("opaque") is True:
        if desired != simplify_entity(original):
            raise AreaEditError(f"{label}: opaque nested entities are read-only")
        return copy.deepcopy(original)
    if not isinstance(desired, dict):
        raise AreaEditError(f"{label}: nested entity must be an object")
    if desired.get("class") != original.get("class"):
        raise AreaEditError(
            f"{label}: changing nested class {original.get('class')!r} -> {desired.get('class')!r} is not supported"
        )
    if set(desired) != {"class", "name", "fields"}:
        raise AreaEditError(f"{label}: nested entity must contain exactly class, name, and fields")
    if not isinstance(desired["name"], str):
        raise AreaEditError(f"{label}.name must be a string")
    if not isinstance(desired["fields"], dict):
        raise AreaEditError(f"{label}.fields must be an object")
    out = copy.deepcopy(original)
    out["name"] = desired["name"]
    apply_fields(out, desired["fields"], label)
    return out


def _canonical_f32(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AreaEditError(f"{label}: expected numeric f32 value")
    number = float(value)
    if not math.isfinite(number):
        raise AreaEditError(f"{label}: f32 value must be finite")
    try:
        packed = struct.pack("<f", number)
    except (OverflowError, struct.error) as exc:
        raise AreaEditError(f"{label}: value is outside finite f32 range") from exc
    result = struct.unpack("<f", packed)[0]
    if not math.isfinite(result):
        raise AreaEditError(f"{label}: value is outside finite f32 range")
    return result


def canonicalize_value_for_property(value: Any, prop: dict[str, Any], label: str) -> Any:
    type_code = prop.get("type_code")
    subtype = prop.get("subtype")
    if type_code == 2 and subtype == 3:
        return _canonical_f32(value, label)
    if type_code == 2 and subtype == 4:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AreaEditError(f"{label}: expected numeric f64 value")
        result = float(value)
        if not math.isfinite(result):
            raise AreaEditError(f"{label}: f64 value must be finite")
        return result
    if type_code == 0x0080:
        if not isinstance(value, list):
            raise AreaEditError(f"{label}: expected a float-vector array")
        return [_canonical_f32(item, f"{label}[{index}]") for index, item in enumerate(value)]
    return copy.deepcopy(value)


def set_value_from_simple(
    original: Any, desired: Any, label: str, prop: dict[str, Any]
) -> Any:
    if isinstance(original, dict) and isinstance(original.get("class"), str):
        if original.get("opaque") is True or isinstance(original.get("properties"), list):
            return set_nested_entity_from_simple(original, desired, label)
    if original is None and isinstance(desired, dict) and isinstance(desired.get("class"), str):
        raise AreaEditError(f"{label}: creating a nested entity where the original value was null is not supported")
    return canonicalize_value_for_property(desired, prop, label)


def normalize_resizable_reference_values(desired: Any, label: str) -> list[str]:
    """Validate a Perks/boosters array and enforce the game-observed 2-item cap."""
    if not isinstance(desired, list):
        raise AreaEditError(f"{label}: expected a JSON array")
    if len(desired) > MAX_MISSION_ARRAY_ITEMS:
        raise AreaEditError(
            f"{label}: at most {MAX_MISSION_ARRAY_ITEMS} entries are supported; got {len(desired)}"
        )
    if not all(isinstance(value, str) and value for value in desired):
        raise AreaEditError(f"{label}: every entry must be a non-empty string reference; use [] for none")
    return desired

def apply_resizable_reference_property(prop: dict[str, Any], desired: Any, label: str) -> None:
    """Write a capped Perks/boosters array while preserving stock empty semantics."""
    elems = prop.get("elements")
    if not isinstance(elems, list) or not all(isinstance(element, dict) for element in elems):
        raise AreaEditError(f"{label}: malformed original property")
    if prop.get("named_elements") is not False:
        raise AreaEditError(f"{label}: expected an unnamed Clara reference array")

    values = normalize_resizable_reference_values(desired, label)
    encoded_values = values if values else [""]

    new_elems: list[dict[str, Any]] = []
    for index, value in enumerate(encoded_values):
        if index < len(elems):
            element = copy.deepcopy(elems[index])
        elif elems:
            element = copy.deepcopy(elems[-1])
            for key in ("original_value", "original_name", "raw_base64"):
                element.pop(key, None)
        else:
            element = {}
        element["value"] = value
        new_elems.append(element)
    prop["elements"] = new_elems


def apply_fields(
    entity: dict[str, Any],
    desired_fields: dict[str, Any],
    label: str,
    *,
    resizable_fields: frozenset[str] = frozenset(),
) -> None:
    """Apply friendly fields back onto a decoded entity in-place.

    Property cardinality remains fixed except for explicitly allowed fields.
    For MapMission records only Perks and boosters may resize, and both are capped at two entries.
    """
    if not isinstance(desired_fields, dict):
        raise AreaEditError(f"{label} must be an object")

    props = entity.get("properties")
    if not isinstance(props, list):
        raise AreaEditError(f"{label}: original properties must be a list")
    pmap: dict[str, dict[str, Any]] = {}
    for index, prop in enumerate(props):
        if not isinstance(prop, dict):
            raise AreaEditError(f"{label}: original property {index} must be an object")
        name = prop.get("name")
        if not isinstance(name, str) or not name:
            raise AreaEditError(f"{label}: original property {index} has invalid name")
        if name in pmap:
            raise AreaEditError(f"{label}: duplicate original property {name!r}")
        pmap[name] = prop
    expected = set(pmap)
    actual = set(desired_fields)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise AreaEditError(f"{label}: missing field(s): {', '.join(missing)}")
    if extra:
        raise AreaEditError(f"{label}: unknown field(s): {', '.join(extra)}")

    for name, prop in pmap.items():
        desired = desired_fields[name]
        if name in resizable_fields:
            apply_resizable_reference_property(prop, desired, f"{label}.{name}")
            continue

        elems = prop.get("elements")
        if not isinstance(elems, list):
            raise AreaEditError(f"{label}.{name}: malformed original property")

        if len(elems) == 0:
            if desired not in ([], None):
                raise AreaEditError(
                    f"{label}.{name}: property originally has zero elements; cardinality changes are not supported"
                )
            continue

        if not all(isinstance(element, dict) for element in elems):
            raise AreaEditError(f"{label}.{name}: every original element must be an object")

        if len(elems) == 1:
            elems[0]["value"] = set_value_from_simple(
                elems[0].get("value"), desired, f"{label}.{name}", prop
            )
            continue

        if not isinstance(desired, list) or len(desired) != len(elems):
            raise AreaEditError(
                f"{label}.{name}: expected a list of exactly {len(elems)} elements; "
                "array resizing is supported only for MapMission.Perks and MapMission.boosters (max 2 entries)"
            )
        for i, (elem, dvalue) in enumerate(zip(elems, desired)):
            elem["value"] = set_value_from_simple(
                elem.get("value"), dvalue, f"{label}.{name}[{i}]", prop
            )



def area_number_from_ref(ref: str) -> int:
    match = re.fullmatch(rf"{re.escape(MAP_AREAS_ROOT)}/MapArea(\d+)", ref)
    if not match:
        raise AreaEditError(f"MapAreasOrder.Areas contains unexpected reference {ref!r}")
    area = int(match.group(1))
    if area < 1:
        raise AreaEditError(f"MapAreasOrder.Areas contains invalid area number {area}")
    return area


def ordered_area_refs(entities: dict[str, dict[str, Any]]) -> list[str]:
    """Return validated MapArea references in gameplay order."""
    order = entities.get(MAP_AREAS_ORDER)
    if order is None or order.get("class") != "MapAreasOrder":
        raise AreaEditError(f"{MAP_AREAS_ORDER} was not found as a MapAreasOrder")

    refs = get_field(order, "Areas")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list) or not refs or not all(isinstance(x, str) for x in refs):
        raise AreaEditError("MapAreasOrder.Areas must contain one or more MapArea references")
    if len(refs) != len(set(refs)):
        raise AreaEditError("MapAreasOrder.Areas contains duplicate area references")

    seen_numbers: set[int] = set()
    for ref in refs:
        area = area_number_from_ref(ref)
        if area in seen_numbers:
            raise AreaEditError(f"MapAreasOrder.Areas lists area number {area} more than once")
        seen_numbers.add(area)
        entity = entities.get(ref)
        if entity is None or entity.get("class") != "MapArea":
            raise AreaEditError(f"MapAreasOrder references missing/non-MapArea entity {ref!r}")

    return list(refs)


def ordered_area_numbers(entities: dict[str, dict[str, Any]]) -> list[int]:
    return [area_number_from_ref(ref) for ref in ordered_area_refs(entities)]


def require_listed_area(entities: dict[str, dict[str, Any]], area: int) -> None:
    if area < 1:
        raise AreaEditError(f"area number must be positive; got {area}")
    numbers = ordered_area_numbers(entities)
    if area not in numbers:
        shown = ", ".join(str(x) for x in numbers[:8])
        if len(numbers) > 8:
            shown += ", ..."
        raise AreaEditError(
            f"Area {area} is not listed in MapAreasOrder.Areas "
            f"({len(numbers)} area(s) present: {shown})"
        )



def mission_suffix_number(name: str, area: int) -> int:
    m = re.fullmatch(rf"Mission{area:03d}_(\d+)", name)
    if not m:
        raise AreaEditError(f"unexpected mission name {name!r} for area {area}")
    return int(m.group(1))


def collect_area_geometry(entities: dict[str, dict[str, Any]], area: int) -> dict[str, Any]:
    apath = f"{MAP_AREAS_ROOT}/MapArea{area:03d}"
    aentity = entities.get(apath)
    if aentity is None or aentity.get("class") != "MapArea":
        raise AreaEditError(f"Area {area}: {apath} was not found as a MapArea")

    adef_path = get_field(aentity, "mapAreaDefinition")
    if not isinstance(adef_path, str) or adef_path not in entities:
        raise AreaEditError(f"Area {area}: invalid mapAreaDefinition reference {adef_path!r}")
    adef = entities[adef_path]
    if adef.get("class") != "MapAreaDef":
        raise AreaEditError(f"Area {area}: {adef_path} is not MapAreaDef")

    main_path_path = get_field(adef, "MainPath")
    if not isinstance(main_path_path, str) or main_path_path not in entities:
        raise AreaEditError(f"Area {area}: invalid MainPath reference {main_path_path!r}")
    main_path = entities[main_path_path]
    if main_path.get("class") != "MapPath":
        raise AreaEditError(f"Area {area}: {main_path_path} is not MapPath")

    milestones = get_field(main_path, "mapPathMilestones")
    if isinstance(milestones, str):
        milestone_paths = [milestones]
    elif isinstance(milestones, list) and all(isinstance(x, str) for x in milestones):
        milestone_paths = list(milestones)
    else:
        raise AreaEditError(f"Area {area}: mapPathMilestones has unexpected value {milestones!r}")

    for mp in milestone_paths:
        e = entities.get(mp)
        if e is None or e.get("class") != "MapPoint":
            raise AreaEditError(f"Area {area}: milestone {mp!r} is missing or is not a MapPoint")

    return {
        "area_entity": aentity,
        "main_path_path": main_path_path,
        "milestone_paths": milestone_paths,
    }


def collect_area_missions(
    entities: dict[str, dict[str, Any]], area: int
) -> list[dict[str, Any]]:
    prefix = f"Mission{area:03d}_"
    found: list[tuple[int, dict[str, Any]]] = []
    root_prefix = MISSION_ROOT + "/"
    for path, entity in entities.items():
        if not path.startswith(root_prefix):
            continue
        name = path.rsplit("/", 1)[-1]
        if not name.startswith(prefix):
            continue
        suffix = mission_suffix_number(name, area)
        if entity.get("class") != "MapMission":
            raise AreaEditError(f"{path} matches Area {area} mission naming but is not MapMission")
        found.append((suffix, entity))
    found.sort(key=lambda item: item[0])
    return [entity for _, entity in found]


def schema_parameter(full: dict[str, Any], type_index: int, label: str) -> dict[str, Any]:
    schema = full.get("schema")
    params = schema.get("parameters") if isinstance(schema, dict) else None
    if not isinstance(params, list) or not (0 <= type_index < len(params)):
        raise AreaEditError(f"{label}: schema parameter index {type_index} is unavailable")
    param = params[type_index]
    if not isinstance(param, dict):
        raise AreaEditError(f"{label}: malformed schema parameter {type_index}")
    return param


def build_id_name_lookup(
    full: dict[str, Any], entity: dict[str, Any], property_name: str, label: str
) -> dict[str, int]:
    """Build name -> numeric id from a Clara id_name property's alias table."""
    prop = get_property(entity, property_name)
    type_index = prop.get("type_index")
    if not isinstance(type_index, int):
        raise AreaEditError(f"{label} has no valid Clara type index")
    param = schema_parameter(full, type_index, label)
    if prop.get("type_code") != 4 or param.get("type_code") != 4:
        raise AreaEditError(f"{label} is not a Clara id_name property")
    aliases = param.get("aliases")
    if not isinstance(aliases, list) or not aliases or not all(isinstance(x, str) for x in aliases):
        raise AreaEditError(f"{label} schema does not expose a usable name/id alias table")
    if len(aliases) != len(set(aliases)):
        raise AreaEditError(f"{label} schema contains duplicate names")
    return {name: index for index, name in enumerate(aliases)}


def build_force_location_lookup(
    entities: dict[str, dict[str, Any]], mission_entity: dict[str, Any]
) -> dict[str, int]:
    """Build ForceLocation name -> numeric value for its exact Clara u32_name type."""
    prop = get_property(mission_entity, "ForceLocation")
    type_index = prop.get("type_index")
    if not isinstance(type_index, int):
        raise AreaEditError("MapMission.ForceLocation has no valid Clara type index")
    if prop.get("type_code") != 0x0800:
        raise AreaEditError("MapMission.ForceLocation is not a Clara u32_name property")

    lookup: dict[str, int] = {}
    for entity in entities.values():
        for candidate in entity.get("properties", []):
            if candidate.get("type_index") != type_index:
                continue
            for element in candidate.get("elements", []):
                value = element.get("value") if isinstance(element, dict) else None
                if not isinstance(value, dict):
                    continue
                name = value.get("name")
                number = value.get("value")
                if (
                    not isinstance(name, str)
                    or not isinstance(number, int)
                    or isinstance(number, bool)
                    or not 0 <= number <= 0xFFFFFFFF
                ):
                    continue
                previous = lookup.get(name)
                if previous is not None and previous != number:
                    raise AreaEditError(
                        f"ForceLocation name {name!r} maps to conflicting values {previous} and {number}"
                    )
                lookup[name] = number
    if not lookup:
        raise AreaEditError("could not build any ForceLocation name/value mappings from the input maplib")
    return lookup


def resolve_named_area_fields(
    desired: dict[str, Any], costume_price_range_lookup: dict[str, int], label: str
) -> dict[str, Any]:
    """Convert name-only CostumePriceRange JSON to Clara's id+name object."""
    if not isinstance(desired, dict):
        raise AreaEditError(f"{label} must be an object")
    out = dict(desired)
    price_name = out.get("CostumePriceRange")

    if not isinstance(price_name, str):
        raise AreaEditError(f"{label}.CostumePriceRange must be a price-range name string")
    if price_name not in costume_price_range_lookup:
        valid = ", ".join(costume_price_range_lookup)
        raise AreaEditError(
            f"{label}.CostumePriceRange: unknown name {price_name!r}; valid names: {valid}"
        )
    out["CostumePriceRange"] = {
        "id": costume_price_range_lookup[price_name],
        "name": price_name,
    }
    return out


def resolve_named_mission_fields(
    desired: dict[str, Any],
    location_lookup: dict[str, int],
    force_location_lookup: dict[str, int],
    label: str,
) -> dict[str, Any]:
    """Convert name-only Location/ForceLocation JSON to Clara's numeric+name objects."""
    if not isinstance(desired, dict):
        raise AreaEditError(f"{label} must be an object")
    out = dict(desired)

    location_name = out.get("Location")
    if not isinstance(location_name, str):
        raise AreaEditError(f"{label}.Location must be a location name string")
    if location_name not in location_lookup:
        valid = ", ".join(location_lookup)
        raise AreaEditError(f"{label}.Location: unknown name {location_name!r}; valid names: {valid}")
    out["Location"] = {"id": location_lookup[location_name], "name": location_name}

    force_name = out.get("ForceLocation")
    if not isinstance(force_name, str):
        raise AreaEditError(f"{label}.ForceLocation must be a force-location name string")
    if force_name not in force_location_lookup:
        valid = ", ".join(sorted(force_location_lookup))
        raise AreaEditError(f"{label}.ForceLocation: unknown name {force_name!r}; valid names: {valid}")
    out["ForceLocation"] = {"value": force_location_lookup[force_name], "name": force_name}
    return out


def build_area_json(full: dict[str, Any], area: int) -> dict[str, Any]:
    """Return the intentionally minimal editable JSON representation."""
    entities = build_entity_index(full)
    require_listed_area(entities, area)
    geometry = collect_area_geometry(entities, area)
    missions = collect_area_missions(entities, area)
    milestone_paths = geometry["milestone_paths"]

    if len(missions) != len(milestone_paths):
        raise AreaEditError(
            f"Area {area}: {len(milestone_paths)} MapPath milestones but {len(missions)} Mission{area:03d}_ records; "
            "ordinal level mapping is ambiguous"
        )

    levels = [
        {
            "level_number": level_no,
            "mission": label_mission_fields(simplify_mission_fields(mission_entity)),
        }
        for level_no, mission_entity in enumerate(missions, 1)
    ]
    return {
        "area": simplify_area_fields(geometry["area_entity"]),
        "levels": levels,
    }


def validate_minimal_doc(doc: dict[str, Any]) -> None:
    if not isinstance(doc, dict):
        raise AreaEditError("editable JSON must be an object")
    expected = {"area", "levels"}
    actual = set(doc)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise AreaEditError(f"JSON is missing top-level key(s): {', '.join(missing)}")
    if extra:
        raise AreaEditError(f"minimal JSON permits only 'area' and 'levels'; remove: {', '.join(extra)}")
    if not isinstance(doc["area"], dict):
        raise AreaEditError("area must be an object containing all MapArea properties")
    if not isinstance(doc["levels"], list):
        raise AreaEditError("levels must be a list")

def validate_mission_ids_unique(entities: dict[str, dict[str, Any]]) -> None:
    seen: dict[int, str] = {}
    duplicates: list[tuple[int, str, str]] = []
    for path, entity in entities.items():
        if entity.get("class") != "MapMission":
            continue
        mid = get_field(entity, "MissionId")
        if not isinstance(mid, int) or isinstance(mid, bool):
            raise AreaEditError(f"{path}.MissionId must be an integer; got {mid!r}")
        if mid in seen:
            duplicates.append((mid, seen[mid], path))
        else:
            seen[mid] = path
    if duplicates:
        detail = "; ".join(f"{mid}: {a} and {b}" for mid, a, b in duplicates[:10])
        raise AreaEditError(f"duplicate MissionId values: {detail}")


def apply_area_json(full: dict[str, Any], area: int, doc: dict[str, Any]) -> tuple[int, int]:
    validate_minimal_doc(doc)
    entities = build_entity_index(full)
    require_listed_area(entities, area)
    geometry = collect_area_geometry(entities, area)
    missions = collect_area_missions(entities, area)
    milestone_paths = geometry["milestone_paths"]
    jlevels = doc["levels"]

    if len(jlevels) != len(missions) or len(jlevels) != len(milestone_paths):
        raise AreaEditError(
            f"Area {area}: level count is structural and must remain {len(missions)}; JSON contains {len(jlevels)}"
        )

    # The area object contains exactly the MapArea properties and all are editable.
    # CostumePriceRange is name-only in JSON; reconstruct its enum id from schema.
    costume_price_range_lookup = build_id_name_lookup(
        full, geometry["area_entity"], "CostumePriceRange", "MapArea.CostumePriceRange"
    )
    desired_area = resolve_named_area_fields(
        doc["area"], costume_price_range_lookup, f"Area {area}.area"
    )
    apply_fields(geometry["area_entity"], desired_area, f"Area {area}.area")

    if not missions:
        raise AreaEditError(f"Area {area} contains no MapMission records")
    location_lookup = build_id_name_lookup(
        full, missions[0], "Location", "MapMission.Location"
    )
    force_location_lookup = build_force_location_lookup(entities, missions[0])
    target_paths = {path for path, e in entities.items() if e.get("class") == "MapTarget"}

    changed_level_count = 0
    for i, (jlevel, mission_entity) in enumerate(zip(jlevels, missions), 1):
        if not isinstance(jlevel, dict):
            raise AreaEditError(f"levels[{i-1}] must be an object")
        if set(jlevel) != {"level_number", "mission"}:
            raise AreaEditError(
                f"levels[{i-1}] may contain only read-only level_number and editable mission"
            )
        if jlevel.get("level_number") != i:
            raise AreaEditError(f"levels[{i-1}].level_number is read-only and must remain {i}")

        before = simplify_mission_fields(mission_entity)
        mission_label = f"Area {area} Level {i}.mission"
        flat_mission = unlabel_mission_fields(jlevel.get("mission"), mission_label)
        desired = resolve_named_mission_fields(
            flat_mission, location_lookup, force_location_lookup, mission_label
        )
        apply_fields(
            mission_entity,
            desired,
            f"Area {area} Level {i}.mission",
            resizable_fields=CAPPED_MISSION_ARRAY_FIELDS,
        )
        after = simplify_mission_fields(mission_entity)
        if before != after:
            changed_level_count += 1

        objective = after.get("MissionTarget")
        if not isinstance(objective, str) or objective not in target_paths:
            raise AreaEditError(
                f"Area {area} Level {i}: MissionTarget {objective!r} does not resolve to a MapTarget in this maplib"
            )

    validate_mission_ids_unique(entities)
    return area, changed_level_count

def cmd_decode(args: argparse.Namespace) -> None:
    data = args.input.read_bytes()
    full = blibclara_editor.decode_file(data, args.input.name)
    doc = build_area_json(full, args.area)
    args.output.write_text(json.dumps(doc, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"Decoded Area {args.area}: {len(doc['levels'])} levels -> {args.output}")


def _reject_json_constant(value: str) -> None:
    raise AreaEditError(f"invalid JSON numeric constant {value!r}")


def cmd_encode(args: argparse.Namespace) -> None:
    original = args.input.read_bytes()
    full = blibclara_editor.decode_file(original, args.input.name)
    try:
        doc = json.loads(
            args.json.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise AreaEditError(f"invalid JSON: {exc}") from exc

    area, changed_levels = apply_area_json(full, args.area, doc)
    expected_doc = build_area_json(full, area)
    rebuilt = blibclara_editor.encode_manifest(original, full)

    # Verify the exact rebuilt bytes in memory before touching the output path.
    check = blibclara_editor.decode_file(rebuilt, args.output.name)
    check_doc = build_area_json(check, area)
    if check_doc != expected_doc:
        raise AreaEditError(
            "post-encode verification failed: rebuilt area does not match the edited minimal JSON"
        )

    args.output.write_bytes(rebuilt)
    print(
        f"Encoded Area {area} edits -> {args.output}\n"
        f"Changed mission records: {changed_levels}/{len(doc['levels'])}\n"
        f"Output bytes: {len(rebuilt)}\n"
        f"Output SHA-256: {hashlib.sha256(rebuilt).hexdigest()}"
    )


def cmd_check(args: argparse.Namespace) -> None:
    data = args.input.read_bytes()
    full = blibclara_editor.decode_file(data, args.input.name)
    entities = build_entity_index(full)
    area_numbers = ordered_area_numbers(entities)
    order = entities[MAP_AREAS_ORDER]
    total_exposed = get_field(order, "TotalAreasExposed")

    rows = []
    for area in area_numbers:
        try:
            geometry = collect_area_geometry(entities, area)
            missions = collect_area_missions(entities, area)
            rows.append((area, len(geometry["milestone_paths"]), len(missions), geometry["main_path_path"]))
        except AreaEditError as exc:
            rows.append((area, None, None, f"ERROR: {exc}"))

    bad = [row for row in rows if row[1] is None or row[1] != row[2]]
    for area, nodes, missions, path in rows:
        print(f"Area {area:03d}: nodes={str(nodes):>2} missions={str(missions):>2}  {path}")

    if not isinstance(total_exposed, int) or isinstance(total_exposed, bool):
        raise AreaEditError(f"MapAreasOrder.TotalAreasExposed must be an integer; got {total_exposed!r}")
    if total_exposed != len(area_numbers):
        raise AreaEditError(
            f"MapAreasOrder.TotalAreasExposed is {total_exposed}, but Areas contains {len(area_numbers)} entries"
        )
    if bad:
        raise AreaEditError(f"{len(bad)} area(s) have ambiguous node/mission counts")

    validate_mission_ids_unique(entities)
    print(
        f"All {len(rows)} listed areas have matching MapPoint/Mission counts; "
        f"TotalAreasExposed={total_exposed}; MissionId values are unique."
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Decode/edit any Jelly Lab area as minimal JSON containing only area and levels, "
            "including cloned areas above 60."
        )
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("decode", help="maplib + area number -> minimal editable area JSON")
    d.add_argument("input", type=Path, help="input maplib.blibclara")
    d.add_argument("area", type=int, help="Jelly Lab area number listed in MapAreasOrder")
    d.add_argument("output", type=Path, help="output editable JSON")
    d.set_defaults(func=cmd_decode)

    e = sub.add_parser("encode", help="maplib + area number + edited minimal JSON -> rebuilt maplib")
    e.add_argument("input", type=Path, help="input maplib.blibclara")
    e.add_argument("area", type=int, help="Jelly Lab area number represented by the JSON")
    e.add_argument("json", type=Path, help="edited minimal selected-area JSON")
    e.add_argument("output", type=Path, help="output rebuilt maplib.blibclara")
    e.set_defaults(func=cmd_encode)

    c = sub.add_parser("check", help="validate every area listed in MapAreasOrder")
    c.add_argument("input", type=Path, help="input maplib.blibclara")
    c.set_defaults(func=cmd_check)

    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except (AreaEditError, blibclara_editor.CodecError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
