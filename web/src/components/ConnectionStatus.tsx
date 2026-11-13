import clsx from 'clsx';
import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { ago } from '../lib/format';
import { useAppliedTheme } from '../lib/theme';
import { NEXT_THEME, useThemeChoice, saveTheme } from '../lib/themeChoice';
import { BROKER_DOWN_TITLE, useBrokerDown, useHealth, useOffline, useSessions } from '../lib/queries';
import { Dot } from './ui';

/** System, Light or Dark; System follows the device, including when it changes while Rynhult is open. */
export function useTheme() {
  const choice = useThemeChoice();
  useAppliedTheme(choice);
  const toggle = useCallback(() => saveTheme(NEXT_THEME[choice]), [choice]);
  return { choice, toggle };
}

/** Global freshness: every page polls; say plainly when the data has stopped updating. */
export function LiveIndicator() {
  const health = useHealth();
  const offline = useOffline();
  const brokerDown = useBrokerDown();
  if (health.data?.ha) return <Link to="/ha" className="text-xs text-muted">Active main · epoch {health.data.ha.epoch ?? "unavailable"}</Link>;
  const label = offline ? "Offline, retrying" : brokerDown ? "Sessions unavailable" : "Live";
  const body = <><Dot tone={offline || brokerDown ? "bad" : "ok"} pulse={!offline && !brokerDown} /> {label}</>;
  const className = clsx("hidden items-center gap-1.5 text-xs sm:inline-flex", offline || brokerDown ? "text-bad" : "text-faint");
  // While sessions are unavailable, the indicator links to what to do.
  return brokerDown && !offline
    ? <span role="status" className="hidden sm:inline-flex"><Link to="/guide/User-Guide/Troubleshooting" title={BROKER_DOWN_TITLE} className={clsx(className, "underline underline-offset-2")}>{body}</Link></span>
    : <span role="status" title={offline ? "Rynhult is not answering; showing the last data received" : "Updating every few seconds"} className={className}>{body}</span>;
}

/** On phones the top bar has no room for the Live indicator, so a problem shows as a banner instead: hide a healthy
    indicator when space is short, never an unhealthy one (UX review NAV-4). */
export function ConnectionBanner() {
  const sessions = useSessions();
  const health = useHealth();
  const offline = useOffline();
  const brokerDown = useBrokerDown();
  if (!offline && !brokerDown) return null;
  const updated = Math.max(sessions.dataUpdatedAt, offline ? 0 : health.dataUpdatedAt);
  return (
    <p role="status" className="flex shrink-0 flex-wrap items-center gap-x-1.5 border-b border-bad/30 bg-bad/10 px-4 py-1.5 text-xs text-bad sm:hidden">
      <Dot tone="bad" /> <b className="font-medium">{offline ? "Offline" : "Sessions unavailable"}</b>
      <span title={offline ? "Rynhult is not answering; showing the last data received" : BROKER_DOWN_TITLE}>
        · retrying{updated ? ` · updated ${ago(new Date(updated).toISOString())}` : ""}</span>
      {!offline && <Link to="/guide/User-Guide/Troubleshooting" className="underline underline-offset-2">What to do</Link>}
    </p>
  );
}

