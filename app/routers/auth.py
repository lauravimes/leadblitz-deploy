from fastapi import APIRouter, Request, Response, Depends, Form
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models import User
from app.auth.passwords import hash_password, verify_password
from app.auth.sessions import create_token

router = APIRouter(tags=["auth"])


def _set_session(response: Response, user: User) -> Response:
    token = create_token(user.id)
    response.set_cookie(
        "session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30,
    )
    return response


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = db.query(User).filter(User.email == email.lower().strip()).first()

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "Invalid email or password"},
        )

    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/search"
    return _set_session(response, user)


@router.post("/register")
def register(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    email_clean = email.lower().strip()

    if db.query(User).filter(User.email == email_clean).first():
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "An account with that email already exists"},
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "Password must be at least 8 characters"},
        )

    user = User(
        email=email_clean,
        password_hash=hash_password(password),
        full_name=full_name.strip(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/search"
    return _set_session(response, user)


@router.post("/logout")
def logout():
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/login"
    response.delete_cookie("session")
    return response
