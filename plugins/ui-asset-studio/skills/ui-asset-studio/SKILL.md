---
name: ui-asset-studio
description: "Faithfully process existing UI image assets without redesign: create true-alpha transparent PNG cutouts, preserve or remove shadows, trim/extract screenshot icons, assess and trace simple bitmaps to editable SVG, optimize/check/fix Sketch-oriented SVG, export @1x/@2x/@3x, review UI-icon quality or consistency, and structure Image-2 prompts. Use for PNG/JPG/WebP/SVG/UI screenshots and requests such as '抠图', '去白底', '保留阴影', '裁掉空白', '提取图标', '转成 SVG', 'Sketch 打开变形', '检查真矢量', '导出 2x', '图标是否统一', or '优化 Image-2 提示词'. Do not use to redesign, regenerate, or replace an existing asset unless the user explicitly asks."
---

# UI Asset Studio

Process UI assets faithfully, non-destructively, and with inspectable evidence.
Use the bundled offline CLI for deterministic file work. Use visual inspection for aesthetic review; its metrics do not replace design judgment.

## Non-Negotiable Rules

- Preserve the original shape, proportion, layout, text, numbers, icon construction, color, gradient, highlight, stroke, shadow, and decoration unless the user explicitly requests a design change.
- Never treat a cutout request as a request to regenerate a similar image.
- Never remove every white pixel. Remove only the border-connected canvas background; preserve enclosed white text, symbols, highlights, antialiasing, and semi-transparent edges.
- Never call an SVG editable if it embeds a bitmap through `image`, base64, or an external URL. For unsuitable photos, textures, smoke, complex lighting, or 3D effects, recommend transparent PNG. Mark any intentional approximate trace as `simplified` or `traced`.
- Never overwrite an input. Keep outputs in an independent directory and rely on the CLI's collision-safe names.
- Do not alter files for review-only requests.

## First Pass

1. Identify every supplied file and the requested outcome. Treat missing, unreadable, unsupported, or ambiguous inputs as a clear error, not a successful result.
2. Inspect each physical asset before processing:

   ```bash
   python3 scripts/ui_asset_studio.py inspect --input <asset> --outdir <output-dir>
   ```

3. Read the generated JSON report for actual format, extension mismatch, size, ratio, color mode, alpha, estimated canvas background, gradient/vectorization risk, and SVG compatibility risk.
4. Choose the smallest faithful operation. Do not chain a trace after a cutout or an optimization after an SVG fix unless the user asked for both.
5. Return output paths, report paths, handling strategy, alpha/shadow/trim status, quality checks, and any remaining risk.

Default output is `<input-parent>/ui-asset-studio-output`. Pass `--outdir <directory>` when the user specifies an output location. The CLI never overwrites an existing result; later runs use a numbered sibling.

## Task Routing

| Intent | Action |
| --- | --- |
| "抠图", "去白底", transparent PNG | `remove-bg`; default `--shadow preserve --trim --margin 8` |
| "去掉阴影" | `remove-bg --shadow remove` |
| "裁掉空白", crop subject | `trim --margin <safe-pixels>` |
| "从 UI 稿提取图标" | Prefer exact `extract --box x,y,width,height`; use candidate mode only when a flat canvas background makes it safe |
| "转成 SVG" | `vector-assess`, then `trace` only for a suitable simple icon |
| "检查 SVG / 真矢量" | `svg-check` |
| "优化 SVG" | `svg-optimize` |
| "Sketch 打开变形" | `svg-check`, then `svg-fix`, then inspect the render-comparison and Sketch-preflight report |
| "导出 1x/2x/3x" | `export-scales` with an explicit source scale for raster input |
| "图标是否统一" / visual quality | Read [references/icon-review.md](references/icon-review.md), inspect the images, then optionally run `review-icons` for geometry evidence only |
| "Image-2 提示词" | Read [references/image-2-prompts.md](references/image-2-prompts.md); write a complete prompt and do not generate an image unless asked |

## Raster Workflow

Run a faithful cutout as follows:

```bash
python3 scripts/ui_asset_studio.py remove-bg \
  --input <source.png> \
  --outdir <output-dir> \
  --shadow preserve \
  --trim --margin 8
```

