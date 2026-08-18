#!/usr/bin/env python3
"""Clara BLIB Editor v20 — per-library schema-aware editing for ``.blibclara`` files.

SCOPE
=====
This tool decodes, validates, edits, and rebuilds the BLIBCLARA format recovered
from Minion Rush. Code shared with standalone BCLARA is deliberately factored
out instead of duplicated:

* ``clara_common.py`` owns the Clara v12 schema, entity preamble, property-array,
  value-type, nested-entity, and entity-envelope codecs;
* ``bclara_editor.py`` owns the single executable-faithful
  Clara_movie implementation;
* this module owns only the BLIBCLARA container, recursive folder tree,
  original-backed per-library JSON merge logic, and BLIB-specific preservation policy.

DEPENDENCIES
============
Keep these three files importable from the same directory or ``PYTHONPATH``::

    clara_common.py
    bclara_editor.py
    blibclara_library_editor.py

BLIBCLARA ON-DISK FORMAT
========================
All numeric fields are little-endian. ``String16`` is ``u16 byte_length`` plus
that many bytes, with no NUL terminator or alignment padding.

BLIBCLARA::

    Schema                         # parsed by clara_common.py
    u16 library_count
    Library libraries[library_count]

Schema::

    u32 schema_header
    u16 parameter_count
    Parameter parameters[parameter_count]
    u16 class_count
    ClassDef classes[class_count]

Parameter::

    u16 type_code
    String16 name
    u8 subtype
    u16 alias_count
    String16 aliases[alias_count]

ClassDef::

    String16 name
    u8 generic
    u32 property_count
    (String16 property_name, u32 parameter_index)[property_count]

Library::

    u16 marker                    # 0x1AAA
    u16 version                   # 12
    String16 name
    u8 root_tag                   # normally 'f'; runtime value is preserved
    Folder root

Folder::

    String16 name
    u16 capacity_f
    u16 capacity_e
    u16 capacity_g
    u16 capacity_m
    u16 capacity_u
    u16 record_count
    Record records[record_count]

Known record tags are ``f`` folder, ``e`` entity, ``g`` group, ``m`` movie, and
``u`` multilayer. The recovered executable has no default payload handler for an
unrecognized one-byte folder tag: it consumes that tag and continues. Such tags
are therefore preserved as zero-payload ``unknown`` records. Folder capacities
are runtime allocation hints in ``f/e/g/m/u`` order. Larger original capacities
are preserved and raised only when an edit requires more immediate records.

The executable also reads each library root tag without checking that it is
``'f'`` before entering the folder loader. Editable JSON omits that byte as
preservation metadata; encode restores it from the exact original binary.

SCHEMA AND ENTITIES
===================
Known classes are decoded by schema class/property name through
``clara_common.py``. The common codec handles the recovered Clara type codes
``0x0002`` through ``0x1000``, numeric i8/i16/i32/f32/f64 subtypes,
named/unnamed property arrays, nested entities, the common entity preamble, and
exact reuse of unchanged serialized property values.

The embedded schema is read-only and is not copied into per-library editable JSON.
Encode always obtains it from the exact original BLIBCLARA. Classes absent from
the embedded schema are kept as opaque size-bounded bodies.

MOVIES
======
``m`` records are parsed and written by ``bclara_editor.py``
so both container editors use one Clara_movie grammar. Recovered track tags are
``e/x/s/m/p/b`` and known key-mask bits are ``0x01/0x02/0x04/0x08/0x10``. The
executable-faithful codec also preserves an unrecognized track-tag byte using the
runtime's zero-initialized effective ``e`` behavior, arbitrary negative track
terminators, and unknown key-mask bits that the runtime ignores.

EDITING MODEL
=============
``decode`` requires one library name and writes JSON for that library only. No
other libraries or embedded-schema dump are included. Preservation-only details
such as entity preambles, original encoded property values, opaque entity bodies,
folder allocation capacities, library root-tag bytes, and executable-visible movie
metadata are omitted from the editable JSON.

``encode`` reads the target library name from the JSON, requires the exact original
BLIBCLARA identified by ``source_sha256``, restores preservation metadata from that
original, replaces only the named library, rebuilds the complete container, then
reparses and semantically verifies the complete result. All other libraries come
unchanged from the original input.

There is deliberately no backward-compatibility layer for older whole-file or
per-library editable JSON formats; only the current format is accepted.

COMMANDS
========
    python blibclara_library_editor.py decode INPUT.blibclara LIBRARY OUTPUT.json
    python blibclara_library_editor.py encode ORIGINAL.blibclara EDITED.json OUTPUT.blibclara
    python blibclara_library_editor.py verify INPUT.blibclara
    python blibclara_library_editor.py roundtrip INPUT.blibclara OUTPUT.blibclara
    python blibclara_library_editor.py list-libraries INPUT.blibclara

KNOWN LIMITATIONS
=================
* Only the recovered Minion Rush Clara marker ``0x1AAA`` and version ``12`` plus
  the recovered property type dispatch are supported. Unknown versions or type
  codes are rejected.
* The embedded schema is read-only and omitted from editable JSON. The editor
  cannot add/remove schema classes, change class property definitions, or
  synthesize a new schema.
* Unknown-class top-level or nested entities are preserved losslessly but are
  opaque/read-only in the per-library JSON workflow; they cannot safely be newly
  created, renamed, or moved.
* Creating or moving a known-class entity requires a usable same-class entity
  preamble template from the original file. If no template exists, or existing
  templates disagree and the choice is ambiguous, the edit fails closed.
* The common entity preamble is structurally understood but not fully named
  semantically, which is why original-backed metadata remains necessary.
* Encoding requires the exact original BLIBCLARA. A per-library JSON file is not
  a self-contained replacement for its source binary.
* Movie support inherits the executable-faithful BCLARA movie limitations:
  several track-kind meanings, the movie ``flag``, and payloads ``0x08``/``0x10``
  remain partly unresolved even though their recovered binary grammar is supported.
  Unknown track tags, ignored mask bits, and arbitrary negative terminators are
  preserved from the original when the corresponding movie/track/key remains
  positionally compatible; newly created records use canonical valid forms.
* Structural validation cannot prove game-level reference integrity, uniqueness
  constraints, class-specific invariants, or dependencies between libraries,
  BCLARA files, scripts, graphs, or other resources.
* Older whole-file and per-library editable JSON formats are intentionally
  rejected rather than translated or silently accepted.

SAFETY MODEL
============
Malformed lengths/counts, unsupported Clara property types, schema/property
mismatches, ambiguous preamble templates, invalid numeric ranges, and source-hash
mismatches fail closed. Generated output is reparsed and semantically verified
before it is accepted as a successful encode.
"""
from __future__ import annotations

