import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Monitor } from "lucide-react";
import { api, post } from "../lib/api";
import { Button, Card, Empty, ErrorNote, QueryState } from "./ui";
import { PreviewPicker } from "./PreviewPicker";

interface Discovery { available: boolean; ports: number[]; reason?: string }

/** Credentials only ever leave Rynhult in a fragment navigation to the isolated gateway. */
export function Preview({ projectId, projectName, folder, branch, sessionId }: {
  projectId: number; projectName: string; folder: string; branch?: string | null; sessionId?: string;
}) {
  const session = sessionId !== undefined;  // a session's preview: its branch and folder, and picks go to it
  const [search, setSearch] = useSearchParams();
  const [reload, setReload] = useState(0);
  const discovery = useQuery({ queryKey: ["preview", projectId], queryFn: () => api<Discovery>(`/api/projects/${projectId}/preview`), refetchInterval: 5000 });
  const ports = discovery.data?.ports ?? [];
  const requested = Number(search.get("port"));
  const port = ports.includes(requested) ? requested : ports[0];
  const access = useQuery({ queryKey: ["preview-access", projectId, port, reload], queryFn: () => post<{url: string; frame_url: string}>(`/api/projects/${projectId}/preview`, {port}),
    enabled: !!port && discovery.data?.available === true, retry: false, staleTime: Infinity, gcTime: 0, refetchOnWindowFocus: false });
  const select = (value: string) => {
    const next = new URLSearchParams(search);
    next.set("port", value);
    setSearch(next, {replace: true});
  };
  return <Card className="min-w-0 overflow-hidden">
    <section aria-label="Preview context" className="space-y-2 px-4 pt-4 text-xs text-muted">
      <h2 className="text-sm font-semibold text-fg">{projectName} preview</h2>
      <p className="break-words">{session && <>Session branch: <span className="font-mono">{branch ?? "not reported"}</span> · </>}{session ? "Session folder" : "Project folder"}: <span className="font-mono [overflow-wrap:anywhere]">{folder}</span></p>
      <p>Reserved ports are shared across this project. A port may serve another session or worktree; check which server is running there.</p>
    </section>
    <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
      <label className="flex items-center gap-2 text-[13px]">Preview port
        <select aria-label="Preview port" name="preview-port" value={port ?? ""} onChange={e => select(e.target.value)} disabled={!ports.length}
          className="h-8 rounded-md border border-line-input bg-panel-2 px-2 text-fg focus-visible:ring-2 focus-visible:ring-accent">
          {!ports.length && <option value="">No listening ports</option>}
          {ports.map(p => <option key={p} value={p}>{p}</option>)}
        </select>
      </label>
      <Button size="sm" aria-label="Reload preview" onClick={() => { void discovery.refetch(); setReload(n => n + 1); }}>Reload</Button>
      {access.data && port && <a href={access.data.url} target="_blank" rel="noopener noreferrer"
        className="text-[13px] text-accent hover:underline focus-visible:ring-2 focus-visible:ring-accent">Open in new tab</a>}
    </div>
    <QueryState query={discovery}>{data => !data.available ? <Empty icon={Monitor}>{data.reason}</Empty> : !data.ports.length
      ? <Empty icon={Monitor}><span>Nothing is listening on a reserved port.</span><p className="mt-2">Reserve a port on the project page, then ask the agent to start the dev server there.</p></Empty>
      : <>
        {access.error ? <div className="p-4"><ErrorNote error={access.error} /><p className="mt-2 text-xs text-muted">Select Reload to try again.</p></div>
          : <QueryState query={access}>{data => <PreviewPicker key={`${data.frame_url}:${reload}`} projectId={projectId} port={port} sessionId={sessionId} frameUrl={data.frame_url} />}</QueryState>}
        <p className="border-t border-line px-4 py-3 text-xs text-muted">Private preview · Access lasts 5 minutes. Select Reload to renew it.</p>
      </>}</QueryState>
  </Card>;
}
