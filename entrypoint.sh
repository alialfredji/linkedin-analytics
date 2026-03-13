#!/bin/sh
set -e

# Default env vars
DB_PATH="${DB_PATH:-/data/linkedin.db}"
DASHBOARD_PATH="${DASHBOARD_PATH:-/data/dashboard.html}"
COOKIE_PATH="${COOKIE_PATH:-/data/cookies.json}"
CRON_SCHEDULE="${CRON_SCHEDULE:-0 6 * * *}"
PERIOD="${PERIOD:-past_28_days}"

export DB_PATH DASHBOARD_PATH COOKIE_PATH PERIOD

CRON_FILE=/etc/cron.d/linkedin-analytics
{
    printf 'PATH=/usr/local/bin:/usr/bin:/bin\n'
    printf '%s root DB_PATH=%s DASHBOARD_PATH=%s COOKIE_PATH=%s LINKEDIN_USERNAME=%s LINKEDIN_PASSWORD=%s PERIOD=%s /usr/local/bin/python3 /app/extract.py --period %s >> /proc/1/fd/1 2>&1\n' \
        "$CRON_SCHEDULE" \
        "$DB_PATH" \
        "$DASHBOARD_PATH" \
        "$COOKIE_PATH" \
        "${LINKEDIN_USERNAME:-}" \
        "${LINKEDIN_PASSWORD:-}" \
        "${PERIOD}" \
        "${PERIOD}"
} > "$CRON_FILE"
chmod 0644 "$CRON_FILE"

# Run an initial extraction on first startup if DB doesn't exist yet
if [ ! -f "$DB_PATH" ]; then
    echo "[entrypoint] First run — extracting data now..."
    python /app/extract.py --period "${PERIOD}" || echo "[entrypoint] Initial extraction failed (may need auth)"
fi

# Start cron in background
echo "[entrypoint] Starting cron (schedule: $CRON_SCHEDULE)..."
cron

# Serve dashboard on port 8080
echo "[entrypoint] Serving dashboard at http://0.0.0.0:8080"
exec python /app/serve.py
