# Team Shelf Recovery and Simulated Rerouting

**Goal:** Restore completed team-shelf orders and inventory safely after a server restart, and implement safe simulated route changes while retaining robot/segment evidence.

**Constraints:** Preserve user data; never connect to physical hardware, command motors, release unproven physical occupancy, or infer real stock from the synthetic twin. Existing v3 map, shared-pickup control and isolated MQTT workflow must continue to work.

- [x] Verify same-world ownership, persistent request IDs, order and 24-slot storage, last robot pose/yaw/carrying and completed action evidence. Incomplete or unknown work remains blocked; if the slot DB committed immediately before interruption, the claim may already be released and all stock is reported unknown instead.
- [x] Exercise completed, interrupted and uncertain cases including duplicate submissions, mismatched worlds and startup failures; test server restart with real Unreal and a dedicated local broker/database.
- [x] Add a simulated route-generation transition preserving both old and new occupancy atomically; validate directions, owner, session, old/new event lineage and robot-side acceptance without treating PUBACK as acceptance.
- [x] Test two simulated robots through a conflict, accepted reroute and completion; exercise disconnects, duplicates, restart and ambiguous publishing at tested boundaries.
- [x] Compare current robot team evidence and resync code, carry out isolated interoperability tests, and retain physical control blocks where hardware evidence or agreed contracts are missing.
- [x] Run relevant regressions, commit features separately and push to the server-communication branch.

The team v3 web reroute test uses a synthetic world, test-selected starting positions and an artificial simultaneous-dispatch barrier. A reroute in a rendered Unreal scene and physical devices remain unverified. After a rerouted handoff, restarting the server is deliberately blocked; use the read-only investigation described in `server/docs/team_shelf_recovery_inspection.md`. Continuous reservation waits are bounded to 120 seconds; if a proof or acknowledgement becomes uncertain or expires, work remains blocked rather than being retried.
