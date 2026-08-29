import asyncio
import io
import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import qrcode
from fastapi import Cookie, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATABASE_PATH = os.getenv("DATABASE_PATH", "music-together.db")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
SPONSORBLOCK_ENABLED = os.getenv("SPONSORBLOCK_ENABLED", "1") == "1"
SPONSORBLOCK_CATEGORIES = tuple(
    category.strip()
    for category in os.getenv(
        "SPONSORBLOCK_CATEGORIES",
        "sponsor,selfpromo,interaction,intro,outro,preview,filler,music_offtopic",
    ).split(",")
    if category.strip()
)
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET")
OIDC_AUTHORIZATION_URL = os.getenv("OIDC_AUTHORIZATION_URL", f"{OIDC_ISSUER}/application/o/authorize/")
OIDC_TOKEN_URL = os.getenv("OIDC_TOKEN_URL", f"{OIDC_ISSUER}/application/o/token/")
OIDC_USERINFO_URL = os.getenv("OIDC_USERINFO_URL", f"{OIDC_ISSUER}/application/o/userinfo/")
OIDC_REDIRECT_URI = os.getenv("OIDC_REDIRECT_URI", f"{PUBLIC_BASE_URL}/auth/callback")
OIDC_SCOPES = os.getenv("OIDC_SCOPES", "openid email profile")
REQUIRE_OIDC_AUTH = os.getenv("REQUIRE_OIDC_AUTH", "0") == "1"
PLAYBACK_OWNER = os.getenv("PLAYBACK_OWNER", "host").lower()
if PLAYBACK_OWNER not in {"host", "kiosk"}:
    PLAYBACK_OWNER = "host"
STATIC_DIR = Path(__file__).parent / "static"


def now() -> str:
    return datetime.now(UTC).isoformat()


def connection() -> sqlite3.Connection:
    database = sqlite3.connect(DATABASE_PATH)
    database.row_factory = sqlite3.Row
    return database


