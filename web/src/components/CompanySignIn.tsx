import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, Trash2 } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { api, post } from "../lib/api";
import { ago, stamp } from "../lib/format";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useConfirm } from "./confirm";
import { CopyBlock } from "./InstallSteps";
import { type Leftovers, RemoveChoice } from "./People";
import { announce, Badge, Button, Card, CardHeader, Empty, ErrorNote, Field, IconButton, Input, LoadingRows, Note, QueryState } from "./ui";

interface ProviderState { issuer: string; client_id: string; has_secret: boolean; enabled: boolean; session_hours: number; mfa_acr: string[]; passkey_enrollment: boolean; redirect_uri: string }
interface Probe { ok: boolean; reason?: string; detail?: string; issuer?: string; authorization_endpoint?: string; token_endpoint?: string; keys?: number }
interface Identity { id: number; username: string; person_id: string; issuer: string; subject: string; created_at: string; last_login_at: string | null; removes_person: boolean; owner: boolean }
interface PrivateHost { id: string; host: string; port: number; addresses: string[]; added_by: string; added_at: string }
interface Resolved { host: string; port: number; addresses: string[] }
interface SignInInvite { id: number; username: string; email: string; existing: boolean; account: string | null; created_at: string; expires_at: string }
interface SignInLink { id: number; url: string }
interface Identities { identities: Identity[]; invites: SignInInvite[] }

const PROBE_WORDS: Record<string, string> = {
  provider_unavailable: "Rynhult couldn't reach the provider. Check the issuer address, then test again.",
  bad_issuer: "The issuer must be an https address without a query or user name.",
  bad_discovery: "The provider's discovery document doesn't match this issuer, or names an endpoint that isn't https. Check the issuer address.",
  bad_keys: "The provider doesn't publish a signing key Rynhult can use (RS256). Check the app's token settings at your provider.",
  private_address: "The provider's address is on a private network. Allow it under Provider addresses on your network.",
  address_changed: "The provider's addresses changed since an owner allowed them. Check and confirm them again.",
};

/** Settings → Company sign-in (organizations mode only): the provider, who may sign in, and sign-in links. */
export function CompanySignIn() {
  const queryClient = useQueryClient();
  const provider = useQuery({ queryKey: ["oidc"], queryFn: () => api<ProviderState>("/api/organizations/oidc") });
  return (
    <Card>
      <CardHeader title="Company sign-in" icon={Building2}
        subtitle="Single sign-on with your company's OpenID Connect provider · off until you turn it on · owners keep signing in with their passkey or password" />
      <QueryState query={provider} loading={<LoadingRows rows={3} />} errorTitle="Couldn't read the company sign-in settings.">
        {(data) => <div className="divide-y divide-line">
          <ProviderForm state={data} onSaved={() => queryClient.invalidateQueries({ queryKey: ["oidc"] })} />
          <PrivateHosts />
          <WhoCanSignIn configured={!!data.issuer} />
        </div>}
      </QueryState>
    </Card>
  );
}

