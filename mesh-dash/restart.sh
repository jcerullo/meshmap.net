#!/bin/bash

# Get parameters
DELAY=${1:-5}  # Default to 5 seconds if not specified
# PARENT_PID is not strictly needed if systemd handles the stop/start of the correct service.

# Log file location (adjust as needed)
LOG_FILE="/tmp/meshtastic-restart.log" # Or use systemd's journal

# Log function
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

log "Restart script started. Service: mesh-dash, Delay: ${DELAY}s"

log "Waiting for ${DELAY} seconds before initiating restart..."
sleep "$DELAY"

log "Attempting to restart mesh-dash service via systemctl..."

# Stop the service (systemd will handle killing the process)
log "Stopping mesh-dash service..."
if systemctl stop mesh-dash.service; then
    log "mesh-dash service stopped successfully."
else
    log "ERROR: Failed to stop mesh-dash service. Attempting to continue..."
fi

# Optional: Add a small delay to ensure the service is fully stopped.
sleep 2

# Start the service
log "Starting mesh-dash service..."
if systemctl start mesh-dash.service; then
    log "mesh-dash service started successfully."
else
    log "ERROR: Failed to start mesh-dash service."
    exit 1 # Exit with an error if start fails
fi

log "Restart script finished."
exit 0