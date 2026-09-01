#!/bin/bash
#
# Install (or reinstall) the nightly ingestion as a launchd job.
#
#   bash scripts/install_launchd.sh          # install and load
#   bash scripts/install_launchd.sh --remove # unload and delete
#
# Generates the plist from wherever this repo actually lives rather than
# shipping a hardcoded path, because a committed plist with someone else's
# absolute path in it is wrong by default.

set -euo pipefail

LABEL="com.nba-tracker.nightly"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "${1:-}" = "--remove" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL"
    exit 0
fi

# macOS protects ~/Documents, ~/Desktop and ~/Downloads from background agents
# (TCC). A launchd job cannot even stat a script inside them — it fails with
# "Operation not permitted" and exit 126, which looks nothing like a permissions
# problem in the log. Refuse to install rather than let that be discovered at
# 8am some morning.
case "$PROJECT_ROOT" in
    "$HOME"/Documents/*|"$HOME"/Desktop/*|"$HOME"/Downloads/*)
        echo "ERROR: the repo is in a TCC-protected directory:"
        echo "         $PROJECT_ROOT"
        echo "       launchd cannot read Documents, Desktop or Downloads."
        echo "       Move the repo (e.g. to ~/dev/nba-tracker) and re-run this."
        exit 1
        ;;
esac

if [ ! -x "$PROJECT_ROOT/venv/bin/python" ]; then
    echo "ERROR: no venv at $PROJECT_ROOT/venv — create it before installing"
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_ROOT/logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$PROJECT_ROOT/scripts/nightly.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PROJECT_ROOT</string>

    <!--
      8am local. Games on the East Coast finish around 1am ET, which is 10pm
      Pacific, so nothing requires an overnight run — and 8am is a time the
      machine is awake, which avoids needing pmset to schedule a wake.

      Asleep at 8am: launchd runs the job on the next wake.
      Shut down at 8am: the run is skipped, and that is fine — the job is
      self-healing, so the next run collects every game that was missed.
    -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>8</integer>
        <key>Minute</key><integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>$PROJECT_ROOT/logs/nightly.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_ROOT/logs/nightly.log</string>

    <!-- A scheduled batch job, not a daemon: do not restart it on exit. -->
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLIST_EOF

plutil -lint "$PLIST" >/dev/null
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "installed $LABEL"
echo "  project : $PROJECT_ROOT"
echo "  runs    : 08:00 daily"
echo "  log     : $PROJECT_ROOT/logs/nightly.log"
echo
echo "  run now : launchctl start $LABEL"
echo "  status  : launchctl list | grep nba-tracker"
echo "  remove  : bash scripts/install_launchd.sh --remove"
