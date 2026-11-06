# Read-only Chat

Chat is another way to **read the same session's conversation**. It does not start another agent or conversation. There is no composer, Send button, upload or model control in this release. Continue working and answer permission questions in **Terminal**.

## Open Chat

Chat is off by default. An owner can enable **Chat** in **Settings → Admin features**. While it is off, linked Chat views show the disabled notice without opening a terminal.

When the account service reports a qualified readable source, the session page offers a **Chat** tab next to **Terminal**. The current readers qualify specific Claude Code and Codex formats; other providers or CLI versions may be unavailable. Rynhult checks the source rather than assuming support from an agent's name. If the source is still being established, the visible session page checks again periodically; a newly available Chat tab does not require reloading the page.

Select Chat for a full reading view. You can bookmark the session address with `?tab=chat` to open directly in that view. A plain session link still opens Terminal; a personal default of writable Chat is not available yet. Opening a session directly in read-only Chat never takes terminal control.

**Open Terminal** returns to the terminal. If you had already opened it on this page, switching between Chat and Terminal keeps that attachment. Other session tabs retain their normal terminal behavior. Chat itself never types, approves a request or takes control from another window.

## Read beside the terminal

Use the [working columns](/User-Guide/New-Layout#arrange-your-working-views) to keep Chat
and Terminal visible together. Each view keeps its own reading position.

## Earlier messages and updates

Chat starts with a bounded page of recent readable messages. **Load earlier** requests an older page when one is available, keeping the message you were reading in place. Identical text from different turns stays separate. Updates to the same native item replace that item rather than appending a duplicate.

While you are at the bottom, Chat follows new messages. While you read earlier history, **New messages · Jump to latest** lets you choose when to catch up. New replies do not take keyboard focus or read every token aloud. Open views for the same person, session and source share one reader. A visible view owns the reads; other views reuse its snapshot and consumed position. Hidden views stop receiving transcript updates. The last view leaving releases the shared cache. Each view checks access before joining or resuming; browsers without a usable shared-worker facility keep Terminal available instead of starting competing readers. Reload replaces the shared snapshot for these open views.

**Source details** explains what the reader can show. A source observation time is when Rynhult checked the file, not when every message was written. The API does not currently report the actual CLI version to the page, so the source line says **CLI version not reported**. Historical messages can predate several process runs; they are not labelled as belonging to the current run.

Tool activity appears only as a collapsed count notice. Individual tool arguments/results, private reasoning, attachments and unsupported native structures are not available in this text-only reader. **At least** is a lower bound from observed source reads, not a complete tool history. Omitted or oversized records can leave a visible gap. That warning and its omitted-record lower bound remain visible across heartbeats, busy/error states and view changes. Only an explicit replacement snapshot can clear that view’s gap evidence.

## Source states and privacy

- **Unknown source:** the source, format or verified run has not been established. This does not mean the conversation is empty.
- **No messages yet:** a supported source was successfully read and contains no visible messages.
- **Unsupported:** the provider or CLI version is not qualified. A direct Chat link stays on the read-only explanation. Choose **Open Terminal** to attach; the notice never opens a terminal automatically.
- **Busy:** the bounded account reader queue is occupied. Chat keeps the last consumed position and messages and retries with a bounded backoff. Busy is different from an unreadable source.
- **Machine offline / read error:** retained messages may be stale. Retry with **Reload Chat** or use Terminal.
- **Incomplete history / more history being checked:** bounded reads, omitted records or unfinished verification prevent a complete-history claim.
- **Source or run changed:** reload Chat for a fresh snapshot. Rynhult does not guess which old cursor belongs to the changed source.
- **Access ended:** messages are cleared when the reader detects that access is no longer allowed. A member cannot read another person's private session.

Formatting uses a small non-recursive Markdown subset (plain paragraphs, inline emphasis/code, explicit links and fenced code), without active HTML or remote image fetching. At most the latest 40 messages receive formatting, with 4,096 characters, 128 lines/tokens and eight quote-prefix levels per message. Larger, older or more complex content is shown as escaped plain text. Ambiguous emphasis in commands, globs or arithmetic stays literal; inline code uses matching backtick runs. This keeps hostile formatting from blocking the terminal. A formatting failure falls back inside that message; an unexpected Chat-panel failure keeps Terminal available. Web links show their destination and open separately. Tool text, code and statements about approvals have no authority to run an action. Known secrets are redacted by the server, but this is not a guarantee that every secret somebody pasted into an agent conversation can be recognized.

The page retains at most 1,000 messages and 2 MiB of message text. If it reaches that limit, it keeps your reading history and asks you to reload the newest page instead of silently discarding the message you were reading. Chat content is not saved in browser storage or the shared query cache.

If Chat says too many sources are open in this browser, close another session page, then choose **Reload Chat**. Terminal remains available.

## Chat in the new layout

[Working columns](/User-Guide/New-Layout) replace the page-local split in the new
layout. **Open Chat beside terminal** and **Open terminal beside Chat** open or
focus the matching view, using another column when space permits. A phone shows
one view at a time and retains the wider arrangement. Moving or filling either
view keeps its resource. Chat remains read-only; opening Terminal starts as an
observer and does not take input control. Column arrangements follow the personal project/device layout described in
Working columns; Chat messages and reading history are not saved across reloads.
