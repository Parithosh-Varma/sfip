"""Read-only API surface: health, state, SSE stream headers."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    new = dataclasses.replace(settings, db_path=tmp_path / "api.db")
    monkeypatch.setattr(db, "settings", new)
    import app.api as api_mod
    monkeypatch.setattr(api_mod, "db", db)
    db.init_db()
    with TestClient(api_mod.app, raise_server_exceptions=False) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_state_empty(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.json()["investigations"] == []


def test_no_mutating_routes(client):
    routes = {f"{','.join(sorted(r.methods))} {r.path}" for r in client.app.routes if hasattr(r, "methods")}
    mutating = [x for x in routes if x.startswith(("POST", "PUT", "DELETE", "PATCH"))]
    assert mutating == [], mutating
