import { postInBackground } from './api';

/** A tab lease expires without reconnect replay; input events never make network requests. */
export function startPushPresence(scope: string) {
  if (scope !== '/inbox' && !/^\/sessions\/[^/]+$/.test(scope)) return;
  const tab = crypto.randomUUID();
  let lastInput = Date.now();
  let sequence = 0;
  const controller = new AbortController();
  const heartbeat = (ending?: unknown) => {
    void postInBackground('/api/push/presence', { tab, sequence: ++sequence, scope,
      idle_ms: Math.max(0, Date.now() - lastInput),
      active: ending !== true && document.visibilityState === 'visible' && document.hasFocus(),
    }).catch(() => undefined);
  };
  for (const event of ['pointerdown', 'keydown', 'wheel']) {
    window.addEventListener(event, () => { lastInput = Date.now(); }, { signal: controller.signal });
  }
  window.addEventListener('visibilitychange', heartbeat, { signal: controller.signal });
  const timer = window.setInterval(heartbeat, 15000);
  heartbeat();
  return () => {
    clearInterval(timer);
    controller.abort();
    heartbeat(true);
  };
}
