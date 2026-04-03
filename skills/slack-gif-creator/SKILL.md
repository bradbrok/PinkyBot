---
name: slack-gif-creator
description: Knowledge and utilities for creating animated GIFs optimized for Slack. Provides constraints, validation tools, and animation concepts. Use when users request animated GIFs for Slack like 'make me a GIF of X doing Y for Slack.'
---

# Slack GIF Creator

## Slack Requirements
- Emoji GIFs: 128x128 (recommended), Message GIFs: 480x480
- FPS: 10-30 (lower = smaller file), Colors: 48-128, Duration: <3s for emoji

## Core Workflow
```python
from core.gif_builder import GIFBuilder
from PIL import Image, ImageDraw

builder = GIFBuilder(width=128, height=128, fps=10)
for i in range(12):
    frame = Image.new('RGB', (128, 128), (240, 248, 255))
    draw = ImageDraw.Draw(frame)
    # Draw animation using PIL primitives
    builder.add_frame(frame)
builder.save('output.gif', num_colors=48, optimize_for_emoji=True)
```

## Drawing Graphics

Use PIL ImageDraw primitives:
```python
draw.ellipse([x1, y1, x2, y2], fill=(r,g,b), outline=(r,g,b), width=3)
draw.polygon(points, fill=(r,g,b), outline=(r,g,b), width=3)
draw.line([(x1,y1),(x2,y2)], fill=(r,g,b), width=5)
draw.rectangle([x1,y1,x2,y2], fill=(r,g,b), outline=(r,g,b), width=3)
```

**Don't use**: Emoji fonts (unreliable), pre-packaged graphics.

## Making Graphics Look Good
- **Thick lines**: Always `width=2` or higher
- **Visual depth**: Gradients, layered shapes
- **Interesting shapes**: Highlights, rings, patterns
- **Vibrant colors**: Complementary, good contrast
- **Be creative**: Combine concepts (bouncing + rotating, pulsing + sliding)

## Available Utilities
- `core.gif_builder.GIFBuilder` - assembles frames, optimizes
- `core.validators.validate_gif` - check Slack requirements
- `core.easing.interpolate` - smooth motion (ease_in, ease_out, bounce_out, elastic_out)
- `core.frame_composer` - helpers: gradients, circles, text, stars

## Animation Concepts
- **Shake**: `math.sin()` with frame index for position offset
- **Pulse**: `math.sin(t * freq * 2 * math.pi)` for size rhythm
- **Bounce**: `easing='bounce_out'` for landing, `easing='ease_in'` for falling
- **Spin**: `image.rotate(angle, resample=Image.BICUBIC)`
- **Fade**: adjust alpha channel or use `Image.blend()`
- **Slide**: start off-screen, `easing='ease_out'` to position
- **Particles**: radiate outward with velocity + gravity

## Optimization (when asked for smaller file)
1. Lower FPS (10 instead of 20)
2. Fewer colors (`num_colors=48`)
3. Smaller dimensions
4. `remove_duplicates=True`
5. `optimize_for_emoji=True`

## Dependencies
`pip install pillow imageio numpy`