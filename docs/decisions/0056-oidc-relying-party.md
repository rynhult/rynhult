# OIDC relying party: in-house token verification, local owners, invite-only first sign-in

Date: 2026-10-25. Status: accepted implementation choice within the approved Organizations design
([ORGANIZATIONS.md](../ORGANIZATIONS.md) "Identity and lifecycle", decision
0008 (private decision record)). Off by default; unqualified against a real provider.

## Context

Phase 1 starts with OIDC sign-in, Entra ID first. The design requires code flow + PKCE, one-use state and
nonce, exact redirect URI, validated issuer/audience/signature/times, safe key rotation, stable identity keyed by
(organization, issuer, subject), no email linking, and no implicit owner authority from an IdP. The environment
had no JOSE library installed and new machine-wide packages need approval.

## Decision

- **Verify ID tokens with `cryptography` directly** (`src/acc/server/oidc.py`): compact JWS, RS256 only, keys
  only from the configured issuer's discovery-named JWKS. A narrow allowlist is smaller to review than a general
  JOSE library's algorithm negotiation, and the alg-confusion classes (none, HS256 with the public key, embedded
  keys/URLs) cannot be reached. ES256/PS256 can be added explicitly when a qualified provider needs them.
- **Owners never sign in through the provider.** Binding refuses owner accounts, the callback refuses an owner
  role, and an open company sign-in stops counting once its person is promoted to owner. Owners keep passkeys or password + TOTP, which is the break-glass path when the IdP is down.
- **First sign-in requires an owner's prior act:** pre-provisioning a subject, or a one-time 7-day sign-in link
  bound to the invited directory account (owner answer, 2026-10-25): the verified token must name the same email
  (`email_verified`) or UPN (`upn`; `preferred_username` only from Entra ID), ASCII case-insensitively. That check gates redemption only; identity stays (issuer,
  subject). Links can create a member or link an existing member, who must also present their own local sign-in
  from the last 15 minutes ("recent authentication to both identities"). No JIT or domain enrollment. Directory
  group / OU sync is the intended long-term path (SCIM phase); `admitted_by_directory()` is its hook.
- **MFA (owner answer, 2026-10-25):** company MFA counts only when the ID token proves it (`amr` contains `mfa`,
  or a known multi-factor `acr`; refined by [0061](0061-oidc-mfa-proof-and-passkey-enrollment.md): `amr`
  `mfa`/`hwk`/`fido`, and only owner-named `acr` values). Otherwise, while passkeys are Required for members, a member with a passkey gets a
  session restricted to the passkey check (`auth_session.step_up`), and one without is refused.
- **Stable IDs:** `user.person_id` is filled at first binding with the phase 0 report's value
  (uuid5 of the saved installation namespace and the user row id), so a later full migration agrees with it.
- **Company sessions last 12 hours by default, an owner setting bounded to 1-24 hours** (owner answer,
  2026-10-25); lowering it caps sign-ins already open. They are revoked when SSO is turned off, the provider changes, or the person is
  unbound. Provider logout (RP-initiated or back-channel) is out of scope for this slice.
- **Client secret** sealed with the vault data key under purpose-bound associated data (issuer and client ID), not
  as a vault key, so it can never be granted to projects or delivered to sessions.

## Consequences

- Organizations mode (`ACC_ORGANIZATIONS=1`) and an owner's switch are both needed; personal installs see nothing.
- Entra pairwise subjects are usually unknown in advance, so sign-in links are the practical onboarding path until
  SCIM maps an immutable attribute.
- A lost vault data key makes the stored client secret unreadable; sign-in then fails with "unavailable" and the
  owner re-enters the secret. The vault's create-on-empty check does not yet count the OIDC secret.
- Owners stay local-only (owner answer, 2026-10-25), keeping break-glass independent of the provider.
- Rynhult requests `openid profile email` so Entra sends `preferred_username`/`email` for the invite check.

## Alternatives considered

- **Authlib / PyJWT / joserfc:** mature, but not installed and broader than needed; can replace the verifier later
  behind the same `Provider.verify` interface if qualification asks for it.
- **Email or UPN matching for first sign-in:** rejected by the design (mutable, reassignable claims).
- **JIT creation for any tenant user:** rejected for this slice; decision 0008 makes JIT a weaker, opt-in fallback.