import argparse
import copy
import json
import struct
import sys
from pathlib import Path
from typing import Any

try:
    import clara_common as clara
except ImportError as exc:
    raise SystemExit(
        "error: clara_common.py is required and must be importable from the same directory (or PYTHONPATH)"
    ) from exc
try:
    import bclara_editor as bclara_codec
except ImportError as exc:
    raise SystemExit(
        "error: bclara_editor.py is required and must be importable from the same directory (or PYTHONPATH)"
    ) from exc

CodecError = clara.ClaraError
Reader = clara.Reader
Schema = clara.Schema
sha256 = clara.sha256
b64e = clara.b64e
b64d = clara.b64d
pack_string = clara.pack_string
parse_schema = clara.parse_schema
parse_entity_record = clara.parse_entity_record
schema_json = clara.schema_json
checked_count = clara.checked_count
semantic_equal = clara.semantic_equal
encode_envelope = clara.encode_envelope

ENTITY_TAG = clara.ENTITY_TAG
CLARA_MARKER = bclara_codec.MARKER
CLARA_VERSION = bclara_codec.VERSION
RECORD_KIND_ORDER = bclara_codec.RECORD_KIND_ORDER

def parse_movie_record(r: Reader, path: str) -> dict[str, Any]:
    """Decode one Clara_movie body with the shared standalone BCLARA codec."""
    try:
        return bclara_codec.parse_movie(r, path)
    except bclara_codec.BClaraError as exc:
        raise CodecError(f"{path}: {exc}") from exc


def encode_movie_record(item: dict[str, Any], path: str) -> bytes:
    """Encode one complete tagged Clara_movie record via the shared BCLARA codec."""
    try:
        return b"m" + bclara_codec.encode_movie(item, path)
    except bclara_codec.BClaraError as exc:
        raise CodecError(f"{path}: {exc}") from exc


def parse_group_record(r: Reader, path: str) -> dict[str, Any]:
    name = r.shared_string(path + ".name")
    count = checked_count(r.u32(path + ".item_count"), 2, r.remaining(), path + ".item_count")
    items = [r.shared_string(f"{path}.items[{i}]") for i in range(count)]
    return {"kind": "group", "name": name, "items": items}


def parse_multilayer_record(r: Reader, path: str) -> dict[str, Any]:
    name = r.shared_string(path + ".name")
    layer_count = checked_count(r.u32(path + ".layer_count"), 2, r.remaining(), path + ".layer_count")
    layers = [r.shared_string(f"{path}.layers[{i}]") for i in range(layer_count)]
    column_count = checked_count(r.u32(path + ".column_count"), 2, r.remaining(), path + ".column_count")
    columns = [r.shared_string(f"{path}.columns[{i}]") for i in range(column_count)]
    cells = layer_count * column_count
    if cells > r.remaining() // 4:
        raise CodecError(f"{path}.matrix requires {cells * 4} bytes, only {r.remaining()} remain")
    matrix = []
    for i in range(layer_count):
        matrix.append([r.u32(f"{path}.matrix[{i}][{j}]") for j in range(column_count)])
    return {"kind": "multilayer", "name": name, "layers": layers,
            "columns": columns, "matrix": matrix}


