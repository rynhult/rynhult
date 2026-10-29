import { Copy, Download, ExternalLink, RefreshCw } from "lucide-react";
import { type RefObject, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { announce, Button } from "./ui";

/** Copy the exact displayed text, selecting it when clipboard access is unavailable. */
export function CopyButton({ text, label, target }: { text: string; label: string; target: RefObject<HTMLElement | null> }) {
  const [result, setResult] = useState('');
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setResult('Copied');
      announce('Copied');
    } catch {
      if (target.current) {
        target.current.focus();
        const range = document.createRange();
        range.selectNodeContents(target.current);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
      const message = 'Clipboard unavailable. Text selected; copy it manually.';
      setResult(message);
      announce(message);
    }
  };
  return <span className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
    <Button size="sm" className="h-auto! min-h-7 pointer-coarse:min-h-11 max-w-full whitespace-normal! break-all text-left" onClick={copy}><Copy size={13} aria-hidden="true" />{label}</Button>
    {result && <span className="text-xs text-muted">{result}</span>}
  </span>;
}

/** One thing to copy: a command shown in full, with a copy button. */
export function CopyBlock({ text, label, stacked = false }: { text: string; label: string; stacked?: boolean }) {
  const target = useRef<HTMLPreElement>(null);
  return (
    <div className={`min-w-0 max-w-full flex flex-wrap items-start gap-2 rounded-md border border-line bg-panel-2 p-3 ${stacked ? "flex-col" : ""}`}>
      {stacked && <CopyButton text={text} label={label} target={target} />}
      <pre ref={target} role="region" aria-label={label.replace(/^Copy /, '')} tabIndex={0} className={`min-w-0 max-w-full max-h-80 flex-1 overflow-auto whitespace-pre-wrap break-all font-mono text-[12px] ${stacked ? "w-full" : ""}`}><code>{text}</code></pre>
      {!stacked && <CopyButton text={text} label={label} target={target} />}
    </div>
  );
}

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
  const where = machine ?? "this machine";
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
