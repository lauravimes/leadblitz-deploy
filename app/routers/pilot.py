import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit
from fastapi import APIRouter, Request, Depends, Form, HTTPException, BackgroundTasks
from fastapi.responses import RedirectResponse, Response
from itsdangerous import BadSignature
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.deps import get_db, get_optional_user, require_admin
from app.models import PilotPlan, Campaign, Lead, LeadOutcome, Payment
from app.services.pilot import COOKIE, MAX_AGE, OPTIONS, NICHES, serializer, cookie_plan, recommendation, notify_application
from app.services.rate_limit import pilot_limiter, client_ip
from app.validation import normalize_email, is_valid_email

router = APIRouter(tags=["pilot"])


def now():
    return datetime.now(timezone.utc)


def limited(request):
    if not pilot_limiter.allow(client_ip(request)):
        raise HTTPException(429, "Please wait a little before trying again.")


def check_form(request, csrf):
    if not csrf or not secrets.compare_digest(csrf, request.cookies.get(COOKIE, "")):
        raise HTTPException(403, "This form expired. Open the questionnaire again.")


def valid_portfolio(value):
    try:
        parts = urlsplit(value)
        return len(value) <= 500 and parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username and not parts.password
    except ValueError:
        return False


def private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    return response


def render(request, template, **context):
    return private(request.app.state.templates.TemplateResponse(template, {"request": request, **context}))


