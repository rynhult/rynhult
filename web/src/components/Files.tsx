import "./Files.css";
import { useUnsavedDraft } from '../lib/useUnsaved';
import { unsavedWork } from '../lib/unsaved';
import { UnavailableView, unavailableResource } from './shell/RecoveryStates';
import { Surface as Card } from './shell/PagePrimitives';
import { useNewShell } from '../lib/uiShell';
import { useFileDocument } from "../lib/useFileDocument";
import type { FileRead } from "../lib/documentRegistry";
import { useWorkingView } from "../lib/workingView";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useBlocker, useSearchParams } from "react-router-dom";
import { ChevronDown, ChevronRight, File, FilePlus, Folder, Lock, PanelLeftClose, PanelLeftOpen, RefreshCw, UserRoundPen } from "lucide-react";
import { api, post, ApiError } from "../lib/api";
import { Button, Dialog, Empty, ErrorNote, Field, IconButton, Input, QueryState, Skeleton, useMedia } from "./ui";
import { useConfirm } from "./confirm";
import { lazyImport } from "../lib/lazyImport";

import { FileUploads } from "./FileUploads";

const FileEditor = lazy(() => lazyImport(() => import("./FileEditor")));
const FileMarkdownPreview = lazy(() => lazyImport(() => import("./FileMarkdownPreview")));
type Entry = { name: string; path: string; kind: "directory" | "file" | "unsupported"; size: number; status: string; protected: boolean };
type Listing = { entries: Entry[]; working: boolean };
type Read = FileRead;
type Saved = Read & { conflict?: boolean; current?: Read; commit?: string; commit_error?: string; commit_warning?: string; notice?: string };
const imageTypes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const audioTypes = new Set(["audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4"]);
const videoTypes = new Set(["video/mp4", "video/webm"]);
const size = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;

function Directory({ base, path, selected, onSelect, onManage, onFolder }: {
  base: string; path: string; selected: string; onSelect: (entry: Entry) => void; onManage: (entry: Entry) => void; onFolder: (path: string) => void;
}) {
  const newShell = useNewShell();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const query = useQuery({ queryKey: ["files-tree", base, path], queryFn: () => api<Listing>(`${base}?path=${encodeURIComponent(path)}`), ...(newShell ? { retry: (count: number, error: unknown) => !unavailableResource(error) && count < 1 } : {}) });
  return <QueryState query={query} isEmpty={(d) => !d.entries.length} empty={<p className="px-3 py-2 text-xs text-muted">Empty folder</p>}>
    {(data) => <ul className="min-w-0">{data.entries.map((entry) => {
      const isDir = entry.kind === "directory";
      const open = expanded.has(entry.path);
      const activate = () => { if (isDir) { onFolder(entry.path); setExpanded((old) => { const next = new Set(old); if (next.has(entry.path)) next.delete(entry.path); else next.add(entry.path); return next; }); } else { onFolder(entry.path.split("/").slice(0, -1).join("/")); onSelect(entry); } };
      return <li key={entry.path} style={{ contentVisibility: "auto", containIntrinsicSize: "auto 36px" }}>
        <div data-upload-blocked={entry.protected || entry.kind === "unsupported" ? "true" : undefined} data-upload-folder={isDir && !entry.protected ? entry.path : undefined} className={`relative flex min-w-0 items-center gap-1 rounded-md hover:bg-panel-2 ${selected === entry.path ? "bg-panel-2" : ""}`}>
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
        {isDir && open && <div className="ml-3 border-l border-line pl-1"><Directory base={base} path={entry.path} selected={selected} onSelect={onSelect} onManage={onManage} onFolder={onFolder} /></div>}
      </li>;
    })}</ul>}
  </QueryState>;
}

