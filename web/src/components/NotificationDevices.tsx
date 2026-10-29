// Notification devices (UX declutter W5): the push subscriptions that receive alerts. A separate registry from Access
// devices (components/Devices.tsx): removing one stops alerts there and never revokes that device's access.
import { useSingleMutation as useMutation } from "../lib/mutations";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BellOff, BellRing, Check, ChevronRight, Smartphone, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, post } from "../lib/api";
import { ago } from "../lib/format";
import { Badge, Button, Card, CardHeader, Empty, ErrorNote, IconButton, LoadingRows, QueryState } from "./ui";
import { installedApp } from "../lib/app";

interface Device { id: number; label: string; endpoint: string; created_at: string; last_ok_at: string | null; failures: number }
interface PushState { public_key: string; devices: Device[] }

const toKey = (b64: string) => Uint8Array.from(atob(b64.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(b64.length / 4) * 4, "=")), (c) => c.charCodeAt(0));
const isIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent);
const installed = installedApp;
const supported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const deviceName = () => {
  const ua = navigator.userAgent;
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "Linux";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) && !/iPhone|iPad/.test(ua) ? "Chrome" : "Safari";
  return `${os} · ${browser}${installed() ? " (app)" : ""}`;
};

function ThisDevice({ state }: { state: PushState }) {
  const queryClient = useQueryClient();
  const [endpoint, setEndpoint] = useState<string | null>(null);
  const [permission, setPermission] = useState(typeof Notification !== "undefined" ? Notification.permission : "denied");
  useEffect(() => {
    if (!supported()) return;
    navigator.serviceWorker.ready.then((reg) => reg.pushManager.getSubscription()).then((sub) => setEndpoint(sub?.endpoint ?? null)).catch(() => undefined);
  }, []);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["push"] });
  const enable = useMutation({
    mutationFn: async () => {
      const result = await Notification.requestPermission();  // must follow a tap (iOS requires it)
      setPermission(result);
      if (result !== "granted") throw new Error("Notifications are blocked for Rynhult. Allow them in this browser's or the phone's settings, then try again.");
      const reg = await navigator.serviceWorker.ready;
      const sub = (await reg.pushManager.getSubscription()) ??
        await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: toKey(state.public_key) });
      const json = sub.toJSON() as { endpoint: string; keys: { p256dh: string; auth: string } };
      await post("/api/push/subscribe", { endpoint: json.endpoint, keys: json.keys, label: deviceName() });
      setEndpoint(json.endpoint);
    },
    onSuccess: refresh,
  });
  const disable = useMutation({
    mutationFn: async () => {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) { await post("/api/push/unsubscribe", { endpoint: sub.endpoint }); await sub.unsubscribe(); }
      setEndpoint(null);
    },
    onSuccess: refresh,
  });
  const test = useMutation({ mutationFn: () => post<{ delivered: number }>("/api/push/test", endpoint ? { endpoint } : {}) });
  const on = !!endpoint && state.devices.some((d) => d.endpoint === endpoint);

  if (!supported() || (isIOS() && !installed())) {
    return (
      <div className="space-y-3 p-4 text-[13px]">
        <p className="text-fg">{isIOS() ? "On iPhone and iPad, notifications work once Rynhult is on your Home Screen:" : "This browser can't receive push notifications."}</p>
        {isIOS() && (
          <ol className="list-decimal space-y-1.5 pl-5 text-muted">
            <li>In Safari, tap the <b className="text-fg">Share</b> button.</li>
            <li>Choose <b className="text-fg">Add to Home Screen</b>, then <b className="text-fg">Add</b>.</li>
            <li>Open <b className="text-fg">Rynhult</b> from the Home Screen and come back to <b className="text-fg">Settings</b>.</li>
            <li>Tap <b className="text-fg">Turn on notifications</b> and allow them.</li>
          </ol>
        )}
        <p className="text-xs text-faint">Needs iOS 16.4 or later. Rynhult opens full-screen like an app once it's on the Home Screen.</p>
      </div>
    );
  }
  return (
    <div className="space-y-3 p-4">
      <div className="flex flex-wrap items-center gap-3">
        {on ? <Badge tone="ok"><Check size={11} aria-hidden="true" /> On for this device</Badge>
          : permission === "denied" ? <Badge tone="bad">Blocked in browser settings</Badge> : <Badge>Off for this device</Badge>}
        <span className="text-xs text-muted">{deviceName()}</span>
      </div>
      <div className="flex flex-wrap gap-2">
        {on ? <>
          <Button onClick={() => test.mutate()} disabled={test.isPending}><BellRing size={14} aria-hidden="true" /> Send a test</Button>
          <Button variant="ghost" onClick={() => disable.mutate()} disabled={disable.isPending}><BellOff size={14} aria-hidden="true" /> Turn off on this device</Button>
        </> : (
          <Button variant="primary" onClick={() => enable.mutate()} disabled={enable.isPending || permission === "denied"}>
            <BellRing size={14} aria-hidden="true" /> {enable.isPending ? "Turning on…" : "Turn on notifications"}
          </Button>
        )}
      </div>
      {test.isSuccess && <p className="text-xs text-ok">Sent to the push service. System notifications stay quiet while Rynhult is visible.</p>}
      <ErrorNote error={enable.error ?? disable.error ?? test.error} />
    </div>
  );
}

