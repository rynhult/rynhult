import { useEffect, useRef, useState } from 'react';
import type { ClipboardEvent, DragEvent } from 'react';
import { api } from '../lib/api';

/** File gestures belong to the person; text gestures remain entirely xterm-owned. */
export function useTerminalUpload(sessionId: string, connection: () => { socket?: WebSocket | null; write: boolean; insert: (text: string) => void }, target?: HTMLElement) {
  const latest = useRef(connection);
  latest.current = connection;
  const [message, setMessage] = useState('');
  const busy = useRef(false);
  async function upload(files: File[]) {
    const initial = latest.current();
    if (!initial.write) { setMessage('Read-only terminal: acquire control before uploading files.'); return; }
    if (busy.current) { setMessage('An upload is already running.'); return; }
    if (files.length > 10 || files.some(file => file.size > 4 * 1024 * 1024) || files.reduce((sum, file) => sum + file.size, 0) > 16 * 1024 * 1024) {
      setMessage('Upload up to 10 files, 4 MiB per file and 16 MiB total.'); return;
    }
    busy.current = true;
    setMessage(`Uploading ${files.length} files…`);
    try {
      const token = initial.socket && new URL(initial.socket.url).searchParams.get('upload_id');
      if (!token) throw new Error('Terminal connection unavailable.');
      const encoded = await Promise.all(files.map(async file => ({ path: file.name || `paste-${new Date().toISOString().replace(/[^0-9]/g, '').slice(0, 12)}.png`, data: await new Promise<string>((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('Could not read file.')); reader.readAsDataURL(file);
      }) })));
      const result = await api<{ files: { path?: string; error?: string; conflict?: boolean }[] }>(`/api/files/session/${sessionId}/upload`, { json: { files: encoded, terminal_upload: token } });
      const current = latest.current();
      if (!current.write || current.socket !== initial.socket) throw new Error('Files uploaded; terminal control changed. Insert their paths from Files.');
      const paths = result.files.filter(row => !row.error && !row.conflict).map(row => row.path);
      if (paths.some(path => typeof path !== 'string' || path.match(/^\.rynhult\/uploads\/[A-Za-z0-9._-]+$/)?.[0] !== path)) {
        throw new Error('Upload returned an unsafe path. No paths inserted.');
      }
      if (paths.length) current.insert(paths.join(' '));
      const errors = result.files.filter(row => row.error || row.conflict).map(row => row.error || 'Filename conflict.');
      setMessage(errors.length ? errors.join(' ') : 'Upload done. Paths inserted; press Enter when ready.');
    } catch (error) { setMessage((error as Error).message); }
    finally { busy.current = false; }
  }
  function take(event: { preventDefault(): void; stopPropagation(): void }, transfer: DataTransfer | null) {
    if (!transfer || !Array.from(transfer.items).some(item => item.kind === 'file')) return;
    event.preventDefault(); event.stopPropagation();
    const files = Array.from(transfer.items).filter(item => item.kind === 'file').map(item => item.getAsFile()).filter((file): file is File => !!file);
    if (files.length) void upload(files);
  }
  useEffect(() => {
    if (!target) return;
    const paste = (event: globalThis.ClipboardEvent) => take(event, event.clipboardData);
    const drop = (event: globalThis.DragEvent) => take(event, event.dataTransfer);
    const drag = (event: globalThis.DragEvent) => {
      if (event.dataTransfer?.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = latest.current().write ? 'copy' : 'none'; }
    };
    target.addEventListener('paste', paste, true);
    target.addEventListener('drop', drop, true);
    target.addEventListener('dragover', drag, true);
    return () => { target.removeEventListener('paste', paste, true); target.removeEventListener('drop', drop, true); target.removeEventListener('dragover', drag, true); };
  }, [target, sessionId]);
  return { message, handlers: {
    onPasteCapture: (event: ClipboardEvent) => take(event, event.clipboardData),
    onDropCapture: (event: DragEvent) => take(event, event.dataTransfer),
    onDragOverCapture: (event: DragEvent) => { if (Array.from(event.dataTransfer.types).includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = connection().write ? 'copy' : 'none'; } },
  } };
}
