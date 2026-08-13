#!/usr/bin/env python3
"""
strip_costume_iap.py

Strip IAP price references from the FIRST upgrade entry of every Minion costume
in a clean designlib.json produced by blibclara_editor.py.

What it changes
---------------
For every Clara entity whose class is "MinionCostume" or "@MinionCostume":

    costumeUpgradeArray
        -> elements[0]
        -> nested MinionCostumeUpgrade
        -> property "iap_price"
        -> elements[0]["value"] = ""

Only the first costumeUpgradeArray entry is modified. Later upgrade entries are
left unchanged.

The property itself is NOT deleted. Its existing Clara string value is replaced
with an empty string, matching the representation already used by costumes that
do not have an IAP price.

Usage
-----
Write a new JSON file:

    python strip_costume_iap.py designlib.json designlib_no_iap.json

Modify the input file in place:

    python strip_costume_iap.py designlib.json --in-place

Preview what would change without writing anything:

    python strip_costume_iap.py designlib.json --dry-run

Show every affected costume:

    python strip_costume_iap.py designlib.json output.json --verbose

After editing, rebuild the .blibclara with the Clara editor and its metadata
sidecar, for example:

    python blibclara_editor.py encode \
        designlib.blibclara \
        designlib_no_iap.json \
        designlib_modified.blibclara

Important
---------
When a separate output JSON is written, the script automatically copies the
input JSON's ``.meta.json`` sidecar to the matching output-sidecar name when it
is present. This lets blibclara_editor.py use its normal default metadata
lookup. If no sidecar is present, the script warns and you must pass --meta
explicitly during encoding.

The script deliberately targets only the MinionCostume/@MinionCostume classes and property
"costumeUpgradeArray". It will not modify unrelated entities that happen to
contain an "iap_price" property.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator


COSTUME_CLASSES = {"MinionCostume", "@MinionCostume"}
UPGRADE_CLASSES = {"MinionCostumeUpgrade", "@MinionCostumeUpgrade"}
EXPECTED_FORMAT = "generic-clara-blib-editable"
UPGRADE_ARRAY_PROPERTY = "costumeUpgradeArray"
IAP_PROPERTY = "iap_price"


class EditError(RuntimeError):
    pass


def iter_entities_in_folder(folder: dict[str, Any]) -> Iterator[dict[str, Any]]:
    records = folder.get("records")
    if not isinstance(records, list):
        raise EditError("Folder is missing a valid 'records' list")

    for record in records:
        if not isinstance(record, dict):
            continue

        kind = record.get("kind")
        if kind == "entity":
            entity = record.get("entity")
            if isinstance(entity, dict):
                yield entity
        elif kind == "folder":
            child = record.get("folder")
            if isinstance(child, dict):
                yield from iter_entities_in_folder(child)


def iter_all_entities(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    libraries = document.get("libraries")
    if not isinstance(libraries, list):
        raise EditError("JSON does not contain a valid top-level 'libraries' list")

    for library in libraries:
        if not isinstance(library, dict):
            continue
        root = library.get("root_folder")
        if not isinstance(root, dict):
            raise EditError("A library is missing its 'root_folder'")
        yield from iter_entities_in_folder(root)


def find_unique_property(entity: dict[str, Any], name: str, *, context: str) -> dict[str, Any] | None:
    props = entity.get("properties")
    if not isinstance(props, list):
        return None
    matches = [p for p in props if isinstance(p, dict) and p.get("name") == name]
    if len(matches) > 1:
        raise EditError(f"{context}: duplicate property {name!r}")
    return matches[0] if matches else None


def validate_document(document: dict[str, Any]) -> None:
    fmt = document.get("format")
    if fmt != EXPECTED_FORMAT:
        raise EditError(
            f"Expected a clean Clara editable JSON with format {EXPECTED_FORMAT!r}; got {fmt!r}"
        )
    if not isinstance(document.get("libraries"), list):
        raise EditError("JSON does not contain a valid top-level 'libraries' list")


def strip_costume_iap(document: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    stats = {
        "costumes_seen": 0,
        "costumes_with_no_upgrades": 0,
        "first_upgrade_missing": 0,
        "iap_property_missing": 0,
        "iap_already_empty": 0,
        "changed": 0,
    }

    changed_names: list[str] = []

    for costume in iter_all_entities(document):
        if costume.get("class") not in COSTUME_CLASSES:
            continue

        stats["costumes_seen"] += 1
        costume_name = str(costume.get("name", "<unnamed>"))

        upgrades_prop = find_unique_property(costume, UPGRADE_ARRAY_PROPERTY, context=costume_name)
        if upgrades_prop is None:
            stats["costumes_with_no_upgrades"] += 1
            continue
        if upgrades_prop.get("type_code") != 32:
            raise EditError(
                f"{costume_name}: {UPGRADE_ARRAY_PROPERTY} is not nested-entity type 0x20"
            )

        elements = upgrades_prop.get("elements")
        if not isinstance(elements, list) or not elements:
            stats["costumes_with_no_upgrades"] += 1
            continue

        first = elements[0]
        if not isinstance(first, dict):
            stats["first_upgrade_missing"] += 1
            continue

        upgrade = first.get("value")
        if not isinstance(upgrade, dict):
            stats["first_upgrade_missing"] += 1
            continue

        # Fail closed if this does not look like the expected nested entity.
        upgrade_class = upgrade.get("class")
        if upgrade_class not in UPGRADE_CLASSES:
            raise EditError(
                f"{costume_name}: first {UPGRADE_ARRAY_PROPERTY} entry has "
                f"unexpected class {upgrade_class!r}"
            )

        iap_prop = find_unique_property(upgrade, IAP_PROPERTY, context=f"{costume_name}.first_upgrade")
        if iap_prop is None:
            stats["iap_property_missing"] += 1
            continue
        if iap_prop.get("type_code") not in (8, 16):
            raise EditError(
                f"{costume_name}: first-upgrade {IAP_PROPERTY} has unexpected "
                f"type 0x{iap_prop.get('type_code', -1):X}"
            )
        if iap_prop.get("named_elements"):
            raise EditError(f"{costume_name}: first-upgrade {IAP_PROPERTY} unexpectedly has named elements")

        iap_elements = iap_prop.get("elements")
        if not isinstance(iap_elements, list) or len(iap_elements) != 1:
            raise EditError(
                f"{costume_name}: first-upgrade {IAP_PROPERTY} must be exactly one scalar element"
            )

        iap_element = iap_elements[0]
        if not isinstance(iap_element, dict) or "value" not in iap_element:
            stats["iap_property_missing"] += 1
            continue

        current = iap_element["value"]
        if not isinstance(current, str):
            raise EditError(
                f"{costume_name}: first-upgrade {IAP_PROPERTY} is not a string: "
                f"{type(current).__name__}"
            )

        if current == "":
            stats["iap_already_empty"] += 1
            continue

        iap_element["value"] = ""
        stats["changed"] += 1
        changed_names.append(costume_name)

        if verbose:
            print(f"{costume_name}: {current!r} -> ''")

    return {"stats": stats, "changed_names": changed_names}


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
    except OSError as exc:
        raise EditError(f"Could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EditError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(obj, dict):
        raise EditError("Top-level JSON value must be an object")
    return obj


def default_meta_path(json_path: Path) -> Path:
    return Path(str(json_path) + ".meta.json")


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(document, f, ensure_ascii=True, indent=2, allow_nan=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def copy_metadata_sidecar(input_json: Path, output_json: Path) -> Path | None:
    src = default_meta_path(input_json)
    if not src.is_file():
        return None
    dst = default_meta_path(output_json)
    if src.resolve() != dst.resolve():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return dst


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Clear the iap_price of every MinionCostume's first "
            "costumeUpgradeArray entry in designlib.json."
        )
    )
    p.add_argument("input", type=Path, help="Input designlib.json")
    p.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="Output JSON. Required unless --in-place or --dry-run is used.",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the input JSON. A .bak backup is created first.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without writing an output file.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print each costume whose first-upgrade IAP price is cleared.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.in_place and args.output is not None:
        print("error: do not specify OUTPUT together with --in-place", file=sys.stderr)
        return 2
    if args.dry_run and args.in_place:
        print("error: --dry-run and --in-place cannot be combined", file=sys.stderr)
        return 2
    if not args.in_place and not args.dry_run and args.output is None:
        print(
            "error: specify OUTPUT, --in-place, or --dry-run",
            file=sys.stderr,
        )
        return 2

    try:
        if args.output is not None and not args.in_place:
            if args.input.resolve() == args.output.resolve():
                raise EditError("OUTPUT is the same as INPUT; use --in-place to get a backup")
        document = load_json(args.input)
        validate_document(document)
        result = strip_costume_iap(document, verbose=args.verbose)
        stats = result["stats"]

        print(
            "Costumes: "
            f"{stats['costumes_seen']} seen; "
            f"{stats['changed']} changed; "
            f"{stats['iap_already_empty']} already empty; "
            f"{stats['costumes_with_no_upgrades']} without upgrades; "
            f"{stats['iap_property_missing']} without first-upgrade iap_price."
        )

        if args.dry_run:
            print("Dry run: no file written.")
            return 0

        if args.in_place:
            backup = args.input.with_suffix(args.input.suffix + ".bak")
            counter = 1
            while backup.exists():
                backup = args.input.with_suffix(args.input.suffix + f".bak.{counter}")
                counter += 1
            shutil.copy2(args.input, backup)
            write_json(args.input, document)
            print(f"Wrote {args.input}")
            print(f"Backup: {backup}")
        else:
            write_json(args.output, document)
            meta_out = copy_metadata_sidecar(args.input, args.output)
            print(f"Wrote {args.output}")
            if meta_out is not None:
                print(f"Copied metadata sidecar to {meta_out}")
            else:
                print(
                    f"Warning: no metadata sidecar found at {default_meta_path(args.input)}; "
                    "you will need to pass --meta explicitly when encoding.",
                    file=sys.stderr,
                )

        return 0

    except (EditError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
