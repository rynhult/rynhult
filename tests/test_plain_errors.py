"""No raw server errors on default paths (UX review 2, beta item 2): the pull request tab and Publishing answer in
words a person can act on, never a 500 or a server path."""
from __future__ import annotations

import httpx
import pytest

from acc.server import pulls
from acc.server.cloudflare import Cloudflare


class Answers:
    """A GitHub client that answers each path with a fixed (status, body)."""

    def __init__(self, answers: dict):
        self.answers = answers

    async def request(self, method, path, body=None, tail=0):
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                return answer
        return 404, {"message": "Not Found"}


@pytest.mark.parametrize("listing", ["<html>proxy error</html>", {"message": "odd"}, [None, "x", {"head": "main"}]])
async def test_find_tolerates_odd_bodies(listing):
    api = Answers({"repos/o/r/pulls?": (200, listing)})
    if isinstance(listing, list):
        assert await pulls.find(api, "o/r", "feature") is None  # nothing usable in it: no pull request
    else:
        with pytest.raises(pulls.PullError, match="unexpected answer") as raised:
            await pulls.find(api, "o/r", "feature")
        assert raised.value.status == 502


async def test_status_with_an_odd_repository_answer_is_a_plain_502():
    api = Answers({"repos/o/r/": (200, []), "repos/o/r": (200, "not json")})
    with pytest.raises(pulls.PullError, match="unexpected answer for the repository"):
        await pulls.status(api, "o/r", "feature")
    api = Answers({"repos/o/r/pulls/7": (200, ["a list"]), "repos/o/r/pulls?": (200, [{"number": 7, "state": "open", "head": {"ref": "feature"}}])})
    with pytest.raises(pulls.PullError, match="unexpected answer for the pull request"):
        await pulls.find(api, "o/r", "feature")


async def test_zones_without_cloudflare_are_a_state_not_an_error(api, env):
    assert not env["settings"].cloudflare_token_path.exists()
    response = await api.get("/api/publish/zones")
    assert response.status_code == 200, response.text
    assert response.json()["configured"] is False and response.json()["zones"] == []


async def test_zones_provider_failures_are_fixed_and_never_echo_responses(api, env):
    token = env["settings"].cloudflare_token_path
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("bad")
    api.app.state.publisher._cloudflare = Cloudflare("bad", transport=httpx.MockTransport(
        lambda request: httpx.Response(403, json={"success": False, "errors": [{"message": "Invalid API Token"}]})))
    response = await api.get("/api/publish/zones")
    assert response.status_code == 502
    assert response.json()['detail'] == "The DNS provider didn't answer. An owner can check its credential in Settings → DNS."
    assert 'Invalid API Token' not in response.text and '/srv' not in response.text
    api.app.state.publisher._cloudflare = Cloudflare("bad", transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("no route"))))
    response = await api.get("/api/publish/zones")
    assert response.status_code == 502
    assert response.json()['detail'] == "The DNS provider didn't answer. An owner can check its credential in Settings → DNS."
    assert 'no route' not in response.text


async def test_a_lone_surrogate_is_a_422_not_a_500(api):
    """JSON can escape half of a UTF-16 pair (New session once cut an emoji topic in half, break-it 9). The default
    422 echoes that input back, which can't be encoded as UTF-8."""
    body = '{"provider": "claude", "topic": "a", "name": "a\\ud83d"}'
    answer = await api.post("/api/sessions/preview", content=body, headers={"content-type": "application/json"})
    assert answer.status_code == 422, answer.text
    assert [item["loc"] for item in answer.json()["detail"]] == [["body", "name"]]
