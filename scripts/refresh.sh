#!/usr/bin/env bash
# Daily data refresh for the SLS Mini App, driven by launchd.
#   scrape all chains -> regenerate static JSON -> deploy Worker
#
# Run by ~/Library/LaunchAgents/com.sls.refresh.plist. Logs to
# ~/Library/Logs/sls-refresh.log; notifies on failure.
#
# Sleep handling covers three cases at the scheduled time:
#   1. asleep, lid closed, on battery -> pmset wakes it (see DEPLOY/README),
#      and `pmset disablesleep 1` keeps it awake despite the closed lid for the
#      whole run (caffeinate alone CANNOT override a lid-close event).
#   2. lid closed, run finished -> disablesleep is ALWAYS restored to 0 via a
#      trap, so the Mac can sleep normally again (even if the scrape crashes).
#   3. lid open, on battery, in use -> caffeinate -i -s stops idle/system sleep
#      if I step away mid-scrape; no wake needed.
set -uo pipefail
cd "$(dirname "$0")/.."

# launchd starts with a bare PATH (no nvm). wrangler is invoked via `npx`, so
# node must be reachable; caffeinate/osascript/pmset live in /usr/bin.
export PATH="$HOME/.nvm/versions/node/v22.17.0/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LOG="$HOME/Library/Logs/sls-refresh.log"
exec >>"$LOG" 2>&1
echo "===== $(date '+%F %T') start ====="

# Disable lid-close sleep for the duration, then ALWAYS restore it — on success,
# on failure, and on SIGINT/SIGTERM. Needs a NOPASSWD sudoers rule scoped to
# exactly these two pmset calls (scripts/sls-pmset.sudoers).
restore_sleep() {
  sudo /usr/bin/pmset -a disablesleep 0 \
    && echo "$(date '+%T') disablesleep -> 0 (sleep restored)" \
    || echo "$(date '+%T') WARN: failed to restore disablesleep"
}
trap restore_sleep EXIT INT TERM

if sudo /usr/bin/pmset -a disablesleep 1; then
  echo "$(date '+%T') disablesleep -> 1 (lid-close sleep off for the run)"
else
  # Not fatal: lid-open / on-AC runs still work via caffeinate below.
  echo "$(date '+%T') WARN: could not set disablesleep (sudoers rule missing?)"
fi

# -i prevent idle sleep, -s prevent system sleep — covers the lid-open cases.
caffeinate -i -s bash -c '
  set -o pipefail
  ./.venv/bin/python run_scraper.py || { echo "SCRAPE FAILED (rc=$?)"; exit 10; }
  ./deploy.sh                       || { echo "DEPLOY FAILED (rc=$?)"; exit 20; }
  # промо-банери топ-знижок → best-sales-images/<дата>/ (локальний контент).
  # Не фатально: scrape+deploy уже успішні, биті банери не валять прогін.
  ./.venv/bin/python promo.py       || echo "PROMO BANNERS FAILED (rc=$?) — non-fatal"
'
rc=$?

if [ "$rc" -ne 0 ]; then
  osascript -e 'display notification "Refresh failed — see ~/Library/Logs/sls-refresh.log" with title "SLS"' || true
  echo "===== $(date '+%F %T') FAIL rc=$rc ====="
else
  echo "===== $(date '+%F %T') OK ====="
fi
exit "$rc"
