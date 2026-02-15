import os
import re
import json
import logging
import asyncio
import sys # Added for sys.executable and sys.argv
from typing import Dict, Set, Optional, List, Any # Added List, Any

from fastapi import APIRouter, HTTPException, Path, Body, status, Request # Added Request
from pydantic import BaseModel as PydanticBaseModel, Field

DEFAULT_DASH_CONFIG_PATH = ".mesh-dash_config"

DASH_CONFIG_PATH = os.environ.get("DASH_CONFIG_PATH", DEFAULT_DASH_CONFIG_PATH)
ABS_DASH_CONFIG_PATH = os.path.abspath(DASH_CONFIG_PATH)

config_logger = logging.getLogger("meshtastic_dashboard.config") # Assuming logger is configured in main app
config_logger.info(f"System module using MeshDash config path: {ABS_DASH_CONFIG_PATH}")

# --- Pydantic Models ---
class ConfigUpdateRequest(PydanticBaseModel):
    value: str = Field(..., description="The new string value for the configuration key.")

# Pydantic Models for the new /initial-setup endpoint
class AdminUserSetup(PydanticBaseModel):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)

class ConfigValuesSetup(PydanticBaseModel):
    SEND_LOCAL_NODE_LOCATION: str
    SEND_OTHER_NODES_LOCATION: str
    COMMUNITY_API: str
    # --- ADDED MISSING FIELDS ---
    LOCATION_OFFSET_ENABLED: str
    LOCATION_OFFSET_METERS: str
    # --- END ADDED FIELDS ---

class RawSelectionsSetup(PydanticBaseModel): # Optional, but good for debugging
    operatingMode: str
    joinCommunity: str
    shareLocation: str
    shareDetectedNodes: str
    # You could add enteredOffsetMeters here if you want to log it from rawSelections
    # enteredOffsetMeters: Optional[str] = None


class InitialSetupPayload(PydanticBaseModel):
    adminUser: AdminUserSetup
    configValues: ConfigValuesSetup
    rawSelections: Optional[RawSelectionsSetup] = None

# --- Utility Functions ---
def read_dash_config(filepath: str) -> Dict[str, str]:
    """
    Reads the MeshDash .mesh-dash_config file and returns a dictionary.
    Handles KEY=VALUE and KEY="VALUE" formats, ignoring comments and blanks.
    This is a synchronous function.
    """
    config = {}
    abs_filepath = os.path.abspath(filepath)
    if not os.path.exists(abs_filepath):
        config_logger.warning(f"MeshDash config file not found: {abs_filepath}")
        return {}
    try:
        with open(abs_filepath, 'r', encoding="utf-8") as f: # Added encoding
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                match = re.match(r'^([^=\s]+)\s*=\s*("?)(.*?)\2\s*$', line)
                if match:
                    key = match.group(1)
                    value = match.group(3)
                    config[key] = value
                else:
                    config_logger.warning(f"Could not parse config line in {abs_filepath}: {line}")
    except IOError as e:
        config_logger.error(f"IOError reading MeshDash config file {abs_filepath}: {e}", exc_info=True)
        return {}
    except Exception as e:
        config_logger.error(f"Unexpected error reading MeshDash config file {abs_filepath}: {e}", exc_info=True)
        return {}
    config_logger.debug(f"Successfully read {len(config)} keys from {abs_filepath}")
    return config

