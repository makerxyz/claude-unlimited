"""Claude Code behind the gateway defaults to the 1-hour prompt cache and a
400k auto-compact window."""
import json

import claude_unlimited.cli as cli
from claude_unlimited.config import Pool, Profile, Settings, save_pool
from claude_unlimited.gateway import Gateway, _upgrade_cache_ttl
from tests.test_cli_daemon_lifecycle import _stub_launch
from tests.test_gateway import fake_response, pool_env  # noqa: F401  (fixture)

CLAUDE_CODE_UA = {"user-agent": "claude-cli/2.1.284 (external, claude-desktop-3p, agent-sdk/0.2)"}


def _launch(monkeypatch, *args):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "_probe_health", lambda host, port, timeout=1.0: True)
    monkeypatch.setattr(cli, "_fetch_placeholder_token", lambda host, port: "tok")
    calls = []
    _stub_launch(monkeypatch, calls)
    assert cli.main(["code", *args]) == 0
    return calls[0][1]


def test_code_defaults_to_1h_cache_and_400k_compact(monkeypatch):
    for name in ("CLAUDE_CODE_PROMPT_CACHE_TTL", "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL",
                 "CLAUDE_CODE_AUTO_COMPACT_WINDOW"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    argv = _launch(monkeypatch)
    assert cli.os.environ["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "1h"
    assert cli.os.environ["CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"] == "1h"
    assert argv[argv.index("--autocompact") + 1] == "400000"


def test_code_never_overrides_what_the_caller_set(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_PROMPT_CACHE_TTL", "5m")
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", "5m")
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "300000")
    argv = _launch(monkeypatch)
    assert cli.os.environ["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "5m"
    assert cli.os.environ["CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"] == "5m"
    assert "--autocompact" not in argv


def test_explicit_autocompact_flag_wins(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    argv = _launch(monkeypatch, "--autocompact", "900000")
    assert argv.count("--autocompact") == 1 and "900000" in argv


BODY = {
    "model": "claude-sonnet-5",
    "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}],
}


def test_upgrade_adds_1h_to_every_ttlless_block():
    out = json.loads(_upgrade_cache_ttl(json.dumps(BODY).encode()))
    assert out["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert out["messages"][0]["content"][0]["cache_control"]["ttl"] == "1h"


def test_upgrade_leaves_a_request_that_sets_any_ttl_alone():
    body = json.loads(json.dumps(BODY))
    body["system"][0]["cache_control"]["ttl"] = "5m"
    raw = json.dumps(body).encode()
    assert _upgrade_cache_ttl(raw) == raw


def test_upgrade_ignores_unparseable_and_uncached_bodies():
    for raw in (b"not json", b"{}", b'{"messages": []}', b""):
        assert _upgrade_cache_ttl(raw) == raw


def _forwarded(headers, settings=None, kind="oauth", base_url=None):
    save_pool(Pool(profiles=[Profile(id="a", name="A", kind=kind, base_url=base_url,
                                     automatic=True, enabled=True)],
                   settings=settings or Settings()))
    sent = []

    def transport(req):
        sent.append(json.loads(req.body))
        return fake_response(200, {"anthropic-ratelimit-unified-5h-utilization": "0.1"})

    assert Gateway(transport=transport).handle(
        "POST", "/v1/messages", headers, json.dumps(BODY).encode()).status == 200
    return sent[0]


def test_gateway_upgrades_claude_code_traffic(pool_env):  # noqa: F811
    assert _forwarded(CLAUDE_CODE_UA)["system"][0]["cache_control"]["ttl"] == "1h"


def test_gateway_leaves_other_clients_alone(pool_env):  # noqa: F811
    assert "ttl" not in _forwarded({"user-agent": "python-requests/2.0"})["system"][0]["cache_control"]
    assert "ttl" not in _forwarded({})["system"][0]["cache_control"]


def test_gateway_flag_turns_it_off(pool_env):  # noqa: F811
    s = Settings(claude_code_cache_ttl_1h=False)
    assert "ttl" not in _forwarded(CLAUDE_CODE_UA, settings=s)["system"][0]["cache_control"]


def test_gateway_does_not_rewrite_for_non_anthropic_endpoints(pool_env):  # noqa: F811
    sent = _forwarded(CLAUDE_CODE_UA, kind="api", base_url="https://api.deepseek.com/anthropic")
    assert "ttl" not in sent["system"][0]["cache_control"]
