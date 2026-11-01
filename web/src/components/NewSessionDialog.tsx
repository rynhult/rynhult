import {SecurityBadge} from './SecurityBadge';
import {frozenSecurity} from '../lib/security';
import { useFeatures } from "./Feature";
import { PreviewBadge } from "./Containers";
import { desktopBuild } from "../lib/desktop";
import { CopyBlock } from "./InstallSteps";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { AutomationFields, type Automation } from "./SessionAutomation";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import clsx from "clsx";
import { ChevronRight, FolderPlus, Rocket, ShieldAlert } from "lucide-react";
import { api, post } from "../lib/api";
import { LEGACY_OPTIONS, type Catalog, type MultiRunChoice, type MultiRunGroup, type IssueRef, type Project, type Provider, type ProviderInfo, type SessionDetail } from "../lib/types";
import { MultiRunFields, newRunChoice, runDangerous, runOptions } from "./MultiRunFields";
import { defaultGrant, OrchestratorFields, type TeamGrant } from "./OrchestratorFields";
import { NewSessionIssuePicker } from "./IssueSources";
import { CatalogPicker } from "./CatalogPicker";
import { ScheduleTiming, defaultSchedule, type ScheduleDraft } from "./ScheduleTiming";
import { SessionKeys } from "./Keys";
import { advancedSummary, RUN_MODES, suggestedName, type NewSessionPreset, type RunMode } from "./newSession";
import { Badge, Button, Dialog, ErrorNote, Field, Input, Note, SearchInput, Segmented, Select, Skeleton, Textarea } from "./ui";
import { useConfirm } from "./confirm";
import { NewProjectDialog } from "./NewProject";
import { useInstance, useProviderRegistry, useSessions } from "../lib/queries";
import { isLive } from "./session-bits";
import { useBudgets } from "./Budgets";
import { useEndpoints } from "./Endpoints";
import { isLocalProject } from "../lib/format";
import { readBriefDraft, type BriefDraft, type BriefView } from "../lib/projectBrief";
import { BriefLaunchReview, type BriefLaunchChoice } from "./BriefLaunchReview";
import { useBriefNavigationConsent } from "./UnsavedBriefGuard";
import { machineDetail, useLaunchMachines } from "./MachineChoice";

/** The session's name: the automatic one is shown, and Rename opens an editor. A name chosen earlier (a draft, an
 *  issue, a preset) shows the editor straight away; an empty name launches with the automatic one. */
function SessionName({ value, auto, onChange }: { value: string; auto: string; onChange: (name: string) => void }) {
  const [editing, setEditing] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const returnFocus = useRef(false);
  const open = editing || !!value.trim();
  useEffect(() => {
    if (open || !returnFocus.current) return;
    returnFocus.current = false;
    box.current?.querySelector("button")?.focus();  // back to Rename after using the automatic name
  }, [open]);
  return (
    <div ref={box} className="min-w-0">
      {open ? (
        <div className="flex min-w-0 items-end gap-2">
          <div className="min-w-0 flex-1">
            <Field label="Name">
              <Input value={value} onChange={(e) => onChange(e.target.value)} maxLength={40} placeholder={auto} autoFocus={editing} />
            </Field>
          </div>
          <Button size="sm" variant="ghost" onClick={() => { returnFocus.current = true; onChange(""); setEditing(false); }}>Use automatic name</Button>
        </div>
      ) : (
        <div className="flex min-w-0 items-center justify-between gap-2 text-[13px]">
          <p className="min-w-0 text-muted">Name: <span className="font-mono text-fg [overflow-wrap:anywhere]" data-testid="automatic-name">{auto}</span></p>
          <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>Rename</Button>
        </div>
      )}
    </div>
  );
}

interface ModelOption { id: string; label: string; description: string; source: string; efforts?: string[] }
type ModelCatalog = Record<string, unknown> & { fetched_at?: number };
const modelsFor = (catalog: ModelCatalog | undefined, provider: string): ModelOption[] =>
  Array.isArray(catalog?.[provider]) ? (catalog[provider] as ModelOption[]) : [];
type OptionValues = Record<string, string | boolean>;
/** The provider's launch options with defaults filled in (`chosen` wins). */
const optionValues = (info: ProviderInfo | undefined, chosen: OptionValues): OptionValues =>
  Object.fromEntries((info?.options ?? []).map((o) => [o.key, chosen[o.key] ?? o.default ?? ""]));

interface Preview { command: string[]; cwd: string; files: Record<string, string>; digest: string; trusted: boolean; import_mode: string;
  machine?: string; checkout?: { requires_clone: boolean };
  inputs?: { initial_prompt?: string | null };
  not_delivered?: { kind: string; id: string; reason: string }[];
  keys?: { keys: { id: string; name: string; variable: string }[]; held_back: { id: string; name: string; variable: string }[] } }

function DeliveryWarnings({ preview, agent }: { preview?: Preview; agent: string }) {
  if (!preview?.not_delivered?.length) return null;
  return <div role="status" className="min-w-0 rounded-lg border border-wait/30 bg-wait/10 p-3 text-xs text-wait [overflow-wrap:anywhere]" data-testid="not-delivered">
    <div className="mb-1 font-medium">Not passed to {agent} from this profile</div>
    <ul className="space-y-0.5">
      {preview.not_delivered.map(x => (
        <li key={`${x.kind}:${x.id}`}><span className="font-mono">{x.kind === "instructions" ? "instructions" : `${x.kind} · ${x.id}`}</span>: {x.reason}</li>
      ))}
    </ul>
  </div>;
}

