from fastapi.testclient import TestClient

from dashboard.main import app

client = TestClient(app)


def test_index_shows_title() -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Agents Dashboard" in resp.text


def test_healthz() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
