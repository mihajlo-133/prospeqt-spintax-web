# Deployment Guide — Prospeqt Spintax Web

Target: Render (free or starter tier). Single web service, no DB, no Redis.

---

## 1. One-time setup: GitHub repo

```bash
cd /Users/mihajlo/Desktop/prospeqt-spintax-web

git init
git add -A
git status                                  # confirm .env is NOT staged
git commit -m "Initial commit: spintax web + batch API"

# Create the empty repo on GitHub first (private), then:
git remote add origin git@github.com:<you>/prospeqt-spintax-web.git
git branch -M main
git push -u origin main
```

**Verify** before pushing:

```bash
git ls-files | grep -E "\.env$"             # must return NOTHING
```

If `.env` shows up, run `git rm --cached .env && git commit -am "untrack env"`
before you push.

---

## 2. Render service

1. Render dashboard → **New +** → **Web Service**
2. Connect to GitHub → pick `prospeqt-spintax-web`
3. Settings:
   - **Name:** `prospeqt-spintax` (or similar — becomes the URL prefix)
   - **Region:** Frankfurt or Oregon (closer to OpenAI = lower latency)
   - **Branch:** `main`
   - **Runtime:** Python (auto-detected)
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** *(leave blank — Procfile is detected)*
   - **Plan:** Free works for low volume. **Starter ($7/mo)** if you want
     the service to never sleep + faster cold start. **Standard** only
     needed if you regularly run 20+ concurrent o3 jobs.

4. **Environment variables** — under "Environment" tab, add:

   | Key | Value | Notes |
   |---|---|---|
   | `ADMIN_PASSWORD` | choose a strong password | Web UI login |
   | `OPENAI_API_KEY` | `sk-proj-...` | Same key as `.env` |
   | `SESSION_SECRET` | 32+ random bytes | Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
   | `BATCH_API_KEY` | the bearer token | Generate: `python3 -c "import secrets; print('sk_batch_'+secrets.token_urlsafe(32))"` — share with team via 1Password |
   | `DAILY_SPEND_CAP_USD` | `50.0` | Adjust if needed |
   | `OPENAI_MODEL` | `o3` | Default model |

   `PORT` is set by Render automatically — don't set it.

5. **Health check path:** `/health` (already exposed, returns `{"status":"ok"}`)

6. Click **Create Web Service**. First build takes ~3-5 min.

---

## 3. Post-deploy verification

Replace `<your-app>` with your actual Render URL.

```bash
# 1. Health check (should return 200, no auth needed)
curl https://<your-app>.onrender.com/health

# 2. Login page (should return 200 HTML)
curl -I https://<your-app>.onrender.com/login

# 3. Bearer auth on the batch endpoint (should be 401 without token)
curl -X POST https://<your-app>.onrender.com/api/spintax/batch \
  -H "Content-Type: application/json" \
  -d '{"md":"## Segment 1\nEmail 1\nbody","platform":"instantly","dry_run":true}'

# 4. Bearer auth WITH token (should be 200, returns parsed segments after ~30-90s)
TOKEN="<your BATCH_API_KEY>"
curl -X POST https://<your-app>.onrender.com/api/spintax/batch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"md":"## Segment 1\nEmail 1\nSubj: hi\nbody","platform":"instantly","dry_run":true}'
```

---

## 4. Teammate onboarding

Each teammate gets:

1. The web URL → log in with `ADMIN_PASSWORD` for the manual UI
2. The bearer token (`BATCH_API_KEY`) → headless API access via the CLI

Tell them to add to their shell profile:

```bash
export SPINTAX_API_URL="https://<your-app>.onrender.com"
export SPINTAX_API_KEY="sk_batch_..."
```

Then use `scripts/spintax_cli.py` from any directory:

```bash
python3 ~/path/to/spintax_cli.py ~/Downloads/Enavra\ \(2\).md
```

In Claude Code, just say:

> "spintax this file using the Prospeqt API: ~/Downloads/Enavra (2).md"

…and Claude runs the CLI for them.

---

## 5. Common issues

| Symptom | Cause | Fix |
|---|---|---|
| `502 Bad Gateway` on first request | Free tier cold start | Wait 15-30s, retry. Starter tier avoids this. |
| Parse times out | Free tier worker timeout | Check Procfile has `--timeout 600`. Should already be there. |
| `openai_org_not_verified` | Org needs verification for o3-pro | Use `o3` instead until org is verified at platform.openai.com |
| 401 from CLI | Wrong/missing `BATCH_API_KEY` | Verify env var: `echo $SPINTAX_API_KEY` |
| `daily_cap_hit` | Hit `$DAILY_SPEND_CAP_USD` for the UTC day | Bump cap in Render env or wait for midnight UTC reset |

---

## 6. Updating the deployed version

```bash
cd /Users/mihajlo/Desktop/prospeqt-spintax-web
git add -A
git commit -m "your change"
git push origin main
```

Render auto-deploys on push to `main`. Build takes ~2-3 min. The OLD
service stays up until the new one passes the health check (zero-downtime).

---

## 7. Logs

Render dashboard → your service → **Logs** tab. Filter by error level.
Or via Render CLI:

```bash
render logs <service-id> --tail
```

---

## 8. Cost expectations

| Component | Cost |
|---|---|
| Render Free | $0/mo (cold starts after 15min idle) |
| Render Starter | $7/mo (always-on, faster) |
| OpenAI o3 (per Enavra-size batch) | ~$0.90 |
| OpenAI o3 (per HeyReach-size batch ~33 segs) | ~$3.50 (only Email 1 spun) |

Daily cap defaults to $50 — covers ~50 Enavra-size batches or ~14 HeyReach batches before hitting the limit. Bump in Render env if you scale up.
