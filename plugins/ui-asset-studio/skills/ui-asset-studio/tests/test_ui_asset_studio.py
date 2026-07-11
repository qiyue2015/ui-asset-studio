from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from PIL import Image, ImageDraw, ImageFilter


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts" / "ui_asset_studio.py"


def write_badge(path: Path) -> None:
    """Create a white-canvas label with an exterior shadow and interior white mark."""
    canvas = Image.new("RGBA", (160, 110), (255, 255, 255, 255))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((28, 22, 132, 82), radius=13, fill=(0, 0, 0, 95))
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))
    canvas.alpha_composite(shadow)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((30, 18, 130, 78), radius=12, fill=(208, 37, 47, 255))
    draw.ellipse((68, 33, 92, 57), fill=(255, 255, 255, 255))
    draw.rectangle((96, 34, 113, 56), fill=(255, 255, 255, 255))
    canvas.convert("RGB").save(path, format="PNG")


def write_simple_icon(path: Path) -> None:
    image = Image.new("RGB", (72, 72), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((14, 14, 58, 58), radius=8, fill="black")
    draw.rectangle((31, 22, 41, 50), fill="white")
    image.save(path, format="PNG")


def write_simple_svg(path: Path) -> None:
    path.write_text(
        """<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\">\n"
        "<style>.ink { fill: #1e293b; }</style>\n"
        "<defs><path id=\"dot\" d=\"M2 2h8v8H2z\"/></defs>\n"
        "<use href=\"#dot\" class=\"ink\" x=\"7\" y=\"7\"/>\n"
        "</svg>\n""",
        encoding="utf-8",
    )


class UiAssetStudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ui-asset-studio-test-")
        self.root = Path(self.temp.name)
        self.outdir = self.root / "out"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *arguments: str, expected: int = 0) -> dict:
        result = subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            expected,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        if expected:
            return {"stderr": result.stderr, "stdout": result.stdout}
        return json.loads(result.stdout)

    def read_report(self, payload: dict) -> dict:
        report_path = Path(payload["reports"]["json"])
        self.assertTrue(report_path.is_file())
        return json.loads(report_path.read_text(encoding="utf-8"))

    def test_inspect_identifies_extension_mismatch(self) -> None:
        source = self.root / "label.jpg"
        write_badge(source)
        payload = self.run_cli("inspect", "--input", str(source), "--outdir", str(self.outdir))
        report = self.read_report(payload)
        self.assertEqual(report["input"]["actual_format"], "PNG")
        self.assertFalse(report["input"]["extension_matches_format"])

    def test_remove_background_preserves_internal_white_and_real_alpha(self) -> None:
        source = self.root / "label.png"
        write_badge(source)
        source_bytes = source.read_bytes()
        payload = self.run_cli(
            "remove-bg", "--input", str(source), "--outdir", str(self.outdir),
            "--shadow", "preserve", "--trim", "--margin", "6",
        )
        result = Path(payload["output"])
        report = self.read_report(payload)
        self.assertEqual(source.read_bytes(), source_bytes, "source must not be modified")
        self.assertEqual(result.suffix, ".png")
        with Image.open(result) as image:
            rgba = image.convert("RGBA")
            self.assertEqual(rgba.mode, "RGBA")
            corners = [rgba.getpixel(point)[3] for point in ((0, 0), (rgba.width - 1, 0), (0, rgba.height - 1), (rgba.width - 1, rgba.height - 1))]
            self.assertEqual(corners, [0, 0, 0, 0])
            pixels = rgba.load()
            white_opaque = sum(
                1
                for y in range(rgba.height)
                for x in range(rgba.width)
                if pixels[x, y][0] > 240
                and pixels[x, y][1] > 240
                and pixels[x, y][2] > 240
                and pixels[x, y][3] > 245
            )
            self.assertGreater(white_opaque, 100, "interior white mark should remain opaque")
        checks = report["quality_checks"]
        self.assertTrue(checks["true_alpha_channel"])
        self.assertTrue(checks["transparent_corners"])
        self.assertTrue(checks["internal_light_pixels_preserved"])
        self.assertGreater(checks["partial_alpha_pixels"], 0)

    def test_remove_background_can_remove_shadow_without_replacing_source(self) -> None:
        source = self.root / "label.png"
        write_badge(source)
        payload = self.run_cli(
            "remove-bg", "--input", str(source), "--outdir", str(self.outdir),
            "--shadow", "remove", "--no-trim",
        )
        report = self.read_report(payload)
        self.assertGreater(report["strategy"]["matte"]["shadow_pixels_removed"], 0)
        self.assertEqual(report["strategy"]["shadow"], "remove")
        self.assertTrue(Path(payload["output"]).is_file())

    def test_trim_removes_opaque_canvas_whitespace(self) -> None:
        source = self.root / "trim-source.png"
        image = Image.new("RGB", (100, 80), "white")
        ImageDraw.Draw(image).rectangle((40, 28, 59, 51), fill="black")
        image.save(source)
        payload = self.run_cli("trim", "--input", str(source), "--outdir", str(self.outdir), "--margin", "3")
        with Image.open(payload["output"]) as result:
            self.assertEqual(result.size, (26, 30))

    def test_extract_precise_box_is_non_destructive(self) -> None:
        source = self.root / "screen.png"
        image = Image.new("RGB", (100, 80), "white")
        ImageDraw.Draw(image).rectangle((30, 20, 49, 39), fill="black")
        image.save(source)
        payload = self.run_cli(
            "extract", "--input", str(source), "--outdir", str(self.outdir),
            "--box", "30,20,20,20", "--margin", "2",
        )
        with Image.open(payload["outputs"][0]) as result:
            self.assertEqual(result.size, (24, 24))
        self.assertTrue(Path(payload["reports"]["markdown"]).is_file())

    def test_trace_creates_true_path_svg(self) -> None:
        source = self.root / "simple.png"
        write_simple_icon(source)
        assessment = self.run_cli("vector-assess", "--input", str(source), "--outdir", str(self.outdir))
        self.assertTrue(assessment["assessment"]["suitable"])
        payload = self.run_cli("trace", "--input", str(source), "--outdir", str(self.outdir))
        output = Path(payload["output"])
        report = self.read_report(payload)
        self.assertEqual(output.suffix, ".svg")
        self.assertNotIn("<image", output.read_text(encoding="utf-8").lower())
        self.assertTrue(report["quality_checks"]["true_editable_vector"])
        self.assertFalse(report["quality_checks"]["contains_embedded_bitmap"])

    def test_svg_check_detects_embedded_bitmap(self) -> None:
        source = self.root / "fake.svg"
        source.write_text(
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\" viewBox=\"0 0 10 10\">"
            "<image href=\"data:image/png;base64,AA==\" width=\"10\" height=\"10\"/></svg>",
            encoding="utf-8",
        )
        payload = self.run_cli("svg-check", "--input", str(source), "--outdir", str(self.outdir))
        info = payload["svg"]
        self.assertTrue(info["contains_embedded_bitmap"])
        self.assertFalse(info["true_vector"])
        self.assertEqual(info["sketch_preflight"]["status"], "fail")

    def test_svg_fix_expands_simple_use_and_checks_visual_render(self) -> None:
        source = self.root / "source.svg"
        write_simple_svg(source)
        payload = self.run_cli("svg-fix", "--input", str(source), "--outdir", str(self.outdir))
        output = Path(payload["output"])
        report = self.read_report(payload)
        text = output.read_text(encoding="utf-8")
        self.assertNotIn("<use", text)
        self.assertNotIn("<style", text)
        self.assertTrue(report["quality_checks"]["xml_valid"])
        self.assertTrue(report["quality_checks"]["viewBox_present"])
        self.assertTrue(report["strategy"]["visual_render_comparison"]["performed"])
        self.assertTrue(report["quality_checks"]["render_visual_match"])

    def test_export_scales_preserves_alpha_and_names(self) -> None:
        source = self.root / "source.png"
        image = Image.new("RGBA", (72, 72), (0, 0, 0, 0))
        ImageDraw.Draw(image).ellipse((12, 12, 60, 60), fill=(20, 40, 200, 255))
        image.save(source)
        payload = self.run_cli(
            "export-scales", "--input", str(source), "--outdir", str(self.outdir),
            "--source-scale", "3", "--scales", "1,2,3",
        )
        expected = [(24, 24), (48, 48), (72, 72)]
        for path, size, suffix in zip(payload["outputs"], expected, ("@1x.png", "@2x.png", "@3x.png")):
            self.assertTrue(Path(path).name.endswith(suffix))
            with Image.open(path) as image:
                self.assertEqual(image.size, size)
                self.assertEqual(image.mode, "RGBA")
        self.assertTrue(payload["quality_checks"]["all_dimensions_correct"])
        self.assertTrue(payload["quality_checks"]["all_outputs_have_alpha"])

    def test_export_svg_scales_renders_directly_from_vector(self) -> None:
        source = self.root / "source.svg"
        write_simple_svg(source)
        payload = self.run_cli(
            "export-scales", "--input", str(source), "--outdir", str(self.outdir),
            "--base-width", "24", "--base-height", "24", "--scales", "1,2,3",
        )
        self.assertTrue(payload["quality_checks"]["svg_rendered_directly"])
        for path, expected in zip(payload["outputs"], ((24, 24), (48, 48), (72, 72))):
            with Image.open(path) as image:
                self.assertEqual(image.size, expected)

    def test_review_is_report_only(self) -> None:
        first, second = self.root / "first.png", self.root / "second.png"
        write_simple_icon(first)
        write_simple_icon(second)
        first_bytes = first.read_bytes()
        payload = self.run_cli("review-icons", "--inputs", str(first), str(second), "--outdir", str(self.outdir))
        self.assertEqual(first.read_bytes(), first_bytes)
        report = self.read_report(payload)
        self.assertTrue(report["quality_checks"]["review_only"])
        self.assertEqual(report["quality_checks"]["icon_count"], 2)

    def test_missing_file_has_clear_failure(self) -> None:
        result = self.run_cli("inspect", "--input", str(self.root / "missing.png"), expected=2)
        self.assertIn("Input file does not exist", result["stderr"])


