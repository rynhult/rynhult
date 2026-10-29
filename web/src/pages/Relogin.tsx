import { desktopBuild } from "../lib/desktop";
import { CopyButton } from "../components/InstallSteps";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { api, post } from "../lib/api";
import type { LoginFlow } from "../lib/types";
import { Button, Dialog, ErrorNote, Field, Input } from "../components/ui";
import { onMachine } from "../components/Packages";
import { providerLabel, useInstance, useProviderRegistry } from "../lib/queries";

export type Provider = string;  // a provider id, or "github" (gh's own login)

/** The CLI's own login, run by the agent account's broker: on this machine, or on `machine` through its link. */
export function ReloginDialog({ provider, account, machine, onClose }: { provider: Provider | null; account?: string; machine?: string; onClose: () => void }) {
  const instance = useInstance();
  const agent_user = account ?? instance.agent_user;
  return provider ? <LoginDialog key={JSON.stringify([provider, agent_user, machine])}
    provider={provider} account={account} agent_user={agent_user} machine={machine} displayMachine={machine ?? instance.host} onClose={onClose} /> : null;
}

function LoginDialog({ provider, account, agent_user, machine, displayMachine, onClose }: {
  provider: Provider; account?: string; agent_user: string; machine?: string; displayMachine: string; onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const loginLink = useRef<HTMLDivElement>(null);
  const [flow, setFlow] = useState<LoginFlow | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const operation = useRef<{ active: boolean; flow: LoginFlow | null } | null>(null);
  const registry = useProviderRegistry();
  const name = provider === "github" ? "GitHub" : provider ? providerLabel(provider, registry.byId(provider)) : "";
  // "paste": a URL, then a code pasted back here; otherwise a device code the person enters on the sign-in page
  const paste = provider !== "github" && (registry.byId(provider ?? "")?.login ?? (provider === "claude" ? "paste" : "device")) === "paste";
  useEffect(() => {
    const current = { active: true, flow: null as LoginFlow | null };
    operation.current = current;
    const cancel = (started: LoginFlow) => {
      if (started.flow_id && !started.done) {
        post(`/api/accounts/login/${started.flow_id}/cancel${onMachine(machine)}`)
          .catch((error) => console.error("Could not cancel abandoned CLI login", error));
      }
    };
    const query = [account ? `account=${encodeURIComponent(account)}` : "", onMachine(machine, "")].filter(Boolean).join("&");
    post<LoginFlow>(`/api/accounts/${provider}/login${query ? `?${query}` : ""}`).then((started) => {
      if (!current.active) { cancel(started); return; }
      current.flow = started;
      setFlow(started);
    }, (error) => { if (current.active) setError(error); });
    return () => {
      current.active = false;
      if (current.flow) cancel(current.flow);
    };
  }, [provider, account, machine]);

  useEffect(() => {  // Codex device login completes on its own; poll for it
    if (paste || !provider || !flow?.flow_id || flow.done) return;  // device logins finish on their own
    const current = operation.current;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await api<LoginFlow>(`/api/accounts/login/${flow.flow_id}${onMachine(machine)}`);
        if (!active || !current?.active) return;
        current.flow = { ...next, flow_id: flow.flow_id };
        if (next.done) { setFlow(current.flow); queryClient.invalidateQueries({ queryKey: ["accounts"] }); return; }
      } catch (e) { if (active && current?.active) setError(e); }
      if (active && current?.active) timer = setTimeout(poll, 3000);
    };
    timer = setTimeout(poll, 3000);
    return () => { active = false; clearTimeout(timer); };
  }, [provider, paste, flow, queryClient, machine]);

  const close = () => {
    queryClient.invalidateQueries({ queryKey: ["accounts"] });
    queryClient.invalidateQueries({ queryKey: ["attention"] });
    onClose();
  };
  const submit = async () => {
    if (!flow?.flow_id) return;
    const current = operation.current;
    setBusy(true); setError(null);
    try {
      const result = await post<LoginFlow>(`/api/accounts/login/${flow.flow_id}/code${onMachine(machine)}`, { code });
      if (!current?.active) return;
      current.flow = { ...result, flow_id: flow.flow_id };
      setFlow(current.flow);
      queryClient.invalidateQueries({ queryKey: ["accounts"] });
    } catch (e) { if (current?.active) setError(e); } finally { if (current?.active) setBusy(false); }
  };

  return (
    <Dialog open={!!provider} onClose={close} dismissible={false} title={`${provider === "github" ? "Log in" : "Re-login"}: ${name} (as ${agent_user} on ${displayMachine})`}>
      <div className="space-y-4 text-[13px]">
        {!flow && !error && <p className="text-muted">Starting the CLI's own login…</p>}
        {flow && !flow.done && (
          <>
            <p className="text-muted">1. {desktopBuild ? "Copy this link into your regular browser" : "Open this link"} and sign in with the account that sessions running as <span className="font-mono">{agent_user}</span> should use.</p>
            {desktopBuild ? <div className="space-y-2">
              <div ref={loginLink} tabIndex={0} role="region" aria-label="Provider login link" className="break-all rounded-md border border-line bg-bg p-3 font-mono text-[12px]">{flow.url}</div>
              <CopyButton text={flow.url ?? ""} label="Copy login link" target={loginLink} />
            </div> : <a href={flow.url ?? "#"} target="_blank" rel="noreferrer"
              className="flex items-center gap-2 break-all rounded-md border border-line bg-bg p-3 font-mono text-[12px] text-accent hover:border-accent">
              <ExternalLink size={14} className="shrink-0" /> {flow.url}
            </a>}
            {paste ? (
              <>
                <Field label="2. Paste the authorization code shown after you approve">
                  <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="code#state" autoFocus />
                </Field>
                <div className="flex justify-end gap-2">
                  <Button variant="ghost" onClick={close}>Cancel</Button>
                  <Button variant="primary" disabled={!code.trim() || busy} onClick={submit}>{busy ? "Finishing…" : "Finish login"}</Button>
                </div>
              </>
            ) : (
              <>
                {flow.user_code ? <>
                  <p className="text-muted">2. Enter this code when asked:</p>
                  <div className="rounded-md border border-line bg-bg py-3 text-center font-mono text-xl tracking-widest">{flow.user_code}</div>
                </> : <p className="text-muted">2. Approve the sign-in on that page.</p>}
                <p className="text-xs text-faint">Waiting for approval… this updates itself when {name} confirms.</p>
              </>
            )}
          </>
        )}
        {flow?.done && (
          <div className="space-y-3">
            <p className={flow.ok ? "text-ok" : "text-bad"}>{flow.ok ? "Logged in. Running sessions keep their conversations." : `Login failed. ${flow.message}`}</p>
            <div className="flex justify-end"><Button onClick={close}>Close</Button></div>
          </div>
        )}
        <ErrorNote error={error} />
        <p className="text-xs text-faint">Rynhult runs the CLI's own login command; the link and code go to your browser only and are not stored or logged.</p>
      </div>
    </Dialog>
  );
}
