#!/usr/bin/env python3
"""
Encode, decode, and verify Minion Rush ``filesConfig.dat`` and
``config##########`` containers.

Recovered container format
--------------------------
The whole file is XXTEA-encrypted as little-endian uint32 words using the
four key words ``[1, 6, 7, 7]``.

After XXTEA decryption the canonical plaintext layout is::

    uint32_le payload_length
    payload[payload_length]
    char md5_hex[32]                 # uppercase ASCII hexadecimal
    zero padding                     # exactly enough to reach 4-byte alignment

The MD5 input is exactly::

    uint32_le payload_length || payload

The low-level ``decode_container()`` / ``build_container()`` functions work
with arbitrary payload bytes.  The command-line ``decode`` and ``encode``
commands are intentionally JSON-oriented: they require a valid UTF-8 JSON
payload and reject non-standard ``NaN``/``Infinity`` values and invalid
Unicode surrogate strings.

Command-line usage
------------------
Decode an encrypted config to formatted JSON::

    python minion_rush_config_codec.py decode filesConfig.dat
    python minion_rush_config_codec.py decode INPUT -o OUTPUT.json

The default decode output is ``INPUT.decoded.json``.  ``--compact`` emits
compact JSON, ``--indent N`` selects pretty-print indentation,
``--raw-payload-output PATH`` saves the exact decrypted payload bytes, and
``--metadata-output PATH`` writes container metadata as JSON.

Encode JSON into a config container::

    python minion_rush_config_codec.py encode INPUT.json
    python minion_rush_config_codec.py encode INPUT.json -o OUTPUT

By default the JSON is parsed and rendered again before encryption.  Use
``--compact`` for compact JSON, or ``--preserve-json-bytes`` to validate the
JSON and then encrypt its exact original UTF-8 bytes without reformatting.
The default encoded filename is derived from the JSON filename and ends in
``.encoded``.

Verify only the binary container contract::

    python minion_rush_config_codec.py verify filesConfig.dat

``verify`` checks XXTEA block structure, payload bounds, the exact uppercase
MD5 text, and canonical zero padding.  It deliberately does *not* require the
payload to be JSON; use ``decode`` when JSON validation is also desired.

Limitations
-----------
This is a container codec, not a schema validator for the JSON objects inside
Minion Rush configs.  A syntactically valid JSON payload can still contain
keys or values that the game itself does not understand.  The recovered
format described above is treated strictly so malformed/noncanonical
containers fail instead of being silently normalized during verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any, Sequence

UINT32_MASK = 0xFFFFFFFF
XXTEA_DELTA = 0x9E3779B9
XXTEA_KEY = (1, 6, 7, 7)


class ConfigError(Exception):
    """Invalid Minion Rush config container or JSON payload."""


def _u32(value: int) -> int:
    return value & UINT32_MASK


def _validate_block(data: bytes) -> int:
    if not data:
        raise ConfigError("Input block is empty.")
    if len(data) % 4 != 0:
        raise ConfigError(
            f"XXTEA block size must be divisible by 4; got {len(data)} bytes."
        )
    count = len(data) // 4
    if count < 2:
        raise ConfigError("XXTEA requires at least two uint32 words.")
    return count


def _validate_key(key: Sequence[int]) -> tuple[int, int, int, int]:
    """Validate/normalize an XXTEA key to exactly four uint32 words."""
    try:
        key_length = len(key)
    except TypeError as exc:
        raise ConfigError("XXTEA key must contain exactly four integer words.") from exc
    if key_length != 4:
        raise ConfigError(
            f"XXTEA key must contain exactly four integer words; got {key_length}."
        )

    normalized: list[int] = []
    for index, word in enumerate(key):
        if not isinstance(word, int):
            raise ConfigError(f"XXTEA key word {index} is not an integer: {word!r}")
        normalized.append(_u32(word))
    return (normalized[0], normalized[1], normalized[2], normalized[3])


def xxtea_encrypt(
    data: bytes,
    key: Sequence[int] = XXTEA_KEY,
) -> bytes:
    """Encrypt a raw XXTEA block using little-endian uint32 words."""
    word_count = _validate_block(data)
    key_words = _validate_key(key)
    words = list(struct.unpack(f"<{word_count}I", data))

    rounds = 6 + 52 // word_count
    total = 0
    z = words[word_count - 1]

    for _ in range(rounds):
        total = _u32(total + XXTEA_DELTA)
        e = (total >> 2) & 3

        for position in range(word_count - 1):
            y = words[position + 1]
            mix = _u32(
                (((z >> 5) ^ _u32(y << 2))
                 + ((y >> 3) ^ _u32(z << 4)))
                ^ ((total ^ y) + (key_words[(position & 3) ^ e] ^ z))
            )
            words[position] = _u32(words[position] + mix)
            z = words[position]

        y = words[0]
        position = word_count - 1
        mix = _u32(
            (((z >> 5) ^ _u32(y << 2))
             + ((y >> 3) ^ _u32(z << 4)))
            ^ ((total ^ y) + (key_words[(position & 3) ^ e] ^ z))
        )
        words[position] = _u32(words[position] + mix)
        z = words[position]

    return struct.pack(f"<{word_count}I", *words)


def xxtea_decrypt(
    data: bytes,
    key: Sequence[int] = XXTEA_KEY,
) -> bytes:
    """Decrypt a raw XXTEA block using little-endian uint32 words."""
    word_count = _validate_block(data)
    key_words = _validate_key(key)
    words = list(struct.unpack(f"<{word_count}I", data))

    rounds = 6 + 52 // word_count
    total = _u32(rounds * XXTEA_DELTA)
    y = words[0]

    while total != 0:
        e = (total >> 2) & 3

        for position in range(word_count - 1, 0, -1):
            z = words[position - 1]
            mix = _u32(
                (((z >> 5) ^ _u32(y << 2))
                 + ((y >> 3) ^ _u32(z << 4)))
                ^ ((total ^ y) + (key_words[(position & 3) ^ e] ^ z))
            )
            words[position] = _u32(words[position] - mix)
            y = words[position]

        z = words[word_count - 1]
        mix = _u32(
            (((z >> 5) ^ _u32(y << 2))
             + ((y >> 3) ^ _u32(z << 4)))
            ^ ((total ^ y) + (key_words[e] ^ z))
        )
        words[0] = _u32(words[0] - mix)
        y = words[0]
        total = _u32(total - XXTEA_DELTA)

    return struct.pack(f"<{word_count}I", *words)


def build_container(payload: bytes) -> tuple[bytes, dict[str, Any]]:
    """Create and encrypt a canonical Minion Rush config container."""
    if len(payload) > UINT32_MASK:
        raise ConfigError("Payload is too large for the uint32 length field.")

    prefix = struct.pack("<I", len(payload)) + payload
    md5_hex = hashlib.md5(prefix).hexdigest().upper().encode("ascii")
    plaintext = prefix + md5_hex

    padding_size = (-len(plaintext)) % 4
    plaintext += b"\x00" * padding_size

    encrypted = xxtea_encrypt(plaintext)
    metadata = {
        "payload_length": len(payload),
        "decrypted_size": len(plaintext),
        "encrypted_size": len(encrypted),
        "md5": md5_hex.decode("ascii"),
        "padding_size": padding_size,
        "xxtea_key_words": list(XXTEA_KEY),
    }
    return encrypted, metadata


def decode_container(encrypted: bytes) -> tuple[bytes, dict[str, Any]]:
    """Decrypt and strictly verify a Minion Rush config container."""
    decrypted = xxtea_decrypt(encrypted)

    if len(decrypted) < 36:
        raise ConfigError("Decrypted container is too small.")

    payload_length = struct.unpack_from("<I", decrypted, 0)[0]
    payload_end = 4 + payload_length
    checksum_end = payload_end + 32

    if checksum_end > len(decrypted):
        raise ConfigError("Payload length or checksum extends past the file.")

    payload = decrypted[4:payload_end]
    stored_md5_bytes = decrypted[payload_end:checksum_end]
    try:
        stored_md5 = stored_md5_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ConfigError("Stored MD5 is not ASCII.") from exc

    calculated_md5 = hashlib.md5(decrypted[:payload_end]).hexdigest().upper()
    if stored_md5 != calculated_md5:
        raise ConfigError(
            "MD5 verification failed: "
            f"stored={stored_md5!r}, calculated={calculated_md5}"
        )

    padding = decrypted[checksum_end:]
    expected_padding_size = (-checksum_end) % 4
    if len(padding) != expected_padding_size:
        raise ConfigError(
            "Noncanonical padding length: "
            f"expected {expected_padding_size} byte(s), got {len(padding)}."
        )
    if any(padding):
        raise ConfigError(f"Nonzero padding found: {padding.hex()}")

    metadata = {
        "payload_length": payload_length,
        "decrypted_size": len(decrypted),
        "encrypted_size": len(encrypted),
        "stored_md5": stored_md5,
        "calculated_md5": calculated_md5,
        "padding_size": len(padding),
        "xxtea_key_words": list(XXTEA_KEY),
    }
    return payload, metadata


def _reject_json_constant(token: str) -> Any:
    raise ConfigError(f"Non-standard JSON numeric constant is not allowed: {token}")


def _validate_json_value(value: Any, path: str = "$") -> None:
    """Reject values that cannot be represented as strict interoperable UTF-8 JSON."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ConfigError(f"Invalid Unicode surrogate in JSON string at {path}.") from exc
        return

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError(f"Non-finite JSON number at {path} is not allowed.")
        return

    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return

    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigError(f"JSON object key at {path} is not a string.")
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ConfigError(f"Invalid Unicode surrogate in JSON key at {path}.") from exc
            _validate_json_value(item, f"{path}.{key}")


