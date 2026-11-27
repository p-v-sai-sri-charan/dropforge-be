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
(one file per weekday, so the last 7 days are kept), and the `offsite` service copies each dump to R2 (`backups/` in
`R2_BUCKET`, kept `BACKUP_KEEP_DAYS`, default 30) once the R2 variables are set.

Restore: download a dump from R2, then `docker compose exec -T mongo mongorestore --archive --gzip --drop < dropforge-YYYY-MM-DD.archive.gz`.

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
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | Cloudflare R2 image storage + off-site backups (see below) |
| `R2_PUBLIC_BUCKET`, `R2_PUBLIC_URL` | Optional: public bucket + its custom domain, so shop photos load straight from Cloudflare |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | Older image storage. Needed until existing files are moved to R2 |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Payments (enable international payments and AED in Razorpay) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_VERIFY_SERVICE` | Phone OTP (service SID starts with `VA`) |
| `RATE_LIMIT_*` | Optional per-IP, per-minute limits |

## Image storage (Cloudflare R2)

1. In Cloudflare: R2 → create two buckets, `dropforge-private` (leave public access off) and `dropforge-public`.
   On `dropforge-public`, Settings → Custom domains → connect `cdn.dropforge.in`.
2. R2 → Manage API tokens → create a token with Object Read & Write on both buckets. Put the account ID, key ID and
   secret plus the bucket names and `R2_PUBLIC_URL` in `.env`, then `docker compose up -d --build`.
   New uploads now go to R2. Private artwork is only ever served through the API (owner / admin checks).
3. Move existing Cloudinary files (links stay the same):
   `docker compose exec api python scripts/migrate_to_r2.py --dry-run`, then without `--dry-run`.
   After checking the site, `--delete-source` removes the Cloudinary copies; then the Cloudinary variables can go.

Thumbnails: `/api/files/<path>?w=480` returns a WEBP no wider than 240 / 480 / 960 px, made once and kept in R2.

## Local development

```bash
pip install -r requirements-dev.txt
uvicorn server:app --reload --port 8001
```

## Promotion tracking & push notifications

- `POST /api/track` - anonymous funnel events from the app/website (visit, product_view, design_started, checkout_started)
  with the visitor's campaign (UTM tags). Events expire after ~13 months. Orders store `attribution` (campaign that brought
  the customer); `GET /api/admin/marketing/campaigns?days=30` reports visitors, funnel steps, paid orders and revenue
  per source/campaign.
- `POST /api/push/register|unregister`, `GET/PUT /api/push/prefs` - app devices (Expo push tokens) and the customer's
  "offers" opt-out. Order status changes, confirmations and refunds notify the customer automatically (`push.py`).
- `POST /api/admin/push/broadcast` - promotion to every app user who hasn't opted out (`dry_run: true` counts devices).
- `EXPO_ACCESS_TOKEN` (optional) - only if "enhanced push security" is on in the Expo project.
