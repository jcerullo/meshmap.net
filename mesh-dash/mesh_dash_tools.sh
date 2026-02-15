#!/usr/bin/env bash
set -euo pipefail # Exit on error, unset variable, or pipe failure

# Determine the script's absolute directory to define INSTALL_DIR
# This assumes the management script resides directly within the installation directory.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
INSTALL_DIR="$SCRIPT_DIR" # Define INSTALL_DIR based on script location

# --- Configuration Variables ---
INSTALL_SUBDIR="mesh-dash" # Note: This seems unused if INSTALL_DIR is the main dir. Keep for context? Or remove if truly unused.
VENV_DIR="mesh-dash_venv"
CONFIG_FILE=".mesh-dash_config"
SUMMARY_FILE="Installation_details.txt" # Note: Also seems unused in the provided functions.
SERVICE_NAME="mesh-dash"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
RUN_SCRIPT_NAME="run_meshdash.sh"
DB_PATTERN="*.db"

# --- Color Codes ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color (Removed trailing space)

# --- Logging Functions ---
echoinfo() { printf "${BLUE}[INFO]${NC} %s\n" "$1"; }
echoerror() { printf "${RED}${BOLD}[ERROR]${NC} %s\n" "$@" >&2; }
echowarn() { printf "${YELLOW}[WARN]${NC} %s\n" "$1"; }
echosuccess() { printf "${GREEN}[SUCCESS]${NC} %s\n" "$1"; }
echostep() { printf "\n${GREEN}${BOLD}=== %s ===${NC}\n" "$1"; }
echosubstep() { printf "\n${CYAN}--- %s ---${NC}\n" "$1"; }

# --- Utility Functions ---

press_any_key() {
    # Check if running in an interactive terminal
    if [[ -t 0 ]]; then
        printf "\n${CYAN}Press any key to return...${NC}"
        # Read a single character, silently, without interpreting backslashes
        read -n 1 -s -r
        echo "" # Move to the next line
    fi
}

check_prereqs() {
    local missing_critical=0
    local missing_optional=0

    # Check for sudo or root privileges
    if ! command -v sudo &> /dev/null; then
        if [[ $EUID -ne 0 ]]; then
            echoerror "'sudo' command not found and not running as root. Service/removal operations requiring root may fail."
            missing_critical=1
        else
            echoinfo "Running as root, 'sudo' command check skipped."
        fi
    fi

    # Check for systemctl (optional, for service management)
    if ! command -v systemctl &> /dev/null; then
        echowarn "'systemctl' command not found. Service management options will not work."
        missing_optional=1
    fi

    # Check for a text editor (optional, for config editing)
    if ! command -v "${EDITOR:-nano}" &> /dev/null && ! command -v nano &> /dev/null && ! command -v vi &> /dev/null; then
        echowarn "No common text editor (nano, vi) found, and EDITOR variable not set. Config editing may not work."
        missing_optional=1
    fi

    if [[ $missing_critical -ne 0 ]]; then
        return 1 # Critical prerequisite missing
    elif [[ $missing_optional -ne 0 ]]; then
        return 2 # Optional prerequisite missing
    else
        return 0 # All found or not needed
    fi
}

get_sudo_cmd() {
    if [[ $EUID -ne 0 ]] && command -v sudo &> /dev/null; then
        echo "sudo"
    else
        # If already root or sudo doesn't exist, return empty string
        echo ""
    fi
}

get_editor_cmd() {
    if [[ -n "${EDITOR-}" ]] && command -v "$EDITOR" &>/dev/null; then
        echo "$EDITOR"
    elif command -v nano &>/dev/null; then
        echo "nano"
    elif command -v vi &>/dev/null; then
        echo "vi"
    else
        echo "" # No suitable editor found
    fi
}

