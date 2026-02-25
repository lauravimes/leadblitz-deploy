import logging

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.models import User, UserCredits, CreditTransaction
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
        rows.append({
            "id": u.id,
            "email": u.email,
            "name": u.full_name,
            "is_admin": u.is_admin,
            "created_at": u.created_at,
            "balance": credits.balance if credits else 0,
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
