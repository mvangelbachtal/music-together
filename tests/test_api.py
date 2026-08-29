import importlib
import io
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient


def test_queue_votes_and_completion_advance(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.main as main

    main.DATABASE_PATH = str(tmp_path / "test.db")
    importlib.reload(main)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        guest_token = session["guest_url"].rsplit("/", 1)[-1]
        host_token = session["host_url"].rsplit("/", 1)[-1]

        first = client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/first", "title": "First"}).json()
        client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/second", "title": "Second"})
        duplicate = client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://www.youtube.com/watch?v=first", "title": "Duplicate"}).json()
        assert len(duplicate["queue"]) == 2

        item_id = first["queue"][0]["id"]
        client.post(f"/api/sessions/{guest_token}/queue/{item_id}/vote")
        voted = client.get(f"/api/sessions/{guest_token}").json()
        assert voted["queue"][0]["voted"] is True
        toggled = client.post(f"/api/sessions/{guest_token}/queue/{item_id}/vote").json()
        assert toggled["queue"][0]["votes"] == 0

        client.post(f"/api/sessions/{guest_token}/queue/{item_id}/vote")
        client.post(f"/api/host/{host_token}/queue/{item_id}/play")
        completed = client.post(f"/api/host/{host_token}/queue/{item_id}/complete").json()
        assert completed["playing"]["video_id"] == "second"

        next_state = client.post(f"/api/host/{host_token}/next").json()
        assert next_state["playing"] is None


def test_session_can_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.main as main

    main.DATABASE_PATH = str(tmp_path / "test.db")
    importlib.reload(main)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        guest_token = session["guest_url"].rsplit("/", 1)[-1]
        host_token = session["host_url"].rsplit("/", 1)[-1]
        assert client.post(f"/api/host/{host_token}/end").status_code == 200
        assert client.get(f"/api/sessions/{guest_token}").status_code == 404


def test_session_playback_owner_is_selected_at_creation(tmp_path, monkeypatch):
    database_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    import app.main as main

    main.DATABASE_PATH = str(database_path)
    main.PLAYBACK_OWNER = "host"
    importlib.reload(main)
    with TestClient(main.app) as client:
        kiosk_session = client.post("/api/sessions", json={"kiosk_playback": True}).json()
        host_session = client.post("/api/sessions", json={"kiosk_playback": False}).json()
        kiosk_token = kiosk_session["guest_url"].rsplit("/", 1)[-1]
        host_token = host_session["guest_url"].rsplit("/", 1)[-1]

        assert client.get(f"/api/sessions/{kiosk_token}").json()["playback_owner"] == "kiosk"
        assert client.get(f"/api/sessions/{host_token}").json()["playback_owner"] == "host"


def test_session_toggle_can_override_kiosk_default(tmp_path, monkeypatch):
    database_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("PLAYBACK_OWNER", "kiosk")
    import app.main as main

    main.DATABASE_PATH = str(database_path)
    importlib.reload(main)
    with TestClient(main.app) as client:
        default_session = client.post("/api/sessions").json()
        host_session = client.post("/api/sessions", json={"kiosk_playback": False}).json()
        default_token = default_session["guest_url"].rsplit("/", 1)[-1]
        host_token = host_session["guest_url"].rsplit("/", 1)[-1]

        assert client.get(f"/api/sessions/{default_token}").json()["playback_owner"] == "kiosk"
        assert client.get(f"/api/sessions/{host_token}").json()["playback_owner"] == "host"


def test_existing_authenticated_session_returns_playback_owner(tmp_path, monkeypatch):
    database_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("REQUIRE_OIDC_AUTH", "0")
    import app.main as main

    main.DATABASE_PATH = str(database_path)
    importlib.reload(main)
    with TestClient(main.app) as client:
        first = client.post("/api/sessions", json={"kiosk_playback": True}, cookies={"oidc_subject": "test-user"})
        reused = client.post("/api/sessions", cookies={"oidc_subject": "test-user"})

    assert first.status_code == 200
    assert reused.status_code == 200
    assert reused.json()["playback_owner"] == "kiosk"


def test_playback_pauses_after_three_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.main as main

    main.DATABASE_PATH = str(tmp_path / "test.db")
    importlib.reload(main)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        host_token = session["host_url"].rsplit("/", 1)[-1]
        for _ in range(3):
            state = client.post(f"/api/host/{host_token}/playback-failure")
        assert state.json()["playback_paused"] is True
        resumed = client.post(f"/api/host/{host_token}/resume-playback").json()
        assert resumed["playback_paused"] is False


def test_expired_session_is_unavailable_and_kiosk_qr_exists(tmp_path, monkeypatch):
    database_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    import app.main as main

    main.DATABASE_PATH = str(database_path)
    importlib.reload(main)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        kiosk_token = session["kiosk_url"].rsplit("/", 1)[-1]
        guest_token = session["guest_url"].rsplit("/", 1)[-1]
        assert client.get(f"/api/qr/{kiosk_token}").headers["content-type"] == "image/png"
        old_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        with sqlite3.connect(database_path) as database:
            database.execute("UPDATE sessions SET last_host_seen = ? WHERE kiosk_token = ?", (old_time, kiosk_token))
        assert client.get(f"/api/sessions/{guest_token}").status_code == 404


def test_transport_state_is_shared(tmp_path, monkeypatch):
    database_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    import app.main as main

    main.DATABASE_PATH = str(database_path)
    importlib.reload(main)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        host_token = session["host_url"].rsplit("/", 1)[-1]
        paused = client.post(f"/api/host/{host_token}/transport", json={"state": "paused", "position": 12.5, "volume": 42}).json()
        assert paused["playback_state"] == "paused"
        assert paused["playback_position"] == 12.5
        assert paused["playback_volume"] == 42
        assert paused["playback_revision"] == 1

        assert "video_enabled" not in paused


def test_transport_position_heartbeat_does_not_change_revision(tmp_path, monkeypatch):
    database_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    import app.main as main

    main.DATABASE_PATH = str(database_path)
    importlib.reload(main)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        host_token = session["host_url"].rsplit("/", 1)[-1]
        started = client.post(f"/api/host/{host_token}/transport", json={"state": "playing", "position": 0, "volume": 80}).json()
        heartbeat = client.post(f"/api/host/{host_token}/transport-position", json={"position": 12.5}).json()

    assert heartbeat["playback_position"] == 12.5
    assert heartbeat["playback_revision"] == started["playback_revision"]


def test_fallback_playlist_can_be_skipped(tmp_path, monkeypatch):
    database_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    import app.main as main

    main.DATABASE_PATH = str(database_path)
    importlib.reload(main)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        host_token = session["host_url"].rsplit("/", 1)[-1]
        configured = client.post(
            f"/api/host/{host_token}/fallback",
            json={"playlist_url": "https://www.youtube.com/playlist?list=playlist123"},
        ).json()
        skipped = client.post(f"/api/host/{host_token}/fallback/skip").json()
        missing = client.post(f"/api/host/{host_token}/fallback/skip")

    assert configured["fallback_playlist"].endswith("playlist123")
    assert skipped["playback_revision"] == configured["playback_revision"] + 1
    assert missing.status_code == 200


def test_websocket_preserves_guest_vote_state(tmp_path, monkeypatch):
    database_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    import app.main as main

    main.DATABASE_PATH = str(database_path)
    importlib.reload(main)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        guest_token = session["guest_url"].rsplit("/", 1)[-1]
        client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/vote-state", "title": "Vote state"})
        queue_item = client.get(f"/api/sessions/{guest_token}").json()["queue"][0]
        client.post(f"/api/sessions/{guest_token}/queue/{queue_item['id']}/vote")
        with client.websocket_connect(f"/ws/{guest_token}") as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["queue"][0]["voted"] is True


def test_oidc_callback_handles_missing_parameters(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.main as main

    main.DATABASE_PATH = str(tmp_path / "test.db")
    importlib.reload(main)
    with TestClient(main.app) as client:
        missing = client.get("/auth/callback")
        denied = client.get("/auth/callback?error=access_denied&error_description=User%20cancelled", follow_redirects=False)
        assert missing.status_code == 400
        assert denied.status_code == 307
        assert parse_qs(urlparse(denied.headers["location"]).query)["auth_error"] == ["User cancelled"]


def test_sponsorblock_segments_are_available_to_session_players(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.main as main

    main.DATABASE_PATH = str(tmp_path / "test.db")
    main.SPONSORBLOCK_ENABLED = True
    importlib.reload(main)

    class FakeResponse(io.BytesIO):
        def __init__(self):
            super().__init__(b'[{"category":"intro","segment":[0,12]},{"category":"outro","segment":[180,195]}]')

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        assert "skipSegments" in request.full_url
        request_categories = parse_qs(urlparse(request.full_url).query)["categories"][0]
        assert "sponsor" in request_categories
        assert "outro" in request_categories
        assert timeout == 4
        return FakeResponse()

    monkeypatch.setattr(main, "urlopen", fake_urlopen)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        guest_token = session["guest_url"].rsplit("/", 1)[-1]
        response = client.get(f"/api/sessions/{guest_token}/skip-segments/video123")

    assert response.status_code == 200
    assert response.json() == {"segments": [
        {"start": 0, "end": 12, "category": "intro"},
        {"start": 180, "end": 195, "category": "outro"},
    ]}
