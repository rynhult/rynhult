import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, routeDemoSessions, wikiShotPath } from "./helpers";
import type { ProviderInfo } from "../src/lib/types";

test.use({ storageState: AUTH_FILE });

// UX review PV-1: a provider's brand mark is a square, never a round (status-looking) dot.
test("provider marks are squares, not status dots", async ({ page }) => {
  await routeDemoSessions(page);
  await page.goto("/sessions");
  const mark = page.locator("[data-provider-mark]:visible").first();  // earlier specs can leave multi-runs, whose rows start collapsed
  await expect(mark).toBeVisible();
  expect(await mark.evaluate((el) => getComputedStyle(el).borderRadius)).toBe("2px");
  await page.goto("/providers");
  await expect(page.locator("[data-provider-mark]:visible").first()).toBeVisible();
});

// PV-2: a CLI Rynhult hasn't checked says so, a failed check says so, and "Checking…" is only for a check under way.
test("providers: explicit check states instead of an open-ended Checking…", async ({ page }) => {
  const now = Date.now() / 1000;
  let fail = false;
  await page.route("**/api/accounts*", (route) => fail ? route.fulfill({ status: 503, json: { detail: "check unavailable" } })
    : route.fulfill({ json: { checked_at: now, account: "agent", accounts: ["agent"], default_account: "agent",
      claude: { logged_in: true, email: "alex@example.com" }, codex: { logged_in: false } } }));
  await page.goto("/providers");
  await expect(page.getByText("Logged in").first()).toBeVisible();
  await expect(page.getByText("Not checked yet").first()).toBeVisible();
  await expect(page.getByText("Checking…")).toHaveCount(0);
  fail = true;
  await page.reload();
  await expect(page.getByText("Couldn't check").first()).toBeVisible({ timeout: 15_000 });
});

