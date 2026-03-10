"""
trading/position_monitor.py
---------------------------
Ghost Manager — server-side position monitoring.

Watches open positions in the background and automatically
triggers stop-loss or take-profit sells via the SwapEngine
when price targets are hit.

This protects capital even while the user is offline.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Position:
    user_id: str
    token_ca: str
    token_symbol: str
    chain: str
    pair_address: str
    entry_price: float
    token_amount: int           # raw token units
    take_profit_pct: float      # e.g. 100.0 = 2×
    stop_loss_pct: float        # e.g. 25.0  = -25%
    opened_at: datetime         = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str                 = "open"   # open | closed_tp | closed_sl | closed_manual

    @property
    def take_profit_price(self) -> float:
        return self.entry_price * (1 + self.take_profit_pct / 100)

    @property
    def stop_loss_price(self) -> float:
        return self.entry_price * (1 - self.stop_loss_pct / 100)

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return ((current_price - self.entry_price) / self.entry_price) * 100

    def summary(self, current_price: float = 0.0) -> str:
        pnl = self.pnl_pct(current_price) if current_price else 0
        emoji = "📈" if pnl >= 0 else "📉"
        return (
            f"{emoji} <b>{self.token_symbol}</b>\n"
            f"Entry: ${self.entry_price:.6f}\n"
            f"TP:    ${self.take_profit_price:.6f} (+{self.take_profit_pct}%)\n"
            f"SL:    ${self.stop_loss_price:.6f}  (-{self.stop_loss_pct}%)\n"
            f"PnL:   {pnl:+.1f}%\n"
            f"Status: {self.status.upper()}"
        )


# ---------------------------------------------------------------------------
# Ghost Manager
# ---------------------------------------------------------------------------

class GhostManager:
    """
    Runs as a background asyncio task.
    Polls live prices every *poll_interval* seconds and closes
    positions that hit their TP or SL targets.
    """

    def __init__(
        self,
        poll_interval: int = 10,
        on_close: Callable[[Position, str, float], None] = None,
    ):
        self.poll_interval = poll_interval
        self.on_close      = on_close or (lambda *_: None)
        self._positions: Dict[str, Position] = {}   # keyed by token_ca
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_position(self, position: Position) -> None:
        self._positions[position.token_ca] = position
        logger.info("👁️  Monitoring %s for %s", position.token_symbol, position.user_id)

    def remove_position(self, token_ca: str) -> None:
        self._positions.pop(token_ca, None)

    def get_positions(self, user_id: str = None) -> List[Position]:
        positions = list(self._positions.values())
        if user_id:
            positions = [p for p in positions if p.user_id == user_id]
        return positions

    async def start(self) -> None:
        self._running = True
        logger.info("👻 GhostManager started — watching %d positions", len(self._positions))
        while self._running:
            await self._check_all()
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Price checking logic
    # ------------------------------------------------------------------

    async def _check_all(self) -> None:
        if not self._positions:
            return

        open_positions = [p for p in self._positions.values() if p.status == "open"]
        if not open_positions:
            return

        # Batch price fetch (group by chain)
        prices = await self._fetch_prices(open_positions)

        for pos in open_positions:
            current = prices.get(pos.token_ca)
            if current is None:
                continue

            if current >= pos.take_profit_price:
                self._close(pos, "take_profit", current)
            elif current <= pos.stop_loss_price:
                self._close(pos, "stop_loss", current)

    def _close(self, pos: Position, reason: str, current_price: float) -> None:
        pos.status = f"closed_{reason}"
        self.on_close(pos, reason, current_price)
        self.remove_position(pos.token_ca)

        label = "🎯 Take Profit" if reason == "take_profit" else "🛑 Stop Loss"
        logger.info(
            "%s triggered for %s | entry=%.6f | exit=%.6f | pnl=%.1f%%",
            label,
            pos.token_symbol,
            pos.entry_price,
            current_price,
            pos.pnl_pct(current_price),
        )

    async def _fetch_prices(self, positions: List[Position]) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        cas = [p.token_ca for p in positions]

        try:
            url = f"{DEXSCREENER_API}/{','.join(cas)}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return prices
            for pair in resp.json().get("pairs", []):
                ca = pair.get("baseToken", {}).get("address", "")
                try:
                    prices[ca] = float(pair.get("priceUsd", 0))
                except (TypeError, ValueError):
                    pass
        except Exception as exc:
            logger.error("_fetch_prices: %s", exc)

        return prices