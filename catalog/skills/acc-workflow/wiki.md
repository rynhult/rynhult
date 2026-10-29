# Project wiki: layout and writing rules

Use this reference only when the project already uses this wiki layout or the
person has explicitly requested it. Otherwise follow the existing documentation
layout; do not create a wiki or skeleton because this skill is loaded. Changes
and exports still require the launch role's write authority.

In this layout the wiki lives in the repository at `docs/wiki/`, next to the code
it documents. Update it alongside behavior changes. Rynhult renders it (Project →
**Wiki**). The layout is Azure DevOps' **publish code as wiki** format, so the same
folder can be published to an Azure DevOps wiki without changes.

## Layout
```
docs/wiki/
├── .order                  # page sequence for this folder: page names without .md, one per line
├── Home.md                 # first line of the root .order: the home page
├── User-Guide.md           # a page with subpages...
├── User-Guide/             # ...has a peer folder with the same name
│   ├── .order
│   ├── Getting-Started.md
│   └── Sessions.md
├── Internal.md
├── Internal/
│   ├── .order
│   ├── Architecture.md
│   └── Operations.md
└── .attachments/           # every image the wiki uses (one folder, at the wiki root)
    └── sessions-new-session-dialog.png
```

### Rules
- **Page title = file name with hyphens for spaces.** `Getting-Started.md` shows as "Getting Started".
- **File names:**
  - no spaces, and none of `/ \ #`
  - must not start or end with `.`
  - unique within their folder (case-sensitive)
- **Every folder gets a `.order` file.** Pages not listed in it follow alphabetically, so list them all.
- **Subpages:** `Name.md` plus a peer folder `Name/`. A folder with no peer page still shows as a parent, but give it a page.
- **Images:**
  - Store them in `docs/wiki/.attachments/<page>-<what>.png`, with lowercase-hyphen names.
  - Reference them with absolute paths: `![What the screenshot shows](/.attachments/sessions-new-session-dialog.png)`.
  - Always write alt text that says what matters in the image.
  - Remove images no page uses.
- **Links between pages:** use absolute wiki paths without `.md`, e.g. `[Sessions](/User-Guide/Sessions)`. Anchors work too: `[Stop](/User-Guide/Sessions#stopping-a-session)`.
- **Format:** GitHub-flavoured Markdown (tables, task lists and fenced code are supported). No raw HTML, because it isn't rendered.

## What goes in it
### Home
- What the project is, in two sentences.
- Who it's for.
- Links into both sections.
- Its current status.

### User-Guide (for people using it)
Task-oriented: one page per job the user does. An existing page should cover, where relevant:
- **What it's for:** one or two sentences.
- **Before you start:** prerequisites, if any.
- **Steps:** numbered. Put the UI label in bold exactly as it appears ("Select **New session**").
- **A screenshot** of each screen the steps visit, placed right after the step that opens it.
- **What you should see:** the expected result.
- **Troubleshooting:** the common errors, what they mean, and how to fix them.

Also: **Getting-Started** (install or first use), **FAQ**, and a **Glossary** if the domain has jargon.

### Internal (for people building it)
- **Architecture:** the components, how they talk to each other and why. Use a diagram as an image or a clear ASCII sketch in a code block. Include the trust boundaries.
- **Components:** one page per major part. Cover its responsibility, its interfaces (API routes, CLI, files) and its configuration.
- **Data-Model:** tables, entities, important fields and migrations.
- **Configuration:** every setting and environment variable, with its default and effect.
- **Development:** setup, how to run it locally, tests, lint, and conventions (link the design system if there is one).
- **Operations:** deploy, upgrade, back up and restore, logs, and common incidents (a runbook).
- **Security:** auth, secrets handling, permissions, and the threat model in brief.
- **Decisions:** an index linking to the ADRs in `docs/decisions/`, with no copies.

Write for a newcomer: define terms the first time you use them, prefer short sentences, and show exact commands in code blocks. Keep the wiki true. When you change behaviour, fix every page that described the old behaviour (search the wiki for it).

## Screenshots
- **Tool:** Playwright, either the `playwright` MCP or the webapp-testing skill, against a **running instance**.
- **Size and theme:** desktop at 1440×900 in the dark theme unless the page is about the light theme. Add a 390×844 phone shot when the layout differs meaningfully.
- **Framing:** show the state the step describes (dialog open, item selected, result visible). Crop to the relevant region when the full window adds nothing.
- **Upkeep:** retake a screenshot whenever the UI it shows changes. Stale screenshots are bugs.
- **Data:** never take screenshots of real data when the wiki is public (see below). For private wikis, avoid secrets, tokens and personal data in any case.

## Public wikis (`docs/wiki/.public` exists)
The wiki will be published, so everything in it, text and screenshots alike, uses **artificial items**:
- **Domains:** `example.com`, `example.org` and `*.example.net`.
- **IP addresses:** the documentation ranges `192.0.2.0/24`, `198.51.100.0/24` and `203.0.113.0/24` (and `10.0.0.x` for "a LAN address").
- **People and accounts:** fictional names such as "Alex Example" and `alex@example.com`, and placeholder tokens such as `ghp_EXAMPLE…`.
- **Hosts, projects and repositories:** generic demo names such as `acc-host`, `demo-api`, `example-org/demo-app`.
- **Screenshots:** take them only from a demo instance seeded with that artificial data (the project documents how to start one), never from production.

Before committing **any** wiki change, run `acc-wiki-check` from the repository root: it checks that every page link and image resolves and that no image in `.attachments` is unused. For a public wiki it also scans `docs/wiki/` for identifiers on the local denylist (`~/.config/acc/wiki-denylist`, kept outside every repo) and for anything shaped like a secret or a real email address, in page text and in file names. It cannot read text inside images, which is why public screenshots must come from the demo instance. Fix every hit; the check exits non-zero until it's clean.
