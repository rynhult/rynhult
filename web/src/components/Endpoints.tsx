import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Cpu, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { api, post } from "../lib/api";
import { ago } from "../lib/format";
import { useConfirm } from "./confirm";
import { Badge, Button, Card, CardHeader, Dialog, Empty, ErrorNote, Field, IconButton, Input, QueryState, Select } from "./ui";

export interface ModelEndpoint {
  id: string; name: string; kind: string; styles: string[]; models: { id: string; label: string; size?: number }[];
  models_at: string | null; has_key: boolean; context_tokens: number | null;
  base_url?: string; urls?: Record<string, string>; last_error?: string | null; created_by?: string | null;
}

export const useEndpoints = (enabled = true) =>
  useQuery({ queryKey: ["endpoints"], queryFn: () => api<ModelEndpoint[]>("/api/endpoints"), enabled, staleTime: 30000 });

/* Presets fill in what each server usually needs; every field stays editable. */
const PRESETS: Record<string, { label: string; kind: string; base: string; openai?: string; anthropic?: string; key?: boolean; hint: string }> = {
  ollama: { label: "Ollama (this machine)", kind: "ollama", base: "http://127.0.0.1:11434", openai: "/v1", anthropic: "",
    hint: "Speaks both APIs, so Claude Code, Codex and Copilot can all use it. Set its context with OLLAMA_CONTEXT_LENGTH." },
  lmstudio: { label: "LM Studio (lms server)", kind: "openai", base: "http://127.0.0.1:1234", openai: "/v1",
    hint: "OpenAI-style API: Codex and Copilot." },
  llamacpp: { label: "llama.cpp server", kind: "openai", base: "http://127.0.0.1:8080", openai: "/v1", anthropic: "",
    hint: "OpenAI and Anthropic Messages APIs." },
  vllm: { label: "vLLM", kind: "openai", base: "http://127.0.0.1:8000", openai: "/v1", anthropic: "",
    hint: "OpenAI and Anthropic Messages APIs." },
  deepseek: { label: "DeepSeek API", kind: "openai", base: "https://api.deepseek.com", openai: "", anthropic: "/anthropic", key: true,
    hint: "Hosted: needs an API key from platform.deepseek.com. Models deepseek-v4-pro and deepseek-flash." },
  openrouter: { label: "OpenRouter", kind: "openai", base: "https://openrouter.ai/api", openai: "/v1", key: true,
    hint: "Hosted: many models behind one OpenAI-style API and key." },
  custom: { label: "Other (OpenAI- or Anthropic-compatible)", kind: "custom", base: "", openai: "/v1", hint: "Fill in the URLs the server documents." },
};

function unencrypted(url: string): boolean {
  try {
    const u = new URL(url);
    return u.protocol === "http:" && !["localhost", "[::1]"].includes(u.hostname) && !/^127\./.test(u.hostname);
  } catch {
    return false;
  }
}

function AddEndpointDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [preset, setPreset] = useState("ollama");
  const p = PRESETS[preset];
  const blank = (key = preset) => {
    const x = PRESETS[key];
    return { id: key === "custom" ? "" : key, name: x.label.replace(/ \(.*\)$/, ""), base: x.base,
      openai: x.openai !== undefined ? x.base + x.openai : "", anthropic: x.anthropic !== undefined ? x.base + x.anthropic : "",
      key: "", context: key === "ollama" ? "32768" : "", plainOk: false };
  };
  const [f, setF] = useState(blank());
  const choose = (key: string) => { setPreset(key); setF(blank(key)); };
  // A key sent over plain http to another machine crosses the network readable: the owner confirms it.
  const clear = !!f.key && [f.base, f.openai, f.anthropic].some(unencrypted);
  const add = useMutation({
    mutationFn: () => post<ModelEndpoint>("/api/endpoints", {
      id: f.id, name: f.name, kind: p.kind, base_url: f.base,
      styles: Object.fromEntries([["openai", f.openai], ["anthropic", f.anthropic]].filter(([, url]) => url)),
      api_key: f.key || null, context_tokens: f.context ? Number(f.context) : null, plain_http_ok: clear && f.plainOk }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["endpoints"] }); onClose(); setF(blank()); },
  });
  return (
    <Dialog open={open} onClose={onClose} title="Add a model endpoint" wide dismissible={false}
      description="A local model server or a compatible API that sessions can use instead of the agent's own models. Rynhult lists its models when you add it."
      footer={<><ErrorNote error={add.error} /><Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!f.id || !f.name || !f.base || !(f.openai || f.anthropic) || (p.key && !f.key) || (clear && !f.plainOk) || add.isPending}
          onClick={() => add.mutate()}>{add.isPending ? "Checking…" : "Add endpoint"}</Button></>}>
      <div className="space-y-3">
        <Field label="Server" hint={p.hint}>
          <Select value={preset} onChange={(e) => choose(e.target.value)}>
            {Object.entries(PRESETS).map(([key, x]) => <option key={key} value={key}>{x.label}</option>)}
          </Select>
        </Field>
        <div className="grid gap-3 sm:grid-cols-[12rem_1fr]">
          <Field label="ID"><Input value={f.id} onChange={(e) => setF({ ...f, id: e.target.value })} className="font-mono" placeholder="e.g. ollama" /></Field>
          <Field label="Name"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        </div>
        <Field label="Server URL" hint="Its models are listed from here (Ollama: /api/tags; others: the OpenAI-style /models)">
          <Input value={f.base} onChange={(e) => setF({ ...f, base: e.target.value })} className="font-mono" /></Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="OpenAI-style URL" hint="Codex and Copilot (blank: not offered)">
            <Input value={f.openai} onChange={(e) => setF({ ...f, openai: e.target.value })} className="font-mono" placeholder="…/v1" /></Field>
          <Field label="Anthropic-style URL" hint="Claude Code and Copilot (blank: not offered)">
            <Input value={f.anthropic} onChange={(e) => setF({ ...f, anthropic: e.target.value })} className="font-mono" /></Field>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={p.key ? "API key" : "API key (optional)"} hint="Stored in Rynhult's database; only sessions on this endpoint get it">
            <Input type="password" autoComplete="off" value={f.key} onChange={(e) => setF({ ...f, key: e.target.value })} /></Field>
          <Field label="Context window (tokens, optional)" hint="So agents compact before the server cuts the prompt">
            <Input inputMode="numeric" value={f.context} onChange={(e) => setF({ ...f, context: e.target.value.replace(/\D/g, "") })} /></Field>
        </div>
        {clear && (
          <label className="flex items-start gap-2 rounded-md border border-wait/40 bg-wait/10 p-3 text-[13px]">
            <input type="checkbox" className="mt-0.5" checked={f.plainOk} onChange={(e) => setF({ ...f, plainOk: e.target.checked })} />
            <span>This server uses plain <span className="font-mono">http</span> on another machine, so the API key travels
              unencrypted. Send it anyway (only on a network you trust).</span>
          </label>)}
      </div>
    </Dialog>
  );
}

