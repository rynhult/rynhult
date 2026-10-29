import clsx from "clsx";
import { ChevronDown, type LucideIcon, Search, X } from "lucide-react";
import {
  cloneElement, Fragment, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactElement, type ReactNode,
  type SelectHTMLAttributes, type TextareaHTMLAttributes, useEffect, useId, useRef, useState,
} from "react";
import { focusableElements, useDismissableLayer, useRovingFocus } from "./keyboard";

/* Rynhult design system v2: the only place visual primitives are defined. Pages compose these.
   Scale: text 11/12/13/13.5/15/20 px; spacing 4px grid; radius 6 (controls) / 10 (surfaces). */

type Variant = "primary" | "secondary" | "ghost" | "danger";

function preventClickThrough(event: MouseEvent) {
  if (event.detail > 1) {
    event.preventDefault();
    event.stopImmediatePropagation();
  } else {
    document.removeEventListener("click", preventClickThrough, true);
  }
}

/** Shared appearance for action buttons and real navigation links. */
export function buttonClass({ variant = "secondary", size = "md", className }: { variant?: Variant; size?: "sm" | "md"; className?: string } = {}) {
  return clsx(
    "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-md font-medium transition-colors",
    // Native disabled buttons must still catch pointer hits; passing through can target the layer underneath.
    "disabled:cursor-not-allowed disabled:opacity-50 [&_svg]:shrink-0",
    size === "sm" ? "h-7 px-2.5 text-[12.5px]" : "h-8 px-3 text-[13px]",
    "pointer-coarse:h-11 pointer-coarse:min-w-11",  // 44 px tap targets on touch screens (phone pass)
    variant === "primary" && "bg-accent text-accent-ink shadow-sm hover:brightness-110",
    variant === "secondary" && "border border-line-strong bg-panel-2 text-fg hover:bg-panel-3",
    variant === "ghost" && "text-muted hover:bg-panel-2 hover:text-fg",
    variant === "danger" && "border border-bad/40 text-bad hover:bg-bad/10",
    className,
  );
}

