# Community themes

Themes change your personal appearance, not projects, permissions, saved layouts or agents. **Settings → Appearance** and **Catalog → Themes** offer the same approved packages to signed-in people. Choose **Account → Appearance** in the Settings contextual sidebar (on a phone, open **More** for Browse).

This phase supports local declarative packages. Workspace-specific choices, arbitrary CSS, remote fonts/assets and executable plugins are unavailable. Terminal colors remain built in.

## Review and install

Only an owner can add a package to the shared Catalog. Rynhult checks your sign-in and owner role again before publishing an import. If your access has been revoked, the import is refused.

1. Open **Catalog → Themes** and choose a **Theme package (.zip)**, at most 2 MiB.
2. Select **Review theme package**. Read the name, author/source, licence and exact package digest. **Publisher not verified** means validation did not authenticate the publisher.
3. Inspect the isolated synthetic preview in Light and Dark. It contains no live sessions or private files. **Cancel** leaves Catalog and your choice unchanged. Reviews expire after 15 minutes; a changed Catalog requires another review.
4. Select **Add theme to Catalog**. This installs the reviewed package without applying it to anybody.

Invalid packages, executable content, unknown tokens, incompatible versions and unreadable colors are refused. Those checks have no override. Images can only be bounded packaged PNG thumbnails; they do not replace app identity or decorate working screens. This phase validates thumbnail files but does not display them in the picker.

## Apply or reset your choice

1. Open **Settings → Appearance**. **Appearance mode** selects System, Light or Dark for this browser. System follows the device; this mode stays separate from your account's installed-theme choice and stays in sync with the quick theme toggle.
2. Under **Installed themes**, select a card's **Preview**. The review shows the exact installed version, author/source, licence, digest and validation result in the existing isolated preview. **Cancel** leaves your choice unchanged.
3. Select **Apply to my account** to save that reviewed version for yourself. Members have the same personal chooser. Importing remains owner-only in **Catalog → Themes**; owners can follow **Manage themes in Catalog** from Appearance.

![Personal Appearance with an installed theme, light mode](/.attachments/themes-appearance-light.png)


Any signed-in person can **Preview** an installed, available version and select **Apply to my account**. This changes only that person's presentation; it does not change anybody else's choice. The server keeps the exact package digest so a newer import does not silently change your appearance. Reopening a signed-in browser loads your saved choice. Nothing is saved as arbitrary CSS in browser storage.

**Reset account theme** returns to built-in Rynhult. It keeps your System / Light / Dark choice and saved layout. If another browser changed your theme while the page was open, reload the current preference before saving again.

## Recovery

Open `/catalog?appearance=builtin` on your Rynhult address to use the built-in appearance without changing the saved choice. The recovery state stays active as you navigate in that page. Select **Themes**, then **Reset account theme** to save the built-in choice. Explicitly applying a reviewed theme leaves recovery mode.

Missing, corrupted or incompatible selected packages fall back to built-in Rynhult and show a notice. The old selection is retained so it is not silently replaced by another publisher's content. A connection/read failure also falls back rather than claiming a custom theme is current. Sign-out restores built-in appearance and clears the prior person's presentation-query cache.

Package removal, automatic update discovery and workspace overrides are not available in this phase. Owners can import a new compatible version under the same ID when its source and author are unchanged; people explicitly preview/apply that digest. A changed publisher/source needs a different ID and review. Existing skills, subagents, MCP servers, pins, profile choices and built-in restore flows continue to work separately.
