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
from dreamdex_core.contract import ERC20_ABI, read_book_levels
from dreamdex_core.execute import PlaceParams, place_order
from dreamdex_core.gotchas import OrderType, build_expire_ns
from dreamdex_core.nonce import NonceManager
from dreamdex_core.quant import align_to_lot, align_to_tick, to_raw

from dreamdex_bot.execution import round_trip_7702 as rt
from dreamdex_bot.execution.deploy_7702 import deploy_impl

# ERC-20 pairs only — a native-SOMI leg needs the >=5M payout gas and can't ride
# the fixed-gas round-trip. These are exactly the two eligible non-stable pairs.
CANDIDATE_SYMBOLS = ["WBTC:USDso", "WETH:USDso"]


@dataclass
class Settings:
    # Sizing. Counter-intuitively NOT "as large as possible": a live clip-size sweep
    # (12 samples x both books, net = book-walk + our fixed 2.5M-gas round-trip
    # amortized over the clip) found a U-shaped net cost-per-volume with the minimum
    # at ~$40-50. Above ~$65 the book-walk dominates (thin touch); below ~$30 our
    # heavy atomic-tx gas dominates. $80 clips cost ~0.5bp/vol MORE than $50. Capping
    # here at $50 (the sweet spot) trades ~1.6x more round-trips for ~0.5bp better
    # efficiency, lifting our capital-bound ceiling ~$1.25M -> ~$1.6M. This is why
    # trader-3 (~$30 clips) out-efficiencies us; our optimum sits above theirs
    # because our atomic round-trip is gas-heavier than their single-leg orders.
    max_clip_usdso: float = 50.0
    balance_fraction: float = 0.95     # fraction of free USDso a clip may use
    # Prefer this venue by this bps margin: WBTC is deep + always tight + big-clip,
    # so route there unless another pair is cheaper by more than the bias (WETH's
    # tiny clips cost the same gas for ~4x less volume).
    preferred_symbol: str = "WBTC:USDso"
    preferred_bias_bps: float = 1.5
    depth_fraction: float = 0.50       # never clip more than this share of the depth fillable at our cross limit
    cross_bps: float = 5.0             # cross each touch by this much so both legs fill
    # STRICT SPREAD GATE (the "only trade when cheap" rule). Spread IS our cost, so
    # skip any book wider than this — no momentum/trend gate (atomic round-trip =
    # flat inventory, so trend is irrelevant). WBTC ~1.3bp, WETH ~2-5bp normally.
    # This is only a cheap PRE-FILTER on the QUOTED spread; the real gate below is
    # size-aware. Tightened 3.0 -> 2.5.
    max_spread_bps: float = 2.5
    # EFFECTIVE-COST GATE (the substantive fix). Quoted spread understates cost when
    # the touch is hollow: a book can quote <2.5bp yet cost 4+bp after our clip walks
    # it (bid-side touch collapsed to ~$23 during the July wide-market stretch). So
    # gate on the VWAP round-trip cost for the ACTUAL clip: buy_slip+sell_slip vs mid.
    # rt_cost/2 == realized cost-per-volume, so 2.6bp here == ~1.3bp/vol == our
    # lifetime efficiency. We only trade when we can roughly match our own edge;
    # during a widened market we skip and preserve capital (which raises the ceiling).
    max_rt_cost_bps: float = 2.6
    cycle_interval_sec: float = 3.0
    # Hard halt below this much SOMI/STT. Gas is CHEAP on Somnia (~6 gwei → a
    # round-trip is ~0.012 SOMI, a native buy ~0.048 SOMI at 8M gas), so this floor
    # only needs to cover a handful of txs incl. one self-rescue top-up even if gas
    # spikes ~5x. The old 2.0 was ~160 round-trips of usable gas held hostage — it
    # stranded the bot at 1.99 SOMI (halt fired before the top-up could refuel).
    gas_reserve_native: float = 0.3
    # Auto gas top-up: when native gas dips below the trigger, spend a little USDso
    # on SOMI to keep trading. Rule 6 allows it. SOMI ~$0.10, so $2 buys ~20 SOMI ≈
    # 1600 round-trips of runway — and SOMI is a sellable asset, so this parks (not
    # burns) capital. Kept small so nearly all $150 stays as USDso for clips.
    # Mainnet-relevant; dormant on testnet (native there is STT, reserve is ample).
    gas_topup_enabled: bool = True
    gas_topup_trigger_native: float = 1.0
    gas_topup_spend_usdso: float = 2.0
    keepalive_hours: float = 20.0      # force a min trade if idle longer than this
    # Flatten price protection: when selling leftover base, floor the sell at
    # mid*(1-this) so we never dump into a thin/dislocated book. It still fills at
    # the real bid normally; if the bid is below the floor, we DON'T sell (keep the
    # inventory and retry) rather than realize a bad print.
    max_flatten_slippage_bps: float = 20.0
    # Keep the wallet flat automatically: sweep residual/dust every this often (not
    # just on boot, so nothing accumulates between restarts), and recover
    # sub-minimum dust down to this value (below it the gas isn't worth it).
    # 30min was too slow: sell legs under-fill in the hollow bid touch, so WBTC
    # residual rebuilt to ~$45 (half our capital) between sweeps and clips halved.
    # 5min caps stuck inventory at a few $ so full-size clips keep running.
    flatten_interval_sec: float = 300.0
    dust_recover_min_usd: float = 0.50
    # Endurance mode: only trade the cheap spread regime by default. Keepalive
    # may relax this slightly so the wallet never goes silent for 24h.
    keepalive_max_spread_bps: float = 4.0
    keepalive_max_clip_usdso: float = 8.0
    # Pacing: total volume is capital-bound (~$1.7M at 0.88bp on $150), so speed
    # doesn't raise the ceiling — it just decides how fast we spend the fuel. We
    # spread it across the competition instead of blowing it in ~14h: after each
    # round-trip, wait proportionally so realized volume ≈ this per-day rate.
    # Fast spend: ~$200k/day banks the full ~$1.11M capital-bound ceiling in ~5-6
    # days, which de-risks an early cancel (rule 10) and closes the pace gap to the
    # leaders. This is safe because WBTC sits inside the 3bp gate ~100% of the time
    # (measured) — so trading MORE often stays just as cheap; pace up ≠ cost up. The
    # gate still protects every trade; the keepalive covers the 24h-DQ tail.
    target_daily_volume_usdso: float = 200_000.0
    round_trip_gas: int = rt.DEFAULT_ROUND_TRIP_GAS
    max_minutes: float = 10.0
    max_round_trips: int = 10_000_000


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

    def fillable_usd(self, pool: Pool, best_bid: float, best_ask: float, cross_bps: float) -> float:
        """USDso we can actually FILL on each side at our cross limit: the buy
        reaches asks up to best_ask*(1+cross); the sell reaches bids down to
        best_bid*(1-cross). A round-trip needs both, so the SMALLER side bounds a
        clip that closes with no residual inventory. This is per-pair and per-
        moment — a thin book (WETH) self-limits to a small clip, a deep one (WBTC)
        is bounded only by capital. Measuring at the cross limit (not a loose fixed
        band) is the fix for the leftover-WETH accumulation."""
        buy_limit = best_ask * (1 + cross_bps / 10_000)
        sell_limit = best_bid * (1 - cross_bps / 10_000)
        ask_usd = 0.0
        for price_raw, size_raw in read_book_levels(pool._contract, False, 15):  # asks (we buy)
            price = from_raw(price_raw, pool.quote_decimals)
            if price <= buy_limit:
                ask_usd += price * from_raw(size_raw, pool.base_decimals)
        bid_usd = 0.0
        for price_raw, size_raw in read_book_levels(pool._contract, True, 15):  # bids (we sell)
            price = from_raw(price_raw, pool.quote_decimals)
            if price >= sell_limit:
                bid_usd += price * from_raw(size_raw, pool.base_decimals)
        return min(ask_usd, bid_usd)

    def effective_rt_cost_bps(self, pool: Pool, clip: float, mid: float) -> float | None:
        """VWAP cost, in bps of mid, to BUY `clip` USDso on the asks and SELL `clip`
        on the bids — i.e. what a round-trip of this size actually pays after walking
        the book, not the quoted touch. Returns buy_slip + sell_slip (rt_cost), or
        None if either side lacks the depth to fill the clip. rt_cost/2 is the
        cost-per-volume this clip would realize."""
        def vwap(levels, notional: float) -> float | None:
            rem, base = notional, 0.0
            for price_raw, size_raw in levels:
                price = from_raw(price_raw, pool.quote_decimals)
                lvl_usd = price * from_raw(size_raw, pool.base_decimals)
                take = min(lvl_usd, rem)
                base += take / price      # base units bought/sold at this level
                rem -= take
                if rem <= 1e-9:
                    break
            if rem > 1e-9 or base <= 0:
                return None               # not enough depth to fill the whole clip
            return notional / base        # notional-weighted average price
        buy_vwap = vwap(read_book_levels(pool._contract, False, 15), clip)   # asks
        sell_vwap = vwap(read_book_levels(pool._contract, True, 15), clip)   # bids
        if buy_vwap is None or sell_vwap is None or mid <= 0:
            return None
        return (buy_vwap - mid) / mid * 1e4 + (mid - sell_vwap) / mid * 1e4

    def emit(self, event: str, **kw) -> None:
        rec = {"ts": round(time.time(), 3), "event": event, **kw}
        line = json.dumps(rec)
        self._log.write(line + "\n")
        self._log.flush()
        # flush=True so logs stream to Render immediately — Python block-buffers
        # stdout when it isn't a TTY, which otherwise hides output for minutes.
        print(line, flush=True)

    def pick_venue(
        self,
        free: float,
        *,
        max_spread_bps: float | None = None,
        keepalive: bool = False,
    ) -> tuple[Pool, float, float, float] | None:
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
            spread_gate = self.cfg.max_spread_bps if max_spread_bps is None else max_spread_bps
            if spread_bps > spread_gate:
                continue  # strict spread gate: only trade when it's cheap
            # Clip = as much capital as we can, capped by the depth we can actually
            # fill at our cross limit — so the round-trip closes flat (no leftover
            # inventory) instead of walking a thin book.
            depth_cap = self.fillable_usd(p, tob.best_bid, tob.best_ask, self.cfg.cross_bps) * self.cfg.depth_fraction
            min_notional = p.min_qty * tob.best_ask
            if keepalive:
                target = max(min_notional * 1.05, 2.0)
                clip = min(self.cfg.keepalive_max_clip_usdso, free * 0.10, depth_cap, target)
            else:
                clip = min(self.cfg.max_clip_usdso, free * self.cfg.balance_fraction, depth_cap)
            if clip < min_notional:
                continue  # can't meet this pair's minimum with current capital/depth
            # SIZE-AWARE COST GATE: what this clip actually pays after walking the
            # book. A book can pass the quoted-spread pre-filter while the hollow-
            # touch effective cost is ~2x higher — that's the capital-burn we're
            # closing. Skip (preserve capital) unless we can trade near our own edge.
            # Bypassed on keepalive so we can still force a trade to dodge 24h-DQ.
            rt_cost = self.effective_rt_cost_bps(p, clip, tob.mid)
            if not keepalive and (rt_cost is None or rt_cost > self.cfg.max_rt_cost_bps):
                continue
            # Bias to the preferred venue (WBTC): deep, always tight, big clips (WETH's
            # tiny clips burn the same gas for ~4x less volume). Score on the EFFECTIVE
            # cost we'd actually pay (fall back to quoted spread if depth is unknown).
            eff = rt_cost if rt_cost is not None else spread_bps
            score = eff - (self.cfg.preferred_bias_bps if sym == self.cfg.preferred_symbol else 0.0)
            if best is None or score < best[0]:
                best = (score, p, spread_bps, clip, min_notional)
        return (best[1], best[2], best[3], best[4]) if best else None

    # ---- main loop ---------------------------------------------------------
    def run(self) -> None:
        self.emit("start", network=self.ctx.net.name, wallet=self.ctx.address,
                  impl=self.impl, broadcast=self.broadcast, gas=self.native_gas(),
                  settings=asdict(self.cfg))
        if self.broadcast:
            self._flatten_residual_base()  # start flat — clear any stranded inventory
        last_flatten = time.time()
        deadline = self.stats.started + self.cfg.max_minutes * 60
        while time.time() < deadline and self.stats.round_trips < self.cfg.max_round_trips:
            vol_before = self.stats.volume_usdso
            try:
                self._tick()
            except Exception as e:  # never let one bad cycle kill a 14-day run
                self.emit("tick_error", error=str(e)[:200])
                self.nm.reset()
                time.sleep(2.0)
            # Periodic sweep so residual/dust can't build up between restarts.
            if self.broadcast and time.time() - last_flatten >= self.cfg.flatten_interval_sec:
                self._flatten_residual_base()
                last_flatten = time.time()
            self._pace_sleep(self.stats.volume_usdso - vol_before)
        self.emit("stop", **self._summary())

    def _pace_sleep(self, traded_volume: float) -> None:
        """Spread realized volume across the day at ~target_daily_volume. After a
        round-trip of `traded_volume`, wait proportionally; a skip (0) just waits
        the base cycle interval before re-checking the book."""
        base = self.cfg.cycle_interval_sec
        if traded_volume <= 0 or self.cfg.target_daily_volume_usdso <= 0:
            time.sleep(base)
            return
        pace = traded_volume / self.cfg.target_daily_volume_usdso * 86_400.0
        time.sleep(max(base, pace))

    def _tick(self) -> None:
        # 1. Gas guard. Try the top-up FIRST when dipping so a low-but-usable balance
        #    self-rescues (buys runway with a little USDso — rule 6 allows it, cheap
        #    vs the volume it unlocks), then re-read and only hard-halt if still under
        #    reserve. Ordering matters: halting before the top-up is what stranded us
        #    at 1.99 SOMI with plenty of usable gas. place_order simulates before
        #    broadcast, so a guaranteed-revert top-up won't even burn gas.
        gas = self.native_gas()
        if self.cfg.gas_topup_enabled and self.broadcast and gas < self.cfg.gas_topup_trigger_native:
            self._topup_gas()
            gas = self.native_gas()  # the top-up may have just refueled us
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
            self._maybe_keepalive(free)
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

    def _topup_gas(self) -> None:
        """Buy ~gas_topup_spend_usdso of native SOMI via a SOMI:USDso IOC. Uses the
        loop's own NonceManager (not a fresh Pool nonce manager) so it can't collide
        with round-trip nonces. Native-base buy → place_order applies the >=5M gas
        floor automatically."""
        try:
            p = Pool.load(self.ctx, "SOMI:USDso")
            tob = p.top_of_book()
            if tob.best_ask is None:
                self.emit("gas_topup_skip", reason="no_ask")
                return
            somi_qty = self.cfg.gas_topup_spend_usdso / tob.best_ask
            price_raw = align_to_tick(
                to_raw(tob.best_ask * 1.01, p.quote_decimals), p.params.tick_size, "ask"
            )
            qty_raw = align_to_lot(to_raw(somi_qty, p.base_decimals), p.params.lot_size)
            if qty_raw < p.params.min_quantity:
                self.emit("gas_topup_skip", reason="below_min", somi_qty=somi_qty)
                return
            res = place_order(self.ctx, self.nm, PlaceParams(
                pool=p.address, base_is_native=True, is_bid=True,
                price_raw=price_raw, quantity_raw=qty_raw, tick_raw=p.params.tick_size,
                lot_raw=p.params.lot_size, min_qty_raw=p.params.min_quantity,
                expire_ns=build_expire_ns(5 * 60_000), order_type=OrderType.IOC,
            ))
            self.emit("gas_topup", somi_qty=round(somi_qty, 4), gas_used=res.gas_used, tx=res.tx_hash)
        except Exception as e:
            self.emit("gas_topup_failed", error=str(e)[:200])

    def _flatten_residual_base(self) -> None:
        """Keep the wallet flat. For each pair, sell any leftover base (IOC) at a
        price floored at mid*(1-max_flatten_slippage_bps) — fills at the real bid in
        a healthy book, defers (keeps it) if the bid has dropped below the floor.

        Sub-minimum DUST (0 < base < minQuantity, so it can't be sold directly) is
        recovered by first BUYING up to the minimum, then selling the whole lot —
        nets the dust back minus the spread. Uses the loop's own NonceManager so it
        can't collide with round-trip nonces. Leaves only true crumbs
        (< dust_recover_min_usd)."""
        for sym in CANDIDATE_SYMBOLS:
            try:
                p = self._pool(sym)
                tob = p.top_of_book()
                if tob.best_bid is None or tob.mid is None:
                    continue
                base = p.wallet_base()
                if base * tob.best_bid < self.cfg.dust_recover_min_usd:
                    continue  # nothing worth clearing
                # Dust recovery: below min tradable -> buy a full min-lot so the
                # total clears the minimum, then fall through to sell everything.
                if base < from_raw(p.params.min_quantity, p.base_decimals):
                    self._buy_to_min(p, tob)
                    base = p.wallet_base()  # re-read after the top-up buy
                floor = tob.mid * (1 - self.cfg.max_flatten_slippage_bps / 10_000)
                if tob.best_bid < floor:
                    self.emit("flatten_deferred", symbol=sym, reason="bid_below_floor",
                              bid=round(tob.best_bid, 4), mid=round(tob.mid, 4),
                              floor=round(floor, 4), base=round(base, 8))
                    continue
                # Limit = the floor; the IOC still fills at the (higher) bid, but
                # can't sell below the floor even if it had to walk the book.
                price_raw = align_to_tick(to_raw(floor, p.quote_decimals), p.params.tick_size, "bid")
                qty_raw = align_to_lot(to_raw(base, p.base_decimals), p.params.lot_size)
                if qty_raw < p.params.min_quantity:
                    continue  # still below min (top-up didn't fill) — retry next sweep
                res = place_order(self.ctx, self.nm, PlaceParams(
                    pool=p.address, base_is_native=p.base_is_native, is_bid=False,
                    price_raw=price_raw, quantity_raw=qty_raw, tick_raw=p.params.tick_size,
                    lot_raw=p.params.lot_size, min_qty_raw=p.params.min_quantity,
                    expire_ns=build_expire_ns(5 * 60_000), order_type=OrderType.IOC,
                ))
                self.emit("flatten_residual", symbol=sym, base=round(base, 8),
                          usd=round(base * tob.best_bid, 2), floor=round(floor, 4), tx=res.tx_hash)
            except Exception as e:
                self.emit("flatten_failed", symbol=sym, error=str(e)[:200])

    def _buy_to_min(self, p: Pool, tob) -> None:
        """Buy one full minQuantity lot (IOC at the ask) so a sub-minimum dust
        balance clears the minimum and becomes sellable."""
        price_raw = align_to_tick(
            to_raw(tob.best_ask * (1 + self.cfg.cross_bps / 10_000), p.quote_decimals),
            p.params.tick_size, "ask",
        )
        res = place_order(self.ctx, self.nm, PlaceParams(
            pool=p.address, base_is_native=p.base_is_native, is_bid=True,
            price_raw=price_raw, quantity_raw=p.params.min_quantity,
            tick_raw=p.params.tick_size, lot_raw=p.params.lot_size,
            min_qty_raw=p.params.min_quantity, expire_ns=build_expire_ns(5 * 60_000),
            order_type=OrderType.IOC,
        ))
        self.emit("dust_topup", symbol=p.symbol,
                  bought=from_raw(p.params.min_quantity, p.base_decimals), tx=res.tx_hash)

    def _maybe_keepalive(self, free: float) -> None:
        idle_h = (time.time() - self.stats.last_fill_ts) / 3600
        if idle_h < self.cfg.keepalive_hours:
            return

        venue = self.pick_venue(
            free,
            max_spread_bps=self.cfg.keepalive_max_spread_bps,
            keepalive=True,
        )
        if venue is None:
            self.emit("keepalive_warn", idle_h=round(idle_h, 2),
                      note="no affordable keepalive venue while approaching 24h DQ window")
            return

        pool, spread_bps, clip, min_notional = venue
        plan = rt.plan_round_trip(pool, size_usdso=clip, cross_bps=self.cfg.cross_bps)
        if plan is None:
            self.emit("keepalive_skip", symbol=pool.symbol, reason="no_plan",
                      spread_bps=round(spread_bps, 2), clip=round(clip, 4))
            return

        res = rt.send_round_trip(
            self.ctx, self.nm, self.impl, plan,
            gas=self.cfg.round_trip_gas, dry_run=not self.broadcast,
            base_decimals=pool.base_decimals, quote_decimals=pool.quote_decimals,
        )
        self.stats.round_trips += 1
        if res.dry_run:
            self.emit("keepalive_dry", symbol=pool.symbol, spread_bps=round(spread_bps, 2),
                      notional=round(plan.notional_usdso, 2), qty=plan.qty)
            return

        self.stats.gas_used += res.gas_used
        if res.status == 1 and res.logs > 0:
            self.stats.fills_ok += 1
            self.stats.volume_usdso += res.volume_usdso
            self.stats.last_fill_ts = time.time()
            self.emit("keepalive_ok", symbol=pool.symbol, spread_bps=round(spread_bps, 2),
                      volume=round(res.volume_usdso, 2), gas_used=res.gas_used,
                      cum_volume=round(self.stats.volume_usdso, 2), tx=res.tx_hash)
        else:
            self.emit("keepalive_bad", symbol=pool.symbol, status=res.status,
                      logs=res.logs, tx=res.tx_hash)

    def _summary(self) -> dict:
        dur = time.time() - self.stats.started
        vol = self.stats.volume_usdso
        return {
            "round_trips": self.stats.round_trips, "fills_ok": self.stats.fills_ok,
            "skips": self.stats.skips, "volume_usdso": round(vol, 2),
            "gas_used": self.stats.gas_used, "minutes": round(dur / 60, 2),
            "vol_per_hour": round(vol / (dur / 3600), 2) if dur > 0 else 0,
        }


