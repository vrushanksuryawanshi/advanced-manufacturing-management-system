# ForgeFlow — Advanced Manufacturing Management System

A working first version of the application described in the supplied 35-page plan. It has a shared SQLite database, a Python/Flask backend, and responsive manager, operator and client workspaces. It is not connected to physical machine controllers.

## Start on this computer

Dependencies have been installed in this workspace. Open **Start Demo.bat**, then visit **http://127.0.0.1:5000**. Keep the terminal window open while using the application. If a server is already running on port 5000, use that browser address instead of starting another copy.

On a new computer, install Python 3.11 or newer, run **Setup.bat** once, then **Start Demo.bat**. The QR scanner is already included in `static/vendor`; Node.js is only needed for browser tests, not to run the application.

Demo accounts (all use password **FactoryDemo123!**):

| Role | Email |
| --- | --- |
| Manager | manager@demo.local |
| Operator | operator@demo.local |
| Client | client@demo.local |
| Second client, to demonstrate isolation | other-client@demo.local |

Demo data is created only when `--demo` is used on an empty database. Starting again preserves your work. Sample orders start waiting; utilization and performance are calculated from recorded activity, not fabricated sample measurements.

## Try the full workflow

1. Sign in as the manager. In **Team & access**, create client accounts; the app emails each client their email address and generated temporary password. Staff register from the sign-in page as a **Machine operator**, **Quality checker**, or **Warehouse manager**. Each role sees only the appropriate stations, and a manager must approve the registration.
2. Add machines in **Machines**, including a quality inspection station and a storage station.
3. Add a product in **Products & routes**. Open **Build route** and click machines on the floor in production order, then QC, then a warehouse. Select an available warehouse address, such as **1/4**. Set minutes per lot, transition minutes, and work instructions. Save the route; opening the product shows its saved path and highlights the selected stations.
4. In **Production orders**, choose **New order**, select the client and route version, and enter quantity, units per lot, priority and required date.
5. Choose **Confirm route & generate QR**. The system creates an immutable route snapshot, divides the quantity into lots, generates one unique QR for each lot, and opens printable labels. The final lot may be smaller.
6. Print and attach the labels. The **same label follows the lot throughout its entire route**. It identifies the lot; it does not automatically start or finish work.
7. Sign in as the operator on another browser or device. Choose **Scan a lot**. Scan the label, scan a saved image, or enter its printed lot code. Review the instructions and choose **Start operation**. Scan again after performing the work and choose **Complete operation**.
8. Each machine operator handles their assigned machine. The quality checker records inspection results. A failed QC result puts the lot on hold; the manager must authorize reinspection. After QC passes, the warehouse manager scans and receives the lot at its reserved address.
9. The manager can open **Machine schedule**, select a waiting operation, and prioritize it with a reason. All downstream forecasts update. Running work is never interrupted.
10. Open an order and choose **Approve client ETA**. The client sees current progress and the most recently approved completion estimate. Approve a revised ETA when a forecast changes materially.

## Factory floor and warehouse addresses

**Factory floor** shows a schematic equipment layout, not a measured building plan. The demo includes M1–M12, QC, and WH. Managers can use **Add station** to add any further machine codes, inspection stations, or warehouses; there is no M12 limit. The layout expands automatically.

**Products & routes → Open product** shows the full saved sequence, highlighted equipment, step instructions, and warehouse address. **Revise route** creates a new version. Existing orders retain their confirmed steps and warehouse destination.

Warehouses start with three rows of ten addresses: **1/1–1/10**, **2/1–2/10**, and **3/1–3/10**. Use **Warehouse → Add row** to extend a warehouse. The availability view distinguishes available, reserved, and occupied addresses.

A saved product route specifies a preferred address. Creating an order reserves an available address for all its lots; if the preferred address is taken, choose another. The server prevents two orders reserving the same address, including simultaneous submissions. Addresses become occupied when stock is received. The assigned warehouse manager can open the address and enter a dispatch reference to release it after every lot in that order has been stored. This tracks one order per address; physical bin capacity and partial dispatch quantities are not modeled.

Older orders that predate address reservations retain their existing location workflow. Their historical locations are not automatically converted into reserved addresses.

## Use it on a phone

The interface adapts to small screens: mobile navigation, stacked cards/forms, touch-sized controls, rear-camera scanning, and horizontally scrollable station schedules. All devices must connect to **the same server** to share data.

For a local network demonstration, run:

```powershell
python app.py --demo --host 0.0.0.0
```

Connect the phone to the same Wi-Fi, find the computer's IPv4 address with `ipconfig`, and open `http://YOUR-COMPUTER-IP:5000`. Windows may ask you to allow the server on your private network. Manual code entry and image upload work for this local demonstration.

