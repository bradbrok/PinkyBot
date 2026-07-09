"""Presentation routes: CRUD, versions, share-token viewer, templates,
generate-from-research, and the general /render/pdf endpoint.

Extracted from api.py. Owns its own HTML/cookie helpers (originally
defined inline in the route block) and a `_session_secret` helper for
HMAC cookie signing.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import html as _html
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pinky_daemon.api_models import (
    CreatePresentationRequest,
    CreateTemplateRequest,
    GeneratePresentationRequest,
    RestoreVersionRequest,
    SetPresentationPasswordRequest,
    UpdatePresentationRequest,
)

router = APIRouter(tags=["presentations"])


# ── Shared dependency state ───────────────────────────────────────────────────

_presentations: Any = None
_research: Any = None
_agents: Any = None
_broker: Any = None
_activity: Any = None


def set_dependencies(
    *,
    presentations,
    research,
    agents,
    broker,
    activity,
) -> None:
    """Wire shared instances for the presentations router."""
    global _presentations, _research, _agents, _broker, _activity
    _presentations = presentations
    _research = research
    _agents = agents
    _broker = broker
    _activity = activity


# ── Local helpers ─────────────────────────────────────────────────────────────


def _session_secret() -> str:
    return os.environ.get("PINKY_SESSION_SECRET", "").strip()


def _build_public_viewer(pres) -> str:
    """Standalone HTML viewer for a shared presentation."""
    escaped = _html.escape(pres.current_html, quote=True)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(pres.title)} — Pinky Presentations</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0a0a0a; color: #eee; font-family: system-ui, sans-serif; height: 100vh; display: flex; flex-direction: column; }}
  iframe {{ flex: 1; width: 100%; border: none; background: #fff; }}
</style>
</head>
<body>
<iframe srcdoc="{escaped}" sandbox="allow-scripts allow-same-origin" title="{_html.escape(pres.title)}"></iframe>
</body>
</html>"""


