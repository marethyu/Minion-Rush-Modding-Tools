#!/usr/bin/env python3
"""
Minion Rush text.bin English localization JSON editor.

This tool edits EXISTING English localization values in:

    text.bin (ZIP container)
      -> text/texts.texts (Gameloft Babel "babl" table)
      -> language EN

It supports modifying existing values plus explicit key additions/deletions.
Renaming is represented as add(new_key) + delete(old_key).

Usage:
    python text_bin_english_json_editor.py decode text.bin english_strings.json
    python text_bin_english_json_editor.py encode text.bin english_strings.json text_modified.bin
    python text_bin_english_json_editor.py check  text.bin

Design goals:
  * export all English strings as an easy-to-edit JSON object
  * explicit "add" and "delete" sections for structural key edits
  * preserve every non-English language block byte-for-byte
  * preserve original key order and key bytes
  * preserve the per-language footer verbatim
  * minimally patch the outer ZIP instead of recreating it with zipfile
  * no-op encode is byte-for-byte identical to the original text.bin
  * validate the rebuilt ZIP/Babel data before writing the output
  * current CLI only; no legacy/import-API compatibility layer

The recovered inner format is:

    4 bytes  magic = b"babl"
    u32 LE   version
    u32 LE   language_count

    repeated language_count times:
        u32 LE   language_name_byte_length
        bytes    language_name (UTF-8/ASCII)
        u32 LE   language_block_size

        # language block begins here
        u32 LE   entry_count
        repeated entry_count times:
            u32 LE key_byte_length
            bytes  key (UTF-8)
            u32 LE value_byte_length
            bytes  value (UTF-8)
        bytes footer  # preserved verbatim; 17 bytes in the supplied file

language_block_size counts from entry_count through the footer.
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
FORMAT_VERSION = 2
INNER_MEMBER = "text/texts.texts"
TARGET_LANGUAGE = "EN"


class TextBinError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()



def read_u32(buf: bytes, off: int, what: str) -> tuple[int, int]:
    if off + 4 > len(buf):
        raise TextBinError(f"Unexpected EOF reading {what} at 0x{off:X}")
    return struct.unpack_from("<I", buf, off)[0], off + 4


def read_blob(buf: bytes, off: int, size: int, what: str) -> tuple[bytes, int]:
    if size < 0 or off + size > len(buf):
        raise TextBinError(
            f"Unexpected EOF reading {what}: offset=0x{off:X}, size={size}"
        )
    return buf[off : off + size], off + size


@dataclass
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
    languages: list[LanguageBlock]
    raw: bytes

    def language(self, name: str) -> LanguageBlock:
        matches = [x for x in self.languages if x.name == name]
        if len(matches) != 1:
            raise TextBinError(
                f"Expected exactly one language {name!r}, found {len(matches)}"
            )
        return matches[0]


def parse_babel(data: bytes) -> BabelFile:
    off = 0
    if len(data) < 12 or data[:4] != b"babl":
        got = data[:4]
        raise TextBinError(f"Not a supported Babel file: magic={got!r}, expected b'babl'")
    off = 4
    version, off = read_u32(data, off, "Babel version")
    language_count, off = read_u32(data, off, "language count")

    # Every language needs at least name_length + block_size + entry_count.
    max_languages = (len(data) - 12) // 12
    if language_count > max_languages:
        raise TextBinError(
            f"Language count {language_count} cannot fit in {len(data)} bytes"
        )

    languages: list[LanguageBlock] = []

    for language_index in range(language_count):
        lang_start = off
        name_len, off = read_u32(data, off, f"language[{language_index}] name length")
        name_raw, off = read_blob(data, off, name_len, f"language[{language_index}] name")
        try:
            name = name_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TextBinError(
                f"language[{language_index}] name is not UTF-8 at 0x{lang_start:X}"
            ) from exc

        block_size_offset = off
        block_size, off = read_u32(data, off, f"language {name} block size")
        content_start = off
        content_end = content_start + block_size
        if content_end > len(data):
            raise TextBinError(
                f"Language {name} block extends beyond file: "
                f"0x{content_start:X}+{block_size} > 0x{len(data):X}"
            )
        if block_size < 4:
            raise TextBinError(f"Language {name} block is too small for entry_count")

        entry_count, off = read_u32(data, off, f"language {name} entry count")
        max_entries = (block_size - 4) // 8  # two u32 lengths per entry, even for empty strings
        if entry_count > max_entries:
            raise TextBinError(
                f"Entry count {entry_count} in {name} cannot fit in its {block_size}-byte block"
            )

        entries: list[BabelEntry] = []
        seen = set()

        for entry_index in range(entry_count):
            if off + 4 > content_end:
                raise TextBinError(f"Truncated key length for entry {entry_index} in {name}")
            key_len, off = read_u32(data, off, f"{name}[{entry_index}] key length")
            key_offset = off
            if off + key_len > content_end:
                raise TextBinError(f"Key {entry_index} in {name} overruns its language block")
            key_raw, off = read_blob(data, off, key_len, f"{name}[{entry_index}] key")

            if off + 4 > content_end:
                raise TextBinError(f"Truncated value length for entry {entry_index} in {name}")
            value_len, off = read_u32(data, off, f"{name}[{entry_index}] value length")
            value_offset = off
            if off + value_len > content_end:
                raise TextBinError(f"Value for entry {entry_index} in {name} overruns its language block")
            value_raw, off = read_blob(data, off, value_len, f"{name}[{entry_index}] value")

            try:
                key = key_raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TextBinError(
                    f"Key {entry_index} in {name} is not UTF-8 at 0x{key_offset:X}"
                ) from exc
            try:
                value = value_raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TextBinError(
                    f"Value for {key!r} in {name} is not UTF-8 at 0x{value_offset:X}"
                ) from exc

            if key in seen:
                raise TextBinError(f"Duplicate key {key!r} in language {name}")
            seen.add(key)

            entries.append(
                BabelEntry(
                    key=key,
                    key_raw=key_raw,
                    value=value,
                )
            )

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
        languages=languages,
        raw=data,
    )


def _validate_edit_plan(
    en: LanguageBlock,
    edited: dict[str, str],
    additions: dict[str, str],
    deletions: list[str],
) -> set[str]:
    """Validate one English edit plan and return its deletion set."""
    if not isinstance(edited, dict):
        raise TextBinError("JSON 'strings' must be an object mapping keys to text")
    if not isinstance(additions, dict):
        raise TextBinError("JSON 'add' must be an object mapping new keys to text")
    if not isinstance(deletions, list):
        raise TextBinError("JSON 'delete' must be an array of existing key names")

    if any(not isinstance(key, str) for key in edited):
        raise TextBinError("Every localization key under 'strings' must be a string")
    if any(not isinstance(key, str) for key in additions):
        raise TextBinError("Every added localization key must be a string")
    for index, key in enumerate(deletions):
        if not isinstance(key, str):
            raise TextBinError(f"delete[{index}] must be a string key")

    original_key_set = {entry.key for entry in en.entries}
    edited_key_set = set(edited)
    missing = sorted(original_key_set - edited_key_set)
    extra = sorted(edited_key_set - original_key_set)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {len(missing)} original keys, e.g. {missing[:5]!r}")
        if extra:
            details.append(f"contains {len(extra)} non-original keys, e.g. {extra[:5]!r}")
        raise TextBinError(
            "JSON 'strings' must contain exactly the original English key set; "
            "use 'add' and 'delete' for structural changes: " + "; ".join(details)
        )

    for key, value in edited.items():
        if not isinstance(value, str):
            raise TextBinError(
                f"strings[{key!r}] must be a JSON string, got {type(value).__name__}"
            )

    add_key_set = set(additions)
    existing_adds = sorted(add_key_set & original_key_set)
    if existing_adds:
        raise TextBinError(
            f"JSON 'add' contains {len(existing_adds)} keys that already exist, "
            f"e.g. {existing_adds[:5]!r}; modify them under 'strings' instead"
        )
    for key, value in additions.items():
        if not isinstance(value, str):
            raise TextBinError(
                f"add[{key!r}] must be a JSON string, got {type(value).__name__}"
            )
        if len(key.encode("utf-8")) > 0xFFFFFFFF:
            raise TextBinError(f"Added key {key!r} is too large")

    delete_set = set(deletions)
    if len(delete_set) != len(deletions):
        raise TextBinError("JSON 'delete' contains duplicate keys")
    unknown_delete = sorted(delete_set - original_key_set)
    if unknown_delete:
        raise TextBinError(
            f"JSON 'delete' contains {len(unknown_delete)} unknown keys, "
            f"e.g. {unknown_delete[:5]!r}"
        )

    return delete_set


def serialize_english_only(
    original: BabelFile,
    edited: dict[str, str],
    additions: dict[str, str] | None = None,
    deletions: list[str] | None = None,
) -> bytes:
    """Rebuild only the EN language block.

    Existing entries retain their original order/key bytes. Explicitly deleted
    original entries are omitted. New entries are appended in JSON object order.
    """
    en = original.language(TARGET_LANGUAGE)
    additions = additions or {}
    deletions = deletions or []
    delete_set = _validate_edit_plan(en, edited, additions, deletions)

    final_count = len(en.entries) - len(delete_set) + len(additions)
    if final_count > 0xFFFFFFFF:
        raise TextBinError(f"Invalid rebuilt English entry count: {final_count}")

    content = bytearray(struct.pack("<I", final_count))

    for entry in en.entries:
        if entry.key in delete_set:
            continue
        value_raw = edited[entry.key].encode("utf-8")
        if len(value_raw) > 0xFFFFFFFF:
            raise TextBinError(f"Value for {entry.key!r} is too large")

        # Preserve original key bytes exactly for existing keys.
        content += struct.pack("<I", len(entry.key_raw))
        content += entry.key_raw
        content += struct.pack("<I", len(value_raw))
        content += value_raw

    # New keys are appended in JSON object order.
    for key, value in additions.items():
        key_raw = key.encode("utf-8")
        value_raw = value.encode("utf-8")
        if len(value_raw) > 0xFFFFFFFF:
            raise TextBinError(f"Value for added key {key!r} is too large")
        content += struct.pack("<I", len(key_raw))
        content += key_raw
        content += struct.pack("<I", len(value_raw))
        content += value_raw

    # Unknown Babel metadata after the entries is preserved exactly.
    content += en.footer
    if len(content) > 0xFFFFFFFF:
        raise TextBinError("Rebuilt English language block exceeds u32 size")

    raw = original.raw
    header_before_size = raw[en.start_offset:en.block_size_offset]
    rebuilt_en = header_before_size + struct.pack("<I", len(content)) + bytes(content)
    return raw[:en.start_offset] + rebuilt_en + raw[en.content_end:]


def _read_validated_zip_member(
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


def extract_inner(path: Path) -> tuple[bytes, zipfile.ZipInfo]:
    """Read and CRC-validate the Babel member in one ZIP open."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return _read_validated_zip_member(zf)
    except zipfile.BadZipFile as exc:
        raise TextBinError(f"{path} is not a valid ZIP-based text.bin") from exc


