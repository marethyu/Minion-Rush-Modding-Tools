#!/usr/bin/env python3
"""
Focused Minion Rush designlib.blibclara costume JSON editor.

This is intentionally a THIN LAYER over ``blibclara_editor.py``.  It does not
reimplement the Clara binary codec.  The dependency performs schema parsing,
recursive entity decoding, binary rebuilding, and semantic verification.

Typical workflow
----------------
Decode every MinionCostume entity to a focused editable JSON file::

    python costume_json_editor.py decode designlib.blibclara costumes.json

Edit ``costumes.json`` (including nested costumeUpgradeArray / bonuses values),
then rebuild designlib::

    python costume_json_editor.py encode \
        designlib.blibclara costumes.json designlib_mod.blibclara

Inspect the source without writing JSON::

    python costume_json_editor.py check designlib.blibclara

Safety model
------------
* The exact original designlib used for decode is required for encode.
* Costume entity paths and costume count are structural and cannot change.
* Every original MinionCostume property is emitted and must remain present.
* Nested entity classes, nested-entity presence, and array cardinalities cannot
  change.  This includes bodyParts, costumeUpgradeArray, bonuses, TextValue,
  and other nested arrays.
* Scalar/property values are editable, including BonusForCostumes SkillType,
  Amount, GameItemType, conditions, prices, flags, references, etc.
* f32/f64 values are canonicalized before rebuilding so ordinary JSON decimals
  such as 12.3 round-trip through the strict generic editor correctly.
* Rebuilt bytes are freshly decoded and structurally verified before the output
  file is written, so failed verification cannot leave an unverified result.

The focused JSON intentionally keeps Clara id/name values as ``{"id": ...,\n"name": ...}``.  For enum-like fields, edit both members consistently.  The
``catalogs`` section lists aliases observed in the embedded schema, including
CostumeSkillType and gameItemType.
"""

from __future__ import annotations

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

try:
    import blibclara_editor as clara
except ImportError as exc:  # pragma: no cover - user-facing dependency error
    raise SystemExit(
        "ERROR: blibclara_editor.py is required. Put it in the same directory "
        "as this script (or on PYTHONPATH)."
    ) from exc


FORMAT = "minion-rush-designlib-costumes-editable"
FORMAT_VERSION = 1
COSTUME_CLASS = "MinionCostume"
COSTUME_ROOT = "/Minion_Costumes_update1/MinionCostumes"


class CostumeEditError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_editor_api() -> None:
    required = ("decode_file", "encode_manifest", "CodecError")
    missing = [name for name in required if not hasattr(clara, name)]
    if missing:
        raise CostumeEditError(
            "blibclara_editor.py is missing required API(s): " + ", ".join(missing)
        )


