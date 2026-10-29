# Budgets

Budgets are optional limits on your own plan usage. **Nobody has one until they set it**, and Rynhult never sets or applies one for you.

They're under **Settings → Budgets**, one line per usage window Rynhult reads: Claude's 5-hour and 7-day windows, Codex's window, and Copilot's monthly AI credits. Each shows how much is used and when it resets, then **No budget** with **Set a budget**.

## Kinds
- **Cap at N%.** Warn once the window passes N% (for example 80% of the 7-day window).
- **Pace.** N% spread evenly over the window: Rynhult warns when usage runs ahead of the time gone by. With 100%, half way through a week you'd be "over" past 50%. It's useful for making a weekly window last the week.

## At the limit
- **Notify me** (the default). An item in your [Inbox](/User-Guide/Glossary#inbox) (and a push, if notifications are on) saying which window is past your budget. It clears by itself once usage is back under, or the window resets.
- **Notify and pause new launches.** Also holds back new [sessions](/User-Guide/Glossary#session) of that agent: New session shows a warning, and **Launch** waits until you tick *Launch this one anyway*. Running sessions are never stopped, and other agents aren't affected.

When a window resets, its old reading no longer counts: the pause ends and the Inbox item clears, even before Rynhult has a fresh reading. Until one arrives, the window shows *Reset: waiting for a new reading*, and your budget applies again from the next reading.

When you're over, **+5% until it resets** raises that window's budget a little until the window resets (select it again for more). **Remove** takes the budget away entirely. While one change is being saved, the other budget actions wait for it.

The Overview's **Plan usage** card shows your budget next to each window (amber when you're over); select it to go to Settings.

## Who can use them
Each person's budgets are their own; nobody else's affect you. Rynhult reads plan usage from the owner's [agent account](/User-Guide/Glossary#agent-account), so for now only owners see usage windows and can set budgets. If an owner becomes a member (or is removed), their budgets are deleted, and their budget Inbox items end.
