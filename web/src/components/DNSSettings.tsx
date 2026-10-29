import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe, KeyRound } from "lucide-react";
import { useState } from "react";
import { api, post } from "../lib/api";
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useConfirm } from "./confirm";
import { announce, Badge, Button, Card, CardHeader, Dialog, ErrorNote, Field, Input, Note, QueryState, Select } from "./ui";

type Qualification = "qualified" | "preview";
interface ProviderChoice { id: string; qualification: Qualification }
interface Credential { qualification?: Qualification; id: string; name: string; provider: string; purpose: string; revision: number; disabled: boolean }
interface Binding { qualification?: Qualification; provider: string; zone?: string; certificate_id?: string; publishing_id?: string;
  revision: number; applied_revision: number | null; status: string; publishing_source?: string; legacy_publishing?: string }
type Action = { kind: "validate" | "add" | "replace" | "remove" | "disable" | "binding" | "apply" | "rollback" | "migrate" | "publishing-rollback" | "publishing-vault" | "adopt"; key?: Credential };

const providerNames: Record<string, string> = { cloudflare: 'Cloudflare', azure: 'Azure DNS', route53: 'Route 53', googleclouddns: 'Google Cloud DNS', digitalocean: 'DigitalOcean', hetzner: 'Hetzner DNS' };
const providerFields: Record<string, [string, string, boolean][]> = {
  googleclouddns: [['project_id', 'Google project ID', false], ['managed_zone', 'Managed zone ID', false],
    ['zone_name', 'Credential zone', false], ['service_account_json', 'Service account JSON', true]],
  digitalocean: [['api_token', 'DigitalOcean API token', true], ['zone_name', 'Credential zone', false]],
  hetzner: [['api_token', 'Hetzner DNS API token', true], ['zone_name', 'Credential zone', false]],
  cloudflare: [['api_token', 'Cloudflare API token', true]],
  azure: [['tenant_id', 'Tenant ID', false], ['subscription_id', 'Subscription ID', false], ['client_id', 'Client ID', false],
    ['client_secret', 'Client secret', true], ['resource_group', 'Resource group', false], ['zone_name', 'Credential zone', false]],
  route53: [['access_key_id', 'Access key ID', false], ['secret_access_key', 'Secret access key', true],
    ['hosted_zone_id', 'Hosted zone ID', false], ['zone_name', 'Credential zone', false], ['session_token', 'Session token (optional)', true]],
};

const previewLabel = 'Preview: not yet qualified for this release';
function QualificationNote({ qualification }: { qualification: Qualification }) {
  return qualification === 'preview' ? <div className="space-y-1">
    <Badge tone="wait" className="h-auto max-w-full whitespace-normal py-1">{previewLabel}</Badge>
    <p className="text-xs text-muted">Cloudflare is the qualified provider.</p>
  </div> : null;
}

// Permission boundaries stay next to the fields even when the creation runbook is closed.
function CredentialScope({ provider }: { provider: string }) {
  const scope: Record<string, string> = {
    cloudflare: 'Use separate certificate and Publishing tokens: Zone Read + DNS Edit on one zone. These permissions allow every DNS record in that zone, not just selected names or types.',
    azure: 'Use separate certificate and Publishing applications: DNS Zone Contributor on exactly one zone, never the resource group or subscription. This role permits every record type in that zone. Record the client secret expiry and rotate before it expires.',
    route53: 'Use separate certificate and Publishing IAM users on one hosted zone. Restrict record types (TXT for certificates; A/AAAA for Publishing), names and actions. Temporary credentials require a session token and replacement of all fields before expiry.',
    googleclouddns: 'Use separate service accounts: DNS Administrator on one managed zone for Publishing; certificates currently require project-level DNS Administrator in a dedicated project. A private credential file is required; root files follow credential generation retention, and live file-custody and rollback tests remain required.',
    digitalocean: 'Use separate scoped tokens: domain:read/create/delete for certificates; add domain:update for Publishing. Authority spans the account: use a dedicated account for zone isolation.',
    hetzner: 'Use separate Read & Write Cloud DNS API tokens in a dedicated Console project containing only the disposable zone. Tokens also permit other project resources; legacy DNS Console tokens do not work.',
  };
  return <p className="text-sm text-muted">{scope[provider]}</p>;
}

