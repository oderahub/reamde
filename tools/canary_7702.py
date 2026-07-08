"""Testnet canary for the EIP-7702 atomic round-trip.

Deploys the impl (or reuses IMPL_ADDRESS), plans a single small round-trip on a
liquid ERC-20 pair, prints it, and — only with --broadcast — fires it live and
reports status/logs/gas/volume.

    # dry run: build + print the plan, deploy nothing, broadcast nothing
    NETWORK=testnet .venv/bin/python tools/canary_7702.py --symbol WETH:USDso --size-usdso 5

    # live on testnet: deploy impl if needed, then fire ONE round-trip
    NETWORK=testnet .venv/bin/python tools/canary_7702.py --symbol WETH:USDso --size-usdso 5 --broadcast

`logs > 0` on a status=1 receipt is the success signal — logs==0 means the 7702
delegation silently didn't run (the classic self-sponsored-nonce mistake).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dreamdex_core import Pool, create_chain_context, from_raw  # noqa: E402
from dreamdex_core.contract import ERC20_ABI  # noqa: E402
from dreamdex_core.nonce import NonceManager  # noqa: E402
from web3 import Web3  # noqa: E402

from dreamdex_bot.execution import round_trip_7702 as rt  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="WETH:USDso")
    ap.add_argument("--size-usdso", type=float, default=5.0)
    ap.add_argument("--cross-bps", type=float, default=5.0)
    ap.add_argument("--gas", type=int, default=rt.DEFAULT_ROUND_TRIP_GAS)
    ap.add_argument("--broadcast", action="store_true", help="actually send (default: dry run)")
    args = ap.parse_args()

    ctx = create_chain_context()
    nm = NonceManager(ctx.w3, ctx.address)
    print(f"network={ctx.net.name} chain={ctx.net.chain_id} wallet={ctx.address}")
    print(f"STT gas={from_raw(ctx.w3.eth.get_balance(ctx.address), 18):.4f}")

    pool = Pool.load(ctx, args.symbol)
    if pool.base_is_native:
        raise SystemExit("Refusing: 7702 fixed-gas round-trip is ERC-20 pairs only (native needs >=5M).")
    q = ctx.w3.eth.contract(address=Web3.to_checksum_address(pool.params.quote_token), abi=ERC20_ABI)
    qbal = from_raw(q.functions.balanceOf(ctx.address).call(), pool.quote_decimals)
    print(f"{args.symbol}: taker_fee_x1k={pool.params.taker_fee_bps_times1k} wallet_USDso={qbal:.4f}")

    plan = rt.plan_round_trip(pool, size_usdso=args.size_usdso, cross_bps=args.cross_bps)
    if plan is None:
        raise SystemExit("No plan: book one-sided or size below one lot.")
    print(
        f"PLAN {plan.symbol}: qty={plan.qty} notional≈${plan.notional_usdso:.2f} "
        f"buy@{plan.buy_price} sell@{plan.sell_price} (bid={plan.best_bid} ask={plan.best_ask}, cross={plan.cross_bps}bp)"
    )

    impl = os.environ.get("IMPL_ADDRESS")
    if not args.broadcast:
        signed = rt.send_round_trip(ctx, nm, impl or "0x" + "11" * 20, plan, gas=args.gas, dry_run=True)
        print(f"DRY RUN ok — signed tx hash would be {signed.tx_hash} (nothing broadcast).")
        print("Re-run with --broadcast to deploy (if needed) and fire it live.")
        return

    if not impl:
        from dreamdex_bot.execution.deploy_7702 import deploy_impl

        print("deploying impl (no IMPL_ADDRESS set)…")
        impl = deploy_impl(ctx, nm)
        print(f"impl deployed at {impl}  →  set IMPL_ADDRESS={impl} to reuse")

    print(f"broadcasting round-trip via impl {impl} …")
    res = rt.send_round_trip(
        ctx, nm, impl, plan, gas=args.gas, dry_run=False,
        base_decimals=pool.base_decimals, quote_decimals=pool.quote_decimals,
    )
    print(
        f"RESULT status={res.status} logs={res.logs} gasUsed={res.gas_used} "
        f"volume≈${res.volume_usdso:.2f} tx={res.tx_hash}"
    )
    if res.status == 1 and res.logs == 0:
        print("⚠️  status=1 but logs=0 — delegation did NOT run (7702 nonce issue). Investigate.")
    elif res.status == 1:
        print("✅ round-trip filled — 7702 path works end-to-end on-chain.")


if __name__ == "__main__":
    main()
