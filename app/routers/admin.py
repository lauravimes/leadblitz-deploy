import logging

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user, require_admin
from app.models import User, UserCredits
from app.services.credits import CreditManager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])


@router.get("/api/admin/users")
def list_users(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)

    users = db.query(User).order_by(User.created_at.desc()).all()
    rows = []
    for u in users:
        credits = db.query(UserCredits).filter(UserCredits.user_id == u.id).first()
        rows.append({
            "id": u.id,
            "email": u.email,
            "name": u.name,
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
    require_admin(user)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return HTMLResponse('<div class="error-msg">User not found</div>')

    cm = CreditManager(db)
    cm.add(user_id, amount, "admin_grant", f"Admin grant by {user.email}")
    return HTMLResponse(f'<span class="saved-flash">Added {amount} credits to {target.email}</span>')


@router.post("/api/admin/toggle-admin")
def toggle_admin(
    request: Request,
    user_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_admin(user)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return HTMLResponse('<div class="error-msg">User not found</div>')

    if target.id == user.id:
        return HTMLResponse('<div class="error-msg">Cannot change your own admin status</div>')

    target.is_admin = not target.is_admin
    db.commit()

    status = "admin" if target.is_admin else "regular user"
    return HTMLResponse(f'<span class="saved-flash">{target.email} is now {status}</span>')
