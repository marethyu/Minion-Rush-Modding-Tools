#!/usr/bin/env python3
"""Clara BCLARA Editor v5 — structural and schema-aware editing for ``.bclara``.

SCOPE
=====
This tool targets the standalone Clara v12 library format recovered from Minion
Rush. It decodes and rebuilds folders, entities, groups, movies, and multilayers.
Entity editing has two modes:

* raw mode exposes the bytes after the entity name as ``payload_hex``;
* semantic mode uses a compatible BLIBCLARA supplied with ``--schema-from`` and
  decodes the same entity/property grammar through ``clara_common.py``.

The tool is intentionally strict about malformed sizes/counts and invalid edited
values, but it mirrors the recovered runtime where the runtime is permissive: unknown
one-byte folder tags are consumed without payload, unknown movie track tags retain
the zero-initialized effective ``e`` behavior, and unused movie mask bits are ignored
and preserved rather than rejected.

DEPENDENCY
==========
``clara_common.py`` must be importable from the same directory or ``PYTHONPATH``.

BCLARA ON-DISK FORMAT
=====================
All integers and floats are little-endian. ``String16`` is ``u16 byte_length``
followed by exactly that many bytes; there is no NUL terminator or alignment
padding.

Top level::

    u16 marker                    # 0x1AAA
    u16 version                   # 12
    String16 library_name
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

The five capacities are allocation hints for immediate ``f/e/g/m/u`` children.
Decoded JSON preserves the original five values. On encode they are retained exactly
when still large enough; structural edits only grow the affected capacities as needed.

The recovered runtime reads the root tag but does not branch on its value before
calling the folder loader. The editor therefore preserves ``root_tag`` as a raw byte
instead of requiring it to be ``'f'``.

Record grammar::

    'f' Folder

    'e' String16 class_name
        u32 body_size
        body[body_size]

        for a resolved/known class, body begins with::
            String16 object_name
            entity_payload...

        for an unresolved class, the runtime skips body[body_size] opaquely

    'g' String16 name
        u32 item_count
        String16 items[item_count]

    'm' ClaraMovie

    'u' String16 name
        u32 layer_count
        String16 layers[layer_count]
        u32 column_count
        String16 columns[column_count]
        u32 matrix[layer_count][column_count]

    other tag bytes
        no payload is consumed by the recovered runtime dispatch; the editor
        preserves such one-byte records as ``kind: "unknown"``

ENTITY SEMANTICS
================
Standalone BCLARA does not embed a schema, but its entity payload uses the same
Clara v12 entity/property grammar as BLIBCLARA. ``clara_common.py`` owns that
shared grammar.

Without ``--schema-from``, an entity is represented by ``class``, ``name``, and
``payload_hex``. With a compatible BLIBCLARA schema, known classes are decoded to
named properties, including arrays and nested entities. When a supplied schema does
not contain the class, the recovered executable skips the complete declared entity
body without parsing an object name; semantic mode now mirrors that behavior and
preserves the whole body as opaque Base64.

Semantic JSON records the SHA-256 of the external BLIBCLARA schema block. Encode
requires a schema with that exact hash. The common entity preamble and unchanged
serialized property bytes are retained in semantic JSON so no-op schema-aware
round trips can remain byte-identical.

CLARA MOVIE GRAMMAR
===================
ClaraMovie::

    String16 name
    u32 fps
    u32 start_time
    u32 end_time
    u8 flag
    u32 track_count
    MovieTrack tracks[track_count]

Track tags recognized by the recovered executable are ``e/x/s/m/p/b``. Only an
``e`` track stores a target String16. The runtime track object is zero-initialized;
an unrecognized tag therefore retains the default effective type ``e`` and consumes
the same target String16. The editor preserves the original unknown tag byte while
representing its effective runtime behavior explicitly.

Each track ends when a signed i32 key time is negative. The exact negative terminator
read from disk is preserved and re-emitted; a newly created track defaults to ``-1``.

Movie key::

    i32 time
    u16 mask
    if mask & 0x01: u8 interpolation + 3*f32   # position
    if mask & 0x02: u8 interpolation + 4*f32   # rotation quaternion
    if mask & 0x04: u8 interpolation + 3*f32   # scale
    if mask & 0x08: String16 + u32 + u32 + u8 # semantics unresolved
    if mask & 0x10: String16 + String16        # semantics unresolved

Editable JSON regenerates the known movie key-mask bits from semantic fields. Any
unknown mask bits are preserved because the recovered runtime simply ignores them.

COMMANDS
========
Raw structural mode::

    python bclara_editor.py decode INPUT.bclara OUTPUT.json
    python bclara_editor.py encode INPUT.json OUTPUT.bclara
    python bclara_editor.py verify INPUT.bclara
    python bclara_editor.py roundtrip INPUT.bclara

Schema-aware entity mode::

    python bclara_editor.py decode INPUT.bclara OUTPUT.json --schema-from designlib.blibclara
    python bclara_editor.py encode INPUT.json OUTPUT.bclara --schema-from designlib.blibclara
    python bclara_editor.py verify INPUT.bclara --schema-from designlib.blibclara
    python bclara_editor.py roundtrip INPUT.bclara --schema-from designlib.blibclara

KNOWN LIMITATIONS
=================
* Only the recovered Minion Rush Clara marker ``0x1AAA`` and version ``12`` are
  supported. Other Clara revisions are rejected.
* Semantic entity decoding requires an external BLIBCLARA schema from the same
  compatible game build. The editor verifies the schema hash on re-encode, but
  cannot independently prove that an arbitrarily supplied schema is the one the
  game originally used for a BCLARA file.
* In raw mode all entity property meaning is the caller's responsibility. In
  semantic mode a class absent from the supplied schema is intentionally opaque and
  read-only at the semantic level, matching the executable's size-bounded skip.
* The common entity preamble is structurally parsed but not fully semantically
  understood. Semantic mode preserves it rather than attempting to regenerate it
  from higher-level concepts.
* Movie position, quaternion rotation, and scale are understood structurally.
  Exact semantics of track kinds other than the well-observed ``e`` path, the
  movie ``flag``, and payloads ``0x08``/``0x10`` remain partly unresolved.
* The exact canonical output choices of Gameloft's original authoring-side writer
  have not been recovered. This editor mirrors the executable loader and preserves
  loader-visible noncanonical forms that are known (folder capacities, arbitrary
  negative movie terminators, ignored movie-mask bits, unknown one-byte record tags,
  and unknown movie-track tags). Semantic property-array headers for edited arrays
  are still emitted in the shortest valid encoding accepted by the loader.
* Structural validity does not prove game-level reference integrity, uniqueness
  constraints, class-specific invariants, or cross-resource dependencies.
* There is deliberately no compatibility layer for older editable JSON formats;
  only the current ``format_version`` is accepted.
"""
from __future__ import annotations