export function Files({ kind, scopeKey }: { kind: "project" | "session" | "workspace"; scopeKey: string }) {
  const newShell = useNewShell();
  const [reviewConflict, setReviewConflict] = useState(false);
  const working = useWorkingView();
  const base = `/api/files/${kind}/${encodeURIComponent(scopeKey)}`;
  const client = useQueryClient();
  const confirm = useConfirm();
  const [search, setSearch] = useSearchParams();
  const selected = search.get("file") ?? "";
  const [opened, setOpened] = useFileDocument('opened');
  const [draft, setDraft] = useFileDocument('draft');
  const [editorVersion, setEditorVersion] = useFileDocument('editorVersion');
  const openedScope = useRef<string | null>(null);
  const [uploadFolder, setUploadFolder] = useState("");
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
  const [conflict, setConflict] = useFileDocument('conflict');
  const [documentError, setDocumentError] = useState('');
  const [canonical, setCanonical] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"create" | "mkdir" | "manage" | "identity" | null>(null);
  const [target, setTarget] = useState<Entry | null>(null);
  const [pathInput, setPathInput] = useState("");
  const [commitName, setCommitName] = useState("");
  const [commitEmail, setCommitEmail] = useState("");
  const dirty = opened?.content !== undefined && draft !== opened.content;
  const draftId = `files:${JSON.stringify([base, selected])}`;
  const clearUnsaved = useUnsavedDraft(draftId, 'files', 'File edits', openedScope.current === JSON.stringify([base, selected]) ? dirty : undefined, { draft, opened, conflict },
    () => { setDraft(opened?.content ?? ''); setConflict(null); });
  const dirtyNow = useRef(dirty);
  dirtyNow.current = dirty;
  useEffect(() => { setUploadFolder(""); }, [base]);
  const tree = useQuery({ queryKey: ["files-tree", base, ""], queryFn: () => api<Listing>(base), ...(newShell ? { retry: (count: number, error: unknown) => !unavailableResource(error) && count < 1 } : {}) });
  const file = useQuery({ queryKey: ["files-read", base, selected], queryFn: () => api<Read>(`${base}/read?path=${encodeURIComponent(selected)}`), enabled: !!selected, refetchOnWindowFocus: !!working, staleTime: working ? 30000 : Infinity, refetchInterval: working ? 30000 : false, ...(newShell ? { retry: (count: number, error: unknown) => !unavailableResource(error) && count < 1 } : {}) });
  useEffect(() => {
    if (!selected || !file.isSuccess) return;
    const frame = focusSelected();
    return () => { if (frame !== undefined) cancelAnimationFrame(frame); };
  }, [selected, file.isSuccess, focusSelected]);
  const resourceUnavailable = unavailableResource(tree.error) || unavailableResource(file.error);
  const accessEnded = working && file.error instanceof ApiError && [401, 403, 404].includes(file.error.status);
  useEffect(() => { if (accessEnded) { working?.setAvailable(false); working?.documents.close(working.key); setDocumentError('File unavailable. Access ended or the file was removed.'); client.removeQueries({ queryKey: ['files-read', base, selected] }); } }, [accessEnded]);
  // Folder denial must also hide the retained tab's filename. Keep the buffer
  // while a read-only recheck determines whether access has returned.
  useEffect(() => { working?.setAvailable(!resourceUnavailable && !accessEnded && (!selected || !!file.data)); }, [resourceUnavailable, accessEnded, selected, file.data, working?.setAvailable]);
  const identity = useQuery({ queryKey: ["files-identity"], queryFn: () => api<{ name: string; email: string }>("/api/files/identity") });
  useEffect(() => {
    if (working && file.data) {
      if (accessEnded) return;
      if (!file.data.file_identity || !file.data.workspace_revision) { setDocumentError('File identity is unavailable. Recheck before editing.'); return; }
      try {
        working.documents.open(working.key);
        const owner = working.documents.bind(working.key, file.data.file_identity, file.data.workspace_revision);
        if (owner !== working.key) { setCanonical(owner); if (working.visible) working.focusExisting(owner); return; }
        setCanonical(null);
      } catch (error) { setDocumentError((error as Error).message); return; }
    }
    const scope = JSON.stringify([base, selected]);
    // A read started while clean may finish after typing or an upload. Preserve
    // both the draft and its original revision so Save still detects conflicts.
    if (openedScope.current === scope && dirtyNow.current) return;
    openedScope.current = scope;
    if (working && opened?.revision === file.data?.revision) return;
    const retained = unsavedWork.read<{ draft: string; opened: Read | null; conflict: Read | null }>(draftId);
    setOpened(retained?.opened ?? file.data ?? null); setDraft(retained?.draft ?? file.data?.content ?? ""); setConflict(retained?.conflict ?? null); setEditorVersion((v) => v + 1);
  }, [base, file.data, file.dataUpdatedAt, selected]);
  useEffect(() => { setNotice(""); }, [selected]);
  const refresh = () => client.invalidateQueries({ queryKey: ["files-tree", base] });
  const save = useMutation({
    onMutate: () => setNotice(""),
    mutationFn: (commit: boolean) => post<Saved>(base, { action: "write", path: selected, content: draft, revision: opened?.revision, ...(working ? { file_identity: opened?.file_identity, workspace_revision: opened?.workspace_revision } : {}), ...(commit ? { message } : {}) }),
    onSuccess: (data) => {
      if (data.conflict) { setReviewConflict(false); setConflict(data.current!); setNotice(data.notice ?? ""); void refresh(); return; }
      client.setQueryData(["files-read", base, selected], data);
      setOpened(data); setConflict(null); setNotice([data.notice ? `Saved. ${data.notice}` : "Saved", data.commit_error ?? data.commit_warning ?? (data.commit ? `Committed ${data.commit.slice(0, 12)} locally` : "")].filter(Boolean).join(" ")); void refresh();
    },
  });
  useEffect(() => { working?.setWork({ dirty, uncertain: save.isPending, composing: false }); }, [dirty, save.isPending, working]);
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
  if (newShell && resourceUnavailable) return <UnavailableView onRetry={() => { void tree.refetch(); if (selected) void file.refetch(); }} />;
  if (documentError || accessEnded) return <div className="space-y-3"><p>{documentError}</p>{dirty && !accessEnded && <><p>Your draft is retained. Copy it before closing and reopening this file.</p><textarea aria-label="Retained file draft" readOnly value={draft} className="h-64 w-full bg-panel font-mono text-xs" /></>}<Button onClick={() => { setDocumentError(''); void file.refetch(); }}>Recheck file</Button></div>;
  if (canonical && working) return <div className="space-y-3"><p>This file is already open in another view.</p><Button onClick={() => working.focusExisting(canonical)}>Focus existing editor</Button><Button onClick={() => void file.refetch()}>Recheck file</Button></div>;
  return <div data-shell-files={newShell || undefined} className="min-w-0 space-y-3">
    {!working && <LegacyFileGuard dirty={dirty} clearUnsaved={clearUnsaved} />}
    {tree.data?.working && <p className={newShell ? "text-xs text-muted" : "rounded-md border border-wait/30 bg-wait/10 px-3 py-2 text-xs text-wait"}>A session is working in this folder. You can edit; saves check for concurrent changes.</p>}
    {!newShell && <div className="flex flex-wrap gap-2">
      <Button size="sm" onClick={() => setShowTree(!showTree)}>{showTree ? "Hide tree" : "Show tree"}</Button>
      <Button size="sm" onClick={() => { setTarget(null); setPathInput(""); mutation.reset(); setDialog("create"); }}>New file</Button>
      <Button size="sm" onClick={() => { setTarget(null); setPathInput(""); mutation.reset(); setDialog("mkdir"); }}>New folder</Button>
      <Button size="sm" onClick={() => { void refresh(); if (selected && !dirty) void file.refetch(); }}>Refresh files</Button>
      <Button size="sm" variant="ghost" onClick={() => { setCommitName(identity.data?.name ?? ""); setCommitEmail(identity.data?.email ?? ""); personal.reset(); setDialog("identity"); }}>Commit identity</Button>
    </div>}
    <FileUploads key={base} base={base} folder={uploadFolder} onFolder={setUploadFolder} tools={newShell ? <>
      <IconButton label={showTree ? "Hide tree" : "Show tree"} icon={showTree ? PanelLeftClose : PanelLeftOpen} onClick={() => setShowTree(!showTree)} />
      <IconButton label="New file" icon={FilePlus} onClick={() => { setTarget(null); setPathInput(""); mutation.reset(); setDialog("create"); }} />
      <IconButton label="New folder" icon={Folder} onClick={() => { setTarget(null); setPathInput(""); mutation.reset(); setDialog("mkdir"); }} />
      <IconButton label="Refresh files" icon={RefreshCw} onClick={() => { void refresh(); if (selected && !dirty) void file.refetch(); }} />
      <IconButton label="Commit identity" icon={UserRoundPen} onClick={() => { setCommitName(identity.data?.name ?? ""); setCommitEmail(identity.data?.email ?? ""); personal.reset(); setDialog("identity"); }} />
    </> : undefined} onSaved={() => { void refresh(); if (!dirtyNow.current) void client.invalidateQueries({ queryKey: ["files-read", base] }); }}>
    <div className={`working-files-grid grid min-w-0 gap-3 ${showTree ? "files-tree-grid" : "grid-cols-1"}`}>
      {showTree && <div data-upload-tree="true" role="region" aria-label="Folder tree" className="min-w-0"><Card className="max-h-[32rem] min-w-0 overflow-auto p-2">
        <Directory base={base} path="" selected={selected} onFolder={setUploadFolder} onSelect={(e) => void select(e)} onManage={(e) => { setTarget(e); setPathInput(e.path); mutation.reset(); setDialog("manage"); }} />
      </Card></div>}
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
              {newShell && <p className="text-xs font-medium text-wait">Save paused · draft kept</p>}
              <h2 className="font-semibold">{newShell ? 'Resolve a save conflict' : 'File changed on disk'}</h2><p className="text-xs text-muted">Your draft is kept in the editor below. Compare it with the current version before retrying.</p>
              {newShell && <>
                <p className="text-sm text-muted">Keep both versions while you review. Review changes and Keep editing never discard your draft or choose a revision.</p>
                <dl className="space-y-2 break-all text-xs text-muted"><div><dt>Draft base revision</dt><dd className="font-mono">{opened?.revision}</dd></div><div><dt>Current disk revision</dt><dd className="font-mono">{conflict.revision}</dd></div><div><dt>Observed time</dt><dd>Not supplied by the current file service.</dd></div></dl>
                <div className="flex flex-wrap gap-2"><Button size="sm" aria-expanded={reviewConflict} onClick={() => setReviewConflict(true)}>Review changes</Button><Button size="sm" onClick={() => { setReviewConflict(false); filePanel.current?.querySelector<HTMLElement>('[role="textbox"]')?.focus(); }}>Keep editing</Button></div>
              </>}
              {(!newShell || reviewConflict) && <pre tabIndex={0} role="region" aria-label="Current version on disk" className="max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-xs">{conflict.content ?? "The file is no longer editable text."}</pre>}
              <Button size="sm" disabled={(newShell && !reviewConflict) || conflict.content === undefined || conflict.redacted} onClick={() => { setOpened({ ...conflict!, file_identity: opened?.file_identity, workspace_revision: opened?.workspace_revision }); setConflict(null); }}>Keep draft and use current revision</Button>
              <Button size="sm" disabled={newShell && !reviewConflict} onClick={async () => { if (await confirm({ title: "Discard draft?", body: "Replace your draft with the current version on disk.", confirm: "Discard draft", danger: true })) { setOpened({ ...conflict!, file_identity: opened?.file_identity, workspace_revision: opened?.workspace_revision }); setDraft(conflict.content ?? ""); setConflict(null); setEditorVersion((v) => v + 1); } }}>Use disk version</Button>
            </div>}
            <div className={`working-markdown-grid grid min-w-0 gap-3 ${/\.(md|markdown)$/i.test(selected) ? "xl:grid-cols-2" : ""}`}>
              <div className="min-w-0"><p className="mb-2 text-xs text-muted">Editor{conflict ? " · your draft" : ""}</p>
                <Suspense fallback={<Skeleton className="h-96" />}><FileEditor key={`${selected}:${editorVersion}`} path={selected} value={draft} readOnly={opened?.redacted || save.isPending} maxLength={working ? 1024 * 1024 : undefined} onChange={setDraft} /></Suspense>
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
          {notice && <p role="status" aria-label="File save status" className="break-all text-xs text-muted">{notice}</p>}
        </>}
      </Card></section>
    </div>
    </FileUploads>
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