def _build_password_gate(pres, *, error: bool = False) -> str:
    """Standalone password gate page for protected presentations."""
    title = _html.escape(pres.title)
    token = _html.escape(pres.share_token)
    error_html = (
        '<p class="error">Incorrect password. Please try again.</p>' if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Protected</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #0d0d0f;
    color: #eee;
    font-family: 'Space Grotesk', system-ui, sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .card {{
    background: #141417;
    border: 1px solid #242428;
    border-radius: 12px;
    padding: 2.5rem 2rem;
    width: 100%;
    max-width: 380px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1.25rem;
  }}
  .lock {{ font-size: 2.5rem; line-height: 1; }}
  h1 {{
    font-size: 1.1rem;
    font-weight: 600;
    color: #f0f0f0;
    text-align: center;
    line-height: 1.4;
  }}
  .subtitle {{
    font-size: 0.8rem;
    color: #666;
    text-align: center;
  }}
  form {{ width: 100%; display: flex; flex-direction: column; gap: 0.75rem; }}
  input[type="password"] {{
    width: 100%;
    padding: 0.65rem 0.9rem;
    background: #0d0d0f;
    border: 1px solid #2a2a2e;
    border-radius: 7px;
    color: #eee;
    font-family: inherit;
    font-size: 0.95rem;
    outline: none;
    transition: border-color 0.15s;
  }}
  input[type="password"]:focus {{ border-color: #f7c56a; }}
  button {{
    width: 100%;
    padding: 0.65rem;
    background: #f7c56a;
    color: #0d0d0f;
    border: none;
    border-radius: 7px;
    font-family: inherit;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s;
  }}
  button:hover {{ opacity: 0.88; }}
  .error {{
    color: #e05c5c;
    font-size: 0.8rem;
    text-align: center;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="lock">🔒</div>
  <h1>{title}</h1>
  <p class="subtitle">This presentation is password protected.</p>
  {error_html}
  <form method="post" action="/p/{token}/unlock">
    <input type="password" name="password" placeholder="Enter password" autofocus required>
    <button type="submit">Unlock</button>
  </form>
</div>
</body>
</html>"""


def _make_pres_cookie_token(share_token: str, password: str, secret: str) -> str:
    """HMAC-signed token that proves the visitor supplied the correct password."""
    msg = f"{share_token}:{password}".encode()
    return _hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _verify_pres_cookie_token(share_token: str, password: str, secret: str, token: str) -> bool:
    expected = _make_pres_cookie_token(share_token, password, secret)
    return _hmac.compare_digest(expected, token)


def _build_generate_prompt(topic_detail: dict, instructions: str = "") -> str:
    topic = topic_detail.get("topic", {})
    briefs = topic_detail.get("briefs", [])
    brief = briefs[-1] if briefs else {}
    title = topic.get("title", "Research Findings")
    description = topic.get("description", "")
    content = brief.get("content", description)
    key_findings = brief.get("key_findings", [])
    topic_id = topic.get("id", 0)
    findings_md = "\n".join(f"- {f}" for f in key_findings) if key_findings else ""
    extra = f"\n\nAdditional instructions: {instructions}" if instructions else ""
    findings_section = f"Key findings:\n{findings_md}" if findings_md else ""
    content_section = f"Full brief content:\n{content[:3000]}" if content else ""
    return (
        f"Create a polished HTML presentation for the research topic: **{title}**\n\n"
        f"Description: {description}\n\n"
        f"{findings_section}\n\n"
        f"{content_section}"
        f"{extra}\n\n"
        f"Requirements:\n"
        f"- Fully self-contained HTML with inline CSS (no external dependencies except stable CDNs)\n"
        f"- Professional slide-style layout — not a scrolling article\n"
        f"- Multiple sections/slides with visual hierarchy\n"
        f"- Use reveal.js CDN or pure CSS if you want animations\n"
        f"- When done, call create_presentation(title='{title}', html_content=<your html>, "
        f"research_topic_id={topic_id}) to publish it.\n"
        f"- Then send the share URL to the owner via messaging."
    )


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/presentations")
async def create_presentation_endpoint(req: CreatePresentationRequest):
    pres = _presentations.create(
        title=req.title,
        html_content=req.html_content,
        description=req.description,
        created_by=req.created_by,
        tags=req.tags,
        research_topic_id=req.research_topic_id,
    )
    _activity.log(
        agent_name=req.created_by or "",
        event_type="presentation_created",
        title=f"Presentation created: {pres.title}",
        description=req.description or "",
        metadata={"presentation_id": pres.id, "research_topic_id": req.research_topic_id},
    )
    return pres.to_dict(include_html=False)


@router.get("/presentations")
async def list_presentations_endpoint(
    tag: str = "",
    created_by: str = "",
    research_topic_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
):
    items = _presentations.list(
        tag=tag, created_by=created_by,
        research_topic_id=research_topic_id,
        limit=limit, offset=offset,
    )
    return {"presentations": [p.to_dict() for p in items], "count": len(items)}


@router.get("/presentations/stats")
async def presentation_stats():
    return _presentations.get_stats()


@router.get("/presentations/{presentation_id}")
async def get_presentation_endpoint(presentation_id: int):
    pres = _presentations.get_with_content(presentation_id)
    if not pres:
        raise HTTPException(404, "Presentation not found")
    return pres.to_dict(include_html=True)


@router.put("/presentations/{presentation_id}")
async def update_presentation_endpoint(presentation_id: int, req: UpdatePresentationRequest):
    pres = _presentations.update(
        presentation_id,
        req.html_content,
        description=req.description,
        created_by=req.created_by,
        title=req.title,
        tags=req.tags,
    )
    if not pres:
        raise HTTPException(404, "Presentation not found")
    return pres.to_dict(include_html=False)


@router.delete("/presentations/{presentation_id}")
async def delete_presentation_endpoint(presentation_id: int):
    deleted = _presentations.delete(presentation_id)
    if not deleted:
        raise HTTPException(404, "Presentation not found")
    return {"deleted": True}


@router.get("/presentations/{presentation_id}/versions")
async def list_presentation_versions(presentation_id: int):
    pres = _presentations.get(presentation_id)
    if not pres:
        raise HTTPException(404, "Presentation not found")
    versions = _presentations.get_versions(presentation_id)
    return {
        "versions": [v.to_dict(include_html=False) for v in versions],
        "count": len(versions),
        "current_version": pres.current_version,
    }


@router.get("/presentations/{presentation_id}/versions/{version}")
async def get_presentation_version(presentation_id: int, version: int):
    ver = _presentations.get_version(presentation_id, version)
    if not ver:
        raise HTTPException(404, "Version not found")
    return ver.to_dict(include_html=True)


@router.post("/presentations/{presentation_id}/restore")
async def restore_presentation_version(presentation_id: int, req: RestoreVersionRequest):
    pres = _presentations.restore_version(presentation_id, req.version)
    if not pres:
        raise HTTPException(404, "Presentation or version not found")
    return pres.to_dict(include_html=False)


@router.get("/presentations/{presentation_id}/share-link")
async def get_share_link(presentation_id: int, request: Request):
    pres = _presentations.get(presentation_id)
    if not pres:
        raise HTTPException(404, "Presentation not found")
    base = str(request.base_url).rstrip("/")
    return {"url": f"{base}/p/{pres.share_token}", "share_token": pres.share_token}


@router.put("/presentations/{presentation_id}/password")
async def set_presentation_password(presentation_id: int, req: SetPresentationPasswordRequest):
    """Set or remove password protection for a presentation.

    NOTE: password stored plaintext for MVP — upgrade to bcrypt if needed.
    """
    pres = _presentations.get(presentation_id)
    if not pres:
        raise HTTPException(404, "Presentation not found")
    ok = _presentations.set_password(presentation_id, req.password)
    if not ok:
        raise HTTPException(500, "Failed to update password")
    return {"protected": bool(req.password)}


@router.post("/presentations/generate")
async def generate_presentation_from_research(req: GeneratePresentationRequest):
    topic_detail = _research.get_topic_detail(req.topic_id)
    if not topic_detail:
        raise HTTPException(404, "Research topic not found")
    prompt = _build_generate_prompt(topic_detail, req.instructions)
    agent_cfg = _agents.get_agent(req.agent_name)
    if not agent_cfg:
        raise HTTPException(404, f"Agent '{req.agent_name}' not found")
    # Route through the broker to the agent's active session. The InjectResult
    # is deliberately discarded: this endpoint reports queued unconditionally
    # (pre-existing semantics) and has no durable comms copy to retire.
    await _broker.inject_agent_message("system", req.agent_name, prompt)
    return {"queued": True, "agent": req.agent_name, "topic_id": req.topic_id}


@router.get("/p/{share_token}", response_class=HTMLResponse)
async def public_presentation_view(share_token: str, request: Request, error: int = 0):
    pres = _presentations.get_by_share_token_with_content(share_token)
    if not pres:
        raise HTTPException(404, "Presentation not found")
    if pres.access_password:
        # Check for valid unlock cookie
        secret = _session_secret()
        cookie_name = f"pres_token_{share_token}"
        cookie_val = request.cookies.get(cookie_name, "")
        if not secret or not (
            cookie_val and _verify_pres_cookie_token(share_token, pres.access_password, secret, cookie_val)
        ):
            return HTMLResponse(_build_password_gate(pres, error=bool(error)), status_code=200)
    return HTMLResponse(_build_public_viewer(pres))


@router.post("/p/{share_token}/unlock")
async def unlock_presentation(share_token: str, request: Request):
    """Validate password and set a signed cookie granting access."""
    pres = _presentations.get_by_share_token(share_token)
    if not pres:
        raise HTTPException(404, "Presentation not found")

    form = await request.form()
    supplied = form.get("password", "")

    if not pres.access_password or _presentations.check_password(pres.id, supplied):
        # Correct password — issue cookie and redirect to viewer
        secret = _session_secret()
        response = RedirectResponse(url=f"/p/{share_token}", status_code=303)
        if secret and pres.access_password:
            cookie_val = _make_pres_cookie_token(share_token, pres.access_password, secret)
            response.set_cookie(
                key=f"pres_token_{share_token}",
                value=cookie_val,
                max_age=86400,  # 24 h
                httponly=True,
                samesite="lax",
            )
        return response
    # Wrong password — redirect back with error flag
    return RedirectResponse(url=f"/p/{share_token}?error=1", status_code=303)


# ── Presentation Templates ────────────────────────────────────────────────────


@router.get("/presentation-templates")
async def list_presentation_templates(tag: str = ""):
    items = _presentations.list_templates(tag=tag)
    return {
        "templates": [t.to_dict(include_html=False) for t in items],
        "count": len(items),
    }


@router.get("/presentation-templates/{template_id}")
async def get_presentation_template_endpoint(template_id: int):
    tmpl = _presentations.get_template(template_id)
    if not tmpl:
        raise HTTPException(404, "Template not found")
    return tmpl.to_dict(include_html=True)


@router.post("/presentation-templates")
async def create_presentation_template(req: CreateTemplateRequest):
    tmpl = _presentations.create_template(
        name=req.name,
        html_content=req.html_content,
        description=req.description,
        tags=req.tags,
        thumbnail_css=req.thumbnail_css,
    )
    return tmpl.to_dict(include_html=False)


@router.delete("/presentation-templates/{template_id}")
async def delete_presentation_template(template_id: int):
    deleted = _presentations.delete_template(template_id)
    if not deleted:
        raise HTTPException(404, "Template not found or is a built-in template")
    return {"deleted": True}


# ── General PDF Rendering ─────────────────────────────────────────────────────


@router.post("/render/pdf")
async def render_pdf(req: dict):
    """Render markdown content as a PDF file.

    Returns the file path for use with pinky-messaging's send_document.
    """
    content = req.get("content", "").strip()
    filename = req.get("filename", "document.pdf")
    title = req.get("title", "Document")
    if not content:
        raise HTTPException(400, "content is required")

    from pinky_daemon.research_export import _HTML_TEMPLATE, EXPORT_DIR, _markdown_to_html

    html_body = _markdown_to_html(content)
    html_str = _HTML_TEMPLATE.format(title=title, body=html_body)

    os.makedirs(EXPORT_DIR, exist_ok=True)
    if not filename.endswith(".pdf"):
        filename += ".pdf"
    path = os.path.join(EXPORT_DIR, filename)

    try:
        from weasyprint import HTML as WP_HTML
        WP_HTML(string=html_str).write_pdf(path)
    except ImportError:
        raise HTTPException(503, "WeasyPrint not installed — cannot render PDFs") from None
    except Exception as e:
        raise HTTPException(500, f"PDF rendering failed: {e}") from e

    return {"success": True, "path": os.path.abspath(path), "filename": filename}
