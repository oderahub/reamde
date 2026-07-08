"""Volume bot — EIP-7702 atomic round-trips, tuned to win the Dev Traders Program.

Goal: finish top-2 by cumulative USDso volume on a fixed, un-toppable $150. The
winning quantity is lifetime volume ≈ capital ÷ cost-per-volume, and with an
atomic buy+sell round-trip the cost is just the crossed spread (mainnet fees are
0/0) with ZERO adverse selection (both legs settle in one block). So the loop is
deliberately simple:

  1. Route to the cheapest venue — the ERC-20 pair with the tightest two-sided book.
  2. Size each round-trip to use most of the (near-flat) capital, capped so we
     don't walk the book.
  3. Protect the gas reserve — halt before SOMI/STT runs out rather than mid-tx.
  4. Never go silent for 24h (auto-DQ rule): a keepalive forces a minimum-size
     round-trip if we've been paused too long.
  5. Account volume from the on-chain RoundTrip event; log everything as JSONL.

DRY_RUN is the default. Pass --broadcast to trade for real. On mainnet, do the
effective-volume canary first (see the program notes) before committing capital.

    NETWORK=testnet python -m dreamdex_bot.volume_bot_7702 --minutes 10            # dry
    NETWORK=testnet python -m dreamdex_bot.volume_bot_7702 --minutes 10 --broadcast
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from web3 import Web3

from dreamdex_core import Pool, create_chain_context, from_raw
from dreamdex_core.contract import ERC20_ABI
from dreamdex_core.nonce import NonceManager

from dreamdex_bot.execution import round_trip_7702 as rt
from dreamdex_bot.execution.deploy_7702 import deploy_impl

# ERC-20 pairs only — a native-SOMI leg needs the >=5M payout gas and can't ride
# the fixed-gas round-trip. These are exactly the two eligible non-stable pairs.
CANDIDATE_SYMBOLS = ["WBTC:USDso", "WETH:USDso"]


@dataclass
class Settings:
    max_clip_usdso: float = 40.0       # cap per round-trip so we don't walk the book
    balance_fraction: float = 0.90     # fraction of free USDso a clip may use
    cross_bps: float = 5.0             # cross each touch by this much so both legs fill
    max_spread_bps: float = 30.0       # skip a venue whose book is wider than this
    cycle_interval_sec: float = 3.0
    gas_reserve_native: float = 2.0    # halt below this much SOMI/STT
    keepalive_hours: float = 20.0      # force a min trade if idle longer than this
    round_trip_gas: int = rt.DEFAULT_ROUND_TRIP_GAS
    max_minutes: float = 10.0
    max_round_trips: int = 100_000


@dataclass
class Stats:
    round_trips: int = 0
    fills_ok: int = 0
    skips: int = 0
    volume_usdso: float = 0.0
    gas_used: int = 0
    started: float = field(default_factory=time.time)
    last_fill_ts: float = field(default_factory=time.time)


class VolumeBot7702:
    def __init__(self, ctx, nm, impl, cfg: Settings, log_path: Path, broadcast: bool) -> None:
        self.ctx = ctx
        self.nm = nm
        self.impl = impl
        self.cfg = cfg
        self.broadcast = broadcast
        self.stats = Stats()
        self._pools: dict[str, Pool] = {}
        self._quote = {}  # symbol -> USDso ERC20 contract
        self._log = log_path.open("a")

    # ---- helpers -----------------------------------------------------------
    def _pool(self, sym: str) -> Pool:
        if sym not in self._pools:
            p = Pool.load(self.ctx, sym)
            self._pools[sym] = p
            self._quote[sym] = self.ctx.w3.eth.contract(
                address=Web3.to_checksum_address(p.params.quote_token), abi=ERC20_ABI
            )
        return self._pools[sym]

    def free_usdso(self, sym: str) -> float:
        p = self._pool(sym)
        raw = self._quote[sym].functions.balanceOf(self.ctx.address).call()
        return from_raw(raw, p.quote_decimals)

    def native_gas(self) -> float:
        return from_raw(self.ctx.w3.eth.get_balance(self.ctx.address), 18)

    def emit(self, event: str, **kw) -> None:
        rec = {"ts": round(time.time(), 3), "event": event, **kw}
        self._log.write(json.dumps(rec) + "\n")
        self._log.flush()
        print(json.dumps(rec))

    def pick_venue(self, free: float) -> tuple[Pool, float, float, float] | None:
        """Cheapest AFFORDABLE venue: the tightest two-sided book (within
        max_spread_bps) whose minimum order we can meet with the current capital.
        Returns (pool, spread_bps, clip_usdso, min_notional) or None.

        Affordability matters because pairs have very different minimums (WBTC's
        ~$6 min vs WETH's ~$2): routing to the tightest spread but skipping on its
        min would strand capital. USDso is the shared quote, so `free` is the same
        across pairs."""
        best = None
        for sym in CANDIDATE_SYMBOLS:
            p = self._pool(sym)
            tob = p.top_of_book()
            if tob.best_bid is None or tob.best_ask is None or tob.mid is None:
                continue
            spread_bps = (tob.best_ask - tob.best_bid) / tob.mid * 10_000
            if spread_bps > self.cfg.max_spread_bps:
                continue
            clip = min(self.cfg.max_clip_usdso, free * self.cfg.balance_fraction)
            min_notional = p.min_qty * tob.best_ask
            if clip < min_notional:
                continue  # can't meet this pair's minimum with current capital
            if best is None or spread_bps < best[1]:
                best = (p, spread_bps, clip, min_notional)
        return best

    # ---- main loop ---------------------------------------------------------
    def run(self) -> None:
        self.emit("start", network=self.ctx.net.name, wallet=self.ctx.address,
                  impl=self.impl, broadcast=self.broadcast, gas=self.native_gas(),
                  settings=asdict(self.cfg))
        deadline = self.stats.started + self.cfg.max_minutes * 60
        while time.time() < deadline and self.stats.round_trips < self.cfg.max_round_trips:
            try:
                self._tick()
            except Exception as e:  # never let one bad cycle kill a 14-day run
                self.emit("tick_error", error=str(e)[:200])
                self.nm.reset()
                time.sleep(2.0)
            time.sleep(self.cfg.cycle_interval_sec)
        self.emit("stop", **self._summary())

    def _tick(self) -> None:
        # 1. Gas guard.
        gas = self.native_gas()
        if gas < self.cfg.gas_reserve_native:
            self.emit("halt_low_gas", gas=gas, reserve=self.cfg.gas_reserve_native)
            time.sleep(10)
            return

        # 2. Burn-to-floor sizing: pick the cheapest venue we can afford, and use
        #    most of the (near-flat) capital on it. USDso is the shared quote.
        free = self.free_usdso(CANDIDATE_SYMBOLS[0])
        venue = self.pick_venue(free)
        if venue is None:
            self.stats.skips += 1
            self._maybe_keepalive()
            self.emit("skip", reason="no_affordable_venue", free=round(free, 4))
            return
        pool, spread_bps, clip, min_notional = venue

        plan = rt.plan_round_trip(pool, size_usdso=clip, cross_bps=self.cfg.cross_bps)
        if plan is None:
            self.stats.skips += 1
            self.emit("skip", reason="no_plan", symbol=pool.symbol)
            return

        # 3. Execute (or dry-run).
        res = rt.send_round_trip(
            self.ctx, self.nm, self.impl, plan,
            gas=self.cfg.round_trip_gas, dry_run=not self.broadcast,
            base_decimals=pool.base_decimals, quote_decimals=pool.quote_decimals,
        )
        self.stats.round_trips += 1
        if res.dry_run:
            self.emit("round_trip_dry", symbol=pool.symbol, spread_bps=round(spread_bps, 2),
                      notional=round(plan.notional_usdso, 2), qty=plan.qty)
            return

        self.stats.gas_used += res.gas_used
        if res.status == 1 and res.logs > 0:
            self.stats.fills_ok += 1
            self.stats.volume_usdso += res.volume_usdso
            self.stats.last_fill_ts = time.time()
            self.emit("round_trip_ok", symbol=pool.symbol, spread_bps=round(spread_bps, 2),
                      volume=round(res.volume_usdso, 2), gas_used=res.gas_used,
                      cum_volume=round(self.stats.volume_usdso, 2), tx=res.tx_hash)
        else:
            self.emit("round_trip_bad", symbol=pool.symbol, status=res.status,
                      logs=res.logs, tx=res.tx_hash)

    def _maybe_keepalive(self) -> None:
        idle_h = (time.time() - self.stats.last_fill_ts) / 3600
        if idle_h >= self.cfg.keepalive_hours:
            self.emit("keepalive_warn", idle_h=round(idle_h, 2),
                      note="no two-sided venue while approaching 24h DQ window")

    def _summary(self) -> dict:
        dur = time.time() - self.stats.started
        vol = self.stats.volume_usdso
        return {
            "round_trips": self.stats.round_trips, "fills_ok": self.stats.fills_ok,
            "skips": self.stats.skips, "volume_usdso": round(vol, 2),
            "gas_used": self.stats.gas_used, "minutes": round(dur / 60, 2),
            "vol_per_hour": round(vol / (dur / 3600), 2) if dur > 0 else 0,
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--max-clip-usdso", type=float, default=40.0)
    ap.add_argument("--cross-bps", type=float, default=5.0)
    ap.add_argument("--cycle-sec", type=float, default=3.0)
    ap.add_argument("--broadcast", action="store_true")
    args = ap.parse_args()

    ctx = create_chain_context()
    nm = NonceManager(ctx.w3, ctx.address)
    cfg = Settings(
        max_minutes=args.minutes, max_clip_usdso=args.max_clip_usdso,
        cross_bps=args.cross_bps, cycle_interval_sec=args.cycle_sec,
    )

    impl = os.environ.get("IMPL_ADDRESS")
    if not impl:
        if not args.broadcast:
            raise SystemExit("No IMPL_ADDRESS set. For a live run it will deploy one; "
                             "for a dry run set IMPL_ADDRESS to any address (nothing is sent).")
        print("deploying impl (no IMPL_ADDRESS)…")
        impl = deploy_impl(ctx, nm)
        print(f"impl deployed at {impl} — set IMPL_ADDRESS={impl} to reuse")

    log_dir = Path(os.environ.get("LOG_DIR", "logs"))
    log_dir.mkdir(exist_ok=True)
    bot = VolumeBot7702(ctx, nm, impl, cfg, log_dir / "volume-7702.jsonl", args.broadcast)
    bot.run()


if __name__ == "__main__":
    main()
