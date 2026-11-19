# Operations Dashboard Implementation Plan

**Goal:** Improve the existing simulated web demo with a responsive, audience-readable operations dashboard while preserving ordering and inventory behavior.

**Architecture:** Reuse the existing FastAPI order-demo status, products, orders, inventory and node endpoints. Show each response's observed scope and availability; add a narrow read-only map/occupancy endpoint only when its source and lifecycle can be validated. Never treat a planned route as a reservation or stale observations as current physical facts.

**Tech Stack:** Python, FastAPI, inline HTML/CSS/JavaScript, unittest and browser rendering.

## Constraints

- Keep existing order submission, idempotency, delayed/unknown outcomes and inventory fencing behavior.
- Preserve simulation-only controls, opt-in field mapping and source labels; no physical commands.
- Mark unavailable, stale and unknown data, do not invent timestamps, reasons or stock.
- Responsive desktop and mobile layouts with keyboard-visible focus and readable status labels.
- Use the existing checkout, preserve user changes, test and push feature commits to `feature/server-communication`.

## Tasks

1. Audit the actual status/inventory/map and reservation sources and their snapshot boundaries. Identify missing fields and avoid pretending planned routes are reserved occupancy.
2. Write behavior-focused tests for added data projections and missing/stale/error states. Build the dashboard layout, order flow, robot cards, current-stage indicators and inventory using existing contracts.
3. If a safe read-only map/occupancy view can be provided, implement it separately with route/occupancy provenance and lifecycle checks, then integrate without blocking ordering or fabricating reservations.
4. Run existing UI/order safety tests and actual loopback browser checks at desktop/mobile widths; inspect empty, pending, stale, error and unknown states. Review, commit, push, and record any unsupported first-version features.
