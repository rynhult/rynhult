import { Copy } from "lucide-react";
import { type RefObject, useRef, useState } from "react";
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
