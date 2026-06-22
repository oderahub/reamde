# DreamDEX Alpha Competition — Phase 2 Feedback

**Participant wallet:** `0x4258950186a12492Bf805f2B9D7facd202921F34`
**Window:** June 8 – June 22, 2026, mainnet (`api.dreamdex.io` + Somnia mainnet RPC)

These are Phase 2 mainnet observations only — issues already covered in
our Phase 1 submission are not repeated here. (One Phase 1 issue, the
per-symbol WS stream staleness, recurred twice during Phase 2; rather than
re-reporting it, Finding 9 documents a new flavor of it we found at
subscription time.) Every finding rests on an artifact — a transaction
hash, a decoded revert, or a timestamped structured-log entry — collected
while operating an automated trading bot unattended through Phase 2. The
bot ran unattended through the full competition (final snapshot
2026-06-22 10:00 UTC), which it completed having crossed both volume
milestones — so these findings reflect end-to-end operation, not a
short test window.

> Note on documentation citations: doc quotes were captured while reading
> the docs portal during the competition. The portal is login-gated, so
> page paths and quotes reflect the version read at the time.

---

## Executive summary — suggested changes in priority order


In priority order, by what a new cohort would benefit from most:

1. **Per-symbol WS heartbeat** — the Phase 1 stream-staleness recurred
   twice in Phase 2, and Finding 9 adds a new flavor: the subscription
   snapshot itself arrives stale. Highest-impact reliability change.
2. **Vault funding can use wallet inventory** (Finding 2) — removes
   significant unnecessary deposit/withdraw friction.
3. **Cancel collateral routing** (Finding 3) — prevents a confusing
   debugging session for any maker-bot author.
4. **Yield payout cadence** (Finding 4) — decision-blocking for
   competition contexts.
5. **Custom error selector list** (Finding 5 + addenda) — accelerates
   revert debugging; we have now reverse-engineered three selectors,
   including `0x782b2567` (`InsufficientGasForPayout`), which silently
   disables the native-SOMI buy path for any client that under-sets its
   gas limit (cost at least one cohort member a working SOMI strategy).
6. **Maker BBO competitiveness disclaimer** (Finding 6) — calibrates
   participant expectations.
7. **Locked-collateral visibility** (Finding 8) — without it, every
   equity calculation a bot author writes is wrong while orders rest.
8. **Cancel revert conditions + idempotent cancel** (Finding 10) — the
   only finding in this report where funds were temporarily
   inaccessible with no API-visible explanation.
9. **Open-orders consistency model** (Finding 11) — the recovery path
   for Findings 8 and 10 must itself be reliable.
10. **WS snapshot freshness** (Finding 9) — extends the
    heartbeat ask to the subscription snapshot.
11. **Vault endpoint auth scoping** (Finding 12) — make the
    auth-vs-public mismatch a deliberate choice.

None of these are protocol-breaking. They are all places where the
protocol behaves correctly but the behavior is either undocumented or
surfaced through error paths a bot author has to reverse-engineer.
Findings 8–11 compound each other in practice: invisible collateral
(8) must be reconstructed from a listing that can omit live orders
(11), after cancels that can silently revert (10), with error
selectors nobody can decode (5).

---

## Finding 2 — Vault funding can pull from wallet inventory (undocumented)

**What we observed.** The Order Types doc states that PostOnly orders
"require vault funding." We interpreted this as a hard requirement that
the asset has to physically reside in the vault.

In practice, after a bid filled, we held 5 USDC.e in the **wallet** (not
the vault) and zero in the vault. The strategy then posted a PostOnly
ASK with `funding=vault` for that 5 USDC.e:

```text
2026-06-09T15:19:32  inventory.initialized  market=USDC.e:USDso
                     vault_base=0  vault_quote=0.004  wallet_base=5
2026-06-09T15:19:33  engine.order_submitted  coid=ym_sell_255723de
                     side=sell  strategy=yield_maker
                     tx_hash=13b86c25626671c228c7468171caf514d0d143e114a5d51d0f41cf05b5fe598a
```

