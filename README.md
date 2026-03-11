# LinkedIn Analytics

Extracts your LinkedIn post analytics and serves them as a self-hosted dashboard. Runs on a schedule via cron, stores data in SQLite, and serves a static HTML dashboard on port 8080.

---

## Architecture

```
LinkedIn → Playwright (headless Chromium) → SQLite → HTML Dashboard
                                                         ↑
                                                    HTTP :8080
```

The container logs in to LinkedIn using your credentials, saves session cookies, scrapes post analytics, writes to `linkedin.db`, generates a `dashboard.html`, and refreshes daily via cron.

---

## Local Development

### 1. Clone and set up env

```bash
git clone https://github.com/alialfredji/linkedin-analytics
cd linkedin-analytics
cp .env.example .env
# Fill in your LinkedIn credentials and adjust CRON_SCHEDULE if needed
```

### 2. Build and run

```bash
docker compose up --build
```

The dashboard will be available at `http://localhost:8080`.

### 3. Manual extraction (skip cron)

```bash
docker compose exec linkedin-analytics python /app/extract.py
```

### 4. Stop

```bash
docker compose down
```

---

## Authentication

LinkedIn uses cookie-based sessions. On first run:

1. The container logs in with `LINKEDIN_USERNAME` + `LINKEDIN_PASSWORD`
2. Session cookies are saved to `COOKIE_PATH` (default: `/data/cookies.json`)
3. All subsequent runs reuse the cookies — credentials are only used to refresh an expired session

If LinkedIn presents a challenge (CAPTCHA, 2FA), the extraction will fail gracefully. Re-run after completing the challenge manually in a real browser (this refreshes the session on LinkedIn's side).

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `LINKEDIN_USERNAME` | ✅ | — | Your LinkedIn email |
| `LINKEDIN_PASSWORD` | ✅ | — | Your LinkedIn password |
| `DB_PATH` | | `/data/linkedin.db` | SQLite database path |
| `DASHBOARD_PATH` | | `/data/dashboard.html` | Generated dashboard path |
| `COOKIE_PATH` | | `/data/cookies.json` | Saved session cookies path |
| `CRON_SCHEDULE` | | `0 6 * * *` | Cron expression for daily run |
| `PERIOD` | | `past_28_days` | Analytics window (`past_7_days`, `past_14_days`, `past_28_days`, `past_90_days`, `past_365_days`) |

---

## Self-Hosting

The container is self-contained and runs on any server with Docker. Minimal example with a reverse proxy:

```yaml
services:
  linkedin-analytics:
    image: ghcr.io/alialfredji/linkedin-analytics:latest
    env_file: .env
    volumes:
      - linkedin_data:/data

  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data

volumes:
  linkedin_data:
  caddy_data:
```

```
# Caddyfile
your-domain.com {
    reverse_proxy linkedin-analytics:8080
}
```

The image is private (`ghcr.io`). Authenticate with a GitHub PAT that has `read:packages` scope:

```bash
echo YOUR_GITHUB_PAT | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

---

## CI/CD

The GitHub Actions workflow at `.github/workflows/deploy.yml` builds and pushes the Docker image to `ghcr.io` on every push to `main`.

Required repository secret: `GHCR_TOKEN` — a GitHub PAT with `write:packages` scope.

---

## Data

All persistent data lives in the `/data` volume:

| File | Description |
|---|---|
| `linkedin.db` | SQLite database with all extracted post analytics |
| `dashboard.html` | Generated static dashboard (regenerated on each run) |
| `cookies.json` | LinkedIn session cookies |

---

## Troubleshooting

**First extraction never ran / `dashboard.html` missing**

Trigger it manually:
```bash
docker compose exec linkedin-analytics python /app/extract.py
```

**Authentication failed**

Log in to LinkedIn in a real browser from the same network, complete any challenges, then re-run extraction. The cookies file may need to be deleted first:
```bash
docker compose exec linkedin-analytics rm /data/cookies.json
docker compose exec linkedin-analytics python /app/extract.py
```

**Container exits immediately**

Check logs:
```bash
docker compose logs linkedin-analytics
```
