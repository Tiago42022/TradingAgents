# Archive manual MT5 adapter

This optional integration connects the user's TradingAgents repository to the
Archive's manually reviewed order queue. It uses only Python's standard library
and the separately installed official `MetaTrader5==5.0.6231` package. It does not
import the research graph, invoke a model, install the repository's LLM stack, or
turn research recommendations into orders. Importing the package starts nothing.

The existing Archive snapshot connector remains a separate read-only process.
This new adapter can place a **real market order**, but only after an active
Archive owner confirms its exact locally prepared review in the browser. No
agent-generated dispatch, scheduled trading, pending orders, position closing or
automatic top-ups are exposed. Never use a real order to test the integration.

## Setup

Use the existing 64-bit Windows MT5 venv and manually signed-in, approved terminal.
Broker passwords remain in that terminal. This adapter will not log in, switch
accounts, enable Algo Trading, or change Python trading permissions. Readiness
checks report those flags; the operator controls them.

Provision `mt5-trading.json` in the protected Door Key directory outside OneDrive
and every repository. Its exact fields are:

| Field | Value |
| --- | --- |
| `terminalPath` | Absolute path to the already running terminal EXE |
| `terminalDataPath` | Exact directory from this terminal's `terminal_info().data_path` |
| `expectedLogin` | Approved numeric account ID, as a string |
| `expectedServer` | Exact approved server |
| `connectionId` | Existing lowercase UUID v4 Archive connection ID |
| `token` | **New** execution bearer: 32 random bytes encoded as 64 lowercase hex characters |
| `endpoint` | `https://hollowheir.com/_archive/mt5/trading` |
| `journalDirectory` | Existing protected external directory dedicated to this connector |
| `manualTrading` | Boolean; only `true` permits confirmed execution |

The directory and config require protected current-user-only FullControl ACLs.
Journal files inherit only that user's access. The bundled read-only verifier
runs with process-local `RemoteSigned`; it changes no execution policy or ACL.
Reparse paths, OneDrive paths, repository paths and unverified ACLs are rejected.
Keep the raw bearer here only. Provision its SHA-256 digest as the hosted
`ARCHIVE_MT5_TRADING_TOKEN_SHA256` and explicitly enable the server's
`ARCHIVE_MT5_MANUAL_TRADING=enabled` only after validation. Existing account,
connection and owner bindings must match. Do not replace the monitoring token.

From any working directory, use the absolute `run.py` path:

```powershell
& $connectorPython -I -u $adapterRunPy --config $protectedTradingConfig --check
& $connectorPython -I -u $adapterRunPy --config $protectedTradingConfig --run
```

`--check` is read-only and prints only a masked account, matching status, currency,
and permission flags. **`--once` is a real queue cycle and can execute an already
confirmed order**; it is not a harmless connectivity test. Do not run two copies.
Use the same journal across restarts. For a hidden process use Windows
`Start-Process -WindowStyle Hidden`, redirect redacted output outside OneDrive,
and retain its specific PID/creation time for stopping. Do not terminate MT5 or
all Python processes. There is no auto-start service or scheduled task.

## Execution and recovery

Catalog and preflight commands can never send an order. Each ready review and
the exact broker request are saved locally before acknowledgement. A confirmed
command must match that local review hash, account and expiry. The adapter then
rechecks quote/specification/exposure, permissions, price deviation, margin and
broker preflight. Netting accounts require the selected symbol to be flat;
pending orders on that symbol are rejected. Hedging exposure must remain as
reviewed. Stops are never silently removed or rounded.
The exact user-chosen broker symbol may be added to MT5 Market Watch so its tick
stream becomes available. Catalog exploration alone changes no symbol selection.

A Windows OS lock prevents competing local executors. SQLite WAL with
`synchronous=FULL` commits the attempt reservation before the sole `order_send`
call. Any crash after reservation is considered uncertain and is never replayed.
This provides at-most-one application send attempt, not broker exactly-once
execution. Broker acceptance is distinct from a fill; partial fills are retained
and never topped up. Market execution does not guarantee a maximum fill price.

Results are durably queued for acknowledgement. Restart recovery repeats only
result acknowledgements and read-only history queries. Reconciliation requires
positive matching broker order/deal evidence; missing history stays unknown.
An unresolved order keeps the account queue blocked. A crash before sending may
therefore leave an unknown intent that requires operator review. Do not delete
the journal or invent a replacement order to clear that state. `fillPrice` from
history is the last actual fill price, not an average invented by this adapter.

The loop sends heartbeats every roughly two seconds between broker operations.
A blocked terminal call can delay that heartbeat, so the website must expire
readiness. Output reports state changes, not an execution heartbeat. The adapter
has no inbound listener or arbitrary remote method/shell dispatcher. HTTPS uses
only the fixed Archive host, verifies certificates, ignores proxy environment
variables and refuses redirects.

## Offline verification

From the repository root, run with the pinned venv (no MT5 initialization):

```powershell
& $connectorPython -m unittest integrations.archive_mt5.test_adapter -v
```

All broker objects in this suite are fake. Tests cover single send, lost replies,
crash reservations, account binding, permissions, expiry, price/stops/volume,
filling flags, exposure, partial/unknown outcomes, reconciliation, persistence
failure, singleton and fixed transport. No live order is part of verification.

Path/identity/ACL checks are adapted from the user's Archive read-only connector
and `scripts/door-key-acl.ps1` (9 November 2026). This optional adapter is supplied
under this repository's Apache-2.0 license.
