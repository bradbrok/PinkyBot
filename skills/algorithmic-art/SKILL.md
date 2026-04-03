---
name: algorithmic-art
description: Creating algorithmic art using p5.js with seeded randomness and interactive parameter exploration. Use this when users request creating art using code, generative art, algorithmic art, flow fields, or particle systems. Create original algorithmic art rather than copying existing artists' work to avoid copyright violations.
---

Algorithmic philosophies are computational aesthetic movements expressed through code. Output .md files (philosophy), .html files (interactive viewer), and .js files (generative algorithms).

Steps:
1. Algorithmic Philosophy Creation (.md file)
2. Express by creating p5.js generative art (.html + .js files)

## ALGORITHMIC PHILOSOPHY CREATION

Create an ALGORITHMIC PHILOSOPHY interpreted through:
- Computational processes, emergent behavior, mathematical beauty
- Seeded randomness, noise fields, organic systems
- Particles, flows, fields, forces
- Parametric variation and controlled chaos

Philosophy must emphasize: Algorithmic expression. Emergent behavior. Computational beauty. Seeded variation.

### HOW TO GENERATE AN ALGORITHMIC PHILOSOPHY

**Name the movement** (1-2 words): "Organic Turbulence" / "Quantum Harmonics" / "Emergent Stillness"

**Articulate the philosophy** (4-6 paragraphs):
- Computational processes and mathematical relationships
- Noise functions and randomness patterns
- Particle behaviors and field dynamics
- Temporal evolution and system states
- Parametric variation and emergent complexity

**Guidelines:**
- Avoid redundancy — each aspect mentioned once
- Emphasize craftsmanship REPEATEDLY: "meticulously crafted algorithm," "product of deep computational expertise," "painstaking optimization," "master-level implementation"
- Leave creative space for implementation choices

### Philosophy Examples (condensed)
- "Organic Turbulence": Chaos constrained by natural law. Flow fields driven by layered Perlin noise. Thousands of particles following vector forces. Meticulously tuned balance refined through countless iterations.
- "Quantum Harmonics": Discrete entities exhibiting wave-like interference. Particles with phase values interfering constructively/destructively. Painstaking frequency calibration.
- "Recursive Whispers": Self-similarity across scales. Branching structures with golden ratios. Every branching angle the product of deep mathematical exploration.
- "Field Dynamics": Invisible forces made visible. Vector fields from mathematical functions. Particles tracing ghost-like evidence of invisible forces.
- "Stochastic Crystallization": Random processes crystallizing into ordered structures. Voronoi tessellation via relaxation algorithms. Master-level generative algorithm.

Output philosophy as a .md file.

## DEDUCING THE CONCEPTUAL SEED

Before implementing, identify the subtle conceptual thread from the original request. This is a subtle, niche reference embedded within the algorithm — not always literal, always sophisticated. The reference must be so refined it enhances depth without announcing itself. Think like a jazz musician quoting another song through algorithmic harmony.

## P5.JS IMPLEMENTATION

### STEP 0: READ THE TEMPLATE FIRST
1. Read `templates/viewer.html` using the Read tool
2. Use it as the LITERAL STARTING POINT
3. Keep all FIXED sections (header, sidebar, Anthropic branding, seed controls, action buttons)
4. Replace only VARIABLE sections (algorithm, parameters, UI controls)

### TECHNICAL REQUIREMENTS

Seeded Randomness:
```javascript
let seed = 12345;
randomSeed(seed);
noiseSeed(seed);
```

Parameters object — emerge naturally from philosophy. Consider: quantities, scales, probabilities, ratios, angles, thresholds.

Algorithm flows from the philosophy. Think: "how to express this philosophy through code?" not "which pattern should I use?"

Canvas: 1200x1200. Can be static (noLoop) or animated.

### CRAFTSMANSHIP REQUIREMENTS
- Balance: Complexity without visual noise, order without rigidity
- Color Harmony: Thoughtful palettes, not random RGB values
- Composition: Visual hierarchy and flow even in randomness
- Performance: Smooth execution
- Reproducibility: Same seed ALWAYS produces identical output

### OUTPUT FORMAT
1. Algorithmic Philosophy (.md)
2. Single self-contained HTML artifact built from `templates/viewer.html`

## INTERACTIVE ARTIFACT

**FIXED** (keep exactly as shown):
- Layout structure, Anthropic branding, seed section (display, prev/next/random/jump), actions (regenerate, reset, download)

**VARIABLE** (customize per artwork):
- p5.js algorithm, parameters object, parameter UI controls, optional color section

Every artwork has unique parameters and algorithm. Same seed ALWAYS produces identical output.

## RESOURCES
- `templates/viewer.html`: REQUIRED starting point for HTML artifacts
- `templates/generator_template.js`: Reference for p5.js best practices

The template is the STARTING POINT, not inspiration. Algorithm is where to create something unique. Keep the exact UI structure and Anthropic branding.