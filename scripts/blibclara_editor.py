#!/usr/bin/env python3
"""Clara BLIB Editor v13 — clean JSON, lossless original-backed editing.

PURPOSE
=======
This program decodes, validates, edits, and re-encodes individual Gameloft
Clara ``.blibclara`` files. It is generic: there are no hard-coded references
to designlib, shoplib, or any other specific library.

DECODE / ENCODE WORKFLOW
========================
``decode`` produces one user-facing JSON file containing the editable schema
description, libraries, folders, entities, properties, groups, and multilayers.
Internal serialization details are intentionally omitted from that JSON.

``encode`` requires the exact original ``.blibclara`` together with the edited
JSON. The encoder decodes the original again to recover the internal information
that the clean JSON omits, including exact schema bytes, entity preambles,
original serialized property values, opaque entity bodies, and folder allocation
capacities. No metadata sidecar is produced or used.

The editable JSON stores the SHA-256 of the source file. Encoding fails if it is
paired with a different original ``.blibclara``.

COMMANDS
========
Decode to clean editable JSON::

    python blibclara_editor.py decode INPUT.blibclara OUTPUT.json

Encode edited JSON using its exact original file::

    python blibclara_editor.py encode ORIGINAL.blibclara EDITED.json \
        OUTPUT.blibclara

Other commands::

    python blibclara_editor.py verify INPUT.blibclara
    python blibclara_editor.py roundtrip INPUT.blibclara OUTPUT.blibclara
    python blibclara_editor.py audit-jpk ARCHIVE.jpk [--report REPORT.json]
    python blibclara_editor.py types

A JPK used by this game is a ZIP-compatible archive with a different extension.
``audit-jpk`` verifies and byte-round-trips every ``.blibclara`` entry.

KNOWN BLIBCLARA FORMAT
======================
The analyzed format consists of:

1. an embedded Clara schema;
2. a ``u16`` count of embedded Clara libraries;
3. for each library: marker ``0x1AAA``, version ``12``, library name, root
   ``'f'`` folder record, and a recursive folder tree.

Each folder stores its name, five allocation capacities in ``f/e/g/m/u`` order,
a record count, and tagged records. The allocation order is source-verified from
``FUN_00483b50``: folder objects (0x54 bytes), entity pointer capacity, group
objects (0x20 bytes), movie objects (0x68 bytes), then multilayer objects
(0x34 bytes). Earlier editor versions incorrectly documented the middle two
slots as movie/group; v13 uses the source-verified order when capacities must
grow after edits.

Tagged records:

* ``f`` — recursive folder;
* ``e`` — entity;
* ``m`` — movie/animation record;
* ``g`` — group;
* ``u`` — multilayer.

Entities are size-bounded and contain a name, an opaque preamble, a property
count, and schema-ordered properties. Property arrays use a compact one-byte
header: bits 0–5 hold a short count, bit 6 means named elements, and bit 7 means
an extended ``u16`` count follows.

The executable recognizes Clara property type codes ``0x0002`` through
``0x1000`` as listed by the ``types`` command. Numeric subtypes are signed i8,
signed i16, signed i32, f32, and f64.

FULLY SUPPORTED
===============
* Multiple libraries in one file.
* Recursive folder trees.
* Top-level and recursively nested entities.
* Unknown-class top-level and nested entities are preserved losslessly as opaque,
  read-only records when they already exist in the source file.
* All twelve Clara property type codes recognized by the analyzed executable.
* Named and unnamed arrays, including resizing.
* Signed integers, f32/f64, strings, float vectors, compound string values,
  and optional nested entities.
* Adding, deleting, moving, and reordering folders and entities.
* Group (``g``) and multilayer (``u``) record parsing/writing.
* Reversible preservation of invalid UTF-8 bytes.
* Exact reuse of unchanged serialized values recovered from the original input.
* Recursive post-encode parsing and semantic verification.
* Byte-identical no-op round-trips for all supplied real samples and all 45
  ``.blibclara`` files in the supplied JPK corpus.

CURRENT LIMITATIONS
===================
* Movie (``m``) records remain unsupported because their event payloads use
  multiple subtype-specific grammars that are not fully mapped. The editor
  fails closed when one is encountered.
* Structural correctness does not guarantee game-level semantic correctness.
  The editor cannot prove reference integrity, uniqueness of internal IDs,
  class-specific invariants, or cross-library dependencies.
* The schema is preserved, not edited or regenerated. New editable entities must
  use an existing schema class. Unknown-class opaque entities can be preserved
  from the source but cannot be newly created, renamed, or moved safely through
  the clean-JSON workflow. New known-class content is encoded canonically.
* For a newly added entity, the encoder uses an existing same-class entity from
  the original input as a preamble template when available. Preamble semantics
  are only partially understood, so adding entities remains riskier than
  editing existing values.
* Support targets the Clara version found in the analyzed Minion Rush Windows
  executable. Unknown versions, record tags, or type codes are rejected.

SAFETY MODEL
============
The editor is fail-closed. It validates lengths, counts, integer ranges, float
ranges, array dimensions, entity sizes, schema order, and the source SHA-256.
Encoding re-decodes the exact original file, merges only the clean JSON edits
into that source-derived structure, reparses the generated output, and compares
the decoded result with the requested editable JSON.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import math
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENTITY_TAG = ord("e")
FORMAT = "generic-clara-blib-manifest"
FORMAT_VERSION = 11  # clean editable/full-manifest JSON schema version

# Exhaustive Clara parameter dispatch recovered from the game executable.
# FUN_0047e... handles only these codes; values above 0x1000 fall through.
CLARA_TYPES: dict[int, str] = {
    0x0002: "numeric",
    0x0004: "id_name",
    0x0008: "clara_string",
    0x0010: "shared_string_object",
    0x0020: "nested_entity",
    0x0040: "triple_string_a",
    0x0080: "float_vector",
    0x0100: "clara_string_plus_u32",
    0x0200: "triple_string_b",
    0x0400: "shared_string_a",
    0x0800: "u32_name",
    0x1000: "shared_string_b",
}
NUMERIC_SUBTYPES: dict[int, str] = {
    0: "i8", 1: "i16", 2: "i32", 3: "f32", 4: "f64"
}


class CodecError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str, label: str) -> bytes:
    if not isinstance(text, str):
        raise CodecError(f"{label} must be a base64 string")
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:
        raise CodecError(f"{label} is not valid base64") from exc


def text_decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="surrogateescape")


def text_encode(value: Any, label: str) -> bytes:
    if not isinstance(value, str):
        raise CodecError(f"{label} must be a string")
    raw = value.encode("utf-8", errors="surrogateescape")
    if len(raw) > 0xFFFF:
        raise CodecError(f"{label} exceeds 65535 encoded bytes")
    return raw


class Reader:
    def __init__(self, data: bytes, *, base: int = 0, label: str = "stream"):
        self.data = data
        self.pos = 0
        self.base = base
        self.label = label

    def absolute(self) -> int:
        return self.base + self.pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def require(self, n: int, what: str) -> None:
        if n < 0 or self.pos + n > len(self.data):
            raise CodecError(
                f"{self.label}: truncated {what} at 0x{self.absolute():X}; "
                f"need {n}, have {self.remaining()}"
            )

    def read(self, n: int, what: str = "bytes") -> bytes:
        self.require(n, what)
        out = self.data[self.pos:self.pos+n]
        self.pos += n
        return out

    def skip(self, n: int, what: str = "bytes") -> None:
        self.read(n, what)

    def u8(self, what: str = "u8") -> int:
        return self.read(1, what)[0]

    def u16(self, what: str = "u16") -> int:
        return struct.unpack("<H", self.read(2, what))[0]

    def u32(self, what: str = "u32") -> int:
        return struct.unpack("<I", self.read(4, what))[0]

    def shared_string(self, what: str = "string") -> str:
        n = self.u16(what + " length")
        return text_decode(self.read(n, what))

    def lp_bytes(self, what: str) -> bytes:
        return self.read(self.u16(what + " length"), what)


def pack_string(value: Any, label: str) -> bytes:
    raw = text_encode(value, label)
    return struct.pack("<H", len(raw)) + raw


@dataclass(frozen=True)
class Param:
    index: int
    type_code: int
    name: str
    subtype: int
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Property:
    name: str
    type_index: int


@dataclass(frozen=True)
class ClassDef:
    name: str
    generic: bool
    properties: tuple[Property, ...]


@dataclass(frozen=True)
class Schema:
    header: int
    params: tuple[Param, ...]
    classes: tuple[ClassDef, ...]
    end: int

    @property
    def by_name(self) -> dict[str, ClassDef]:
        return {x.name: x for x in self.classes}


@dataclass(frozen=True)
class Envelope:
    start: int
    end: int
    class_name: str
    name: str
    payload_start: int
    payload: bytes


def parse_schema(data: bytes) -> Schema:
    r = Reader(data, label="schema")
    header = r.u32("header")
    params: list[Param] = []
    for i in range(r.u16("parameter count")):
        code = r.u16(f"parameter {i} type")
        name = r.shared_string(f"parameter {i} name")
        subtype = r.u8(f"parameter {i} subtype")
        aliases = tuple(r.shared_string(f"parameter {i} alias {j}")
                        for j in range(r.u16(f"parameter {i} alias count")))
        if code not in CLARA_TYPES:
            raise CodecError(
                f"parameter {i} {name!r} uses unsupported/unrecognized Clara "
                f"type code 0x{code:X}; this game executable dispatches only "
                + ", ".join(f"0x{x:X}" for x in CLARA_TYPES)
            )
        if code == 2 and subtype not in NUMERIC_SUBTYPES:
            raise CodecError(
                f"parameter {i} {name!r} uses unknown numeric subtype {subtype}; "
                f"known subtypes are {sorted(NUMERIC_SUBTYPES)}"
            )
        params.append(Param(i, code, name, subtype, aliases))
    classes: list[ClassDef] = []
    for i in range(r.u16("class count")):
        name = r.shared_string(f"class {i} name")
        generic = bool(r.u8(f"class {i} generic"))
        props: list[Property] = []
        for j in range(r.u32(f"class {i} property count")):
            pname = r.shared_string(f"class {i} property {j} name")
            tidx = r.u32(f"class {i} property {j} type")
            if tidx >= len(params):
                raise CodecError(f"class {name!r}.{pname} has invalid type index {tidx}")
            props.append(Property(pname, tidx))
        classes.append(ClassDef(name, generic, tuple(props)))
    if len({c.name for c in classes}) != len(classes):
        raise CodecError("duplicate class names in schema")
    return Schema(header, tuple(params), tuple(classes), r.pos)


def parse_envelope_at(r: Reader, schema: Schema, path: str, *, tag_already_read: bool = False) -> tuple[Envelope, dict[str, Any]]:
    start = r.pos - (1 if tag_already_read else 0)
    if not tag_already_read:
        tag = r.u8(path + ".tag")
        if tag != ENTITY_TAG:
            raise CodecError(f"{path}: expected entity tag 'e', got 0x{tag:02X}")
    class_name = r.shared_string(path + ".class")
    body_len = r.u32(path + ".body_size")
    body_start = r.absolute()
    body = r.read(body_len, path + ".body")
    if class_name not in schema.by_name:
        # The game loader treats the declared body size as an opaque skip boundary
        # when the class factory cannot resolve this class. Mirror that behavior
        # instead of rejecting the entire file.
        env = Envelope(r.base + start, r.absolute(), class_name, "", body_start, body)
        item = {
            "class": class_name,
            "opaque": True,
            "opaque_body_base64": b64e(body),
            "source_offset": env.start,
            "source_size": env.end - env.start,
        }
        return env, item
    br = Reader(body, base=body_start, label=path)
    name = br.shared_string(path + ".name")
    env = Envelope(r.base + start, r.absolute(), class_name, name, body_start + br.pos, body[br.pos:])
    item = decode_envelope(env, schema)
    return env, item


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
    start = r.absolute()
    name = r.shared_string(path + ".name")
    allocation_counts = [r.u16(f"{path}.allocation_counts[{i}]") for i in range(5)]
    record_count = r.u16(path + ".record_count")
    records: list[dict[str, Any]] = []
    for i in range(record_count):
        rec_path = f"{path}.records[{i}]"
        tag = r.u8(rec_path + ".tag")
        if tag == ord("f"):
            child = parse_folder(r, schema, rec_path)
            records.append({"kind": "folder", "folder": child})
        elif tag == ENTITY_TAG:
            env, entity = parse_envelope_at(r, schema, rec_path, tag_already_read=True)
            records.append({"kind": "entity", "entity": entity,
                            "source_offset": env.start, "source_size": env.end-env.start})
        elif tag == ord("g"):
            records.append(parse_group_record(r, rec_path))
        elif tag == ord("u"):
            records.append(parse_multilayer_record(r, rec_path))
        elif tag == ord("m"):
            raise CodecError(
                f"{rec_path}: Clara_movie record encountered at 0x{r.absolute()-1:X}. "
                "Its subtype-dependent event grammar is not yet fully mapped; "
                "refusing to guess its boundary."
            )
        else:
            raise CodecError(f"{rec_path}: unknown Clara_folder tag 0x{tag:02X} at 0x{r.absolute()-1:X}")
    return {"name": name, "allocation_counts": allocation_counts,
            "records": records, "source_offset": start,
            "source_size": r.absolute() - start}


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
        if kind not in kind_slot:
            raise CodecError(f"{path}.records[{i}].kind is unsupported: {kind!r}")
        required[kind_slot[kind]] += 1
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
        elif kind == "multilayer": out += encode_multilayer_record(rec, rp)
        else: raise CodecError(f"{rp}.kind is unsupported: {kind!r}")
    return bytes(out)


def flatten_entities(folder: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in folder.get("records", []):
        if rec.get("kind") == "entity": out.append(rec["entity"])
        elif rec.get("kind") == "folder": out.extend(flatten_entities(rec["folder"]))
    return out

def read_array_header(r: Reader, label: str) -> tuple[int, bool]:
    raw = r.u8(label + " array header")
    count = r.u16(label + " extended count") if raw & 0x80 else raw & 0x3F
    return count, bool(raw & 0x40)


def pack_array_header(count: int, named: bool) -> bytes:
    if not isinstance(count, int) or count < 0 or count > 0xFFFF:
        raise CodecError(f"array count {count!r} is outside 0..65535")
    flag = 0x40 if named else 0
    if count <= 0x3F:
        return bytes([flag | count])
    return bytes([flag | 0x80]) + struct.pack("<H", count)


def parse_preamble(r: Reader) -> bytes:
    start = r.pos
    r.skip(12, "transform 1")
    r.skip(16, "transform 2")
    r.skip(12, "transform 3")
    marker = r.shared_string("optional marker")
    if marker:
        r.skip(9, "optional marker values")
    block = r.lp_bytes("auxiliary block")
    if block:
        r.shared_string("auxiliary name")
        r.skip(4, "auxiliary value")
    return r.data[start:r.pos]


def decode_value(r: Reader, param: Param, schema: Schema, path: str) -> Any:
    code, sub = param.type_code, param.subtype
    if code == 2:
        if sub == 0:
            # The runtime accessor sign-extends this field as a C signed char.
            # Never coerce 0/1 to JSON booleans: the schema does not carry a
            # separate Boolean type and that coercion loses exact semantics.
            return struct.unpack("<b", r.read(1, path))[0]
        if sub == 1: return struct.unpack("<h", r.read(2, path))[0]
        if sub == 2: return struct.unpack("<i", r.read(4, path))[0]
        if sub == 3: return struct.unpack("<f", r.read(4, path))[0]
        if sub == 4: return struct.unpack("<d", r.read(8, path))[0]
        raise CodecError(f"{path}: unsupported numeric subtype {sub}")
    if code == 4:
        return {"id": r.u32(path + ".id"), "name": r.shared_string(path + ".name")}
    if code in (8, 16):
        return text_decode(r.lp_bytes(path))
    if code == 32:
        present = r.u8(path + ".present")
        if present == 0:
            return None
        # The game tests only zero versus nonzero here; preserve compatibility
        # with noncanonical truthy bytes while emitting canonical 0/1 on encode.
        tag = r.u8(path + ".tag")
        if tag != ENTITY_TAG:
            raise CodecError(f"{path}: expected nested entity tag 'e', got 0x{tag:02X}")
        class_name = r.shared_string(path + ".class")
        size = r.u32(path + ".size")
        body_start = r.absolute()
        body = r.read(size, path + ".body")
        cls = schema.by_name.get(class_name)
        if cls is None:
            # As in the executable, an unresolved nested class is skipped using
            # its declared body size. Keep it opaque/read-only so it round-trips.
            return {"class": class_name, "opaque": True,
                    "opaque_body_base64": b64e(body)}
        br = Reader(body, base=body_start, label=path)
        name = br.shared_string(path + ".name")
        entity = decode_entity_payload(br, cls, schema, class_name, name, path)
        if br.remaining():
            raise CodecError(f"{path}: {br.remaining()} trailing nested bytes")
        return entity
    if code in (64, 512):
        return [r.shared_string(f"{path}[{i}]") for i in range(3)]
    if code == 128:
        return [struct.unpack("<f", r.read(4, path))[0]
                for _ in range(r.u8(path + ".component_count"))]
    if code == 256:
        return {"text": text_decode(r.lp_bytes(path + ".text")),
                "trailing_u32": r.u32(path + ".trailing")}
    if code in (1024, 4096):
        return r.shared_string(path)
    if code == 2048:
        return {"value": r.u32(path + ".value"), "name": r.shared_string(path + ".name")}
    raise CodecError(f"{path}: unsupported Clara type code 0x{code:X}")


def decode_entity_payload(r: Reader, cls: ClassDef, schema: Schema,
                          class_name: str, name: str, path: str) -> dict[str, Any]:
    preamble = parse_preamble(r)
    count = r.u16(path + ".property_count")
    if count != len(cls.properties):
        raise CodecError(f"{path}: property count {count} != schema count {len(cls.properties)}")
    props: list[dict[str, Any]] = []
    for prop in cls.properties:
        param = schema.params[prop.type_index]
        n, named = read_array_header(r, path + "." + prop.name)
        elements = []
        for i in range(n):
            ename = r.shared_string(f"{path}.{prop.name}[{i}].element_name") if named else None
            value_start = r.pos
            value = decode_value(r, param, schema, f"{path}.{prop.name}[{i}]")
            value_raw = r.data[value_start:r.pos]
            item = {
                "value": value,
                "original_value": copy.deepcopy(value),
                "raw_base64": b64e(value_raw),
            }
            if named:
                item["name"] = ename
                item["original_name"] = ename
            elements.append(item)
        props.append({
            "name": prop.name,
            "type_index": prop.type_index,
            "type_code": param.type_code,
            "subtype": param.subtype,
            "named_elements": named,
            "elements": elements,
        })
    return {"class": class_name, "name": name, "preamble_hex": preamble.hex(), "properties": props}


def decode_envelope(env: Envelope, schema: Schema) -> dict[str, Any]:
    cls = schema.by_name.get(env.class_name)
    if cls is None:
        raise CodecError(f"top-level entity {env.name!r} uses unknown class {env.class_name!r}")
    r = Reader(env.payload, base=env.payload_start, label=env.name or env.class_name)
    item = decode_entity_payload(r, cls, schema, env.class_name, env.name, env.name or env.class_name)
    if r.remaining():
        raise CodecError(f"{env.name!r}: {r.remaining()} trailing payload bytes")
    item["source_offset"] = env.start
    item["source_size"] = env.end - env.start
    return item


def schema_json(schema: Schema) -> dict[str, Any]:
    return {
        "header": schema.header,
        "parameters": [
            {"index": p.index, "type_code": p.type_code,
             "type_name": CLARA_TYPES[p.type_code], "name": p.name,
             "subtype": p.subtype,
             "subtype_name": NUMERIC_SUBTYPES.get(p.subtype) if p.type_code == 2 else None,
             "aliases": list(p.aliases)} for p in schema.params
        ],
        "classes": [
            {"name": c.name, "generic": c.generic,
             "properties": [{"name": p.name, "type_index": p.type_index} for p in c.properties]}
            for c in schema.classes
        ],
    }


def decode_file(data: bytes, source_name: str) -> dict[str, Any]:
    schema = parse_schema(data)
    r = Reader(data[schema.end:], base=schema.end, label="ClaraFile")
    library_count = r.u16("library_count")
    libraries: list[dict[str, Any]] = []
    all_entities: list[dict[str, Any]] = []
    for i in range(library_count):
        path = f"libraries[{i}]"
        marker = r.u16(path + ".marker")
        version = r.u16(path + ".version")
        if marker != 0x1AAA:
            raise CodecError(f"{path}: expected marker 0x1AAA, got 0x{marker:04X}")
        if version != 12:
            raise CodecError(f"{path}: unsupported Clara library version {version}")
        name = r.shared_string(path + ".name")
        tag = r.u8(path + ".root_tag")
        if tag != ord("f"):
            raise CodecError(f"{path}: expected root folder tag 'f', got 0x{tag:02X}")
        root = parse_folder(r, schema, path + ".root_folder")
        libraries.append({"marker": marker, "version": version, "name": name,
                          "root_folder": root})
        all_entities.extend(flatten_entities(root))
    if r.remaining():
        raise CodecError(f"ClaraFile: {r.remaining()} trailing bytes at 0x{r.absolute():X}")
    return {
        "format": FORMAT, "format_version": FORMAT_VERSION,
        "source_file": source_name, "source_sha256": sha256(data),
        "source_size": len(data), "schema_raw_base64": b64e(data[:schema.end]),
        "schema": schema_json(schema), "libraries": libraries,
        "entities": all_entities,
        "editing_notes": {
            "library_tree": "Edit libraries[].root_folder.records recursively; library and record order are preserved.",
            "entities_index": "The top-level entities list is read-only convenience data; encoding uses libraries only.",
            "allocation_counts": "Five capacities are stored in f/e/g/m/u order and are automatically raised when records are added.",
            "add_entity": "Add a {kind:'entity', entity:{...}} record to any folder.",
            "delete_entity": "Remove its entity record from the containing folder.",
            "groups_multilayers": "g and u records are fully decoded and editable.",
            "movies": "m records fail closed until every event subtype is verified."
        },
    }

def finite_number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CodecError(f"{label} must be numeric")
    out = float(value)
    if not math.isfinite(out):
        raise CodecError(f"{label} must be finite")
    return out

def pack_f32(value: Any, label: str) -> bytes:
    out = finite_number(value, label)
    try:
        packed = struct.pack("<f", out)
    except (OverflowError, struct.error) as exc:
        raise CodecError(f"{label} is outside finite f32 range") from exc
    # Python may encode an overflowing finite input as infinity on some builds.
    if not math.isfinite(struct.unpack("<f", packed)[0]):
        raise CodecError(f"{label} is outside finite f32 range")
    return packed

def checked_count(count: int, minimum_bytes: int, remaining: int, label: str) -> int:
    if count < 0:
        raise CodecError(f"{label} is negative")
    if minimum_bytes and count > remaining // minimum_bytes:
        raise CodecError(f"{label}={count} cannot fit in the remaining {remaining} bytes")
    return count


def encode_value(value: Any, param: Param, schema: Schema, path: str) -> bytes:
    code, sub = param.type_code, param.subtype
    if code == 2:
        if sub == 0:
            if isinstance(value, int) and not isinstance(value, bool) and -128 <= value <= 127:
                return struct.pack("<b", value)
            raise CodecError(f"{path} must be signed i8 (-128..127); booleans are not accepted")
        if sub == 1:
            if isinstance(value, int) and not isinstance(value, bool) and -0x8000 <= value <= 0x7FFF:
                return struct.pack("<h", value)
            raise CodecError(f"{path} must be signed i16")
        if sub == 2:
            if isinstance(value, int) and not isinstance(value, bool) and -(1<<31) <= value < (1<<31):
                return struct.pack("<i", value)
            raise CodecError(f"{path} must be i32")
        if sub == 3: return pack_f32(value, path)
        if sub == 4: return struct.pack("<d", finite_number(value, path))
        raise CodecError(f"{path}: unsupported numeric subtype {sub}")
    if code == 4:
        if not isinstance(value, dict) or not isinstance(value.get("id"), int) or isinstance(value.get("id"), bool):
            raise CodecError(f"{path} must be {{id,name}}")
        if not 0 <= value["id"] <= 0xFFFFFFFF:
            raise CodecError(f"{path}.id outside u32")
        return struct.pack("<I", value["id"]) + pack_string(value.get("name"), path + ".name")
    if code in (8, 16):
        return pack_string(value, path)
    if code == 32:
        if value is None: return b"\x00"
        if not isinstance(value, dict):
            raise CodecError(f"{path} must be a nested entity object or null")
        if value.get("opaque") is True:
            class_name = value.get("class")
            if not isinstance(class_name, str):
                raise CodecError(f"{path}.class must be a string")
            body = b64d(value.get("opaque_body_base64"), path + ".opaque_body_base64")
        else:
            body = encode_entity_body(value, schema, path)
            class_name = value.get("class")
        return b"\x01" + bytes([ENTITY_TAG]) + pack_string(class_name, path + ".class") + struct.pack("<I", len(body)) + body
    if code in (64, 512):
        if not isinstance(value, list) or len(value) != 3:
            raise CodecError(f"{path} must be a three-string list")
        return b"".join(pack_string(x, f"{path}[{i}]") for i, x in enumerate(value))
    if code == 128:
        if not isinstance(value, list) or len(value) > 255:
            raise CodecError(f"{path} must be a list of at most 255 floats")
        return bytes([len(value)]) + b"".join(pack_f32(x, f"{path}[{i}]") for i, x in enumerate(value))
    if code == 256:
        if (not isinstance(value, dict) or
                not isinstance(value.get("trailing_u32"), int) or
                isinstance(value.get("trailing_u32"), bool)):
            raise CodecError(f"{path} must contain text and trailing_u32")
        if not 0 <= value["trailing_u32"] <= 0xFFFFFFFF:
            raise CodecError(f"{path}.trailing_u32 outside u32")
        return pack_string(value.get("text"), path + ".text") + struct.pack("<I", value["trailing_u32"])
    if code in (1024, 4096):
        return pack_string(value, path)
    if code == 2048:
        if not isinstance(value, dict) or not isinstance(value.get("value"), int) or isinstance(value.get("value"), bool):
            raise CodecError(f"{path} must contain value and name")
        if not 0 <= value["value"] <= 0xFFFFFFFF:
            raise CodecError(f"{path}.value outside u32")
        return struct.pack("<I", value["value"]) + pack_string(value.get("name"), path + ".name")
    raise CodecError(f"{path}: unsupported Clara type code 0x{code:X}")


def validate_entity_shape(item: Any, schema: Schema, path: str) -> tuple[ClassDef, list[dict[str, Any]]]:
    if not isinstance(item, dict): raise CodecError(f"{path} must be an object")
    class_name = item.get("class")
    name = item.get("name")
    if not isinstance(class_name, str) or class_name not in schema.by_name:
        raise CodecError(f"{path}.class is unknown")
    if not isinstance(name, str): raise CodecError(f"{path}.name must be a string")
    props = item.get("properties")
    if not isinstance(props, list): raise CodecError(f"{path}.properties must be a list")
    cls = schema.by_name[class_name]
    if len(props) != len(cls.properties):
        raise CodecError(f"{path}: property count {len(props)} != {len(cls.properties)}")
    for i, (got, expected) in enumerate(zip(props, cls.properties)):
        if not isinstance(got, dict) or got.get("name") != expected.name or got.get("type_index") != expected.type_index:
            raise CodecError(f"{path}.properties[{i}] does not match schema property {expected.name!r}")
    return cls, props


def decode_single_raw(raw: bytes, param: Param, schema: Schema, path: str) -> Any:
    r = Reader(raw, label=path)
    value = decode_value(r, param, schema, path)
    if r.remaining():
        raise CodecError(f"{path}: raw value has {r.remaining()} trailing bytes")
    return value


def semantic_equal(a: Any, b: Any) -> bool:
    """JSON-like equality that also treats two NaNs as equal."""
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(semantic_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(semantic_equal(a[k], b[k]) for k in a)
    return a == b

def json_semantic(value: Any) -> Any:
    """Remove provenance-only fields for post-encode semantic comparison."""
    if isinstance(value, list):
        return [json_semantic(x) for x in value]
    if isinstance(value, dict):
        return {k: json_semantic(v) for k, v in value.items()
                if k not in {"original_value", "original_name", "raw_base64",
                             "source_offset", "source_size"}}
    return value



def encode_entity_payload(item: dict[str, Any], schema: Schema, path: str) -> bytes:
    cls, props = validate_entity_shape(item, schema, path)
    try:
        preamble = bytes.fromhex(item.get("preamble_hex", ""))
    except ValueError as exc:
        raise CodecError(f"{path}.preamble_hex is invalid") from exc
    # Validate preamble independently so malformed edits fail before output.
    pr = Reader(preamble, label=path + ".preamble")
    parse_preamble(pr)
    if pr.remaining(): raise CodecError(f"{path}.preamble_hex has trailing bytes")
    out = bytearray(preamble)
    out += struct.pack("<H", len(cls.properties))
    for pitem, pdef in zip(props, cls.properties):
        elements = pitem.get("elements")
        named = pitem.get("named_elements")
        if not isinstance(elements, list) or not isinstance(named, bool):
            raise CodecError(f"{path}.{pdef.name}: invalid array representation")
        out += pack_array_header(len(elements), named)
        param = schema.params[pdef.type_index]
        for i, element in enumerate(elements):
            if not isinstance(element, dict) or "value" not in element:
                raise CodecError(f"{path}.{pdef.name}[{i}] must contain value")
            if named:
                out += pack_string(element.get("name"), f"{path}.{pdef.name}[{i}].name")
            unchanged_value = "original_value" in element and semantic_equal(element.get("value"), element.get("original_value"))
            unchanged_name = (not named) or element.get("name") == element.get("original_name")
            if unchanged_value and unchanged_name and isinstance(element.get("raw_base64"), str):
                raw = b64d(element["raw_base64"], f"{path}.{pdef.name}[{i}].raw_base64")
                decoded_raw = decode_single_raw(raw, param, schema, f"{path}.{pdef.name}[{i}].raw")
                if not semantic_equal(decoded_raw, element.get("original_value")):
                    raise CodecError(f"{path}.{pdef.name}[{i}]: raw_base64 does not encode original_value")
                out += raw
            else:
                out += encode_value(element["value"], param, schema, f"{path}.{pdef.name}[{i}]")
    return bytes(out)


def encode_entity_body(item: dict[str, Any], schema: Schema, path: str) -> bytes:
    return pack_string(item.get("name"), path + ".name") + encode_entity_payload(item, schema, path)


def encode_envelope(item: dict[str, Any], schema: Schema, path: str) -> bytes:
    if not isinstance(item, dict):
        raise CodecError(f"{path} must be an entity object")
    class_name = item.get("class")
    if not isinstance(class_name, str):
        raise CodecError(f"{path}.class must be a string")
    if item.get("opaque") is True:
        body = b64d(item.get("opaque_body_base64"), path + ".opaque_body_base64")
    else:
        body = encode_entity_body(item, schema, path)
    return bytes([ENTITY_TAG]) + pack_string(class_name, path + ".class") + struct.pack("<I", len(body)) + body



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
        if marker != 0x1AAA:
            raise CodecError(f"{path}.marker must remain 0x1AAA")
        if version != 12:
            raise CodecError(f"{path}.version must remain 12")
        out += struct.pack("<HH", marker, version)
        out += pack_string(lib.get("name"), path + ".name")
        out += b"f" + encode_folder(lib.get("root_folder"), schema, path + ".root_folder")
    rebuilt = bytes(out)
    check = decode_file(rebuilt, "verification.blibclara")
    expected = json_semantic(libraries)
    actual = json_semantic(check["libraries"])
    def strip_capacities(v: Any) -> Any:
        if isinstance(v, list):
            return [strip_capacities(x) for x in v]
        if isinstance(v, dict):
            return {k: strip_capacities(x) for k, x in v.items() if k != "allocation_counts"}
        return v
    if not semantic_equal(strip_capacities(expected), strip_capacities(actual)):
        raise CodecError("post-encode semantic verification failed: decoded library tree differs from requested JSON")
    return rebuilt


USER_FORMAT = "generic-clara-blib-editable"


def strip_internal_fields(value: Any) -> Any:
    """Return the clean, user-editable view of a decoded manifest."""
    if isinstance(value, list):
        return [strip_internal_fields(x) for x in value]
    if isinstance(value, dict):
        omitted = {
            "raw_base64", "original_value", "original_name",
            "source_offset", "source_size", "preamble_hex",
            "allocation_counts", "opaque_body_base64",
        }
        return {k: strip_internal_fields(v) for k, v in value.items() if k not in omitted}
    return value


def make_editable_manifest(full: dict[str, Any]) -> dict[str, Any]:
    libraries = strip_internal_fields(full["libraries"])
    for lib in libraries:
        if isinstance(lib, dict):
            lib.pop("marker", None)
            lib.pop("version", None)
    return {
        "format": USER_FORMAT,
        "format_version": FORMAT_VERSION,
        "source_sha256": full["source_sha256"],
        "schema": strip_internal_fields(full["schema"]),
        "libraries": libraries,
        "editing_notes": {
            "primary_tree": "Edit libraries[].root_folder.records recursively.",
            "schema": "Schema descriptions are informational and are not regenerated.",
            "source": "Encoding requires the exact original .blibclara identified by source_sha256.",
            "new_entities": "New entities must use an existing schema class. A same-class entity from the original input supplies the preamble template.",
            "opaque_entities": "Unknown-class entities are preserved losslessly but are read-only and cannot be moved or created.",
            "movies": "Movie ('m') records remain unsupported and fail closed.",
        },
    }


def load_editable(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or obj.get("format") != USER_FORMAT or obj.get("format_version") != FORMAT_VERSION:
        raise CodecError("unsupported editable JSON format/version")
    source_hash = obj.get("source_sha256")
    if (not isinstance(source_hash, str) or len(source_hash) != 64 or
            any(ch not in "0123456789abcdef" for ch in source_hash)):
        raise CodecError("editable JSON source_sha256 must be a lowercase SHA-256 hex string")
    if not isinstance(obj.get("libraries"), list):
        raise CodecError("editable JSON libraries must be a list")
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
    return candidates[0] if candidates else None


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
            be = belems[ei] if isinstance(belems, list) and ei < len(belems) else None
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
    # Capacities are internal allocation hints. Preserve originals where possible;
    # encode_folder will raise them when the edited record population requires it.
    if "allocation_counts" not in out:
        if isinstance(base, dict) and isinstance(base.get("allocation_counts"), list):
            out["allocation_counts"] = copy.deepcopy(base["allocation_counts"])
        else:
            out["allocation_counts"] = [0, 0, 0, 0, 0]
    return out


def merge_editable_with_original(editable: dict[str, Any], original_full: dict[str, Any]) -> dict[str, Any]:
    expected_hash = editable.get("source_sha256")
    actual_hash = original_full.get("source_sha256")
    if expected_hash != actual_hash:
        raise CodecError(
            "editable JSON was decoded from a different original input file: "
            f"expected {expected_hash}, got {actual_hash}"
        )
    full = copy.deepcopy(original_full)
    exact, by_class = build_entity_indexes(full)
    elibs = editable.get("libraries")
    blibs = full.get("libraries")
    if not isinstance(elibs, list) or len(elibs) > 0xFFFF:
        raise CodecError("editable libraries must be a list of at most 65535 entries")
    merged_libs: list[dict[str, Any]] = []
    for i, lib in enumerate(elibs):
        if not isinstance(lib, dict):
            raise CodecError(f"libraries[{i}] must be an object")
        base = blibs[i] if isinstance(blibs, list) and i < len(blibs) else None
        out = copy.deepcopy(lib)
        # Container marker/version are format contracts, not user-editable data.
        out["marker"] = base.get("marker", 0x1AAA) if isinstance(base, dict) else 0x1AAA
        out["version"] = base.get("version", 12) if isinstance(base, dict) else 12
        out["root_folder"] = enrich_folder(
            out.get("root_folder"), base.get("root_folder") if isinstance(base, dict) else None,
            exact, by_class, f"libraries[{i}].root_folder"
        )
        merged_libs.append(out)
    full["libraries"] = merged_libs
    full["entities"] = []
    for lib in merged_libs:
        full["entities"].extend(flatten_entities(lib["root_folder"]))
    return full

def cmd_decode(args: argparse.Namespace) -> None:
    data = args.input.read_bytes()
    full = decode_file(data, args.input.name)
    editable = make_editable_manifest(full)
    args.output.write_text(
        json.dumps(editable, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Decoded {len(full['libraries'])} libraries and {len(full['entities'])} entities "
        f"to {args.output}"
    )


def cmd_encode(args: argparse.Namespace) -> None:
    original = args.input.read_bytes()
    editable = load_editable(args.manifest)
    original_full = decode_file(original, args.input.name)
    merged = merge_editable_with_original(editable, original_full)
    rebuilt = encode_manifest(original, merged)
    args.output.write_bytes(rebuilt)
    print(f"Encoded {len(rebuilt)} bytes to {args.output} using original {args.input}")


def cmd_verify(args: argparse.Namespace) -> None:
    data = args.input.read_bytes()
    manifest = decode_file(data, args.input.name)
    nested = 0
    def walk(v: Any) -> None:
        nonlocal nested
        if isinstance(v, dict):
            if "class" in v and "properties" in v and "preamble_hex" in v: nested += 1
            for x in v.values(): walk(x)
        elif isinstance(v, list):
            for x in v: walk(x)
    for entity in manifest["entities"]:
        for prop in entity.get("properties", []):
            walk(prop.get("elements", []))
    print(f"Verified {args.input}: {len(manifest['entities'])} top-level, {nested} nested entities")


def cmd_roundtrip(args: argparse.Namespace) -> None:
    original = args.input.read_bytes()
    manifest = decode_file(original, args.input.name)
    rebuilt = encode_manifest(original, manifest)
    args.output.write_bytes(rebuilt)
    if rebuilt != original:
        raise CodecError(f"round-trip differs: input {sha256(original)}, output {sha256(rebuilt)}")
    print(f"Byte-identical round-trip written to {args.output}")



def cmd_audit_jpk(args: argparse.Namespace) -> None:
    try:
        archive = zipfile.ZipFile(args.input, "r")
    except zipfile.BadZipFile as exc:
        raise CodecError(f"{args.input} is not a valid ZIP/JPK archive") from exc

    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    with archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".blibclara")]
        if not names:
            raise CodecError("archive contains no .blibclara entries")
        for index, name in enumerate(names, 1):
            data = archive.read(name)
            try:
                manifest = decode_file(data, name)
                rebuilt = encode_manifest(data, manifest)
                identical = rebuilt == data
                if not identical:
                    raise CodecError(
                        f"round-trip differs: input {sha256(data)}, output {sha256(rebuilt)}"
                    )
                row = {
                    "entry": name,
                    "size": len(data),
                    "libraries": len(manifest["libraries"]),
                    "entities": len(manifest["entities"]),
                    "byte_identical": True,
                }
                print(
                    f"[{index}/{len(names)}] OK {name}: "
                    f"{row['libraries']} libraries, {row['entities']} entities"
                )
            except Exception as exc:
                row = {
                    "entry": name,
                    "size": len(data),
                    "byte_identical": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failures.append(name)
                print(f"[{index}/{len(names)}] FAIL {name}: {row['error']}")
            rows.append(row)

    if args.report is not None:
        args.report.write_text(json.dumps(rows, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    if failures:
        raise CodecError(f"{len(failures)} of {len(rows)} entries failed: {', '.join(failures)}")
    print(f"Audited {len(rows)} .blibclara entries; all round-trips were byte-identical")


def cmd_types(args: argparse.Namespace) -> None:
    print("Clara parameter types supported by this executable/editor:")
    for code, name in CLARA_TYPES.items():
        suffix = " (subtypes: " + ", ".join(
            f"{k}={v}" for k, v in NUMERIC_SUBTYPES.items()
        ) + ")" if code == 2 else ""
        print(f"  0x{code:04X}  {name}{suffix}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generic recursive editor for individual .blibclara files")
    sub = p.add_subparsers(dest="command", required=True)
    pt = sub.add_parser("types", help="list all Clara type codes implemented by this game build")
    pt.set_defaults(func=cmd_types)
    q = sub.add_parser("decode", help="decode one .blibclara to clean editable JSON")
    q.add_argument("input", type=Path)
    q.add_argument("output", type=Path)
    q.set_defaults(func=cmd_decode)
    q = sub.add_parser("encode", help="encode clean edited JSON using its exact original .blibclara")
    q.add_argument("input", type=Path)
    q.add_argument("manifest", type=Path)
    q.add_argument("output", type=Path)
    q.set_defaults(func=cmd_encode)
    q = sub.add_parser("verify", help="fully and recursively parse a .blibclara file")
    q.add_argument("input", type=Path); q.set_defaults(func=cmd_verify)
    q = sub.add_parser("roundtrip", help="decode+encode and require byte-identical output")
    q.add_argument("input", type=Path); q.add_argument("output", type=Path); q.set_defaults(func=cmd_roundtrip)
    q = sub.add_parser("audit-jpk", help="verify and byte-round-trip every .blibclara entry in a ZIP/JPK archive")
    q.add_argument("input", type=Path)
    q.add_argument("--report", type=Path, help="optional JSON report path")
    q.set_defaults(func=cmd_audit_jpk)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except (CodecError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
