# Automatic HA starts with a fail-closed protocol model

Context: two mains cannot safely elect on heartbeat silence. External commands
can outlive the database epoch check, and PostgreSQL failover has its own quorum
requirements. The owner wants automatic recovery within minutes.

Decision: model a single durable witness with monotonic leases, incarnation/epoch
CAS and conservative client deadlines. Require fresh lag and independent external
fencing. Ship only pure decisions and a default-off owner deployment switch for
status evaluation. No production observation adapter or automatic promotion call.
See [the design](../HIGH-AVAILABILITY.md#automatic-failover).

Consequences: the installed evaluator always blocks pending real telemetry and
fencing adapters. Simulations establish model properties, not PostgreSQL or host
fencing guarantees. Manual failover remains available under its existing rules.

Alternatives: heartbeat-only election admits split brain; two-main majority has
no failure tolerance; a separate enrolled-machine witness adds a storage/protocol
service without removing external fencing requirements. A consensus-backed
PostgreSQL control plane is the preferred future integration boundary.
