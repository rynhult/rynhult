import type { Mode } from '../components/shell/navigation';

export type SetupLevel = 0 | 1 | 2 | 3 | 4 | 5 | 6;
// Recommendations are display data. They are never passed to a policy or launcher.
export const SECURITY_DEFAULTS = {
  force_push: true, push_default: true, merge_pr: true, rm_outside: true, secrets: true,
  custom_rules: true, allowed_folders: true, tool_install_check: true, provider_prompts: true,
  budget_checks: true, risk_labels: true,
};
export const LEVELS: { id: SetupLevel; name: string; description: string; team: string }[] = [
  { id: 0, name: 'Expert', description: 'Show everything. Keep the setup short.', team: 'Choose a lead, optional specialist roles and an explicit helper ceiling before launching.' },
  { id: 1, name: 'Hobbyist', description: 'Start a project. Ask an agent to help. We’ll guide you.', team: 'One ready agent on your chosen machine. Add optional helpers later.' },
  { id: 2, name: 'Solo developer', description: 'Keep several projects moving, with the tools close by.', team: 'One ready lead with optional review and specialist roles. Review each launch’s limits.' },
  { id: 3, name: 'Small team', description: 'Give each person their own agents, and work together.', team: 'Personal leads and optional shared role suggestions. Each person keeps their own sign-ins and grants.' },
  { id: 4, name: 'Growing company', description: 'Use the small-team flow. Enterprise provisioning is unavailable.', team: 'Personal leads with explicit machine placement and reviewed helper limits.' },
  { id: 5, name: 'Organization', description: 'Use the small-team flow. Enterprise identity is unavailable.', team: 'Personal leads with existing group and machine grants. Identity provisioning is Later.' },
  { id: 6, name: 'Enterprise', description: 'Use the small-team flow. Delegated administration and automatic failover are unavailable.', team: 'Personal leads with reviewed operational roles and limits. Automatic failover is unavailable.' },
];
const minimum: Record<string, number> = {
  '/grid': 2, '/workspaces': 2, '/schedules': 2, '/publishing': 2, '/catalog': 2,
  '/board': 3, '/audit': 3, '/groups': 3,
};
export function pathVisible(level: SetupLevel | null, path: string): boolean {
  return level === null || level === 0 || level >= (minimum[path] ?? 1);
}
export function modeVisible(level: SetupLevel | null, mode: Mode): boolean {
  return pathVisible(level, `/${mode}`);
}
