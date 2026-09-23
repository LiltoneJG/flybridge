#!/usr/bin/env python3
"""Remove nonessential metadata from PNG, GIF, JPEG, and WebP images."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from _bootstrap import activate_project_environment

activate_project_environment()

from flybridge_core import ArgumentParser

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_METADATA_CHUNKS = {b"eXIf", b"iCCP", b"iTXt", b"tEXt", b"tIME", b"zTXt"}
JPEG_METADATA_MARKERS = {0xE1, 0xE2, 0xED, 0xFE}
WEBP_METADATA_CHUNKS = {b"EXIF", b"ICCP", b"XMP "}
WEBP_METADATA_FLAGS = 0x20 | 0x08 | 0x04
SUPPORTED_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


class ImageFormatError(ValueError):
    """Raised when a supported image has invalid container structure."""


def _png_without_metadata(data: bytes) -> bytes:
    if not data.startswith(PNG_SIGNATURE):
        raise ImageFormatError("invalid PNG signature")
    output = bytearray(PNG_SIGNATURE)
    offset = len(PNG_SIGNATURE)
    found_end = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ImageFormatError("truncated PNG chunk")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            raise ImageFormatError("truncated PNG chunk payload")
        chunk_type = data[offset + 4 : offset + 8]
        if chunk_type not in PNG_METADATA_CHUNKS:
            output.extend(data[offset:end])
        offset = end
        if chunk_type == b"IEND":
            found_end = True
            break
    if not found_end or offset != len(data):
        raise ImageFormatError("invalid PNG end chunk")
    return bytes(output)


def _gif_sub_blocks_end(data: bytes, offset: int) -> int:
    while True:
        if offset >= len(data):
            raise ImageFormatError("truncated GIF data blocks")
        length = data[offset]
        offset += 1
        if length == 0:
            return offset
        offset += length
        if offset > len(data):
            raise ImageFormatError("truncated GIF data block")


def _gif_without_metadata(data: bytes) -> bytes:
    if len(data) < 13 or data[:6] not in {b"GIF87a", b"GIF89a"}:
        raise ImageFormatError("invalid GIF header")
    offset = 13
    if data[10] & 0x80:
        offset += 3 * (2 ** ((data[10] & 0x07) + 1))
    if offset > len(data):
        raise ImageFormatError("truncated GIF color table")
    output = bytearray(data[:offset])
    found_trailer = False
    while offset < len(data):
        start = offset
        introducer = data[offset]
        offset += 1
        if introducer == 0x3B:
            output.append(introducer)
            found_trailer = True
            break
        if introducer == 0x2C:
            if offset + 9 > len(data):
                raise ImageFormatError("truncated GIF image descriptor")
            packed = data[offset + 8]
            offset += 9
            if packed & 0x80:
                offset += 3 * (2 ** ((packed & 0x07) + 1))
            if offset >= len(data):
                raise ImageFormatError("truncated GIF image data")
            offset += 1
            offset = _gif_sub_blocks_end(data, offset)
            output.extend(data[start:offset])
            continue
        if introducer != 0x21 or offset >= len(data):
            raise ImageFormatError("invalid GIF block")
        label = data[offset]
        offset += 1
        first_block = offset
        offset = _gif_sub_blocks_end(data, offset)
        keep = label not in {0xFE, 0xFF}
        if label == 0xFF and first_block < len(data):
            size = data[first_block]
            application = data[first_block + 1 : first_block + 1 + size]
            keep = application in {b"ANIMEXTS1.0", b"NETSCAPE2.0"}
        if keep:
            output.extend(data[start:offset])
    if not found_trailer or offset != len(data):
        raise ImageFormatError("invalid GIF trailer")
    return bytes(output)


def _jpeg_without_metadata(data: bytes) -> bytes:
    if not data.startswith(b"\xff\xd8"):
        raise ImageFormatError("invalid JPEG start marker")
    output = bytearray(data[:2])
    offset = 2
    in_scan = False
    while offset < len(data):
        if in_scan:
            scan_start = offset
            while offset < len(data):
                if data[offset] != 0xFF:
                    offset += 1
                    continue
                marker_offset = offset
                while offset < len(data) and data[offset] == 0xFF:
                    offset += 1
                if offset >= len(data):
                    raise ImageFormatError("truncated JPEG scan")
                marker = data[offset]
                if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                    offset += 1
                    continue
                output.extend(data[scan_start:marker_offset])
                offset = marker_offset
                in_scan = False
                break
            if in_scan:
                raise ImageFormatError("JPEG scan has no following marker")
        start = offset
        if data[offset] != 0xFF:
            raise ImageFormatError("invalid JPEG marker")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            raise ImageFormatError("truncated JPEG marker")
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            output.extend(data[start:offset])
            if offset != len(data):
                raise ImageFormatError("data follows JPEG end marker")
            return bytes(output)
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            output.extend(data[start:offset])
            continue
        if offset + 2 > len(data):
            raise ImageFormatError("truncated JPEG segment")
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        if length < 2 or offset + length > len(data):
            raise ImageFormatError("invalid JPEG segment length")
        offset += length
        if marker not in JPEG_METADATA_MARKERS:
            output.extend(data[start:offset])
        if marker == 0xDA:
            in_scan = True
    raise ImageFormatError("JPEG has no scan or end marker")


def _webp_without_metadata(data: bytes) -> bytes:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ImageFormatError("invalid WebP header")
    declared_size = struct.unpack("<I", data[4:8])[0] + 8
    if declared_size != len(data):
        raise ImageFormatError("invalid WebP container size")
    output = bytearray(b"RIFF\x00\x00\x00\x00WEBP")
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise ImageFormatError("truncated WebP chunk")
        chunk_type = data[offset : offset + 4]
        length = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        end = offset + 8 + length + (length & 1)
        if end > len(data):
            raise ImageFormatError("truncated WebP chunk payload")
        if chunk_type not in WEBP_METADATA_CHUNKS:
            chunk = bytearray(data[offset:end])
            if chunk_type == b"VP8X" and length >= 1:
                chunk[8] &= ~WEBP_METADATA_FLAGS
            output.extend(chunk)
        offset = end
    output[4:8] = struct.pack("<I", len(output) - 8)
    return bytes(output)


def strip_metadata(data: bytes, suffix: str) -> bytes:
    """Return an image with privacy-sensitive metadata removed."""
    normalized_suffix = suffix.lower()
    if normalized_suffix == ".png":
        return _png_without_metadata(data)
    if normalized_suffix == ".gif":
        return _gif_without_metadata(data)
    if normalized_suffix in {".jpeg", ".jpg"}:
        return _jpeg_without_metadata(data)
    if normalized_suffix == ".webp":
        return _webp_without_metadata(data)
    raise ImageFormatError(f"unsupported image suffix: {suffix}")


def strip_file(path: Path) -> bool:
    """Strip one image in place and report whether its bytes changed."""
    original = path.read_bytes()
    stripped = strip_metadata(original, path.suffix)
    if stripped == original:
        return False
    path.write_bytes(stripped)
    return True


def main(arguments: list[str]) -> int:
    parser = ArgumentParser(prog="strip_image_metadata.py", description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="image files to normalize")
    options = parser.parse_args(arguments)
    changed = False
    errors = False
    for path in options.paths:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        try:
            if strip_file(path):
                print(f"removed image metadata from {path}")
                changed = True
        except (ImageFormatError, OSError) as error:
            print(f"cannot normalize {path}: {error}", file=sys.stderr)
            errors = True
    return 2 if errors else int(changed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