import argparse
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

BClaraError = clara.ClaraError
Reader = clara.Reader
pack_string = clara.pack_string
require_int = clara.require_int
require_float = clara.require_float
decode_hex = clara.decode_hex
require_fields = clara.require_fields
checked_count = clara.checked_count

MARKER = 0x1AAA
VERSION = 12
FORMAT = "clara-bclara-editable"
FORMAT_VERSION = 4
MOVIE_TRACK_TYPES = ("e", "x", "s", "m", "p", "b")
MOVIE_TRACK_TYPE_SET = frozenset(MOVIE_TRACK_TYPES)
KNOWN_MOVIE_MASK = 0x1F
RECORD_KIND_ORDER = ("folder", "entity", "group", "movie", "multilayer")


def load_external_schema(path: Path) -> tuple[clara.Schema, str]:
    data = path.read_bytes()
    schema = clara.parse_schema(data)
    if len(data) < schema.end + 2:
        raise BClaraError(f"{path}: file ends immediately after schema; expected BLIBCLARA library_count")
    library_count = struct.unpack_from("<H", data, schema.end)[0]
    if library_count == 0:
        raise BClaraError(f"{path}: embedded schema has zero BLIBCLARA libraries")
    if len(data) < schema.end + 6:
        raise BClaraError(f"{path}: truncated first BLIBCLARA library header")
    marker, version = struct.unpack_from("<HH", data, schema.end + 2)
    if marker != MARKER or version != VERSION:
        raise BClaraError(
            f"{path}: not a compatible Clara v{VERSION} BLIBCLARA after its schema "
            f"(marker=0x{marker:04X}, version={version})"
        )
    return schema, clara.sha256(data[:schema.end])


