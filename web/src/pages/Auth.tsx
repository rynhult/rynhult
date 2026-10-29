import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { passkeyCeremony, passkeysAvailable } from "../lib/passkeys";
import { api, post } from "../lib/api";
import type { AuthState } from "../lib/types";
import { CopyBlock } from "../components/InstallSteps";
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

function Shell({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return (
    <div className="grid min-h-full place-items-center bg-[radial-gradient(60rem_30rem_at_50%_-10%,color-mix(in_srgb,var(--accent)_12%,transparent),transparent)] p-4">
      <main className="min-w-0 w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="grid size-11 shrink-0 place-items-center" aria-hidden="true">
            <img src="/rynhult-mark-light.svg" alt="" className="hidden w-1/2 dark:block" />
            <img src="/rynhult-mark-dark.svg" alt="" className="w-1/2 dark:hidden" />
          </div>
          <div>
            <h1 className="text-[20px] font-semibold tracking-[-0.01em]">{title}</h1>
            <p className="mt-0.5 text-[13px] text-muted">{subtitle}</p>
          </div>
        </div>
        <div className="rounded-xl border border-line bg-panel p-6 shadow-pop">{children}</div>
        <p className="mt-4 text-center text-xs text-faint">Your agents. Your machines. Your call.</p>
      </main>
    </div>
  );
}

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
        <Field label={recovering ? "Recovery code" : "Authenticator code"}><Input inputMode={recovering ? "text" : "numeric"} autoComplete="one-time-code" maxLength={recovering ? 100 : 6} value={form.code} onChange={(e) => setForm({ ...form, code: recovering ? e.target.value : e.target.value.replace(/\D/g, "") })} /></Field>
        {recovering && <p className="text-sm text-muted">Your password and one-use offline code remove your passkeys and end existing sign-ins. Owner recovery allows password + authenticator fallback for owners. Members under Require must first ask an owner to allow fallback.</p>}
        <ErrorNote error={error} />
        <Button type="submit" variant="primary" className="w-full" disabled={busy || (recovering ? !form.code : form.code.length !== 6)}>{recovering ? "Recover sign-in" : "Sign in"}</Button>
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

export function Setup() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [token, setToken] = useState("");
  const [enrol, setEnrol] = useState<{ totp_secret: string; qr_svg: string } | null>(null);
  const [form, setForm] = useState({ username: "", password: "", confirm: "", code: "" });
  const [error, setError] = useState<unknown>(null);
  const begin = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try { setEnrol(await post("/api/auth/setup/begin", { setup_token: token })); } catch (err) { setError(err); }
  };
  const complete = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (form.password !== form.confirm) return setError(new Error("Passwords don't match."));
    try {
      await post("/api/auth/setup/complete", { setup_token: token, username: form.username, password: form.password, code: form.code });
      await queryClient.invalidateQueries({ queryKey: ["auth"] });
      navigate("/");
    } catch (err) { setError(err); }
  };
  if (!enrol) {
    return (
      <Shell title="Set up Rynhult" subtitle="One-time setup">
        <form onSubmit={begin} className="space-y-4">
          <p className="text-sm text-muted">On the server, read the setup token:</p>
          <CopyBlock text="sudo cat /srv/acc/data/secrets/setup_token" label="Copy setup command" />
          <Field label="Setup token"><Input value={token} onChange={(e) => setToken(e.target.value)} autoFocus /></Field>
          <ErrorNote error={error} />
          <Button type="submit" variant="primary" className="w-full" disabled={!token}>Continue</Button>
        </form>
      </Shell>
    );
  }
  return (
    <Shell title="Set up Rynhult" subtitle="Choose a password and enrol your authenticator">
      <form onSubmit={complete} className="space-y-4">
        <div className="flex justify-center rounded-lg bg-bg p-3" dangerouslySetInnerHTML={{ __html: enrol.qr_svg }} />
        <p className="text-center text-xs text-muted">Scan this with an authenticator app on your phone (any app that shows 6-digit sign-in codes), or type in the key below. Then enter the code the app shows.</p>
        <p className="break-all text-center font-mono text-[11px] text-faint" title="Manual entry key">{enrol.totp_secret}</p>
        <Field label="Username"><Input value={form.username} autoComplete="username" onChange={(e) => setForm({ ...form, username: e.target.value })} /></Field>
        <Field label="Password" hint="At least 12 characters. Save it in your password manager."><Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
        <Field label="Confirm password"><Input type="password" autoComplete="new-password" value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} /></Field>
        <Field label="Code from your authenticator"><Input inputMode="numeric" maxLength={6} value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value.replace(/\D/g, "") })} /></Field>
        <ErrorNote error={error} />
        <Button type="submit" variant="primary" className="w-full" disabled={!form.username.trim() || form.password.length < 12 || form.code.length !== 6}>Finish setup</Button>
      </form>
    </Shell>
  );
}

/** /invite/<token>: join Rynhult from an owner's one-time link, or reset your own password and authenticator. */
export function InviteAccept() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { token = "" } = useParams();
  const [enrol, setEnrol] = useState<{ reset_for: string | null; role: string; totp_secret: string; qr_svg: string } | null>(null);
  const [form, setForm] = useState({ username: "", password: "", confirm: "", code: "" });
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { post<typeof enrol>("/api/auth/invite/begin", { token }).then(setEnrol, setError); }, [token]);
  const complete = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (form.password !== form.confirm) return setError(new Error("Passwords don't match."));
    try {
      await post("/api/auth/invite/complete", { token, username: enrol?.reset_for ? null : form.username.trim(), password: form.password, code: form.code });
      await queryClient.invalidateQueries();
      navigate("/");
    } catch (err) { setError(err); }
  };
  if (!enrol) {
    return (
      <Shell title="Join Rynhult" subtitle="One-time invite link">
        {error ? <ErrorNote error={error} /> : <p className="text-sm text-muted">Checking the link…</p>}
      </Shell>
    );
  }
  const reset = !!enrol.reset_for;
  return (
    <Shell title={reset ? `Reset ${enrol.reset_for}` : "Join Rynhult"}
      subtitle={reset ? "Choose a new password and enrol your authenticator again" : `You're invited as ${enrol.role === "owner" ? "an owner" : "a member"}. Choose your sign-in.`}>
      <form onSubmit={complete} className="space-y-4">
        <div className="flex justify-center rounded-lg bg-bg p-3" dangerouslySetInnerHTML={{ __html: enrol.qr_svg }} />
        <p className="text-center text-xs text-muted">Scan this with an authenticator app on your phone (any app that shows 6-digit sign-in codes), or type in the key below. Then enter the code the app shows.</p>
        <p className="break-all text-center font-mono text-[11px] text-faint" title="Manual entry key">{enrol.totp_secret}</p>
        {!reset && <Field label="Username"><Input value={form.username} autoComplete="username" onChange={(e) => setForm({ ...form, username: e.target.value })} /></Field>}
        <Field label="Password" hint="At least 12 characters. Save it in your password manager."><Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
        <Field label="Confirm password"><Input type="password" autoComplete="new-password" value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} /></Field>
        <Field label="Code from your authenticator"><Input inputMode="numeric" maxLength={6} value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value.replace(/\D/g, "") })} /></Field>
        <ErrorNote error={error} />
        <Button type="submit" variant="primary" className="w-full" disabled={(!reset && !form.username.trim()) || form.password.length < 12 || form.code.length !== 6}>
          {reset ? "Save and sign in" : "Join"}</Button>
      </form>
    </Shell>
  );
}