confirm_action() {
    local message="$1"
    # Default to requiring 'y'/'yes', unless 'true' is passed for strict 'yes' confirmation
    local require_yes="${2:-false}"
    local prompt_str
    local confirm
    local confirm_lower

    if [[ "$require_yes" == "true" ]]; then
        prompt_str=$(printf "${RED}${BOLD}%s (Type 'yes' to confirm): ${NC}" "$message")
        read -p "$prompt_str" confirm
        confirm_lower=$(echo "$confirm" | tr '[:upper:]' '[:lower:]')
        # Return 0 (success) only if 'yes' was typed
        [[ "$confirm_lower" == "yes" ]]
    else
        prompt_str=$(printf "${YELLOW}%s (y/N): ${NC}" "$message")
        read -p "$prompt_str" confirm
        confirm_lower=$(echo "$confirm" | tr '[:upper:]' '[:lower:]')
        # Return 0 (success) if 'y' or 'yes' was typed
        [[ "$confirm_lower" == "y" || "$confirm_lower" == "yes" ]]
    fi
    # The return status of the `[[ ... ]]` command is used implicitly
}

can_manage_service() {
    if ! command -v systemctl &> /dev/null; then
        echoerror "'systemctl' command not found. Cannot manage service."
        return 1
    fi
    local sudo_cmd
    sudo_cmd=$(get_sudo_cmd)
    # Need sudo or to be root
    if [[ -z "$sudo_cmd" ]] && [[ $EUID -ne 0 ]]; then
        echoerror "Not running as root and 'sudo' command not found. Cannot manage service."
        return 1
    fi
    return 0 # Can manage service
}

# --- Service Management Functions ---

perform_systemctl_action() {
    local action=$1 # e.g., start, stop, enable, disable, restart
    local action_gerund=$2 # e.g., Starting, Stopping, Enabling (for user message)
    local sudo_cmd
    sudo_cmd=$(get_sudo_cmd)

    echoinfo "${action_gerund} service '${SERVICE_NAME}'..."
    # Execute the systemctl command, capture success/failure
    if $sudo_cmd systemctl "$action" "${SERVICE_NAME}.service"; then
        echosuccess "Service '${SERVICE_NAME}' ${action}ed successfully."

        # Reload daemon after enable/disable for changes to take effect
        if [[ "$action" == "enable" || "$action" == "disable" ]]; then
            echoinfo "Reloading systemd daemon..."
            if ! $sudo_cmd systemctl daemon-reload; then
                 # Non-fatal warning if reload fails
                 echowarn "Failed to reload systemd daemon. You may need to run '$sudo_cmd systemctl daemon-reload' manually."
            fi
        fi

        # Reset failed state after starting/restarting
        if [[ "$action" == "start" || "$action" == "restart" ]]; then
            # Suppress output, just try to reset
             $sudo_cmd systemctl reset-failed "${SERVICE_NAME}.service" &> /dev/null || true
        fi
        return 0 # Action succeeded
    else
        echoerror "Failed to ${action} service '${SERVICE_NAME}'. Check status or logs for details."
        return 1 # Action failed
    fi
}

service_status() {
    echosubstep "Getting Service Status ('${SERVICE_NAME}')"
    if ! can_manage_service; then return 1; fi
    local sudo_cmd
    sudo_cmd=$(get_sudo_cmd)

    # Display status without paging
    $sudo_cmd systemctl status "${SERVICE_NAME}.service" --no-pager
}

service_logs() {
    echosubstep "Viewing Recent Service Logs ('${SERVICE_NAME}')"
    if ! can_manage_service; then return 1; fi
    local sudo_cmd
    sudo_cmd=$(get_sudo_cmd)
    echoinfo "Showing last 50 log entries. Press Ctrl+C to exit log view."

    # Show logs using journalctl without paging
    $sudo_cmd journalctl -u "${SERVICE_NAME}.service" -n 50 --no-pager --follow
    # Note: Added --follow to stream logs, user needs Ctrl+C to exit. Removed --no-pager if --follow is used.
    # If you prefer static output, remove --follow and keep --no-pager:
    # $sudo_cmd journalctl -u "${SERVICE_NAME}.service" -n 50 --no-pager
}

# --- Configuration and Manual Run ---

