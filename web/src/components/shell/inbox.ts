import type { AttentionItem } from '../../lib/types';

export const categories = [
  { key: 'decisions', label: 'Decisions' }, { key: 'approvals', label: 'Approvals' },
  { key: 'blocked', label: 'Blocked work' }, { key: 'failures', label: 'Failures' },
  { key: 'information', label: 'Information' },
] as const;
export type InboxCategory = typeof categories[number]['key'];
export function categoryOf(item: AttentionItem): InboxCategory {
  if (['publish_request', 'tool_request'].includes(item.kind)) return 'approvals';
  if (['needs_input', 'team_worklist', 'scope_change', 'image_check'].includes(item.kind)) return 'decisions';
  if (['guardrail_blocked', 'machine_offline', 'replacement_held', 'team_expired', 'team_lifetime',
    'recovery_needed', 'project_autostart_held', 'auth', 'quota', 'group_conflict', 'overlap', 'budget'].includes(item.kind)) return 'blocked';
  if (item.kind.endsWith('_failed') || ['stop_failure', 'spool_parse_error', 'remote_spool_full'].includes(item.kind)
    || item.persistent === 'backup_failed' || item.severity === 'critical') return 'failures';
  return item.severity === 'warn' ? 'blocked' : 'information';
}
/** Fixed application destinations only. Agent wording is neither an ID nor a URL. */
export function relatedWork(item: AttentionItem): string | null {
  if (item.session_id) {
    const section = item.kind === 'team_worklist' ? '?tab=team#worklist'
      : ['team_expired', 'team_lifetime'].includes(item.kind) ? '?tab=team#overview' : '';
    return `/sessions/${encodeURIComponent(item.session_id)}${section}`;
  }
  if (item.kind === 'machine_offline') return item.host ? `/host?machine=${encodeURIComponent(item.host)}` : '/host';
  if (item.kind === 'backup') return '/host#host-backups';
  if (item.kind === 'phone_setup') return '/guide/User-Guide/Connect-Your-Phone';
  if (item.kind === 'auth') return '/providers';
  if (item.kind.startsWith('telemetry_')) return '/settings#telemetry';
  // Approvals without a session are reviewed here, against their original operation ID.
  return null;
}
export function returnPath(value: unknown): string | null {
  return typeof value === 'string' && /^\/(?!\/)/.test(value) && !/^\/inbox(?:[/?#]|$)/.test(value)
    && !/[\\\u0000-\u001f]/.test(value) ? value : null;
}
