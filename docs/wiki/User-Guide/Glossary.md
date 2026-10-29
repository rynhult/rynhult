# Glossary

The words Rynhult uses, in plain language, with where to read more. Other Guide pages link here the first time they use one of these words.

## Session
One agent (Claude Code, Codex or another) working on one task, in a live terminal you can watch and type into. A session keeps running when you close the browser. Start one with **New session**. More: [New session](/User-Guide/New-Session), [Sessions](/User-Guide/Sessions).

## Project
A folder of work, usually a git repository, that sessions run in. Each project lives on one machine. More: [Projects](/User-Guide/Projects).

## Main
The **main Rynhult** is the machine you installed Rynhult on first. It runs the dashboard everyone opens in a browser, and it can run sessions itself. More: [Install](/User-Guide/Install).

## Machine, node
A **machine** is any computer that runs sessions for your Rynhult. The main is one; each other machine you add (a Linux server, a Windows PC or a Mac) is a **node**. You keep using the main's dashboard for all of them. More: [Nodes](/User-Guide/Nodes).

## Agent account
The separate account on a machine that sessions run as, for example `agent`. Agents can only do what that account may do, which keeps them away from your own files and passwords. Each person in Rynhult has an agent account. More: [Guardrails](/User-Guide/Guardrails), [Host and providers](/User-Guide/Host-and-Providers#host).

## Provider account
Your account with an agent's maker, for example your Claude or ChatGPT plan. You sign an agent account in to it on **Providers**, so sessions can use it. More: [Host and providers](/User-Guide/Host-and-Providers#providers).

## Profile
A role for a session, such as **Full-stack** or **Researcher**: instructions, skills and tools chosen for one kind of work. Choosing one is optional. More: [Catalog](/User-Guide/Catalog#profiles).

## Worktree
A separate working copy of a project, on its own git branch, so a session's changes don't mix with another session's. Tick **Own worktree** in New session to give a session one. More: [Sessions](/User-Guide/Sessions#worktrees).

## Preview
Two meanings:
- The **Preview** tab shows a project's running web app inside Rynhult. More: [Preview](/User-Guide/Preview).
- A **Preview** label on a feature means it works but isn't fully supported yet, and is off until an owner turns it on; for example [sessions in containers](/User-Guide/Sessions#sessions-in-containers).

## Inbox
The list of everything that needs you: an agent asking permission, a failed sign-in, a host problem. The sidebar badge counts what's open. More: [Inbox](/User-Guide/Inbox).

## Receiver
A Linux machine you've added that keeps Rynhult's encrypted backups. It can add backups but not delete them, so a problem on the main can't erase your old backups. More: [Backups](/User-Guide/Backups).

## Passkey
A way to sign in with your fingerprint, face, device PIN or a security key instead of your password and authenticator code. More: [People and sign-in](/User-Guide/People-and-Sign-in#add-and-manage-passkeys).

## Remote access
Reaching Rynhult from outside your home or office network. It is off by default and needs an owner to set it up. On your own network, use [Connect your phone](/User-Guide/Connect-Your-Phone) instead. More: [Remote access](/User-Guide/Remote-Access).

## Orchestrator: lead and helpers
An optional role you give one session, the **lead**, to split a larger task across a small team of **helper** sessions (sometimes called workers). The lead assigns their work and combines it, within limits you set, and you review one pull request. More: [Orchestrator](/User-Guide/Orchestrator).
