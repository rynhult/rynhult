import type { Catalog, MultiRunChoice, ProviderInfo } from "../lib/types";
import { useState } from "react";
import { Button, Field, Input, Select } from "./ui";

export const newRunChoice = (provider = "claude"): MultiRunChoice =>
  ({ provider, profile: null, model: "", options: {}, confirm_dangerous: false });

export const runOptions = (choice: MultiRunChoice, provider?: ProviderInfo, catalog?: Catalog) => {
  const defaults = catalog?.profiles.find((p) => p.id === choice.profile)?.[choice.provider] as Record<string, string | boolean> | undefined;
  return Object.fromEntries((provider?.options ?? []).map((o) => [o.key, choice.options[o.key] ?? defaults?.[o.key] ?? o.default ?? ""]));
};

export const runDangerous = (choice: MultiRunChoice, provider?: ProviderInfo, catalog?: Catalog) => {
  const options = runOptions(choice, provider, catalog);
  return provider?.options.some((o) => o.dangerous.includes(options[o.key])) ?? false;
};

export function MultiRunFields({ value, onChange, catalog, providers, models }: {
  value: MultiRunChoice[]; onChange: (choices: MultiRunChoice[]) => void; catalog?: Catalog; providers: ProviderInfo[];
  models: (provider: string) => { id: string; label: string }[];
}) {
  const update = (index: number, change: Partial<MultiRunChoice>) =>
    onChange(value.map((choice, i) => i === index ? { ...choice, confirm_dangerous: false, ...change } : choice));
  return <fieldset className="space-y-4 rounded-lg border border-line p-3">
    <legend className="px-1 font-medium">Multi-run choices</legend>
    <p className="text-xs text-muted">2 to 5 independent runs of the same task. Each gets its own worktree and budget check. Refused runs do not cancel the others.</p>
    {value.map((choice, i) => {
      const provider = providers.find((p) => p.id === choice.provider);
      const options = runOptions(choice, provider, catalog);
      return <fieldset key={i} className="min-w-0 space-y-3 border-t border-line pt-3">
        <legend className="px-1 text-xs text-muted">Run {i + 1}</legend>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={`Run ${i + 1}: Agent`}><Select value={choice.provider}
            onChange={(e) => update(i, { ...newRunChoice(e.target.value) })}>
            {providers.map((p) => <option key={p.id} value={p.id} disabled={p.installed === false}>{p.label}{p.installed === false ? " (not installed)" : ""}</option>)}
          </Select></Field>
          <Field label={`Run ${i + 1}: Profile`}><Select value={choice.profile ?? ""}
            onChange={(e) => update(i, { profile: e.target.value || null, options: {} })}>
            <option value="">None</option>
            {catalog?.profiles.filter((p) => !p.providers || p.providers.includes(choice.provider)).map((p) =>
              <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select></Field>
          <RunModel run={i + 1} value={choice.model} models={models(choice.provider)} placeholder={provider?.model_format}
            onChange={(model) => update(i, { model })} />
          {provider?.options.filter((o) => o.kind === "select").map((o) =>
            <Field key={o.key} label={`Run ${i + 1}: ${o.label}`} hint={o.help || undefined}>
              <Select value={String(options[o.key])} onChange={(e) => update(i, { options: { ...choice.options, [o.key]: e.target.value } })}>
                {o.choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
              </Select>
            </Field>)}
        </div>
        {provider?.options.filter((o) => o.kind === "bool").map((o) => <label key={o.key} className="flex items-start gap-2 text-xs">
          <input type="checkbox" checked={options[o.key] === true}
            onChange={(e) => update(i, { options: { ...choice.options, [o.key]: e.target.checked } })} />
          <span>Run {i + 1}: {o.label}</span>
        </label>)}
        {runDangerous(choice, provider, catalog) && <label className="flex items-start gap-2 rounded-md border border-bad/30 bg-bad/10 p-3 text-xs text-bad">
          <input type="checkbox" checked={choice.confirm_dangerous} onChange={(e) => update(i, { confirm_dangerous: e.target.checked })} />
          <span>Run {i + 1}: The agent will run commands without asking. I accept this permission mode; it is recorded in the audit log.</span>
        </label>}
        {value.length > 2 && <Button variant="ghost" onClick={() => onChange(value.filter((_, n) => n !== i))}>Remove run {i + 1}</Button>}
      </fieldset>;
    })}
    <Button disabled={value.length >= 5} onClick={() => onChange([...value, newRunChoice()])}>Add run</Button>
  </fieldset>;
}

/** The same model choice as a single session (UX review R6): the CLI's list, its default, or Custom… for any id. */
function RunModel({ run, value, models, placeholder, onChange }: {
  run: number; value: string; models: { id: string; label: string }[]; placeholder?: string; onChange: (model: string) => void;
}) {
  const [custom, setCustom] = useState(!!value && !models.some((m) => m.id === value));
  return <Field label={`Run ${run}: Model`}>
    {custom
      ? <Input value={value} placeholder={placeholder ?? "model id"} autoComplete="off" spellCheck={false} autoFocus
          onChange={(e) => onChange(e.target.value)} onBlur={() => { if (!value) setCustom(false); }} />
      : <Select value={value} onChange={(e) => { if (e.target.value === "__custom") { setCustom(true); onChange(""); } else onChange(e.target.value); }}>
          <option value="">CLI default</option>
          {models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          <option value="__custom">Custom…</option>
        </Select>}
  </Field>;
}
