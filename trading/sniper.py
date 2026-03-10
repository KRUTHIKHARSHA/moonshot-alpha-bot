"""
trading/sniper.py
=================
Auto-Sniper — automated token entry engine with two distinct
personality modes designed for different risk appetites.

Modes
-----
SAFE MODE
  Full forensic pipeline before every order:
  1. rug_interceptor scan     — blocks tokens with critical flags
  2. Liquidity floor check    — rejects pools below min_liquidity_usd
  3. Risk-score gate          — rejects anything above max_risk_score
  All three gates must pass before an order is created.

DEGEN MODE
  Speed over safety — fires on any token that clears a single
  minimum-liquidity check.  Suitable for high-frequency sniping
  where the user accepts higher risk for faster entries.

Order lifecycle
---------------
  pending  →  executed   (all checks passed, ready to send to SwapEngine)
  pending  →  rejected   (one or more checks blocked the trade)

Usage example
-------------
    config  = SniperConfig(mode=SniperMode.SAFE, buy_amount_sol=0.2)
    sniper  = AutoSniper(config=config, on_order=my_callback)
    await sniper.start()          # starts the background scan loop
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from scanner.token_scanner import TokenCandidate, TokenScanner
from trading.rug_interceptor import RugReport, run_rug_check

logger = logging.getLogger(__name__)


# ── Enums & config ─────────────────────────────────────────────────────────────

class SniperMode(Enum):
    SAFE  = "safe"
    DEGEN = "degen"


@dataclass
class SniperConfig:
    """
    All tunable parameters for the AutoSniper.

    Attributes
    ----------
    mode : SniperMode
        SAFE = full forensic checks; DEGEN = liquidity check only.
    buy_amount_sol : float
        SOL to spend per snipe (passed to the SwapEngine).
    slippage_bps : int
        Allowed slippage in basis points (100 bps = 1%).
    max_risk_score : int
        Maximum acceptable risk score from the rug interceptor.
        Only enforced in SAFE mode.  Range: 0-100.
    min_liquidity_usd : float
        Minimum USD liquidity required to enter a trade.
        Enforced in both modes.
    take_profit_pct : float
        Percentage gain target.  Passed to the GhostManager.
    stop_loss_pct : float
        Maximum acceptable loss before auto-sell.
        Passed to the GhostManager.
    scan_interval_sec : int
        Seconds between scanner poll cycles.
    """
    mode:               SniperMode = SniperMode.SAFE
    buy_amount_sol:     float      = 0.1
    slippage_bps:       int        = 300
    max_risk_score:     int        = 35       # SAFE mode only
    min_liquidity_usd:  float      = 10_000
    take_profit_pct:    float      = 100.0
    stop_loss_pct:      float      = 25.0
    scan_interval_sec:  int        = 5


# ── Order dataclass ────────────────────────────────────────────────────────────

@dataclass
class SniperOrder:
    """
    Represents a single snipe opportunity.

    status values
    -------------
    'pending'   — created, not yet evaluated
    'executed'  — passed all checks; ready for SwapEngine
    'rejected'  — blocked by one or more safety checks
    """
    token:         TokenCandidate
    config:        SniperConfig
    risk_report:   Optional[RugReport] = None
    status:        str                 = "pending"
    reject_reason: str                 = ""

    # ── Derived helpers ────────────────────────────────────────────────────────

    @property
    def is_executable(self) -> bool:
        return self.status == "executed"

    @property
    def risk_label(self) -> str:
        if self.risk_report:
            return self.risk_report.risk_level
        return "UNKNOWN"

    def summary(self) -> str:
        """Return a Telegram-HTML formatted order summary."""
        status_emoji = {
            "executed": "✅",
            "rejected": "❌",
            "pending":  "⏳",
        }.get(self.status, "❓")

        lines = [
            f"🔫 <b>Sniper Order  {status_emoji}</b>",
            f"<b>Token:</b>  {self.token.name} (${self.token.symbol})",
            f"<b>Chain:</b>  {self.token.chain.upper()}",
            f"<b>Mode:</b>   {self.config.mode.value.upper()}",
            f"<b>Amount:</b> {self.config.buy_amount_sol} SOL",
            f"<b>Status:</b> {self.status.upper()}",
        ]

        if self.risk_report:
            emoji = {"SAFE": "🟢", "LOW": "🟡", "MEDIUM": "🟠",
                     "HIGH": "🔴", "CRITICAL": "☠️"}.get(self.risk_label, "⚪")
            lines.append(
                f"<b>Risk:</b>   {emoji} {self.risk_label} "
                f"({self.risk_report.risk_score}/100)"
            )

        if self.status == "rejected":
            lines.append(f"\n<b>Reason:</b> {self.reject_reason}")

        lines += [
            f"\n<b>Entry price:</b> ${self.token.price_usd:.6f}",
            f"<b>Take Profit:</b> +{self.config.take_profit_pct:.0f}%",
            f"<b>Stop Loss:</b>   -{self.config.stop_loss_pct:.0f}%",
        ]
        return "\n".join(lines)


# ── AutoSniper ─────────────────────────────────────────────────────────────────

class AutoSniper:
    """
    Background scanning engine that evaluates token candidates
    and emits SniperOrders via the on_order callback.

    Parameters
    ----------
    config : SniperConfig
        Runtime configuration for this sniper instance.
    on_order : Callable[[SniperOrder], None]
        Callback invoked for every order (executed or rejected).
        Use this to send Telegram notifications or pass to SwapEngine.
    """

    def __init__(
        self,
        config:   SniperConfig,
        on_order: Callable[[SniperOrder], None] = None,
    ) -> None:
        self.config      = config
        self.on_order    = on_order or (lambda _: None)
        self._scanner    = TokenScanner(
            min_liquidity    = config.min_liquidity_usd,
            min_volume_1h    = 5_000,
            min_price_change_5m = 3.0,
        )
        self._running    = False
        self._orders:    List[SniperOrder] = []

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the continuous scan-and-evaluate loop.
        Runs until stop() is called.
        """
        self._running = True
        logger.info(
            "🔫 AutoSniper started  [mode=%s | amount=%.2f SOL | interval=%ds]",
            self.config.mode.value,
            self.config.buy_amount_sol,
            self.config.scan_interval_sec,
        )
        while self._running:
            try:
                candidates = await self._scanner.scan_once()
                for candidate in candidates:
                    order = await self._evaluate(candidate)
                    self._orders.append(order)
                    self.on_order(order)
            except Exception as exc:
                logger.error("AutoSniper loop error: %s", exc)
            await asyncio.sleep(self.config.scan_interval_sec)

    def stop(self) -> None:
        """Gracefully stop the scan loop."""
        self._running = False
        logger.info("AutoSniper stopped.")

    def get_orders(
        self,
        status: Optional[str] = None,
    ) -> List[SniperOrder]:
        """
        Return all recorded orders, optionally filtered by status.

        Parameters
        ----------
        status : str, optional
            'executed', 'rejected', or 'pending'.
        """
        if status:
            return [o for o in self._orders if o.status == status]
        return list(self._orders)

    @property
    def total_executed(self) -> int:
        return sum(1 for o in self._orders if o.status == "executed")

    @property
    def total_rejected(self) -> int:
        return sum(1 for o in self._orders if o.status == "rejected")

    # ── Evaluation pipeline ────────────────────────────────────────────────────

    async def _evaluate(self, token: TokenCandidate) -> SniperOrder:
        """
        Run a token through the appropriate check pipeline based on mode.

        SAFE  → liquidity check → rug check → risk-score gate
        DEGEN → liquidity check only
        """
        order = SniperOrder(token=token, config=self.config)

        # Gate 1 — Liquidity floor (enforced in both modes)
        if token.liquidity_usd < self.config.min_liquidity_usd:
            return self._reject(
                order,
                f"Liquidity too low "
                f"(${token.liquidity_usd:,.0f} < ${self.config.min_liquidity_usd:,.0f}).",
            )

        if self.config.mode == SniperMode.DEGEN:
            # Degen: single gate cleared → fire
            logger.info("⚡ DEGEN snipe → %s ($%s)", token.name, token.symbol)
            order.status = "executed"
            return order

        # SAFE mode — full forensic scan
        logger.info("🔍 Running rug check for %s…", token.symbol)
        try:
            report = await run_rug_check(token.ca, chain=token.chain)
        except Exception as exc:
            return self._reject(order, f"Rug check error: {exc}")

        order.risk_report = report

        # Gate 2 — Hard flags (critical red flags always block)
        if report.flags and report.risk_score >= 60:
            return self._reject(
                order,
                f"Critical rug flags detected: {'; '.join(report.flags[:2])}",
            )

        # Gate 3 — Risk score ceiling
        if report.risk_score > self.config.max_risk_score:
            return self._reject(
                order,
                f"Risk score {report.risk_score}/100 exceeds limit "
                f"of {self.config.max_risk_score}.",
            )

        # All gates passed
        logger.info(
            "✅ SAFE snipe approved → %s | risk=%s (%d/100)",
            token.symbol,
            report.risk_level,
            report.risk_score,
        )
        order.status = "executed"
        return order

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _reject(order: SniperOrder, reason: str) -> SniperOrder:
        order.status        = "rejected"
        order.reject_reason = reason
        logger.info("❌ Rejected %s — %s", order.token.symbol, reason)
        return order