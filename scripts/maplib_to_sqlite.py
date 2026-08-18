#!/usr/bin/env python3
"""Convert Minion Rush ``maplib.blibclara`` into a queryable SQLite database.

The exporter is intentionally loss-preserving at the decoder level:

* the complete original ``maplib.blibclara`` is stored as a BLOB;
* the complete JSON manifest returned by ``blibclara_library_editor.decode_file()`` is
  stored in the database;
* every top-level Clara entity, property, and property element is also split
  into relational tables for convenient SQL queries;
* all ``MapArea`` and ``MapMission`` entities are indexed in dedicated Jelly Lab
  ``areas`` and ``levels`` tables; every known direct MapArea/MapMission property
  is exposed under its exact Clara property name as a dedicated SQL column
  (complex Clara values use deterministic JSON text);
* absolute Clara path references found inside property values are indexed in an
  ``entity_references`` table.

The property columns are discovered from the decoded Clara class schema rather
than from a hard-coded field list, so fields such as ``mapAreaBackgroundData``
and future MapArea/MapMission properties are exposed automatically. Existing
snake_case convenience columns (fruit thresholds, mission IDs, parsed
ForceLocation, reward names, and so on) are first-class query columns. Any
value is additionally preserved in
``fields_json``, ``entity_json``, ``property_json``, ``value_json``, and the
complete decoded manifest.

Requirements
------------
Keep this script in the same directory as the refactored Clara codec stack::

    clara_common.py
    bclara_editor.py
    blibclara_library_editor.py

``maplib_to_sqlite.py`` imports only ``blibclara_library_editor`` directly; the other
two modules are dependencies of the refactored editor. No third-party Python
packages are required; ``sqlite3`` is part of Python.
Python 3.9+ is supported.

Usage
-----

    python maplib_to_sqlite.py maplib.blibclara

By default this writes ``maplib.sqlite`` beside the input file.  Choose another
output path with::

    python maplib_to_sqlite.py maplib.blibclara -o jelly_lab.sqlite

Existing output files are refused unless ``--force`` is supplied::

    python maplib_to_sqlite.py maplib.blibclara -o jelly_lab.sqlite --force

Useful example queries
----------------------

    SELECT * FROM areas ORDER BY area_number;

    SELECT area_number, level_in_area, mission_id,
           target_value1, target_value2, target_value3, force_location
    FROM levels
    ORDER BY area_number, level_in_area;

    SELECT area_number, mapAreaBackgroundData, mapAreaDefinition,
           FruitData, RequiredFruits, CostumePriceRange
    FROM areas
    ORDER BY area_number;

    SELECT property_name, column_name, representation
    FROM jelly_property_columns
    ORDER BY table_name, property_name;

    SELECT e.path, p.property_name, pe.element_index, pe.value_json
    FROM entities AS e
    JOIN properties AS p ON p.entity_id = e.entity_id
    JOIN property_elements AS pe ON pe.property_id = p.property_id
    WHERE e.path = '/MapMissions/MapMissionDef/Mission001_0001';

    SELECT source_path, property_name, target_path, target_exists
    FROM entity_references
    WHERE source_path LIKE '/MapMissions/%';

    -- Every maplib entity recursively reachable from an area or mission:
    SELECT path, class_name, name
    FROM jelly_related_entities
    ORDER BY path;
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import blibclara_library_editor

MAP_AREAS_ORDER_PATH = "/MapLevelDef/MapSystem/MapAreasOrder"
MAP_MISSION_ROOT = "/MapMissions/MapMissionDef/"
MAP_AREA_RE = re.compile(r".*/MapArea(\d+)$")
MAP_MISSION_RE = re.compile(r"Mission(\d+)_(\d+)$")
SCHEMA_VERSION = 3
DECODER_MANIFEST_FORMAT = "blibclara_library_editor.decode_file"
DECODER_MANIFEST_VERSION = 1


class ExportError(RuntimeError):
    pass


def _json_safe(value: Any) -> Any:
    """Return a deterministic JSON-compatible representation.

    ``blibclara_library_editor`` normally produces JSON-compatible values already.  The
    extra handling here makes the SQLite exporter robust to future decoder
    values containing bytes or non-finite floats without silently losing them.
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return {"$float": "nan"}
        return {"$float": "+inf" if value > 0 else "-inf"}
    if isinstance(value, bytes):
        return {"$bytes_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [_json_safe(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    raise ExportError(f"unsupported decoded value type: {type(value).__name__}")


def _json_text(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _scalar_columns(value: Any) -> tuple[str, int | None, float | None, str | None]:
    if value is None:
        return "null", None, None, None
    if isinstance(value, bool):
        return "bool", int(value), None, None
    if isinstance(value, int):
        # SQLite INTEGER is signed 64-bit. Clara numeric values used by this
        # map fit comfortably, but preserve unexpectedly huge Python ints as text.
        if -(1 << 63) <= value <= (1 << 63) - 1:
            return "int", value, None, None
        return "bigint", None, None, str(value)
    if isinstance(value, float) and math.isfinite(value):
        return "float", None, value, None
    if isinstance(value, str):
        return "text", None, None, value
    return "json", None, None, None


def _join_entity_path(parent: str, name: str) -> str:
    parent = parent.rstrip("/")
    return (parent + "/" + name) if parent else ("/" + name)


def _iter_entities(full: dict[str, Any]) -> Iterator[tuple[int, str, str, dict[str, Any]]]:
    """Yield ``(library_index, path, parent_path, entity)`` in file-tree order."""
    seen: set[str] = set()

    def walk_records(
        library_index: int, folder: dict[str, Any], folder_path: str
    ) -> Iterator[tuple[int, str, str, dict[str, Any]]]:
        records = folder.get("records")
        if not isinstance(records, list):
            raise ExportError(f"folder {folder_path!r} has no records list")
        for record_index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ExportError(
                    f"folder {folder_path!r} record {record_index} is not an object"
                )
            kind = record.get("kind")
            if kind == "folder":
                child = record.get("folder")
                if not isinstance(child, dict):
                    raise ExportError(
                        f"folder {folder_path!r} record {record_index} has no child folder"
                    )
                child_name = child.get("name")
                if not isinstance(child_name, str):
                    raise ExportError(
                        f"folder {folder_path!r} record {record_index} has an invalid child name"
                    )
                child_path = _join_entity_path(folder_path, child_name)
                yield from walk_records(library_index, child, child_path)
            elif kind == "entity":
                entity = record.get("entity")
                if not isinstance(entity, dict):
                    raise ExportError(
                        f"folder {folder_path!r} record {record_index} has no entity object"
                    )
                entity_name = entity.get("name", "")
                if not isinstance(entity_name, str):
                    raise ExportError(
                        f"folder {folder_path!r} record {record_index} has an invalid entity name"
                    )
                entity_path = _join_entity_path(folder_path, entity_name)
                if entity_path in seen:
                    raise ExportError(f"duplicate Clara entity path: {entity_path}")
                seen.add(entity_path)
                yield library_index, entity_path, folder_path, entity
            elif kind not in {"group", "movie", "multilayer"}:
                raise ExportError(
                    f"folder {folder_path!r} record {record_index} has unsupported kind {kind!r}"
                )

    libraries = full.get("libraries")
    if not isinstance(libraries, list):
        raise ExportError("decoded Clara manifest has no libraries list")
    for library_index, library in enumerate(libraries):
        if not isinstance(library, dict):
            raise ExportError(f"libraries[{library_index}] is not an object")
        root = library.get("root_folder")
        if not isinstance(root, dict):
            raise ExportError(f"libraries[{library_index}] has no root_folder object")
        root_name = root.get("name")
        if not isinstance(root_name, str):
            raise ExportError(f"libraries[{library_index}].root_folder.name is not a string")
        root_path = ("/" + root_name) if root_name else "/"
        yield from walk_records(library_index, root, root_path)


def _simplify_value(value: Any) -> Any:
    """Readable semantic view matching the catalogue extractor's behavior."""
    if isinstance(value, list):
        return [_simplify_value(x) for x in value]
    if isinstance(value, dict):
        if isinstance(value.get("class"), str) and isinstance(value.get("properties"), list):
            return {
                "class": value.get("class"),
                "name": value.get("name", ""),
                "fields": _entity_fields(value),
            }
        return {
            str(k): _simplify_value(v)
            for k, v in value.items()
            if k not in {
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


def _simplify_property(prop: dict[str, Any]) -> Any:
    elements = prop.get("elements")
    if not isinstance(elements, list):
        return []
    values = [
        _simplify_value(element.get("value"))
        for element in elements
        if isinstance(element, dict)
    ]
    if not values:
        return []
    return values[0] if len(values) == 1 else values


def _entity_fields(entity: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(entity, dict):
        return {}
    props = entity.get("properties")
    if not isinstance(props, list):
        return {}
    result: dict[str, Any] = {}
    for prop in props:
        if isinstance(prop, dict) and isinstance(prop.get("name"), str):
            result[prop["name"]] = _simplify_property(prop)
    return result


def _enum_u32(value: Any) -> int | None:
    """Return numeric value from either u32 or Clara enum representation."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 0xFFFFFFFF:
        return value
    if isinstance(value, dict):
        inner = value.get("value")
        if isinstance(inner, int) and not isinstance(inner, bool) and 0 <= inner <= 0xFFFFFFFF:
            return inner
    return None


def _plain_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _plain_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _quote_identifier(name: str) -> str:
    """Quote an SQLite identifier safely."""
    return '"' + name.replace('"', '""') + '"'


def _direct_sql_scalar(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, bool)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _infer_property_storage(values: list[Any]) -> tuple[str, str]:
    """Return ``(SQLite affinity, representation)`` for property values."""
    non_null = [value for value in values if value is not None]
    if any(not _direct_sql_scalar(value) for value in non_null):
        return "TEXT", "json"
    if not non_null:
        return "TEXT", "scalar"
    if all(isinstance(value, (bool, int)) and not isinstance(value, float) for value in non_null):
        return "INTEGER", "scalar"
    if all(
        (isinstance(value, (bool, int)) and not isinstance(value, float))
        or (isinstance(value, float) and math.isfinite(value))
        for value in non_null
    ):
        return "REAL", "scalar"
    return "TEXT", "scalar"


def _property_sql_value(value: Any, representation: str) -> Any:
    if value is None:
        return None
    if representation == "json":
        return _json_text(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        if -(1 << 63) <= value <= (1 << 63) - 1:
            return value
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else _json_text(value)
    if isinstance(value, str):
        return value
    return _json_text(value)


def _discover_property_columns(
    full: dict[str, Any],
    entity_objects: dict[str, dict[str, Any]],
    class_name: str,
    table_name: str,
    base_columns: set[str],
) -> list[dict[str, Any]]:
    """Discover every direct Clara property used by a Jelly Lab class.

    Scalar values receive ordinary INTEGER/REAL/TEXT columns. Lists, nested
    entities, enums represented as dictionaries, and other structured values
    receive deterministic JSON text in the discovered property column.
    """
    values_by_property: dict[str, list[Any]] = {}

    # Start from the decoded Clara class schema so even an optional property
    # absent from every current entity still receives a column.
    schema = full.get("schema")
    classes = schema.get("classes") if isinstance(schema, dict) else None
    if isinstance(classes, list):
        for class_def in classes:
            if not isinstance(class_def, dict) or class_def.get("name") != class_name:
                continue
            properties = class_def.get("properties")
            if isinstance(properties, list):
                for prop in properties:
                    if isinstance(prop, dict) and isinstance(prop.get("name"), str):
                        values_by_property.setdefault(prop["name"], [])
            break

    for path, entity in entity_objects.items():
        if entity.get("class") != class_name:
            continue
        if class_name == "MapMission" and not path.startswith(MAP_MISSION_ROOT):
            continue
        fields = _entity_fields(entity)
        for property_name, value in fields.items():
            values_by_property.setdefault(property_name, []).append(value)

    used_casefold = {name.casefold() for name in base_columns}
    specs: list[dict[str, Any]] = []
    for property_name in sorted(values_by_property):
        values = values_by_property[property_name]
        affinity, representation = _infer_property_storage(values)
        candidate_base = property_name
        candidate = candidate_base
        if candidate.casefold() in used_casefold:
            candidate_base = property_name + "__clara"
            candidate = candidate_base
        suffix = 2
        while candidate.casefold() in used_casefold:
            candidate = "%s_%d" % (candidate_base, suffix)
            suffix += 1
        column_name = candidate
        used_casefold.add(column_name.casefold())

        specs.append(
            {
                "table_name": table_name,
                "class_name": class_name,
                "property_name": property_name,
                "column_name": column_name,
                "affinity": affinity,
                "representation": representation,
                "observed_count": len(values),
                "non_null_count": sum(value is not None for value in values),
            }
        )
    return specs


def _install_property_columns(
    db: sqlite3.Connection,
    table_name: str,
    specs: list[dict[str, Any]],
) -> None:
    for spec in specs:
        db.execute(
            "ALTER TABLE %s ADD COLUMN %s %s"
            % (
                _quote_identifier(table_name),
                _quote_identifier(spec["column_name"]),
                spec["affinity"],
            )
        )
        db.execute(
            """INSERT INTO jelly_property_columns
               (table_name, class_name, property_name, column_name,
                storage_affinity, representation, observed_count, non_null_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                spec["table_name"],
                spec["class_name"],
                spec["property_name"],
                spec["column_name"],
                spec["affinity"],
                spec["representation"],
                spec["observed_count"],
                spec["non_null_count"],
            ),
        )


def _table_column_names(db: sqlite3.Connection, table_name: str) -> set[str]:
    rows = db.execute("PRAGMA table_info(%s)" % _quote_identifier(table_name)).fetchall()
    return {str(row[1]) for row in rows}



def _write_property_columns(
    db: sqlite3.Connection,
    table_name: str,
    key_column: str,
    key_value: Any,
    fields: dict[str, Any],
    specs: list[dict[str, Any]],
) -> None:
    assignments: list[str] = []
    values: list[Any] = []
    for spec in specs:
        property_name = spec["property_name"]
        if property_name not in fields:
            continue
        assignments.append(_quote_identifier(spec["column_name"]) + " = ?")
        values.append(_property_sql_value(fields[property_name], spec["representation"]))
    if not assignments:
        return
    values.append(key_value)
    db.execute(
        "UPDATE %s SET %s WHERE %s = ?"
        % (
            _quote_identifier(table_name),
            ", ".join(assignments),
            _quote_identifier(key_column),
        ),
        values,
    )


def _first_path_leaf(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.rsplit("/", 1)[-1]


def _iter_path_strings(value: Any, value_path: str = "$") -> Iterator[tuple[str, str]]:
    """Yield JSON-ish paths and absolute Clara-looking string references."""
    if isinstance(value, str):
        if value.startswith("/"):
            yield value_path, value
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_path_strings(item, "%s[%d]" % (value_path, index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_path_strings(item, "%s.%s" % (value_path, key))


def _create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE database_info (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE source_file (
            source_id INTEGER PRIMARY KEY CHECK(source_id = 1),
            file_name TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_size INTEGER NOT NULL,
            data BLOB NOT NULL,
            decoded_manifest_json TEXT NOT NULL
        );

        CREATE TABLE libraries (
            library_id INTEGER PRIMARY KEY,
            library_index INTEGER NOT NULL UNIQUE,
            name TEXT,
            marker INTEGER,
            version INTEGER,
            root_name TEXT,
            library_json TEXT NOT NULL
        );

        CREATE TABLE schema_parameters (
            parameter_index INTEGER PRIMARY KEY,
            type_code INTEGER,
            type_name TEXT,
            name TEXT,
            subtype INTEGER,
            subtype_name TEXT,
            aliases_json TEXT NOT NULL
        );

        CREATE TABLE schema_classes (
            class_name TEXT PRIMARY KEY,
            generic INTEGER,
            properties_json TEXT NOT NULL
        );

        CREATE TABLE entities (
            entity_id INTEGER PRIMARY KEY,
            library_id INTEGER NOT NULL REFERENCES libraries(library_id),
            path TEXT NOT NULL UNIQUE,
            parent_path TEXT NOT NULL,
            class_name TEXT,
            name TEXT,
            source_offset INTEGER,
            source_size INTEGER,
            fields_json TEXT NOT NULL,
            entity_json TEXT NOT NULL
        );

        CREATE INDEX entities_class_idx ON entities(class_name);
        CREATE INDEX entities_parent_idx ON entities(parent_path);

        CREATE TABLE properties (
            property_id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
            property_index INTEGER NOT NULL,
            property_name TEXT,
            type_index INTEGER,
            type_code INTEGER,
            subtype INTEGER,
            named_elements INTEGER,
            property_json TEXT NOT NULL,
            UNIQUE(entity_id, property_index)
        );

        CREATE INDEX properties_name_idx ON properties(property_name);

        CREATE TABLE property_elements (
            element_id INTEGER PRIMARY KEY,
            property_id INTEGER NOT NULL REFERENCES properties(property_id) ON DELETE CASCADE,
            element_index INTEGER NOT NULL,
            element_name TEXT,
            value_kind TEXT NOT NULL,
            integer_value INTEGER,
            real_value REAL,
            text_value TEXT,
            value_json TEXT NOT NULL,
            raw_base64 TEXT,
            element_json TEXT NOT NULL,
            UNIQUE(property_id, element_index)
        );

        CREATE INDEX property_elements_text_idx ON property_elements(text_value);
        CREATE INDEX property_elements_int_idx ON property_elements(integer_value);

        CREATE TABLE entity_references (
            reference_id INTEGER PRIMARY KEY,
            source_entity_id INTEGER NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
            source_path TEXT NOT NULL,
            property_name TEXT,
            element_index INTEGER,
            value_path TEXT NOT NULL,
            target_path TEXT NOT NULL,
            target_entity_id INTEGER REFERENCES entities(entity_id),
            target_exists INTEGER NOT NULL
        );

        CREATE INDEX entity_references_source_idx ON entity_references(source_entity_id);
        CREATE INDEX entity_references_target_idx ON entity_references(target_path);

        CREATE TABLE jelly_property_columns (
            table_name TEXT NOT NULL,
            class_name TEXT NOT NULL,
            property_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            storage_affinity TEXT NOT NULL,
            representation TEXT NOT NULL CHECK(representation IN ('scalar', 'json')),
            observed_count INTEGER NOT NULL,
            non_null_count INTEGER NOT NULL,
            PRIMARY KEY(table_name, property_name),
            UNIQUE(table_name, column_name)
        );

        CREATE TABLE areas (
            area_number INTEGER PRIMARY KEY,
            order_index INTEGER,
            entity_id INTEGER NOT NULL UNIQUE REFERENCES entities(entity_id),
            entity_path TEXT NOT NULL UNIQUE,
            entity_name TEXT,
            level_count INTEGER NOT NULL DEFAULT 0,
            required_fruits_bonus1 INTEGER,
            required_fruits_bonus2 INTEGER,
            bonus_type1_path TEXT,
            bonus_type1_name TEXT,
            bonus_type2_path TEXT,
            bonus_type2_name TEXT,
            fields_json TEXT NOT NULL
        );

        CREATE TABLE levels (
            level_id INTEGER PRIMARY KEY,
            area_number INTEGER NOT NULL,
            level_in_area INTEGER NOT NULL,
            mission_suffix INTEGER NOT NULL,
            mission_id INTEGER,
            entity_id INTEGER NOT NULL UNIQUE REFERENCES entities(entity_id),
            entity_path TEXT NOT NULL UNIQUE,
            entity_name TEXT,
            target_value1 INTEGER,
            target_value2 INTEGER,
            target_value3 INTEGER,
            force_location INTEGER,
            fields_json TEXT NOT NULL,
            UNIQUE(area_number, level_in_area),
            UNIQUE(area_number, mission_suffix)
        );

        CREATE INDEX levels_area_idx ON levels(area_number, level_in_area);
        CREATE INDEX levels_mission_id_idx ON levels(mission_id);

        CREATE VIEW jelly_related_entities AS
        WITH RECURSIVE related(entity_id) AS (
            SELECT entity_id FROM areas
            UNION
            SELECT entity_id FROM levels
            UNION
            SELECT r.target_entity_id
            FROM entity_references AS r
            JOIN related AS q ON q.entity_id = r.source_entity_id
            WHERE r.target_entity_id IS NOT NULL
        )
        SELECT e.*
        FROM entities AS e
        JOIN related AS q ON q.entity_id = e.entity_id;

        CREATE VIEW jelly_property_elements AS
        SELECT e.path AS entity_path, e.class_name, p.property_index,
               p.property_name, p.type_index, p.type_code, p.subtype,
               pe.element_index, pe.element_name, pe.value_kind,
               pe.integer_value, pe.real_value, pe.text_value, pe.value_json
        FROM jelly_related_entities AS e
        JOIN properties AS p ON p.entity_id = e.entity_id
        JOIN property_elements AS pe ON pe.property_id = p.property_id;
        """
    )


def _insert_schema(db: sqlite3.Connection, full: dict[str, Any]) -> None:
    schema = full.get("schema")
    if not isinstance(schema, dict):
        raise ExportError("decoded Clara manifest has no schema object")

    parameters = schema.get("parameters")
    if not isinstance(parameters, list):
        raise ExportError("decoded Clara schema has no parameters list")
    seen_parameter_indices: set[int] = set()
    for position, item in enumerate(parameters):
        if not isinstance(item, dict):
            raise ExportError(f"schema.parameters[{position}] is not an object")
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ExportError(f"schema.parameters[{position}].index is invalid: {index!r}")
        if index in seen_parameter_indices:
            raise ExportError(f"duplicate schema parameter index: {index}")
        seen_parameter_indices.add(index)
        db.execute(
            """INSERT INTO schema_parameters
               (parameter_index, type_code, type_name, name, subtype, subtype_name, aliases_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                index,
                item.get("type_code"),
                item.get("type_name"),
                item.get("name"),
                item.get("subtype"),
                item.get("subtype_name"),
                _json_text(item.get("aliases", [])),
            ),
        )

    classes = schema.get("classes")
    if not isinstance(classes, list):
        raise ExportError("decoded Clara schema has no classes list")
    seen_classes: set[str] = set()
    for position, item in enumerate(classes):
        if not isinstance(item, dict):
            raise ExportError(f"schema.classes[{position}] is not an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ExportError(f"schema.classes[{position}].name is invalid")
        if name in seen_classes:
            raise ExportError(f"duplicate schema class name: {name}")
        seen_classes.add(name)
        generic = item.get("generic")
        if not isinstance(generic, bool):
            raise ExportError(f"schema class {name!r}.generic is not boolean")
        properties = item.get("properties")
        if not isinstance(properties, list):
            raise ExportError(f"schema class {name!r}.properties is not a list")
        db.execute(
            "INSERT INTO schema_classes(class_name, generic, properties_json) VALUES (?, ?, ?)",
            (name, int(generic), _json_text(properties)),
        )


def _insert_libraries(db: sqlite3.Connection, full: dict[str, Any]) -> dict[int, int]:
    result: dict[int, int] = {}
    libraries = full.get("libraries")
    if not isinstance(libraries, list):
        raise ExportError("decoded Clara manifest has no libraries list")
    for index, library in enumerate(libraries):
        if not isinstance(library, dict):
            raise ExportError(f"libraries[{index}] is not an object")
        root = library.get("root_folder")
        if not isinstance(root, dict):
            raise ExportError(f"libraries[{index}] has no root_folder object")
        root_name = root.get("name")
        if not isinstance(root_name, str):
            raise ExportError(f"libraries[{index}].root_folder.name is not a string")
        cur = db.execute(
            """INSERT INTO libraries
               (library_index, name, marker, version, root_name, library_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                index,
                library.get("name"),
                library.get("marker"),
                library.get("version"),
                root_name,
                _json_text(library),
            ),
        )
        result[index] = int(cur.lastrowid)
    return result


def _insert_entities(
    db: sqlite3.Connection,
    full: dict[str, Any],
    library_ids: dict[int, int],
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    entity_ids: dict[str, int] = {}
    entity_objects: dict[str, dict[str, Any]] = {}

    for library_index, path, parent_path, entity in _iter_entities(full):
        library_id = library_ids.get(library_index)
        if library_id is None:
            raise ExportError(f"entity references missing library index {library_index}")
        fields = _entity_fields(entity)
        cur = db.execute(
            """INSERT INTO entities
               (library_id, path, parent_path, class_name, name, source_offset,
                source_size, fields_json, entity_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                library_id,
                path,
                parent_path,
                entity.get("class"),
                entity.get("name"),
                entity.get("source_offset"),
                entity.get("source_size"),
                _json_text(fields),
                _json_text(entity),
            ),
        )
        entity_id = int(cur.lastrowid)
        entity_ids[path] = entity_id
        entity_objects[path] = entity

        props = entity.get("properties")
        if props is None and entity.get("opaque") is True:
            continue
        if not isinstance(props, list):
            raise ExportError(f"entity {path!r} has no properties list")
        for property_index, prop in enumerate(props):
            if not isinstance(prop, dict):
                raise ExportError(f"entity {path!r} property {property_index} is not an object")
            pcur = db.execute(
                """INSERT INTO properties
                   (entity_id, property_index, property_name, type_index,
                    type_code, subtype, named_elements, property_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entity_id,
                    property_index,
                    prop.get("name"),
                    prop.get("type_index"),
                    prop.get("type_code"),
                    prop.get("subtype"),
                    int(prop["named_elements"])
                    if isinstance(prop.get("named_elements"), bool)
                    else prop.get("named_elements"),
                    _json_text(prop),
                ),
            )
            property_id = int(pcur.lastrowid)
            elements = prop.get("elements")
            if not isinstance(elements, list):
                raise ExportError(f"entity {path!r} property {property_index} has no elements list")
            for element_index, element in enumerate(elements):
                if not isinstance(element, dict):
                    raise ExportError(
                        f"entity {path!r} property {property_index} element {element_index} is not an object"
                    )
                value = element.get("value")
                kind, int_value, real_value, text_value = _scalar_columns(value)
                db.execute(
                    """INSERT INTO property_elements
                       (property_id, element_index, element_name, value_kind,
                        integer_value, real_value, text_value, value_json,
                        raw_base64, element_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        property_id,
                        element_index,
                        element.get("name"),
                        kind,
                        int_value,
                        real_value,
                        text_value,
                        _json_text(value),
                        element.get("raw_base64"),
                        _json_text(element),
                    ),
                )

    return entity_ids, entity_objects


def _insert_references(
    db: sqlite3.Connection,
    entity_ids: dict[str, int],
    entity_objects: dict[str, dict[str, Any]],
) -> None:
    for source_path, source_entity_id in entity_ids.items():
        entity = entity_objects[source_path]
        props = entity.get("properties")
        if not isinstance(props, list):
            continue
        for prop in props:
            if not isinstance(prop, dict):
                continue
            property_name = prop.get("name") if isinstance(prop.get("name"), str) else None
            elements = prop.get("elements")
            if not isinstance(elements, list):
                continue
            for element_index, element in enumerate(elements):
                if not isinstance(element, dict):
                    continue
                for value_path, target_path in _iter_path_strings(element.get("value")):
                    target_entity_id = entity_ids.get(target_path)
                    db.execute(
                        """INSERT INTO entity_references
                           (source_entity_id, source_path, property_name,
                            element_index, value_path, target_path,
                            target_entity_id, target_exists)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            source_entity_id,
                            source_path,
                            property_name,
                            element_index,
                            value_path,
                            target_path,
                            target_entity_id,
                            int(target_entity_id is not None),
                        ),
                    )


def _area_order(entity_objects: dict[str, dict[str, Any]]) -> dict[str, int]:
    order_entity = entity_objects.get(MAP_AREAS_ORDER_PATH)
    if not isinstance(order_entity, dict):
        return {}
    areas_value = _entity_fields(order_entity).get("Areas")
    if isinstance(areas_value, str):
        paths = [areas_value]
    elif isinstance(areas_value, list):
        if any(not isinstance(path, str) for path in areas_value):
            raise ExportError("MapAreasOrder.Areas contains a non-string value")
        paths = areas_value
    else:
        return {}
    if len(paths) != len(set(paths)):
        raise ExportError("MapAreasOrder.Areas contains duplicate area references")
    return {path: index for index, path in enumerate(paths, 1)}


def _insert_jelly_tables(
    db: sqlite3.Connection,
    full: dict[str, Any],
    entity_ids: dict[str, int],
    entity_objects: dict[str, dict[str, Any]],
) -> tuple[int, int]:
    order_by_path = _area_order(entity_objects)
    area_paths_by_number: dict[int, str] = {}

    area_specs = _discover_property_columns(
        full,
        entity_objects,
        "MapArea",
        "areas",
        _table_column_names(db, "areas"),
    )
    level_specs = _discover_property_columns(
        full,
        entity_objects,
        "MapMission",
        "levels",
        _table_column_names(db, "levels"),
    )
    _install_property_columns(db, "areas", area_specs)
    _install_property_columns(db, "levels", level_specs)

    for path, entity in entity_objects.items():
        if entity.get("class") != "MapArea":
            continue
        match = MAP_AREA_RE.fullmatch(path)
        if match is None:
            # Preserve the entity in generic tables even when its name cannot be
            # normalized into a Jelly Lab area number.
            continue
        area_number = int(match.group(1))
        if area_number in area_paths_by_number:
            raise ExportError(
                "duplicate MapArea number %d: %s and %s"
                % (area_number, area_paths_by_number[area_number], path)
            )
        area_paths_by_number[area_number] = path
        fields = _entity_fields(entity)
        bonus1_path = _plain_text(fields.get("BonusType1"))
        bonus2_path = _plain_text(fields.get("BonusType2"))
        db.execute(
            """INSERT INTO areas
               (area_number, order_index, entity_id, entity_path, entity_name,
                required_fruits_bonus1, required_fruits_bonus2,
                bonus_type1_path, bonus_type1_name,
                bonus_type2_path, bonus_type2_name, fields_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                area_number,
                order_by_path.get(path),
                entity_ids[path],
                path,
                entity.get("name"),
                _plain_int(fields.get("RequiredFruitsForBonus1")),
                _plain_int(fields.get("RequiredFruitsForBonus2")),
                bonus1_path,
                _first_path_leaf(bonus1_path),
                bonus2_path,
                _first_path_leaf(bonus2_path),
                _json_text(fields),
            ),
        )
        _write_property_columns(
            db,
            "areas",
            "area_number",
            area_number,
            fields,
            area_specs,
        )

    missions_by_area: dict[int, list[tuple[int, str, dict[str, Any]]]] = {}
    for path, entity in entity_objects.items():
        if entity.get("class") != "MapMission" or not path.startswith(MAP_MISSION_ROOT):
            continue
        name = path.rsplit("/", 1)[-1]
        match = MAP_MISSION_RE.fullmatch(name)
        if match is None:
            continue
        area_number = int(match.group(1))
        mission_suffix = int(match.group(2))
        missions_by_area.setdefault(area_number, []).append((mission_suffix, path, entity))

    total_levels = 0
    for area_number in sorted(missions_by_area):
        rows = sorted(missions_by_area[area_number], key=lambda row: (row[0], row[1]))
        seen_suffixes: set[int] = set()
        for level_in_area, (mission_suffix, path, entity) in enumerate(rows, 1):
            if mission_suffix in seen_suffixes:
                raise ExportError(
                    "duplicate mission suffix %d in area %d" % (mission_suffix, area_number)
                )
            seen_suffixes.add(mission_suffix)
            fields = _entity_fields(entity)
            cur = db.execute(
                """INSERT INTO levels
                   (area_number, level_in_area, mission_suffix, mission_id,
                    entity_id, entity_path, entity_name,
                    target_value1, target_value2, target_value3,
                    force_location, fields_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    area_number,
                    level_in_area,
                    mission_suffix,
                    _plain_int(fields.get("MissionId")),
                    entity_ids[path],
                    path,
                    entity.get("name"),
                    _plain_int(fields.get("TargetValue1")),
                    _plain_int(fields.get("TargetValue2")),
                    _plain_int(fields.get("TargetValue3")),
                    _enum_u32(fields.get("ForceLocation")),
                    _json_text(fields),
                ),
            )
            _write_property_columns(
                db,
                "levels",
                "level_id",
                int(cur.lastrowid),
                fields,
                level_specs,
            )
            total_levels += 1

    db.execute(
        """UPDATE areas
           SET level_count = (
               SELECT COUNT(*) FROM levels WHERE levels.area_number = areas.area_number
           )"""
    )
    return len(area_paths_by_number), total_levels


def _populate_database(
    db: sqlite3.Connection,
    input_path: Path,
    source_data: bytes,
    full: dict[str, Any],
) -> tuple[int, int, int]:
    _create_schema(db)

    source_sha256 = hashlib.sha256(source_data).hexdigest()
    decoded_sha256 = full.get("source_sha256")
    if decoded_sha256 != source_sha256:
        raise ExportError("decoder source SHA-256 does not match the input bytes")
    schema = full.get("schema")
    libraries = full.get("libraries")
    if not isinstance(schema, dict):
        raise ExportError("decoded Clara manifest has no schema object")
    if not isinstance(libraries, list):
        raise ExportError("decoded Clara manifest has no libraries list")

    # Treat decode_file() as the supported API boundary.  The refactored
    # blibclara_library_editor intentionally no longer exposes an internal full-manifest
    # FORMAT constant, so the exporter validates the data it actually consumes
    # instead of depending on private decoder metadata.
    info = {
        "sqlite_schema_version": str(SCHEMA_VERSION),
        "source_file": input_path.name,
        "source_sha256": source_sha256,
        "source_size": str(len(source_data)),
        "decoder_format": DECODER_MANIFEST_FORMAT,
        "decoder_format_version": str(DECODER_MANIFEST_VERSION),
    }
    db.executemany(
        "INSERT INTO database_info(key, value) VALUES (?, ?)",
        sorted(info.items()),
    )
    db.execute(
        """INSERT INTO source_file
           (source_id, file_name, source_sha256, source_size, data, decoded_manifest_json)
           VALUES (1, ?, ?, ?, ?, ?)""",
        (input_path.name, source_sha256, len(source_data), source_data, _json_text(full)),
    )

    _insert_schema(db, full)
    library_ids = _insert_libraries(db, full)
    entity_ids, entity_objects = _insert_entities(db, full, library_ids)
    _insert_references(db, entity_ids, entity_objects)
    area_count, level_count = _insert_jelly_tables(db, full, entity_ids, entity_objects)
    return len(entity_ids), area_count, level_count


def _verify_database(db: sqlite3.Connection, source_data: bytes) -> None:
    row = db.execute("PRAGMA integrity_check").fetchone()
    if row is None or row[0] != "ok":
        raise ExportError("SQLite integrity_check failed: %r" % (row,))
    row = db.execute("PRAGMA foreign_key_check").fetchone()
    if row is not None:
        raise ExportError("SQLite foreign_key_check failed: %r" % (row,))
    source_row = db.execute(
        "SELECT source_sha256, source_size, data FROM source_file WHERE source_id = 1"
    ).fetchone()
    if source_row is None:
        raise ExportError("source_file row is missing after export")
    source_sha256, source_size, stored_data = source_row
    if int(source_size) != len(source_data) or bytes(stored_data) != source_data:
        raise ExportError("stored source BLOB does not match input")
    if source_sha256 != hashlib.sha256(source_data).hexdigest():
        raise ExportError("stored source SHA-256 does not match input")


def _export_maplib(input_path: Path, output_path: Path, *, force: bool) -> dict[str, Any]:
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if not input_path.is_file():
        raise FileNotFoundError("input maplib.blibclara not found: %s" % input_path)
    if output_path.exists() and not force:
        raise FileExistsError("output already exists (use --force to replace it): %s" % output_path)
    if input_path == output_path:
        raise ExportError("input and output paths must be different")

    source_data = input_path.read_bytes()
    try:
        full = blibclara_library_editor.decode_file(source_data, input_path.name)
    except blibclara_library_editor.CodecError as exc:
        raise ExportError("cannot decode %s: %s" % (input_path, exc)) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=output_path.name + ".",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        db = sqlite3.connect(str(temp_path))
        try:
            db.execute("PRAGMA journal_mode = DELETE")
            db.execute("PRAGMA synchronous = FULL")
            db.execute("BEGIN IMMEDIATE")
            entity_count, area_count, level_count = _populate_database(
                db, input_path, source_data, full
            )
            db.commit()
            _verify_database(db, source_data)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        # os.replace installs the fully verified temporary database directly.
        # It also replaces an existing destination atomically on the same
        # filesystem when --force is in effect; do not unlink first.
        os.replace(str(temp_path), str(output_path))
        return {
            "input": str(input_path),
            "output": str(output_path),
            "source_sha256": hashlib.sha256(source_data).hexdigest(),
            "entities": entity_count,
            "areas": area_count,
            "levels": level_count,
        }
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert Minion Rush maplib.blibclara to a loss-preserving SQLite database"
    )
    parser.add_argument("input", type=Path, help="input maplib.blibclara")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output SQLite database; default: INPUT with suffix .sqlite",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output database",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    output = args.output if args.output is not None else args.input.with_suffix(".sqlite")
    try:
        result = _export_maplib(args.input, output, force=args.force)
    except (OSError, sqlite3.Error, ExportError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
