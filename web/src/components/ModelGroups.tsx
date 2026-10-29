import { useEffect, useId, useState } from "react";
import { ChevronDown } from "lucide-react";
import { announce, SearchInput } from "./ui";

export type ModelRow = { id: string; label: string; source?: string; efforts?: string[] };
export interface ModelGroup { id: string; label: string; models: ModelRow[] }

export const FIRST = 6;  // a collapsed group shows this many
const KEY = (person: string) => `acc.models.open.${person}`;
const load = (person: string): Record<string, boolean> => {
  try { return JSON.parse(localStorage.getItem(KEY(person)) ?? "{}") ?? {}; } catch { return {}; }
};
const save = (person: string, open: Record<string, boolean>) => {
  try { localStorage.setItem(KEY(person), JSON.stringify(open)); } catch { /* private window: this visit only */ }
};

/** Available models per provider (owner: "Cursor's list is incredibly long"): each group shows its first few with
 *  Show all N, remembered per person in this browser, and one filter narrows every group at once. */
export function ModelGroups({ groups, person }: { groups: ModelGroup[]; person: string }) {
  const [open, setOpen] = useState<Record<string, boolean>>(() => load(person));
  const [filter, setFilter] = useState("");
  const base = useId();
  useEffect(() => setOpen(load(person)), [person]);
  const query = filter.trim().toLowerCase();
  const shown = groups.map((g) => ({ ...g, matches: query ? g.models.filter((m) => `${m.label} ${m.id}`.toLowerCase().includes(query)) : g.models }))
    .filter((g) => !query || g.matches.length > 0);
  const total = shown.reduce((n, g) => n + g.matches.length, 0);
  useEffect(() => {  // the one live region: say how many match once typing settles
    if (!query) return;
    const timer = setTimeout(() => announce(`${total} ${total === 1 ? "model matches" : "models match"}`), 500);
    return () => clearTimeout(timer);
  }, [query, total]);
  const toggle = (id: string) => {
    const next = { ...open, [id]: !open[id] };
    setOpen(next);
    save(person, next);
  };
  return (
    <>
      <div className="border-b border-line px-4 py-3">
        <SearchInput value={filter} onChange={setFilter} placeholder="Filter models" className="w-full sm:w-72" />
      </div>
      {query && !shown.length ? <p className="px-4 py-6 text-center text-[13px] text-muted">No models match “{filter.trim()}”.</p> : (
        <div className="grid divide-y divide-line md:grid-cols-2 md:divide-x md:divide-y-0">
          {shown.map((g) => {
            const expanded = !!query || !!open[g.id] || g.matches.length <= FIRST;
            const list = expanded ? g.matches : g.matches.slice(0, FIRST);
            const listId = `${base}-${g.id}`;
            return (
              <section key={g.id} aria-labelledby={`${listId}-label`} className="min-w-0 p-4 text-[13px]">
                <h3 id={`${listId}-label`} className="mb-2 text-xs font-medium text-muted">{g.label}
                  <span className="font-normal text-faint"> · {query ? `${g.matches.length} of ${g.models.length}` : g.models.length} {g.models.length === 1 ? "model" : "models"}</span></h3>
                <ul id={listId} aria-label={`${g.label} models`}>
                  {list.map((m) => (
                    <li key={m.id} className="flex min-w-0 justify-between gap-3 py-1"><span className="min-w-0 text-fg [overflow-wrap:anywhere]">{m.label}</span>
                      <span className="truncate font-mono text-xs text-faint">{m.efforts?.length ? m.efforts.join(", ") : m.id}</span></li>
                  ))}
                </ul>
                {!query && g.matches.length > FIRST && (
                  <button type="button" aria-expanded={!!open[g.id]} aria-controls={listId} onClick={() => toggle(g.id)}
                    className="mt-1.5 inline-flex min-h-7 items-center gap-1 rounded-md text-xs font-medium text-accent underline-offset-2 hover:underline pointer-coarse:min-h-11">
                    <ChevronDown size={13} aria-hidden="true" className={open[g.id] ? "rotate-180" : undefined} />
                    {open[g.id] ? `Show the first ${FIRST}` : `Show all ${g.models.length}`}<span className="sr-only"> {g.label} models</span>
                  </button>
                )}
              </section>
            );
          })}
        </div>
      )}
    </>
  );
}