const usePush = () => useQuery({ queryKey: ["push"], queryFn: () => api<PushState>("/api/push") });

/** This browser's own subscription: turn alerts on or off here, and send a test. */
export function NotificationSettings() {
  const push = usePush();
  return <Card>
    <CardHeader title="Notifications" icon={BellRing}
      subtitle="Push alerts when a session needs you, a login is expiring, CI fails, an agent asks to publish, or the host has a problem" />
    <QueryState query={push} loading={<LoadingRows rows={1} />} errorTitle="Couldn't read this device's notification settings.">
      {(data) => <ThisDevice state={data} />}
    </QueryState>
  </Card>;
}

/** Every device that receives alerts, with removal; the app-install steps stay one disclosure away. */
export function NotificationDevices() {
  const queryClient = useQueryClient();
  const push = usePush();
  const remove = useMutation({
    mutationFn: (id: number) => post(`/api/push/devices/${id}/remove`),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["push"] }),
  });
  return <>
    <Card>
      <CardHeader title="Notification devices" icon={Smartphone} subtitle="Receive alerts; removing one does not revoke access" />
      {remove.error ? <div className="px-4 pt-3"><ErrorNote error={remove.error} title="Couldn't remove that device." /></div> : null}
      {/* Loading, failed and empty are different (UX review UI-2): "No devices yet" only once the list arrived empty. */}
      <QueryState query={push} loading={<LoadingRows rows={2} />} errorTitle="Couldn't read the device list." isEmpty={(data) => !data.devices.length}
        empty={<Empty icon={Smartphone}>No notification devices yet. <Link to="#notifications" className="text-accent underline underline-offset-2">Turn on notifications</Link> on each device you want alerts on.</Empty>}>
        {(data) => (
        <div className="divide-y divide-line">
          {data.devices.map((d) => (
            <div key={d.id} className="flex items-center gap-3 px-4 py-3 text-[13px]">
              <Smartphone size={15} className="text-faint" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-fg">{d.label}</div>
                <div className="text-xs text-muted">Added {ago(d.created_at)}{d.last_ok_at ? ` · last delivered ${ago(d.last_ok_at)}` : " · nothing delivered yet"}
                  {d.failures > 0 && <span className="text-wait"> · {d.failures} failed</span>}</div>
              </div>
              <IconButton icon={Trash2} label={`Remove ${d.label}`} disabled={remove.isPending} onClick={() => remove.mutate(d.id)} />
            </div>
          ))}
        </div>
        )}
      </QueryState>
      <p className="border-t border-line px-4 py-3 text-xs text-muted">To stop a device signing in, revoke it under <a href="#access-devices" className="text-accent underline underline-offset-2">Access devices</a>.</p>
    </Card>
    <Card>
      <details className="group">
        <summary className="flex min-h-10 cursor-pointer list-none items-center gap-2 px-4 py-3 text-[13px] font-medium text-fg">
          <ChevronRight size={14} className="text-faint transition-transform group-open:rotate-90" aria-hidden="true" />
          Install Rynhult as an app
        </summary>
        <div className="space-y-2 px-4 pb-4 text-[13px] text-muted">
          <p><b className="text-fg">iPhone or iPad:</b> Safari → Share → Add to Home Screen.</p>
          <p><b className="text-fg">Android:</b> Chrome → menu → Install app (or Add to Home screen).</p>
          <p><b className="text-fg">Mac or PC:</b> Chrome or Edge → the install icon in the address bar; Safari on macOS → File → Add to Dock.</p>
        </div>
      </details>
      <p className="border-t border-line px-4 py-3 text-xs text-faint">Messages are encrypted for each device; Apple, Google or Mozilla relay them without being able to read them.</p>
    </Card>
  </>;
}