def parse_vec_payload(r: Reader, count: int, path: str) -> dict[str, Any]:
    interpolation = r.u8(path + ".interpolation")
    value = [r.f32(f"{path}.value[{i}]") for i in range(count)]
    return {"interpolation": interpolation, "value": value}

def parse_movie(r: Reader, path: str) -> dict[str, Any]:
    name = r.string(path + ".name")
    fps = r.u32(path + ".fps")
    if fps == 0:
        # The recovered runtime immediately computes 1000 / fps.
        raise BClaraError(f"{path}.fps must be nonzero")
    start_time = r.u32(path + ".start_time")
    end_time = r.u32(path + ".end_time")
    flag = r.u8(path + ".flag")
    track_count = checked_count(
        r.u32(path + ".track_count"), 5, r.remaining(), path + ".track_count"
    )

    tracks: list[dict[str, Any]] = []
    for ti in range(track_count):
        tp = f"{path}.tracks[{ti}]"
        raw_type = r.u8(tp + ".type")
        track_type = chr(raw_type) if raw_type in map(ord, MOVIE_TRACK_TYPES) else "unknown"
        if track_type == "unknown":
            # FUN_00487d70 zero-initializes the track type to 0 (the 'e' behavior),
            # and FUN_00484840 has no default switch case. Preserve the original
            # unrecognized byte but parse the target exactly as the runtime does.
            track: dict[str, Any] = {
                "type": "unknown",
                "raw_type_byte": raw_type,
                "effective_type": "e",
            }
            track["target"] = r.string(tp + ".target")
        else:
            track = {"type": track_type}
            if track_type == "e":
                track["target"] = r.string(tp + ".target")

        keys: list[dict[str, Any]] = []
        ki = 0
        while True:
            kp = f"{tp}.keys[{ki}]"
            time = r.i32(kp + ".time")
            if time < 0:
                track["terminator"] = time
                break
            mask = r.u16(kp + ".mask")
            key: dict[str, Any] = {"time": time}
            extra_mask_bits = mask & ~KNOWN_MOVIE_MASK
            if extra_mask_bits:
                # FUN_00484a50 tests only bits 0x01..0x10 and does not reject the rest.
                key["extra_mask_bits"] = extra_mask_bits
            if mask & 0x01:
                key["position"] = parse_vec_payload(r, 3, kp + ".position")
            if mask & 0x02:
                key["rotation"] = parse_vec_payload(r, 4, kp + ".rotation")
            if mask & 0x04:
                key["scale"] = parse_vec_payload(r, 3, kp + ".scale")
            if mask & 0x08:
                key["payload_08"] = {
                    "text": r.string(kp + ".payload_08.text"),
                    "value1": r.u32(kp + ".payload_08.value1"),
                    "value2": r.u32(kp + ".payload_08.value2"),
                    "flag": r.u8(kp + ".payload_08.flag"),
                }
            if mask & 0x10:
                key["payload_10"] = {
                    "text1": r.string(kp + ".payload_10.text1"),
                    "text2": r.string(kp + ".payload_10.text2"),
                }
            keys.append(key)
            ki += 1
        track["keys"] = keys
        tracks.append(track)

    return {
        "kind": "movie",
        "name": name,
        "fps": fps,
        "start_time": start_time,
        "end_time": end_time,
        "flag": flag,
        "tracks": tracks,
    }

def encode_vec_payload(value: Any, count: int, path: str) -> bytes:
    value = require_fields(value, {"interpolation", "value"}, path)
    interpolation = require_int(value.get("interpolation"), 0, 0xFF, path + ".interpolation")
    vec = value.get("value")
    if not isinstance(vec, list) or len(vec) != count:
        raise BClaraError(f"{path}.value must be a list of exactly {count} numbers")
    packed_values = [
        struct.pack("<f", require_float(x, f"{path}.value[{i}]"))
        for i, x in enumerate(vec)
    ]
    # Normalize edited JSON to exact f32 wire values for semantic verification.
    value["value"] = [struct.unpack("<f", raw)[0] for raw in packed_values]
    return bytes([interpolation]) + b"".join(packed_values)

