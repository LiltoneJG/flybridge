from __future__ import annotations

import importlib.util
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType


def _module() -> ModuleType:
    script = Path(__file__).parents[1] / "scripts" / "strip_image_metadata.py"
    spec = importlib.util.spec_from_file_location("strip_image_metadata", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(script.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + payload)
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def _png_with_metadata() -> bytes:
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)),
            _png_chunk(b"tEXt", b"Author\x00Private Person"),
            _png_chunk(b"eXIf", b"private EXIF"),
            _png_chunk(b"iCCP", b"profile\x00\x00compressed"),
            _png_chunk(b"IDAT", b"pixels"),
            _png_chunk(b"IEND", b""),
        )
    )


def _gif_extension(label: int, application: bytes, payload: bytes = b"") -> bytes:
    blocks = bytes((len(application),)) + application
    if payload:
        blocks += bytes((len(payload),)) + payload
    return b"\x21" + bytes((label,)) + blocks + b"\x00"


def test_png_removes_text_exif_and_profile_chunks_without_changing_pixels() -> None:
    module = _module()
    original = _png_with_metadata()

    stripped = module.strip_metadata(original, ".png")

    assert b"Author" not in stripped
    assert b"Private Person" not in stripped
    assert b"private EXIF" not in stripped
    assert b"compressed" not in stripped
    assert _png_chunk(b"IDAT", b"pixels") in stripped
    assert module.strip_metadata(stripped, ".png") == stripped


def test_gif_removes_comments_and_metadata_apps_but_keeps_animation_control() -> None:
    module = _module()
    loop = _gif_extension(0xFF, b"NETSCAPE2.0", b"\x01\x00\x00")
    image = b"\x2c" + b"\x00" * 8 + b"\x00\x02\x02D\x01\x00"
    original = b"".join(
        (
            b"GIF89a\x01\x00\x01\x00\x00\x00\x00",
            loop,
            _gif_extension(0xFF, b"ImageMagick", b"private gamma"),
            _gif_extension(0xFE, b"Private Person"),
            b"\x21\xf9\x04\x00\x00\x00\x00\x00",
            image,
            b"\x3b",
        )
    )

    stripped = module.strip_metadata(original, ".gif")

    assert b"NETSCAPE2.0" in stripped
    assert b"\x21\xf9" in stripped
    assert image in stripped
    assert b"ImageMagick" not in stripped
    assert b"Private Person" not in stripped
    assert module.strip_metadata(stripped, ".gif") == stripped


def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes((marker,)) + struct.pack(">H", len(payload) + 2) + payload


def test_jpeg_removes_exif_xmp_iptc_profiles_and_comments() -> None:
    module = _module()
    scan = _jpeg_segment(0xDA, b"scan-header") + b"pixels"
    original = b"".join(
        (
            b"\xff\xd8",
            _jpeg_segment(0xE0, b"JFIF\x00"),
            _jpeg_segment(0xE1, b"Exif\x00Private Person"),
            _jpeg_segment(0xE2, b"ICC_PROFILE\x00private"),
            _jpeg_segment(0xED, b"private IPTC"),
            _jpeg_segment(0xFE, b"private comment"),
            scan,
            _jpeg_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00private XMP"),
            b"\xff\xd9",
        )
    )

    stripped = module.strip_metadata(original, ".jpg")

    assert b"JFIF" in stripped
    assert scan in stripped
    assert b"Private Person" not in stripped
    assert b"ICC_PROFILE" not in stripped
    assert b"private IPTC" not in stripped
    assert b"private comment" not in stripped
    assert b"private XMP" not in stripped
    assert module.strip_metadata(stripped, ".jpeg") == stripped


def _webp_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    padding = b"\x00" if len(payload) & 1 else b""
    return chunk_type + struct.pack("<I", len(payload)) + payload + padding


def _webp(*chunks: bytes) -> bytes:
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_webp_removes_metadata_and_updates_extended_header_flags() -> None:
    module = _module()
    original = _webp(
        _webp_chunk(b"VP8X", bytes((0x2E,)) + b"\x00" * 9),
        _webp_chunk(b"ICCP", b"private profile"),
        _webp_chunk(b"EXIF", b"Private Person"),
        _webp_chunk(b"XMP ", b"private XMP"),
        _webp_chunk(b"ANIM", b"animation"),
    )

    stripped = module.strip_metadata(original, ".webp")

    assert struct.unpack("<I", stripped[4:8])[0] == len(stripped) - 8
    assert b"ICCP" not in stripped
    assert b"EXIF" not in stripped
    assert b"XMP " not in stripped
    assert b"ANIM" in stripped
    assert stripped[stripped.index(b"VP8X") + 8] == 0x02
    assert module.strip_metadata(stripped, ".webp") == stripped


def test_main_modifies_once_then_succeeds(tmp_path: Path) -> None:
    module = _module()
    image = tmp_path / "screenshot.png"
    image.write_bytes(_png_with_metadata())

    assert module.main([str(image)]) == 1
    assert module.main([str(image)]) == 0
