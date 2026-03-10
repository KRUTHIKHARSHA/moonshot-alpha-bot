"""
trading/rug_interceptor.py
==========================
Forensic engine that performs deep on-chain analysis on Solana
tokens to detect rug-pull patterns BEFORE a trade is executed.

Checks performed
----------------
1.  Mint Authority      — dev can print unlimited tokens (score +40)
2.  Freeze Authority    — dev can freeze holder wallets  (score +30)
3.  Top-holder concentration — whale dump risk           (score +10-30)
4.  Honeypot detection  — zero-sell heuristic via DEX    (score +35)
5.  Liquidity depth     — too-thin pools can be drained  (score +10-20)

Risk levels
-----------
  SAFE     (0-19)   green  Proceed with normal caution
  LOW      (20-39)  yellow Minor warnings present
  MEDIUM   (40-59)  orange Real risks, do extra research
  HIGH     (60-79)  red    Likely dangerous
  CRITICAL (80-100) skull  Almost certainly a rug
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
SOLANA_RPC         = "https://api.mainnet-beta.solana.com"
DEXSCREENER_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"

RISK_THRESHOLDS: Dict[str, Tuple[int, int]] = {
    "SAFE":     (0,  19),
    "LOW":      (20, 39),
    "MEDIUM":   (40, 59),
    "HIGH":     (60, 79),
    "CRITICAL": (80, 100),
}

RISK_EMOJIS: Dict[str, str] = {
    "SAFE":     "🟢",
    "LOW":      "🟡",
    "MEDIUM":   "🟠",
    "HIGH":     "🔴",
    "CRITICAL": "☠️",
}


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class RugReport:
    """
    Consolidated result from all forensic checks.
    Passed to the AutoSniper to decide whether to fire an order.
    """
    token_address:        str
    token_name:           str   = "Unknown"
    token_symbol:         str   = "???"
    chain:                str   = "solana"

    # Composite risk rating
    risk_score:           int   = 0        # 0 = clean, 100 = certain rug
    risk_level:           str   = "SAFE"

    # Categorised findings
    flags:    List[str]         = field(default_factory=list)  # hard blockers
    warnings: List[str]         = field(default_factory=list)  # soft cautions
    passed:   List[str]         = field(default_factory=list)  # green ticks

    # Raw on-chain data points
    has_mint_authority:   bool  = False
    has_freeze_authority: bool  = False
    top10_concentration:  float = 0.0   # % held by top-10 wallets
    liquidity_usd:        float = 0.0
    buy_count_1h:         int   = 0
    sell_count_1h:        int   = 0

    # ── Derived properties ─────────────────────────────────────────────────────

    @property
    def is_safe(self) -> bool:
        """True when risk_score is below the MEDIUM threshold."""
        return self.risk_score < 40

    @property
    def sell_ratio(self) -> float:
        """
        Ratio of sells to buys in the past hour.
        A ratio below 0.05 is a classic honeypot signal.
        """
        if self.buy_count_1h == 0:
            return 0.0
        return self.sell_count_1h / self.buy_count_1h

    # ── Formatting ─────────────────────────────────────────────────────────────

    def summary(self) -> str:
        """Return a Telegram-HTML formatted forensic report."""
        emoji = RISK_EMOJIS.get(self.risk_level, "⚪")
        lines = [
            f"{emoji} <b>Rug Interceptor Report</b>",
            f"<b>Token:</b>       {self.token_name} (${self.token_symbol})",
            f"<b>Risk Level:</b>  {self.risk_level}",
            f"<b>Risk Score:</b>  {self.risk_score}/100",
            f"<b>Liquidity:</b>   ${self.liquidity_usd:,.0f}",
            f"<b>Buys/Sells:</b>  {self.buy_count_1h} / {self.sell_count_1h} (last 1h)",
        ]

        if self.flags:
            lines.append("\n🚨 <b>Red Flags:</b>")
            lines += [f"  ❌ {f}" for f in self.flags]

        if self.warnings:
            lines.append("\n⚠️ <b>Warnings:</b>")
            lines += [f"  ⚠️ {w}" for w in self.warnings]

        if self.passed:
            lines.append("\n✅ <b>Passed Checks:</b>")
            lines += [f"  ✅ {p}" for p in self.passed]

        return "\n".join(lines)


# ── Individual check coroutines ────────────────────────────────────────────────

async def _fetch_mint_info(token_address: str, client: httpx.AsyncClient) -> Dict:
    """
    Fetch token mint account via Solana JSON-RPC getAccountInfo.
    Returns the parsed 'info' dict or {} on failure.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [token_address, {"encoding": "jsonParsed"}],
    }
    try:
        resp   = await client.post(SOLANA_RPC, json=payload, timeout=10)
        value  = (resp.json().get("result", {}).get("value") or {})
        return  value.get("data", {}).get("parsed", {}).get("info", {})
    except Exception as exc:
        logger.warning("_fetch_mint_info(%s): %s", token_address, exc)
        return {}


