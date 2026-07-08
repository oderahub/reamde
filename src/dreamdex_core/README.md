# dreamdex_core (vendored)

This package is the **DreamDEX team's own Python core**, vendored from the
official bot kit:

- Source: https://github.com/somnia-chain/dreamdex-bot-kit (`packages/core-py/dreamdex_core`)
- License: MIT (see the per-file `@license` headers; upstream `LICENSE` applies)

It is vendored unmodified so our EIP-7702 round-trip path
(`dreamdex_bot.execution`) reuses the protocol team's reference implementation
of the order lifecycle, gotcha guards, pinned event topics, pool-params
decoding, and native-buy gas floor — rather than re-deriving them.

Do not hand-edit these files; re-sync from upstream if the kit updates.
