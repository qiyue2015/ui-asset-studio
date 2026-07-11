# SVG And Sketch Compatibility

## Preflight Interpretation

`svg-check` is the source of truth for local structural checks. A `pass` means valid, self-contained path/shape vector content with a stable viewBox and numeric dimensions. A `warn` means it may render but has a portability risk. A `fail` means it is malformed, bitmap-backed, lacks stable geometry, or otherwise cannot be claimed as a stable editable SVG.

| Finding | Why it matters | Safe response |
| --- | --- | --- |
| Missing `viewBox` | Import size can drift | Add it only when numeric width and height establish the intended geometry |
| Missing/relative dimensions | Host decides display size | Set numeric width/height from the existing viewBox where safe |
| Base64 or `<image>` raster | Not fully editable vector | Keep/use transparent PNG; do not call it a true SVG |
| External `href`/CSS URL | Broken or changed content after import | Embed neither; resolve to local vector paths or flag it |
| `<text>` / external fonts | Glyphs can reflow or substitute | Preserve unless a vector editor can convert the exact font to paths; report the risk |
| `mask`, complex `clipPath`, `filter` | Sketch may render differently | Preserve visual intent and report risk; do not delete it just to get a green check |
| `use`/`symbol` | Reference support differs by importer | Expand safe same-file references, then re-check |
| CSS classes / style blocks | Importer CSS support varies | Inline simple presentational attributes without changing values |
| matrix/skew/rotate transform | Geometry can shift after flattening | Preserve it unless a vector renderer proves an equivalent flattening |

## Safe Repair Order

1. Run `svg-check` and keep the original untouched.
2. Run `svg-fix`; it writes `<name>.sketch.svg`, never in place.
3. Read the JSON report. Require valid XML, a stable viewBox/dimensions, no embedded bitmap/external resource, and a passing direct-render comparison before describing the result as visually preserved.
4. If masks, filters, text, or unresolved references remain, state the exact remaining risk. Do not replace the SVG with a PNG inside SVG.
5. For an actual user-visible Sketch delivery, import the generated SVG into the requested document and inspect at its intended size. The bundled command provides structural and renderer evidence; it does not automate a foreground Sketch document or overwrite user work.

## Optimization Guardrails

SVGO reduces redundant markup after portable normalization. It must not be used as a reason to change artwork. Keep `viewBox` and width/height, rerun `svg-check`, and compare before/after renders. If the comparison fails, keep the normalized file only for diagnosis and deliver the original until the visual difference is resolved.
