"""App management + public app viewer routes.

Extracted from api.py. Owns its own CSP / cookie helpers (they were
defined inline in the original block and only used here).
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from pinky_daemon.api_models import (
    CreateAppRequest,
    DeployAppRequest,
    SetAppPasswordRequest,
    UpdateAppRequest,
)

router = APIRouter(tags=["apps"])


# ── Shared dependency state ───────────────────────────────────────────────────

_app_store: Any = None


def set_dependencies(*, app_store) -> None:
    """Wire shared instances for the apps router."""
    global _app_store
    _app_store = app_store


# ── Local helpers ─────────────────────────────────────────────────────────────

_APP_CSP = (
    "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline' https:; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
    "connect-src 'self' https:; "
    "frame-src 'none'; "
    "object-src 'none'"
)


def _session_secret() -> str:
    return os.environ.get("PINKY_SESSION_SECRET", "").strip()


def _app_csp_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": _APP_CSP,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def _build_app_password_gate(found, *, error: bool = False) -> str:
    """Minimal password form for protected apps."""
    err_html = (
        '<p style="color:#e74c3c;margin-bottom:12px">'
        "Wrong password</p>"
        if error
        else ""
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{found.name} — Protected</title>
<style>
body{{font-family:system-ui;display:flex;justify-content:center;
align-items:center;min-height:100vh;margin:0;background:#111;color:#eee}}
form{{background:#1a1a1a;padding:2rem;border-radius:8px;
max-width:320px;width:100%}}
input{{width:100%;padding:8px;margin:8px 0;box-sizing:border-box;
background:#222;border:1px solid #444;color:#eee;border-radius:4px}}
button{{width:100%;padding:10px;background:#4a9eff;color:#fff;
border:none;border-radius:4px;cursor:pointer;font-size:1rem}}
button:hover{{background:#3a8eef}}
</style></head><body>
<form method="POST">
<h2 style="margin-top:0">{found.name}</h2>
<p style="color:#888">This app is password-protected.</p>
{err_html}
<input type="password" name="password" placeholder="Password"
 autofocus required>
<button type="submit">Unlock</button>
</form></body></html>"""


def _app_cookie_name(share_token: str) -> str:
    return f"app_access_{share_token[:8]}"


def _make_app_cookie_token(share_token: str, pwd_hash: str, secret: str) -> str:
    return hmac.new(
        secret.encode(), f"{share_token}:{pwd_hash}".encode(), "sha256"
    ).hexdigest()


def _verify_app_cookie_token(
    share_token: str, pwd_hash: str, secret: str, token: str
) -> bool:
    expected = _make_app_cookie_token(share_token, pwd_hash, secret)
    return hmac.compare_digest(expected, token)


# ── App CRUD ──────────────────────────────────────────────────────────────────


@router.post("/apps")
async def create_app(req: CreateAppRequest):
    new_app = _app_store.create(
        name=req.name,
        description=req.description,
        app_type=req.app_type,
        created_by=req.created_by,
        tags=req.tags,
        html_content=req.html_content,
    )
    return new_app.to_dict(include_html=False)


@router.get("/apps")
async def list_apps(
    status: str = "",
    created_by: str = "",
    tag: str = "",
    limit: int = 100,
    offset: int = 0,
):
    items = _app_store.list(
        status=status, created_by=created_by, tag=tag, limit=limit, offset=offset
    )
    return {"apps": [a.to_dict() for a in items], "count": len(items)}


@router.get("/apps/stats")
async def app_stats():
    return _app_store.get_stats()


@router.get("/apps/{app_id}")
async def get_app(app_id: int):
    found = _app_store.get(app_id)
    if not found:
        raise HTTPException(404, "App not found")
    return found.to_dict(include_html=True)


@router.put("/apps/{app_id}")
async def update_app(app_id: int, req: UpdateAppRequest):
    updated = _app_store.update(
        app_id,
        name=req.name,
        description=req.description,
        app_type=req.app_type,
        tags=req.tags,
        status=req.status,
    )
    if not updated:
        raise HTTPException(404, "App not found")
    return updated.to_dict(include_html=False)


@router.delete("/apps/{app_id}")
async def delete_app(app_id: int):
    deleted = _app_store.delete(app_id)
    if not deleted:
        raise HTTPException(404, "App not found")
    return {"deleted": True}


@router.post("/apps/{app_id}/deploy")
async def deploy_app(app_id: int, req: DeployAppRequest):
    deployed = _app_store.deploy(app_id, req.html_content)
    if not deployed:
        raise HTTPException(404, "App not found")
    return deployed.to_dict(include_html=False)


@router.post("/apps/{app_id}/share")
async def regenerate_app_share(app_id: int, request: Request):
    updated = _app_store.regenerate_share_token(app_id)
    if not updated:
        raise HTTPException(404, "App not found")
    base = str(request.base_url).rstrip("/")
    return {
        "share_token": updated.share_token,
        "url": f"{base}/a/{updated.share_token}",
    }


