import { AuthShell as Shell } from "./AuthShell";
import { lazyImport } from "../lib/lazyImport";
import { lazy, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { passkeyCeremony, passkeysAvailable } from "../lib/passkeys";
import { api, post } from "../lib/api";
import type { AuthState } from "../lib/types";
import { Button, ErrorNote, Field, Input, Note } from "../components/ui";

// Why a company sign-in came back to this page (the server's fixed reason codes; nothing secret is in the address).
const SSO_WORDS: Record<string, string> = {
  not_provisioned: "Your company account isn't set up in Rynhult yet. Ask an owner to add you or send you a sign-in link.",
  failed: "Company sign-in didn't complete: the provider's answer couldn't be verified. Try again, or ask an owner to check the setup.",
  unavailable: "Rynhult couldn't reach your company's sign-in service. Try again later; owners can sign in with their passkey or password.",
  expired: "That sign-in attempt expired or was already used. Start again.",
  denied: "The sign-in was cancelled at your company's sign-in page.",
  disabled: "Company sign-in is off or was just changed. Start again, or ask an owner.",
  invite_expired: "That sign-in link has expired or was already used. Ask an owner for a new one.",
  invite_withdrawn: "That sign-in link was withdrawn by an owner. Ask an owner for a new one.",
  mfa_required: "Your company sign-in didn't confirm multi-factor authentication, and Rynhult requires a passkey you don't have yet. Ask your IT team to require MFA for Rynhult, or ask an owner.",
  invite_mismatch: "That sign-in link is for a different company account. Sign in with the account it was sent to, or ask an owner for a new link.",
  link_needs_local_sign_in: "To link your company account, first sign in here with your own passkey or password, then open the link again within 15 minutes.",
  owner_local_only: "Owners sign in with their own passkey or password, not with the company account.",
  throttled: "Too many sign-in attempts. Wait 10 minutes and try again.",
  cross_site: "Company sign-in must start from this page. Select Sign in with your company account below.",
};

export function Login() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [form, setForm] = useState({ username: "", password: "", code: "" });
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [recovering, setRecovering] = useState(false);
  const [params] = useSearchParams();
  const ssoReason = params.get("sso");
  const state = useQuery({ queryKey: ["auth"], queryFn: () => api<AuthState>("/api/auth/state") });
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post(recovering ? "/api/auth/passkeys/recover" : "/api/auth/login", form);
      await queryClient.invalidateQueries({ queryKey: ["auth"] });
      navigate("/");
    } catch (err) {
      setError(err);
      setForm((f) => ({ ...f, code: "" }));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Shell title="Rynhult" subtitle="One place to supervise your coding agents.">
      {ssoReason && <Note tone="bad" role="alert" className="mb-4">{SSO_WORDS[ssoReason] ?? "Company sign-in didn't complete. Try again."}</Note>}
      {state.data?.sso && <>
        {/* A full-page navigation: the provider's sign-in page can't be shown inside Rynhult. */}
        <a href="/api/auth/oidc/start" className="mb-3 inline-flex h-8 w-full items-center justify-center rounded-md bg-accent px-3 text-[13px] font-medium text-accent-ink shadow-sm transition-colors hover:brightness-110">Sign in with your company account</a>
        <p className="mb-4 text-center text-xs text-muted">Or with this Rynhult's own sign-in:</p>
      </>}
      <Button className="mb-4 w-full" disabled={busy || !passkeysAvailable()} onClick={async () => {
        setBusy(true); setError(null);
        try { await passkeyCeremony('login'); await queryClient.invalidateQueries({ queryKey: ['auth'] }); navigate('/'); }
        catch (err) { setError(err); } finally { setBusy(false); }
      }}>Sign in with a passkey</Button>
      {!passkeysAvailable() && <p className="mb-4 text-sm text-muted">Passkeys need a supported browser and trusted HTTPS.</p>}
      <form onSubmit={submit} className="space-y-4">
        <Field label="Username"><Input autoComplete="username" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} autoFocus /></Field>
        <Field label="Password"><Input type="password" autoComplete="current-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
        <Field label={recovering ? "Recovery code" : "Authenticator code"} hint={recovering ? undefined : "A single-use sign-in recovery code also works here."}><Input inputMode="text" autoComplete="one-time-code" maxLength={100} value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} /></Field>
        {recovering && <p className="text-sm text-muted">Your password and one-use offline code remove your passkeys and end existing sign-ins. Owner recovery allows password + authenticator fallback for owners. Members under Require must first ask an owner to allow fallback.</p>}
        <ErrorNote error={error} />
        <Button type="submit" variant="primary" className="w-full" disabled={busy || !form.code}>{recovering ? "Recover sign-in" : "Sign in"}</Button>
        <Button disabled={busy} className="w-full" variant="ghost" onClick={() => { setRecovering(!recovering); setForm({ ...form, code: "" }); }}>{recovering ? "Use authenticator code" : "Lost your passkeys?"}</Button>
      </form>
    </Shell>
  );
}

/** A company sign-in that didn't prove MFA while passkeys are required: only a passkey check or sign-out until done. */
export function PasskeyStepUp() {
  const queryClient = useQueryClient();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const signOut = async () => {
    try { await post("/api/auth/logout"); } catch { /* already ended */ }
    queryClient.clear();
    window.location.assign("/login");
  };
  return (
    <Shell title="Confirm with your passkey" subtitle="One more step after your company sign-in">
      <p className="mb-4 text-[13px] text-muted">Your company sign-in didn't confirm multi-factor authentication, and Rynhult requires a passkey. Use your Rynhult passkey to continue.</p>
      <Button variant="primary" className="mb-3 w-full" disabled={busy || !passkeysAvailable()} onClick={async () => {
        setBusy(true); setError(null);
        try { await passkeyCeremony("verify"); await queryClient.invalidateQueries({ queryKey: ["auth"] }); }
        catch (err) { setError(err); } finally { setBusy(false); }
      }}>Use your passkey</Button>
      {!passkeysAvailable() && <p className="mb-4 text-sm text-muted">Passkeys need a supported browser and trusted HTTPS.</p>}
      <ErrorNote error={error} />
      <Button className="w-full" variant="ghost" disabled={busy} onClick={signOut}>Sign out</Button>
    </Shell>
  );
}

const SetupPage = lazy(() => lazyImport(() => import('./Setup')));
export function Setup() { return <SetupPage />; }

// Invite enrolment is loaded only on its route, keeping sign-in payload headroom
// for the shared identity boundary rather than loading an unused creation form.
const InvitePage = lazy(() => lazyImport(() => import("./InviteAccept")));
export function InviteAccept() { return <InvitePage />; }
