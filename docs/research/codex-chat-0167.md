# Codex 0.167.1 native Chat format review (2026-11-11)

The account-local Chat reader remains qualified only for Codex 0.163.0.
The 0.167.1 hook qualification and catalog pin do not qualify its native Chat
history. Source comparison found changes, so the version gate is retained:
0.167.1 returns `unsupported` with no rendered items.

Compared OpenAI's tagged `rust-v0.163.0` and `rust-v0.167.1` sources as data
under `/tmp/codex-pin-0167-source` (the older transcript sources are under
`baseline-0163`). No downloaded code or vendor CLI was executed.

- The [0.167.1 rollout wire enum](https://github.com/openai/codex/blob/rust-v0.167.1/codex-rs/history/src/rollout_payload.rs#L29-L88)
  adds `DynamicToolsUpdated` / `dynamic_tools_updated` relative to the
  [0.163.0 enum](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/history/src/rollout_payload.rs#L29-L75).
  The current reader does not classify this variant as an opaque record.
- [0.167.1 message metadata](https://github.com/openai/codex/blob/rust-v0.167.1/codex-rs/protocol/src/models.rs#L1003-L1024)
  adds positional `content_item_metadata`, absent in the
  [0.163.0 definition](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/protocol/src/models.rs#L974-L992).
  Its [provenance types](https://github.com/openai/codex/blob/rust-v0.167.1/codex-rs/protocol/src/models/item_metadata.rs#L61-L115)
  distinguish ordinary user input, harness/hook context, instructions and tool
  sources independently of role. The reader currently uses `content_item_kinds`
  and qualified wrappers; it does not interpret this new attribution.
- Basic [message fields](https://github.com/openai/codex/blob/rust-v0.167.1/codex-rs/protocol/src/models.rs#L1072-L1096)
  (`role`, `content`, `id`, passthrough metadata), text content types and the
  [newline JSON writer](https://github.com/openai/codex/blob/rust-v0.167.1/codex-rs/rollout/src/recorder.rs#L2127-L2162)
  remain structurally compatible. That is narrower than unchanged transcript
  semantics and does not establish full reader compatibility.

`tests/fixtures/codex-chat-0167-unsupported.jsonl` contains source-derived minimal
record fragments for the new variant and provenance metadata, with artificial
identifiers/text. It is not a live transcript. The regression verifies the exact
0.167.1 version stays unsupported and suppresses all message text. A future
qualification needs explicit classification of the added records and review of
user/context attribution with fixtures before broadening the version gate.
