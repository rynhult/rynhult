import { useQuery } from '@tanstack/react-query';
import { useSingleMutation as useMutation } from '../lib/mutations';
import { api } from '../lib/api';
import { Fragment, useState, type ReactNode } from 'react';
import type { SessionDetail } from '../lib/types';
import { providerLabel, useInstance, useProviderRegistry } from '../lib/queries';
import { permissionSummary } from '../lib/words';
import { stamp } from '../lib/format';
import { sessionMachineName } from './session-bits';
import { PreviewBadge } from './Containers';
import { CopyBlock } from './CopyBlock';
import { Card, ErrorNote, Field, Input, Select } from './ui';

const RUN_WORDS: Record<string, string> = { launch: "Started", resume: "Resumed", explanation: "Started to explain its changes", adopted: "Picked up after a Rynhult restart" };

/** Setup (was "Launch spec", UX review SE-4): what this session was given, in words first; the exact inputs and the
    generated files follow for anyone who needs them. */
export function SessionSetup({ session }: { session: SessionDetail }) {
  const registry = useProviderRegistry();
  const inputs = session.launch_spec.inputs as Record<string, unknown>;
  const list = (key: string) => (Array.isArray(inputs[key]) ? inputs[key] as string[] : []);
  const provider = String(inputs.provider ?? session.provider);
  const permissions = permissionSummary(provider, inputs);
  const { host } = useInstance();
  const fileNames = Object.keys(session.launch_spec.files);
  const [filter, setFilter] = useState("");
  const [selectedFile, setSelectedFile] = useState<string | undefined>(() => fileNames[0]);
  const filteredFiles = fileNames.filter(name => name.toLocaleLowerCase().includes(filter.toLocaleLowerCase()));
  const activeFile = selectedFile !== undefined && filteredFiles.includes(selectedFile) ? selectedFile : filteredFiles[0];
  const items: [string, ReactNode][] = [
    ["Agent", <>{providerLabel(provider, registry.byId(provider))} · {inputs.model ? String(inputs.model) : "default model"}{inputs.effort ? ` · ${inputs.effort} effort` : ""}</>],
    ...(provider === "claude" ? [["Claude sign-in", session.claude_auth === "api_key" ? (session.auth?.mode === "subscription" ? "Subscription token" : "API key (API billing)") : "Your Claude login (subscription)"] as [string, ReactNode]] : []),
    ["Permissions", permissions],
    ["Profile", inputs.profile ? String(inputs.profile) : "None"],
    ["Skills", list("skills").length ? list("skills").join(", ") : "None"],
    ...(list("agents").length ? [["Subagents", list("agents").join(", ")] as [string, ReactNode]] : []),
    ["MCP servers", list("mcps").length ? list("mcps").join(", ") : "None"],
    ["Folder", <><span className="font-mono">{session.cwd}</span>{session.worktree_branch && <> · its own {session.container ? "private checkout" : "worktree"} on <span className="font-mono">{session.worktree_branch}</span></>}</>],
    ...(session.container ? [["Runs in", <span>A container <PreviewBadge />, with its account's container limits</span>] as [string, ReactNode]] : []),
    ["Machine", sessionMachineName(session, host)],
    ["Runs as", <span className="font-mono">{session.run_as}</span>],
  ];
  return (
    <div className="space-y-4">
      <Card className="min-w-0 p-4">
        <h3 className="mb-3 text-[13px] font-semibold">How this session was set up</h3>
        <dl className="grid min-w-0 grid-cols-1 gap-x-6 gap-y-2 text-[13px] sm:grid-cols-[9rem_minmax(0,1fr)]">
          {items.map(([label, value]) => <Fragment key={label}><dt className="text-muted">{label}</dt><dd className="min-w-0 break-words text-fg">{value}</dd></Fragment>)}
          <dt className="text-muted">History</dt>
          <dd className="min-w-0 space-y-1">
            {session.runs.map((r) => (
              <div key={r.id} className="flex flex-wrap gap-x-3 text-muted">
                <span className="text-fg" title={`run #${r.id} · ${r.reason}`}>{RUN_WORDS[r.reason] ?? r.reason}</span>
                <span>{stamp(r.started_at)}</span>
                {r.ended_at && <span>ended {r.exit_signal ? `by signal ${r.exit_signal}` : r.exit_status === 0 ? "normally" : `with status ${r.exit_status ?? "unknown"}`}{r.end_reason ? ` · ${r.end_reason}` : ""}</span>}
              </div>
            ))}
          </dd>
        </dl>
      </Card>
      <Card className="min-w-0 p-4">
        <details>
          <summary className="min-h-8 cursor-pointer break-words text-[13px] font-semibold pointer-coarse:min-h-11">
            Exact inputs <span className="ml-2 font-mono text-[11px] font-normal text-faint" title="Digest of the inputs">{session.launch_spec.digest.slice(0, 12)}</span>
          </summary>
          <div className="mt-3 min-w-0">
            <CopyBlock text={JSON.stringify(session.launch_spec.inputs, null, 2)} label="Copy launch inputs" stacked />
          </div>
        </details>
      </Card>
      <Card className="min-w-0 p-4">
        <details>
          <summary className="min-h-8 cursor-pointer text-[13px] font-semibold pointer-coarse:min-h-11">Generated files ({fileNames.length})</summary>
          <div className="mt-3 min-w-0 space-y-3">
            {!!fileNames.length && <div className="grid min-w-0 gap-3 sm:grid-cols-2">
              <Field label="Filter generated files">
                <Input value={filter} onChange={event => setFilter(event.target.value)} type="search" autoComplete="off" spellCheck={false} />
              </Field>
              {!!filteredFiles.length && <Field label="Generated file">
                <Select value={activeFile ?? ""} onChange={event => setSelectedFile(event.target.value)} className="w-full min-w-0">
                  {filteredFiles.map(name => <option key={name} value={name}>{name}</option>)}
                </Select>
              </Field>}
            </div>}
            <p role="status" aria-label="Generated files results" className="text-xs text-muted">
              {!fileNames.length ? "No generated files." : !filteredFiles.length ? "No matching files." : `${filteredFiles.length} of ${fileNames.length} files`}
            </p>
            {activeFile !== undefined && <CopyBlock key={activeFile} text={session.launch_spec.files[activeFile]} label={`Copy ${activeFile}`} stacked />}
          </div>
        </details>
      </Card>
    </div>
  );
}


export function InboxPermissionSetting({ id }: { id: string }) {
  const path = `/api/sessions/${id}/inbox-permissions`;
  const setting = useQuery({ queryKey: ["inbox-permissions", id], queryFn: () => api<{ enabled: boolean }>(path) });
  const change = useMutation({ mutationFn: (enabled: boolean) => api(path, { method: "PUT", json: { enabled } }),
    onSuccess: () => setting.refetch() });
  return <Card><label className="flex items-center gap-2"><input type="checkbox" checked={setting.data?.enabled ?? true}
    disabled={!setting.data || change.isPending} onChange={e => change.mutate(e.target.checked)} />
    Answer permission prompts from the Inbox</label><ErrorNote error={setting.error || change.error} /></Card>;
}

