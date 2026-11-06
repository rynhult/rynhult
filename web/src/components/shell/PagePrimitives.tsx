/** Opt-in page chrome. Standard layout delegates to the original primitives. */
import type { ComponentProps, ReactNode } from 'react';
import clsx from 'clsx';
import { useNewShell } from '../../lib/uiShell';
import * as Legacy from '../ui';
import { Tabs } from '../Tabs';

export function ShellPage(props: ComponentProps<typeof Legacy.Page>) {
  const shell = useNewShell();
  return shell ? <div data-shell-page className={clsx('shell-page', props.wide && 'shell-page-wide', props.className)}>{props.children}</div> : <Legacy.Page {...props} />;
}

export function Workbar(props: ComponentProps<typeof Legacy.PageHeader> & { breadcrumb?: ReactNode }) {
  const shell = useNewShell();
  return shell ? <ShellWorkbar {...props} /> : <Legacy.PageHeader {...props} />;
}
function ShellWorkbar({ title, description, actions, meta, breadcrumb }: ComponentProps<typeof Legacy.PageHeader> & { breadcrumb?: ReactNode }) {
  Legacy.useDocumentTitle(typeof title === 'string' ? title : undefined);
  return <header className="shell-workbar">
    <div className="min-w-0">
      {breadcrumb && <div className="shell-page-meta">{breadcrumb}</div>}
      <h1>{title}</h1>
      {description && <p className="shell-workbar-description">{description}</p>}
      {meta && <div className="shell-workbar-meta">{meta}</div>}
    </div>
    {actions && <div className="shell-workbar-actions">{actions}</div>}
  </header>;
}

export function Surface(props: ComponentProps<typeof Legacy.Card>) {
  const shell = useNewShell();
  return shell ? <section className={clsx('shell-surface', props.className)}>{props.children}</section> : <Legacy.Card {...props} />;
}
export function SurfaceHeader(props: ComponentProps<typeof Legacy.CardHeader>) {
  const shell = useNewShell();
  return shell ? <div className="shell-surface-heading"><Legacy.CardHeader {...props} /></div> : <Legacy.CardHeader {...props} />;
}

/** Retain tab IDs, roving focus, phone overflow and selected-tab visibility. */
export function PaneTabs<T extends string>(props: ComponentProps<typeof Tabs<T>>) {
  const shell = useNewShell();
  return <Tabs {...props} className={clsx(props.className, shell && 'shell-pane-tabs')} />;
}
export function PageStack({ children, className }: { children: ReactNode; className?: string }) {
  const shell = useNewShell();
  return <div className={clsx(shell ? 'shell-page-stack' : 'space-y-6', className)}>{children}</div>;
}
