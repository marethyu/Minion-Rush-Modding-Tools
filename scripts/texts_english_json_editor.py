#!/usr/bin/env python3
"""
Minion Rush English localization JSON editor.

Supported Minion Rush Babel formats
====================================

* Windows: Babel version 1; localization entries store UTF-8 string keys.
  Windows input may be either a ZIP-based ``text.bin``/``text.zip`` containing
  ``text/texts.texts`` or a raw extracted ``.texts`` Babel file. Encoding
  preserves the Windows input representation.

* Android: Babel version 2; localization entries store 32-bit key hashes and
  use a companion Babel key dictionary. Android commands accept ONLY a raw
  ``.texts`` localization file. Android ``.bin``/``.zip`` containers are
  deliberately rejected.

The Babel version is detected from the file contents; Android acceptance is
then restricted to a raw input whose filename ends in ``.texts``.

Android key dictionary requirement
==================================

For Android (Babel v2), decode, encode, and verify require a companion key
dictionary. Supply it explicitly with:

    --keys KEY_FILE_NAME.keys

If ``--keys`` is omitted, the editor looks beside the Android input for:

    TEXT_FILE_NAME.texts.keys

Thus:

    TEXT_FILE_NAME.texts -> TEXT_FILE_NAME.texts.keys

If the default file does not exist, the command fails and reports the expected
path. The filename extension of an explicitly supplied key file is not
restricted; its contents are validated.

Android JSON and structural key editing
=======================================

Android editable JSON never exposes ``0xXXXXXXXX`` localization hashes. Hashes
are resolved through the supplied key dictionary and JSON uses symbolic string
keys.

The JSON contains three editing areas:

    "strings" : edit English values for the original keys
    "add"     : {"NEW_KEY": "English value"}
    "delete"  : ["KEY_TO_DELETE"]

For Android encoding:

* value edits under ``strings`` affect English only;
* ``delete`` removes that key from every language and from the rebuilt key table;
* ``add`` creates a new key-table entry and an English localization entry only.

New Android key hashes are ``CRC32(symbolic UTF-8 key)``. The encoder rejects
symbolic-name duplicates and CRC32 collisions.

Every successful Android encode writes TWO raw files:

    OUTPUT.texts
    OUTPUT.texts.keys

The Android output path itself must end in ``.texts``. A no-op Android encode
produces a byte-identical localization file and a byte-identical companion key
file.

Windows behavior
================

Windows does not use an external key dictionary. Existing ``strings``, ``add``,
and ``delete`` behavior remains English-only.

Commands
========

Windows (``--keys`` not required):

    python texts_english_json_editor.py decode text.bin english_strings.json
    python texts_english_json_editor.py decode texts.texts english_strings.json
    python texts_english_json_editor.py encode text.bin english_strings.json text_modified.bin
    python texts_english_json_editor.py encode texts.texts english_strings.json texts_modified.texts
    python texts_english_json_editor.py verify text.bin
    python texts_english_json_editor.py verify texts.texts

Android (raw ``.texts`` only):

    python texts_english_json_editor.py decode texts.texts english_strings.json --keys texts.texts.keys
    python texts_english_json_editor.py encode texts.texts english_strings.json texts_modified.texts --keys texts.texts.keys
    python texts_english_json_editor.py verify texts.texts --keys texts.texts.keys

For Android, ``--keys`` may be omitted when the default sibling
``INPUT.texts.keys`` exists.


JSON format v8 records the source Babel/container format and exposes symbolic
string localization keys. Re-run decode with this editor before editing JSON
created by older JSON-format versions.

Safety / preservation behavior
==============================

* validates ZIP CRCs for supported Windows ZIP input;
* validates Babel bounds and UTF-8;
* validates Android key dictionaries and CRC32(name) values;
* verifies every stored Android localization hash can be resolved;
* rejects duplicate symbolic names and CRC32 collisions;
* preserves all language footers byte-for-byte;
* preserves Windows ZIP/raw representation;
* Android always remains raw ``.texts`` and never creates a ZIP container;
* with no Android structural key edits, non-English blocks remain byte-for-byte
  unchanged;
* Android delete removes the selected key from every language;
* validates both rebuilt Android localization bytes and rebuilt key-table bytes
  before writing either output;
* a no-op encode/verify must be byte-for-byte identical to the input.

Recovered Babel formats
=======================

Common localization header:

    4 bytes  magic = b"babl"
    u32 LE   version
    u32 LE   language_count

For each language:

    u32 LE   language_name_byte_length
    bytes    language_name (UTF-8)
    u32 LE   language_block_size
    u32 LE   entry_count

Babel version 1 (Windows) entry:

    u32 LE   key_byte_length
    bytes    key (UTF-8)
    u32 LE   value_byte_length
    bytes    value (UTF-8)

Babel version 2 (Android) entry:

    u32 LE   key_hash
    u32 LE   value_byte_length
    bytes    value (UTF-8)

After ``entry_count`` entries, any remaining bytes in the language block are an
opaque footer and are preserved exactly. ``language_block_size`` counts from the
``entry_count`` field through that footer.

Android companion key dictionary:

    4 bytes  magic = b"babl"
    u32 LE   version = 2
    u32 LE   key_count

    repeated key_count times:
        u32 LE   key_name_byte_length
        bytes    key_name (UTF-8)
        u32 LE   key_hash = CRC32(key_name UTF-8 bytes)
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import io
import json
import struct
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path


FORMAT_NAME = "minion-rush-textbin-english-editable"
FORMAT_VERSION = 8
INNER_MEMBER = "text/texts.texts"
TARGET_LANGUAGE = "EN"
SOURCE_RAW = "raw"
SOURCE_ZIP = "zip"
KEY_MODE_BY_BABEL_VERSION = {1: "string", 2: "hash32"}


class TextBinError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_u32(buf: bytes, off: int, what: str, limit: int | None = None) -> tuple[int, int]:
    end = len(buf) if limit is None else min(limit, len(buf))
    if off < 0 or off + 4 > end:
        raise TextBinError(f"Unexpected EOF reading {what} at 0x{off:X}")
    return struct.unpack_from("<I", buf, off)[0], off + 4


def read_blob(
    buf: bytes, off: int, size: int, what: str, limit: int | None = None
) -> tuple[bytes, int]:
    end = len(buf) if limit is None else min(limit, len(buf))
    if size < 0 or off < 0 or off + size > end:
        raise TextBinError(
            f"Unexpected EOF reading {what}: offset=0x{off:X}, size={size}"
        )
    return buf[off : off + size], off + size


def decode_utf8(raw: bytes, what: str, offset: int) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TextBinError(f"{what} is not UTF-8 at 0x{offset:X}") from exc


def format_key_hash(key_hash: int) -> str:
    return f"0x{key_hash:08X}"



@dataclass(frozen=True)
class BabelEntry:
    key: str
    key_raw: bytes
    value: str


@dataclass
class LanguageBlock:
    name: str
    start_offset: int
    block_size_offset: int
    content_end: int
    entries: list[BabelEntry]
    footer: bytes


@dataclass
class BabelFile:
    version: int
    key_mode: str
    languages: list[LanguageBlock]
    raw: bytes

    def language(self, name: str) -> LanguageBlock:
        matches = [language for language in self.languages if language.name == name]
        if len(matches) != 1:
            raise TextBinError(
                f"Expected exactly one language {name!r}, found {len(matches)}"
            )
        return matches[0]


@dataclass(frozen=True)
class TextSource:
    path: Path
    kind: str
    raw: bytes
    babel_raw: bytes
    babel: BabelFile
    zip_info: zipfile.ZipInfo | None = None

    @property
    def kind_label(self) -> str:
        return "ZIP text.bin" if self.kind == SOURCE_ZIP else "raw texts.texts"


@dataclass(frozen=True)
class AndroidKeyTable:
    path: Path
    raw: bytes
    names: tuple[str, ...]
    name_to_hash: dict[str, int]
    hash_to_name: dict[int, str]

    @property
    def count(self) -> int:
        return len(self.names)


def default_android_keys_path(texts_path: Path) -> Path:
    """Return the implicit Android companion path: NAME.texts -> NAME.texts.keys."""
    return texts_path.with_name(texts_path.name + ".keys")


def require_android_texts_source(source: TextSource) -> None:
    """Reject every Android Babel v2 input except a raw file named *.texts."""
    if source.babel.version != 2:
        return
    if source.kind != SOURCE_RAW:
        raise TextBinError(
            "Android Babel v2 accepts only a raw .texts localization file; "
            "ZIP/.bin/.zip containers are not supported"
        )
    if source.path.suffix.lower() != ".texts":
        raise TextBinError(
            f"Android Babel v2 input must have a .texts filename, got: {source.path}"
        )


def require_android_texts_output(source: TextSource, output_path: Path) -> None:
    """Require Android encode output to remain a raw *.texts file."""
    if source.babel.version == 2 and output_path.suffix.lower() != ".texts":
        raise TextBinError(
            f"Android Babel v2 output must have a .texts filename, got: {output_path}"
        )


def parse_android_key_table_bytes(data: bytes, path: Path) -> AndroidKeyTable:
    if len(data) < 12 or data[:4] != b"babl":
        raise TextBinError(
            f"Android key file {path} is not a supported Babel key table: "
            f"magic={data[:4]!r}"
        )

    version, off = read_u32(data, 4, "Android key-table version")
    if version != 2:
        raise TextBinError(
            f"Android key file {path} uses Babel version {version}; expected 2"
        )
    count, off = read_u32(data, off, "Android key count")

    if count > (len(data) - 12) // 8:
        raise TextBinError(
            f"Android key count {count} cannot fit in {len(data)} bytes"
        )

    names: list[str] = []
    name_to_hash: dict[str, int] = {}
    hash_to_name: dict[int, str] = {}

    for index in range(count):
        name_len, off = read_u32(data, off, f"Android key[{index}] name length")
        name_offset = off
        name_raw, off = read_blob(data, off, name_len, f"Android key[{index}] name")
        name = decode_utf8(name_raw, f"Android key[{index}] name", name_offset)
        key_hash, off = read_u32(data, off, f"Android key[{index}] hash")

        expected_hash = binascii.crc32(name_raw) & 0xFFFFFFFF
        if key_hash != expected_hash:
            raise TextBinError(
                f"Android key[{index}] {name!r} has stored hash "
                f"{format_key_hash(key_hash)}, but CRC32(name) is "
                f"{format_key_hash(expected_hash)}"
            )
        if name in name_to_hash:
            raise TextBinError(f"Duplicate Android symbolic key {name!r}")
        if key_hash in hash_to_name:
            raise TextBinError(
                "Android key-table hash collision: "
                f"{hash_to_name[key_hash]!r} and {name!r} both map to "
                f"{format_key_hash(key_hash)}"
            )

        names.append(name)
        name_to_hash[name] = key_hash
        hash_to_name[key_hash] = name

    if off != len(data):
        raise TextBinError(
            f"Trailing bytes after Android key table: parsed=0x{off:X}, "
            f"file=0x{len(data):X}"
        )

    return AndroidKeyTable(
        path=path,
        raw=data,
        names=tuple(names),
        name_to_hash=name_to_hash,
        hash_to_name=hash_to_name,
    )


def parse_android_key_table(path: Path) -> AndroidKeyTable:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TextBinError(f"Could not read Android key file {path}: {exc}") from exc
    return parse_android_key_table_bytes(data, path)


def android_key_hash(name: str) -> int:
    return binascii.crc32(name.encode("utf-8")) & 0xFFFFFFFF


def serialize_android_key_names(names: list[str] | tuple[str, ...]) -> bytes:
    if len(names) > 0xFFFFFFFF:
        raise TextBinError("Android key table contains too many entries")
    out = bytearray(b"babl" + struct.pack("<II", 2, len(names)))
    seen_names: set[str] = set()
    seen_hashes: dict[int, str] = {}
    for index, name in enumerate(names):
        if not isinstance(name, str):
            raise TextBinError(f"Android key[{index}] name must be a string")
        raw = name.encode("utf-8")
        if len(raw) > 0xFFFFFFFF:
            raise TextBinError(f"Android key {name!r} is too large")
        if name in seen_names:
            raise TextBinError(f"Duplicate Android symbolic key {name!r}")
        key_hash = binascii.crc32(raw) & 0xFFFFFFFF
        if key_hash in seen_hashes:
            raise TextBinError(
                "Android key-table hash collision: "
                f"{seen_hashes[key_hash]!r} and {name!r} both map to "
                f"{format_key_hash(key_hash)}"
            )
        seen_names.add(name)
        seen_hashes[key_hash] = name
        out += struct.pack("<I", len(raw))
        out += raw
        out += struct.pack("<I", key_hash)
    return bytes(out)


def output_android_keys_path(output_path: Path) -> Path:
    return default_android_keys_path(output_path)


def resolve_android_keys_path(input_path: Path, requested: Path | None) -> Path:
    if requested is not None:
        return requested
    default = default_android_keys_path(input_path)
    if not default.is_file():
        raise TextBinError(
            "Android Babel v2 requires a key dictionary. No --keys argument was "
            f"provided, and the default key file was not found: {default}"
        )
    return default


def load_required_android_keys(
    source: TextSource, requested: Path | None
) -> AndroidKeyTable | None:
    if source.babel.version != 2:
        return None
    require_android_texts_source(source)
    path = resolve_android_keys_path(source.path, requested)
    table = parse_android_key_table(path)
    validate_android_key_coverage(source.babel, table)
    return table


def entry_symbolic_key(
    babel: BabelFile, entry: BabelEntry, keys: AndroidKeyTable | None
) -> str:
    if babel.version == 1:
        return entry.key
    if keys is None:
        raise TextBinError("Internal error: Android key table is unavailable")
    key_hash = struct.unpack("<I", entry.key_raw)[0]
    try:
        return keys.hash_to_name[key_hash]
    except KeyError as exc:
        raise TextBinError(
            f"Android localization hash {format_key_hash(key_hash)} is missing "
            f"from key file {keys.path}"
        ) from exc


def validate_android_key_coverage(babel: BabelFile, keys: AndroidKeyTable) -> None:
    missing: dict[int, list[str]] = {}
    for language in babel.languages:
        for entry in language.entries:
            key_hash = struct.unpack("<I", entry.key_raw)[0]
            if key_hash not in keys.hash_to_name:
                missing.setdefault(key_hash, []).append(language.name)

    if missing:
        examples = []
        for key_hash, languages in list(missing.items())[:5]:
            examples.append(
                f"{format_key_hash(key_hash)} ({'/'.join(languages[:3])})"
            )
        raise TextBinError(
            f"Android key file {keys.path} does not resolve {len(missing)} "
            "localization hashes, e.g. " + ", ".join(examples)
        )


def validate_symbolic_key(key: object, what: str) -> str:
    if not isinstance(key, str):
        raise TextBinError(f"{what} must be a string")
    raw = key.encode("utf-8")
    if len(raw) > 0xFFFFFFFF:
        raise TextBinError(f"{what} is too large to encode")
    return key

def parse_entry_v1(
    data: bytes, off: int, content_end: int, language: str, index: int
) -> tuple[BabelEntry, int]:
    key_len, off = read_u32(data, off, f"{language}[{index}] key length", content_end)
    key_offset = off
    key_raw, off = read_blob(
        data, off, key_len, f"{language}[{index}] key", content_end
    )
    key = decode_utf8(key_raw, f"Key {index} in {language}", key_offset)

    value_len, off = read_u32(
        data, off, f"{language}[{index}] value length", content_end
    )
    value_offset = off
    value_raw, off = read_blob(
        data, off, value_len, f"{language}[{index}] value", content_end
    )
    value = decode_utf8(value_raw, f"Value for {key!r} in {language}", value_offset)
    return BabelEntry(key=key, key_raw=key_raw, value=value), off


def parse_entry_v2(
    data: bytes, off: int, content_end: int, language: str, index: int
) -> tuple[BabelEntry, int]:
    key_hash, off = read_u32(data, off, f"{language}[{index}] key hash", content_end)
    key_raw = struct.pack("<I", key_hash)
    key = format_key_hash(key_hash)

    value_len, off = read_u32(
        data, off, f"{language}[{index}] value length", content_end
    )
    value_offset = off
    value_raw, off = read_blob(
        data, off, value_len, f"{language}[{index}] value", content_end
    )
    value = decode_utf8(value_raw, f"Value for {key} in {language}", value_offset)
    return BabelEntry(key=key, key_raw=key_raw, value=value), off


def parse_babel(data: bytes) -> BabelFile:
    if len(data) < 12 or data[:4] != b"babl":
        raise TextBinError(
            f"Not a supported Babel file: magic={data[:4]!r}, expected b'babl'"
        )

    version, off = read_u32(data, 4, "Babel version")
    try:
        key_mode = KEY_MODE_BY_BABEL_VERSION[version]
    except KeyError as exc:
        supported = ", ".join(str(v) for v in sorted(KEY_MODE_BY_BABEL_VERSION))
        raise TextBinError(
            f"Unsupported Babel version {version}; supported versions: {supported}"
        ) from exc

    language_count, off = read_u32(data, off, "language count")
    if language_count > (len(data) - 12) // 12:
        raise TextBinError(
            f"Language count {language_count} cannot fit in {len(data)} bytes"
        )

    parse_entry = parse_entry_v1 if version == 1 else parse_entry_v2
    languages: list[LanguageBlock] = []
    seen_language_names: set[str] = set()

    for language_index in range(language_count):
        lang_start = off
        name_len, off = read_u32(data, off, f"language[{language_index}] name length")
        name_offset = off
        name_raw, off = read_blob(
            data, off, name_len, f"language[{language_index}] name"
        )
        name = decode_utf8(name_raw, f"language[{language_index}] name", name_offset)
        if name in seen_language_names:
            raise TextBinError(f"Duplicate language name {name!r}")
        seen_language_names.add(name)

        block_size_offset = off
        block_size, off = read_u32(data, off, f"language {name} block size")
        content_start = off
        content_end = content_start + block_size
        if block_size < 4:
            raise TextBinError(f"Language {name} block is too small for entry_count")
        if content_end > len(data):
            raise TextBinError(
                f"Language {name} block extends beyond file: "
                f"0x{content_start:X}+{block_size} > 0x{len(data):X}"
            )

        entry_count, off = read_u32(
            data, off, f"language {name} entry count", content_end
        )
        # Every supported entry needs at least two u32 fields.
        if entry_count > (block_size - 4) // 8:
            raise TextBinError(
                f"Entry count {entry_count} in {name} cannot fit in its "
                f"{block_size}-byte block"
            )

        entries: list[BabelEntry] = []
        seen_keys: set[str] = set()
        for entry_index in range(entry_count):
            entry, off = parse_entry(data, off, content_end, name, entry_index)
            if entry.key in seen_keys:
                raise TextBinError(
                    f"Duplicate key {entry.key!r} in language {name}"
                )
            seen_keys.add(entry.key)
            entries.append(entry)

        footer = data[off:content_end]
        off = content_end
        languages.append(
            LanguageBlock(
                name=name,
                start_offset=lang_start,
                block_size_offset=block_size_offset,
                content_end=content_end,
                entries=entries,
                footer=footer,
            )
        )

    if off != len(data):
        raise TextBinError(
            f"Trailing bytes after final language: parsed=0x{off:X}, file=0x{len(data):X}"
        )

    return BabelFile(
        version=version,
        key_mode=key_mode,
        languages=languages,
        raw=data,
    )


@dataclass(frozen=True)
class EditPlan:
    strings: dict[str, str]
    additions: dict[str, str]
    deletions: tuple[str, ...]
    delete_set: frozenset[str]


def validate_edit_plan(
    babel: BabelFile,
    keys: AndroidKeyTable | None,
    edited: object,
    additions: object,
    deletions: object,
) -> EditPlan:
    en = babel.language(TARGET_LANGUAGE)

    if not isinstance(edited, dict):
        raise TextBinError("JSON 'strings' must be an object mapping keys to text")
    if not isinstance(additions, dict):
        raise TextBinError("JSON 'add' must be an object mapping new keys to text")
    if not isinstance(deletions, list):
        raise TextBinError("JSON 'delete' must be an array of existing keys")

    checked_edited: dict[str, str] = {}
    for key, value in edited.items():
        key = validate_symbolic_key(key, "strings key")
        if not isinstance(value, str):
            raise TextBinError(
                f"strings[{key!r}] must be a JSON string, got {type(value).__name__}"
            )
        checked_edited[key] = value

    checked_additions: dict[str, str] = {}
    for key, value in additions.items():
        key = validate_symbolic_key(key, "add key")
        if not isinstance(value, str):
            raise TextBinError(
                f"add[{key!r}] must be a JSON string, got {type(value).__name__}"
            )
        checked_additions[key] = value

    checked_deletions: list[str] = []
    for index, key in enumerate(deletions):
        checked_deletions.append(validate_symbolic_key(key, f"delete[{index}]"))

    original_order = [entry_symbolic_key(babel, entry, keys) for entry in en.entries]
    original_keys = set(original_order)
    if len(original_keys) != len(original_order):
        raise TextBinError("Resolved English symbolic keys are not unique")

    edited_keys = set(checked_edited)
    missing = sorted(original_keys - edited_keys)
    extra = sorted(edited_keys - original_keys)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {len(missing)} original keys, e.g. {missing[:5]!r}")
        if extra:
            details.append(
                f"contains {len(extra)} non-original keys, e.g. {extra[:5]!r}"
            )
        raise TextBinError(
            "JSON 'strings' must contain exactly the original English key set; "
            "use 'add' and 'delete' for structural changes: "
            + "; ".join(details)
        )

    addition_keys = set(checked_additions)
    existing_additions = sorted(addition_keys & original_keys)
    if existing_additions:
        raise TextBinError(
            f"JSON 'add' contains {len(existing_additions)} keys that already exist, "
            f"e.g. {existing_additions[:5]!r}; modify their values under 'strings' instead"
        )

    delete_set = set(checked_deletions)
    if len(delete_set) != len(checked_deletions):
        raise TextBinError("JSON 'delete' contains duplicate keys")
    unknown_deletions = sorted(delete_set - original_keys)
    if unknown_deletions:
        raise TextBinError(
            f"JSON 'delete' contains {len(unknown_deletions)} unknown keys, "
            f"e.g. {unknown_deletions[:5]!r}"
        )

    final_existing = [key for key in original_order if key not in delete_set]
    final_names = final_existing + list(checked_additions)
    if len(set(final_names)) != len(final_names):
        seen: set[str] = set()
        duplicates: list[str] = []
        for name in final_names:
            if name in seen and name not in duplicates:
                duplicates.append(name)
            seen.add(name)
        raise TextBinError(
            "Structural edits produce duplicate final localization keys, e.g. "
            f"{duplicates[:5]!r}"
        )

    if babel.version == 2:
        if keys is None:
            raise TextBinError("Internal error: Android key table is unavailable")
        # Validate the complete future key table, including names not present in EN.
        future_names = [name for name in keys.names if name not in delete_set]
        future_names.extend(checked_additions)
        serialize_android_key_names(future_names)  # validates duplicate names/hashes

    return EditPlan(
        strings=checked_edited,
        additions=checked_additions,
        deletions=tuple(checked_deletions),
        delete_set=frozenset(delete_set),
    )


def encode_windows_key(key: str) -> bytes:
    raw = key.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def serialize_windows_english(original: BabelFile, plan: EditPlan) -> bytes:
    en = original.language(TARGET_LANGUAGE)
    final_count = len(en.entries) - len(plan.delete_set) + len(plan.additions)
    if not 0 <= final_count <= 0xFFFFFFFF:
        raise TextBinError(f"Invalid rebuilt English entry count: {final_count}")

    content = bytearray(struct.pack("<I", final_count))
    for entry in en.entries:
        key = entry.key
        if key in plan.delete_set:
            continue
        value_raw = plan.strings[key].encode("utf-8")
        if len(value_raw) > 0xFFFFFFFF:
            raise TextBinError(f"Value for {key!r} is too large")
        content += struct.pack("<I", len(entry.key_raw))
        content += entry.key_raw
        content += struct.pack("<I", len(value_raw))
        content += value_raw

    for key, value in plan.additions.items():
        value_raw = value.encode("utf-8")
        if len(value_raw) > 0xFFFFFFFF:
            raise TextBinError(f"Value for added key {key!r} is too large")
        content += encode_windows_key(key)
        content += struct.pack("<I", len(value_raw))
        content += value_raw

    content += en.footer
    if len(content) > 0xFFFFFFFF:
        raise TextBinError("Rebuilt English language block exceeds u32 size")

    raw = original.raw
    header_before_size = raw[en.start_offset : en.block_size_offset]
    rebuilt_en = header_before_size + struct.pack("<I", len(content)) + bytes(content)
    return raw[: en.start_offset] + rebuilt_en + raw[en.content_end :]


def serialize_android_babel(
    original: BabelFile, keys: AndroidKeyTable, plan: EditPlan
) -> bytes:
    # Rebuild every language because delete is a global Android key operation.
    out = bytearray(original.raw[:12])
    for language in original.languages:
        additions = plan.additions if language.name == TARGET_LANGUAGE else {}
        kept_count = 0
        content = bytearray(b"\0\0\0\0")  # patch entry_count after filtering

        for entry in language.entries:
            old_key = entry_symbolic_key(original, entry, keys)
            if old_key in plan.delete_set:
                continue
            value = plan.strings[old_key] if language.name == TARGET_LANGUAGE else entry.value
            value_raw = value.encode("utf-8")
            if len(value_raw) > 0xFFFFFFFF:
                raise TextBinError(f"Value for {old_key!r} in {language.name} is too large")
            content += entry.key_raw
            content += struct.pack("<I", len(value_raw))
            content += value_raw
            kept_count += 1

        for key, value in additions.items():
            value_raw = value.encode("utf-8")
            if len(value_raw) > 0xFFFFFFFF:
                raise TextBinError(f"Value for added key {key!r} is too large")
            content += struct.pack("<I", android_key_hash(key))
            content += struct.pack("<I", len(value_raw))
            content += value_raw
            kept_count += 1

        struct.pack_into("<I", content, 0, kept_count)
        content += language.footer
        if len(content) > 0xFFFFFFFF:
            raise TextBinError(
                f"Rebuilt Android language block {language.name} exceeds u32 size"
            )

        header_before_size = original.raw[
            language.start_offset : language.block_size_offset
        ]
        out += header_before_size
        out += struct.pack("<I", len(content))
        out += content

    return bytes(out)


def serialize_localization(
    original: BabelFile,
    keys: AndroidKeyTable | None,
    plan: EditPlan,
) -> bytes:
    if original.version == 1:
        return serialize_windows_english(original, plan)
    if original.version == 2:
        if keys is None:
            raise TextBinError("Internal error: Android key table is unavailable")
        return serialize_android_babel(original, keys, plan)
    raise TextBinError(f"Internal error: unsupported Babel version {original.version}")


def build_updated_android_key_table(
    original: AndroidKeyTable,
    plan: EditPlan,
    output_path: Path,
) -> AndroidKeyTable:
    names: list[str] = []
    for name in original.names:
        if name in plan.delete_set:
            continue
        names.append(name)
    names.extend(plan.additions)

    if tuple(names) == original.names:
        raw = original.raw
    else:
        raw = serialize_android_key_names(names)
    return parse_android_key_table_bytes(raw, output_path)

def read_validated_zip_member(
    zf: zipfile.ZipFile, member: str = INNER_MEMBER
) -> tuple[bytes, zipfile.ZipInfo]:
    infos = [info for info in zf.infolist() if info.filename == member]
    if len(infos) != 1:
        raise TextBinError(f"Expected exactly one ZIP member {member!r}, found {len(infos)}")
    bad = zf.testzip()
    if bad is not None:
        raise TextBinError(f"ZIP CRC check failed for member {bad!r}")
    info = infos[0]
    return zf.read(info), info


def extract_zip_babel(data: bytes, context: str) -> tuple[bytes, zipfile.ZipInfo]:
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            return read_validated_zip_member(zf)
    except zipfile.BadZipFile as exc:
        raise TextBinError(
            f"{context} is neither a raw Babel texts.texts file nor a valid ZIP text.bin"
        ) from exc


def require_stored_member(info: zipfile.ZipInfo, context: str = "Input") -> None:
    if info.compress_type != zipfile.ZIP_STORED:
        raise TextBinError(
            f"{context} {INNER_MEMBER} uses ZIP compression method "
            f"{info.compress_type}; expected STORED (0)"
        )


@dataclass(frozen=True)
class CentralEntry:
    central_offset: int
    filename_raw: bytes
    flags: int
    method: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    local_header_offset: int


def find_eocd(raw: bytes) -> tuple[int, tuple]:
    start = max(0, len(raw) - (22 + 0xFFFF))
    off = raw.rfind(b"PK\x05\x06", start)
    if off < 0 or off + 22 > len(raw):
        raise TextBinError("ZIP EOCD record not found or truncated")
    fields = struct.unpack_from("<4s4H2IH", raw, off)
    comment_len = fields[-1]
    if off + 22 + comment_len != len(raw):
        raise TextBinError("ZIP EOCD/comment length mismatch")
    return off, fields


def parse_central_directory(raw: bytes) -> tuple[int, list[CentralEntry], int]:
    eocd_off, eocd = find_eocd(raw)
    (
        _sig,
        disk_no,
        cd_disk,
        entries_this_disk,
        total_entries,
        cd_size,
        cd_offset,
        _comment_len,
    ) = eocd

    if disk_no != 0 or cd_disk != 0 or entries_this_disk != total_entries:
        raise TextBinError("Multi-disk ZIP archives are not supported")
    if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF or total_entries == 0xFFFF:
        raise TextBinError("ZIP64 text.bin is not supported")
    if cd_offset + cd_size > eocd_off:
        raise TextBinError("Invalid ZIP central-directory bounds")

    entries: list[CentralEntry] = []
    off = cd_offset
    for index in range(total_entries):
        if off + 46 > len(raw) or raw[off : off + 4] != b"PK\x01\x02":
            raise TextBinError(f"Invalid central-directory entry {index} at 0x{off:X}")
        fields = struct.unpack_from("<4s6H3I5H2I", raw, off)
        (
            _sig,
            _ver_made,
            _ver_needed,
            flags,
            method,
            _mtime,
            _mdate,
            crc32,
            csize,
            usize,
            name_len,
            extra_len,
            comment_len,
            _disk_start,
            _internal_attr,
            _external_attr,
            local_header_offset,
        ) = fields
        if csize == 0xFFFFFFFF or usize == 0xFFFFFFFF or local_header_offset == 0xFFFFFFFF:
            raise TextBinError("ZIP64 entry is not supported")
        end = off + 46 + name_len + extra_len + comment_len
        if end > len(raw):
            raise TextBinError("Truncated central-directory entry")
        entries.append(
            CentralEntry(
                central_offset=off,
                filename_raw=raw[off + 46 : off + 46 + name_len],
                flags=flags,
                method=method,
                crc32=crc32,
                compressed_size=csize,
                uncompressed_size=usize,
                local_header_offset=local_header_offset,
            )
        )
        off = end

    if off != cd_offset + cd_size:
        raise TextBinError(
            f"Central-directory size mismatch: parsed {off-cd_offset}, header says {cd_size}"
        )
    return cd_offset, entries, eocd_off


def filename_to_text(name_raw: bytes, flags: int) -> str:
    encoding = "utf-8" if flags & 0x800 else "cp437"
    try:
        return name_raw.decode(encoding)
    except UnicodeDecodeError:
        return name_raw.decode("utf-8", errors="replace")


def replace_stored_zip_member_raw(
    original_zip: bytes, member: str, new_payload: bytes
) -> bytes:
    """Replace one STORED member while preserving the rest of the ZIP layout."""
    cd_offset, entries, eocd_off = parse_central_directory(original_zip)
    matches = [
        entry
        for entry in entries
        if filename_to_text(entry.filename_raw, entry.flags) == member
    ]
    if len(matches) != 1:
        raise TextBinError(f"Expected one ZIP member {member!r}, found {len(matches)}")
    target = matches[0]

    if target.flags & 0x08:
        raise TextBinError("ZIP data descriptors are not supported for the target member")
    if target.method != zipfile.ZIP_STORED:
        raise TextBinError(
            f"Target member uses compression method {target.method}; expected STORED (0)"
        )

    lho = target.local_header_offset
    if lho + 30 > len(original_zip) or original_zip[lho : lho + 4] != b"PK\x03\x04":
        raise TextBinError("Invalid target local ZIP header")
    local = struct.unpack_from("<4s5H3I2H", original_zip, lho)
    (
        _sig,
        _ver_needed,
        local_flags,
        local_method,
        _mtime,
        _mdate,
        local_crc,
        local_csize,
        local_usize,
        local_name_len,
        local_extra_len,
    ) = local

    if local_flags != target.flags or local_method != target.method:
        raise TextBinError("Local/central ZIP metadata disagreement")
    local_variable_end = lho + 30 + local_name_len + local_extra_len
    if local_variable_end > cd_offset:
        raise TextBinError("Target local ZIP header overlaps the central directory")
    local_name = original_zip[lho + 30 : lho + 30 + local_name_len]
    if local_name != target.filename_raw:
        raise TextBinError("Local/central ZIP filename disagreement")
    if (
        local_crc != target.crc32
        or local_csize != target.compressed_size
        or local_usize != target.uncompressed_size
    ):
        raise TextBinError("Local/central ZIP size or CRC disagreement")

    data_start = local_variable_end
    data_end = data_start + target.compressed_size
    if data_end > cd_offset:
        raise TextBinError("Target ZIP payload overlaps the central directory")

    old_payload = original_zip[data_start:data_end]
    if new_payload == old_payload:
        return original_zip
    if len(new_payload) > 0xFFFFFFFF:
        raise TextBinError("New texts.texts exceeds standard ZIP u32 size")

    new_crc = binascii.crc32(new_payload) & 0xFFFFFFFF
    new_size = len(new_payload)
    delta = new_size - target.compressed_size
    out = bytearray(original_zip[:data_start] + new_payload + original_zip[data_end:])

    struct.pack_into("<I", out, lho + 14, new_crc)
    struct.pack_into("<I", out, lho + 18, new_size)
    struct.pack_into("<I", out, lho + 22, new_size)

    new_cd_offset = cd_offset + delta
    if not 0 <= new_cd_offset <= 0xFFFFFFFF:
        raise TextBinError("New central-directory offset outside standard ZIP range")

    for entry in entries:
        new_central_offset = entry.central_offset + delta
        if entry is target:
            struct.pack_into("<I", out, new_central_offset + 16, new_crc)
            struct.pack_into("<I", out, new_central_offset + 20, new_size)
            struct.pack_into("<I", out, new_central_offset + 24, new_size)

        if entry.local_header_offset >= data_end:
            new_lho = entry.local_header_offset + delta
            if not 0 <= new_lho <= 0xFFFFFFFF:
                raise TextBinError("Shifted local-header offset outside ZIP u32 range")
            struct.pack_into("<I", out, new_central_offset + 42, new_lho)

    struct.pack_into("<I", out, eocd_off + delta + 16, new_cd_offset)
    return bytes(out)


def load_text_source(path: Path) -> TextSource:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TextBinError(f"Could not read {path}: {exc}") from exc

    if raw[:4] == b"babl":
        kind = SOURCE_RAW
        babel_raw = raw
        info = None
    else:
        kind = SOURCE_ZIP
        babel_raw, info = extract_zip_babel(raw, str(path))
        require_stored_member(info)

    return TextSource(
        path=path,
        kind=kind,
        raw=raw,
        babel_raw=babel_raw,
        babel=parse_babel(babel_raw),
        zip_info=info,
    )


def build_output(source: TextSource, rebuilt_babel: bytes) -> bytes:
    if source.kind == SOURCE_RAW:
        return rebuilt_babel
    if source.kind == SOURCE_ZIP:
        return replace_stored_zip_member_raw(source.raw, INNER_MEMBER, rebuilt_babel)
    raise TextBinError(f"Internal error: unsupported source kind {source.kind!r}")


def extract_output_babel(source_kind: str, output: bytes) -> bytes:
    if source_kind == SOURCE_RAW:
        return output
    if source_kind == SOURCE_ZIP:
        babel_raw, info = extract_zip_babel(output, "Rebuilt output")
        require_stored_member(info, "Post-encode")
        return babel_raw
    raise TextBinError(f"Internal error: unsupported source kind {source_kind!r}")


def load_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as file:
            obj = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise TextBinError(f"Could not read JSON {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise TextBinError("Editable JSON root must be an object")
    return obj


def make_editable_json(
    source: TextSource, keys: AndroidKeyTable | None
) -> dict:
    en = source.babel.language(TARGET_LANGUAGE)
    source_meta = {
        "file": source.path.name,
        "container": source.kind,
        "sha256": sha256_bytes(source.raw),
        "size": len(source.raw),
        "babel_sha256": sha256_bytes(source.babel_raw),
        "babel_size": len(source.babel_raw),
    }
    if source.kind == SOURCE_ZIP:
        source_meta["inner_member"] = INNER_MEMBER

    result = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "babel_version": source.babel.version,
        "key_mode": "string",
        "source": source_meta,
        "language": TARGET_LANGUAGE,
        "string_count": len(en.entries),
        "strings": {
            entry_symbolic_key(source.babel, entry, keys): entry.value
            for entry in en.entries
        },
        "add": {},
        "delete": [],
    }
    return result


def validate_editable_json(
    obj: dict, source: TextSource, keys: AndroidKeyTable | None
) -> EditPlan:
    babel = source.babel
    if obj.get("format") != FORMAT_NAME:
        raise TextBinError(
            f"Unsupported JSON format {obj.get('format')!r}; expected {FORMAT_NAME!r}"
        )
    if obj.get("format_version") != FORMAT_VERSION:
        raise TextBinError(
            f"Unsupported format_version {obj.get('format_version')!r}; expected "
            f"{FORMAT_VERSION}. Re-run decode with this editor version."
        )
    if obj.get("babel_version") != babel.version:
        raise TextBinError(
            f"JSON babel_version is {obj.get('babel_version')!r}, but input uses "
            f"Babel {babel.version}"
        )
    if obj.get("key_mode") != "string":
        raise TextBinError(
            f"JSON key_mode must be 'string', got {obj.get('key_mode')!r}"
        )
    if obj.get("language") != TARGET_LANGUAGE:
        raise TextBinError(
            f"JSON language must be {TARGET_LANGUAGE!r}, got {obj.get('language')!r}"
        )

    meta = obj.get("source")
    if not isinstance(meta, dict):
        raise TextBinError("JSON source metadata is missing")
    if meta.get("container") != source.kind:
        raise TextBinError(
            f"JSON source container is {meta.get('container')!r}, but input is "
            f"{source.kind!r}; decode this exact representation again"
        )
    if meta.get("sha256") != sha256_bytes(source.raw):
        raise TextBinError(
            "Input does not match the exact file used to create this JSON:\n"
            f"  JSON source SHA-256: {meta.get('sha256')}\n"
            f"  input SHA-256:       {sha256_bytes(source.raw)}\n"
            f"  input file:          {source.path}\n"
            "Decode this exact input again before editing/encoding."
        )
    if meta.get("size") != len(source.raw):
        raise TextBinError(
            f"JSON source size is {meta.get('size')!r}, expected {len(source.raw)}"
        )
    if meta.get("babel_sha256") != sha256_bytes(source.babel_raw):
        raise TextBinError("JSON Babel SHA-256 does not match the input")
    if meta.get("babel_size") != len(source.babel_raw):
        raise TextBinError(
            f"JSON babel_size is {meta.get('babel_size')!r}, expected {len(source.babel_raw)}"
        )
    if source.kind == SOURCE_ZIP and meta.get("inner_member") != INNER_MEMBER:
        raise TextBinError(f"JSON source inner_member must be {INNER_MEMBER!r}")

    if babel.version == 2 and keys is None:
        raise TextBinError("Internal error: Android key table is unavailable")

    en = babel.language(TARGET_LANGUAGE)
    if obj.get("string_count") != len(en.entries):
        raise TextBinError(
            f"JSON string_count is {obj.get('string_count')!r}, expected {len(en.entries)}"
        )

    return validate_edit_plan(
        babel,
        keys,
        obj.get("strings"),
        obj.get("add", {}),
        obj.get("delete", []),
    )


def compare_language_headers(original: BabelFile, rebuilt: BabelFile) -> None:
    if original.version != rebuilt.version or original.key_mode != rebuilt.key_mode:
        raise TextBinError("Post-encode validation failed: Babel format changed")
    if [x.name for x in original.languages] != [x.name for x in rebuilt.languages]:
        raise TextBinError("Post-encode validation failed: language list changed")


def validate_windows_rebuilt(
    source: TextSource, rebuilt: BabelFile, plan: EditPlan
) -> None:
    original = source.babel
    compare_language_headers(original, rebuilt)

    for old, new in zip(original.languages, rebuilt.languages):
        if old.name == TARGET_LANGUAGE:
            continue
        old_raw = original.raw[old.start_offset : old.content_end]
        new_raw = rebuilt.raw[new.start_offset : new.content_end]
        if old_raw != new_raw:
            raise TextBinError(
                f"Post-encode validation failed: non-English language {old.name} changed"
            )

    old_en = original.language(TARGET_LANGUAGE)
    new_en = rebuilt.language(TARGET_LANGUAGE)
    if new_en.footer != old_en.footer:
        raise TextBinError("Post-encode validation failed: English footer changed")

    surviving = [entry for entry in old_en.entries if entry.key not in plan.delete_set]
    expected_order = [entry.key for entry in surviving] + list(plan.additions)
    actual_order = [entry.key for entry in new_en.entries]
    if actual_order != expected_order:
        raise TextBinError("Post-encode validation failed: English key order changed")

    for old, new in zip(surviving, new_en.entries):
        if old.key_raw != new.key_raw:
            raise TextBinError(
                f"Post-encode validation failed: raw key changed for {old.key!r}"
            )

    expected = {
        key: value for key, value in plan.strings.items() if key not in plan.delete_set
    }
    expected.update(plan.additions)
    actual = {entry.key: entry.value for entry in new_en.entries}
    if actual != expected:
        raise TextBinError(
            "Post-encode validation failed: English key/value table did not round-trip"
        )


def validate_android_rebuilt(
    source: TextSource,
    input_keys: AndroidKeyTable,
    output_keys: AndroidKeyTable,
    rebuilt: BabelFile,
    plan: EditPlan,
) -> None:
    original = source.babel
    compare_language_headers(original, rebuilt)
    validate_android_key_coverage(rebuilt, output_keys)

    for old_lang, new_lang in zip(original.languages, rebuilt.languages):
        if new_lang.footer != old_lang.footer:
            raise TextBinError(
                f"Post-encode validation failed: footer changed for {old_lang.name}"
            )

        expected_order: list[str] = []
        expected_values: list[str] = []
        for old_entry in old_lang.entries:
            old_key = entry_symbolic_key(original, old_entry, input_keys)
            if old_key in plan.delete_set:
                continue
            expected_order.append(old_key)
            expected_values.append(
                plan.strings[old_key]
                if old_lang.name == TARGET_LANGUAGE
                else old_entry.value
            )

        if old_lang.name == TARGET_LANGUAGE:
            expected_order.extend(plan.additions)
            expected_values.extend(plan.additions.values())

        actual_order = [
            entry_symbolic_key(rebuilt, entry, output_keys) for entry in new_lang.entries
        ]
        actual_values = [entry.value for entry in new_lang.entries]
        if actual_order != expected_order:
            raise TextBinError(
                f"Post-encode validation failed: key order/content changed unexpectedly "
                f"in {old_lang.name}"
            )
        if actual_values != expected_values:
            raise TextBinError(
                f"Post-encode validation failed: localization values changed unexpectedly "
                f"in {old_lang.name}"
            )

    # Without structural Android edits, every non-English language must remain exact.
    if not plan.delete_set:
        for old, new in zip(original.languages, rebuilt.languages):
            if old.name == TARGET_LANGUAGE:
                continue
            old_raw = original.raw[old.start_offset : old.content_end]
            new_raw = rebuilt.raw[new.start_offset : new.content_end]
            if old_raw != new_raw:
                raise TextBinError(
                    f"Post-encode validation failed: non-English language {old.name} "
                    "changed during a value-only/add-only edit"
                )


def validate_rebuilt_output(
    source: TextSource,
    input_keys: AndroidKeyTable | None,
    output_keys: AndroidKeyTable | None,
    rebuilt_output: bytes,
    plan: EditPlan,
) -> bytes:
    fresh_inner = extract_output_babel(source.kind, rebuilt_output)
    fresh = parse_babel(fresh_inner)

    if source.babel.version == 1:
        validate_windows_rebuilt(source, fresh, plan)
    else:
        if input_keys is None or output_keys is None:
            raise TextBinError("Internal error: Android key table is unavailable")
        validate_android_rebuilt(source, input_keys, output_keys, fresh, plan)
    return fresh_inner

def print_android_keys_summary(keys: AndroidKeyTable | None) -> None:
    if keys is None:
        return
    print(f"Android keys:          {keys.path}")
    print(f"Android key entries:   {keys.count}")


def command_decode(input_path: Path, json_path: Path, keys_path: Path | None) -> None:
    source = load_text_source(input_path)
    keys = load_required_android_keys(source, keys_path)
    en = source.babel.language(TARGET_LANGUAGE)
    editable = make_editable_json(source, keys)

    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with json_path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(editable, file, ensure_ascii=False, indent=2)
            file.write("\n")
    except OSError as exc:
        raise TextBinError(f"Could not write JSON {json_path}: {exc}") from exc

    print(f"Input:                 {input_path}")
    print(f"Input format:          {source.kind_label}")
    print(f"SHA-256:               {sha256_bytes(source.raw)}")
    if source.kind == SOURCE_ZIP:
        print(f"Babel member:          {INNER_MEMBER}")
    print(f"Babel size:            {len(source.babel_raw)}")
    print(f"Babel version:         {source.babel.version}")
    print(f"Babel key storage:     {source.babel.key_mode}")
    print("JSON key mode:         string")
    print_android_keys_summary(keys)
    print(f"Languages:             {len(source.babel.languages)}")
    print(f"English strings:       {len(en.entries)}")
    print(f"English footer:        {len(en.footer)} bytes")
    print(f"Output JSON:           {json_path}")


def command_encode(
    input_path: Path,
    json_path: Path,
    output_path: Path,
    keys_path: Path | None,
) -> None:
    source = load_text_source(input_path)
    input_keys = load_required_android_keys(source, keys_path)
    require_android_texts_output(source, output_path)
    obj = load_json(json_path)
    plan = validate_editable_json(obj, source, input_keys)

    rebuilt_inner = serialize_localization(source.babel, input_keys, plan)
    rebuilt_output = build_output(source, rebuilt_inner)

    output_keys: AndroidKeyTable | None = None
    output_keys_path: Path | None = None
    if source.babel.version == 2:
        assert input_keys is not None
        output_keys_path = output_android_keys_path(output_path)
        output_keys = build_updated_android_key_table(input_keys, plan, output_keys_path)

    fresh_inner = validate_rebuilt_output(
        source, input_keys, output_keys, rebuilt_output, plan
    )

    if (
        output_keys is not None
        and output_keys_path is not None
        and input_keys is not None
        and output_keys.raw != input_keys.raw
    ):
        try:
            same_key_path = output_keys_path.resolve() == input_keys.path.resolve()
        except OSError:
            same_key_path = output_keys_path.absolute() == input_keys.path.absolute()
        if same_key_path:
            raise TextBinError(
                "Android structural key edits would overwrite the input key dictionary "
                f"{input_keys.path}. Use a different output .texts filename or supply "
                "the source dictionary from a different --keys path."
            )

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_keys_path is not None:
            output_keys_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(rebuilt_output)
        if output_keys is not None and output_keys_path is not None:
            output_keys_path.write_bytes(output_keys.raw)
    except OSError as exc:
        raise TextBinError(f"Could not write encoded output: {exc}") from exc

    original_map = {
        entry_symbolic_key(source.babel, entry, input_keys): entry.value
        for entry in source.babel.language(TARGET_LANGUAGE).entries
    }
    changed_values = [
        key
        for key in original_map
        if key not in plan.delete_set and original_map[key] != plan.strings[key]
    ]
    final_count = (
        len(plan.strings) - len(plan.delete_set) + len(plan.additions)
    )

    print(f"Input:                 {input_path}")
    print(f"Input format:          {source.kind_label}")
    print(f"Editable JSON:         {json_path}")
    print(f"Output:                {output_path}")
    print(f"Babel version/storage: {source.babel.version} / {source.babel.key_mode}")
    print("JSON key mode:         string")
    print_android_keys_summary(input_keys)
    if output_keys is not None and output_keys_path is not None and input_keys is not None:
        print(f"Output Android keys:   {output_keys_path}")
        print(
            f"Android key entries:   {input_keys.count} -> {output_keys.count} "
            f"({output_keys.count-input_keys.count:+d})"
        )
        print(
            "Key-file no-op exact:  "
            + ("yes" if output_keys.raw == input_keys.raw else "no")
        )
    print(f"Original EN strings:   {len(plan.strings)}")
    print(f"Modified values:       {len(changed_values)}")
    print(f"Added keys:            {len(plan.additions)}")
    print(f"Deleted keys:          {len(plan.deletions)}")
    print(f"Final EN strings:      {final_count}")
    print(
        f"Babel size:            {len(source.babel_raw)} -> {len(fresh_inner)} "
        f"({len(fresh_inner)-len(source.babel_raw):+d})"
    )
    print(
        f"File size:             {len(source.raw)} -> {len(rebuilt_output)} "
        f"({len(rebuilt_output)-len(source.raw):+d})"
    )
    print(f"No-op byte-identical:  {'yes' if rebuilt_output == source.raw else 'no'}")
    print(f"Output SHA-256:        {sha256_bytes(rebuilt_output)}")
    if changed_values:
        print("Changed values:")
        for key in changed_values[:20]:
            print(f"  {key}")
        if len(changed_values) > 20:
            print(f"  ... and {len(changed_values)-20} more")
def command_verify(input_path: Path, keys_path: Path | None) -> None:
    source = load_text_source(input_path)
    keys = load_required_android_keys(source, keys_path)
    babel = source.babel
    en = babel.language(TARGET_LANGUAGE)

    mapping = {
        entry_symbolic_key(babel, entry, keys): entry.value
        for entry in en.entries
    }
    plan = validate_edit_plan(babel, keys, mapping, {}, [])
    rebuilt_inner = serialize_localization(babel, keys, plan)
    rebuilt_output = build_output(source, rebuilt_inner)
    validate_rebuilt_output(source, keys, keys, rebuilt_output, plan)

    if rebuilt_inner != source.babel_raw or rebuilt_output != source.raw:
        raise TextBinError("No-op round-trip was not byte-for-byte identical")
    if keys is not None:
        rebuilt_keys = build_updated_android_key_table(keys, plan, keys.path)
        if rebuilt_keys.raw != keys.raw:
            raise TextBinError("Android key-table no-op round-trip was not byte-identical")

    print(f"Input:                 {input_path}")
    print(f"Input format:          {source.kind_label}")
    print(f"SHA-256:               {sha256_bytes(source.raw)}")
    if source.kind == SOURCE_ZIP:
        assert source.zip_info is not None
        with zipfile.ZipFile(io.BytesIO(source.raw), "r") as zf:
            print(f"ZIP entries:           {len(zf.infolist())}")
        print(f"Babel member:          {INNER_MEMBER}")
        print(f"Inner compression:     {source.zip_info.compress_type} (STORED)")
    print(f"Babel size:            {len(source.babel_raw)}")
    print(f"Babel SHA-256:         {sha256_bytes(source.babel_raw)}")
    print(f"Babel magic/version:   babl / {babel.version}")
    print(f"Babel key storage:     {babel.key_mode}")
    print("JSON key mode:         string")
    print_android_keys_summary(keys)
    print(f"Languages:             {len(babel.languages)}")
    print(f"Language order:        {', '.join(x.name for x in babel.languages)}")
    print(f"English strings:       {len(en.entries)}")
    print(f"English footer bytes:  {len(en.footer)}")
    print("Babel no-op exact:     yes")
    print("File no-op exact:      yes")
    if keys is not None:
        print("Key-file no-op exact:  yes")

def add_keys_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--keys",
        type=Path,
        help=(
            "Android Babel v2 companion key dictionary. If omitted for Android, "
            "defaults to NAME.texts.keys beside the required raw NAME.texts input. "
            "Not required for Windows Babel v1."
        ),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Edit Minion Rush English localization via symbolic-key JSON; Windows "
            "accepts ZIP or raw input, while Android accepts raw .texts only"
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    decode = sub.add_parser("decode", help="Export all English strings to editable JSON")
    decode.add_argument("input", type=Path, help="Windows: ZIP or raw; Android: raw .texts only")
    decode.add_argument("json", type=Path, help="Output editable JSON")
    add_keys_argument(decode)

    encode = sub.add_parser("encode", help="Apply edited JSON; Android also writes a rebuilt .texts.keys")
    encode.add_argument("input", type=Path, help="Exact source used for decode; Android must be raw .texts")
    encode.add_argument("json", type=Path, help="Edited JSON")
    encode.add_argument("output", type=Path, help="Windows preserves container type; Android output must be .texts")
    add_keys_argument(encode)

    verify = sub.add_parser(
        "verify",
        help="Validate format, Android keys, and byte-exact no-op round trip",
    )
    verify.add_argument("input", type=Path, help="Windows: ZIP or raw; Android: raw .texts only")
    add_keys_argument(verify)
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        if args.command == "decode":
            command_decode(args.input, args.json, args.keys)
        elif args.command == "encode":
            command_encode(args.input, args.json, args.output, args.keys)
        elif args.command == "verify":
            command_verify(args.input, args.keys)
        else:
            parser.error(f"Unknown command {args.command!r}")
        return 0
    except (TextBinError, OSError, ValueError, struct.error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
