// Shared force-graph engine for the KG (Memories) and KB graph views.
//
// Design goals (Brad, 2026-06-10, "make it look like the flavor network"):
//  - tight organic clusters: collision resolution + per-cluster gravity
//  - no corner-flinging: capped forces/velocities, no hard wall clamp
//  - quiet edges: callers render curved, cluster-tinted paths via edgePath()
//  - labels that earn their space: greedy rect declutter via computeLabelSet()
//  - auto-zoom: fitView() computes the transform framing the settled layout
//
// The sim mutates node objects in place ({x, y, vx, vy, r}) — both pages rely
// on persistent node identity so positions survive filter changes.

const FORCE_CAP = 6;      // max per-pair force magnitude per tick
const VELOCITY_CAP = 10;  // max node speed per tick — kills runaway flings

/**
 * Start a force simulation. Returns { stop() }.
 *
 * opts:
 *   nodes          [{id, x, y, vx, vy, r, ...}]  mutated in place
 *   edges          [{source, target, weight?}]   ids into nodeById
 *   nodeById       Map(id -> node)
 *   clusterCenter  (node) => {x, y} | null       per-node gravity anchor
 *   gravity        cluster gravity strength      (default 0.03)
 *   repulsion      repulsion constant            (default 1100)
 *   linkBase       base link rest length         (default 70)
 *   maxIter        ticks before settle           (default 260)
 *   speed          sim iterations per RAF frame  (default 3 — fast settle)
 *   skip           (node) => bool                true = pinned (dragged) node
 *   onTick         (iteration) => void           reassign state for reactivity
 *   onSettle       () => void                    layout done — fit the view
 */
export function createForceSim(opts) {
    const {
        nodes, edges, nodeById,
        clusterCenter = null,
        gravity = 0.03,
        repulsion = 1100,
        linkBase = 70,
        maxIter = 260,
        speed = 3,
        skip = () => false,
        onTick = () => {},
        onSettle = () => {},
    } = opts;

    let raf = null;
    let iteration = 0;
    let stopped = false;

    function frame() {
        if (stopped) return;
        for (let s = 0; s < speed && iteration < maxIter; s++) step();
        if (iteration >= maxIter) { raf = null; onSettle(); return; }
        onTick(iteration);
        raf = requestAnimationFrame(frame);
    }

    function step() {
        iteration++;
        const decay = 1 - iteration / maxIter;
        const len = nodes.length;

        // Pairwise repulsion (O(n^2) on the filtered subset — small in practice)
        for (let i = 0; i < len; i++) {
            for (let j = i + 1; j < len; j++) {
                const a = nodes[i], b = nodes[j];
                const dx = b.x - a.x, dy = b.y - a.y;
                const dist = Math.sqrt(dx * dx + dy * dy) || 1;
                let force = (repulsion / (dist * dist)) * decay;
                if (force > FORCE_CAP) force = FORCE_CAP;
                const fx = (dx / dist) * force, fy = (dy / dist) * force;
                a.vx -= fx; a.vy -= fy;
                b.vx += fx; b.vy += fy;
            }
        }

        // Spring attraction along edges; rest length grows with node radii so
        // hubs keep breathing room, and weight tightens repeated relations.
        for (const e of edges) {
            const a = nodeById.get(e.source), b = nodeById.get(e.target);
            if (!a || !b) continue;
            const dx = b.x - a.x, dy = b.y - a.y;
            const dist = Math.sqrt(dx * dx + dy * dy) || 1;
            const rest = linkBase + a.r + b.r;
            const w = Math.min(3, e.weight || 1);
            let force = (dist - rest) * 0.008 * w * decay;
            if (force > FORCE_CAP) force = FORCE_CAP;
            else if (force < -FORCE_CAP) force = -FORCE_CAP;
            const fx = (dx / dist) * force, fy = (dy / dist) * force;
            a.vx += fx; a.vy += fy;
            b.vx -= fx; b.vy -= fy;
        }

        // Cluster gravity + integration. No hard wall clamp: walls + strong
        // gravity were what pinned runaway nodes into the corners; the view
        // is framed by fitView() instead.
        for (const n of nodes) {
            if (skip(n)) { n.vx = 0; n.vy = 0; continue; }
            const c = clusterCenter ? clusterCenter(n) : null;
            if (c) {
                n.vx += (c.x - n.x) * gravity * decay;
                n.vy += (c.y - n.y) * gravity * decay;
            }
            n.vx *= 0.85; n.vy *= 0.85;
            const spd = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
            if (spd > VELOCITY_CAP) {
                n.vx = (n.vx / spd) * VELOCITY_CAP;
                n.vy = (n.vy / spd) * VELOCITY_CAP;
            }
            n.x += n.vx; n.y += n.vy;
        }

        // Positional collision resolution — two passes separate overlapping
        // circles directly, which is what keeps dense clusters readable.
        for (let pass = 0; pass < 2; pass++) {
            for (let i = 0; i < len; i++) {
                for (let j = i + 1; j < len; j++) {
                    const a = nodes[i], b = nodes[j];
                    const dx = b.x - a.x, dy = b.y - a.y;
                    const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
                    const minDist = a.r + b.r + 3;
                    if (dist < minDist) {
                        const push = (minDist - dist) / 2;
                        const ux = dx / dist, uy = dy / dist;
                        const aPinned = skip(a), bPinned = skip(b);
                        if (!aPinned) { a.x -= ux * push * (bPinned ? 2 : 1); a.y -= uy * push * (bPinned ? 2 : 1); }
                        if (!bPinned) { b.x += ux * push * (aPinned ? 2 : 1); b.y += uy * push * (aPinned ? 2 : 1); }
                    }
                }
            }
        }

    }

    raf = requestAnimationFrame(frame);
    return {
        stop() {
            stopped = true;
            if (raf) { cancelAnimationFrame(raf); raf = null; }
        },
    };
}