def write_dash_config(filepath: str, config_data_to_update: Dict[str, str]) -> Set[str]:
    """
    Writes updated key-value pairs back to the MeshDash config file,
    preserving existing comments, blank lines, and unmentioned keys.
    This is a synchronous function.
    Returns the set of keys successfully written/updated.
    """
    lines_to_write = []
    updated_keys = set(config_data_to_update.keys())
    keys_actually_written = set()
    abs_filepath = os.path.abspath(filepath)
    config_logger.info(f"Attempting to write updates for keys {updated_keys} to {abs_filepath}")

    config_dir = os.path.dirname(abs_filepath)
    if not os.path.exists(config_dir):
        try:
            os.makedirs(config_dir, exist_ok=True)
            config_logger.info(f"Created directory for config file: {config_dir}")
        except Exception as e:
            config_logger.error(f"Failed to create directory for config file {config_dir}: {e}")
            raise IOError(f"Failed to create directory for config file: {e}")


    try:
        if os.path.exists(abs_filepath):
            with open(abs_filepath, 'r', encoding="utf-8") as f_read: # Added encoding
                original_lines = f_read.readlines()
                config_logger.debug(f"Read {len(original_lines)} lines from existing config file.")
        else:
            original_lines = []
            config_logger.info(f"Config file {abs_filepath} does not exist, will create.")

        for line in original_lines:
            stripped_line = line.strip()

            if not stripped_line or stripped_line.startswith('#'):
                lines_to_write.append(line)
                continue

            match = re.match(r'^([^=\s]+)\s*=', stripped_line)
            if match:
                key = match.group(1)

                if key in updated_keys:
                    new_value = str(config_data_to_update[key]) # Ensure it's a string

                    # Apply quoting logic
                    if key in ["INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_PASSWORD", "DB_PATH", "MESHTASTIC_HOST"]: # Added DB_PATH, MESHTASTIC_HOST as examples that might need quotes
                        formatted_line = f'{key}="{new_value}"\n'
                    elif new_value.lower() in ["true", "false"]: # Handles booleans like COMMUNITY_API, LOCATION_OFFSET_ENABLED
                        formatted_line = f'{key}={new_value.lower()}\n'
                    # Check for floats (like LOCATION_OFFSET_METERS) or integers (like MESHTASTIC_PORT)
                    elif re.fullmatch(r"[-+]?\d*\.\d+|\d+", new_value) and not (' ' in new_value or '#' in new_value):
                        formatted_line = f'{key}={new_value}\n'
                    elif ' ' in new_value or not new_value or '#' in new_value: # General case for values needing quotes
                        formatted_line = f'{key}="{new_value}"\n'
                    else: # Default for simple strings not needing quotes
                        formatted_line = f'{key}={new_value}\n'
                    
                    lines_to_write.append(formatted_line)
                    keys_actually_written.add(key)
                    config_logger.debug(f"Updated line for key '{key}'")
                else:
                    lines_to_write.append(line)
            else:
                lines_to_write.append(line)
                config_logger.debug(f"Preserving non-matching line: {line.strip()}")

        new_keys_to_add = updated_keys - keys_actually_written
        if new_keys_to_add:
            if lines_to_write and lines_to_write[-1].strip() != "":
                lines_to_write.append("\n")
            
            # Check if the specific "--- Added via API ---" header for these keys should be added
            # or if they fit under an existing section. For now, generic add.
            # Consider adding sections for different types of config if not already present.
            has_api_header = any("# --- Added via API ---" in l_strip for l_strip in [line.strip() for line in lines_to_write])
            if not has_api_header:
                 # Check if a more specific header like "--- Location Offset Settings ---" already exists for these keys
                has_offset_header = any("--- Location Offset Settings ---" in l_strip for l_strip in [line.strip() for line in lines_to_write])
                if not has_offset_header and ("LOCATION_OFFSET_ENABLED" in new_keys_to_add or "LOCATION_OFFSET_METERS" in new_keys_to_add):
                     lines_to_write.append("\n# --- Location Offset Settings (for Community API Heartbeat) ---\n")
                elif not any(key.startswith("INITIAL_") for key in new_keys_to_add): # Avoid general API header for initial admin setup if it's the only new thing
                    lines_to_write.append("# --- Added via API ---\n")


            for key in sorted(list(new_keys_to_add)): # Sort for consistent ordering
                new_value = str(config_data_to_update[key])
                if key in ["INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_PASSWORD", "DB_PATH", "MESHTASTIC_HOST"]:
                    formatted_line = f'{key}="{new_value}"\n'
                elif new_value.lower() in ["true", "false"]:
                    formatted_line = f'{key}={new_value.lower()}\n'
                elif re.fullmatch(r"[-+]?\d*\.\d+|\d+", new_value) and not (' ' in new_value or '#' in new_value):
                    formatted_line = f'{key}={new_value}\n'
                elif ' ' in new_value or not new_value or '#' in new_value:
                    formatted_line = f'{key}="{new_value}"\n'
                else:
                    formatted_line = f'{key}={new_value}\n'
                lines_to_write.append(formatted_line)
                keys_actually_written.add(key)
                config_logger.debug(f"Added new line for key '{key}'")

        with open(abs_filepath, 'w', encoding="utf-8") as f_write: # Added encoding
            f_write.writelines(lines_to_write)

        config_logger.info(f"Successfully wrote config updates to {abs_filepath} for keys: {keys_actually_written}")
        return keys_actually_written

    except IOError as e:
        config_logger.error(f"IOError writing MeshDash config file {abs_filepath}: {e}", exc_info=True)
        raise
    except Exception as e:
        config_logger.error(f"Unexpected error writing MeshDash config file {abs_filepath}: {e}", exc_info=True)
        raise

# --- FastAPI Router Definition ---
config_router = APIRouter()

# --- Existing Endpoints ---
@config_router.get("/", response_model=Dict[str, str])
async def get_current_dash_config():
    """Reads and returns the current MeshDash configuration."""
    config_logger.info("API request received to GET current MeshDash config.")
    try:
        config_data = await asyncio.to_thread(read_dash_config, ABS_DASH_CONFIG_PATH)
        if not config_data and not os.path.exists(ABS_DASH_CONFIG_PATH):
            config_logger.warning(f"MeshDash config file {ABS_DASH_CONFIG_PATH} not found during GET request.")
            raise HTTPException(status_code=404, detail=f"MeshDash config file not found at {ABS_DASH_CONFIG_PATH}")
        config_logger.info(f"Returning {len(config_data)} MeshDash config keys.")
        return config_data
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        config_logger.error(f"Error processing GET request for MeshDash config: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to read MeshDash config: {e}")

