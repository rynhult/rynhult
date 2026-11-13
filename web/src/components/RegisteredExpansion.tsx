import { useLayoutEffect } from 'react';

/** Browser-window expansion. Keep the canonical slot and external editor mounted. */
export default function RegisteredExpansion({ element, host }: {
  element: HTMLDivElement; host?: HTMLElement | null;
}) {
  useLayoutEffect(() => {
    if (!host) return;
    const previousZ = host.style.zIndex;
    host.style.zIndex = '41';
    const hidden = new Map<HTMLElement, { inert: boolean; aria: string | null; visibility: string }>();
    const observers: MutationObserver[] = [];
    let child: HTMLElement = element;
    while (child.parentElement) {
      const parent = child.parentElement, active = child;
      const hide = () => {
        for (const sibling of parent.children) {
          if (!(sibling instanceof HTMLElement) || sibling === active || sibling === host || hidden.has(sibling)
            || sibling.getAttribute('aria-label') === 'Announcements'
            || sibling.matches('[role="dialog"], [role="alertdialog"]') || sibling.querySelector('[role="dialog"], [role="alertdialog"]')) continue;
          hidden.set(sibling, { inert: sibling.inert, aria: sibling.getAttribute('aria-hidden'), visibility: sibling.style.visibility });
          sibling.inert = true; sibling.setAttribute('aria-hidden', 'true'); sibling.style.visibility = 'hidden';
        }
      };
      hide();
      const observer = new MutationObserver(hide); observer.observe(parent, { childList: true }); observers.push(observer);
      child = parent;
    }
    const resize = () => {
      element.style.top = `${window.visualViewport?.offsetTop ?? 0}px`;
      element.style.height = `${window.visualViewport?.height ?? innerHeight}px`;
    };
    resize();
    window.addEventListener('resize', resize);
    window.visualViewport?.addEventListener('resize', resize);
    window.visualViewport?.addEventListener('scroll', resize);
    const keyboard = (event: KeyboardEvent) => {
      if (event.isComposing || [...document.querySelectorAll<HTMLElement>('[role="dialog"], [role="alertdialog"]')]
        .some(dialog => dialog !== element && !dialog.contains(element) && dialog.getClientRects().length && getComputedStyle(dialog).visibility !== 'hidden')) return;
      if (event.key === 'Tab') {
        const controls = [...element.querySelectorAll<HTMLElement>('button:not(:disabled), textarea, input, summary, [tabindex="0"]'),
          ...host.querySelectorAll<HTMLElement>('textarea:not(:disabled)')].filter(el => el.getClientRects().length);
        const index = controls.indexOf(document.activeElement as HTMLElement);
        event.preventDefault(); controls[(index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length]?.focus();
      }
    };
    window.addEventListener('keydown', keyboard, true);
    element.querySelector<HTMLButtonElement>('[data-collapse-terminal]')?.focus({ preventScroll: true });
    return () => {
      host.style.zIndex = previousZ;
      for (const observer of observers) observer.disconnect();
      for (const [sibling, previous] of hidden) {
        sibling.inert = previous.inert; sibling.style.visibility = previous.visibility;
        if (previous.aria === null) sibling.removeAttribute('aria-hidden'); else sibling.setAttribute('aria-hidden', previous.aria);
      }
      window.removeEventListener('keydown', keyboard, true);
      window.removeEventListener('resize', resize);
      window.visualViewport?.removeEventListener('resize', resize);
      window.visualViewport?.removeEventListener('scroll', resize);
    };
  }, [element, host]);
  return null;
}
