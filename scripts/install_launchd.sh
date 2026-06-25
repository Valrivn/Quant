#!/bin/bash
# install_launchd.sh - Install macOS launchd service for Psychological Pipeline

set -euo pipefail

PLIST_SOURCE="/Users/hayden/Desktop/quant-py/scripts/com.quant.psychological.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.quant.psychological.plist"
LOG_DIR="/Users/hayden/Desktop/quant-py/logs"

echo "Installing macOS launchd service for Psychological Pipeline..."

# Create log directory
mkdir -p "${LOG_DIR}"

# Copy plist to LaunchAgents
cp "${PLIST_SOURCE}" "${PLIST_DEST}"
echo "Copied plist to ${PLIST_DEST}"

# Load the service
launchctl load "${PLIST_DEST}"
echo "Service loaded"

# Verify it's loaded
if launchctl list | grep -q "com.quant.psychological"; then
    echo "✓ Service is running"
else
    echo "✗ Service failed to load"
    exit 1
fi

echo ""
echo "Service installed successfully!"
echo "  Runs: Tue/Fri at 17:30 (5:30 PM)"
echo "  Logs: ${LOG_DIR}/"
echo ""
echo "To check status: launchctl list com.quant.psychological"
echo "To view logs: tail -f ${LOG_DIR}/launchd_stdout.log"
echo "To uninstall: launchctl unload ${PLIST_DEST} && rm ${PLIST_DEST}"