import { Card, Page, PageHeader, StaleNote, Note } from '../components/ui';

import type { HAHealth } from "../lib/types";
import { Link } from "react-router-dom";

export function HighAvailability({ status, error }: { status: HAHealth; error?: unknown }) {
  return <main id="main-content" className="h-full overflow-auto">
    <Page>
      <PageHeader title="High availability" description="Manual active-passive control" />
      {error ? <StaleNote error={error} /> : null}
      {status.uncertain && <Note role="status">A command outcome is uncertain. The active main keeps working, but clean handover is blocked: an owner must resolve the external work and independently fence the current main before recovery promotion.</Note>}
      <Card>
        <dl className="grid gap-4 p-4 sm:grid-cols-3">
          <div><dt className="text-xs text-muted">This main</dt><dd className="mt-1 text-sm">{status.role === 'active' ? 'Active main' : 'Standby · read-only'}</dd></div>
          <div><dt className="text-xs text-muted">Authority epoch</dt><dd className="tnum mt-1 text-sm">{status.epoch ?? 'Unavailable'}</dd></div>
          <div><dt className="text-xs text-muted">Database</dt><dd className="mt-1 text-sm">{status.database === 'ok' ? 'Connected' : 'Unavailable · actions stopped'}</dd></div>
        </dl>
      </Card>
      <Card><div className="space-y-2 p-4 text-sm" role="status">
        <p>Would promote now: {status.automatic_evaluation?.would_promote ? 'Yes' : 'No'}</p>
        <p className="text-muted">{!status.automatic_evaluation
          ? 'Evaluation unavailable · refresh status or ask the installation owner.'
          : status.automatic_evaluation.evaluation_enabled
            ? 'Telemetry unavailable · witness and replication measurements are not connected.'
            : 'Disabled · automatic failover evaluation is off.'}</p>
        <p className="text-muted">Automatic promotion is not connected. Use the owner’s manual failover runbook.</p>
      </div></Card>
      <Card><div className="space-y-2 p-4 text-sm">
        <p>Manual promotion only</p>
        {status.role === "active" && <p><Link to="/">Open dashboard</Link></p>}
        <p className="text-muted">An owner must demote and drain the active main before promoting a standby. Standby starts no supervisor and dispatches no broker or publishing commands.</p>
        <p className="text-muted">Use the owner’s failover runbook and authenticated HA API to change authority. A missed heartbeat never promotes a main.</p>
        <p className="text-muted">Status refreshes every 5 seconds. Workload views are available on the active main.</p>
      </div></Card>
    </Page>
  </main>;
}
