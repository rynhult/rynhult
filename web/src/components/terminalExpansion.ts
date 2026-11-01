import { useLayoutEffect, useRef, useState } from 'react';

export const TERMINAL_EXPANSION_KEYS = { grid: 'acc-terminal-expanded:grid', session: 'acc-terminal-expanded:session' };
export function expandedGridSession() {
  try { return localStorage.getItem(TERMINAL_EXPANSION_KEYS.grid) ?? ''; } catch { return ''; }
}

/** Browser-local presentation only. Keep terminal DOM, scrollback and socket mounted while changing geometry. */
export function useTerminalExpansion(sessionId: string, grid: boolean, enabled: boolean, onExpansionChange?: (expanded: boolean) => void) {
  const key = TERMINAL_EXPANSION_KEYS[grid ? 'grid' : 'session'];
  const [expanded, setExpanded] = useState(() => {
    try { return enabled && localStorage.getItem(key) === sessionId; } catch { return false; }
  });
  const root = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState({
    height: window.visualViewport?.height ?? window.innerHeight,
    top: window.visualViewport?.offsetTop ?? 0,
  });
  const [fullscreenError, setFullscreenError] = useState('');
  const change = (value: boolean) => {
    if (!value && document.fullscreenElement === root.current) void document.exitFullscreen().catch(() => {});
    setExpanded(value);
    try {
      if (value) localStorage.setItem(key, sessionId);
      else localStorage.removeItem(key);
    } catch { /* private mode: the current view still works */ }
  };

  useLayoutEffect(() => {
    if (!expanded || !root.current) return;
    onExpansionChange?.(true);
    const element = root.current;
    const hidden: { el: HTMLElement; inert: boolean; aria: string | null; visibility: string }[] = [];
    // Inert siblings along the ancestor chain, never the terminal's own ancestors.
    // Preserve their layout so collapsing does not clamp the page's scroll position.
    const seen = new Set<HTMLElement>();
    const observers: MutationObserver[] = [];
    let child: HTMLElement = element;
    while (child.parentElement) {
      const parent = child.parentElement;
      const activeChild = child;
      const hideSiblings = () => {
        for (const sibling of parent.children) {
          if (sibling === activeChild || !(sibling instanceof HTMLElement) || seen.has(sibling) || sibling.getAttribute('aria-label') === 'Announcements') continue;
          // Global dialogs (Jump to / New session) stay above the expanded surface.
          // Their existing modal layer owns focus/inertness while open.
          if (sibling.matches('[role="dialog"], [role="alertdialog"]') || sibling.querySelector('[role="dialog"], [role="alertdialog"]')) continue;
          seen.add(sibling);
          hidden.push({ el: sibling, inert: sibling.inert, aria: sibling.getAttribute('aria-hidden'), visibility: sibling.style.visibility });
          sibling.inert = true;
          sibling.setAttribute('aria-hidden', 'true');
          sibling.style.visibility = 'hidden';
        }
      };
      hideSiblings();
      // Responsive navigation and live updates can add siblings while expanded.
      const observer = new MutationObserver(hideSiblings);
      observer.observe(parent, {childList: true});
      observers.push(observer);
      child = parent;
      if (child === document.body) break;
    }
    const resize = () => setViewport({
      height: window.visualViewport?.height ?? window.innerHeight,
      top: window.visualViewport?.offsetTop ?? 0,
    });
    resize();
    window.addEventListener('resize', resize);
    window.visualViewport?.addEventListener('resize', resize);
    window.visualViewport?.addEventListener('scroll', resize);
    element.querySelector<HTMLButtonElement>('[data-collapse-terminal]')?.focus({ preventScroll: true });
    return () => {
      onExpansionChange?.(false);
      for (const observer of observers) observer.disconnect();
      window.removeEventListener('resize', resize);
      window.visualViewport?.removeEventListener('resize', resize);
      window.visualViewport?.removeEventListener('scroll', resize);
      for (const { el, inert, aria, visibility } of hidden) {
        el.inert = inert;
        el.style.visibility = visibility;
        if (aria === null) el.removeAttribute('aria-hidden');
        else el.setAttribute('aria-hidden', aria);
      }
      if (document.fullscreenElement === element) void document.exitFullscreen().catch(() => {});
      requestAnimationFrame(() => {
        if (element.isConnected) element.querySelector<HTMLButtonElement>('[data-expand-terminal]')?.focus({ preventScroll: true });
      });
    };
  }, [expanded, onExpansionChange]);

  const fullscreen = async () => {
    setFullscreenError('');
    try { await root.current?.requestFullscreen(); }
    catch { setFullscreenError('Browser full screen is unavailable. The terminal stays expanded.'); }
  };
  const focusBar = () => root.current?.querySelector<HTMLButtonElement>('[data-collapse-terminal]')?.focus({ preventScroll: true });
  return { expanded, root, focusBar, change, viewport, fullscreen, fullscreenError };
}
