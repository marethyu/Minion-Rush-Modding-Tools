#!/usr/bin/env python3
"""Remove all costume IAP price references from Minion_Costumes_update1 JSON.

INPUT FORMAT
============
The input must be the single-library JSON produced by the current
``blibclara_library_editor.py`` for the ``Minion_Costumes_update1`` library::

    python blibclara_library_editor.py decode \
        designlib.blibclara \
        Minion_Costumes_update1 \
        Minion_Costumes_update1.json

The script accepts only the current per-library editable format. Older whole-file
BLIBCLARA JSON formats are intentionally unsupported.

WHAT IT CHANGES
===============
For every ``MinionCostume``/``@MinionCostume`` entity in the selected library,
the script examines every nested ``MinionCostumeUpgrade`` in
``costumeUpgradeArray``. Any non-empty string value in that upgrade's
``iap_price`` property is replaced with ``""``.

The property itself is retained. No unrelated property, entity, or library
metadata is modified.

USAGE
=====
Write a new JSON file::

    python strip_costume_iap.py \
        Minion_Costumes_update1.json \
        Minion_Costumes_update1_no_iap.json

Modify the input file in place (with a backup)::

    python strip_costume_iap.py Minion_Costumes_update1.json --in-place

Preview without writing::

    python strip_costume_iap.py Minion_Costumes_update1.json --dry-run

Show each cleared reference::

    python strip_costume_iap.py \
        Minion_Costumes_update1.json \
        Minion_Costumes_update1_no_iap.json \
        --verbose

Rebuild ``designlib.blibclara`` with the current library editor::

    python blibclara_library_editor.py encode \
        designlib.blibclara \
        Minion_Costumes_update1_no_iap.json \
        designlib_modified.blibclara
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


EXPECTED_FORMAT = "generic-clara-blib-library-editable"
EXPECTED_FORMAT_VERSION = 1
EXPECTED_LIBRARY = "Minion_Costumes_update1"
COSTUME_CLASSES = {"MinionCostume", "@MinionCostume"}
UPGRADE_CLASSES = {"MinionCostumeUpgrade", "@MinionCostumeUpgrade"}
UPGRADE_ARRAY_PROPERTY = "costumeUpgradeArray"
IAP_PROPERTY = "iap_price"


class EditError(RuntimeError):
    pass


def find_unique_property(
    entity: dict[str, Any], name: str, *, context: str
) -> dict[str, Any] | None:
    properties = entity.get("properties")
    if not isinstance(properties, list):
        return None
    matches = [
        prop
        for prop in properties
        if isinstance(prop, dict) and prop.get("name") == name
    ]
    if len(matches) > 1:
        raise EditError(f"{context}: duplicate property {name!r}")
    return matches[0] if matches else None


def iter_entities_in_folder(folder: dict[str, Any]) -> Iterator[dict[str, Any]]:
    records = folder.get("records")
    if not isinstance(records, list):
        raise EditError("library root contains a folder without a valid 'records' list")

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise EditError(f"folder record {index} is not an object")

        kind = record.get("kind")
        if kind == "entity":
            entity = record.get("entity")
            if not isinstance(entity, dict):
                raise EditError(f"folder record {index} has a malformed entity")
            yield entity
        elif kind == "folder":
            child = record.get("folder")
            if not isinstance(child, dict):
                raise EditError(f"folder record {index} has a malformed child folder")
            yield from iter_entities_in_folder(child)


def validate_document(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("format") != EXPECTED_FORMAT:
        raise EditError(
            f"expected format {EXPECTED_FORMAT!r}; got {document.get('format')!r}"
        )
    if document.get("format_version") != EXPECTED_FORMAT_VERSION:
        raise EditError(
            f"expected format_version {EXPECTED_FORMAT_VERSION}; "
            f"got {document.get('format_version')!r}"
        )

    library = document.get("library")
    if not isinstance(library, dict):
        raise EditError("JSON does not contain a valid top-level 'library' object")
    if library.get("name") != EXPECTED_LIBRARY:
        raise EditError(
            f"expected library {EXPECTED_LIBRARY!r}; got {library.get('name')!r}"
        )

    root = library.get("root_folder")
    if not isinstance(root, dict):
        raise EditError("library is missing a valid 'root_folder'")
    return root


def strip_costume_iap(
    document: dict[str, Any], *, verbose: bool = False
) -> dict[str, Any]:
    root = validate_document(document)
    stats = {
        "costumes_seen": 0,
        "upgrades_seen": 0,
        "iap_properties_missing": 0,
        "iap_already_empty": 0,
        "changed": 0,
    }
    changed: list[tuple[str, int, str]] = []

    for costume in iter_entities_in_folder(root):
        if costume.get("class") not in COSTUME_CLASSES:
            continue

        stats["costumes_seen"] += 1
        costume_name = str(costume.get("name", "<unnamed>"))
        upgrades = find_unique_property(
            costume, UPGRADE_ARRAY_PROPERTY, context=costume_name
        )
        if upgrades is None:
            continue
        if upgrades.get("type_code") != 32:
            raise EditError(
                f"{costume_name}: {UPGRADE_ARRAY_PROPERTY} is not nested-entity type 0x20"
            )

        elements = upgrades.get("elements")
        if not isinstance(elements, list):
            raise EditError(
                f"{costume_name}: {UPGRADE_ARRAY_PROPERTY}.elements must be a list"
            )

        for upgrade_index, element in enumerate(elements):
            context = f"{costume_name}.{UPGRADE_ARRAY_PROPERTY}[{upgrade_index}]"
            if not isinstance(element, dict):
                raise EditError(f"{context}: element must be an object")

            upgrade = element.get("value")
            if upgrade is None:
                continue
            if not isinstance(upgrade, dict):
                raise EditError(f"{context}: value must be a nested entity or null")
            if upgrade.get("class") not in UPGRADE_CLASSES:
                raise EditError(
                    f"{context}: unexpected nested class {upgrade.get('class')!r}"
                )

            stats["upgrades_seen"] += 1
            iap = find_unique_property(upgrade, IAP_PROPERTY, context=context)
            if iap is None:
                stats["iap_properties_missing"] += 1
                continue
            if iap.get("type_code") not in (8, 16):
                raise EditError(
                    f"{context}.{IAP_PROPERTY}: unexpected Clara type "
                    f"0x{iap.get('type_code', -1):X}"
                )
            if iap.get("named_elements"):
                raise EditError(
                    f"{context}.{IAP_PROPERTY}: named elements are unsupported"
                )

            iap_elements = iap.get("elements")
            if not isinstance(iap_elements, list) or len(iap_elements) != 1:
                raise EditError(
                    f"{context}.{IAP_PROPERTY}: expected exactly one scalar element"
                )
            iap_element = iap_elements[0]
            if not isinstance(iap_element, dict) or "value" not in iap_element:
                raise EditError(
                    f"{context}.{IAP_PROPERTY}: malformed scalar element"
                )

            current = iap_element["value"]
            if not isinstance(current, str):
                raise EditError(
                    f"{context}.{IAP_PROPERTY}: expected string, "
                    f"got {type(current).__name__}"
                )
            if not current:
                stats["iap_already_empty"] += 1
                continue

            iap_element["value"] = ""
            stats["changed"] += 1
            changed.append((costume_name, upgrade_index, current))
            if verbose:
                print(
                    f"{costume_name} upgrade {upgrade_index}: "
                    f"{current!r} -> ''"
                )

    return {"stats": stats, "changed": changed}


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except OSError as exc:
        raise EditError(f"could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EditError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise EditError("top-level JSON value must be an object")
    return value


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".tmp-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(
                document,
                file,
                ensure_ascii=True,
                indent=2,
                allow_nan=False,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Clear every costume iap_price in a "
            "Minion_Costumes_update1 per-library JSON file."
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Minion_Costumes_update1.json from blibclara_library_editor.py",
    )
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="output JSON; required unless --in-place or --dry-run is used",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="overwrite INPUT after creating a unique .bak backup",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report changes without writing a file",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every IAP reference that is cleared",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.in_place and args.output is not None:
        print("error: do not specify OUTPUT together with --in-place", file=sys.stderr)
        return 2
    if args.dry_run and args.in_place:
        print("error: --dry-run and --in-place cannot be combined", file=sys.stderr)
        return 2
    if not args.in_place and not args.dry_run and args.output is None:
        print("error: specify OUTPUT, --in-place, or --dry-run", file=sys.stderr)
        return 2

    try:
        if args.output is not None and not args.in_place:
            if args.input.resolve() == args.output.resolve():
                raise EditError(
                    "OUTPUT is the same as INPUT; use --in-place to get a backup"
                )

        document = load_json(args.input)
        result = strip_costume_iap(document, verbose=args.verbose)
        stats = result["stats"]
        print(
            "Costume IAP prices: "
            f"{stats['costumes_seen']} costumes; "
            f"{stats['upgrades_seen']} upgrades; "
            f"{stats['changed']} cleared; "
            f"{stats['iap_already_empty']} already empty; "
            f"{stats['iap_properties_missing']} upgrades without iap_price."
        )

        if args.dry_run:
            print("Dry run: no file written.")
            return 0

        if args.in_place:
            backup = args.input.with_suffix(args.input.suffix + ".bak")
            counter = 1
            while backup.exists():
                backup = args.input.with_suffix(
                    args.input.suffix + f".bak.{counter}"
                )
                counter += 1
            shutil.copy2(args.input, backup)
            write_json(args.input, document)
            print(f"Wrote {args.input}")
            print(f"Backup: {backup}")
        else:
            write_json(args.output, document)
            print(f"Wrote {args.output}")

        return 0
    except (EditError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