function CredentialHelp({ provider }: { provider: string }) {
  return <div className="space-y-2 text-sm text-muted">
    {provider === 'cloudflare' && <p>Create two custom API tokens with Zone Read and DNS Edit, each scoped to the one trial zone. Use one for certificates and one for Publishing. <a href="https://developers.cloudflare.com/fundamentals/api/get-started/create-token/" target="_blank" rel="noreferrer">Create a Cloudflare token</a>.</p>}
    {provider === 'azure' && <ol className="list-decimal space-y-1 pl-5">
      <li>In Microsoft Entra ID → App registrations → New registration, create a dedicated single-tenant application. Copy its Directory (tenant) ID and Application (client) ID.</li>
      <li>Under Certificates &amp; secrets, create a client secret. Enter its value here, and record its expiry in your rotation calendar.</li>
      <li>Open the one DNS zone → Access control (IAM) → Add role assignment. Choose DNS Zone Contributor and the new service principal. Scope the role to this DNS zone, never the resource group or subscription.</li>
      <li>Copy the subscription ID, resource group and zone name from the DNS zone overview. Repeat with a separate application for Publishing. This role permits every record type in that zone.</li>
      <li><a href="https://learn.microsoft.com/en-us/entra/identity-platform/howto-create-service-principal-portal" target="_blank" rel="noreferrer">Create an Azure service principal</a> · <a href="https://learn.microsoft.com/en-us/azure/dns/dns-protect-zones-recordsets" target="_blank" rel="noreferrer">Assign a role on one DNS zone</a>.</li>
    </ol>}
    {provider === 'route53' && <ol className="list-decimal space-y-1 pl-5">
      <li>Create a dedicated IAM user without console access. Under Permissions, add an inline policy for your one hosted-zone ARN. Create a separate user for certificates and Publishing.</li>
      <li>Allow ListResourceRecordSets on that hosted-zone ARN and GetChange on arn:aws:route53:::change/*. Allow ChangeResourceRecordSets on the hosted-zone ARN with ForAllValues:StringEquals for route53:ChangeResourceRecordSetsRecordTypes: TXT for certificates, A and AAAA for Publishing. Restrict normalized names and actions too; use the complete policy in the Guide.</li>
      <li>Under Security credentials → Create access key, choose the use case for an application outside AWS. Enter the access key ID and secret here. Copy the hosted-zone ID and zone name from Route 53. The pinned certificate module uses an explicit hosted-zone ID; account-wide zone discovery is unnecessary.</li>
      <li>For temporary credentials, also enter the session token and replace all fields before expiry. Rynhult uses only these explicit credentials.</li>
      <li><a href="https://docs.aws.amazon.com/IAM/latest/UserGuide/access-key-self-managed.html" target="_blank" rel="noreferrer">Create an AWS access key</a> · <a href="https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/specifying-conditions-route53.html" target="_blank" rel="noreferrer">Restrict Route 53 permissions</a>.</li>
    </ol>}
    {provider === 'googleclouddns' && <p>Create separate service accounts for certificates and Publishing. Grant DNS Administrator (roles/dns.admin) on one managed zone for Publishing. The certificate candidate lists managed zones across the project: use a dedicated project with project-level DNS Administrator until narrower discovery is qualified. Paste the service-account JSON on one line. The candidate requires a private credential file; root files follow credential generation retention; live file-custody and rollback tests remain required. <a href="https://docs.cloud.google.com/dns/docs/zones/iam-per-resource-zones" target="_blank" rel="noreferrer">Google managed-zone permissions</a>.</p>}
    {provider === 'digitalocean' && <p>Create separate scoped tokens. Certificates: domain:read, domain:create and domain:delete. Publishing: also domain:update. These permit domain operations across the account; use a dedicated account for zone isolation. <a href="https://docs.digitalocean.com/reference/api/scopes/domain/" target="_blank" rel="noreferrer">DigitalOcean token scopes</a>.</p>}
    {provider === 'hetzner' && <p>Create separate DNS API tokens in a dedicated Hetzner Console project with Read &amp; Write access. This candidate uses the Cloud DNS API and cannot use legacy DNS Console tokens. Project tokens also permit other project resources; keep only the disposable zone there. <a href="https://docs.hetzner.cloud/reference/cloud#dns" target="_blank" rel="noreferrer">Hetzner Cloud DNS API</a>.</p>}
    <p><a href="/guide/User-Guide/DNS-Providers" target="_blank" rel="noreferrer">DNS provider setup and trial-zone Guide (opens in a new tab)</a>.</p>
  </div>;
}

export function DNSSettings() {
  const client = useQueryClient();
  const confirm = useConfirm();
  const providers = useQuery({ queryKey: ["dns-providers"], queryFn: () => api<ProviderChoice[]>("/api/dns/providers") });
  const statusFor = (provider: string, status?: Qualification): Qualification =>
    status ?? providers.data?.find(choice => choice.id === provider)?.qualification ?? 'preview';
  const choiceLabel = (id: string, status?: Qualification) =>
    `${providerNames[id] ?? id}${statusFor(id, status) === 'preview' ? ' — ' + previewLabel : ''}`;
  const credentials = useQuery({ queryKey: ["dns-credentials"], queryFn: () => api<Credential[]>("/api/dns/credentials") });
  const binding = useQuery({ queryKey: ["dns-binding"], queryFn: () => api<Binding>("/api/dns/binding") });
  const keys = useQuery({ queryKey: ["vault"], queryFn: () => api<{ variable: string; name: string }[]>("/api/vault/keys") });
  const [notice, setNotice] = useState("");
  const [action, setAction] = useState<Action | null>(null);
  const [publication, setPublication] = useState("");
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("certificates");
  const [fields, setFields] = useState<Record<string, string>>({});
  const [password, setPassword] = useState("");
  const [provider, setProvider] = useState("cloudflare");
  const [zone, setZone] = useState("");
  const [certificate, setCertificate] = useState("");
  const [publishing, setPublishing] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const suggested = [...(keys.data ?? []).map(k => k.variable + " " + k.name), ...(credentials.data ?? []).map(k => k.name)]
    .some(n => /cloudflare|cf_api_token/i.test(n));
  const open = (next: Action) => {
    setPublication(""); setNotice(""); setName(""); setPurpose("certificates"); setFields({}); setPassword(""); setConfirmed(false);
    setProvider(next.key?.provider ?? (next.kind === "add" ? "cloudflare" : binding.data?.provider !== "internal" ? binding.data?.provider ?? "cloudflare" : "cloudflare"));
    setZone(binding.data?.zone ?? ""); setCertificate(binding.data?.certificate_id ?? "");
    setPublishing(binding.data?.publishing_id ?? ""); setAction(next); mutation.reset();
  };
  const close = () => { setAction(null); setFields({}); setPassword(""); mutation.reset(); };
  const mutation = useMutation({
    mutationFn: async () => {
      if (!action) return;
      const auth = { password };
      if (action.kind === 'adopt') return post('/api/dns/adopt', { ...auth, request_id: publication });
      if (action.kind === "add") return post('/api/dns/credentials', { ...auth, name, provider, purpose, fields: Object.fromEntries(Object.entries(fields).filter(([, value]) => value)) });
      if (action.kind === "replace") return post(`/api/dns/credentials/${action.key!.id}/replace`, { ...auth, fields: Object.fromEntries(Object.entries(fields).filter(([, value]) => value)) });
      if (action.kind === "remove" || action.kind === "disable" || action.kind === "validate") return post(`/api/dns/credentials/${action.key!.id}/${action.kind}`, auth);
      if (action.kind === "binding") return post('/api/dns/binding', { ...auth, provider, zone, certificate_id: certificate, publishing_id: publishing });
      if (action.kind === 'publishing-rollback' || action.kind === 'publishing-vault') return post('/api/dns/publishing-source', { ...auth, source: action.kind === 'publishing-rollback' ? 'legacy' : 'vault' });
      if (action.kind === "apply") return post('/api/dns/apply', { ...auth, revision: binding.data!.revision });
      return post(`/api/dns/${action.kind}`, auth);
    },
    onSuccess: () => { if (action?.kind === "validate") { setNotice("Credential readable. Certificate issuance remains unverified."); announce("Credential readable. Certificate issuance remains unverified."); } client.invalidateQueries({ queryKey: ["dns-credentials"] }); client.invalidateQueries({ queryKey: ["dns-binding"] }); close(); },
  });
  const destructive = async (kind: "remove" | "disable", key: Credential) => {
    if (await confirm({ title: `${kind === "remove" ? "Remove" : "Disable"} ${key.name}?`,
      body: 'Bound credentials cannot be removed or disabled. Changing a credential can interrupt renewal or Publishing.', confirm: kind === 'remove' ? 'Remove credential' : 'Disable credential', danger: true })) open({ kind, key });
  };
  const title = action?.kind === "add" ? "Add infrastructure credential" : action?.kind === "replace" ? "Replace infrastructure credential"
    : action?.kind === "adopt" ? "Adopt a reviewed legacy DNS record" : action?.kind === "binding" ? "Configure DNS" : action?.kind === "apply" ? "Apply DNS binding"
    : action?.kind === "publishing-rollback" ? "Restore legacy Publishing token" : action?.kind === "publishing-vault" ? "Use vault for Publishing" : action?.kind === "rollback" ? "Roll back DNS credentials" : action?.kind === "migrate" ? "Import legacy Cloudflare credentials" : "Confirm credential change";
  const submit = action?.kind === "binding" ? "Save DNS binding" : action?.kind === "add" || action?.kind === "replace" ? "Save credential"
    : action?.kind === "apply" ? "Confirm apply" : action?.kind === "remove" ? "Remove credential" : action?.kind === "disable" ? "Disable credential" : action?.kind === "validate" ? "Validate credential" : action?.kind === "rollback" ? "Roll back credentials" : action?.kind === "migrate" ? "Import credentials" : action?.kind === "adopt" ? "Adopt record" : action?.kind === "publishing-rollback" ? "Restore legacy token" : action?.kind === "publishing-vault" ? "Use vault for Publishing" : "Confirm change";
  const credentialForm = action?.kind === "add" || action?.kind === "replace";
  return <>
    <div id="infrastructure-keys"><Card>
      <CardHeader icon={KeyRound} title="Infrastructure Keys" subtitle="Owner-only DNS credentials. No reveal, project grants, sharing or delivery to agents or other machines."
        action={<Button onClick={() => open({ kind: "add" })}>Add infrastructure credential</Button>} />
      <QueryState query={credentials} errorTitle="Couldn't load infrastructure credentials.">
        {rows => <div className="divide-y divide-line">
          {notice && <p className="p-4 text-muted">{notice}</p>}
          {!rows.length && <p className="p-4 text-muted">No infrastructure credentials. Internal certificates need none.</p>}
          {rows.map(key => <div key={key.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
            <div className="min-w-0 flex-1"><p className="break-words">{key.name}</p>
              <p className="text-xs text-muted">{providerNames[key.provider]} · {key.purpose} · revision {key.revision}{key.disabled ? ' · Disabled' : ''}</p>
              <QualificationNote qualification={statusFor(key.provider, key.qualification)} /></div>
            <Button onClick={() => open({ kind: "validate", key })}>Validate <span className="sr-only">{key.name}</span></Button>
            <Button onClick={() => open({ kind: "replace", key })}>Replace <span className="sr-only">{key.name}</span></Button>
            <Button variant="danger" onClick={() => destructive("disable", key)}>Disable <span className="sr-only">{key.name}</span></Button>
            <Button variant="danger" onClick={() => destructive("remove", key)}>Remove <span className="sr-only">{key.name}</span></Button>
          </div>)}
          <div className="flex flex-wrap gap-2 p-4"><Button onClick={() => open({ kind: "migrate" })}>Import legacy Cloudflare credentials</Button>
            <Button onClick={() => open({ kind: "adopt" })}>Adopt reviewed legacy record</Button></div>
        </div>}
      </QueryState>
    </Card></div>
    <div id="dns-certificates"><Card>
      <CardHeader icon={Globe} title="DNS and certificates" subtitle="Separate certificate and Publishing credentials. DNS setup does not enable public Rynhult access."
        action={<Button onClick={() => open({ kind: "binding" })}>Configure DNS</Button>} />
      <QueryState query={binding} errorTitle="Couldn't load DNS settings.">
        {data => <div className="space-y-3 p-4">
          <Badge>{data.status === 'internal' ? 'Internal certificates' : data.status === 'applied' ? 'Applied' : data.status === 'failed' ? 'Apply failed' : 'Saved, not applied'}</Badge>
          <p className="text-muted">{data.zone ? `${providerNames[data.provider]} · ${data.zone} · revision ${data.revision}` : "Caddy’s internal CA remains the first-install default. Trust its root on your devices."}</p>
          <p className="text-xs text-muted">Saving records your certificate and Publishing selections. Production Publishing requires a qualified provider. Credential failures never trigger automatic fallback.</p>
          {data.provider !== "internal" && <QualificationNote qualification={statusFor(data.provider, data.qualification)} />}
          <p className="text-xs text-muted">Certificate issuance and renewal on this installation still require staging and production checks.</p>
          {data.zone && data.provider === 'cloudflare' && <p className="text-xs text-muted">Publishing source: {data.publishing_source === 'legacy' ? 'Legacy rollback token' : 'Selected vault credential'}</p>}
          {data.legacy_publishing === 'retired' && (data.publishing_source === 'legacy'
            ? <Note>The legacy Publishing token was retired in Keys. Publishing jobs are refused until you select Use vault for Publishing.</Note>
            : <p className="text-xs text-muted">The legacy Publishing token was retired when its imported copy was removed, disabled or replaced. It can’t be restored.</p>)}
          {data.zone && <div className="flex flex-wrap gap-2">
            <Button onClick={() => open({ kind: "apply" })}>Apply DNS binding</Button>
            <Button onClick={() => open({ kind: "rollback" })}>Roll back DNS credentials</Button>
            {data.provider === 'cloudflare' && (data.publishing_source === 'legacy' || data.legacy_publishing !== 'retired') && <Button onClick={() => open({ kind: data.publishing_source === 'legacy' ? 'publishing-vault' : 'publishing-rollback' })}>{data.publishing_source === 'legacy' ? 'Use vault for Publishing' : 'Restore legacy Publishing token'}</Button>}
          </div>}
        </div>}
      </QueryState>
    </Card></div>
    <Dialog open={!!action} onClose={close} title={title} dismissible={false}
      description="Confirm your current password for this owner-only change. Values are write-only."
      footer={<><ErrorNote error={mutation.error} /><Button variant="ghost" onClick={close} disabled={mutation.isPending}>Cancel</Button>
        <Button variant={action?.kind === "remove" || action?.kind === "disable" ? "danger" : "primary"} onClick={() => mutation.mutate()} disabled={mutation.isPending || !password ||
          ((action?.kind === "add" || action?.kind === "binding") && !providers.data) ||
          (credentialForm && ((providerFields[provider] ?? []).some(([key]) => key !== 'session_token' && !fields[key]) || (action?.kind === 'add' && !name.trim()))) ||
          (action?.kind === 'adopt' && !publication) ||
          (action?.kind === 'binding' && (!confirmed || !provider || !zone || !certificate || !publishing))}>{mutation.isPending ? 'Saving…' : submit}</Button></>}>
      <div className="space-y-3">
        {action?.kind === 'add' && <>
          <Field label="Credential name"><Input autoComplete="off" value={name} onChange={e => setName(e.target.value)} maxLength={80} /></Field>
          <Field label="Credential purpose"><Select value={purpose} onChange={e => setPurpose(e.target.value)}><option value="certificates">Certificates</option><option value="publishing">Publishing</option></Select></Field>
          <QueryState query={providers} errorTitle="Couldn't load DNS providers.">{choices =>
            <Field label="Credential provider"><Select value={provider} onChange={e => { setProvider(e.target.value); setFields({}); }}>
              {choices.map(({ id, qualification }) => <option value={id} key={id}>{choiceLabel(id, qualification)}</option>)}
            </Select></Field>}
          </QueryState>
        </>}
        {credentialForm && <>
          <QualificationNote qualification={statusFor(provider, action?.key?.qualification)} />
          {statusFor(provider, action?.key?.qualification) === 'preview' && <p className="text-sm text-muted">Pending live qualification. You can save credentials; production Publishing stays unavailable until both certificate and Publishing trial-zone tests pass.</p>}
          <CredentialScope provider={provider} />
          <p className="text-sm text-muted">Enter secrets here; never paste them into agent chat.</p>
          {(providerFields[provider] ?? []).map(([key, label, secret]) => <Field label={label} key={key}>
            <Input type={secret ? 'password' : 'text'} autoComplete="off" spellCheck={false} value={fields[key] ?? ''}
              onChange={e => setFields(previous => ({ ...previous, [key]: e.target.value }))} maxLength={4096} />
          </Field>)}
        </>}
        {action?.kind === 'replace' && <p className="text-muted">The previous encrypted value remains protected for 24 hours. Replacing it here does not revoke it at the provider. Apply and verify renewal before revoking the old token.</p>}
        {action?.kind === 'binding' && <>
          {suggested && <p className="text-muted">Cloudflare suggested from recognized key names. Confirm your provider below; ordinary keys are never converted or reused.</p>}
          <QueryState query={providers} errorTitle="Couldn't load DNS providers.">{choices =>
            <Field label="DNS provider"><Select value={provider} onChange={e => { setProvider(e.target.value); setCertificate(""); setPublishing(""); setConfirmed(false); }}>
              <option value="">Choose a provider</option>
              {choices.map(({ id, qualification }) => <option value={id} key={id}>{choiceLabel(id, qualification)}</option>)}
            </Select></Field>}
          </QueryState>
          {provider && <QualificationNote qualification={statusFor(provider)} />}
          <Field label="Zone name"><Input autoComplete="off" spellCheck={false} placeholder="example.com…" value={zone} onChange={e => setZone(e.target.value)} /></Field>
          <Field label="Certificate credential"><Select value={certificate} onChange={e => setCertificate(e.target.value)}><option value="">Choose certificate credential</option>
            {(credentials.data ?? []).filter(k => k.provider === provider && k.purpose === 'certificates' && !k.disabled).map(k => <option key={k.id} value={k.id}>{k.name}{statusFor(k.provider, k.qualification) === "preview" ? " — " + previewLabel : ""}</option>)}</Select></Field>
          <Field label="Publishing credential"><Select value={publishing} onChange={e => setPublishing(e.target.value)}><option value="">Choose Publishing credential</option>
            {(credentials.data ?? []).filter(k => k.provider === provider && k.purpose === 'publishing' && !k.disabled).map(k => <option key={k.id} value={k.id}>{k.name}{statusFor(k.provider, k.qualification) === "preview" ? " — " + previewLabel : ""}</option>)}</Select></Field>
          <label className="flex items-start gap-2"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />Confirm provider and zone</label>
        </>}
        {(action?.kind === 'publishing-rollback' || action?.kind === 'publishing-vault') && <p className="text-muted">Switch the credential used for future Publishing jobs. This does not change DNS records or Caddy. The legacy token publishes only in the bound zone. Keep the legacy token until you confirm the vault cutover on a trial zone. Ownership checks remain required for both sources.</p>}
        {action?.kind === 'adopt' && <>
          <p className="text-muted">Review the original applied publication in Publishing and the record at Cloudflare. Enter its publication ID. Rynhult requires an exact match to that historical record, address and DNS-only state. Adoption records ownership without changing DNS. Unknown records and existing ledger entries are refused.</p>
          <Field label="Reviewed publication ID"><Input value={publication} onChange={e => setPublication(e.target.value)} autoComplete="off" placeholder="pub-…" /></Field>
        </>}
        {action?.kind === 'apply' && <p className="text-muted">The root helper validates and reloads Caddy using the selected certificate credential. Root policy and a qualified binary release are required. A controlled restart may briefly interrupt streams. Reload does not prove certificate renewal.</p>}
        {action?.kind === 'rollback' && <p className="text-muted">Restore the previous protected credential generation within 24 hours. Confirm the old credential is still active at the provider before proceeding.</p>}
        {action?.kind === 'migrate' && <p className="text-muted">Import both fixed legacy files into separate encrypted Keys entries. Existing entries are never overwritten. Select their zone in Configure DNS, validate and apply before retiring legacy files. The rollback window is 24 hours after successful apply.</p>}
        <Field label="Current password"><Input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} maxLength={1024} /></Field>
        {credentialForm && <details key={provider} className="rounded-md border border-line p-3">
          <summary className="cursor-pointer text-sm font-medium">Create a scoped {providerNames[provider] ?? provider} credential</summary>
          <div className="mt-3"><CredentialHelp provider={provider} /></div>
        </details>}
      </div>
    </Dialog>
  </>;
}
