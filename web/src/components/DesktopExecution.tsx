import { useState, type FormEvent } from "react";
import { useConfirm } from "./confirm";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { api, post } from "../lib/api";
import { announce, Badge, Button, Card, ErrorNote, Field, Input, Note, Select } from "./ui";

interface DesktopStatus {
  provisioned: boolean;
  setup_supported: boolean;
  setup_kind: "linux" | "macos-installer" | "unavailable";
  account: string;
  protection: string;
  projects: { id: number; name: string; approved: boolean }[];
}

export function DesktopExecution() {
  const location = useLocation();
  const [provider, setProvider] = useState("claude");
  const [version, setVersion] = useState("");
  const client = useQueryClient();
  const confirm = useConfirm();
  const status = useQuery({ queryKey: ["desktop", location.pathname], queryFn: () => api<DesktopStatus>("/api/desktop/status") });
  const projectId = Number(location.pathname.match(/^\/projects\/(\d+)/)?.[1]);
  const project = status.data?.projects.find(p => p.id === projectId);
  const consent = useMutation({
    mutationFn: (accept: boolean) => post(`/api/desktop/projects/${projectId}/native`, { accept }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["desktop"] }),
  });
  const install = useMutation({
    mutationFn: () => post<{ version: string }>("/api/desktop/cli", { provider, version }),
    onSuccess: () => {
      announce("CLI installed in the agent account.");
      client.invalidateQueries({ queryKey: ["provider-registry"] });
      client.invalidateQueries({ queryKey: ["accounts"] });
    },
  });
  const installCLI = async (event: FormEvent) => {
    event.preventDefault();
    if (await confirm({ title: `Install ${provider === "claude" ? "Claude Code" : "Codex"} ${version}?`, confirm: "Install CLI",
      body: `Download the specified npm package and execute its installer as ${status.data?.account}. This installs into the agent account and needs no administrator prompt. Stop all sessions first. Rynhult has not qualified this version.`, danger: true })) install.mutate();
  };
  return <Card className="mb-4 p-4">
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-sm font-semibold">This computer · Standalone</span>
      <Badge>{status.isPending ? "Checking execution…" : status.error ? "Execution unavailable" : status.data?.provisioned ? status.data.protection : "Execution setup needed"}</Badge>
    </div>
    <ErrorNote error={status.error} onRetry={status.refetch} />
    {status.data?.provisioned ? <>
      <p className="mt-2 text-xs text-muted">Sessions run as {status.data.account}. They share that account’s files and provider sign-ins. Sign in to Claude or Codex in Providers.</p>
      {location.pathname === "/providers" && <form onSubmit={installCLI} className="mt-3 space-y-3">
        <p className="text-xs text-muted">Install or update a reviewed, exact CLI version from npm into the agent account. Node and npm must be available from setup. Stop sessions before changing a CLI; updates never run automatically.</p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="Agent CLI"><Select name="cli-provider" value={provider} onChange={e => setProvider(e.target.value)}><option value="claude">Claude Code</option><option value="codex">Codex</option></Select></Field>
          <Field label="Exact CLI version"><Input name="cli-version" value={version} onChange={e => setVersion(e.target.value)} required autoComplete="off" spellCheck={false} pattern="[0-9]+[.][0-9]+[.][0-9]+(-[A-Za-z0-9.-]+)?" placeholder="0.163.0…" /></Field>
          <div className="flex items-end"><Button type="submit" disabled={install.isPending}>{install.isPending ? "Installing…" : "Install or update CLI"}</Button></div>
        </div>
        <ErrorNote error={install.error} />
        {install.data && <p className="text-xs text-muted">Installed version {install.data.version} in the agent account.</p>}
      </form>}
      {project && <div className="mt-3 flex flex-wrap items-center gap-3">
        <span className="text-xs text-muted">{project.approved ? "Native sessions approved for this project." : "Approve native execution before launching a session in this project."}</span>
        <Button disabled={consent.isPending} onClick={async () => {
          if (project.approved || await confirm({ title: "Allow native sessions?", confirm: "Allow native sessions", danger: true,
            body: `Sessions in ${project.name} run as ${status.data.account}. They are not isolated: agents share this account’s files and credentials and can access everything it can. Account separation depends on filesystem permissions. Allow this project to use native execution?` })) consent.mutate(!project.approved);
        }}>{project.approved ? "Revoke native approval" : "Allow native sessions for this project"}</Button>
        <ErrorNote error={consent.error} />
      </div>}
    </> : status.data && <Note className="mt-3">
      {status.data.setup_kind === "macos-installer" ? <>Local execution needs a separate agent account. Install the reviewed execution setup package for this Mac. macOS Installer asks for administrator authorization once; then restart the app. Ordinary sessions need no administrator prompt. See the Guide’s Desktop apps page for setup and uninstall.</> : status.data.setup_supported ? <>Local execution needs a separate agent account. An administrator must review the packaged desktop/setup.py dry-run plan and apply it once. Then restart the app. Ordinary launches need no administrator prompt. See the Guide’s Desktop apps page for setup and uninstall.</> : <>Local agent execution is not available on this platform yet. See the Guide’s Desktop apps page for platform status.</>}
    </Note>}
  </Card>;
}
