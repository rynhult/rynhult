import { useSingleMutation as useMutation } from "../lib/mutations";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useBlocker, useSearchParams } from "react-router-dom";
import { ChevronDown, ChevronRight, File, Folder, Lock } from "lucide-react";
import { api, post } from "../lib/api";
import { Button, Card, Dialog, Empty, ErrorNote, Field, Input, QueryState, Skeleton, useMedia } from "./ui";
import { useConfirm } from "./confirm";
import { lazyImport } from "../lib/lazyImport";

const FileEditor = lazy(() => lazyImport(() => import("./FileEditor")));
const FileMarkdownPreview = lazy(() => lazyImport(() => import("./FileMarkdownPreview")));
type Entry = { name: string; path: string; kind: "directory" | "file" | "unsupported"; size: number; status: string; protected: boolean };
type Listing = { entries: Entry[]; working: boolean };
type Read = { content?: string; revision: string; size: number; too_large?: boolean; binary?: boolean; media_type?: string; redacted?: boolean };
type Saved = Read & { conflict?: boolean; current?: Read; commit?: string; commit_error?: string; commit_warning?: string };
const imageTypes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const audioTypes = new Set(["audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4"]);
const videoTypes = new Set(["video/mp4", "video/webm"]);
const size = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;

function Directory({ base, path, selected, onSelect, onManage }: {
  base: string; path: string; selected: string; onSelect: (entry: Entry) => void; onManage: (entry: Entry) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const query = useQuery({ queryKey: ["files-tree", base, path], queryFn: () => api<Listing>(`${base}?path=${encodeURIComponent(path)}`) });
  return <QueryState query={query} isEmpty={(d) => !d.entries.length} empty={<p className="px-3 py-2 text-xs text-muted">Empty folder</p>}>
    {(data) => <ul className="min-w-0">{data.entries.map((entry) => {
      const isDir = entry.kind === "directory";
      const open = expanded.has(entry.path);
      const activate = () => { if (isDir) setExpanded((old) => { const next = new Set(old); if (next.has(entry.path)) next.delete(entry.path); else next.add(entry.path); return next; }); else onSelect(entry); };
      return <li key={entry.path} style={{ contentVisibility: "auto", containIntrinsicSize: "auto 36px" }}>
        <div className={`relative flex min-w-0 items-center gap-1 rounded-md hover:bg-panel-2 ${selected === entry.path ? "bg-panel-2" : ""}`}>
          {/* Stretch the existing semantic button over the decorative metadata. Keeping that text outside its
              label preserves the readable file/status name and WCAG label-in-name; Manage stays above the hit area. */}
          <button type="button" aria-label={`${isDir ? "Toggle" : "Open"} ${entry.path}${entry.status ? `, ${entry.status}` : ""}`} aria-expanded={isDir ? open : undefined}
            disabled={entry.protected || entry.kind === "unsupported"}
            className="flex min-h-9 min-w-0 flex-1 items-center gap-1.5 py-1 pl-2 text-left text-xs disabled:opacity-60 after:absolute after:inset-0 after:rounded-md after:content-['']"
            onClick={activate}>
            {entry.protected ? <Lock size={12} className="shrink-0" aria-hidden="true" /> : isDir ? <>{open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}<Folder size={12} /></> : <File size={12} className="shrink-0" aria-hidden="true" />}
            <span className="min-w-0 flex-1 truncate" title={entry.name}>{entry.name}</span>
          </button>
          <span aria-hidden="true" className="pointer-events-none flex shrink-0 items-center gap-1.5 self-stretch pr-2 text-xs">
            {entry.status && <span className="text-wait" title={entry.status}>{entry.status === "modified" ? "M" : entry.status === "ignored" ? "I" : "N"}</span>}
            {!isDir && <span className="tnum text-faint">{size(entry.size)}</span>}
          </span>
          {!entry.protected && entry.kind !== "unsupported" && <Button size="sm" variant="ghost" className="relative z-10" aria-label={`Manage ${entry.path}`} onClick={() => onManage(entry)}>…</Button>}
        </div>
        {isDir && open && <div className="ml-3 border-l border-line pl-1"><Directory base={base} path={entry.path} selected={selected} onSelect={onSelect} onManage={onManage} /></div>}
      </li>;
    })}</ul>}
  </QueryState>;
}

export function Files({ kind, scopeKey }: { kind: "project" | "session" | "workspace"; scopeKey: string }) {
  const base = `/api/files/${kind}/${encodeURIComponent(scopeKey)}`;
  const client = useQueryClient();
  const confirm = useConfirm();
  const [search, setSearch] = useSearchParams();
  const selected = search.get("file") ?? "";
  const [opened, setOpened] = useState<Read | null>(null);
  const [draft, setDraft] = useState("");
  const [editorVersion, setEditorVersion] = useState(0);
  const [showTree, setShowTree] = useState(true);
  const phone = useMedia("(max-width: 767px)");
  const filePanel = useRef<HTMLElement>(null);
  const focusSelected = useCallback(() => {
    if (!phone) return;
    setShowTree(false);
    return requestAnimationFrame(() => {
      filePanel.current?.focus({ preventScroll: true });
      filePanel.current?.scrollIntoView({ block: "start" });
    });
  }, [phone]);
  const [message, setMessage] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState<Read | null>(null);
  const [dialog, setDialog] = useState<"create" | "mkdir" | "manage" | "identity" | null>(null);
  const [target, setTarget] = useState<Entry | null>(null);
  const [pathInput, setPathInput] = useState("");
  const [commitName, setCommitName] = useState("");
  const [commitEmail, setCommitEmail] = useState("");
  const dirty = opened?.content !== undefined && draft !== opened.content;
  const blocker = useBlocker(dirty);
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    void confirm({ title: "Discard unsaved edits?", body: "Leaving this file will discard your draft.", confirm: "Discard draft", danger: true })
      .then((discard) => { if (discard) blocker.proceed(); else blocker.reset(); });
  }, [blocker, confirm]);
  const tree = useQuery({ queryKey: ["files-tree", base, ""], queryFn: () => api<Listing>(base) });
  const file = useQuery({ queryKey: ["files-read", base, selected], queryFn: () => api<Read>(`${base}/read?path=${encodeURIComponent(selected)}`), enabled: !!selected, refetchOnWindowFocus: false, staleTime: Infinity });
  useEffect(() => {
    if (!selected || !file.isSuccess) return;
    const frame = focusSelected();
    return () => { if (frame !== undefined) cancelAnimationFrame(frame); };
  }, [selected, file.isSuccess, focusSelected]);
  const identity = useQuery({ queryKey: ["files-identity"], queryFn: () => api<{ name: string; email: string }>("/api/files/identity") });
  useEffect(() => {
    setOpened(file.data ?? null); setDraft(file.data?.content ?? ""); setConflict(null); setEditorVersion((v) => v + 1);
  }, [file.data, selected]);
  useEffect(() => { setNotice(""); }, [selected]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  const refresh = () => client.invalidateQueries({ queryKey: ["files-tree", base] });
  const save = useMutation({
    onMutate: () => setNotice(""),
    mutationFn: (commit: boolean) => post<Saved>(base, { action: "write", path: selected, content: draft, revision: opened?.revision, ...(commit ? { message } : {}) }),
    onSuccess: (data) => {
      if (data.conflict) { setConflict(data.current!); setNotice(""); return; }
      client.setQueryData(["files-read", base, selected], data);
      setOpened(data); setConflict(null); setNotice(data.commit_error ?? data.commit_warning ?? (data.commit ? `Committed ${data.commit.slice(0, 12)} locally` : "Saved")); void refresh();
    },
  });
  useEffect(() => { save.reset(); }, [base, selected, save.reset]);
  const mutation = useMutation({
    mutationFn: (body: object) => post(base, body),
    onSuccess: () => { setDialog(null); void refresh(); if (target?.path === selected) { setSearch((s) => { s.delete("file"); return s; }); } },
  });
  const personal = useMutation({
    mutationFn: () => post("/api/files/identity", { name: commitName, email: commitEmail }),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["files-identity"] }); setDialog(null); },
  });
  function select(entry: Entry) {
    if (entry.path === selected) { focusSelected(); return; }
    setSearch((s) => { const next = new URLSearchParams(s); next.set("file", entry.path); return next; });
  }
  async function destructive(action: "delete" | "rename") {
    if (!target) return;
    setDialog(null);
    if (!await confirm({ title: action === "delete" ? `Delete ${target.path}?` : `Rename ${target.path}?`, body: action === "delete" ? "This removes the file or empty folder from disk. Uncommitted contents cannot be recovered through Rynhult." : `Move to ${pathInput}. Existing files will never be overwritten.`, confirm: action === "delete" ? "Delete" : "Rename", danger: true })) { setDialog("manage"); return; }
    setDialog("manage");
    mutation.mutate({ action, path: target.path, destination: pathInput });
  }
  const downloadUrl = `${base}/download?path=${encodeURIComponent(selected)}`;
  const mediaType = opened?.media_type ?? "";
  const text = opened?.content !== undefined;
  const canSave = text && !opened?.redacted && !save.isPending && !conflict;
  return <div className="min-w-0 space-y-3">
    {tree.data?.working && <p className="rounded-md border border-wait/30 bg-wait/10 px-3 py-2 text-xs text-wait">A session is working in this folder. You can edit; saves check for concurrent changes.</p>}
    <div className="flex flex-wrap gap-2">
      <Button size="sm" onClick={() => setShowTree(!showTree)}>{showTree ? "Hide tree" : "Show tree"}</Button>
      <Button size="sm" onClick={() => { setTarget(null); setPathInput(""); mutation.reset(); setDialog("create"); }}>New file</Button>
      <Button size="sm" onClick={() => { setTarget(null); setPathInput(""); mutation.reset(); setDialog("mkdir"); }}>New folder</Button>
      <Button size="sm" onClick={() => { void refresh(); if (selected && !dirty) void file.refetch(); }}>Refresh files</Button>
      <Button size="sm" variant="ghost" onClick={() => { setCommitName(identity.data?.name ?? ""); setCommitEmail(identity.data?.email ?? ""); personal.reset(); setDialog("identity"); }}>Commit identity</Button>
    </div>
    <div className={`grid min-w-0 gap-3 ${showTree ? "lg:grid-cols-[minmax(16rem,20rem)_minmax(0,1fr)]" : "grid-cols-1"}`}>
      {showTree && <Card className="max-h-[32rem] min-w-0 overflow-auto p-2" aria-label="Folder tree">
        <Directory base={base} path="" selected={selected} onSelect={(e) => void select(e)} onManage={(e) => { setTarget(e); setPathInput(e.path); mutation.reset(); setDialog("manage"); }} />
      </Card>}
      <section ref={filePanel} tabIndex={-1} aria-label="Selected file" className="min-w-0 scroll-mt-4"><Card className="min-w-0 space-y-3 p-3">
        {!selected ? <Empty>Select a file to open it. Protected files stay locked. M = modified · N = new · I = ignored.</Empty> : <>
          <div className="flex flex-wrap items-center justify-between gap-2"><span className="break-all font-mono text-xs">{selected}{dirty ? " · unsaved" : ""}</span>
            <a className="text-xs text-accent hover:underline underline underline-offset-2" href={downloadUrl} download={selected.split("/").pop()}>Download</a></div>
          <ErrorNote error={file.error} />
          {!opened && file.isLoading && <Skeleton className="h-40" />}
          {opened?.too_large && <p className="text-muted">Too large to open (1 MiB limit). Downloads are limited to 4 MiB.</p>}
          {opened?.binary && (imageTypes.has(mediaType)
            ? <img src={downloadUrl} alt={selected} width={800} height={600} className="max-h-[32rem] max-w-full object-contain" />
            : audioTypes.has(mediaType)
              ? <audio key={downloadUrl} src={downloadUrl} controls preload="metadata" aria-label={`Audio preview: ${selected}`} className="w-full">Download the file to listen.</audio>
              : videoTypes.has(mediaType)
                ? <video key={downloadUrl} src={downloadUrl} controls preload="metadata" aria-label={`Video preview: ${selected}`} className="max-h-[32rem] w-full">Download the file to watch.</video>
                : <p className="text-muted">Binary file. Download to view it.</p>)}
          {opened?.redacted && <p className="text-wait">Protected values were redacted. This file is read-only; ask your agent to edit it.</p>}
          {text && <>
            {conflict && <div role="alert" className="space-y-2 rounded-md border border-wait p-3">
              <h2 className="font-semibold">File changed on disk</h2><p className="text-xs text-muted">Your draft is kept in the editor below. Compare it with the current version before retrying.</p>
              <pre tabIndex={0} role="region" aria-label="Current version on disk" className="max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-xs">{conflict.content ?? "The file is no longer editable text."}</pre>
              <Button size="sm" disabled={conflict.content === undefined || conflict.redacted} onClick={() => { setOpened(conflict); setConflict(null); }}>Keep draft and use current revision</Button>
              <Button size="sm" onClick={async () => { if (await confirm({ title: "Discard draft?", body: "Replace your draft with the current version on disk.", confirm: "Discard draft", danger: true })) { setOpened(conflict); setDraft(conflict.content ?? ""); setConflict(null); setEditorVersion((v) => v + 1); } }}>Use disk version</Button>
            </div>}
            <div className={`grid min-w-0 gap-3 ${/\.(md|markdown)$/i.test(selected) ? "xl:grid-cols-2" : ""}`}>
              <div className="min-w-0"><p className="mb-2 text-xs text-muted">Editor{conflict ? " · your draft" : ""}</p>
                <Suspense fallback={<Skeleton className="h-96" />}><FileEditor key={`${selected}:${editorVersion}`} path={selected} value={draft} readOnly={opened?.redacted || save.isPending} onChange={setDraft} /></Suspense>
              </div>
              {/\.(md|markdown)$/i.test(selected) && <section aria-label="Markdown preview" className="min-w-0 overflow-auto rounded-md border border-line p-3"><p className="mb-2 text-xs text-muted">Live preview</p>
                <Suspense fallback={<Skeleton className="h-96" />}><FileMarkdownPreview text={draft} /></Suspense>
              </section>}
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <Button variant="primary" disabled={!canSave} onClick={() => save.mutate(false)}>{save.isPending ? "Saving…" : "Save"}</Button>
              <div className="min-w-0 flex-1"><Field label="Commit message" hint={identity.data?.name ? `Local commits as ${identity.data.name} <${identity.data.email}>. Never pushes.` : "Set Commit identity before committing."}><Input value={message} maxLength={2000} onChange={(e) => setMessage(e.target.value)} /></Field></div>
              <Button disabled={!canSave || !message.trim() || !identity.data?.email} onClick={() => save.mutate(true)}>Save and commit</Button>
            </div>
          </>}
          <ErrorNote error={save.error} />
          {notice && <p role="status" aria-label="File save status" className="text-xs text-muted">{notice}</p>}
        </>}
      </Card></section>
    </div>
    <Dialog open={dialog !== null} onClose={() => setDialog(null)} dismissible={false} title={dialog === "identity" ? "Commit identity" : dialog === "create" ? "New file" : dialog === "mkdir" ? "New folder" : `Manage ${target?.name ?? "file"}`} footer={<>
      <Button variant="ghost" onClick={() => setDialog(null)}>Cancel</Button>
      {dialog === "identity" ? <Button variant="primary" disabled={personal.isPending || !commitName.trim() || !commitEmail.trim()} onClick={() => personal.mutate()}>Save identity</Button>
        : dialog === "manage" ? <><Button variant="danger" disabled={mutation.isPending} onClick={() => void destructive("delete")}>Delete</Button><Button disabled={!pathInput || mutation.isPending || pathInput === target?.path} onClick={() => void destructive("rename")}>Rename</Button></>
        : <Button variant="primary" disabled={!pathInput || mutation.isPending} onClick={() => mutation.mutate({ action: dialog, path: pathInput, content: "" })}>Create</Button>}
    </>}>
      <div className="space-y-3">
        {dialog === "identity" ? <><p className="text-xs text-muted">Your account's name and email for local commits, on every machine.</p><Field label="Commit name"><Input value={commitName} autoComplete="name" onChange={(e) => setCommitName(e.target.value)} /></Field><Field label="Commit email"><Input type="email" value={commitEmail} autoComplete="email" onChange={(e) => setCommitEmail(e.target.value)} /></Field><ErrorNote error={personal.error} /></>
          : <><Field label="Path" hint="Relative to this folder. Parent folders must already exist. Delete folders only when empty. Renames refuse protected descendants."><Input value={pathInput} onChange={(e) => setPathInput(e.target.value)} /></Field><ErrorNote error={mutation.error} /></>}
      </div>
    </Dialog>
  </div>;
}