def parse_folder(r: Reader, schema: Schema, path: str = "root") -> dict[str, Any]:
    name = r.shared_string(path + ".name")
    allocation_counts = [r.u16(f"{path}.allocation_counts[{i}]") for i in range(5)]
    record_count = r.u16(path + ".record_count")
    if record_count > r.remaining():
        raise CodecError(
            f"{path}.record_count={record_count} cannot fit in {r.remaining()} remaining bytes"
        )
    records: list[dict[str, Any]] = []
    for i in range(record_count):
        rec_path = f"{path}.records[{i}]"
        tag = r.u8(rec_path + ".tag")
        if tag == ord("f"):
            child = parse_folder(r, schema, rec_path)
            records.append({"kind": "folder", "folder": child})
        elif tag == ENTITY_TAG:
            records.append({
                "kind": "entity",
                "entity": parse_entity_record(r, schema, rec_path, tag_already_read=True),
            })
        elif tag == ord("g"):
            records.append(parse_group_record(r, rec_path))
        elif tag == ord("u"):
            records.append(parse_multilayer_record(r, rec_path))
        elif tag == ord("m"):
            records.append(parse_movie_record(r, rec_path))
        else:
            # FUN_00483b50 has no default payload handler. After consuming an
            # unrecognized record tag byte, the runtime simply continues with
            # the next record. Preserve that exact zero-payload record.
            records.append({"kind": "unknown", "tag": tag})
    actual_counts = [
        sum(rec.get("kind") == kind for rec in records) for kind in RECORD_KIND_ORDER
    ]
    for i, (capacity, actual) in enumerate(zip(allocation_counts, actual_counts)):
        if capacity < actual:
            raise CodecError(
                f"{path}.allocation_counts[{i}]={capacity} is smaller than actual {actual}"
            )
    return {"name": name, "allocation_counts": allocation_counts, "records": records}


def encode_group_record(item: dict[str, Any], path: str) -> bytes:
    if not isinstance(item.get("items"), list):
        raise CodecError(f"{path}.items must be a list")
    items = item["items"]
    if len(items) > 0xFFFFFFFF:
        raise CodecError(f"{path}.items is too long")
    return b"g" + pack_string(item.get("name"), path + ".name") + struct.pack("<I", len(items)) + b"".join(
        pack_string(v, f"{path}.items[{i}]") for i, v in enumerate(items))


def encode_multilayer_record(item: dict[str, Any], path: str) -> bytes:
    layers, columns, matrix = item.get("layers"), item.get("columns"), item.get("matrix")
    if not isinstance(layers, list) or not isinstance(columns, list) or not isinstance(matrix, list):
        raise CodecError(f"{path}: layers, columns, and matrix must be lists")
    if len(matrix) != len(layers) or any(not isinstance(row, list) or len(row) != len(columns) for row in matrix):
        raise CodecError(f"{path}.matrix dimensions must be layer_count x column_count")
    out = bytearray(b"u" + pack_string(item.get("name"), path + ".name"))
    out += struct.pack("<I", len(layers))
    for i, v in enumerate(layers): out += pack_string(v, f"{path}.layers[{i}]")
    out += struct.pack("<I", len(columns))
    for i, v in enumerate(columns): out += pack_string(v, f"{path}.columns[{i}]")
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 0xFFFFFFFF:
                raise CodecError(f"{path}.matrix[{i}][{j}] must be u32")
            out += struct.pack("<I", v)
    return bytes(out)


