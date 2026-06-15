// Frontend security helpers — keep user/agent-controlled values from turning
// navigation into an open redirect or a javascript:/data: link.

// C0 control chars + DEL — stripped so they can't smuggle a scheme or CRLF.
const CONTROL_CHARS = /[\x00-\x1f\x7f]/g;

/**
 * Sanitize a `next` redirect target down to a SAME-ORIGIN app path.
 *
 * Accepts only a single-slash-rooted path ("/foo"). Rejects protocol-relative
 * URLs ("//evil.com" — browsers treat these as another origin), absolute URLs
 * with a scheme, backslash tricks ("/\\evil.com"), and embedded control chars.
 * Anything that doesn't qualify falls back to "/".
 *
 * Used for the `next` query param on Login/Setup so a crafted
 * `/login?next=https://evil.com` can't bounce a freshly-authenticated user
 * off-origin.
 */
export function sanitizeNextPath(next) {
    if (typeof next !== 'string') return '/';
    const v = next.replace(CONTROL_CHARS, '').trim();
    if (!v.startsWith('/')) return '/'; // must be a rooted path, not a scheme/relative
    if (v.startsWith('//')) return '/'; // protocol-relative -> another origin
    if (v.includes('\\')) return '/'; // backslash variants browsers normalize to //
    return v;
}

/**
 * Return `url` only if it's a safe external href (http/https/mailto), else null
 * so the caller can render plain text instead of a live link.
 *
 * Blocks dangerous schemes (javascript:, data:, vbscript:, ...) that can be
 * stored as agent/user-controlled URLs (repo links, KB sources, assets) and
 * later executed on click. `rel=noopener` does NOT stop `javascript:` — the
 * scheme has to be rejected. Other hosts are allowed on purpose: these are
 * genuinely external links.
 */
export function safeExternalHref(url) {
    if (typeof url !== 'string') return null;
    const v = url.replace(CONTROL_CHARS, '').trim();
    if (!v) return null;
    let parsed;
    try {
        parsed = new URL(v, window.location.origin);
    } catch {
        return null;
    }
    return ['http:', 'https:', 'mailto:'].includes(parsed.protocol) ? v : null;
}
