import logging

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import JSONResponse, HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.config import get_settings
from app.models import Payment, UserSubscription
from app.services.credits import credit_manager, CREDIT_COSTS, CREDIT_COST_LABELS
from app.services.stripe_client import (
    CREDIT_PACKAGES,
    WebhookNotConfigured,
    create_checkout_session,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["credits"])


def package_sold_out(db: Session, package_id: str) -> bool:
    """Enforce ``max_buyers`` on limited packages (e.g. Founding Member)."""
    package = CREDIT_PACKAGES.get(package_id)
    if not package or not package.get("max_buyers"):
        return False
    sold = (
        db.query(func.count(Payment.id))
        .filter(Payment.plan_name == package["name"], Payment.status == "completed")
        .scalar()
    ) or 0
    return sold >= package["max_buyers"]


@router.get("/api/credits/balance")
def get_balance(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    balance = credit_manager.get_balance(db, user.id)
    return HTMLResponse(str(balance))


@router.get("/api/credits")
def get_credits(request: Request, db: Session = Depends(get_db)):
    templates = request.app.state.templates
    user = get_current_user(request, db)
    info = credit_manager.get_user_credits(db, user.id)
    return templates.TemplateResponse(
        "pages/credits.html",
        {
            "request": request,
            "user": user,
            "credits": info,
            "packages": CREDIT_PACKAGES,
            "costs": CREDIT_COSTS,
            "cost_labels": CREDIT_COST_LABELS,
            "active_page": "credits",
        },
    )


@router.get("/api/credits/history")
def credit_history(request: Request, db: Session = Depends(get_db)):
    templates = request.app.state.templates
    user = get_current_user(request, db)
    transactions = credit_manager.get_transaction_history(db, user.id)
    return templates.TemplateResponse(
        "partials/transaction_list.html",
        {"request": request, "transactions": transactions},
    )


@router.post("/api/credits/checkout")
def checkout(
    request: Request,
    package_id: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    info = credit_manager.get_user_credits(db, user.id)

    if package_id not in CREDIT_PACKAGES:
        return JSONResponse({"error": "Unknown package"}, status_code=400)
    if package_sold_out(db, package_id):
        return JSONResponse({"error": "This offer has sold out"}, status_code=400)

    settings = get_settings()
    if not settings.stripe_secret_key:
        return JSONResponse({"error": "Payments are not configured"}, status_code=503)

    base_url = str(request.base_url).rstrip("/")
    try:
        result = create_checkout_session(
            user_id=user.id,
            user_email=user.email,
            package_id=package_id,
            success_url=f"{base_url}/credits/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/credits/cancel",
            stripe_customer_id=info.get("stripe_customer_id"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        logger.exception("Stripe checkout session creation failed for user %s", user.id)
        return JSONResponse({"error": "Could not start checkout. Please try again."}, status_code=502)

    return JSONResponse({"url": result["url"]})


def grant_from_checkout_session(db: Session, session: dict, stripe_event_id: str | None = None) -> bool:
    """Shared by the webhook and the success page. ``session`` is a plain dict of a
    Stripe Checkout Session. Returns True if credits were granted by this call."""
    if session.get("payment_status") not in (None, "paid"):
        return False
    metadata = session.get("metadata") or {}
    try:
        user_id = int(metadata.get("user_id", 0))
        credits_amount = int(metadata.get("credits", 0))
        amount_cents = int(metadata.get("amount_cents", 0))
    except (TypeError, ValueError):
        return False
    plan_name = metadata.get("plan_name", "")
    session_id = session.get("id", "")
    customer = session.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")

    if not user_id or not credits_amount or not session_id:
        return False

    return credit_manager.record_purchase(
        db,
        user_id=user_id,
        credits_amount=credits_amount,
        plan_name=plan_name,
        amount_cents=amount_cents,
        checkout_session_id=session_id,
        stripe_event_id=stripe_event_id,
        stripe_customer_id=customer if isinstance(customer, str) else None,
    )


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = verify_webhook_signature(payload, sig)
    except WebhookNotConfigured as e:
        logger.error("[STRIPE WEBHOOK] %s", e)
        return JSONResponse({"error": "Webhook not configured"}, status_code=503)
    except Exception as e:
        logger.warning("[STRIPE WEBHOOK] Signature verification failed: %s", e)
        return JSONResponse({"error": "Invalid signature"}, status_code=400)

    event_type = event["type"]
    data = event["data"]

    if event_type == "checkout.session.completed":
        if data.get("payment_status") == "paid":
            grant_from_checkout_session(db, data, stripe_event_id=event.get("id"))
        else:
            logger.info("[STRIPE] Session %s completed but not paid (%s)", data.get("id"), data.get("payment_status"))
    elif event_type == "checkout.session.async_payment_succeeded":
        grant_from_checkout_session(db, data, stripe_event_id=event.get("id"))
    elif event_type == "checkout.session.expired":
        logger.info("[STRIPE] Checkout session expired")

    return JSONResponse({"status": "ok"})


@router.get("/api/payments/history")
def payment_history(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    payments = (
        db.query(Payment)
        .filter_by(user_id=user.id)
        .order_by(Payment.created_at.desc())
        .limit(50)
        .all()
    )
    return JSONResponse([
        {
            "id": p.id,
            "plan_name": p.plan_name,
            "amount_cents": p.amount_cents,
            "credits_purchased": p.credits_purchased,
            "status": p.status,
            "created_at": p.created_at.isoformat() if p.created_at else "",
        }
        for p in payments
    ])


@router.get("/api/subscriptions")
def list_subscriptions(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    subs = (
        db.query(UserSubscription)
        .filter_by(user_id=user.id)
        .order_by(UserSubscription.created_at.desc())
        .all()
    )
    return JSONResponse([
        {
            "id": s.id,
            "package_id": s.package_id,
            "status": s.status,
            "cancel_at_period_end": s.cancel_at_period_end,
            "current_period_end": s.current_period_end.isoformat() if s.current_period_end else None,
        }
        for s in subs
    ])


@router.post("/api/subscriptions/{sub_id}/cancel")
def cancel_subscription(sub_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    sub = db.query(UserSubscription).filter_by(id=sub_id, user_id=user.id).first()
    if not sub:
        return JSONResponse({"error": "Subscription not found"}, status_code=404)

    sub.cancel_at_period_end = True
    sub.status = "canceling"
    db.commit()

    if sub.stripe_subscription_id:
        try:
            import stripe
            s = get_settings()
            stripe.api_key = s.stripe_secret_key
            stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=True)
        except Exception as e:
            logger.error(f"[STRIPE] Failed to cancel subscription: {e}")

    return JSONResponse({"status": "canceled_at_period_end"})
