import logging
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

import models, schemas
from auth import (
    create_access_token,
    get_admin_user,
    get_current_user,
    hash_password,
    verify_password,
)
from config import settings
from database import Base, engine, get_db
from integrations import push_to_kit, send_reset_email, send_verify_email

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("torc")

# Creates the users table on first boot if it doesn't exist.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="TORC API", version="1.0.0")

# --- CORS ---
# The browser blocks requests from your Netlify site to this API unless the
# API explicitly says that origin is allowed. FRONTEND_URL must exactly match
# your live site (no trailing slash), e.g. https://legendary-pika-5dd723.netlify.app
allowed_origins = [settings.FRONTEND_URL]
if settings.FRONTEND_URL.startswith("https://") and ".netlify.app" in settings.FRONTEND_URL:
    # Netlify deploy previews get their own subdomains; allow the site's own origin only.
    pass

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "service": "TORC API",
        "status": "ok",
        "founder_window_open": settings.FOUNDER_WINDOW_OPEN,
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


# ---------------- Auth ----------------

@app.post("/auth/check-email", response_model=schemas.CheckEmailResponse)
def check_email(body: schemas.CheckEmailRequest, db: Session = Depends(get_db)):
    """
    Frontend calls this first. If the email already has an account, show the
    password field for LOGIN. If not, show the signup fields. This is how
    apps decide 'new user or returning user' from a single email box.
    """
    exists = (
        db.query(models.User)
        .filter(models.User.email == body.email.lower())
        .first()
        is not None
    )
    return schemas.CheckEmailResponse(exists=exists)


@app.post("/auth/signup", response_model=schemas.TokenResponse, status_code=201)
async def signup(body: schemas.SignupRequest, db: Session = Depends(get_db)):
    email = body.email.lower()

    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists.",
        )

    verify_token = secrets.token_urlsafe(32)

    user = models.User(
        email=email,
        password_hash=hash_password(body.password),
        name=body.name,
        # THE FOUNDER STAMP. Set once, here, and never recalculated.
        # When you close the founding window (FOUNDER_WINDOW_OPEN=false in Render),
        # new users get is_founder=False, but everyone already in the table keeps True.
        #
        # NOTE: is_founder alone is not enough. Founding pricing only APPLIES
        # once email_verified is also True — see /auth/verify-email. That's what
        # stops someone locking a founder rate with a fake address.
        is_founder=settings.FOUNDER_WINDOW_OPEN,
        email_verified=False,
        verify_token=verify_token,
        verify_token_expires=datetime.utcnow() + timedelta(hours=48),
        last_login=datetime.utcnow(),
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    # Send the "confirm your email" link. Fails soft if Resend isn't configured.
    await send_verify_email(user.email, verify_token)

    # Push to Kit so they land on your marketing list too. Fails soft.
    await push_to_kit(user.email, user.name)

    log.info("New signup: %s (founder=%s, unverified)", user.email, user.is_founder)

    return schemas.TokenResponse(
        access_token=create_access_token(user.id),
        user=schemas.UserOut.model_validate(user),
    )


@app.post("/auth/login", response_model=schemas.TokenResponse)
def login(body: schemas.LoginRequest, db: Session = Depends(get_db)):
    email = body.email.lower()
    user = db.query(models.User).filter(models.User.email == email).first()

    # Same error for "no such user" and "wrong password" — don't leak which
    # emails have accounts.
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been deactivated.")

    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)

    return schemas.TokenResponse(
        access_token=create_access_token(user.id),
        user=schemas.UserOut.model_validate(user),
    )


# ---------------- Email verification ----------------

@app.post("/auth/verify-email", response_model=schemas.TokenResponse)
def verify_email(body: schemas.VerifyEmailRequest, db: Session = Depends(get_db)):
    """
    Consumes the token from the emailed link. THIS is the moment founding
    pricing actually becomes real for the account.
    """
    user = (
        db.query(models.User)
        .filter(models.User.verify_token == body.token)
        .first()
    )

    if (
        not user
        or not user.verify_token_expires
        or user.verify_token_expires < datetime.utcnow()
    ):
        raise HTTPException(
            status_code=400,
            detail="This verification link is invalid or has expired. Request a new one.",
        )

    user.email_verified = True
    user.verify_token = None
    user.verify_token_expires = None
    db.commit()
    db.refresh(user)

    log.info("Email verified: %s (founder=%s)", user.email, user.is_founder)

    # Log them straight in — no reason to make them type a password again.
    return schemas.TokenResponse(
        access_token=create_access_token(user.id),
        user=schemas.UserOut.model_validate(user),
    )