view_config() {
    echosubstep "Viewing Configuration File ('${CONFIG_FILE}')"
    local config_path="$INSTALL_DIR/$CONFIG_FILE"
    if [ -f "$config_path" ]; then
        echoinfo "Contents of '$config_path':"
        printf -- "----------------------------------------\n"
        cat "$config_path"
        printf -- "----------------------------------------\n"
    else
        echowarn "Configuration file '$config_path' not found."
    fi
}

edit_config() {
    echosubstep "Editing Configuration File ('${CONFIG_FILE}')"
    local config_path="$INSTALL_DIR/$CONFIG_FILE"
    local editor_cmd
    editor_cmd=$(get_editor_cmd)

    if [ ! -f "$config_path" ]; then
        echowarn "Configuration file '$config_path' not found. Cannot edit."
        return 1
    fi

    if [ -z "$editor_cmd" ]; then
        echoerror "No suitable text editor (nano, vi, or \$EDITOR) found. Cannot edit configuration."
        return 1
    fi

    echowarn "You are about to edit the configuration file: $config_path"
    echowarn "Incorrect changes may prevent MeshDash from starting or functioning correctly."
    echowarn "The service (if running) might need to be restarted after saving changes."

    if confirm_action "Do you want to proceed with editing using '${editor_cmd}'?" false; then
        echoinfo "Launching ${editor_cmd}..."
        # Execute the editor command safely with quoted arguments
        if "$editor_cmd" "$config_path"; then
             echosuccess "Editor closed. Remember to restart the service if needed for changes to take effect."
        else
             # Editor might return non-zero even if saved (e.g., vim closing without changes)
             # So, just warn, don't necessarily treat as failure.
             echowarn "Editor exited (Status: $?). Check if your changes were saved."
        fi
    else
        echoinfo "Configuration edit cancelled."
    fi
}

run_manually() {
    echosubstep "Running MeshDash Manually"
    local run_script_path="$INSTALL_DIR/$RUN_SCRIPT_NAME"

    if [ ! -f "$run_script_path" ]; then
        echoerror "Manual run script '$run_script_path' not found."
        echoerror "It might have been removed or the installation was incomplete."
        return 1
    fi

    # Ensure the script is executable
    if [ ! -x "$run_script_path" ]; then
        echowarn "Run script '$run_script_path' is not executable. Attempting to set permissions..."
        if chmod +x "$run_script_path"; then
            echosuccess "Set execute permission on run script."
        else
            echoerror "Failed to set execute permission on '$run_script_path'. Cannot run manually."
            return 1
        fi
    fi

    # Warn if service is potentially active
    if command -v systemctl &> /dev/null; then
        local sudo_cmd
        sudo_cmd=$(get_sudo_cmd)
         # Check if the service unit exists and is active, suppressing errors if it doesn't exist
        if $sudo_cmd systemctl is-active --quiet "${SERVICE_NAME}.service" &>/dev/null; then
            echowarn "The MeshDash systemd service ('${SERVICE_NAME}') appears to be active."
            echowarn "Running manually at the same time might cause conflicts (e.g., port already in use)."
            if ! confirm_action "Continue running manually anyway?" false; then
                echoinfo "Manual run cancelled."
                return 1 # User cancelled
            fi
        fi
    fi

    echoinfo "Attempting to start MeshDash using '$run_script_path'..."
    echoinfo "Press ${BOLD}Ctrl+C${NC} in the terminal running the script to stop it."
    printf -- "----------------------------------------\n"

    # Execute the script directly
    "$run_script_path"
    local exit_code=$? # Capture the exit code
    printf -- "----------------------------------------\n"
    echoinfo "Manual run script finished with exit code: $exit_code"
}

# --- Backup and Removal Functions ---

