from datetime import datetime, timezone
from itsdangerous import URLSafeTimedSerializer, BadSignature
from app.config import get_settings
from app.models import PilotPlan

COOKIE = "leadblitz_plan"
MAX_AGE = 60 * 60 * 24 * 30
OPTIONS = {
    "experience": {"first": "Working towards my first client", "occasional": "Occasional paid projects", "established": "An established web business"},
    "offer": {"new": "New websites", "redesign": "Website redesigns", "improvements": "Ongoing improvements"},
    "obstacle": {"prospects": "Finding suitable prospects", "message": "Knowing what to say", "replies": "Getting replies"},
    "niche": {"trades": "Trades and home services", "health": "Dental and health practices", "professional": "Professional services", "hospitality": "Restaurants and hospitality", "choose": "Help me choose"},
}
NICHES = {"trades": "plumber", "health": "dentist", "professional": "accountant", "hospitality": "restaurant", "choose": "plumber"}


def serializer():
    return URLSafeTimedSerializer(get_settings().session_secret, salt="leadblitz-pilot-v1")


def cookie_plan(request, db):
    try:
        pid = serializer().loads(request.cookies.get(COOKIE, ""), max_age=MAX_AGE)
        if not isinstance(pid, str):
            return None
        return db.get(PilotPlan, pid)
    except BadSignature:
        return None


def link_plan(request, db, user):
    plan = cookie_plan(request, db)
    if plan and plan.completed_at and plan.user_id in (None, user.id):
        plan.user_id = user.id
        db.commit()
        return plan
    return None


def recommendation(answers):
    niche = NICHES[answers["niche"]]
    offer = {"new": "a simple first website", "redesign": "a focused website redesign", "improvements": "a small, specific website improvement"}[answers["offer"]]
    focus = {
        "prospects": "Start with a small shortlist. Check each business is operating, has a contact route and fits the service you can deliver.",
        "message": "Choose one issue you have checked yourself. Write a short note explaining what you found and ask whether they would like to see your suggestion.",
        "replies": "Review the fit and specificity of your last messages. Use one checked observation and a small next step, then record positive replies separately from all replies.",
    }[answers["obstacle"]]
    return {"niche": niche, "offer": offer, "focus": focus,
            "beginner": answers["experience"] == "first",
            "suggested_niche": answers["niche"] == "choose"}


def notify_application(plan_id):
    from app.database import SessionLocal
    from app.services.system_email import send_system_email, build_branded_email
    # Personal application details stay in the authenticated admin view.
    db = SessionLocal()
    try:
        plan = db.get(PilotPlan, plan_id)
        if plan and plan.applied_at and not plan.notified_at:
            ok = send_system_email("sh@shapplications.com", "New LeadBlitz pilot application",
                build_branded_email("A designer has applied", "<p>A new pilot application is ready to review in your LeadBlitz admin area.</p>", "Review applications", "https://leadblitz.co/admin/pilot"))
            if ok:
                plan.notified_at = datetime.now(timezone.utc)
                db.commit()
    finally:
        db.close()
