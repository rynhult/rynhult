import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { post } from '../lib/api';
import { RecoveryCodes } from '../components/RecoveryCodes';
import { CopyBlock } from '../components/CopyBlock';
import { Button, ErrorNote, Field, Input } from '../components/ui';
import { AuthShell as Shell } from './AuthShell';

export default function Setup() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [codes, setCodes] = useState<string[] | null>(null);
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
      const result = await post<{ recovery_codes: string[] }>("/api/auth/setup/complete", { setup_token: token, username: form.username, password: form.password, code: form.code });
      setCodes(result.recovery_codes);
    } catch (err) { setError(err); }
  };
  if (codes) return <Shell title="Save recovery codes" subtitle="Keep access if you lose your authenticator"><RecoveryCodes codes={codes} onSaved={async () => { setCodes(null); await queryClient.invalidateQueries({ queryKey: ["auth"] }); navigate("/"); }} /></Shell>;
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
