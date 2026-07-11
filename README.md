# UI Asset Studio

An open-source Codex plugin for faithful, offline-first UI asset processing. It isolates existing artwork without redesigning it, creates true-alpha PNGs, and helps make SVG assets stable in Sketch.

## Install

Add this repository as a Codex marketplace:

```bash
codex plugin marketplace add qiyue2015/ui-asset-studio --sparse .agents/plugins
```

Restart the Codex desktop app, select the **UI Asset Studio** marketplace in the plugin directory, install the plugin, and start a new task.

## What It Does

- Remove only the border-connected canvas background and emit real-alpha PNGs.
- Preserve or remove exterior shadows, trim safely, and extract icon crops from UI screenshots.
- Assess bitmap vectorization, trace suitable simple icons to path-based SVG, and reject bitmap-in-SVG impostors.
- Check, optimize, and repair SVGs for stable Sketch import without silently flattening incompatible effects.
- Export PNG assets at `@1x`, `@2x`, and `@3x`.
- Review individual UI assets or icon-set consistency without modifying review-only inputs.
- Turn a design request into a constrained Image-2 prompt while preserving reference-image invariants.

The bundled Skill is discovered from natural language, including requests such as `抠图`, `去白底`, `保留阴影`, `提取图标`, `转成 SVG`, `Sketch 打开变形`, `检查真矢量`, `导出 2x`, and `优化 Image-2 提示词`.

## Usage

After installing, describe the goal naturally:

```text
把这张红色活动标签抠出来，背景透明，保留阴影，不要重新设计。
```

For local CLI development:

```bash
python3 -m pip install -r plugins/ui-asset-studio/skills/ui-asset-studio/scripts/requirements.txt
python3 plugins/ui-asset-studio/skills/ui-asset-studio/scripts/ui_asset_studio.py remove-bg \
  --input label.png --outdir ui-assets --shadow preserve --trim --margin 8
```

## Development

```bash
python3 -m unittest discover \
  -s plugins/ui-asset-studio/skills/ui-asset-studio/tests \
  -p 'test_*.py'
```

The core workflow requires only local, free dependencies: Pillow and VTracer. SVG optimization can use `svgo` when it is installed. Direct Sketch import validation is optional and requires Sketch on macOS.

## Guarantees And Limits

- Source files are never overwritten; output files use collision-safe names in an independent output directory.
- Background removal preserves internal white text, highlights, and icon details rather than deleting all white pixels.
- A file is called editable SVG only when it contains paths or geometry, never when it embeds a PNG.
- Photos, textures, smoke, complex lighting, and 3D effects are reported as unsuitable for full vectorization. The workflow recommends transparent PNGs or explicitly labelled simplified traces instead.

## Layout

```text
.agents/plugins/marketplace.json       # Codex marketplace catalog
plugins/ui-asset-studio/               # Installable plugin
  .codex-plugin/plugin.json            # Plugin manifest
  skills/ui-asset-studio/              # Skill, CLI, references, and tests
```

## License

[MIT](LICENSE)