export function Button({ variant = "secondary", size = "md", className, ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" }) {
  return <button type="button" {...props} className={buttonClass({ variant, size, className })} />;
}

/** Icon-only button: always has an accessible name and a tooltip. On touch screens it grows to 40px (UX review PH-2);
    desktop rows keep their 28px density. */
export function IconButton({ label, icon: Icon, className, ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { label: string; icon: LucideIcon }) {
  return (
    <button type="button" aria-label={label} title={label} {...props}
      className={clsx("inline-grid size-7 shrink-0 pointer-coarse:size-11 place-items-center rounded-md text-muted transition-colors hover:bg-panel-2 hover:text-fg disabled:opacity-50", className)}>
      <Icon size={15} aria-hidden="true" />
    </button>
  );
}

/** Page frame: one width and rhythm for every page. */
export function Page({ children, wide, className }: { children: ReactNode; wide?: boolean; className?: string }) {
  return <div className={clsx("mx-auto w-full space-y-6 px-4 pb-6 pt-5 md:px-8 md:pb-10 md:pt-7", wide ? "max-w-[1600px]" : "max-w-[1280px]", className)}>{children}</div>;
}

/** Whether a media query matches now, following changes (a phone rotating, a window resized). */
export function useMedia(query: string) {
  const get = () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(query).matches;
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

export function useDocumentTitle(title?: string) {
  useEffect(() => { document.title = title ? `${title} · Rynhult` : "Rynhult"; }, [title]);
}

export function PageHeader({ title, description, actions, meta }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; meta?: ReactNode }) {
  useDocumentTitle(typeof title === "string" ? title : undefined);
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        <h1 className="text-[20px] font-semibold leading-tight tracking-[-0.01em] text-fg">{title}</h1>
        {description && <p className="mt-1 max-w-[70ch] text-[13px] text-muted">{description}</p>}
        {meta && <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">{meta}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return <section className={clsx("min-w-0 rounded-[10px] border border-line bg-panel shadow-card", className)}>{children}</section>;
}

export function CardHeader({ title, action, subtitle, icon: Icon }: { title: ReactNode; action?: ReactNode; subtitle?: ReactNode; icon?: LucideIcon }) {
  return (
    <div className="flex min-h-12 flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-2.5">
      <div className="flex min-w-0 max-sm:basis-full flex-1 items-center gap-2.5">
        {Icon && <Icon size={15} className="shrink-0 text-faint" aria-hidden="true" />}
        <div className="min-w-0">
          <h2 className="break-words text-[13px] font-semibold text-fg">{title}</h2>
          {subtitle && <p className="break-words text-xs text-muted">{subtitle}</p>}
        </div>
      </div>
      {action && <div className="flex max-w-full flex-wrap items-center gap-1.5">{action}</div>}
    </div>
  );
}

/** Quiet label above a group of sections or fields. */
export function SectionLabel({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex items-center justify-between">
      <h2 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">{children}</h2>
      {action}
    </div>
  );
}

type Tone = "neutral" | "accent" | "ok" | "wait" | "bad" | "info";
const TONE_TEXT: Record<Tone, string> = { neutral: "text-muted", accent: "text-accent", ok: "text-ok", wait: "text-wait", bad: "text-bad", info: "text-info" };
const TONE_BG: Record<Tone, string> = {
  neutral: "bg-panel-2 border border-line", accent: "bg-accent/12", ok: "bg-ok/12", wait: "bg-wait/12", bad: "bg-bad/12", info: "bg-info/12",
};

export function Badge({ children, tone = "neutral", className, title }: { children: ReactNode; tone?: Tone; className?: string; title?: string }) {
  return (
    <span title={title} className={clsx("inline-flex h-5 shrink-0 items-center gap-1 whitespace-nowrap rounded px-1.5 text-[11px] font-medium leading-none",
      TONE_BG[tone], TONE_TEXT[tone], className)}>{children}</span>
  );
}

export function Dot({ tone = "neutral", pulse, className, large }: { tone?: Tone | "idle"; pulse?: boolean; className?: string; large?: boolean }) {
  const bg = { neutral: "bg-faint", idle: "bg-idle", accent: "bg-accent", ok: "bg-ok", wait: "bg-wait", bad: "bg-bad", info: "bg-info" }[tone];
  const dot = large ? "size-2.5 2xl:size-3" : "size-2";  // large: the status board, read from across a room
  return (
    <span className={clsx("relative inline-flex shrink-0", dot, className)} aria-hidden="true">
      {pulse && <span className={clsx("absolute inline-flex size-full animate-ping rounded-full opacity-60", bg)} />}
      <span className={clsx("relative inline-flex rounded-full", dot, bg)} />
    </span>
  );
}

/** Headline number with an optional icon, detail line and meter. */
export function Stat({ label, value, detail, icon: Icon, tone, meter, href }: {
  label: string; value: ReactNode; detail?: ReactNode; icon?: LucideIcon; tone?: Tone; meter?: number; href?: string;
}) {
  const body = (
    <>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-muted">{label}</span>
        {Icon && <Icon size={15} className={clsx("text-faint", tone && tone !== "neutral" && TONE_TEXT[tone])} aria-hidden="true" />}
      </div>
      <div className={clsx("tnum mt-1.5 text-[22px] font-semibold leading-none tracking-[-0.02em]", tone && tone !== "neutral" ? TONE_TEXT[tone] : "text-fg")}>{value}</div>
      {detail && <div className="mt-1.5 text-xs text-faint [overflow-wrap:anywhere]">{detail}</div>}
      {meter !== undefined && <Meter value={meter} className="mt-3" label={label} />}
    </>
  );
  const cls = "block rounded-[10px] border border-line bg-panel px-4 py-3.5 shadow-card";
  return href ? <a href={href} className={clsx(cls, "transition-colors hover:border-line-strong")}>{body}</a> : <div className={cls}>{body}</div>;
}

/** A status board panel (DESIGN.md, Status board): a card with an uppercase title and a summary, whose body scrolls
    on its own when the screen is smaller than the board. Type steps up one size at 2xl (a 1920 px screen). */
export function BoardPanel({ title, icon: Icon, summary, children, className }: {
  title: string; icon?: LucideIcon; summary?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section aria-label={title} className={clsx("flex min-h-0 flex-col min-w-0 rounded-[10px] border border-line bg-panel shadow-card", className)}>
      <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5 2xl:px-5 2xl:py-3">
        <h2 className="flex items-center gap-2 text-[12px] font-semibold uppercase tracking-[0.06em] text-muted 2xl:text-[14px]">
          {Icon && <Icon size={15} className="text-faint" aria-hidden="true" />}{title}</h2>
        {summary && <span className="tnum text-[13px] text-muted 2xl:text-[16px]">{summary}</span>}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto" tabIndex={0} role="region" aria-label={`${title} (scrolls)`}>{children}</div>
    </section>
  );
}

/** A status board headline number: big enough to read from across a room; tone only when something is wrong. */
export function BoardStat({ label, value, tone, detail }: { label: string; value: ReactNode; tone?: Tone; detail?: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] font-medium uppercase tracking-[0.06em] text-muted 2xl:text-[13px]">{label}</div>
      <div className={clsx("tnum text-[26px] font-semibold leading-tight tracking-[-0.02em] 2xl:text-[34px]", tone && tone !== "neutral" ? TONE_TEXT[tone] : "text-fg")}>{value}</div>
      {detail && <div className="truncate text-[12px] text-faint 2xl:text-[14px]">{detail}</div>}
    </div>
  );
}

/** Usage bar: accent below 60%, amber to 85%, red above. */
export function Meter({ value, className, label }: { value: number; className?: string; label?: string }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className={clsx("h-1.5 overflow-hidden rounded-full bg-panel-3", className)} role="meter" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <div className={clsx("h-full rounded-full transition-[width]", pct > 85 ? "bg-bad" : pct > 60 ? "bg-wait" : "bg-accent")} style={{ width: `${Math.max(pct, 1.5)}%` }} />
    </div>
  );
}

const tabKey = (id: string) => id.replace(/[^A-Za-z0-9_-]+/g, "-");

/** ARIA tabs (UX review SE-8): one Tab stop (the selected tab), Arrow keys, Home and End move and select, and with an
    `id` each tab controls the `TabPanel` given the same id. The selected tab is scrolled into view (a deep link on a
    phone), the strip fades at an edge that has more tabs beyond it, and `phone` names the tabs a phone shows; the
    others go under "More" there (UX review SE-7). */
export function Tabs<T extends string>({ tabs, value, onChange, className, phone, id, label }: {
  tabs: readonly { id: T; label: ReactNode; count?: number }[]; value: T; onChange: (id: T) => void; className?: string;
  phone?: readonly T[];
  /** Links the tabs to a `TabPanel` with the same id. */
  id?: string;
  /** The tab list's accessible name, when the page has more than one. */
  label?: string;
}) {
  const strip = useRef<HTMLDivElement>(null);
  const [fade, setFade] = useState({ left: false, right: false });
  useEffect(() => {
    const el = strip.current;
    if (!el) return;
    const measure = () => setFade({ left: el.scrollLeft > 1, right: el.scrollLeft + el.clientWidth < el.scrollWidth - 1 });
    el.querySelector<HTMLElement>("[aria-selected=true]")?.scrollIntoView({ block: "nearest", inline: "nearest" });
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
export function TabPanel({ id, value, className, children }: { id: string; value: string; className?: string; children: ReactNode }) {
  return <div role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-tab-${tabKey(value)}`} className={className}>{children}</div>;
}

/** Menus opt into roving focus; mixed-content panels keep normal Tab navigation.
    Escape/actions return to the trigger; outside clicks and Tab retain their destination. */
export function Popover({ label, trigger, children, className, align = "right", wide, menu = false }: {
  label: string; trigger: ReactNode; children: ReactNode | ((close: () => void) => ReactNode); className?: string;
  align?: "left" | "right"; wide?: boolean; menu?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const enterLast = useRef(false);
  const id = useId();
  const { onFocusCapture, restoreFocus } = useDismissableLayer({
    open, root: box, onDismiss: () => setOpen(false), dismissOnOutside: true, closeOnTab: menu,
    initialFocus: () => {
      const items = panel.current ? focusableElements(panel.current, menu ? "[role=menuitem]" : undefined) : [];
      return (enterLast.current ? items.at(-1) : items[0]) ?? null;
    },
  });
  const close = () => { restoreFocus(); setOpen(false); };
  const onMenuKey = useRovingFocus(panel, { selector: "[role=menuitem]", orientation: "vertical", typeahead: true });
  return (
    <div ref={box} onFocusCapture={onFocusCapture} className={clsx("relative shrink-0", className)}>
      <button type="button" aria-expanded={open} aria-controls={id} aria-haspopup={menu ? "menu" : undefined} aria-label={typeof trigger === "string" ? undefined : label}
        onClick={(event) => { event.currentTarget.focus(); enterLast.current = false; setOpen(!open); }}
        onKeyDown={(event) => {
          if (!menu || event.defaultPrevented || event.nativeEvent.isComposing || event.ctrlKey || event.altKey || event.metaKey || !["ArrowDown", "ArrowUp"].includes(event.key)) return;
          event.preventDefault(); enterLast.current = event.key === "ArrowUp";
          if (open && panel.current) {
            const items = focusableElements(panel.current, "[role=menuitem]");
            (enterLast.current ? items.at(-1) : items[0])?.focus();
          } else setOpen(true);
        }}
        className="inline-flex h-8 min-w-8 pointer-coarse:h-11 pointer-coarse:min-w-11 items-center justify-center gap-1 rounded-md px-2 text-[13px] font-medium text-muted transition-colors hover:bg-panel-2 hover:text-fg aria-expanded:bg-panel-2 aria-expanded:text-fg">
        {trigger}
      </button>
      {open && (
        <div ref={panel} id={id} role={menu ? "menu" : "group"} aria-label={label} onKeyDown={menu ? (event) => {
          onMenuKey(event);
          if (event.key === " " && !event.defaultPrevented && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229
            && !event.ctrlKey && !event.altKey && !event.metaKey && event.target instanceof HTMLAnchorElement
            && event.target.getAttribute("role") === "menuitem" && event.target.getAttribute("aria-disabled") !== "true") {
            event.preventDefault(); event.target.click();
          }
        } : undefined}
          className={clsx("absolute z-30 mt-1.5 max-h-[70vh] overflow-y-auto rounded-[10px] border border-line-strong bg-panel p-1.5 shadow-pop",
            align === "right" ? "right-0" : "left-0", wide ? "w-[20rem] max-w-[calc(100vw-2rem)]" : "min-w-44")}>
          {typeof children === "function" ? children(close) : children}
        </div>
      )}
    </div>
  );
}

/** Segmented control for a small set of filters. */
export function Segmented<T extends string>({ options, value, onChange, label }: { options: readonly { id: T; label: ReactNode }[]; value: T; onChange: (id: T) => void; label: string }) {
  const root = useRef<HTMLDivElement>(null);
  const onKeyDown = useRovingFocus(root, { selector: "[role=radio]", onNavigate: (item) => item.click() });
  return (
    <div ref={root} role="radiogroup" aria-label={label} onKeyDown={onKeyDown} className="inline-flex max-w-full flex-wrap items-center gap-1 rounded-md border border-line bg-panel p-0.5 text-[12.5px]">
      {options.map((o) => (
        <button type="button" key={o.id} role="radio" aria-checked={value === o.id}
          tabIndex={value === o.id || (!options.some(x => x.id === value) && o === options[0]) ? 0 : -1}
          onClick={() => onChange(o.id)}
          className={clsx("min-h-8 pointer-coarse:min-h-11 min-w-0 max-w-full break-words rounded px-2.5 py-1 transition-colors", value === o.id ? "bg-panel-3 font-medium text-fg" : "text-muted hover:text-fg")}>{o.label}</button>
      ))}
    </div>
  );
}

/** Independent choices: each chip is a native toggle button, grouped by its legend. */
export function ToggleChipGroup({ label, options, selected, onChange }: {
  label: ReactNode; options: { id: string; label?: ReactNode; title?: string }[]; selected: string[]; onChange: (next: string[]) => void;
}) {
  return (
    <fieldset className="space-y-1.5">
      <legend className="text-xs font-medium text-muted">{label}</legend>
      <div className="flex flex-wrap gap-1.5">
        {options.map((option) => {
          const on = selected.includes(option.id);
          return (
            <button key={option.id} type="button" aria-pressed={on} title={option.title}
              onClick={() => onChange(on ? selected.filter((id) => id !== option.id) : [...selected, option.id])}
              className={clsx("rounded-md border px-2 py-1 text-xs transition-colors",
                on ? "border-accent bg-accent/15 text-fg" : "border-line-strong text-muted hover:text-fg")}>
              {option.label ?? option.id}
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}

export function Toolbar({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx("flex flex-wrap items-center gap-2", className)}>{children}</div>;
}

export function SearchInput({ value, onChange, placeholder = "Search", className }: { value: string; onChange: (v: string) => void; placeholder?: string; className?: string }) {
  return (
    <label className={clsx("relative flex h-8 pointer-coarse:h-11 min-w-48 items-center", className)}>
      <Search size={14} className="pointer-events-none absolute left-2.5 text-faint" aria-hidden="true" />
      <input type="search" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} aria-label={placeholder}
        className="h-full w-full rounded-md border border-line bg-panel pl-8 pr-2.5 text-[13px] text-fg placeholder:text-faint focus:border-accent focus:outline-none" />
    </label>
  );
}

/* One polite live region for the whole app (UX review SE-9, R8): state changes are announced once, by the page that
   knows about them, instead of every badge being its own live region. */
const listeners = new Set<(text: string) => void>();
export function announce(text: string) { listeners.forEach((listener) => listener(text)); }
export function Announcer() {
  const [text, setText] = useState("");
  useEffect(() => {
    // Clear first so the same sentence twice is still read twice.
    const listener = (next: string) => { setText(""); window.setTimeout(() => setText(next), 50); };
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  }, []);
  return <div aria-live="polite" role="status" aria-label="Announcements" className="sr-only">{text}</div>;
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded border border-line-strong bg-panel-2 px-1 font-sans text-[11px] font-medium text-muted">{children}</kbd>;
}

const fieldClass = "rounded-md border border-line-input bg-bg px-3 py-1.5 text-[13px] text-fg placeholder:text-faint transition-colors focus:border-accent focus:outline-none disabled:opacity-60";

/** Fields are full width unless the caller gives a width (w-36, w-64…); both classes together would fight in CSS. */
const field = (extra: string, className?: string) =>
  clsx(fieldClass, /(^|\s)w-/.test(className ?? "") ? null : "w-full", extra, className);

export const Input = (props: InputHTMLAttributes<HTMLInputElement>) => <input {...props} className={field("h-8 pointer-coarse:h-11", props.className)} />;
export const Textarea = (props: TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} className={field("", props.className)} />;
export const Select = (props: SelectHTMLAttributes<HTMLSelectElement>) => <select {...props} className={field("h-8 pointer-coarse:h-11", props.className)} />;

export function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactElement }) {
  // The hint sits outside the <label> so it never becomes part of the control's accessible name;
  // it is linked as a description instead.
  const hintId = useId();
  return (
    <div className="space-y-1.5">
      <label className="block space-y-1.5">
        <span className="text-xs font-medium text-muted">{label}</span>
        {hint ? cloneElement(children as ReactElement<{ "aria-describedby"?: string }>, { "aria-describedby": hintId }) : children}
      </label>
      {hint && <p id={hintId} className="text-xs text-faint">{hint}</p>}
    </div>
  );
}

export function Dialog({ open, onClose, title, children, wide, description, footer, dismissible = true, sheet }: {
  open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean | "xl"; description?: ReactNode;
  /** Pinned below the scrolling body, so primary actions never fall below the fold. */
  footer?: ReactNode;
  /** False for forms: a stray click on the backdrop must not discard what was typed (Esc and Close still work). */
  dismissible?: boolean;
  /** A full-height panel from the left edge (the phone navigation drawer). The title is its accessible name only, and
      `children` lay out and scroll themselves. */
  sheet?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const { onFocusCapture } = useDismissableLayer({
    open, root: panel, onDismiss: onClose, modal: true,
    initialFocus: (element) => element.contains(document.activeElement) ? null
      : focusableElements(element, "input, select, textarea, button:not([aria-label='Close']), a[href]")[0]
        ?? focusableElements(element, "button")[0] ?? element,
  });
  useEffect(() => {
    if (!open) return;
    return () => {
      // The rest of a double-click must not activate the page exposed by closing.
      document.addEventListener("click", preventClickThrough, true);
    };
  }, [open]);
  if (!open) return null;
  return (
    <div className={sheet ? "fixed inset-0 z-50 flex bg-black/55"
      : "fixed inset-0 z-50 flex items-start justify-center overflow-y-auto overscroll-contain bg-black/55 p-3 pt-[5vh] backdrop-blur-[2px] md:p-4 md:pt-[7vh]"}
      onMouseDown={(event) => { event.preventDefault(); if (dismissible) onClose(); }}>
      <div ref={panel} tabIndex={-1} className={sheet ? "relative flex h-full w-72 max-w-[85vw] flex-col border-r border-line bg-panel pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] shadow-pop"
        : clsx("flex max-h-[90vh] w-full flex-col rounded-xl border border-line bg-panel shadow-pop",
          wide === "xl" ? "max-w-5xl" : wide ? "max-w-3xl" : "max-w-lg")}
        onFocusCapture={onFocusCapture}
        onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={title}>
        {sheet ? <>
          <IconButton label="Close" icon={X} onClick={onClose} className="absolute right-2 top-3.5 z-10" />
          <div className="min-h-0 flex-1">{children}</div>
        </> : <>
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-line px-5 py-3.5">
          <div>
            <h2 className="text-[15px] font-semibold">{title}</h2>
            {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
          </div>
          <IconButton label="Close" icon={X} onClick={onClose} className="-mr-1.5" />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-5">{children}</div>
        {footer && <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-line bg-panel px-5 py-3">{footer}</div>}
        </>}
      </div>
    </div>
  );
}

/** Loading, error and empty are different states: never show "nothing here" for a request that failed or hasn't returned. */
export function QueryState<T>({ query, isEmpty, empty, loading, errorTitle, children }: {
  query: { data: T | undefined; error: unknown; isLoading: boolean; refetch?: () => unknown };
  isEmpty?: (data: T) => boolean; empty?: ReactNode; loading?: ReactNode;
  /** What failed, in words ("Couldn't read the handoff notes."); the server's message follows it. */
  errorTitle?: string;
  children: (data: T) => ReactNode;
}) {
  if (query.data === undefined) {
    if (query.error) return <div className="p-4"><ErrorNote error={query.error} title={errorTitle} onRetry={query.refetch} /></div>;
    return <>{loading ?? <LoadingRows />}</>;
  }
  return (
    <>
      {query.error ? <div className="px-4 pt-3"><StaleNote error={query.error} /></div> : null}
      {isEmpty?.(query.data) ? empty : children(query.data)}
    </>
  );
}

/** A note about one item that needs attention (amber) or is broken (red): the exception, never the healthy case. */
export function Note({ tone = "wait", icon: Icon, children, className, role }: {
  tone?: "wait" | "bad"; icon?: LucideIcon; children: ReactNode; className?: string; role?: "status" | "alert";
}) {
  return (
    <p role={role} className={clsx("flex items-start gap-1.5 rounded-md border px-3 py-1.5 text-xs [overflow-wrap:anywhere]",
      tone === "bad" ? "border-bad/30 bg-bad/10 text-bad" : "border-wait/30 bg-wait/10 text-wait", className)}>
      {Icon && <Icon size={13} className="mt-px shrink-0" aria-hidden="true" />}<span className="min-w-0">{children}</span>
    </p>
  );
}

/** Shown above data that is still displayed but no longer refreshing. */
export function StaleNote({ error }: { error: unknown }) {
  return <Note role="status">Showing the last data received; refresh failed: {errorWords(error).message}</Note>;
}

/** A failure, with Retry when the caller can try again (QueryState passes its query's refetch). */
/** What a failed request means, in words (UX review ER-1). A 4xx answer is Rynhult's own sentence and is shown as it is; a
 *  5xx answer or a network failure gets a plain sentence, with the server's raw text under Details. */
export function errorWords(error: unknown): { message: string; raw?: string } {
  const raw = error instanceof Error ? error.message : String(error);
  const status = (error as { status?: number } | null)?.status;
  if (error instanceof TypeError && !status) return { message: "Rynhult didn't answer. Check your connection and try again.", raw };
  if (status === undefined || status < 500) return { message: raw };
  const message = status === 503 ? "That service isn't available right now. Try again in a moment."
    : status === 504 ? "That took too long to answer. Try again."
    : status === 502 ? "A service Rynhult relies on didn't answer as expected. Try again; Details says which."
    : "Something went wrong inside Rynhult. Try again; if it keeps happening, the server log has the cause.";
  return { message, raw: raw && raw !== "Internal Server Error" ? raw : undefined };
}

export function ErrorNote({ error, title, onRetry, action }: { error: unknown; title?: string; onRetry?: () => unknown; action?: ReactNode }) {
  if (!error) return null;
  const { message, raw } = errorWords(error);
  if (message.startsWith('Machine offline:')) return <p className="px-3 py-2 text-[13px] text-muted">{message}</p>;
  const body = <>
    {title ? <><b className="font-medium">{title}</b> {message}</> : message}
    {raw && <details className="mt-1 text-xs"><summary className="cursor-pointer w-fit">Details</summary><span className="font-mono [overflow-wrap:anywhere]">{raw}</span></details>}
  </>;
  if (!onRetry && !action) return <div role="alert" className="rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-[13px] text-bad [overflow-wrap:anywhere]">{body}</div>;
  return (
    <div role="alert" className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-[13px] text-bad">
      <div className="min-w-0 [overflow-wrap:anywhere]">{body}</div>
      <span className="flex gap-1.5">{action}{onRetry && <Button size="sm" onClick={() => onRetry()}>Retry</Button>}</span>
    </div>
  );
}

export function Empty({ children, icon: Icon, action }: { children: ReactNode; icon?: LucideIcon; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-10 text-center">
      {Icon && <span className="mb-1 grid size-9 place-items-center rounded-lg border border-line bg-panel-2 text-faint"><Icon size={17} aria-hidden="true" /></span>}
      <div className="max-w-md text-[13px] text-muted">{children}</div>
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse rounded-md bg-panel-2", className)} aria-hidden="true" />;
}

/** Placeholder rows while a list loads. */
export function LoadingRows({ rows = 3 }: { rows?: number }) {
  return (
    <div className="divide-y divide-line" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3.5"><Skeleton className="h-3.5 w-40" /><Skeleton className="ml-auto h-3.5 w-20" /></div>
      ))}
    </div>
  );
}

/** Key/value pairs (details panels). */
export function Meta({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-4 gap-y-2 text-[13px]">
      {items.map(([k, v], i) => (<Fragment key={i}><dt className="text-muted">{k}</dt><dd className="min-w-0 break-words text-fg">{v}</dd></Fragment>))}
    </dl>
  );
}