@router.get("/apps/{app_id}/status")
async def app_health(app_id: int):
    health = _app_store.check_health(app_id)
    if not health.get("ok") and health.get("error") == "App not found":
        raise HTTPException(404, "App not found")
    return health


@router.get("/apps/{app_id}/share-link")
async def get_app_share_link(app_id: int, request: Request):
    found = _app_store.get(app_id)
    if not found:
        raise HTTPException(404, "App not found")
    base = str(request.base_url).rstrip("/")
    return {"url": f"{base}/a/{found.share_token}", "share_token": found.share_token}


@router.put("/apps/{app_id}/password")
async def set_app_password(app_id: int, req: SetAppPasswordRequest):
    """Set or remove password protection for an app."""
    ok = _app_store.set_password(app_id, req.password)
    if not ok:
        raise HTTPException(404, "App not found")
    return {"protected": bool(req.password)}


@router.post("/apps/{app_id}/upload")
async def upload_app_file(app_id: int, file: UploadFile):
    """Upload a static file to an app's directory."""
    found = _app_store.get(app_id)
    if not found:
        raise HTTPException(404, "App not found")
    static_dir = _app_store.ensure_static_dir(app_id)
    filename = file.filename or "upload"
    # Sanitize — no path traversal
    safe_name = Path(filename).name
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(400, "Invalid filename")
    dest = static_dir / safe_name
    content = await file.read()
    dest.write_bytes(content)
    return {"uploaded": safe_name, "size": len(content)}


@router.get("/apps/{app_id}/files/{file_path:path}")
async def serve_app_file(app_id: int, file_path: str):
    """Serve a static file from an app's directory."""
    found = _app_store.get(app_id)
    if not found:
        raise HTTPException(404, "App not found")
    static_dir = _app_store.get_static_dir(app_id)
    target = (static_dir / file_path).resolve()
    if not target.is_relative_to(static_dir.resolve()):
        raise HTTPException(403, "Forbidden")
    if not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target, headers=_app_csp_headers())


# ── Public app viewer ─────────────────────────────────────────────────────────


@router.get("/a/{share_token}", response_class=HTMLResponse)
async def public_app_viewer(share_token: str, request: Request, error: int = 0):
    found = _app_store.get_by_share_token(share_token)
    if not found or found.status != "deployed":
        raise HTTPException(404, "App not found")
    # Password gate
    if found.access_password:
        secret = _session_secret()
        cookie_name = _app_cookie_name(share_token)
        cookie_val = request.cookies.get(cookie_name, "")
        if not (
            cookie_val
            and _verify_app_cookie_token(
                share_token, found.access_password, secret, cookie_val
            )
        ):
            return HTMLResponse(
                _build_app_password_gate(found, error=bool(error)),
                status_code=200,
                headers=_app_csp_headers(),
            )
    # Serve DB content or static index.html
    if found.html_content.strip():
        return HTMLResponse(
            content=found.html_content,
            headers=_app_csp_headers(),
        )
    # Fallback: serve index.html from static dir
    static_dir = _app_store.get_static_dir(found.id)
    index = static_dir / "index.html"
    if index.is_file():
        return HTMLResponse(
            content=index.read_text(),
            headers=_app_csp_headers(),
        )
    raise HTTPException(404, "App has no content")


@router.post("/a/{share_token}")
async def unlock_app(share_token: str, request: Request):
    """Validate password and set a cookie granting access."""
    found = _app_store.get_by_share_token(share_token)
    if not found:
        raise HTTPException(404, "App not found")
    form = await request.form()
    supplied = str(form.get("password", ""))
    if _app_store.check_password(found.id, supplied):
        secret = _session_secret()
        response = RedirectResponse(
            url=f"/a/{found.share_token}", status_code=303
        )
        if secret and found.access_password:
            cookie_val = _make_app_cookie_token(
                found.share_token, found.access_password, secret
            )
            response.set_cookie(
                _app_cookie_name(found.share_token),
                cookie_val,
                httponly=True,
                samesite="strict",
                max_age=86400,
            )
        return response
    return RedirectResponse(
        url=f"/a/{found.share_token}?error=1", status_code=303
    )


@router.get("/a/{share_token}/{file_path:path}")
async def public_app_static_file(
    share_token: str, file_path: str, request: Request
):
    """Serve static files from a public app by share token."""
    found = _app_store.get_by_share_token(share_token)
    if not found or found.status != "deployed":
        raise HTTPException(404, "App not found")
    # Password check for static files too
    if found.access_password:
        secret = _session_secret()
        cookie_name = _app_cookie_name(share_token)
        cookie_val = request.cookies.get(cookie_name, "")
        if not (
            cookie_val
            and _verify_app_cookie_token(
                share_token,
                found.access_password,
                secret,
                cookie_val,
            )
        ):
            raise HTTPException(403, "Password required")
    static_dir = _app_store.get_static_dir(found.id)
    target = (static_dir / file_path).resolve()
    if not target.is_relative_to(static_dir.resolve()):
        raise HTTPException(403, "Forbidden")
    if not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target, headers=_app_csp_headers())