def _harden_rpc(ctx) -> None:
    """Make the RPC transport survive dropped idle connections. Somnia's RPC closes
    idle keep-alive sockets, so the first call after a paced sleep often hits
    'Connection reset by peer'. Mount a retry adapter that transparently reconnects
    and re-issues, so a reset costs a quick retry instead of a whole failed tick
    (and the occasional reverted tx that followed one). Safe on POST: JSON-RPC
    reads are idempotent, and re-sending a signed tx is idempotent (same hash)."""
    import requests
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except Exception:  # pragma: no cover
        from urllib3.util import Retry  # type: ignore

    retry = Retry(
        total=4, connect=4, read=3, backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=None,  # retry POST too (see docstring)
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    ctx.w3.provider = Web3.HTTPProvider(
        ctx.net.rpc_url, session=session, request_kwargs={"timeout": 30}
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    # Runtime: prefer --days for the real competition run; --minutes for short tests.
    ap.add_argument("--days", type=float, default=None, help="run length in days (overrides --minutes)")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--max-clip-usdso", type=float, default=Settings.max_clip_usdso)
    ap.add_argument("--cross-bps", type=float, default=Settings.cross_bps)
    ap.add_argument("--max-spread-bps", type=float, default=Settings.max_spread_bps)
    ap.add_argument("--cycle-sec", type=float, default=3.0)
    ap.add_argument("--target-daily", type=float, default=Settings.target_daily_volume_usdso,
                    help="paced volume/day (USDso); 0 = trade as fast as possible")
    ap.add_argument("--broadcast", action="store_true")
    args = ap.parse_args()

    minutes = args.days * 1440 if args.days is not None else args.minutes
    ctx = create_chain_context()
    _harden_rpc(ctx)  # survive dropped idle RPC connections
    nm = NonceManager(ctx.w3, ctx.address)
    cfg = Settings(
        max_minutes=minutes, max_clip_usdso=args.max_clip_usdso,
        cross_bps=args.cross_bps, max_spread_bps=args.max_spread_bps,
        cycle_interval_sec=args.cycle_sec,
        target_daily_volume_usdso=args.target_daily,
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
