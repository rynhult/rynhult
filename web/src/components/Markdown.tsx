import { Children, isValidElement, useRef, type ReactNode } from 'react';
import type { Components } from 'react-markdown';
import { CopyButton } from './InstallSteps';
function raw(children: ReactNode): string {
  return Children.toArray(children).map(child => isValidElement<{ children?: ReactNode }>(child) ? raw(child.props.children) : String(child)).join('');
}
/** Scrollable tables and code blocks are focusable (keyboard scrolling) named groups, not landmarks: a page can hold
 *  many with the same first line or headers, and landmarks must have unique names. */
function label(kind: string, detail: string): string {
  const short = detail.length > 60 ? `${detail.slice(0, 59)}…` : detail;
  return short ? `${kind}: ${short}` : kind;
}
function headers(children: ReactNode): string[] {
  const elements = (node: ReactNode) => Children.toArray(node).filter(isValidElement<{ children?: ReactNode }>);
  const head = elements(children).find(child => child.type === 'thead');
  const row = head && elements(head.props.children)[0];
  return row ? elements(row.props.children).map(cell => raw(cell.props.children).trim()).filter(Boolean) : [];
}
function Code({ children }: { children?: ReactNode }) {
  const target = useRef<HTMLPreElement>(null);
  const text = raw(children);
  return <div className="min-w-0 max-w-full space-y-2">
    <CopyButton text={text} label="Copy code" target={target} />
    <pre ref={target} role="group" aria-label={label('Code', text.trim().split('\n')[0])} tabIndex={0} className="max-w-full overflow-auto">{children}</pre>
  </div>;
}
export const markdownComponents: Components = {
  pre: ({ children }) => <Code>{children}</Code>,
  table: ({ children }) => <div role="group" aria-label={label('Table', headers(children).join(', '))} tabIndex={0} className="max-w-full overflow-auto"><table>{children}</table></div>,
};
