# Backend Deployment Guide

## Overview

The FastAPI backend needs to be deployed to a long-running service to handle market data analysis, signals, and pipeline requests from the Vercel frontend.

## Architecture

```
Vercel Frontend → PYTHON_BACKEND_URL → FastAPI Backend (Railway/Render/Docker)
                                         ↓
                                      Supabase (Auth + DB)
                                         ↓
                                      Upstox (Market Data)
```

## Deployment Options

### Option 1: Railway (Recommended)

1. Create a new project on [Railway](https://railway.app)
2. Connect this GitHub repository
3. Set the root directory to `/`
4. Railway will auto-detect the `Dockerfile` and `railway.json`

### Option 2: Render

1. Create a new Web Service on [Render](https://render.com)
2. Connect this GitHub repository
3. Set the Dockerfile path to `Dockerfile`
4. Set the start command to `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`

### Option 3: Docker (Any Platform)

```bash
docker build -t trading-system-backend .
docker run -p 8000:8000 --env-file backend/.env trading-system-backend
```

## Required Environment Variables

The following environment variables must be set on the backend host:

| Variable | Description | Required |
|----------|-------------|----------|
| `SUPABASE_URL` | Supabase project URL | Yes |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key | Yes |
| `SUPABASE_JWT_SECRET` | Supabase JWT secret | Yes |
| `UPSTOX_TOKEN_ENCRYPTION_KEY` | AES-256 key for token encryption | Yes |
| `UPSTOX_CLIENT_ID` | Upstox client ID (market data account) | For live pipeline |
| `UPSTOX_SERVICE_ACCOUNT_TOKEN` | Upstox access token (market data account) | For live pipeline |
| `ENVIRONMENT` | `production` or `development` | Yes |
| `LOG_LEVEL` | Logging level (default: INFO) | No |
| `LIVE_PIPELINE_ENABLED` | Enable live WebSocket pipeline | No |
| `SIGNAL_UNIVERSE` | Comma-separated list of symbols to analyze | No |

## Post-Deployment

1. Verify the backend is running: `GET /health`
2. Set `PYTHON_BACKEND_URL` in Vercel to the backend URL
3. Trigger a new Vercel deployment

## Vercel Configuration

After deploying the backend, set the following environment variable in Vercel:

| Variable | Value |
|----------|-------|
| `PYTHON_BACKEND_URL` | `https://your-backend-url.railway.app` |

This must be set in **Production** and **Preview** environments.

## Health Check

The backend exposes a health check endpoint at `GET /health` that returns:

```json
{
  "status": "ok",
  "service": "trading-system-backend",
  "environment": "production"
}
```