**Live phone camera scanning needs a trusted HTTPS address.** A plain HTTP LAN address is not a secure context. Deploy the app behind HTTPS before using live camera scanning on phones. Localhost is allowed for development on the computer itself. See [MDN's camera requirements](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia) and the [QR scanner documentation](https://scanapp.org/html5-qrcode-docs/docs/intro). A browser needs the user's camera permission; the camera is stopped when scanning succeeds or the dialog closes.

No QR image or camera frame is sent to an external QR service. QR generation runs in Python and image decoding runs locally in the browser.

## Scheduling and analytics assumptions

- Stations have **continuous 24/7 availability**, one active lot per station, and no shifts, weekends or holiday calendar yet.
- Due dates are interpreted in India Standard Time (UTC+05:30) by default. Set `FACTORY_UTC_OFFSET_MINUTES` for another fixed factory offset. Timestamps are stored in UTC and displayed in the viewer's local time. Daylight-saving shift calendars are not implemented.
- Route estimates are **minutes per lot**, not per unit. A smaller final lot uses the same estimate. Create a suitable route version for different production quantities.
- Scheduling is a deterministic serial heuristic using explicit overrides, order priority, creation order, route precedence, machine capacity and transition allowances. It is not a mathematically optimal shop-floor optimizer. It may reserve a station for an earlier-priority lot arriving later.
- A priority override promotes the selected operation and its waiting predecessor steps. It does not preempt active work. Previous/new positions, actor, reason and time are audited.
- Operators cannot start an operation before its predecessors finish, bypass the selected station's queue, or start on an occupied or unavailable station. Transition estimates guide forecasts; they are not enforced as mandatory minimum travel times.
- Original planned timestamps stay fixed. Current forecasts recalculate as time and production change. An overdue running operation is forecast to finish at least one minute from now until the operator completes it; no unearned predictive accuracy is claimed.
- Held routes and unavailable stations have no forecast finish until resolved. Other independent routes can still run.
- Historical benchmarks require five prior completed observations for the same part, **route version**, machine, route position and lot quantity. A completed operation excludes itself from its benchmark. This avoids grading a process from insufficient or incompatible data.
- Process deviation and schedule delay are separate. Historical deviation is green below −5%, orange from −5% through +5%, and red above +5%. A running operation's elapsed duration is provisional.
- Waiting/transition time is the elapsed time from the previous completion (or order creation for the first step) to actual start. It cannot distinguish physical travel from idle queue time without additional arrival scans.
- Utilization is actual busy minutes in the trailing 24 hours divided by 1,440 available minutes; prior failed QC attempts remain included. It is not shift-based OEE.
- Views refresh every 20 seconds while idle. Forms and scanner dialogs are not replaced by background refreshes. Start/complete decisions are always revalidated by the server.
- Clients see the last **manager-approved ETA**, even if an internal forecast changes. Their production progress updates independently.

## Data and accounts

The database is `instance/manufacturing.db`. The persistent session signing key is `instance/session-key`. Neither is included in source control. SQLite transactions serialize production changes; a database constraint additionally prevents two active lots on one station.

For a clean installation without demo accounts, use a separate copy of the project **without an `instance` folder**, install dependencies, and run:

```powershell
python app.py --init-manager
```

Create your real manager interactively. Operators can register at the sign-in page, but cannot sign in until a manager approves them and assigns one machine. Managers create client accounts; the app emails their login address and generated temporary password. Configure outbound SMTP before creating clients by setting `SMTP_HOST`, `SMTP_PORT` (usually `587`), `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_FROM` in the server environment. The mail server must support STARTTLS. If email delivery fails, account creation is rolled back. Managers are not created from the public registration page. Do not expose the demo database publicly. For HTTPS hosting set `COOKIE_SECURE=1`, keep the signing key stable and private, and run behind a configured HTTPS reverse proxy. No public deployment has been performed.

Before changing or moving data, stop the server and back up the entire `instance` folder. The current version does not include password recovery, account deactivation, or an administration UI for changing existing operator assignments.

## Validation

Backend workflow tests use isolated databases under `tmp/tests`:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Browser tests use an isolated server on port 5001 and installed Microsoft Edge:

```powershell
npm install
npm run test:ui
```

Tests cover permissions, client isolation, CSRF, concurrent starts, route precedence, queue overrides, unique QR labels, route snapshots, QC failure/reinspection, storage, schedule capacity, historical thresholds and responsive browser workflows. Browser tests also decode a generated QR image. Screenshots are written to `tmp/screenshots`. Real phone cameras, printers, and a deployed HTTPS environment still need device acceptance testing.

## Remaining extensions

See [the requirements review](docs/requirements-review.md) for how the PDF maps to this version. Future work includes shift calendars, a proportional time-axis Gantt editor, machine-specific drag-and-drop queue editing, richer historical reports/export, planned downtime and setup models, notification delivery, account recovery, production deployment/backup automation, and ML prediction after sufficient real data exists.
