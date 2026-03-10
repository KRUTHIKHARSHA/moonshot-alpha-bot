"""
scanner/token_scanner.py
------------------------
Token discovery and validation layer.

Responsibilities:
  - Fetch live token/pair data from DexScreener API
  - Validate supported chains (Solana, ETH, BSC, Base)
  - Add tokens to a user's watchlist (transactional)
  - Build and refresh the daily Top-20 shortlist
"""

import asyncio
import logging
from typing import Tuple

import httpx
from firebase_admin import firestore

from database.firebase import db

logger = logging.getLogger(__name__)

SUPPORTED_CHAINS = ["solana", "ethereum", "bsc", "base"]


# ── DexScreener helpers ────────────────────────────────────────────────────────

async def get_token_pair_data(token_address: str) -> dict | None:
    """
    Fetch the best trading pair for a token from DexScreener.

    Returns a normalised dict with keys:
        chain, pairAddress, name, symbol, mcap, liquidity, priceUsd, imageUrl
    or None if no valid pair is found.
    """
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = client if False else await client.get(url)   # type: ignore
            resp = await httpx.AsyncClient(timeout=15).__aenter__()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        pairs = resp.json().get("pairs") or []
        valid = [p for p in pairs if float(p.get("priceUsd") or 0) > 0]
        if not valid:
            return None
        # Pick the pair with the highest USD liquidity
        best = max(valid, key=lambda p: p.get("liquidity", {}).get("usd", 0))
        return {
            "chain":       best.get("chainId"),
            "pairAddress": best.get("pairAddress"),
            "name":        best.get("baseToken", {}).get("name"),
            "symbol":      best.get("baseToken", {}).get("symbol"),
            "mcap":        best.get("fdv", 0),
            "liquidity":   best.get("liquidity", {}).get("usd", 0),
            "priceUsd":    float(best["priceUsd"]),
            "imageUrl":    best.get("info", {}).get("imageUrl"),
        }
    except Exception as exc:
        logger.error(f"get_token_pair_data({token_address}): {exc}")
        return None


def format_large_number(num) -> str:
    """Format a large number as a human-readable string (e.g. 1.4M, 320K)."""
    if num is None:
        return "N/A"
    try:
        num = float(num)
        if num >= 1_000_000:
            return f"{num / 1_000_000:.1f}M"
        if num >= 1_000:
            return f"{num / 1_000:.0f}K"
        return f"{num:.2f}"
    except (ValueError, TypeError):
        return "N/A"


# ── Wishlist: add token ────────────────────────────────────────────────────────

async def add_token_to_wishlist(user_id: str, token_address: str) -> Tuple[bool, str]:
    """
    Validate the token address, confirm chain support, then atomically
    add it to the user's wishlist in Firestore.
    """
    if not db:
        return False, "Database connection error."

    token_data = await get_token_pair_data(token_address)
    if not token_data:
        return False, "❌ Could not find a valid token with that address."

    chain = (token_data.get("chain") or "").lower()
    if chain not in SUPPORTED_CHAINS:
        return (
            False,
            f"❌ <b>{token_data.get('name')}</b> is on <b>{chain.upper()}</b>. "
            f"We support: {', '.join(c.upper() for c in SUPPORTED_CHAINS)}.",
        )

    try:
        user_ref = db.collection("subscribers").document(user_id)
        hub_ref  = db.collection("wishlisted_tokens").document(token_address)

        # Quick duplicate check before opening a transaction
        pre = user_ref.get()
        if pre.exists and pre.to_dict():
            if any(i.get("ca") == token_address for i in pre.to_dict().get("wishlist", [])
                   if isinstance(i, dict)):
                return False, "ℹ️ This token is already in your wishlist."

        @firestore.transactional
        def _txn(transaction):
            doc      = user_ref.get(transaction=transaction)
            wishlist = doc.to_dict().get("wishlist", []) if doc.exists and doc.to_dict() else []
            new_item = {
                "ca":     token_address,
                "status": "new",
                "name":   token_data.get("name", "Unknown"),
                "symbol": token_data.get("symbol", "N/A"),
                "chain":  chain,
            }
            wishlist.append(new_item)
            transaction.set(user_ref, {"wishlist": wishlist}, merge=True)
            transaction.set(
                hub_ref,
                {"watched_by": firestore.ArrayUnion([user_id]), "chain": chain},
                merge=True,
            )
            return new_item

        item = _txn(db.transaction())
        logger.info(f"User {user_id} added {token_address} ({chain}) to wishlist.")
        return (
            True,
            f"✅ Added <b>{item['name']} (${item['symbol']})</b> on {chain.upper()} to your wishlist!",
        )
    except Exception as exc:
        logger.error(f"add_token_to_wishlist({user_id}): {exc}")
        return False, "❌ An unexpected error occurred."


