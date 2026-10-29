# Node task ownership

The main owns connection work, not the processes already dispatched on a machine.
Cancelling an update, package helper or broker wait does not undo a remote change.
Inspect an interrupted machine before retrying. No automatic replay is added here.

This inventory covers explicit `create_task`/`ensure_future` calls in the main,
node-related registry starts, and their structured children. Source symbols below
are in `src/acc/server/` unless stated otherwise. Node-side agent/helper workers are
outside this table. Framework HTTP/WebSocket handler tasks belong to the ASGI
server; their child work is included. New task creation must have an owner in this
inventory, even when the owner is a request rather than a node.

## Lifecycle rules

`node_tasks.NodeTasks` retains tasks until completion, retrieves exceptions and
logs unexpected background failures once. An awaited child uses `report=False`:
its caller handles/logs its result. One failure does not cancel unrelated jobs.
Root main/HA worker and monitor tasks use the same exception observer, so a
worker that exits unexpectedly is not silently consumed by shutdown gather.
Completed tasks are excluded from drain waits, so deferred completion callbacks
cannot make a drain spin without yielding.

Each `nodes.NodeLink` owns its transport and connection-bound work in `link.tasks`.
`NodeHub.start(machine, ...)` adopts jobs into that exact current link. A job
started while offline has a hub-owned scope for that machine; a replacement,
removal or shutdown cancels it too. A newer connection never inherits old work.
The hub retains retired scopes until their drain finishes, rather than losing the
last reference when it removes a link. `disconnect` stops work immediately;
`drain(name)` joins its cleanup. Removal uses both. `broker.close()` closes and
drains every node before dropping cached clients. Shutdown uses close code 1001 (reconnect allowed); removal retains 4401
(enrollment revoked), and replacement retains 4409. The hub rejects new work during
shutdown; an HA promotion explicitly reopens it.

Socket-close cleanup is the only task allowed to start after its work scope has
closed. It gets one second to send the close; the owned receive child is cancelled
as well, so a stalled close cannot strand the connection handler. ASGI owns the
underlying transport teardown. No drain waits for the whole ASGI handler: that
would conflict with HA request/activity fencing during handover.

`joined_thread` retains and shields a worker, then joins it even after repeated
cancellation. Python cannot kill a running thread. Removal/shutdown may wait for
its underlying operation, using any existing helper timeout. If an OS call never
returns, teardown can wait indefinitely. It must not claim completion while that
mutation is still running. A thread can finish an already-started effect during
that wait. Cancellation prevents its caller from starting the next effect.

## Inventory