backup_databases() {
    echosubstep "Backing Up Database Files ('${DB_PATTERN}')"

    local db_files=()
    # Safely find files and populate the array
    while IFS= read -r -d $'\0' file; do
        db_files+=("$file")
    # Use -print0 and read -d $'\0' for maximum safety with filenames
    done < <(find "$INSTALL_DIR" -maxdepth 1 -name "$DB_PATTERN" -type f -print0)

    # Correct syntax for checking array length
    if [ ${#db_files[@]} -eq 0 ]; then
        echowarn "No database files matching '${DB_PATTERN}' found in '$INSTALL_DIR' to back up."
        return 0 # Nothing to back up is not an error
    fi

    local timestamp
    timestamp=$(date +"%Y%m%d_%H%M%S")

    # Determine parent directory safely
    local parent_dir
    parent_dir=$(dirname "$INSTALL_DIR") || parent_dir="." # Fallback to "." if dirname fails
    # Handle root or relative install dir cases
    if [[ -z "$parent_dir" || "$parent_dir" == "/" ]]; then
        parent_dir="." # Place backup in current dir if parent is root or empty
    fi
    local backup_base_dir="${parent_dir}/Database-Backup"
    local backup_dir="${backup_base_dir}/${SERVICE_NAME}_${timestamp}"

    echoinfo "Attempting to create backup directory: $backup_dir"
    if ! mkdir -p "$backup_dir"; then
        echoerror "Failed to create backup directory '$backup_dir'."
        echoerror "Please check permissions in the parent directory ('$parent_dir')."
        return 1
    fi

    echoinfo "Found the following database files to back up:"
    local count=0
    for file in "${db_files[@]}"; do
        printf "  %s\n" "$(basename "$file")"
        count=$((count + 1))
    done

    echoinfo "Backing up $count database file(s)..."
    local all_backed_up=true

    for file in "${db_files[@]}"; do
        local basename
        basename=$(basename "$file")
        echoinfo " -> Copying '$basename'..."
        # Use cp -a to preserve attributes, permissions, timestamps
        if ! cp -a "$file" "$backup_dir/$basename"; then
            echoerror "Failed to back up '$basename'. Check permissions or disk space."
            all_backed_up=false
        fi
    done

    if [ "$all_backed_up" = true ]; then
        echosuccess "All found database files backed up successfully to: $backup_dir"
        return 0
    else
        echoerror "Some database files could not be backed up. Check messages above."
        echowarn "Backup directory '$backup_dir' may contain partial backups."
        return 1
    fi
}

remove_service_component() {
    echosubstep "Removing Systemd Service Component ('${SERVICE_NAME}')"

    if ! can_manage_service; then return 1; fi
    local sudo_cmd
    sudo_cmd=$(get_sudo_cmd)

    local service_known=false
    # Check if systemd knows about the service OR if the file exists
    if $sudo_cmd systemctl list-unit-files --all | grep -q "^${SERVICE_NAME}.service" || [ -f "$SERVICE_FILE" ]; then
        service_known=true
    fi

    if [ "$service_known" = false ]; then
        echowarn "Service '${SERVICE_NAME}' does not appear to be installed or known by systemd."
        return 0 # Nothing to remove is not an error
    fi

    echoinfo "Stopping the service (if running)..."
    # Ignore errors if already stopped or doesn't exist
    $sudo_cmd systemctl stop "${SERVICE_NAME}.service" &> /dev/null || true

    echoinfo "Disabling the service (to prevent auto-start)..."
    # Ignore errors if already disabled
    if $sudo_cmd systemctl disable "${SERVICE_NAME}.service" &> /dev/null; then
        echoinfo "Service disabled."
    else
        echowarn "Could not disable service (might already be disabled or failed, ignoring)."
    fi

    # Remove the service file if it exists
    if [ -f "$SERVICE_FILE" ]; then
        echoinfo "Removing service file: ${SERVICE_FILE}..."
        if $sudo_cmd rm -f "$SERVICE_FILE"; then
            echosuccess "Service file removed."
        else
            echoerror "Failed to remove service file '${SERVICE_FILE}'. Check permissions or if the file exists."
            # Continue with daemon-reload even if rm fails
        fi
    else
        echoinfo "Service file '${SERVICE_FILE}' not found, skipping file removal."
    fi

    echoinfo "Reloading systemd daemon..."
    if $sudo_cmd systemctl daemon-reload; then
        echosuccess "Systemd daemon reloaded."
    else
        echoerror "Failed to reload systemd daemon. Run '$sudo_cmd systemctl daemon-reload' manually if issues occur."
        # This might be a more serious issue, but continue removal attempt
    fi

    echoinfo "Resetting potential failed state for the service unit..."
    # Ignore errors, just attempt cleanup
    $sudo_cmd systemctl reset-failed "${SERVICE_NAME}.service" &> /dev/null || true

    echosuccess "Service component removal process completed."
    return 0
}

remove_venv() {
    echosubstep "Removing Virtual Environment ('${VENV_DIR}')"
    local venv_path="$INSTALL_DIR/$VENV_DIR"

    if [ ! -d "$venv_path" ]; then
        echowarn "Virtual environment directory '$venv_path' not found."
        return 0 # Nothing to remove is not an error
    fi

    if ! confirm_action "Are you sure you want to permanently delete the Python virtual environment directory '$venv_path'? This contains all installed Python packages." false; then
        echoinfo "Virtual environment removal cancelled."
        return 1 # User cancelled
    fi

    echoinfo "Removing directory: $venv_path"
    if rm -rf "$venv_path"; then
        echosuccess "Virtual environment directory removed."
        return 0
    else
        echoerror "Failed to remove directory '$venv_path'. Check permissions."
        return 1
    fi
}

purge_databases() {
    echosubstep "Purging Database Files ('${DB_PATTERN}')"

    local db_files=()
    # Safely find files and populate the array
    while IFS= read -r -d $'\0' file; do
        db_files+=("$file")
    done < <(find "$INSTALL_DIR" -maxdepth 1 -name "$DB_PATTERN" -type f -print0)

    # Correct syntax for checking array length
    if [ ${#db_files[@]} -eq 0 ]; then
        echowarn "No database files matching '${DB_PATTERN}' found in '$INSTALL_DIR'."
        return 0 # Nothing to purge is not an error
    fi

    echoinfo "Found the following database files to potentially remove:"
    for file in "${db_files[@]}"; do
        printf "  %s\n" "$(basename "$file")"
    done

    # Require explicit 'yes' for destructive action
    if ! confirm_action "PERMANENTLY DELETE these database files? This cannot be undone." true; then
        echoinfo "Database file removal cancelled."
        return 1 # User cancelled
    fi

    echoinfo "Removing database files..."
    local all_removed=true

    for file in "${db_files[@]}"; do
        local basename
        basename=$(basename "$file")
        echoinfo " -> Removing '$basename'..."
        if ! rm -f "$file"; then
            echoerror "Failed to remove '$basename'. Check permissions."
            all_removed=false
        fi
    done

    if [ "$all_removed" = true ]; then
        echosuccess "All found database files removed."
        return 0
    else
        echoerror "Some database files could not be removed. Check messages above."
        return 1
    fi
}

remove_other_local_files() {
    echosubstep "Removing Other Local Files (Config, Run Script)" # Removed Summary as it wasn't used

    local config_path="$INSTALL_DIR/$CONFIG_FILE"
    local run_script_path="$INSTALL_DIR/$RUN_SCRIPT_NAME"
    # local summary_path="$INSTALL_DIR/$SUMMARY_FILE" # Variable SUMMARY_FILE seems unused
    local files_to_remove=()

    # Add files to the array only if they exist
    [ -f "$config_path" ] && files_to_remove+=("$config_path")
    [ -f "$run_script_path" ] && files_to_remove+=("$run_script_path")
    # [ -f "$summary_path" ] && files_to_remove+=("$summary_path")

    # Correct syntax for checking array length
    if [ ${#files_to_remove[@]} -eq 0 ]; then
        echowarn "No other local files (config, run script) found in '$INSTALL_DIR'."
        return 0 # Nothing to remove is not an error
    fi

    echoinfo "The following files will be removed:"
    for file in "${files_to_remove[@]}"; do
        printf "  %s\n" "$(basename "$file")"
    done

    if ! confirm_action "Are you sure you want to permanently delete these files?" false; then
        echoinfo "Removal of other local files cancelled."
        return 1 # User cancelled
    fi

    echoinfo "Proceeding with removal..."
    local success=true
    for file in "${files_to_remove[@]}"; do
        local basename
        basename=$(basename "$file")
        echoinfo " -> Removing '$basename'..."
        if ! rm -f "$file"; then
            echoerror "Failed to remove '$basename'. Check permissions."
            success=false
        fi
    done

    if [ "$success" = true ]; then
        echosuccess "Other local files removed successfully."
        return 0
    else
        echoerror "Some other local files could not be removed."
        return 1
    fi
}

remove_all_except_self() {
    echosubstep "Removing All Files/Dirs in '$INSTALL_DIR' (Except This Management Script)"

    local self_real_path
    # Get the real path of the script being executed
    self_real_path=$(realpath "${BASH_SOURCE[0]}")
    local self_basename
    self_basename=$(basename "$self_real_path")
    local self_dir
    self_dir=$(dirname "$self_real_path")

    # Critical safety check: Ensure script is running from the expected INSTALL_DIR
    if [[ "$self_dir" != "$INSTALL_DIR" ]]; then
        echoerror "Safety Check Failed: Script directory ('$self_dir') does not match installation directory ('$INSTALL_DIR')."
        echoerror "Aborting full removal to prevent accidental data loss."
        echoerror "Ensure this script (${self_basename}) is located directly inside the directory you want to clear."
        return 1
    fi

    echowarn "This action will attempt to remove everything inside '$INSTALL_DIR',"
    echowarn "including the virtual environment, databases, config files, logs, etc.,"
    echowarn "EXCEPT for this script itself ('$self_basename')."
    echowarn "This operation is PERMANENT and CANNOT BE UNDONE."

    # Require explicit 'yes' confirmation for this highly destructive action
    if ! confirm_action "Are you absolutely sure you want to remove all other items in '$INSTALL_DIR'?" true; then
        echoinfo "Operation cancelled."
        return 1 # User cancelled
    fi

    echoinfo "Scanning directory '$INSTALL_DIR' for items to remove..."
    local item_count=0
    local removal_errors=0

    # Enable dotglob to match hidden files/dirs (like .git, .env)
    shopt -s dotglob
    local item # Declare item locally

    # Loop through all items (files, dirs, links) in the install directory
    for item in "$INSTALL_DIR"/*; do
        # Important: Check if the item exists and is not the script itself
        # The check for existence handles the case where the directory is empty or contains only the script
        if [[ -e "$item" && "$(basename "$item")" != "$self_basename" ]]; then
            item_count=$((item_count + 1))
            local item_basename
            item_basename=$(basename "$item") # Get basename for logging
            echoinfo " -> Removing '$item_basename'..."
            # Use rm -rf to remove both files and directories recursively and forcefully
            if ! rm -rf "$item"; then
                echoerror "Failed to remove '$item_basename'. Check permissions or if it's in use."
                removal_errors=$((removal_errors + 1))
            fi
        fi
    done

    # Disable dotglob after the loop
    shopt -u dotglob

    echoinfo "Scan complete. Attempted removal of $item_count item(s)."

    if [[ $removal_errors -eq 0 ]]; then
        echosuccess "All other items in '$INSTALL_DIR' removed successfully."
        return 0
    else
        echoerror "$removal_errors error(s) occurred during removal. Check messages above."
        echowarn "The directory '$INSTALL_DIR' may not be completely clean."
        return 1
    fi
}


# --- Main Menu / Script Logic ---

main_menu() {
    while true; do
        clear # Clear screen for menu
        printf "${BOLD}MeshDash Management Tool${NC}\n"
        printf "Installation Directory: ${CYAN}%s${NC}\n" "$INSTALL_DIR"
        printf "Service Name: ${CYAN}%s${NC}\n" "$SERVICE_NAME"
        echostep "Select an Action"

        # Service Management Options (only if systemctl available)
        if command -v systemctl &> /dev/null; then
             printf "  ${YELLOW}Service Management:${NC}\n"
             printf "   1) Start Service\n"
             printf "   2) Stop Service\n"
             printf "   3) Restart Service\n"
             printf "   4) Enable Service (Auto-start on boot)\n"
             printf "   5) Disable Service (No auto-start on boot)\n"
             printf "   6) View Service Status\n"
             printf "   7) View Recent Service Logs (Ctrl+C to exit)\n"
        else
             printf "  ${YELLOW}Service Management (systemctl not found):${NC}\n"
             printf "   (Service options disabled)\n"
        fi

        # Configuration and Manual Run
        printf "  ${YELLOW}Configuration & Manual Run:${NC}\n"
        printf "   8) View Configuration File\n"
        local editor_cmd_check
        editor_cmd_check=$(get_editor_cmd)
        if [[ -n "$editor_cmd_check" ]]; then
            printf "   9) Edit Configuration File (using %s)\n" "$editor_cmd_check"
        else
             printf "   9) ${YELLOW}Edit Configuration File (No editor found)${NC}\n"
        fi
        printf "  10) Run MeshDash Manually (Foreground)\n"

        # Backup and Removal
        printf "  ${YELLOW}Backup & Removal:${NC}\n"
        printf "  11) Backup Database Files (%s)\n" "$DB_PATTERN"
        if command -v systemctl &> /dev/null; then
             printf "  12) ${RED}Remove Systemd Service ONLY${NC}\n"
        else
             printf "  12) ${RED}(Remove Systemd Service - N/A)${NC}\n"
        fi
        printf "  13) ${RED}Remove Virtual Environment ONLY (%s)${NC}\n" "$VENV_DIR"
        printf "  14) ${RED}Purge Database Files ONLY (%s)${NC}\n" "$DB_PATTERN"
        printf "  15) ${RED}Remove Other Local Files ONLY (Config, Run Script)${NC}\n"
        printf "  16) ${RED}${BOLD}!!! REMOVE EVERYTHING (Except this script) !!!${NC}\n"

        # Exit
        printf "  ${YELLOW}Other:${NC}\n"
        printf "   0) Exit\n"

        printf "\n${CYAN}Enter your choice: ${NC}"
        read -r choice

        case "$choice" in
            1) can_manage_service && perform_systemctl_action "start" "Starting" ;;
            2) can_manage_service && perform_systemctl_action "stop" "Stopping" ;;
            3) can_manage_service && perform_systemctl_action "restart" "Restarting" ;;
            4) can_manage_service && perform_systemctl_action "enable" "Enabling" ;;
            5) can_manage_service && perform_systemctl_action "disable" "Disabling" ;;
            6) can_manage_service && service_status ;;
            7) can_manage_service && service_logs ;;
            8) view_config ;;
            9) [[ -n "$editor_cmd_check" ]] && edit_config || echoerror "No suitable editor found." ;;
           10) run_manually ;;
           11) backup_databases ;;
           12) can_manage_service && remove_service_component || echoerror "Cannot manage service (systemctl missing or no sudo/root)." ;;
           13) remove_venv ;;
           14) purge_databases ;;
           15) remove_other_local_files ;;
           16) remove_all_except_self ;;
            0) echoinfo "Exiting."; exit 0 ;;
            *) echoerror "Invalid choice. Please try again." ;;
        esac
        # Pause only if not exiting or viewing streaming logs
        if [[ "$choice" != "7" && "$choice" != "0" ]]; then
             press_any_key
        fi
    done
}

# --- Script Entry Point ---

# Perform initial prerequisite checks
check_prereqs
prereq_status=$?
if [[ $prereq_status -eq 1 ]]; then
    echoerror "Critical prerequisites are missing. Cannot continue."
    press_any_key
    exit 1
elif [[ $prereq_status -eq 2 ]]; then
    echowarn "Some optional features may not be available (see warnings above)."
    press_any_key # Allow user to see warnings before menu
fi

# Display the main menu
main_menu

exit 0