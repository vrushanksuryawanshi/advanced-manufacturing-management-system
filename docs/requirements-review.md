# Review of the supplied manufacturing plan

Source: `ChatGPT-Manufacturing Web Application Design-20260924-1159 (1).pdf`, 35 pages, read in full. It is a prior design conversation, treated as product reference material rather than instructions to execute. References to an earlier PPT inside it are not independently verified because that PPT was not supplied.

The user's additional requirements are responsive use by all three roles and automatic unique QR generation after manager setup. This implementation uses a browser application for all devices, with a shared backend and database. One QR belongs to one lot, not one route step or machine. A second lot of the same part receives a different QR.

| Plan area | First-version implementation | Boundary / next step |
| --- | --- | --- |
| Authentication and roles (§4, 37) | Password hashing, sessions, CSRF, server-side role checks, station assignments, client ownership filtering | Account recovery/deactivation and assignment editing not yet exposed |
| Multiple orders (§5) | Multiple concurrent orders and lots, quantities, priority, due dates | No order cancellation or post-creation quantity editing |
| Part codes and routes (§6–9) | Create part, reuse route, create/revise preserved versions, locked order snapshots | No drawing/document attachments |
| Manager estimates (§11–12) | Per-step minutes, transition allowance and instructions in each route version | No automatic quantity scaling or setup/per-unit breakdown |
| Machine scheduling (§10, 15–17, 32–34) | Capacity-aware forecasts, route precedence, priority queues, queue cards | Sequence view rather than proportional Gantt; continuous availability, no shift calendar |
| Process overrides (§18–20) | Prioritize a waiting operation and its pending ancestors; capture reason, actor, old/new queue position and timestamp | No arbitrary position drag-and-drop; physical machine control is outside scope |
| QR traceability (§25, 29, 36) | Unique unguessable lot token generated on confirmed order creation, printable labels, camera/image/manual lookup | QR is identity only; authenticated operator explicitly confirms each event |
| Operator tracking (§4, 20–21) | Station authorization, start/complete timestamps, operator identity, no skipped steps or double starts | No shift handoff or pause/resume model |
| Quality and storage (§29, 36) | QC pass/fail with notes, hold blocks next step, manager-authorized reinspection, required warehouse location | No multi-step rework route or inventory dispatch module |
| Utilization (§13–14, 27) | Recorded busy minutes, trailing 24h utilization, waiting workload, station availability | Not shift-adjusted OEE; no downtime categories |
| Planned vs actual (§12, 21–23) | Immutable baseline, current forecast, actual duration, schedule start/end deltas, historical deviation | Expanded statistical charts/export remain future work |
| Historical analysis (§6, 22, 24, 28) | Completed comparable-operation means, five-sample threshold, preserved versions | Quantity and route-version matching is deliberately strict; richer trends and most-used-route reports can follow |
| ETA (§24–25, 35) | Remaining route, station queues, transitions, QC and storage included; forecasts recalculate | Heuristic, not ML; manager approval publishes client ETA |
| Client portal (§4, 35) | Own orders only, part, quantity, current stages, progress, approved ETA, final status | No internal queues, employee details or other clients' orders |
| Timeline (§29) | Order/route/QR, operator starts/completions, quality, overrides, ETA approvals and storage events | Latest 100 events in manager UI; complete events remain in database |
| Responsive/mobile (user addition) | Responsive navigation, cards, forms, mobile scan flow, rear-camera request and image/manual fallback | Real iOS/Android camera testing needs a trusted HTTPS deployment |
| Future ML (§1, 37) | Real timestamps and comparable historical data foundation | ML intentionally not represented as implemented without a training dataset |

## Decisions that need real factory information later

1. Working days, shifts, breaks and holidays. The current scheduling assumption is continuous availability.
2. Whether estimates are per piece, per lot, or setup plus processing. This version treats them as per lot.
3. Whether a QC failure needs only reinspection or a separately approved rework route.
4. The production hostname/hosting environment for HTTPS and physical phone camera trials.
5. Who may change queues, approve client ETAs and authorize reinspection in actual use.

These boundaries are documented to keep the first version honest and testable; the application already supports an end-to-end manager → operator → QC → warehouse → client demonstration.
