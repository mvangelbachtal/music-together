import importlib
import io
import json
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
        assert first["queue"][0]["votes"] == 1
        assert first["queue"][0]["voted"] is True

        removed = client.post(f"/api/sessions/{guest_token}/queue/{item_id}/vote").json()
        removed_item = next(item for item in removed["queue"] if item["id"] == item_id)
        assert removed_item["votes"] == 0
        assert removed_item["voted"] is False

        restored = client.post(f"/api/sessions/{guest_token}/queue/{item_id}/vote").json()
        restored_item = next(item for item in restored["queue"] if item["id"] == item_id)
        assert restored_item["votes"] == 1
        assert restored_item["voted"] is True

        client.post(f"/api/host/{host_token}/queue/{item_id}/play")
        completed = client.post(f"/api/host/{host_token}/queue/{item_id}/complete").json()
        assert completed["playing"]["video_id"] == "second"

        next_state = client.post(f"/api/host/{host_token}/next").json()
        assert next_state["playing"] is None


def test_adding_existing_queued_song_votes_for_other_guest(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.main as main

    main.DATABASE_PATH = str(tmp_path / "test.db")
    importlib.reload(main)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        guest_token = session["guest_url"].rsplit("/", 1)[-1]

        added = client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/shared", "title": "Shared"}).json()
        assert added["queue"][0]["votes"] == 1

        with TestClient(main.app) as other_client:
            resubmitted = other_client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/shared", "title": "Shared"}).json()
            item = next(item for item in resubmitted["queue"] if item["video_id"] == "shared")
            assert item["votes"] == 2
            assert item["voted"] is True

            repeated = other_client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/shared", "title": "Shared"}).json()
            repeated_item = next(item for item in repeated["queue"] if item["video_id"] == "shared")
            assert repeated_item["votes"] == 2


def test_song_can_be_requested_again_after_it_already_played(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.main as main

    main.DATABASE_PATH = str(tmp_path / "test.db")
    importlib.reload(main)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        guest_token = session["guest_url"].rsplit("/", 1)[-1]
        host_token = session["host_url"].rsplit("/", 1)[-1]

        first_add = client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/replay", "title": "Replay"}).json()
        item_id = first_add["queue"][0]["id"]
        client.post(f"/api/host/{host_token}/queue/{item_id}/play")
        client.post(f"/api/host/{host_token}/queue/{item_id}/complete")

        removed_add = client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/removable", "title": "Removable"}).json()
        removed_item_id = removed_add["queue"][0]["id"]
        client.post(f"/api/host/{host_token}/queue/{removed_item_id}/remove")

        replayed = client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/replay", "title": "Replay"}).json()
        re_removed = client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/removable", "title": "Removable"}).json()

        assert any(item["video_id"] == "replay" and item["votes"] == 1 for item in replayed["queue"])
        assert any(item["video_id"] == "removable" and item["votes"] == 1 for item in re_removed["queue"])


def test_playlist_bulk_add_uses_zero_votes_and_ranks_below_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    import app.main as main

    main.DATABASE_PATH = str(tmp_path / "test.db")
    main.YOUTUBE_API_KEY = "test-key"
    importlib.reload(main)

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        assert "playlistItems" in request.full_url
        assert timeout == 8
        payload = json.dumps({"items": [
            {"snippet": {"resourceId": {"videoId": "bulk-one"}, "title": "Bulk One", "channelTitle": "Channel"}},
            {"snippet": {"resourceId": {"videoId": "bulk-two"}, "title": "Bulk Two", "channelTitle": "Channel"}},
        ]}).encode()
        return FakeResponse(payload)

    monkeypatch.setattr(main, "urlopen", fake_urlopen)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions").json()
        guest_token = session["guest_url"].rsplit("/", 1)[-1]
        host_token = session["host_url"].rsplit("/", 1)[-1]

        bulk = client.post(f"/api/host/{host_token}/playlist", json={"url": "https://www.youtube.com/playlist?list=abc123"}).json()
        assert len(bulk["queue"]) == 2
        assert all(item["votes"] == 0 for item in bulk["queue"])

        requested = client.post(f"/api/sessions/{guest_token}/songs", json={"url": "https://youtu.be/requested", "title": "Requested"}).json()
        assert requested["queue"][0]["video_id"] == "requested"
        assert requested["queue"][0]["votes"] == 1

        forbidden = client.post(f"/api/sessions/{guest_token}/playlist", json={"url": "https://www.youtube.com/playlist?list=abc123"})
        assert forbidden.status_code == 404


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
