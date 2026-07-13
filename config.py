import os


class Settings:
    """
    All configuration comes from environment variables set in the Render dashboard.
    Nothing sensitive is ever hardcoded in this file.
    """

    # --- Database ---
    # Render provides this automatically when you attach a Postgres instance.
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # --- JWT / auth ---
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 30  # 30 days ("remember me")
    RESET_TOKEN_EXPIRE_MINUTES: int = 60             # password reset link valid 1 hour

    # --- Founder logic ---
    # Everyone who signs up while this is "true" gets founder pricing locked in.
    # Flip to "false" in Render env vars when you close the founding window.
    FOUNDER_WINDOW_OPEN: bool = os.getenv("FOUNDER_WINDOW_OPEN", "true").lower() == "true"

    # --- Frontend origin (for CORS + password reset links) ---
    # Set this to your live Netlify URL, e.g. https://legendary-pika-5dd723.netlify.app
    # or your custom domain once you have one.
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:8000")

    # --- Kit (email marketing) ---
    # Optional. If both are set, new signups are pushed to your Kit list automatically.
    KIT_API_KEY: str = os.getenv("KIT_API_KEY", "")
    KIT_FORM_ID: str = os.getenv("KIT_FORM_ID", "")

    # --- Resend (transactional email: password resets) ---
    # Optional. If unset, password reset will return the token in the API response
    # instead of emailing it (useful for local testing, NOT safe in production).
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    RESEND_FROM: str = os.getenv("RESEND_FROM", "TORC <onboarding@resend.dev>")


settings = Settings()