def setup_database() -> None:
    with connection() as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                host_token TEXT NOT NULL UNIQUE,
                guest_token TEXT NOT NULL UNIQUE,
                kiosk_token TEXT NOT NULL UNIQUE,
                fallback_playlist TEXT,
                state TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                last_host_seen TEXT NOT NULL,
                google_subject TEXT,
                playback_paused INTEGER NOT NULL DEFAULT 0,
                failure_window_started TEXT,
                failure_count INTEGER NOT NULL DEFAULT 0,
                playback_state TEXT NOT NULL DEFAULT 'stopped',
                playback_position REAL NOT NULL DEFAULT 0,
                playback_revision INTEGER NOT NULL DEFAULT 0,
                playback_volume INTEGER NOT NULL DEFAULT 80,
                playback_owner TEXT NOT NULL DEFAULT 'host',
                video_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS queue_items (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT 'YouTube selection',
                thumbnail TEXT,
                state TEXT NOT NULL DEFAULT 'queued',
                submitted_at TEXT NOT NULL,
                UNIQUE(session_id, video_id)
            );
            CREATE TABLE IF NOT EXISTS votes (
                queue_item_id INTEGER NOT NULL REFERENCES queue_items(id),
                guest_id TEXT NOT NULL,
                PRIMARY KEY(queue_item_id, guest_id)
            );
            """
        )


def migrate_database() -> None:
    setup_database()
    with connection() as database:
        columns = {row["name"] for row in database.execute("PRAGMA table_info(sessions)")}
        if "google_subject" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN google_subject TEXT")
        if "playback_paused" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN playback_paused INTEGER NOT NULL DEFAULT 0")
        if "failure_window_started" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN failure_window_started TEXT")
        if "failure_count" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")
        if "playback_state" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN playback_state TEXT NOT NULL DEFAULT 'stopped'")
        if "playback_position" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN playback_position REAL NOT NULL DEFAULT 0")
        if "playback_revision" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN playback_revision INTEGER NOT NULL DEFAULT 0")
        if "playback_volume" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN playback_volume INTEGER NOT NULL DEFAULT 80")
        if "playback_owner" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN playback_owner TEXT NOT NULL DEFAULT 'host'")
        if "video_enabled" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN video_enabled INTEGER NOT NULL DEFAULT 1")


async def cleanup_sessions() -> None:
    while True:
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        with connection() as database:
            database.execute("UPDATE sessions SET state = 'expired' WHERE state = 'active' AND last_host_seen < ?", (cutoff,))
            database.execute("DELETE FROM votes WHERE queue_item_id IN (SELECT id FROM queue_items WHERE session_id IN (SELECT id FROM sessions WHERE state != 'active'))")
        await asyncio.sleep(900)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    migrate_database()
    cleanup_task = asyncio.create_task(cleanup_sessions())
    try:
        yield
    finally:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)


app = FastAPI(title="Music Together", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SongRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(default="YouTube selection", max_length=200)


class FallbackRequest(BaseModel):
    playlist_url: str = Field(default="", max_length=500)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100)


class CreateSessionRequest(BaseModel):
    kiosk_playback: bool | None = None


class TransportRequest(BaseModel):
    state: str = Field(pattern="^(playing|paused|stopped)$")
    position: float = Field(default=0, ge=0)
    volume: int = Field(default=80, ge=0, le=100)


class PlaybackPositionRequest(BaseModel):
    position: float = Field(ge=0)


def token_session(token: str, kind: str) -> sqlite3.Row:
    column = {"guest": "guest_token", "kiosk": "kiosk_token", "host": "host_token"}[kind]
    with connection() as database:
        session = database.execute(f"SELECT * FROM sessions WHERE {column} = ?", (token,)).fetchone()
    if not session or session["state"] != "active":
        raise HTTPException(status_code=404, detail="Session unavailable")
    if datetime.fromisoformat(session["last_host_seen"]) < datetime.now(UTC) - timedelta(hours=24):
        with connection() as database:
            database.execute("UPDATE sessions SET state = 'expired' WHERE id = ?", (session["id"],))
            database.execute("DELETE FROM votes WHERE queue_item_id IN (SELECT id FROM queue_items WHERE session_id = ?)", (session["id"],))
        raise HTTPException(status_code=404, detail="Session unavailable")
    return session


@app.post("/api/search")
def search(request: SearchRequest) -> dict:
    if not YOUTUBE_API_KEY:
        raise HTTPException(status_code=503, detail="Search is unavailable until YOUTUBE_API_KEY is configured")
    query = urlencode({"part": "snippet", "q": request.query, "type": "video", "maxResults": 8, "key": YOUTUBE_API_KEY})
    try:
        with urlopen(UrlRequest(f"https://www.googleapis.com/youtube/v3/search?{query}"), timeout=8) as response:
            payload = json.load(response)
    except Exception as error:
        raise HTTPException(status_code=502, detail="YouTube search is temporarily unavailable") from error
    return {"results": [{"video_id": item["id"]["videoId"], "title": item["snippet"]["title"], "artist": item["snippet"]["channelTitle"], "thumbnail": item["snippet"].get("thumbnails", {}).get("medium", {}).get("url")} for item in payload.get("items", []) if item.get("id", {}).get("videoId")]}


@app.get("/api/sessions/{token}/skip-segments/{video_id}")
def skip_segments(token: str, video_id: str) -> dict:
    try:
        token_session(token, "guest")
    except HTTPException:
        try:
            token_session(token, "kiosk")
        except HTTPException:
            token_session(token, "host")
    if not SPONSORBLOCK_ENABLED:
        return {"segments": []}
    categories = json.dumps(SPONSORBLOCK_CATEGORIES, separators=(",", ":"))
    query = urlencode({"videoID": video_id, "categories": categories})
    try:
        with urlopen(UrlRequest(f"https://sponsor.ajay.app/api/skipSegments?{query}", headers={"x-client-name": "music-together"}), timeout=4) as response:
            payload = json.load(response)
    except Exception:
        return {"segments": []}
    return {"segments": [{"start": segment["segment"][0], "end": segment["segment"][1], "category": segment["category"]} for segment in payload if len(segment.get("segment", [])) == 2]}


def video_id(url: str) -> str:
    for marker in ("youtu.be/", "watch?v=", "music.youtube.com/watch?v="):
        if marker in url:
            value = url.split(marker, 1)[1].split("&", 1)[0].split("?", 1)[0]
            if value:
                return value[:100]
    if url.startswith("youtube:"):
        return url.removeprefix("youtube:")[:100]
    raise HTTPException(status_code=422, detail="Use a YouTube or YouTube Music URL")


def queue_for(session_id: int, guest_id: str | None = None) -> list[dict]:
    with connection() as database:
        rows = database.execute(
            """
            SELECT q.*, COUNT(v.guest_id) AS votes,
                CASE WHEN ? IS NOT NULL AND EXISTS (
                    SELECT 1 FROM votes own_vote WHERE own_vote.queue_item_id = q.id AND own_vote.guest_id = ?
                ) THEN 1 ELSE 0 END AS voted
            FROM queue_items q LEFT JOIN votes v ON v.queue_item_id = q.id
            WHERE q.session_id = ? AND q.state = 'queued'
            GROUP BY q.id ORDER BY votes DESC, q.submitted_at ASC
            """,
            (guest_id, guest_id, session_id),
        ).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item["voted"] = bool(item["voted"])
    return items


def session_payload(session: sqlite3.Row, guest_id: str | None = None) -> dict:
    with connection() as database:
        playing = database.execute(
            "SELECT * FROM queue_items WHERE session_id = ? AND state = 'playing' LIMIT 1", (session["id"],)
        ).fetchone()
    return {"playing": dict(playing) if playing else None, "queue": queue_for(session["id"], guest_id), "fallback_playlist": session["fallback_playlist"], "playback_paused": bool(session["playback_paused"]), "failure_count": session["failure_count"], "playback_owner": session["playback_owner"], "playback_state": session["playback_state"], "playback_position": session["playback_position"], "playback_volume": session["playback_volume"], "playback_revision": session["playback_revision"]}


def host_session(token: str, oidc_subject: str | None = None) -> sqlite3.Row:
    session = token_session(token, "host")
    if REQUIRE_OIDC_AUTH and (not oidc_subject or oidc_subject != session["google_subject"]):
        raise HTTPException(status_code=403, detail="Host authentication required")
    return session


@app.get("/auth/login")
def oidc_login() -> Response:
    if not OIDC_CLIENT_ID or not OIDC_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="OIDC authentication is not configured")
    state = secrets.token_urlsafe(32)
    response = RedirectResponse(OIDC_AUTHORIZATION_URL + "?" + urlencode({"client_id": OIDC_CLIENT_ID, "redirect_uri": OIDC_REDIRECT_URI, "response_type": "code", "scope": OIDC_SCOPES, "state": state}))
    response.set_cookie("oauth_state", state, httponly=True, samesite="lax", max_age=600)
    return response


@app.get("/auth/callback")
def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> Response:
    if error:
        description = error_description or error
        response = RedirectResponse(f"/?auth_error={urlencode({'message': description})[8:]}")
        response.delete_cookie("oauth_state")
        return response
    if not code or not state:
        raise HTTPException(status_code=400, detail="OIDC callback is missing code or state")
    if not OIDC_CLIENT_ID or not OIDC_CLIENT_SECRET or state != request.cookies.get("oauth_state"):
        raise HTTPException(status_code=400, detail="Invalid OAuth callback")
    payload = urlencode({"code": code, "client_id": OIDC_CLIENT_ID, "client_secret": OIDC_CLIENT_SECRET, "redirect_uri": OIDC_REDIRECT_URI, "grant_type": "authorization_code"}).encode()
    try:
        with urlopen(UrlRequest(OIDC_TOKEN_URL, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=8) as token_response:
            access_token = json.load(token_response)["access_token"]
        with urlopen(UrlRequest(OIDC_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}), timeout=8) as user_response:
            oidc_subject = json.load(user_response)["sub"]
    except Exception as exc:
        raise HTTPException(status_code=502, detail="OIDC authentication failed") from exc
    response = RedirectResponse("/")
    response.set_cookie("oidc_subject", oidc_subject, httponly=True, samesite="lax", max_age=86400)
    response.delete_cookie("oauth_state")
    return response


@app.get("/", response_class=HTMLResponse)
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def config() -> dict:
    return {"default_playback_owner": PLAYBACK_OWNER}


@app.post("/api/sessions")
def create_session(response: Response, request: CreateSessionRequest | None = None, oidc_subject: str | None = Cookie(default=None)) -> dict:
    if REQUIRE_OIDC_AUTH and not oidc_subject:
        raise HTTPException(status_code=401, detail="Sign in before creating a session")
    if oidc_subject:
        with connection() as database:
            existing = database.execute("SELECT host_token, guest_token, kiosk_token, playback_owner FROM sessions WHERE google_subject = ? AND state = 'active'", (oidc_subject,)).fetchone()
        if existing:
            response.set_cookie("host_token", existing["host_token"], httponly=True, samesite="lax")
            return {"guest_url": f"{PUBLIC_BASE_URL}/guest/{existing['guest_token']}", "kiosk_url": f"{PUBLIC_BASE_URL}/kiosk/{existing['kiosk_token']}", "host_url": f"{PUBLIC_BASE_URL}/host/{existing['host_token']}", "playback_owner": existing["playback_owner"]}
    host_token, guest_token, kiosk_token = (secrets.token_urlsafe(32) for _ in range(3))
    playback_owner = ("kiosk" if request.kiosk_playback else "host") if request is not None else PLAYBACK_OWNER
    with connection() as database:
        cursor = database.execute(
            "INSERT INTO sessions(host_token, guest_token, kiosk_token, created_at, last_host_seen, google_subject, playback_owner) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (host_token, guest_token, kiosk_token, now(), now(), oidc_subject, playback_owner),
        )
        session_id = cursor.lastrowid
    response.set_cookie("host_token", host_token, httponly=True, samesite="lax")
    return {"id": session_id, "guest_url": f"{PUBLIC_BASE_URL}/guest/{guest_token}", "kiosk_url": f"{PUBLIC_BASE_URL}/kiosk/{kiosk_token}", "host_url": f"{PUBLIC_BASE_URL}/host/{host_token}", "playback_owner": playback_owner}


@app.get("/api/sessions/{token}")
def read_session(token: str, guest_id: str | None = Cookie(default=None)) -> dict:
    try:
        session = token_session(token, "guest")
    except HTTPException:
        session = token_session(token, "kiosk")
    return session_payload(session, guest_id)


@app.get("/api/host/{token}")
def read_host_session(token: str, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE sessions SET last_host_seen = ? WHERE id = ?", (now(), session["id"]))
    return {**session_payload(host_session(token, google_subject)), "host_url": f"{PUBLIC_BASE_URL}/host/{session['host_token']}", "guest_url": f"{PUBLIC_BASE_URL}/guest/{session['guest_token']}", "kiosk_url": f"{PUBLIC_BASE_URL}/kiosk/{session['kiosk_token']}"}


@app.post("/api/sessions/{token}/songs")
def add_song(response: Response, token: str, request: SongRequest, guest_id: str | None = Cookie(default=None)) -> dict:
    session = token_session(token, "guest")
    guest_id = guest_id or secrets.token_urlsafe(18)
    identifier = video_id(request.url)
    with connection() as database:
        existing = database.execute(
            "SELECT id FROM queue_items WHERE session_id = ? AND video_id = ? AND state IN ('queued', 'playing')",
            (session["id"], identifier),
        ).fetchone()
        if not existing:
            database.execute(
                "INSERT INTO queue_items(session_id, video_id, title, artist, thumbnail, submitted_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session["id"], identifier, request.title, request.artist, f"https://i.ytimg.com/vi/{identifier}/hqdefault.jpg", now()),
            )
    response.set_cookie("guest_id", guest_id, httponly=True, samesite="lax")
    return {"ok": True, "guest_id": guest_id, "queue": queue_for(session["id"], guest_id)}


@app.post("/api/sessions/{token}/queue/{item_id}/vote")
def vote(response: Response, token: str, item_id: int, guest_id: str | None = Cookie(default=None)) -> dict:
    session = token_session(token, "guest")
    if not guest_id:
        guest_id = secrets.token_urlsafe(18)
    with connection() as database:
        item = database.execute("SELECT id FROM queue_items WHERE id = ? AND session_id = ? AND state = 'queued'", (item_id, session["id"])).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Queue item unavailable")
        try:
            database.execute("INSERT INTO votes(queue_item_id, guest_id) VALUES (?, ?)", (item_id, guest_id))
        except sqlite3.IntegrityError:
            database.execute("DELETE FROM votes WHERE queue_item_id = ? AND guest_id = ?", (item_id, guest_id))
    response.set_cookie("guest_id", guest_id, httponly=True, samesite="lax")
    return {"ok": True, "guest_id": guest_id, "queue": queue_for(session["id"], guest_id)}


@app.post("/api/host/{token}/fallback")
def set_fallback(token: str, request: FallbackRequest, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE sessions SET fallback_playlist = ?, last_host_seen = ? WHERE id = ?", (request.playlist_url or None, now(), session["id"]))
    return session_payload(host_session(token, google_subject))


@app.post("/api/host/{token}/fallback/skip")
def skip_fallback(token: str, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    if not session["fallback_playlist"]:
        raise HTTPException(status_code=404, detail="Fallback playlist is not configured")
    with connection() as database:
        database.execute("UPDATE sessions SET playback_revision = playback_revision + 1, last_host_seen = ? WHERE id = ?", (now(), session["id"]))
    return session_payload(host_session(token, google_subject))


@app.post("/api/host/{token}/queue/{item_id}/play")
def play_item(token: str, item_id: int, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE queue_items SET state = 'queued' WHERE session_id = ? AND state = 'playing'", (session["id"],))
        changed = database.execute("UPDATE queue_items SET state = 'playing' WHERE id = ? AND session_id = ? AND state = 'queued'", (item_id, session["id"]))
        if not changed.rowcount:
            raise HTTPException(status_code=404, detail="Queue item unavailable")
        database.execute("UPDATE sessions SET playback_state = 'playing', playback_position = 0, playback_revision = playback_revision + 1 WHERE id = ?", (session["id"],))
        database.execute("DELETE FROM votes WHERE queue_item_id = ?", (item_id,))
    return session_payload(host_session(token, google_subject))


@app.post("/api/host/{token}/queue/{item_id}/remove")
def remove_item(token: str, item_id: int, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE queue_items SET state = 'removed' WHERE id = ? AND session_id = ? AND state IN ('queued', 'playing')", (item_id, session["id"]))
        database.execute("DELETE FROM votes WHERE queue_item_id = ?", (item_id,))
    return session_payload(host_session(token, google_subject))


@app.post("/api/host/{token}/queue/{item_id}/complete")
def complete_item(token: str, item_id: int, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE queue_items SET state = 'played' WHERE id = ? AND session_id = ? AND state = 'playing'", (item_id, session["id"]))
        database.execute("DELETE FROM votes WHERE queue_item_id = ?", (item_id,))
        next_item = database.execute(
            "SELECT id FROM queue_items WHERE session_id = ? AND state = 'queued' ORDER BY (SELECT COUNT(*) FROM votes WHERE queue_item_id = queue_items.id) DESC, submitted_at ASC LIMIT 1",
            (session["id"],),
        ).fetchone()
        if next_item:
            database.execute("UPDATE queue_items SET state = 'playing' WHERE id = ?", (next_item["id"],))
            database.execute("UPDATE sessions SET playback_state = 'playing', playback_position = 0, playback_revision = playback_revision + 1 WHERE id = ?", (session["id"],))
    return session_payload(host_session(token, google_subject))


@app.post("/api/kiosk/{token}/complete")
def kiosk_complete(token: str) -> dict:
    session = token_session(token, "kiosk")
    with connection() as database:
        database.execute("UPDATE queue_items SET state = 'played' WHERE session_id = ? AND state = 'playing'", (session["id"],))
        database.execute("DELETE FROM votes WHERE queue_item_id IN (SELECT id FROM queue_items WHERE session_id = ? AND state = 'played')", (session["id"],))
        next_item = database.execute(
            "SELECT id FROM queue_items WHERE session_id = ? AND state = 'queued' ORDER BY (SELECT COUNT(*) FROM votes WHERE queue_item_id = queue_items.id) DESC, submitted_at ASC LIMIT 1",
            (session["id"],),
        ).fetchone()
        if next_item:
            database.execute("UPDATE queue_items SET state = 'playing' WHERE id = ?", (next_item["id"],))
            database.execute("UPDATE sessions SET playback_state = 'playing', playback_position = 0, playback_revision = playback_revision + 1 WHERE id = ?", (session["id"],))
    return session_payload(token_session(token, "kiosk"))


@app.post("/api/host/{token}/next")
def next_item(token: str, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE queue_items SET state = 'played' WHERE session_id = ? AND state = 'playing'", (session["id"],))
        selected = database.execute(
            "SELECT id FROM queue_items WHERE session_id = ? AND state = 'queued' ORDER BY (SELECT COUNT(*) FROM votes WHERE queue_item_id = queue_items.id) DESC, submitted_at ASC LIMIT 1",
            (session["id"],),
        ).fetchone()
        if selected:
            database.execute("UPDATE queue_items SET state = 'playing' WHERE id = ?", (selected["id"],))
            database.execute("DELETE FROM votes WHERE queue_item_id = ?", (selected["id"],))
            database.execute("UPDATE sessions SET playback_state = 'playing', playback_position = 0, playback_revision = playback_revision + 1 WHERE id = ?", (session["id"],))
    return session_payload(host_session(token, google_subject))


@app.post("/api/host/{token}/queue/{item_id}/skip")
def skip_item(token: str, item_id: int, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE queue_items SET state = 'skipped' WHERE id = ? AND session_id = ? AND state IN ('queued', 'playing')", (item_id, session["id"]))
        database.execute("DELETE FROM votes WHERE queue_item_id = ?", (item_id,))
        next_item = database.execute(
            "SELECT id FROM queue_items WHERE session_id = ? AND state = 'queued' ORDER BY (SELECT COUNT(*) FROM votes WHERE queue_item_id = queue_items.id) DESC, submitted_at ASC LIMIT 1",
            (session["id"],),
        ).fetchone()
        if next_item:
            database.execute("UPDATE queue_items SET state = 'playing' WHERE id = ?", (next_item["id"],))
            database.execute("UPDATE sessions SET playback_state = 'playing', playback_position = 0, playback_revision = playback_revision + 1 WHERE id = ?", (session["id"],))
    return session_payload(host_session(token, google_subject))


@app.post("/api/host/{token}/playback-failure")
def playback_failure(token: str, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    current_time = datetime.now(UTC)
    with connection() as database:
        started = datetime.fromisoformat(session["failure_window_started"]) if session["failure_window_started"] else None
        if not started or current_time - started > timedelta(minutes=10):
            count = 1
            window_started = now()
        else:
            count = session["failure_count"] + 1
            window_started = session["failure_window_started"]
        paused = count >= 3
        database.execute("UPDATE sessions SET failure_window_started = ?, failure_count = ?, playback_paused = ?, last_host_seen = ? WHERE id = ?", (window_started, count, paused, now(), session["id"]))
    return session_payload(host_session(token, google_subject))


@app.post("/api/host/{token}/resume-playback")
def resume_playback(token: str, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE sessions SET playback_paused = 0, failure_count = 0, failure_window_started = NULL, last_host_seen = ? WHERE id = ?", (now(), session["id"]))
    return session_payload(host_session(token, google_subject))


@app.post("/api/host/{token}/transport")
def transport(token: str, request: TransportRequest, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE sessions SET playback_state = ?, playback_position = ?, playback_volume = ?, playback_revision = playback_revision + 1, last_host_seen = ? WHERE id = ?", (request.state, request.position, request.volume, now(), session["id"]))
    return session_payload(host_session(token, google_subject))


@app.post("/api/host/{token}/transport-position")
def transport_position(token: str, request: PlaybackPositionRequest, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE sessions SET playback_position = ?, last_host_seen = ? WHERE id = ?", (request.position, now(), session["id"]))
    return session_payload(host_session(token, google_subject))


@app.post("/api/host/{token}/end")
def end_session(token: str, google_subject: str | None = Cookie(default=None, alias="oidc_subject")) -> dict:
    session = host_session(token, google_subject)
    with connection() as database:
        database.execute("UPDATE sessions SET state = 'ended' WHERE id = ?", (session["id"],))
    return {"ok": True}


@app.get("/api/qr/{token}")
def qr(token: str) -> StreamingResponse:
    session = token_session(token, "kiosk")
    image = qrcode.make(f"{PUBLIC_BASE_URL}/guest/{session['guest_token']}")
    output = io.BytesIO()
    image.get_image().save(output, format="PNG")
    output.seek(0)
    return StreamingResponse(output, media_type="image/png")


@app.get("/guest/{token}", response_class=HTMLResponse)
@app.get("/kiosk/{token}", response_class=HTMLResponse)
@app.get("/host/{token}", response_class=HTMLResponse)
def client_page(token: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws/{token}")
async def updates(websocket: WebSocket, token: str) -> None:
    await websocket.accept()
    try:
        try:
            session = token_session(token, "guest")
            guest_id = websocket.cookies.get("guest_id")
        except HTTPException:
            try:
                session = token_session(token, "kiosk")
                guest_id = None
            except HTTPException:
                session = host_session(token, websocket.cookies.get("oidc_subject"))
                guest_id = None
        while True:
            with connection() as database:
                current_session = database.execute("SELECT * FROM sessions WHERE id = ?", (session["id"],)).fetchone()
            if not current_session or current_session["state"] != "active":
                return
            await websocket.send_json(session_payload(current_session, guest_id))
            await asyncio.sleep(2)
    except (WebSocketDisconnect, HTTPException):
        return
