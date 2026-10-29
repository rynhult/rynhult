# Private development workspaces

Owner approved, 2026-10-22 (PLAN: Private workspaces).

Development is a workspace kind with an individual Project record, marked `private_workspace`, rooted in the creating member's agent account's local folder. This reuses the project implementations for worktrees and branches, Changes, Files, Preview, reserved ports, approved Docker compose stacks, vault keys, budgets and internal git backup. Existing research/council workspaces retain their shared built-in project and behavior. The Projects list excludes Development workspaces; their workspace card opens the shared project detail surface.

Creation initializes local git on main and creates an initial commit so worktrees can start immediately. It adds no remote. An owner can convert an existing workspace in place, preserving its history and attaching its sessions to the individual record. Conversion refuses repositories with remotes and running sessions, so their frozen launch policies cannot retain permission to push.

A private workspace's launch policy contains an unconditional local-only flag, independent of configurable instance guardrails. The existing shell parser refuses git push, git remote add/set-url and gh repo create, including wrapped and nested commands. Launch instructions explain local-only development and the separate promotion action. ACC's privileged internal backup uses the existing broker backup protocol without adding a git remote and is unaffected by agent command guards.

Promotion remains explicit and separately confirmed for GitHub creation. Development promotion updates the existing record, preserving ports, keys, compose approval and backups, and clears its private marker. Existing sessions retain their stricter frozen policy; new project sessions use normal project policy. Issue and pull request views are hidden for Development workspaces.

Verification covers creation and git state, worktree sessions and shared capability endpoints, conversion, promotion, account ownership and permission boundaries, deterministic guard corpus cases and ordinary project behavior, plus Playwright creation and session launch. Guide pages, changelog and plan accompany implementation.
