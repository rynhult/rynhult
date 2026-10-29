import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardPaste, KeyRound, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { useState } from "react";
import { api, post } from "../lib/api";
import { ago } from "../lib/format";
import { ENV_VARIABLE_GROUPS, ENV_VARIABLE_PATTERN, ENV_VARIABLES } from "../lib/envVariables";
import { useConfirm } from "./confirm";
import { Badge, Button, Card, CardHeader, Dialog, Empty, ErrorNote, Field, IconButton, Input, QueryState, Segmented, Select, Textarea } from "./ui";

/* The key vault (docs/VAULT.md). Values are write-only: Rynhult never sends one back, so nothing here can show one. */

export interface VaultKey {
  id: string; name: string; variable: string; note: string | null; owner: string; shared: boolean; remote: boolean;
  mine: boolean; created_at: string; updated_at: string; last_used_at: string | null;
  projects: { id: number; name: string; archived: boolean }[]; mcp: string[];
  server_only?: boolean;
}
interface EnvEntry { line: number; variable: string; problem: string | null; exists: boolean }

const invalidate = (queryClient: ReturnType<typeof useQueryClient>) => {
  queryClient.invalidateQueries({ queryKey: ["vault"] });
  queryClient.invalidateQueries({ queryKey: ["project-keys"] });
  queryClient.invalidateQueries({ queryKey: ["preview"] });
};

const SHARING = [{ id: "private", label: "Private" }, { id: "shared", label: "Shared" }] as const;

function Sharing({ shared, remote, onShared, onRemote }: { shared: boolean; remote: boolean; onShared: (v: boolean) => void; onRemote: (v: boolean) => void }) {
  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <span className="block text-xs font-medium text-muted">Sharing</span>
        <Segmented label="Sharing" options={SHARING} value={shared ? "shared" : "private"} onChange={(v) => onShared(v === "shared")} />
        <p className="text-xs text-faint">{shared ? "Everyone who signs in to Rynhult can see its name and give it to their projects. Only you can change it."
          : "Only your own launches use it; nobody else sees it exists."}</p>
      </div>
      <label className="flex items-start gap-2 text-[13px]">
        <input type="checkbox" className="mt-0.5" checked={remote} onChange={(e) => onRemote(e.target.checked)} />
        <span><span className="text-fg">Other machines</span>
          <span className="block text-xs text-faint">Send it to sessions on enrolled machines too. That machine can read it while the session runs.</span></span>
      </label>
    </div>
  );
}

function VariableField({ value, onChange, stored, hint }: {
  value: string; onChange: (value: string) => void; stored: readonly VaultKey[]; hint?: string;
}) {
  const [choice, setChoice] = useState(() => ENV_VARIABLES.some((v) => v.name === value) ? value : value ? "custom" : "");
  const [custom, setCustom] = useState(() => choice === "custom" ? value : "");
  const description = ENV_VARIABLES.find((v) => v.name === choice)?.description;
  const badVariable = !!value && !ENV_VARIABLE_PATTERN.test(value);
  return (
    <div className="min-w-0 space-y-3">
      <Field label="Variable" hint={description ?? hint ?? "Choose the variable your tool expects, or Custom for another name."}>
        <Select value={choice} className="min-w-0 max-w-full" onChange={(e) => {
          const next = e.target.value;
          setChoice(next);
          onChange(next === "custom" ? custom : next);
        }}>
          <option value="">Choose a variable…</option>
          {ENV_VARIABLE_GROUPS.map((group) => (
            <optgroup key={group.provider} label={group.provider}>
              {group.variables.map((v) => (
                <option key={v.name} value={v.name}>
                  {v.name} — {v.description}{stored.some((k) => k.variable === v.name) ? " · you have one" : ""}
                </option>
              ))}
            </optgroup>
          ))}
          <option value="custom">Custom</option>
        </Select>
      </Field>
      {choice === "custom" && (
        <Field label="Custom variable" hint={badVariable ? "Capital letters, digits and _, not starting with a digit" : "Up to 64 characters: capital letters, digits and _, not starting with a digit"}>
          <Input value={value} onChange={(e) => {
            const next = e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_");
            setCustom(next);
            onChange(next);
          }} className="font-mono" maxLength={64} aria-invalid={badVariable} autoComplete="off" spellCheck={false} />
        </Field>
      )}
    </div>
  );
}

