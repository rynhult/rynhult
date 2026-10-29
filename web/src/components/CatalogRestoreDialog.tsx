import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { post } from "../lib/api";
import type { Catalog } from "../lib/types";
import { Badge, Button, Dialog, ErrorNote, Field, Input, Select } from "./ui";

type RestoreRow = {
  key: string; id: string; kind: string; status: string; diff: string; collision: boolean; refused: string;
  flags: { level: string; message: string; files: string[] }[]; excluded_profiles: string[];
};
type Preview = { token: string; source: string; version: string; items: RestoreRow[] };

export function CatalogRestoreDialog({ onClose }: { onClose: () => void }) {
  const client = useQueryClient();
  const [source, setSource] = useState("installed");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [aliases, setAliases] = useState<Record<string, string>>({});
  const [acceptRisks, setAcceptRisks] = useState(false);
  const [result, setResult] = useState("");
  const review = useMutation({
    mutationFn: () => post<Preview>("/api/catalog/restore/preview", { source }),
    onSuccess: (value) => { setPreview(value); setAliases({}); setAcceptRisks(false); setResult(""); apply.reset(); },
  });
  const apply = useMutation({
    mutationFn: (ids: string[] | null) => post<Catalog>("/api/catalog/restore/apply", {
      token: preview!.token, ids, aliases, accept_risks: acceptRisks,
    }),
    onSuccess: (catalog, ids) => {
      client.setQueryData(["catalog"], catalog);
      const count = ids?.length ?? preview!.items.filter((r) => !r.collision && !r.refused).length;
      const skipped = ids ? 0 : preview!.items.filter((r) => r.collision || r.refused).length;
      setResult(`Restored ${count} built-in item${count === 1 ? "" : "s"} from ${preview!.version}.${skipped ? ` Skipped ${skipped} colliding or incompatible items.` : ""} Profile choices were preserved.`);
      setPreview(null);
    },
  });
  const busy = review.isPending || apply.isPending;
  const blocked = (row: RestoreRow) => !!row.refused || (row.collision && !aliases[row.key]?.trim())
    || (row.flags.some((f) => f.level === "bad") && !acceptRisks);
  const all = preview?.items.filter((r) => !r.collision && !r.refused) ?? [];
  return <Dialog open onClose={onClose} wide title="Restore built-ins" dismissible={false}
    description="Review built-in skills, agents and MCP servers. Custom items and profile choices stay in place."
    footer={<>
      <Button onClick={onClose} disabled={busy}>Close</Button>
      {preview && <Button variant="primary" disabled={busy || !all.length || all.some(blocked)} onClick={() => apply.mutate(null)}>
        {apply.isPending ? "Restoring…" : "Restore all"}
      </Button>}
    </>}>
    <div className="space-y-4">
      <div className="flex items-end gap-3">
        <Field label="Source">
          <Select id="restore-source" value={source} disabled={busy} onChange={(e) => {
            setSource(e.target.value); setPreview(null); setResult(""); review.reset(); apply.reset();
          }}>
            <option value="installed">This version</option>
            <option value="latest">Latest release</option>
          </Select>
        </Field>
        <Button disabled={busy} onClick={() => { setPreview(null); setResult(""); review.mutate(); }}>
          {review.isPending ? "Checking…" : "Preview"}
        </Button>
      </div>
      <p className="text-xs text-muted">Latest release uses the Updates repository and verifies the release bundle. GitHub checks are limited to once a minute.</p>
      {review.error && <ErrorNote error={review.error} />}
      {apply.error && <ErrorNote error={apply.error} />}
      <p role="status" aria-live="polite" className="text-sm text-fg">{result}</p>
      {preview && <>
        <p className="text-sm text-muted">Source: {preview.source === "installed" ? "This version" : "Latest stable release"} · {preview.version}</p>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input type="checkbox" checked={acceptRisks} onChange={(e) => setAcceptRisks(e.target.checked)} disabled={busy} />
          Accept flagged risks
        </label>
        <div className="divide-y divide-line">
          {preview.items.map((row) => <section key={row.key} className="space-y-2 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0 break-all font-mono text-xs"><span>{row.key}</span> <Badge>{row.status}</Badge></div>
              <Button size="sm" disabled={busy || blocked(row)} aria-label={`Restore ${row.key}`} onClick={() => apply.mutate([row.key])}>Restore</Button>
            </div>
            {row.refused && <p className="text-sm text-bad">Refused: {row.refused}</p>}
            {row.collision && <>
              <p className="text-sm text-wait">A custom item uses this ID and will be kept. Restore all skips this item. Choose an unused ID to restore a separate built-in copy.</p>
              <Field label={`Alternate ID for ${row.key}`}>
                <Input id={`alias-${row.kind}-${row.id}`} value={aliases[row.key] ?? ""} disabled={busy}
                  onChange={(e) => setAliases({ ...aliases, [row.key]: e.target.value })} autoComplete="off" spellCheck={false} />
              </Field>
            </>}
            {!!row.excluded_profiles.length && <p className="text-sm text-muted">Excluded by profile choices: {row.excluded_profiles.join(", ")}. Edit Profiles to include it again.</p>}
            {row.flags.map((flag, i) => <p key={i} className={flag.level === "bad" ? "text-sm text-wait" : "text-sm text-muted"}>
              {flag.level}: {flag.message}{flag.files.length ? ` (${flag.files.join(", ")})` : ""}
            </p>)}
            {row.diff && <details><summary className="cursor-pointer text-xs text-muted">View diff</summary>
              <pre tabIndex={0} role="region" aria-label="Restore diff" className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md bg-panel-2 p-3 font-mono text-xs">{row.diff}</pre>
            </details>}
          </section>)}
        </div>
      </>}
    </div>
  </Dialog>;
}
