#!/usr/bin/env python3
"""Clone the current last Minion Rush Jelly Lab area as a new last area.

Requires ``blibclara_editor.py`` beside this script (or on ``PYTHONPATH``).
The generic Clara codec lives there; this tool contains only Jelly Lab cloning logic.

Usage::

    python clone_last_area.py maplib.blibclara
    python clone_last_area.py maplib.blibclara maplib_extended.blibclara

Behavior:
* discovers the source area from the final ``MapAreasOrder.Areas`` entry;
* clones it as ``MapArea(N+1)`` and appends it to the area order;
* updates ``TotalAreasExposed`` to the new ordered-area count;
* clones every ``MapMission`` belonging to the source area;
* assigns fresh consecutive ``MissionId`` values after the current maximum;
* preserves the source area's MapAreaDef/MapPath geometry references unchanged;
* freshly decodes and semantically verifies the rebuilt bytes before writing output.

The input is never overwritten. Supplying the same input and output path is rejected.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import sys
from pathlib import Path
from typing import Any

try:
    import blibclara_editor as clara
except ImportError as exc:
    raise SystemExit(
        "ERROR: blibclara_editor.py is required. Put it beside this script or on PYTHONPATH."
    ) from exc

for _name in ("CodecError", "decode_file", "encode_manifest"):
    if not hasattr(clara, _name):
        raise SystemExit(f"ERROR: current blibclara_editor.py is missing required API {_name!r}")


MAP_AREAS_ROOT = "/MapLevelDef/MapSystem/MapAreas"
MAP_AREAS_ORDER = "/MapLevelDef/MapSystem/MapAreasOrder"
MISSION_ROOT = "/MapMissions/MapMissionDef"


class CloneError(RuntimeError):
    pass


def _join_path(parent: str, name: Any, label: str) -> str:
    if not isinstance(name, str) or not name:
        raise CloneError(f"{label} has an invalid or empty name: {name!r}")
    return f"{parent}/{name}" if parent else f"/{name}"


def _build_path_indexes(
    full: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Index entity and folder paths, rejecting ambiguous duplicate paths."""
    entities: dict[str, dict[str, Any]] = {}
    folders: dict[str, dict[str, Any]] = {}

    def walk(folder: dict[str, Any], path: str) -> None:
        if path in folders:
            raise CloneError(f"duplicate folder path: {path}")
        folders[path] = folder

        records = folder.get("records")
        if not isinstance(records, list):
            raise CloneError(f"{path}: records must be a list")

        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise CloneError(f"{path}.records[{index}] must be an object")
            kind = record.get("kind")
            if kind == "folder":
                child = record.get("folder")
                if not isinstance(child, dict):
                    raise CloneError(f"{path}.records[{index}]: malformed folder record")
                child_path = _join_path(path, child.get("name"), f"{path}.records[{index}].folder")
                walk(child, child_path)
            elif kind == "entity":
                entity = record.get("entity")
                if not isinstance(entity, dict):
                    raise CloneError(f"{path}.records[{index}]: malformed entity record")
                entity_path = _join_path(path, entity.get("name"), f"{path}.records[{index}].entity")
                if entity_path in entities:
                    raise CloneError(f"duplicate entity path: {entity_path}")
                entities[entity_path] = entity

    libraries = full.get("libraries")
    if not isinstance(libraries, list):
        raise CloneError("decoded Clara file has no valid libraries list")

    for index, library in enumerate(libraries):
        if not isinstance(library, dict):
            raise CloneError(f"libraries[{index}] must be an object")
        root = library.get("root_folder")
        if not isinstance(root, dict):
            raise CloneError(f"libraries[{index}].root_folder is missing or malformed")
        root_path = _join_path("", root.get("name"), f"libraries[{index}].root_folder")
        walk(root, root_path)

    return entities, folders


