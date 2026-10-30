#!/usr/bin/env bash
# Verify the deployment, then install (or print) the daily cron entry.
#
#   scripts/install_cron.sh           # verify + install into the current user's crontab
#   scripts/install_cron.sh --print   # verify + print the entry only
#
# The schedule, in IST (Asia/Kolkata):
#
#   Yesterday   once a day   14:00   run.py yesterday
#
# 14:00 IST is after the US day has ended (12:30 IST, or 13:30 IST in US winter
# time), so the run loads the complete previous US day.
#
# Cron reads the schedule on the SERVER's clock, so the IST time is converted
# below (14:00 IST = 0 14 * * * on an IST server, 30 8 * * * on a UTC server).
# Every path in the entry is derived on this server -- nothing is assumed.
# Re-running replaces the previous entry (matched by its marker comment);
# other jobs are untouched.
#
# Overrides:  CRON_YESTERDAY_TIME="14:00"   CRON_TIMEZONE=Asia/Kolkata

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
WRAPPER="$PROJECT_DIR/scripts/run_products_report.sh"
ENV_FILE="$PROJECT_DIR/.env"
SESSION_DIR="$PROJECT_DIR/data/session"
CRON_LOG="$PROJECT_DIR/logs/products_cron.log"
MARKER_PREFIX="# products-report:"
MARKER_YESTERDAY="$MARKER_PREFIX Products page (Yesterday) daily at 14:00 IST -> powerbidb.Products"

CRON_TIMEZONE="${CRON_TIMEZONE:-Asia/Kolkata}"
YESTERDAY_TIME="${CRON_YESTERDAY_TIME:-14:00}"

fail() { echo "ERROR: $*" >&2; exit 1; }
ok() { echo "  ok  $*"; }

# UTC offset in minutes of a timezone ("" = the server's own), e.g. +0530 -> 330.
offset_minutes() {
    local z
    if [[ -n "$1" ]]; then z="$(TZ="$1" date +%z)"; else z="$(date +%z)"; fi
    local total=$(( 10#${z:1:2} * 60 + 10#${z:3:2} ))
    [[ "${z:0:1}" == "-" ]] && total=$(( -total ))
    echo "$total"
}

# "H M" in CRON_TIMEZONE -> "M H * * *" on this server's clock.
schedule_for() {
    local shift=$(( $(offset_minutes "") - $(offset_minutes "$CRON_TIMEZONE") ))
    local total=$(( ((10#$1 * 60 + 10#$2 + shift) % 1440 + 1440) % 1440 ))
    echo "$(( total % 60 )) $(( total / 60 )) * * *"
}

[[ -f "/usr/share/zoneinfo/$CRON_TIMEZONE" ]] || fail "unknown timezone $CRON_TIMEZONE (install tzdata)"
[[ "$YESTERDAY_TIME" =~ ^([0-9]{1,2}):([0-9]{2})$ ]] || fail "CRON_YESTERDAY_TIME must be HH:MM, got '$YESTERDAY_TIME'"
SCHEDULE_YESTERDAY="$(schedule_for "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}")"

echo "Verifying deployment in $PROJECT_DIR"
[[ -f "$PROJECT_DIR/run.py" ]] || fail "entry point missing: $PROJECT_DIR/run.py"
ok "entry point $PROJECT_DIR/run.py"
[[ -x "$PYTHON" ]] || fail "virtualenv interpreter missing: $PYTHON (create it: uv sync --no-dev)"
"$PYTHON" -c "import playwright, pandas, sqlalchemy, mysql.connector, dotenv" \
    || fail "dependencies not installed in $PYTHON"
ok "interpreter $PYTHON ($("$PYTHON" --version 2>&1))"
[[ -x "$WRAPPER" ]] || fail "wrapper not executable: chmod +x $WRAPPER"
ok "wrapper $WRAPPER"
[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE (cp .env.example .env && chmod 600 .env)"
[[ "$(stat -c %a "$ENV_FILE")" == "600" ]] || fail "$ENV_FILE must be mode 600: chmod 600 $ENV_FILE"
ok "env file $ENV_FILE (mode 600)"
command -v flock >/dev/null || fail "flock not found (install util-linux)"
ok "flock $(command -v flock)"
(cd "$PROJECT_DIR" && "$PYTHON" -c "from app.config import load_settings; load_settings('$ENV_FILE')") \
    || fail "configuration check failed (see the message above)"
ok "configuration"
[[ -n "$(ls -A "$SESSION_DIR" 2>/dev/null)" ]] \
    || fail "no saved Amazon session in $SESSION_DIR -- run once: $PYTHON $PROJECT_DIR/run.py login"
ok "saved Amazon session $SESSION_DIR"
ok "server clock: $(date +%Z) (UTC$(date +%z)); schedule time is $CRON_TIMEZONE"
ok "Yesterday daily at $YESTERDAY_TIME $CRON_TIMEZONE = '$SCHEDULE_YESTERDAY' on this server"
if [[ "$(date -d 2026-01-15 +%z)" != "$(date -d 2026-07-15 +%z)" ]]; then
    echo "  WARNING this server's clock changes for daylight saving; the converted hour is only right"
    echo "          for the current season. Prefer a UTC or IST server, or re-run this script after each change."
fi

ENTRY_YESTERDAY="$SCHEDULE_YESTERDAY $WRAPPER yesterday >> $CRON_LOG 2>&1"

echo
echo "Cron entry:"
echo "  $MARKER_YESTERDAY"
echo "  $ENTRY_YESTERDAY"

if [[ "${1:-}" == "--print" ]]; then
    exit 0
fi

current="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$current" | awk -v p="$MARKER_PREFIX" '
    index($0, p) == 1 { skip = 1; next }
    skip == 1 { skip = 0; next }
    { print }')"
printf '%s\n%s\n%s\n' "$filtered" "$MARKER_YESTERDAY" "$ENTRY_YESTERDAY" \
    | sed '/^$/N;/^\n$/D' | crontab -
echo
echo "Installed for user $(id -un). Inspect with: crontab -l"