def encode_movie_key(key: Any, path: str) -> bytes:
    key = require_fields(
        key,
        {"time", "position", "rotation", "scale", "payload_08", "payload_10", "extra_mask_bits"},
        path,
    )
    time = require_int(key.get("time"), 0, 0x7FFFFFFF, path + ".time")
    mask = 0
    payload = bytearray()

    if "position" in key:
        mask |= 0x01
        payload += encode_vec_payload(key["position"], 3, path + ".position")
    if "rotation" in key:
        mask |= 0x02
        payload += encode_vec_payload(key["rotation"], 4, path + ".rotation")
    if "scale" in key:
        mask |= 0x04
        payload += encode_vec_payload(key["scale"], 3, path + ".scale")
    if "payload_08" in key:
        v = require_fields(
            key["payload_08"], {"text", "value1", "value2", "flag"}, path + ".payload_08"
        )
        mask |= 0x08
        payload += pack_string(v.get("text"), path + ".payload_08.text")
        payload += struct.pack("<I", require_int(v.get("value1"), 0, 0xFFFFFFFF, path + ".payload_08.value1"))
        payload += struct.pack("<I", require_int(v.get("value2"), 0, 0xFFFFFFFF, path + ".payload_08.value2"))
        payload += bytes([require_int(v.get("flag"), 0, 0xFF, path + ".payload_08.flag")])
    if "payload_10" in key:
        v = require_fields(key["payload_10"], {"text1", "text2"}, path + ".payload_10")
        mask |= 0x10
        payload += pack_string(v.get("text1"), path + ".payload_10.text1")
        payload += pack_string(v.get("text2"), path + ".payload_10.text2")

    extra = key.get("extra_mask_bits", 0)
    extra = require_int(extra, 0, 0xFFFF, path + ".extra_mask_bits")
    if extra & KNOWN_MOVIE_MASK:
        raise BClaraError(
            f"{path}.extra_mask_bits may contain only bits outside 0x{KNOWN_MOVIE_MASK:04X}"
        )
    mask |= extra
    return struct.pack("<iH", time, mask) + payload

def encode_movie(item: Any, path: str) -> bytes:
    item = require_fields(
        item,
        {"kind", "name", "fps", "start_time", "end_time", "flag", "tracks"},
        path,
    )
    out = bytearray()
    out += pack_string(item.get("name"), path + ".name")
    fps = require_int(item.get("fps"), 1, 0xFFFFFFFF, path + ".fps")
    start_time = require_int(item.get("start_time"), 0, 0xFFFFFFFF, path + ".start_time")
    end_time = require_int(item.get("end_time"), 0, 0xFFFFFFFF, path + ".end_time")
    flag = require_int(item.get("flag"), 0, 0xFF, path + ".flag")
    tracks = item.get("tracks")
    if not isinstance(tracks, list):
        raise BClaraError(f"{path}.tracks must be a list")
    if len(tracks) > 0xFFFFFFFF:
        raise BClaraError(f"{path}.tracks is too long")
    out += struct.pack("<III", fps, start_time, end_time)
    out += bytes([flag])
    out += struct.pack("<I", len(tracks))

    for ti, track in enumerate(tracks):
        tp = f"{path}.tracks[{ti}]"
        track = require_fields(
            track,
            {"type", "raw_type_byte", "effective_type", "target", "keys", "terminator"},
            tp,
        )
        track_type = track.get("type")
        if track_type == "unknown":
            raw_type = require_int(track.get("raw_type_byte"), 0, 0xFF, tp + ".raw_type_byte")
            if raw_type in map(ord, MOVIE_TRACK_TYPES):
                raise BClaraError(f"{tp}.raw_type_byte is a recognized track tag; use its normal type")
            if track.get("effective_type") != "e":
                raise BClaraError(f"{tp}.effective_type must be 'e' for an unknown runtime track tag")
            out += bytes([raw_type])
            out += pack_string(track.get("target"), tp + ".target")
        else:
            if track_type not in MOVIE_TRACK_TYPE_SET:
                raise BClaraError(
                    f"{tp}.type must be one of {'/'.join(MOVIE_TRACK_TYPES)} or 'unknown'"
                )
            if "raw_type_byte" in track or "effective_type" in track:
                raise BClaraError(f"{tp}.raw_type_byte/effective_type are only valid for type 'unknown'")
            out += track_type.encode("ascii")
            if track_type == "e":
                out += pack_string(track.get("target"), tp + ".target")
            elif "target" in track:
                raise BClaraError(f"{tp}.target is only valid for effective type 'e'")

        keys = track.get("keys")
        if not isinstance(keys, list):
            raise BClaraError(f"{tp}.keys must be a list")
        for ki, key in enumerate(keys):
            out += encode_movie_key(key, f"{tp}.keys[{ki}]")

        terminator = track.get("terminator", -1)
        terminator = require_int(terminator, -(1 << 31), -1, tp + ".terminator")
        # Normalize newly created tracks so post-encode verification sees the exact value.
        track["terminator"] = terminator
        out += struct.pack("<i", terminator)
    return bytes(out)


