import { environmentValue } from "../src/acc/env.mjs";
import { defineConfig, devices } from "@playwright/test";

const PORT = Number(environmentValue("ACC_E2E_PORT") ?? 18741);
// Each run gets a fresh state dir under this base; tests find it via <base>/info.json. ACC_E2E_BASE and ACC_E2E_PYTHON
// let a run from another checkout or account (e.g. an agent's worktree) use its own folder and Python.
export const E2E_BASE = environmentValue("ACC_E2E_BASE") ?? "/tmp/acc-e2e";
const PYTHON = environmentValue("ACC_E2E_PYTHON")
  ? `PYTHONPATH=src ${environmentValue("ACC_E2E_PYTHON")}` : "uv run python";
process.env.ACC_E2E_DEVICE_CA = environmentValue("ACC_E2E_DEVICE_CA") ?? "1";
process.env.ACC_E2E_REMOTE_EDGE = environmentValue("ACC_E2E_REMOTE_EDGE") ?? "1";

const statefulSpecs = [
  "**/80-admin-features.spec.ts",
  "**/10-app.spec.ts",
  "**/12-files.spec.ts",
  "**/18-terminal-access.spec.ts",
  "**/19-catalog-restore.spec.ts",
  "**/19-updates.spec.ts",
  "**/21-preview.spec.ts",
  "**/21-preview-picker.spec.ts",
  "**/21-tools.spec.ts",
  "**/22-pull-request.spec.ts",
  "**/22-risk-labels.spec.ts",
  "**/23-breakit.spec.ts",
  "**/23-groups.spec.ts",
  "**/29-first-run.spec.ts",
  "**/35-terminal-output.spec.ts",
  "**/40-dns.spec.ts",
  "**/41-agent-steps.spec.ts",
  "**/41-containers.spec.ts",
  "**/87-break-it-10.spec.ts",
  "**/41-status-board.spec.ts",
  "**/43-telemetry.spec.ts",
  "**/44-passkeys.spec.ts",
  "**/46-owner-invite-verification.spec.ts",
  "**/50-ux-final-containment.spec.ts",
  "**/55-break-it-2-chaos.spec.ts",
  "**/61-publish-addresses.spec.ts",
  "**/63-device-pairing.spec.ts",
  "**/64-remote-access-apply.spec.ts",
  "**/67-break-it-3-chaos.spec.ts",
  "**/67-break-it-3-pairing-mismatch.spec.ts",
  "**/67-break-it-3-remote-conflict.spec.ts",
  "**/90-logout.spec.ts",
];
const mobileStatefulSpecs = [
  "**/35-terminal-output-mobile.spec.ts",
  "**/38-polish-mobile.spec.ts",
  "**/50-ux-final-containment-mobile.spec.ts",
];

export default defineConfig({
  testDir: "e2e",
  // Secure-cookie proofs need the separate HTTPS harness; plain HTTP has no __Host- cookies.
  testIgnore: /csrf\.spec\.ts$/,
  globalTeardown: "./e2e/global-teardown.ts",
  outputDir: `e2e/.results/${PORT}`,  // concurrent fixture servers must not delete each other's traces
  fullyParallel: false,
  workers: 4,
  timeout: 45_000,
  reporter: [["list"]],
  // WebAuthn requires a hostname RP ID; Chromium rejects an IP literal even on loopback.
  // Dark like the wiki's screenshots: the app follows the system theme, and Chromium's default is light.
  use: { baseURL: `http://${environmentValue("ACC_E2E_HOST") ?? "localhost"}:${PORT}`, colorScheme: "dark", trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "setup", testMatch: /00-setup\.spec\.ts/, use: { ...devices["Desktop Chrome"] } },
    // A serial describe cannot exclude another file's workers from installation-wide state.
    { name: "desktop-stateful", dependencies: ["setup"], testMatch: statefulSpecs, workers: 1,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "mobile-stateful", dependencies: ["desktop-stateful"], testMatch: mobileStatefulSpecs, workers: 1,
      use: { ...devices["Pixel 7"] } },
    // Orchestrator v2 and acknowledged delivery are default-on. Include their real
    // spool/brief/adversarial cases here; focused configs remain available for isolated reruns.
    { name: "desktop", dependencies: ["mobile-stateful"], testIgnore: [/mobile|00-setup\.spec\.ts/, /csrf\.spec\.ts$/, ...statefulSpecs],
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "mobile", testMatch: [/mobile/, /44-quality-routes\.spec\.ts/], testIgnore: mobileStatefulSpecs,
      use: { ...devices["Pixel 7"] }, dependencies: ["mobile-stateful"] },
  ],
  webServer: {
    command: `cd .. && ${PYTHON} tests/e2e_server.py ${E2E_BASE} ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },  // lets the server kill its tmux test server
    timeout: 60_000,
  },
});
