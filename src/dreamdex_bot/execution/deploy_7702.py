"""Compile + deploy the DreamDexVolumeBatch7702 implementation once per network.

The implementation is stateless — every wallet delegates to the SAME deployed
address via its own EIP-7702 authorization — so this runs once per network and
the address is reused (set IMPL_ADDRESS_<NET> in .env afterwards).
"""
from __future__ import annotations

from pathlib import Path

from web3 import Web3

from dreamdex_core.client import ChainContext
from dreamdex_core.nonce import NonceManager

_CONTRACT_SRC = Path(__file__).resolve().parents[3] / "contracts" / "DreamDexVolumeBatch7702.sol"
_SOLC_VERSION = "0.8.20"


def compile_impl() -> tuple[list, str]:
    """Return (abi, bytecode_hex). Requires py-solc-x; installs solc on first use."""
    import solcx

    try:
        solcx.set_solc_version(_SOLC_VERSION)
    except Exception:
        solcx.install_solc(_SOLC_VERSION)

    out = solcx.compile_source(
        _CONTRACT_SRC.read_text(),
        output_values=["abi", "bin"],
        solc_version=_SOLC_VERSION,
        optimize=True,
        optimize_runs=200,
    )
    key = next(k for k in out if k.endswith(":DreamDexVolumeBatch7702"))
    return out[key]["abi"], out[key]["bin"]


def deploy_impl(ctx: ChainContext, nm: NonceManager, *, gas: int | None = None) -> str:
    """Deploy the implementation and return its checksummed address.

    Somnia's gas accounting is heavy, so a small contract can still out-gas a
    naive fixed limit — estimate with generous headroom and a high fallback."""
    abi, bytecode = compile_impl()
    factory = ctx.w3.eth.contract(abi=abi, bytecode=bytecode)
    if gas is None:
        try:
            est = factory.constructor().estimate_gas({"from": ctx.address})
            gas = max(int(est * 1.5), 3_000_000)
        except Exception:
            gas = 8_000_000
    tx = factory.constructor().build_transaction(
        {
            "from": ctx.address,
            "nonce": nm.reserve(),
            "gas": gas,
            "chainId": ctx.net.chain_id,
            **nm.gas_fields(),
        }
    )
    signed = ctx.account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    try:
        h = ctx.w3.eth.send_raw_transaction(raw)
        receipt = ctx.w3.eth.wait_for_transaction_receipt(h, timeout=120)
    except Exception:
        nm.reset()
        raise
    if receipt["status"] != 1 or not receipt["contractAddress"]:
        raise RuntimeError(f"impl deploy failed: status={receipt['status']} tx={receipt['transactionHash'].hex()}")
    return Web3.to_checksum_address(receipt["contractAddress"])