# ── Top-20 shortlist builder ───────────────────────────────────────────────────

async def update_top_20_shortlist():
    """
    Run once every 24 h.
    Scans past alerts, fetches live prices from DexScreener, ranks tokens
    by percentage gain since the alert was issued, and saves the top 20
    candidates back to Firestore.
    """
    if not db:
        logger.error("DB not connected – skipping Top-20 update.")
        return

    logger.info("Starting daily Top-20 shortlist update…")
    try:
        # 1. Pull last 1 000 alerts
        alerts = [
            d.to_dict()
            for d in db.collection("past_alerts")
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(1000)
            .stream()
        ]
        if not alerts:
            return

        # 2. Group pair addresses by chain
        pairs_by_chain: dict[str, list[str]] = {}
        for a in alerts:
            chain = a.get("chain", "solana").lower()
            addr  = a.get("pairAddress")
            if addr:
                pairs_by_chain.setdefault(chain, []).append(addr)

        # 3. Fetch live prices and compute % gain
        results = []
        async with httpx.AsyncClient(timeout=20) as client:
            for chain, addresses in pairs_by_chain.items():
                for i in range(0, len(addresses), 30):          # DexScreener batch limit
                    chunk = addresses[i : i + 30]
                    url   = (
                        f"https://api.dexscreener.com/latest/dex/pairs/"
                        f"{chain}/{','.join(chunk)}"
                    )
                    try:
                        resp = await client.get(url)
                        if resp.status_code == 200:
                            live = {p["pairAddress"]: p for p in resp.json().get("pairs", [])}
                            for alert in alerts:
                                pa = alert.get("pairAddress")
                                if pa in live:
                                    current = float(live[pa].get("priceUsd") or 0)
                                    entry   = float(
                                        alert.get("priceAtCall") or alert.get("priceUsd") or 0
                                    )
                                    if entry > 0:
                                        gain = ((current - entry) / entry) * 100
                                        results.append(
                                            {
                                                "symbol":      alert.get("symbol"),
                                                "name":        alert.get("name"),
                                                "chain":       alert.get("chain"),
                                                "entry_price": entry,
                                                "pairAddress": pa,
                                                "imageUrl":    alert.get("imageUrl"),
                                                "_gain":       gain,   # used for sorting only
                                            }
                                        )
                    except Exception as exc:
                        logger.error(f"Top-20 batch error ({chain}): {exc}")
                    await asyncio.sleep(0.5)

        # 4. Sort and keep top 20
        results.sort(key=lambda x: x["_gain"], reverse=True)
        top_20 = results[:20]
        for item in top_20:
            item.pop("_gain", None)      # don't persist the sort key

        if top_20:
            batch     = db.batch()
            col_ref   = db.collection("TOP_20")
            for doc in col_ref.stream():
                batch.delete(doc.reference)
            for idx, item in enumerate(top_20, start=1):
                batch.set(col_ref.document(str(idx)), item)
            batch.commit()
            logger.info(f"✅ Top-20 shortlist updated with {len(top_20)} tokens.")

    except Exception as exc:
        logger.error(f"update_top_20_shortlist: {exc}")