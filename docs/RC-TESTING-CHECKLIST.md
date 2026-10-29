# rc.1 tester checklist

Tick each line as you go. The how-to for every step is in [RC-TESTING.md](RC-TESTING.md). Anything that doesn't
work, or surprises you: review the in-app report first when available, then
[open a public RC issue](https://github.com/rynhult/rynhult/issues/new).
If you cannot file an issue, contact [support@rynhult.dev](mailto:support@rynhult.dev).
Never include secrets or logs containing tokens. Send security problems privately to
[security@rynhult.dev](mailto:security@rynhult.dev); use [Discussions](https://github.com/rynhult/rynhult/discussions) for questions.
See *Report a bug* in the tester kit for what to include.

**Test scope:** the self-hosted Linux server, existing Linux/macOS/Windows-WSL nodes and the dashboard in
desktop and phone browsers, including the installed phone web app. Native apps are outside v1, even where
experimental source is present. They follow v1.0 one at a time, in an order not chosen yet: native Windows, macOS
standalone, Linux desktop, iOS and Android are candidates, not immediate rc.1 follow-ups.
See [decision 0073](decisions/0073-v1-linux-server-scope.md).

## Install or upgrade

- [ ] I'm testing on a machine I can wipe (ideally a virtual machine).
- [ ] I downloaded the tarball, `BUILD`, `SHA256SUMS` and `SHA256SUMS.asc`; I compared both full release-key fingerprints, verified the signature, then `sha256sum -c SHA256SUMS` printed `OK` for both files.
- [ ] **New install:** `acc-install` (Ubuntu/Debian) or `setup.sh --unqualified-distro` (Fedora, openSUSE, Arch)
      finished without errors.
- [ ] **Upgrade from 0.14:** the backup in `/var/backups/acc/<today>` has `acc.db` and `secrets`; the swap and
      `setup.sh --yes` finished; my projects and sessions are still there; I updated my other machines on each
      machine (step 6; Host's **Update** refuses a release candidate).
- [ ] I signed in (setup token on a new install, my usual sign-in after an upgrade).
- [ ] **Settings → Updates** shows **Rynhult 0.15.0-rc.1** and a build.

## First 30 minutes

- [ ] An agent CLI is signed in on **Providers**.
- [ ] I added a project with **New project**.
- [ ] I started a session with **New session** and typed into its terminal.
- [ ] I closed the browser, came back, and the session was still running.
- [ ] **Changes** and **Activity** showed what the agent did.
- [ ] An agent's question appeared under **Needs you** and in **Inbox**, and I answered it.
- [ ] Rynhult works on my phone, installed from the browser, with notifications on.
- [ ] **Search** found a word from my session.

## If you can

- [ ] I added a second machine (Linux, macOS or Windows through WSL) from **Host → Machines → Add machine**
      and ran a session on it.
- [ ] I looked at **Settings → Usage and bug reports → Show exactly what would be sent**.
- [ ] I opted in to usage statistics and/or bug reports (optional).
- [ ] I reviewed a bug report Rynhult offered, and sent it or chose **Don't send**.
- [ ] I tried a Preview or off-by-default feature, and said which one in my report.

## When you're done

- [ ] I reported every problem I found, with the version and build, my distribution, and the steps.
- [ ] I copied anything I want to keep from `/srv/acc/workspaces`, then wiped the test machine, or ran
      `deploy/uninstall.sh` (first without options, then with `--yes`) and noted anything left over.
