# LinkedIn Analytics

Self-hosted LinkedIn analytics tool. Extracts follower stats, post performance, and audience demographics from LinkedIn using Playwright. Stores data in SQLite and serves a static HTML dashboard.

Runs on your Hetzner server as a Docker container. Extracts data on a daily schedule. Accessible via HTTPS at your own domain.

---

## Architecture

```
GitHub push → GitHub Actions → GHCR image
                                    ↓
                         Hetzner server (Docker Compose)
                         ├── linkedin-analytics (this)
                         │   ├── cron: daily LinkedIn extraction
                         │   ├── SQLite: /data/linkedin.db
                         │   └── HTTP dashboard: :8080
                         └── Caddy (reverse proxy + TLS)
                             └── linkedin.alialfredji.com → :8080
```

---

## Local Development

### Prerequisites

- Docker + Docker Compose
- LinkedIn credentials

### Setup

```bash
# Clone the repo
git clone https://github.com/alialfredji/linkedin-analytics.git
cd linkedin-analytics

# Create your local .env
cp .env.example .env
# Edit .env with your LinkedIn credentials
```

`.env` contents:
```env
LINKEDIN_USERNAME=your@email.com
LINKEDIN_PASSWORD=yourpassword
DB_PATH=/data/linkedin.db
DASHBOARD_PATH=/data/dashboard.html
COOKIE_PATH=/data/cookies.json
CRON_SCHEDULE=0 6 * * *
```

### Run locally

```bash
# Start the container
docker compose up

# Container will:
# 1. Attempt initial data extraction on first run
# 2. Schedule daily cron job
# 3. Serve dashboard at http://localhost:8080
```

Open http://localhost:8080 to view the dashboard.

### Manual extraction

```bash
# Trigger extraction inside the running container
docker compose exec linkedin-analytics python /app/extract.py

# Dry run (shows plan without scraping)
docker compose exec linkedin-analytics python /app/extract.py --dry-run

# Extract specific metrics only
docker compose exec linkedin-analytics python /app/extract.py --metrics followers,posts
```

### Stop

```bash
docker compose down
```

Data persists in `./data/` between restarts.

---

## Authentication

The tool authenticates with LinkedIn in this order:

1. **Cookie file** — If `/data/cookies.json` exists, restores the session and validates it by navigating to the feed. If valid, proceeds without login.
2. **Credential login** — If cookies are missing or expired, logs in with `LINKEDIN_USERNAME` + `LINKEDIN_PASSWORD`. Saves cookies to `/data/cookies.json` on success.
3. **Security challenge** — If LinkedIn raises a checkpoint/challenge after login, the extraction fails gracefully with a message. Manual intervention required (log in via browser, export cookies).

Cookies are refreshed automatically on each successful login.

### Cookie expiry

LinkedIn sessions last several weeks. The daily cron job will naturally refresh cookies on each successful run. If you get repeated auth failures, it means LinkedIn requires re-verification — log in manually via browser and update the `cookies.json` in the data volume.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `LINKEDIN_USERNAME` | Yes | — | LinkedIn email address |
| `LINKEDIN_PASSWORD` | Yes | — | LinkedIn password |
| `DB_PATH` | No | `/data/linkedin.db` | SQLite database path |
| `DASHBOARD_PATH` | No | `/data/dashboard.html` | Generated dashboard HTML path |
| `COOKIE_PATH` | No | `/data/cookies.json` | Persisted session cookies path |
| `CRON_SCHEDULE` | No | `0 6 * * *` | Cron expression for daily extraction |

---

## Deployment (Hetzner + n8n-setup)

