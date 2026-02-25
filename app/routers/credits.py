import logging

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.config import get_settings
from app.models import User, Payment, UserSubscription
from app.services.credits import credit_manager, CREDIT_COSTS
from app.services.stripe_client import CREDIT_PACKAGES, create_checkout_session, verify_webhook_signature

logger = logging.getLogger(__name__)
router = APIRouter(tags=["credits"])


@router.get("/api/credits/balance")
def get_balance(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    balance = credit_manager.get_balance(db, user.id)
    from fastapi.responses import HTMLResponse
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

    return JSONResponse({"url": result["url"]})


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = verify_webhook_signature(payload, sig)
    except Exception as e:
        logger.error(f"[STRIPE WEBHOOK] Signature verification failed: {e}")
        return JSONResponse({"error": "Invalid signature"}, status_code=400)

    event_type = event["type"]
    data = event["data"]

    if event_type == "checkout.session.completed":
        metadata = data.get("metadata", {})
        user_id = int(metadata.get("user_id", 0))
        package_id = metadata.get("package_id", "")
        credits_amount = int(metadata.get("credits", 0))
        plan_name = metadata.get("plan_name", "")
        amount_cents = int(metadata.get("amount_cents", 0))
        session_id = data.get("id", "")

        if user_id and credits_amount:
            if not credit_manager.check_duplicate_session(db, session_id):
                credit_manager.add_credits(
                    db, user_id, credits_amount,
                    f"Purchased {plan_name} ({credits_amount} credits)",
                    stripe_checkout_session_id=session_id,
                )
                payment = Payment(
                    user_id=user_id,
                    stripe_session_id=session_id,
                    amount_cents=amount_cents,
                    credits_purchased=credits_amount,
                    plan_name=plan_name,
                    status="completed",
                )
                db.add(payment)
                db.commit()
                logger.info(f"[STRIPE] Added {credits_amount} credits to user {user_id}")

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
