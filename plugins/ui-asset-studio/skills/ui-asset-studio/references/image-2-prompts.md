# Image-2 Prompt Shaping

Return a complete prompt in the user's language. Use only relevant sections, but preserve explicit requirements verbatim where possible.

```text
Task goal: <what the output must achieve>
Reference image use: <exact source / style-only / composition-only>
Asset type: <UI asset, banner, product visual, etc.>
Subject: <existing or requested subject>
Composition and aspect ratio: <canvas size, placement, safety area>
Style: <only requested style>
Color, lighting, and material: <required visual treatment>
Must preserve: <locked elements>
Must remove: <specific unwanted elements>
Do not change: <negative invariants>
Background: <transparent / solid / scene>
Output: <PNG/WebP/JPG, dimensions, variants>
Variation plan: <only when requested>
Negative constraints: <avoid list>
```

## Exact-Source Cutout Prompt

Use this wording as a base and fill in the requested shadow and crop policy:

```text
Use the uploaded image as the exact source asset. Isolate only the existing subject; do not redesign, redraw, regenerate, replace, or reinterpret it.
Preserve its exact shape, proportions, layout, numbers, text, icons, colors, gradients, highlights, strokes, decorations, and <preserve/remove> the existing natural shadow.
Delete only the outside canvas background. Keep all subject-internal white or pale details, including text, symbols, highlights, antialiased edges, and semi-transparent edge pixels.
Do not crop the subject, deform it, add light effects, texture, reflection, decoration, or a new background. Do not draw a checkerboard.
Output a PNG with a real transparent alpha background and keep a <N-pixel / described> safe margin around the full visible asset.
```

For a prompt-only request, return the prompt and any assumptions. Do not invoke image generation or claim that a generated output exists.
