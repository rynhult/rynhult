import { environmentValue } from "../../src/acc/env.mjs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { E2E_BASE } from "../playwright.config";

export default function teardown() {
  const python = environmentValue("ACC_E2E_PYTHON") ?? fileURLToPath(new URL("../../.venv/bin/python", import.meta.url));
  const cleanup = fileURLToPath(new URL("../../tests/owned_tmux.py", import.meta.url));
  const gitCleanup = fileURLToPath(new URL("../../tests/owned_git_http.py", import.meta.url));
  const errors: unknown[] = [];
  for (const script of [cleanup, gitCleanup]) {
    try { execFileSync(python, [script, E2E_BASE], { timeout: 30_000, stdio: "inherit" }); }
    catch (error) { errors.push(error); }
  }
  if (errors.length) throw new AggregateError(errors, "E2E process cleanup failed");
}