This tool is designed to run alongside n8n on the same Hetzner server using [alialfredji/n8n-setup](https://github.com/alialfredji/n8n-setup).

### Prerequisites

- n8n-setup is already deployed and running
- Cloudflare DNS managing your domain
- Docker registry credentials for GHCR

### Step 1 — Build & push the image

On every push to `main`, GitHub Actions builds and pushes the image to GHCR:

```
ghcr.io/alialfredji/linkedin-analytics:latest
ghcr.io/alialfredji/linkedin-analytics:sha-<commit>
```

The workflow file is at `.github/workflows/build-push.yml`. It requires the `GITHUB_TOKEN` secret (automatic) and `packages: write` permission (already set).

To push manually:
```bash
docker build -t ghcr.io/alialfredji/linkedin-analytics:latest .
echo $GHCR_TOKEN | docker login ghcr.io -u alialfredji --password-stdin
docker push ghcr.io/alialfredji/linkedin-analytics:latest
```

### Step 2 — Update n8n-setup

The n8n-setup repo contains the server-side configuration. Apply these changes:

**`deployment/docker-compose.yml`** — Add the linkedin-analytics service:
```yaml
linkedin-analytics:
  image: ghcr.io/alialfredji/linkedin-analytics:latest
  env_file: .env
  volumes:
    - linkedin_data:/data
  networks:
    - app-network
  restart: unless-stopped

volumes:
  linkedin_data:
```

**`deployment/Caddyfile`** — Add the subdomain block:
```
linkedin.alialfredji.com {
  reverse_proxy linkedin-analytics:8080
}
```

**`deployment/.env`** — Add credentials:
```env
LINKEDIN_USERNAME=your@email.com
LINKEDIN_PASSWORD=yourpassword
CRON_SCHEDULE=0 6 * * *
```

**`terraform/main.tf`** — Add DNS record:
```hcl
resource "cloudflare_dns_record" "linkedin_analytics" {
  zone_id = var.cloudflare_zone_id
  name    = "linkedin"
  type    = "A"
  content = hcloud_server.n8n.ipv4_address
  ttl     = 1
  proxied = false
}
```

### Step 3 — Apply Terraform (DNS)

```bash
cd terraform
terraform plan
terraform apply
```

### Step 4 — Deploy on server

SSH into the Hetzner server:

```bash
# Log into GHCR
echo $GHCR_TOKEN | docker login ghcr.io -u alialfredji --password-stdin

# Pull the new image
docker compose pull linkedin-analytics

# Start the service
docker compose up -d linkedin-analytics

# Verify
docker compose ps
docker compose logs linkedin-analytics
```

Caddy will automatically provision a Let's Encrypt certificate for `linkedin.alialfredji.com`.

### Step 5 — Verify

```bash
# Check HTTPS
curl https://linkedin.alialfredji.com

# Check container is healthy
docker compose ps linkedin-analytics

# Trigger a manual extraction
docker compose exec linkedin-analytics python /app/extract.py
```

---

## CI/CD

GitHub Actions workflow (`.github/workflows/build-push.yml`):

- **Trigger**: push to `main`
- **Registry**: GHCR (`ghcr.io/alialfredji/linkedin-analytics`)
- **Tags**: `latest` + `sha-<commit>`
- **Cache**: GitHub Actions cache for faster builds

To deploy a new version after pushing to `main`:
1. Wait for the GitHub Actions build to complete
2. On the server: `docker compose pull linkedin-analytics && docker compose up -d linkedin-analytics`

---

## Data

SQLite database at `/data/linkedin.db`. Tables include:

- Follower counts over time
- Post impressions, reactions, comments, shares
- Audience demographics (country, role, industry)

The dashboard is regenerated after each extraction at `/data/dashboard.html` and served statically on port 8080.

---

## Troubleshooting

**Container exits immediately**
```bash
docker compose logs linkedin-analytics
```
Usually a missing env var or auth failure on first run.

**Auth keeps failing**
LinkedIn may have flagged the account. Log in via browser manually, then export cookies using a browser extension (e.g., EditThisCookie) and place the JSON at the `COOKIE_PATH` location on the server.

**Dashboard not updating**
Check the cron is running inside the container:
```bash
docker compose exec linkedin-analytics crontab -l
docker compose exec linkedin-analytics cat /proc/1/fd/1
```

**Port 8080 conflict locally**
Change the port in `docker-compose.yml`:
```yaml
ports:
  - "9090:8080"
```