/**
 * Compute the {zoom, panX, panY} that frames all nodes in a viewW×viewH
 * viewBox with `pad` graph-units of breathing room. The transform contract is
 * translate(panX panY) scale(zoom) — matching both pages' <g> wrapper.
 */
export function fitView(nodes, viewW, viewH, pad = 70, minZoom = 0.25, maxZoom = 1.6) {
    if (!nodes.length) return { zoom: 1, panX: 0, panY: 0 };
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
        if (n.x - n.r < minX) minX = n.x - n.r;
        if (n.x + n.r > maxX) maxX = n.x + n.r;
        if (n.y - n.r < minY) minY = n.y - n.r;
        if (n.y + n.r > maxY) maxY = n.y + n.r;
    }
    minX -= pad; minY -= pad; maxX += pad; maxY += pad;
    const w = Math.max(1, maxX - minX), h = Math.max(1, maxY - minY);
    const zoom = Math.max(minZoom, Math.min(maxZoom, Math.min(viewW / w, viewH / h)));
    return {
        zoom,
        panX: (viewW - w * zoom) / 2 - minX * zoom,
        panY: (viewH - h * zoom) / 2 - minY * zoom,
    };
}

/**
 * Smoothly animate a view-state object toward a target transform.
 * `apply` receives {zoom, panX, panY} every frame — assign to page state.
 * Returns a cancel function.
 */
export function animateView(from, to, apply, duration = 380) {
    const start = performance.now();
    let raf = null;
    const ease = (t) => 1 - Math.pow(1 - t, 3); // easeOutCubic
    function frame(now) {
        const t = Math.min(1, (now - start) / duration);
        const k = ease(t);
        apply({
            zoom: from.zoom + (to.zoom - from.zoom) * k,
            panX: from.panX + (to.panX - from.panX) * k,
            panY: from.panY + (to.panY - from.panY) * k,
        });
        if (t < 1) raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);
    return () => { if (raf) cancelAnimationFrame(raf); };
}

/**
 * Greedy label declutter: walk nodes by degree (hubs first) and keep a label
 * only if its estimated text rect doesn't collide with an already-placed one.
 * Font size is in graph units (labels live inside the zoomed <g>), so the
 * result is zoom-independent and only needs recomputing per layout/filter.
 * Returns a Set of node ids whose labels should render.
 */
export function computeLabelSet(nodes, { fontSize = 11, charW = 6.6, maxLabels = 80 } = {}) {
    const placed = [];
    const visible = new Set();
    const sorted = [...nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0) || b.r - a.r);
    for (const n of sorted) {
        if (visible.size >= maxLabels) break;
        const label = n.label || '';
        const w = Math.min(22, label.length) * charW;
        const h = fontSize + 4;
        // label rect sits centered below the node (matches both renderers)
        const rect = { x: n.x - w / 2, y: n.y + n.r + 3, w, h };
        let collides = false;
        for (const p of placed) {
            if (rect.x < p.x + p.w && rect.x + rect.w > p.x &&
                rect.y < p.y + p.h && rect.y + rect.h > p.y) { collides = true; break; }
        }
        if (!collides) { placed.push(rect); visible.add(n.id); }
    }
    return visible;
}

/**
 * Slightly-curved quadratic path between two nodes — the organic look from
 * the flavor-network reference. Curvature is a fraction of edge length
 * applied perpendicular to the midpoint.
 */
export function edgePath(src, tgt, curvature = 0.12) {
    const dx = tgt.x - src.x, dy = tgt.y - src.y;
    const mx = (src.x + tgt.x) / 2, my = (src.y + tgt.y) / 2;
    const cx = mx - dy * curvature, cy = my + dx * curvature;
    return `M ${src.x} ${src.y} Q ${cx} ${cy} ${tgt.x} ${tgt.y}`;
}

/** Cursor-anchored wheel zoom: returns the next {zoom, panX, panY}. */
export function zoomAt(state, vbX, vbY, factor, minZoom = 0.2, maxZoom = 4) {
    const gx = (vbX - state.panX) / state.zoom;
    const gy = (vbY - state.panY) / state.zoom;
    const zoom = Math.max(minZoom, Math.min(maxZoom, state.zoom * factor));
    return { zoom, panX: vbX - gx * zoom, panY: vbY - gy * zoom };
}

/** '#rrggbb' -> 'rgba(r,g,b,a)' — for cluster-tinted edges. */
export function hexToRgba(hex, alpha) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return `rgba(255,255,255,${alpha})`;
    const v = parseInt(m[1], 16);
    return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${alpha})`;
}