function ProviderForm({ state, onSaved }: { state: ProviderState; onSaved: () => void }) {
  const initial = () => ({ issuer: state.issuer, client_id: state.client_id, client_secret: "", enabled: state.enabled, session_hours: String(state.session_hours), mfa_acr: state.mfa_acr.join(" "), passkey_enrollment: state.passkey_enrollment });
  const [form, setForm] = useState(initial);
  useEffect(() => setForm(initial()), [state]);  // eslint-disable-line react-hooks/exhaustive-deps
  const save = useMutation({
    mutationFn: () => api<ProviderState>("/api/organizations/oidc", { method: "PUT", json: { ...form, client_secret: form.client_secret || null, session_hours: Number(form.session_hours), mfa_acr: form.mfa_acr.split(/[\s,]+/).filter(Boolean) } }),
    onSuccess: onSaved,
  });
  const probe = useMutation({ mutationFn: () => post<Probe>("/api/organizations/oidc/test", { issuer: form.issuer }) });
  const moved = form.issuer !== state.issuer || form.client_id !== state.client_id;
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(); };
  return (
    <form onSubmit={submit} className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-2 text-[13px]">
        {state.enabled ? <Badge tone="info">On</Badge> : <Badge>Off</Badge>}
        <span className="text-muted">{state.enabled ? "The sign-in page offers Sign in with your company account." : "Nobody can sign in with the provider yet."}</span>
      </div>
      <Field label="Redirect URI" hint="Register exactly this address with the provider. It is fixed by Rynhult's address.">
        <CopyBlock text={state.redirect_uri} label="Copy redirect URI" />
      </Field>
      <Field label="Issuer" hint="From the provider, for example https://login.microsoftonline.com/<tenant ID>/v2.0">
        <Input value={form.issuer} inputMode="url" autoComplete="off" spellCheck={false} onChange={(e) => setForm({ ...form, issuer: e.target.value })} />
      </Field>
      <Field label="Sign-in client ID"><Input value={form.client_id} autoComplete="off" spellCheck={false} onChange={(e) => setForm({ ...form, client_id: e.target.value })} /></Field>
      <Field label="Sign-in client secret" hint={state.has_secret && !moved ? "Saved, encrypted with the vault's key and never shown again. Leave empty to keep it." : moved && state.has_secret ? "A new issuer or client ID needs its secret entered again." : "Encrypted with the vault's key. Never shown again or given to sessions."}>
        <Input type="password" autoComplete="new-password" value={form.client_secret} onChange={(e) => setForm({ ...form, client_secret: e.target.value })} />
      </Field>
      <Field label="Company sign-in lasts (hours)" hint="1 to 24. Rynhult notices a person disabled in the directory only at their next sign-in. Shortening it also shortens sign-ins already open.">
        <Input type="number" min={1} max={24} step={1} inputMode="numeric" className="block w-24" value={form.session_hours} onChange={(e) => setForm({ ...form, session_hours: e.target.value })} />
      </Field>
      <Field label="acr values that prove multi-factor" hint="Optional, separated by spaces. A token whose amr says mfa, hwk or fido always counts. Add only values your provider documents as multi-factor for this tenant. Adding one, or letting members add passkeys again, needs you to verify your sign-in again first (Settings, Passkeys).">
        <Input value={form.mfa_acr} autoComplete="off" spellCheck={false} placeholder="none" onChange={(e) => setForm({ ...form, mfa_acr: e.target.value })} />
      </Field>
      <label className="flex items-start gap-2 text-[13px]">
        <input type="checkbox" className="mt-0.5 size-3.5 accent-[var(--accent)]" checked={form.passkey_enrollment} onChange={(e) => setForm({ ...form, passkey_enrollment: e.target.checked })} />
        <span>Let members add a passkey after a multi-factor company sign-in<span className="block text-xs text-muted">For members who sign in only with the company account: one passkey, within five minutes of a sign-in whose token proves multi-factor.</span></span>
      </label>
      <label className="flex items-start gap-2 text-[13px]">
        <input type="checkbox" className="mt-0.5 size-3.5 accent-[var(--accent)]" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
        <span>Allow company sign-in<span className="block text-xs text-muted">Turning it off, or changing the issuer or client ID, ends every company sign-in now.</span></span>
      </label>
      <ErrorNote error={save.error} title="Couldn't save company sign-in." />
      {probe.data && (probe.data.ok
        ? <p className="text-[13px] text-muted" role="status">Connected: discovery and {probe.data.keys} signing {probe.data.keys === 1 ? "key" : "keys"} read from {probe.data.issuer}. No sign-in was attempted.</p>
        : <Note tone="bad" role="status">{PROBE_WORDS[probe.data.reason ?? ""] ?? "The provider's answer wasn't usable. Check the issuer address, then test again."}{probe.data.detail ? ` (${probe.data.detail})` : ""}</Note>)}
      <ErrorNote error={probe.error} title="Couldn't test the connection." />
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save"}</Button>
        <Button disabled={probe.isPending || !form.issuer} onClick={() => probe.mutate()}>{probe.isPending ? "Testing…" : "Test connection"}</Button>
      </div>
    </form>
  );
}

