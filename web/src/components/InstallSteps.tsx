import { useMachineLabel } from "../lib/machineLabel";
import { Download, ExternalLink, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";
import { Button } from "./ui";
import { CopyBlock } from "./CopyBlock";
export { CopyBlock, CopyButton } from "./CopyBlock";

/** How to install a CLI by hand where Rynhult can't (from the server's table, src/acc/server/agent_setup.py: every command
 * and link is the provider's own). */
export interface InstallGuide {
  label: string; system: "macos" | "linux" | "wsl"; shell: string; shell_command?: string | null;
  commands: { how: string; command: string; link: boolean }[]; docs: string; after: "signin" | "ready" | "endpoint";
}

const host = (url: string) => { try { return new URL(url).host; } catch { return url; } };

/** The numbered steps: open the right shell, run the official command, the provider's page, Check now, then sign in. */
export function InstallSteps({ guide, machine, onCheck, checking, onCard }: {
  guide: InstallGuide; machine?: string; onCheck?: () => void; checking?: boolean; onCard?: boolean;
}) {
  const [first, ...others] = guide.commands;
  const where = useMachineLabel(machine) ?? "this machine";
  return (
    <ol className="list-decimal space-y-3 pl-5 text-[13px] text-fg marker:text-muted" aria-label={`Install ${guide.label} on ${where}`}>
      <li className="space-y-2"><p>{guide.shell}</p>
        {guide.shell_command && <CopyBlock text={guide.shell_command} label="Copy the command to switch accounts" />}</li>
      <li className="space-y-2">
        {first.link ? (
          <p>Download <a href={first.command} className="inline-flex pointer-coarse:min-h-11 items-center gap-1 font-medium text-accent underline underline-offset-2">
            <Download size={13} aria-hidden="true" />{first.command.split("/").pop()}</a>, open it and drag {guide.label} to Applications, then open {guide.label} once
            (it offers to add the <code className="font-mono text-[12px]">ollama</code> command).</p>
        ) : (<>
          <p>Install it with {first.how}:</p>
          <CopyBlock text={first.command} label={`Copy the ${guide.label} install command`} />
        </>)}
        {others.map((c) => (
          <div key={c.command} className="space-y-1.5"><p className="text-xs text-muted">Or with {c.how}:</p>
            <CopyBlock text={c.command} label={`Copy the ${guide.label} install command (${c.how})`} /></div>
        ))}
      </li>
      <li><a href={guide.docs} target="_blank" rel="noreferrer" className="inline-flex pointer-coarse:min-h-11 items-center gap-1 text-accent underline underline-offset-2">
        {guide.label}'s install instructions on {host(guide.docs)}<ExternalLink size={12} aria-hidden="true" /><span className="sr-only"> (opens in a new tab)</span></a>
        {" "}if the command fails or you want another way.</li>
      <li className="space-y-2"><p>Back here, select <b>Check now</b>: Rynhult reads the version you installed.</p>
        {onCheck && <Button size="sm" onClick={onCheck} disabled={checking}>
          <RefreshCw size={13} className={checking ? "animate-spin" : ""} aria-hidden="true" /> Check now<span className="sr-only"> for {guide.label} on {where}</span></Button>}</li>
      <li>{guide.after === "signin" ? (onCard ? <>Sign in from this card: select <b>Log in</b> once it appears.</> : <>Sign in from {guide.label}'s card on this page (<b>Log in</b>).</>)
        : guide.after === "ready" ? <>{guide.label} has no sign-in of its own: give it models with a key, a model endpoint or its <code className="font-mono text-[12px]">/connect</code>.</>
        : <>{guide.label} has no sign-in: pull a model, then add it under <b>Model endpoints</b>.</>}</li>
      <li className="list-none text-xs text-muted">Step by step for every system: <Link to="/guide/User-Guide/Connect-an-Agent" className="text-accent underline underline-offset-2">Guide → Connect an agent</Link>.</li>
    </ol>
  );
}
