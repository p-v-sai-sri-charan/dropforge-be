# DropForge API

FastAPI + MongoDB + Redis backend for the DropForge app (frontend repo: Dropforge-fe).

## Run with Docker

```bash
cp .env.example .env     # fill in secrets
docker compose up -d --build
curl http://127.0.0.1:5001/api/health
```

The API listens on `127.0.0.1:5001`; put your reverse proxy (Nginx) in front of it for HTTPS. The proxy must pass
`X-Forwarded-For` (`proxy_set_header X-Forwarded-For $remote_addr;`) because rate limiting and country detection use the client IP.
Mongo and Redis are only reachable inside the compose network. A `backup` service writes a daily `mongodump` to `./backups`
(one file per weekday, so the last 7 days are kept); copy that folder off the server too.

## Environment

| Variable | Purpose |
| --- | --- |
| `JWT_SECRET` | Auth secret (required) |
| `DB_NAME`, `MONGO_URL`, `REDIS_URL` | Set by docker-compose; override for local runs |
| `CORS_ORIGINS` | Comma-separated allowed web origins (localhost is always allowed) |
| `ADMIN_PHONES` | Comma-separated phones that get admin access after OTP login |
| `SUPPORT_CONTACT` | Support email / phone shown to customers (custom design orders can only be changed through support) |
| `ANTHROPIC_API_KEY` | Claude: AI ideas, vector graphics, safety checks |
| `OPENAI_API_KEY` | AI PNG images (gpt-image) |
| `OPENAI_IMAGE_MODEL`, `OPENAI_IMAGE_QUALITY`, `AI_FREE_IMAGES_PER_MONTH` | Optional image settings (defaults `gpt-image-1`, `medium`, `3`) |
| `KEY_ENCRYPTION_SECRET` | Encrypts creators' saved OpenAI keys (defaults to `JWT_SECRET`) |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | Image storage (required) |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Payments (enable international payments and AED in Razorpay) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_VERIFY_SERVICE` | Phone OTP (service SID starts with `VA`) |
| `RATE_LIMIT_*` | Optional per-IP, per-minute limits |

## Local development

```bash
pip install -r requirements-dev.txt
uvicorn server:app --reload --port 8001
```
