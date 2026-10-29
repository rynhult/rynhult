import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, verifyAgain, wikiShotPath } from "./helpers";

// Organizations phase 1. The e2e server runs with organizations mode on; company sign-in itself stays off unless a
// test turns it on. No identity provider is reachable here, so "Test connection" is answered by a route.

test.describe("owner settings", () => {
  test.use({ storageState: AUTH_FILE });

  test("Company sign-in: configure, test, add a member and make a sign-in link", async ({ page }, testInfo) => {
    await page.route("**/api/organizations/oidc/test", (route) => route.fulfill({ json: {
      ok: true, issuer: "https://login.example.com/11111111-2222-3333-4444-555555555555/v2.0", keys: 2,
      authorization_endpoint: "https://login.example.com/authorize", token_endpoint: "https://login.example.com/token" } }));
    await page.goto("/settings#company-sign-in");
    const card = page.locator("#company-sign-in");
    await expect(card.getByRole("heading", { name: "Company sign-in" })).toBeVisible();
    await expect(card.getByText("Off", { exact: true })).toBeVisible();
    await expect(card.getByText(/\/api\/auth\/oidc\/callback/)).toBeVisible();
    await card.getByRole("textbox", { name: "Issuer", exact: true }).fill("https://login.example.com/11111111-2222-3333-4444-555555555555/v2.0");
    await card.getByRole("textbox", { name: "Sign-in client ID", exact: true }).fill("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee");
    await card.getByLabel("Sign-in client secret", { exact: true }).fill("example-secret-value");
    await expect(card.getByRole("spinbutton", { name: "Company sign-in lasts (hours)" })).toHaveValue("12");
    await card.getByRole("spinbutton", { name: "Company sign-in lasts (hours)" }).fill("8");
    const enrollment = card.getByRole("checkbox", { name: /Let members add a passkey after a multi-factor company sign-in/ });
    await expect(enrollment).toBeChecked();
    await card.getByRole("textbox", { name: "acr values that prove multi-factor", exact: true }).fill("c2 urn:example:loa:mfa");
    // Counting more acr values as MFA relaxes sign-in: it needs a fresh verification, as relaxing passkeys does.
    await card.getByRole("button", { name: "Save" }).click();
    await expect(card.getByRole("alert")).toContainText("Verify your sign-in again");
    await verifyAgain(page);
    await card.getByRole("button", { name: "Save" }).click();
    await expect(card.getByText(/Saved, encrypted with the vault's key/)).toBeVisible();
    await expect(card.getByLabel("Sign-in client secret", { exact: true })).toHaveValue("");
    await card.getByRole("button", { name: "Test connection" }).click();
    await expect(card.getByText(/Connected: discovery and 2 signing keys/)).toBeVisible();

    await card.getByRole("textbox", { name: "Username", exact: true }).first().fill("robin");
    await card.getByRole("textbox", { name: "Subject", exact: true }).fill("AbCdEf0123456789-robin");
    await card.getByRole("textbox", { name: "Agent account", exact: true }).first().fill("robin-agent");
    await card.getByRole("button", { name: "Add member" }).click();
    await expect(card.getByText("AbCdEf0123456789-robin")).toBeVisible();

    await card.getByRole("textbox", { name: "Username", exact: true }).nth(1).fill("sam");
    await card.getByRole("textbox", { name: "Agent account", exact: true }).nth(1).fill("sam-agent");
    await card.getByRole("textbox", { name: "Company account", exact: true }).fill("sam@example.com");
    await card.getByRole("button", { name: "Make sign-in link" }).click();
    const inviteRow = card.getByRole("listitem").filter({ hasText: "sam-agent" });
    await expect(card.getByText(/\/api\/auth\/oidc\/start\?invite=/)).toBeVisible();
    await expect(inviteRow.getByText("Invited", { exact: true })).toBeVisible();
    await expectAxeClean(page, "#company-sign-in");
    // The page scrolls inside its own container: make the whole card fit the viewport for an element shot.
    await page.setViewportSize({ width: 1440, height: 2400 });
    await card.scrollIntoViewIfNeeded();
    await card.screenshot({ path: wikiShotPath(testInfo, "settings-company-sign-in.png") });
    await page.setViewportSize({ width: 390, height: 844 });
    await inviteRow.screenshot({
      path: wikiShotPath(testInfo, "settings-company-sign-in-invite-phone.png"),
    });
    await page.setViewportSize({ width: 1440, height: 900 });

    await inviteRow.getByRole("button", { name: "Withdraw", exact: true }).click();
    const withdraw = page.getByRole("dialog");
    await expect(withdraw).toContainText("sam-agent");
    await withdraw.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(card.getByText(/\/api\/auth\/oidc\/start\?invite=/)).toBeVisible();
    await inviteRow.getByRole("button", { name: "Withdraw", exact: true }).click();
    await withdraw.getByRole("button", { name: "Withdraw", exact: true }).click();
    await expect(inviteRow).toHaveCount(0);
    await expect(card.getByText(/\/api\/auth\/oidc\/start\?invite=/)).toHaveCount(0);

    // robin was added here, so company sign-in is their only way in: removing it removes the person (People's
    // own choice for what they own), and the dialog says so. The secret never comes back from the server.
    await card.getByRole("button", { name: "Remove robin", exact: true }).click();
    const removal = page.getByRole("dialog");
    await expect(removal).toContainText("Remove robin from Rynhult?");
    await expect(removal).toContainText("robin signs in only with their company account, so this removes them from Rynhult");
    await removal.getByRole("radio", { name: "Archive them" }).click();
    await removal.getByRole("button", { name: "Remove robin", exact: true }).click();
    await expect(card.getByText("AbCdEf0123456789-robin")).toHaveCount(0);
    const people: { users: { username: string }[] } = await (await page.request.get("/api/users")).json();
    expect(people.users.map((u) => u.username)).not.toContain("robin");
    const state = await page.request.get("/api/organizations/oidc");
    expect(JSON.stringify(await state.json())).not.toContain("example-secret-value");
    expect((await state.json()).session_hours).toBe(8);
    expect((await state.json()).mfa_acr).toEqual(["c2", "urn:example:loa:mfa"]);
    expect((await state.json()).passkey_enrollment).toBe(true);
  });

  test("Company sign-in: allow a provider on the local network after confirming its addresses", async ({ page }) => {
    // No LAN provider resolves here: the address check and the saved list are answered by routes.
    const hosts: object[] = [];
    await page.route("**/api/organizations/oidc/private-hosts/resolve", (route) => route.fulfill({ json: {
      host: "sso.example.lan", port: 443, addresses: ["10.0.0.20"] } }));
    await page.route("**/api/organizations/oidc/private-hosts", async (route) => {
      if (route.request().method() === "POST") {
        expect(route.request().postDataJSON()).toEqual({ url: "https://sso.example.lan/realms/staff", addresses: ["10.0.0.20"] });
        hosts.push({ id: "a1", host: "sso.example.lan", port: 443, addresses: ["10.0.0.20"], added_by: "casey", added_at: "2026-10-25T12:00:00Z" });
        return route.fulfill({ status: 201, json: hosts[0] });
      }
      return route.fulfill({ json: { hosts } });
    });
    await page.goto("/settings#company-sign-in");
    const card = page.locator("#company-sign-in");
    await expect(card.getByRole("heading", { name: "Provider addresses on your network" })).toBeVisible();
    await card.getByRole("textbox", { name: "Provider address", exact: true }).fill("https://sso.example.lan/realms/staff");
    await card.getByRole("button", { name: "Check addresses" }).click();
    await expect(card.getByText("10.0.0.20")).toBeVisible();
    const allow = card.getByRole("button", { name: "Allow these addresses" });
    await expect(allow).toBeDisabled();  // the owner confirms the addresses first
    await card.getByLabel("I confirm these addresses belong to our identity provider").check();
    await allow.click();
    await expect(card.getByRole("button", { name: "Stop allowing sso.example.lan" })).toBeVisible();
    await expectAxeClean(page, "#company-sign-in");
  });
});

test.describe("company-only member", () => {
  test.use({ storageState: AUTH_FILE });

  test("Passkeys explains the window after a multi-factor company sign-in, and when it is closed", async ({ page }, testInfo) => {
    const until = new Date(Date.now() + 4 * 60000).toISOString();
    let open = true;
    await page.route("**/api/auth/passkeys", (route) => route.fulfill({ json: {
      passkeys: [], recovery_code: false, policy: { owners: "allow", members: "allow" }, company_only: true,
      enrollment_until: open ? until : null } }));
    await page.goto("/settings#passkeys");
    const card = page.locator("#passkeys");
    await expect(card.getByText(/proved multi-factor authentication, so you can add one passkey now/)).toBeVisible();
    await expect(card.getByText(/^Verify first/)).toHaveCount(0);
    await expect(card.getByLabel("Verification password")).toHaveCount(0);  // no local password to verify with
    await expect(card.getByRole("heading", { name: "3. Save a recovery code" })).toHaveCount(0);  // recovery needs a password
    await card.getByRole("textbox", { name: "Passkey name" }).fill("Work laptop");
    await expect(card.getByRole("button", { name: "Add passkey" })).toBeEnabled();
    await expectAxeClean(page, "#passkeys");
    await card.scrollIntoViewIfNeeded();
    // The member's part only: this browser is signed in as an owner, so the card also shows the owner policy.
    const top = await card.boundingBox(), form = await card.getByRole("button", { name: "Add passkey" }).boundingBox();
    await page.screenshot({ path: wikiShotPath(testInfo, "settings-passkey-after-company-mfa.png"),
      clip: { x: top!.x, y: top!.y, width: top!.width, height: form!.y + form!.height + 16 - top!.y } });
    open = false;
    await page.reload();
    await expect(card.getByText(/sign out and sign in again with your company account using multi-factor/)).toBeVisible();
  });
});

test.describe("sign-in page", () => {
  for (const width of [1440, 390]) {
    test(`offers company sign-in when it is on, and explains a refusal, at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
      await page.route("**/api/auth/state", (route) => route.fulfill({ json: {
        setup_required: false, authenticated: false, username: null, sso: true } }));
      await page.goto("/login?sso=not_provisioned");
      await expect(page.getByRole("alert")).toContainText("isn't set up in Rynhult yet");
      const company = page.getByRole("link", { name: "Sign in with your company account" });
      await expect(company).toHaveAttribute("href", "/api/auth/oidc/start");
      // Break-glass: local sign-in stays on the same page.
      await expect(page.getByRole("button", { name: "Sign in with a passkey" })).toBeVisible();
      await expect(page.getByLabel("Username")).toBeVisible();
      await expectAxeClean(page);
      if (width === 1440) await page.screenshot({ path: wikiShotPath(testInfo, "login-company-sign-in.png") });
    });
  }

  test("a company sign-in without MFA proof asks for the passkey first", async ({ page }) => {
    await page.route("**/api/auth/state", (route) => route.fulfill({ json: {
      setup_required: false, authenticated: true, username: "robin", role: "member", sign_in_method: "oidc", step_up_required: true } }));
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Confirm with your passkey" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
    await expect(page.getByRole("navigation")).toHaveCount(0);  // no app shell until the check is done
    await expectAxeClean(page);
  });

  test("without company sign-in the page is unchanged", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByRole("button", { name: "Sign in with a passkey" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Sign in with your company account" })).toHaveCount(0);
  });
});
