/** Originals and extracted text remain private; only a person accepts proposed fields. */
import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import type { BriefView, Reference } from '../lib/projectBrief';
import { announce, ErrorNote, Field, Input } from './ui';

const accept = '.md,.markdown,.txt,.pdf,.docx,.odt,.rtf,.html,.htm,.png,.jpg,.jpeg,.webp';
export function validateDocumentFiles(files: File[], existing: Reference[] = []) {
  if (existing.length + files.length > 10) throw new Error('Choose up to 10 source documents per project.');
  if (files.some(file => file.size > 10 * 1024 * 1024 || file.size === 0)) throw new Error('Choose nonempty files of at most 10 MiB each.');
  if (existing.reduce((n, r) => n + r.size, 0) + files.reduce((n, f) => n + f.size, 0) > 40 * 1024 * 1024) throw new Error('Source documents must total at most 40 MiB.');
}
export async function uploadDocuments(projectId: number, files: File[]) {
  const encoded = [];
  for (const file of files) {
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error(`Could not read ${file.name}. Choose it again.`));
      reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.readAsDataURL(file);
    });
    encoded.push({ name: file.name, data });
  }
  return api<{ documents: Reference[] }>(`/api/projects/${projectId}/brief/documents`, { json: { files: encoded } });
}

export function BriefDocuments({ projectId, files = [], onFiles }: { projectId?: number; files?: File[]; onFiles?: (files: File[]) => void }) {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['project-brief-documents', projectId], enabled: !!projectId,
    queryFn: () => api<BriefView>(`/api/projects/${projectId}/brief`) });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const documents = query.data?.documents ?? [];
  return <details className="rounded-lg border border-line p-3">
    <summary className="min-h-11 cursor-pointer py-2.5 font-medium">Start from documents</summary>
    <div className="mt-3 space-y-3">
      <p className="text-xs text-muted">Upload Markdown, text, PDF, DOCX, ODT, RTF, HTML or images. Up to 10 files per project, 10 MiB each, 40 MiB total. Originals and extracted text stay private; they are never exported to the repository or wiki.</p>
      <Field label="Source documents" hint={projectId ? 'Files upload when selected. Failed extraction keeps the original as a reference.' : 'Files upload after the project is created.'}>
        <Input type="file" name="brief-documents" multiple accept={accept} disabled={busy} onChange={async event => {
          const chosen = Array.from(event.target.files ?? []);
          event.target.value = '';
          if (!chosen.length) return;
          setError(undefined);
          try {
            validateDocumentFiles(chosen, documents);
            if (!projectId) { onFiles?.(chosen); return; }
            setBusy(true);
            const result = await uploadDocuments(projectId, chosen);
            const failed = result.documents.filter(r => r.document?.status === 'error').length;
            announce(`${result.documents.length} documents stored privately. ${failed ? `${failed} extraction failures; originals kept.` : 'Text ready for the lead.'}`);
            await Promise.all([query.refetch(), client.invalidateQueries({ queryKey: ['project-brief', projectId] })]);
          } catch (e) { setError(e); } finally { setBusy(false); }
        }} />
      </Field>
      {busy && <p role="status" className="text-xs text-muted">Uploading and extracting document text…</p>}
      <ErrorNote error={error || query.error} />
      <ul className="space-y-2 text-[13px]">{files.map((file, i) => <li key={i} className="break-words">{file.name} · Ready to upload</li>)}
        {documents.map(reference => <li key={reference.reference_id} className="break-words">
          <p>{reference.caption} · {reference.document?.status === 'ready' ? 'Text ready' : reference.document?.status === 'error' ? 'Extraction failed' : 'Awaiting extraction'}</p>
          {reference.document?.error && <p className="text-wait">{reference.document.error}</p>}
          {reference.document?.truncated && <p className="text-muted">Extraction is bounded; ask the lead to check the original for omitted content.</p>}
          <a className="inline-flex min-h-11 items-center underline" href={`/api/projects/${projectId}/brief/references/${reference.reference_id}`}>Download {reference.reference_id}</a>
        </li>)}
      </ul>
      <p className="font-medium text-[13px]">Draft the brief from these documents</p>
      <p className="text-xs text-muted">Start a team with helpers and leave Task empty. At kickoff the lead reads the documents as untrusted evidence, proposes a cited brief, then asks clarifying questions on the working list. Use the proposal banner’s Review action and accept the fields you want. Images need visual reading; unreadable files need a text description.</p>
    </div>
  </details>;
}
