import { useInstance } from "../lib/queries";
import clsx from "clsx";
import { CornerDownLeft, CornerDownRight, FolderGit2, type LucideIcon, Moon, Plus, Search, TerminalSquare } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useProjects, useGroupAdministration, useRole, useSessions } from "../lib/queries";
import { navFor } from "./nav";
import { Kbd } from "./ui";
import { sessionState } from "./session-bits";
import { useDismissableLayer } from "./keyboard";
import { navigationIndex } from "../lib/keyboard";

interface Command { id: string; group: string; label: string; hint?: string; icon: LucideIcon; run: () => void; keywords?: string }

type PaletteProps = {
  open: boolean; onClose: () => void; onNewSession: () => void; onToggleTheme: () => void;
};

/** ⌘K: jump to any page, session or project, or run a global action. */
export function CommandPalette({ open, ...props }: PaletteProps) {
  return open ? <Palette {...props} /> : null;
}

function Palette({ onClose, onNewSession, onToggleTheme }: Omit<PaletteProps, "open">) {
  const nav = navFor(useRole().owner, useGroupAdministration(), useInstance().features);
  const navigate = useNavigate();
  const sessions = useSessions();
  const projects = useProjects();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const { onFocusCapture, restoreFocus } = useDismissableLayer({ open: true, root: panel, modal: true, onDismiss: onClose });

  const commands = useMemo<Command[]>(() => {
    const go = (to: string) => () => navigate(to);
    return [
      { id: "new", group: "Actions", label: "New session", icon: Plus, run: onNewSession, keywords: "launch start agent" },
      { id: "theme", group: "Actions", label: "Change theme (System, Light, Dark)", icon: Moon, run: onToggleTheme },
      ...(sessions.data ?? []).filter((s) => s.desired_state !== "archived").map((s) => ({
        id: `s${s.id}`, group: "Sessions", label: s.name, hint: `${s.project} · ${sessionState(s).label}`,
        icon: TerminalSquare, keywords: `${s.project} ${s.provider} ${s.tmux_name}`,
        run: s.management === "external" && s.remote_url ? () => { window.open(s.remote_url!, "_blank", "noopener"); } : go(`/sessions/${s.id}`),
      })),
      ...(projects.data ?? []).filter((p) => !p.archived).map((p) => ({
        id: `p${p.id}`, group: "Projects", label: p.name, hint: p.repo ?? p.slug, icon: FolderGit2, run: go(`/projects/${p.id}`), keywords: p.slug,
      })),
      ...nav.flatMap((g) => g.items).map((n) => ({ id: `n${n.to}`, group: "Go to", label: n.label, icon: n.icon, run: go(n.to) })),
    ];
  }, [sessions.data, projects.data, navigate, onNewSession, onToggleTheme]);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    const searchAll: Command = { id: "search", group: "Conversations", label: `Search conversations for “${query.trim()}”`,
      icon: Search, run: () => navigate(`/search?q=${encodeURIComponent(query.trim())}`) };
    return [...commands.filter((c) => `${c.label} ${c.hint ?? ""} ${c.keywords ?? ""} ${c.group}`.toLowerCase().includes(q)), searchAll];
  }, [commands, query, navigate]);

  useEffect(() => setActive(0), [query]);
  useEffect(() => { list.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" }); }, [active]);

  const choose = (c?: Command) => { if (!c) return; restoreFocus(); onClose(); c.run(); };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.target !== input.current || e.defaultPrevented || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229 || e.ctrlKey || e.altKey || e.metaKey) return;
    const next = navigationIndex(e.key, active, results.length, { orientation: "vertical", loop: false, homeEnd: false });
    if (next !== null) { e.preventDefault(); setActive(next); }
    else if (e.key === "Enter") { e.preventDefault(); choose(results[active]); }
  };
  let lastGroup = "";
  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center bg-black/50 p-3 pt-[12vh] backdrop-blur-[2px]" onMouseDown={onClose}>
      <div ref={panel} tabIndex={-1} className="w-full max-w-xl overflow-hidden rounded-xl border border-line bg-panel shadow-pop" role="dialog" aria-modal="true" aria-label="Jump to…" aria-describedby="palette-scope"
        onMouseDown={(e) => e.stopPropagation()} onKeyDown={onKey} onFocusCapture={onFocusCapture}>
        <p id="palette-scope" className="px-4 pb-2 pt-3 text-xs text-muted">Pages, projects, sessions and commands</p>
        <div className="flex items-center gap-2.5 border-b border-line px-4">
          <CornerDownRight size={16} className="text-faint" aria-hidden="true" />
          <input ref={input} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Jump to…" aria-label="Jump to…" aria-describedby="palette-scope"
            role="combobox" aria-expanded="true" aria-controls="palette-list" aria-activedescendant={results[active] ? `cmd-${results[active].id}` : undefined}
            className="h-12 min-w-0 flex-1 bg-transparent text-[14px] text-fg placeholder:text-faint focus:outline-none" />
          <Kbd>esc</Kbd>
        </div>
        <div ref={list} id="palette-list" role="listbox" tabIndex={-1} className="max-h-[52vh] overflow-y-auto p-1.5">
          {results.length === 0 && <p className="px-3 py-8 text-center text-[13px] text-muted">No matches.</p>}
          {results.map((c, i) => {
            const header = c.group !== lastGroup;
            lastGroup = c.group;
            return (
              <div key={c.id}>
                {header && <div className="px-2.5 pb-1 pt-2.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">{c.group}</div>}
                <div id={`cmd-${c.id}`} role="option" aria-selected={i === active} data-index={i}
                  onMouseMove={() => setActive(i)} onClick={() => choose(c)}
                  className={clsx("flex cursor-pointer items-center gap-3 rounded-md px-2.5 py-2 text-[13px]", i === active ? "bg-panel-3 text-fg" : "text-muted")}>
                  <c.icon size={15} className="shrink-0" aria-hidden="true" />
                  <span className="truncate text-fg">{c.label}</span>
                  {c.hint && <span className={clsx("truncate text-xs", i === active ? "text-muted" : "text-faint")}>{c.hint}</span>}
                  {i === active && <CornerDownLeft size={13} className="ml-auto shrink-0 text-faint" aria-hidden="true" />}
                </div>
              </div>
            );
          })}
        </div>
        <Link to="/search" onClick={onClose} className="flex min-h-10 items-center gap-2 border-t border-line px-4 py-2 text-[13px] text-muted underline underline-offset-2 hover:bg-panel-2 hover:text-fg">
          <Search size={15} aria-hidden="true" /> Search conversation content
        </Link>
      </div>
    </div>
  );
}
