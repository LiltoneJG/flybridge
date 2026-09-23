#!/usr/bin/env python3
"""Render Flybridge PNG brand assets from the canonical SVG mark."""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from strip_image_metadata import strip_file

ROOT = Path(__file__).parents[1]
DEFAULT_MARK = ROOT / "docs" / "assets" / "flybridge-mark.svg"
DEFAULT_ANIMATED_HEADER = ROOT / "docs" / "assets" / "flybridge-header.gif"


@dataclass(frozen=True)
class ImageSpec:
    output: Path
    width: int
    height: int
    mark_scale: float


DEFAULT_SPECS = (
    ImageSpec(ROOT / "docs" / "assets" / "flybridge-logo.png", 1024, 1024, 0.68),
    ImageSpec(ROOT / "docs" / "assets" / "flybridge-header.png", 1280, 360, 0.72),
)


def _renderer() -> list[str]:
    magick = shutil.which("magick")
    if magick:
        return [magick]
    convert = shutil.which("convert")
    if convert:
        return [convert]
    raise RuntimeError("ImageMagick is required (install `magick` or `convert`)")


def _decoration_svg(width: int, height: int) -> str:
    stroke = max(3, round(height * 0.012))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
  viewBox="0 0 {width} {height}">
  <ellipse cx="{width / 2:g}" cy="{height * 0.44:g}" rx="{width * 0.42:g}"
    ry="{height * 0.7:g}" fill="#ffffff" fill-opacity="0.16"/>
  <path d="M {-width * 0.08:g} {height * 0.72:g}
    C {width * 0.23:g} {height * 0.53:g}, {width * 0.68:g} {height * 0.91:g},
      {width * 1.08:g} {height * 0.63:g}" fill="none" stroke="#6f9aa1"
    stroke-opacity="0.2" stroke-width="{stroke}"/>
  <path d="M {-width * 0.08:g} {height * 0.8:g}
    C {width * 0.28:g} {height * 0.63:g}, {width * 0.69:g} {height * 0.97:g},
      {width * 1.08:g} {height * 0.71:g}" fill="none" stroke="#ffffff"
    stroke-opacity="0.32" stroke-width="{stroke}"/>
</svg>
"""


def _animated_decoration_svg(width: int, height: int, phase: float) -> str:
    stroke = max(3, round(height * 0.012))
    period = width * 0.92

    def wave_path(baseline: float, amplitude: float, phase_offset: float) -> str:
        points = []
        for x in range(-64, width + 65, 32):
            angle = 2 * math.pi * (x / period - phase + phase_offset)
            y = baseline + amplitude * math.sin(angle)
            points.append(f"{x},{y:.2f}")
        return " ".join(points)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
  viewBox="0 0 {width} {height}">
  <ellipse cx="{width / 2:g}" cy="{height * 0.44:g}" rx="{width * 0.42:g}"
    ry="{height * 0.7:g}" fill="#ffffff" fill-opacity="0.16"/>
  <polyline points="{wave_path(height * 0.72, height * 0.035, 0)}"
    fill="none" stroke="#6f9aa1" stroke-opacity="0.2" stroke-width="{stroke}"/>
  <polyline points="{wave_path(height * 0.8, height * 0.035, 0.08)}"
    fill="none" stroke="#ffffff" stroke-opacity="0.32" stroke-width="{stroke}"/>
</svg>
"""


def render(mark: Path, spec: ImageSpec) -> None:
    if spec.width <= 0 or spec.height <= 0:
        raise ValueError("image dimensions must be positive")
    if not 0 < spec.mark_scale <= 0.9:
        raise ValueError("mark scale must be greater than 0 and at most 0.9")
    if not mark.is_file():
        raise FileNotFoundError(f"SVG mark not found: {mark}")

    command = _renderer()
    mark_size = round(min(spec.width, spec.height) * spec.mark_scale)
    mark_offset_y = -round(spec.height * 0.05)
    spec.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flybridge-brand-") as temporary:
        temporary_path = Path(temporary)
        decoration_svg = temporary_path / "decoration.svg"
        background_png = temporary_path / "background.png"
        decoration_png = temporary_path / "decoration.png"
        mark_png = temporary_path / "mark.png"
        decoration_svg.write_text(_decoration_svg(spec.width, spec.height), encoding="utf-8")

        subprocess.run(
            command
            + [
                "-size",
                f"{spec.width}x{spec.height}",
                "gradient:#edf4f3-#cbdcdf",
                "-colorspace",
                "sRGB",
                "-strip",
                "-depth",
                "8",
                f"PNG32:{background_png}",
            ],
            check=True,
        )
        subprocess.run(
            command
            + [
                "-background",
                "none",
                str(decoration_svg),
                "-strip",
                "-depth",
                "8",
                f"PNG32:{decoration_png}",
            ],
            check=True,
        )
        subprocess.run(
            command
            + [
                "-background",
                "none",
                "-density",
                "384",
                str(mark),
                "-resize",
                f"{mark_size}x{mark_size}",
                "-strip",
                "-depth",
                "8",
                f"PNG32:{mark_png}",
            ],
            check=True,
        )
        subprocess.run(
            command
            + [
                str(background_png),
                str(decoration_png),
                "-gravity",
                "center",
                "-compose",
                "over",
                "-composite",
                str(mark_png),
                "-gravity",
                "center",
                "-geometry",
                f"+0{mark_offset_y:+d}",
                "-compose",
                "over",
                "-composite",
                "-strip",
                "-depth",
                "8",
                f"PNG24:{spec.output}",
            ],
            check=True,
        )
    strip_file(spec.output)
    print(f"generated {spec.output} ({spec.width}x{spec.height})")


