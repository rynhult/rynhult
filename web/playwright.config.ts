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
  "**/terminal-height.spec.ts",
  "**/terminal-writer.spec.ts",
  "**/terminal-paste-upload.spec.ts",
  "**/shell-polish-3.spec.ts",
  "**/shell-context-spacing.spec.ts",
  "**/shell-launch.spec.ts",
  "**/shell-a11y.spec.ts",
  "**/shell-exception-states.spec.ts",
  "**/shell-u1-primitives.spec.ts",
  "**/shell-u1-adopt.spec.ts",
  "**/shell-palette.spec.ts",
  // Security tests share a real owner authenticator and global registry revisions.
  "**/security-settings.spec.ts",
  // Real machine checks share the installation-wide cooldown.
  "**/86-a11y-rc.spec.ts",
  // Real owner TOTP counters and Security registry revisions are shared.
  "**/72-ux-details.spec.ts",
  // Gate budgets are installation-wide; concurrent repetitions change each other's queue state.
  "**/75-gates.spec.ts",
  "**/perf-projects-overview.spec.ts",
  "**/shell-security-shield.spec.ts",
  "**/shell-polish.spec.ts",
  "**/shell-p1a.spec.ts",
  "**/shell-p1b.spec.ts",
  "**/shell-p1c.spec.ts",
  "**/shell-recent-results.spec.ts",
  "**/hosts-p1.spec.ts",
  "**/shell-inbox.spec.ts",
  "**/settings-p1.spec.ts",
  "**/83-settings-focus.spec.ts",
  "**/93-settings-small.spec.ts",
  "**/settings-authority.spec.ts",
  "**/setup-levels.spec.ts",
  "**/layout-defaults.spec.ts",
  "**/setup-levels-units.spec.ts",
  "**/shell-units.spec.ts",
  "**/continue-resume-check.spec.ts",
  "**/themes-appearance.spec.ts",
  "**/themes.spec.ts",
  "**/shell-p2a.spec.ts",
  "**/layout-store.spec.ts",
  "**/shell-p2b.spec.ts",
  "**/chat-read.spec.ts",
  "**/p4-views.spec.ts",
  "**/shell-project-pages.spec.ts",
  "**/80-admin-features.spec.ts",
  "**/10-app.spec.ts",
  "**/12-files.spec.ts",
  "**/18-terminal-access.spec.ts",
  "**/19-catalog-restore.spec.ts",
  "**/19-updates.spec.ts",
  "**/21-preview.spec.ts",
  "**/21-preview-picker.spec.ts",
  "**/21-tools.spec.ts",
  "**/delegated-approvals.spec.ts",
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
  "**/75-verification.spec.ts",
  "**/89-orch-ux.spec.ts",
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
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  outputDir: `e2e/.results/${PORT}`,  // concurrent fixture servers must not delete each other's traces
  fullyParallel: false,
  // One shared disposable installation: preserve declaration order without failure dependencies.
  workers: 1,
  timeout: 45_000,
  reporter: [["list"]],
  // WebAuthn requires a hostname RP ID; Chromium rejects an IP literal even on loopback.
  // Dark like the wiki's screenshots: the app follows the system theme, and Chromium's default is light.
  use: { baseURL: `http://${environmentValue("ACC_E2E_HOST") ?? "localhost"}:${PORT}`, colorScheme: "dark", trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "setup", testMatch: /00-setup\.spec\.ts/, use: { ...devices["Desktop Chrome"] } },
    // Standalone test-only Vite fixture; the spec owns its loopback server.
    { name: "view-registry", testMatch: /view-registry\.spec\.ts$/, workers: 1,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    // A serial describe cannot exclude another file's workers from installation-wide state.
    { name: "desktop-stateful", dependencies: ["setup"], testMatch: statefulSpecs, workers: 1,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "mobile-stateful", dependencies: ["setup"], testMatch: mobileStatefulSpecs, workers: 1,
      use: { ...devices["Pixel 7"] } },
    // Persistent-terminal qualification uses the normal harness/config, one file per focused run.
    { name: "terminal-chromium", dependencies: ["setup"], workers: 1,
      testMatch: /(?:termfull|terminal-input|terminal-polling|terminal-host|terminal-wheel)\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "terminal-webkit", dependencies: ["setup"], workers: 1,
      metadata: { presentationIsolation: true }, testMatch: /terminal-height\.spec\.ts$/,
      use: { ...devices["Desktop Safari"], viewport: { width: 1440, height: 900 } } },
    // Orchestrator v2 and acknowledged delivery are default-on. Include their real
    // spool/brief/adversarial cases here; focused configs remain available for isolated reruns.
    { name: "desktop", dependencies: ["setup"], testIgnore: [/mobile|00-setup\.spec\.ts/, /csrf\.spec\.ts$/, /view-registry\.spec\.ts$/, /(?:termfull|terminal-input|terminal-polling|terminal-host|terminal-wheel)\.spec\.ts$/, ...statefulSpecs],
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "mobile", testMatch: [/mobile/, /44-quality-routes\.spec\.ts/], testIgnore: mobileStatefulSpecs,
      use: { ...devices["Pixel 7"] }, dependencies: ["setup"] },
  ],
  webServer: {
    command: `cd .. && ${PYTHON} tests/e2e_server.py ${E2E_BASE} ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },  // lets the server kill its tmux test server
    timeout: 60_000,
  },
});
