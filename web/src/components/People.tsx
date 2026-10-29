import { CopyBlock } from "./InstallSteps";
import { useSingleMutation as useMutation } from "../lib/mutations";
import clsx from "clsx";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, LogOut, MonitorSmartphone, Trash2, UserPlus, Users } from "lucide-react";
import { type FormEvent, useState } from "react";
import { api, post } from "../lib/api";
import { ago } from "../lib/format";
import { useConfirm } from "./confirm";
import { Badge, Button, Card, CardHeader, ErrorNote, Field, IconButton, Input, Segmented, Select } from "./ui";

interface SignIn { id: string; user_agent: string | null; created_at: string; last_seen_at: string; expires_at: string; current: boolean }
interface Person { id: number; username: string; role: "owner" | "member"; account: string; created_at: string; last_seen_at: string | null; you: boolean }
interface OpenInvite { id: number; role: string; account: string | null; reset_for: number | null; created_by: string; created_at: string; expires_at: string }
interface PeopleView { users: Person[]; invites: OpenInvite[]; accounts: string[]; default_account: string }

const browserName = (ua: string | null) => {
  if (!ua) return "Unknown device";
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "Device";
  const name = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Browser";
  return `${os} · ${name}`;
};

/** A one-time link to hand over (shown once; Rynhult keeps only its hash). */
function LinkBox({ link }: { link: string }) {
  const url = `${window.location.origin}${link}`;
  return (
    <div className="space-y-1.5">
      <CopyBlock text={url} label="Copy link" />
      <p className="text-xs text-faint">Works once, for 72 hours. Send it privately; Rynhult can't show it again.</p>
    </div>
  );
}

export function SignIns() {
  const queryClient = useQueryClient();
  const sessions = useQuery({ queryKey: ["signins"], queryFn: () => api<SignIn[]>("/api/auth/sessions") });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["signins"] });
  const revoke = useMutation({ mutationFn: (id: string) => post(`/api/auth/sessions/${id}/revoke`), onSuccess: refresh });
  const others = useMutation({ mutationFn: () => post("/api/auth/sessions/revoke-others"), onSuccess: refresh });
  const count = sessions.data?.filter((s) => !s.current).length ?? 0;
  return (
    <Card>
      <CardHeader title="Where you're signed in" icon={MonitorSmartphone} subtitle="Sign-ins last 14 days. Ending one also closes its terminals."
        action={count > 0 ? <Button size="sm" onClick={() => others.mutate()} disabled={others.isPending}><LogOut size={13} aria-hidden="true" /> Sign out everywhere else</Button> : undefined} />
      <ul className="divide-y divide-line">
        {sessions.data?.map((s) => (
          <li key={s.id} className="flex items-center gap-3 px-4 py-2.5 text-[13px]">
            <span className="min-w-0 flex-1 truncate">{browserName(s.user_agent)}</span>
            {s.current ? <Badge tone="ok">this device</Badge> : <span className="text-xs text-faint">active {ago(s.last_seen_at)}</span>}
            {!s.current && <IconButton label={`Sign out ${browserName(s.user_agent)}`} icon={LogOut} onClick={() => revoke.mutate(s.id)} />}
          </li>
        ))}
      </ul>
      <div className="px-4 pb-3"><ErrorNote error={revoke.error ?? others.error ?? sessions.error} /></div>
    </Card>
  );
}

export function PasswordCard() {
  const [form, setForm] = useState({ current: "", next: "", confirm: "" });
  const [done, setDone] = useState(false);
  const change = useMutation({
    mutationFn: () => post("/api/auth/password", { current_password: form.current, new_password: form.next }),
    onSuccess: () => { setDone(true); setForm({ current: "", next: "", confirm: "" }); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); setDone(false); change.mutate(); };
  return (
    <Card>
      <CardHeader title="Password" icon={KeyRound} subtitle="Changing it signs you out everywhere else" />
      <form onSubmit={submit} className="grid gap-3 p-4 sm:grid-cols-3">
        <Field label="Current password"><Input type="password" autoComplete="current-password" value={form.current} onChange={(e) => setForm({ ...form, current: e.target.value })} /></Field>
        <Field label="New password" hint="At least 12 characters"><Input type="password" autoComplete="new-password" value={form.next} onChange={(e) => setForm({ ...form, next: e.target.value })} /></Field>
        <Field label="Confirm new password"><Input type="password" autoComplete="new-password" value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} /></Field>
        <div className="flex items-center gap-3 sm:col-span-3">
          <Button type="submit" variant="primary" disabled={!form.current || form.next.length < 12 || form.next !== form.confirm || change.isPending}>Change password</Button>
          {done && <span className="text-xs text-ok">Password changed.</span>}
        </div>
        <div className="sm:col-span-3"><ErrorNote error={change.error} /></div>
      </form>
    </Card>
  );
}

