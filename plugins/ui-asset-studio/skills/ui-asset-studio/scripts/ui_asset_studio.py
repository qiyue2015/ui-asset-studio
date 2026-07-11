#!/usr/bin/env python3
"""Offline, non-destructive UI asset processing helpers.

The CLI deliberately refuses uncertain destructive segmentation.  It writes every
result and its JSON/Markdown quality report to a sibling output directory.
"""

from __future__ import annotations

import argparse
import copy
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable
import xml.etree.ElementTree as ET


VERSION = "1.0.0"
RASTER_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


class AssetError(RuntimeError):
    """A clear, user-actionable asset processing failure."""


def die(message: str) -> None:
    raise AssetError(message)


def require_modules():
    try:
        from PIL import Image
    except ImportError as exc:
        die(
            "Missing local image dependency. Install with: "
            "python3 -m pip install --user -r scripts/requirements.txt"
        )
    return Image


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def default_outdir(input_path: Path, supplied: str | None) -> Path:
    return Path(supplied).expanduser().resolve() if supplied else input_path.parent / "ui-asset-studio-output"


def unique_path(directory: Path, filename: str) -> Path:
    """Return a path that never overwrites an existing artifact."""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    index = 2
    while True:
        candidate = directory / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def output_path(input_path: Path, outdir: Path, action: str, suffix: str) -> Path:
    return unique_path(outdir, f"{input_path.stem}.{action}{suffix}")


def report_paths(output: Path, report_only: bool = False) -> tuple[Path, Path]:
    if report_only:
        return output, unique_path(output.parent, f"{output.stem}.report.md")
    prefix = output.with_suffix("")
    return (
        unique_path(output.parent, f"{prefix.name}.report.json"),
        unique_path(output.parent, f"{prefix.name}.report.md"),
    )


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    return value


def write_report(output: Path, report: dict[str, Any]) -> dict[str, str]:
    report_only = report.get("output", {}).get("type") == "report_only"
    json_path, markdown_path = report_paths(output, report_only)
    report = {key: json_safe(value) for key, value in report.items()}
    report["report_paths"] = {"json": str(json_path), "markdown": str(markdown_path)}
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    quality = report.get("quality_checks", {})
    risks = report.get("risks", []) or ["None reported by the local checks."]
    strategy = report.get("strategy", {})
    lines = [
        f"# UI Asset Studio: {report.get('action', 'report')}",
        "",
        f"- Input: `{report.get('input', {}).get('path', 'unknown')}`",
        f"- Output: `{report.get('output', {}).get('path', output)}`",
        f"- Created: {report.get('created_at', '')}",
        f"- Alpha output: {quality.get('true_alpha_channel', 'n/a')}",
        f"- Shadow handling: {strategy.get('shadow', 'n/a')}",
        f"- Auto trim: {strategy.get('trimmed', 'n/a')}",
        "",
        "## Quality checks",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in quality.items())
    lines.extend(["", "## Risks or limits", ""])
    lines.extend(f"- {risk}" for risk in risks)
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def is_svg_path(path: Path) -> bool:
    if path.suffix.lower() == ".svg":
        return True
    try:
        return path.read_bytes().lstrip().startswith(b"<svg")
    except OSError:
        return False


