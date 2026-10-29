import { expect, test } from "@playwright/test";
import { AUTH_FILE, info, PASSWORD, totp, verifyAgain } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("X3/Q2: demotion closes other people's terminals immediately but preserves own terminal and login", async ({ page, browser }) => {
  await page.goto("/");
  const csrf = (await page.context().cookies()).find((c) => c.name === "acc_csrf")!.value;
  const headers = { "X-ACC-CSRF": csrf, Origin: info().origin };
  await verifyAgain(page);  // inviting an owner needs fresh verification
  const invite = await page.request.post("/api/users/invites", { headers, data: { role: "owner", account: "finn-agent" } });
  expect(invite.ok()).toBe(true);
  const token = (await invite.json()).link.split("/").pop();
  const other = await browser.newContext();
  try {
    const begin = await other.request.post("/api/auth/invite/begin", { headers: { Origin: info().origin }, data: { token } });
    const joined = await other.request.post("/api/auth/invite/complete", { headers: { Origin: info().origin },
      data: { token, username: "finn", password: PASSWORD, code: totp((await begin.json()).totp_secret) } });
    expect(joined.ok()).toBe(true);
    const otherCsrf = (await other.cookies()).find((c) => c.name === "acc_csrf")!.value;
    const otherHeaders = { "X-ACC-CSRF": otherCsrf, Origin: info().origin };
    const project = await page.request.post("/api/projects", { headers,
      data: { slug: "terminal-access", name: "Terminal demo", workspace_path: info().workspace } });
    const pid = (await project.json()).id;
    await page.request.post(`/api/projects/${pid}/trust`, { headers });
    const theirs = await page.request.post("/api/sessions", { headers,
      data: { project_id: pid, provider: "claude", name: "Other person's session" } });
    const own = await other.request.post("/api/sessions", { headers: otherHeaders,
      data: { project_id: pid, provider: "claude", name: "Own session" } });
    expect(theirs.ok()).toBe(true);
    expect(own.ok()).toBe(true);
    const theirsId = (await theirs.json()).id;
    const ownId = (await own.json()).id;
    const crossView = await other.newPage();
    const ownView = await other.newPage();
    await crossView.goto(`/sessions/${theirsId}`);
    await expect(crossView.getByText("Read-write", { exact: true })).toBeVisible();
    await ownView.goto(`/sessions/${ownId}`);
    await expect(ownView.getByText("Read-write", { exact: true })).toBeVisible();
    const users = await (await page.request.get("/api/users")).json();
    const uid = users.users.find((u: { username: string }) => u.username === "finn").id;
    const demoted = await page.request.post(`/api/users/${uid}/role`, { headers, data: { role: "member" } });
    expect(demoted.ok()).toBe(true);
    await expect(crossView.getByText("terminal access revoked", { exact: true })).toBeVisible({ timeout: 5000 });
    await expect(ownView.getByText("Read-write", { exact: true })).toBeVisible();
    expect((await other.request.get(`/api/sessions/${theirsId}`)).status()).toBe(404);
    expect((await other.request.get(`/api/sessions/${ownId}`)).ok()).toBe(true);
    expect((await (await other.request.get("/api/auth/state")).json()).authenticated).toBe(true);
  } finally { await other.close(); }
});