@app.post("/auth/resend-verification", response_model=schemas.MessageResponse)
async def resend_verification(
    body: schemas.ResendVerifyRequest, db: Session = Depends(get_db)
):
    """Re-issues a verification link. Same generic response either way."""
    email = body.email.lower()
    user = db.query(models.User).filter(models.User.email == email).first()

    generic = "If that account exists and isn't verified, a new link is on its way."

    if not user or user.email_verified:
        return schemas.MessageResponse(message=generic)

    token = secrets.token_urlsafe(32)
    user.verify_token = token
    user.verify_token_expires = datetime.utcnow() + timedelta(hours=48)
    db.commit()

    sent = await send_verify_email(user.email, token)

    if not sent:
        # Resend isn't wired up yet — hand the token back so the flow is testable.
        return schemas.MessageResponse(message=generic, debug_reset_token=token)

    return schemas.MessageResponse(message=generic)


# ---------------- Password reset ----------------

@app.post("/auth/forgot-password", response_model=schemas.MessageResponse)
async def forgot_password(
    body: schemas.ForgotPasswordRequest, db: Session = Depends(get_db)
):
    email = body.email.lower()
    user = db.query(models.User).filter(models.User.email == email).first()

    generic = "If an account exists for that email, a reset link is on its way."

    # Always return the same message whether or not the account exists.
    if not user:
        return schemas.MessageResponse(message=generic)

    token = secrets.token_urlsafe(32)
    user.reset_token = token
    user.reset_token_expires = datetime.utcnow() + timedelta(
        minutes=settings.RESET_TOKEN_EXPIRE_MINUTES
    )
    db.commit()

    sent = await send_reset_email(user.email, token)

    if not sent:
        # Resend isn't configured yet. Return the token directly so you can
        # still test the flow. REMOVE THIS BEHAVIOR by setting RESEND_API_KEY.
        return schemas.MessageResponse(message=generic, debug_reset_token=token)

    return schemas.MessageResponse(message=generic)


@app.post("/auth/reset-password", response_model=schemas.MessageResponse)
def reset_password(body: schemas.ResetPasswordRequest, db: Session = Depends(get_db)):
    user = (
        db.query(models.User)
        .filter(models.User.reset_token == body.token)
        .first()
    )

    if (
        not user
        or not user.reset_token_expires
        or user.reset_token_expires < datetime.utcnow()
    ):
        raise HTTPException(status_code=400, detail="This reset link is invalid or expired.")

    user.password_hash = hash_password(body.new_password)
    user.reset_token = None
    user.reset_token_expires = None
    db.commit()

    return schemas.MessageResponse(message="Password updated. You can log in now.")


# ---------------- Account ----------------

@app.get("/me", response_model=schemas.UserOut)
def get_me(current_user: models.User = Depends(get_current_user)):
    """Dashboard calls this on load to render the account state."""
    return current_user


@app.patch("/me", response_model=schemas.UserOut)
def update_me(
    body: schemas.UpdateProfileRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.name is not None:
        current_user.name = body.name
    db.commit()
    db.refresh(current_user)
    return current_user


@app.post("/me/change-password", response_model=schemas.MessageResponse)
def change_password(
    body: schemas.ChangePasswordRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    current_user.password_hash = hash_password(body.new_password)
    db.commit()

    return schemas.MessageResponse(message="Password changed.")


@app.delete("/me", response_model=schemas.MessageResponse)
def delete_me(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hard delete. The user is gone, including their founder status."""
    db.delete(current_user)
    db.commit()
    return schemas.MessageResponse(message="Account deleted.")


# ---------------- Admin ----------------

@app.get("/admin/stats", response_model=schemas.AdminStatsOut)
def admin_stats(
    admin: models.User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Everyone who has signed up, plus the counts that matter.
    Locked to ADMIN_EMAIL — enforced here on the server, not in the browser.
    """
    now = datetime.utcnow()
    start_of_today = datetime(now.year, now.month, now.day)
    week_ago = now - timedelta(days=7)

    users = (
        db.query(models.User)
        .order_by(models.User.created_at.desc())
        .all()
    )

    # A founder only counts if they verified. Unverified signups are noise.
    founders    = sum(1 for u in users if u.is_founder and u.email_verified)
    unverified  = sum(1 for u in users if not u.email_verified)

    return schemas.AdminStatsOut(
        total_users=len(users),
        founders=founders,
        unverified=unverified,
        non_founders=len(users) - founders - unverified,
        signups_today=sum(1 for u in users if u.created_at >= start_of_today),
        signups_this_week=sum(1 for u in users if u.created_at >= week_ago),
        founder_window_open=settings.FOUNDER_WINDOW_OPEN,
        users=[schemas.AdminUserOut.model_validate(u) for u in users],
    )


@app.get("/admin/export")
def admin_export(
    admin: models.User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    CSV of every user. Useful for bulk-importing into Kit, or as a backup
    of who your founders are independent of the database.
    """
    users = db.query(models.User).order_by(models.User.created_at.asc()).all()

    lines = ["email,name,is_founder,email_verified,created_at,last_login"]
    for u in users:
        name = (u.name or "").replace(",", " ")
        last = u.last_login.isoformat() if u.last_login else ""
        lines.append(
            f"{u.email},{name},{u.is_founder},{u.email_verified},"
            f"{u.created_at.isoformat()},{last}"
        )

    return Response(
        content="\n".join(lines),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=torc-users.csv"},
    )
