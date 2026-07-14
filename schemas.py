from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


# ---------- Requests ----------

class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: Optional[str] = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class CheckEmailRequest(BaseModel):
    """Used by the frontend to decide whether to show 'log in' or 'sign up'."""
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class VerifyEmailRequest(BaseModel):
    token: str


class ResendVerifyRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)


# ---------- Responses ----------

class UserOut(BaseModel):
    id: int
    email: EmailStr
    name: Optional[str]
    is_founder: bool          # stamped at signup
    email_verified: bool      # clicked the link?
    created_at: datetime

    @property
    def founder_active(self) -> bool:
        """Founder pricing only counts once the email is verified."""
        return self.is_founder and self.email_verified

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class CheckEmailResponse(BaseModel):
    exists: bool


class MessageResponse(BaseModel):
    message: str
    # Only populated when RESEND_API_KEY is not configured (local/testing).
    debug_reset_token: Optional[str] = None


# ---------- Admin ----------

class AdminUserOut(BaseModel):
    id: int
    email: EmailStr
    name: Optional[str]
    is_founder: bool
    email_verified: bool
    created_at: datetime
    last_login: Optional[datetime]

    class Config:
        from_attributes = True


class AdminStatsOut(BaseModel):
    total_users: int
    founders: int              # verified founders — the real number
    unverified: int            # signed up, never clicked the link
    non_founders: int
    signups_today: int
    signups_this_week: int
    founder_window_open: bool
    users: list[AdminUserOut]
