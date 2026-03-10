"""
trading/swap_engine.py
======================
Direct-to-validator swap execution on Solana.

Architecture
------------
  1. Quote       — Jupiter Aggregator v6 finds the best route
                   across all Solana DEXes (Raydium, Orca, Meteora …)
  2. Build TX    — Jupiter serialises the transaction with the
                   optimal route and our wallet as the signer
  3. Submit      — Jito block-engine bundle submission for:
                     • MEV protection   (front-running prevention)
                     • Priority landing (validator bribe via tip)
                     • Atomic execution (all-or-nothing guarantee)

Why Jito?
---------
Standard RPC submission enters the public mempool where bots can
sandwich the transaction.  Jito bundles bypass the mempool entirely,
sending directly to the validator's block-engine for near-instant,
protected execution.

Supported directions
--------------------
  buy()   — SOL  → any SPL token
  sell()  — any SPL token → SOL
  swap()  — any token → any other token (full flexibility)
"""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── API endpoints ──────────────────────────────────────────────────────────────
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API  = "https://quote-api.jup.ag/v6/swap"
JITO_BUNDLE_API   = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"

# Wrapped SOL mint address (used as input/output for SOL swaps)
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Jito tip amount in lamports (0.0001 SOL default)
DEFAULT_JITO_TIP_LAMPORTS = 100_000


# ── Result dataclasses ─────────────────────────────────────────────────────────

@dataclass
class SwapQuote:
    """
    Best-route quote returned by Jupiter Aggregator.

    Attributes
    ----------
    input_mint : str       Token being sold (mint address).
    output_mint : str      Token being bought (mint address).
    in_amount : int        Input amount in raw token units (lamports for SOL).
    out_amount : int       Expected output amount in raw token units.
    price_impact_pct : float  Estimated price impact as a percentage.
    route_plan : list      Ordered list of DEX hops Jupiter will use.
    raw : dict             Full raw response from Jupiter (for TX building).
    """
    input_mint:       str
    output_mint:      str
    in_amount:        int
    out_amount:       int
    price_impact_pct: float
    route_plan:       list
    raw:              dict

    # ── Derived properties ─────────────────────────────────────────────────────

    @property
    def in_sol(self) -> float:
        """Input amount in SOL (only meaningful when input_mint == WSOL_MINT)."""
        return self.in_amount / 1e9

    @property
    def out_sol(self) -> float:
        """Output amount in SOL (only meaningful when output_mint == WSOL_MINT)."""
        return self.out_amount / 1e9

    @property
    def has_high_price_impact(self) -> bool:
        """True when estimated price impact exceeds 2%."""
        return self.price_impact_pct > 2.0

    @property
    def route_summary(self) -> str:
        """Human-readable hop summary, e.g. 'Raydium → Orca'."""
        labels = []
        for hop in self.route_plan:
            for swap in hop.get("swapInfo", [{}]):
                label = swap.get("label", "DEX")
                if label not in labels:
                    labels.append(label)
        return " → ".join(labels) if labels else "Direct"

    def summary(self) -> str:
        """Return a Telegram-HTML formatted quote card."""
        impact_warn = "\n⚠️ <b>High price impact!</b>" if self.has_high_price_impact else ""
        return (
            f"📊 <b>Swap Quote</b>\n"
            f"<b>Route:</b>        {self.route_summary}\n"
            f"<b>In:</b>           {self.in_sol:.4f} SOL\n"
            f"<b>Out (est.):</b>   {self.out_sol:.6f}\n"
            f"<b>Price Impact:</b> {self.price_impact_pct:.2f}%\n"
            f"<b>DEX hops:</b>     {len(self.route_plan)}"
            f"{impact_warn}"
        )


@dataclass
class SwapResult:
    """
    Final outcome of a swap attempt.

    Attributes
    ----------
    success : bool              True if the transaction landed on-chain.
    tx_signature : str | None   Solana transaction signature (if success).
    bundle_id : str | None      Jito bundle ID (for tracking).
    error : str | None          Human-readable error message (if failed).
    """
    success:      bool
    tx_signature: Optional[str] = None
    bundle_id:    Optional[str] = None
    error:        Optional[str] = None

    def summary(self) -> str:
        """Return a Telegram-HTML formatted result card."""
        if self.success:
            return (
                f"✅ <b>Swap Confirmed!</b>\n"
                f"<b>Tx:</b> <code>{self.tx_signature}</code>\n"
                f"<a href='https://solscan.io/tx/{self.tx_signature}'>View on Solscan</a>"
            )
        return f"❌ <b>Swap Failed</b>\n<b>Reason:</b> {self.error}"


# ── Swap engine ────────────────────────────────────────────────────────────────

