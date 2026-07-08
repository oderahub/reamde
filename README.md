<!-- # dreamdex-bot

Trading and QA bot for the DreamDEX alpha competition on Somnia.

## What this is

A focused Python bot that runs three concurrent strategies on DreamDEX during the 7-day alpha:

- **Volume mill** — Wallet-funded IOC ping-pong on USDC.e/USDso. Both sides are pegged to ~$1, so inventory risk is near-zero and the bot can cycle capital fast to climb the volume leaderboard.
- **Yield maker** — Vault-funded PostOnly quotes on SOMI/USDso with an Avellaneda-Stoikov-style reservation price that shifts with inventory. Earns Gaussian-weighted collateral yield without bleeding to adverse selection.
- **QA prober** — Off the main loop. Deliberately probes the protocol's edge cases (tick precision, STP, PostOnly-crosses, FOK undersize, rate limit ceiling, WS reconnect drift, silent rejection path) and logs every request/response pair as evidence for feedback reports.

A live risk manager evaluates pluggable rules on every tick and can pause, reduce, or kill the bot if conditions degrade.

## Architecture

```
                    ┌─────────────────────┐
                    │   WebSocket Feed    │  orderbook / trades / own orders
                    └──────────┬──────────┘
                               │
                               ▼
       ┌─────────────────────────────────────────────┐
       │              MarketState                    │  in-memory book + own orders + inventory
       └──────────┬──────────────────┬───────────────┘
                  │                  │
       ┌──────────▼──────┐  ┌────────▼────────┐
       │  Strategies     │  │  Risk Manager   │  pluggable rules → RiskEvent[]
       │  (3 loops)      │  │                 │
       └──────────┬──────┘  └────────┬────────┘
                  │ signals          │ actions
                  └─────────┬────────┘
                            ▼
                  ┌──────────────────┐
                  │  Order Executor  │  serialized nonce queue + prepare-tx via REST
                  └────────┬─────────┘
                           ▼
                  ┌──────────────────┐
                  │  Somnia RPC      │
                  └──────────────────┘
```

## What was extracted from prior art (with attribution)

The architecture takes ideas (not code) from three reference projects, none of which target DreamDEX directly:

- [`chainstacklabs/hyperliquid-trading-bot`](https://github.com/chainstacklabs/hyperliquid-trading-bot) (Apache 2.0): pluggable `RiskRule` base class, `RiskEvent` with action enum, the `TradingStrategy.generate_signals()` interface, YAML-driven config.
- [`Polymarket/poly-market-maker`](https://github.com/Polymarket/poly-market-maker) (MIT): "Bands" strategy state machine, the `get_orders(book, target_prices) -> (cancels, places)` two-list return pattern, free-collateral accounting.
- [`Drakkar-Software/OctoBot-Market-Making`](https://github.com/Drakkar-Software/OctoBot-Market-Making) (GPLv3, *patterns only*): the dual-loop volume-vs-inventory architecture.

The Avellaneda-Stoikov reservation price for the yield maker is from Avellaneda & Stoikov (2008), "High-frequency trading in a limit order book."

## Setup

```bash
# Clone and enter
cd dreamdex-bot

# Install
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your fresh competition wallet's address and private key

# Run on testnet (default)
python -m bots.main --config configs/testnet.yaml

# Run on mainnet (Monday 10am UTC)
NETWORK=mainnet python -m bots.main --config configs/mainnet.yaml
```

## Safety

- The competition wallet must be freshly generated and never used for anything else.
- `.env` is in `.gitignore`. Never commit private keys.
- Realized-loss circuit breaker defaults to -$12.50 (25% of starting $50). Adjust in `.env`.
- The bot ships with `ENABLE_QA_PROBER=false`. Probes are run as separate scripts under `src/dreamdex_bot/probes/` to keep the main loop deterministic.

## Layout

```
src/dreamdex_bot/
├── config.py           # All settings, market metadata, contract addresses
├── interfaces/         # Strategy and Risk interfaces (extracted from chainstack)
├── core/
│   ├── signer.py       # web3.py signer with serialized async nonce queue + RBF reconciler
│   ├── rest_client.py  # httpx async client + SIWE auth + JWT auto-refresh
│   ├── ws_client.py    # websockets reconnect + heartbeat
│   ├── risk_manager.py # RiskManager + concrete rules
│   └── engine.py       # Main loop orchestrator
├── strategies/
│   ├── volume_mill.py  # USDC.e/USDso IOC taker ping-pong
│   └── yield_maker.py  # SOMI/USDso PostOnly with A-S reservation price
├── probes/             # Standalone QA probe scripts (one file per probe)
└── utils/
    ├── logger.py       # Structured JSONL logging
    └── markets.py      # Tick/lot rounding helpers
``` -->