export type Leftovers = "transfer" | "archive";

/** In the remove confirmation: what happens to what they own. Their username is retired either way. */
export function RemoveChoice({ username, onChange }: { username: string; onChange: (v: Leftovers) => void }) {
  const [value, setValue] = useState<Leftovers>("transfer");
  const pick = (v: Leftovers) => { setValue(v); onChange(v); };
  return (
    <div className="space-y-3">
      <p>They're signed out everywhere and can't sign in again. Nobody can use the name {username} after this.</p>
      <Segmented label="Their projects, sessions and keys" value={value} onChange={pick}
        options={[{ id: "transfer", label: "Give them to me" }, { id: "archive", label: "Archive them" }]} />
      <p className="text-xs text-faint">{value === "transfer"
        ? "Their projects, sessions, keys (private ones included) and port reservations become yours. Running sessions keep running."
        : "Their projects and sessions are archived (running sessions stop) and their private keys are deleted. Shared keys and port reservations become yours, since others may use them."}</p>
    </div>
  );
}

/** Owners: who can sign in, their role and agent account; invites and resets. */
export function People() {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const people = useQuery({ queryKey: ["people"], queryFn: () => api<PeopleView>("/api/users") });
  const [role, setRole] = useState<"member" | "owner">("member");
  const [account, setAccount] = useState("");
  const [newName, setNewName] = useState("");  // with account "+new": Rynhult creates <name>-agent on the host
  const creating = account === "+new";
  const slug = newName.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "");
  const [link, setLink] = useState<{ for: string; link: string } | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["people"] });
  const invite = useMutation({
    mutationFn: () => post<{ link: string }>("/api/users/invites", creating
      ? { role, account: `${slug}-agent`, create_account: true } : { role, account: account || null }),
    onSuccess: (r) => { setLink({ for: "invite", link: r.link }); setAccount(""); setNewName(""); refresh(); },
  });
  const reset = useMutation({
    mutationFn: (p: Person) => post<{ link: string }>(`/api/users/${p.id}/reset`).then((r) => ({ ...r, who: p.username })),
    onSuccess: (r) => { setLink({ for: r.who, link: r.link }); refresh(); },
  });
  const act = useMutation({ mutationFn: (path: string) => post(path), onSuccess: refresh });
  const remove = useMutation({ mutationFn: ({ id, resources }: { id: number; resources: Leftovers }) => post(`/api/users/${id}/remove`, { resources }),
    onSuccess: () => { refresh(); queryClient.invalidateQueries({ queryKey: ["vault"] }); } });
  const changeRole = useMutation({ mutationFn: ({ id, role }: { id: number; role: string }) => post(`/api/users/${id}/role`, { role }), onSuccess: refresh });
  const view = people.data;
  // A member runs as an account of their own: never the owners' default one or one someone else has. Owners may share
  // the default account, but not take a member's (the server refuses both).
  const accounts = (view?.accounts ?? []).filter((a) => role === "member"
    ? a !== view?.default_account && !view?.users.some((u) => u.account === a)
    : !view?.users.some((u) => u.role === "member" && u.account === a));
  // Key vault: owners see how many keys each person has (names stay theirs) and can delete them when someone leaves
  const keyCounts = useQuery({ queryKey: ["vault", "people"], queryFn: () => api<{ owner: string; keys: number }[]>("/api/vault/people") });
  const keysOf = (username: string) => keyCounts.data?.find((k) => k.owner === username)?.keys ?? 0;
  const dropKeys = useMutation({ mutationFn: (username: string) => post(`/api/vault/people/${encodeURIComponent(username)}/remove`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["vault"] }) });
  return (
    <Card>
      <CardHeader title="People" icon={Users} subtitle="Who can sign in. Members see and run only their own projects and sessions, as their own agent account." />
      {dropKeys.error ? <div className="px-4 pt-3"><ErrorNote error={dropKeys.error} /></div> : null}
      <ul className="divide-y divide-line">
        {view?.users.map((p) => (
          <li key={p.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-[13px]">
            <span className="font-medium">{p.username}</span>
            {p.you && <Badge>you</Badge>}
            <span className="font-mono text-xs text-muted">{p.account}</span>
            <span className="ml-auto flex items-center gap-1">
              <span className="mr-2 text-xs text-faint">{p.last_seen_at ? `active ${ago(p.last_seen_at)}` : "never signed in"}
                {keysOf(p.username) > 0 && ` · ${keysOf(p.username)} ${keysOf(p.username) === 1 ? "key" : "keys"}`}</span>
              {!p.you && keysOf(p.username) > 0 && <IconButton label={`Delete ${p.username}'s keys`} icon={KeyRound} className="text-bad" onClick={async () => {
                if (await confirm({ title: `Delete ${p.username}'s keys?`, confirm: "Delete keys", danger: true,
                  body: `All ${keysOf(p.username)} of their keys are deleted for good, private ones included (keys an MCP server uses are kept). Sessions lose them at their next launch or resume. This is recorded in the audit log.` })) dropKeys.mutate(p.username);
              }} />}
              <Select aria-label={`Role of ${p.username}`} value={p.role} disabled={p.you} className="w-28"
                onChange={(e) => changeRole.mutate({ id: p.id, role: e.target.value })}>
                <option value="owner">Owner</option><option value="member">Member</option>
              </Select>
              <IconButton label={`Reset ${p.username}'s sign-in`} icon={KeyRound} onClick={() => reset.mutate(p)} />
              {!p.you && <IconButton label={`Remove ${p.username}`} icon={Trash2} onClick={async () => {
                let resources: Leftovers = "transfer";
                if (await confirm({ title: `Remove ${p.username}?`, body: <RemoveChoice username={p.username} onChange={(v) => { resources = v; }} />, confirm: "Remove", danger: true })) remove.mutate({ id: p.id, resources });
              }} />}
            </span>
          </li>
        ))}
      </ul>
      <form className={clsx("grid gap-3 border-t border-line p-4 sm:items-end", creating ? "sm:grid-cols-[10rem_minmax(0,1fr)_minmax(0,1fr)_auto]" : "sm:grid-cols-[10rem_minmax(0,1fr)_auto]")}
        onSubmit={(e) => { e.preventDefault(); invite.mutate(); }}>
        <Field label="Role"><Select value={role} onChange={(e) => { setRole(e.target.value as "member" | "owner"); setAccount(""); }}><option value="member">Member</option><option value="owner">Owner</option></Select></Field>
        <Field label="Agent account">
          <Select value={account} onChange={(e) => setAccount(e.target.value)}>
            <option value="">{role === "owner" ? `Default (${view?.default_account ?? "…"})` : "Choose…"}</option>
            {accounts.map((a) => <option key={a} value={a}>{a}</option>)}
            <option value="+new">New account…</option>
          </Select>
        </Field>
        {creating && (
          <Field label="Their name" hint={slug ? `Creates the Linux user ${slug}-agent` : "Lowercase letters, digits and dashes"}>
            <Input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="alex" autoFocus />
          </Field>
        )}
        <Button type="submit" variant="primary" disabled={invite.isPending || (role === "member" && !account) || (creating && !slug)}>
          <UserPlus size={14} aria-hidden="true" /> {invite.isPending && creating ? "Creating account…" : "Create invite link"}</Button>
        <p className="text-xs text-faint sm:col-span-full">Their sessions run as their agent account, a Linux user of their own with no admin rights and no usage limits. Pick one, or choose <b>New account…</b> and Rynhult creates it on the host. A member never shares an account: the owners' default one and accounts someone else has aren't offered.</p>
      </form>
      {link && (
        <div className="space-y-2 border-t border-line p-4">
          <p className="text-[13px]">{link.for === "invite" ? "Invite link" : `Reset link for ${link.for}`}</p>
          <LinkBox link={link.link} />
        </div>
      )}
      {!!view?.invites.length && (
        <div className="border-t border-line px-4 py-3">
          <p className="mb-2 text-xs text-muted">Open links</p>
          <ul className="space-y-1.5 text-[13px]">
            {view.invites.map((i) => (
              <li key={i.id} className="flex items-center gap-2">
                <span className="flex-1">{i.reset_for ? `Reset for ${view.users.find((u) => u.id === i.reset_for)?.username ?? "a user"}` : `${i.role} as ${i.account}`}
                  <span className="text-xs text-faint"> · by {i.created_by} · until {new Date(i.expires_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span></span>
                <Button size="sm" variant="ghost" onClick={() => act.mutate(`/api/users/invites/${i.id}/revoke`)}>Revoke</Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="px-4 pb-3"><ErrorNote error={invite.error ?? reset.error ?? act.error ?? remove.error ?? changeRole.error ?? people.error} /></div>
    </Card>
  );
}
