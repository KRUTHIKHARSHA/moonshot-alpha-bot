"""
database/firebase.py
--------------------
All Firebase / Firestore interactions:
  - User subscription management
  - Past alert storage and retrieval
  - Wishlist management (add / remove / fetch)
"""

import os
import json
import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Tuple

import firebase_admin
from firebase_admin import credentials, firestore

logger = logging.getLogger(__name__)
db = None  # module-level Firestore client


def init_firebase():
    """Initialise Firestore from a base64-encoded service-account credential."""
    global db
    try:
        encoded = os.getenv("FIREBASE_CREDENTIALS_BASE64")
        if not encoded:
            raise ValueError("FIREBASE_CREDENTIALS_BASE64 env var not set.")
        cred = credentials.Certificate(json.loads(base64.b64decode(encoded)))
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("✅ Firebase initialised.")
    except Exception as exc:
        logger.critical(f"Firebase init failed: {exc}")


# ── Subscription helpers ───────────────────────────────────────────────────────

def get_user_subscription(user_id: str) -> dict | None:
    """Return subscription-status dict, or None if the user doesn't exist."""
    if not db:
        return None
    try:
        doc = db.collection("subscribers").document(str(user_id)).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        if not data.get("terms_agreed"):
            return {"status": "pending_agreement", "data": data}
        expires_at = data.get("expires_at")
        if data.get("is_active") and expires_at and datetime.now(timezone.utc) > expires_at:
            db.collection("subscribers").document(str(user_id)).update({"is_active": False})
            data["is_active"] = False
            return {"status": "expired", "data": data}
        status = "active" if data.get("is_active") else "inactive"
        return {"status": status, "data": data}
    except Exception as exc:
        logger.error(f"get_user_subscription({user_id}): {exc}")
        return None


def set_user_subscription(user_id: str, plan_name: str, plans_config: dict) -> bool:
    """Activate a paid plan for a user."""
    if not db or plan_name not in plans_config:
        return False
    try:
        plan = plans_config[plan_name]
        now  = datetime.now(timezone.utc)
        db.collection("subscribers").document(user_id).set(
            {
                "plan":        plan_name,
                "subscribed_at": now,
                "expires_at":  now + timedelta(days=plan["duration_days"]),
                "is_active":   True,
            },
            merge=True,
        )
        return True
    except Exception as exc:
        logger.error(f"set_user_subscription({user_id}): {exc}")
        return False


def record_terms_agreement(user_id: str, user_name: str, referrer_id: str = None) -> bool:
    """Record that a user agreed to the Terms of Service."""
    if not db:
        return False
    try:
        data = {
            "terms_agreed": True,
            "agreed_at":    firestore.SERVER_TIMESTAMP,
            "user_name":    user_name,
        }
        if referrer_id:
            data["referred_by"] = referrer_id
        db.collection("subscribers").document(user_id).set(data, merge=True)
        return True
    except Exception as exc:
        logger.error(f"record_terms_agreement({user_id}): {exc}")
        return False


def add_referral_reward(user_id: str, reward_days: int) -> bool:
    """Extend a referrer's subscription by reward_days free days."""
    if not db:
        return False
    try:
        ref = db.collection("subscribers").document(user_id)
        doc = ref.get()
        if not doc.exists:
            return False
        data      = doc.to_dict()
        now       = datetime.now(timezone.utc)
        base_date = max(now, data.get("expires_at", now))
        ref.update({"expires_at": base_date + timedelta(days=reward_days), "is_active": True})
        return True
    except Exception as exc:
        logger.error(f"add_referral_reward({user_id}): {exc}")
        return False


def get_all_active_subscribers() -> List[str]:
    """Return user IDs of all active subscribers."""
    if not db:
        return []
    try:
        docs = db.collection("subscribers").where(
            filter=firestore.FieldFilter("is_active", "==", True)
        ).stream()
        return [d.id for d in docs]
    except Exception as exc:
        logger.error(f"get_all_active_subscribers: {exc}")
        return []


def get_active_alert_subscribers() -> List[str]:
    """Active subscribers who have NOT muted alerts."""
    if not db:
        return []
    try:
        docs = db.collection("subscribers").where(
            filter=firestore.FieldFilter("is_active", "==", True)
        ).stream()
        return [d.id for d in docs if not d.to_dict().get("alerts_muted", False)]
    except Exception as exc:
        logger.error(f"get_active_alert_subscribers: {exc}")
        return []


