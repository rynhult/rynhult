import clsx from "clsx";
import { ChevronDown } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { useWorkingView, workingId } from "../lib/workingView";
import { useRovingFocus } from "./keyboard";
import { Popover } from "./ui";

const tabKey = (id: string) => id.replace(/[^A-Za-z0-9_-]+/g, "-");

/** ARIA tabs (UX review SE-8): one Tab stop (the selected tab), Arrow keys, Home and End move and select, and with an
    `id` each tab controls the `TabPanel` given the same id. The selected tab is revealed horizontally (a deep link on a
    phone), without scrolling the page. The strip fades at an edge that has more tabs beyond it; `phone` names the
    tabs a phone shows, and the others go under "More" there (UX review SE-7). */
export function Tabs<T extends string>({ tabs, value, onChange, className, phone, id, label }: {
  tabs: readonly { id: T; label: ReactNode; count?: number }[]; value: T; onChange: (id: T) => void; className?: string;
  phone?: readonly T[];
  /** Links the tabs to a `TabPanel` with the same id. */
  id?: string;
  /** The tab list's accessible name, when the page has more than one. */
  label?: string;
}) {
  const working = useWorkingView();
  if (id) id = workingId(id, working);
  const strip = useRef<HTMLDivElement>(null);
  const [fade, setFade] = useState({ left: false, right: false });
  useEffect(() => {
    const el = strip.current;
    if (!el) return;
    const measure = () => setFade({ left: el.scrollLeft > 1, right: el.scrollLeft + el.clientWidth < el.scrollWidth - 1 });
    const selected = el.querySelector<HTMLElement>("[aria-selected=true]");
    if (selected) {
      // A lower tab strip can be below the fold. Reveal only its horizontal overflow;
      // scrollIntoView also moves the page and hides the project heading on mount.
      const stripBounds = el.getBoundingClientRect();
      const tabBounds = selected.getBoundingClientRect();
      const left = stripBounds.left + el.clientLeft;
      const right = left + el.clientWidth;
      if (tabBounds.left < left) el.scrollLeft += tabBounds.left - left;
      else if (tabBounds.right > right) el.scrollLeft += tabBounds.right - right;
    }
    measure();
    el.addEventListener("scroll", measure, { passive: true });
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => { el.removeEventListener("scroll", measure); observer.disconnect(); };
  }, [value, tabs.length]);
  const onPhone = (t: T) => !phone || phone.includes(t) || t === value;
  const more = phone ? tabs.filter((t) => !onPhone(t.id)) : [];
  const onKeyDown = useRovingFocus(strip, { selector: "[role=tab]", orientation: "horizontal", onNavigate: (item) => item.click() });
  const mask = fade.left && fade.right ? "[mask-image:linear-gradient(to_right,transparent,black_24px,black_calc(100%-24px),transparent)]"
    : fade.right ? "[mask-image:linear-gradient(to_right,black_calc(100%-24px),transparent)]"
      : fade.left ? "[mask-image:linear-gradient(to_right,transparent,black_24px)]" : undefined;
  return (
    <div className={clsx("flex min-w-0 items-end border-b border-line", className)}>
      <div ref={strip} role="tablist" aria-label={label} onKeyDown={onKeyDown}
        className={clsx("flex min-w-0 flex-1 gap-1 overflow-x-auto [scrollbar-width:none]", mask)}>
        {tabs.map((t) => (
          <button key={t.id} type="button" role="tab" data-tab={t.id} aria-selected={value === t.id} tabIndex={value === t.id ? 0 : -1}
            id={id ? `${id}-tab-${tabKey(t.id)}` : undefined} aria-controls={id && value === t.id ? `${id}-panel` : undefined}
            onClick={() => onChange(t.id)}
            className={clsx("-mb-px inline-flex min-h-9 pointer-coarse:min-h-11 shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 pb-2.5 pt-1.5 text-[13px] transition-colors",
              value === t.id ? "border-accent font-medium text-fg" : "border-transparent text-muted hover:text-fg",
              !onPhone(t.id) && "max-sm:hidden")}>
            {t.label}
            {t.count !== undefined && <span className="tnum rounded bg-panel-2 px-1.5 text-[11px] text-muted">{t.count}</span>}
          </button>
        ))}
      </div>
      {more.length > 0 && (
        <Popover menu label="More" className="mb-1 sm:hidden" trigger={<>More <ChevronDown size={13} aria-hidden="true" /></>}>
          {(close) => more.map((t) => (
            <button key={t.id} type="button" role="menuitem" tabIndex={-1} onClick={() => { close(); onChange(t.id); }}
              className="flex min-h-10 pointer-coarse:min-h-11 w-full items-center justify-between gap-3 rounded-md px-3 text-left text-[13px] text-fg hover:bg-panel-2">
              {t.label}{t.count !== undefined && <span className="tnum rounded bg-panel-2 px-1.5 text-[11px] text-muted">{t.count}</span>}
            </button>
          ))}
        </Popover>
      )}
    </div>
  );
}

/** The panel a `Tabs` with the same `id` controls; labelled by the selected tab. */
export function TabPanel({ id, value, className, children, standalone = false }: { id: string; value: string; className?: string; children: ReactNode; standalone?: boolean }) {
  id = workingId(id, useWorkingView());
  return <div role={standalone ? undefined : "tabpanel"} id={`${id}-panel`} aria-labelledby={standalone ? undefined : `${id}-tab-${tabKey(value)}`} className={className}>{children}</div>;
}