def _get_property(entity: dict[str, Any], name: str) -> dict[str, Any]:
    properties = entity.get("properties")
    if not isinstance(properties, list):
        raise CloneError(f"{entity.get('name', '<unnamed>')}: properties must be a list")
    matches = [prop for prop in properties if isinstance(prop, dict) and prop.get("name") == name]
    if len(matches) != 1:
        raise CloneError(
            f"{entity.get('name', '<unnamed>')}: expected exactly one property {name!r}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _get_single(entity: dict[str, Any], name: str) -> Any:
    elements = _get_property(entity, name).get("elements")
    if not isinstance(elements, list) or len(elements) != 1 or not isinstance(elements[0], dict):
        count = len(elements) if isinstance(elements, list) else "invalid"
        raise CloneError(
            f"{entity.get('name', '<unnamed>')}.{name}: expected exactly one element, got {count}"
        )
    return elements[0].get("value")


def _set_single(entity: dict[str, Any], name: str, value: Any) -> None:
    elements = _get_property(entity, name).get("elements")
    if not isinstance(elements, list) or len(elements) != 1 or not isinstance(elements[0], dict):
        count = len(elements) if isinstance(elements, list) else "invalid"
        raise CloneError(
            f"{entity.get('name', '<unnamed>')}.{name}: expected exactly one element, got {count}"
        )
    elements[0]["value"] = value


def _find_entity_record(folder: dict[str, Any], name: str) -> dict[str, Any]:
    records = folder.get("records")
    if not isinstance(records, list):
        raise CloneError(f"folder {folder.get('name', '<unnamed>')!r}: records must be a list")
    matches = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("kind") == "entity"
        and isinstance(record.get("entity"), dict)
        and record["entity"].get("name") == name
    ]
    if len(matches) != 1:
        raise CloneError(
            f"expected exactly one entity record {name!r} in folder "
            f"{folder.get('name', '<unnamed>')!r}, found {len(matches)}"
        )
    return matches[0]


def _mission_id(entity: dict[str, Any]) -> int:
    value = _get_single(entity, "MissionId")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CloneError(f"{entity.get('name', '<unnamed>')}.MissionId is invalid: {value!r}")
    return value


def _all_mission_ids(entities: dict[str, dict[str, Any]]) -> list[int]:
    return [
        _mission_id(entity)
        for entity in entities.values()
        if entity.get("class") == "MapMission"
    ]


def _collect_area_missions(
    entities: dict[str, dict[str, Any]], area: int
) -> list[tuple[str, dict[str, Any]]]:
    prefix = f"{MISSION_ROOT}/Mission{area:03d}_"
    missions = [
        (path, entity)
        for path, entity in entities.items()
        if path.startswith(prefix) and entity.get("class") == "MapMission"
    ]
    missions.sort(key=lambda item: _mission_id(item[1]))
    return missions


def _parse_ordered_area_path(path: Any) -> int:
    if not isinstance(path, str):
        raise CloneError(f"last MapAreasOrder.Areas value is not a string: {path!r}")
    prefix = MAP_AREAS_ROOT + "/MapArea"
    if not path.startswith(prefix):
        raise CloneError(f"unexpected last MapAreasOrder area reference: {path}")
    digits = path[len(prefix):]
    if not digits.isdigit():
        raise CloneError(f"cannot parse area number from last ordered area: {path}")
    area = int(digits)
    if area <= 0:
        raise CloneError(f"invalid last ordered area number: {area}")
    return area


def _field_signature(
    entity: dict[str, Any], *, exclude: frozenset[str] = frozenset()
) -> tuple[Any, ...]:
    """Return property values/names without codec provenance fields."""
    properties = entity.get("properties")
    if not isinstance(properties, list):
        raise CloneError(f"{entity.get('name', '<unnamed>')}: properties must be a list")
    result = []
    for prop in properties:
        if not isinstance(prop, dict):
            raise CloneError(f"{entity.get('name', '<unnamed>')}: malformed property record")
        name = prop.get("name")
        if name in exclude:
            continue
        elements = prop.get("elements")
        if not isinstance(elements, list):
            raise CloneError(f"{entity.get('name', '<unnamed>')}.{name}: elements must be a list")
        values = []
        for element in elements:
            if not isinstance(element, dict):
                raise CloneError(f"{entity.get('name', '<unnamed>')}.{name}: malformed element")
            values.append((element.get("name"), copy.deepcopy(element.get("value"))))
        result.append((name, prop.get("type_index"), prop.get("named_elements"), tuple(values)))
    return tuple(result)