# ── Alert helpers ──────────────────────────────────────────────────────────────

def check_if_alert_exists(chain: str, contract_address: str) -> bool:
    if not db:
        return True
    doc_id = f"{chain.lower()}-{contract_address.lower()}"
    return db.collection("past_alerts").document(doc_id).get().exists


def save_past_alert(alert_message: str, risk: str, pair_data: dict):
    """Persist an alert to Firestore so it appears in the alert history."""
    if not db:
        return
    try:
        doc_id = f"{pair_data['chain'].lower()}-{pair_data['ca'].lower()}"
        db.collection("past_alerts").document(doc_id).set(
            {
                "message":     alert_message,
                "risk":        risk.lower(),
                "timestamp":   firestore.SERVER_TIMESTAMP,
                "ca":          pair_data["ca"],
                "chain":       pair_data["chain"],
                "priceAtCall": pair_data["priceUsd"],
                "pairAddress": pair_data["pairAddress"],
                "imageUrl":    pair_data.get("imageUrl"),
                "name":        pair_data.get("name"),
                "symbol":      pair_data.get("symbol"),
            }
        )
    except Exception as exc:
        logger.error(f"save_past_alert: {exc}")


def get_all_past_alerts() -> List[dict]:
    if not db:
        return []
    try:
        docs = db.collection("past_alerts").order_by(
            "timestamp", direction=firestore.Query.DESCENDING
        ).stream()
        return [d.to_dict() for d in docs]
    except Exception as exc:
        logger.error(f"get_all_past_alerts: {exc}")
        return []


def get_past_alerts_by_risk(risk: str, page: int, page_size: int) -> Tuple[List[dict], int]:
    """Paginated alert retrieval filtered by risk level (high / medium / low)."""
    if not db:
        return [], 0
    try:
        query = db.collection("past_alerts").where(
            filter=firestore.FieldFilter("risk", "==", risk.lower())
        )
        total = query.count().get()[0][0].value
        docs  = (
            query.order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(page_size)
            .offset(page * page_size)
            .stream()
        )
        return [d.to_dict() for d in docs], total
    except Exception as exc:
        logger.error(f"get_past_alerts_by_risk({risk}): {exc}")
        return [], 0


# ── Wishlist helpers ───────────────────────────────────────────────────────────

def get_user_wishlist(user_id: str) -> List[Dict[str, Any]]:
    if not db:
        return []
    try:
        doc = db.collection("subscribers").document(user_id).get()
        if doc.exists and doc.to_dict():
            return [i for i in doc.to_dict().get("wishlist", []) if isinstance(i, dict)]
        return []
    except Exception as exc:
        logger.error(f"get_user_wishlist({user_id}): {exc}")
        return []


def remove_token_from_wishlist(user_id: str, token_address: str) -> Tuple[bool, str]:
    """Atomically remove a token from a user's wishlist."""
    if not db:
        return False, "Database connection error."
    try:
        user_ref = db.collection("subscribers").document(user_id)
        hub_ref  = db.collection("wishlisted_tokens").document(token_address)

        @firestore.transactional
        def _txn(transaction):
            doc = user_ref.get(transaction=transaction)
            if not doc.exists or not doc.to_dict():
                raise ValueError("User not found.")
            wishlist = doc.to_dict().get("wishlist", [])
            name, found = "This token", False
            for item in wishlist:
                if isinstance(item, dict) and item.get("ca") == token_address:
                    name, found = f"<b>{item.get('name', 'Unknown')}</b>", True
                    break
            if not found:
                raise ValueError("Token not found in your wishlist.")
            updated = [i for i in wishlist
                       if not (isinstance(i, dict) and i.get("ca") == token_address)]
            transaction.update(user_ref, {"wishlist": updated})
            transaction.update(hub_ref, {"watched_by": firestore.ArrayRemove([user_id])})
            return True, f"✅ Removed {name} from your wishlist."

        success, msg = _txn(db.transaction())
        hub_doc = hub_ref.get()
        if hub_doc.exists and not hub_doc.to_dict().get("watched_by"):
            hub_ref.delete()          # clean up hub when nobody watches the token
        return success, msg
    except ValueError as ve:
        return False, f"❌ {ve}"
    except Exception as exc:
        logger.error(f"remove_token_from_wishlist: {exc}")
        return False, "❌ An unexpected error occurred."