def render_animated_header(
    mark: Path,
    output: Path = DEFAULT_ANIMATED_HEADER,
    width: int = 1280,
    height: int = 360,
    frame_count: int = 40,
) -> None:
    """Render a looping header whose waves carry a fixed-horizontal-position boat."""
    if not mark.is_file():
        raise FileNotFoundError(f"SVG mark not found: {mark}")

    command = _renderer()
    mark_size = round(min(width, height) * 0.72)
    mark_offset_y = -round(height * 0.05)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flybridge-header-animation-") as temporary:
        temporary_path = Path(temporary)
        background_png = temporary_path / "background.png"
        mark_png = temporary_path / "mark.png"
        subprocess.run(
            command
            + [
                "-size",
                f"{width}x{height}",
                "gradient:#edf4f3-#cbdcdf",
                "-colorspace",
                "sRGB",
                "-strip",
                "-depth",
                "8",
                f"PNG24:{background_png}",
            ],
            check=True,
        )
        subprocess.run(
            command
            + [
                "-background",
                "none",
                "-density",
                "384",
                str(mark),
                "-resize",
                f"{mark_size}x{mark_size}",
                "-strip",
                "-depth",
                "8",
                f"PNG32:{mark_png}",
            ],
            check=True,
        )

        frames = []
        for frame_number in range(frame_count):
            phase = frame_number / frame_count
            angle = 2 * math.pi * (width / 2 / (width * 0.92) - phase)
            bob = round(height * 0.014 * math.sin(angle))
            tilt = 2.2 * math.cos(angle)
            decoration_svg = temporary_path / f"decoration-{frame_number:03d}.svg"
            decoration_png = temporary_path / f"decoration-{frame_number:03d}.png"
            rotated_mark_png = temporary_path / f"mark-{frame_number:03d}.png"
            frame_png = temporary_path / f"frame-{frame_number:03d}.png"
            decoration_svg.write_text(
                _animated_decoration_svg(width, height, phase), encoding="utf-8"
            )
            subprocess.run(
                command
                + [
                    "-background",
                    "none",
                    str(decoration_svg),
                    "-strip",
                    "-depth",
                    "8",
                    f"PNG32:{decoration_png}",
                ],
                check=True,
            )
            subprocess.run(
                command
                + [
                    str(mark_png),
                    "-background",
                    "none",
                    "-rotate",
                    f"{tilt:.3f}",
                    f"PNG32:{rotated_mark_png}",
                ],
                check=True,
            )
            subprocess.run(
                command
                + [
                    str(background_png),
                    str(decoration_png),
                    "-gravity",
                    "center",
                    "-compose",
                    "over",
                    "-composite",
                    str(rotated_mark_png),
                    "-gravity",
                    "center",
                    "-geometry",
                    f"+0{mark_offset_y + bob:+d}",
                    "-compose",
                    "over",
                    "-composite",
                    "-strip",
                    "-depth",
                    "8",
                    f"PNG24:{frame_png}",
                ],
                check=True,
            )
            frames.append(str(frame_png))

        subprocess.run(
            command
            + [
                "-delay",
                "8",
                *frames,
                "-loop",
                "0",
                "-layers",
                "OptimizePlus",
                "-colors",
                "96",
                "-dither",
                "FloydSteinberg",
                str(output),
            ],
            check=True,
        )
    strip_file(output)
    print(f"generated {output} ({width}x{height}, {frame_count} frames)")


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mark", type=Path, default=DEFAULT_MARK, help="source SVG mark")
    parser.add_argument("--output", type=Path, help="write one custom PNG instead of presets")
    parser.add_argument("--width", type=int, help="custom image width")
    parser.add_argument("--height", type=int, help="custom image height")
    parser.add_argument(
        "--mark-scale",
        type=float,
        default=0.68,
        help="mark size relative to the shorter image edge (default: 0.68)",
    )
    options = parser.parse_args(arguments)

    custom_values = (options.output, options.width, options.height)
    if any(value is not None for value in custom_values):
        if not all(value is not None for value in custom_values):
            parser.error("--output, --width, and --height must be specified together")
        specs = (ImageSpec(options.output, options.width, options.height, options.mark_scale),)
    else:
        specs = DEFAULT_SPECS

    try:
        for spec in specs:
            render(options.mark, spec)
        if not any(value is not None for value in custom_values):
            render_animated_header(options.mark)
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
