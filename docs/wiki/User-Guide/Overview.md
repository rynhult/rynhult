# Overview

> Screenshots use fictional demo accounts and fixed example metrics, not live systems or qualification evidence.
> Some illustrations predate the current frame and need refresh; use the written navigation and controls below.

The home screen answers three questions: what needs me, what is running, and is the machine healthy.

![The overview page](/.attachments/overview.png)

On a phone:

![Earlier phone Overview illustration; current notices use the Inbox rail badge](/.attachments/phone-overview.png)

It reads in that order on a phone: what needs you, the [sessions](/User-Guide/Glossary#session) to answer, then health and machines. On a wide screen the four counts share the top row and machines join the right-hand column.

- **Get started** (until it's done or hidden). Three steps that tick themselves: sign in an agent, add a [project](/User-Guide/Glossary#project), start your first session. See [Getting started](/User-Guide/Getting-Started#get-started).
- **Connect your phone** (until you dismiss it). One line with a link to [Connect your phone](/User-Guide/Connect-Your-Phone); **Dismiss** hides it for you on every browser.
- While a session needs you or a critical [Inbox](/User-Guide/Glossary#inbox) item is open, these two wait below **Live sessions**.
- **Top row.**
  - **Needs you**: how many sessions are blocked on you (waiting for an answer, or needing recovery), and how many are live, working and idle. Their names are in **Live sessions** right below. Select it to see just those sessions. Open Sessions to filter the same priority group.
  - **Inbox**: how many items are open in the [Inbox](/User-Guide/Inbox) (waiting sessions, approvals, alerts and notes), with the first one. Select it to open the Inbox. The Inbox badge in the rail shows the open count.
  - Host CPU and memory (live readings from the host, with how old they are; **Host** shows its own live readings, taken at a different moment), and containers up. On a phone these two follow **Live sessions**.
- **Inbox status.** The rail shows an open-count badge. Select **Inbox** for individual notices, including machine and backup alerts. There is no notices banner. See [Inbox](/User-Guide/Inbox#compact-status-without-blocking-the-page).
- **Live sessions.** Every running session with its machine (including the [main Rynhult](/User-Guide/Glossary#main)), status, agent, what it's doing now and what it has changed ("+44 −0 · 3 files"), grouped into **Needs you**, **Working**, **Unknown** (hook evidence is too old) and **Idle**. Empty groups are hidden. Wide tables have a dedicated **Host** column: select it for ascending/descending machine order within the priority groups. Phones show **· on acc-host** inline, including the main machine. Select a row to open it.
- **Machines** (owners, once other machines are added). Each machine's role and state; an offline always-on machine stands out. **All machines** opens **Host**, where **Check machines now** runs.
- **Plan usage.** How much of each CLI subscription's window is used, as the CLIs report it. Claude and Codex measure usage differently, so Rynhult never adds them up. Copilot shows **See GitHub settings** instead of a quota meter. If you set a [budget](/User-Guide/Budgets) for a window, it shows next to it (amber when you're past it).
- **Projects** and **Storage.** Each project's containers and live sessions, and how full each disk is.

Status colours: amber means waiting for you, red means something failed, green dots mean healthy or working. Grey means idle or stopped.

![Live sessions name both the main and remote machines](/.attachments/overview-machines.png)

![Inline machine names remain visible in phone Live sessions, even with long session names](/.attachments/overview-machines-phone.png)

Host readings refresh without writing Inbox alerts. The supervisor evaluates host alerts on its regular polling cycle, so an alert can appear after the live reading changes. If an explicit provider, model, account or GitHub refresh meets a busy database, Rynhult shows the last cached result; refresh again to retry.

The owner status board includes machine roles and availability. **Check machines now**, on **Host → Machines**, checks live links and reports offline machines from recorded presence; Rynhult cannot dial them. Always-on offline alerts appear in Inbox and machine warning state. Workstations stay neutral. See [machine roles](/User-Guide/Nodes#roles-and-offline-machines).

### Review filters

Use **Review state** to show sessions ready for review, and **Pull request state**
to filter linked GitHub pull requests. The filters combine. Ready for review has
its own session group. Members see their own sessions; owner access is unchanged.

Pull request states come from the existing GitHub snapshot schedule. They are
cached for a minute during background refreshes and wait when GitHub reports a
rate limit. The project page’s Refresh asks for a new reading once, bypassing
that minute of caching while still respecting GitHub’s rate limit. Explicit
refreshes share a 15-second cooldown per repository across all viewers. Recent
pull requests (up to 20 per repository, plus session branches outside that list)
include merged and closed requests. The reader uses the existing token and scopes, so projects without an eligible GitHub
snapshot have no lifecycle label. Opening Overview makes no GitHub request.