const shellQuote = (s: string) => (/^[\w@%+=:,./<>-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`);

export function NewSessionDialog({ open, onClose, onLaunched, onDiscard, preset, schedule }: {
  open: boolean; onClose: () => void; onLaunched?: () => void; preset?: NewSessionPreset; schedule?: ScheduleDraft;
  /** Clear the kept draft (Close keeps it for next time; UX review NS-6). */
  onDiscard?: () => void;
}) {
  const saved = schedule?.launch;
  const feature = useFeatures();
  const featureState = useInstance().features;
  const [scheduleConfig, setScheduleConfig] = useState(schedule ?? defaultSchedule());
  const queryClient = useQueryClient();
  const instance = useInstance();
  const sessions = useSessions();
  const navigate = useNavigate();
  const allowBriefNavigation = useBriefNavigationConsent();
  const [briefChoice, setBriefChoice] = useState<BriefLaunchChoice>();
  const launchButton = useRef<HTMLSpanElement>(null);
  const returningFromBrief = useRef(false);
  useEffect(() => {
    if (!briefChoice && returningFromBrief.current) { returningFromBrief.current = false; launchButton.current?.querySelector("button")?.focus(); }
  }, [!!briefChoice]);
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/api/projects"), enabled: open });
  // "local" is the built-in group for local workspaces: those get their own folder, never its root.
  const usable = (projects.data ?? []).filter((p) => !p.archived && p.import_mode === "managed" && !isLocalProject(p));
  const [mode, setMode] = useState<"project" | "workspace">(preset?.mode ?? "project");
  const [topic, setTopic] = useState("");
  const existing = preset?.workspace;  // a session in an existing local workspace (e.g. Council after Researcher)
  const [customModel, setCustomModel] = useState(!!saved?.model);
  const catalogQuery = useQuery({ queryKey: ["catalog"], queryFn: () => api<Catalog>("/api/catalog"), enabled: open, staleTime: 3600000 });
  const catalog = catalogQuery.data;
  const [profile, setProfile] = useState(String(saved?.profile ?? ""));
  const [picks, setPicks] = useState<{ skills: string[]; agents: string[]; mcps: string[] }>({ skills: (saved?.skills as string[]) ?? ["acc-workflow"], agents: (saved?.agents as string[]) ?? [], mcps: (saved?.mcps as string[]) ?? [] });
  const [form, setForm] = useState({ project_id: (saved?.project_id as number) ?? preset?.project_id ?? 0, provider: (saved?.provider as Provider) ?? "claude" as Provider, name: (saved?.name as string) ?? preset?.name ?? "", model: String(saved?.model ?? ""), effort: String(saved?.effort ?? ""),
    initial_prompt: (saved?.initial_prompt as string) ?? preset?.initial_prompt ?? "", endpoint: String(saved?.endpoint ?? ""), worktree: (saved?.worktree as boolean) ?? preset?.worktree ?? !!schedule });
  const [machineChoice, setMachineChoice] = useState<string | undefined>(saved?.machine as string | undefined);
  const [cloneOnMachine, setCloneOnMachine] = useState(!!saved?.clone_on_machine);
  // One Run choice (owner decision 0008, Q5): once, several agents, with helpers, or on a schedule.
  const [run, setRun] = useState<RunMode>(schedule ? "schedule" : saved?.orchestrator ? "helpers" : "once");
  const orchestrator = run === "helpers";
  const [advanced, setAdvanced] = useState(false);
  const [teamGrant, setTeamGrant] = useState<TeamGrant>((saved?.orchestrator as TeamGrant) ?? defaultGrant("local"));

  const [issue, setIssue] = useState<{ projectId?: number; ref?: IssueRef }>({ projectId: (saved?.project_id as number) ?? preset?.project_id, ref: (saved?.issue_source as IssueRef) ?? preset?.issue_source });
  const [automation, setAutomation] = useState<Automation>((saved?.automation as Automation) ?? {});
  const [runChoices, setRunChoices] = useState<MultiRunChoice[]>([newRunChoice(), newRunChoice("codex")]);
  const projectId = mode === "project" ? form.project_id || undefined : undefined;
  const project = usable.find((p) => p.id === form.project_id);
  const machine = mode === "workspace" ? "local" : machineChoice ?? project?.host ?? "local";
  const otherMachine = mode === "project" && !!project && machine !== (project.host || "local");
  const scopeReady = open && (mode === "workspace" || !!projectId);
  const machines = useLaunchMachines(projectId, scopeReady);
  const selectedMachine = machines.data?.find(item => item.name === machine);
  const registry = useProviderRegistry(projectId, scopeReady, machine);
  const models = useQuery({ queryKey: ["models", projectId ?? "account", machine],
    queryFn: () => api<ModelCatalog>(`/api/models?${new URLSearchParams({ machine, ...(projectId ? { project_id: String(projectId) } : {}) })}`),
    enabled: scopeReady, staleTime: 3600000 });
  const previousProject = useRef(projectId);
  useEffect(() => {
    if (previousProject.current === projectId) return;
    previousProject.current = projectId;
    setTeamGrant((grant) => ({ ...grant, brief_version: undefined }));
    setMachineChoice(undefined); setCloneOnMachine(false);
    setForm((f) => ({ ...f, model: "", effort: "" }));
    setRunChoices((choices) => choices.map((c) => ({ ...c, model: "", confirm_dangerous: false })));
    setCustomModel(false);
  }, [projectId]);
  const chooseMachine = (next: string) => {
    setMachineChoice(next); setCloneOnMachine(false); setConfirmBypass(false);
    setForm(f => ({ ...f, model: "", effort: "" }));
    setRunChoices(choices => choices.map(choice => ({ ...choice, model: "", confirm_dangerous: false })));
    setCustomModel(false);
  };
  const endpoints = useEndpoints(open && feature("local_models"));
  const budgetState = useBudgets(open).data;
  const budgetPaused = Object.entries(budgetState?.status ?? {})
    .filter(([id, st]) => st.over && st.action === "pause" && id.split(".")[0] === form.provider)
    .map(([id]) => budgetState?.windows.find((w) => w.id === id)?.label ?? id);
  // "Launch anyway" confirms this agent's paused windows only: another agent, or another set of windows, asks again.
  const budgetKey = `${form.provider}|${budgetPaused.join("|")}`;
  const [budgetAck, setBudgetAck] = useState<string | null>(null);
  const budgetOk = budgetPaused.length > 0 && budgetAck === budgetKey;
  const [chosenOptions, setChosenOptions] = useState<OptionValues>(saved ? {
    ...(saved.options as OptionValues ?? {}), ...Object.fromEntries(LEGACY_OPTIONS.filter(k => saved[k] !== undefined).map(k => [k, saved[k]])) as OptionValues,
  } : {});  // this provider's launch options, as changed here
  const info = registry.byId(form.provider);
  // Model endpoints this agent can use (it speaks one of their API styles)
  const usableEndpoints = (endpoints.data ?? []).filter((e) => e.styles.some((s) => info?.endpoint_styles.includes(s)));
  const endpoint = usableEndpoints.find((e) => e.id === form.endpoint);
  const options = optionValues(info, chosenOptions);
  if (endpoint && "remote_control" in options) options.remote_control = false;  // Remote Control needs the vendor's models
  const [confirmBypass, setConfirmBypass] = useState(false);
  const [showPicks, setShowPicks] = useState(false);
  const [pickQuery, setPickQuery] = useState("");
  useEffect(() => {
    // A preset project that can't be used here (archived, read-only, local) falls back to the first usable one.
    if (projects.data && usable.length && !usable.some((p) => p.id === form.project_id)) setForm((f) => ({ ...f, project_id: usable[0].id }));
  }, [projects.data, usable, form.project_id]);
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) => setForm((f) => ({ ...f, [key]: value }));
  const workspace = mode === "workspace";
  useEffect(() => {
    if ((run === "several" && !feature("multirun")) || (run === "schedule" && !feature("schedules")) || (run === "helpers" && !feature("teams"))) setRun("once");
    if (!feature("local_models") && form.endpoint) setForm(old => ({ ...old, endpoint: "", model: "" }));
  }, [featureState, run, form.endpoint]); // featureState owns the effective switches
  const multi = run === "several" && !workspace && !schedule;
  const scheduling = !!schedule || (run === "schedule" && !workspace);
  const task = form.initial_prompt.trim();
  const autoName = existing ? profile || "session" : workspace ? [...topic.trim()].slice(0, 40).join("") || "session" : suggestedName(task);
  const body = useMemo(() => ({ ...form, machine, clone_on_machine: cloneOnMachine, worktree: form.worktree || otherMachine,
    endpoint: feature("local_models") ? form.endpoint : "", name: form.name.trim() || autoName,
    ...Object.fromEntries(Object.entries(options).filter(([k]) => LEGACY_OPTIONS.includes(k))),
    options: Object.fromEntries(Object.entries(options).filter(([k]) => !LEGACY_OPTIONS.includes(k))),
    ...(feature("teams") && orchestrator && !workspace ? { orchestrator: teamGrant, worktree: true } : {}),
    automation: { autoresume: feature("automation") && !form.endpoint && ["claude", "codex", "copilot"].includes(form.provider) && !!automation.autoresume, ...(feature("automation") && automation.goal?.text.trim() ? { goal: automation.goal } : {}) },
    confirm_dangerous: confirmBypass, budget_ok: budgetOk, profile: profile || null, ...picks,
    issue_source: feature("issue_sources") && !workspace && issue.projectId === form.project_id ? issue.ref : undefined,
    ...(workspace ? { project_id: null, worktree: false, ...(existing ? { workspace: existing } : { topic: topic.trim() || "untitled" }) } : {}) }),
  [featureState, form, machine, cloneOnMachine, otherMachine, options, automation, orchestrator, teamGrant, confirmBypass, budgetOk, profile, picks, workspace, topic, existing, issue, autoName]);  // eslint-disable-line react-hooks/exhaustive-deps
  const compatible = (kind: "skills" | "agents" | "mcps", ids: string[], provider: Provider) =>
    ids.filter((id) => catalog?.[kind].find((i) => i.id === id)?.providers.includes(provider));
  const chooseProfile = (id: string) => {
    setProfile(id);
    const p = catalog?.profiles.find((x) => x.id === id);
    if (!p) { setPicks({ skills: ["acc-workflow"], agents: [], mcps: [] }); return; }
    setPicks({ skills: compatible("skills", p.skills, form.provider), agents: compatible("agents", p.agents, form.provider),
      mcps: compatible("mcps", p.mcps, form.provider) });
    setChosenOptions((o) => ({ ...o, ...((p[form.provider] as OptionValues | undefined) ?? {}) }));
  };
  const [presetApplied, setPresetApplied] = useState(false);
  useEffect(() => {
    if (presetApplied || !catalog || !preset) return;
    setPresetApplied(true);
    const wanted = preset.profile ?? (preset.mode === "workspace" ? "researcher" : "");
    if (wanted && catalog.profiles.some((p) => p.id === wanted)) chooseProfile(wanted);
  }, [catalog, preset, presetApplied]);  // eslint-disable-line react-hooks/exhaustive-deps
  const profiles = (catalog?.profiles ?? []).filter((p) => (!p.providers || p.providers.includes(form.provider)) && (!workspace || p.standalone));
  useEffect(() => {
    // The person can choose Local workspace while the catalog request is still
    // pending. Select the default when it arrives, retaining a valid chosen or
    // preset profile rather than leaving a native select with no matching value.
    if (!workspace || !catalog || (preset && !presetApplied)) return;
    const eligible = catalog.profiles.filter(p => p.standalone && (!p.providers || p.providers.includes(form.provider)));
    if (eligible.some(p => p.id === profile)) return;
    const selected = eligible.find(p => p.id === 'researcher') ?? eligible[0];
    if (selected) chooseProfile(selected.id);
  }, [workspace, catalog, form.provider, profile, preset, presetApplied]); // eslint-disable-line react-hooks/exhaustive-deps
  const switchMode = (next: "project" | "workspace") => {
    setMode(next);
    setIssue({});
    set("worktree", false);  // the checkbox is project-only: never carry a hidden choice into a local workspace
    if (next === "workspace") setRun("once");  // several agents, helpers and schedules need a project
    if (next === "workspace" && !catalog?.profiles.find((p) => p.id === profile)?.standalone) {
      const standalone = (catalog?.profiles ?? []).filter((p) => p.standalone);  // research first, then council
      chooseProfile((standalone.find((p) => p.id === "researcher") ?? standalone[0])?.id ?? "");
    }
  };

  const preview = useQuery({
    queryKey: ["preview", body],
    queryFn: () => post<Preview>("/api/sessions/preview", body),
    enabled: open && !multi && (workspace ? (!!existing || !!topic.trim()) && !!profile : !!form.project_id),
    retry: false,
  });
  // The server owns the kickoff wording and selected brief/mode. Derive the
  // default rather than writing it into the person's draft: late previews,
  // project changes and run-mode changes must never overwrite their edits.
  const firstMessage = form.initial_prompt.trim() || !orchestrator || workspace
    ? form.initial_prompt : preview.data?.inputs?.initial_prompt ?? "";
  const multiChoices = runChoices.map((c) => ({ ...c, options: runOptions(c, registry.byId(c.provider), catalog) }));
  const multiPreviews = useQueries({ queries: (multi ? multiChoices : []).map((choice) => {
    const launch = { ...choice, name: body.name, project_id: form.project_id,
      machine, clone_on_machine: cloneOnMachine, initial_prompt: form.initial_prompt, worktree: true };
    return { queryKey: ["preview", launch], queryFn: () => post<Preview>("/api/sessions/preview", launch),
      enabled: open && !!form.project_id, retry: false };
  }) });
  // Trust is part of launching (UX review 2, first run): an untrusted project asks once, here, and the draft stays.
  const confirm = useConfirm();
  const [creatingProject, setCreatingProject] = useState(false);
  const trust = useMutation({
    mutationFn: (id: number) => post<Project>(`/api/projects/${id}/trust`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
  const launch = useMutation({
    mutationFn: async (choice: { grant?: TeamGrant; briefDecision?: boolean }): Promise<MultiRunGroup | SessionDetail> => multi ? post<MultiRunGroup>("/api/multiruns", {
      project_id: form.project_id, machine, clone_on_machine: cloneOnMachine, name: body.name, initial_prompt: form.initial_prompt, runs: multiChoices,
    }) : scheduling
      ? api<SessionDetail>(`/api/schedules${schedule?.id ? `/${schedule.id}` : ""}`, {
        method: schedule?.id ? "PUT" : "POST", json: { ...scheduleConfig, launch: { ...body, budget_ok: false } } })
      : post<SessionDetail>("/api/sessions", { ...body, ...(choice.grant ? { orchestrator: choice.grant } : {}) }),
    onSuccess: (session, choice) => {
      queryClient.invalidateQueries({ queryKey: ["sessions"] });
      queryClient.invalidateQueries({ queryKey: ["multiruns"] });
      if (choice.briefDecision) allowBriefNavigation(`/sessions/${session.id}`);
      (onLaunched ?? onClose)();
      if (scheduling) { queryClient.invalidateQueries({ queryKey: ["schedules"] }); if (!schedule) navigate("/schedules"); }
      else navigate(`/${multi ? "multiruns" : "sessions"}/${session.id}`);
    },
  });
  const teamDangerous = orchestrator && teamGrant.agents.some((a) => registry.byId(a.agent)?.options.some((o) =>
    o.dangerous.includes(a.options[o.key] ?? (catalog?.profiles.find((p) => p.id === a.profile)?.[a.agent] as OptionValues | undefined)?.[o.key] ?? o.default ?? '')));
  const dangerous = !multi && (teamDangerous || (info?.options ?? []).some((o) => o.dangerous.includes(options[o.key])));
  const needsClone = otherMachine && (multi ? multiPreviews.some(p => p.data?.checkout?.requires_clone) : preview.data?.checkout?.requires_clone);
  const machineBlocked = machines.isPending || !!machines.error || !selectedMachine?.available || !!needsClone && !cloneOnMachine;
  const overBudget = !scheduling && !multi && budgetPaused.length > 0 && !budgetOk;
  const unavailable = !multi && info?.installed === false;
  const multiBlocked = multi && (!form.initial_prompt.trim() || runChoices.some((c) =>
    !registry.byId(c.provider) || registry.byId(c.provider)?.installed === false
    || (runDangerous(c, registry.byId(c.provider), catalog) && !c.confirm_dangerous))
    || multiPreviews.some((p) => p.isPending || !!p.error));
  const untrusted = !workspace && !!project && !project.trusted;
  const ensureTrusted = async () => {
    if (untrusted && project) {
      if (!await confirm({ title: `Trust ${project.name}?`, confirm: scheduling ? "Trust and save" : "Trust and launch",
        body: <>Agents launched here can read, edit and run files in <span className="font-mono text-fg [overflow-wrap:anywhere]">{project.workspace_path}</span>, within the permissions you pick for each session. Rynhult records the folder as trusted for every agent CLI, so sessions start without the CLI's own trust question.</> })) return false;
      try { await trust.mutateAsync(project.id); } catch { return false; }  // shown below
    }
    return true;
  };
  const reviewBrief = useMutation({
    mutationFn: async (draft: BriefDraft) => {
      const current = await api<BriefView>(`/api/projects/${projectId}/brief`);
      return { projectId: projectId!, draft, nextVersion: current.next_version, exportPath: draft.exportPath ?? current.export_path };
    },
    onSuccess: setBriefChoice,
  });
  const start = async () => {
    const draft = projectId && orchestrator && instance.orchestrator && !scheduling ? readBriefDraft(projectId) : null;
    if (draft && typeof draft.draft?.product === "string" && draft.draft.product.trim()) { reviewBrief.mutate(draft); return; }
    if (await ensureTrusted()) launch.mutate({});
  };
  const briefPending = orchestrator && !workspace && instance.orchestrator && teamGrant.brief_version === undefined;
  const blocked = machineBlocked || briefPending || registry.isPending || !!registry.error || multiBlocked || unavailable || overBudget || (workspace
    ? (!existing && !topic.trim()) || !profile || (dangerous && !confirmBypass)
    : !project || project.import_mode !== "managed" || (dangerous && !confirmBypass)
      || (!multi && !!endpoint && !form.model));

  const reason = machines.isPending ? "Checking machines…" : machines.error ? "Could not load machines. Retry above."
    : !selectedMachine?.available ? selectedMachine?.reason ?? "Choose an available machine."
    : needsClone && !cloneOnMachine ? `Choose Clone on ${machine} before launching.`
    : registry.isPending ? "Checking available agents…"
    : registry.error ? "Could not check available agents: fix the error shown above."
    : briefPending ? "Review the project brief version before launching."
    : multiBlocked ? (!task ? `Add a task to start ${runChoices.length} runs.`
      : multiPreviews.some((p) => p.isPending) ? "Checking each run…"
      : multiPreviews.some((p) => !!p.error) ? "A run can't start: fix the error shown above."
      : runChoices.some((c) => !registry.byId(c.provider) || registry.byId(c.provider)?.installed === false) ? "A run's agent isn't installed: choose another."
      : "Confirm each unrestricted run above.")
    : unavailable ? `This agent is not installed on ${machine} for this project's account. Choose another agent.`
    : overBudget ? "Your budget pauses this agent: confirm below to launch anyway." : workspace
    ? (!existing && !topic.trim() ? "Give the workspace a topic." : !profile ? "Choose a profile that can run without a project."
      : dangerous && !confirmBypass ? "Confirm the unrestricted mode above." : preview.error ? "Fix the error shown above." : "")
    : !usable.length ? "Add a project first: New project, above."
    : !project ? "Choose a project." : project.import_mode !== "managed" ? "This project is read-only in Rynhult."
    : !multi && endpoint && !form.model ? `Choose one of ${endpoint.name}'s models in Advanced.`
    : dangerous && !confirmBypass ? "Confirm the unrestricted mode above." : !multi && preview.error ? "Fix the error shown above." : "";
  const selectedCount = picks.skills.length + picks.agents.length + picks.mcps.length;

  // What the closed Advanced section holds, in one line (WP7): run mode, model, permissions, checkout, skills, goal.
  const chosenModel = endpoint ? endpoint.models.find((m) => m.id === form.model)?.label ?? form.model
    : modelsFor(models.data, form.provider).find((m) => m.id === form.model)?.label ?? form.model;
  const optionWords = (info?.options ?? []).flatMap((o) => o.kind === "select"
    ? [o.choices.find((c) => c.value === options[o.key])?.label ?? ""] : options[o.key] === true && o.key !== "remote_control" ? [o.label] : [])
    .filter(Boolean).map((w) => w.toLowerCase());
  const timing = scheduleConfig.timing.kind === "cron" ? scheduleConfig.timing.expression : scheduleConfig.timing.kind === "once"
    ? `once${scheduleConfig.timing.at ? ` at ${scheduleConfig.timing.at.replace("T", " ")}` : ""}`
    : `${scheduleConfig.timing.kind} at ${scheduleConfig.timing.time ?? "09:00"}${scheduleConfig.enabled ? "" : ", paused"}`;
  const summary = advancedSummary({ run: scheduling ? "schedule" : multi ? "several" : run === "helpers" && !workspace ? "helpers" : "once",
    runs: runChoices.length, schedule: timing, workspace, model: chosenModel, effort: form.effort, options: optionWords,
    worktree: form.worktree || otherMachine || !!project?.private_workspace, skills: picks.skills.length, agents: picks.agents.length, mcps: picks.mcps.length,
    goal: !!automation.goal?.text.trim(), autoresume: !!automation.autoresume });
  // Own worktree (owner decision 0008, Q6): a warning only when another live session already works in this checkout.
  const sharing = !workspace && project && !otherMachine && !form.worktree && !orchestrator && !multi && !project.private_workspace
    ? (sessions.data ?? []).filter((x) => x.project_id === project.id && isLive(x) && !x.worktree) : [];
  const runsOn = machine === "local" ? instance.host : machine;
  const runsAs = workspace ? instance.agent_user : project?.account ?? instance.agent_user;
  const dirty = !!(task || form.name.trim() || topic.trim());
  const discard = async () => {
    if (await confirm({ title: "Discard this draft?", body: "The task, name and choices you entered here are cleared.", confirm: "Discard draft", danger: true })) onDiscard?.();
  };
  const runModes = RUN_MODES.filter((m) => (m.id !== "several" || feature("multirun")) && (m.id !== "schedule" || feature("schedules")) && (m.id !== "helpers" || feature("teams")) && (!desktopBuild && !workspace) || m.id === "once");
  const primary = scheduling ? (schedule?.id ? "Save schedule" : scheduleConfig.enabled ? "Create schedule" : "Create paused schedule")
    : untrusted ? "Trust and launch" : "Launch";

  return (
    <>
    <Dialog open={open} onClose={onClose} title={schedule ? (schedule.id ? "Edit schedule" : "New schedule") : "New session"} wide dismissible={false}
      description={scheduling ? "A saved launch that starts by itself at the times you choose. Each run is an ordinary session under your agent account."
        : "Say what to work on and launch; the rest has defaults under Advanced."}
      footer={briefChoice ? undefined : <>
        <span className={clsx("mr-auto min-w-0 basis-full text-xs sm:basis-auto", reason && !/Add a task|topic|Choose a project/.test(reason) ? "text-wait" : "text-muted")} role="status">
          {reason || (scheduling ? (scheduleConfig.enabled ? "Ready. It starts at the times you chose." : "Ready. It's saved paused: tick Enable schedule to start it, or enable it later on Schedules.")
            : untrusted ? "Ready. Launch asks you to trust the project first." : "Ready to launch.")}
          {orchestrator && instance.orchestrator && teamGrant.brief_version !== undefined && <span className="block">
            {teamGrant.brief_version ? `Project brief: saved version ${teamGrant.brief_version}.` : 'Project brief: none pinned.'}
          </span>}
        </span>
        {multi?multiPreviews.map((p,i)=><SecurityBadge key={i} policy={frozenSecurity(p.data?.files)} label={`Run ${i+1} launch`}/>):<SecurityBadge policy={frozenSecurity(preview.data?.files)} label="This launch"/>}
        <ErrorNote error={trust.error} title="Couldn't trust the project." />
        <ErrorNote error={launch.error} />
        <ErrorNote error={reviewBrief.error} title="Couldn't check the next brief version. Try Launch again." />
        <ErrorNote error={registry.error || models.error} />
        {dirty && onDiscard && <Button variant="ghost" onClick={discard}>Discard draft</Button>}
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <span ref={launchButton} className="contents"><Button variant="primary" disabled={blocked || (!multi && !!preview.error) || launch.isPending || trust.isPending || reviewBrief.isPending} onClick={start}>
          <Rocket size={14} aria-hidden="true" /> {reviewBrief.isPending ? "Checking brief…" : launch.isPending || trust.isPending ? (scheduling ? "Saving…" : "Launching…") : primary}
        </Button></span>
      </>}>
      {briefChoice ? <>
        <ErrorNote error={trust.error} title="Couldn't trust the project." />
        <BriefLaunchReview choice={briefChoice} grant={teamGrant} ensureTrusted={ensureTrusted} onGrant={setTeamGrant}
          onLaunch={grant => launch.mutateAsync({ grant, briefDecision: true })} onBack={() => {
            returningFromBrief.current = true; setBriefChoice(undefined); reviewBrief.reset(); launch.reset();
          }} />
      </> : <div className="min-w-0 space-y-6">
        <section aria-labelledby="ns-what" className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 id="ns-what" className="text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">What</h3>
            {schedule ? <Badge>Scheduled project session</Badge> : existing ? <Badge tone="info">Local workspace</Badge> : (
              <Segmented label="Where it runs" value={mode} onChange={switchMode} options={[
                { id: "project", label: "Project" }, { id: "workspace", label: "Local workspace" }]} />)}
          </div>
          {workspace ? (existing ? (
            <Field label="Workspace" hint="This session shares the folder and its reports">
              <Input value={existing} readOnly className="font-mono" />
            </Field>
          ) : (
            <Field label="Topic" hint="A new local git folder, local/<topic>-<date>, for research before a project.">
              <Input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="e.g. Beyblade tournament scheduling apps" maxLength={80} autoFocus />
            </Field>
          )) : (
            <>
              <Field label="Project">
                <Select value={form.project_id} onChange={(e) => { set("project_id", Number(e.target.value)); setIssue({}); }} disabled={!usable.length}>
                  {!usable.length && <option value={0}>No project yet</option>}
                  {usable.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </Select>
              </Field>
              {project && <p className="-mt-2 truncate font-mono text-[11.5px] text-faint" title={project.workspace_path}>{project.host && project.host !== "local" ? `${project.host}: ` : ""}{project.workspace_path}</p>}
              {project?.containers && <p className="text-[13px] text-muted">Container session <PreviewBadge />. Every launch requires this host's passing qualification record.</p>}
              {untrusted && <Note icon={ShieldAlert} className="-mt-1">Not trusted for agents yet. Launch asks you to trust it first; your choices here stay.</Note>}
              {projects.data && !usable.length && (
                <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line px-3 py-2.5 text-[13px] text-muted">
                  No project yet. Make one here; this form keeps what you've entered.
                  <Button size="sm" variant="primary" onClick={() => setCreatingProject(true)}><FolderPlus size={13} aria-hidden="true" /> New project</Button>
                </div>
              )}
              {feature("issue_sources") && !multi && project && !project.private_workspace && <NewSessionIssuePicker key={project.id} projectId={project.id}
                selected={issue.projectId === project.id ? issue.ref : undefined}
                onSelect={(ref, name) => { setIssue({ projectId: project.id, ref }); if (name && !form.name.trim()) set("name", name); }} />}
            </>
          )}
          <Field label={multi ? "Task" : "Task (optional)"} hint={multi ? "The same task is sent once to every run." : scheduling ? "Sent at the start of every run." : orchestrator ? "This first message starts the lead’s planning session. Edit it to give your own instructions. It is not resent on resume." : "Sent once when the session starts. Without one, the agent waits for you."}>
            <Textarea rows={orchestrator ? 6 : 3} value={firstMessage} onChange={(e) => set("initial_prompt", e.target.value)} placeholder="What should the agent work on?"
              autoFocus={!workspace} aria-required={multi || undefined} />
          </Field>
          {sharing.length > 0 && (
            <Note icon={ShieldAlert}>
              <span className="block">{sharing.map((x) => x.name).join(", ")} {sharing.length === 1 ? "is" : "are"} already working in this checkout. Two agents in one folder can overwrite each other's files.</span>
              <button type="button" className="mt-1 inline-flex pointer-coarse:min-h-11 items-center font-medium underline underline-offset-2" onClick={() => set("worktree", true)}>Use its own worktree</button>
            </Note>
          )}
          {scheduling && <ScheduleTiming value={scheduleConfig} onChange={setScheduleConfig} />}
        </section>

        <section aria-labelledby="ns-who" className="min-w-0 space-y-3">
          <h3 id="ns-who" className="text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">Who</h3>
          <Field label="Machine" hint={workspace ? "Local workspaces stay on this Rynhult. Choose a project to use another machine." : selectedMachine ? machineDetail(selectedMachine) : "Choose where this session runs."}>
            <Select value={machine} disabled={machines.isPending || workspace} onChange={e => chooseMachine(e.target.value)}>
              {!machines.data?.some(item => item.name === machine) && <option value={machine}>{machine === "local" ? "This Rynhult (main)" : machine}</option>}
              {machines.data?.map(item => <option key={item.name} value={item.name}
                disabled={!item.available || !!project?.containers && item.name !== "local" || !!project?.private_workspace && item.name !== (project.host || "local")}>
                {item.label}{!item.available ? ` — ${item.reason}` : ""}</option>)}
            </Select>
          </Field>
          <ErrorNote error={machines.error} title="Couldn't load machines." />
          {machines.isError && <Button size="sm" onClick={() => machines.refetch()}>Retry machines</Button>}
          {needsClone && <div className="space-y-2 text-xs text-muted">
            <p>This machine needs a copy of the project. Clone copies committed Git files through Rynhult, not uncommitted changes or login credentials. The transfer limit is 8 MiB.</p>
            {cloneOnMachine ? <p role="status">The project will be cloned on {machine} when you launch.</p>
              : <Button onClick={() => setCloneOnMachine(true)}>Clone on {machine}</Button>}
          </div>}
          {otherMachine && !needsClone && preview.data?.machine === machine && <p className="text-xs text-muted">Using an existing checkout on {machine}.</p>}
          {multi ? <MultiRunFields value={runChoices} onChange={setRunChoices} catalog={catalog} providers={registry.list}
            models={(p) => modelsFor(models.data, p)} /> : <>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Agent">
                <Select value={form.provider} onChange={(e) => {
                  const provider = e.target.value as Provider;
                  set("provider", provider); setBudgetAck(null); set("model", ""); set("effort", ""); set("endpoint", ""); setCustomModel(false);
                  const p = catalog?.profiles.find((x) => x.id === profile);
                  setChosenOptions((p?.[provider] as OptionValues | undefined) ?? {});
                  setPicks((x) => ({ skills: compatible("skills", x.skills, provider), agents: compatible("agents", x.agents, provider), mcps: compatible("mcps", x.mcps, provider) }));
                  if (profile && catalog?.profiles.find((p) => p.id === profile)?.providers?.includes(provider) === false) setProfile("");
                }}>
                  {(registry.list.length ? registry.list : [{ id: "claude", label: "Claude Code" }, { id: "codex", label: "Codex" }] as ProviderInfo[])
                    .filter(p => !desktopBuild || ["claude", "codex"].includes(p.id)).map((p) => (
                      <option key={p.id} value={p.id} disabled={p.installed === false}>
                        {p.label}{p.preview ? " (preview)" : ""}{p.installed === false ? ` (not installed on ${machine})` : ""}
                      </option>
                    ))}
                </Select>
              </Field>
              <Field label="Profile">
                <Select value={profile} onChange={(e) => chooseProfile(e.target.value)}>
                  {!workspace && <option value="">None (house rules only)</option>}
                  {profiles.map((p) => <option key={p.id} value={p.id} title={p.description}>{p.name}</option>)}
                </Select>
              </Field>
            </div>
            {profile ? <p className="-mt-1 text-xs text-muted">{catalog?.profiles.find((p) => p.id === profile)?.description}</p>
              : !workspace && profiles.length > 0 && <p className="-mt-1 text-xs text-muted">Optional: a profile gives the agent a role, with its own skills and tools. Not sure? Leave None, or choose Full-stack to build an app end to end.</p>}
            {info?.preview && (
              <Note>{info.label} support is a preview: {info.preview_note ?? "Rynhult hasn't been verified against the real CLI yet."}
                {info.tier !== "full" && " It has no reliable “needs you” signal, so check on it in the terminal."}</Note>
            )}
            <SessionKeys keys={preview.data?.keys} />
          </>}
          <SessionName value={form.name} auto={autoName} onChange={(name) => set("name", name)} />
          <p className="text-xs text-faint">Runs on {runsOn} as <span className="font-mono">{runsAs}</span>. Watch and answer it here, or in its terminal{info?.supports.includes("remote_control") ? " or Remote Control" : ""}.</p>
        </section>

        <section aria-label="Advanced" className="min-w-0 rounded-lg border border-line">
          <button type="button" onClick={() => setAdvanced(!advanced)} aria-expanded={advanced} aria-controls="ns-advanced"
            className="flex w-full items-start gap-2 px-3 py-2.5 text-left">
            <ChevronRight size={14} className={clsx("mt-0.5 shrink-0 text-faint transition-transform", advanced && "rotate-90")} aria-hidden="true" />
            <span className="min-w-0 flex-1">
              <span className="block text-[13px] font-medium text-fg">Advanced</span>
              <span className="block text-xs text-muted [overflow-wrap:anywhere]" data-testid="advanced-summary">{summary}</span>
            </span>
            <span className="text-xs text-accent">{advanced ? "Done" : "Change"}</span>
          </button>
          {advanced && (
            <div id="ns-advanced" className="min-w-0 space-y-5 border-t border-line p-3">
              {!schedule && runModes.length > 1 && (
                <fieldset className="min-w-0 space-y-2">
                  <legend className="mb-1.5 text-xs font-medium text-muted">Run</legend>
                  {runModes.map((m) => (
                    <label key={m.id} className="flex items-start gap-2.5 text-[13px]">
                      <input type="radio" name="ns-run" value={m.id} checked={run === m.id} className="mt-0.5 size-3.5 accent-[var(--accent)]"
                        disabled={m.id === "helpers" && (registry.isPending || !!registry.error)}
                        onChange={() => {
                          setRun(m.id);
                          setConfirmBypass(false);
                          if (m.id === "helpers") setTeamGrant(defaultGrant(machine, registry.data?.team_agents));
                          if (m.id === "schedule" && !form.worktree) set("worktree", true);  // scheduled runs default to their own worktree
                        }} />
                      <span><span className="font-medium">{m.label}</span><span className="block text-xs text-muted">{m.detail}</span></span>
                    </label>
                  ))}
                </fieldset>
              )}
              {orchestrator && !workspace && <OrchestratorFields key={project?.id} projectId={project?.id} value={teamGrant}
                onChange={(value) => { setTeamGrant(value); setConfirmBypass(false); }} onOpenBrief={onClose} catalog={catalog} providers={registry.list} />}
              {!workspace && project && !multi && (
                <label className="flex items-start gap-2.5 text-[13px]">
                  <input type="checkbox" className="mt-0.5 size-3.5 accent-[var(--accent)]" checked={orchestrator || otherMachine || !!project.private_workspace || form.worktree} disabled={orchestrator || otherMachine || project.private_workspace}
                    onChange={(e) => set("worktree", e.target.checked)} />
                  <span><span className="font-medium">Own worktree</span>
                    <span className="block text-xs text-muted">A separate copy on its own branch (acc/…), so parallel agents don't overwrite each other's files. Needs git.{otherMachine ? " Required on another machine." : project.private_workspace ? " Required for Development workspaces; git stays local-only." : ""}</span></span>
                </label>
              )}
              {!multi && <>
                {usableEndpoints.length > 0 && (
                  <Field label="Models from" hint={endpoint ? `${endpoint.name}: hooks, resume and the terminal work as usual; only the model changes.` : undefined}>
                    <Select value={form.endpoint} onChange={(e) => { set("endpoint", e.target.value); set("model", ""); setCustomModel(false); }}>
                      <option value="">{info?.label ?? "The agent"}'s own models</option>
                      {usableEndpoints.map((e) => <option key={e.id} value={e.id}>{e.name} ({e.models.length} models)</option>)}
                    </Select>
                  </Field>
                )}
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <Field label="Model" hint={models.data?.fetched_at ? `List refreshed ${new Date(models.data.fetched_at * 1000).toLocaleDateString()}` : undefined}>
                    {customModel ? (
                      <Input value={form.model} onChange={(e) => set("model", e.target.value)} placeholder={info?.model_format ?? "model id"} autoFocus
                        onBlur={() => { if (!form.model) setCustomModel(false); }} />
                    ) : (
                      <Select value={form.model} onChange={(e) => {
                        if (e.target.value === "__custom") { setCustomModel(true); set("model", ""); } else set("model", e.target.value);
                      }}>
                        <option value="">{endpoint ? "Choose a model…" : "CLI default"}</option>
                        {(endpoint ? endpoint.models.map((m) => ({ ...m, description: "", source: "endpoint" })) : modelsFor(models.data, form.provider)).map((m) => (
                          <option key={m.id} value={m.id} title={m.description}>{m.label}{m.source === "cli alias" ? "" : ` (${m.id})`}</option>
                        ))}
                        <option value="__custom">Custom…</option>
                      </Select>
                    )}
                  </Field>
                  {(!info || info.supports.includes("effort")) && (
                    <Field label="Effort">
                      <Select value={form.effort} onChange={(e) => set("effort", e.target.value)}>
                        <option value="">Default</option>
                        {(modelsFor(models.data, form.provider).find((m) => m.id === form.model)?.efforts ?? ["low", "medium", "high", "xhigh", "max"])
                          .map((x) => <option key={x}>{x}</option>)}
                      </Select>
                    </Field>
                  )}
                  {info?.options.filter((o) => o.kind === "select").map((o) => (
                    <Field key={o.key} label={o.label} hint={o.help || undefined}>
                      <Select value={String(options[o.key])} onChange={(e) => setChosenOptions((x) => ({ ...x, [o.key]: e.target.value }))}>
                        {o.choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
                      </Select>
                    </Field>
                  ))}
                </div>
                {info?.options.filter((o) => o.kind === "bool").map((o) => (
                  <label key={o.key} className="flex items-start gap-2 text-[13px] text-fg">
                    <input type="checkbox" checked={options[o.key] === true} disabled={!!endpoint && o.key === "remote_control"}
                      onChange={(e) => setChosenOptions((x) => ({ ...x, [o.key]: e.target.checked }))}
                      className="mt-0.5 size-3.5 accent-[var(--accent)]" />
                    <span>{o.label}{o.help && <span className="block text-xs text-faint">{o.help}</span>}</span>
                  </label>
                ))}
                <fieldset hidden={!feature("automation")} className="min-w-0 space-y-3">
                  <legend className="mb-1.5 text-xs font-medium text-muted">Goal and auto-resume</legend>
                  <AutomationFields provider={form.provider} value={automation} onChange={setAutomation} disabled={!!form.endpoint} />
                </fieldset>
                {catalog && (
                  <div className="rounded-lg border border-line">
                    <button type="button" onClick={() => setShowPicks(!showPicks)} aria-expanded={showPicks}
                      className="flex w-full flex-wrap items-center gap-x-2 gap-y-0.5 px-3 py-2.5 text-left text-[13px]">
                      <ChevronRight size={14} className={clsx("text-faint transition-transform", showPicks && "rotate-90")} aria-hidden="true" />
                      <span className="font-medium text-fg">Skills and tools</span>
                      <span className="text-xs text-muted">{picks.skills.length} skill{picks.skills.length === 1 ? "" : "s"} · {picks.agents.length} agent{picks.agents.length === 1 ? "" : "s"} · {picks.mcps.length} MCP</span>
                      <span className="ml-auto text-xs text-accent">{showPicks ? "Done" : "Customize"}</span>
                    </button>
                    {!showPicks && selectedCount > 0 && (
                      <div className="flex flex-wrap gap-1 border-t border-line px-3 py-2.5">
                        {[...picks.skills, ...picks.agents, ...picks.mcps.map((m) => `mcp · ${m}`)].map((x) => <Badge key={x}>{x}</Badge>)}
                      </div>
                    )}
                    {showPicks && (
                      <div className="space-y-3 border-t border-line p-3">
                        <SearchInput value={pickQuery} onChange={setPickQuery} placeholder="Filter skills, agents and servers" className="w-full" />
                        <CatalogPicker title="Skills" query={pickQuery} items={catalog.skills} selected={picks.skills} provider={form.provider} onChange={(skills) => setPicks((x) => ({ ...x, skills }))} />
                        {(!info || info.supports.includes("agents")) && <CatalogPicker title="Agents (subagents)" query={pickQuery} items={catalog.agents} selected={picks.agents} provider={form.provider} onChange={(agents) => setPicks((x) => ({ ...x, agents }))} />}
                        <CatalogPicker title="MCP servers" query={pickQuery} items={catalog.mcps} selected={picks.mcps} provider={form.provider} onChange={(mcps) => setPicks((x) => ({ ...x, mcps }))} />
                        <p className="text-[11px] text-faint">Only what is selected is enabled for this session. Hover an item for its source and licence.</p>
                      </div>
                    )}
                  </div>
                )}
              </>}
              <div className="min-w-0 space-y-2">
                <div className="text-xs font-medium text-muted">Exactly what will run</div><p className="text-xs text-muted">Commands containing placeholders such as &lt;run-dir&gt; are previews, not directly runnable. Replace placeholders before running.</p>
                {multi ? multiPreviews.map((p, i) => <details key={i} className="min-w-0 rounded-lg border border-line bg-bg text-xs">
                  <summary className="cursor-pointer px-3 py-2 font-medium">Run {i + 1}: {registry.byId(runChoices[i].provider)?.label ?? runChoices[i].provider}{p.error ? " · can't start" : ""}</summary>
                  <div className="space-y-2 border-t border-line p-3">
                    {p.data ? <>
                      <CopyBlock text={`cd ${shellQuote(p.data.cwd)}
${p.data.command.map(shellQuote).join(" ")}`} label="Copy launch command" />
                    </> : !p.error && <Skeleton className="h-10" />}
                  </div>
                </details>) : <>
                {!preview.error && <div className="rounded-lg border border-line bg-bg p-3 font-mono text-[12px] leading-relaxed">
                  {preview.data ? (
                    <>
                      <CopyBlock text={`cd ${shellQuote(preview.data.cwd)}
${preview.data.command.map(shellQuote).join(" ")}`} label="Copy launch command" />
                    </>
                  ) : <Skeleton className="h-10" />}
                </div>}
                {preview.data && Object.entries(preview.data.files).map(([name, content]) => (
                  <details key={name} className="min-w-0 rounded-lg border border-line bg-bg text-[12px]">
                    <summary className="cursor-pointer px-3 py-2 font-mono text-muted hover:text-fg">{name}</summary>
                    <CopyBlock text={content} label={`Copy ${name}`} />
                  </details>
                ))}
                <p className="text-xs text-faint">Hooks report status to Rynhult; nothing else is injected{preview.data?.keys?.keys.length ? " besides the keys listed above, as environment variables" : ""}.</p>
                </>}
              </div>
            </div>
          )}
        </section>

        {multi ? multiPreviews.map((p, i) => {
          if (!p.error && !p.data?.not_delivered?.length && !p.data?.keys?.keys.length && !p.data?.keys?.held_back.length) return null;
          const label = `Run ${i + 1}: ${registry.byId(runChoices[i].provider)?.label ?? runChoices[i].provider}`;
          return <div key={i} role="group" aria-label={label} className="min-w-0 space-y-2">
            <p className="text-xs font-medium text-muted">{label}</p>
            <ErrorNote error={p.error} title="This launch can't start as it is." />
            <SessionKeys keys={p.data?.keys} />
            <DeliveryWarnings preview={p.data} agent={label} />
          </div>;
        }) : <>
          <ErrorNote error={preview.error} title="This launch can't start as it is." />
          <DeliveryWarnings preview={preview.data} agent={info?.label ?? form.provider} />
        </>}
        {dangerous && <p className="text-xs text-muted">This confirmation cannot override Security. Provider prompts must be Off in an owner-reviewed scope before bypass options can launch.</p>}
        {dangerous && (
          <label className="flex items-start gap-2 rounded-md border border-bad/30 bg-bad/10 p-3 text-xs text-bad">
            <input type="checkbox" checked={confirmBypass} onChange={(e) => setConfirmBypass(e.target.checked)} className="mt-0.5" />
            <span><ShieldAlert size={13} className="mr-1 inline" aria-hidden="true" />{teamDangerous ? `The selected helpers can run commands without asking. I accept their permission modes for ${scheduling ? "every run of this saved schedule" : "this team"}; it is recorded in the audit log.` : `The agent will run commands without asking. I accept this for ${scheduling ? "every run of this saved schedule" : "this session"}; it is recorded in the audit log.`}</span>
          </label>
        )}
        {!scheduling && !multi && budgetPaused.length > 0 && (
          <label className="flex items-start gap-2 rounded-md border border-wait/30 bg-wait/10 p-3 text-xs text-wait">
            <input type="checkbox" checked={budgetOk} onChange={(e) => setBudgetAck(e.target.checked ? budgetKey : null)} className="mt-0.5" />
            <span>Your budget pauses new launches of this agent ({budgetPaused.join(", ")}). Launch this one anyway.</span>
          </label>
        )}
      </div>}
    </Dialog>
    <NewProjectDialog open={creatingProject} onClose={() => setCreatingProject(false)}
      onCreated={(made) => { setMode("project"); set("project_id", made.id); setIssue({}); }} />
    </>
  );
}
