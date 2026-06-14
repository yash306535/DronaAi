# Deployment Guide

DRONA AI deploys as two services: the FastAPI backend on **Render** and the
Vite/React frontend on **Vercel**. No real secrets live in the repository —
every secret is set in the respective provider's dashboard.

## Backend — Render (`backend/render.yaml`)

The Render Blueprint provisions a managed PostgreSQL database (`drona-db`) and a
Python web service (`drona-api`):

- **Build:** `pip install -r requirements.txt`
- **Pre-deploy (migrations):** `alembic upgrade head`
- **Start:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Health check:** `GET /health`

### Backend environment variables

| Key | Source | Notes |
|-----|--------|-------|
| `DATABASE_URL` | `fromDatabase` (auto) | Injected from the managed `drona-db` connection string. PostgreSQL on deploy, SQLite locally. |
| `JWT_SECRET` | dashboard (`sync: false`) | **Required.** Long random HS256 signing secret. |
| `OPENAI_API_KEY` | dashboard (`sync: false`) | **Required.** Stage-2 Vision + LLM generation. |
| `ANTHROPIC_API_KEY` | dashboard (`sync: false`) | Optional alternate LLM provider. |
| `SMTP_PASSWORD` | dashboard (`sync: false`) | Optional Herald email channel. |
| `FRONTEND_ORIGINS` | dashboard (`sync: false`) | Comma-separated CORS allowlist; set to the deployed Vercel origin(s). |
| `ENVIRONMENT` | blueprint value | `production` on Render. |
| `JWT_ALGORITHM` | blueprint value | `HS256`. |
| `ACCESS_TOKEN_TTL_MINUTES` | blueprint value | `15`. |
| `REFRESH_TOKEN_TTL_DAYS` | blueprint value | `7`. |
| `MAX_BODY_BYTES` | blueprint value | `1048576` (1 MB). |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` | dashboard / value | Optional email channel. |
| `PYTHON_VERSION` | blueprint value | Pinned to `3.11.9`. |

Required secrets (`JWT_SECRET`, `OPENAI_API_KEY`) must be set before the first
deploy — the app aborts startup naming any missing key (Requirement 15.1/15.2).
See `backend/.env.example` for the full local key list.

### Database migrations

Migrations are managed with Alembic (`backend/alembic.ini`, `backend/alembic/`).
`alembic/env.py` resolves the database URL and `target_metadata` from the app
itself (`app.core.config` + `app.core.db.Base`), so migrations always match the
ORM models.

```bash
cd backend
alembic upgrade head        # apply all migrations (run automatically on deploy)
alembic revision --autogenerate -m "describe change"   # create a new migration
```

## Frontend — Vercel (`frontend/vercel.json`)

- **Framework:** Vite
- **Build:** `npm run build`
- **Output:** `dist`
- **SPA routing:** all routes rewrite to `/index.html` so client-side routing
  (React Router) works on deep links and refreshes.

### Frontend environment variables

| Key | Notes |
|-----|-------|
| `VITE_API_BASE_URL` | Base URL of the deployed Render backend API, including the `/api/v1` prefix, e.g. `https://drona-api.onrender.com/api/v1` (defaults to `http://localhost:8000/api/v1` in dev). Vite inlines `VITE_`-prefixed vars at build time, so set it in the Vercel project settings before building. |

After deploying the frontend, add its origin to the backend's
`FRONTEND_ORIGINS` so CORS permits the SPA.
