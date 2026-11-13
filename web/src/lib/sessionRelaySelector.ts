import type { SelectorResult, SelectorState } from './sessionRelayTypes';
const blockers = ['active_turn', 'background_tool', 'pending_decision', 'login_or_trust_prompt', 'terminal_draft', 'queued_send', 'uncertain_operation'];
/** Deliberate selections only. Mount, default and URL restoration must never call this to switch. */
export function selectRelayView(s: SelectorState): SelectorResult {
  const result = (action: SelectorResult['action'], reason: string | null = null): SelectorResult => ({ action, reason });
  if (!['chat', 'terminal'].includes(s.owner) || !['chat', 'terminal'].includes(s.target)) return result('refused', 'unknown_state');
  if (s.shared_live || s.target === s.owner) return result('view_only');
  if (!s.direction_qualified) return result('view_only', 'direction_unqualified');
  if (!s.may_control) return result('view_only', 'not_controller');
  if (s.switch_state === 'recovery_required') return result('refused', 'recovery_required');
  if (!['owned', 'prepared', 'draining', 'stopping', 'starting'].includes(s.switch_state)) return result('refused', 'unknown_state');
  if (s.switch_state !== 'owned') return result('pending');
  if (s.blockers.some(b => !blockers.includes(b))) return result('refused', 'unknown_state');
  if (s.blockers.length) return result('refused', [...s.blockers].sort()[0]);
  return s.idle_verified ? result('switch') : result('refused', 'unknown_state');
}
export function relayEnabled(features: Record<string, boolean> | undefined, optedIn?: boolean) {
  return features?.session_relay === true && optedIn === true;
}
