# Owner-confirmed issue-server addresses

Renumbered on 2026-10-24 from 0010 to 0034.

Context: SR3-01 closed a member-controlled SSRF path by refusing all non-public destinations. Owners requested
self-hosted git issue sources, including private networks. Merely validating DNS at connection setup leaves
rebinding possible; globally installing a custom CA would broaden unrelated trust.

Decision: owners manage explicit HTTPS server entries in Settings, confirm a freshly resolved exact address set,
and may upload a per-entry CA. Connections select entry ids. Each outbound request re-resolves and checks the
entire set, then pins the socket address while preserving HTTP/TLS host identity. Only public addresses and
ordinary LAN/ULA ranges are eligible; metadata/link-local and other special destinations stay refused. Audit
stores decisions and pins, never PEM. Existing vault authority, response/deadline bounds and untrusted text apply
to both GitLab self-managed and Gitea/Forgejo. Public sources need no entry.

Consequences: owners explicitly authorize ACC network access to selected LAN servers. DNS changes require
reconfirmation; deleted entries fail before cache access. CA contexts are local to each request's selected host,
port and base path. The owner remains trusted to choose legitimate git hosts. Cache hits perform no network IO.

Alternatives: private URLs for all members reopen SSRF; hostname-only entries permit DNS rebinding; machine-wide
CA installation broadens trust and requires host administration. None is used.
