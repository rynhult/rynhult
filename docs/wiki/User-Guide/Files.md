# Files

Browse and edit a [project](/User-Guide/Glossary#project)'s folder, a workspace, or a [session](/User-Guide/Glossary#session)'s own [worktree](/User-Guide/Glossary#worktree) from Rynhult. Owners can open every project; members can open only their own, with the same access as sessions.

## Open and edit

1. Open a project and select **Files**. For a workspace, open its card's three-dot actions menu and select **Files**. A session's **Files** tab opens that session's worktree rather than the main project checkout.
2. Expand folders as needed and select a file. Rynhult loads one directory at a time. **M** means modified, **N** means new and **I** means ignored by git. File sizes appear beside the names.
3. Edit text in the editor. Markdown has a **Live preview** beside it on a wide screen and below it on a phone. Code has syntax highlighting. Preview images in Markdown are represented by their alt text; the preview does not fetch external images.

![Markdown editor, folder tree and live preview](/.attachments/files-editor.png)

4. Select **Save** to write the file. **Refresh files** reloads the tree and, when there are no unsaved edits, the open file. Switching files asks before discarding a draft.
5. On phones, choosing a file collapses the tree and focuses the loaded file. **Show tree** brings it back; **Hide tree** closes it manually. Cancelling a draft warning keeps the draft, selected file and open tree unchanged. The tree stays open on desktop.

![Phone editor with its tree collapsed](/.attachments/files-phone.png)

![An accepted file selection focuses the phone editor without its folder tree](/.attachments/files-selected-phone.png)

![The same selection keeps the folder tree beside the desktop editor](/.attachments/files-selected.png)

## Commit locally

1. Select **Commit identity**, enter your **Commit name** and **Commit email**, then **Save identity**. This is your account's identity on every machine, including when an owner edits another person's project.
2. Enter a **Commit message** and select **Save and commit**.

Rynhult saves and commits this file's editor contents on the current branch. Other staged work stays staged; repository hooks, signing and clean filters do not run. There is no push. A detached checkout cannot be committed: choose a branch through your agent first. If committing fails, the file stays saved and Rynhult says so; correct the repository problem and retry **Save and commit**.

## Files and folders

- **New file** and **New folder** take a path relative to the open folder's root. Parent folders must already exist. Select a new file from the tree to edit it.
- The **…** button beside an entry opens its rename and delete actions. Confirm the action before anything changes. Existing destinations are never overwritten.
- Renaming a folder preserves ordinary contents, but refuses protected files, links, special files and trees over 2,000 entries. Delete folders only when empty. Deleting uncommitted work cannot be undone through Rynhult.

## Upload files

Expand a folder to select it as the upload destination, or select a file to use
its containing folder. Check **Upload to** before choosing **Upload files**; the
file picker accepts several files together. **Use workspace root** resets the
destination. You can also drop files onto an unlocked folder row or the tree;
a folder-row drop chooses that folder.

![Upload files, the selected destination and a completed batch](/.attachments/files-upload.png)

![Upload controls and the file tree on a phone](/.attachments/files-upload-phone.png)

Uploads accept text and binary files, including images, audio and PDFs. The limit
is **4 MiB per file**, **16 MiB per batch**, and **100 files**. Oversized files show
a refusal beside their name. Batch progress measures the transfer; wait for each
file to say **Uploaded** before leaving. Errors appear per file, so successful
files can remain even if another file fails. If the connection is lost, check the
tree before trying again. General upload errors appear in the named **File upload
error** alert within Files, separately from editor conflicts or other page notices.

An existing name asks you to **Replace**, **Keep both**, or **Cancel upload**.
Replace checks that the original has not changed since the question appeared.
Before replacing it, Rynhult moves the existing file to a unique hidden name in
the same folder. The result says **Replaced report.txt (the previous copy is kept
as …)** and gives its path. If a late edit is detected, both versions are kept
and the result gives their paths. Keep both chooses an unused numbered name, such as
`drawing (1).png`. Cancel leaves the existing file alone. Protected files, links,
Git internals and files containing hidden Keys values cannot be replaced here.
Uploads use the project's machine and account, just like edits, and each submitted
batch is recorded in the audit log without file contents. Uploads do not commit.

## Concurrent edits

**A session is working in this folder** warns that an agent may also be editing. You can continue. If the file changes after you open it, **Save** refuses to overwrite it and shows **File changed on disk**: the editor retains your draft and a separate panel shows the current disk version.

Compare them, then choose **Keep draft and use current revision** to merge any needed changes into your draft and save again. **Use disk version** asks before discarding your draft. Further changes on disk cause another conflict. Editor saves, including **Save and commit**, preserve the previous file under a hidden name before publishing the new version. If an agent edits during that move, Rynhult keeps the incoming draft too and reports both paths. If another program recreates the original name, its new file stays untouched. A conflicted save does not commit.

### Recover and clean up previous copies

Recovery files stay in the same folder as the edited file. Names begin with
`.rynhult-replaced-` for displaced versions and `.rynhult-incoming-` for drafts
kept after a late conflict or an unsuccessful replacement. The timestamp and
random suffix make each name unique. They appear in the Files tree after refresh;
select one to inspect or download it, or rename it to restore it after checking
the destination. Existing destinations are never overwritten by Rename.

Copies remain until you explicitly delete them through **… → Delete** or your
agent. There is no automatic expiry or pruning: an older copy may contain the
only surviving agent edit. They consume workspace disk space and are not
included in **Save and commit**. Clean up copies you no longer need; ordinary
Files protections and size limits still apply. If a connection fails during
replacement, refresh the folder and check these hidden recovery files before
retrying. A program that already had the old file open may continue writing to
the moved copy, so inspect it after that program finishes.

## Protected files and limits

Secret files remain listed with a lock: `.env` (except templates such as `.env.example`), private keys, credential stores, `session.env`, Rynhult's run folder and git internals. They cannot be opened, downloaded or changed. Symlinks and special files are unavailable, including links that point inside the folder. The registered folder and every parent leading to it must also be real directories, without symlinks. Folders inside `.git` or a protected credential folder cannot be used as a Files root; choose the ordinary project folder instead.

Text is limited to 1 MiB. Larger files show **Too large**. PNG, JPEG, GIF and WebP files within that limit have a preview. Recognized MP3, WAV, Ogg audio, M4A, MP4 and WebM files have audio or video controls; playback depends on your browser’s codec support. Playback does not start automatically. HTML and SVG are never rendered inline; other binaries are download-only. **Download** keeps the original file name and extension. Downloads are limited to 4 MiB. Directories over 2,000 entries are refused rather than silently truncated.

On Linux, parents leading to a registered folder need only traversal permission.
They do not need directory-listing permission; the folder being browsed still does.

Known vault values, including retained previous values, are redacted from everything Files displays. Redacted text is read-only to prevent overwriting the original with placeholders; ask your agent to edit it instead. Text downloads are redacted too. A binary containing a known vault value is refused.

If a file cannot be opened, check its size, path and type. **Broker unavailable** means its machine or agent broker is disconnected; reconnect it and refresh. Old machines need a broker update before they can serve Files. Files and commits remain on the machine where the project lives.

Unsaved edits also prompt before Back/Forward, changing tabs or following another
Rynhult link. Choose **Cancel** to keep editing, or **Discard draft** to leave. Reloading
or closing the page uses your browser's unsaved-changes warning.

Starting a save clears the previous save message. If the connection fails, your
draft stays in the editor and the error is shown; retry **Save** after reconnecting.

![Back navigation asks before discarding an unsaved file draft](/.attachments/files-unsaved-navigation.png)
Cancelling a navigation warning also keeps an in-progress save's status intact.

An upload finishing or a delayed refresh response does not replace a draft you
have started typing. The draft keeps the revision you originally opened, so a
later **Save** still detects intervening disk edits and offers the conflict choices.
