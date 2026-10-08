"""GET /v1/models is answered by the daemon itself, from the model catalogue,
with the 1M variants Claude Desktop's model picker folds into one entry."""

import json
import threading
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
from claude_unlimited import model_catalogue


class _Model:
    def __init__(self, model_id):
        self.id = model_id


class _Catalogue:
    anthropic = (_Model("claude-opus-5-5"), _Model("claude-haiku-4-5"), _Model("claude-sonnet-5-5"))


def test_every_discovered_id_is_kept_and_1m_capable_ones_gain_a_variant(monkeypatch):
    monkeypatch.setattr(model_catalogue, "current", lambda: _Catalogue())
    ids = [m["id"] for m in daemon._local_models_list()["data"]]
    assert ids == ["claude-opus-5-5", "claude-haiku-4-5", "claude-sonnet-5-5",
                   "claude-opus-5-5[1m]", "claude-sonnet-5-5[1m]"]  # haiku has no 1M variant


def test_it_still_answers_before_the_catalogue_has_loaded(monkeypatch):
    monkeypatch.setattr(model_catalogue, "current", lambda: None)
    ids = [m["id"] for m in daemon._local_models_list()["data"]]
    assert "claude-sonnet-5-5" in ids and "claude-sonnet-5-5[1m]" in ids

    def boom():
        raise RuntimeError("catalogue unreadable")
    monkeypatch.setattr(model_catalogue, "current", boom)
    assert daemon._local_models_list()["data"], "model discovery must never fail the endpoint"


def test_the_endpoint_is_served_locally_without_touching_an_upstream(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(model_catalogue, "current", lambda: _Catalogue())
    server = daemon.make_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for suffix in ("/v1/models", "/v1/models/"):
            with urllib.request.urlopen(base + suffix, timeout=2) as resp:
                body = json.loads(resp.read())
            assert resp.status == 200 and body["object"] == "list"
            assert body["data"][0] == {"id": "claude-opus-5-5", "object": "model", "created": 0, "owned_by": "anthropic"}
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
