const API = window.location.origin;

function authRedirectTarget(payload) {
    const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    const path = payload && payload.setup_required ? '/setup' : '/login';
    return `${path}?next=${encodeURIComponent(next || '/')}`;
}

export async function api(method, path, body, { keepalive = false } = {}) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        keepalive,
    };
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(`${API}${path}`, opts);
    if (!resp.ok) {
        const contentType = resp.headers.get('content-type') || '';
        const raw = await resp.text();
        let payload = null;
        if (contentType.includes('application/json')) {
            try {
                payload = JSON.parse(raw);
            } catch {
                payload = null;
            }
        }
        if (
            resp.status === 401 &&
            payload &&
            typeof payload.setup_required === 'boolean' &&
            !['/login', '/setup', '/landing'].includes(window.location.pathname)
        ) {
            window.location.href = authRedirectTarget(payload);
        }
        const detail = payload ? (payload.detail || JSON.stringify(payload)) : raw;
        throw new Error(`${resp.status}: ${detail}`);
    }
    return resp.json();
}

export function sse(path) {
    return new EventSource(`${API}${path}`);
}
