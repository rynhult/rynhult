# Codex hook trust (2026-10-23)

ACC's package catalog pins **0.163.0**, not 0.164.0: `deploy/packages/acc-packages:51–52` defines the target, and `src/acc/node/packages.py:55–61` imports that same catalog on nodes. `catalog/manifest.toml` contains session policy, not a second CLI version pin. No package target changes in this task.

## Versioned upstream evidence

These links reference OpenAI's release tags, never moving `main`. Both releases support the same trust identity for ACC's plain, non-plugin command hooks. 0.164.0 adds plugin content trust and model-request events, which ACC does not generate.

| Rule | Pinned rust-v0.163.0 | Also reviewed rust-v0.164.0 |
| --- | --- | --- |
| Event labels and `path:event:group:index` key | [hooks/src/lib.rs, lines 95–124](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/lib.rs#L95-L124) | [lines 100–130](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/hooks/src/lib.rs#L100-L130) |
| File path is the key source | [engine/discovery.rs, lines 166–181](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/engine/discovery.rs#L166-L181) | [lines 184–199](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/hooks/src/engine/discovery.rs#L184-L199) |
| Group/handler indices are zero-based, before filtering | [lines 486–509](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/engine/discovery.rs#L486-L509) | [lines 526–544](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/hooks/src/engine/discovery.rs#L526-L544) |
| Normalize command before hashing | [lines 527–568](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/engine/discovery.rs#L527-L568) | [lines 566–607](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/hooks/src/engine/discovery.rs#L566-L607) |
| Timeout: default 600, except SessionEnd/Interrupt default 1 | [lines 744–765](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/engine/discovery.rs#L744-L765), [events/session_end.rs:21](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/events/session_end.rs#L21) | [lines 841–862](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/hooks/src/engine/discovery.rs#L841-L862), [session_end.rs:21](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/hooks/src/events/session_end.rs#L21) |
| Identity: event label, group matcher, just this normalized handler; convert through TOML (absent options omitted) | [lines 768–793](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/engine/discovery.rs#L768-L793), [config/hook_config.rs:153–183](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/config/src/hook_config.rs#L153-L183) | [lines 865–890](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/hooks/src/engine/discovery.rs#L865-L890) |
| Recursively sort JSON keys, compact JSON bytes, SHA-256, lowercase hex with `sha256:` prefix | [config/src/fingerprint.rs:54–65](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/config/src/fingerprint.rs#L54-L65) | [same lines](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/config/src/fingerprint.rs#L54-L65) |
| State contains optional `enabled` and `trusted_hash`; trust requires equal hash | [config/hook_config.rs:27–33](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/config/src/hook_config.rs#L27-L33), [discovery.rs:796–822](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/engine/discovery.rs#L796-L822) | [hook_config.rs:27–40](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/config/src/hook_config.rs#L27-L40) |
| Startup review heading and choices | [tui/src/startup_hooks_review.rs:238–276](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/startup_hooks_review.rs#L238-L276) | [startup_hooks_review.rs](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/tui/src/startup_hooks_review.rs) |

For ACC's minimal group, the bytes for PreToolUse are exactly:

```json
{"event_name":"pre_tool_use","hooks":[{"async":false,"command":"/srv/acc/bin/acc-guard codex PreToolUse","timeout":600,"type":"command"}]}
```

Their digest is `sha256:f58a847bc3ae5e6b700f02389c5ea117a5cecb697334e08ee08a89bbc60f38c4`.

## Real-value verification

`tests/fixtures/codex-hook-trust.json` preserves eight upstream-produced command/hash
pairs captured read-only from a qualified installation. State-key home paths were
normalized to the artificial `/home/alex-agent` for publication; paths are not part
of the command-group hash. All eight hashes reproduce exactly. No unrelated
configuration was copied or changed. The regression compares literal upstream
hashes independently of the implementation.

## Trust boundary and failure behavior

ACC already chooses and writes its hook commands. Persisting trust for those same definitions avoids an unattended startup dialog without approving arbitrary user commands. Match the **entire group** against the exact minimal dictionary ACC generates, including shell-quoted executable paths. A matcher, extra handler, extra field, timeout, changed command or merely similar command never qualifies. Preserve original indices; don't filter/reorder groups. The hash helper supports only these minimal groups, not arbitrary hooks.

Bootstrap probes the actual configured launch executable with `--version` (five-second timeout). Only exact release outputs for 0.163.0 and 0.164.0 qualify. A newer patch, prerelease, failed/missing executable or unknown output writes no trust. A future version requires source review and fixture qualification before extending the allowlist. Normal hook installation still occurs on an unknown version; the person retains Codex's review prompt.

Use ACC's existing TOML approach: retain source text and validate with `tomllib`. Append missing per-key tables; edit only a canonical existing ACC state table's `trusted_hash` scalar, retaining comments and `enabled = false`. Compare the parsed candidate with the original plus exactly the intended updates before replacing the file via a private temporary file. Inline/dotted alternative layouts needing edits, invalid TOML, malformed hooks JSON, and unexpected structures fail closed, with no partial trust write. Other hooks' state and all unrelated settings remain intact. This intentionally prefers a review prompt over risky config rewriting. It does not enable explicitly disabled hooks or bypass Codex's trust checks globally.

The caller's hook path is trusted ACC deployment input, as before. This does not defend against an agent account already able to replace ACC's hook executables or config, and is not an OS security boundary. A compare-before-replace check avoids overwriting an observed concurrent config edit, but is not a cross-process transaction across both files; concurrent external writers can still race. A later changed hook definition has a different Codex hash and needs review.

A remaining review dialog is detected through the existing screen prompt channel. ACC reports **Needs you**, with “Codex is asking to trust hooks; open the session to review” in activity and Inbox, and removes the screen-owned item when the dialog clears. No automated keypress trusts other hooks.