def parse_entity(r: Reader, path: str, schema: clara.Schema | None) -> dict[str, Any]:
    class_name = r.string(path + ".class")
    body_size = r.u32(path + ".body_size")
    body_start = r.absolute()
    body = r.read(body_size, path + ".body")

    # Raw mode deliberately keeps the useful name/payload split. It is an editor
    # convenience, not a claim that the runtime failed class resolution.
    if schema is None:
        if body_size < 2:
            raise BClaraError(f"{path}: raw entity body too short to contain object name")
        br = Reader(body, base=body_start, label=path + ".body")
        name = br.string(path + ".name")
        return {
            "kind": "entity",
            "class": class_name,
            "name": name,
            "payload_hex": body[br.pos:].hex(),
        }

    cls = schema.by_name.get(class_name)
    if cls is None:
        # FUN_00483b50 resolves the class first. On failure it skips exactly the
        # declared u32 body size and does not attempt to deserialize object_name.
        return {
            "kind": "entity",
            "class": class_name,
            "opaque": True,
            "opaque_body_base64": clara.b64e(body),
        }

    br = Reader(body, base=body_start, label=path + ".body")
    name = br.string(path + ".name")
    entity = clara.decode_entity_payload(br, cls, schema, class_name, name, path)
    if br.remaining():
        raise BClaraError(f"{path}: {br.remaining()} trailing semantic entity bytes")
    return {"kind": "entity", **entity}


def parse_group(r: Reader, path: str) -> dict[str, Any]:
    name = r.string(path + ".name")
    count = checked_count(r.u32(path + ".item_count"), 2, r.remaining(), path + ".item_count")
    return {"kind": "group", "name": name,
            "items": [r.string(f"{path}.items[{i}]") for i in range(count)]}


def parse_multilayer(r: Reader, path: str) -> dict[str, Any]:
    name = r.string(path + ".name")
    layer_count = checked_count(r.u32(path + ".layer_count"), 2, r.remaining(), path + ".layer_count")
    layers = [r.string(f"{path}.layers[{i}]") for i in range(layer_count)]
    column_count = checked_count(r.u32(path + ".column_count"), 2, r.remaining(), path + ".column_count")
    columns = [r.string(f"{path}.columns[{i}]") for i in range(column_count)]
    cells = layer_count * column_count
    if cells > r.remaining() // 4:
        raise BClaraError(f"{path}.matrix needs {cells * 4} bytes, only {r.remaining()} remain")
    matrix = [[r.u32(f"{path}.matrix[{i}][{j}]") for j in range(column_count)]
              for i in range(layer_count)]
    return {"kind": "multilayer", "name": name, "layers": layers,
            "columns": columns, "matrix": matrix}