function AddKeyDialog({ open, onClose, stored }: { open: boolean; onClose: () => void; stored: readonly VaultKey[] }) {
  const queryClient = useQueryClient();
  const blank = { name: "", variable: "", value: "", note: "", shared: false, remote: false };
  const [f, setF] = useState(blank);
  const close = () => { setF(blank); onClose(); };
  const add = useMutation({
    mutationFn: () => post<VaultKey>("/api/vault/keys", { ...f, note: f.note || null }),
    onSuccess: () => { invalidate(queryClient); close(); },
  });
  const badVariable = !!f.variable && !ENV_VARIABLE_PATTERN.test(f.variable);
  return (
    <Dialog open={open} onClose={close} title="Add a key" dismissible={false}
      description="Stored encrypted. After you add it, nobody can read it back, you included; you can replace it."
      footer={<><ErrorNote error={add.error} /><Button variant="ghost" onClick={close}>Cancel</Button>
        <Button variant="primary" disabled={!f.name.trim() || !f.variable || badVariable || !f.value.trim() || add.isPending}
          onClick={() => add.mutate()}>{add.isPending ? "Adding…" : "Add key"}</Button></>}>
      <div className="space-y-3">
        <Field label="Name" hint="Shown everywhere, e.g. OpenAI (work)">
          <Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} maxLength={80} /></Field>
        <VariableField value={f.variable} onChange={(variable) => setF({ ...f, variable })} stored={stored} />
        <Field label="Value"><Input type="password" autoComplete="off" spellCheck={false} value={f.value}
          onChange={(e) => setF({ ...f, value: e.target.value })} className="font-mono" /></Field>
        <Field label="Note (optional)"><Input value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} maxLength={500} /></Field>
        <Sharing shared={f.shared} remote={f.remote} onShared={(shared) => setF({ ...f, shared })} onRemote={(remote) => setF({ ...f, remote })} />
      </div>
    </Dialog>
  );
}

function PasteEnvDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [entries, setEntries] = useState<EnvEntry[] | null>(null);
  const [pick, setPick] = useState<Record<string, boolean>>({});
  const [names, setNames] = useState<Record<string, string>>({});
  const [shared, setShared] = useState(false);
  const [remote, setRemote] = useState(false);
  const close = () => { setText(""); setEntries(null); setPick({}); setNames({}); setShared(false); setRemote(false); onClose(); };
  const read = useMutation({
    mutationFn: () => post<{ entries: EnvEntry[] }>("/api/vault/import/preview", { text }),
    onSuccess: (data) => {
      setEntries(data.entries);
      setPick(Object.fromEntries(data.entries.filter((e) => !e.problem).map((e) => [e.variable, !e.exists])));
    },
  });
  const chosen = Object.entries(pick).filter(([, on]) => on).map(([v]) => v);
  const add = useMutation({
    mutationFn: () => post("/api/vault/import", { text, pick: chosen, names, shared, remote }),
    onSuccess: () => { invalidate(queryClient); close(); },
  });
  return (
    <Dialog open={open} onClose={close} title="Paste a .env" wide dismissible={false}
      description="Rynhult reads the names and variables; the values are never shown back. Comments, export and quotes are fine."
      footer={<><ErrorNote error={read.error ?? add.error} /><Button variant="ghost" onClick={close}>Cancel</Button>
        {entries ? <Button variant="primary" disabled={!chosen.length || add.isPending} onClick={() => add.mutate()}>
          {add.isPending ? "Adding…" : `Add ${chosen.length} ${chosen.length === 1 ? "key" : "keys"}`}</Button>
          : <Button variant="primary" disabled={!text.trim() || read.isPending} onClick={() => read.mutate()}>{read.isPending ? "Reading…" : "Read it"}</Button>}</>}>
      {!entries ? (
        <Field label=".env contents" hint="Lines like OPENAI_API_KEY=sk-…; nothing is stored until you choose what to add">
          <Textarea rows={9} value={text} onChange={(e) => setText(e.target.value)} className="font-mono text-[12px]" spellCheck={false}
            autoComplete="off" placeholder={"# keys\nexport OPENAI_API_KEY=…\nGITHUB_TOKEN='…'"} /></Field>
      ) : (
        <div className="space-y-4">
          <ul className="divide-y divide-line rounded-lg border border-line" aria-label="Keys found">
            {entries.map((e) => (
              <li key={`${e.line}:${e.variable}`} className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-2 text-[13px]">
                <label className="flex min-w-48 flex-1 items-center gap-2">
                  <input type="checkbox" disabled={!!e.problem} checked={!!pick[e.variable] && !e.problem}
                    onChange={(x) => setPick({ ...pick, [e.variable]: x.target.checked })} />
                  <span className="font-mono text-fg">{e.variable}</span>
                  <span className="text-xs text-faint">line {e.line}</span>
                </label>
                {e.problem ? <span className="text-xs text-wait">{e.problem}</span> : (
                  <Input aria-label={`Name for ${e.variable}`} value={names[e.variable] ?? ""} placeholder={e.variable} className="w-56"
                    onChange={(x) => setNames({ ...names, [e.variable]: x.target.value })} maxLength={80} />)}
                {e.exists && !e.problem && <Badge tone="wait" title="You already have a key with this variable">you have one</Badge>}
              </li>
            ))}
            {!entries.length && <li className="px-3 py-3 text-[13px] text-muted">No KEY=value lines found.</li>}
          </ul>
          <Sharing shared={shared} remote={remote} onShared={setShared} onRemote={setRemote} />
        </div>
      )}
    </Dialog>
  );
}

function EditKeyDialog({ vaultKey, onClose, stored }: { vaultKey: VaultKey | null; onClose: () => void; stored: readonly VaultKey[] }) {
  const queryClient = useQueryClient();
  const [f, setF] = useState(() => vaultKey && { name: vaultKey.name, variable: vaultKey.variable, note: vaultKey.note ?? "", shared: vaultKey.shared, remote: vaultKey.remote });
  const save = useMutation({
    mutationFn: () => post<VaultKey>(`/api/vault/keys/${vaultKey!.id}`, f),
    onSuccess: () => { invalidate(queryClient); onClose(); },
  });
  if (!vaultKey || !f) return null;
  const badVariable = !ENV_VARIABLE_PATTERN.test(f.variable);
  return (
    <Dialog open onClose={onClose} title={`Edit ${vaultKey.name}`} dismissible={false}
      footer={<><ErrorNote error={save.error} /><Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!f.name.trim() || badVariable || save.isPending} onClick={() => save.mutate()}>Save</Button></>}>
      <div className="space-y-3">
        <Field label="Name"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} maxLength={80} /></Field>
        <VariableField value={f.variable} onChange={(variable) => setF({ ...f, variable })} stored={stored}
          hint="Sessions see the new name from their next launch or resume" />
        <Field label="Note"><Input value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} maxLength={500} /></Field>
        <Sharing shared={f.shared} remote={f.remote} onShared={(shared) => setF({ ...f, shared })} onRemote={(remote) => setF({ ...f, remote })} />
      </div>
    </Dialog>
  );
}

function ReplaceDialog({ vaultKey, onClose }: { vaultKey: VaultKey | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  const replace = useMutation({
    mutationFn: () => post<VaultKey>(`/api/vault/keys/${vaultKey!.id}/replace`, { value }),
    onSuccess: () => { invalidate(queryClient); setValue(""); onClose(); },
  });
  if (!vaultKey) return null;
  return (
    <Dialog open onClose={onClose} title={`Replace ${vaultKey.name}`} dismissible={false}
      description="The old value is gone once you save. Running sessions keep theirs until their next launch or resume."
      footer={<><ErrorNote error={replace.error} /><Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!value.trim() || replace.isPending} onClick={() => replace.mutate()}>Replace value</Button></>}>
      <Field label="New value"><Input type="password" autoComplete="off" spellCheck={false} value={value}
        onChange={(e) => setValue(e.target.value)} className="font-mono" /></Field>
    </Dialog>
  );
}

