import { useEffect, useRef, type FocusEvent, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react";
import { navigationIndex, type NavigationOptions } from "../lib/keyboard";

const focusable = "a[href], button, input:not([type=hidden]), select, textarea, summary, [tabindex], [contenteditable=true]";

export function focusableElements(root: HTMLElement, selector = focusable) {
  return [...root.querySelectorAll<HTMLElement>(selector)].filter((item) =>
    !item.matches(":disabled, [aria-disabled=true]") && !item.closest("[hidden], [inert]")
    && item.getClientRects().length > 0 && getComputedStyle(item).visibility !== "hidden");
}

type DismissReason = "escape" | "outside" | "tab";
type LayerOptions = {
  open: boolean; root: RefObject<HTMLElement | null>; onDismiss: (reason: DismissReason) => void; modal?: boolean;
  dismissOnOutside?: boolean; closeOnTab?: boolean; initialFocus?: (root: HTMLElement) => HTMLElement | null;
};

type Layer = { root: HTMLElement; modal: boolean; outside: boolean; closeOnTab: boolean; dismiss: (reason: DismissReason) => void };
const layers: Layer[] = [];

function onLayerKey(event: KeyboardEvent) {
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
  const top = layers.at(-1);
  if (event.key === "Escape" && top) {
    event.preventDefault(); event.stopPropagation(); top.dismiss("escape");
    return;
  }
  if (event.key !== "Tab") return;
  if (top?.closeOnTab) top.dismiss("tab");
  const modal = [...layers].reverse().find((layer) => layer.modal);
  if (!modal) return;
  const items = focusableElements(modal.root).filter((item) => item.tabIndex >= 0);
  const first = items[0] ?? modal.root, last = items.at(-1) ?? modal.root;
  const active = document.activeElement;
  // A roving menu item can sit beyond the modal's last normal Tab stop.
  const before = active && !!(first.compareDocumentPosition(active) & Node.DOCUMENT_POSITION_PRECEDING);
  const after = active && !!(last.compareDocumentPosition(active) & Node.DOCUMENT_POSITION_FOLLOWING);
  if (!items.length || active === modal.root || !modal.root.contains(active)
    || (!event.shiftKey && (active === last || after)) || (event.shiftKey && (active === first || before))) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  }
}

function onLayerPointer(event: MouseEvent) {
  const top = layers.at(-1);
  if (top?.outside && event.target instanceof Node && !top.root.contains(event.target)) top.dismiss("outside");
}

/** One Escape owner; the innermost modal still owns Tab while a non-modal child is open. */
export function useDismissableLayer({ open, root, ...options }: LayerOptions) {
  const latest = useRef(options);
  latest.current = options;
  const previous = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const element = root.current;
    if (!open || !element) return;
    const opener = previous.current ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    previous.current = opener;
    let restore = true;
    const layer: Layer = {
      root: element, modal: !!latest.current.modal, outside: !!latest.current.dismissOnOutside,
      closeOnTab: !!latest.current.closeOnTab,
      dismiss: (reason) => { restore = reason === "escape"; latest.current.onDismiss(reason); },
    };
    // Child effects run first when nested scopes mount together.
    const child = layers.findIndex((entry) => element.contains(entry.root));
    layers.splice(child < 0 ? layers.length : child, 0, layer);
    if (layers.length === 1) {
      document.addEventListener("keydown", onLayerKey);
      document.addEventListener("mousedown", onLayerPointer);
    }
    const initial = latest.current.initialFocus
      ? latest.current.initialFocus(element)
      : layer.modal && !element.contains(document.activeElement)
        ? focusableElements(element).find((item) => item.tabIndex >= 0) ?? element : null;
    initial?.focus();
    return () => {
      layers.splice(layers.indexOf(layer), 1);
      if (!layers.length) {
        document.removeEventListener("keydown", onLayerKey);
        document.removeEventListener("mousedown", onLayerPointer);
      }
      // Do not steal autofocus from a replacement dialog or a newly focused page.
      if (restore && opener?.isConnected && (element.contains(document.activeElement) || document.activeElement === document.body)) opener.focus();
      previous.current = null;
    };
  }, [open, root]);
  return {
    restoreFocus: () => { if (previous.current?.isConnected) previous.current.focus(); },
    onFocusCapture: (event: FocusEvent<HTMLElement>) => {
      if (open && !previous.current && event.relatedTarget instanceof HTMLElement && !event.currentTarget.contains(event.relatedTarget)) previous.current = event.relatedTarget;
    },
  };
}

export function useRovingFocus(root: RefObject<HTMLElement | null>, options: NavigationOptions & {
  selector: string; onNavigate?: (item: HTMLElement) => void; typeahead?: boolean;
}) {
  const buffer = useRef<{ text: string; time: number; element: HTMLElement | null }>({ text: "", time: 0, element: null });
  return (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.defaultPrevented || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || event.ctrlKey || event.altKey || event.metaKey) return;
    if (event.target instanceof HTMLElement && event.target.closest("input, textarea, select, [contenteditable=true], [contenteditable='']")) return;
    const items = root.current ? focusableElements(root.current, options.selector) : [];
    const current = items.findIndex((item) => item === document.activeElement);
    if (current < 0) return;
    if (buffer.current.element !== root.current) buffer.current = { text: "", time: 0, element: root.current };
    let next = navigationIndex(event.key, current, items.length, options);
    if (next !== null) buffer.current.text = "";
    else if (options.typeahead && event.key.length === 1 && event.key !== " ") {
      event.preventDefault();
      const now = Date.now(), key = event.key.toLocaleLowerCase();
      let text = now - buffer.current.time > 1000 ? key : buffer.current.text + key;
      if ([...text].every((char) => char === key)) text = key;
      buffer.current = { text, time: now, element: root.current };
      const matches = (index: number) => (items[index].getAttribute("aria-label") ?? items[index].textContent ?? "").trim().toLocaleLowerCase().startsWith(text);
      if (text.length > 1 && matches(current)) next = current;
      else for (let offset = 1; offset <= items.length; offset++) {
        const index = (current + offset) % items.length;
        if (matches(index)) { next = index; break; }
      }
    }
    if (next === null) return;
    event.preventDefault();
    items[next].focus();
    options.onNavigate?.(items[next]);
  };
}
