# FAQ

**Does closing the browser stop my [session](/User-Guide/Glossary#session)?**
No. Sessions run in tmux on the host. Close the tab, reboot your laptop or lose the network: the session keeps going.

**What happens when the host reboots?**
Rynhult resumes every session that was running, in the same conversation. If a conversation can't be resumed it says so (**Recovery needed**). It never starts a new one in its place.

**Does Rynhult answer prompts for me?**
Never. Dismissing an inbox notice only clears it, and trust is recorded in the CLIs' own settings, never typed.

**Can agents publish sites or change DNS?**
Only by asking. You approve every request.

**Why is there no combined usage number?**
Claude and Codex measure plan usage differently; adding them up would be misleading.

**Does Rynhult send usage data or crash reports anywhere?**
Only if an owner explicitly opts in. Both choices are off by default. Usage contains fixed values and rough
counts; each scrubbed bug report requires approval. Crashed sessions contain metadata only. Preview the exact
payload and inspect the local log in [Usage and bug reports](/User-Guide/Usage-and-Bug-Reports). The default
owner-hosted collector is a pre-release staging service; what it receives before version 1 is deleted at launch. No third-party analytics are used.

**Where is my data?**
On your host: Rynhult's database and secrets under `/srv/acc/data`, [projects](/User-Guide/Glossary#project) under `/srv/acc/workspaces`, wikis and reports in each repository.

**Can Rynhult tell me when a session needs me?**
Yes: turn on push notifications on your phone or computer (see [Notifications and app](/User-Guide/Notifications-and-App)).

## Can the status board run on a display without signing in?

Not yet. The [Status board](/User-Guide/Status-Board) needs an owner's normal sign-in, so a wall display is signed in
like any other browser. A display link that works without signing in is a proposal with open questions about what it
would expose and how it would be revoked.

## How do I report a security problem?

Report it privately, using the contact in the repository's
[security policy](https://github.com/rynhult/rynhult/security/policy).
Do not put passwords, session transcripts, network details or exploit instructions in a
public issue.