@config_router.put("/{key_name}", response_model=Dict[str, str])
async def update_dash_config_key(
    key_name: str = Path(..., description="The configuration key name to update.", example="MESHTASTIC_HOST"),
    update_request: ConfigUpdateRequest = Body(...)
):
    """Updates the value of a specific key in the MeshDash configuration file."""
    config_logger.info(f"API request received to PUT update for MeshDash config key: '{key_name}'")
    new_value = update_request.value
    config_to_write = {key_name: new_value}

    try:
        written_keys = await asyncio.to_thread(write_dash_config, ABS_DASH_CONFIG_PATH, config_to_write)
        if key_name not in written_keys:
            config_logger.warning(f"Key '{key_name}' might have been newly added or not reported as updated by write_dash_config. Proceeding.")

        config_logger.info(f"Successfully updated MeshDash config key '{key_name}' to '{new_value}' in {ABS_DASH_CONFIG_PATH}")
        return {key_name: new_value}
    except HTTPException as http_exc:
        raise http_exc
    except IOError as e:
        config_logger.error(f"IOError during config write for key '{key_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to write config file update: {e}")
    except Exception as e:
        config_logger.error(f"Unexpected error during config write for key '{key_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error updating config file: {e}")

# --- New Endpoints for Initial Setup ---
@config_router.post("/initial-setup", tags=["System Config API"])
async def handle_initial_setup(payload: InitialSetupPayload, request: Request):
    """
    Handles the initial setup data submission.
    Writes admin credentials and privacy settings to the .mesh-dash_config file.
    Then, triggers an automatic server restart.
    """
    client_host = request.client.host if request.client else "unknown"
    config_logger.info(f"Initial setup request from {client_host}: User '{payload.adminUser.username}', Configs: {payload.configValues}")

    config_updates = {
        "INITIAL_ADMIN_USERNAME": payload.adminUser.username,
        "INITIAL_ADMIN_PASSWORD": payload.adminUser.password,
        "SEND_LOCAL_NODE_LOCATION": payload.configValues.SEND_LOCAL_NODE_LOCATION.lower(),
        "SEND_OTHER_NODES_LOCATION": payload.configValues.SEND_OTHER_NODES_LOCATION.lower(),
        "COMMUNITY_API": payload.configValues.COMMUNITY_API.lower(),
        # --- ADDED PROCESSING FOR NEW FIELDS ---
        "LOCATION_OFFSET_ENABLED": payload.configValues.LOCATION_OFFSET_ENABLED.lower(),
        "LOCATION_OFFSET_METERS": payload.configValues.LOCATION_OFFSET_METERS # This is a string like "50.0", no .lower() needed
        # --- END ADDED PROCESSING ---
    }

    try:
        written_keys = await asyncio.to_thread(write_dash_config, ABS_DASH_CONFIG_PATH, config_updates)
        
        if not all(key in written_keys for key in config_updates.keys()):
            missing_keys = [key for key in config_updates.keys() if key not in written_keys]
            config_logger.warning(f"Not all keys were reported as written/updated by write_dash_config during initial setup. Missing/not reported: {missing_keys}. This might be okay if they were new keys.")

        config_logger.info(f"Successfully wrote initial setup data to config file: {ABS_DASH_CONFIG_PATH}")

    except IOError as e:
        config_logger.error(f"IOError during initial setup config write: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to write config file update: {e}")
    except Exception as e:
        config_logger.error(f"Unexpected error during initial setup config write: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Unexpected error updating config file: {e}")

    new_file_indicator_path = os.path.join(os.path.dirname(ABS_DASH_CONFIG_PATH), "static", ".new") 
    if os.path.exists(new_file_indicator_path):
        try:
            os.remove(new_file_indicator_path)
            config_logger.info(f"Successfully removed setup indicator file: {new_file_indicator_path}")
        except Exception as e:
            config_logger.error(f"Failed to remove setup indicator file '{new_file_indicator_path}': {e}. Please remove it manually.")
    else:
        config_logger.info(f"Setup indicator file '{new_file_indicator_path}' not found, no removal needed.")

    response_message = "Initial setup data received and configuration file updated. Server is now restarting to apply changes and create admin user."
    config_logger.info(response_message)


    async def deferred_restart():
        await asyncio.sleep(1.5) 
        config_logger.warning("Executing automatic server restart after initial setup...")
        try:
            python_executable = sys.executable
            script_args = sys.argv 
            os.execv(python_executable, [python_executable] + script_args)
        except Exception as e:
            config_logger.critical(f"FATAL: Failed to execute automatic restart: {e}", exc_info=True)

    asyncio.create_task(deferred_restart())

    return {"message": response_message, "restart_scheduled": True}

@config_router.post("/request-restart", tags=["System Config API"])
async def request_application_restart(request: Request):
    """
    Advises the user that a restart is needed for configuration changes to take full effect.
    Does not programmatically restart the application.
    """
    client_host = request.client.host if request.client else "unknown"
    config_logger.info(f"Request from {client_host} to acknowledge need for application restart.")
    message = "Acknowledgement: For all recent configuration changes to be fully applied, please restart the MeshDash service (e.g., using 'sudo systemctl restart mesh-dash' or your deployment method)."
    return {"message": message}