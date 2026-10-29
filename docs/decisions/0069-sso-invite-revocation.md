# Retain revoked company sign-in invitations

## Context

An owner must be able to withdraw an unused company sign-in invitation, including
while its browser is waiting for the identity provider. Deleting the invitation
cascades pending flows and makes that callback look expired instead of withdrawn.

## Decision

Retain the invitation with a nullable `withdrawn_at` marker. Hide revoked rows from
the active invitation list. Check revocation before and after provider I/O, within
the existing short write transactions. Pending flows keep their normal lifetime
and one-use browser binding; a revoked invitation cannot admit a member.
Withdrawal is idempotent. The withdrawal is audited as `org.invite_withdrawn`, a repeat as
`org.invite_withdraw_repeated`, and a refused one (used or unknown) as `org.invite_withdraw_refused` with its reason.
Used invitations remain history and cannot be withdrawn.

Migration 0066 follows main's 0054. Downgrade expires revoked invitations before
dropping the marker, preventing accidental reactivation on rollback.

## Consequences and alternatives

Revoked invitation rows remain stored, rather than being hard-deleted. No provider
I/O runs while holding the database write lock. Hard deletion was rejected because
it loses the reason for a pending callback's refusal. Keeping a separate tombstone
table would duplicate the same invitation identity and require another lookup.
