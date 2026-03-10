"""
core/scheduler.py
-----------------
Background task runner.

Current tasks:
  - update_top_calls()  — runs every 24 h, rebuilds the
    Top-20 shortlist by scanning past alert performance.
"""

import asyncio
import logging

import httpx

from database.firebase import db, get_recent_alerts

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 86_400   # 24 hours


async def background_scheduler() -> None:
    """Infinite loop that runs maintenance tasks on a schedule."""
    while True:
        logger.info("⏰ Scheduler: running daily tasks…")
        await update_top_calls()
        logger.info("⏰ Scheduler: sleeping for 24 hours.")
        await asyncio.sleep(INTERVAL_SECONDS)


async def update_top_calls() -> None:
    """
    Fetch live prices for all past alerts, rank by gain,
    and persist the top-20 shortlist to Firestore.
    """
    if not db:
        logger.warning("update_top_calls: DB not connected, skipping.")
        return

    logger.info("📊 Updating Top-20 shortlist…")
    alerts = get_recent_alerts(limit=500)
    if not alerts:
        return

    # Group pair addresses by chain for batch API calls
    by_chain: dict = {}
    for alert in alerts:
        chain = alert.get("chain", "solana").lower()
        addr  = alert.get("pairAddress")
        if addr:
            by_chain.setdefault(chain, []).append(addr)

    results = []
    chunk_size = 30

    async with httpx.AsyncClient(timeout=20) as client:
        for chain, addresses in by_chain.items():
            for i in range(0, len(addresses), chunk_size):
                chunk = addresses[i : i + chunk_size]
                url   = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{','.join(chunk)}"
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    live_pairs = {
                        p.get("pairAddress"): p
                        for p in resp.json().get("pairs", [])
                    }
                    for alert in alerts:
                        pair_addr = alert.get("pairAddress")
                        if pair_addr not in live_pairs:
                            continue
                        live       = live_pairs[pair_addr]
                        current    = float(live.get("priceUsd", 0))
                        entry      = float(alert.get("priceUsd", 0))
                        if entry > 0 and current > 0:
                            gain = ((current - entry) / entry) * 100
                            results.append({
                                "symbol":      alert.get("symbol"),
                                "name":        alert.get("name"),
                                "chain":       alert.get("chain"),
                                "entry_price": entry,
                                "pairAddress": pair_addr,
                                "imageUrl":    alert.get("imageUrl"),
                                "_gain":       gain,
                            })
                except Exception as exc:
                    logger.error("update_top_calls batch error: %s", exc)
                await asyncio.sleep(0.5)

    # Keep top 20 by gain
    results.sort(key=lambda x: x["_gain"], reverse=True)
    top20 = results[:20]

    if not top20:
        return

    # Write to Firestore
    try:
        batch = db.batch()
        for doc in db.collection("TOP_20").stream():
            batch.delete(doc.reference)
        for idx, item in enumerate(top20):
            item.pop("_gain", None)
            batch.set(db.collection("TOP_20").document(str(idx + 1)), item)
        batch.commit()
        logger.info("✅ Top-20 shortlist updated with %d tokens.", len(top20))
    except Exception as exc:
        logger.error("update_top_calls write error: %s", exc)