def encode_folder(folder: dict[str, Any], schema: Schema, path: str = "root") -> bytes:
    if not isinstance(folder, dict): raise CodecError(f"{path} must be an object")
    counts = folder.get("allocation_counts")
    records = folder.get("records")
    if not isinstance(counts, list) or len(counts) != 5 or any(not isinstance(x, int) or isinstance(x, bool) or not 0 <= x <= 0xFFFF for x in counts):
        raise CodecError(f"{path}.allocation_counts must contain five u16 values in f/e/g/m/u order")
    if not isinstance(records, list) or len(records) > 0xFFFF:
        raise CodecError(f"{path}.records must be a list of at most 65535 records")
    # The engine allocates five immediate-record pools in f/e/g/m/u order.
    # Preserve larger original capacities, but never emit a capacity smaller
    # than the number of records that will be loaded into that pool.
    kind_slot = {"folder": 0, "entity": 1, "group": 2, "movie": 3, "multilayer": 4}
    required = [0, 0, 0, 0, 0]
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise CodecError(f"{path}.records[{i}] must be an object")
        kind = rec.get("kind")
        if kind in kind_slot:
            required[kind_slot[kind]] += 1
        elif kind == "unknown":
            tag = rec.get("tag")
            if not isinstance(tag, int) or isinstance(tag, bool) or not 0 <= tag <= 0xFF:
                raise CodecError(f"{path}.records[{i}].tag must be a byte")
            if tag in {ord("f"), ord("e"), ord("g"), ord("m"), ord("u")}:
                raise CodecError(f"{path}.records[{i}].tag is a recognized Clara record tag; use its normal kind")
        else:
            raise CodecError(f"{path}.records[{i}].kind is unsupported: {kind!r}")
    effective_counts = [max(counts[i], required[i]) for i in range(5)]
    out = bytearray(pack_string(folder.get("name"), path + ".name"))
    out += b"".join(struct.pack("<H", x) for x in effective_counts)
    out += struct.pack("<H", len(records))
    for i, rec in enumerate(records):
        rp = f"{path}.records[{i}]"
        if not isinstance(rec, dict): raise CodecError(f"{rp} must be an object")
        kind = rec.get("kind")
        if kind == "folder": out += b"f" + encode_folder(rec.get("folder"), schema, rp)
        elif kind == "entity": out += encode_envelope(rec.get("entity"), schema, rp + ".entity")
        elif kind == "group": out += encode_group_record(rec, rp)
        elif kind == "movie": out += encode_movie_record(rec, rp)
        elif kind == "multilayer": out += encode_multilayer_record(rec, rp)
        elif kind == "unknown":
            tag = rec.get("tag")
            if not isinstance(tag, int) or isinstance(tag, bool) or not 0 <= tag <= 0xFF:
                raise CodecError(f"{rp}.tag must be a byte")
            if tag in {ord("f"), ord("e"), ord("g"), ord("m"), ord("u")}:
                raise CodecError(f"{rp}.tag is recognized; use its normal record kind")
            out += bytes([tag])
        else: raise CodecError(f"{rp}.kind is unsupported: {kind!r}")
    return bytes(out)


