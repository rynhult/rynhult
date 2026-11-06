import { RecoveryCodes } from "../components/RecoveryCodes";
import { useEffect, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { post } from "../lib/api";
import { Button, ErrorNote, Field, Input } from "../components/ui";
import { AuthShell as Shell } from "./AuthShell";

/** /invite/<token>: join Rynhult from an owner's one-time link, or reset your own password and authenticator. */
export default function InviteAccept() {
  const [codes, setCodes] = useState<string[] | null>(null);
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
      const result = await post<{ recovery_codes: string[] }>("/api/auth/invite/complete", { token, username: enrol?.reset_for ? null : form.username.trim(), password: form.password, code: form.code });
      // Joining can replace an existing sign-in. Start the new document at its
      // destination before polling auth: otherwise the identity boundary reloads
      // this now-consumed invite URL and leaves invalidation pending forever.
      // Failed completion never reaches this navigation or clears draft identity.
      setCodes(result.recovery_codes);
    } catch (err) { setError(err); }
  };
  if (codes) return <Shell title="Save recovery codes" subtitle="Keep these offline"><RecoveryCodes codes={codes} onSaved={() => { location.href = "/"; }} /></Shell>;
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