def validate_input(path_value: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        die(f"Input file does not exist: {path}")
    if path.stat().st_size == 0:
        die(f"Input file is empty: {path}")
    return path


def image_has_alpha(image) -> bool:
    return image.mode in {"RGBA", "LA"} or "transparency" in image.info


def extension_matches_format(path: Path, actual_format: str | None) -> bool | None:
    if not actual_format:
        return None
    expected = {
        "JPEG": {".jpg", ".jpeg"},
        "PNG": {".png"},
        "WEBP": {".webp"},
        "BMP": {".bmp"},
        "TIFF": {".tif", ".tiff"},
        "GIF": {".gif"},
    }.get(actual_format.upper())
    return None if expected is None else path.suffix.lower() in expected


def rgb_distance(a: tuple[int, int, int], b: tuple[float, float, float]) -> float:
    # A weighted RGB distance is more stable than deleting one color channel.
    return math.sqrt(
        0.299 * (a[0] - b[0]) ** 2
        + 0.587 * (a[1] - b[1]) ** 2
        + 0.114 * (a[2] - b[2]) ** 2
    )


def color_saturation(rgb: tuple[int, int, int]) -> float:
    high, low = max(rgb), min(rgb)
    return 0.0 if high == 0 else (high - low) / high


def border_coordinates(width: int, height: int, step: int = 1) -> list[tuple[int, int]]:
    step = max(1, step)
    points = {(x, 0) for x in range(0, width, step)}
    points.update((x, height - 1) for x in range(0, width, step))
    points.update((0, y) for y in range(0, height, step))
    points.update((width - 1, y) for y in range(0, height, step))
    return list(points)


@dataclass(frozen=True)
class BackgroundModel:
    color: tuple[float, float, float]
    dominance: float
    spread: float
    core_threshold: float
    edge_threshold: float


def estimate_background(image, tolerance: float) -> BackgroundModel:
    """Estimate a uniform canvas background solely from border pixels.

    A connected-component matte uses this model later, so interior whites are not
    treated as background merely because their color resembles the canvas.
    """
    rgba = image.convert("RGBA")
    width, height = rgba.size
    if width < 3 or height < 3:
        die("Image is too small to estimate a safe border background.")
    sample_step = max(1, int(max(width, height) / 600))
    pixels = rgba.load()
    samples = [pixels[x, y][:3] for x, y in border_coordinates(width, height, sample_step)]
    # Quantization suppresses antialiasing and compression noise at the edge.
    bins = Counter(tuple(channel // 12 for channel in pixel) for pixel in samples)
    dominant_bin, count = bins.most_common(1)[0]
    selected = [
        pixel
        for pixel in samples
        if all(abs(pixel[index] // 12 - dominant_bin[index]) <= 1 for index in range(3))
    ]
    if not selected:
        die("Could not estimate a border background from this image.")
    color = tuple(sum(pixel[index] for pixel in selected) / len(selected) for index in range(3))
    distances = sorted(rgb_distance(pixel, color) for pixel in selected)
    percentile = distances[min(len(distances) - 1, int(len(distances) * 0.90))]
    spread = distances[-1] if distances else 0.0
    dominance = count / max(1, len(samples))
    core = max(8.0, percentile + tolerance)
    edge = max(core + 34.0, min(140.0, core * 2.4))
    return BackgroundModel(color, dominance, spread, core, edge)


def background_risk(model: BackgroundModel) -> str | None:
    if model.dominance < 0.58:
        return "The canvas border has no dominant flat color; automatic background removal is unsafe."
    if model.spread > 100 and model.dominance < 0.78:
        return "The canvas border appears textured or gradient-heavy; automatic background removal is unsafe."
    return None


def smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def subject_bbox_from_alpha(image, threshold: int = 1) -> tuple[int, int, int, int] | None:
    alpha = image.convert("RGBA").getchannel("A")
    return alpha.point(lambda value: 255 if value > threshold else 0).getbbox()


def expand_bbox(bbox: tuple[int, int, int, int], size: tuple[int, int], margin: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    width, height = size
    return (
        max(0, left - margin),
        max(0, top - margin),
        min(width, right + margin),
        min(height, bottom + margin),
    )


def connected_background_matte(image, model: BackgroundModel, shadow: str) -> tuple[Any, dict[str, Any]]:
    """Remove only edge-connected background-like pixels.

    This is intentionally conservative. A white element enclosed by a red label is
    never reached from the canvas border, so it retains alpha even on a white canvas.
    """
    Image = require_modules()
    source = image.convert("RGBA")
    width, height = source.size
    source_pixels = source.load()
    count = width * height
    distances = [0.0] * count
    reachable = bytearray(count)
    queue: deque[int] = deque()

    for y in range(height):
        for x in range(width):
            index = y * width + x
            distances[index] = rgb_distance(source_pixels[x, y][:3], model.color)

    for x, y in border_coordinates(width, height):
        index = y * width + x
        if distances[index] <= model.edge_threshold and not reachable[index]:
            reachable[index] = 1
            queue.append(index)

    while queue:
        current = queue.popleft()
        x, y = current % width, current // width
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height:
                next_index = ny * width + nx
                if not reachable[next_index] and distances[next_index] <= model.edge_threshold:
                    reachable[next_index] = 1
                    queue.append(next_index)

    output = source.copy()
    output_pixels = output.load()
    transparent = partial = shadow_removed = 0
    preserved_internal_light = 0
    for y in range(height):
        for x in range(width):
            index = y * width + x
            red, green, blue, source_alpha = source_pixels[x, y]
            original = (red, green, blue)
            if not reachable[index]:
                if min(original) >= 225 and source_alpha > 0:
                    preserved_internal_light += 1
                continue
            distance = distances[index]
            if distance <= model.core_threshold:
                alpha = 0
            else:
                ratio = (distance - model.core_threshold) / (model.edge_threshold - model.core_threshold)
                alpha = int(round(255 * smoothstep(ratio)))
            # Shadows are usually low-saturation dark pixels outside the subject.
            if shadow == "remove" and color_saturation(original) < 0.12 and distance < model.edge_threshold:
                alpha = 0
                shadow_removed += 1
            alpha = min(alpha, source_alpha)
            if alpha == 0:
                output_pixels[x, y] = (0, 0, 0, 0)
                transparent += 1
            else:
                if alpha < 255:
                    partial += 1
                output_pixels[x, y] = (red, green, blue, alpha)

    return output, {
        "background_color": [round(channel, 2) for channel in model.color],
        "background_dominance": round(model.dominance, 4),
        "background_spread": round(model.spread, 2),
        "core_threshold": round(model.core_threshold, 2),
        "edge_threshold": round(model.edge_threshold, 2),
        "edge_connected_pixels": int(sum(reachable)),
        "transparent_pixels": transparent,
        "partial_alpha_pixels": partial,
        "shadow_pixels_removed": shadow_removed,
        "internal_light_pixels_preserved": preserved_internal_light,
    }


def quality_for_png(image, original_size: tuple[int, int] | None = None) -> dict[str, Any]:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()
    corners = [pixels[x, y][3] for x, y in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))]
    bbox = subject_bbox_from_alpha(rgba, 1)
    alpha_values = list(rgba.getchannel("A").getdata())
    partial = sum(1 for value in alpha_values if 0 < value < 255)
    return {
        "true_alpha_channel": rgba.mode == "RGBA",
        "transparent_corners": all(value == 0 for value in corners),
        "corner_alpha": corners,
        "visible_bounds": list(bbox) if bbox else None,
        "subject_present": bbox is not None,
        "subject_touches_canvas": bool(
            bbox and (bbox[0] == 0 or bbox[1] == 0 or bbox[2] == width or bbox[3] == height)
        ),
        "partial_alpha_pixels": partial,
        "dimensions": {"width": width, "height": height},
        "aspect_ratio_preserved": (
            None
            if original_size is None
            else abs((width / height) - (original_size[0] / original_size[1])) < 0.0001
        ),
    }


def image_info(path: Path) -> dict[str, Any]:
    Image = require_modules()
    try:
        with Image.open(path) as image:
            image.load()
            actual_format = image.format
            rgba = image.convert("RGBA")
            model = estimate_background(rgba, 16.0)
            risk = background_risk(model)
            alpha = rgba.getchannel("A")
            alpha_extrema = alpha.getextrema()
            colors = rgba.convert("RGB").quantize(colors=64).getcolors(64) or []
            unique_estimate = len(colors)
            width, height = image.size
            gradient_likelihood = model.spread > 45 or unique_estimate > 42
            vector_suitable = unique_estimate <= 28 and not gradient_likelihood and max(width, height) <= 2048
            risks = []
            if risk:
                risks.append(risk)
            if gradient_likelihood:
                risks.append("Complex gradients or high color variation reduce safe vectorization quality.")
            return {
                "kind": "raster",
                "path": str(path),
                "file_extension": path.suffix.lower(),
                "actual_format": actual_format,
                "extension_matches_format": extension_matches_format(path, actual_format),
                "dimensions": {"width": width, "height": height},
                "aspect_ratio": round(width / height, 6),
                "color_mode": image.mode,
                "has_alpha": image_has_alpha(image),
                "alpha_extrema": list(alpha_extrema),
                "background": {
                    "estimated_rgb": [round(value, 2) for value in model.color],
                    "dominance": round(model.dominance, 4),
                    "spread": round(model.spread, 2),
                    "type": "flat_or_near_flat" if not risk else "complex_or_uncertain",
                },
                "has_complex_gradient": gradient_likelihood,
                "estimated_color_count": unique_estimate,
                "suitable_for_vectorization": vector_suitable,
                "risks": risks,
            }
    except AssetError:
        raise
    except Exception as exc:
        die(f"Unable to read raster image '{path}': {exc}")


def parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:px)?\s*", value)
    return float(match.group(1)) if match else None


def parse_viewbox(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    fields = re.split(r"[\s,]+", value.strip())
    if len(fields) != 4:
        return None
    try:
        return tuple(float(field) for field in fields)  # type: ignore[return-value]
    except ValueError:
        return None


def href_for(element: ET.Element) -> str | None:
    return element.get("href") or element.get(f"{{{XLINK_NS}}}href")


def inspect_svg(path: Path) -> dict[str, Any]:
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        return {
            "kind": "svg",
            "path": str(path),
            "xml_valid": False,
            "risks": [f"Malformed SVG/XML: {exc}"],
            "sketch_preflight": {"status": "fail", "reasons": ["XML is not valid."]},
        }
    root = tree.getroot()
    if local_name(root.tag) != "svg":
        return {
            "kind": "svg",
            "path": str(path),
            "xml_valid": False,
            "risks": ["Root element is not <svg>."],
            "sketch_preflight": {"status": "fail", "reasons": ["Root element is not SVG."]},
        }
    counts: Counter[str] = Counter()
    embedded: list[str] = []
    external: list[str] = []
    transforms: list[str] = []
    style_text: list[str] = []
    for element in root.iter():
        name = local_name(element.tag)
        counts[name] += 1
        href = href_for(element)
        if name == "image" and href:
            if href.startswith("data:image"):
                embedded.append(href[:32] + "...")
            elif not href.startswith("#"):
                external.append(href)
        if href and name not in {"image", "use"} and not href.startswith("#") and not href.startswith("data:"):
            external.append(href)
        transform = element.get("transform")
        if transform:
            transforms.append(transform)
        if name == "style" and element.text:
            style_text.append(element.text)
    css = "\n".join(style_text)
    external.extend(re.findall(r"url\(\s*['\"]?((?:https?:)?//[^)'\"\s]+)", css, flags=re.I))
    viewbox = parse_viewbox(root.get("viewBox"))
    width, height = parse_number(root.get("width")), parse_number(root.get("height"))
    has_mask = counts["mask"] > 0
    has_clip = counts["clipPath"] > 0
    has_filter = counts["filter"] > 0
    has_text = counts["text"] > 0 or counts["tspan"] > 0
    has_use = counts["use"] > 0 or counts["symbol"] > 0
    complex_transform = any(re.search(r"(?:matrix|skew|rotate)\s*\(", value) for value in transforms)
    visual_elements = sum(counts[name] for name in ("path", "rect", "circle", "ellipse", "line", "polygon", "polyline"))
    editable = not embedded and not external and visual_elements > 0
    risks: list[str] = []
    if not viewbox:
        risks.append("Missing or invalid viewBox can cause import-size drift.")
    if width is None or height is None:
        risks.append("Width or height is missing/non-pixel; import size may depend on the host.")
    if embedded:
        risks.append("Contains embedded raster bitmap; this is not a fully editable vector asset.")
    if external:
        risks.append("Contains external resource references; the asset is not self-contained.")
    if has_text:
        risks.append("Contains text; font availability can change appearance after import.")
    if has_mask or has_clip:
        risks.append("Contains mask or clipPath; complex clipping can differ in Sketch.")
    if has_filter:
        risks.append("Contains SVG filters; rendering can differ in Sketch or other editors.")
    if has_use:
        risks.append("Contains use/symbol references; expand them for more predictable Sketch import.")
    if complex_transform:
        risks.append("Contains matrix/skew/rotate transforms; flatten only after visual comparison.")
    if counts["style"] or any(element.get("class") for element in root.iter()):
        risks.append("Contains CSS classes/styles; inline styles for portable editor import.")
    status = "pass" if not risks else "warn"
    if not editable or not viewbox or width is None or height is None:
        status = "fail"
    return {
        "kind": "svg",
        "path": str(path),
        "file_extension": path.suffix.lower(),
        "actual_format": "SVG",
        "extension_matches_format": path.suffix.lower() == ".svg",
        "xml_valid": True,
        "viewBox": list(viewbox) if viewbox else None,
        "dimensions": {"width": width, "height": height},
        "elements": dict(sorted(counts.items())),
        "contains_embedded_bitmap": bool(embedded),
        "contains_external_resources": bool(external),
        "contains_text": has_text,
        "contains_mask": has_mask,
        "contains_clip_path": has_clip,
        "contains_filter": has_filter,
        "contains_use_or_symbol": has_use,
        "contains_complex_transform": complex_transform,
        "contains_css": bool(counts["style"] or any(element.get("class") for element in root.iter())),
        "editable_vector": editable,
        "true_vector": editable,
        "sketch_preflight": {"status": status, "reasons": risks},
        "risks": risks,
    }


def inspect_asset(path: Path) -> dict[str, Any]:
    return inspect_svg(path) if is_svg_path(path) else image_info(path)


def trim_to_subject(image, margin: int, background_model: BackgroundModel | None = None):
    rgba = image.convert("RGBA")
    bbox = subject_bbox_from_alpha(rgba, 1)
    if bbox is None or bbox == (0, 0, *image.size):
        # An opaque source needs visible-background analysis to trim whitespace.
        model = background_model or estimate_background(rgba, 18.0)
        pixels = rgba.load()
        foreground: list[tuple[int, int]] = []
        for y in range(rgba.height):
            for x in range(rgba.width):
                if rgb_distance(pixels[x, y][:3], model.color) > model.core_threshold:
                    foreground.append((x, y))
        if not foreground:
            die("No visible subject found for automatic trimming.")
        xs, ys = zip(*foreground)
        bbox = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
    box = expand_bbox(bbox, rgba.size, margin)
    return rgba.crop(box), {"original_bounds": list(bbox), "crop_bounds": list(box), "margin": margin}


def command_inspect(args: argparse.Namespace) -> dict[str, Any]:
    input_path = validate_input(args.input)
    info = inspect_asset(input_path)
    outdir = default_outdir(input_path, args.outdir)
    report_anchor = output_path(input_path, outdir, "inspection", ".json")
    report = {
        "action": "inspect",
        "created_at": datetime.now(UTC).isoformat(),
        "input": info,
        "output": {"path": str(report_anchor), "type": "report_only"},
        "strategy": {"shadow": "not_processed", "trimmed": False},
        "quality_checks": {"input_readable": True},
        "risks": info.get("risks", []),
    }
    paths = write_report(report_anchor, report)
    return {"input": info, "reports": paths}


def command_remove_bg(args: argparse.Namespace) -> dict[str, Any]:
    input_path = validate_input(args.input)
    if is_svg_path(input_path):
        die("remove-bg accepts raster images only. SVG is already a vector asset; use svg-check or svg-fix.")
    Image = require_modules()
    try:
        with Image.open(input_path) as opened:
            opened.load()
            original = opened.convert("RGBA")
    except Exception as exc:
        die(f"Unable to read raster image '{input_path}': {exc}")
    model = estimate_background(original, args.tolerance)
    risk = background_risk(model)
    if risk and not args.allow_complex_background:
        die(f"{risk} Refusing to guess. Crop to a flat-background area or use --allow-complex-background with visual review.")
    output_image, matte = connected_background_matte(original, model, args.shadow)
    trim_info: dict[str, Any] | None = None
    if args.trim:
        output_image, trim_info = trim_to_subject(output_image, args.margin)
    outdir = default_outdir(input_path, args.outdir)
    output = output_path(input_path, outdir, "transparent", ".png")
    output_image.save(output, format="PNG")
    output_info = image_info(output)
    quality = quality_for_png(output_image, original.size)
    quality["internal_light_pixels_preserved"] = matte["internal_light_pixels_preserved"] > 0
    quality["natural_antialiasing_present"] = matte["partial_alpha_pixels"] > 0
    quality["shadow_requirement_met"] = (
        args.shadow == "remove" or matte["partial_alpha_pixels"] > 0
    )
    risks = []
    if risk:
        risks.append(risk)
    if quality["subject_touches_canvas"]:
        risks.append("Subject reaches the output edge; increase --margin if this was not intended.")
    if matte["internal_light_pixels_preserved"] == 0:
        risks.append("No enclosed light pixels were detected automatically; inspect light details visually.")
    report = {
        "action": "remove_background",
        "created_at": datetime.now(UTC).isoformat(),
        "input": image_info(input_path),
        "output": output_info,
        "strategy": {
            "method": "edge_connected_background_matte",
            "shadow": args.shadow,
            "trimmed": args.trim,
            "trim": trim_info,
            "matte": matte,
        },
        "quality_checks": quality,
        "risks": risks,
    }
    reports = write_report(output, report)
    return {"output": str(output), "reports": reports, "quality_checks": quality}


def command_trim(args: argparse.Namespace) -> dict[str, Any]:
    input_path = validate_input(args.input)
    if is_svg_path(input_path):
        die("trim accepts raster images. For an SVG, preserve vector geometry and set an explicit viewBox instead.")
    Image = require_modules()
    try:
        with Image.open(input_path) as opened:
            opened.load()
            original = opened.convert("RGBA")
    except Exception as exc:
        die(f"Unable to read raster image '{input_path}': {exc}")
    model = estimate_background(original, args.tolerance)
    trimmed, trim_info = trim_to_subject(original, args.margin, model)
    outdir = default_outdir(input_path, args.outdir)
    output = output_path(input_path, outdir, "trimmed", ".png")
    trimmed.save(output, format="PNG")
    quality = quality_for_png(trimmed)
    quality["auto_crop_kept_safe_margin"] = args.margin >= 0
    report = {
        "action": "trim",
        "created_at": datetime.now(UTC).isoformat(),
        "input": image_info(input_path),
        "output": image_info(output),
        "strategy": {"method": "alpha_or_edge_background_bounds", "shadow": "not_changed", "trimmed": True, "trim": trim_info},
        "quality_checks": quality,
        "risks": [background_risk(model)] if background_risk(model) else [],
    }
    reports = write_report(output, report)
    return {"output": str(output), "reports": reports, "quality_checks": quality}


def parse_box(raw: str) -> tuple[int, int, int, int]:
    values = [value.strip() for value in raw.split(",")]
    if len(values) != 4:
        die("--box must be x,y,width,height using integer pixels.")
    try:
        x, y, width, height = (int(value) for value in values)
    except ValueError:
        die("--box must be x,y,width,height using integer pixels.")
    if width <= 0 or height <= 0:
        die("--box width and height must be greater than zero.")
    return x, y, width, height


def crop_box(image, box: tuple[int, int, int, int], margin: int):
    x, y, width, height = box
    left = max(0, x - margin)
    top = max(0, y - margin)
    right = min(image.width, x + width + margin)
    bottom = min(image.height, y + height + margin)
    if left >= right or top >= bottom:
        die("Requested crop box is outside the input image.")
    return image.crop((left, top, right, bottom)), [left, top, right, bottom]


def component_boxes(image, model: BackgroundModel, min_area: int) -> list[tuple[int, int, int, int, int]]:
    """Find non-background components for an initial UI-screenshot extraction pass."""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()
    active = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            if rgb_distance(pixels[x, y][:3], model.color) > model.edge_threshold:
                active[y * width + x] = 1
    seen = bytearray(width * height)
    boxes: list[tuple[int, int, int, int, int]] = []
    for seed in range(width * height):
        if not active[seed] or seen[seed]:
            continue
        queue: deque[int] = deque([seed])
        seen[seed] = 1
        min_x = max_x = seed % width
        min_y = max_y = seed // width
        area = 0
        while queue:
            point = queue.popleft()
            x, y = point % width, point // width
            area += 1
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    index = ny * width + nx
                    if active[index] and not seen[index]:
                        seen[index] = 1
                        queue.append(index)
        component_width, component_height = max_x - min_x + 1, max_y - min_y + 1
        if area >= min_area and component_width < width * 0.8 and component_height < height * 0.8:
            boxes.append((min_x, min_y, component_width, component_height, area))
    return sorted(boxes, key=lambda value: (-value[4], value[1], value[0]))


def command_extract(args: argparse.Namespace) -> dict[str, Any]:
    input_path = validate_input(args.input)
    if is_svg_path(input_path):
        die("extract accepts a UI screenshot/raster image. SVG nodes are already individually addressable.")
    Image = require_modules()
    try:
        with Image.open(input_path) as opened:
            opened.load()
            source = opened.convert("RGBA")
    except Exception as exc:
        die(f"Unable to read screenshot '{input_path}': {exc}")
    outdir = default_outdir(input_path, args.outdir)
    candidates: list[dict[str, Any]] = []
    outputs: list[str] = []
    if args.box:
        crop, bounds = crop_box(source, parse_box(args.box), args.margin)
        if args.transparent:
            model = estimate_background(crop, args.tolerance)
            risk = background_risk(model)
            if risk and not args.allow_complex_background:
                die(f"{risk} Refusing transparent extraction without a precise safe crop/background.")
            crop, matte = connected_background_matte(crop, model, args.shadow)
        else:
            matte = None
        output = output_path(input_path, outdir, "extracted", ".png")
        crop.save(output, format="PNG")
        outputs.append(str(output))
        candidates.append({"bounds": bounds, "output": str(output), "matte": matte})
    else:
        model = estimate_background(source, args.tolerance)
        risk = background_risk(model)
        if risk and not args.allow_complex_background:
            die(f"{risk} Automatic candidate extraction is unsafe. Supply --box x,y,width,height.")
        boxes = component_boxes(source, model, args.min_area)
        if not boxes:
            die("No independent foreground candidates found. Supply --box x,y,width,height.")
        for index, (x, y, width, height, area) in enumerate(boxes[: args.max_candidates], start=1):
            crop, bounds = crop_box(source, (x, y, width, height), args.margin)
            output = output_path(input_path, outdir, f"extracted-{index:02d}", ".png")
            crop.save(output, format="PNG")
            outputs.append(str(output))
            candidates.append({"bounds": bounds, "area": area, "output": str(output)})
    anchor = Path(outputs[0])
    report = {
        "action": "extract_icon",
        "created_at": datetime.now(UTC).isoformat(),
        "input": image_info(input_path),
        "output": {"path": outputs[0], "additional_outputs": outputs[1:]},
        "strategy": {
            "method": "manual_box" if args.box else "foreground_component_candidates",
            "shadow": args.shadow if args.transparent else "not_processed",
            "trimmed": False,
            "transparent_requested": args.transparent,
            "candidates": candidates,
        },
        "quality_checks": {"output_count": len(outputs), "source_unchanged": True},
        "risks": ["Automatic candidates may include text or adjacent UI decoration; visually select the intended crop."] if not args.box else [],
    }
    reports = write_report(anchor, report)
    return {"outputs": outputs, "reports": reports, "candidates": candidates}


def vector_assessment(input_path: Path) -> dict[str, Any]:
    if is_svg_path(input_path):
        info = inspect_svg(input_path)
        return {
            "suitable": False,
            "reason": "Input is already SVG; inspect or repair it instead of raster tracing.",
            "input": info,
        }
    info = image_info(input_path)
    suitable = bool(info["suitable_for_vectorization"])
    reason = (
        "Simple low-color raster icon is a candidate for path tracing."
        if suitable
        else "Complex color/gradient content is not suitable for faithful full vectorization. Prefer transparent PNG."
    )
    return {"suitable": suitable, "reason": reason, "input": info}


def command_vector_assess(args: argparse.Namespace) -> dict[str, Any]:
    input_path = validate_input(args.input)
    assessment = vector_assessment(input_path)
    outdir = default_outdir(input_path, args.outdir)
    anchor = output_path(input_path, outdir, "vector-assessment", ".json")
    report = {
        "action": "vector_assessment",
        "created_at": datetime.now(UTC).isoformat(),
        "input": assessment["input"],
        "output": {"path": str(anchor), "type": "report_only"},
        "strategy": {"shadow": "not_processed", "trimmed": False, "assessment": assessment},
        "quality_checks": {"assessment_completed": True},
        "risks": assessment["input"].get("risks", []),
    }
    reports = write_report(anchor, report)
    return {"assessment": assessment, "reports": reports}


def command_trace(args: argparse.Namespace) -> dict[str, Any]:
    input_path = validate_input(args.input)
    assessment = vector_assessment(input_path)
    if not assessment["suitable"] and not args.force_simplified:
        die(f"{assessment['reason']} Use --force-simplified only when a simplified/traced result is acceptable.")
    try:
        import vtracer
    except ImportError:
        die("VTracer is required for true path tracing. Install with: python3 -m pip install --user vtracer")
    outdir = default_outdir(input_path, args.outdir)
    action = "traced" if assessment["suitable"] else "simplified"
    output = output_path(input_path, outdir, action, ".svg")
    try:
        vtracer.convert_image_to_svg_py(
            str(input_path),
            str(output),
            colormode="binary" if assessment["input"].get("estimated_color_count", 64) <= 4 else "color",
            hierarchical="stacked",
            mode="spline",
            filter_speckle=4,
            color_precision=6,
            layer_difference=16,
            corner_threshold=60,
            length_threshold=4.0,
            max_iterations=10,
            splice_threshold=45,
            path_precision=3,
        )
    except Exception as exc:
        die(f"VTracer could not create an SVG: {exc}")
    svg_info = inspect_svg(output)
    if not svg_info.get("true_vector"):
        output.unlink(missing_ok=True)
        die("Trace result was not a true editable vector SVG; no output was kept.")
    report = {
        "action": "trace_to_svg",
        "created_at": datetime.now(UTC).isoformat(),
        "input": assessment["input"],
        "output": svg_info,
        "strategy": {
            "method": "vtracer_path_tracing",
            "shadow": "not_processed",
            "trimmed": False,
            "label": action,
            "assessment": assessment,
        },
        "quality_checks": {
            "true_editable_vector": svg_info.get("true_vector"),
            "contains_embedded_bitmap": svg_info.get("contains_embedded_bitmap"),
            "xml_valid": svg_info.get("xml_valid"),
        },
        "risks": (
            ["This is a simplified/traced SVG, not a faithful substitute for complex raster effects."]
            if action == "simplified"
            else svg_info.get("risks", [])
        ),
    }
    reports = write_report(output, report)
    return {"output": str(output), "reports": reports, "quality_checks": report["quality_checks"]}


STYLE_RULE = re.compile(r"\.([A-Za-z_][\w-]*)\s*\{([^{}]*)\}")
STYLE_PROPERTY = re.compile(r"([A-Za-z-]+)\s*:\s*([^;]+)")
PORTABLE_STYLE_PROPERTIES = {
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-opacity", "stroke-width",
    "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "opacity", "clip-rule",
}


def parse_style_properties(text: str) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for key, value in STYLE_PROPERTY.findall(text)
        if key.strip() in PORTABLE_STYLE_PROPERTIES
    }


def parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def inline_simple_css(root: ET.Element) -> dict[str, Any]:
    class_rules: dict[str, dict[str, str]] = {}
    styles: list[ET.Element] = []
    unresolved = False
    for element in root.iter():
        if local_name(element.tag) != "style":
            continue
        styles.append(element)
        text = element.text or ""
        matches = list(STYLE_RULE.finditer(text))
        if text.strip() and not matches:
            unresolved = True
        for match in matches:
            class_rules[match.group(1)] = parse_style_properties(match.group(2))
        residual = STYLE_RULE.sub("", text).strip()
        if residual:
            unresolved = True
    inlined = 0
    for element in root.iter():
        classes = (element.get("class") or "").split()
        if classes and all(item in class_rules for item in classes):
            for class_name in classes:
                for key, value in class_rules[class_name].items():
                    element.attrib.setdefault(key, value)
            element.attrib.pop("class", None)
            inlined += 1
        inline = element.get("style")
        if inline:
            properties = parse_style_properties(inline)
            if properties:
                for key, value in properties.items():
                    element.attrib.setdefault(key, value)
                element.attrib.pop("style", None)
                inlined += 1
            else:
                unresolved = True
    if not unresolved:
        parents = parent_map(root)
        for style in styles:
            parents[style].remove(style)
    return {"inlined_elements": inlined, "unresolved_css": unresolved}


def flatten_simple_use(root: ET.Element) -> dict[str, Any]:
    by_id = {element.get("id"): element for element in root.iter() if element.get("id")}
    parents = parent_map(root)
    flattened = 0
    unresolved = 0
    for use in list(root.iter()):
        if local_name(use.tag) != "use":
            continue
        href = href_for(use) or ""
        if not href.startswith("#") or href[1:] not in by_id:
            unresolved += 1
            continue
        original = by_id[href[1:]]
        if local_name(original.tag) not in {"path", "g", "rect", "circle", "ellipse", "polygon", "polyline", "line", "symbol"}:
            unresolved += 1
            continue
        clone = copy.deepcopy(original)
        if local_name(clone.tag) == "symbol":
            group = ET.Element(f"{{{SVG_NS}}}g")
            group.extend(list(clone))
            clone = group
        clone.attrib.pop("id", None)
        translate = ""
        x, y = use.get("x"), use.get("y")
        if parse_number(x) not in (None, 0) or parse_number(y) not in (None, 0):
            translate = f"translate({parse_number(x) or 0} {parse_number(y) or 0})"
        transform = " ".join(part for part in (translate, use.get("transform", ""), clone.get("transform", "")) if part)
        if transform:
            clone.set("transform", transform)
        for key, value in use.attrib.items():
            if local_name(key) not in {"href", "x", "y", "transform"}:
                clone.attrib.setdefault(key, value)
        parent = parents.get(use)
        if parent is None:
            unresolved += 1
            continue
        position = list(parent).index(use)
        parent.remove(use)
        parent.insert(position, clone)
        flattened += 1
    return {"flattened_use": flattened, "unresolved_use": unresolved}


def normalize_svg_tree(root: ET.Element) -> dict[str, Any]:
    changes: dict[str, Any] = {"metadata_removed": 0}
    parents = parent_map(root)
    for element in list(root.iter()):
        if local_name(element.tag) in {"metadata", "namedview"}:
            parent = parents.get(element)
            if parent is not None:
                parent.remove(element)
                changes["metadata_removed"] += 1
    viewbox = parse_viewbox(root.get("viewBox"))
    width, height = parse_number(root.get("width")), parse_number(root.get("height"))
    if not viewbox and width is not None and height is not None and width > 0 and height > 0:
        root.set("viewBox", f"0 0 {width:g} {height:g}")
        viewbox = (0.0, 0.0, width, height)
        changes["viewBox_added"] = True
    if viewbox:
        if width is None:
            root.set("width", f"{viewbox[2]:g}")
            changes["width_added"] = True
        if height is None:
            root.set("height", f"{viewbox[3]:g}")
            changes["height_added"] = True
    changes["css"] = inline_simple_css(root)
    changes["use"] = flatten_simple_use(root)
    return changes


def render_svg(path: Path, destination: Path, width: int, height: int) -> tuple[bool, str]:
    sips = shutil.which("sips")
    if not sips:
        return False, "macOS sips renderer is unavailable"
    result = subprocess.run(
        [sips, "-s", "format", "png", "-z", str(height), str(width), str(path), "--out", str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not destination.exists():
        return False, (result.stderr or result.stdout or "sips failed").strip()
    return True, "sips direct SVG rendering"


def visual_compare_svg(before: Path, after: Path, svg_info: dict[str, Any]) -> dict[str, Any]:
    Image = require_modules()
    viewbox = svg_info.get("viewBox") or [0, 0, 128, 128]
    ratio = float(viewbox[2]) / max(1.0, float(viewbox[3]))
    height = 512
    width = max(1, min(2048, round(height * ratio)))
    with tempfile.TemporaryDirectory(prefix="ui-asset-studio-svg-") as temp_dir:
        first, second = Path(temp_dir) / "before.png", Path(temp_dir) / "after.png"
        before_ok, before_note = render_svg(before, first, width, height)
        after_ok, after_note = render_svg(after, second, width, height)
        if not before_ok or not after_ok:
            return {"performed": False, "reason": before_note if not before_ok else after_note}
        before_pixels = list(Image.open(first).convert("RGBA").getdata())
        after_pixels = list(Image.open(second).convert("RGBA").getdata())
        absolute = [sum(abs(a - b) for a, b in zip(left, right)) / 4 for left, right in zip(before_pixels, after_pixels)]
        mean = sum(absolute) / len(absolute)
        changed = sum(1 for value in absolute if value > 12) / len(absolute)
        return {
            "performed": True,
            "renderer": "macOS_sips_direct_svg",
            "render_dimensions": {"width": width, "height": height},
            "mean_channel_difference": round(mean, 4),
            "materially_changed_pixel_ratio": round(changed, 6),
            "visual_match": mean <= 2.5 and changed <= 0.01,
        }


def run_svgo(source: Path, destination: Path) -> dict[str, Any]:
    svgo = shutil.which("svgo")
    if not svgo:
        shutil.copy2(source, destination)
        return {"performed": False, "reason": "svgo is not installed; kept normalized SVG."}
    result = subprocess.run(
        [svgo, "--multipass", "--pretty", "--disable=removeViewBox", "--input", str(source), "--output", str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        shutil.copy2(source, destination)
        return {"performed": False, "reason": (result.stderr or result.stdout).strip()}
    return {"performed": True, "tool": "svgo", "output": (result.stdout or "").strip()}


def process_svg(input_path: Path, outdir: Path, action: str, optimize: bool) -> dict[str, Any]:
    before = inspect_svg(input_path)
    if not before.get("xml_valid"):
        die("SVG XML is malformed; repair it in a vector editor before running compatibility normalization.")
    try:
        tree = ET.parse(input_path)
    except ET.ParseError as exc:
        die(f"Could not parse SVG: {exc}")
    changes = normalize_svg_tree(tree.getroot())
    output = output_path(input_path, outdir, action, ".svg")
    with tempfile.TemporaryDirectory(prefix="ui-asset-studio-svg-") as temp_dir:
        normalized = Path(temp_dir) / "normalized.svg"
        tree.write(normalized, encoding="utf-8", xml_declaration=True)
        optimization = run_svgo(normalized, output) if optimize else {"performed": False, "reason": "SVGO skipped by request."}
        if not optimize:
            shutil.copy2(normalized, output)
    after = inspect_svg(output)
    comparison = visual_compare_svg(input_path, output, before)
    quality = {
        "xml_valid": after.get("xml_valid"),
        "viewBox_present": bool(after.get("viewBox")),
        "dimensions_stable": before.get("dimensions") == after.get("dimensions"),
        "true_editable_vector": after.get("true_vector"),
        "contains_embedded_bitmap": after.get("contains_embedded_bitmap"),
        "contains_external_resources": after.get("contains_external_resources"),
        "render_visual_match": comparison.get("visual_match"),
        "sketch_preflight": after.get("sketch_preflight", {}).get("status"),
    }
    risks = list(after.get("risks", []))
    if comparison.get("performed") and not comparison.get("visual_match"):
        risks.append("Raster render comparison changed materially; inspect before adopting this SVG.")
    if not comparison.get("performed"):
        risks.append(f"Visual render comparison unavailable: {comparison.get('reason')}")
    report = {
        "action": "svg_fix" if action == "sketch" else "svg_optimize",
        "created_at": datetime.now(UTC).isoformat(),
        "input": before,
        "output": after,
        "strategy": {
            "method": "portable_svg_normalization",
            "shadow": "preserved_as_svg_content",
            "trimmed": False,
            "changes": changes,
            "optimization": optimization,
            "visual_render_comparison": comparison,
        },
        "quality_checks": quality,
        "risks": risks,
    }
    reports = write_report(output, report)
    return {"output": str(output), "reports": reports, "quality_checks": quality, "comparison": comparison}


def command_svg_check(args: argparse.Namespace) -> dict[str, Any]:
    input_path = validate_input(args.input)
    if not is_svg_path(input_path):
        die("svg-check accepts SVG files only.")
    info = inspect_svg(input_path)
    outdir = default_outdir(input_path, args.outdir)
    anchor = output_path(input_path, outdir, "svg-check", ".json")
    report = {
        "action": "svg_check",
        "created_at": datetime.now(UTC).isoformat(),
        "input": info,
        "output": {"path": str(anchor), "type": "report_only"},
        "strategy": {"shadow": "not_processed", "trimmed": False, "method": "xml_and_compatibility_preflight"},
        "quality_checks": {
            "xml_valid": info.get("xml_valid"),
            "true_editable_vector": info.get("true_vector"),
            "sketch_preflight": info.get("sketch_preflight", {}).get("status"),
        },
        "risks": info.get("risks", []),
    }
    reports = write_report(anchor, report)
    return {"svg": info, "reports": reports}


def command_svg_optimize(args: argparse.Namespace) -> dict[str, Any]:
    input_path = validate_input(args.input)
    if not is_svg_path(input_path):
        die("svg-optimize accepts SVG files only.")
    return process_svg(input_path, default_outdir(input_path, args.outdir), "optimized", not args.no_svgo)


def command_svg_fix(args: argparse.Namespace) -> dict[str, Any]:
    input_path = validate_input(args.input)
    if not is_svg_path(input_path):
        die("svg-fix accepts SVG files only.")
    return process_svg(input_path, default_outdir(input_path, args.outdir), "sketch", not args.no_svgo)


def svg_base_size(path: Path) -> tuple[int, int]:
    info = inspect_svg(path)
    dimensions = info.get("dimensions", {})
    width, height = dimensions.get("width"), dimensions.get("height")
    if width and height:
        return round(width), round(height)
    viewbox = info.get("viewBox")
    if viewbox:
        return round(viewbox[2]), round(viewbox[3])
    die("SVG needs numeric width/height or a valid viewBox for scale export.")


def parse_scales(raw: str) -> list[int]:
    try:
        scales = sorted({int(value.strip()) for value in raw.split(",")})
    except ValueError:
        die("--scales must be comma-separated positive integers, for example 1,2,3.")
    if not scales or any(value <= 0 for value in scales):
        die("--scales must contain positive integers.")
    return scales


def scale_output_path(input_path: Path, outdir: Path, scale: int) -> Path:
    return unique_path(outdir, f"{input_path.stem}@{scale}x.png")


def command_export_scales(args: argparse.Namespace) -> dict[str, Any]:
    input_path = validate_input(args.input)
    outdir = default_outdir(input_path, args.outdir)
    scales = parse_scales(args.scales)
    outputs: list[str] = []
    records: list[dict[str, Any]] = []
    is_svg = is_svg_path(input_path)
    Image = require_modules()
    if is_svg:
        natural_width, natural_height = svg_base_size(input_path)
        base_width = args.base_width or natural_width
        base_height = args.base_height or natural_height
    else:
        if args.source_scale <= 0:
            die("--source-scale must be a positive integer.")
        with Image.open(input_path) as opened:
            opened.load()
            source = opened.convert("RGBA")
        base_width = args.base_width or round(source.width / args.source_scale)
        base_height = args.base_height or round(source.height / args.source_scale)
        if base_width <= 0 or base_height <= 0:
            die("Base scale dimensions must be positive.")
        largest = max(scales)
        if (base_width * largest > source.width or base_height * largest > source.height) and not args.allow_upscale:
            die(
                "Requested scale exceeds the raster source quality. Supply a higher-resolution source, "
                "set --source-scale correctly, or explicitly use --allow-upscale."
            )
    for scale in scales:
        width, height = base_width * scale, base_height * scale
        output = scale_output_path(input_path, outdir, scale)
        if is_svg:
            ok, reason = render_svg(input_path, output, width, height)
            if not ok:
                die(f"Cannot directly render SVG scale export: {reason}")
            render_mode = "direct_vector_svg_render"
        else:
            source.resize((width, height), Image.Resampling.LANCZOS).save(output, format="PNG")
            render_mode = "lanczos_from_source_raster"
        info = image_info(output)
        records.append({"scale": scale, "path": str(output), "expected": [width, height], "actual": info["dimensions"], "mode": render_mode, "has_alpha": info["has_alpha"]})
        outputs.append(str(output))
    anchor = Path(outputs[0])
    quality = {
        "all_dimensions_correct": all(record["expected"] == [record["actual"]["width"], record["actual"]["height"]] for record in records),
        "all_outputs_have_alpha": all(record["has_alpha"] for record in records),
        "svg_rendered_directly": all(record["mode"] == "direct_vector_svg_render" for record in records) if is_svg else None,
        "source_quality_guard_applied": not args.allow_upscale,
    }
    report = {
        "action": "export_scales",
        "created_at": datetime.now(UTC).isoformat(),
        "input": inspect_asset(input_path),
        "output": {"path": outputs[0], "additional_outputs": outputs[1:]},
        "strategy": {"shadow": "preserved", "trimmed": False, "scales": records, "source_scale": args.source_scale},
        "quality_checks": quality,
        "risks": (["Raster upscaling was explicitly allowed; inspect sharpness."] if args.allow_upscale and not is_svg else []),
    }
    reports = write_report(anchor, report)
    return {"outputs": outputs, "reports": reports, "quality_checks": quality}


def icon_metrics(path: Path) -> dict[str, Any]:
    if is_svg_path(path):
        info = inspect_svg(path)
        return {
            "path": str(path),
            "kind": "svg",
            "viewBox": info.get("viewBox"),
            "element_count": sum(info.get("elements", {}).values()),
            "risk_count": len(info.get("risks", [])),
            "true_vector": info.get("true_vector"),
        }
    Image = require_modules()
    with Image.open(path) as opened:
        opened.load()
        image = opened.convert("RGBA")
    bbox = subject_bbox_from_alpha(image, 8)
    if bbox is None or bbox == (0, 0, image.width, image.height):
        model = estimate_background(image, 18.0)
        pixels = image.load()
        active = [(x, y) for y in range(image.height) for x in range(image.width) if rgb_distance(pixels[x, y][:3], model.color) > model.core_threshold]
        if active:
            xs, ys = zip(*active)
            bbox = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
    if bbox is None:
        return {"path": str(path), "kind": "raster", "error": "No visible icon bounds found."}
    left, top, right, bottom = bbox
    visible_width, visible_height = right - left, bottom - top
    coverage = (visible_width * visible_height) / (image.width * image.height)
    center_x, center_y = (left + right) / 2, (top + bottom) / 2
    colors = image.convert("RGB").quantize(colors=32).getcolors(32) or []
    return {
        "path": str(path),
        "kind": "raster",
        "canvas": [image.width, image.height],
        "visible_bounds": list(bbox),
        "visible_size_ratio": [round(visible_width / image.width, 4), round(visible_height / image.height, 4)],
        "visible_area_ratio": round(coverage, 4),
        "center_offset_ratio": [round((center_x - image.width / 2) / image.width, 4), round((center_y - image.height / 2) / image.height, 4)],
        "estimated_color_count": len(colors),
    }


def command_review_icons(args: argparse.Namespace) -> dict[str, Any]:
    paths = [validate_input(value) for value in args.inputs]
    metrics = [icon_metrics(path) for path in paths]
    raster = [item for item in metrics if item.get("kind") == "raster" and "visible_area_ratio" in item]
    outliers: list[dict[str, Any]] = []
    if len(raster) >= 2:
        mean_coverage = sum(item["visible_area_ratio"] for item in raster) / len(raster)
        for item in raster:
            delta = abs(item["visible_area_ratio"] - mean_coverage)
            if delta > 0.12:
                outliers.append({"path": item["path"], "metric": "visible_area_ratio", "delta": round(delta, 4)})
    outdir = Path(args.outdir).expanduser().resolve() if args.outdir else paths[0].parent / "ui-asset-studio-output"
    anchor = output_path(paths[0], outdir, "icon-review", ".json")
    report = {
        "action": "review_icons",
        "created_at": datetime.now(UTC).isoformat(),
        "input": {"paths": [str(path) for path in paths]},
        "output": {"path": str(anchor), "type": "report_only"},
        "strategy": {"method": "non_destructive_geometry_metrics", "shadow": "not_processed", "trimmed": False},
        "quality_checks": {"review_only": True, "icon_count": len(paths), "metric_outliers": outliers},
        "risks": ["Metrics support visual review; they do not replace human judgment of semantics, stroke language, or brand fit."],
        "metrics": metrics,
    }
    reports = write_report(anchor, report)
    return {"metrics": metrics, "outliers": outliers, "reports": reports}


def add_common_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, help="Input asset path.")
    parser.add_argument("--outdir", help="Independent output directory. Defaults beside input to ui-asset-studio-output.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline, non-destructive UI asset processing.")
    parser.add_argument("--version", action="version", version=f"ui-asset-studio {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="Inspect input format, alpha, background, and risks.")
    add_common_input(inspect)
    inspect.set_defaults(handler=command_inspect)

    remove = commands.add_parser("remove-bg", help="Create a true-alpha PNG using edge-connected background removal.")
    add_common_input(remove)
    remove.add_argument("--shadow", choices=("preserve", "remove"), default="preserve")
    remove.add_argument("--trim", action=argparse.BooleanOptionalAction, default=True)
    remove.add_argument("--margin", type=int, default=8)
    remove.add_argument("--tolerance", type=float, default=18.0)
    remove.add_argument("--allow-complex-background", action="store_true")
    remove.set_defaults(handler=command_remove_bg)

    trim = commands.add_parser("trim", help="Crop visible subject and retain a safe margin.")
    add_common_input(trim)
    trim.add_argument("--margin", type=int, default=8)
    trim.add_argument("--tolerance", type=float, default=18.0)
    trim.set_defaults(handler=command_trim)

    extract = commands.add_parser("extract", help="Extract a precise or candidate icon crop from a UI screenshot.")
    add_common_input(extract)
    extract.add_argument("--box", help="Exact x,y,width,height crop in source pixels.")
    extract.add_argument("--max-candidates", type=int, default=12)
    extract.add_argument("--min-area", type=int, default=24)
    extract.add_argument("--margin", type=int, default=4)
    extract.add_argument("--tolerance", type=float, default=18.0)
    extract.add_argument("--transparent", action="store_true")
    extract.add_argument("--shadow", choices=("preserve", "remove"), default="preserve")
    extract.add_argument("--allow-complex-background", action="store_true")
    extract.set_defaults(handler=command_extract)

    assess = commands.add_parser("vector-assess", help="Assess whether a bitmap is appropriate for true vector tracing.")
    add_common_input(assess)
    assess.set_defaults(handler=command_vector_assess)

    trace = commands.add_parser("trace", help="Trace a suitable simple bitmap to path-based SVG.")
    add_common_input(trace)
    trace.add_argument("--force-simplified", action="store_true", help="Permit a clearly labelled simplified trace for an unsuitable source.")
    trace.set_defaults(handler=command_trace)

    check = commands.add_parser("svg-check", help="Check SVG truthfulness and Sketch compatibility risks.")
    add_common_input(check)
    check.set_defaults(handler=command_svg_check)

    optimize = commands.add_parser("svg-optimize", help="Normalize and optimize an SVG without changing its source.")
    add_common_input(optimize)
    optimize.add_argument("--no-svgo", action="store_true")
    optimize.set_defaults(handler=command_svg_optimize)

    fix = commands.add_parser("svg-fix", help="Create a Sketch-oriented portable SVG and validate rendering.")
    add_common_input(fix)
    fix.add_argument("--no-svgo", action="store_true")
    fix.set_defaults(handler=command_svg_fix)

    scales = commands.add_parser("export-scales", help="Export true PNG @1x/@2x/@3x variants.")
    add_common_input(scales)
    scales.add_argument("--scales", default="1,2,3")
    scales.add_argument("--base-width", type=int)
    scales.add_argument("--base-height", type=int)
    scales.add_argument("--source-scale", type=int, default=1)
    scales.add_argument("--allow-upscale", action="store_true")
    scales.set_defaults(handler=command_export_scales)

    review = commands.add_parser("review-icons", help="Create non-destructive visual-balance metrics for one icon or a group.")
    review.add_argument("--inputs", nargs="+", required=True, help="One or more SVG/raster icon paths.")
    review.add_argument("--outdir", help="Independent report directory.")
    review.set_defaults(handler=command_review_icons)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except AssetError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Error: processing cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:  # Defensive: never pretend an unexpected processing error succeeded.
        print(f"Error: unexpected failure: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
