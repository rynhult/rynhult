import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, FileText, Image as ImageIcon } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { Artifact, SessionArtifacts, SessionDetail } from "../lib/types";
import { Card, Dialog, Empty, ErrorNote, Skeleton } from "./ui";

const TEXT_SHOWN = 256 * 1024;  // characters of a text artifact shown in the page; Download has all of it

const url = (session: SessionDetail, a: Artifact) =>
  `/api/sessions/${session.id}/artifacts/${a.name.split("/").map(encodeURIComponent).join("/")}`;
const versioned = (session: SessionDetail, a: Artifact) => `${url(session, a)}?v=${a.mtime}-${a.size}`;  // reload when it changes

const size = (bytes: number) =>
  bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;

/** The files the session's agent left in .acc/artifacts, polled like Changes: often during a turn, slowly when idle, and
 * once more when a turn ends. Shared by the tab and its count. */
export function useArtifacts(session: SessionDetail) {
  const working = ["active", "waiting_input"].includes(session.observed_state);
  const query = useQuery({
    queryKey: ["artifacts", session.id], queryFn: () => api<SessionArtifacts>(`/api/sessions/${session.id}/artifacts`),
    refetchInterval: working ? 10000 : 60000, retry: false,
  });
  const { refetch } = query;
  const wasWorking = useRef(working);
  useEffect(() => {
    if (wasWorking.current && !working) void refetch();
    wasWorking.current = working;
  }, [working, refetch]);
  return query;
}

function DownloadLink({ session, artifact }: { session: SessionDetail; artifact: Artifact }) {
  return (
    <a href={url(session, artifact)} download={artifact.name.split("/").pop()} aria-label={`Download ${artifact.name}`}
      className="inline-flex items-center gap-1 rounded text-xs text-muted hover:text-fg">
      <Download size={13} aria-hidden="true" /> Download
    </a>
  );
}

function Caption({ session, artifact }: { session: SessionDetail; artifact: Artifact }) {
  return (
    <div className="flex items-center justify-between gap-2 border-t border-line px-3 py-2">
      <span className="min-w-0 truncate font-mono text-[11.5px] text-fg" title={artifact.name}>{artifact.name}</span>
      <span className="flex shrink-0 items-center gap-2 text-[11px] text-faint">
        {size(artifact.size)}{!artifact.too_large && <DownloadLink session={session} artifact={artifact} />}
      </span>
    </div>
  );
}

function TooLarge({ artifact }: { artifact: Artifact }) {
  return <p className="px-3 py-6 text-center text-xs text-muted">Too large to show ({size(artifact.size)}).</p>;
}

function TextArtifact({ session, artifact }: { session: SessionDetail; artifact: Artifact }) {
  const text = useQuery({
    queryKey: ["artifact", session.id, artifact.name, artifact.mtime, artifact.size], enabled: !artifact.too_large,
    queryFn: async () => {
      const response = await fetch(url(session, artifact), { credentials: "same-origin" });
      if (!response.ok) throw new ApiError(response.status, "Couldn't open this file. It may have been moved or deleted; refresh the page and try again.");
      return response.text();
    },
  });
  const content = text.data ?? "";
  return (
    <Card className="overflow-hidden">
      <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
        <span className="flex min-w-0 items-center gap-2">
          <FileText size={14} className="shrink-0 text-faint" aria-hidden="true" />
          <span className="truncate font-mono text-[12px] text-fg" title={artifact.name}>{artifact.name}</span>
        </span>
        <span className="flex shrink-0 items-center gap-2 text-[11px] text-faint">
          {size(artifact.size)}{!artifact.too_large && <DownloadLink session={session} artifact={artifact} />}
        </span>
      </div>
      {artifact.too_large ? <TooLarge artifact={artifact} /> : text.error ? <div className="p-3"><ErrorNote error={text.error} /></div>
        : text.data === undefined ? <Skeleton className="m-3 h-20" /> : (
          <>
            <pre tabIndex={0} aria-label={`Contents of ${artifact.name}`}
              className="max-h-80 overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-[12px] text-muted focus-visible:outline-2 focus-visible:outline-accent">
              {content.slice(0, TEXT_SHOWN)}
            </pre>
            {content.length > TEXT_SHOWN && <p className="border-t border-line px-3 py-1.5 text-[11px] text-faint">Showing the first {size(TEXT_SHOWN)}; download it for the rest.</p>}
          </>
        )}
    </Card>
  );
}

export function Artifacts({ session }: { session: SessionDetail }) {
  const query = useArtifacts(session);
  const [open, setOpen] = useState<Artifact | null>(null);
  if (!query.data) return query.error ? <ErrorNote error={query.error} /> : <Skeleton className="h-40" />;
  const files = query.data.files;
  if (!files.length) {
    return <Card>
      <Empty icon={ImageIcon}>No artifacts yet. Screenshots, short videos, test logs and reports appear here when the agent leaves them for you.</Empty>
      <details className="px-4 pb-4 text-xs text-muted">
        <summary className="w-fit cursor-pointer hover:text-fg">Where do artifacts come from?</summary>
        <p className="mt-2">Agents save them in <span className="font-mono">.acc/artifacts/</span> in their working folder.</p>
      </details>
    </Card>;
  }
  const media = files.filter((a) => a.kind !== "text");
  const texts = files.filter((a) => a.kind === "text");
  return (
    <div className="space-y-4">
      {query.data.truncated && <p className="text-xs text-wait">Showing the first {files.length} files.</p>}
      {media.length > 0 && (
        <ul className="grid list-none grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] gap-3 p-0" aria-label="Screenshots and videos">
          {media.map((a) => (
            <li key={a.name}>
              <Card className="overflow-hidden">
                {a.too_large ? <TooLarge artifact={a} /> : a.kind === "image" ? (
                  <button type="button" onClick={() => setOpen(a)} aria-label={`Open ${a.name}`}
                    className="block w-full bg-panel-2 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-accent">
                    <img src={versioned(session, a)} alt={a.name} loading="lazy" className="mx-auto h-40 w-full object-contain" />
                  </button>
                ) : (
                  <video src={versioned(session, a)} controls preload="metadata" aria-label={a.name} className="h-40 w-full bg-black" />
                )}
                <Caption session={session} artifact={a} />
              </Card>
            </li>
          ))}
        </ul>
      )}
      {texts.map((a) => <TextArtifact key={a.name} session={session} artifact={a} />)}
      <Dialog open={open !== null} onClose={() => setOpen(null)} title={open?.name ?? ""} wide="xl"
        description={open ? size(open.size) : undefined}>
        {open && <img src={versioned(session, open)} alt={open.name} className="mx-auto max-h-[70vh] max-w-full object-contain" />}
      </Dialog>
    </div>
  );
}