def _extract_inner_from_bytes(data: bytes) -> tuple[bytes, zipfile.ZipInfo]:
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            return _read_validated_zip_member(zf)
    except zipfile.BadZipFile as exc:
        raise TextBinError("rebuilt output is not a valid ZIP-based text.bin") from exc


@dataclass
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
    # EOCD can be followed by at most 65535 bytes of comment.
    start = max(0, len(raw) - (22 + 0xFFFF))
    off = raw.rfind(b"PK\x05\x06", start)
    if off < 0:
        raise TextBinError("ZIP EOCD record not found")
    if off + 22 > len(raw):
        raise TextBinError("Truncated ZIP EOCD")
    fields = struct.unpack_from("<4s4H2IH", raw, off)
    comment_len = fields[-1]
    if off + 22 + comment_len != len(raw):
        raise TextBinError("ZIP EOCD/comment length mismatch")
    return off, fields


def parse_central_directory(raw: bytes) -> tuple[int, int, list[CentralEntry], int]:
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
        filename_raw = raw[off + 46 : off + 46 + name_len]
        entries.append(
            CentralEntry(
                central_offset=off,
                filename_raw=filename_raw,
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
    return cd_offset, cd_size, entries, eocd_off


def filename_to_text(name_raw: bytes, flags: int) -> str:
    encoding = "utf-8" if (flags & 0x800) else "cp437"
    try:
        return name_raw.decode(encoding)
    except UnicodeDecodeError:
        return name_raw.decode("utf-8", errors="replace")


def replace_stored_zip_member_raw(original_zip: bytes, member: str, new_payload: bytes) -> bytes:
    """Replace one STORED ZIP member while preserving the original archive layout.

    This deliberately patches the original ZIP rather than asking zipfile to recreate
    it, so local/central extra fields, timestamps, attributes, ordering, comments, etc.
    remain byte-identical except where sizes/CRC/offsets must change.
    """
    cd_offset, _cd_size, entries, eocd_off = parse_central_directory(original_zip)
    matches = [e for e in entries if filename_to_text(e.filename_raw, e.flags) == member]
    if len(matches) != 1:
        raise TextBinError(f"Expected one ZIP member {member!r}, found {len(matches)}")
    target = matches[0]

    if target.flags & 0x08:
        raise TextBinError("ZIP data descriptors are not supported for the target member")
    if target.method != 0:
        raise TextBinError(
            f"Target member uses compression method {target.method}; expected STORED (0)"
        )

    lho = target.local_header_offset
    if lho + 30 > len(original_zip) or original_zip[lho : lho + 4] != b"PK\x03\x04":
        raise TextBinError("Invalid target local ZIP header")
    lf = struct.unpack_from("<4s5H3I2H", original_zip, lho)
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
    ) = lf
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

    # Insert the new payload first; every byte after the old payload shifts by delta.
    out = bytearray(original_zip[:data_start] + new_payload + original_zip[data_end:])

    # Patch target local header (located before payload, so its offset is unchanged).
    struct.pack_into("<I", out, lho + 14, new_crc)
    struct.pack_into("<I", out, lho + 18, new_size)
    struct.pack_into("<I", out, lho + 22, new_size)

    # Entire central directory shifts because it follows all local data.
    new_cd_offset = cd_offset + delta
    if new_cd_offset < 0 or new_cd_offset > 0xFFFFFFFF:
        raise TextBinError("New central-directory offset outside standard ZIP range")

    for entry in entries:
        new_central_offset = entry.central_offset + delta

        # The replaced member's central size/CRC fields.
        if entry is target:
            struct.pack_into("<I", out, new_central_offset + 16, new_crc)
            struct.pack_into("<I", out, new_central_offset + 20, new_size)
            struct.pack_into("<I", out, new_central_offset + 24, new_size)

        # Local entries that physically followed the replaced payload have shifted.
        new_lho = entry.local_header_offset
        if entry.local_header_offset >= data_end:
            new_lho += delta
            if new_lho < 0 or new_lho > 0xFFFFFFFF:
                raise TextBinError("Shifted local-header offset outside ZIP u32 range")
            struct.pack_into("<I", out, new_central_offset + 42, new_lho)

    new_eocd_off = eocd_off + delta
    struct.pack_into("<I", out, new_eocd_off + 16, new_cd_offset)

    return bytes(out)


def load_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise TextBinError(f"Could not read JSON {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise TextBinError("Editable JSON root must be an object")
    return obj


def make_editable_json(source_path: Path, outer_raw: bytes, inner_raw: bytes, babel: BabelFile) -> dict:
    en = babel.language(TARGET_LANGUAGE)
    strings = {entry.key: entry.value for entry in en.entries}
    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "source": {
            "file": source_path.name,
            "sha256": sha256_bytes(outer_raw),
            "size": len(outer_raw),
            "inner_member": INNER_MEMBER,
            "inner_sha256": sha256_bytes(inner_raw),
            "inner_size": len(inner_raw),
        },
        "language": TARGET_LANGUAGE,
        "string_count": len(strings),
        "strings": strings,
        "add": {},
        "delete": [],
    }


def validate_editable_json(
    obj: dict,
    source_path: Path,
    source_outer: bytes,
    babel: BabelFile,
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    if obj.get("format") != FORMAT_NAME:
        raise TextBinError(
            f"Unsupported JSON format {obj.get('format')!r}; expected {FORMAT_NAME!r}"
        )
    if obj.get("format_version") != FORMAT_VERSION:
        raise TextBinError(
            f"Unsupported format_version {obj.get('format_version')!r}; expected {FORMAT_VERSION}. "
            "Re-run decode with this editor version."
        )
    if obj.get("language") != TARGET_LANGUAGE:
        raise TextBinError(
            f"JSON language must be {TARGET_LANGUAGE!r}, got {obj.get('language')!r}"
        )

    source = obj.get("source")
    if not isinstance(source, dict):
        raise TextBinError("JSON source metadata is missing")
    expected_sha = source.get("sha256")
    actual_sha = sha256_bytes(source_outer)
    if expected_sha != actual_sha:
        raise TextBinError(
            "Input text.bin does not match the file used to create this JSON:\n"
            f"  JSON source SHA-256: {expected_sha}\n"
            f"  input SHA-256:       {actual_sha}\n"
            f"  input file:          {source_path}\n"
            "Decode this exact text.bin again before editing/encoding."
        )

    strings = obj.get("strings")
    additions = obj.get("add", {})
    deletions = obj.get("delete", [])

    en = babel.language(TARGET_LANGUAGE)
    if obj.get("string_count") != len(en.entries):
        raise TextBinError(
            f"JSON string_count is {obj.get('string_count')!r}, expected {len(en.entries)}"
        )

    _validate_edit_plan(en, strings, additions, deletions)
    return strings, additions, deletions

def compare_non_english_blocks(original: BabelFile, rebuilt: BabelFile) -> None:
    orig_names = [x.name for x in original.languages]
    new_names = [x.name for x in rebuilt.languages]
    if orig_names != new_names:
        raise TextBinError("Post-encode validation failed: language list changed")
    if original.version != rebuilt.version:
        raise TextBinError("Post-encode validation failed: Babel version changed")

    for old, new in zip(original.languages, rebuilt.languages):
        if old.name == TARGET_LANGUAGE:
            continue
        old_raw = original.raw[old.start_offset : old.content_end]
        new_raw = rebuilt.raw[new.start_offset : new.content_end]
        if old_raw != new_raw:
            raise TextBinError(
                f"Post-encode validation failed: non-English language {old.name} changed"
            )


def _validate_rebuilt_output(
    original: BabelFile,
    rebuilt_outer: bytes,
    strings: dict[str, str],
    additions: dict[str, str],
    deletions: list[str],
) -> bytes:
    """Fully validate rebuilt bytes before they are written to the output path."""
    fresh_inner, info = _extract_inner_from_bytes(rebuilt_outer)
    if info.compress_type != zipfile.ZIP_STORED:
        raise TextBinError(
            f"Post-encode validation failed: {INNER_MEMBER} is no longer STORED"
        )

    fresh = parse_babel(fresh_inner)
    compare_non_english_blocks(original, fresh)

    original_en = original.language(TARGET_LANGUAGE)
    fresh_en = fresh.language(TARGET_LANGUAGE)
    if fresh_en.footer != original_en.footer:
        raise TextBinError("Post-encode validation failed: English footer changed")

    delete_set = set(deletions)
    surviving = [entry for entry in original_en.entries if entry.key not in delete_set]
    expected_order = [entry.key for entry in surviving] + list(additions)
    actual_order = [entry.key for entry in fresh_en.entries]
    if actual_order != expected_order:
        raise TextBinError("Post-encode validation failed: English key order changed")

    # Existing keys must retain their original raw UTF-8 key bytes exactly.
    for old, new in zip(surviving, fresh_en.entries):
        if old.key_raw != new.key_raw:
            raise TextBinError(
                f"Post-encode validation failed: raw key bytes changed for {old.key!r}"
            )

    expected = {key: value for key, value in strings.items() if key not in delete_set}
    expected.update(additions)
    got = {entry.key: entry.value for entry in fresh_en.entries}
    if got != expected:
        raise TextBinError(
            "Post-encode validation failed: English key/value table did not round-trip"
        )

    return fresh_inner


def command_decode(input_path: Path, json_path: Path) -> None:
    outer_raw = input_path.read_bytes()
    inner_raw, info = extract_inner(input_path)
    if info.compress_type != zipfile.ZIP_STORED:
        raise TextBinError(
            f"{INNER_MEMBER} uses ZIP compression method {info.compress_type}; expected STORED"
        )
    babel = parse_babel(inner_raw)
    en = babel.language(TARGET_LANGUAGE)

    editable = make_editable_json(input_path, outer_raw, inner_raw, babel)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(editable, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Input:              {input_path}")
    print(f"SHA-256:            {sha256_bytes(outer_raw)}")
    print(f"Inner member:       {INNER_MEMBER}")
    print(f"Babel version:      {babel.version}")
    print(f"Languages:          {len(babel.languages)}")
    print(f"English strings:    {len(en.entries)}")
    print(f"English footer:     {len(en.footer)} bytes")
    print(f"Output JSON:        {json_path}")


def command_encode(input_path: Path, json_path: Path, output_path: Path) -> None:
    source_outer = input_path.read_bytes()
    source_inner, info = extract_inner(input_path)
    if info.compress_type != zipfile.ZIP_STORED:
        raise TextBinError(
            f"{INNER_MEMBER} uses ZIP compression method {info.compress_type}; expected STORED"
        )
    original = parse_babel(source_inner)
    obj = load_json(json_path)
    strings, additions, deletions = validate_editable_json(obj, input_path, source_outer, original)

    rebuilt_inner = serialize_english_only(original, strings, additions, deletions)
    rebuilt_outer = replace_stored_zip_member_raw(source_outer, INNER_MEMBER, rebuilt_inner)

    # Validate the exact bytes that will be written before touching the output path.
    fresh_inner = _validate_rebuilt_output(
        original, rebuilt_outer, strings, additions, deletions
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(rebuilt_outer)

    delete_set = set(deletions)
    expected = {k: v for k, v in strings.items() if k not in delete_set}
    expected.update(additions)
    original_map = {e.key: e.value for e in original.language(TARGET_LANGUAGE).entries}
    changed = [k for k in original_map if k not in delete_set and original_map[k] != strings[k]]
    noop = rebuilt_outer == source_outer

    print(f"Input:                 {input_path}")
    print(f"Editable JSON:         {json_path}")
    print(f"Output:                {output_path}")
    print(f"Original EN strings:   {len(strings)}")
    print(f"Modified keys:         {len(changed)}")
    print(f"Added keys:            {len(additions)}")
    print(f"Deleted keys:          {len(deletions)}")
    print(f"Final EN strings:      {len(expected)}")
    print(f"Inner size:            {len(source_inner)} -> {len(fresh_inner)} ({len(fresh_inner)-len(source_inner):+d})")
    print(f"Outer size:            {len(source_outer)} -> {len(rebuilt_outer)} ({len(rebuilt_outer)-len(source_outer):+d})")
    print(f"No-op byte-identical:  {'yes' if noop else 'no'}")
    print(f"Output SHA-256:        {sha256_bytes(rebuilt_outer)}")
    if changed:
        print("Changed keys:")
        for key in changed[:20]:
            print(f"  {key}")
        if len(changed) > 20:
            print(f"  ... and {len(changed)-20} more")


def command_check(input_path: Path) -> None:
    outer_raw = input_path.read_bytes()
    inner_raw, info = extract_inner(input_path)
    if info.compress_type != zipfile.ZIP_STORED:
        raise TextBinError(
            f"{INNER_MEMBER} uses ZIP compression method {info.compress_type}; expected STORED"
        )
    babel = parse_babel(inner_raw)
    en = babel.language(TARGET_LANGUAGE)
    with zipfile.ZipFile(input_path, "r") as zf:
        zip_entry_count = len(zf.infolist())

    print(f"Input:                 {input_path}")
    print(f"SHA-256:               {sha256_bytes(outer_raw)}")
    print(f"ZIP entries:           {zip_entry_count}")
    print(f"Inner member:          {INNER_MEMBER}")
    print(f"Inner compression:     {info.compress_type} ({'STORED' if info.compress_type == 0 else 'other'})")
    print(f"Inner size:            {len(inner_raw)}")
    print(f"Inner SHA-256:         {sha256_bytes(inner_raw)}")
    print(f"Babel magic/version:   babl / {babel.version}")
    print(f"Languages:             {len(babel.languages)}")
    print(f"Language order:        {', '.join(x.name for x in babel.languages)}")
    print(f"English strings:       {len(en.entries)}")
    print(f"English footer bytes:  {len(en.footer)}")

    # Prove that rebuilding EN from the parsed values is a byte-exact no-op.
    mapping = {e.key: e.value for e in en.entries}
    rebuilt_inner = serialize_english_only(babel, mapping)
    rebuilt_outer = replace_stored_zip_member_raw(outer_raw, INNER_MEMBER, rebuilt_inner)
    print(f"Inner no-op exact:     {'yes' if rebuilt_inner == inner_raw else 'NO'}")
    print(f"Outer no-op exact:     {'yes' if rebuilt_outer == outer_raw else 'NO'}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Edit/add/delete English strings in Minion Rush text.bin via JSON"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_decode = sub.add_parser("decode", help="Export all English strings to editable JSON")
    p_decode.add_argument("input", type=Path, help="Original text.bin")
    p_decode.add_argument("json", type=Path, help="Output editable JSON")

    p_encode = sub.add_parser("encode", help="Apply edited English JSON to text.bin")
    p_encode.add_argument("input", type=Path, help="The exact original text.bin used for decode")
    p_encode.add_argument("json", type=Path, help="Edited JSON")
    p_encode.add_argument("output", type=Path, help="Output modified text.bin")

    p_check = sub.add_parser("check", help="Validate format and exact no-op round trip")
    p_check.add_argument("input", type=Path, help="text.bin to inspect")

    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        if args.command == "decode":
            command_decode(args.input, args.json)
        elif args.command == "encode":
            command_encode(args.input, args.json, args.output)
        elif args.command == "check":
            command_check(args.input)
        else:
            parser.error(f"Unknown command {args.command!r}")
        return 0
    except (TextBinError, OSError, ValueError, struct.error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
