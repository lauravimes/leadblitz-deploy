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
        "max_buyers": 100,
    },
}

# NOTE: credit *costs* live in app.services.credits.CREDIT_COSTS — the single
# source of truth used both for charging and for display.


class WebhookNotConfigured(RuntimeError):
    pass


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


def _to_plain_dict(obj: Any) -> Dict[str, Any]:
    """stripe-python >= 8 returns StripeObject (not a dict subclass); older versions
    returned dict subclasses. Normalise to a plain dict either way."""
    if obj is None:
        return {}
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(obj)


def verify_webhook_signature(payload: bytes, signature: str) -> Dict[str, Any]:
    """Verify a Stripe webhook and return ``{"id", "type", "data"}`` with ``data``
    as a plain dict.

    Fails closed: if STRIPE_WEBHOOK_SECRET is not configured, no event is accepted.
    Accepting unsigned events would let anyone mint credits by POSTing JSON.
    """
    _get_stripe()
    s = get_settings()

    if not s.stripe_webhook_secret:
        raise WebhookNotConfigured("STRIPE_WEBHOOK_SECRET is not set; refusing unsigned webhook")

    event = stripe.Webhook.construct_event(payload, signature, s.stripe_webhook_secret)
    return {
        "id": event.id,
        "type": event.type,
        "data": _to_plain_dict(event.data.object),
    }


def retrieve_checkout_session(session_id: str) -> Dict[str, Any]:
    _get_stripe()
    session = stripe.checkout.Session.retrieve(session_id)
    return _to_plain_dict(session)
