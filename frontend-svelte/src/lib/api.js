const API = window.location.origin;

function authRedirectTarget(payload) {
    const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    const path = payload && payload.setup_required ? '/setup' : '/login';
    return `${path}?next=${encodeURIComponent(next || '/')}`;
}

export async function api(method, path, body) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
    };
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(`${API}${path}`, opts);
    if (!resp.ok) {
        const contentType = resp.headers.get('content-type') || '';
        let payload = null;
        if (contentType.includes('application/json')) {
            try {
                payload = await resp.json();
            } catch {
                payload = null;
            }
        }
        if (
            resp.status === 401 &&
            payload &&
            typeof payload.setup_required === 'boolean' &&
            !['/login', '/setup'].includes(window.location.pathname)
        ) {
            window.location.href = authRedirectTarget(payload);
        }
        const detail = payload ? (payload.detail || JSON.stringify(payload)) : await resp.text();
        throw new Error(`${resp.status}: ${detail}`);
    }
    return resp.json();
}

export function sse(path) {
    return new EventSource(`${API}${path}`);
}
