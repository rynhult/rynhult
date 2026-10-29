# Qualify a DNS provider

Use this kit as the owner to collect evidence on a **disposable, publicly delegated test zone**. It never changes
provider release flags. Run it on a disposable Linux operator host, outside agent [sessions](/User-Guide/Glossary#session), from a reviewed checkout.
No normal test suite contacts a real DNS provider. The examples use `example.com`; substitute your actual disposable zone.

**Cloudflare is qualified; the other five implemented providers remain offered in preview.** Their portal label is **[Preview](/User-Guide/Glossary#preview): not yet qualified for this release**. This command collects evidence; it does not promote a provider or approve an installed Caddy binary.

## Recorded Cloudflare qualification

The owner ran Cloudflare qualification on **2026-10-26 at 17:23 UTC**, after #330
fixed compressed provider replies, against **a disposable zone**. Two separate
zone-scoped API tokens were used, one for Publishing and one for certificates,
each with **Zone DNS Edit + Zone Read** for that zone only.

Candidate Caddy SHA256:
`22e29521abb1ce00c030192f3c6f9126ecd84c431a672872d04095c1b1c15aba`.

All **10 stages passed**:

1. Explicit single zone.
2. Publishing create.
3. Publishing update.
4. Publishing ownership refusal.
5. Publishing delete.
6. Certificate staging issuance.
7. Shared TXT preservation.
8. TXT cleanup.
9. Forced staging renewal.
10. Ledger empty.

The kit's `release_qualified` stays **false by design**: this records the owner-run
evidence, not an automatic release-flag change or approval of another Caddy binary.

## Before starting

1. Create and delegate a new public test zone. Keep production zones, mail and other people's records out of it.
2. Create **two independent credentials**, Publishing and certificates, using the provider checklist below.
   Use short expiry where supported. Accept any documented broader authority explicitly; otherwise stop.
3. Obtain the reviewed Caddy candidate and its SHA256 **independently from the release reviewer**. The kit copies
   the verified bytes into its private directory before executing them. A digest downloaded beside an executable
   is not independent approval. Check the module pins against the reviewed release manifest.
4. Use an ordinary owner's terminal, with a private, owner-controlled parent directory, and choose a **new** state
   directory for every run. Never run inside a recorded terminal, shell tracing, an agent session or a screen share.
   Do not put credentials in arguments, shell variables, environment variables, redirected stdin or artifacts.

## Least-privilege credential checklists

These grants match the current adapters and pinned modules. They are qualification candidates, not claims that live
IAM tests have passed. Create the zone with your administrator identity, then create the limited identities below.
Do not grant the qualification identities permission to create zones or administer IAM unless a limitation explicitly
says otherwise. Never use a production administrator credential to work around a failed probe.

### Cloudflare — `cloudflare`

1. In **My [Profile](/User-Guide/Glossary#profile) → API Tokens → Create Token**, choose **Create Custom Token**.
2. Add **Zone → DNS → Edit** and **Zone → Zone → Read**. Under **Zone Resources**, choose
   **Include → Specific zone → your test zone**. Add no account permissions and no other zones.
3. Create separate expiring tokens named for Publishing and certificates. Paste each `api_token` only at its hidden prompt.
   Cloudflare cannot restrict these tokens to record names or types; both can edit every DNS record in that one zone.
4. Confirm each token's resource summary names only the test zone. The kit also refuses a discovery result containing
   more than that zone.

[Cloudflare token creation](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/).

### Route 53 — `route53`

1. Create a **public hosted zone**. Record its unprefixed hosted-zone ID and exact apex name; the kit checks its SOA.
2. Create two IAM users with **no console access**, or two short-lived assumed-role sessions. Give each only the
   corresponding inline policy in `deploy/dns/policies/route53-publishing.json` or `route53-certificates.json`.
3. Replace **every** `ZEXAMPLE` with this zone's ID and every example domain with its exact apex. For this kit, narrow
   Publishing's normalized-name pattern to `acc-dns-trial-*.example.com`; narrow certificates' patterns to
   `_acme-challenge.acc-cert-trial-*.example.com`. Keep the type/action conditions: Publishing A/AAAA and CREATE/DELETE;
   certificates TXT and CREATE/UPSERT/DELETE. Keep zone-scoped ListResourceRecordSets and GetChange on `change/*`.
   No ListHostedZones or full-access policy is needed with the explicit ID.
4. At each purpose's hidden prompts enter `access_key_id`, `secret_access_key`, `hosted_zone_id`, `zone_name`, and
   `session_token` if temporary credentials are used (otherwise leave that optional prompt empty).

[Route 53 condition keys and name normalization](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/specifying-conditions-route53.html).

### Azure public DNS — `azure` — qualification hold

The pinned Azure module cannot preserve shared TXT records. The command writes a failed report with
`azure_shared_txt_release_hold` **before prompting or making network writes**. Do not bypass it. After a reviewed
module repair, the hold must be removed with sentinel regression evidence before live qualification.

Prepare the limited identities for that later run:

1. Create a public zone in a dedicated resource group and two Entra application/service principals, with no directory roles.
2. Create two custom roles with empty DataActions/NotActions. Both need `Microsoft.Network/dnsZones/read` and
   `Microsoft.Network/dnsZones/*/read` for zone/record discovery. Publishing additionally needs
   `Microsoft.Network/dnsZones/A/write`, `/A/delete`, `/AAAA/write`, `/AAAA/delete` (each suffix uses the same
   `Microsoft.Network/dnsZones` prefix). Certificates needs `/TXT/write` and `/TXT/delete` only.
3. Set the role definitions' AssignableScopes to the dedicated resource group, then **assign** each role only at
   `/subscriptions/SUBSCRIPTION/resourceGroups/GROUP/providers/Microsoft.Network/dnsZones/example.com`.
   No inherited Contributor, Owner, DNS Zone Contributor or subscription assignment for either principal.
4. Create a short-lived client secret for each. Future hidden prompts are `tenant_id`, `subscription_id`, `client_id`,
   `client_secret`, `resource_group`, `zone_name`. These roles restrict write types, not names within the zone.

[Azure zone scope and custom roles](https://learn.microsoft.com/en-gb/azure/dns/dns-protect-zones-recordsets).

### Google Cloud DNS — `googleclouddns`

1. Enable Cloud DNS in a dedicated [project](/User-Guide/Glossary#project) containing **only this public managed zone**. Record project ID,
   managed-zone resource name and exact DNS apex. Create two service accounts with no basic project role.
2. Create a custom role with exactly `dns.managedZones.get`, `dns.changes.create`, `dns.changes.get`,
   `dns.resourceRecordSets.list`, `dns.resourceRecordSets.create`, `dns.resourceRecordSets.update`,
   `dns.resourceRecordSets.delete`. Bind it on the test managed zone to both service accounts.
3. For the certificate account only, add a project-level custom role containing `dns.managedZones.list`:
   the pinned module discovers zones at project scope. Keep the project dedicated. Neither account needs
   DNS Administrator, zone create/delete, IAM or compute permissions. Cloud DNS IAM does not isolate record types/names here.
4. Create one JSON service-account key per account. At the hidden `service_account_json` prompt paste **compact JSON
   on one line**, with PEM newlines represented as JSON `\n`, never an external-account or ambient ADC credential.
   Other hidden prompts: `project_id`, `managed_zone`, `zone_name`. Handle the downloaded key in your trusted credential
   manager; never print it into a shell log. Revoke both keys after qualification.

[Cloud DNS permissions and zone-level IAM](https://docs.cloud.google.com/dns/docs/access-control).

### DigitalOcean — `digitalocean`

1. Use a dedicated account/team with **only the disposable DNS domain**, without production resources. Tokens are not
   limited to a single domain; record this broader domain authority in the release review, or stop.
2. In **API → Tokens → Generate New Token**, choose custom scopes. For certificates select only `domain:read`,
   `domain:create`, `domain:delete`; for Publishing add `domain:update`. Do not select full access or unrelated scopes.
   These scopes also permit domain operations, not just record operations. Set a short expiration.
3. Create separate tokens and paste each `api_token` plus the exact `zone_name` at hidden prompts. Do not add
   other domains to this account while qualification credentials exist.

[Token creation](https://docs.digitalocean.com/reference/api/create-personal-access-token/) and
[domain scope definitions](https://docs.digitalocean.com/reference/api/scopes/domain/create/).

### Hetzner Console Cloud DNS — `hetzner`

1. Create a dedicated **Console project** containing only the disposable zone, in **primary** mode. Never use the
   legacy DNS Console API/token or put production servers in this project.
2. Under **Security → API tokens → Generate API token**, create separate Publishing and certificate tokens with
   **Read & Write**. The current API has no DNS-only or per-zone write token: each token controls other resources
   in the project too. Explicitly accept that authority or stop. Delete the tokens after the run.
3. Paste each `api_token` and exact `zone_name` at hidden prompts. The kit resolves the exact primary zone;
   it does not choose a zone by position. Do not add other resources while the tokens exist.

[Hetzner project token creation and permission levels](https://docs.hetzner.com/cloud/api/getting-started/generating-api-token/).

### Wave 3

Porkbun, Gandi, OVH, deSEC and RFC 2136 have not merged into this checkout. Their IDs are refused, not treated as a
Cloudflare fallback. Add exact provider-specific IAM/TSIG instructions, schemas, adapter/module pins and boundary
fixtures when those implementations merge; then run this same qualification path. No qualification is claimed today.

## One command per provider

From the reviewed checkout, run this command, replacing the zone, paths and digest. Use one of the exact IDs above
for `--provider`; run separately for each provider with its own fresh state directory. The Azure command currently
produces the hold report. The provider-specific checklist is required before running.

```bash
PYTHONPATH=src python -m acc.dns_qualify \
  --provider cloudflare \
  --disposable-zone example.com --confirm-zone example.com \
  --state-dir /private/dns-trial-cloudflare \
  --caddy /private/caddy-reviewed \
  --candidate-sha256 INDEPENDENTLY_REVIEWED_SHA256
```

The digest must be 64 lowercase hex characters. Repeat with `route53`, `azure`, `googleclouddns`, `digitalocean`
or `hetzner` and a new corresponding state directory. There are **no credential flags**. All fields use hidden
interactive prompts, including identifiers, to avoid accidental disclosure. The kit refuses non-interactive or
echoing input; it does not read environment credentials or export the infrastructure vault.

Expected result: exit 0 and a private `report.json` with `status: passed`, the exact zone, reviewed candidate hash,
start time, fixed PASS labels, and `release_qualified: false`. Exit 1 means incomplete qualification. Invalid arguments
exit 2 without reflecting their values. Invalid confirmation/paths may fail before a report can safely be created.

The command reuses the server adapters and ownership ledger. It creates a random A record, verifies it through the
provider API, updates and verifies it, proves a foreign ledger cannot adopt it, then deletes and verifies absence.
For certificates it seeds a TXT sentinel at a random challenge name, runs the reviewed Caddy against Let's Encrypt
**staging**, checks preservation, restarts with the same certificate storage and a forced renewal window, requires
a different serial and fingerprint, checks TXT preservation again, removes its sentinel, verifies absence and
checks the ledger and challenge intents are empty. It does not modify your installed Caddy or open a listener.

The Python boundary refuses other zone IDs and names outside the generated trial prefixes. Caddy receives only the
random trial hostname and explicit credentials; it is a trusted reviewed executable, not a sandboxed untrusted
program. **The credential scopes and dedicated accounts/projects are the external enforcement boundary**. Explicit
zone discovery is not proof that a principal has no other inherited grants; review IAM and perform negative tests separately.

## Failure, cleanup and release review

On the first observed error the command stops probes and terminates only its own Caddy process. It does not retry
mutations or attempt speculative remote cleanup after an uncertain failure. Existing adapters retain their bounded
read throttling/polling behavior; this command adds no probe retries. Caddy error-level events fail the run; its
bounded private transient output is discarded, never copied to the report. Repeated ACME errors may still occur
inside Caddy before the next half-second observation; this is not an ACME transaction rollback.

1. Retain the private state directory. Read `failed_stage` in `report.json`. No provider exception body is included.
2. In the provider console, compare **only** the trial names in `ledger.db` (`dns_owned_record`) and
   `challenge-*.json` with their exact IDs, values, TTLs and provider audit history. Pending/uncertain writes may have
   succeeded. Do not delete unrelated records, remove shared TXT values or retry a stale snapshot. Have the owner
   reconcile changed records explicitly. There is no automatic resume command.
3. After reconciliation, verify no trial records remain, revoke both credentials, remove downloaded key copies from
   the trusted credential manager when appropriate, and keep the sanitized report as evidence. Keep ledger/intents
   private until reconciliation is signed off. The kit deletes its temporary credential file, Caddy storage and verified
   executable on ordinary success/failure; a host crash can leave private files that the owner must remove after review.
4. Complete negative IAM tests, parallel TXT issuance, actual lifetime renewal/ARI scheduling, dependency/provenance,
   root helper rollback and service sandbox gates before approving a release. A forced staging renewal tests the renewal
   path, not a weeks-long production renewal timer. No full provider qualification or production activation is automatic.
