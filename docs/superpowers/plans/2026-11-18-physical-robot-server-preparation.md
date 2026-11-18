# Physical Robot Server Preparation Implementation Plan

**Goal:** Prepare and test server-side integration while the physical robot and its operating protocol are unfinished.

**Architecture:** Preserve the existing simulation-only runtime and its restrictions. Build independently testable server boundaries and use isolated simulated peers; do not authorize physical motion or infer physical position, stopping, or clearance from simulated evidence. Reuse existing reservation, MQTT, and recovery logic only where its guarantees actually apply.

**Tech Stack:** Python, FastAPI, SQLite, MQTT, unittest.

**Design references:** `origin/docs/project-plan:docs/design.md`, `server/docs/field_robot_interop.md`, `server/docs/segment_recovery_proposal.md`.

## Global constraints

- Preserve the design's existing MQTT order format. New wire fields and recovery semantics are proposals until the robot team agrees.
- Keep physical command publishing and clearance disabled; mock evidence is not evidence of physical safety.
- No physical motors, cameras, shared brokers, credentials, or production state in tests.
- Record uncertain outcomes and preserve reservations; MQTT PUBACK is not robot acceptance or execution.
- For each independently usable feature: write a meaningful failing test, implement, test, review, commit and push.

## Tasks and dependencies

1. **Offline integration boundary:** validate agreed order shape and represent absent/untrusted physical evidence without granting motion or accessing a network. Test malformed orders, unknown robots, stale or inconsistent observations, and fail-closed behavior. Document proposal-only fields.
2. **Command and event ledger:** persist command identity, payload digest, send attempt and proposed responses; make duplicates idempotent, reject conflicting content or order, and preserve uncertain outcomes across restart. Test collisions, crash/reopen and concurrent attempts against real isolated SQLite files.
3. **Reservation and recovery boundary:** use only explicit evidence interfaces; atomically associate verified events, resource transitions and next reservations in isolated tests. Default to hold when evidence or recovery data is absent. Test contested resources and interruption at transaction boundaries.
4. **Isolated peer and MQTT scenarios:** connect a controlled fake robot to a disposable local broker, injecting loss, duplication, delays, reconnect and restart. Exercise the real server boundary and database, rather than only the fake.
5. **Action and inventory outcomes:** distinguish proposed pickup/load success, failure and uncertainty, deduplicate results, retain reservations when uncertain, and separate simulated Unreal acknowledgments from physical results. Exercise real persistence using fake outcomes.
6. **Operations and security:** prepare read-only status with explicit unknown battery and observation provenance; validate proposed per-device topic policies offline and exercise existing TLS/ACL handling without shared infrastructure.

Tasks are dependent where they share contracts or persistent state. Exact physical authentication, location, clearance, robot ACK and recovery wire semantics remain unagreed and must not be presented as implemented hardware behavior.
