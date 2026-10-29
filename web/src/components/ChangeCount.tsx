import clsx from "clsx";
import type { SessionDetail } from "../lib/types";

/** "+40 −2 · 3 files" for a session row; nothing when it hasn't changed anything. */
export function ChangeCount({ changes, className }: { changes: SessionDetail["changes"]; className?: string }) {
  if (!changes?.git || !changes.files) return null;
  return (
    <span className={clsx("tnum whitespace-nowrap", className)}
      title={changes.comparison_ref ? `Current branch compared with its merge-base with ${changes.comparison_ref}`
        : changes.since_launch === true ? "Changed in its workspace since the session started"
        : changes.since_launch === false ? "Uncommitted workspace changes" : "Workspace changes"}>
      <span className="text-ok">+{changes.additions}</span> <span className="text-bad">−{changes.deletions}</span>
      <span className="text-faint"> · {changes.files} file{changes.files === 1 ? "" : "s"}</span>
    </span>
  );
}