def parse_folder(r: Reader, schema: clara.Schema | None, path: str = "root") -> dict[str, Any]:
    name = r.string(path + ".name")
    allocation_counts = [r.u16(f"{path}.allocation_counts[{i}]") for i in range(5)]
    record_count = r.u16(path + ".record_count")
    if record_count > r.remaining():
        raise BClaraError(f"{path}.record_count={record_count} cannot fit in {r.remaining()} remaining bytes")
    records: list[dict[str, Any]] = []
    for i in range(record_count):
        rp = f"{path}.records[{i}]"
        tag = r.u8(rp + ".tag")
        if tag == ord("f"):
            records.append({"kind": "folder", "folder": parse_folder(r, schema, rp + ".folder")})
        elif tag == ord("e"):
            records.append(parse_entity(r, rp, schema))
        elif tag == ord("g"):
            records.append(parse_group(r, rp))
        elif tag == ord("m"):
            records.append(parse_movie(r, rp))
        elif tag == ord("u"):
            records.append(parse_multilayer(r, rp))
        else:
            # FUN_00483b50 has no default payload handler: after consuming the tag
            # byte it simply advances to the next record. Preserve that exact form.
            records.append({"kind": "unknown", "tag": tag})
    return {
        "name": name,
        "allocation_counts": allocation_counts,
        "records": records,
    }


def decode_bytes(data: bytes, source_name: str = "<memory>", *,
                 schema: clara.Schema | None = None, schema_sha256: str | None = None) -> dict[str, Any]:
    r = Reader(data, label=source_name)
    marker = r.u16("marker")
    version = r.u16("version")
    if marker != MARKER:
        raise BClaraError(f"expected Clara marker 0x{MARKER:04X}, got 0x{marker:04X}")
    if version != VERSION:
        raise BClaraError(f"unsupported Clara library version {version}; expected {VERSION}")
    library_name = r.string("library_name")
    # The recovered loader reads this byte and immediately enters FUN_00483b50;
    # it does not compare the value with 'f'. Preserve rather than canonicalize.
    root_tag = r.u8("root_tag")
    root = parse_folder(r, schema)
    if r.remaining():
        raise BClaraError(f"{r.remaining()} trailing bytes after root folder at 0x{r.absolute():X}")
    doc = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "library_name": library_name,
        "root_tag": root_tag,
        "entity_mode": "semantic" if schema is not None else "raw",
        "root": root,
    }
    if schema is not None:
        if not isinstance(schema_sha256, str):
            raise BClaraError("internal error: semantic decode requires schema_sha256")
        doc["schema_sha256"] = schema_sha256
    return doc


def encode_raw_entity(item: Any, path: str) -> bytes:
    item = require_fields(item, {"kind", "class", "name", "payload_hex"}, path)
    payload = decode_hex(item.get("payload_hex"), path + ".payload_hex")
    body = pack_string(item.get("name"), path + ".name") + payload
    if len(body) > 0xFFFFFFFF:
        raise BClaraError(f"{path}.body exceeds u32 size")
    return b"e" + pack_string(item.get("class"), path + ".class") + struct.pack("<I", len(body)) + body


def encode_group(item: Any, path: str) -> bytes:
    item = require_fields(item, {"kind", "name", "items"}, path)
    items = item.get("items")
    if not isinstance(items, list) or len(items) > 0xFFFFFFFF:
        raise BClaraError(f"{path}.items must be a list fitting u32")
    return b"g" + pack_string(item.get("name"), path + ".name") + struct.pack("<I", len(items)) + b"".join(
        pack_string(v, f"{path}.items[{i}]") for i, v in enumerate(items))


def encode_multilayer(item: Any, path: str) -> bytes:
    item = require_fields(item, {"kind", "name", "layers", "columns", "matrix"}, path)
    layers, columns, matrix = item.get("layers"), item.get("columns"), item.get("matrix")
    if not isinstance(layers, list) or not isinstance(columns, list) or not isinstance(matrix, list):
        raise BClaraError(f"{path}.layers, columns, and matrix must be lists")
    if len(layers) > 0xFFFFFFFF or len(columns) > 0xFFFFFFFF:
        raise BClaraError(f"{path} dimensions exceed u32")
    if len(matrix) != len(layers) or any(not isinstance(row, list) or len(row) != len(columns) for row in matrix):
        raise BClaraError(f"{path}.matrix dimensions must be layer_count x column_count")
    out = bytearray(b"u" + pack_string(item.get("name"), path + ".name"))
    out += struct.pack("<I", len(layers))
    out += b"".join(pack_string(v, f"{path}.layers[{i}]") for i, v in enumerate(layers))
    out += struct.pack("<I", len(columns))
    out += b"".join(pack_string(v, f"{path}.columns[{i}]") for i, v in enumerate(columns))
    for i, row in enumerate(matrix):
        for j, value in enumerate(row):
            out += struct.pack("<I", require_int(value, 0, 0xFFFFFFFF, f"{path}.matrix[{i}][{j}]"))
    return bytes(out)