function KeyBadges({ k }: { k: VaultKey }) {
  return <>
    <span className="font-mono text-xs text-muted">{k.variable}</span>
    {k.shared ? <Badge>shared</Badge> : <Badge>private</Badge>}
    {k.remote && <Badge>other machines</Badge>}
    {!k.mine && <Badge title="Its owner">{k.owner}</Badge>}
  </>;
}

const usedLine = (k: VaultKey) => [
  k.projects.length ? `Given to ${k.projects.map((p) => p.name + (p.archived ? " (archived)" : "")).join(", ")}` : "Not given to a project yet",
  k.mcp.length ? `MCP server ${k.mcp.join(", ")}` : null,
  k.last_used_at ? `last used ${ago(k.last_used_at)}` : "never used",
].filter(Boolean).join(" · ");

/** Settings → Keys: your keys and the ones others share. */
export function KeysCard() {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const keys = useQuery({ queryKey: ["vault"], queryFn: () => api<VaultKey[]>("/api/vault/keys") });
  const [adding, setAdding] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [editing, setEditing] = useState<VaultKey | null>(null);
  const [replacing, setReplacing] = useState<VaultKey | null>(null);
  const remove = useMutation({ mutationFn: (id: string) => post(`/api/vault/keys/${id}/remove`), onSuccess: () => invalidate(queryClient) });
  return (
    <Card>
      <CardHeader title="Keys" icon={KeyRound}
        subtitle="API keys sessions get as environment variables, in the projects you give them to. Values can't be read back."
        action={<div className="flex gap-2">
          <Button size="sm" variant="ghost" onClick={() => setPasting(true)}><ClipboardPaste size={13} aria-hidden="true" /> Paste .env</Button>
          <Button size="sm" onClick={() => setAdding(true)}><Plus size={13} aria-hidden="true" /> Add key</Button>
        </div>} />
      <QueryState query={keys} isEmpty={(d) => !d.length}
        empty={<Empty icon={KeyRound}>No keys yet. Add one, or paste a .env, then give it to a project from the project's page.</Empty>}>
        {(data) => (
          <ul className="divide-y divide-line" aria-label="Keys">
            {data.map((k) => (
              <li key={k.id} className="flex flex-wrap items-start gap-x-3 gap-y-1 px-4 py-3 text-[13px]">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2"><span className="font-medium text-fg">{k.name}</span><KeyBadges k={k} /></div>
                  {k.note && <p className="mt-0.5 text-xs text-muted">{k.note}</p>}
                  <p className="mt-0.5 text-xs text-faint">{usedLine(k)}</p>
                </div>
                {k.mine && <>
                  <IconButton label={`Edit ${k.name}`} icon={Pencil} onClick={() => setEditing(k)} />
                  <IconButton label={`Replace ${k.name}'s value`} icon={RefreshCw} onClick={() => setReplacing(k)} />
                  <IconButton label={`Delete ${k.name}`} icon={Trash2} onClick={async () => {
                    if (await confirm({ title: `Delete ${k.name}?`, confirm: "Delete", danger: true,
                      body: `Sessions lose ${k.variable} at their next launch or resume${k.projects.length ? `, in ${k.projects.length} ${k.projects.length === 1 ? "project" : "projects"}` : ""}. The value can't be recovered.` })) remove.mutate(k.id);
                  }} />
                </>}
              </li>
            ))}
          </ul>
        )}
      </QueryState>
      {remove.error ? <div className="px-4 pb-3"><ErrorNote error={remove.error} /></div> : null}
      {adding && <AddKeyDialog open onClose={() => setAdding(false)} stored={keys.data ?? []} />}
      <PasteEnvDialog open={pasting} onClose={() => setPasting(false)} />
      {editing && <EditKeyDialog key={editing.id} vaultKey={editing} onClose={() => setEditing(null)} stored={keys.data ?? []} />}
      {replacing && <ReplaceDialog key={replacing.id} vaultKey={replacing} onClose={() => setReplacing(null)} />}
    </Card>
  );
}

/** Project page → Keys: what its sessions get, Give and Remove. Archived projects too (their sessions can be resumed). */
export function ProjectKeysCard({ projectId }: { projectId: number }) {
  const queryClient = useQueryClient();
  const keys = useQuery({ queryKey: ["project-keys", projectId],
    queryFn: () => api<{ keys: VaultKey[]; available: VaultKey[] }>(`/api/projects/${projectId}/keys`) });
  const [choice, setChoice] = useState("");
  const give = useMutation({ mutationFn: (id: string) => post(`/api/vault/keys/${id}/give`, { project_id: projectId }),
    onSuccess: () => { setChoice(""); invalidate(queryClient); } });
  const take = useMutation({ mutationFn: (id: string) => post(`/api/vault/keys/${id}/take`, { project_id: projectId }),
    onSuccess: () => invalidate(queryClient) });
  const available = keys.data?.available ?? [];
  return (
    <Card>
      <CardHeader title="Keys" icon={KeyRound} subtitle="Project keys (yours and shared ones). Issue-source keys stay on the server." />
      <QueryState query={keys} isEmpty={(d) => !d.keys.length}
        empty={<Empty icon={KeyRound}>No keys given to this project.{available.length ? " Give one below." : " Add keys in Settings → Keys."}</Empty>}>
        {(data) => (
          <ul className="divide-y divide-line" aria-label="Keys given to this project">
            {data.keys.map((k) => (
              <li key={k.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-[13px]">
                <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2"><span className="font-medium text-fg">{k.name}</span><KeyBadges k={k} />{k.server_only && <Badge>server only</Badge>}</div>
                <IconButton label={`Remove ${k.name} from this project`} icon={X} onClick={() => take.mutate(k.id)} />
              </li>
            ))}
          </ul>
        )}
      </QueryState>
      {available.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3">
          <Select aria-label="Key to give" value={choice} onChange={(e) => setChoice(e.target.value)} className="w-72 max-w-full">
            <option value="">Choose a key…</option>
            {available.map((k) => <option key={k.id} value={k.id}>{k.name} ({k.variable}){k.mine ? "" : ` · ${k.owner}`}</option>)}
          </Select>
          <Button size="sm" disabled={!choice || give.isPending} onClick={() => give.mutate(choice)}><Plus size={13} aria-hidden="true" /> Give</Button>
        </div>
      )}
      {(give.error || take.error) ? <div className="px-4 pb-3"><ErrorNote error={give.error ?? take.error} /></div> : null}
    </Card>
  );
}

/** New session: the variables the session will get (names only), and any held back on another machine. */
export function SessionKeys({ keys }: { keys?: { keys: { id: string; name: string; variable: string }[]; held_back: { id: string; name: string; variable: string }[] } }) {
  if (!keys || (!keys.keys.length && !keys.held_back.length)) return null;
  return (
    <div className="rounded-lg border border-line bg-bg p-3 text-xs" data-testid="session-keys">
      <div className="mb-1 flex items-center gap-1.5 font-medium text-muted"><KeyRound size={12} aria-hidden="true" /> Keys this session gets</div>
      <ul className="space-y-0.5">
        {keys.keys.map((k) => {
          const description = ENV_VARIABLES.find((v) => v.name === k.variable)?.description;
          return <li key={k.id} className="[overflow-wrap:anywhere]"><span className="font-mono text-fg">{k.variable}</span> <span className="text-muted">· {k.name}
            {description && <> · {description}</>}</span></li>;
        })}
        {keys.held_back.map((k) => <li key={k.id} className="text-wait"><span className="font-mono">{k.variable}</span> · {k.name}: not sent to other machines</li>)}
      </ul>
    </div>
  );
}