def flatten_entities(folder: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in folder.get("records", []):
        if rec.get("kind") == "entity": out.append(rec["entity"])
        elif rec.get("kind") == "folder": out.extend(flatten_entities(rec["folder"]))
    return out


def decode_file(data: bytes, source_name: str) -> dict[str, Any]:
    schema = parse_schema(data)
    r = Reader(data[schema.end:], base=schema.end, label=source_name)
    library_count = r.u16("library_count")
    libraries: list[dict[str, Any]] = []
    all_entities: list[dict[str, Any]] = []
    for i in range(library_count):
        path = f"libraries[{i}]"
        marker = r.u16(path + ".marker")
        version = r.u16(path + ".version")
        if marker != CLARA_MARKER:
            raise CodecError(
                f"{path}: expected marker 0x{CLARA_MARKER:04X}, got 0x{marker:04X}"
            )
        if version != CLARA_VERSION:
            raise CodecError(f"{path}: unsupported Clara library version {version}")
        name = r.shared_string(path + ".name")
        # The recovered loader reads this byte and immediately enters the folder
        # loader without comparing it with 'f'. Preserve it verbatim.
        root_tag = r.u8(path + ".root_tag")
        root = parse_folder(r, schema, path + ".root_folder")
        libraries.append({"marker": marker, "version": version, "name": name,
                          "root_tag": root_tag, "root_folder": root})
        all_entities.extend(flatten_entities(root))
    if r.remaining():
        raise CodecError(f"{source_name}: {r.remaining()} trailing bytes at 0x{r.absolute():X}")
    return {
        "source_sha256": sha256(data),
        "schema_raw_base64": b64e(data[:schema.end]),
        "schema": schema_json(schema), "libraries": libraries,
        "entities": all_entities,
    }


def semantic_view(value: Any) -> Any:
    """Remove provenance-only fields for post-encode semantic comparison."""
    if isinstance(value, list):
        return [semantic_view(x) for x in value]
    if isinstance(value, dict):
        return {k: semantic_view(v) for k, v in value.items()
                if k not in {"original_value", "original_name", "raw_base64"}}
    return value


def encode_manifest(original: bytes, manifest: dict[str, Any]) -> bytes:
    if manifest.get("source_sha256") != sha256(original):
        raise CodecError("manifest was decoded from a different input file")
    schema_raw = b64d(manifest.get("schema_raw_base64"), "schema_raw_base64")
    if original[:len(schema_raw)] != schema_raw:
        raise CodecError("schema bytes differ from input")
    schema = parse_schema(schema_raw)
    if schema.end != len(schema_raw):
        raise CodecError("schema_raw_base64 contains non-schema bytes")
    libraries = manifest.get("libraries")
    if not isinstance(libraries, list) or len(libraries) > 0xFFFF:
        raise CodecError("libraries must be a list of at most 65535 libraries")
    out = bytearray(schema_raw)
    out += struct.pack("<H", len(libraries))
    for i, lib in enumerate(libraries):
        path = f"libraries[{i}]"
        if not isinstance(lib, dict):
            raise CodecError(f"{path} must be an object")
        marker = lib.get("marker")
        version = lib.get("version")
        if marker != CLARA_MARKER:
            raise CodecError(f"{path}.marker must remain 0x{CLARA_MARKER:04X}")
        if version != CLARA_VERSION:
            raise CodecError(f"{path}.version must remain {CLARA_VERSION}")
        root_tag = lib.get("root_tag")
        if not isinstance(root_tag, int) or isinstance(root_tag, bool) or not 0 <= root_tag <= 0xFF:
            raise CodecError(f"{path}.root_tag must be a byte")
        out += struct.pack("<HH", marker, version)
        out += pack_string(lib.get("name"), path + ".name")
        out += bytes([root_tag]) + encode_folder(lib.get("root_folder"), schema, path + ".root_folder")
    rebuilt = bytes(out)
    check = decode_file(rebuilt, "verification.blibclara")
    expected = semantic_view(libraries)
    actual = semantic_view(check["libraries"])
    def strip_capacities(v: Any) -> Any:
        if isinstance(v, list):
            return [strip_capacities(x) for x in v]
        if isinstance(v, dict):
            return {k: strip_capacities(x) for k, x in v.items() if k != "allocation_counts"}
        return v
    if not semantic_equal(strip_capacities(expected), strip_capacities(actual)):
        raise CodecError("post-encode semantic verification failed: decoded library tree differs from requested data")
    return rebuilt


USER_FORMAT = "generic-clara-blib-library-editable"
EDITABLE_FORMAT_VERSION = 1


def strip_internal_fields(value: Any) -> Any:
    """Return the clean user-editable view without preservation-only metadata."""
    if isinstance(value, list):
        return [strip_internal_fields(x) for x in value]
    if not isinstance(value, dict):
        return value

    omitted = {
        "raw_base64", "original_value", "original_name",
        "preamble_hex", "allocation_counts", "opaque_body_base64",
    }
    out = {k: strip_internal_fields(v) for k, v in value.items() if k not in omitted}

    # These fields are executable-visible but semantically inert encodings.
    # enrich_movie() restores them from the exact original during encode.
    if "keys" in value and "type" in value:
        out.pop("terminator", None)
        out.pop("raw_type_byte", None)
        out.pop("effective_type", None)
    if "time" in value:
        out.pop("extra_mask_bits", None)
    return out


def find_library(libraries: Any, name: str) -> tuple[int, dict[str, Any]]:
    if not isinstance(libraries, list):
        raise CodecError("decoded BLIBCLARA libraries are invalid")
    matches = [
        (i, library)
        for i, library in enumerate(libraries)
        if isinstance(library, dict) and library.get("name") == name
    ]
    if not matches:
        raise CodecError(f"library {name!r} not found; use list-libraries to see valid names")
    if len(matches) != 1:
        raise CodecError(f"library name {name!r} is ambiguous ({len(matches)} matches)")
    return matches[0]


def make_editable_library(full: dict[str, Any], library_name: str) -> dict[str, Any]:
    _, source_library = find_library(full.get("libraries"), library_name)
    library = strip_internal_fields(source_library)
    if not isinstance(library, dict):
        raise CodecError(f"library {library_name!r} decoded to an invalid object")
    library.pop("marker", None)
    library.pop("version", None)
    library.pop("root_tag", None)
    return {
        "format": USER_FORMAT,
        "format_version": EDITABLE_FORMAT_VERSION,
        "source_sha256": full["source_sha256"],
        "library": library,
    }


def load_editable(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(obj, dict) or obj.get("format") != USER_FORMAT or
            obj.get("format_version") != EDITABLE_FORMAT_VERSION):
        raise CodecError("unsupported editable JSON format/version")

    expected_fields = {"format", "format_version", "source_sha256", "library"}
    extra = set(obj) - expected_fields
    if extra:
        raise CodecError(f"editable JSON has unsupported top-level fields: {', '.join(sorted(extra))}")
    missing = expected_fields - set(obj)
    if missing:
        raise CodecError(f"editable JSON is missing top-level fields: {', '.join(sorted(missing))}")

    source_hash = obj.get("source_sha256")
    if (not isinstance(source_hash, str) or len(source_hash) != 64 or
            any(ch not in "0123456789abcdef" for ch in source_hash)):
        raise CodecError("editable JSON source_sha256 must be a lowercase SHA-256 hex string")

    library = obj.get("library")
    if not isinstance(library, dict):
        raise CodecError("editable JSON library must be an object")
    allowed_library_fields = {"name", "root_folder"}
    extra_library_fields = set(library) - allowed_library_fields
    if extra_library_fields:
        raise CodecError(
            "editable JSON library has unsupported fields: "
            + ", ".join(sorted(extra_library_fields))
        )
    missing_library_fields = allowed_library_fields - set(library)
    if missing_library_fields:
        raise CodecError(
            "editable JSON library is missing fields: "
            + ", ".join(sorted(missing_library_fields))
        )
    if not isinstance(library.get("name"), str):
        raise CodecError("editable JSON library.name must be a string")
    if not isinstance(library.get("root_folder"), dict):
        raise CodecError("editable JSON library.root_folder must be an object")
    return obj


def build_entity_indexes(full: dict[str, Any]) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    exact: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_class: dict[str, list[dict[str, Any]]] = {}

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            if isinstance(v.get("class"), str) and isinstance(v.get("name"), str) and isinstance(v.get("properties"), list):
                exact.setdefault((v["class"], v["name"]), []).append(v)
                by_class.setdefault(v["class"], []).append(v)
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(full.get("libraries", []))
    return exact, by_class


def choose_base_entity(edit: dict[str, Any], positional: Any,
                       exact: dict[tuple[str, str], list[dict[str, Any]]],
                       by_class: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    cls, name = edit.get("class"), edit.get("name")
    matches = exact.get((cls, name), []) if isinstance(cls, str) and isinstance(name, str) else []
    if len(matches) == 1:
        return matches[0]
    if isinstance(positional, dict) and positional.get("class") == cls:
        return positional
    candidates = by_class.get(cls, []) if isinstance(cls, str) else []
    preambles = {
        candidate.get("preamble_hex")
        for candidate in candidates
        if isinstance(candidate.get("preamble_hex"), str)
    }
    if candidates and len(preambles) == 1:
        return candidates[0]
    return None


def enrich_entity(edit: dict[str, Any], positional: Any,
                  exact: dict[tuple[str, str], list[dict[str, Any]]],
                  by_class: dict[str, list[dict[str, Any]]], path: str) -> dict[str, Any]:
    if not isinstance(edit, dict):
        raise CodecError(f"{path} must be an entity object")
    out = copy.deepcopy(edit)
    if out.get("opaque") is True:
        if not (isinstance(positional, dict) and positional.get("opaque") is True and
                positional.get("class") == out.get("class")):
            raise CodecError(f"{path}: opaque unknown-class entities are read-only and may not be moved or newly created")
        raw_body = positional.get("opaque_body_base64")
        if not isinstance(raw_body, str):
            raise CodecError(f"{path}: original input lacks opaque entity body")
        out["opaque_body_base64"] = raw_body
        return out
    base = choose_base_entity(out, positional, exact, by_class)
    if base is None:
        raise CodecError(f"{path}: no existing same-class entity is available as a preamble template")
    preamble = base.get("preamble_hex")
    if not isinstance(preamble, str):
        raise CodecError(f"{path}: original input template lacks preamble")
    out["preamble_hex"] = preamble
    eprops = out.get("properties")
    bprops = base.get("properties") if isinstance(base, dict) else None
    if not isinstance(eprops, list):
        raise CodecError(f"{path}.properties must be a list")
    for pi, ep in enumerate(eprops):
        bp = bprops[pi] if isinstance(bprops, list) and pi < len(bprops) else None
        if not isinstance(ep, dict):
            continue
        elems = ep.get("elements")
        belems = bp.get("elements") if isinstance(bp, dict) else None
        if not isinstance(elems, list):
            continue
        for ei, elem in enumerate(elems):
            if not isinstance(elem, dict):
                continue
            be = None
            if isinstance(belems, list):
                if ep.get("named_elements") is True and isinstance(elem.get("name"), str):
                    named_matches = [
                        candidate for candidate in belems
                        if isinstance(candidate, dict) and candidate.get("name") == elem.get("name")
                    ]
                    if len(named_matches) == 1:
                        be = named_matches[0]
                if be is None and ei < len(belems):
                    be = belems[ei]
            if isinstance(be, dict):
                elem["original_value"] = copy.deepcopy(be.get("value"))
                if isinstance(be.get("raw_base64"), str):
                    elem["raw_base64"] = be["raw_base64"]
                if ep.get("named_elements") is True:
                    elem["original_name"] = be.get("name")
            value = elem.get("value")
            base_value = be.get("value") if isinstance(be, dict) else None
            if isinstance(value, dict) and value.get("opaque") is True:
                if not (isinstance(base_value, dict) and base_value.get("opaque") is True and
                        base_value.get("class") == value.get("class")):
                    raise CodecError(f"{path}.properties[{pi}].elements[{ei}].value: opaque nested entities are read-only")
                raw_body = base_value.get("opaque_body_base64")
                if not isinstance(raw_body, str):
                    raise CodecError(f"{path}.properties[{pi}].elements[{ei}].value: original input lacks opaque body")
                value["opaque_body_base64"] = raw_body
            elif isinstance(value, dict) and "class" in value and "properties" in value:
                elem["value"] = enrich_entity(value, base_value, exact, by_class,
                                               f"{path}.properties[{pi}].elements[{ei}].value")
    return out


def enrich_movie(edit: dict[str, Any], positional: Any, path: str) -> dict[str, Any]:
    """Restore movie preservation metadata omitted from per-library editable JSON."""
    if not isinstance(edit, dict) or edit.get("kind") != "movie":
        raise CodecError(f"{path} must be a movie object")
    out = copy.deepcopy(edit)
    base = positional if isinstance(positional, dict) and positional.get("kind") == "movie" else None
    if base is None:
        return out

    etracks = out.get("tracks")
    btracks = base.get("tracks")
    if not isinstance(etracks, list):
        raise CodecError(f"{path}.tracks must be a list")
    for ti, track in enumerate(etracks):
        if not isinstance(track, dict):
            raise CodecError(f"{path}.tracks[{ti}] must be an object")
        bt = btracks[ti] if isinstance(btracks, list) and ti < len(btracks) and isinstance(btracks[ti], dict) else None

        if track.get("type") == "unknown":
            if not (isinstance(bt, dict) and bt.get("type") == "unknown"):
                raise CodecError(
                    f"{path}.tracks[{ti}]: unknown runtime track tags are read-only and may not be newly created or moved"
                )
            raw_type = bt.get("raw_type_byte")
            effective_type = bt.get("effective_type")
            if not isinstance(raw_type, int) or isinstance(raw_type, bool) or not 0 <= raw_type <= 0xFF:
                raise CodecError(f"{path}.tracks[{ti}]: original input lacks valid raw_type_byte")
            if effective_type != "e":
                raise CodecError(f"{path}.tracks[{ti}]: original input has invalid effective_type")
            track["raw_type_byte"] = raw_type
            track["effective_type"] = effective_type

        # Any negative i32 terminates a runtime track. Preserve the source value
        # for an existing positional track; newly created tracks canonicalize to -1.
        if isinstance(bt, dict) and isinstance(bt.get("terminator"), int) and not isinstance(bt.get("terminator"), bool):
            track["terminator"] = bt["terminator"]

        ekeys = track.get("keys")
        bkeys = bt.get("keys") if isinstance(bt, dict) else None
        if not isinstance(ekeys, list):
            raise CodecError(f"{path}.tracks[{ti}].keys must be a list")
        for ki, key in enumerate(ekeys):
            if not isinstance(key, dict):
                raise CodecError(f"{path}.tracks[{ti}].keys[{ki}] must be an object")
            bk = bkeys[ki] if isinstance(bkeys, list) and ki < len(bkeys) and isinstance(bkeys[ki], dict) else None
            # Unknown mask bits are ignored by the executable; preserving them is
            # lossless and does not alter the editable semantic fields.
            if isinstance(bk, dict) and isinstance(bk.get("extra_mask_bits"), int) and not isinstance(bk.get("extra_mask_bits"), bool):
                key["extra_mask_bits"] = bk["extra_mask_bits"]
    return out


def enrich_folder(edit: dict[str, Any], base: Any,
                  exact: dict[tuple[str, str], list[dict[str, Any]]],
                  by_class: dict[str, list[dict[str, Any]]], path: str) -> dict[str, Any]:
    if not isinstance(edit, dict):
        raise CodecError(f"{path} must be a folder object")
    out = copy.deepcopy(edit)
    erecs = out.get("records")
    brecs = base.get("records") if isinstance(base, dict) else None
    if not isinstance(erecs, list):
        raise CodecError(f"{path}.records must be a list")
    for i, rec in enumerate(erecs):
        if not isinstance(rec, dict):
            raise CodecError(f"{path}.records[{i}] must be an object")
        br = brecs[i] if isinstance(brecs, list) and i < len(brecs) else None
        kind = rec.get("kind")
        if kind == "entity":
            positional = br.get("entity") if isinstance(br, dict) and br.get("kind") == "entity" else None
            rec["entity"] = enrich_entity(rec.get("entity"), positional, exact, by_class,
                                           f"{path}.records[{i}].entity")
        elif kind == "folder":
            positional = br.get("folder") if isinstance(br, dict) and br.get("kind") == "folder" else None
            rec["folder"] = enrich_folder(rec.get("folder"), positional, exact, by_class,
                                           f"{path}.records[{i}].folder")
        elif kind == "movie":
            positional = br if isinstance(br, dict) and br.get("kind") == "movie" else None
            enriched = enrich_movie(rec, positional, f"{path}.records[{i}]")
            erecs[i] = enriched
    # Capacities are internal allocation hints. Preserve originals where possible;
    # encode_folder will raise them when the edited record population requires it.
    if "allocation_counts" not in out:
        if isinstance(base, dict) and isinstance(base.get("allocation_counts"), list):
            out["allocation_counts"] = copy.deepcopy(base["allocation_counts"])
        else:
            out["allocation_counts"] = [0, 0, 0, 0, 0]
    return out


def merge_editable_with_original(
    editable: dict[str, Any], original_full: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    expected_hash = editable.get("source_sha256")
    actual_hash = original_full.get("source_sha256")
    if expected_hash != actual_hash:
        raise CodecError(
            "editable JSON was decoded from a different original input file: "
            f"expected {expected_hash}, got {actual_hash}"
        )

    edit_library = editable["library"]
    library_name = edit_library["name"]
    library_index, base = find_library(original_full.get("libraries"), library_name)

    full = copy.deepcopy(original_full)
    exact, by_class = build_entity_indexes(original_full)

    replacement = copy.deepcopy(edit_library)
    replacement["marker"] = CLARA_MARKER
    replacement["version"] = CLARA_VERSION
    replacement["root_tag"] = base["root_tag"]
    replacement["root_folder"] = enrich_folder(
        replacement["root_folder"],
        base["root_folder"],
        exact,
        by_class,
        f"library[{library_name!r}].root_folder",
    )

    full["libraries"][library_index] = replacement
    return full, library_name


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def count_nested_entities(entities: list[dict[str, Any]]) -> int:
    count = 0

    def walk(value: Any) -> None:
        nonlocal count
        if isinstance(value, dict):
            if "class" in value and "properties" in value and "preamble_hex" in value:
                count += 1
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for entity in entities:
        for prop in entity.get("properties", []):
            walk(prop.get("elements", []))
    return count


def cmd_decode(args: argparse.Namespace) -> None:
    data = args.input.read_bytes()
    full = decode_file(data, args.input.name)
    editable = make_editable_library(full, args.library)
    write_json(args.output, editable)
    _, library = find_library(full["libraries"], args.library)
    entity_count = len(flatten_entities(library["root_folder"]))
    print(f"Decoded library {args.library!r} with {entity_count} top-level entities to {args.output}")


def cmd_encode(args: argparse.Namespace) -> None:
    original = args.input.read_bytes()
    editable = load_editable(args.manifest)
    original_full = decode_file(original, args.input.name)
    merged, library_name = merge_editable_with_original(editable, original_full)
    rebuilt = encode_manifest(original, merged)
    args.output.write_bytes(rebuilt)
    print(
        f"Encoded library {library_name!r} into {args.output} "
        f"using original {args.input} ({len(rebuilt)} bytes)"
    )


def cmd_verify(args: argparse.Namespace) -> None:
    data = args.input.read_bytes()
    manifest = decode_file(data, args.input.name)
    nested = count_nested_entities(manifest["entities"])
    print(f"Verified {args.input}: {len(manifest['entities'])} top-level, {nested} nested entities")


def cmd_list_libraries(args: argparse.Namespace) -> None:
    """Print BLIBCLARA library names in on-disk order, one per line."""
    manifest = decode_file(args.input.read_bytes(), args.input.name)
    for library in manifest["libraries"]:
        print(library["name"])


def cmd_roundtrip(args: argparse.Namespace) -> None:
    original = args.input.read_bytes()
    manifest = decode_file(original, args.input.name)
    rebuilt = encode_manifest(original, manifest)
    args.output.write_bytes(rebuilt)
    if rebuilt != original:
        raise CodecError(f"round-trip differs: input {sha256(original)}, output {sha256(rebuilt)}")
    print(f"Byte-identical round-trip written to {args.output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Schema-aware recursive editor for Minion Rush Clara v12 .blibclara files"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("decode", help="decode one named library to editable JSON")
    command.add_argument("input", type=Path)
    command.add_argument("library", help="exact BLIBCLARA library name")
    command.add_argument("output", type=Path)
    command.set_defaults(func=cmd_decode)

    command = sub.add_parser("encode", help="replace the library named by edited JSON in its exact original .blibclara")
    command.add_argument("input", type=Path)
    command.add_argument("manifest", type=Path)
    command.add_argument("output", type=Path)
    command.set_defaults(func=cmd_encode)

    command = sub.add_parser("verify", help="fully and recursively parse a .blibclara file")
    command.add_argument("input", type=Path)
    command.set_defaults(func=cmd_verify)

    command = sub.add_parser("roundtrip", help="require a byte-identical decode/encode round trip")
    command.add_argument("input", type=Path)
    command.add_argument("output", type=Path)
    command.set_defaults(func=cmd_roundtrip)

    command = sub.add_parser("list-libraries", help="print all library names in a .blibclara file")
    command.add_argument("input", type=Path)
    command.set_defaults(func=cmd_list_libraries)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
        return 0
    except (CodecError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
