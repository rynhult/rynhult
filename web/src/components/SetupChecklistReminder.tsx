import { Link, Navigate } from 'react-router-dom';
import { useRole } from '../lib/queries';
import { useSetupPreference } from '../lib/setupPreference';
import { STEPS } from '../lib/setupWizard';
export function SetupChecklistReminder() {
  const { username } = useRole(); const query = useSetupPreference(username);
  const progress = query.data?.wizard;
  const unfinished = STEPS.filter(([id]) => id !== 'finish' && progress?.steps[id]?.state !== 'completed');
  if (progress?.recovery_required && progress.steps.recovery?.state !== 'completed') return <Navigate to="/setup/wizard" replace />;
  return <aside aria-label="Setup checklist" className="space-y-2 rounded-lg border border-line p-4">
    <Link className="inline-flex min-h-11 items-center underline" to="/setup/wizard">{progress?.updated_at ? 'Continue setup / Review setup' : 'Review setup'}</Link>
    {progress?.updated_at && unfinished.length > 0 && <p className="text-sm">Unfinished: {unfinished.map(([, title]) => title).join(', ')}. Deferred steps are not ready.</p>}
    {query.isError && <p role="status">Setup progress is unavailable. Open the checklist to retry.</p>}
  </aside>;
}
