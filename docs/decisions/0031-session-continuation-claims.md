# Session continuation claims

Renumbered on 2026-10-24 from 0007 to 0031.

Date: 2026-10-22

## Context

Opt-in limit recovery and bounded goals cause ACC to send input without a person present. Replaying a prompt after a controller restart can duplicate work. Current brokers and provider terminals do not offer transactional, idempotent prompt delivery. Plan usage readings describe account windows rather than goal-specific token spending.

## Decision

Persist settings, counters, reset identities, human cancellation and a stable continuation claim on the session before sending any input. Treat ambiguous delivery as requiring human inspection and never replay a claimed command. Preserve counters and start time across edits. Use account-window percentage for the optional usage cap, and pause if the session's account has no trustworthy reading. Only assistant markers determine done/blocked; no second model judges completion.

## Consequences

At-most-once dispatch can lose a continuation if ACC dies after claiming but before sending. The panel exposes that claim. Updating brokers is required for the screen observation operation. Current controller serialization is local; future multi-controller work needs shared transactional claims/fencing and broker idempotency before promising reliable retries. Goals remain independent of teams and scheduling.

## Alternatives

In-memory timers lose intent on restart. Retrying sends without broker deduplication can duplicate instructions. A separate judging model adds cost and discretion; it can be an explicitly chosen future option. A per-goal usage cap needs accounting ACC does not currently collect for every provider.
