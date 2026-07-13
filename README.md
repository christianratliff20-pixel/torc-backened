# TORC API

FastAPI backend for TORC. Handles accounts, auth, and founder pricing locks.

## Environment variables (set these in Render)

| Variable | Required | What it does |
|---|---|---|
| `DATABASE_URL` | yes | Auto-set by Render when you attach Postgres |
| `SECRET_KEY` | yes | Signs JWTs. Generate a long random string. |
| `FRONTEND_URL` | yes | Your Netlify URL, no trailing slash. Required for CORS. |
| `FOUNDER_WINDOW_OPEN` | no | `true` (default) = new signups get founder pricing. Flip to `false` to close it. |
| `KIT_API_KEY` | no | Pushes new signups to your Kit list |
| `KIT_FORM_ID` | no | Which Kit form to add them to |
| `RESEND_API_KEY` | no | Sends password-reset emails. Without it, resets return the token in the response (testing only). |
| `RESEND_FROM` | no | e.g. `TORC <hello@yourdomain.com>` |

## Endpoints

- `POST /auth/check-email` — does this email have an account? (drives login-vs-signup UI)
- `POST /auth/signup` — create account, stamp founder status
- `POST /auth/login` — returns JWT
- `POST /auth/forgot-password` — emails a reset link
- `POST /auth/reset-password` — consumes the token, sets new password
- `GET /me` — current account (requires Bearer token)
- `PATCH /me` — update name
- `POST /me/change-password` — change password while logged in
- `DELETE /me` — delete account

## Founder pricing

`is_founder` is stamped **once at signup** and never recalculated. When you raise
public prices later, founders keep their rate because the flag lives on their row.