def encode_folder(folder: Any, schema: clara.Schema | None, path: str = "root") -> bytes:
    folder = require_fields(folder, {"name", "allocation_counts", "records"}, path)
    records = folder.get("records")
    if not isinstance(records, list) or len(records) > 0xFFFF:
        raise BClaraError(f"{path}.records must be a list fitting u16")

    stored_counts = folder.get("allocation_counts")
    if (
        not isinstance(stored_counts, list)
        or len(stored_counts) != 5
        or any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 0xFFFF for x in stored_counts)
    ):
        raise BClaraError(f"{path}.allocation_counts must contain exactly five u16 integers")
    actual_counts = [
        sum(isinstance(rec, dict) and rec.get("kind") == kind for rec in records)
        for kind in RECORD_KIND_ORDER
    ]
    # Preserve source allocation hints exactly unless an edit needs more room.
    counts = [max(stored, actual) for stored, actual in zip(stored_counts, actual_counts)]
    folder["allocation_counts"] = counts

    out = bytearray(pack_string(folder.get("name"), path + ".name"))
    out += b"".join(struct.pack("<H", n) for n in counts)
    out += struct.pack("<H", len(records))
    for i, rec in enumerate(records):
        rp = f"{path}.records[{i}]"
        if not isinstance(rec, dict):
            raise BClaraError(f"{rp} must be an object")
        kind = rec.get("kind")
        if kind == "folder":
            out += b"f" + encode_folder(rec.get("folder"), schema, rp + ".folder")
        elif kind == "entity":
            if schema is not None and ("properties" in rec or rec.get("opaque") is True):
                out += clara.encode_envelope(rec, schema, rp)
            else:
                out += encode_raw_entity(rec, rp)
        elif kind == "group":
            out += encode_group(rec, rp)
        elif kind == "movie":
            out += b"m" + encode_movie(rec, rp)
        elif kind == "multilayer":
            out += encode_multilayer(rec, rp)
        elif kind == "unknown":
            rec = require_fields(rec, {"kind", "tag"}, rp)
            tag = require_int(rec.get("tag"), 0, 0xFF, rp + ".tag")
            if tag in map(ord, "fegmu"):
                raise BClaraError(f"{rp}.tag is a known record tag; use its structured kind")
            out += bytes([tag])
        else:
            raise BClaraError(f"{rp}.kind is unsupported: {kind!r}")
    return bytes(out)


def encode_document(doc: Any, *, schema: clara.Schema | None = None,
                    schema_sha256: str | None = None) -> bytes:
    doc = require_fields(
        doc,
        {"format", "format_version", "library_name", "root_tag", "entity_mode", "schema_sha256", "root"},
        "document",
    )
    if doc.get("format") != FORMAT or doc.get("format_version") != FORMAT_VERSION:
        raise BClaraError(f"unsupported editable JSON format/version; expected {FORMAT!r} v{FORMAT_VERSION}")
    mode = doc.get("entity_mode")
    if mode not in {"raw", "semantic"}:
        raise BClaraError("document.entity_mode must be 'raw' or 'semantic'")
    if mode == "semantic":
        if schema is None or schema_sha256 is None:
            raise BClaraError("semantic JSON requires --schema-from with the matching BLIBCLARA")
        if doc.get("schema_sha256") != schema_sha256:
            raise BClaraError("semantic JSON was decoded with a different Clara schema")
    elif "schema_sha256" in doc:
        raise BClaraError("raw JSON must not contain schema_sha256")
    use_schema = schema if mode == "semantic" else None
    root_tag = require_int(doc.get("root_tag"), 0, 0xFF, "root_tag")
    out = bytearray(struct.pack("<HH", MARKER, VERSION))
    out += pack_string(doc.get("library_name"), "library_name")
    out += bytes([root_tag]) + encode_folder(doc.get("root"), use_schema, "root")
    return bytes(out)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BClaraError(f"cannot read JSON {path}: {exc}") from exc


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


