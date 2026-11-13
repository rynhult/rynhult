export const ENTERPRISE_NOTE = 'V1 supports small teams. This level does not enable enterprise identity, automatic provisioning, delegated administration or automatic failover.';
export const STEPS = [
  ['recovery', 'Save recovery codes', 'Keep access if you lose your authenticator.'],
  ['level', 'Choose level', 'Choose guidance and normal navigation.'],
  ['security', 'Review protections', 'Review repository, files, secrets, installation, provider prompts and budget protections.'],
  ['machine', 'First machine', 'Without a connected machine and account, sessions cannot start.'],
  ['provider', 'Provider and keys', 'Without a ready provider or effective project key, sessions cannot start.'],
  ['project', 'First project and people', 'You need a project to start a session. People and groups can wait.'],
  ['backups', 'Backups', 'Until a capture succeeds and you save its recovery kit, recovery is unproven.'],
  ['updates', 'Updates', 'Without an update plan, you must check and update manually.'],
  ['features', 'Choose preview features', 'Skipping keeps installation defaults and your existing choices.'],
  ['finish', 'Review and finish', 'Unfinished items remain in your checklist.'],
] as const;
export type WizardStep = typeof STEPS[number][0];
export interface WizardProgress {
  recovery_required: boolean; version: number; preset_version: number; revision: number;
  steps: Partial<Record<WizardStep, { state: 'pending' | 'completed' | 'deferred'; updated_at: number }>>;
  binding: { machine: string; account: string; machine_id?: string | null } | null; project_id: number | null; updated_at: number | null;
}
export interface WizardChange {
  revision: number; step: WizardStep; state: 'pending' | 'completed' | 'deferred';
  binding?: WizardProgress['binding']; project_id?: number | null;
}
