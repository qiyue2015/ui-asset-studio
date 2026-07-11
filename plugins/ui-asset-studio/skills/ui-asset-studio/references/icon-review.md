# UI Icon Review Standard

Review the images as a system, not as isolated illustrations. Do not edit anything when the user asked only for assessment.

## Check In This Order

1. **Visual weight and occupancy:** Similar icons should occupy comparable optical area at the target size. A tiny glyph beside a dense filled mark reads inconsistent even if both fit the same canvas.
2. **Stroke and corner language:** Compare nominal and perceived stroke width, cap/join treatment, sharpness, and corner radius. Avoid mixed geometric languages without an explicit semantic reason.
3. **Optical balance:** Check center of mass, left/right balance, top/bottom balance, baseline, and negative space. Mathematical centering is not always optical centering.
4. **Line/fill/detail language:** Avoid an arbitrary mix of outline, solid, duotone, or illustration-like styles. Check detail density and whether each icon survives at the intended small size.
5. **State and color:** Compare inactive/active behavior, contrast, hue/value, and whether color carries meaning consistently.
6. **Semantics and brand fit:** Flag ambiguous, facial/expression-like, childish, decorative, or overly illustrative treatment when it weakens a UI control.

## Required Output Shape

- Verdict: `consistent`, `mostly consistent`, or `inconsistent`.
- Most problematic icon: name it and say why first.
- Top three issues: prioritize the largest system-level mismatch.
- Concrete adjustments: specify target weight, stroke/corner/occupied-area direction, not only "make it more unified".
- Scope note: `review only; no asset files changed`.
