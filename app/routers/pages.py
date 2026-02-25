from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_optional_user, get_current_user, require_admin
from app.models import User, Campaign, Lead

router = APIRouter(tags=["pages"])


def _tpl(request: Request):
    return request.app.state.templates


@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    user = get_optional_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    user = get_optional_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return _tpl(request).TemplateResponse("pages/login.html", {"request": request, "user": None})


@router.get("/register")
def register_page(request: Request, db: Session = Depends(get_db)):
    user = get_optional_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return _tpl(request).TemplateResponse("pages/register.html", {"request": request, "user": None})


@router.get("/dashboard")
def dashboard_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return _tpl(request).TemplateResponse(
        "pages/dashboard.html",
        {"request": request, "user": user, "active_page": "dashboard"},
    )


@router.get("/search")
def search_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    campaigns = (
        db.query(Campaign)
        .filter(Campaign.user_id == user.id)
        .order_by(Campaign.created_at.desc())
        .limit(5)
        .all()
    )
    return _tpl(request).TemplateResponse(
        "pages/search.html",
        {"request": request, "user": user, "campaigns": campaigns, "active_page": "search"},
    )


@router.get("/leads")
def leads_page(
    request: Request,
    stage: str = None,
    campaign_id: str = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    q = db.query(Lead).filter(Lead.user_id == user.id)
    if stage:
        q = q.filter(Lead.stage == stage)
    if campaign_id:
        q = q.filter(Lead.campaign_id == campaign_id)
    leads = q.order_by(Lead.created_at.desc()).all()

    campaigns = (
        db.query(Campaign)
        .filter(Campaign.user_id == user.id)
        .order_by(Campaign.created_at.desc())
        .all()
    )
    return _tpl(request).TemplateResponse(
        "pages/leads.html",
        {
            "request": request,
            "user": user,
            "leads": leads,
            "campaigns": campaigns,
            "stage_filter": stage,
            "campaign_filter": campaign_id,
            "active_page": "leads",
        },
    )


@router.get("/leads/{lead_id}")
def lead_detail_page(lead_id: str, request: Request, db: Session = Depends(get_db)):
    from app.services.technographics import classify_tech_health

    user = get_current_user(request, db)
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.user_id == user.id).first()
    if not lead:
        return RedirectResponse("/leads", status_code=302)
    return _tpl(request).TemplateResponse(
        "pages/lead_detail.html",
        {
            "request": request,
            "user": user,
            "lead": lead,
            "active_page": "leads",
            "classify_tech_health": classify_tech_health,
        },
    )


@router.get("/email")
def email_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return _tpl(request).TemplateResponse(
        "pages/email.html",
        {"request": request, "user": user, "active_page": "email"},
    )


@router.get("/sms")
def sms_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return _tpl(request).TemplateResponse(
        "pages/sms.html",
        {"request": request, "user": user, "active_page": "sms"},
    )


@router.get("/credits")
def credits_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    from app.services.stripe_client import CREDIT_PACKAGES, CREDIT_COSTS
    from app.config import get_settings
    settings = get_settings()
    return _tpl(request).TemplateResponse(
        "pages/credits.html",
        {
            "request": request,
            "user": user,
            "active_page": "credits",
            "packages": CREDIT_PACKAGES,
            "costs": CREDIT_COSTS,
            "stripe_pk": settings.stripe_publishable_key,
        },
    )


@router.get("/credits/success")
def payment_success_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return _tpl(request).TemplateResponse(
        "pages/payment_success.html",
        {"request": request, "user": user, "active_page": "credits"},
    )


@router.get("/credits/cancel")
def payment_cancel_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return _tpl(request).TemplateResponse(
        "pages/payment_cancel.html",
        {"request": request, "user": user, "active_page": "credits"},
    )


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return _tpl(request).TemplateResponse(
        "pages/settings.html",
        {"request": request, "user": user, "active_page": "settings"},
    )


@router.get("/import")
def import_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return _tpl(request).TemplateResponse(
        "pages/import.html",
        {"request": request, "user": user, "active_page": "leads"},
    )


@router.get("/forgot-password")
def forgot_password_page(request: Request):
    return _tpl(request).TemplateResponse(
        "pages/forgot_password.html",
        {"request": request, "user": None},
    )


@router.get("/reset-password")
def reset_password_page(request: Request, token: str = ""):
    return _tpl(request).TemplateResponse(
        "pages/reset_password.html",
        {"request": request, "user": None, "token": token},
    )


@router.get("/admin")
def admin_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
    return _tpl(request).TemplateResponse(
        "pages/admin.html",
        {"request": request, "user": user, "active_page": "admin"},
    )