| Work and creation point | Owner / who cancels and joins | Disconnect, replacement and removal | Main shutdown / failure observation |
|---|---|---|---|
| Node control sender: `NodeLink.__init__ → _pump` | `link.tasks`; `NodeLink.close`, then hub/handler drain | Close fails channels and pending sends, cancels worker; retired scope stays owned | `Brokers.close → NodeHub.close`; expected socket loss closes link, unexpected task failure observed once |
| Priority preview/control writer: `acc.preview_sender.PrioritySender.__init__` | Existing task adopted by `link.tasks`; common node-side instances keep their existing owner | Link closes priority sender and fails queued send futures | Joined with link; sender handles transport failures and wakes its consumers |
| Heartbeat watchdog: `NodeLink.__init__ → _watch` | `link.tasks` | Deadline closes link; replacement/removal cancels watchdog | Joined with link; heartbeat warning is emitted by watchdog |
| Incoming frame wait: `api.nodes.connect → ws.receive` | One `link.tasks` child at a time; handler awaits it | Cancelled on close, unblocking handler even if close-frame send stalls | Joined with link; expected closed-link cancellation is not logged as a server error |
| Socket close: `connect`'s `link.on_close → close_socket` | Link cleanup task; one-second bound; handler and hub can join it | Sends existing replacement/revocation code; timeout cannot leave receive wait alive | Included in hub drain; already-closed sockets are expected |
| Identity follow-up: `connect → machine_followup` | `link.tasks` | Cancelled on every ended connection; no old follow-up migrates to successor | Joined, including its thread operations; existing expected refusal warning retained |
| CSR issue: `connect` machine-csr handler → `machine_issue` | `link.tasks`; existing one-at-a-time/rate gates retained | Cancelled on replacement/removal; signer result cannot be delivered on closed link | Joined through helper completion; signer refusal logged once, no new issuance retry |
| Identity lookup, switched/issue/ready workers: `api.nodes` via `joined_thread` | Structured child of identity work | Cancellation joins actual worker; issuance still rechecks active enrollment before persistence | Worker failure during cancellation logged once; normal exception goes to identity caller |
| Retired-scope drain: `NodeHub._drain_scope → scope.join` | Hub retains the drain task until its completion callback removes it | Replacement/removal retain old scopes until work actually finishes | `NodeHub.drain` joins them; unexpected drain failures retrieved/logged once |
| Approved remote tool execution: `tools.Tools.decide → execute` | Current link/offline machine scope through `hub.start`, also indexed by `Tools.tasks` | Cancelled on disconnect/replacement/removal, including before first execution; approved/applying request records interruption when authority permits | Main/HA joins `Tools.tasks`; unexpected failures observed once; no claim that a dispatched install was undone |
| Local tool execution: `Tools.decide → execute` | `Tools.local_tasks`, indexed by `Tools.tasks` | Unrelated node loss does not cancel local work | Main/HA cancels and joins; restart recovery remains the fallback if fencing prevents an interruption write |
| Git backup round: `backups.BackupMixin.schedule_backups → _backup_round` | Supervisor field `_backup_task`, with registry exception observer | Round survives loss of one machine and proceeds to other projects | `Supervisor.run` finally cancels **and awaits** round; unexpected round failures logged once |
| Remote project push: `_backup_round → backup_project` | Exact link/offline machine scope through `hub.start`; round awaits it | Cancelled on ended node; cancellation of one child does not cancel other machines' round work | Round cancellation propagates and joins child; existing one-push token/client cleanup retained |
| Backup repository/status threads: `_backup_project` via `joined_thread` | Structured child of push or request | Already-started local repository/status mutation is joined, not abandoned | No thread outlives completed backup teardown; existing operation exceptions remain caller-owned |
| Parallel remote spool fetch: `Supervisor.fetch_remote_spools → _fetch_host_spools` | One hub-owned task per host; supervisor awaits budgeted group | Node loss cancels only its fetch; reconnect resets existing host backoff | Parent `finally` cancels and joins all children, including cancellation during `asyncio.wait`; caller logs failures once |
| Main supervisor: `app.lifespan` or `ha_runtime.Runtime.start → Supervisor.run` | App lifespan / HA runtime task list | Per-node children above stop; main loop continues for other machines | Root workers are cancelled/joined even on exceptional lifespan exit; supervisor joins its backup child |
| Host snapshot/container-audit threads: supervisor stages via `joined_thread` | Structured child of supervisor | Local shared stages, not per-connection jobs; unrelated node loss leaves them alone | Shutdown joins actual mutation thread; normal stage handling owns errors |
| Presence, planned downtime, idle-workstation evaluation: supervisor stages | Awaited stages, **no independent task** | Existing current-link/active-node tests govern each stage; old handlers cannot mark a newer link offline | Stops with supervisor; stage errors use existing supervisor logging |
| Updater loop: `app.lifespan` / `Runtime.start → Updates.run` | Main/HA worker task list | Loops current links; `request_node` and `follow` queue messages synchronously, **no per-node update task** | Parent cancelled/joined; existing retry warning retained |
| Update recovery and isolation reporting: `node_update_state`, node handshake/update-state, API projections | Inline request/receive processing, **no independent main task** | Stored recovery remains visible offline; per-account isolation probes run on the node, not this main | No extra task to cancel; receive task and database sessions bound processing |
| Backup target worker: `backup_jobs.BackupJobs.run` | Main/HA worker; one shielded tick thread, already joined on cancellation | Durable local capture/restore/archive jobs are service-owned, not a disconnected node's transport; scheduler checks active node/policy | Existing tick shield joins mutation before worker ends; known errors retried by durable worker |
| Remote access reconcile loop and sweeper: `app.lifespan → RemoteAccess.run → sweeping` | App owns loop; loop finally cancels **and joins** sweeper; retire thread uses `joined_thread` | Sweeper closes revoked/retired machine streams; not killed for one node's loss | Both loop and sweeper joined; existing retry warnings retained; HA public edge remains unqualified |
| HA authority monitor: `Runtime.lifespan → monitor` | Runtime lifespan | Authority loss stops all workers/hub; manual epoch transitions unchanged | Monitor cancelled/joined before runtime stop; no automatic promotion added |
| Publication jobs: `publish.Publisher.approve → execute` | Publisher registry / `Publisher.tasks`; installation-wide DNS/Caddy work | Not a node execution task; not cancelled for unrelated node loss | Both non-HA lifespan and HA stop cancel/join jobs; existing uncertain-effect safeguards retained; unexpected failure observed once |
| Remote preview reservation renewal: `preview_remote.opened → renew` | Exact `link.tasks` and request context | Cancelled immediately with old link, even if browser request has not exited | Request finally also cancels/joins idempotently; normal errors consumed by context |
| Remote HTTP preview upload: `preview.Proxy.remote_http → send_body` | Exact stream's `link.tasks`, plus response/request finally | Node closure cancels an upload blocked on browser body input, not just broker I/O | Response finally joins upload; early failure also joins; no successor-link adoption |
| Preview WebSocket inbound/outbound/recheck: `preview.Proxy.websocket` | Request's task list, `FIRST_COMPLETED`, finally cancel/join | Closed node preview stream ends upstream; sibling bridge tasks are joined | ASGI cancels handler, whose finally joins all three; no detached work |
| Terminal send/receive/login check: `tty.bridge` | Request's task set, finally cancel/join | Closed broker channel wakes bridge; no node-created terminal task survives its request | ASGI owns handler; bridge joins its three children |
| Public edge receive/revocation race: `edge.EdgeGuard.guarded_receive` | Request finally cancels **and joins** message and revocation waiters | Identity sweep wakes request; either race result cancels other waiter | Parent cancellation now joins both children, instead of leaking a blocked receive |
| Short mutation workers: `db.write_in_thread` | Awaiting request/stage; existing shield/join contract | Not detached node work; mutation scope owns cancellation | Existing cancellation joins transaction before returning; failure logged by existing owner |
| Owner machine checks: `api.nodes.check_machines` | Request-owned `asyncio.gather` children, no detached background set | Connection failure reported per machine; no reconnect migration | Request cancellation propagates to gathered calls; broker-client finally closes them |

## Tests and changes

The ownership pass fixed missing joins for link workers, identity work, backup
rounds, spool fan-out, the remote sweeper and edge receive races; adopted remote
jobs and preview children; added non-HA publication shutdown cleanup; and made
normal lifespan cleanup unconditional with `finally`.

`tests/test_node_tasks.py` exercises registry and authenticated reconnect storms,
blocked identity and snapshot threads, removal during work, remote tool/push
cancellation, exceptional shutdown, pending publication, a stalled socket close,
request cancellation and preview renewal before request exit. Failure logging is
checked once without cancelling a sibling. The shared test helpers
`task_snapshot` / `assert_no_task_leaks` compare actual task identities, fail on
new live tasks and clean up leaks after the assertion so failures do not poison
the next test. They also have a deliberately leaking self-test.

This is ownership and cleanup, not rollback, remote process termination, automatic
retry or stronger isolation. Cancelled tool jobs record a failed/interrupted
result when database authority permits; HA fencing can defer that record to the
existing startup recovery path. Existing helper and network deadlines still
bound blocking work; a new timeout policy is not introduced for those effects.
