# DreamDEX Dev Traders Program — Feedback & Findings

Running log of API/docs/protocol issues found while building and operating our
bot during the 14-day Dev Traders Program. Each entry: what we observed, why it
matters to a bot author, and a suggested fix. Cross-checked against on-chain
state or a captured artifact, not speculation.

Participant: trader-4 · wallet `0x99e98338320F0485D1fb2553Dd5C85345783D1A5`

---

## Finding 1 — Leaderboard PnL column is a stale / mid-cycle snapshot (recurs from the Alpha cohort)

**What we observed.** The public leaderboard listed `trader-1`
(`0x703e10344158d7C6CB943596328211a0a22422F6`) with **PnL = −$80.45** on
$9,018 volume. An on-chain read at the same moment showed that wallet holding
**149.20 USDso and 49.57 SOMI** — i.e. a realized loss of only **~$0.80**, not
$80. The −$80 is the wallet sampled *mid-round-trip*, while it held ~$80 of base
inventory (WBTC/WETH) that the PnL formula (`USDso balance − 150`) does not mark
to market.

**Why it matters.** PnL becomes unreadable for any bot that carries inventory
between two legs — it swings by the full clip size depending on the snapshot
instant, and makes cross-trader comparison meaningless. It also invites wrong
conclusions (we briefly mis-read trader-1 as bleeding 89 bp/volume when he is
actually ~0.9 bp/volume and near-flat).

**Suggested fix.** Mark base inventory at mid and include it in PnL, or take the
snapshot only in a quiescent window after each state change, or show a separate
"base inventory at mid" column. (This is the same issue we reported in the Alpha
cohort — it has not been addressed.)

---

## Finding 2 — "Effective Volume" is undocumented; its formula is unknowable to participants

**What we observed.** The leaderboard added an **Effective Volume** column that
materially discounts raw volume (`trader-1`: $9,018 raw → $4,181 effective, a
~54% haircut). Nothing in the program rules, the trading docs, or the bot kit
defines how it is computed or whether **rank/rewards** are scored on raw or
effective volume. The rules say the KPI is "trading volume generated" and
milestones pay per "500k USDso volume" (implying raw), yet the leaderboard's most
prominent adjusted metric is effective — a decision-blocking ambiguity for anyone
choosing a strategy.

**Why it matters.** Whether a round-trip / market-making / directional strategy is
optimal flips entirely on this definition. Participants cannot rationally allocate
capital without it.

**Suggested fix.** Publish the effective-volume formula (or at least the
wash/self-trade discount rules) and state explicitly which column determines
rank and milestone rewards.

**Update (resolved for our case).** Effective volume is NOT a large haircut on
real-book trading: once the leaderboard settled, our effective/raw was **99.96%**
and trader-1's **99.39%**. The apparent 54% haircut we first saw
($4,181 / $9,018) was a *transient*: the Effective Volume column updates on a
slower cadence than raw Volume, so mid-update it trails and looks discounted.

---

## Finding 3 — Effective Volume lags raw Volume, producing misleading intermediate readings

**What we observed.** Immediately after a burst of trades, the leaderboard shows
raw Volume updated but Effective Volume still at an older, much lower value
(trader-1: $9,018 raw vs $4,181 effective — a phantom 54% haircut — which later
converged to $12,210 vs $12,136, i.e. ~99%). The two columns are not sampled
atomically.

**Why it matters.** This nearly caused us to abandon an efficient strategy based
on a transient artifact. Any participant checking the board mid-update will
misjudge whether their volume "counts," and could make a bad strategic pivot.

**Suggested fix.** Update raw and effective volume from the same snapshot, or
label effective volume with its "as of" timestamp so participants know it trails.

---

## Positive note — core trading path is solid on mainnet

15 EIP-7702 atomic round-trips (buy+sell in one tx) on WBTC/WETH executed
flawlessly: every tx `status=1` with fills, inventory returned exactly flat, and
realized cost was **0.88 bp per dollar of volume** (fees are 0/0; cost is just the
crossed spread). The `placeOrder` auto-pull model, the ≥5M native-gas guard, and
type-4 transactions on Somnia mainnet all worked as documented.

<!-- Add findings below as we hit them. -->