The order broadcast, passed simulation, and rested on the book. The
protocol evidently pulled inventory from wallet on order placement
rather than requiring an explicit vault deposit step.

**Why this matters.** This is genuinely useful behavior — it removes a
deposit / withdraw cycle between every maker fill — but it is not
documented anywhere we found. The natural reading of "requires vault
funding" leads bot authors to write an explicit deposit step before
posting maker orders, which is unnecessary friction.

**Suggested fix.** Add a note to `trading/common/order-types.md` clarifying
that for PostOnly orders, the protocol can pull base or quote from
either the vault or the wallet at order-placement time. If there is a
preference order (vault first, then wallet?), document it.

---

## Finding 3 — Cancel refunds released collateral go to wallet, not vault

**What we observed.** We deposited 15 USDso to the `USDC.e:USDso`
vault:

```text
deposit_tx = 82c2a8dcfca9c5b5905d2ef91d7713030b0b9845b5fa30bbc6a6e84728c2b3a5
deposit_status = 1
```

Across two bid orders, 10 USDso was locked as collateral. After
cancelling both orders cleanly:

```text
cancel_tx_1 = e4909e5034137b78e93a70b99a82e9a2be2c0bae7a58e985a1ccb136f44eb5ee  status=1
cancel_tx_2 = 1e440e1db6b065c1fed87db2dc816e0b1d9ff6401b199fcaca9ac83008284eb4  status=1
```

we expected the released ~10 USDso to be back in the vault as free
balance. Instead, a subsequent vault withdraw attempt reverted with:

```text
ContractCustomError 0xcf479181
  data: 0x000e35fa931a0000  → "have"  ≈ 0.004 USDso
        0x8ac7230489e80000  → "want"  = 10 USDso
```

A direct on-chain wallet balance check confirmed the funds had landed
in the **wallet** as ERC-20 USDso, not back in the vault:

```text
wallet USDso: 54.98 (up by ~10 vs the post-deposit baseline)
vault USDso:  0.004 (down to dust)
```

**Why this is hard for bot authors.** The mental model for an LP-style
flow is "deposit → resting → fill → resting again." Cancellation routing
the refund to wallet means the next cycle has to re-deposit, which adds
latency, gas, and operational complexity. It also caused us to write a
withdraw helper that turned out to be unnecessary in this state — we
had nothing meaningful to withdraw because the funds had already left
the vault.

**Suggested fix.** Document the destination of collateral on cancel
explicitly — likely in `trading/vaults.md` or the order-types page — so
maker-bot authors can model the collateral lifecycle correctly.

---

## Finding 4 — Yield payout cadence is `[TODO]` on the Fees page

**What we observed.** The Collateral Yield Algorithm doc
(`trading/common/yield-algorithm.md`) defines the scoring formula
clearly: `score = quantity × W × seconds`, with `W` Gaussian-weighted to
mid-price proximity. Payment is described as periodic settlement to the
maker's wallet.

The Fees doc (`trading/common/fees.md`) has the payment-method line:

> Payment: Yield is paid out in USDso to the maker's wallet. \[TODO: Add methodology]

**Why this matters.** Without a documented cadence (per block, per
hour, per day, per week, per settlement window?), a bot author running a
short-window experiment cannot decide whether maker quoting is viable.
We ran a 25-minute live maker test and saw no yield credit in the
wallet — but with the cadence undocumented, we cannot tell whether that
means yield wasn't accrued, or whether it accrued but the next
settlement boundary wasn't crossed yet.

For Alpha-Competition-sized windows (days), this gap is decision-blocking:
a daily payout is meaningful; a weekly payout might miss the entire
contest.

**Suggested fix.** Replace the `[TODO]` with the actual cadence, the
trigger (cron / epoch / block-height / on-demand?), and how to verify
historical accrual.

---

## Finding 5 — Custom error selectors are not documented

**What we observed.** A vault withdraw revert returned the custom error
data shown in Finding 3, beginning with selector `0xcf479181`. We were
able to interpret it only because the two parameters happened to be
straightforward `(have, want)` uint256 amounts, and we could check both
against the live vault balance.

