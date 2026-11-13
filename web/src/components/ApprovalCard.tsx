import { useState } from 'react';
import { visibleText } from '../lib/visibleText';
import { RiskLabel } from './RiskLabel';
import type { AttentionItem } from '../lib/types';

function Diff({ text }: { text: string }) {
  const [all, setAll] = useState(false);
  const lines = text.split('\n');
  return <>
    <pre aria-label="CLI diff" className="whitespace-pre-wrap break-all font-mono text-xs">{all ? text : lines.slice(0, 40).join('\n')}</pre>
    {lines.length > 40 && <button type="button" className="min-h-11 text-accent underline" onClick={() => setAll(!all)}>{all ? 'Show less' : `Show all (${lines.length} lines)`}</button>}
  </>;
}

const labels = new Set(['Rewrites history', 'Deletes files', 'Touches secrets paths', 'Spends money',
  'Restarts or stops services', 'Installs packages', 'Network / external message', 'No specific hint']);
function validFacts(value: unknown): value is NonNullable<AttentionItem['approval_card']> {
  if (!value || typeof value !== 'object') return false;
  const fields = value as Record<string, unknown>;
  return typeof fields.label === 'string' && labels.has(fields.label)
    && ['command', 'cwd', 'diff'].every(key => fields[key] === null || typeof fields[key] === 'string')
    && ['tool_name', 'file_path'].every(key => fields[key] == null || typeof fields[key] === 'string');
}

export function ApprovalCard({ facts, input, risk }: { facts: NonNullable<AttentionItem['approval_card']>; input?: string | null; risk?: AttentionItem['risk'] }) {
  if (!validFacts(facts)) return <section aria-label="CLI approval details" className="mt-3">
    <p>Approval details unavailable: invalid card data. Review the request in the terminal.</p>
  </section>;
  input = typeof input === 'string' ? input : null;
  const hidden = [facts.command, facts.cwd, facts.diff, facts.tool_name, facts.file_path, input].some(t => t != null && visibleText(t) !== t);
  const toolOnly = facts.command === null && facts.diff === null;
  return <section aria-label="CLI approval details" className="mt-3 min-w-0 space-y-2 rounded-md border border-line bg-panel-2 p-3">
    <div aria-label="Hints"><h3 className="text-sm font-medium">Hints (not checks)</h3><p>{facts.label}</p>{risk && <RiskLabel risk={risk} />}</div>
    {hidden && <p role="note">Hidden characters shown</p>}
    {facts.tool_name && <p>Tool: <span className="font-mono">{visibleText(facts.tool_name)}</span></p>}
    {facts.file_path && <pre aria-label="CLI file path" className="whitespace-pre-wrap break-all font-mono text-xs">{visibleText(facts.file_path)}</pre>}
    <p className="text-xs text-muted">Label is a hint, not a check</p>
    <p className="text-xs text-muted">Command</p>
    {facts.command !== null ? <pre aria-label="CLI command" className="whitespace-pre-wrap break-all font-mono text-xs">{visibleText(facts.command)}</pre> : <p>{toolOnly && input ? "This tool has no command; see tool input below." : "Command unavailable; input was not supplied or was too large."}</p>}
    <p className="text-xs text-muted">Working directory</p>
    {facts.cwd !== null ? <pre aria-label="CLI working directory" className="whitespace-pre-wrap break-all font-mono text-xs">{visibleText(facts.cwd)}</pre> : <p>Working directory not supplied by CLI (or exceeds display limit).</p>}
    <p className="text-xs text-muted">Unified diff</p>
    {facts.diff !== null ? <Diff key={facts.diff} text={visibleText(facts.diff)} /> : <p>No unified diff supplied; review the tool input when available.</p>}
    {input != null ? (toolOnly ? <div><h3>Tool input</h3><pre aria-label="Complete tool input" className="whitespace-pre-wrap break-all font-mono text-xs">{visibleText(input)}</pre></div> : <details><summary>Complete tool input</summary><pre className="whitespace-pre-wrap break-all font-mono text-xs">{visibleText(input)}</pre></details>) : <p>Complete tool input unavailable or too large (over 8 kB).</p>}
  </section>;
}