- The matte algorithm estimates the background from image-border pixels and removes only pixels connected to that border. This preserves white content enclosed by a colored subject.
- Use `--shadow remove` only when requested. It conservatively removes low-saturation edge-connected shadow pixels; inspect the report and image for soft-effect edge cases.
- If the report says the border is textured or gradient-heavy, stop rather than forcing an unreliable result. Request a tight crop, an exact icon box, a mask, or use `--allow-complex-background` only with explicit visual review.
- `remove-bg` always emits a PNG in RGBA mode. Confirm `true_alpha_channel`, `transparent_corners`, `subject_present`, `internal_light_pixels_preserved`, and `shadow_requirement_met` in the report.
- For a crop only, use `trim`; it changes canvas bounds but retains original pixels and produces a PNG to preserve possible alpha.

For screenshot extraction, prefer a precise crop:

```bash
python3 scripts/ui_asset_studio.py extract \
  --input <screen.png> --box 120,340,48,48 --margin 4 \
  --outdir <output-dir> --transparent --shadow preserve
```

Without `--box`, `extract` creates candidate crops only. It may detect nearby text or decoration, so visually select the intended candidate before delivery.

## SVG Workflow

Read [references/svg-sketch.md](references/svg-sketch.md) for compatibility rules before repairing a non-trivial SVG.

```bash
python3 scripts/ui_asset_studio.py svg-check --input <icon.svg> --outdir <output-dir>
python3 scripts/ui_asset_studio.py svg-fix --input <icon.svg> --outdir <output-dir>
```

- `svg-check` validates XML and reports viewBox, dimensions, embedded bitmaps, external resources, text, masks, clip paths, filters, `use`/`symbol`, CSS, and transform risks.
- `svg-fix` preserves the source and writes `<name>.sketch.svg`. It adds safe missing dimensions/viewBox, inlines simple CSS, expands safe internal `use` references, removes editor metadata, runs SVGO when available, and directly raster-renders before/after with macOS `sips` for visual comparison.
- Do not claim "Sketch safe" when `sketch_preflight` is `warn` or `fail`, or when `render_visual_match` is false. Explain the specific blocker instead of flattening a mask/filter/text effect destructively.
- For a bitmap, run `vector-assess` before `trace`. Trace only a simple, low-color, low-gradient icon. `trace` uses VTracer paths and rejects a raster-in-SVG result. Use `--force-simplified` only when the user accepts a clearly labelled approximation.

## Multiple Scales

```bash
# A 3x PNG source: create 1x, 2x, and 3x without enlargement.
python3 scripts/ui_asset_studio.py export-scales \
  --input <icon.png> --source-scale 3 --scales 1,2,3 --outdir <output-dir>

# Render every PNG directly from vector source.
python3 scripts/ui_asset_studio.py export-scales \
  --input <icon.svg> --base-width 24 --base-height 24 --scales 1,2,3 --outdir <output-dir>
```

Do not use `--allow-upscale` unless the user accepts a quality risk. For SVG, the CLI renders each target size directly from vector source, not from a small raster intermediate. Verify every expected size and alpha result in the report.

## Visual Review

For one asset or a group, inspect the actual images first. Review only; do not modify files. Use the standards in [references/icon-review.md](references/icon-review.md), then give a direct conclusion:

1. State whether the set is coherent and name the most inconsistent icon.
2. Prioritize visual weight, stroke width, corner language, occupied area, optical center, whitespace, negative space, line-vs-fill language, detail density, small-size recognition, active/inactive states, color, and brand relation.
3. Call out illustration-like, overly expressive, childish, or semantically ambiguous treatment when relevant.
4. Give concrete adjustment directions, not vague advice.

Use metrics only to support the review:

```bash
python3 scripts/ui_asset_studio.py review-icons --inputs <icon-a> <icon-b> <icon-c> --outdir <output-dir>
```

## Image-2 Prompt Shaping

Read [references/image-2-prompts.md](references/image-2-prompts.md). Return a ready-to-use prompt with the user's required invariants. For cutout prompts, explicitly say that the uploaded image is the exact source; isolate the existing subject only; do not redesign, regenerate, crop, deform, add decoration, or draw a checkerboard; preserve text/numbers/icons/gradients/highlights/strokes/shadows as requested; delete only the outside background; and output a real transparent PNG.

## Dependencies and Failure Handling

The deterministic CLI needs local, free dependencies only:

```bash
python3 -m pip install --user -r scripts/requirements.txt
```

It also uses `svgo` for optional SVG optimization and macOS `sips` for direct SVG rendering/visual comparison when they are available. Do not install model weights or use a paid cloud API for the basic workflow.

When a command fails, report its exact error and the safe next action. Never fabricate a processing result, alpha channel, vector conversion, visual comparison, or test pass.
