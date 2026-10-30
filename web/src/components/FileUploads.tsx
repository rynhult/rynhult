import { useRef, useState } from "react";
import type { ReactNode, DragEvent } from "react";
import { uploadJson } from "../lib/api";
import { Button } from "./ui";

const MAX_FILE = 4 * 1024 * 1024;
const MAX_BATCH = 16 * 1024 * 1024;
type Result = { requested_path: string; path?: string; error?: string; conflict?: boolean; revision?: string; notice?: string };
type Item = { file: File; path: string; result?: Result };
const encode = (file: File) => new Promise<string>((resolve, reject) => {
  const reader = new FileReader();
  reader.onerror = () => reject(new Error(`Cannot read ${file.name}.`));
  reader.onload = () => resolve(String(reader.result).split(",")[1]);
  reader.readAsDataURL(file);
});

export function FileUploads({ base, folder, onFolder, onSaved, children }: {
  base: string; folder: string; onFolder: (path: string) => void; onSaved: () => void; children: ReactNode;
}) {
  const input = useRef<HTMLInputElement>(null);
  const lock = useRef(false);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [items, setItems] = useState<Item[]>([]);
  const [error, setError] = useState("");
  async function send(rows: Item[], choice = "refuse") {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(""); setProgress(0);
    try {
      const files = [];
      for (const row of rows) files.push({ path: row.path, data: await encode(row.file), overwrite: choice, revision: row.result?.revision });
      const result = await uploadJson<{ files: Result[] }>(`${base}/upload`, { files }, setProgress);
      setItems((old) => old.map((row) => {
        const index = rows.indexOf(row);
        return index < 0 ? row : { ...row, result: result.files[index] };
      }));
      onSaved();
    } catch (e) { setError(e instanceof Error ? e.message : "Upload failed. Check the folder before trying again."); }
    finally { lock.current = false; setBusy(false); }
  }
  function start(files: File[], target: string) {
    if (lock.current || !files.length) return;
    if (files.length > 100 || files.reduce((sum, file) => sum + file.size, 0) > MAX_BATCH) {
      setError("Choose up to 100 files, with at most 16 MiB in the batch."); return;
    }
    const rows = files.map((file) => ({ file, path: target ? `${target}/${file.name}` : file.name,
      ...(file.size > MAX_FILE ? { result: { requested_path: file.name, error: "File exceeds the 4 MiB per-file limit." } } : {}) }));
    setItems(rows); setError("");
    const valid = rows.filter((row) => !row.result);
    if (valid.length) void send(valid);
  }
  function drop(e: DragEvent) {
    if (!e.dataTransfer.types.includes("Files")) return;
    e.preventDefault(); e.stopPropagation();
    const element = e.target as HTMLElement;
    if (!element.closest("[data-upload-tree]")) return;
    if (element.closest("[data-upload-blocked]")) { setError("That destination is protected. Choose an unlocked folder."); return; }
    const target = element.closest<HTMLElement>("[data-upload-folder]");
    const destination = target?.dataset.uploadFolder ?? folder;
    onFolder(destination);
    start(Array.from(e.dataTransfer.files), destination);
  }
  return <div className="min-w-0 space-y-3" onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); e.dataTransfer.dropEffect = busy ? "none" : "copy"; } }} onDrop={drop}>
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" disabled={busy} onClick={() => input.current?.click()}>Upload files</Button>
      <input ref={input} type="file" multiple className="sr-only" tabIndex={-1} aria-label="Choose files to upload" onChange={(e) => { start(Array.from(e.target.files ?? []), folder); e.target.value = ""; }} />
      <p className="min-w-0 break-all text-xs text-muted">Upload to: {folder || "workspace root"}. Drop files onto a folder or this tree. 4 MiB per file · 16 MiB per batch.</p>
      {folder && <Button size="sm" onClick={() => onFolder("")}>Use workspace root</Button>}
    </div>
    {busy && <div role="status" className="text-xs"><label>Uploading batch: {progress}% <progress aria-label="Batch upload progress" value={progress} max={100} className="max-w-full" /></label><p>Wait for the server to confirm each file.</p></div>}
    {error && <p role="alert" aria-label="File upload error" aria-atomic="true" className="break-words text-sm text-danger">{error}</p>}
    {items.length > 0 && <ul aria-label="Upload results" aria-live="polite" className="space-y-2 text-xs">{items.map((row, i) => <li key={i} className="min-w-0 break-all rounded-md border border-line p-2">
      <span>{row.result?.path ?? row.path}: {row.result?.error ?? row.result?.notice ?? (row.result?.conflict ? "Already exists or changed. Choose what to do." : row.result ? "Uploaded" : busy ? "Uploading…" : "Not confirmed; check folder.")}</span>
      {row.result?.conflict && <div className="mt-2 flex flex-wrap gap-2">
        <Button size="sm" disabled={busy} onClick={() => void send([row], "replace")}>Replace {row.file.name}</Button>
        <Button size="sm" disabled={busy} onClick={() => void send([row], "keep-both")}>Keep both</Button>
        <Button size="sm" disabled={busy} onClick={() => setItems((old) => old.map((r) => r === row ? { ...r, result: { requested_path: r.path, error: "Cancelled; existing file kept." } } : r))}>Cancel upload</Button>
      </div>}
    </li>)}</ul>}
    {children}
  </div>;
}