function WhoCanSignIn({ configured }: { configured: boolean }) {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const list = useQuery({ queryKey: ["oidc-identities"], queryFn: () => api<Identities>("/api/organizations/identities") });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["oidc-identities"] });
  const [person, setPerson] = useState({ username: "", subject: "", account: "" });
  const [invite, setInvite] = useState({ username: "", email: "", account: "", existing: false });
  const [link, setLink] = useState<SignInLink | null>(null);
  const add = useMutation({
    mutationFn: () => post("/api/organizations/identities", { ...person, account: person.account || null }),
    onSuccess: () => { setPerson({ username: "", subject: "", account: "" }); refresh(); },
  });
  const makeLink = useMutation({
    mutationFn: () => post<SignInLink>("/api/organizations/sso-invites", { ...invite, account: invite.account || null }),
    onSuccess: (data) => { setLink(data); refresh(); },
  });
  // A member only company sign-in lets in is removed from Rynhult (People > Remove); anyone else keeps their own sign-in.
  const remove = useMutation({
    mutationFn: ({ id, resources }: { id: number; resources?: Leftovers }) =>
      api(`/api/organizations/identities/${id}${resources ? `?resources=${resources}` : ""}`, { method: "DELETE" }),
    onSuccess: () => { refresh(); queryClient.invalidateQueries({ queryKey: ["people"] }); },
  });
  const stop = async (i: Identity) => {
    if (!i.removes_person) {
      if (await confirm({ title: `Stop ${i.username} signing in with the provider?`, confirm: "Stop company sign-in", danger: true,
        body: i.owner
          ? `${i.username} is an owner, and owners never sign in with the provider, so this only removes the binding. They stay an owner. Their projects and sessions stay.`
          : `${i.username} also has a password and authenticator of their own, so they stay a member and can still sign in with those. Their company sign-ins end now. Their projects and sessions stay.` })) remove.mutate({ id: i.id });
      return;
    }
    let resources: Leftovers = "transfer";
    if (await confirm({ title: `Remove ${i.username} from Rynhult?`, confirm: `Remove ${i.username}`, danger: true,
      body: <div className="space-y-3">
        <p>{i.username} signs in only with their company account, so this removes them from Rynhult, the same as Remove under Settings → People.</p>
        <RemoveChoice username={i.username} onChange={(v) => { resources = v; }} />
      </div> })) remove.mutate({ id: i.id, resources });
  };
  const withdraw = useMutation({
    mutationFn: (id: number) => api(`/api/organizations/sso-invites/${id}`, { method: "DELETE" }),
    onSuccess: (_data, id) => {
      setLink((current) => current?.id === id ? null : current);
      announce("Sign-in link withdrawn.");
      return refresh();
    },
  });
  return (
    <div className="space-y-4 p-4">
      <h3 className="text-[13px] font-semibold text-fg">Who can sign in</h3>
      <p className="text-[13px] text-muted">Only people you add here or send a sign-in link to. A link checks the invited company account once; after that Rynhult knows the person by the provider's issuer and subject, never by email or name. Directory group sync is planned.</p>
      <QueryState query={list} loading={<LoadingRows rows={2} />} errorTitle="Couldn't read who can sign in." isEmpty={(d) => !d.identities.length && !d.invites.length}
        empty={<Empty icon={Building2}>Nobody signs in with the provider yet.</Empty>}>
        {(data) => <ul className="divide-y divide-line rounded-md border border-line">
          {data.identities.map((i) => (
            <li key={i.id} className="flex items-center gap-3 px-3 py-2 text-[13px]">
              <div className="min-w-0 flex-1">
                <div className="text-fg">{i.username}</div>
                <div className="truncate text-xs text-muted" title={`${i.issuer} · person ${i.person_id}`}>Subject <code className="font-mono">{i.subject}</code> · {i.last_login_at ? `signed in ${ago(i.last_login_at)}` : "not signed in yet"}</div>
              </div>
              <IconButton icon={Trash2} label={i.removes_person ? `Remove ${i.username}` : `Stop ${i.username} signing in with the provider`}
                disabled={remove.isPending} onClick={() => stop(i)} />
            </li>
          ))}
          {data.invites.map((i) => (
            <li key={`invite-${i.id}`} className="flex flex-wrap items-center gap-3 px-3 py-2 text-[13px]">
              <div className="min-w-0 flex-1 basis-48 break-words">
                <div className="text-fg">{i.username}</div>
                <div className="text-xs text-muted">Sign-in link {i.existing ? "to link an existing person" : "for a new member"}, unused</div>
                <div className="text-xs text-muted">Company account <span>{i.email}</span></div>
                <div className="text-xs text-muted">Agent account <code className="font-mono">{i.account ?? "installation default"}</code></div>
                <div className="text-xs text-muted">Created <time dateTime={i.created_at} title={i.created_at}>{stamp(i.created_at)}</time></div>
              </div>
              <Badge>Invited</Badge>
              <Button variant="ghost" disabled={withdraw.isPending} onClick={async () => {
                if (await confirm({
                  title: `Withdraw ${i.username}'s sign-in link?`,
                  body: `Company account ${i.email}; agent account ${i.account ?? "installation default"}, created ${stamp(i.created_at)}. This link and any sign-in attempts already started with it stop working. Other invitations and existing company sign-ins stay.`,
                  confirm: "Withdraw", danger: true,
                })) withdraw.mutate(i.id);
              }}>Withdraw</Button>
            </li>
          ))}
        </ul>}
      </QueryState>
      <ErrorNote error={remove.error} title="Couldn't remove that sign-in." />
      <ErrorNote error={withdraw.error} title="Couldn't withdraw that sign-in link." />
      {configured ? <>
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
          <h4 className="text-xs font-semibold text-muted">Add a member by subject</h4>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Username"><Input value={person.username} autoComplete="off" onChange={(e) => setPerson({ ...person, username: e.target.value })} /></Field>
            <Field label="Subject" hint="The sub claim the provider sends for them"><Input value={person.subject} autoComplete="off" spellCheck={false} onChange={(e) => setPerson({ ...person, subject: e.target.value })} /></Field>
            <Field label="Agent account"><Input value={person.account} autoComplete="off" placeholder="name-agent" onChange={(e) => setPerson({ ...person, account: e.target.value })} /></Field>
          </div>
          <ErrorNote error={add.error} title="Couldn't add that member." />
          <Button type="submit" disabled={add.isPending || !person.username || !person.subject}>Add member</Button>
        </form>
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); setLink(null); makeLink.mutate(); }}>
          <h4 className="text-xs font-semibold text-muted">Or send a one-time sign-in link</h4>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Username"><Input value={invite.username} autoComplete="off" onChange={(e) => setInvite({ ...invite, username: e.target.value })} /></Field>
            <Field label="Company account" hint="Their email or user name in the directory. Only that account can use the link."><Input type="email" value={invite.email} autoComplete="off" spellCheck={false} placeholder="name@example.com" onChange={(e) => setInvite({ ...invite, email: e.target.value })} /></Field>
            {!invite.existing && <Field label="Agent account"><Input value={invite.account} autoComplete="off" placeholder="name-agent" onChange={(e) => setInvite({ ...invite, account: e.target.value })} /></Field>}
          </div>
          <label className="flex items-start gap-2 text-[13px]">
            <input type="checkbox" className="mt-0.5 size-3.5 accent-[var(--accent)]" checked={invite.existing} onChange={(e) => setInvite({ ...invite, existing: e.target.checked })} />
            <span>Link an existing member<span className="block text-xs text-muted">They open the link within 15 minutes of signing in with their own password or passkey.</span></span>
          </label>
          <ErrorNote error={makeLink.error} title="Couldn't make a sign-in link." />
          <Button type="submit" disabled={makeLink.isPending || !invite.username || !invite.email.includes("@")}>Make sign-in link</Button>
          {link && <div className="space-y-1"><CopyBlock text={link.url} label="Copy sign-in link" /><p className="text-xs text-muted">Works once, for 7 days, and only for that company account.</p></div>}
        </form>
      </> : <p className="text-[13px] text-muted">Save the provider's issuer first.</p>}
    </div>
  );
}

