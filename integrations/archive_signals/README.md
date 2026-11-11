# Archive Gold M30 signal collector

This optional Windows process publishes **closed, raw M30 candles** from the
operator's already signed-in MT5 terminal to Archive. The Gold room owns the
monitoring toggle and exact broker symbol. Archive evaluates the strategy and
offers human review or dismissal of a setup. This collector does not execute,
prepare or close orders and does not import the manual trading adapter.

The pure rule and its offline tests live in `../archive_strategies`: at least
three red Heiken Ashi candles followed by the first closed green produces an
entry observation; the first subsequent closed red produces an exit observation.
A virtual entry is pattern state, not proof that the user opened a position.
Only M30 is supported. Synthetic Heiken Ashi prices are not execution quotes.

## Operator-controlled installation

Use a dedicated Windows Python environment with the official `MetaTrader5`
package installed. Keep the approved terminal open and sign in there yourself.
No broker password is needed by this process. Algo Trading permission is not
needed to collect candles.

Create a separately protected external file named `mt5-signals.json`. The file
and parent directory must be owned by the current Windows user, have inheritance
disabled, and have only that user's FullControl ACL entry. Keep it outside any
Git repository, OneDrive or other shared/synchronized directory. The loader
rejects reparse paths and rejects extra fields (including passwords).

| Field | Meaning |
| --- | --- |
| `terminalPath` | Operator-approved absolute path to the already-running `terminal64.exe` |
| `terminalDataPath` | Exact approved terminal data directory |
| `expectedLogin` | Numeric account login as a string |
| `expectedServer` | Exact broker server name |
| `connectionId` | Archive connection UUID |
| `token` | Independent read-only signals device bearer: 64 lowercase hex characters |
| `endpoint` | Exactly `https://hollowheir.com/_archive/mt5/signals` |

Provision the signals bearer separately. Never reuse the execution bearer or
put these values in repository files, command arguments, screenshots or logs.
Account credentials and execution configuration are deliberately not accepted.

Start explicitly, using absolute paths held in your own PowerShell variables:

```powershell
& $signalPython -I -u $signalRunPy --config $protectedSignalConfig --probe
& $signalPython -I -u $signalRunPy --config $protectedSignalConfig --run
```

Here `$signalRunPy` points to this directory's `run.py`. `--probe` verifies the
terminal/account binding only: it makes no HTTP requests and reads no candles.
`--run` polls settings every 15 seconds, or waits 30 seconds after a transient
transport failure or a conflicting snapshot. A conflict causes a fresh settings
read on the next poll, never a replay of the rejected POST. Stop with Ctrl+C. No scheduled task, Windows service or
startup entry is installed. These commands have not been run against a live
terminal as part of the offline test suite.

The adapter refuses initialization when the exact terminal process is not
already running. MetaQuotes' initialization API can itself launch a terminal if
it exits during that process check; there is no attach-only flag. The collector
does not explicitly launch a process, submit a login, change permissions, or
change Market Watch. If history for the exact selected symbol is unavailable,
open its M30 chart in the approved terminal and retry.

## Wire contract and failure behavior

GET and POST use `Authorization: Bearer <signals-device-token>` against the fixed
HTTPS endpoint. Redirects are rejected; no configurable host or command endpoint
exists. Responses and credentials are never printed in error messages.

GET settings:

```json
{"schema":1,"connectionId":"connection-uuid","enabled":false,"symbol":null,"timeframe":"M30"}
```

When enabled, `symbol` must be an exact Gold identifier: `GOLD` or `XAUUSD`, with
broker suffixes such as `#`, `.m`, `micro`, `mini` or `m`. The monitor uses that
string verbatim, checks the symbol's returned name, and never guesses aliases.
When disabled it only polls settings; it closes its terminal attachment and
does not read candles or POST a snapshot.

POST snapshot:

```json
{
  "schema":1,"connectionId":"connection-uuid",
  "accountLogin":"numeric-login","server":"exact-approved-server",
  "capturedAt":1800010800200,"sequence":1800010800200,
  "symbol":"GOLD","timeframe":"M30","status":"ready",
  "bars":[{"time":1800000000,"open":"4000.0","high":"4003.5","low":"3998.0","close":"4001.25"}]
}
```

The example shows the field shape; a ready capture contains at least four and
at most 256 bars. Timestamps `capturedAt` and `sequence` are UTC milliseconds;
bar `time` is the interval's opening UTC seconds, aligned to a 30-minute boundary.
OHLC values are the decimal round-trip representation of MT5's raw floats.

- Verify executable directory, terminal data directory, account and server before
  and after every candle capture. Discard the capture on a switch/disconnection.
- Request only `TIMEFRAME_M30`, filter current/future bars by actual close time,
  and retain only the latest contiguous segment after a missing interval/weekend.
  Do not invent candles or bridge a gap to create a setup.
- Reject duplicate/reversed timestamps, invalid OHLC, oversized histories, slow
  captures and clock rollback. A candle closing during a read is excluded until
  the next poll, so a partly collected interval cannot trigger a setup.
- Missing/short history or an unavailable/mismatched terminal posts
  `status: "unavailable"` with `bars: []`. Such a snapshot carries the approved
  configured account binding, not data read from a different account.
- Polls intentionally resend observed history to refresh connection health.
  The server owns activation baselines, signal deduplication, signal expiry,
  notification delivery and Review/Skip authorization. The collector never
  emits a setup, an execution instruction or an entry/exit retry.
- `sequence` is the capture timestamp, increasing within the process. Restart
  uses the current timestamp. A wall clock behind the server's accepted sequence
  fails closed; correct the computer time before restarting. A failed request
  does not queue stale histories for later replay.
- The server must revalidate that monitoring is still enabled and that the
  symbol/binding match before accepting a snapshot, including an in-flight POST
  that arrives after the user turns monitoring off.

## Offline verification

From the repository root, use any available Python 3.11+ interpreter:

```powershell
& $testPython -m unittest integrations.archive_strategies.test_heiken_ashi_m30 integrations.archive_signals.test_monitor -v
```

Tests use a fake terminal and fake HTTP transport. They cover candle boundaries,
gaps, decimal preservation, exact symbol selection, account switches, disabled
monitoring, replay/restart snapshots, connection failures, malformed responses,
credential handling and a static allowlist of read-only terminal methods. No
real broker credentials or network access are needed.

MetaQuotes documentation: [copy_rates_from_pos](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py)
and [initialize](https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py).
