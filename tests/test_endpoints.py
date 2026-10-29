"""Model endpoints (local LLM servers and compatible APIs): owners manage them, keys stay write-only, and each agent
gets the endpoint in the shape its CLI expects."""
from __future__ import annotations

import json

import httpx
import pytest

from acc import providers
from acc.server.compiler import CompileError, LaunchInputs, build_args, compile_files, session_environment

OLLAMA = {"id": "local", "name": "Local Ollama", "kind": "ollama", "base_url": "http://127.0.0.1:11434",
          "styles": {"openai": "http://127.0.0.1:11434/v1", "anthropic": "http://127.0.0.1:11434"}}


def fake_ollama(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/tags"
    return httpx.Response(200, json={"models": [{"name": "qwen3:4b", "size": 2497293931}, {"name": "gpt-oss:20b"}]})


@pytest.fixture
def endpoint_server(api):
    api.app.state.ctx.endpoint_transport = httpx.MockTransport(fake_ollama)
    return api


async def test_owner_adds_an_endpoint_and_its_models_are_listed(endpoint_server):
    api = endpoint_server
    created = await api.post("/api/endpoints", json={**OLLAMA, "api_key": "sk-secret"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert [m["id"] for m in body["models"]] == ["gpt-oss:20b", "qwen3:4b"] and body["last_error"] is None
    assert body["has_key"] is True and "api_key" not in json.dumps(body) and "sk-secret" not in created.text
    listing = (await api.get("/api/endpoints")).json()
    assert listing[0]["styles"] == ["anthropic", "openai"]
    assert (await api.post("/api/endpoints", json=OLLAMA)).status_code == 409
    bad = await api.post("/api/endpoints", json={**OLLAMA, "id": "x", "styles": {"grpc": "http://a"}})
    assert bad.status_code == 422


async def test_launch_on_an_endpoint_needs_a_model_and_a_matching_style(endpoint_server, project):
    api = endpoint_server
    await api.post("/api/endpoints", json=OLLAMA)
    base = {"project_id": project["id"], "name": "x", "endpoint": "local"}
    no_model = await api.post("/api/sessions/preview", json={**base, "provider": "claude"})
    assert no_model.status_code == 422 and "models" in no_model.text
    preview = (await api.post("/api/sessions/preview", json={**base, "provider": "codex", "model": "qwen3:4b"})).json()
    assert json.loads(preview["files"]["endpoint.json"]) == {"id": "local", "name": "Local Ollama", "style": "openai",
                                                             "url": "http://127.0.0.1:11434/v1"}
    command = " ".join(preview["command"])
    assert 'model_provider="acc_local"' in command and 'model_providers.acc_local.base_url="http://127.0.0.1:11434/v1"' in command
    unknown = await api.post("/api/sessions/preview", json={**base, "endpoint": "nope", "provider": "codex", "model": "m"})
    assert unknown.status_code == 422


def test_each_cli_gets_the_endpoint_its_way():
    endpoint = {"id": "deepseek", "name": "DeepSeek", "styles": {"anthropic": "https://api.deepseek.com/anthropic",
                                                                "openai": "https://api.deepseek.com"}}
    claude = LaunchInputs(provider="claude", model="deepseek-v4-pro", endpoint="deepseek", remote_control=False)
    files = compile_files(claude, "/h", "/s", endpoint=endpoint).files
    env = session_environment(claude, None, "/r", files, "sk-ds")
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic" and env["ANTHROPIC_AUTH_TOKEN"] == "sk-ds"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-v4-pro" and env["ANTHROPIC_API_KEY"] == ""
    with pytest.raises(CompileError, match="Remote Control"):
        compile_files(LaunchInputs(provider="claude", model="m", endpoint="deepseek", remote_control=True), "/h", "/s",
                      endpoint=endpoint)

    copilot = LaunchInputs(provider="copilot", model="deepseek-v4-pro", endpoint="deepseek")
    files = compile_files(copilot, "/h", "/s", endpoint=endpoint).files
    env = session_environment(copilot, None, "/r", files, "sk-ds")
    assert env["COPILOT_PROVIDER_TYPE"] == "openai" and env["COPILOT_PROVIDER_BASE_URL"] == "https://api.deepseek.com"
    assert env["COPILOT_MODEL"] == "deepseek-v4-pro" and env["COPILOT_PROVIDER_API_KEY"] == "sk-ds"

    codex = LaunchInputs(provider="codex", model="deepseek-v4-pro", endpoint="deepseek")
    files = compile_files(codex, "/h", "/s", endpoint=endpoint).files
    assert session_environment(codex, None, "/r", files, "sk-ds")["ACC_ENDPOINT_API_KEY"] == "sk-ds"
    args = build_args(codex, run_dir="/r", tmux_name="t", native_id=None, resume=False, files=files)
    assert "--model" in args and 'model_providers.acc_deepseek.env_key="ACC_ENDPOINT_API_KEY"' in args

    anthropic_only = {"id": "a", "name": "A", "styles": {"anthropic": "http://a"}}
    with pytest.raises(CompileError, match="speaks anthropic"):
        compile_files(LaunchInputs(provider="codex", model="m", endpoint="a"), "/h", "/s", endpoint=anthropic_only)
    assert providers.get("copilot").endpoint_styles == ("openai", "anthropic")


async def test_session_on_an_endpoint_launches_with_its_key_in_the_env_file(endpoint_server, project, env):
    from helpers import wait_for
    from test_supervisor import launch, state_is
    api = endpoint_server
    await api.post("/api/endpoints", json={**OLLAMA, "api_key": "sk-local"})
    session = await launch(api, project, provider="codex", model="qwen3:4b", endpoint="local")
    await wait_for(state_is(api, session["id"], "idle"))
    env_file = env["run_dir"] / session["id"] / "session.env"
    assert "ACC_ENDPOINT_API_KEY=sk-local" in env_file.read_text() and env_file.stat().st_mode & 0o777 == 0o600
    detail = (await api.get(f"/api/sessions/{session['id']}")).text
    assert "sk-local" not in detail  # never in the launch spec or the API


# Bounded model discovery (security review 2026-10-22, #6): mocked transports, no live endpoint.

async def refreshed(api, handler) -> dict:
    """Add the Ollama endpoint with `handler` as its server, or refresh it; returns its owner view."""
    api.app.state.ctx.endpoint_transport = httpx.MockTransport(handler)
    created = await api.post("/api/endpoints", json=OLLAMA)
    if created.status_code == 409:
        created = await api.post(f"/api/endpoints/{OLLAMA['id']}/refresh")
    assert created.status_code in (200, 201), created.text
    return created.json()


async def test_oversized_model_list_is_refused(api):
    from acc.server.api import endpoints
    big = json.dumps({"models": [{"name": f"m{i}"} for i in range(200_000)]}).encode()
    assert len(big) > endpoints.MAX_LIST_BYTES
    body = await refreshed(api, lambda request: httpx.Response(200, content=big))
    assert body["models"] == [] and body["last_error"] == "ValueError: the model list is over 2 MiB"

    def chunked(request):  # no Content-Length: the cap applies while streaming
        async def stream():
            for _ in range(10_000):
                yield b" " * 1024
        return httpx.Response(200, content=stream())
    assert (await refreshed(api, chunked))["last_error"] == "ValueError: the model list is over 2 MiB"

    compressed = await refreshed(api, lambda r: httpx.Response(200, content=b"x", headers={"content-encoding": "gzip"}))
    assert compressed["last_error"] == "ValueError: the model list came compressed"


async def test_previous_models_survive_a_bad_refresh(endpoint_server):
    api = endpoint_server
    good = (await api.post("/api/endpoints", json=OLLAMA)).json()
    bad = await refreshed(api, lambda request: httpx.Response(200, json=["not", "a", "dict"]))
    assert bad["last_error"] == "ValueError: the model list is not a JSON object"
    assert bad["models"] == good["models"] and len(good["models"]) == 2


@pytest.mark.parametrize("payload, error", [
    (["a", "b"], "the model list is not a JSON object"),
    ("text", "the model list is not a JSON object"),
    ({"models": {"name": "x"}}, "the model list has no list of models"),
    ({"models": ["qwen3:4b"]}, "a model entry is not a JSON object"),
    ({"models": [{"name": 7}]}, "a model entry has no text id"),
    ({"models": [{"size": 1}]}, "a model entry has no text id"),
])
async def test_malformed_lists_become_the_normal_error(api, payload, error):
    body = await refreshed(api, lambda request: httpx.Response(200, json=payload))
    assert body["last_error"] == f"ValueError: {error}" and body["models"] == []


async def test_not_json_and_deep_nesting_are_errors(api):
    body = await refreshed(api, lambda request: httpx.Response(200, content=b"<html>"))
    assert body["last_error"].startswith("JSONDecodeError")
    deep = await refreshed(api, lambda request: httpx.Response(200, content=b"[" * 200_000))
    assert deep["last_error"] == "ValueError: the model list is nested too deeply"


async def test_rows_capped_before_sorting_and_labels_bounded():
    from types import SimpleNamespace

    from acc.server.api import endpoints
    rows = [{"id": f"m{i:04d}", "display_name": 5} for i in range(2000, 0, -1)]
    rows[0]["display_name"] = "L" * 10_000  # m2000
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": rows}))
    endpoint = SimpleNamespace(kind="openai", base_url="http://x", styles={"openai": "http://x/v1"}, api_key=None)
    models = await endpoints.list_models(endpoint, transport)
    assert len(models) == endpoints.MAX_MODELS
    assert models[0]["id"] == "m1501" and models[-1]["id"] == "m2000"  # the first 500 rows, then sorted
    assert models[-1]["label"] == "L" * endpoints.MAX_LABEL
    assert models[0]["label"] == "m1501"  # a non-text display_name falls back to the id


async def test_slow_server_hits_the_deadline(api, monkeypatch):
    import asyncio

    from acc.server.api import endpoints
    monkeypatch.setattr(endpoints, "LIST_DEADLINE", 0.2)

    def slow(request):
        async def stream():
            while True:
                await asyncio.sleep(0.05)
                yield b" "
        return httpx.Response(200, content=stream())
    body = await refreshed(api, slow)
    assert body["last_error"] == "ValueError: no complete model list within 0.2 s"


@pytest.mark.parametrize("url", ["http://a:b:c", "http://host:99999", "http://host:0", "http://user:pw@host",
                                 "http://host/v1?x=1", "http://host/#frag", "ftp://host", "http://[nope]", "http://-bad-.lan",
                                 "http://host/a b", "http://"])
async def test_endpoint_urls_are_checked_strictly(endpoint_server, url):
    bad = await endpoint_server.post("/api/endpoints", json={**OLLAMA, "base_url": url})
    assert bad.status_code == 422, url


async def test_a_key_over_plain_http_to_another_machine_needs_a_confirmation(endpoint_server):
    api = endpoint_server
    lan = {**OLLAMA, "id": "lan", "base_url": "http://gpu-box.lan:11434", "styles": {"openai": "http://gpu-box.lan:11434/v1"}}
    refused = await api.post("/api/endpoints", json={**lan, "api_key": "sk-x"})
    assert refused.status_code == 422 and "plain http" in refused.text
    assert (await api.post("/api/endpoints", json=lan)).status_code == 201  # no key: nothing to expose
    confirmed = await api.post("/api/endpoints", json={**lan, "id": "lan2", "api_key": "sk-x", "plain_http_ok": True})
    assert confirmed.status_code == 201
    local = await api.post("/api/endpoints", json={**OLLAMA, "id": "loop", "api_key": "sk-x",
                                                   "styles": {"openai": "http://[::1]:11434/v1"}})
    assert local.status_code == 201  # this machine: never crosses the network
