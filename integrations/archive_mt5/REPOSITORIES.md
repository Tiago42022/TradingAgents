# Reviewed trading repositories

Reviewed on 11 November 2026 for the Archive integration. These are reference
projects, not runtime dependencies of the manual MT5 adapter. No strategy is
given permission to submit orders by this integration.

| Repository | Relevant use | Execution fit |
| --- | --- | --- |
| [TradingAgents](https://github.com/Tiago42022/TradingAgents) | Research framework; home of this optional adapter | The adapter is isolated from research outputs. |
| [Vibe-Trading](https://github.com/Tiago42022/Vibe-Trading/tree/67ce0ca5c9b2a3e3d321217eff043dad913a27fa) | Generic MT5 connector, candlestick and portfolio components | Useful comparison; no verified XM integration. Its MT5 path does not supply Archive's immutable review, SL/TP interface and durable uncertain-result fence. |
| [Kronos](https://github.com/Tiago42022/Kronos) | OHLCV forecasting and research backtests | Forecasts are research outputs, not broker orders or guaranteed outcomes. |
| [AutoHedge](https://github.com/Tiago42022/AutoHedge) | Multi-agent pipeline | The reviewed README documents Solana execution; it does not provide this XM connection. |
| [day-trader-skill](https://github.com/Tiago42022/day-trader-skill/tree/3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd) | US-stock research and explicit order-review concepts | Its broker script prepares Alpaca payloads; it does not execute MT5 orders. |

The manual adapter was compared with Vibe-Trading's `agent/src/trading/connectors/mt5/`
and `agent/src/live/sdk_order_gate.py`. Exact terminal/server/account binding,
fresh broker quotes, protection levels, single-send journaling and conservative
history reconciliation remain local requirements. No code from these additional
repositories is copied here. Their licences and any bundled notices need review
before future reuse.
