/** Page chrome shared by the application frame. */
import type { ComponentProps, ReactNode } from 'react';
import clsx from 'clsx';
import * as UI from '../ui';
import { Tabs } from '../Tabs';

export function ShellPage(props: ComponentProps<typeof UI.Page>) {
  return <div data-shell-page className={clsx('shell-page', props.wide && 'shell-page-wide', props.className)}>{props.children}</div>;
}

export function Workbar(props: ComponentProps<typeof UI.PageHeader> & { breadcrumb?: ReactNode }) {
  return <ShellWorkbar {...props} />;
}
function ShellWorkbar({ title, description, actions, meta, breadcrumb }: ComponentProps<typeof UI.PageHeader> & { breadcrumb?: ReactNode }) {
  UI.useDocumentTitle(typeof title === 'string' ? title : undefined);
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

export function Surface(props: ComponentProps<typeof UI.Card>) {
  return <section className={clsx('shell-surface', props.className)}>{props.children}</section>;
}
export function SurfaceHeader(props: ComponentProps<typeof UI.CardHeader>) {
  return <div className="shell-surface-heading"><UI.CardHeader {...props} /></div>;
}

/** Retain tab IDs, roving focus, phone overflow and selected-tab visibility. */
export function PaneTabs<T extends string>(props: ComponentProps<typeof Tabs<T>>) {
  return <Tabs {...props} className={clsx(props.className, 'shell-pane-tabs')} />;
}
export function PageStack({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx('shell-page-stack', className)}>{children}</div>;
}
