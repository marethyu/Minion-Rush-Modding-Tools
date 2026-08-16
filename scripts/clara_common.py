#!/usr/bin/env python3
"""Shared Clara v12 schema/entity codec used by the BCLARA and BLIBCLARA editors.

PURPOSE
=======
This module contains only serialization logic common to both Clara containers. It
has no CLI and deliberately contains no folder, movie, BCLARA, BLIBCLARA, or JPK
container logic.

RECOVERED COMMON ENTITY FORMAT
==============================
All fields are little-endian. ``String16`` is ``u16 byte_length`` followed by
exactly that many bytes.

An entity envelope is::

    u8       tag = 'e'
    String16 class_name
    u32      body_size
    byte     body[body_size]

For a known class, ``body`` is::

    String16 object_name
    EntityPreamble
    u16      property_count
    PropertyArray properties[property_count]  # class-schema order

``EntityPreamble`` is the common runtime object prefix recovered from the Minion
Rush executable: 12 + 16 + 12 transform bytes, an optional String16 marker with
9 following bytes when non-empty, then a String16-sized auxiliary byte block;
when that block is non-empty an auxiliary String16 and u32 follow.

A property array starts with one byte. Bits 0..5 contain an inline element count,
bit 6 means elements have String16 names, and bit 7 means a u16 extended count
follows. The supported type dispatch is the exact Clara v12 dispatch recovered
for this build: 0x0002, 0x0004, 0x0008, 0x0010, 0x0020, 0x0040, 0x0080,
0x0100, 0x0200, 0x0400, 0x0800, and 0x1000. Numeric type 0x0002 uses subtypes
0..4 for i8/i16/i32/f32/f64.

SCHEMA FORMAT
=============
The schema block embedded at the beginning of BLIBCLARA is::

    u32 schema_header
    u16 parameter_count
    Parameter parameters[parameter_count]
    u16 class_count
    ClassDef classes[class_count]

    Parameter := u16 type_code, String16 name, u8 subtype,
                 u16 alias_count, String16 aliases[alias_count]

    ClassDef  := String16 name, u8 generic, u32 property_count,
                 (String16 property_name, u32 parameter_index)[property_count]

Standalone BCLARA omits this schema, but its entity bodies use the same grammar.
A matching BLIBCLARA schema can therefore be supplied externally to decode and
encode BCLARA entities semantically.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import math
import struct
from dataclasses import dataclass
from functools import cached_property
from typing import Any

ENTITY_TAG = ord("e")
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
NUMERIC_SUBTYPES: dict[int, str] = {0: "i8", 1: "i16", 2: "i32", 3: "f32", 4: "f64"}

class ClaraError(RuntimeError):
    pass

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")

def b64d(text: str, label: str) -> bytes:
    if not isinstance(text, str):
        raise ClaraError(f"{label} must be a base64 string")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ClaraError(f"{label} is not valid base64") from exc

def text_decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="surrogateescape")

def text_encode(value: Any, label: str) -> bytes:
    if not isinstance(value, str):
        raise ClaraError(f"{label} must be a string")
    raw = value.encode("utf-8", errors="surrogateescape")
    if len(raw) > 0xFFFF:
        raise ClaraError(f"{label} exceeds 65535 encoded bytes")
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
            raise ClaraError(
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

    # Interface required by bclara_editor.py's shared Clara_movie codec.
    def i32(self, what: str = "i32") -> int:
        return struct.unpack("<i", self.read(4, what))[0]

    def f32(self, what: str = "f32") -> float:
        return struct.unpack("<f", self.read(4, what))[0]

    def shared_string(self, what: str = "string") -> str:
        n = self.u16(what + " length")
        return text_decode(self.read(n, what))

    def lp_bytes(self, what: str) -> bytes:
        return self.read(self.u16(what + " length"), what)

    def string(self, what: str = "string") -> str:
        return self.shared_string(what)

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

    @cached_property
    def by_name(self) -> dict[str, ClassDef]:
        return {x.name: x for x in self.classes}

def pack_string(value: Any, label: str) -> bytes:
    raw = text_encode(value, label)
    return struct.pack("<H", len(raw)) + raw

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
            raise ClaraError(
                f"parameter {i} {name!r} uses unsupported/unrecognized Clara "
                f"type code 0x{code:X}; this game executable dispatches only "
                + ", ".join(f"0x{x:X}" for x in CLARA_TYPES)
            )
        if code == 2 and subtype not in NUMERIC_SUBTYPES:
            raise ClaraError(
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
                raise ClaraError(f"class {name!r}.{pname} has invalid type index {tidx}")
            props.append(Property(pname, tidx))
        classes.append(ClassDef(name, generic, tuple(props)))
    if len({c.name for c in classes}) != len(classes):
        raise ClaraError("duplicate class names in schema")
    return Schema(header, tuple(params), tuple(classes), r.pos)

def parse_entity_record(
    r: Reader, schema: Schema, path: str, *, tag_already_read: bool = False
) -> dict[str, Any]:
    if not tag_already_read:
        tag = r.u8(path + ".tag")
        if tag != ENTITY_TAG:
            raise ClaraError(f"{path}: expected entity tag 'e', got 0x{tag:02X}")
    class_name = r.shared_string(path + ".class")
    body_len = r.u32(path + ".body_size")
    body_start = r.absolute()
    body = r.read(body_len, path + ".body")
    cls = schema.by_name.get(class_name)
    if cls is None:
        # The game loader treats the declared body size as an opaque skip boundary
        # when the class factory cannot resolve this class. Mirror that behavior
        # instead of rejecting the entire file.
        return {
            "class": class_name,
            "opaque": True,
            "opaque_body_base64": b64e(body),
        }
    br = Reader(body, base=body_start, label=path)
    name = br.shared_string(path + ".name")
    item = decode_entity_payload(br, cls, schema, class_name, name, path)
    if br.remaining():
        raise ClaraError(f"{path}: {br.remaining()} trailing entity-body bytes")
    return item

def read_array_header(r: Reader, label: str) -> tuple[int, bool]:
    raw = r.u8(label + " array header")
    count = r.u16(label + " extended count") if raw & 0x80 else raw & 0x3F
    return count, bool(raw & 0x40)

def pack_array_header(count: int, named: bool) -> bytes:
    if not isinstance(count, int) or count < 0 or count > 0xFFFF:
        raise ClaraError(f"array count {count!r} is outside 0..65535")
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
        raise ClaraError(f"{path}: unsupported numeric subtype {sub}")
    if code == 4:
        return {"id": r.u32(path + ".id"), "name": r.shared_string(path + ".name")}
    if code in (8, 16):
        return text_decode(r.lp_bytes(path))
    if code == 32:
        present = r.u8(path + ".present")
        if present == 0:
            return None
        # The runtime treats any nonzero byte as present. Unchanged values retain
        # their original raw bytes; edited values are emitted canonically as 0/1.
        tag = r.u8(path + ".tag")
        if tag != ENTITY_TAG:
            raise ClaraError(f"{path}: expected nested entity tag 'e', got 0x{tag:02X}")
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
            raise ClaraError(f"{path}: {br.remaining()} trailing nested bytes")
        return entity
    if code in (64, 512):
        return [r.shared_string(f"{path}[{i}]") for i in range(3)]
    if code == 128:
        count = r.u8(path + ".component_count")
        if count > 4:
            raise ClaraError(f"{path}.component_count={count} exceeds runtime vec4 storage")
        return [struct.unpack("<f", r.read(4, path))[0] for _ in range(count)]
    if code == 256:
        return {"text": text_decode(r.lp_bytes(path + ".text")),
                "trailing_u32": r.u32(path + ".trailing")}
    if code in (1024, 4096):
        return r.shared_string(path)
    if code == 2048:
        return {"value": r.u32(path + ".value"), "name": r.shared_string(path + ".name")}
    raise ClaraError(f"{path}: unsupported Clara type code 0x{code:X}")

def decode_entity_payload(r: Reader, cls: ClassDef, schema: Schema,
                          class_name: str, name: str, path: str) -> dict[str, Any]:
    preamble = parse_preamble(r)
    count = r.u16(path + ".property_count")
    if count != len(cls.properties):
        raise ClaraError(f"{path}: property count {count} != schema count {len(cls.properties)}")
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

def finite_number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ClaraError(f"{label} must be numeric")
    out = float(value)
    if not math.isfinite(out):
        raise ClaraError(f"{label} must be finite")
    return out

def pack_f32(value: Any, label: str) -> bytes:
    out = finite_number(value, label)
    try:
        packed = struct.pack("<f", out)
    except (OverflowError, struct.error) as exc:
        raise ClaraError(f"{label} is outside finite f32 range") from exc
    # Python may encode an overflowing finite input as infinity on some builds.
    if not math.isfinite(struct.unpack("<f", packed)[0]):
        raise ClaraError(f"{label} is outside finite f32 range")
    return packed

def checked_count(count: int, minimum_bytes: int, remaining: int, label: str) -> int:
    if count < 0:
        raise ClaraError(f"{label} is negative")
    if minimum_bytes and count > remaining // minimum_bytes:
        raise ClaraError(f"{label}={count} cannot fit in the remaining {remaining} bytes")
    return count

def encode_value(value: Any, param: Param, schema: Schema, path: str) -> bytes:
    code, sub = param.type_code, param.subtype
    if code == 2:
        if sub == 0:
            if isinstance(value, int) and not isinstance(value, bool) and -128 <= value <= 127:
                return struct.pack("<b", value)
            raise ClaraError(f"{path} must be signed i8 (-128..127); booleans are not accepted")
        if sub == 1:
            if isinstance(value, int) and not isinstance(value, bool) and -0x8000 <= value <= 0x7FFF:
                return struct.pack("<h", value)
            raise ClaraError(f"{path} must be signed i16")
        if sub == 2:
            if isinstance(value, int) and not isinstance(value, bool) and -(1<<31) <= value < (1<<31):
                return struct.pack("<i", value)
            raise ClaraError(f"{path} must be i32")
        if sub == 3: return pack_f32(value, path)
        if sub == 4: return struct.pack("<d", finite_number(value, path))
        raise ClaraError(f"{path}: unsupported numeric subtype {sub}")
    if code == 4:
        if not isinstance(value, dict) or not isinstance(value.get("id"), int) or isinstance(value.get("id"), bool):
            raise ClaraError(f"{path} must be {{id,name}}")
        if not 0 <= value["id"] <= 0xFFFFFFFF:
            raise ClaraError(f"{path}.id outside u32")
        return struct.pack("<I", value["id"]) + pack_string(value.get("name"), path + ".name")
    if code in (8, 16):
        return pack_string(value, path)
    if code == 32:
        if value is None: return b"\x00"
        if not isinstance(value, dict):
            raise ClaraError(f"{path} must be a nested entity object or null")
        if value.get("opaque") is True:
            class_name = value.get("class")
            if not isinstance(class_name, str):
                raise ClaraError(f"{path}.class must be a string")
            body = b64d(value.get("opaque_body_base64"), path + ".opaque_body_base64")
        else:
            body = encode_entity_body(value, schema, path)
            class_name = value.get("class")
        return b"\x01" + bytes([ENTITY_TAG]) + pack_string(class_name, path + ".class") + struct.pack("<I", len(body)) + body
    if code in (64, 512):
        if not isinstance(value, list) or len(value) != 3:
            raise ClaraError(f"{path} must be a three-string list")
        return b"".join(pack_string(x, f"{path}[{i}]") for i, x in enumerate(value))
    if code == 128:
        if not isinstance(value, list) or len(value) > 4:
            raise ClaraError(f"{path} must be a list of at most 4 floats")
        return bytes([len(value)]) + b"".join(pack_f32(x, f"{path}[{i}]") for i, x in enumerate(value))
    if code == 256:
        if (not isinstance(value, dict) or
                not isinstance(value.get("trailing_u32"), int) or
                isinstance(value.get("trailing_u32"), bool)):
            raise ClaraError(f"{path} must contain text and trailing_u32")
        if not 0 <= value["trailing_u32"] <= 0xFFFFFFFF:
            raise ClaraError(f"{path}.trailing_u32 outside u32")
        return pack_string(value.get("text"), path + ".text") + struct.pack("<I", value["trailing_u32"])
    if code in (1024, 4096):
        return pack_string(value, path)
    if code == 2048:
        if not isinstance(value, dict) or not isinstance(value.get("value"), int) or isinstance(value.get("value"), bool):
            raise ClaraError(f"{path} must contain value and name")
        if not 0 <= value["value"] <= 0xFFFFFFFF:
            raise ClaraError(f"{path}.value outside u32")
        return struct.pack("<I", value["value"]) + pack_string(value.get("name"), path + ".name")
    raise ClaraError(f"{path}: unsupported Clara type code 0x{code:X}")

def validate_entity_shape(item: Any, schema: Schema, path: str) -> tuple[ClassDef, list[dict[str, Any]]]:
    if not isinstance(item, dict): raise ClaraError(f"{path} must be an object")
    class_name = item.get("class")
    name = item.get("name")
    if not isinstance(class_name, str) or class_name not in schema.by_name:
        raise ClaraError(f"{path}.class is unknown")
    if not isinstance(name, str): raise ClaraError(f"{path}.name must be a string")
    props = item.get("properties")
    if not isinstance(props, list): raise ClaraError(f"{path}.properties must be a list")
    cls = schema.by_name[class_name]
    if len(props) != len(cls.properties):
        raise ClaraError(f"{path}: property count {len(props)} != {len(cls.properties)}")
    for i, (got, expected) in enumerate(zip(props, cls.properties)):
        if not isinstance(got, dict) or got.get("name") != expected.name or got.get("type_index") != expected.type_index:
            raise ClaraError(f"{path}.properties[{i}] does not match schema property {expected.name!r}")
    return cls, props

def decode_single_raw(raw: bytes, param: Param, schema: Schema, path: str) -> Any:
    r = Reader(raw, label=path)
    value = decode_value(r, param, schema, path)
    if r.remaining():
        raise ClaraError(f"{path}: raw value has {r.remaining()} trailing bytes")
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

def encode_entity_payload(item: dict[str, Any], schema: Schema, path: str) -> bytes:
    cls, props = validate_entity_shape(item, schema, path)
    try:
        preamble = bytes.fromhex(item.get("preamble_hex", ""))
    except ValueError as exc:
        raise ClaraError(f"{path}.preamble_hex is invalid") from exc
    # Validate preamble independently so malformed edits fail before output.
    pr = Reader(preamble, label=path + ".preamble")
    parse_preamble(pr)
    if pr.remaining(): raise ClaraError(f"{path}.preamble_hex has trailing bytes")
    out = bytearray(preamble)
    out += struct.pack("<H", len(cls.properties))
    for pitem, pdef in zip(props, cls.properties):
        elements = pitem.get("elements")
        named = pitem.get("named_elements")
        if not isinstance(elements, list) or not isinstance(named, bool):
            raise ClaraError(f"{path}.{pdef.name}: invalid array representation")
        out += pack_array_header(len(elements), named)
        param = schema.params[pdef.type_index]
        for i, element in enumerate(elements):
            if not isinstance(element, dict) or "value" not in element:
                raise ClaraError(f"{path}.{pdef.name}[{i}] must contain value")
            if named:
                out += pack_string(element.get("name"), f"{path}.{pdef.name}[{i}].name")
            unchanged_value = "original_value" in element and semantic_equal(element.get("value"), element.get("original_value"))
            unchanged_name = (not named) or element.get("name") == element.get("original_name")
            if unchanged_value and unchanged_name and isinstance(element.get("raw_base64"), str):
                raw = b64d(element["raw_base64"], f"{path}.{pdef.name}[{i}].raw_base64")
                decoded_raw = decode_single_raw(raw, param, schema, f"{path}.{pdef.name}[{i}].raw")
                if not semantic_equal(decoded_raw, element.get("original_value")):
                    raise ClaraError(f"{path}.{pdef.name}[{i}]: raw_base64 does not encode original_value")
                out += raw
            else:
                encoded = encode_value(element["value"], param, schema, f"{path}.{pdef.name}[{i}]")
                out += encoded
                # Canonicalize edited f32-backed JSON values to their exact wire
                # representation before post-encode semantic comparison.
                if param.type_code == 2 and param.subtype == 3:
                    element["value"] = struct.unpack("<f", encoded)[0]
                elif param.type_code == 128:
                    count = encoded[0]
                    element["value"] = [
                        struct.unpack_from("<f", encoded, 1 + j * 4)[0]
                        for j in range(count)
                    ]
    return bytes(out)

def encode_entity_body(item: dict[str, Any], schema: Schema, path: str) -> bytes:
    return pack_string(item.get("name"), path + ".name") + encode_entity_payload(item, schema, path)

def encode_envelope(item: dict[str, Any], schema: Schema, path: str) -> bytes:
    if not isinstance(item, dict):
        raise ClaraError(f"{path} must be an entity object")
    class_name = item.get("class")
    if not isinstance(class_name, str):
        raise ClaraError(f"{path}.class must be a string")
    if item.get("opaque") is True:
        body = b64d(item.get("opaque_body_base64"), path + ".opaque_body_base64")
    else:
        body = encode_entity_body(item, schema, path)
    return bytes([ENTITY_TAG]) + pack_string(class_name, path + ".class") + struct.pack("<I", len(body)) + body


def require_int(value: Any, lo: int, hi: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClaraError(f"{label} must be an integer")
    if not lo <= value <= hi:
        raise ClaraError(f"{label}={value} outside {lo}..{hi}")
    return value


def require_float(value: Any, label: str) -> float:
    out = finite_number(value, label)
    try:
        packed = struct.pack("<f", out)
    except (OverflowError, struct.error) as exc:
        raise ClaraError(f"{label} is outside finite f32 range") from exc
    if not math.isfinite(struct.unpack("<f", packed)[0]):
        raise ClaraError(f"{label} is outside finite f32 range")
    return out


def decode_hex(value: Any, label: str) -> bytes:
    if not isinstance(value, str):
        raise ClaraError(f"{label} must be a hexadecimal string")
    try:
        return bytes.fromhex("".join(value.split()))
    except ValueError as exc:
        raise ClaraError(f"{label} is not valid hexadecimal") from exc


def require_fields(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ClaraError(f"{label} must be an object")
    extra = set(value) - allowed
    if extra:
        raise ClaraError(f"{label} has unsupported fields: {', '.join(sorted(extra))}")
    return value


def semantic_view(value: Any) -> Any:
    """Remove provenance-only fields before comparing edited and reparsed JSON."""
    if isinstance(value, list):
        return [semantic_view(x) for x in value]
    if isinstance(value, dict):
        return {
            k: semantic_view(v)
            for k, v in value.items()
            if k not in {"original_value", "original_name", "raw_base64"}
        }
    return value
