# Preview

See a [project](/User-Guide/Glossary#project)'s running web app inside Rynhult while you work with its agent. Previews are private: owners and the project's member can open them. Publishing a site is a separate action.

## Before you start

The project must have a reserved port on its machine. For a project on another machine, that machine must be connected and run an updated Rynhult [node](/User-Guide/Glossary#machine-node). Ask the agent to start its dev server on that port. Rynhult never starts a server when you open Preview. An owner must have configured private preview DNS and certificates (see [Configuration](/Internal/Configuration#preview-network-setup)).

## Open a preview

1. Open a project and select **Preview**. Select a listening port under **Preview port**. The picker only lists that project's reserved ports that are accepting connections.

![A project's Preview tab showing the demo app and port picker](/.attachments/preview-project.png)

2. On a [session](/User-Guide/Glossary#session)'s page, select **Preview** to see the same project app beside its terminal. On smaller screens the terminal sits below the preview. For sessions without an available terminal, Rynhult shows activity beside it.

![A session's Preview tab showing the app beside its terminal](/.attachments/preview-session.png)

The context above the controls names the project and folder. From a session it also names that session's reported branch and [worktree](/User-Guide/Glossary#worktree) folder. These are context, not proof that the selected port runs that worktree: reserved ports are shared across the project.

![Preview identifies the project, session branch and folder without claiming the port is exclusive to that worktree](/.attachments/preview-context.png)

![The same preview context and shared-port warning on a phone](/.attachments/preview-context-phone.png)

3. Select **Reload** after restarting a server, to reload the app, or to renew expired access. **Open in new tab** opens the app separately with the same private access checks.

![The Preview controls wrap on a phone above the app](/.attachments/preview-phone.png)

Projects on another machine use the same controls. Rynhult carries the app through the machine’s existing private connection; no port needs opening on that machine.

![A remote demo project using Preview through its machine connection](/.attachments/preview-remote.png)

You should see the running app. Its own hot reload works through the preview WebSocket connection when supported by the dev server. Preview uses a different hostname from Rynhult; the app cannot read Rynhult's cookies or control its page. A preview is shared by project and port, including sessions in separate worktrees: choose the port running the worktree you want to inspect.

## Troubleshooting

- **Nothing is listening on a reserved port.** Reserve a port in the project's **Ports** tab, then ask the agent to start the app on it. The list refreshes every five seconds. A listening non-web service may appear but cannot render a web page.
- **Preview is available for projects on this machine. Update this machine to enable preview.** Ask an owner to [update that machine](/User-Guide/Nodes#updating-a-node), then reload Preview.
- **host … is not connected.** Restore its connection to Rynhult, then reload Preview. Rynhult connects to the project’s own machine; its dev server must run there.
- **Preview access expired.** Access lasts five minutes. Select **Reload** in Rynhult to renew it. Signing out, losing project access, archiving the project or releasing its port also stops further preview requests.
- **App is unavailable.** Check that its server is still running and listening on the reserved port on loopback or all interfaces.
- **Hostname, certificate or cookie error.** Ask an owner to check private wildcard DNS, the Caddy template and device trust. Prefer a preview domain on the same site as Rynhult; browsers may block iframe cookies for an unrelated domain. With Caddy's own CA, trust its root on the device (see [Settings](/User-Guide/Settings)).
- **Dev server rejects the hostname or hot reload fails.** Add the preview hostname to the dev server's allowed hosts and configure hot reload to use the browser's preview hostname over HTTPS. Absolute localhost URLs in app code bypass the preview and need to use relative URLs instead.

Preview WebSocket messages are limited to 64 KiB on other machines. A dev server that sends larger hot-reload messages may need smaller messages or a full reload. Disconnecting or updating a machine closes its preview connections; reload once it reconnects.

Previews block service workers, upstream cookies, downloads, popups and top-level navigation. Apps that require these browser capabilities may need changes for preview use.

## Send an element to an agent

1. On a project's **Preview** tab, choose a running session under **Send to session**. On a session's own
   **Preview** tab, the destination is already that session. A stopped, external or disconnected session cannot receive selections.
2. Select **Pick element**, then click or tap an element in the app. You can also focus an element and press
   **Enter**. **Escape** or **Cancel picking** ends selection without sending anything.
3. Review **Send element to agent?**. It names the destination and shows the exact message: a structural selector,
   a short text excerpt and a visible bounding box measured in CSS pixels from the preview viewport's top-left corner.
   The message labels the page description as untrusted context. Common secret shapes and the session's vault
   values are redacted, and invisible characters are removed, so nothing is sent that you can't see. Select
   **Send to agent** to submit it, or **Cancel** to discard it. **Send to agent** becomes available a moment after
   the dialog opens, so a double-click or an extra Enter can't send by accident.

![Reviewing the element description and destination before sending](/.attachments/preview-pick-element.png)

The same confirmation works on a phone.

![Reviewing an element selected by touch](/.attachments/preview-pick-phone.png)

Only a selection you confirm is sent. The confirmed message becomes part of the ordinary session conversation and
message audit; the picker audit records the project and port without the excerpt. Picks end after 30 seconds and
confirmation expires after two minutes. Reloading, changing ports or sessions, or leaving the page discards the
selection. If a send fails, check the terminal before picking again: the message might already have arrived.

The selector is a hint about the rendered page, not a source-file location. Page scripts can influence the description,
so review it even when the selected element looks familiar. The picker omits form values, passwords, editable text,
HTML, attributes and URLs; it does not include a screenshot. Cross-origin navigations, nested frames and shadow DOM
contents cannot be inspected. If selection is unavailable, reload the preview and choose an element in the main app.
