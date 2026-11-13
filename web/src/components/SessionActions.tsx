import { CheckedResume } from "./shell/ResumeAction";
import { useSingleMutation as useMutation } from "../lib/mutations";
import clsx from "clsx";
import { useQueryClient } from "@tanstack/react-query";
import { Archive, CircleStop, ExternalLink, Hand, MoreHorizontal, Play } from "lucide-react";
import { useConfirm } from "./confirm";
import { post } from "../lib/api";
import type { SessionSummary } from "../lib/types";
import { Badge, Button, ErrorNote, Popover } from "./ui";
import { OFFLINE_TITLE, useOffline } from "../lib/queries";
import { isLive } from "./session-bits";

export function SessionActions({ session, compact, checkResume = false, primaryOnly = false, secondaryOnly = false }: { session: SessionSummary; compact?: boolean; checkResume?: boolean; primaryOnly?: boolean; secondaryOnly?: boolean }) {

  const queryClient = useQueryClient();
  const action = useMutation({
    mutationFn: (verb: string) => post(`/api/sessions/${session.id}/${verb}`),
    onSettled: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["sessions"] }),
        ...(checkResume ? [queryClient.invalidateQueries({ queryKey: ['resume-check', session.id] })] : []),
      ]);
    },
  });
  const confirm = useConfirm();
  const run = async (verb: string, ask?: { title: string; body: string; confirm: string }) => {
    if (ask && !(await confirm({ ...ask, danger: verb === "archive" }))) return;
    action.mutate(verb);
  };
  const size = compact ? "sm" : "md";
  const live = isLive(session);
  const offline = useOffline();
  const off = offline ? { disabled: true, title: OFFLINE_TITLE } : {};
  const resumeOff = session.machine_offline ? { disabled: true, title: "Machine offline. Resume returns when the machine checks in." } : off;
  if (session.management === "external") {
    if (primaryOnly) return null;
    return (
      <div className="flex items-center gap-1.5">
        {!primaryOnly && session.remote_url && (
          <a href={session.remote_url} target="_blank" rel="noreferrer"
            className="inline-flex h-7 pointer-coarse:h-11 items-center gap-1 rounded-md px-2 text-[13px] text-muted hover:bg-panel-2 hover:text-fg underline underline-offset-2"
            title="Open in Claude Remote Control"><ExternalLink size={14} /> {!compact && "Remote"}</a>
        )}
        {!compact && <Badge tone="neutral" className="whitespace-nowrap">{`observe-only · ${session.run_as}`}</Badge>}
      </div>
    );
  }
  const working = session.observed_state === "active" || session.observed_state === "waiting_input";
  const interrupt = () => run("interrupt");
  const stop = () => run("stop", { title: `Stop ${session.name}?`, body: "The process ends; the conversation is kept and Resume brings it back.", confirm: "Stop session" });
  const menuItem = "flex min-h-10 w-full items-center gap-2 rounded-md px-3 text-left text-[13px] hover:bg-panel-2";
  return (
    <div className={clsx("flex items-center gap-1.5", checkResume && "min-w-0 flex-1 flex-wrap")}>
      {!primaryOnly && session.remote_url && (
        <a href={session.remote_url} target="_blank" rel="noreferrer"
          className="inline-flex h-7 pointer-coarse:h-11 items-center gap-1 rounded-md px-2 text-[13px] text-muted hover:bg-panel-2 hover:text-fg underline underline-offset-2"
          title="Open in Claude Remote Control (claude.ai / mobile app)" aria-label="Open in Remote Control">
          <ExternalLink size={14} /> {!compact && "Remote"}
        </a>
      )}
      {/* With a mouse, Interrupt and Stop sit in the row. On touch they go behind "⋯", so a tap meant for the terminal
          link can't interrupt or stop an agent mid-edit (UX review OV-4). */}
      {!secondaryOnly && working && (
        <Button size={size} variant="ghost" onClick={interrupt} title="Interrupt: send Esc to stop the current turn" aria-label="Interrupt" {...off}
          className={clsx(live && "pointer-coarse:hidden")}>
          <Hand size={14} aria-hidden="true" /> {!compact && "Interrupt"}
        </Button>
      )}
      {!secondaryOnly && (live ? (
        <>
          <Button size={size} variant="ghost" onClick={stop} title="Stop (keeps the conversation)" aria-label="Stop" className="pointer-coarse:hidden" {...off}>
            <CircleStop size={14} aria-hidden="true" /> {!compact && "Stop"}
          </Button>
          <Popover menu label={`Actions for ${session.name}`} className="hidden pointer-coarse:block" trigger={<MoreHorizontal size={16} aria-hidden="true" />}>
            {(close) => <>
              {working && <button type="button" role="menuitem" tabIndex={-1} className={clsx(menuItem, "text-fg disabled:opacity-50")} {...off} onClick={() => { close(); interrupt(); }}>
                <Hand size={14} aria-hidden="true" /> Interrupt the current turn</button>}
              <button type="button" role="menuitem" tabIndex={-1} className={clsx(menuItem, "text-bad disabled:opacity-50")} {...off} onClick={() => { close(); void stop(); }}>
                <CircleStop size={14} aria-hidden="true" /> Stop session…</button>
            </>}
          </Popover>
        </>
      ) : session.desired_state !== "archived" && (
        checkResume ? <CheckedResume session={session} compact={compact} pending={action.isPending} resume={() => void run('resume')} />
          : <Button size={size} variant="ghost" onClick={() => run("resume")} title="Resume the same conversation" aria-label="Resume" {...resumeOff}>
            <Play size={14} aria-hidden="true" /> {!compact && "Resume"}
          </Button>
      ))}
      {!primaryOnly && !compact && !live && session.desired_state !== "archived" && (
        <Button size={size} variant="ghost" {...off} onClick={() => run("archive", { title: `Archive ${session.name}?`, body: "It leaves the session lists. The transcript stays on disk.", confirm: "Archive" })}>
          <Archive size={14} aria-hidden="true" /> Archive
        </Button>
      )}
      {action.error && (checkResume ? <div className="min-w-0 basis-full"><ErrorNote error={action.error} /></div> : <ErrorNote error={action.error} />)}
    </div>
  );
}
