import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.deps import get_db, get_current_user
from app.models import User, UserCredits, CreditTransaction, Lead, Campaign, Payment
from app.services.credits import credit_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])


def _check_admin(user: User):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/api/admin/users")
def list_users(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    _check_admin(user)

    users = db.query(User).order_by(User.created_at.desc()).all()
    rows = []
    for u in users:
        credits = db.query(UserCredits).filter(UserCredits.user_id == u.id).first()
        lead_count = db.query(func.count(Lead.id)).filter(Lead.user_id == u.id).scalar() or 0
        total_spent = (
            db.query(func.coalesce(func.sum(Payment.amount_cents), 0))
            .filter(Payment.user_id == u.id, Payment.status == "completed")
            .scalar()
        )
        domain = u.email.rsplit("@", 1)[-1] if "@" in u.email else ""
        rows.append({
            "id": u.id,
            "email": u.email,
            "name": u.full_name,
            "domain": domain,
            "is_admin": u.is_admin,
            "created_at": u.created_at,
            "balance": credits.balance if credits else 0,
            "signup_ip": u.signup_ip or "—",
            "lead_count": lead_count,
            "total_spent_cents": total_spent,
        })

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "partials/admin_user_list.html",
        {"request": request, "users": rows},
    )


@router.post("/api/admin/credits/add")
def admin_add_credits(
    request: Request,
    user_id: int = Form(...),
    amount: int = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    _check_admin(user)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return HTMLResponse('<div class="error-msg">User not found</div>')

    new_balance = credit_manager.add_credits(db, user_id, amount, f"Admin grant by {user.email}")
    return HTMLResponse(f'<span class="saved-flash">Added {amount} credits to {target.email} (balance: {new_balance})</span>')


@router.post("/api/admin/credits/set")
def admin_set_credits(
    request: Request,
    user_id: int = Form(...),
    balance: int = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    _check_admin(user)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return HTMLResponse('<div class="error-msg">User not found</div>')

    credits = db.query(UserCredits).filter(UserCredits.user_id == user_id).first()
    if not credits:
        credits = UserCredits(user_id=user_id, balance=0, total_purchased=0, total_used=0)
        db.add(credits)
        db.flush()

    old_balance = credits.balance or 0
    diff = balance - old_balance
    credits.balance = balance
    if diff > 0:
        credits.total_purchased = (credits.total_purchased or 0) + diff

    transaction = CreditTransaction(
        user_id=user_id,
        amount=diff,
        transaction_type="admin_set",
        description=f"Balance set to {balance} by {user.email} (was {old_balance})",
        balance_after=balance,
    )
    db.add(transaction)
    db.commit()

    return HTMLResponse(f'<span class="saved-flash">Set {target.email} balance to {balance} credits (was {old_balance})</span>')


@router.post("/api/admin/toggle-admin")
def toggle_admin(
    request: Request,
    user_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    _check_admin(user)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return HTMLResponse('<div class="error-msg">User not found</div>')

    if target.id == user.id:
        return HTMLResponse('<div class="error-msg">Cannot change your own admin status</div>')

    target.is_admin = not target.is_admin
    db.commit()

    status = "admin" if target.is_admin else "regular user"
    return HTMLResponse(f'<span class="saved-flash">{target.email} is now {status}</span>')


@router.post("/api/admin/backfill-emailed")
def backfill_emailed(
    request: Request,
    db: Session = Depends(get_db),
):
    """Mark all leads that have an email but no last_emailed_at as emailed once."""
    user = get_current_user(request, db)
    _check_admin(user)

    leads = db.query(Lead).filter(
        Lead.email.isnot(None),
        Lead.email != "",
        Lead.last_emailed_at.is_(None),
        Lead.emails_sent_count <= 0,
    ).all()

    now = datetime.now(timezone.utc)
    count = 0
    for lead in leads:
        lead.last_emailed_at = now
        lead.emails_sent_count = 1
        count += 1
    db.commit()

    return HTMLResponse(f'<span class="saved-flash">Backfilled {count} leads as emailed</span>')


@router.get("/api/admin/users/{user_id}")
def user_detail(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Expanded user detail panel — loaded via HTMX into admin page."""
    user = get_current_user(request, db)
    _check_admin(user)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return HTMLResponse('<div class="error-msg">User not found</div>')

    credits = db.query(UserCredits).filter(UserCredits.user_id == user_id).first()

    # Activity stats
    lead_count = db.query(func.count(Lead.id)).filter(Lead.user_id == user_id).scalar() or 0
    scored_count = (
        db.query(func.count(Lead.id))
        .filter(Lead.user_id == user_id, Lead.score.isnot(None))
        .scalar()
    ) or 0
    emailed_count = (
        db.query(func.count(Lead.id))
        .filter(Lead.user_id == user_id, Lead.emails_sent_count > 0)
        .scalar()
    ) or 0
    campaign_count = db.query(func.count(Campaign.id)).filter(Campaign.user_id == user_id).scalar() or 0

    # Purchases
    payments = (
        db.query(Payment)
        .filter(Payment.user_id == user_id, Payment.status == "completed")
        .order_by(Payment.created_at.desc())
        .limit(20)
        .all()
    )

    # Credit transactions (last 20)
    transactions = (
        db.query(CreditTransaction)
        .filter(CreditTransaction.user_id == user_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(20)
        .all()
    )

    # Find other accounts sharing this IP
    linked_accounts = []
    if target.signup_ip:
        linked = (
            db.query(User)
            .filter(User.signup_ip == target.signup_ip, User.id != user_id)
            .all()
        )
        for la in linked:
            la_credits = db.query(UserCredits).filter(UserCredits.user_id == la.id).first()
            linked_accounts.append({
                "id": la.id,
                "email": la.email,
                "created_at": la.created_at,
                "balance": la_credits.balance if la_credits else 0,
            })

    domain = target.email.rsplit("@", 1)[-1] if "@" in target.email else ""

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "partials/admin_user_detail.html",
        {
            "request": request,
            "target": target,
            "domain": domain,
            "credits": credits,
            "lead_count": lead_count,
            "scored_count": scored_count,
            "emailed_count": emailed_count,
            "campaign_count": campaign_count,
            "payments": payments,
            "transactions": transactions,
            "linked_accounts": linked_accounts,
        },
    )