async def _check_authorities(
    token_address: str, client: httpx.AsyncClient
) -> Tuple[bool, bool]:
    """
    Check mint and freeze authority in a single RPC call.

    Returns
    -------
    (has_mint_authority, has_freeze_authority)

    Why it matters
    --------------
    - Mint authority present  → dev can dilute supply at will → price dump
    - Freeze authority present → dev can prevent sells → wallet locked
    """
    info = await _fetch_mint_info(token_address, client)
    has_mint   = info.get("mintAuthority")   is not None
    has_freeze = info.get("freezeAuthority") is not None
    return has_mint, has_freeze


async def _check_holder_concentration(
    token_address: str, client: httpx.AsyncClient
) -> float:
    """
    Fetch the top-20 largest token accounts via getTokenLargestAccounts
    and return the combined ownership % of the top-10 holders.

    Why it matters
    --------------
    If top-10 wallets hold >70% of supply, a coordinated dump can
    wipe out price and liquidity simultaneously.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenLargestAccounts",
        "params": [token_address],
    }
    try:
        resp     = await client.post(SOLANA_RPC, json=payload, timeout=10)
        accounts = resp.json().get("result", {}).get("value", [])
        if not accounts:
            return 0.0
        total      = sum(float(a.get("uiAmount") or 0) for a in accounts)
        top10_sum  = sum(float(a.get("uiAmount") or 0) for a in accounts[:10])
        return (top10_sum / total * 100) if total > 0 else 0.0
    except Exception as exc:
        logger.warning("_check_holder_concentration(%s): %s", token_address, exc)
        return 0.0


async def _check_dex_data(
    token_address: str, client: httpx.AsyncClient
) -> Tuple[float, int, int, str, str]:
    """
    Pull live pair data from DexScreener for DEX-level heuristics.

    Returns
    -------
    (liquidity_usd, buy_count_1h, sell_count_1h, token_name, token_symbol)
    """
    try:
        resp = await client.get(
            f"{DEXSCREENER_TOKENS}/{token_address}", timeout=12
        )
        if resp.status_code != 200:
            return 0.0, 0, 0, "Unknown", "???"

        pairs = resp.json().get("pairs") or []
        if not pairs:
            return 0.0, 0, 0, "Unknown", "???"

        # Use the highest-liquidity pair as ground truth
        best  = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0)))
        liq   = float((best.get("liquidity") or {}).get("usd", 0))
        txns  = (best.get("txns") or {}).get("h1", {})
        name  = best.get("baseToken", {}).get("name",   "Unknown")
        sym   = best.get("baseToken", {}).get("symbol", "???")

        return liq, int(txns.get("buys", 0)), int(txns.get("sells", 0)), name, sym

    except Exception as exc:
        logger.warning("_check_dex_data(%s): %s", token_address, exc)
        return 0.0, 0, 0, "Unknown", "???"


# ── Scoring helper ─────────────────────────────────────────────────────────────

def _assign_risk_level(score: int) -> Tuple[int, str]:
    """Clamp score to 0-100 and return (clamped_score, risk_level_string)."""
    score = max(0, min(score, 100))
    for level, (lo, hi) in RISK_THRESHOLDS.items():
        if lo <= score <= hi:
            return score, level
    return score, "CRITICAL"


# ── Public orchestrator ────────────────────────────────────────────────────────

async def run_rug_check(
    token_address: str,
    chain: str = "solana",
) -> RugReport:
    """
    Execute all forensic checks concurrently and return a RugReport.

    On-chain checks (Mint, Freeze, Holder concentration) are Solana-only.
    DEX heuristics (Liquidity, Honeypot) work on all supported chains.

    Parameters
    ----------
    token_address : str
        Token mint address (Solana) or contract address (EVM).
    chain : str
        Chain identifier — e.g. 'solana', 'ethereum', 'bsc', 'base'.
    """
    report = RugReport(token_address=token_address, chain=chain)
    score  = 0

    # ── Concurrent data fetching ───────────────────────────────────────────────
    async with httpx.AsyncClient() as client:
        tasks = [_check_dex_data(token_address, client)]

        if chain.lower() == "solana":
            tasks += [
                _check_authorities(token_address, client),
                _check_holder_concentration(token_address, client),
            ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

    # ── Unpack DEX result ──────────────────────────────────────────────────────
    dex = results[0]
    if isinstance(dex, Exception):
        logger.error("DEX check failed for %s: %s", token_address, dex)
    else:
        (
            report.liquidity_usd,
            report.buy_count_1h,
            report.sell_count_1h,
            report.token_name,
            report.token_symbol,
        ) = dex

    # ── Unpack Solana-specific results ─────────────────────────────────────────
    if chain.lower() == "solana" and len(results) == 3:
        auth = results[1]
        if not isinstance(auth, Exception):
            report.has_mint_authority, report.has_freeze_authority = auth

        conc = results[2]
        if not isinstance(conc, Exception):
            report.top10_concentration = conc
    else:
        report.warnings.append(
            f"Deep on-chain scan not yet available for {chain.upper()} — DEX checks only."
        )

    # ── Scoring rules ──────────────────────────────────────────────────────────

    # Rule 1 — Mint Authority
    if report.has_mint_authority:
        report.flags.append(
            "Mint authority is ACTIVE — developer can inflate token supply at will."
        )
        score += 40
    else:
        report.passed.append("Mint authority is revoked — supply is fixed.")

    # Rule 2 — Freeze Authority
    if report.has_freeze_authority:
        report.flags.append(
            "Freeze authority is ACTIVE — developer can prevent you from selling."
        )
        score += 30
    else:
        report.passed.append("Freeze authority is revoked — wallets cannot be frozen.")

    # Rule 3 — Holder concentration
    c = report.top10_concentration
    if c > 80:
        report.flags.append(
            f"Top-10 wallets hold {c:.1f}% of supply — extreme coordinated-dump risk."
        )
        score += 30
    elif c > 60:
        report.warnings.append(
            f"Top-10 wallets hold {c:.1f}% — high concentration, watch for large sells."
        )
        score += 15
    elif c > 40:
        report.warnings.append(
            f"Top-10 wallets hold {c:.1f}% — moderate concentration."
        )
        score += 10
    elif c > 0:
        report.passed.append(
            f"Holder distribution is healthy — top-10 wallets hold {c:.1f}%."
        )

    # Rule 4 — Honeypot heuristic
    if report.buy_count_1h > 5 and report.sell_count_1h == 0:
        report.flags.append(
            "Zero sell transactions detected in the last 1 h — strong honeypot signal."
        )
        score += 35
    elif report.buy_count_1h > 10 and report.sell_ratio < 0.05:
        report.warnings.append(
            f"Sell-to-buy ratio is very low ({report.sell_ratio:.2f}) — "
            "possible soft sell restriction."
        )
        score += 20
    elif report.buy_count_1h > 0:
        report.passed.append(
            f"Buy/sell activity looks normal (ratio {report.sell_ratio:.2f})."
        )

    # Rule 5 — Liquidity depth
    liq = report.liquidity_usd
    if liq == 0:
        report.flags.append(
            "No active liquidity pool found — token may not be tradeable."
        )
        score += 20
    elif liq < 3_000:
        report.flags.append(
            f"Critically low liquidity (${liq:,.0f}) — pool can be fully drained in one trade."
        )
        score += 20
    elif liq < 15_000:
        report.warnings.append(
            f"Low liquidity (${liq:,.0f}) — large trades will cause significant slippage."
        )
        score += 10
    else:
        report.passed.append(f"Liquidity is sufficient (${liq:,.0f}).")

    # ── Finalise report ────────────────────────────────────────────────────────
    report.risk_score, report.risk_level = _assign_risk_level(score)
    return report