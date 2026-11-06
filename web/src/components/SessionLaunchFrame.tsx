import {useId, useLayoutEffect, useRef, type ComponentProps} from 'react';
import {Dialog} from './ui';
/** Presentation only: both frames contain the same launch form and review. */
export function SessionLaunchFrame({workspace, recovery=false, ...props}: ComponentProps<typeof Dialog> & {workspace: boolean; recovery?: boolean}) {
  const id=useId();
  const frame=useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if(workspace && props.open) {
      frame.current?.querySelector<HTMLTextAreaElement>('[data-launch-task]')?.focus({preventScroll:true});
      const main=frame.current?.closest('main'); if(main)main.scrollTop=0;
    }
  }, [workspace,props.open]);
  if(!workspace)return <Dialog {...props}/>;
  if(!props.open)return null;
  return <section ref={frame} aria-labelledby={id} className="min-h-full min-w-0 bg-panel text-fg">
    <header className="border-b border-line px-6 py-4"><h1 id={id} className="text-lg font-semibold">{props.title}</h1></header>
    <div className={`mx-auto ${recovery ? 'max-w-5xl' : 'max-w-3xl'} space-y-6 px-6 py-8`}>
      {props.description && <p className="text-sm text-muted">{props.description}</p>}
      {props.children}
      {props.footer && <footer className="flex flex-wrap items-center gap-3 border-t border-line pt-5">{props.footer}</footer>}
    </div>
  </section>;
}
