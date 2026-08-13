#!/usr/bin/env python3
"""
Minion Rush JPK Tool
====================

A safe ZIP-container utility for the Minion Rush Windows ``.jpk`` archives
examined in this project.

IMPORTANT FORMAT NOTE
---------------------
The tested Minion Rush ``.jpk`` files are ordinary PKZIP/ZIP archives with a
``.jpk`` extension.  They begin with the normal ``PK`` signatures and are
readable by Python's :mod:`zipfile` module and Windows after renaming to
``.zip``.

This tool is nevertheless intentionally *template based*: when modifying an
archive it copies the original entry list and ZIP metadata and substitutes only
the requested payloads.  This avoids common repacking mistakes such as adding
an extra containing directory, changing archive paths, changing per-entry
compression methods, or accidentally retaining the old file alongside the new
one.

The tool preserves, for every existing entry:

* archive pathname and entry ordering;
* compression method (stored/deflated/etc. where supported by Python);
* DOS timestamp;
* ZIP extra fields;
* internal/external attributes;
* creator/extractor versions;
* flag bits where Python permits them;
* entry comment;
* archive comment.

CRC32 and compressed/uncompressed sizes for replaced files are recalculated by
ZIP automatically, as required by the format.

Unchanged entries are decompressed and recompressed when an archive is patched.
Their *contents and ZIP metadata* are preserved, but their raw compressed byte
streams are not guaranteed to be bit-identical to the input archive.  This is
normally irrelevant to ZIP readers and to the tested game archives.  Use the
``compare`` command to verify logical equivalence.

COMMANDS
--------

List archive entries::

    python jpk.py list archive.jpk

Show detailed archive/entry information::

    python jpk.py info archive.jpk
    python jpk.py info archive.jpk designlib.blibclara

Extract one entry::

    python jpk.py extract archive.jpk designlib.blibclara designlib.blibclara

Extract all entries safely::

    python jpk.py extract-all archive.jpk output_directory

Replace exactly one existing archive entry while using the original JPK as the
layout/metadata template::

    python jpk.py replace original.jpk designlib.blibclara \
        modified_designlib.blibclara patched.jpk

Patch several entries at once.  Each ``--replace`` is ``ARCHIVE_PATH=FILE``::

    python jpk.py patch original.jpk patched.jpk \
        --replace designlib.blibclara=modified_designlib.blibclara \
        --replace shoplib.blibclara=modified_shoplib.blibclara

Synchronize files from a directory against an existing JPK.  Only paths that
already exist in the template archive are replaced by default::

    python jpk.py sync original.jpk edited_directory patched.jpk

``sync`` is useful after extracting a JPK, editing files in place, and wanting
to rebuild it without introducing a containing directory.  New files are *not*
added unless ``--add-new`` is specified.

Delete an existing entry::

    python jpk.py delete original.jpk obsolete/path.bin patched.jpk

Verify ZIP integrity, duplicate names, suspicious absolute/traversal paths, and
entry readability::

    python jpk.py verify archive.jpk

Compare two JPKs logically (entry names, order, metadata, size, and CRC)::

    python jpk.py compare original.jpk patched.jpk

Add ``--deep`` to byte-compare uncompressed entry contents even when CRC/size
match::

    python jpk.py compare original.jpk patched.jpk --deep

COMMON SAFE WORKFLOW
--------------------

1. Keep the game's original JPK unchanged as the template.
2. Extract the file to edit, or the entire archive.
3. Modify the extracted asset (for example with ``blibclara_editor``).
4. Use ``replace`` or ``sync`` against the *original* JPK.
5. Run ``verify`` on the output.
6. Optionally run ``compare`` to see exactly which entries differ.

Do not create a ZIP by selecting the containing extraction directory in Windows;
that commonly produces ``folder/designlib.blibclara`` instead of the required
root path ``designlib.blibclara``.

LIMITATIONS
-----------

* This is a ZIP/JPK container tool; it does not understand ``.blibclara``
  semantics.
* It does not preserve the exact compressed byte stream or exact central-
  directory bytes of unchanged entries.  It preserves logical ZIP metadata and
  data.
* Encrypted ZIP entries are not supported for modification.
* Unsupported ZIP compression methods depend on the Python runtime's ``zipfile``
  support.
* ZIP signatures, external manifests, or hashes outside the JPK (if a future
  game build uses them) are outside this tool's scope.

Extraction rejects absolute, traversal, UNC, and Windows drive-qualified member paths.

Python 3.9+ recommended.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


class JPKError(Exception):
    pass


def _fmt_size(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{n} B"


def _method_name(method: int) -> str:
    names = {
        zipfile.ZIP_STORED: "stored",
        zipfile.ZIP_DEFLATED: "deflate",
        getattr(zipfile, "ZIP_BZIP2", -999): "bzip2",
        getattr(zipfile, "ZIP_LZMA", -998): "lzma",
    }
    return names.get(method, f"method-{method}")


def _has_windows_drive_prefix(name: str) -> bool:
    # A ZIP entry such as C:/Windows/file is not absolute to PurePosixPath, but
    # is drive-qualified on Windows and must be treated as unsafe.
    return re.match(r"^[A-Za-z]:($|/)", name) is not None


def _normalize_archive_name(name: str) -> str:
    # ZIP convention always uses '/'. Reject absolute, drive-qualified, NUL,
    # or traversing names for modification requests.
    if not isinstance(name, str):
        raise JPKError("archive path must be text")
    if "\x00" in name:
        raise JPKError("archive path contains NUL")
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if not name:
        raise JPKError("archive path is empty")
    p = PurePosixPath(name)
    if p.is_absolute() or _has_windows_drive_prefix(name) or any(part == ".." for part in p.parts):
        raise JPKError(f"unsafe archive path: {name!r}")
    return str(p)


def _safe_output_path(root: Path, archive_name: str) -> Path:
    normalized = archive_name.replace("\\", "/")
    p = PurePosixPath(normalized)
    if ("\x00" in archive_name or p.is_absolute() or _has_windows_drive_prefix(normalized)
            or any(part in ("..", "") for part in p.parts)):
        raise JPKError(f"refusing unsafe archive member path: {archive_name!r}")
    target = root.joinpath(*p.parts)
    root_real = root.resolve()
    # parent might not exist yet; resolve(strict=False) is available.
    target_real = target.resolve(strict=False)
    try:
        target_real.relative_to(root_real)
    except ValueError:
        raise JPKError(f"archive member escapes extraction directory: {archive_name!r}")
    return target


def _clone_zipinfo(src: zipfile.ZipInfo, *, filename: Optional[str] = None) -> zipfile.ZipInfo:
    """Clone user-visible ZIP metadata for rewriting with zipfile."""
    zi = zipfile.ZipInfo(filename=filename if filename is not None else src.filename,
                        date_time=src.date_time)
    zi.compress_type = src.compress_type
    zi.comment = src.comment
    zi.extra = src.extra
    zi.create_system = src.create_system
    zi.create_version = src.create_version
    zi.extract_version = src.extract_version
    zi.reserved = src.reserved
    # Preserve non-data-descriptor/non-encryption flags. zipfile will manage
    # bits needed by its own writer. UTF-8 filename bit is derived automatically.
    zi.flag_bits = src.flag_bits & ~(0x1 | 0x8)
    zi.volume = src.volume
    zi.internal_attr = src.internal_attr
    zi.external_attr = src.external_attr
    return zi


def _open_zip(path: Path, mode: str = "r") -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path, mode)
    except (OSError, zipfile.BadZipFile) as e:
        raise JPKError(f"cannot open {path}: {e}") from e


def _duplicates(infos: Sequence[zipfile.ZipInfo]) -> Dict[str, List[int]]:
    d: Dict[str, List[int]] = {}
    for i, zi in enumerate(infos):
        d.setdefault(zi.filename, []).append(i)
    return {k: v for k, v in d.items() if len(v) > 1}


def _require_unique_entry(zf: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    name = _normalize_archive_name(name)
    matches = [zi for zi in zf.infolist() if zi.filename == name]
    if not matches:
        raise JPKError(f"entry not found: {name}")
    if len(matches) != 1:
        raise JPKError(f"entry name occurs {len(matches)} times; refusing ambiguous operation: {name}")
    return matches[0]


def cmd_list(args: argparse.Namespace) -> None:
    path = Path(args.archive)
    with _open_zip(path) as zf:
        infos = zf.infolist()
        if args.names_only:
            for zi in infos:
                print(zi.filename)
            return
        print(f"Archive: {path}")
        print(f"Entries: {len(infos)}")
        print(f"{'#':>4} {'Method':<9} {'Original':>12} {'Packed':>12} {'CRC32':>8}  Name")
        for i, zi in enumerate(infos):
            print(f"{i:4d} {_method_name(zi.compress_type):<9} {zi.file_size:12d} "
                  f"{zi.compress_size:12d} {zi.CRC:08X}  {zi.filename}")


def _print_entry_info(zi: zipfile.ZipInfo) -> None:
    print(f"Name:              {zi.filename}")
    print(f"Uncompressed:      {zi.file_size} ({_fmt_size(zi.file_size)})")
    print(f"Compressed:        {zi.compress_size} ({_fmt_size(zi.compress_size)})")
    print(f"Compression:       {_method_name(zi.compress_type)} ({zi.compress_type})")
    print(f"CRC32:             {zi.CRC:08X}")
    print(f"Timestamp:         {zi.date_time}")
    print(f"Flag bits:         0x{zi.flag_bits:04X}")
    print(f"Create system:     {zi.create_system}")
    print(f"Create version:    {zi.create_version}")
    print(f"Extract version:   {zi.extract_version}")
    print(f"Internal attr:     0x{zi.internal_attr:X}")
    print(f"External attr:     0x{zi.external_attr:X}")
    print(f"Local hdr offset:  {zi.header_offset}")
    print(f"Extra bytes:       {len(zi.extra)}")
    print(f"Comment bytes:     {len(zi.comment)}")
    print(f"Directory:         {zi.is_dir()}")


def cmd_info(args: argparse.Namespace) -> None:
    path = Path(args.archive)
    with _open_zip(path) as zf:
        if args.entry:
            zi = _require_unique_entry(zf, args.entry)
            _print_entry_info(zi)
        else:
            infos = zf.infolist()
            print(f"Archive:           {path}")
            print(f"File size:         {path.stat().st_size} ({_fmt_size(path.stat().st_size)})")
            print(f"Entries:           {len(infos)}")
            print(f"Archive comment:   {zf.comment!r}")
            print(f"Duplicate names:   {len(_duplicates(infos))}")
            methods: Dict[int, int] = {}
            for zi in infos:
                methods[zi.compress_type] = methods.get(zi.compress_type, 0) + 1
            print("Compression methods:")
            for method, count in sorted(methods.items()):
                print(f"  {_method_name(method)} ({method}): {count}")


def cmd_extract(args: argparse.Namespace) -> None:
    archive = Path(args.archive)
    out = Path(args.output)
    with _open_zip(archive) as zf:
        zi = _require_unique_entry(zf, args.entry)
        if zi.is_dir():
            raise JPKError(f"entry is a directory: {zi.filename}")
        out.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(zi, "r") as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
    print(f"Extracted {zi.filename} -> {out}")


def cmd_extract_all(args: argparse.Namespace) -> None:
    archive = Path(args.archive)
    root = Path(args.output_directory)
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    with _open_zip(archive) as zf:
        for zi in zf.infolist():
            target = _safe_output_path(root, zi.filename)
            if zi.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(zi, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            # Best effort timestamp preservation.
            try:
                import datetime
                dt = datetime.datetime(*zi.date_time)
                ts = dt.timestamp()
                os.utime(target, (ts, ts))
            except Exception:
                pass
            count += 1
    print(f"Extracted {count} files to {root}")


def _parse_replacements(items: Sequence[str]) -> Dict[str, Path]:
    replacements: Dict[str, Path] = {}
    for item in items:
        if "=" not in item:
            raise JPKError(f"--replace must be ARCHIVE_PATH=FILE: {item!r}")
        name, filename = item.split("=", 1)
        name = _normalize_archive_name(name)
        p = Path(filename)
        if not p.is_file():
            raise JPKError(f"replacement file does not exist: {p}")
        if name in replacements:
            raise JPKError(f"duplicate replacement target: {name}")
        replacements[name] = p
    return replacements


def _atomic_output_path(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".tmp-", dir=output.parent)
    os.close(fd)
    # Leave the securely-created file in place. ZipFile(..., "w") will truncate
    # it, avoiding an unlink/recreate race.
    return Path(temp_name)


def _rewrite_archive(template: Path, output: Path,
                     replacements: Mapping[str, Path],
                     deletes: Iterable[str] = (),
                     additions: Optional[Mapping[str, Path]] = None) -> Tuple[int, int, int]:
    deletes_set = {_normalize_archive_name(x) for x in deletes}
    replacements = {_normalize_archive_name(k): Path(v) for k, v in replacements.items()}
    additions = {_normalize_archive_name(k): Path(v) for k, v in (additions or {}).items()}
    for label, mapping in (("replacement", replacements), ("addition", additions)):
        for name, path in mapping.items():
            if not path.is_file():
                raise JPKError(f"{label} file does not exist for {name!r}: {path}")

    tmp = _atomic_output_path(output)
    replaced = deleted = added = 0
    try:
        # The source archive must be closed before os.replace(), especially on
        # Windows where an open ZIP handle can prevent in-place replacement.
        with _open_zip(template) as src:
            infos = src.infolist()
            dup = _duplicates(infos)
            ambiguous = set(replacements) & set(dup)
            ambiguous |= deletes_set & set(dup)
            if ambiguous:
                raise JPKError(
                    "refusing ambiguous duplicate-name modification for: "
                    + ", ".join(sorted(ambiguous))
                )

            for zi in infos:
                if zi.flag_bits & 0x1:
                    raise JPKError(f"encrypted entry cannot be safely rewritten: {zi.filename!r}")
                if zi.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                                            getattr(zipfile, "ZIP_BZIP2", -1),
                                            getattr(zipfile, "ZIP_LZMA", -1)}:
                    raise JPKError(
                        f"unsupported compression method {zi.compress_type} for {zi.filename!r}"
                    )

            existing = {zi.filename for zi in infos}
            missing = set(replacements) - existing
            missing |= deletes_set - existing
            if missing:
                raise JPKError(
                    "target entry not present in template: " + ", ".join(sorted(missing))
                )
            conflicts = set(additions) & existing
            if conflicts:
                raise JPKError(
                    "new entry already exists in template: " + ", ".join(sorted(conflicts))
                )

            with zipfile.ZipFile(tmp, "w", allowZip64=True) as dst:
                dst.comment = src.comment
                for zi in infos:
                    if zi.filename in deletes_set:
                        deleted += 1
                        continue
                    clone = _clone_zipinfo(zi)
                    if zi.filename in replacements:
                        data = replacements[zi.filename].read_bytes()
                        replaced += 1
                    else:
                        data = src.read(zi)
                    dst.writestr(clone, data)

                for name, path in additions.items():
                    data = path.read_bytes()
                    zi = zipfile.ZipInfo(name)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zi.external_attr = 0x20
                    dst.writestr(zi, data)
                    added += 1

        # Validate after both writer and source archive handles are closed.
        with _open_zip(tmp) as check:
            try:
                bad = check.testzip()
            except (RuntimeError, NotImplementedError, OSError, zipfile.BadZipFile) as exc:
                raise JPKError(f"rewritten archive failed integrity verification: {exc}") from exc
            if bad is not None:
                raise JPKError(f"rewritten archive failed CRC verification at {bad}")

        os.replace(tmp, output)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        if isinstance(exc, JPKError):
            raise
        if isinstance(exc, (OSError, RuntimeError, NotImplementedError,
                            zipfile.BadZipFile, zipfile.LargeZipFile)):
            raise JPKError(f"failed to rewrite archive: {exc}") from exc
        raise
    return replaced, deleted, added


def cmd_replace(args: argparse.Namespace) -> None:
    archive_name = _normalize_archive_name(args.entry)
    source_file = Path(args.file)
    if not source_file.is_file():
        raise JPKError(f"replacement file does not exist: {source_file}")
    r, d, a = _rewrite_archive(Path(args.template), Path(args.output), {archive_name: source_file})
    print(f"Created {args.output}: replaced {r} entry")


def cmd_patch(args: argparse.Namespace) -> None:
    replacements = _parse_replacements(args.replace or [])
    if not replacements:
        raise JPKError("patch requires at least one --replace ARCHIVE_PATH=FILE")
    r, d, a = _rewrite_archive(Path(args.template), Path(args.output), replacements)
    print(f"Created {args.output}: replaced {r} entries")


def _walk_directory(root: Path, *, exclude: Iterable[Path] = ()) -> Dict[str, Path]:
    files: Dict[str, Path] = {}
    excluded = {p.resolve(strict=False) for p in exclude}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.resolve(strict=False) not in excluded:
            rel = p.relative_to(root).as_posix()
            files[_normalize_archive_name(rel)] = p
    return files


def cmd_sync(args: argparse.Namespace) -> None:
    template = Path(args.template)
    root = Path(args.directory)
    if not root.is_dir():
        raise JPKError(f"sync directory does not exist: {root}")
    disk_files = _walk_directory(root, exclude=[template, Path(args.output)])
    with _open_zip(template) as zf:
        existing = {zi.filename for zi in zf.infolist() if not zi.is_dir()}
    replacements = {name: path for name, path in disk_files.items() if name in existing}
    additions = {name: path for name, path in disk_files.items() if name not in existing} if args.add_new else {}
    if not replacements and not additions:
        raise JPKError("no files in the directory match entries in the template archive")
    r, d, a = _rewrite_archive(template, Path(args.output), replacements, additions=additions)
    skipped = len(disk_files) - len(replacements) - len(additions)
    print(f"Created {args.output}: replaced {r}, added {a}, ignored {skipped} non-template files")


def cmd_delete(args: argparse.Namespace) -> None:
    r, d, a = _rewrite_archive(Path(args.template), Path(args.output), {}, deletes=[args.entry])
    print(f"Created {args.output}: deleted {d} entry")


def _unsafe_name(name: str) -> bool:
    name2 = name.replace("\\", "/")
    p = PurePosixPath(name2)
    return ("\x00" in name or p.is_absolute() or _has_windows_drive_prefix(name2)
            or any(part == ".." for part in p.parts))


def cmd_verify(args: argparse.Namespace) -> None:
    archive = Path(args.archive)
    issues: List[str] = []
    with _open_zip(archive) as zf:
        infos = zf.infolist()
        dup = _duplicates(infos)
        for name, idxs in dup.items():
            issues.append(f"duplicate entry name {name!r} at indices {idxs}")
        for zi in infos:
            if _unsafe_name(zi.filename):
                issues.append(f"unsafe archive path {zi.filename!r}")
            if zi.flag_bits & 0x1:
                issues.append(f"encrypted entry not supported for modification: {zi.filename!r}")
        try:
            bad = zf.testzip()
        except (RuntimeError, NotImplementedError, OSError, zipfile.BadZipFile) as exc:
            issues.append(f"archive read/integrity failure: {exc}")
        else:
            if bad is not None:
                issues.append(f"CRC/read failure: {bad}")

    print(f"Archive: {archive}")
    print(f"Entries: {len(infos)}")
    if issues:
        print(f"Verification: FAILED ({len(issues)} issue(s))")
        for issue in issues:
            print(f"  - {issue}")
        raise JPKError("archive verification failed")
    print("Verification: OK")


def _metadata_tuple(zi: zipfile.ZipInfo) -> tuple:
    return (
        zi.filename, zi.compress_type, zi.date_time, zi.comment, zi.extra,
        zi.create_system, zi.create_version, zi.extract_version, zi.reserved,
        zi.flag_bits, zi.volume, zi.internal_attr, zi.external_attr,
    )


def cmd_compare(args: argparse.Namespace) -> None:
    a_path, b_path = Path(args.archive_a), Path(args.archive_b)
    differences = 0
    with _open_zip(a_path) as za, _open_zip(b_path) as zb:
        ia, ib = za.infolist(), zb.infolist()
        print(f"A entries: {len(ia)}")
        print(f"B entries: {len(ib)}")
        if len(ia) != len(ib):
            differences += 1
            print("DIFF: entry counts differ")
        maxn = max(len(ia), len(ib))
        for i in range(maxn):
            if i >= len(ia):
                differences += 1
                print(f"DIFF [{i}]: only in B: {ib[i].filename}")
                continue
            if i >= len(ib):
                differences += 1
                print(f"DIFF [{i}]: only in A: {ia[i].filename}")
                continue
            x, y = ia[i], ib[i]
            if x.filename != y.filename:
                differences += 1
                print(f"DIFF [{i}]: name {x.filename!r} != {y.filename!r}")
                continue
            meta_a, meta_b = _metadata_tuple(x), _metadata_tuple(y)
            if meta_a != meta_b:
                differences += 1
                print(f"DIFF [{i}] metadata: {x.filename}")
            if x.CRC != y.CRC or x.file_size != y.file_size:
                differences += 1
                print(f"DIFF [{i}] data: {x.filename} "
                      f"CRC {x.CRC:08X}/{y.CRC:08X}, size {x.file_size}/{y.file_size}")
            elif args.deep:
                if za.read(x) != zb.read(y):
                    differences += 1
                    print(f"DIFF [{i}] contents despite CRC match: {x.filename}")
        if za.comment != zb.comment:
            differences += 1
            print("DIFF: archive comments differ")
    if differences:
        print(f"Comparison: {differences} difference(s)")
        sys.exit(1)
    print("Comparison: logically identical")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Inspect, extract, verify, and safely patch Minion Rush ZIP-based .jpk archives.")
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("list", help="list entries")
    q.add_argument("archive")
    q.add_argument("--names-only", action="store_true")
    q.set_defaults(func=cmd_list)

    q = sub.add_parser("info", help="show archive or entry metadata")
    q.add_argument("archive")
    q.add_argument("entry", nargs="?")
    q.set_defaults(func=cmd_info)

    q = sub.add_parser("extract", help="extract one entry")
    q.add_argument("archive")
    q.add_argument("entry")
    q.add_argument("output")
    q.set_defaults(func=cmd_extract)

    q = sub.add_parser("extract-all", help="safely extract all entries")
    q.add_argument("archive")
    q.add_argument("output_directory")
    q.set_defaults(func=cmd_extract_all)

    q = sub.add_parser("replace", help="replace one existing entry using original archive as template")
    q.add_argument("template")
    q.add_argument("entry")
    q.add_argument("file")
    q.add_argument("output")
    q.set_defaults(func=cmd_replace)

    q = sub.add_parser("patch", help="replace multiple existing entries")
    q.add_argument("template")
    q.add_argument("output")
    q.add_argument("--replace", action="append", metavar="ARCHIVE_PATH=FILE")
    q.set_defaults(func=cmd_patch)

    q = sub.add_parser("sync", help="rebuild from a directory while preserving template archive paths/metadata")
    q.add_argument("template")
    q.add_argument("directory")
    q.add_argument("output")
    q.add_argument("--add-new", action="store_true", help="also add files not present in the template")
    q.set_defaults(func=cmd_sync)

    q = sub.add_parser("delete", help="delete one existing entry")
    q.add_argument("template")
    q.add_argument("entry")
    q.add_argument("output")
    q.set_defaults(func=cmd_delete)

    q = sub.add_parser("verify", help="test CRC/readability, duplicates, and unsafe paths")
    q.add_argument("archive")
    q.set_defaults(func=cmd_verify)

    q = sub.add_parser("compare", help="compare two JPK/ZIP archives logically")
    q.add_argument("archive_a")
    q.add_argument("archive_b")
    q.add_argument("--deep", action="store_true", help="also byte-compare uncompressed contents")
    q.set_defaults(func=cmd_compare)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except JPKError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
