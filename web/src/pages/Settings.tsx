import { ImageAccess } from "../components/ImageAccess";
import { AdminFeatures } from "../components/AdminFeatures";
import { Feature, useFeatures } from "../components/Feature";
import { SelectionPreferences } from "../components/SelectionReasons";
import { Telemetry, TelemetryPaused } from "../components/Telemetry";
import { DNSSettings } from "../components/DNSSettings";
import { useQuery } from "@tanstack/react-query";
import { flushSync } from "react-dom";
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { buttonClass, Card, CardHeader, Page, PageHeader, Select } from "../components/ui";
import { Devices } from "../components/Devices";
import { NotificationDevices, NotificationSettings } from "../components/NotificationDevices";
import { Passkeys } from "../components/Passkeys";
import { PasswordCard, People, SignIns } from "../components/People";
import { SecuritySettings } from "../components/SecuritySettings";
import { ToolPolicies } from "../components/Tools";
import { Budgets } from "../components/Budgets";
import { ContainerLimits } from "../components/Containers";
import { Certificate } from "../components/Certificate";
import { PublishingAddresses } from "../components/PublishingAddresses";
import { Updates } from "../components/Updates";
import { IssueServers } from "../components/IssueServers";
import { KeysCard } from "../components/Keys";
import { RiskLabelsSetting } from "../components/RiskLabelsSetting";
import { CompanySignIn } from "../components/CompanySignIn";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useInstance } from "../lib/queries";
import type { AuthState } from "../lib/types";

