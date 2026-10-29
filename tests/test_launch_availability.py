"""New-session data must come from the project's execution account and host."""
import pytest
from helpers import write_db
from test_ownership import two  # noqa: F401


async def test_provider_and_model_queries_follow_project_execution_scope(api, project, monkeypatch):
    from acc.server.models import Project

    with write_db(api.app.state.db) as s:
        row = s.get(Project, project["id"])
        row.host, row.account = "lab-box", "alex-agent"
        s.commit()

    async def call(method, *, account=None, host=None, **params):
        if (account, host) != ("alex-agent", "lab-box"):
            return {"claude": True, "codex": False} if method == "installed_providers" else {"codex": []}
        return ({"claude": False, "codex": True} if method == "installed_providers"
                else {"codex": [{"id": "node-model", "label": "Node model"}]})

    monkeypatch.setattr(api.app.state.ctx.broker, "call", call)
    response = await api.get("/api/providers/registry", params={"project_id": project["id"]})
    assert response.status_code == 200
    installed = {p["id"]: p["installed"] for p in response.json()["providers"]}
    assert installed["codex"] is True and installed["claude"] is False
    response = await api.get("/api/models", params={"project_id": project["id"]})
    assert response.json()["codex"] == [{"id": "node-model", "label": "Node model", "image_input": None,
        "image_qualification": {"scope": "native", "status": "unknown", "checked_at": None, "evidence": None}}]


@pytest.mark.parametrize("route", ["/api/providers/registry", "/api/models"])
async def test_launch_scope_cannot_probe_someone_elses_project(two, route):  # noqa: F811
    response = await two["member"].get(route, params={"project_id": two["project"]["id"]})
    assert response.status_code == 404