def resolve_schema(path: Path | None) -> tuple[clara.Schema | None, str | None]:
    return (None, None) if path is None else load_external_schema(path)


def command_decode(inp: Path, out: Path, schema_from: Path | None) -> None:
    schema, schema_hash = resolve_schema(schema_from)
    doc = decode_bytes(inp.read_bytes(), inp.name, schema=schema, schema_sha256=schema_hash)
    write_json(out, doc)
    mode = doc["entity_mode"]
    print(f"decoded {inp} -> {out} ({mode} entities)")


def command_encode(inp: Path, out: Path, schema_from: Path | None) -> None:
    schema, schema_hash = resolve_schema(schema_from)
    doc = load_json(inp)
    data = encode_document(doc, schema=schema, schema_sha256=schema_hash)
    reparsed = decode_bytes(data, out.name, schema=schema if doc.get("entity_mode") == "semantic" else None,
                            schema_sha256=schema_hash if doc.get("entity_mode") == "semantic" else None)
    if not clara.semantic_equal(clara.semantic_view(doc), clara.semantic_view(reparsed)):
        raise BClaraError("post-encode semantic verification failed")
    out.write_bytes(data)
    print(f"encoded {inp} -> {out} ({len(data)} bytes)")


def command_verify(inp: Path, schema_from: Path | None) -> None:
    schema, schema_hash = resolve_schema(schema_from)
    data = inp.read_bytes()
    doc = decode_bytes(data, inp.name, schema=schema, schema_sha256=schema_hash)
    rebuilt = encode_document(doc, schema=schema, schema_sha256=schema_hash)
    reparsed = decode_bytes(rebuilt, inp.name + "<rebuilt>", schema=schema, schema_sha256=schema_hash)
    if not clara.semantic_equal(clara.semantic_view(doc), clara.semantic_view(reparsed)):
        raise BClaraError("decode/encode semantic verification failed")
    print(f"OK: {inp}: {len(data)} bytes; mode={doc['entity_mode']}; no-op byte-identical={rebuilt == data}")


def command_roundtrip(inp: Path, schema_from: Path | None) -> None:
    schema, schema_hash = resolve_schema(schema_from)
    data = inp.read_bytes()
    doc = decode_bytes(data, inp.name, schema=schema, schema_sha256=schema_hash)
    rebuilt = encode_document(doc, schema=schema, schema_sha256=schema_hash)
    if rebuilt != data:
        first = next((i for i, (a, b) in enumerate(zip(data, rebuilt)) if a != b), min(len(data), len(rebuilt)))
        raise BClaraError(f"round-trip is not byte-identical: original={len(data)} rebuilt={len(rebuilt)}, first difference at 0x{first:X}")
    print(f"OK: {inp}: byte-identical {doc['entity_mode']} round trip ({len(data)} bytes)")


def add_schema_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--schema-from", type=Path, metavar="BLIBCLARA",
                        help="decode/encode entities semantically using this compatible .blibclara schema")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Decode and rebuild Minion Rush Clara v12 .bclara files.")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("decode", help="decode BCLARA to editable JSON")
    d.add_argument("input", type=Path); d.add_argument("output", type=Path); add_schema_option(d)
    e = sub.add_parser("encode", help="encode editable JSON to BCLARA")
    e.add_argument("input", type=Path); e.add_argument("output", type=Path); add_schema_option(e)
    v = sub.add_parser("verify", help="parse, rebuild, and semantically verify a BCLARA")
    v.add_argument("input", type=Path); add_schema_option(v)
    r = sub.add_parser("roundtrip", help="require byte-identical decode/encode round trip")
    r.add_argument("input", type=Path); add_schema_option(r)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "decode": command_decode(args.input, args.output, args.schema_from)
        elif args.command == "encode": command_encode(args.input, args.output, args.schema_from)
        elif args.command == "verify": command_verify(args.input, args.schema_from)
        elif args.command == "roundtrip": command_roundtrip(args.input, args.schema_from)
        else: raise AssertionError(args.command)
        return 0
    except (BClaraError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