const PERSONAL = ['Access devices', 'Passkeys', 'Password', 'Sign-ins', 'Notifications', 'Notification devices', 'Keys', 'Risk labels', 'Guardrails', 'Security', 'Certificate'];
const INSTANCE = ['Admin features', 'Remote access', 'Usage and bug reports', 'Updates', 'Publishing', 'People', 'Company sign-in', 'DNS', 'Issue sources', 'Tool approvals', 'Budgets', 'Selection allow-lists', 'Image access', 'Container limits'];
const anchor = (name: string) => name.toLowerCase().replaceAll(' ', '-');
const RENAMED: Record<string, string> = { 'your-devices': 'access-devices', devices: 'notification-devices' };
const SettingsView = createContext<{ selected: string; errors: string[] }>({ selected: "all", errors: [] });
function SettingsSection({ name, children }: { name: string; children: React.ReactNode }) {
  const { selected, errors } = useContext(SettingsView);
  const navigate = useNavigate();
  const id = anchor(name);
  const [invalid, setInvalid] = useState(false);
  useEffect(() => { if (selected === id || selected === 'all') setInvalid(false); }, [selected, id]);
  // A section's dialogs render inside it. Back or Forward while one is open must not hide that modal and leave
  // its focus trap holding a page the person can't see.
  const box = useRef<HTMLDivElement>(null);
  const [dialog, setDialog] = useState(false);
  useEffect(() => {
    const read = () => setDialog(!!box.current?.querySelector('[role="dialog"]'));
    read();
    const observer = new MutationObserver(read);
    observer.observe(box.current!, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, []);
  // Keep forms mounted: navigation must not discard a draft, an in-flight save or its error.
  return <div ref={box} id={id} data-settings-section={name} hidden={selected !== "all" && selected !== id && !(id !== "updates" && errors.includes(id)) && !invalid && !dialog}
    onInvalidCapture={() => {
      // Router navigation is deferred. Local state must reveal the form before
      // native validation tries to focus its first invalid control.
      if (selected !== "all" && selected !== id && !invalid) {
        flushSync(() => setInvalid(true));
        navigate(`#${id}`);
      }
    }}
    className="scroll-mt-4 min-w-0 space-y-2" tabIndex={-1}>{children}</div>;
}

// These are existing links used outside Settings, inside their enclosing section.
const LEGACY_SECTIONS: Record<string, string> = {
  telemetry: 'usage-and-bug-reports', 'infrastructure-keys': 'dns', 'dns-certificates': 'dns',
};
type Attention = { name: string; messages: string[]; error: boolean };

/** Preserve the kit's warning/error text even when its form is not the focused section.
 * Observe the mounted forms, rather than duplicating their security/query state here.
 * The summary is plain React text; it never copies HTML or form values.
 */
function useSettingsAttention(root: React.RefObject<HTMLDivElement | null>) {
  const [attention, setAttention] = useState<Attention[]>([]);
  useEffect(() => {
    if (!root.current) return;
    const read = () => {
      const next = [...root.current!.querySelectorAll<HTMLElement>('[data-settings-section]')].map(section => {
        const messages = new Set<string>();
        let error = false;
        // Device lifecycle uses a neutral Badge even while revocation is pending.
        // Keep that security state (and its device label) visible too.
        if (section.id === 'access-devices') {
          section.querySelectorAll<HTMLElement>('li p').forEach(row => {
            const status = row.querySelector('span')?.textContent?.trim();
            if (status && ['Revocation pending', 'revoked', 'expired'].includes(status)) messages.add(row.textContent!.trim());
          });
        }
        section.querySelectorAll<HTMLElement>('[role="alert"], .text-wait, .text-bad').forEach(note => {
          // Danger buttons are actions, not active warnings. Closed details and dialogs
          // retain their own disclosure/focus semantics and must not be flattened here.
          if (note.closest('button, input, select, textarea, [role="dialog"], dialog, [data-settings-informational]')) return;
          let parent: HTMLElement | null = note;
          while (parent && parent !== section) {
            if (parent.hidden || (parent instanceof HTMLDetailsElement && !parent.open && !parent.querySelector('summary')?.contains(note))) return;
            parent = parent.parentElement;
          }
          if (note.parentElement?.closest('[role="alert"], .text-wait, .text-bad')) return;
          if (note.getAttribute("role") === "alert") error = true;
          // The certificate Badge's adjacent sentence explains the missing trust.
          const source = section.id === 'certificate' && note.matches('span.text-bad') ? note.parentElement! : note;
          const copy = source.cloneNode(true) as HTMLElement;
          copy.querySelectorAll('details, button, input, select, textarea').forEach(child => child.remove());
          const text = copy.textContent?.replace(/\s+/g, ' ').trim();
          const term = note.closest('dd')?.previousElementSibling;
          if (text) messages.add(term?.tagName === 'DT' ? `${term.textContent}: ${text}` : text);
        });
        return { name: section.dataset.settingsSection!, messages: [...messages], error };
      }).filter(item => item.messages.length);
      setAttention(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next);
    };
    read();
    const observer = new MutationObserver(read);
    observer.observe(root.current, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['class', 'role', 'hidden', 'open'] });
    return () => observer.disconnect();
  }, [root]);
  return attention;
}

/** What a screen reader should hear from the summary: only a warning that newly appears in a section the person isn't
 * looking at. The summary itself isn't live: choosing a section reshuffles it (navigation, not news), and an error keeps
 * its section shown, where the section's own alert already speaks. Warnings present while the page loads aren't news
 * either: they set the baseline. */
function useAttentionNews(attention: Attention[], selected: string) {
  const [said, setSaid] = useState('');
  const known = useRef(new Set<string>());
  const settled = useRef(false);
  const looking = useRef(selected);
  looking.current = selected;
  useEffect(() => { const timer = window.setTimeout(() => { settled.current = true; }, 2000); return () => window.clearTimeout(timer); }, []);
  useEffect(() => {
    const warnings = attention.filter(item => !item.error).flatMap(item => item.messages.map(message => ({ name: item.name, line: `${item.name}: ${message}` })));
    const fresh = warnings.filter(w => !known.current.has(w.line) && anchor(w.name) !== looking.current);
    known.current = new Set(warnings.map(w => w.line));
    if (settled.current && fresh.length) setSaid(fresh.map(w => w.line).join(' '));
  }, [attention]);
  return said;
}

export function Settings() {
  const { hash } = useLocation();
  const navigate = useNavigate();
  const content = useRef<HTMLDivElement>(null);
  const attention = useSettingsAttention(content);
  const auth = useQuery({ queryKey: ["auth"], queryFn: () => api<AuthState>("/api/auth/state") });
  const owner = auth.data?.role === "owner";
  const instance = useInstance();
  const feature = useFeatures();
  // Usage and bug reports stays listed while telemetry is off: deleting data already sent must remain possible.
  const sectionFeatures: Record<string, string> = { "Remote access": "remote_access", Publishing: "publishing", "Issue sources": "issue_sources", "Company sign-in": "organizations", "Selection allow-lists": "orchestrator", Notifications: "notifications", "Notification devices": "notifications", "Risk labels": "risk_labels" };
  const groups = [
    { label: 'Personal', names: PERSONAL.filter(name => (!sectionFeatures[name] || feature(sectionFeatures[name])) && (name !== 'Certificate' || owner || instance.private_ca)) },
    ...(owner ? [{ label: 'Instance', names: INSTANCE.filter(name => (!sectionFeatures[name] || feature(sectionFeatures[name])) && (name !== 'Company sign-in' || instance.organizations) && (name !== 'Selection allow-lists' || instance.orchestrator)) }] : []),
  ];
  const requested = hash.slice(1);
  const section = RENAMED[requested] ?? LEGACY_SECTIONS[requested] ?? requested;
  const known = groups.some(group => group.names.some(name => anchor(name) === section));
  // Unknown fragments retain the old full-page behavior, so future nested anchors
  // still work. An owner-only fragment never grants access to an owner component.
  const selected = !requested ? 'passkeys' : requested === 'settings-index' || requested === 'all' || !known ? 'all' : section;
  const previousFragment = useRef(requested);
  const choosingSection = useRef(false);
  useEffect(() => {
    const previous = previousFragment.current;
    previousFragment.current = requested;
    if (choosingSection.current) { choosingSection.current = false; return; }
    if (!requested && previous === requested) return;
    const targetId = requested === 'all' ? 'settings-index' : (RENAMED[requested] ?? requested) || 'passkeys';
    const focus = () => {
      const target = document.getElementById(targetId);
      if (!target || target.closest('[hidden]')) return false;
      if (target.contains(document.activeElement) && document.activeElement?.matches(':invalid')) return true;
      if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
      target.scrollIntoView({ block: 'start' });
      target.focus({ preventScroll: true });
      return true;
    };
    if (focus()) return;
    // Some bookmarked targets arrive with query results (e.g. DNS credentials).
    const observer = new MutationObserver(() => { if (focus()) observer.disconnect(); });
    observer.observe(content.current!, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [requested, selected]);
  const announced = useAttentionNews(attention, selected);
  return (
    <Page>
      <PageHeader title="Settings" description="Your sign-in, keys, devices and notifications; owners also manage instance policy." />
      <nav id="settings-index" tabIndex={-1} aria-label="Settings sections" className="space-y-3 scroll-mt-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="min-w-0 flex-1 space-y-1 text-xs text-muted" htmlFor="settings-section">Settings section
            <Select id="settings-section" className="w-full pointer-coarse:min-h-11" value={selected} onChange={event => {
              choosingSection.current = true;
              event.currentTarget.focus();
              navigate(`#${event.target.value}`);
            }}>
              <option value="all">All sections</option>
              {groups.map(group => <optgroup label={group.label} key={group.label}>{group.names.map(name => <option value={anchor(name)} key={name}>{name}</option>)}</optgroup>)}
            </Select>
          </label>
          <Link className={buttonClass()} to={selected === 'all' ? '#passkeys' : '#all'}>{selected === 'all' ? 'Focus a section' : 'All sections'}</Link>
        </div>
        {selected === 'all' && groups.map(group => <div key={group.label}><h2 className="text-xs font-semibold text-muted">{group.label}</h2><div className="flex flex-wrap gap-x-4 gap-y-1">{group.names.map(name => <Link className={buttonClass({ size: "sm" })} to={`#${anchor(name)}`} key={name}>{name}</Link>)}</div></div>)}
      </nav>
      {selected !== 'all' && attention.some(item => anchor(item.name) !== selected) && <section aria-label="Settings needing attention" className="[overflow-wrap:anywhere] space-y-2 rounded-lg border border-wait/30 p-3 text-xs">
        <h2 className="font-semibold text-wait">Settings needing attention</h2>
        {attention.filter(item => anchor(item.name) !== selected).map(item => <div key={item.name}>
          <Link className={buttonClass({ size: "sm" })} to={`#${anchor(item.name)}`}>{item.name}</Link>
          <ul className="list-disc space-y-1 pl-4 text-wait">{item.messages.map(message => <li key={message}>{message}</li>)}</ul>
        </div>)}
      </section>}
      <p role="status" className="sr-only" data-testid="settings-announcer">{announced}</p>
      <SettingsView.Provider value={{ selected, errors: attention.filter(item => item.error).map(item => anchor(item.name)) }}><div ref={content} className="space-y-6">
      {owner && <SettingsSection name="Admin features"><AdminFeatures /></SettingsSection>}
      {owner && <Feature name="remote_access"><SettingsSection name="Remote access"><Card><CardHeader title="Remote access" subtitle="Use your VPN or Tailscale from outside. Public access without one is off by default and not yet qualified · LAN device pairing" action={<Link className={buttonClass()} to="/settings/remote-access">Remote access</Link>} /></Card></SettingsSection></Feature>}
      <SettingsSection name="Access devices"><Devices owner={owner} /></SettingsSection>
      {owner && <SettingsSection name="Usage and bug reports">{feature("telemetry") ? <Telemetry /> : <TelemetryPaused />}</SettingsSection>}
      {owner && <SettingsSection name="Updates"><Updates /></SettingsSection>}
      {owner && <Feature name="publishing"><SettingsSection name="Publishing"><PublishingAddresses /></SettingsSection></Feature>}
      {owner && <SettingsSection name="People"><People /></SettingsSection>}
      {owner && instance.organizations && <Feature name="organizations"><SettingsSection name="Company sign-in"><CompanySignIn /></SettingsSection></Feature>}
      <SettingsSection name="Keys"><KeysCard /></SettingsSection>
      {owner && <SettingsSection name="DNS"><DNSSettings /></SettingsSection>}
      {owner && <Feature name="issue_sources"><SettingsSection name="Issue sources"><IssueServers /></SettingsSection></Feature>}
      <Feature name="risk_labels"><SettingsSection name="Risk labels"><RiskLabelsSetting /></SettingsSection></Feature>
      <SettingsSection name="Guardrails"><Card><CardHeader title="Guardrails" subtitle="Guardrail switches now have one scoped editor in Security."/><div className="p-4"><Link className={buttonClass()} to="#security">Open Security settings</Link></div></Card></SettingsSection>
      <SettingsSection name="Security"><SecuritySettings owner={owner}/></SettingsSection>
      {owner && <SettingsSection name="Tool approvals"><ToolPolicies /></SettingsSection>}
      {owner && <SettingsSection name="Budgets"><Budgets /></SettingsSection>}
      {owner && instance.orchestrator && <Feature name="orchestrator"><SettingsSection name="Selection allow-lists"><SelectionPreferences /></SettingsSection></Feature>}
      {owner && <SettingsSection name="Image access"><ImageAccess /></SettingsSection>}
      {owner && <SettingsSection name="Container limits"><ContainerLimits /></SettingsSection>}
      {(owner || instance.private_ca) && <SettingsSection name="Certificate"><Certificate owner={owner} privateCa={!!instance.private_ca} /></SettingsSection>}
      <SettingsSection name="Sign-ins"><SignIns /></SettingsSection>
      <SettingsSection name="Passkeys"><Passkeys owner={owner} /></SettingsSection>
      <SettingsSection name="Password"><PasswordCard /></SettingsSection>
      <Feature name="notifications"><SettingsSection name="Notifications"><NotificationSettings /></SettingsSection></Feature>
      <Feature name="notifications"><SettingsSection name="Notification devices"><NotificationDevices /></SettingsSection></Feature>
      </div></SettingsView.Provider>
      <a href="#settings-index" className={buttonClass()}>Back to settings sections</a>
      <p className="text-center text-xs text-faint">Rynhult{instance.version ? ` v${instance.version}` : ""} · {instance.host} · <Link to="/guide" className="underline-offset-2 hover:underline">Guide</Link>{instance.source_url && <> · <a href={instance.source_url} target="_blank" rel="noreferrer" className="underline-offset-2 hover:underline">Source code</a> (MIT)</>}{instance.source_download && <> · <a href={instance.source_download} download className="underline-offset-2 hover:underline">Source of this version</a></>}</p>
    </Page>
  );
}
