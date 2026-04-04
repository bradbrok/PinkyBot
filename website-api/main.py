import asyncio
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from google_auth_oauthlib.flow import Flow

# ── Config ────────────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REDIRECT_URI = "https://pinkybot.ai/oauth/google/callback"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
DB_PATH = "/var/lib/pinkybot-oauth/sessions.db"

# ── DB ────────────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT PRIMARY KEY,
            access_token  TEXT,
            refresh_token TEXT,
            expiry        TEXT,
            created_at    REAL,
            used          INTEGER DEFAULT 0
        )
    """)
    con.commit()
    return con


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _build_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uris": [REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)


# ── Background cleanup ────────────────────────────────────────────────────────

async def _cleanup_loop():
    while True:
        await asyncio.sleep(60)
        try:
            con = _get_db()
            cutoff = datetime.now(timezone.utc).timestamp() - 300  # 5 min
            con.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
            con.commit()
            con.close()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure DB exists on startup
    con = _get_db()
    con.close()
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/oauth/google/start")
async def google_start(session: str):
    """Validate session UUID, build Google auth URL, redirect."""
    if not _valid_uuid(session):
        raise HTTPException(400, "Invalid session ID")

    flow = _build_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=session,
    )
    return RedirectResponse(auth_url)


@app.get("/oauth/google/callback")
async def google_callback(code: str, state: str):
    """Exchange code for tokens, store in DB, postMessage to opener."""
    if not _valid_uuid(state):
        raise HTTPException(400, "Invalid state parameter")

    try:
        flow = _build_flow()
        flow.fetch_token(code=code)
        creds = flow.credentials

        access_token = creds.token
        refresh_token = creds.refresh_token
        expiry = creds.expiry.isoformat() if creds.expiry else None

        con = _get_db()
        con.execute(
            """
            INSERT OR REPLACE INTO sessions
                (session_id, access_token, refresh_token, expiry, created_at, used)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (state, access_token, refresh_token, expiry,
             datetime.now(timezone.utc).timestamp()),
        )
        con.commit()
        con.close()
    except Exception as e:
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html><head><title>OAuth Error</title></head>
<body style="font-family:sans-serif;text-align:center;padding:2rem">
<h3>OAuth error: {e}</h3>
<script>window.close();</script>
</body></html>""",
            status_code=400,
        )

    return HTMLResponse(
        f"""<!DOCTYPE html>
<html><head><title>Connected</title></head>
<body style="font-family:sans-serif;text-align:center;padding:2rem">
<p>Connected! You can close this window.</p>
<script>
(function() {{
  try {{
    window.opener.postMessage({{type: 'pinkybot-oauth', session: '{state}'}}, '*');
  }} catch(e) {{}}
  window.close();
}})();
</script>
</body></html>"""
    )


@app.get("/oauth/google/token")
async def google_token(session: str):
    """One-time token retrieval. Returns tokens then marks session as used."""
    if not _valid_uuid(session):
        raise HTTPException(400, "Invalid session ID")

    con = _get_db()
    row = con.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session,)
    ).fetchone()

    if not row or row["used"]:
        con.close()
        raise HTTPException(404, detail="session not found or expired")

    con.execute(
        "UPDATE sessions SET used = 1 WHERE session_id = ?", (session,)
    )
    con.commit()
    con.close()

    return {
        "access_token": row["access_token"],
        "refresh_token": row["refresh_token"],
        "expiry": row["expiry"],
    }
