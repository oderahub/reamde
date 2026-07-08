"""EIP-7702 atomic buy->sell round-trip execution.

The whole point for the Dev Traders Program: manufacture volume at the lowest
possible cost-per-dollar. One type-4 (set-code) transaction delegates the wallet
to `DreamDexVolumeBatch7702` and runs `atomicRoundTrip`, which IOC-buys and then
IOC-sells exactly what it bought — two fills, one signature, one gas payment,
inventory back to flat. Flat inventory is the key property: with no drift between
legs, the $150 bankroll only bleeds the (tiny) round-trip spread, so it stretches
to the maximum lifetime volume.

This module owns only the on-chain mechanics of the round-trip. Pricing/sizing,
the momentum gate, the burn-to-floor loop, and the 24h keepalive live in the
strategy loop that calls `send_round_trip`.

Built on the vendored `dreamdex_core` (the protocol team's own Python core), so
tick/lot quantization, the native-buy gas floor, and the pinned event topics all
match the reference implementation.

The one 7702 subtlety (mirrors the kit's `executor: "self"`): when the account
that signs the authorization also SENDS the transaction, the tx consumes nonce N
and the authorization must be signed at nonce N+1. Processing then advances the
account nonce by TWO (N from the tx, N+1 from the applied authorization), so the
caller must reserve two nonces per round-trip.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from web3 import Web3

from dreamdex_core.client import ChainContext
from dreamdex_core.gotchas import build_expire_ns
from dreamdex_core.nonce import NonceManager
from dreamdex_core.pool import Pool
from dreamdex_core.quant import align_to_lot, align_to_tick, from_raw, to_raw

# Default broadcast gas limit for an atomic buy+sell. The kit measures ~2.3M used
# for a round-trip on an ERC-20 pair; 6M leaves comfortable headroom. ERC-20 pairs
# only — a native-SOMI leg would need the >=5M payout floor and shouldn't ride a
# fixed-gas round-trip (see dreamdex_core gotchas #4).
DEFAULT_ROUND_TRIP_GAS = 6_000_000

# Minimal ABI for the one function we call on our (delegated) own address.
IMPL_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "atomicRoundTrip",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "quoteToken", "type": "address"},
            {"name": "baseToken", "type": "address"},
            {"name": "buyPrice", "type": "uint256"},
            {"name": "sellPrice", "type": "uint256"},
            {"name": "quantity", "type": "uint256"},
            {"name": "expireTimestampNs", "type": "uint64"},
        ],
        "outputs": [],
    }
]

# RoundTrip(address indexed pool, uint256 boughtBase, uint256 buyPrice, uint256 sellPrice)
TOPIC_ROUND_TRIP = Web3.keccak(text="RoundTrip(address,uint256,uint256,uint256)").hex()


@dataclass
class RoundTripPlan:
    """Everything needed to submit one round-trip — pure, no chain writes."""

    symbol: str
    pool: str
    quote_token: str
    base_token: str
    buy_price_raw: int
    sell_price_raw: int
    qty_raw: int
    expire_ns: int
    # Human-unit echoes for logging / sanity.
    best_bid: float
    best_ask: float
    buy_price: float
    sell_price: float
    qty: float
    notional_usdso: float
    cross_bps: float


@dataclass
class RoundTripResult:
    tx_hash: str
    status: int
    logs: int
    gas_used: int
    bought_base_raw: int | None
    # Realized notional traded, in USDso, summed across both fills (buy + sell).
    volume_usdso: float
    dry_run: bool = False


def plan_round_trip(
    pool: Pool,
    *,
    size_usdso: float,
    cross_bps: float,
    expire_ms: int = 5 * 60_000,
) -> RoundTripPlan | None:
    """Price and size a round-trip from the live top of book. Returns None if the
    book is one-sided or the size rounds below one lot (nothing to trade)."""
    tob = pool.top_of_book()
    if tob.best_bid is None or tob.best_ask is None:
        return None

    # Cross both touches by cross_bps so each leg actually fills (gotcha #9: a
    # taker priced exactly at the touch often fails to cross).
    buy_price_raw = align_to_tick(
        to_raw(tob.best_ask * (1 + cross_bps / 10_000), pool.quote_decimals),
        pool.params.tick_size,
        "ask",
    )
    sell_price_raw = align_to_tick(
        to_raw(tob.best_bid * (1 - cross_bps / 10_000), pool.quote_decimals),
        pool.params.tick_size,
        "bid",
    )
    qty_raw = align_to_lot(
        to_raw(size_usdso / tob.best_ask, pool.base_decimals), pool.params.lot_size
    )
    if qty_raw < pool.params.min_quantity or qty_raw <= 0:
        return None

    qty = from_raw(qty_raw, pool.base_decimals)
    return RoundTripPlan(
        symbol=pool.symbol,
        pool=pool.address,
        quote_token=Web3.to_checksum_address(pool.params.quote_token),
        base_token=Web3.to_checksum_address(pool.params.base_token),
        buy_price_raw=buy_price_raw,
        sell_price_raw=sell_price_raw,
        qty_raw=qty_raw,
        expire_ns=build_expire_ns(expire_ms),
        best_bid=tob.best_bid,
        best_ask=tob.best_ask,
        buy_price=from_raw(buy_price_raw, pool.quote_decimals),
        sell_price=from_raw(sell_price_raw, pool.quote_decimals),
        qty=qty,
        notional_usdso=qty * tob.best_ask,
        cross_bps=cross_bps,
    )


def _encode_calldata(ctx: ChainContext, plan: RoundTripPlan) -> str:
    impl = ctx.w3.eth.contract(abi=IMPL_ABI)
    return impl.encode_abi(
        "atomicRoundTrip",
        args=[
            Web3.to_checksum_address(plan.pool),
            plan.quote_token,
            plan.base_token,
            plan.buy_price_raw,
            plan.sell_price_raw,
            plan.qty_raw,
            plan.expire_ns,
        ],
    )


def send_round_trip(
    ctx: ChainContext,
    nm: NonceManager,
    impl_address: str,
    plan: RoundTripPlan,
    *,
    gas: int = DEFAULT_ROUND_TRIP_GAS,
    dry_run: bool = True,
    base_decimals: int = 18,
    quote_decimals: int = 18,
) -> RoundTripResult:
    """Delegate the wallet to `impl_address` and run `atomicRoundTrip` in one
    type-4 tx. With `dry_run=True` (default) it builds and signs everything but
    does NOT broadcast — use it to eyeball the exact tx before going live.

    Reserves TWO nonces: N for the transaction, N+1 for the self-sponsored
    authorization (which the tx also consumes).
    """
    data = _encode_calldata(ctx, plan)

    tx_nonce = nm.reserve()
    auth_nonce = nm.reserve()  # must be tx_nonce + 1; the tx consumes it too
    assert auth_nonce == tx_nonce + 1, "nonce manager handed out non-consecutive nonces"

    signed_auth = ctx.account.sign_authorization(
        {
            "chainId": ctx.net.chain_id,
            "address": Web3.to_checksum_address(impl_address),
            "nonce": auth_nonce,
        }
    )

    tx = {
        "chainId": ctx.net.chain_id,
        "nonce": tx_nonce,
        "to": ctx.address,  # call our own now-delegated address
        "value": 0,
        "gas": gas,
        "data": data,
        "authorizationList": [signed_auth],
        **nm.gas_fields(),
    }

    signed = ctx.account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction

    if dry_run:
        return RoundTripResult(
            tx_hash="0x" + signed.hash.hex().lstrip("0x"),
            status=-1,
            logs=0,
            gas_used=0,
            bought_base_raw=None,
            volume_usdso=0.0,
            dry_run=True,
        )

    try:
        h = ctx.w3.eth.send_raw_transaction(raw)
        receipt = ctx.w3.eth.wait_for_transaction_receipt(h, timeout=120)
    except Exception:
        nm.reset()  # burn nonces; force a fresh sync next reserve
        raise

    bought_base_raw, volume_usdso = _parse_round_trip(
        receipt, base_decimals, quote_decimals
    )
    # logs==0 on a succeeded tx = the delegation silently didn't run (7702 caveat).
    return RoundTripResult(
        tx_hash=receipt["transactionHash"].hex(),
        status=receipt["status"],
        logs=len(receipt["logs"]),
        gas_used=receipt["gasUsed"],
        bought_base_raw=bought_base_raw,
        volume_usdso=volume_usdso,
    )


def _parse_round_trip(
    receipt: Any, base_decimals: int, quote_decimals: int
) -> tuple[int | None, float]:
    """Value the round-trip from our impl's own `RoundTrip` event, which reliably
    carries the realized `boughtBase` (handles partial fills) plus both leg prices.
    Counted volume = boughtBase × (buyPrice + sellPrice), i.e. the buy notional plus
    the sell notional, both in USDso. Returns (bought_base_raw, volume_usdso).

    We value from RoundTrip rather than the pool's OrderFilled logs on purpose: the
    contract emits RoundTrip itself, so its layout is fixed and can't drift with a
    protocol upgrade the way the shared OrderFilled event has (gotcha #10)."""
    want_rt = TOPIC_ROUND_TRIP.lower().lstrip("0x")
    for log in receipt["logs"]:
        topics = log.get("topics", [])
        if not topics:
            continue
        if topics[0].hex().lower().lstrip("0x") != want_rt:
            continue
        # RoundTrip(indexed pool, boughtBase, buyPrice, sellPrice) — 3 non-indexed.
        fields = _decode_words(log["data"])
        if len(fields) < 3:
            continue
        bought_base_raw, buy_price_raw, sell_price_raw = fields[0], fields[1], fields[2]
        bought = from_raw(bought_base_raw, base_decimals)
        buy_px = from_raw(buy_price_raw, quote_decimals)
        sell_px = from_raw(sell_price_raw, quote_decimals)
        return bought_base_raw, bought * (buy_px + sell_px)
    return None, 0.0


def _decode_words(data: Any) -> list[int]:
    """Split ABI-encoded non-indexed log data into 32-byte uint words."""
    if isinstance(data, (bytes, bytearray)):
        b = bytes(data)
    else:
        s = data[2:] if isinstance(data, str) and data.startswith("0x") else str(data)
        b = bytes.fromhex(s)
    return [int.from_bytes(b[i : i + 32], "big") for i in range(0, len(b), 32)]