def parse_json_text(text: str) -> Any:
    """Parse strict JSON and validate strings/numbers used by the CLI."""
    try:
        parsed = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    _validate_json_value(parsed)
    return parsed


def parse_json_payload(payload: bytes) -> Any:
    """Decode a UTF-8 payload and parse it as strict JSON."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("JSON payload is not valid UTF-8.") from exc
    return parse_json_text(text)


def render_json_payload(parsed: Any, *, compact: bool, indent: int) -> bytes:
    """Render parsed JSON as valid UTF-8, rejecting non-finite/surrogate values."""
    _validate_json_value(parsed)
    try:
        if compact:
            rendered = json.dumps(
                parsed,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        else:
            rendered = json.dumps(
                parsed,
                ensure_ascii=False,
                allow_nan=False,
                indent=indent,
            )
        return rendered.encode("utf-8")
    except (UnicodeEncodeError, ValueError) as exc:
        raise ConfigError(f"JSON cannot be represented as strict UTF-8 JSON: {exc}") from exc


def load_json_payload(
    input_path: Path,
    *,
    preserve_bytes: bool,
    compact: bool,
    indent: int,
) -> bytes:
    """Load/validate JSON and return either exact or re-rendered UTF-8 bytes."""
    raw = input_path.read_bytes()
    parsed = parse_json_payload(raw)
    if preserve_bytes:
        return raw
    return render_json_payload(parsed, compact=compact, indent=indent)


def default_decoded_path(input_path: Path) -> Path:
    return input_path.with_name(input_path.name + ".decoded.json")


def default_encoded_path(input_path: Path) -> Path:
    name = input_path.name
    for suffix in (".decoded.json", ".json"):
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
            break
    return input_path.with_name(name + ".encoded")


def command_decode(args: argparse.Namespace) -> int:
    encrypted = args.input.read_bytes()
    payload, metadata = decode_container(encrypted)
    parsed = parse_json_payload(payload)

    output = args.output or default_decoded_path(args.input)
    rendered = render_json_payload(parsed, compact=args.compact, indent=args.indent)
    output.write_bytes(rendered + b"\n")

    if args.raw_payload_output:
        args.raw_payload_output.write_bytes(payload)
    if args.metadata_output:
        args.metadata_output.write_text(
            json.dumps(metadata, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"Decoded: {args.input}")
    print(f"Output:  {output}")
    print(f"Payload: {metadata['payload_length']} bytes")
    print(f"MD5:     {metadata['calculated_md5']}")
    return 0


def command_encode(args: argparse.Namespace) -> int:
    payload = load_json_payload(
        args.input,
        preserve_bytes=args.preserve_json_bytes,
        compact=args.compact,
        indent=args.indent,
    )
    encrypted, metadata = build_container(payload)

    output = args.output or default_encoded_path(args.input)
    output.write_bytes(encrypted)

    if args.metadata_output:
        args.metadata_output.write_text(
            json.dumps(metadata, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"Encoded: {args.input}")
    print(f"Output:  {output}")
    print(f"Payload: {metadata['payload_length']} bytes")
    print(f"MD5:     {metadata['md5']}")
    print(f"Padding: {metadata['padding_size']} bytes")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    _, metadata = decode_container(args.input.read_bytes())
    print(f"Valid:   {args.input}")
    print(f"Payload: {metadata['payload_length']} bytes")
    print(f"MD5:     {metadata['calculated_md5']}")
    return 0


def _add_json_format_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    parser.add_argument("--indent", type=int, default=2, help="pretty-print indentation (default: 2)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Encode, decode, or verify Minion Rush filesConfig/config containers."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    decode = subparsers.add_parser(
        "decode",
        help="decrypt/verify a config container and decode its UTF-8 JSON payload",
    )
    decode.add_argument("input", type=Path)
    decode.add_argument("-o", "--output", type=Path)
    decode.add_argument("--raw-payload-output", type=Path)
    decode.add_argument("--metadata-output", type=Path)
    _add_json_format_options(decode)
    decode.set_defaults(func=command_decode)

    encode = subparsers.add_parser("encode", help="validate JSON and encrypt a config container")
    encode.add_argument("input", type=Path)
    encode.add_argument("-o", "--output", type=Path)
    encode.add_argument("--metadata-output", type=Path)
    encode.add_argument(
        "--preserve-json-bytes",
        action="store_true",
        help=(
            "validate strict UTF-8 JSON, then encrypt its exact original bytes "
            "without reformatting"
        ),
    )
    _add_json_format_options(encode)
    encode.set_defaults(func=command_encode)

    verify = subparsers.add_parser(
        "verify",
        help="verify only the binary container/MD5/padding contract (does not parse JSON)",
    )
    verify.add_argument("input", type=Path)
    verify.set_defaults(func=command_verify)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (OSError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
