# Provider CLI update targets

Context: ACC's locked application build did not reproduce npm provider versions:
installation and the daily helper selected upstream latest while docs called Codex pinned.

Decision: use exact catalog targets for Codex and Copilot, shared by Linux/WSL and
Mac. Retain OpenCode's documented supported line and minimum. Report upstream
availability separately from the install target. Require explicit owner opt-in for
rolling vendor automatic updates, resetting legacy implicit rolling defaults once.
Record provider qualification before advancing catalog pins or supported lines.

Consequences: upstream npm releases no longer change the default runtime independently
of ACC. Package notes and state preserve target/channel/upstream provenance. Operators
can still replace executables manually; daily updates never downgrade newer npm CLIs.
Rolling opt-in accepts independent compatibility changes.

Alternatives: documentation-only correction leaves the reported reliability defect;
pinning all vendor installers without qualification would claim unsupported reproducibility.
A configurable arbitrary npm version or latest override adds an unnecessary privileged input.