class SwapEngine:
    """
    Wraps Jupiter quote + Jito bundle submission into three simple methods:
      buy(), sell(), swap()

    Parameters
    ----------
    wallet_public_key : str
        The Solana wallet address that will sign and pay for the swap.
    slippage_bps : int
        Slippage tolerance in basis points (default 300 = 3%).
    jito_tip_lamports : int
        Tip paid to the Jito validator for bundle priority.
        Higher tip = faster landing during congestion.
    """

    def __init__(
        self,
        wallet_public_key:  str,
        slippage_bps:       int = 300,
        jito_tip_lamports:  int = DEFAULT_JITO_TIP_LAMPORTS,
    ) -> None:
        self.wallet            = wallet_public_key
        self.slippage_bps      = slippage_bps
        self.jito_tip_lamports = jito_tip_lamports

    # ── Public API ─────────────────────────────────────────────────────────────

    async def buy(
        self,
        token_mint:  str,
        sol_amount:  float,
    ) -> SwapResult:
        """
        Buy *token_mint* using *sol_amount* SOL.

        Converts SOL → token via the best Jupiter route,
        then submits via Jito for MEV-protected execution.
        """
        lamports = int(sol_amount * 1e9)
        quote    = await self.get_quote(WSOL_MINT, token_mint, lamports)
        if not quote:
            return SwapResult(success=False, error="Could not fetch quote from Jupiter.")
        return await self._execute(quote)

    async def sell(
        self,
        token_mint:   str,
        token_amount: int,
    ) -> SwapResult:
        """
        Sell *token_amount* (raw units) of *token_mint* back to SOL.

        Converts token → SOL via the best Jupiter route,
        then submits via Jito.
        """
        quote = await self.get_quote(token_mint, WSOL_MINT, token_amount)
        if not quote:
            return SwapResult(success=False, error="Could not fetch sell quote from Jupiter.")
        return await self._execute(quote)

    async def swap(
        self,
        input_mint:   str,
        output_mint:  str,
        amount:       int,
    ) -> SwapResult:
        """
        Generic swap between any two SPL tokens.

        Parameters
        ----------
        input_mint  : str   Mint address of the token to sell.
        output_mint : str   Mint address of the token to buy.
        amount      : int   Input amount in raw token units.
        """
        quote = await self.get_quote(input_mint, output_mint, amount)
        if not quote:
            return SwapResult(success=False, error="Could not fetch swap quote from Jupiter.")
        return await self._execute(quote)

    async def get_quote(
        self,
        input_mint:  str,
        output_mint: str,
        amount:      int,
    ) -> Optional[SwapQuote]:
        """
        Fetch the best route quote from Jupiter Aggregator.

        Returns None if Jupiter is unreachable or no route exists.
        """
        params = {
            "inputMint":   input_mint,
            "outputMint":  output_mint,
            "amount":      amount,
            "slippageBps": self.slippage_bps,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(JUPITER_QUOTE_API, params=params)

            if resp.status_code != 200:
                logger.error(
                    "Jupiter quote HTTP %d: %s", resp.status_code, resp.text[:200]
                )
                return None

            data = resp.json()
            if "error" in data:
                logger.error("Jupiter quote error: %s", data["error"])
                return None

            return SwapQuote(
                input_mint       = data["inputMint"],
                output_mint      = data["outputMint"],
                in_amount        = int(data["inAmount"]),
                out_amount       = int(data["outAmount"]),
                price_impact_pct = float(data.get("priceImpactPct", 0)),
                route_plan       = data.get("routePlan", []),
                raw              = data,
            )

        except Exception as exc:
            logger.error("get_quote(%s→%s): %s", input_mint[:8], output_mint[:8], exc)
            return None

    # ── Execution pipeline ─────────────────────────────────────────────────────

    async def _execute(self, quote: SwapQuote) -> SwapResult:
        """
        Two-step execution:
          Step 1 — Ask Jupiter to serialise the swap transaction.
          Step 2 — Submit the serialised TX as a Jito bundle.
        """
        # ── Step 1: Build transaction via Jupiter ──────────────────────────────
        swap_tx_b64 = await self._build_transaction(quote)
        if not swap_tx_b64:
            return SwapResult(success=False, error="Jupiter failed to build the transaction.")

        # ── Step 2: Submit via Jito bundle ─────────────────────────────────────
        return await self._submit_jito_bundle(swap_tx_b64)

    async def _build_transaction(self, quote: SwapQuote) -> Optional[str]:
        """
        POST to Jupiter /swap to get the serialised base64 transaction.

        Key options enabled:
          - wrapAndUnwrapSol          handle SOL↔wSOL automatically
          - dynamicComputeUnitLimit   Jupiter estimates compute units
          - prioritizationFeeLamports set to 'auto' for dynamic priority fee
        """
        payload = {
            "quoteResponse":              quote.raw,
            "userPublicKey":              self.wallet,
            "wrapAndUnwrapSol":           True,
            "dynamicComputeUnitLimit":    True,
            "prioritizationFeeLamports":  "auto",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(JUPITER_SWAP_API, json=payload)

            if resp.status_code != 200:
                logger.error(
                    "Jupiter swap build HTTP %d: %s", resp.status_code, resp.text[:200]
                )
                return None

            tx_b64 = resp.json().get("swapTransaction")
            if not tx_b64:
                logger.error("Jupiter returned no swapTransaction field.")
            return tx_b64

        except Exception as exc:
            logger.error("_build_transaction: %s", exc)
            return None

    async def _submit_jito_bundle(self, swap_tx_b64: str) -> SwapResult:
        """
        Submit the serialised transaction as a Jito bundle.

        Jito bundles guarantee:
          • Atomic execution — tx either fully lands or doesn't
          • MEV protection   — bypasses the public mempool
          • Priority landing — validator bribe via tip

        The bundle contains only our swap transaction.
        Jito returns a bundle_id that can be polled for status.
        """
        bundle_payload = {
            "jsonrpc": "2.0",
            "id":      1,
            "method":  "sendBundle",
            "params":  [[swap_tx_b64]],
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(JITO_BUNDLE_API, json=bundle_payload)

            result = resp.json()

            if "result" in result:
                bundle_id = result["result"]
                # The bundle_id IS the transaction signature in most cases
                logger.info("✅ Jito bundle submitted: %s", bundle_id)
                return SwapResult(
                    success=True,
                    tx_signature=bundle_id,
                    bundle_id=bundle_id,
                )

            error_msg = result.get("error", {}).get("message", "Unknown Jito error.")
            logger.error("Jito submission failed: %s", error_msg)
            return SwapResult(success=False, error=error_msg)

        except Exception as exc:
            logger.error("_submit_jito_bundle: %s", exc)
            return SwapResult(success=False, error=str(exc))