def _default_output_path(input_path: Path, new_area: int) -> Path:
    tag = f"_area{new_area}"
    if input_path.suffix:
        return input_path.with_name(input_path.stem + tag + input_path.suffix)
    return input_path.with_name(input_path.name + tag)


def clone_last_area(input_path: Path, output_path: Path | None = None) -> Path:
    """Clone the final ordered Jelly Lab area and its missions."""
    input_path = input_path.resolve()
    if not input_path.is_file():
        raise CloneError(f"input file does not exist: {input_path}")

    original = input_path.read_bytes()
    try:
        full = clara.decode_file(original, input_path.name)
    except clara.CodecError as exc:
        raise CloneError(str(exc)) from exc

    entities, folders = _build_path_indexes(full)
    for required_path, label in (
        (MAP_AREAS_ROOT, "MapAreas folder"),
        (MISSION_ROOT, "MapMission folder"),
    ):
        if required_path not in folders:
            raise CloneError(f"{label} not found: {required_path}")
    if MAP_AREAS_ORDER not in entities:
        raise CloneError(f"MapAreasOrder entity not found: {MAP_AREAS_ORDER}")

    order = entities[MAP_AREAS_ORDER]
    areas_elements = _get_property(order, "Areas").get("elements")
    if not isinstance(areas_elements, list) or not areas_elements:
        raise CloneError("MapAreasOrder.Areas is empty or malformed")
    if not isinstance(areas_elements[-1], dict):
        raise CloneError("last MapAreasOrder.Areas element is malformed")

    source_area_path = areas_elements[-1].get("value")
    source_area = _parse_ordered_area_path(source_area_path)
    new_area = source_area + 1
    source_area_name = f"MapArea{source_area:03d}"
    new_area_name = f"MapArea{new_area:03d}"
    expected_source_path = f"{MAP_AREAS_ROOT}/{source_area_name}"
    new_area_path = f"{MAP_AREAS_ROOT}/{new_area_name}"

    if source_area_path != expected_source_path:
        raise CloneError(
            "last ordered area reference does not use the expected canonical name: "
            f"{source_area_path} (expected {expected_source_path})"
        )
    if source_area_path not in entities:
        raise CloneError(f"source Area {source_area} not found: {source_area_path}")
    if new_area_path in entities:
        raise CloneError(f"Area {new_area} already exists: {new_area_path}")

    output_path = (
        output_path.resolve()
        if output_path is not None
        else _default_output_path(input_path, new_area).resolve()
    )
    if input_path == output_path:
        raise CloneError("refusing to overwrite the input file; choose a different output path")

    area_folder = folders[MAP_AREAS_ROOT]
    mission_folder = folders[MISSION_ROOT]
    source_missions = _collect_area_missions(entities, source_area)
    if not source_missions:
        raise CloneError(f"no MapMission records were found for Area {source_area}")

    existing_ids = _all_mission_ids(entities)
    if not existing_ids:
        raise CloneError("no MapMission MissionId values were found")
    if len(existing_ids) != len(set(existing_ids)):
        raise CloneError("input already contains duplicate MapMission MissionId values")

    first_new_id = max(existing_ids) + 1
    last_new_id = first_new_id + len(source_missions) - 1
    if last_new_id > 0x7FFFFFFF:
        raise CloneError(
            f"new MissionId range {first_new_id}..{last_new_id} exceeds signed i32 range"
        )

    planned_missions: list[tuple[str, str, int]] = []
    for index, (source_path, _source_entity) in enumerate(source_missions):
        source_name = source_path.rsplit("/", 1)[-1]
        new_id = first_new_id + index
        new_name = f"Mission{new_area:03d}_{new_id:04d}"
        new_path = f"{MISSION_ROOT}/{new_name}"
        if new_path in entities:
            raise CloneError(f"new mission name already exists: {new_path}")
        planned_missions.append((source_name, new_name, new_id))

    # Mutate only after all preconditions and generated names/IDs have been validated.
    source_area_record = _find_entity_record(area_folder, source_area_name)
    new_area_record = copy.deepcopy(source_area_record)
    new_area_record["entity"]["name"] = new_area_name
    area_folder["records"].append(new_area_record)

    new_order_element = copy.deepcopy(areas_elements[-1])
    new_order_element["value"] = new_area_path
    areas_elements.append(new_order_element)
    new_area_count = len(areas_elements)
    _set_single(order, "TotalAreasExposed", new_area_count)

    new_ids: list[int] = []
    for source_name, new_name, new_id in planned_missions:
        source_record = _find_entity_record(mission_folder, source_name)
        clone = copy.deepcopy(source_record)
        clone["entity"]["name"] = new_name
        _set_single(clone["entity"], "MissionId", new_id)
        mission_folder["records"].append(clone)
        new_ids.append(new_id)

    try:
        rebuilt = clara.encode_manifest(original, full)
        check = clara.decode_file(rebuilt, output_path.name)
    except clara.CodecError as exc:
        raise CloneError(str(exc)) from exc

    entities_after, _ = _build_path_indexes(check)
    if new_area_path not in entities_after:
        raise CloneError(f"post-encode verification failed: {new_area_name} is missing")

    order_after = entities_after.get(MAP_AREAS_ORDER)
    if not isinstance(order_after, dict):
        raise CloneError("post-encode verification failed: MapAreasOrder is missing")
    order_elements = _get_property(order_after, "Areas").get("elements")
    if not isinstance(order_elements, list):
        raise CloneError("post-encode verification failed: MapAreasOrder.Areas is malformed")
    order_values = [element.get("value") if isinstance(element, dict) else None for element in order_elements]
    if len(order_values) != new_area_count or order_values[-1] != new_area_path:
        raise CloneError("post-encode verification failed: new area is not the final ordered area")
    if _get_single(order_after, "TotalAreasExposed") != new_area_count:
        raise CloneError(
            "post-encode verification failed: TotalAreasExposed does not match ordered-area count"
        )

    source_area_after = entities_after.get(source_area_path)
    new_area_after = entities_after.get(new_area_path)
    if not isinstance(source_area_after, dict) or not isinstance(new_area_after, dict):
        raise CloneError("post-encode verification failed: source/new area entity is missing")
    if _field_signature(source_area_after) != _field_signature(new_area_after):
        raise CloneError(
            f"post-encode verification failed: {new_area_name} fields differ from {source_area_name}"
        )

    new_missions = _collect_area_missions(entities_after, new_area)
    actual_new_ids = [_mission_id(entity) for _, entity in new_missions]
    if actual_new_ids != new_ids:
        raise CloneError(
            f"post-encode verification failed: Area {new_area} IDs are {actual_new_ids}, "
            f"expected {new_ids}"
        )
    if len(new_missions) != len(source_missions):
        raise CloneError(
            f"post-encode verification failed: Area {new_area} mission count "
            f"does not match Area {source_area}"
        )

    for (source_path, source_entity), (_, cloned_entity) in zip(source_missions, new_missions):
        if _field_signature(source_entity, exclude=frozenset({"MissionId"})) != _field_signature(
            cloned_entity, exclude=frozenset({"MissionId"})
        ):
            raise CloneError(
                "post-encode verification failed: cloned mission fields differ from source "
                f"{source_path}"
            )

    all_ids_after = _all_mission_ids(entities_after)
    if len(all_ids_after) != len(set(all_ids_after)):
        raise CloneError("post-encode verification failed: duplicate MissionId values")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(rebuilt)

    print(f"OK: cloned Jelly Lab Area {source_area} -> Area {new_area}")
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print(f"Ordered areas: {new_area_count}")
    print(f"Area {source_area} missions: {len(source_missions)}")
    print(f"Area {new_area} missions: {len(new_missions)}")
    print(f"Area {new_area} MissionIds: {new_ids[0]}..{new_ids[-1]}")
    print(f"TotalAreasExposed: {_get_single(order_after, 'TotalAreasExposed')}")
    print(f"Input SHA-256:  {hashlib.sha256(original).hexdigest()}")
    print(f"Output SHA-256: {hashlib.sha256(rebuilt).hexdigest()}")
    return output_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clone the current last ordered Minion Rush Jelly Lab area as a new last area"
    )
    parser.add_argument("input", type=Path, help="input maplib.clara or maplib.blibclara")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help="output file (default: INPUT_area<N+1>.EXT)",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        clone_last_area(args.input, args.output)
        return 0
    except (CloneError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