def build_indexes(full: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return an absolute entity-path index into *full*.

    Values are the actual mutable entity objects inside the decoded manifest, so
    edits through the index are reflected in the tree passed to
    ``clara.encode_manifest``. Folder paths are tracked only to reject malformed
    duplicate structure; no unused folder index is retained.
    """
    entities: dict[str, dict[str, Any]] = {}
    folder_paths: set[str] = set()

    def child_path(parent: str, name: Any, kind: str) -> str:
        if not isinstance(name, str) or not name:
            raise CostumeEditError(f"{kind} below {parent or '/'} has an invalid name")
        return f"{parent}/{name}" if parent else f"/{name}"

    def walk(folder: dict[str, Any], path: str) -> None:
        if path in folder_paths:
            raise CostumeEditError(f"duplicate folder path {path}")
        folder_paths.add(path)

        records = folder.get("records", [])
        if not isinstance(records, list):
            raise CostumeEditError(f"folder {path} has malformed records")

        for rec in records:
            if not isinstance(rec, dict):
                raise CostumeEditError(f"malformed record below {path}")
            kind = rec.get("kind")
            if kind == "folder":
                child = rec.get("folder")
                if not isinstance(child, dict):
                    raise CostumeEditError(f"malformed folder record below {path}")
                walk(child, child_path(path, child.get("name"), "folder"))
            elif kind == "entity":
                entity = rec.get("entity")
                if not isinstance(entity, dict):
                    raise CostumeEditError(f"malformed entity record below {path}")
                entity_path = child_path(path, entity.get("name"), "entity")
                if entity_path in entities:
                    raise CostumeEditError(f"duplicate entity path {entity_path}")
                entities[entity_path] = entity

    libraries = full.get("libraries", [])
    if not isinstance(libraries, list):
        raise CostumeEditError("decoded manifest has malformed libraries")
    for lib in libraries:
        if not isinstance(lib, dict):
            raise CostumeEditError("decoded manifest contains a malformed library")
        root = lib.get("root_folder")
        if not isinstance(root, dict):
            continue
        walk(root, child_path("", root.get("name"), "root folder"))

    return entities


def simplify_value(value: Any) -> Any:
    """Remove codec provenance while preserving the complete editable value."""
    if isinstance(value, list):
        return [simplify_value(x) for x in value]
    if isinstance(value, dict):
        if isinstance(value.get("class"), str) and isinstance(value.get("properties"), list):
            return simplify_entity(value)
        if value.get("opaque") is True and isinstance(value.get("class"), str):
            return {
                "class": value.get("class"),
                "name": value.get("name", ""),
                "opaque": True,
            }
        return {
            k: simplify_value(v)
            for k, v in value.items()
            if k
            not in {
                "raw_base64",
                "original_value",
                "original_name",
                "source_offset",
                "source_size",
                "preamble_hex",
                "opaque_body_base64",
            }
        }
    return value


def simplify_property(prop: dict[str, Any]) -> Any:
    values = [simplify_value(el.get("value")) for el in prop.get("elements", [])]
    if not values:
        return []
    if len(values) == 1:
        return values[0]
    return values


def simplify_fields(entity: dict[str, Any]) -> dict[str, Any]:
    return {prop["name"]: simplify_property(prop) for prop in entity.get("properties", [])}


def simplify_entity(entity: dict[str, Any]) -> dict[str, Any]:
    if entity.get("opaque") is True:
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


def schema_parameter_map(full: dict[str, Any]) -> dict[int, dict[str, Any]]:
    params = full.get("schema", {}).get("parameters", [])
    out: dict[int, dict[str, Any]] = {}
    for p in params:
        if isinstance(p, dict) and isinstance(p.get("index"), int):
            out[p["index"]] = p
    return out


def canonicalize_scalar_for_param(value: Any, param: dict[str, Any] | None, label: str) -> Any:
    """Canonicalize JSON numerics to the value the Clara decoder will return.

    ``blibclara_editor.encode_manifest`` compares the requested decoded tree to
    a fresh decode exactly.  Python's 12.3 is not exactly the same number as an
    IEEE-754 f32 decoded back from disk, so f32 edits need explicit narrowing
    before the generic editor performs its semantic verification.
    """
    if not isinstance(param, dict) or param.get("type_code") != 2:
        return copy.deepcopy(value)

    subtype = param.get("subtype")
    if subtype in (3, 4):  # f32 / f64
        kind = "f32" if subtype == 3 else "f64"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CostumeEditError(f"{label}: expected numeric {kind} value")
        try:
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError
            if subtype == 3:
                numeric = struct.unpack("<f", struct.pack("<f", numeric))[0]
                if not math.isfinite(numeric):
                    raise ValueError
            return numeric
        except (OverflowError, struct.error, ValueError) as exc:
            raise CostumeEditError(f"{label}: value is outside finite {kind} range") from exc

    # Integer subtypes are intentionally left as integers; the generic codec
    # performs the authoritative range validation.
    return copy.deepcopy(value)


def set_nested_entity_from_simple(
    original: dict[str, Any],
    desired: dict[str, Any],
    label: str,
    params: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    if original.get("opaque") is True:
        raise CostumeEditError(f"{label}: opaque nested entities are read-only")
    if not isinstance(desired, dict):
        raise CostumeEditError(f"{label}: nested entity must be an object")
    if desired.get("class") != original.get("class"):
        raise CostumeEditError(
            f"{label}: changing nested class {original.get('class')!r} -> "
            f"{desired.get('class')!r} is not supported"
        )

    out = copy.deepcopy(original)
    # Nested entity names are editable values, but the focused JSON is complete:
    # silently omitting one would hide an accidental JSON edit.
    if "name" not in desired or not isinstance(desired["name"], str):
        raise CostumeEditError(f"{label}.name must be present and be a string")
    out["name"] = desired["name"]

    dfields = desired.get("fields")
    if not isinstance(dfields, dict):
        raise CostumeEditError(f"{label}.fields must be an object")
    apply_fields(out, dfields, label, params)
    return out


def set_value_from_simple(
    original: Any,
    desired: Any,
    label: str,
    param: dict[str, Any] | None,
    params: dict[int, dict[str, Any]],
) -> Any:
    if isinstance(original, dict) and isinstance(original.get("class"), str):
        if original.get("opaque") is True or isinstance(original.get("properties"), list):
            return set_nested_entity_from_simple(original, desired, label, params)

    # Optional nested-entity presence is structural in this focused editor.
    if param and param.get("type_code") == 32:
        if original is None:
            if desired is not None:
                raise CostumeEditError(
                    f"{label}: creating a nested entity where the original value was null is not supported"
                )
            return None
        if desired is None:
            raise CostumeEditError(
                f"{label}: deleting a nested entity is not supported by this focused editor"
            )

    return canonicalize_scalar_for_param(desired, param, label)


def apply_fields(
    entity: dict[str, Any],
    desired_fields: dict[str, Any],
    label: str,
    params: dict[int, dict[str, Any]],
) -> None:
    """Apply focused JSON fields to a decoded entity in place.

    Property set, element count, and nested classes are fixed.  Values are
    editable recursively.
    """
    if not isinstance(desired_fields, dict):
        raise CostumeEditError(f"{label} must be an object")

    props = entity.get("properties", [])
    pmap = {p.get("name"): p for p in props}
    expected = set(pmap)
    actual = set(desired_fields)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise CostumeEditError(f"{label}: missing field(s): {', '.join(missing)}")
    if extra:
        raise CostumeEditError(f"{label}: unknown field(s): {', '.join(extra)}")

    for name, prop in pmap.items():
        elems = prop.get("elements")
        if not isinstance(elems, list):
            raise CostumeEditError(f"{label}.{name}: malformed original property")
        desired = desired_fields[name]
        param = params.get(prop.get("type_index"))

        if len(elems) == 0:
            if desired not in ([], None):
                raise CostumeEditError(
                    f"{label}.{name}: property originally has zero elements; "
                    "cardinality changes are not supported"
                )
            continue

        if len(elems) == 1:
            elems[0]["value"] = set_value_from_simple(
                elems[0].get("value"), desired, f"{label}.{name}", param, params
            )
            continue

        if not isinstance(desired, list) or len(desired) != len(elems):
            raise CostumeEditError(
                f"{label}.{name}: expected a list of exactly {len(elems)} elements; "
                "array resizing is not supported by this costume-parameter editor"
            )
        for i, (elem, dvalue) in enumerate(zip(elems, desired)):
            elem["value"] = set_value_from_simple(
                elem.get("value"), dvalue, f"{label}.{name}[{i}]", param, params
            )


def costume_sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
    path, entity = item
    name = entity.get("name", path.rsplit("/", 1)[-1])
    m = re.match(r"^(\d+)_", str(name))
    return (int(m.group(1)) if m else 1_000_000, str(name).lower())


def collect_costumes(
    entities: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    rows = [
        (path, entity)
        for path, entity in entities.items()
        if entity.get("class") == COSTUME_CLASS
    ]
    rows.sort(key=costume_sort_key)
    if not rows:
        raise CostumeEditError("no MinionCostume entities were found in this designlib")
    return rows


def get_property(entity: dict[str, Any], name: str) -> dict[str, Any] | None:
    for prop in entity.get("properties", []):
        if prop.get("name") == name:
            return prop
    return None


def simple_field(entity: dict[str, Any], name: str) -> Any:
    prop = get_property(entity, name)
    return simplify_property(prop) if prop is not None else None


def collect_used_param_indices_from_entity(entity: dict[str, Any], out: set[int]) -> None:
    if entity.get("opaque") is True:
        return
    for prop in entity.get("properties", []):
        ti = prop.get("type_index")
        if isinstance(ti, int):
            out.add(ti)
        for elem in prop.get("elements", []):
            value = elem.get("value")
            if (
                isinstance(value, dict)
                and isinstance(value.get("class"), str)
                and isinstance(value.get("properties"), list)
            ):
                collect_used_param_indices_from_entity(value, out)


def build_catalogs(full: dict[str, Any], costumes: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    used: set[int] = set()
    for _path, entity in costumes:
        collect_used_param_indices_from_entity(entity, used)

    params = schema_parameter_map(full)
    catalogs: dict[str, Any] = {}
    for idx in sorted(used):
        p = params.get(idx)
        if not p:
            continue
        aliases = p.get("aliases")
        if not isinstance(aliases, list) or not aliases:
            continue
        catalogs[p.get("name", f"type_{idx}")] = {
            "type_index": idx,
            "type_name": p.get("type_name"),
            "values": [{"id": i, "name": name} for i, name in enumerate(aliases)],
        }
    return catalogs


def count_costume_stats(costumes: list[tuple[str, dict[str, Any]]]) -> dict[str, int]:
    upgrades = 0
    bonuses = 0
    multi_bonus_levels = 0
    no_bonus_levels = 0

    for _path, entity in costumes:
        p = get_property(entity, "costumeUpgradeArray")
        if p is None:
            continue
        for elem in p.get("elements", []):
            up = elem.get("value")
            if not isinstance(up, dict) or not isinstance(up.get("properties"), list):
                continue
            upgrades += 1
            bp = get_property(up, "bonuses")
            n = len(bp.get("elements", [])) if bp is not None else 0
            bonuses += n
            if n > 1:
                multi_bonus_levels += 1
            if n == 0:
                no_bonus_levels += 1

    return {
        "costumes": len(costumes),
        "upgrade_levels": upgrades,
        "bonus_records": bonuses,
        "multi_bonus_levels": multi_bonus_levels,
        "no_bonus_levels": no_bonus_levels,
    }


def build_costume_json(
    full: dict[str, Any], source_data: bytes, source_path: Path
) -> dict[str, Any]:
    entities = build_indexes(full)
    costumes = collect_costumes(entities)

    out_costumes = []
    for path, entity in costumes:
        out_costumes.append(
            {
                "path": path,
                "class": COSTUME_CLASS,
                "name": entity.get("name", path.rsplit("/", 1)[-1]),
                "costumeId": simple_field(entity, "costumeId"),
                "fields": simplify_fields(entity),
            }
        )

    stats = count_costume_stats(costumes)
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "source": {
            "file": source_path.name,
            "sha256": sha256_bytes(source_data),
            "size": len(source_data),
        },
        "costume_root_hint": COSTUME_ROOT,
        "stats": stats,
        "costumes": out_costumes,
        "catalogs": build_catalogs(full, costumes),
        "editing_notes": {
            "scope": (
                "Every direct MinionCostume property is present. Nested Clara entities are expanded recursively, "
                "including bodyParts and costumeUpgradeArray."
            ),
            "abilities": (
                "Costume abilities are under costumes[].fields.costumeUpgradeArray[].fields.bonuses. "
                "Each BonusForCostumes contains SkillType, Amount, GameItemType, and condition/location fields."
            ),
            "multiple_abilities": (
                "Some upgrade levels contain multiple bonuses. Keep the bonuses list length unchanged; edit each entry in place."
            ),
            "id_name_values": (
                "Clara enum/id-name fields are represented as {id,name}. Edit both consistently. "
                "Use catalogs for schema aliases such as CostumeSkillType and gameItemType."
            ),
            "booleans": (
                "This Clara schema stores boolean-like values as signed i8 numerics. Keep the decoded 0/1 representation; "
                "JSON true/false is intentionally not substituted."
            ),
            "structure": (
                "Do not add/remove/reorder costumes, add/remove nested array elements, or change nested entity classes. "
                "This focused editor is for property-value changes only."
            ),
            "golden_upgrades": (
                "GoldenUpgradeLayer and MultiplayerUpgrades are references stored on MinionCostume. "
                "This focused editor edits the references themselves but does not expand the referenced top-level entities."
            ),
        },
    }


def validate_header(doc: dict[str, Any], source_data: bytes) -> None:
    if not isinstance(doc, dict):
        raise CostumeEditError("costume JSON root must be an object")
    if doc.get("format") != FORMAT or doc.get("format_version") != FORMAT_VERSION:
        raise CostumeEditError("unsupported costume JSON format/version")
    source = doc.get("source")
    if not isinstance(source, dict) or source.get("sha256") != sha256_bytes(source_data):
        raise CostumeEditError(
            "JSON was decoded from a different designlib.blibclara. "
            "Re-decode this exact input before editing/encoding."
        )
    if not isinstance(doc.get("costumes"), list):
        raise CostumeEditError("costumes must be a list")


def apply_costume_json(
    full: dict[str, Any], source_data: bytes, doc: dict[str, Any]
) -> tuple[int, int]:
    validate_header(doc, source_data)
    entities = build_indexes(full)
    current = collect_costumes(entities)
    current_paths = [path for path, _ in current]
    params = schema_parameter_map(full)

    rows = doc["costumes"]
    if len(rows) != len(current_paths):
        raise CostumeEditError(
            f"costume count must remain {len(current_paths)}; JSON contains {len(rows)}"
        )

    seen: set[str] = set()
    changed = 0
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CostumeEditError(f"costumes[{i}] must be an object")
        path = row.get("path")
        if not isinstance(path, str):
            raise CostumeEditError(f"costumes[{i}].path must be a string")
        if path in seen:
            raise CostumeEditError(f"duplicate costume path in JSON: {path}")
        seen.add(path)

        entity = entities.get(path)
        if entity is None or entity.get("class") != COSTUME_CLASS:
            raise CostumeEditError(f"costumes[{i}].path is not an existing MinionCostume: {path}")
        if row.get("class") != COSTUME_CLASS:
            raise CostumeEditError(f"{path}: class must remain {COSTUME_CLASS!r}")
        if row.get("name") != entity.get("name"):
            raise CostumeEditError(
                f"{path}: top-level entity name is structural and must remain {entity.get('name')!r}"
            )

        before = simplify_fields(entity)
        desired = row.get("fields")
        apply_fields(entity, desired, f"costumes[{i}].fields", params)
        after = simplify_fields(entity)
        if before != after:
            changed += 1

        # costumeId is a convenience mirror.  Require it to agree with fields
        # so stale metadata cannot obscure what will actually be encoded.
        if row.get("costumeId") != after.get("costumeId"):
            raise CostumeEditError(
                f"{path}: top-level costumeId mirror must match fields.costumeId"
            )

    # With equal counts, unique paths, and every path validated as an existing
    # MinionCostume above, the path set is necessarily unchanged.
    return len(current_paths), changed


def verify_rebuilt_structure(
    rebuilt: bytes, output_name: str, expected_paths: list[str]
) -> dict[str, int]:
    check = clara.decode_file(rebuilt, output_name)
    entities = build_indexes(check)
    costumes = collect_costumes(entities)
    actual_paths = [path for path, _ in costumes]
    if actual_paths != expected_paths:
        raise CostumeEditError("post-encode verification failed: MinionCostume path set changed")
    return count_costume_stats(costumes)


def decode_designlib(path: Path) -> tuple[bytes, dict[str, Any]]:
    """Read and decode one designlib, preserving one source of truth for callers."""
    data = path.read_bytes()
    return data, clara.decode_file(data, path.name)


def reject_json_constant(token: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant {token!r} is not supported")


def cmd_decode(args: argparse.Namespace) -> None:
    data, full = decode_designlib(args.input)
    doc = build_costume_json(full, data, args.input)
    args.output.write_text(json.dumps(doc, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
    s = doc["stats"]
    print(
        f"Decoded {s['costumes']} costumes / {s['upgrade_levels']} upgrade levels / "
        f"{s['bonus_records']} bonus records -> {args.output}\n"
        f"Multi-bonus levels: {s['multi_bonus_levels']}\n"
        f"Dependency: blibclara_editor.py"
    )


def cmd_encode(args: argparse.Namespace) -> None:
    original, full = decode_designlib(args.input)
    entities = build_indexes(full)
    expected_paths = [path for path, _ in collect_costumes(entities)]

    try:
        doc = json.loads(args.json.read_text(encoding="utf-8"), parse_constant=reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CostumeEditError(f"invalid JSON: {exc}") from exc

    total, changed = apply_costume_json(full, original, doc)
    rebuilt = clara.encode_manifest(original, full)
    stats = verify_rebuilt_structure(rebuilt, args.output.name, expected_paths)
    args.output.write_bytes(rebuilt)

    print(
        f"Encoded costume edits -> {args.output}\n"
        f"Changed MinionCostume entities: {changed}/{total}\n"
        f"Verified upgrade levels: {stats['upgrade_levels']}\n"
        f"Verified bonus records: {stats['bonus_records']}\n"
        f"Output bytes: {len(rebuilt)}\n"
        f"Output SHA-256: {sha256_bytes(rebuilt)}"
    )


def cmd_check(args: argparse.Namespace) -> None:
    data, full = decode_designlib(args.input)
    entities = build_indexes(full)
    costumes = collect_costumes(entities)
    stats = count_costume_stats(costumes)

    print(f"Input: {args.input}")
    print(f"SHA-256: {sha256_bytes(data)}")
    print(f"MinionCostume entities: {stats['costumes']}")
    print(f"Upgrade levels: {stats['upgrade_levels']}")
    print(f"BonusForCostumes records: {stats['bonus_records']}")
    print(f"Multi-bonus upgrade levels: {stats['multi_bonus_levels']}")
    print(f"Zero-bonus upgrade levels: {stats['no_bonus_levels']}")
    print("\nCostumes:")
    for path, entity in costumes:
        cid = simple_field(entity, "costumeId")
        ups = get_property(entity, "costumeUpgradeArray")
        nup = len(ups.get("elements", [])) if ups else 0
        print(f"  {entity.get('name',''):<42} id={cid!r:<34} upgrades={nup}  {path}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Decode all MinionCostume properties from designlib.blibclara to a focused editable JSON, "
            "or apply that JSON back using blibclara_editor.py."
        )
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("decode", help="designlib -> editable MinionCostume JSON")
    d.add_argument("input", type=Path, help="input designlib.blibclara")
    d.add_argument("output", type=Path, help="output editable costume JSON")
    d.set_defaults(func=cmd_decode)

    e = sub.add_parser("encode", help="designlib + edited costume JSON -> rebuilt designlib")
    e.add_argument("input", type=Path, help="the exact designlib.blibclara used for decode")
    e.add_argument("json", type=Path, help="edited costume JSON")
    e.add_argument("output", type=Path, help="output rebuilt designlib.blibclara")
    e.set_defaults(func=cmd_encode)

    c = sub.add_parser("check", help="summarize MinionCostume records in designlib")
    c.add_argument("input", type=Path, help="input designlib.blibclara")
    c.set_defaults(func=cmd_check)

    return p


def main() -> int:
    require_editor_api()
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except (CostumeEditError, clara.CodecError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
