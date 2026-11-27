# Team Shelf Integration Implementation Plan

> Execute in the existing checkout while preserving the four untracked Unreal tools.

**Goal:** Connect web orders to the existing simulated fleet and MQTT reservation workflow and slot-specific Unreal pickup and placement on the v3 shelf layout.

**Architecture:** Keep physical controls disabled. Bind slot claims and correlated Unreal actions to the existing simulation task/session, and retain claims on ambiguous cross-store or transport outcomes. The team's design describes MQTT for the twin; this local simulation follows the already-established WebSocket twin interface and does not change the physical protocol.

**Tech Stack:** FastAPI, SQLite, isolated MQTT, Python tests, Unreal Engine 5.7 WebSocket.

**Spec:** Existing user request, `server/docs/handoff_2026-11-26.md`, and repository `AGENTS.md`.

## Steps

- [ ] Preserve and package the four-product Unreal scene changes and independently verify slot snapshots, pickup, placement and rejected actions.
- [ ] Add failing integration tests for existing web admission, unique slot claims, existing fleet dispatch, correlated actions, completion and uncertain/disconnected cases.
- [ ] Connect those boundaries without allowing unconfirmed pickup, duplicate commands or slot release, and run targeted existing and new regression tests.
- [ ] Run isolated MQTT and actual Unreal end-to-end tests; record precise limits of virtual coordinates, inventory, assets and physical verification.
- [ ] Connect synthetic shelf observations and approval to the integrated path when the new owner/session boundary supports it; test missing/stale observations.
- [ ] Review separately the already identified physical contract, simulated replan, and production security gaps; do not infer physical safety from simulation.
- [ ] Commit and push completed, verified features separately to `feature/server-communication`.