for (const width of [1440, 390]) for (const theme of ["dark", "light"] as const) {
  test(`Providers keeps exceptions visible and inventories optional at ${width}px in ${theme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.emulateMedia({ colorScheme: theme });
    const now = Date.now() / 1000;
    await page.route("**/api/providers/registry", async route => {
      const response = await route.fetch();
      const data: { providers: ProviderInfo[] } = await response.json();
      await route.fulfill({ response, json: { ...data, providers: data.providers.map(provider =>
        provider.id === "copilot" ? { ...provider, installed: false, usage_url: "https://example.com/billing" } : provider) } });
    });
    await page.route("**/api/accounts*", route => route.fulfill({ json: {
      account: "agent", default_account: "agent", accounts: ["agent"], checked_at: now,
      claude: { logged_in: true, email: "alex@example.com", subscription: "Pro", login_expires_at: now + 86400 },
      codex: { logged_in: false, error: "Sign-in expired" }, github: {},
    } }));
    await page.route("**/api/providers", route => route.fulfill({ json: {
      observations: { "claude.rate_limits": { value: { five_hour: { used_percentage: 38 }, seven_day: { used_percentage: 61 } },
        source: "statusline", observed_at: now } }, links: {}, not_exposed: ["No combined percentage across providers"],
    } }));
    await page.route("**/api/models", route => route.fulfill({ json: {
      fetched_at: now, claude: [{ id: "example-model", label: "Example model" }],
    } }));
    await page.route("**/api/packages?*", route => route.fulfill({ json: {
      helper: true, reported: true, kind: "local", pending: [], packages: {
        claude: { label: "Claude Code", installed: "2.1.300", latest: "2.1.300", method: "apt", auto_update: false },
        codex: { label: "Codex", installed: "0.163.0", latest: "0.164.0", method: "npm",
          last: { action: "check", ok: false, detail: "Version check failed", at: now } },
      },
    } }));
    await page.goto("/providers");
    await expect(page.getByText("Logged in", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("searchbox", { name: "Filter models" })).not.toBeVisible();
    await expect(page.getByRole("radiogroup", { name: "Agent account" })).toHaveCount(0);
    const claude = page.getByRole("article", { name: "Claude Code account", exact: true });
    await expect(claude.getByText("alex@example.com", { exact: true })).not.toBeVisible();
    await expect(claude.getByText(/^Login expires /)).toBeVisible();
    await expect(claude.getByText(/Session statusline \(official\)/)).toBeVisible();
    await expect(page.getByRole("article", { name: "Codex account", exact: true }).getByText("Sign-in expired")).toBeVisible();
    await expect(page.getByText("Not checked yet").first()).toBeVisible();
    await expect(page.getByText(/Version check failed/)).toBeVisible();
    const unavailable = page.getByRole("article", { name: "GitHub Copilot CLI account", exact: true });
    await expect(unavailable.getByText("Not installed for agent", { exact: true })).toBeVisible();
    await expect(unavailable.getByRole("button", { name: "Install", exact: true })).toBeEnabled();
    // Each provider's software sits on its own tile (UX follow-up to W11): no separate install section.
    await expect(page.locator("#provider-software")).toHaveCount(0);
    await expect(claude.getByRole("group", { name: "Claude Code software" }).getByRole("button", { name: "Check Claude Code for updates" })).toBeVisible();
    await expect(claude.getByRole("checkbox", { name: "Auto-update" })).toBeVisible();
    await expectAxeClean(page);
    if (width === 1440 && theme === "dark") {
      await claude.screenshot({ animations: "disabled", path: wikiShotPath(testInfo, "providers-software.png") });
    }
    const details = claude.locator("summary");
    await details.focus();
    await page.keyboard.press("Enter");
    await expect(claude.getByText("alex@example.com", { exact: true })).toBeVisible();
    await unavailable.locator("summary").click();
    await expect(unavailable.getByRole("link", { name: "Usage and billing" })).toBeVisible();
    await page.getByRole("group", { name: "Provider sections" }).getByRole("link", { name: "Available models" }).click();
    await expect(page.getByRole("searchbox", { name: "Filter models" })).toBeVisible();
    await page.getByRole("searchbox", { name: "Filter models" }).fill("example");
    await expect(page.getByText("Example model", { exact: true })).toBeVisible();
    if (width === 1440 && theme === "dark") {
      await page.locator("#provider-models").screenshot({ animations: "disabled", path: wikiShotPath(testInfo, "providers-models.png") });
    }
    await page.locator("#provider-models > details > summary").click();
    await expect(page.getByRole("searchbox", { name: "Filter models" })).not.toBeVisible();
    await page.getByRole("group", { name: "Provider sections" }).getByRole("link", { name: "Available models" }).click();
    await expect(page.getByRole("searchbox", { name: "Filter models" })).toBeVisible();
    await expect(page.getByRole("group", { name: "Provider sections" }).getByRole("link", { name: "Agent software" })).toHaveCount(0);
    await expectAxeClean(page);
    expect(await page.getByRole("main").evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(0);
  });
}

test("a failed software action stays on its provider's tile, and an old software link opens the sign-ins", async ({ page }) => {
  await page.route("**/api/packages?*", route => route.fulfill({ json: {
    helper: true, reported: true, kind: "local", pending: [], packages: {
      claude: { label: "Claude Code", installed: "2.1.300", latest: "2.1.300", method: "apt" },
    },
  } }));
  let release!: () => void;
  const checked = new Promise<void>(resolve => { release = resolve; });
  let refused = true;
  await page.route("**/api/packages/claude/check", async route => {
    await checked;
    await route.fulfill(refused ? { status: 403, json: { detail: "Example request refused" } } : { json: { ok: true } });
  });

  try {
    await page.goto("/providers#provider-software");
    await expect(page.locator("#provider-sign-ins")).toBeFocused();
    const claude = page.getByRole("article", { name: "Claude Code account", exact: true });
    await claude.getByRole("button", { name: "Check Claude Code for updates" }).click();
    release();
    await expect(claude.getByText("Example request refused", { exact: true })).toBeVisible();
    refused = false;
    await claude.getByRole("button", { name: "Check Claude Code for updates" }).click();
    await expect(page.getByText("Example request refused", { exact: true })).toHaveCount(0);
  } finally {
    release();
  }
});

test("another agent account never borrows the default account's plan or usage", async ({ page }) => {
  await page.route("**/api/accounts*", route => {
    const account = new URL(route.request().url()).searchParams.get("account") ?? "agent";
    return route.fulfill({ json: { account, default_account: "agent", accounts: ["agent", "sam-agent"],
      checked_at: Date.now() / 1000, codex: { logged_in: true, plan: account === "agent" ? "Pro" : "Team" } } });
  });
  await page.route("**/api/providers", route => route.fulfill({ json: {
    observations: { "codex.rate_limits": { value: { plan: "Pro", primary: { window_minutes: 300, used_percent: 38 } },
      source: "app-server", observed_at: Date.now() / 1000 } }, links: {}, not_exposed: [],
  } }));
  await page.goto("/providers");
  const codex = page.getByRole("article", { name: "Codex account", exact: true });
  await expect(codex.getByText("38%", { exact: true })).toBeVisible();
  await page.getByRole("radio", { name: "sam-agent", exact: true }).click();
  await expect(codex.getByText("Team", { exact: true })).toBeVisible();
  await expect(codex.getByText("38%", { exact: true })).not.toBeVisible();
  await expect(codex.getByText(/Plan usage belongs to agent/)).toBeVisible();
});

test("model catalog failures stay visible while its inventory is closed", async ({ page }) => {
  await page.route("**/api/models", route => route.fulfill({ status: 503, json: { detail: "Catalog unavailable" } }));
  await page.goto("/providers");
  await expect(page.getByText("Couldn't read the model catalog.", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("searchbox", { name: "Filter models" })).not.toBeVisible();
});

async function missingSoftware(page: import('@playwright/test').Page, note?: string) {
  // Resolve fixture metadata before the browser can cancel a registry request.
  const registryResponse = await page.request.get('/api/providers/registry');
  expect(registryResponse.ok()).toBe(true);
  const data = await registryResponse.json();
  await page.route('**/api/providers/registry', route => route.fulfill({ json: {
    ...data, providers: data.providers.map((p: ProviderInfo) => p.id === 'codex' ? { ...p, installed: false } : p),
  } }));
  await page.route('**/api/accounts*', route => route.fulfill({ json: {
    account: 'agent', default_account: 'agent', accounts: ['agent'], checked_at: Date.now() / 1000,
    codex: { logged_in: false },
  } }));
  await page.route('**/api/packages?*', route => route.fulfill({ json: {
    helper: true, reported: true, kind: 'helper', pending: [],
    packages: { codex: { label: 'Codex', installed: null, installable: true, method: 'npm', note } },
  } }));
}

for (const theme of ['dark', 'light'] as const) test(`Missing provider tile preserves warnings and install refusals in ${theme}`, async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: theme });
  await missingSoftware(page, 'Example software needs owner attention before installing.');
  await page.route('**/api/packages/codex/install*', route => route.fulfill({ status: 403,
    json: { detail: 'Example install refused for this machine' } }));
  await page.goto('/providers');
  const codex = page.getByRole('article', { name: 'Codex account', exact: true });
  await expect(codex.getByText('Example software needs owner attention before installing.', { exact: true })).toBeVisible();
  await codex.getByRole('button', { name: 'Install', exact: true }).click();
  await expect(codex.getByRole('alert')).toContainText('Example install refused for this machine');
  await codex.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `../.acc/artifacts/ux-followups-review/providers-${theme}-390.png` });
  await expectAxeClean(page);
});

test('Provider install state belongs to the selected machine', async ({ page }) => {
  await missingSoftware(page);
  const targets: string[] = [];
  await page.route('**/api/packages/codex/install*', route => {
    targets.push(new URL(route.request().url()).searchParams.get('machine') ?? 'local');
    return route.fulfill({ status: 202, json: { ok: true } });
  });
  await page.goto('/providers');
  const codex = page.getByRole('article', { name: 'Codex account', exact: true });
  // Packages can render a separate Install before the registry identifies the missing-provider tile.
  await expect(codex.getByText('Not installed for agent', { exact: true })).toBeVisible();
  await codex.getByRole('button', { name: 'Install', exact: true }).click();
  await expect(codex.getByRole('button', { name: 'Installing…', exact: true })).toBeDisabled();
  // Router transition keeps Providers mounted, as the machine picker does.
  await page.evaluate(() => {
    history.pushState(history.state, '', '/providers?machine=lab-box');
    dispatchEvent(new PopStateEvent('popstate', { state: history.state }));
  });
  await expect(codex.getByRole('button', { name: 'Install', exact: true })).toBeEnabled();
  await codex.getByRole('button', { name: 'Install', exact: true }).click();
  await expect.poll(() => targets).toEqual(['local', 'lab-box']);
});

test('A missing provider reports an install action error on its own tile', async ({ page }) => {
  await missingSoftware(page);
  await page.route('**/api/packages/codex/install*', route => route.fulfill({ status: 403,
    json: { detail: 'Example install permission refused' } }));
  await page.goto('/providers');
  const codex = page.getByRole('article', { name: 'Codex account', exact: true });
  await expect(codex.getByText('Not installed for agent', { exact: true })).toBeVisible();
  await codex.getByRole('button', { name: 'Install', exact: true }).click();
  await expect(codex.getByRole('alert')).toContainText('Example install permission refused');
});

test('Software controls retain machine scope and account-specific reads', async ({ page }) => {
  await page.route('**/api/accounts*', route => {
    const account = new URL(route.request().url()).searchParams.get('account') ?? 'agent';
    return route.fulfill({ json: { account, default_account: 'agent', accounts: ['agent', 'sam-agent'],
      checked_at: Date.now() / 1000, codex: { logged_in: false } } });
  });
  const reads: string[] = [];
  let auto = false;
  await page.route('**/api/packages?*', route => {
    reads.push(new URL(route.request().url()).searchParams.get('account') ?? '');
    return route.fulfill({ json: { helper: true, reported: true, kind: 'local', pending: [], packages: {
      codex: { label: 'Codex', installed: '0.163.0', latest: '0.164.0', method: 'npm', auto_update: auto,
        note: 'Example pinned release needs qualification before advancing.' },
    } } });
  });
  const actions: { action: string; machine: string | null; body: unknown }[] = [];
  await page.route('**/api/packages/codex/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    actions.push({ action: url.pathname.split('/').pop()!, machine: url.searchParams.get('machine'),
      body: request.postDataJSON() });
    if (url.pathname.endsWith('/auto')) auto = request.postDataJSON().enabled;
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/providers');
  await page.getByRole('radio', { name: 'sam-agent', exact: true }).click();
  await expect.poll(() => reads.includes('sam-agent')).toBe(true);
  const tile = page.getByRole('article', { name: 'Codex account', exact: true });
  await expect(tile.getByText('Example pinned release needs qualification before advancing.')).toBeVisible();
  // The controlled value changes after the server acknowledges and the query refreshes.
  await tile.getByRole('checkbox', { name: 'Auto-update' }).click();
  await expect.poll(() => actions).toEqual([{ action: 'auto', machine: null, body: { enabled: true } }]);
  await expect(tile.getByRole('checkbox', { name: 'Auto-update' })).toBeChecked();
  await page.goto('/providers?machine=studio-mac');
  await expect(tile.getByRole('group', { name: 'Codex software' })).toBeVisible();
  await expect(tile.getByRole('checkbox', { name: 'Auto-update' })).toHaveCount(0);
  await tile.getByRole('button', { name: 'Update', exact: true }).click();
  await expect.poll(() => actions.map(({ action, machine }) => ({ action, machine }))).toEqual([
    { action: 'auto', machine: null }, { action: 'update', machine: 'studio-mac' },
  ]);
});

// Node GitHub CLI prerequisites: keep the broker's machine-specific remedy visible.
test("GitHub login displays the missing node CLI remedy", async ({ page }) => {
  await page.route("**/api/accounts*", route => route.fulfill({ json: {
    account: "agent", default_account: "agent", accounts: ["agent"], github: { logged_in: false },
  } }));
  const detail = "gh is not installed on alex-pc: rerun the Linux/WSL node installer with --install-prereqs, then retry GitHub login.";
  await page.route("**/api/accounts/github/login*", route => route.fulfill({ status: 400, json: { detail } }));
  await page.goto("/providers");
  await page.getByRole("article", { name: "GitHub account", exact: true }).getByRole("button", { name: "Log in", exact: true }).click();
  await page.getByRole("button", { name: "Start re-login", exact: true }).click();
  await expect(page.getByRole("dialog").getByText(detail, { exact: true })).toBeVisible();
});
