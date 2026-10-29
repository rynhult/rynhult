import { CopyBlock } from "./InstallSteps";
import { useQuery } from "@tanstack/react-query";
import { Download, ShieldCheck } from "lucide-react";
import { api } from "../lib/api";
import { Badge, Card, CardHeader, ErrorNote, Meta, Skeleton } from "./ui";

interface CertificateInfo {
  kind: "public" | "caddy" | "file" | "unverified"; primary: string; file?: string; subject?: string; issuer?: string;
  fingerprint?: string; not_before?: string; not_after?: string;
}

const KIND: Record<CertificateInfo["kind"], string> = {
  public: "A public certificate: browsers and new machines trust it as it is, so there's nothing to install.",
  caddy: "Caddy's own certificate authority signs Rynhult's certificate (its root was copied when Rynhult was installed).",
  file: "A private certificate authority, from the file in ACC_NODE_CA_FILE.",
  unverified: "Not publicly trusted, and no root is configured: set ACC_NODE_CA_FILE to the root that signs it (Guide → Install).",
};
const LABEL: Record<CertificateInfo["kind"], string> = {
  public: "Public", caddy: "Caddy's own CA", file: "Private CA (file)", unverified: "Not verified",
};

const download = "inline-flex h-8 pointer-coarse:h-11 items-center gap-1.5 rounded-md border border-line-strong bg-panel-2 px-3 text-[13px] font-medium text-fg transition-colors hover:bg-panel-3";

/** How each kind of device comes to trust the root: short, the labels as each system shows them. */
function Steps() {
  return (
    <div className="space-y-2 text-[13px] text-muted">
      <p><b className="text-fg">iPhone or iPad:</b> open the profile in Safari → Settings → <b className="text-fg">Profile Downloaded</b> → Install. Then General → About → Certificate Trust Settings and turn on the Rynhult root.</p>
      <p><b className="text-fg">Mac:</b> open the profile → System Settings → General → <b className="text-fg">Device Management</b> (Profiles on older macOS) → Rynhult → Install.</p>
      <p><b className="text-fg">Windows:</b> open rynhult-root.crt → Install Certificate → Current User → Place all certificates in <b className="text-fg">Trusted Root Certification Authorities</b> → Finish.</p>
      <p><b className="text-fg">Android:</b> Settings → Security → Encryption &amp; credentials → Install a certificate → <b className="text-fg">CA certificate</b> → rynhult-root.crt (the menu names vary by phone).</p>
      <p><b className="text-fg">Linux:</b> run from the directory containing the downloaded rynhult-root.crt:</p><CopyBlock text="sudo cp rynhult-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates" label="Copy Linux trust command" /><p>Firefox keeps its own list: Settings → Certificates → Import.</p>
      <p className="text-xs text-faint">Restart the browser afterwards. Push notifications and installing Rynhult as an app need a trusted certificate.</p>
    </div>
  );
}

function Downloads() {
  return (
    <div className="flex flex-wrap gap-2">
      <a className={download} href="/api/certificate/rynhult-root.crt" download><Download size={14} aria-hidden="true" />rynhult-root.crt</a>
      <a className={download} href="/api/certificate/rynhult-root.mobileconfig" download><Download size={14} aria-hidden="true" />rynhult-root.mobileconfig</a>
    </div>
  );
}

const day = (iso?: string) => iso ? new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "–";

/** Settings → Certificate. Owners see which certificate Rynhult uses; everyone can download a private root. */
export function Certificate({ owner, privateCa }: { owner: boolean; privateCa: boolean }) {
  const info = useQuery({ queryKey: ["certificate"], queryFn: () => api<CertificateInfo>("/api/certificate"), enabled: owner });
  if (!owner) {
    if (!privateCa) return null;
    return (
      <Card>
        <CardHeader title="Certificate" icon={ShieldCheck} subtitle="Rynhult uses a private certificate: add its root to each device you open Rynhult on" />
        <div className="space-y-4 p-4"><Downloads /><Steps /></div>
      </Card>
    );
  }
  const c = info.data;
  const soon = c?.not_after && new Date(c.not_after).getTime() - Date.now() < 90 * 86400e3;
  return (
    <Card>
      <CardHeader title="Certificate" icon={ShieldCheck} subtitle="The certificate Rynhult is served with, and how devices and new machines come to trust it" />
      {!c ? <div className="p-4">{info.error ? <ErrorNote error={info.error} /> : <Skeleton className="h-20" />}</div> : (
        <div className="space-y-4 p-4">
          <div className="space-y-2 text-[13px]">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={c.kind === "unverified" ? "bad" : "neutral"}>{LABEL[c.kind]}</Badge>
              <span className="text-muted">{KIND[c.kind]}</span>
            </div>
            {c.fingerprint && (
              <Meta items={[
                ["Root", c.subject ?? "–"],
                ["Valid until", <span key="until" className={soon ? "text-wait" : undefined}>{day(c.not_after)}</span>],
                ["File", <code key="file" className="font-mono text-[12px]">{c.file}</code>],
              ]} />
            )}
            {c.fingerprint && (
              <div>
                <div className="mb-1 text-xs text-muted">SHA-256 fingerprint (new machines check it: Host → Machines → Add machine)</div>
                <CopyBlock text={c.fingerprint} label="Copy the fingerprint" />
              </div>
            )}
          </div>
          {c.fingerprint && <><Downloads /><Steps /></>}
        </div>
      )}
    </Card>
  );
}