For other revert paths — particularly the contract-level place-order
rejection paths the Phase 1 testnet report enumerated under
`order_simulation_rejected` — we could not identify a documented
mapping from selector to meaning.

**Suggested fix.** Publish a list of custom error selectors in
`developers/contracts/events.md` (or a new `errors.md` page next to it),
with the ABI encoding of their parameters, so bot authors can decode
reverts without inspector access.

---

## Finding 6 — Small-capital makers cannot competitively quote at BBO

**Phase 1 cross-reference:** Finding 4 noted that `USDC.e:USDso` is the
most-efficient market for cohort volume. This finding adds a
maker-side counterpart.

**What we observed.** During the live yield-maker test, we quoted at
BBO match (`improve_ticks: 0`, so `bid_price = best_bid` and
`ask_price = best_ask`) at $5 size on `USDC.e:USDso`, on a market doing
~38 trades/minute with ~$1,750/minute in volume. Over 25 minutes of
live resting time spread across two bot-restart cycles, we received
**one fill**: a single 5-USDC.e bid was hit during a pause window
when the dominant maker presumably did not refresh in time.

The dominant maker at $0.9999/$1.0001 evidently had both larger
displayed size and earlier time-in-queue, which under Price-Time
Priority means our $5 quote sat behind every previously-placed order
at that price level. Most takers swept the dominant maker's quantity
and were fully filled before reaching us.

**Why this matters for bot authors.** The Profit-Oriented Strategy
Guidance section of the protocol docs encourages maker quoting at fair
value. In practice on the cohort markets, there is a **minimum capital
threshold** below which maker quoting at BBO does not return fills at a
meaningful rate. Bot authors below that threshold are better off using
IOC (taker) for volume goals even though they pay the spread.

**Suggested fix.** Either (a) publish the typical top-of-book quantity
range per market so bot authors can size accordingly, or (b) add a
maker-strategy note suggesting that participants with small starting
capital should not expect significant fills at BBO until the dominant
maker is consumed. Together with Finding 4 from the cumulative report,
this would let a new participant make a calibrated maker-vs-taker
decision on day one.

---

## Finding 7 — Throughput improvement from parallelizing balance refresh

**Phase 1 cross-reference:** Finding 5 measured 6.6 tx/min wall-clock
ceiling on the bot, with the bottleneck called out as four sequential
vault-balance GETs after every tx.

**What we observed in Phase 2.** After refactoring the engine's
`_refresh_balances` to `asyncio.gather` across the four watched markets
(commit-level change in our repo), measured throughput on a
dual-market `volume_mill` (`WETH:USDso` + `WBTC:USDso`) cycling at
`cycle_interval_sec: 10.0`:

| Metric | Phase 1 | Phase 2 |
| --- | --- | --- |
| Tx rate (single-market push) | 6.6 / min | 11 / min |
| Volume rate | \$1,170 / hour (WETH-only after USDC.e add) | \$19,800 / hour |
| Cost-per-dollar-of-volume | \~3 bps (USDC.e + WETH mix) | \~0.83 bps (WETH + WBTC) |

The 1.67× tx-rate improvement and the better cost ratio both came
without any per-tx code-path changes — just a parallel I/O fan-out on
the post-tx balance refresh. We confirm that with this change the
binding constraint is the protocol round-trip (REST prepare + RPC
broadcast), not application-side serialization.

**Why this matters.** The Phase 1 recommendation that the protocol
expose a combined "prepare + execute" endpoint is still the cleanest
path to higher single-wallet throughput. But for bot authors who can't
wait for that change, parallel balance refresh is a meaningful
short-term lift and worth highlighting in any future bot-author guide.

---

## Finding 8 — Collateral locked in resting orders is invisible to every balance read

**What we observed.** When a PostOnly order is placed, its collateral
leaves the wallet (Finding 2) — but it does not appear in the
`/v0/markets/{symbol}/vault/balance` response either. While an order
rests, the collateral exists only inside the pool contract, with no API
surface that reports it.

Timeline from 2026-06-10 (all from our structured logs):

