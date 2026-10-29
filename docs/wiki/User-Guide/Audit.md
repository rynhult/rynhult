# Audit

> Screenshots use fictional demo accounts and fixed example metrics, not live systems or qualification evidence.

The owner can **Search audit log** across the person (actor), action name, target and stored details. Search runs on the server after a short pause while typing, combines with Type, Person and Since, and treats punctuation literally. The page shows how many entries it received before repeated terminal reads are folded; it does not count the entire matching history. **More available** means there is another page. **Previous** and **Next** use stable entry cursors, 100 entries at a time; newly arriving events do not shift forward pages. Changing a filter returns to the newest matching entries. Search is limited to 256 characters and has a database time/work budget. If it reaches that limit, narrow Type, Person or Since and retry; an interrupted search does not claim there are no matches. Access remains owner-only.

Every change made through Rynhult is recorded: who, what and when. That covers sign-ins, [sessions](/User-Guide/Glossary#session) created, stopped and renamed, trust, publishing decisions, catalog changes, repository creation and promotions.

![The audit log](/.attachments/audit.png)

Each entry reads as a sentence, for example *alex opened api-refactor's terminal to type* or *alex trusted demo-api*. Hover an entry for its exact action name and details. Just looking at a session opens its terminal, so those entries fold into one per person, session and day (*· 4 times that day*); so do text snapshots of a terminal (*alex read api-refactor's terminal output as text*). Secrets are never recorded.

Filter by **Type** (Sessions, Terminals, [Projects](/User-Guide/Glossary#project), Publishing, Sign-in…), by **Person** (or **Rynhult (automatic)** for what Rynhult did by itself) and from a day with **Since**.

For [Orchestrator](/User-Guide/Orchestrator) teams, choose the type **Orchestrator teams** to see `team.grant`, `team.start`, `team.message`, `team.merge`, `team.stop`, `team.finish` and `team.expired`. Agent decisions identify the calling session; grants identify the person who gave the role.

For [Multi-run](/User-Guide/Sessions#multi-run), choose **Several agents**:
`multirun.start` records the chosen launch combinations (not the task text);
`multirun.keep` records the winner and whether archiving the others was requested.
Each run also has its normal launch, dangerous-mode confirmation and lifecycle
audit entries.

Local Git identity saves use `git.identity.update`, targeting the person's account
without copying their name or email into the log. Push subscription creation uses
`push.subscribe`; key rotations, ownership changes and failure-counter resets use
`push.update`. Identical re-subscriptions add no event. Push events target the
device's numeric id; update details say whether ownership or keys changed, without
recording endpoints or keys. The change and its audit entry commit together.