/** LAN providers: refused unless an owner confirms the host's exact DNS answer (like Issue sources' git servers). */
function PrivateHosts() {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const list = useQuery({ queryKey: ["oidc-private-hosts"], queryFn: () => api<{ hosts: PrivateHost[] }>("/api/organizations/oidc/private-hosts") });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["oidc-private-hosts"] });
  const [url, setUrl] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const resolve = useMutation({ mutationFn: () => post<Resolved>("/api/organizations/oidc/private-hosts/resolve", { url }), onSuccess: () => setConfirmed(false) });
  const allow = useMutation({
    mutationFn: () => post("/api/organizations/oidc/private-hosts", { url, addresses: resolve.data!.addresses }),
    onSuccess: () => { setUrl(""); setConfirmed(false); resolve.reset(); refresh(); },
  });
  const remove = useMutation({ mutationFn: (id: string) => api(`/api/organizations/oidc/private-hosts/${id}`, { method: "DELETE" }), onSuccess: refresh });
  const busy = resolve.isPending || allow.isPending;
  return (
    <div className="space-y-3 p-4">
      <h3 className="text-[13px] font-semibold text-fg">Provider addresses on your network</h3>
      <p className="text-[13px] text-muted">Rynhult only contacts providers at public addresses. For a provider on your own network, allow each of its addresses (issuer, token and key endpoints) here. Loopback, link-local and cloud metadata addresses are never allowed. If DNS changes, sign-in stops until an owner confirms again.</p>
      <QueryState query={list} loading={<LoadingRows rows={1} />} errorTitle="Couldn't read the allowed provider addresses." isEmpty={(d) => !d.hosts.length}
        empty={<p className="text-xs text-muted">None. Public providers such as Entra ID need no entry.</p>}>
        {(data) => <div className="divide-y divide-line rounded-md border border-line">
          {data.hosts.map((h) => (
            <div key={h.id} className="flex items-center gap-3 px-3 py-2 text-[13px]">
              <div className="min-w-0 flex-1">
                <div className="break-all text-fg">{h.host}{h.port === 443 ? "" : `:${h.port}`}</div>
                <div className="break-all font-mono text-xs text-muted">{h.addresses.join(", ")}</div>
              </div>
              <IconButton icon={Trash2} label={`Stop allowing ${h.host}`} disabled={remove.isPending}
                onClick={async () => { if (await confirm({ title: `Stop allowing ${h.host}?`, body: "Company sign-in stops working if the provider uses this address.", confirm: "Stop allowing", danger: true })) remove.mutate(h.id); }} />
            </div>
          ))}
        </div>}
      </QueryState>
      <ErrorNote error={remove.error} title="Couldn't remove that address." />
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); resolve.mutate(); }}>
        <Field label="Provider address" hint="An https address on that host, for example https://sso.example.lan/realms/staff">
          <Input value={url} inputMode="url" autoComplete="off" spellCheck={false} disabled={busy} placeholder="https://sso.example.lan…"
            onChange={(e) => { setUrl(e.target.value); resolve.reset(); setConfirmed(false); }} />
        </Field>
        <Button type="submit" disabled={busy || !url}>{resolve.isPending ? "Checking…" : "Check addresses"}</Button>
      </form>
      {resolve.data && <div className="space-y-2 text-[13px]" role="status">
        <p>{resolve.data.host} resolves to:</p>
        <ul className="list-inside list-disc font-mono text-xs">{resolve.data.addresses.map((ip) => <li key={ip}>{ip}</li>)}</ul>
        <label className="flex items-start gap-2"><input type="checkbox" className="mt-0.5 size-3.5 accent-[var(--accent)]" checked={confirmed} disabled={busy}
          onChange={(e) => setConfirmed(e.target.checked)} />I confirm these addresses belong to our identity provider</label>
        <Button variant="primary" disabled={busy || !confirmed} onClick={() => allow.mutate()}>{allow.isPending ? "Allowing…" : "Allow these addresses"}</Button>
      </div>}
      <ErrorNote error={resolve.error || allow.error} title="Couldn't allow that provider address." />
    </div>
  );
}
