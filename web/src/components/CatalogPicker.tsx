import { ToggleChipGroup } from "./ui";
import type { CatalogItem, Provider } from "../lib/types";

export function CatalogPicker({ title, items, selected, provider, onChange, query = "" }: {
  title: string; items: CatalogItem[]; selected: string[]; provider: Provider; onChange: (next: string[]) => void; query?: string;
}) {
  const q = query.trim().toLowerCase();
  const usable = items.filter((i) => i.providers.includes(provider) && (!q || `${i.id} ${i.name ?? ""} ${i.notes ?? ""}`.toLowerCase().includes(q)));
  if (!usable.length) return null;
  return (
    <ToggleChipGroup
      label={<>{title} <span className="tnum text-faint">{selected.filter((id) => items.some((i) => i.id === id)).length}/{items.filter((i) => i.providers.includes(provider)).length}</span></>}
      options={usable.map((item) => ({ id: item.id,
        title: `${item.name ?? item.id}\n${item.license} · ${item.source}${item.notes ? `\n${item.notes}` : ""}` }))}
      selected={selected} onChange={onChange} />
  );
}