function LegacyFileGuard({ dirty, clearUnsaved }: { dirty: boolean; clearUnsaved: () => void }) {
  const confirm = useConfirm();
  const blocker = useBlocker(({ currentLocation, nextLocation }) => {
    if (!dirty) return false;
    // The shell keeps this editor mounted while its launch page is open.
    // Exempt only adding/removing that marker; a changed file, tab, hash or
    // pathname still uses the existing discard decision.
    if (currentLocation.pathname === nextLocation.pathname && currentLocation.hash === nextLocation.hash) {
      const current = new URLSearchParams(currentLocation.search);
      const next = new URLSearchParams(nextLocation.search);
      const from = current.getAll("new-session"), to = next.getAll("new-session");
      const overlayOnly = (!from.length && to.length === 1 && to[0] === "1")
        || (from.length === 1 && from[0] === "1" && !to.length);
      current.delete("new-session"); next.delete("new-session");
      if (overlayOnly && current.toString() === next.toString()) return false;
    }
    return true;
  });
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    void confirm({ title: "Discard unsaved edits?", body: "Leaving this file will discard your draft.", confirm: "Discard draft", danger: true })
      .then((discard) => { if (discard) { clearUnsaved(); blocker.proceed(); } else blocker.reset(); });
  }, [blocker, confirm, clearUnsaved]);
  return null;
}
