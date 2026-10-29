# Company sign-in: what proves MFA, and passkeys for company-only members

Date: 2026-10-25. Status: accepted (owner decisions on the organizations sign-in follow-ups). Builds on
[0056](0056-oidc-relying-party.md). Off by default with the rest of company sign-in; unqualified against a real
provider.

## Context

Decision 0056 counted company MFA when the ID token's `amr` contained `mfa` or its `acr` was one of a few
well-known multi-factor values (OpenID PAPE URIs, `phr`, `phrh`). `acr` values mean whatever each provider and
tenant decides: Entra ID's v1 tokens send `acr: "1"` after any password sign-in, and authentication-context values
are tenant-defined. A fixed list either misses a tenant's real value or trusts one the tenant doesn't mean.

Members created by company sign-in (pre-provisioned or invited) have no local password or authenticator anyone
knows, so they could never verify again to add a Rynhult passkey. While passkeys are Required, such a member
without a passkey depends on every sign-in proving MFA.

## Decision

1. **MFA is proven by `amr`, or by acr values the owner names.** By default the ID token must say
   `amr` contains `mfa`, `hwk` or `fido` (RFC 8176). An owner can list up to 10 tenant-specific `acr` values in
   Settings → Company sign-in; a token whose `acr` is one of them also counts. No `acr` counts by default. `0` and
   `1` can never be saved or counted. Changing the list affects later sign-ins only.
2. **Company-only members may add a passkey only right after an MFA-proven sign-in.** When such a member's
   company sign-in proves MFA and its signed `auth_time` is within the last 600 seconds, that sign-in opens a
   five-minute window (`auth_session.passkey_enroll_until`) in which
   it may register **one** passkey without verifying again. Registering closes the window atomically. Nothing else
   that needs a fresh verification (removing passkeys, recovery codes, role changes) accepts the window. Owners can
   turn this off (`oidc_provider.passkey_enrollment`, on by default); turning it off also closes open windows.
   Members with local credentials (linked members) get no window: they verify with their own password and
   authenticator or passkey, as before.
   When enrollment is enabled, the authorization request includes `max_age=600`. Missing, non-numeric, boolean,
   non-finite, future or older `auth_time` opens no window; a fresh token's `iat` is not proof of recent
   authentication. Ordinary company sign-in still follows the existing MFA/step-up policy in those cases.
3. **Who is company-only** is recorded on the user (`user.company_only`), set when company sign-in creates the
   member. Migration 0054 back-fills it from the audit log (`organizations.identity_bound` with `how` of
   `preprovisioned` or `invited`). A completed reset link clears it: the member then has a password and
   authenticator of their own (#326).
4. **Required member passkeys do not require a recovery code for company-only members.** They still need one
   passkey at the current address before the policy can be enabled. Owners still need two passkeys and a recovery
   code; members with local credentials still need one passkey and a recovery code. Role changes use the same
   recovery exception, never an exception for a target owner role.
5. **Migration 0054 cannot be downgraded while company-only accounts have passkeys.** Removing `company_only`
   would turn their company-confirmation keys into standalone sign-in credentials on the older code. Refuse before
   any schema change, with instructions to remove the affected passkeys or accounts first; never delete or convert
   them automatically.

## Consequences

- Owners using Entra ID need nothing extra when Conditional Access requires MFA for the app (Entra sends
  `amr: ["mfa", ...]`). Providers that only signal MFA through `acr` need the owner to enter their values.
- A token claiming `hwk` or `fido` alone is trusted as multi-factor because the provider vouches for a
  hardware-bound or FIDO authenticator; that is the owner's decision.
- With a passkey, a company-only member can satisfy the step-up and verify again like anyone else, so a sign-in
  without MFA proof stops being a dead end once one MFA-proven sign-in has happened.
- Company-only members still have no password, so the password-plus-recovery-code path doesn't apply to them; a
  lost passkey is replaced by another MFA-proven company sign-in with recent provider authentication.
- Providers must return trustworthy `auth_time` when asked for `max_age`. A provider that omits it can still sign
  members in, but cannot open the enrollment window. This remains unqualified against a real provider.

## Alternatives considered

- **Keep the built-in acr list:** rejected; values are tenant-specific and some (Entra v1 `1`) are misleading.
- **Let the window stand in for every fresh verification:** rejected; the window exists only so a member with no
  local credentials can get a passkey, not to skip verification for other changes.
- **Any company sign-in may add a passkey:** rejected; without MFA proof a phished password alone would let an
  attacker plant a passkey that later satisfies the Required policy.
- **Use token issue time for enrollment freshness:** rejected; an existing provider session can issue a new token
  without authenticating the person again.