/** Owners: the model servers sessions can run on (local LLMs and compatible APIs). */
export function EndpointsCard() {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const endpoints = useEndpoints();
  const [adding, setAdding] = useState(false);
  const refresh = useMutation({ mutationFn: (id: string) => post(`/api/endpoints/${id}/refresh`),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["endpoints"] }) });
  const remove = useMutation({ mutationFn: (id: string) => post(`/api/endpoints/${id}/remove`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["endpoints"] }) });
  return (
    <Card>
      <CardHeader title="Model endpoints" icon={Cpu}
        subtitle="Local LLM servers and compatible APIs (Ollama, LM Studio, llama.cpp, vLLM, DeepSeek…) sessions can use instead of an agent's own models"
        action={<Button size="sm" onClick={() => setAdding(true)}><Plus size={13} aria-hidden="true" /> Add</Button>} />
      <QueryState query={endpoints} errorTitle="Couldn't read the model endpoints." isEmpty={(data) => !data.length}
        empty={<Empty icon={Cpu}>No endpoints yet. Add Ollama or another model server to run agents on local or alternative models.</Empty>}>
        {(data) => (
        <ul className="divide-y divide-line">
          {data.map((e) => (
            <li key={e.id} className="flex flex-wrap items-start gap-x-3 gap-y-1 px-4 py-3 text-[13px]">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2"><span className="font-medium text-fg">{e.name}</span>
                  {e.styles.map((s) => <Badge key={s}>{s === "openai" ? "OpenAI API" : "Anthropic API"}</Badge>)}
                  {e.has_key && <Badge>key</Badge>}{e.context_tokens && <Badge>{Math.round(e.context_tokens / 1024)}k context</Badge>}</div>
                <div className="truncate font-mono text-xs text-faint">{e.base_url}</div>
                {e.last_error ? <p className="mt-1 text-xs text-bad">{e.last_error}</p>
                  : <p className="mt-1 text-xs text-muted">{e.models.length} models{e.models_at ? ` · listed ${ago(e.models_at)}` : ""}
                    {e.models.length > 0 && `: ${e.models.slice(0, 6).map((m) => m.id).join(", ")}${e.models.length > 6 ? "…" : ""}`}</p>}
              </div>
              <IconButton label={`Refresh ${e.name}'s models`} icon={RefreshCw} onClick={() => refresh.mutate(e.id)} />
              <IconButton label={`Remove ${e.name}`} icon={Trash2} onClick={async () => {
                if (await confirm({ title: `Remove ${e.name}?`, body: "Running sessions keep going; resuming them later needs an endpoint with this ID again.", confirm: "Remove", danger: true })) remove.mutate(e.id);
              }} />
            </li>
          ))}
        </ul>
        )}
      </QueryState>
      <div className="px-4 pb-3"><ErrorNote error={refresh.error ?? remove.error} /></div>
      <AddEndpointDialog open={adding} onClose={() => setAdding(false)} />
    </Card>
  );
}