@unittest.skipUnless(
    os.environ.get("UI_ASSET_STUDIO_RUN_SKETCH_IMPORT_TEST") == "1",
    "Set UI_ASSET_STUDIO_RUN_SKETCH_IMPORT_TEST=1 to run the isolated live Sketch import test.",
)
class SketchImportIntegrationTests(unittest.TestCase):
    @staticmethod
    def find_sketchtool() -> Path | None:
        from_path = shutil.which("sketchtool")
        if from_path:
            return Path(from_path)
        result = subprocess.run(
            ["mdfind", 'kMDItemCFBundleIdentifier == "com.bohemiancoding.sketch3"'],
            capture_output=True,
            text=True,
            check=False,
        )
        for application in (Path(value) for value in result.stdout.splitlines() if value.strip()):
            candidate = application / "Contents" / "MacOS" / "sketchtool"
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def sketch_pids() -> set[int]:
        result = subprocess.run(
            ["pgrep", "-x", "Sketch"],
            capture_output=True,
            text=True,
            check=False,
        )
        return {int(value) for value in result.stdout.split() if value.isdigit()}

    @staticmethod
    def stop_isolated_sketches(pids: set[int]) -> None:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and SketchImportIntegrationTests.sketch_pids() & pids:
            time.sleep(0.25)
        for pid in SketchImportIntegrationTests.sketch_pids() & pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_sketch_importer_accepts_repaired_svg(self) -> None:
        sketchtool = self.find_sketchtool()
        if not sketchtool:
            self.skipTest("Sketch sketchtool is not installed.")
        before = self.sketch_pids()
        if before:
            self.skipTest("Sketch is already running; do not disturb an existing user session.")
        with tempfile.TemporaryDirectory(prefix="ui-asset-studio-sketch-") as directory:
            root = Path(directory)
            source = root / "source.svg"
            write_simple_svg(source)
            outdir = root / "out"
            fixed = subprocess.run(
                [sys.executable, str(CLI), "svg-fix", "--input", str(source), "--outdir", str(outdir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(fixed.returncode, 0, fixed.stderr)
            repaired = Path(json.loads(fixed.stdout)["output"])
            context = json.dumps({"before": str(source), "after": str(repaired)})
            script = (
                'const dom = require("sketch/dom"); '
                'function load(path) { '
                'const source = String(NSString.stringWithContentsOfFile_encoding_error(path, NSUTF8StringEncoding, null)); '
                'const layer = dom.createLayerFromData(source, "svg"); '
                'if (!layer) { throw new Error("SVG import returned no layer for " + path); } '
                'return layer.type; } '
                'console.log(JSON.stringify({before: load(context.before), after: load(context.after)}));'
            )
            try:
                imported = subprocess.run(
                    [
                        str(sketchtool), "run-script", script,
                        "--context", context,
                        "--new-instance=yes", "--without-activating=yes", "--timeout=20",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(imported.returncode, 0, imported.stderr + imported.stdout)
                self.assertIn('"before"', imported.stdout)
                self.assertIn('"after"', imported.stdout)
                self.assertNotIn("nativeException", imported.stderr + imported.stdout)
            finally:
                self.stop_isolated_sketches(self.sketch_pids() - before)


if __name__ == "__main__":
    unittest.main()
