"""
Outbound integrations. Both are OPTIONAL and fail soft:
if the API keys aren't set, signup and password reset still work —
they just skip the external call instead of crashing.
"""
import logging

import httpx

from config import settings

log = logging.getLogger("torc.integrations")


async def push_to_kit(email: str, name: str | None = None) -> None:
    """
    Adds a new signup to your Kit list so you can email them later.
    Silently no-ops if KIT_API_KEY / KIT_FORM_ID aren't configured.
    """
    if not settings.KIT_API_KEY or not settings.KIT_FORM_ID:
        log.info("Kit not configured — skipping list push for %s", email)
        return

    url = f"https://api.convertkit.com/v3/forms/{settings.KIT_FORM_ID}/subscribe"
    payload = {"api_key": settings.KIT_API_KEY, "email": email}
    if name:
        payload["first_name"] = name

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                log.warning("Kit push failed (%s): %s", r.status_code, r.text)
    except Exception as e:
        # Never let a marketing-list failure break a real signup.
        log.warning("Kit push errored for %s: %s", email, e)


async def send_verify_email(email: str, verify_token: str) -> bool:
    """
    Sends the 'confirm your email' link. Until they click it, the account
    exists but does NOT count as a founder.
    Returns False if Resend isn't configured.
    """
    if not settings.RESEND_API_KEY:
        log.info("Resend not configured — verification email not sent for %s", email)
        return False

    link = f"{settings.FRONTEND_URL}?verify_token={verify_token}"

    html = f"""
    <div style="font-family:Inter,Helvetica,Arial,sans-serif;background:#0B0C0E;color:#ECEDEF;padding:40px 24px;">
      <h1 style="font-size:22px;margin:0 0 16px;letter-spacing:0.05em;">TORC</h1>
      <p style="color:#ECEDEF;font-size:17px;line-height:1.5;margin:0 0 14px;">
        Confirm your email to lock in founding pricing.
      </p>
      <p style="color:#9AA0AA;line-height:1.6;margin:0 0 24px;font-size:14px;">
        Your account is created, but founding pricing isn't locked until you confirm this
        address. One click and it's yours permanently — even after prices go up.
      </p>
      <a href="{link}"
         style="display:inline-block;background:#F5A623;color:#0B0C0E;text-decoration:none;
                font-weight:700;padding:14px 26px;border-radius:8px;">
        Confirm &amp; lock my rate
      </a>
      <p style="color:#666B74;font-size:13px;margin:24px 0 0;">
        This link expires in 48 hours. If you didn't sign up for TORC, ignore this email.
      </p>
    </div>
    """

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                json={
                    "from": settings.RESEND_FROM,
                    "to": [email],
                    "subject": "Confirm your email — lock your TORC founding rate",
                    "html": html,
                },
            )
            if r.status_code >= 400:
                log.warning("Verify email failed (%s): %s", r.status_code, r.text)
                return False
            return True
    except Exception as e:
        log.warning("Verify email errored for %s: %s", email, e)
        return False


async def send_reset_email(email: str, reset_token: str) -> bool:
    """
    Sends the password-reset link via Resend.
    Returns True if it was actually sent, False if Resend isn't configured.
    """
    if not settings.RESEND_API_KEY:
        log.info("Resend not configured — reset email not sent for %s", email)
        return False

    reset_link = f"{settings.FRONTEND_URL}?reset_token={reset_token}"

    html = f"""
    <div style="font-family:Inter,Helvetica,Arial,sans-serif;background:#0B0C0E;color:#ECEDEF;padding:40px 24px;">
      <h1 style="font-size:22px;margin:0 0 16px;letter-spacing:0.02em;">TORC</h1>
      <p style="color:#9AA0AA;line-height:1.6;margin:0 0 20px;">
        Someone asked to reset the password on this account. If that wasn't you, ignore this email —
        nothing changes until the link below is used.
      </p>
      <a href="{reset_link}"
         style="display:inline-block;background:#F5A623;color:#0B0C0E;text-decoration:none;
                font-weight:700;padding:14px 26px;border-radius:8px;">
        Reset your password
      </a>
      <p style="color:#666B74;font-size:13px;margin:24px 0 0;">
        This link expires in 1 hour.
      </p>
    </div>
    """

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                json={
                    "from": settings.RESEND_FROM,
                    "to": [email],
                    "subject": "Reset your TORC password",
                    "html": html,
                },
            )
            if r.status_code >= 400:
                log.warning("Resend failed (%s): %s", r.status_code, r.text)
                return False
            return True
    except Exception as e:
        log.warning("Resend errored for %s: %s", email, e)
        return False