@router.get("/client-plan")
def questionnaire(request: Request, db: Session = Depends(get_db)):
    limited(request)
    plan = cookie_plan(request, db)
    if plan is None:
        clean = lambda k, default="": re.sub(r"[^a-zA-Z0-9_\-]", "", request.query_params.get(k, default))[:100]
        plan = PilotPlan(source=clean("utm_source", "direct"), campaign=clean("utm_campaign"), content=clean("utm_content"))
        db.add(plan); db.commit()
    token = serializer().dumps(plan.id)
    response = render(request, "pages/client_plan.html", plan=plan, options=OPTIONS, csrf=token, user=get_optional_user(request, db))
    response.set_cookie(COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return response


@router.post("/client-plan/start")
def started(request: Request, csrf: str = Form(...), db: Session = Depends(get_db)):
    limited(request); check_form(request, csrf)
    plan = cookie_plan(request, db)
    if not plan:
        raise HTTPException(403, "Please reopen the questionnaire.")
    if not plan.started_at:
        plan.started_at = now(); db.commit()
    return Response(status_code=204)


@router.post("/client-plan")
def complete(request: Request, csrf: str = Form(...), experience: str = Form(...), offer: str = Form(...),
             obstacle: str = Form(...), niche: str = Form(...), location: str = Form(...), db: Session = Depends(get_db)):
    limited(request); check_form(request, csrf)
    plan = cookie_plan(request, db)
    if not plan:
        raise HTTPException(403, "Please reopen the questionnaire.")
    answers = dict(experience=experience, offer=offer, obstacle=obstacle, niche=niche)
    if any(v not in OPTIONS[k] for k, v in answers.items()) or not 2 <= len(location.strip()) <= 120:
        raise HTTPException(422, "Choose an answer to each question and enter a town or city (2–120 characters).")
    answers["location"] = location.strip()
    plan.answers = answers; plan.started_at = plan.started_at or now(); plan.completed_at = now()
    user = get_optional_user(request, db)
    if user and plan.user_id in (None, user.id):
        plan.user_id = user.id
    db.commit()
    return private(RedirectResponse("/client-plan/result", status_code=303))


@router.get("/client-plan/result")
def result(request: Request, db: Session = Depends(get_db)):
    plan = cookie_plan(request, db)
    if not plan or not plan.answers:
        return RedirectResponse("/client-plan", status_code=302)
    return render(request, "pages/client_plan_result.html", plan=plan, result=recommendation(plan.answers),
                  csrf=request.cookies[COOKIE], user=get_optional_user(request, db), error=None, values={})


@router.post("/client-plan/apply")
def apply(request: Request, background: BackgroundTasks, csrf: str = Form(...), name: str = Form(...),
          email: str = Form(...), portfolio: str = Form(""), ready: str = Form(""), consent: str = Form(""),
          website_confirm: str = Form(""), db: Session = Depends(get_db)):
    limited(request); check_form(request, csrf)
    plan = cookie_plan(request, db)
    if not plan or not plan.answers:
        raise HTTPException(403, "Complete your plan first.")
    email = normalize_email(email)
    portfolio = portfolio.strip()
    error = None
    if website_confirm:
        raise HTTPException(422, "Unable to submit this application.")
    if not 1 <= len(name.strip()) <= 120 or not is_valid_email(email):
        error = "Please enter your name and a valid email address."
    elif portfolio and not valid_portfolio(portfolio):
        error = "Enter a full portfolio URL starting with https://, or leave it blank."
    elif consent != "yes":
        error = "Please confirm we may contact you about your application."
    if error:
        return render(request, "pages/client_plan_result.html", plan=plan, result=recommendation(plan.answers),
                      csrf=csrf, user=get_optional_user(request, db), error=error, values=dict(name=name, email=email, portfolio=portfolio))
    plan = db.query(PilotPlan).filter(PilotPlan.id == plan.id).populate_existing().with_for_update().one()
    # Idempotent per browser plan: refresh/retry does not send another notification.
    if not plan.applied_at:
        plan.applicant_name = name.strip(); plan.applicant_email = email; plan.portfolio = portfolio
        plan.ready_this_week = ready == "yes"; plan.applied_at = now(); plan.status = "new"
        db.commit()
        background.add_task(notify_application, plan.id)
    return private(RedirectResponse("/client-plan/result#pilot", status_code=303))


@router.get("/admin/pilot")
def admin_pilot(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    applications = db.query(PilotPlan).filter(PilotPlan.applied_at.isnot(None)).order_by(PilotPlan.applied_at.desc()).limit(500).all()
    metrics = dict(visits=db.query(PilotPlan).count(), started=db.query(PilotPlan).filter(PilotPlan.started_at.isnot(None)).count(),
                   completed=db.query(PilotPlan).filter(PilotPlan.completed_at.isnot(None)).count(), applications=db.query(PilotPlan).filter(PilotPlan.applied_at.isnot(None)).count())
    # Counts are distinct linked accounts; stage history preserves earlier milestones.
    cohort = db.query(PilotPlan.user_id.label("uid"), func.min(PilotPlan.completed_at).label("joined_at")).filter(PilotPlan.user_id.isnot(None), PilotPlan.completed_at.isnot(None)).group_by(PilotPlan.user_id).subquery()
    ids = [x[0] for x in db.query(cohort.c.uid)]
    metrics["linked_accounts"] = len(ids)
    metrics["searched"] = db.query(func.count(func.distinct(Campaign.user_id))).join(cohort, cohort.c.uid == Campaign.user_id).filter(Campaign.user_id.in_(ids), Campaign.created_at >= cohort.c.joined_at).scalar()
    metrics["scored"] = db.query(func.count(func.distinct(Lead.user_id))).join(cohort, cohort.c.uid == Lead.user_id).filter(Lead.user_id.in_(ids), Lead.last_scored_at >= cohort.c.joined_at).scalar()
    metrics["sent_in_app"] = db.query(func.count(func.distinct(Lead.user_id))).join(cohort, cohort.c.uid == Lead.user_id).filter(Lead.user_id.in_(ids), Lead.last_emailed_at >= cohort.c.joined_at).scalar()
    for stage in ("contacted", "replied", "meeting", "won"):
        metrics[stage] = db.query(func.count(func.distinct(LeadOutcome.user_id))).join(cohort, cohort.c.uid == LeadOutcome.user_id).filter(LeadOutcome.user_id.in_(ids), LeadOutcome.stage == stage, LeadOutcome.created_at >= cohort.c.joined_at).scalar()
    metrics["purchased"] = db.query(func.count(func.distinct(Payment.user_id))).join(cohort, cohort.c.uid == Payment.user_id).filter(Payment.user_id.in_(ids), Payment.status == "completed", Payment.created_at >= cohort.c.joined_at).scalar()
    sources = db.query(PilotPlan.source, PilotPlan.campaign, PilotPlan.content, func.count(PilotPlan.id), func.count(PilotPlan.completed_at), func.count(PilotPlan.applied_at)).group_by(PilotPlan.source, PilotPlan.campaign, PilotPlan.content).all()
    return render(request, "pages/pilot_admin.html", user=user, applications=applications, metrics=metrics, sources=sources,
                  options=OPTIONS, csrf=serializer().dumps({"admin": user.id}), active_page="admin")


@router.post("/admin/pilot/{plan_id}")
def review_application(plan_id: str, request: Request, csrf: str = Form(...), status: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    try:
        valid = serializer().loads(csrf, max_age=3600).get("admin") == user.id
    except (BadSignature, AttributeError):
        valid = False
    if not valid:
        raise HTTPException(403, "Reload the application list before saving.")
    if status not in ("new", "invited", "scheduled", "session_done", "followed_up", "closed") or len(notes)>5000:
        raise HTTPException(422, "Choose a valid status; notes must be under 5,000 characters.")
    plan = db.get(PilotPlan, plan_id)
    if not plan or not plan.applied_at:
        raise HTTPException(404, "Application not found")
    plan.status = status; plan.admin_notes = notes; db.commit()
    return private(RedirectResponse("/admin/pilot#application-"+plan.id, status_code=303))
