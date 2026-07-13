import logging
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

import models, schemas
from auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from config import settings
from database import Base, engine, get_db
from integrations import push_to_kit, send_reset_email

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

    user = models.User(
        email=email,
        password_hash=hash_password(body.password),
        name=body.name,
        # THE FOUNDER STAMP. Set once, here, and never recalculated.
        # When you close the founding window (FOUNDER_WINDOW_OPEN=false in Render),
        # new users get is_founder=False, but everyone already in the table keeps True.
        is_founder=settings.FOUNDER_WINDOW_OPEN,
        last_login=datetime.utcnow(),
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    # Push to Kit so they land on your marketing list too. Fails soft.
    await push_to_kit(user.email, user.name)

    log.info("New signup: %s (founder=%s)", user.email, user.is_founder)

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
