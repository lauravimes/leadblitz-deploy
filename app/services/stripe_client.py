import json
import logging
from typing import Any, Dict, Optional

import stripe

from app.config import get_settings

logger = logging.getLogger(__name__)

CREDIT_PACKAGES = {
    "starter": {
        "name": "Starter",
        "credits": 100,
        "price_cents": 1500,
        "description": "100 credits for AI lead scoring, outreach, and more",
    },
    "professional": {
        "name": "Professional",
        "credits": 500,
        "price_cents": 5900,
        "description": "500 credits for growing agencies",
    },
    "pro_team": {
        "name": "Pro Team",
        "credits": 2000,
        "price_cents": 19900,
        "description": "2000 credits for high-volume teams",
    },
    "founding_member": {
        "name": "Founding Member",
        "credits": 2000,
        "price_cents": 9900,
        "description": "2000 credits — 50% off Pro Team (limited to first 100 buyers)",
    },
}

CREDIT_COSTS = {
    "ai_scoring": 1,
    "email_send": 0,
    "sms_send": 2,
    "lead_search": 0,
    "email_personalization": 1,
}


def _get_stripe():
    s = get_settings()
    stripe.api_key = s.stripe_secret_key
    return stripe


def create_checkout_session(
    user_id: int,
    user_email: str,
    package_id: str,
    success_url: str,
    cancel_url: str,
    stripe_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    if package_id not in CREDIT_PACKAGES:
        raise ValueError(f"Invalid package: {package_id}")

    package = CREDIT_PACKAGES[package_id]
    _get_stripe()

    session_params = {
        "payment_method_types": ["card"],
        "line_items": [
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": package["name"],
                        "description": package["description"],
                    },
                    "unit_amount": package["price_cents"],
                },
                "quantity": 1,
            }
        ],
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {
            "user_id": str(user_id),
            "package_id": package_id,
            "credits": str(package["credits"]),
            "plan_name": package["name"],
            "amount_cents": str(package["price_cents"]),
        },
    }

    if stripe_customer_id:
        session_params["customer"] = stripe_customer_id
    else:
        session_params["customer_email"] = user_email

    session = stripe.checkout.Session.create(**session_params)
    return {"session_id": session.id, "url": session.url}


def verify_webhook_signature(payload: bytes, signature: str) -> Dict[str, Any]:
    _get_stripe()
    s = get_settings()

    if not s.stripe_webhook_secret:
        event_data = json.loads(payload.decode("utf-8"))
        event = stripe.Event.construct_from(event_data, stripe.api_key)
        return {"type": event.type, "data": event.data.object}

    event = stripe.Webhook.construct_event(payload, signature, s.stripe_webhook_secret)
    return {"type": event.type, "data": event.data.object}
