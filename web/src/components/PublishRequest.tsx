import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { post } from "../lib/api";
import { publishFactsLine, publishWarnings, where, type PublishFacts } from "../lib/publish";
import type { PublishRequest } from "../lib/types";
import { OFFLINE_TITLE, useOffline, useRole } from "../lib/queries";
import { useConfirm } from "./confirm";
import { Button, Note, errorWords } from "./ui";

/** One summary of a publish request, the same in Inbox, Overview and Publishing (UX review IN-1): the facts on one
    line, then each warning as a note. */
export function PublishSummary({ request, facts: showFacts = true }: { request: PublishFacts; facts?: boolean }) {
  const facts = showFacts && publishFactsLine(request);
  const warnings = publishWarnings(request);
  return (
    <div className="space-y-1">
      {facts && <p className="text-xs text-muted">{facts}</p>}
      {warnings.map((w) => <Note key={w.text} tone={w.tone} icon={AlertTriangle} className="w-fit">{w.text}</Note>)}
    </div>
  );
}

type Actionable = Pick<PublishRequest, "id" | "status"> & PublishFacts;

/** Approve, Reject and Unpublish. Approving a request with a warning asks first, naming what happens. */
export function PublishActions({ request }: { request: Actionable }) {
  const queryClient = useQueryClient();
  const offline = useOffline();
  const act = useMutation({
    mutationFn: ({ verb, body }: { verb: string; body?: object }) => post(`/api/publish/${request.id}/${verb}`, body),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["publish"] });
      queryClient.invalidateQueries({ queryKey: ["attention"] });
    },
  });
  const confirm = useConfirm();
  const { owner } = useRole();
  const replaces = request.replaces;
  const warnings = publishWarnings(request).filter((w) => w.tone === "wait");
  const approve = async () => {
    if (replaces) {
      const ok = await confirm({
        title: `Replace the live site ${request.host}?`, danger: true, confirm: "Replace the live site",
        body: <div className="space-y-2">
          <p>{request.host} is live now. Approving points it somewhere else, for everyone who uses it:</p>
          <ul className="list-disc space-y-1 pl-5">
            <li>Now: port <span className="tnum font-mono text-fg">{replaces.port}</span>{replaces.project ? <> (project <span className="text-fg">{replaces.project}</span>)</> : " (no project)"}, on {where(replaces.access)}{replaces.decided_by ? `, approved by ${replaces.decided_by}` : ""}</li>
            <li>After: port <span className="tnum font-mono text-fg">{request.port ?? "?"}</span>, on {where(request.access)}</li>
          </ul>
          {warnings.length > 0 && <ul className="list-disc space-y-1 pl-5 text-wait">{warnings.map((w) => <li key={w.text}>{w.text}</li>)}</ul>}
          <p>Reject it unless you meant to move this name.</p>
        </div>,
      });
      if (ok) act.mutate({ verb: "approve", body: { confirm_replace: true } });
      return;
    }
    if (warnings.length && !(await confirm({
      title: `Publish ${request.host}?`, danger: request.access === "public", confirm: "Approve and publish",
      body: <div className="space-y-2">
        <p>Approving creates its DNS record and web server site now: {request.host} will serve port <span className="tnum font-mono text-fg">{request.port ?? "?"}</span> on {where(request.access)}.</p>
        <ul className="list-disc space-y-1 pl-5 text-wait">{warnings.map((w) => <li key={w.text}>{w.text}</li>)}</ul>
        <p>Reject it if this isn't the app you expect on that port.</p>
      </div>,
    }))) return;
    act.mutate({ verb: "approve" });
  };
  if (act.error) return <span className="max-w-64 text-xs text-bad" role="alert">{errorWords(act.error).message}</span>;
  if (request.status === "pending_approval") {
    if (!owner) return <span className="text-xs text-faint">Waiting for an owner</span>;
    return (
      <div className="flex gap-1">
        <Button size="sm" variant={replaces ? "danger" : "primary"} onClick={approve} disabled={act.isPending || offline} title={offline ? OFFLINE_TITLE : undefined}>{replaces || warnings.length ? "Review and approve" : "Approve"}</Button>
        <Button size="sm" variant="ghost" onClick={() => act.mutate({ verb: "reject" })} disabled={act.isPending || offline} title={offline ? OFFLINE_TITLE : undefined}>Reject</Button>
      </div>
    );
  }
  if (request.status === "applied" && request.action === "publish") {
    return <Button size="sm" variant="danger" disabled={act.isPending || offline} title={offline ? OFFLINE_TITLE : undefined}
      onClick={async () => (await confirm({ title: `Unpublish ${request.host}?`, body: "Its Caddy site and its DNS record are removed. The app keeps running on its port.", confirm: "Unpublish", danger: true })) && act.mutate({ verb: "unpublish" })}>Unpublish</Button>;
  }
  return null;
}
