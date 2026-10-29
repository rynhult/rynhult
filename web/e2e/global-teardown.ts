import { environmentValue } from "../../src/acc/env.mjs";
import { execFileSync } from "node:child_process";
import { E2E_BASE } from "../playwright.config";

export default function teardown() {
  execFileSync(environmentValue("ACC_E2E_PYTHON") ?? "../.venv/bin/python", ["../tests/owned_tmux.py", E2E_BASE],
    { timeout: 30_000, stdio: "inherit" });
}