```text
06:59:24  wallet USDso 49.10   (no open orders)
06:59:27  PostOnly bid placed  (~$19.90 notional)
06:59:28  wallet USDso 29.20   vault_quote 0.000
```

With two bids resting earlier the same morning, the wallet read 9.14
while ~$40 sat in order locks — visible nowhere.

**Why this is hard for bot authors.** Any equity or drawdown
calculation built from wallet + vault reads silently loses the locked
amount. In our case a routine requote (new bid placed before the old
bid's cancel refund landed) made account equity appear to collapse from
$49 to $9 — a phantom −90% drawdown that tripped our kill switch and
halted the bot, with all funds in fact safe. The only workaround is to
reconstruct locked value client-side from open-order remaining × price,
which depends on the open-orders listing being reliable (see Finding 11).

**Suggested fix.** Expose locked collateral per market (or
account-wide) — e.g. a `lockedInOrders` field on the vault balance
response — or document the full collateral custody lifecycle in
`trading/vaults.md`: wallet → pool lock (placement) → consumed (fill) or
wallet refund (cancel), including the latency of the refund leg.

---

## Finding 9 — WS orderbook snapshot ~15 bps stale at subscription time

**What we observed.** After the 2026-06-10 morning incidents we added an
automated REST-vs-WS reconciler to the bot (REST poll every 4s, replace
the in-memory book at >1.5 bps BBO drift). On its very first pass —
0.2–0.3 seconds after `ws.connected` — it caught two symbols whose WS
snapshots disagreed with REST by 15 bps:

```text
06:59:25.465  ws.connected
06:59:25.658  ws_book_stale_replaced  market=USDC.e:USDso  drift_bps=15.0000
06:59:25.770  ws_book_stale_replaced  market=WBTC:USDso    drift_bps=15.0061
```

This is a different flavor from the Phase 1 stream-staleness: not a live stream going
quiet, but the *initial snapshot* delivered on subscription already
being stale. The suspiciously round 15.0000 bps on the stablecoin pair
suggests a cached snapshot rather than a fresh book read.

**Why this matters.** A maker that prices its first quotes off the
subscription snapshot quotes 15 bps off market the moment it boots —
post-only orders either reject (would-cross) or rest uselessly deep.
This compounds the stream-staleness: bot authors now have to distrust both the
stream *and* the snapshot.

**Suggested fix.** Serve the subscription snapshot from the same source
as `/v0/orderbooks`, or document the expected snapshot freshness so
clients know to reconcile against REST at startup.

---

## Finding 10 — Order cancellation can revert deterministically, with no way to diagnose or even detect it from the API surface

**What we observed.** During an automated shutdown on 2026-06-10 we
cancelled two freshly placed resting bids. All four cancel transactions
(two per order, including retries) **reverted on-chain** while the REST
prepare endpoint happily returned signable transactions every time:

```text
order 147573952589684098111 (bid, placed 06:36:45)
  cancel @ 06:36:50  status=0  gasUsed=197,341
  cancel @ 06:36:59  status=0  gasUsed=197,341   (identical revert point)
  cancel @ 07:54:12  status=1  gasUsed=199,832   (succeeds, 78 min later)

order 184467440737103201374 (bid, placed 06:36:51)
  cancel @ 06:36:54  status=0  gasUsed=30,484
  cancel @ 06:37:00  status=0  gasUsed=30,484    (early revert, different mode)
```

The first order rejected two cancel attempts at T+5s and T+14s after
placement — reverting ~2,500 gas short of the successful run's total,
i.e. at the very end of the cancel flow — then cancelled cleanly with
the same calldata pattern 78 minutes later. Its ~$20 of collateral
stayed locked (and invisible, Finding 8) the whole time.

**Why this is hard for bot authors.** Three independent gaps compound:

1. The prepare endpoint performs no validation — it returns a signable
   cancel tx for any order id, so the client learns about failure only
   from the receipt.
2. The revert reasons are undocumented custom selectors (Finding 5),
   so we cannot tell *why* a cancel of a genuinely open, later-cancellable
   order reverts.
3. A cancel-and-replace loop that does not await receipts (the natural
   high-throughput pattern) silently accumulates zombie resting orders
   with locked collateral.

Additionally, recovery is awkward: the cancel path only accepts the
numeric uint128 id (`DELETE /orders/ym_buy_93b536f2` → 400
`invalid_param`, pattern `^[0-9]+$`), and that id is only obtainable
from tx simulation or the per-order WS channel — a client that misses
both (e.g. restart between placement and ack) must re-list open orders
to rediscover its own order.

**Update (same day, later session).** Order age does not explain the
pattern: order `239807672958231966995` (a resting PostOnly ask placed
~12:00 UTC) also rejected a cancel at ~70 minutes of age —
`0xab52522be8eac061a7fcbcf949fa9234a4750294f0ecc28c3adcc40d7f823264`,
status=0 — while a different order of similar age cancelled fine the
same hour. The same order then cancelled successfully in a later sweep
that day (`0x7c8ddeb395eff91decfb9e881c1a7e2e5968d7a83b49a8f3b551dd1e1bcf441a`,
status=1). Whatever gates cancellation, it is per-order state, not a
settlement window — and it eventually clears on its own.

**Suggested fix.** Document the conditions under which cancel reverts
(is there a minimum age / settlement window after placement?), publish
the revert selectors (Finding 5), consider making cancel idempotent
(no-op success on already-cancelled), and accept `clientOrderId` as a
cancel key.

---

## Finding 11 — Open-orders listing inconsistent with on-chain order state

**What we observed.** Order `147573952589684098111` (Finding 10) was
never successfully cancelled before 07:54 — both earlier cancel txs
reverted on-chain. Yet the REST open-orders listing disagreed with
itself across the same window:

```text
06:59:25  GET /orders?status=open (4 markets)  → engine.initialized open_orders=0
07:54:0x  GET /orders?status=open (WETH)       → 2 orders, including 147573952589684098111
07:54:12  cancel of 147573952589684098111      → status=1 (it was live on-chain all along)
```

A live, cancellable order was absent from the listing at 06:59 and
present at 07:54, with no successful state-changing transaction for
that order in between (receipts above).

**Why this matters.** The open-orders listing is the recovery path of
last resort (Finding 10) and the only client-side source for
reconstructing locked collateral (Finding 8). If it can omit a live
order, a bot that reconciles state after restart will undercount its
locked funds and may double-commit collateral.

**Suggested fix.** Document the consistency model of
`/orders?status=open` (indexer lag? eventual consistency window?) and,
if possible, expose an as-of block number in the response so clients
can reason about staleness.

---

## Finding 5 addendum — second undocumented error selector

Order placement simulation on 2026-06-10 06:36:55 reverted with
selector `0xe450d38c` and three ABI-encoded words that decode cleanly
as `(address wallet, uint256 have, uint256 want)`:

```text
selector:  0xe450d38c
wallet:    0x4258950186a12492bf805f2b9d7facd202921f34
have:      0x7ecd734e8031e000   ≈  9.136 USDso
want:      0x1156b7a71c0e1e000  ≈ 19.992 USDso
```

The values matched our wallet state exactly (insufficient free quote
for a bid's collateral). This is the second selector we have had to
reverse-engineer (after `0xcf479181`, Finding 3/5) — both turned out to
be benign insufficient-balance variants, but each cost a debugging
session that a published selector table would have avoided.

---

## Finding 5 addendum 2 — third selector `0x782b2567` (`InsufficientGasForPayout`), the highest-impact one

The most operationally severe selector we encountered gates the
native-SOMI BUY payout path on `SOMI:USDso`:

```text
selector: 0x782b2567  →  InsufficientGasForPayout(uint256 gasLeft)
```

**What we observed.** Another cohort member reported `placeOrder` on
`SOMI:USDso` reverting with this selector and concluded native-base buys
were unsupported. We verified their three cited transactions did revert
on-chain — then ran the identical call shape (`isBid=true`,
`msg.value=0`, ERC-20 quote as input) from our own wallet and it
**succeeded** (`status=1`, tx
`bce80ca51a24e1541a72901e1701edc537324f1f9365bcfa1e9b26d3edff60ca`). The
only difference was the broadcast gas limit. Our clients never hard-cap
it below the guard: the engine sizes gas at `eth_estimateGas × 1.25` and
falls back to **8,000,000** on estimate failure
(`core/engine.py:_gas_limit_for_prepared_tx`), and the gas-buy tool
honors the server-prepared gas limit (`tools/buy_somi_gas.py`). The
reverting client hard-coded a `3,000,000` limit.

**Confirmed by the protocol team.** The team subsequently confirmed
`0x782b2567 = InsufficientGasForPayout(uint256 gasLeft)` — a deliberate
guard on the native-SOMI payout path that needs roughly **≥5,000,000
gas** to clear under Somnia's gas model. Native-base BUY is fully
supported and the call shape above is correct; the only requirement is
the gas limit. The fix is client-side: raise the broadcast gas limit to
≥5M **and simulate at the same limit** — a simulation run at a lower
limit passes while the broadcast reverts, which is exactly how this
failure hides from a client that sim-gates its orders.

**Why this belongs in the report.** This is the third selector (after
`0xcf479181` and `0xe450d38c`) we had to reverse-engineer from behavior,
and the highest-impact: it silently disables an entire order path, its
revert is indistinguishable from "feature unsupported" without the
decode, and it cost at least one cohort member a working SOMI strategy.
A published selector table (Finding 5's suggested fix) plus a one-line
note on the native-payout gas floor in `trading/common/order-types.md`
would have prevented the misdiagnosis entirely.

---

## Finding 12 — Vault balance endpoint is not scoped to the authenticated wallet

**What we observed.** `GET /v0/markets/{symbol}/vault/balance` requires
authentication, but honors an arbitrary `walletAddress` query parameter:
with our session token we could read the vault balances of any other
participant's wallet.

**Why we mention it.** The data is public on-chain anyway, so this is
not a confidentiality issue — but the mismatch between "endpoint
requires auth" and "auth does not scope the response" is worth a
deliberate decision. Either drop the auth requirement (it is public
data) or scope the parameter to the authenticated wallet, so the
behavior is intentional rather than incidental.

---

## Finding 13 — Multi-symbol orderbooks query silently returns empty

**What we observed.**

```text
GET /v0/orderbooks?symbols=WETH:USDso&depth=5             -> 1 book
GET /v0/orderbooks?symbols=WETH:USDso,WBTC:USDso&depth=5  -> {"orderbooks": []}
```

The plural parameter name (`symbols`) suggests multi-symbol support, but a
comma-separated list returns an empty array rather than an error. The
silent empty response cost us a debugging session in a monitoring tool
that assumed the request had succeeded.

**Suggested fix.** Support comma-separated symbols, or return 400 on
multi-symbol input so the limitation is discoverable.

---

## Client-side mitigation patterns (working code)

These run in production in our bot and may help other cohort authors
until protocol-side changes land.

### REST-vs-WS book reconciler (mitigates the staleness in Finding 9)

```python
    async def _rest_book_reconcile_loop(self) -> None:
        while not self._stopped:
            for m in self.markets_to_watch:
                if self._stopped:
                    return
                try:
                    book = await self.rest.get_orderbook(m.value, depth=20)
                except Exception as e:
                    log.warning("engine.book_reconcile_fetch_failed",
                                market=m.value, error=str(e))
                    continue
                rest_state = self._book_to_state(m, book)
                drift = self._bbo_drift_bps(self.market_state.get(m), rest_state)
                if drift is not None and drift <= self._reconcile_max_drift_bps:
                    continue
                self._books[m] = {
                    "bids": book.get("bids", []), "asks": book.get("asks", []),
                }
                self.market_state[m] = rest_state
                log.warning(
                    "engine.ws_book_stale_replaced",
                    market=m.value,
                    drift_bps=str(drift) if drift is not None else "ws_side_missing",
                    max_drift_bps=str(self._reconcile_max_drift_bps),
                )
```

### Idle-state reconciliation (recovers from missed fill/cancel WS events; see Findings 8 and 11)

```python
    async def _maybe_idle_reconcile(self) -> None:
        """When the bot has been idle (no tx) for a while, re-read balances and
        open orders from REST and clear strategy quote-tracking for orders that
        have vanished. This is the recovery path for a missed fill/cancel WS
        event, which otherwise leaves a strategy believing it is quoting when
        it holds nothing on the book."""
        if self._idle_reconcile_after_sec <= 0:
            return
        now = time.time()
        idle_for = now - self.last_successful_tx_ts
        if idle_for < self._idle_reconcile_after_sec:
            return
        if now - self._last_idle_reconcile_ts < self._idle_reconcile_after_sec:
            return
        self._last_idle_reconcile_ts = now

        try:
            await self._refresh_balances()
            await self._refresh_open_orders()
        except Exception as e:
            log.warning("engine.idle_reconcile_failed", error=str(e))
            return

        live_coids = set(self.client_order_to_order_id.keys())
        # Drop miss-counts for orders that came back so a recovered listing
        # resets the confirmation counter.
        for coid in list(self._order_miss_counts.keys()):
            if coid in live_coids:
                del self._order_miss_counts[coid]

        cleared = 0
        for strat in self.strategies:
            for coid in strat.tracked_client_order_ids():
                if coid in live_coids:
                    continue
                # Finding 11: confirm the order is missing on consecutive
                # polls before acting, so a transient listing gap doesn't
                # clear a genuinely-resting quote.
                self._order_miss_counts[coid] = self._order_miss_counts.get(coid, 0) + 1
                if self._order_miss_counts[coid] < self._idle_reconcile_miss_threshold:
                    continue
                self._order_miss_counts.pop(coid, None)
                await self._notify_reject(strat.name, coid, "idle_reconcile_vanished")
                cleared += 1

        if cleared:
            log.warning("engine.idle_reconcile_cleared_stale_quotes",
                        idle_for_sec=f"{idle_for:.0f}", cleared=cleared)
            self._report(
```

### Equity that counts order-locked collateral (prevents the Finding 8 phantom drawdown)

```python
        # Collateral locked in resting orders is pulled into the pool
        # contract at placement and is invisible to wallet AND vault balance
        # reads. Count it, or a requote window (new bid placed before the
        # old bid's cancel refund lands) reads as a capital loss — observed
        # live 2026-06-10 as a phantom -90% drawdown that tripped the kill
        # switch while all funds were safely locked in two resting bids.
        for order in self.open_orders.values():
            try:
                market = MarketSymbol(str(order.get("market") or order.get("symbol", "")))
            except ValueError:
                continue
            remaining = Decimal(str(
                order.get("remainingQuantity", order.get("quantity", "0")) or "0"
            ))
            if remaining <= 0:
                continue
            side = str(order.get("side", "")).lower()
            if side == "buy":
                price = Decimal(str(order.get("price", "0") or "0"))
                total_value += remaining * price
            elif side == "sell":
                mark = self.market_state.get(market)
                if mark is not None and mark.mid:
                    total_value += remaining * mark.mid
        realized = sum((inv.realized_pnl_usd for inv in inv_view.values()), Decimal(0))
```

### Numeric-id cancel guard (avoids the Finding 10 clientOrderId 400)

```python
    order_id = self.client_order_to_order_id.get(cancel.order_id, cancel.order_id)
    if not str(order_id).isdigit():
        # A coid that never resolved to an exchange id means the order
        # never reached the book. The cancel endpoint only accepts numeric
        # ids - a DELETE here 400s for an order that doesn't exist.
        log.warning("engine.cancel_skipped_unresolved_id",
                    requested_id=str(cancel.order_id))
        return
```

---

## Appendix — source artifacts (for protocol-team verification)


```text
Vault deposit
  approval_tx:   0xf7b59b8a279c22b613e219f0db7efae72aab02287235a5b91d6eefed60968995
  deposit_tx:    0x82c2a8dcfca9c5b5905d2ef91d7713030b0b9845b5fa30bbc6a6e84728c2b3a5

Maker orders posted live (yield_maker test)
  buy_run1:      0xe39ba7d625586dc2eec336d3a892fdb6ce3b1d16136f646bdb9ec0c19425f19d
  buy_run2:      0x7b2633d5c03e05cd56a240369fd588fbb58c8620f997e2fa927b958093aaab1d
  sell_run2:    0x13b86c25626671c228c7468171caf514d0d143e114a5d51d0f41cf05b5fe598a

Order cancellations
  cancel_ask:    0xe4909e5034137b78e93a70b99a82e9a2be2c0bae7a58e985a1ccb136f44eb5ee
  cancel_bid:    0x1e440e1db6b065c1fed87db2dc816e0b1d9ff6401b199fcaca9ac83008284eb4

Flatten leftover USDC.e to USDso (IOC sell)
  flatten_tx:    0x7db27d07f54b470cc3db3ccebb682f3fde982fb7ddba649240b7fed670638061

Vault withdraw revert (custom error decoded in Finding 3)
  selector:      0xcf479181
  have:          0x000e35fa931a0000  =  0.004 USDso
  want:          0x8ac7230489e80000  = 10.000 USDso

Cancel reverts on freshly placed orders (Finding 10, all status=0 on-chain)
  order_a:       147573952589684098111
  place_a:       0xc9d17149749cd7c4631701eeb2bac104692aadaafa3fd5ca9106d9116d2b147b
  cancel_a_1:    0xd9b33dadd35c9ffc269f78af2d103972edb98f4ae7eae6839c7ee430f9f943d8  gasUsed=197341
  cancel_a_2:    0x6ca9ea1a54b5f1e5a57c539671e581a10d2807ed8bebb3ad052cfdbbb918bc94  gasUsed=197341
  cancel_a_ok:   0x999d4a9f778ee55be3352fdb3071d2531d3c5aff7feb0dc0021e2449c0de8f4e  status=1 gasUsed=199832
  order_b:       184467440737103201374
  place_b:       0xaeb6c4cb7be24df534ed1be80cb1c4a5d01a12e91dce6f423e727d7963a314f1
  cancel_b_1:    0x4ce007d19557b47ca969dc1b523688174e42b53a14a60dcad14eb36a2f2974ad  gasUsed=30484
  cancel_b_2:    0x9d7f5ac74d0746044e0af05977f83936317e29741a60caeabb06eb5673472bd5  gasUsed=30484

Cancel reverts at ~70 min of order age (Finding 10 update)
  order_c:       239807672958231966995
  cancel_c_fail: 0xab52522be8eac061a7fcbcf949fa9234a4750294f0ecc28c3adcc40d7f823264  status=0
  cancel_c_ok:   0x7c8ddeb395eff91decfb9e881c1a7e2e5968d7a83b49a8f3b551dd1e1bcf441a  status=1

Insufficient-collateral placement revert (Finding 5 addendum)
  selector:      0xe450d38c
  wallet:        0x4258950186a12492bf805f2b9d7facd202921f34
  have:          0x7ecd734e8031e000   ≈  9.136 USDso
  want:          0x1156b7a71c0e1e000  ≈ 19.992 USDso

WS snapshot staleness at subscribe (Finding 9, from structured logs)
  06:59:25.465   ws.connected
  06:59:25.658   ws_book_stale_replaced  USDC.e:USDso  drift_bps=15.0000
  06:59:25.770   ws_book_stale_replaced  WBTC:USDso    drift_bps=15.0061

Native-SOMI BUY gas-limit guard (Finding 5 addendum 2)
  selector:          0x782b2567 = InsufficientGasForPayout(uint256 gasLeft)
  our buy tx:        bce80ca51a24e1541a72901e1701edc537324f1f9365bcfa1e9b26d3edff60ca  status=1 (SOMI bought with USDso, >=5M gas)
  reverting client:  hard-coded 3,000,000 gas limit
  team confirmation: needs ~>=5,000,000 gas; native-base BUY supported, fix is client-side (raise + sim at same limit)
```

---

*Raw structured logs available on request for any finding above.*
