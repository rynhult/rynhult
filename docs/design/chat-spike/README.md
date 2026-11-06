# Fixture-only transcript following

From the repository root:

```sh
PYTHONPATH=src .venv/bin/python docs/design/chat-spike/replay.py
```

Use the project's Python environment; no new dependencies are needed. The script creates its own temporary directory, writes synthetic Claude/Codex JSONL records on a background thread and reads each append with Rynhult's existing transcript parser. Assertions cover an incomplete JSON/UTF-8 append and two deliberately identical assistant messages. It prints only fixed example messages and removes the temporary data on exit. There are no file-path arguments, account discovery, provider launches or live service calls.

This is a feasibility replay, not a production incremental follower or a real CLI qualification. It rereads snapshots, synchronises fixture writes with reads and does not exercise rotation, compaction, streaming tool records, redaction, authorisation, node routing, image ingestion or sending. Those acceptance cases and the proposed bounded cursor architecture are in [the design](../chat-view.md).

## P3a: native on-disk evidence (2026-11-01)

The separate `native-fixtures/` files came from **installed Claude Code 2.1.308
and Codex 0.163.0**, not hand-authored transcript records. Read them offline:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python docs/design/chat-spike/inspect_native.py
```

This command never starts a provider or discovers any account files. It checks
fixture digests/counts, existing text-parser output, deliberately identical turns,
Claude UUID/session/version continuity and Codex turn/item/session identity.
It does not test the proposed incremental cursor or live input implementation.

Capture used a new `/tmp/chat-p3a-research/{claude,codex}` home and empty workspace
for each provider. The process environment was constructed from scratch: PATH
`/usr/bin:/bin`, fresh HOME/XDG config/cache, fresh CODEX_HOME/CLAUDE_CONFIG_DIR,
`DISABLE_TELEMETRY=1`, `DISABLE_AUTOUPDATER=1`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. Dummy API keys were the fixed
non-secret `fixture-only`. ANTHROPIC_BASE_URL and the Codex fixture provider URL
pointed only to an owned `127.0.0.1` ephemeral-port HTTP server. NO_PROXY permitted
that address; HTTP_PROXY/HTTPS_PROXY/ALL_PROXY pointed at the unused loopback
port 1 so accidental proxy-aware external requests failed. This was not an
OS-level network sandbox or a claim about all possible CLI network traffic.
No real provider credential, live transcript, Rynhult session or account config
was read. No tool calls were returned by the fake model.

The fake service returned Anthropic message-start/text-block/text-delta/block-stop/
message-delta/message-stop SSE events and Responses created/item-added/text-delta/
item-done/completed SSE events. Each completion's only text was
`Fixture reply é 🌲`; prompt was `Fixture question é 🌲`. The model reused
`msg_fixture` deliberately. The request handler discarded request bodies after
reading them; it did not persist native system instructions. No real inference
or model entitlement was tested.

Exact native command shapes (placeholders refer **only to this disposable
fixture**, never run with a live session ID or inherited home):

```text
claude -p --session-id <fresh-fixture-uuid> --output-format stream-json
  --verbose --tools "" --model claude-sonnet-4-6 'Fixture question é 🌲'
claude -p --resume <same-fixture-uuid> --output-format stream-json
  --verbose --tools "" --model claude-sonnet-4-6 'Fixture question é 🌲'

codex exec --skip-git-repo-check --ignore-user-config --sandbox read-only --json
  -c 'model_provider="fixture"'
  -c 'model_providers.fixture.name="fixture"'
  -c 'model_providers.fixture.base_url="http://127.0.0.1:<owned-port>/v1"'
  -c 'model_providers.fixture.wire_api="responses"'
  -c 'model_providers.fixture.requires_openai_auth=false'
  -c 'model_providers.fixture.supports_websockets=false'
  --model gpt-5.4 'Fixture question é 🌲'
```

Codex resume kept these options and replaced the final prompt argument with
`resume <thread_id from first stdout event> 'Fixture question é 🌲'`.
CODEX_HOME must exist before starting: an initial probe refused a missing
fixture directory; after creating it, both initial and resumed runs exited 0.
Claude initial/resume both exited 0. Native processes exited before resume;
this experiment never ran two writers against one live conversation.

The committed files are **projections**. Claude retains visible user/assistant
records. Codex retains session identity/version metadata, selected turn metadata,
canonical user/assistant records (including one injected environment record to
exercise filtering), and task boundaries. Full API request bodies, system
instructions, usage/config and diagnostics are omitted. Temporary path prefixes
become `/tmp/rynhult-chat-fixture`; generated IDs/timestamps remain for identity
inspection. The manifest records raw source digest/count/types as provenance and
projected-file digest/count for reproducible offline checks. The raw capture and
scratch homes were deleted; no full account directory is committed.

These print/exec fixtures are real native format observations **but not TUI
qualification**. Rotation, compaction, tool records, rich streaming, images,
provider authentication, flush latency, nodes/containers, new Chat authorization,
input and model transitions remain gates in [the adapter specification](../chat-p3a-adapters.md).
