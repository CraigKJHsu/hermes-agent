from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "interaction-viewer"
    / "dashboard"
    / "plugin_api.py"
)


def _load_module():
    name = "hermes_dashboard_plugin_interaction_viewer_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_interaction_api_forwards_read_only_filters(monkeypatch):
    module = _load_module()
    captured = {}

    class FakeIndex:
        def query(self, **kwargs):
            captured.update(kwargs)
            return {"interactions": [], "sources": []}

    monkeypatch.setattr(module, "InteractionIndex", FakeIndex)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/interaction-viewer")
    client = TestClient(app)

    response = client.get(
        "/api/plugins/interaction-viewer/interactions",
        params={
            "session_id": "session-1",
            "delegation_id": "delegation-1",
            "include_internal": "true",
            "include_unlinked_openclaw": "true",
            "classes": "agent_handoff,execution_trace",
            "limit": 77,
        },
    )

    assert response.status_code == 200
    assert captured == {
        "limit": 77,
        "before": None,
        "before_id": None,
        "session_id": "session-1",
        "delegation_id": "delegation-1",
        "interaction_classes": ["agent_handoff", "execution_trace"],
        "include_internal": True,
        "include_unlinked_openclaw": True,
    }


def test_interaction_api_returns_400_for_invalid_classification(monkeypatch):
    module = _load_module()

    class FakeIndex:
        def query(self, **_kwargs):
            raise ValueError("Unknown interaction classes: guessed")

    monkeypatch.setattr(module, "InteractionIndex", FakeIndex)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/interaction-viewer")
    client = TestClient(app)

    response = client.get(
        "/api/plugins/interaction-viewer/interactions",
        params={"classes": "guessed"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown interaction classes: guessed"
