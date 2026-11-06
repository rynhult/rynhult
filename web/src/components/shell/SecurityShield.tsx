import { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ShieldAlert, ShieldCheck, ShieldOff, ShieldQuestionMark } from 'lucide-react';
import { useOffline } from '../../lib/queries';
import { securityLink } from '../../lib/security';
import { CurrentSecurityBadge, securityBadgeWords, type CurrentSecurityStatus } from '../SecurityBadge';

/** Reuses the badge's owner-only scope/queries; only its shell presentation differs. */
export function CurrentSecurityShield() {
  return <CurrentSecurityBadge render={status => <SecurityShield {...status} />} />;
}

function SecurityShield({ policy, label, scope, notices, loading, unavailable, offline: machineOffline }: CurrentSecurityStatus) {
  const offline = useOffline();
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  const trigger = useRef<HTMLAnchorElement>(null);
  const id = useId();
  const pending = policy?.enforcement?.allowed_folders === 'pending';
  const unrestricted = policy?.mode === 'unrestricted' && !pending;
  const warning = pending || !!policy?.varying_switches?.length || Object.values(policy?.switches ?? {}).some(value => value === false);
  const unknown = offline || unavailable || loading || !policy;
  const state = offline ? 'offline' : unavailable ? 'unavailable' : loading || !policy ? 'loading' : unrestricted ? 'unrestricted' : warning ? 'custom' : 'on';
  const status = offline ? 'Offline' : unavailable ? 'Security state unavailable' : 'Security state loading';
  const words = policy ? securityBadgeWords(policy) : '';
  const text = `${unknown ? `${status}${words ? ` · last known ${words}` : ''}` : words} · ${label}${machineOffline && !offline ? ' · machine offline' : ''}`;
  const count = policy?.older_sessions?.length ?? 0;
  const Icon = offline ? ShieldOff : unknown ? ShieldQuestionMark : warning || unrestricted ? ShieldAlert : ShieldCheck;
  const show = () => {
    const rect = trigger.current?.getBoundingClientRect();
    if (rect) setPosition({ top: rect.bottom, left: Math.max(12, Math.min(rect.right - 320, window.innerWidth - 332)) });
  };
  useEffect(() => {
    const close = () => setPosition(null);
    window.addEventListener('resize', close);
    return () => window.removeEventListener('resize', close);
  }, []);
  return <div className="shell-security" onMouseEnter={show}
    onMouseLeave={event => { if (!event.currentTarget.contains(document.activeElement)) setPosition(null); }}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setPosition(null); }}
    onKeyDown={event => { if (event.key === 'Escape' && position) { event.preventDefault(); event.stopPropagation(); setPosition(null); } }}>
    <Link ref={trigger} to={securityLink(scope)} className="shell-security-trigger" data-state={state}
      data-tone={unrestricted ? 'unrestricted' : warning || unknown ? 'warning' : 'neutral'}
      aria-label={`Security: ${text}`} aria-describedby={position ? id : undefined}
      onFocus={show} onClick={() => setPosition(null)}>
      <Icon size={20} aria-hidden="true" />
      {count > 0 && <span className="shell-security-count" aria-hidden="true">{count > 99 ? '99+' : count}</span>}
    </Link>
    {position && <div id={id} role="tooltip" className="shell-security-tooltip" style={{ ...position, maxHeight: `calc(100dvh - ${position.top + 12}px)` }}>
      <div><p>{text}</p>{notices.map(notice => <p key={notice}>{notice}</p>)}<p className="text-muted">Open Security settings.</p></div>
    </div>}
  </div>;
}
