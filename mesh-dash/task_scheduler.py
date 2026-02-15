#!/usr/bin/env python3
import asyncio
import sqlite3
import logging
import datetime
import httpx
import json
from croniter import croniter
from typing import Dict, Any, List, Optional, Tuple
import os
import re
import random
import sys
import math

try:
    import tzlocal
    SYSTEM_TZ = tzlocal.get_localzone() # This can be a zoneinfo.ZoneInfo object
except ImportError:
    logging.warning("tzlocal library not found. Using system's default UTC offset. "
                    "Install with: pip install tzlocal")
    SYSTEM_TZ = datetime.datetime.now().astimezone().tzinfo


# --- Database and Config File Definitions ---
TASKS_DATABASE_FILE = "tasks.db"
CONFIG_FILE_NAME = ".mesh-dash_config" # Consistent with main_app.py

# --- Determine Script Directory and Config File Path ---
_scheduler_script_dir_fallback = os.getcwd()
try:
    SCHEDULER_SCRIPT_OWN_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError: 
    SCHEDULER_SCRIPT_OWN_DIR = os.getcwd()

_potential_path_cwd = os.path.join(os.getcwd(), CONFIG_FILE_NAME)
_potential_path_own_dir = os.path.join(SCHEDULER_SCRIPT_OWN_DIR, CONFIG_FILE_NAME)
_potential_path_parent_dir = os.path.join(os.path.dirname(SCHEDULER_SCRIPT_OWN_DIR), CONFIG_FILE_NAME)

if os.path.exists(_potential_path_cwd):
    CONFIG_FILE_PATH = _potential_path_cwd
    _config_path_source_for_file = "current working directory"
elif os.path.exists(_potential_path_own_dir):
    CONFIG_FILE_PATH = _potential_path_own_dir
    _config_path_source_for_file = "scheduler script directory"
elif os.path.exists(_potential_path_parent_dir):
    CONFIG_FILE_PATH = _potential_path_parent_dir
    _config_path_source_for_file = "scheduler script parent directory"
else:
    CONFIG_FILE_PATH = _potential_path_cwd # Fallback, might not exist
    _config_path_source_for_file = "current working directory (fallback)"

# --- Configuration Defaults for Scheduler ---
DEFAULT_SCHEDULER_LOG_LEVEL_STR = "INFO"
DEFAULT_MAIN_APP_WEBSERVER_HOST = "0.0.0.0" 
DEFAULT_MAIN_APP_WEBSERVER_PORT = 8000      
DEFAULT_HEARTBEAT_API_URL = "https://meshdash.co.uk/api.php"
DEFAULT_COMMUNITY_API_ENABLED = False
DEFAULT_HEARTBEAT_INTERVAL_MINUTES = 1
DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED = False
DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED = False
DEFAULT_LOCATION_OFFSET_ENABLED = False
DEFAULT_LOCATION_OFFSET_METERS = 0.0
DEFAULT_SCHEDULER_MAX_RETRIES = 3
DEFAULT_SCHEDULER_RETRY_DELAY_SECONDS = 10
DEFAULT_SCHEDULER_CONNECT_TIMEOUT = 10.0
DEFAULT_SCHEDULER_RW_TIMEOUT = 30.0

# --- Global Configuration Variables ---
SCHEDULER_LOG_LEVEL_STR = DEFAULT_SCHEDULER_LOG_LEVEL_STR
MAIN_APP_API_URL = f"http://{DEFAULT_MAIN_APP_WEBSERVER_HOST}:{DEFAULT_MAIN_APP_WEBSERVER_PORT}" 
HEARTBEAT_REMOTE_API_URL = DEFAULT_HEARTBEAT_API_URL
COMMUNITY_API_ENABLED = DEFAULT_COMMUNITY_API_ENABLED
HEARTBEAT_INTERVAL = datetime.timedelta(minutes=DEFAULT_HEARTBEAT_INTERVAL_MINUTES)
SEND_LOCAL_NODE_LOCATION = DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED
SEND_OTHER_NODES_LOCATION = DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED
LOCATION_OFFSET_ENABLED = DEFAULT_LOCATION_OFFSET_ENABLED
LOCATION_OFFSET_METERS = DEFAULT_LOCATION_OFFSET_METERS
MAX_RETRIES = DEFAULT_SCHEDULER_MAX_RETRIES
INITIAL_RETRY_DELAY_SECONDS = DEFAULT_SCHEDULER_RETRY_DELAY_SECONDS
CONNECT_TIMEOUT = DEFAULT_SCHEDULER_CONNECT_TIMEOUT
READ_WRITE_TIMEOUT = DEFAULT_SCHEDULER_RW_TIMEOUT

EARTH_RADIUS_METERS = 6378137.0
INSTANCE_SECOND_OFFSET = random.uniform(1, 59)

logger = logging.getLogger("meshtastic_dashboard.scheduler")
if not logger.hasHandlers():
    _initial_handler = logging.StreamHandler(sys.stdout)
    _initial_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - [%(funcName)s] %(message)s')
    _initial_handler.setFormatter(_initial_formatter)
    logger.addHandler(_initial_handler)
    logger.setLevel(logging.INFO) 

logger.info(f"SCHEDULER: Resolved CONFIG_FILE_PATH to: {os.path.abspath(CONFIG_FILE_PATH)} (Source logic: {_config_path_source_for_file})")

def parse_bool_config(value: Any, default_value: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        val_lower = value.strip().lower()
        if val_lower in ('true', '1', 't', 'yes', 'y'):
            return True
        if val_lower in ('false', '0', 'f', 'no', 'n'):
            return False
    logger.debug(f"Could not parse '{value}' as bool, using default: {default_value}")
    return default_value


def load_scheduler_configuration(resolved_config_file_path: str):
    global MAIN_APP_API_URL, SEND_LOCAL_NODE_LOCATION, SEND_OTHER_NODES_LOCATION
    global HEARTBEAT_REMOTE_API_URL, COMMUNITY_API_ENABLED, HEARTBEAT_INTERVAL
    global LOCATION_OFFSET_ENABLED, LOCATION_OFFSET_METERS
    global MAX_RETRIES, INITIAL_RETRY_DELAY_SECONDS, CONNECT_TIMEOUT, READ_WRITE_TIMEOUT
    global SCHEDULER_LOG_LEVEL_STR

    config_keys_defaults = {
        "WEBSERVER_HOST": DEFAULT_MAIN_APP_WEBSERVER_HOST,
        "WEBSERVER_PORT": DEFAULT_MAIN_APP_WEBSERVER_PORT,
        "SEND_LOCAL_NODE_LOCATION": str(DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED).lower(),
        "SEND_OTHER_NODES_LOCATION": str(DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED).lower(),
        "EXTERNAL_MAP_API_BASE_URL": DEFAULT_HEARTBEAT_API_URL,
        "COMMUNITY_API": str(DEFAULT_COMMUNITY_API_ENABLED).lower(),
        "HEARTBEAT_INTERVAL_MINUTES": DEFAULT_HEARTBEAT_INTERVAL_MINUTES,
        "LOCATION_OFFSET_ENABLED": str(DEFAULT_LOCATION_OFFSET_ENABLED).lower(),
        "LOCATION_OFFSET_METERS": str(DEFAULT_LOCATION_OFFSET_METERS),
        "LOG_LEVEL": DEFAULT_SCHEDULER_LOG_LEVEL_STR,
        "SCHEDULER_MAX_RETRIES": DEFAULT_SCHEDULER_MAX_RETRIES,
        "SCHEDULER_RETRY_DELAY_SECONDS": DEFAULT_SCHEDULER_RETRY_DELAY_SECONDS,
        "SCHEDULER_CONNECT_TIMEOUT": DEFAULT_SCHEDULER_CONNECT_TIMEOUT,
        "SCHEDULER_RW_TIMEOUT": DEFAULT_SCHEDULER_RW_TIMEOUT,
    }
    
    # Values read from file, or defaults if not in file
    file_values = config_keys_defaults.copy()
    # Tracks if a key was explicitly found in the file
    found_in_file = {key: False for key in config_keys_defaults}
    # Final source for logging
    sources = {key: "script default" for key in config_keys_defaults}


    logger.info(f"Scheduler: Loading configuration from: {os.path.abspath(resolved_config_file_path)}")
    if os.path.exists(resolved_config_file_path):
        try:
            with open(resolved_config_file_path, "r") as f:
                for line_number, line in enumerate(f, 1):
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key in file_values:
                            file_values[key] = value
                            found_in_file[key] = True
                            sources[key] = f"file ('{os.path.basename(resolved_config_file_path)}')"
                        # Handle specific alternate key names if necessary for cross-compatibility
                        elif key == "EXTERNAL_MAP_API_BASE_URL":
                            file_values["EXTERNAL_MAP_API_BASE_URL"] = value
                            found_in_file["EXTERNAL_MAP_API_BASE_URL"] = True # Mark as found for heartbeat url logic
                            sources["HEARTBEAT_API_URL"] = f"file (as EXTERNAL_MAP_API_BASE_URL)"


        except Exception as e:
            logger.error(f"Scheduler: Error reading config file {resolved_config_file_path}: {e}. Using defaults/ENV.", exc_info=True)
    else:
        logger.warning(f"Scheduler: Config file not found at {resolved_config_file_path}. Using defaults/ENV.")

    # --- Apply values with FILE > ENV > DEFAULT priority for critical keys ---

    # LOG_LEVEL (for scheduler's own logger)
    if found_in_file["LOG_LEVEL"]:
        SCHEDULER_LOG_LEVEL_STR = str(file_values["LOG_LEVEL"]).upper()
        # sources["LOG_LEVEL"] is already "file..."
    else: # Not in file, try ENV, then default
        env_log_level = os.environ.get("SCHEDULER_LOG_LEVEL", os.environ.get("LOG_LEVEL"))
        if env_log_level:
            SCHEDULER_LOG_LEVEL_STR = env_log_level.upper()
            sources["LOG_LEVEL"] = "environment variable (SCHEDULER_LOG_LEVEL or LOG_LEVEL)"
        else:
            SCHEDULER_LOG_LEVEL_STR = str(config_keys_defaults["LOG_LEVEL"]).upper()
            # sources["LOG_LEVEL"] remains "script default"
    
    effective_log_level = getattr(logging, SCHEDULER_LOG_LEVEL_STR, logging.INFO)
    if logger.level != effective_log_level:
        logger.setLevel(effective_log_level)
        logging.getLogger("httpx").setLevel(logging.INFO if effective_log_level <= logging.DEBUG else logging.WARNING)
        logger.info(f"Scheduler log level set/re-set to: {SCHEDULER_LOG_LEVEL_STR} (Effective: {logging.getLevelName(effective_log_level)})")

    # Main App API URL (derived from WEBSERVER_HOST and WEBSERVER_PORT)
    # WEBSERVER_HOST/PORT will use file values if present, else defaults. Then ENV can override MAIN_APP_API_URL directly.
    webserver_host_val = str(file_values["WEBSERVER_HOST"])
    try:
        webserver_port_val = int(file_values["WEBSERVER_PORT"])
    except ValueError:
        logger.warning(f"Invalid WEBSERVER_PORT '{file_values['WEBSERVER_PORT']}' in config/default. Using default {DEFAULT_MAIN_APP_WEBSERVER_PORT}.")
        webserver_port_val = DEFAULT_MAIN_APP_WEBSERVER_PORT
        sources["WEBSERVER_PORT"] = "script default (parse error)"
        if found_in_file["WEBSERVER_PORT"]: sources["WEBSERVER_PORT"] = "file (parse error)"


    env_main_app_url = os.environ.get("MAIN_APP_API_URL")
    if env_main_app_url: # Direct ENV override for the full URL
        MAIN_APP_API_URL = env_main_app_url.rstrip('/')
        sources["MAIN_APP_API_URL_DERIVED"] = "environment variable (MAIN_APP_API_URL)"
    else: # Derive from (file or default) WEBSERVER_HOST/PORT
        connect_host = "127.0.0.1" if webserver_host_val == "0.0.0.0" else webserver_host_val
        MAIN_APP_API_URL = f"http://{connect_host}:{webserver_port_val}"
        if found_in_file["WEBSERVER_HOST"] or found_in_file["WEBSERVER_PORT"]:
             sources["MAIN_APP_API_URL_DERIVED"] = f"derived from file/default WEBSERVER_HOST/PORT"
        else:
            sources["MAIN_APP_API_URL_DERIVED"] = "derived from script defaults for WEBSERVER_HOST/PORT"


    # SEND_LOCAL_NODE_LOCATION
    if found_in_file["SEND_LOCAL_NODE_LOCATION"]:
        SEND_LOCAL_NODE_LOCATION = parse_bool_config(file_values["SEND_LOCAL_NODE_LOCATION"], DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED)
    else:
        env_val = os.environ.get("SEND_LOCAL_NODE_LOCATION")
        if env_val is not None:
            SEND_LOCAL_NODE_LOCATION = parse_bool_config(env_val, DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED)
            sources["SEND_LOCAL_NODE_LOCATION"] = "environment variable"
        else:
            SEND_LOCAL_NODE_LOCATION = DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED # From default map

    # SEND_OTHER_NODES_LOCATION
    if found_in_file["SEND_OTHER_NODES_LOCATION"]:
        SEND_OTHER_NODES_LOCATION = parse_bool_config(file_values["SEND_OTHER_NODES_LOCATION"], DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED)
    else:
        env_val = os.environ.get("SEND_OTHER_NODES_LOCATION")
        if env_val is not None:
            SEND_OTHER_NODES_LOCATION = parse_bool_config(env_val, DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED)
            sources["SEND_OTHER_NODES_LOCATION"] = "environment variable"
        else:
            SEND_OTHER_NODES_LOCATION = DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED

    # COMMUNITY_API (Scheduler specific key for enabling heartbeat)
    if found_in_file["COMMUNITY_API"]:
        COMMUNITY_API_ENABLED = parse_bool_config(file_values["COMMUNITY_API"], DEFAULT_COMMUNITY_API_ENABLED)
    else:
        env_val = os.environ.get("COMMUNITY_API")
        if env_val is not None:
            COMMUNITY_API_ENABLED = parse_bool_config(env_val, DEFAULT_COMMUNITY_API_ENABLED)
            sources["COMMUNITY_API"] = "environment variable"
        else:
            COMMUNITY_API_ENABLED = DEFAULT_COMMUNITY_API_ENABLED
            
    # LOCATION_OFFSET_ENABLED
    if found_in_file["LOCATION_OFFSET_ENABLED"]:
        LOCATION_OFFSET_ENABLED = parse_bool_config(file_values["LOCATION_OFFSET_ENABLED"], DEFAULT_LOCATION_OFFSET_ENABLED)
    else:
        env_val = os.environ.get("LOCATION_OFFSET_ENABLED")
        if env_val is not None:
            LOCATION_OFFSET_ENABLED = parse_bool_config(env_val, DEFAULT_LOCATION_OFFSET_ENABLED)
            sources["LOCATION_OFFSET_ENABLED"] = "environment variable"
        else:
            LOCATION_OFFSET_ENABLED = DEFAULT_LOCATION_OFFSET_ENABLED

    # LOCATION_OFFSET_METERS
    if found_in_file["LOCATION_OFFSET_METERS"]:
        try: LOCATION_OFFSET_METERS = float(file_values["LOCATION_OFFSET_METERS"])
        except ValueError:
            LOCATION_OFFSET_METERS = DEFAULT_LOCATION_OFFSET_METERS
            sources["LOCATION_OFFSET_METERS"] = "file (parse error)"
    else:
        env_val = os.environ.get("LOCATION_OFFSET_METERS")
        if env_val is not None:
            try:
                LOCATION_OFFSET_METERS = float(env_val)
                sources["LOCATION_OFFSET_METERS"] = "environment variable"
            except ValueError:
                LOCATION_OFFSET_METERS = DEFAULT_LOCATION_OFFSET_METERS
                sources["LOCATION_OFFSET_METERS"] = "environment variable (parse error)"
        else:
            LOCATION_OFFSET_METERS = DEFAULT_LOCATION_OFFSET_METERS
    
    # HEARTBEAT_REMOTE_API_URL (uses EXTERNAL_MAP_API_BASE_URL from file if scheduler's own key isn't set)
    # Priority: ENV(SCHEDULER_HEARTBEAT_API_URL) > ENV(HEARTBEAT_API_URL) > FILE(HEARTBEAT_API_URL) > FILE(EXTERNAL_MAP_API_BASE_URL) > DEFAULT
    explicit_hb_url_env = os.environ.get("SCHEDULER_HEARTBEAT_API_URL")
    general_hb_url_env = os.environ.get("HEARTBEAT_API_URL")

    if explicit_hb_url_env:
        HEARTBEAT_REMOTE_API_URL = explicit_hb_url_env
        sources["HEARTBEAT_API_URL"] = "environment variable (SCHEDULER_HEARTBEAT_API_URL)"
    elif general_hb_url_env:
        HEARTBEAT_REMOTE_API_URL = general_hb_url_env
        sources["HEARTBEAT_API_URL"] = "environment variable (HEARTBEAT_API_URL)"
    elif found_in_file.get("HEARTBEAT_API_URL"): # Check if scheduler specific key was in file
         HEARTBEAT_REMOTE_API_URL = file_values["HEARTBEAT_API_URL"]
         # sources["HEARTBEAT_API_URL"] already set to "file..."
    elif found_in_file.get("EXTERNAL_MAP_API_BASE_URL"): # Fallback to main app's key from file
        HEARTBEAT_REMOTE_API_URL = file_values["EXTERNAL_MAP_API_BASE_URL"]
        sources["HEARTBEAT_API_URL"] = sources.get("EXTERNAL_MAP_API_BASE_URL", "file (as EXTERNAL_MAP_API_BASE_URL)")
    else: # Fallback to default if no other source
        HEARTBEAT_REMOTE_API_URL = DEFAULT_HEARTBEAT_API_URL
        sources["HEARTBEAT_API_URL"] = "script default"


    # HEARTBEAT_INTERVAL_MINUTES
    if found_in_file["HEARTBEAT_INTERVAL_MINUTES"]:
        try: HEARTBEAT_INTERVAL = datetime.timedelta(minutes=int(file_values["HEARTBEAT_INTERVAL_MINUTES"]))
        except ValueError:
            HEARTBEAT_INTERVAL = datetime.timedelta(minutes=DEFAULT_HEARTBEAT_INTERVAL_MINUTES)
            sources["HEARTBEAT_INTERVAL_MINUTES"] = "file (parse error)"
            logger.warning(f"Invalid HEARTBEAT_INTERVAL_MINUTES from file '{file_values['HEARTBEAT_INTERVAL_MINUTES']}'. Using default.")
    else:
        env_val = os.environ.get("HEARTBEAT_INTERVAL_MINUTES")
        if env_val:
            try:
                HEARTBEAT_INTERVAL = datetime.timedelta(minutes=int(env_val))
                sources["HEARTBEAT_INTERVAL_MINUTES"] = "environment variable"
            except ValueError:
                HEARTBEAT_INTERVAL = datetime.timedelta(minutes=DEFAULT_HEARTBEAT_INTERVAL_MINUTES)
                sources["HEARTBEAT_INTERVAL_MINUTES"] = "environment variable (parse error)"
                logger.warning(f"Invalid HEARTBEAT_INTERVAL_MINUTES from ENV '{env_val}'. Using default.")
        else:
            HEARTBEAT_INTERVAL = datetime.timedelta(minutes=DEFAULT_HEARTBEAT_INTERVAL_MINUTES)


    # Scheduler specific numerical settings (Retries, Timeouts)
    # These will use file value if present and valid, else ENV if present and valid, else default.
    def get_numerical_config(key_name: str, default_value: Any, is_float: bool = False):
        val_to_set = default_value
        final_source = "script default"

        if found_in_file.get(key_name):
            try:
                val_to_set = float(file_values[key_name]) if is_float else int(file_values[key_name])
                final_source = sources[key_name] # Already "file..."
            except ValueError:
                logger.warning(f"Invalid {key_name} from file ('{file_values[key_name]}'). Trying ENV or default.")
                final_source = "file (parse error)" # Mark error, will try ENV
                # val_to_set remains default_value for now
        
        # If not from file (or file parse error), try ENV
        if not found_in_file.get(key_name) or final_source == "file (parse error)":
            env_val_str = os.environ.get(key_name)
            if env_val_str is not None:
                try:
                    val_to_set = float(env_val_str) if is_float else int(env_val_str)
                    final_source = "environment variable"
                except ValueError:
                    logger.warning(f"Invalid {key_name} from ENV ('{env_val_str}'). Using default {default_value}.")
                    val_to_set = default_value # Fallback to actual default on ENV parse error
                    final_source = "environment variable (parse error)"
            # If also not in ENV (or ENV parse error), val_to_set is already default_value
            elif final_source == "file (parse error)": # Was file error, no ENV
                 val_to_set = default_value # Ensure it's the false default

        sources[key_name] = final_source
        return val_to_set

    MAX_RETRIES = get_numerical_config("SCHEDULER_MAX_RETRIES", DEFAULT_SCHEDULER_MAX_RETRIES)
    INITIAL_RETRY_DELAY_SECONDS = get_numerical_config("SCHEDULER_RETRY_DELAY_SECONDS", DEFAULT_SCHEDULER_RETRY_DELAY_SECONDS)
    CONNECT_TIMEOUT = get_numerical_config("SCHEDULER_CONNECT_TIMEOUT", DEFAULT_SCHEDULER_CONNECT_TIMEOUT, is_float=True)
    READ_WRITE_TIMEOUT = get_numerical_config("SCHEDULER_RW_TIMEOUT", DEFAULT_SCHEDULER_RW_TIMEOUT, is_float=True)


    logger.info("--- Scheduler Effective Configuration ---")
    logger.info(f"  Main App API URL: {MAIN_APP_API_URL} (Source: {sources.get('MAIN_APP_API_URL_DERIVED', 'unknown')})")
    logger.info(f"  Send Local Location: {SEND_LOCAL_NODE_LOCATION} (Source: {sources.get('SEND_LOCAL_NODE_LOCATION', 'unknown')})")
    logger.info(f"  Send Other Locations: {SEND_OTHER_NODES_LOCATION} (Source: {sources.get('SEND_OTHER_NODES_LOCATION', 'unknown')})")
    logger.info(f"  Community API Enabled: {COMMUNITY_API_ENABLED} (Source: {sources.get('COMMUNITY_API', 'unknown')})")
    logger.info(f"  Location Offset Enabled: {LOCATION_OFFSET_ENABLED} (Source: {sources.get('LOCATION_OFFSET_ENABLED', 'unknown')})")
    logger.info(f"  Location Offset Meters: {LOCATION_OFFSET_METERS:.1f}m (Source: {sources.get('LOCATION_OFFSET_METERS', 'unknown')})")
    logger.info(f"  Scheduler Log Level: {SCHEDULER_LOG_LEVEL_STR} (Source: {sources.get('LOG_LEVEL', 'unknown')})")
    if COMMUNITY_API_ENABLED:
        logger.info(f"  Heartbeat URL: {HEARTBEAT_REMOTE_API_URL} (Source: {sources.get('HEARTBEAT_API_URL', 'unknown')})")
        logger.info(f"  Heartbeat Interval: {HEARTBEAT_INTERVAL.total_seconds() / 60:.1f} minutes (Source: {sources.get('HEARTBEAT_INTERVAL_MINUTES', 'unknown')})")
    else:
        logger.info("  Community API Heartbeat: DISABLED (COMMUNITY_API_ENABLED is false)")
    logger.info(f"  Max Retries (Scheduler API calls): {MAX_RETRIES} (Source: {sources.get('SCHEDULER_MAX_RETRIES', 'unknown')})")
    logger.info(f"  Initial Retry Delay (Scheduler API calls): {INITIAL_RETRY_DELAY_SECONDS}s (Source: {sources.get('SCHEDULER_RETRY_DELAY_SECONDS', 'unknown')})")
    logger.info(f"  Connect Timeout (Scheduler API calls): {CONNECT_TIMEOUT}s (Source: {sources.get('SCHEDULER_CONNECT_TIMEOUT', 'unknown')})")
    logger.info(f"  Read/Write Timeout (Scheduler API calls): {READ_WRITE_TIMEOUT}s (Source: {sources.get('SCHEDULER_RW_TIMEOUT', 'unknown')})")
    logger.info("--- End Scheduler Effective Configuration ---")

# --- Utility Functions (offset_lat_lon, log_*, format_timedelta) ---
# These functions remain largely the same as in your provided code.

def offset_lat_lon(lat_deg: float, lon_deg: float, offset_meters: float) -> Tuple[float, float]:
    if offset_meters == 0:
        return lat_deg, lon_deg
    lat_rad = math.radians(lat_deg)
    lon_rad = math.radians(lon_deg)
    angular_distance = offset_meters / EARTH_RADIUS_METERS
    bearing_rad = math.radians(random.uniform(0, 360))
    new_lat_rad = math.asin(
        math.sin(lat_rad) * math.cos(angular_distance) +
        math.cos(lat_rad) * math.sin(angular_distance) * math.cos(bearing_rad)
    )
    new_lon_rad = lon_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(angular_distance) * math.cos(lat_rad),
        math.cos(angular_distance) - math.sin(lat_rad) * math.sin(new_lat_rad)
    )
    new_lat_deg = math.degrees(new_lat_rad)
    new_lon_deg = math.degrees(new_lon_rad)
    new_lon_deg = (new_lon_deg + 540) % 360 - 180
    return new_lat_deg, new_lon_deg

def log_separator(char="=", length=80): return char * length
def log_timestamp(tz: Optional[datetime.tzinfo] = None):
    effective_tz = tz or SYSTEM_TZ 
    try:
        if effective_tz:
             return datetime.datetime.now(effective_tz).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + f" ({effective_tz})"
        else: 
            return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " (Naive/No TZ)"
    except Exception: 
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " (Error determining TZ)"

def log_header(text):
    timestamp = log_timestamp()
    return f"\n{log_separator('=')}\n🕒 {timestamp} | 🔍 {text}\n{log_separator('=')}\n"

def log_subheader(text):
    timestamp = log_timestamp()
    return f"\n{log_separator('-', 70)}\n⏱️ {timestamp} | 📌 {text}\n{log_separator('-', 70)}\n"

def log_warning(text): return f"🟠 WARNING 🟠 {text}" 
def log_error(text): return f"🔴 ERROR 🔴 {text}"   

def format_timedelta(td: datetime.timedelta) -> str:
    try:
        total_seconds = int(td.total_seconds())
        is_past = total_seconds < 0
        if is_past: total_seconds = abs(total_seconds)
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        formatted = ""
        if days > 0: formatted += f"{days}d "
        if days > 0 or hours > 0: formatted += f"{hours:02d}h:"
        if days > 0 or hours > 0 or minutes > 0: formatted += f"{minutes:02d}m:"
        formatted += f"{seconds:02d}s"
        if is_past: return f"Past ({formatted.strip()} ago)"
        return formatted.strip()
    except Exception: return "Error formatting timedelta"

# --- Database Interaction ---
def get_tasks_db_conn_scheduler():
    db_file_path = os.path.join(SCHEDULER_SCRIPT_OWN_DIR, TASKS_DATABASE_FILE)
    if not os.path.exists(db_file_path):
        logger.error(log_error(f"Scheduler cannot find tasks database: {db_file_path}"))
        raise FileNotFoundError(f"Tasks database not found at {db_file_path}")
    try:
        conn = sqlite3.connect(db_file_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
        except Exception as e: logger.warning(log_warning(f"Could not set WAL mode for tasks DB: {e}"))
        return conn
    except sqlite3.Error as e:
        logger.error(log_error(f"Tasks DB connection error: {e}"))
        raise

def get_scheduled_tasks() -> list[Dict[str, Any]]:
    tasks = []
    conn = None
    try:
        conn = get_tasks_db_conn_scheduler()
        cursor = conn.cursor()
        cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString FROM tasks WHERE enabled = TRUE")
        rows = cursor.fetchall()
        tasks = [dict(row) for row in rows]
    except FileNotFoundError: logger.info("Tasks database file not found, assuming no tasks.")
    except sqlite3.OperationalError as e:
        if "no such column: enabled" in str(e):
            logger.warning(log_warning("Tasks table missing 'enabled' column. Fetching all tasks. Consider updating schema."))
            if conn: 
                cursor = conn.cursor()
                cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString FROM tasks")
                tasks = [dict(row) for row in cursor.fetchall()]
        else: logger.error(log_error(f"SQLite error fetching tasks: {e}"), exc_info=True)
    except Exception as e: logger.error(log_error(f"Error fetching tasks: {e}"), exc_info=True)
    finally:
        if conn: conn.close()
    return tasks


# --- Task and Heartbeat Actions (largely unchanged but use reloaded globals) ---
async def trigger_send_message_action(client: httpx.AsyncClient, task: Dict[str, Any]):
    node_id = task.get('nodeId')
    payload_text = task.get('actionPayload')
    task_id = task.get('id', 'N/A')

    if not node_id or payload_text is None: 
        detail = f"Task ID {task_id}: "
        if not node_id: detail += "missing nodeId. "
        if payload_text is None: detail += "missing actionPayload."
        logger.warning(log_warning(detail + " Skipping task."))
        return False

    try:
        payload_data = json.loads(payload_text)
        is_json = True
    except (json.JSONDecodeError, TypeError):
        is_json = False
        payload_data = {} 

    message_data: Dict[str, Any]
    api_endpoint_url: str
    base_url = MAIN_APP_API_URL 

    if is_json:
        if 'url' in payload_data and 'block_id' in payload_data and 'prefix' in payload_data:
            api_endpoint_url = f"{base_url}/monitor/website" 
            payload_data['node_id'] = node_id 
            message_data = payload_data
            logger.debug(f"Task {task_id}: Identified as Website Monitor task.")
        else: 
            api_endpoint_url = f"{base_url}/api/messages" 
            if 'message' not in payload_data:
                logger.warning(log_warning(f"Task {task_id}: JSON payload for standard message missing 'message' key. Payload: {payload_text}. Skipping task."))
                return False
            payload_data['destination'] = node_id
            message_data = payload_data
            logger.debug(f"Task {task_id}: Identified as Standard Message (JSON payload) task.")
    else: 
        api_endpoint_url = f"{base_url}/api/messages" 
        message_data = {"message": payload_text, "destination": node_id}
        logger.debug(f"Task {task_id}: Identified as Standard Message (Plain Text) task.")

    logger.info(log_header(f"TASK ACTION Task {task_id} → POST {api_endpoint_url}"))
    try:
        logger.info(f"Payload: {json.dumps(message_data, indent=2)}")
    except Exception: 
        logger.info(f"Payload (raw, type: {type(message_data)}): {message_data}")
    
    last_exception = None
    for attempt in range(MAX_RETRIES):
        current_delay = INITIAL_RETRY_DELAY_SECONDS * (2 ** attempt) 
        if attempt > 0:
            logger.info(f"Retrying task action {task_id} (Attempt {attempt + 1}/{MAX_RETRIES}). Waiting {current_delay:.1f}s...")
            await asyncio.sleep(current_delay)
        try:
            start = datetime.datetime.now()
            response = await client.post(api_endpoint_url, json=message_data)
            elapsed = (datetime.datetime.now() - start).total_seconds()
            logger.info(f"Response {response.status_code} in {elapsed:.3f}s (Attempt {attempt + 1}/{MAX_RETRIES})")

            if 200 <= response.status_code < 300:
                if logger.isEnabledFor(logging.DEBUG):
                    try: logger.debug(f"Response Body: {json.dumps(response.json(), indent=2)}")
                    except Exception: logger.debug(f"Response Body (non-JSON): {response.text[:200]}")
                return True 
            else: 
                response_body_log = response.text[:500] 
                try: 
                    body_json = response.json()
                    response_body_log = json.dumps(body_json, indent=2)
                    if 'detail' in body_json:
                        logger.error(log_error(f"Task {task_id} API Error Detail: {body_json['detail']}"))
                        response_body_log = None 
                except Exception: pass 
                
                logger.error(log_error(f"Task {task_id} API Error ({response.status_code}). Body: {response_body_log or response.text[:500]}"))
                if response.status_code in [408, 429] or response.status_code >= 500: 
                    last_exception = httpx.HTTPStatusError(f"Status code {response.status_code}", request=response.request, response=response)
                    continue 
                else: 
                    logger.warning(f"Task {task_id}: Non-retriable HTTP status {response.status_code}. Failing task action.")
                    return False 
        except httpx.TimeoutException as e:
            logger.error(log_error(f"Task {task_id} Timeout error (Attempt {attempt + 1}/{MAX_RETRIES}): {e} (URL: {e.request.url if e.request else 'N/A'})"))
            last_exception = e
        except httpx.RequestError as e: 
            logger.error(log_error(f"Task {task_id} HTTP Request error (Attempt {attempt + 1}/{MAX_RETRIES}): {e} (URL: {e.request.url if e.request else 'N/A'})"))
            last_exception = e
        except Exception as e: 
            logger.error(log_error(f"Task {task_id} generic error during API call (Attempt {attempt + 1}/{MAX_RETRIES}): {e}"), exc_info=True)
            last_exception = e 
    
    logger.error(log_error(f"Task {task_id} action failed after {MAX_RETRIES} attempts. Last error: {last_exception}"))
    return False

async def trigger_task_action(client: httpx.AsyncClient, task: Dict[str, Any]):
    ttype = task.get('taskType', '').lower()
    task_id = task.get('id', 'N/A')
    logger.info(log_subheader(f"INITIATING ACTION for Task {task_id} (type: {ttype})"))
    action_successful = False
    if ttype in ['sendmessage', 'message', 'send', 'sendmsg', 'website_monitor', 'websitemonitor']:
        action_successful = await trigger_send_message_action(client, task)
    else:
        logger.warning(log_warning(f"Unknown or unhandled taskType '{task.get('taskType')}' for task {task_id}"))
        action_successful = False 

    if action_successful:
        logger.info(f"✅ Task {task_id} action completed successfully (including retries if any).")
    else:
        logger.error(log_error(f"❌ Task {task_id} action failed after all attempts or was unhandled."))
    return action_successful

async def send_heartbeat(client: httpx.AsyncClient):
    status_data, stats_data, all_nodes_data = None, None, None
    fetch_errors: List[str] = [] 
    processed_other_nodes_data: List[Dict[str, Any]] = [] 
    local_node_id = None
    location_disabled_placeholder = "Location Disabled"

    base_url = MAIN_APP_API_URL 
    status_url, stats_url, nodes_url = f"{base_url}/api/status", f"{base_url}/api/stats", f"{base_url}/api/nodes"
    logger.info(log_subheader("Starting Heartbeat Process"))
    logger.debug(f"Heartbeat: Fetching data from {status_url}, {stats_url}, {nodes_url}")

    async def fetch_with_retry(url: str, timeout_val: float = 15.0) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        last_err = None
        for attempt in range(MAX_RETRIES):
            if attempt > 0: await asyncio.sleep(INITIAL_RETRY_DELAY_SECONDS * (2 ** attempt))
            try:
                resp = await client.get(url, timeout=timeout_val)
                if 200 <= resp.status_code < 300: return resp.json(), None
                last_err = httpx.HTTPStatusError(f"Status {resp.status_code}", request=resp.request, response=resp)
                logger.warning(log_warning(f"Heartbeat: Failed fetch {url.split('/')[-1]} (Attempt {attempt+1}): {resp.status_code}"))
            except Exception as e: 
                last_err = e
                logger.warning(log_warning(f"Heartbeat: Error fetching {url.split('/')[-1]} (Attempt {attempt+1}): {e}"))
        
        err_msg = f"Failed to fetch {url.split('/')[-1]} after {MAX_RETRIES} attempts. Last error: {last_err}"
        logger.error(log_error(err_msg))
        return None, err_msg

    status_data, status_err = await fetch_with_retry(status_url)
    if status_err: fetch_errors.append(status_err)
    if status_data and isinstance(status_data.get("local_node_info"), dict):
        local_node_id = status_data["local_node_info"].get("node_id")

    stats_data, stats_err = await fetch_with_retry(stats_url)
    if stats_err: fetch_errors.append(stats_err)

    all_nodes_data_raw, nodes_err = await fetch_with_retry(nodes_url, timeout_val=20.0) 
    if nodes_err: fetch_errors.append(nodes_err)
    if isinstance(all_nodes_data_raw, dict): 
        all_nodes_data = all_nodes_data_raw
    elif all_nodes_data_raw is not None: 
        logger.warning(log_warning(f"Data from {nodes_url} was not a dictionary as expected. Type: {type(all_nodes_data_raw)}"))
        fetch_errors.append(f"Invalid data format from {nodes_url}")

    logger.debug("Heartbeat: Processing fetched data...")
    processed_local_node_info = status_data.get('local_node_info') if status_data and isinstance(status_data.get('local_node_info'), dict) else {} 

    def _process_node_location(node_dict: Dict[str, Any], node_id_str: str, is_local: bool):
        should_send_location = SEND_LOCAL_NODE_LOCATION if is_local else SEND_OTHER_NODES_LOCATION
        if not should_send_location:
            logger.debug(f"Heartbeat: Location sending for {'local' if is_local else 'other'} node {node_id_str} DISABLED by config. Anonymizing.")
            node_dict['latitude'] = location_disabled_placeholder
            node_dict['longitude'] = location_disabled_placeholder
            if 'position' in node_dict and isinstance(node_dict['position'], dict):
                node_dict['position']['latitude'] = location_disabled_placeholder
                node_dict['position']['longitude'] = location_disabled_placeholder
                node_dict['position']['latitudeI'] = None 
                node_dict['position']['longitudeI'] = None
        elif LOCATION_OFFSET_ENABLED and LOCATION_OFFSET_METERS > 0:
            current_lat = node_dict.get('latitude')
            current_lon = node_dict.get('longitude')
            if isinstance(current_lat, (float, int)) and isinstance(current_lon, (float, int)):
                try:
                    offset_lat, offset_lon = offset_lat_lon(float(current_lat), float(current_lon), LOCATION_OFFSET_METERS)
                    logger.debug(f"Heartbeat: Offsetting {'local' if is_local else 'other'} node {node_id_str} location by {LOCATION_OFFSET_METERS:.1f}m: "
                                 f"Original ({current_lat:.6f}, {current_lon:.6f}) -> Offset ({offset_lat:.6f}, {offset_lon:.6f})")
                    node_dict['latitude'] = offset_lat
                    node_dict['longitude'] = offset_lon
                    if 'position' in node_dict and isinstance(node_dict['position'], dict):
                        node_dict['position']['latitude'] = offset_lat
                        node_dict['position']['longitude'] = offset_lon
                        node_dict['position']['latitudeI'] = None 
                        node_dict['position']['longitudeI'] = None
                except Exception as e:
                    logger.warning(f"Heartbeat: Error offsetting {'local' if is_local else 'other'} node {node_id_str} location: {e}")
            else:
                 logger.debug(f"Heartbeat: Node {node_id_str} has no valid numeric lat/lon to offset. Location data: Lat='{current_lat}', Lon='{current_lon}'")
        return node_dict 

    if processed_local_node_info and local_node_id: 
        processed_local_node_info = _process_node_location(processed_local_node_info, local_node_id, is_local=True)

    if isinstance(all_nodes_data, dict):
        logger.debug(f"Heartbeat: Processing {len(all_nodes_data)} nodes for 'other_nodes_data'.")
        for node_id_key, node_data_val in all_nodes_data.items():
            if node_id_key == local_node_id: continue 
            if isinstance(node_data_val, dict):
                processed_node_item = node_data_val.copy() 
                processed_node_item = _process_node_location(processed_node_item, node_id_key, is_local=False)
                processed_other_nodes_data.append(processed_node_item)
            else:
                logger.warning(f"Heartbeat: Skipping invalid node data entry for {node_id_key} from /api/nodes. Data: {node_data_val}")
    else:
        logger.debug("Heartbeat: No data from /api/nodes or data is not a dict, cannot process 'other_nodes_data'.")

    payload = {
        "type": "heartbeat_v2",
        "fetched_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "local_node_info": processed_local_node_info if processed_local_node_info else None, 
        "stats_data": stats_data, 
        "other_nodes_data": processed_other_nodes_data, 
        "fetch_errors": fetch_errors if fetch_errors else None 
    }

    logger.info(f"Heartbeat: Sending payload to remote API: {HEARTBEAT_REMOTE_API_URL}")
    last_remote_exception = None
    for attempt in range(MAX_RETRIES): 
        current_delay = INITIAL_RETRY_DELAY_SECONDS * (2 ** attempt)
        if attempt > 0:
            logger.info(f"Retrying remote heartbeat send (Attempt {attempt + 1}/{MAX_RETRIES}). Waiting {current_delay:.1f}s...")
            await asyncio.sleep(current_delay)
        try:
            if logger.isEnabledFor(logging.DEBUG):
                try: payload_log = json.dumps(payload, indent=2, default=str) 
                except Exception as json_err: payload_log = f"Could not serialize payload for logging: {json_err}\nRaw: {str(payload)[:500]}"
                logger.debug(f"Heartbeat: Sending Payload to {HEARTBEAT_REMOTE_API_URL}:\n{payload_log}")

            start_time_hb_send = datetime.datetime.now()
            response = await client.post(HEARTBEAT_REMOTE_API_URL, json=payload)
            elapsed = (datetime.datetime.now() - start_time_hb_send).total_seconds()
            logger.info(f"Remote Heartbeat Response {response.status_code} in {elapsed:.3f}s (Attempt {attempt + 1}/{MAX_RETRIES})")
            if 200 <= response.status_code < 300:
                if logger.isEnabledFor(logging.DEBUG): logger.debug(f"Heartbeat sent successfully to {HEARTBEAT_REMOTE_API_URL}.")
                return True 
            else:
                response_body_log = response.text[:500]
                try: response_body_log = json.dumps(response.json(), indent=2)
                except Exception: pass
                logger.error(log_error(f"Remote Heartbeat API Error ({response.status_code}): {response_body_log}"))
                if response.status_code in [408, 429] or response.status_code >= 500:
                    last_remote_exception = httpx.HTTPStatusError(f"Status {response.status_code}", request=response.request, response=response)
                    continue
                else:
                    logger.warning(f"Remote Heartbeat: Non-retriable HTTP status {response.status_code}. Failing heartbeat send.")
                    return False
        except httpx.TimeoutException as e:
            logger.error(log_error(f"Heartbeat Timeout error sending to REMOTE API ({HEARTBEAT_REMOTE_API_URL}) (Attempt {attempt + 1}/{MAX_RETRIES}): {e}"))
            last_remote_exception = e
        except httpx.RequestError as e:
            logger.error(log_error(f"Heartbeat HTTP Request error sending to REMOTE API ({HEARTBEAT_REMOTE_API_URL}) (Attempt {attempt + 1}/{MAX_RETRIES}): {e}"))
            last_remote_exception = e
        except Exception as e:
            logger.error(log_error(f"Heartbeat generic error during REMOTE API call to {HEARTBEAT_REMOTE_API_URL} (Attempt {attempt + 1}/{MAX_RETRIES}): {e}"), exc_info=True)
            last_remote_exception = e
    
    logger.error(log_error(f"Heartbeat send failed after {MAX_RETRIES} attempts. Last error: {last_remote_exception}"))
    return False

async def check_and_trigger_tasks(client: httpx.AsyncClient, system_tz_val: datetime.tzinfo, last_check_time_utc_val: datetime.datetime):
    current_check_time_local_for_display = datetime.datetime.now(system_tz_val)
    current_check_time_utc_val = current_check_time_local_for_display.astimezone(datetime.timezone.utc)
    last_check_time_local_for_croniter = last_check_time_utc_val.astimezone(system_tz_val)

    logger.info(log_header(f"TASK CHECK @ {current_check_time_local_for_display.isoformat()}"))
    logger.info(f"System Timezone (for interpreting cron H:M): {system_tz_val}")
    logger.debug(f"Croniter start_time reference (Scheduler Local): {last_check_time_local_for_croniter.isoformat()}")
    logger.debug(f"Checking for tasks due between (UTC): {last_check_time_utc_val.isoformat()} < T <= {current_check_time_utc_val.isoformat()}")

    tasks = get_scheduled_tasks() 
    logger.info(f"Found {len(tasks)} enabled tasks in database.")
    triggered_count = 0

    if not tasks:
        logger.info("No enabled tasks found to check.")
        return triggered_count, current_check_time_utc_val 

    for task in tasks:
        cron_str = task.get('cronString')
        task_id = task.get('id', 'N/A')
        task_type = task.get('taskType', 'N/A')
        node_id_val = task.get('nodeId', 'N/A')
        logger.info(log_subheader(f"Evaluating Task {task_id} | Type: {task_type} | Node: {node_id_val} | Schedule: '{cron_str}'"))

        if not cron_str:
            logger.warning(log_warning(f"Skipping task {task_id}: No cronString defined."))
            continue
        try:
            itr = croniter(cron_str, start_time=last_check_time_local_for_croniter, ret_type=datetime.datetime, is_prev=False)
            next_scheduled_time_local = itr.get_next() 

            if next_scheduled_time_local.tzinfo is None and system_tz_val: 
                logger.debug(f"Croniter returned naive datetime for task {task_id}. Making it aware with {system_tz_val}.")
                next_scheduled_time_local = next_scheduled_time_local.replace(tzinfo=system_tz_val) 
            
            next_scheduled_time_utc = next_scheduled_time_local.astimezone(datetime.timezone.utc)
            time_until_run = next_scheduled_time_utc - current_check_time_utc_val

            if logger.isEnabledFor(logging.DEBUG): 
                logger.debug(f"  < Last Check (Local for Cron) : {last_check_time_local_for_croniter.isoformat()}")
                logger.debug(f"  ? Next Run   (Interpreted Local): {next_scheduled_time_local.isoformat()}")
                logger.debug(f"  ? Next Run   (Converted UTC)   : {next_scheduled_time_utc.isoformat()}")
                logger.debug(f"  <= Now        (UTC)             : {current_check_time_utc_val.isoformat()}")
                logger.debug(f"  ⏳ Time Until Next             : {format_timedelta(time_until_run)}")
            else: 
                 logger.info(f"  Next scheduled run for task {task_id} (Local Time): {next_scheduled_time_local.strftime('%Y-%m-%d %H:%M:%S %Z')}")

            if last_check_time_utc_val < next_scheduled_time_utc <= current_check_time_utc_val:
                logger.info(f"✅ Task {task_id} is DUE (Scheduled Local: {next_scheduled_time_local.isoformat()}, which is {next_scheduled_time_utc.isoformat()} UTC). Triggering action...")
                success = await trigger_task_action(client, task) 
                if success: triggered_count += 1
            elif logger.isEnabledFor(logging.DEBUG): 
                logger.debug(f"Task {task_id} is not due in this interval.")

        except ValueError as e: logger.error(log_error(f"Cron format error for task {task_id} ('{cron_str}'): {e}"), exc_info=False)
        except Exception as e: logger.error(log_error(f"Unexpected Cron error for task {task_id} ('{cron_str}'): {e}"), exc_info=True)
    
    logger.info(f"Task check complete. Triggered {triggered_count} tasks this check cycle.")
    return triggered_count, current_check_time_utc_val 


async def run_scheduler_periodically():
    load_scheduler_configuration(CONFIG_FILE_PATH) 

    logger.info(log_header("STARTING SCHEDULER (Cron H:M interpreted in Scheduler's Local Timezone)"))

    if not SYSTEM_TZ: 
        logger.critical(log_error("CRITICAL: SYSTEM_TZ is not defined. Scheduler cannot determine local timezone for cron interpretation. Exiting."))
        return
    
    logger.info(f"Detected System Timezone        : {SYSTEM_TZ} (Current Offset: {datetime.datetime.now(SYSTEM_TZ).strftime('%z') if SYSTEM_TZ else 'N/A'})")
    logger.info(f"Scheduler will interpret cron H:M components in this Timezone.")
    logger.info(f"Instance offset approx          : {INSTANCE_SECOND_OFFSET:.2f} seconds past the minute.")
    logger.info(log_separator('-'))
    
    initial_jitter_val = random.uniform(0.5, 5.0)
    logger.info(f"Applying initial startup delay of {initial_jitter_val:.2f} seconds...")
    await asyncio.sleep(initial_jitter_val)

    last_heartbeat_time_utc = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    last_check_time_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    logger.info(f"Initial last_check_time_utc set to: {last_check_time_utc.isoformat()}")

    config_reload_error_logged_this_session = False

    while True:
        tasks_triggered_this_cycle = 0
        heartbeat_action_taken_this_cycle = "Not Applicable"
        actual_check_time_utc_for_this_cycle = datetime.datetime.now(datetime.timezone.utc) 

        try:
            try:
                logger.debug("Scheduler loop: Attempting to reload configuration...")
                load_scheduler_configuration(CONFIG_FILE_PATH) 
                if config_reload_error_logged_this_session:
                    logger.info("Successfully reloaded scheduler configuration after previous error.")
                    config_reload_error_logged_this_session = False
            except Exception as cfg_load_err:
                if not config_reload_error_logged_this_session:
                    logger.error(f"Failed to reload scheduler configuration in loop: {cfg_load_err}. Using last known configuration values.", exc_info=True)
                    config_reload_error_logged_this_session = True
            
            current_timeout_config = httpx.Timeout(READ_WRITE_TIMEOUT, connect=CONNECT_TIMEOUT)
            async with httpx.AsyncClient(timeout=current_timeout_config) as client_for_this_cycle:

                now_naive_for_calc = datetime.datetime.now() 
                target_second_of_minute = int(INSTANCE_SECOND_OFFSET) 
                target_microsecond_of_second = int((INSTANCE_SECOND_OFFSET - target_second_of_minute) * 1_000_000)
                
                next_potential_target_naive = now_naive_for_calc.replace(
                    second=target_second_of_minute, 
                    microsecond=target_microsecond_of_second, 
                    tzinfo=None 
                )
                
                if next_potential_target_naive <= now_naive_for_calc:
                    target_time_naive = next_potential_target_naive + datetime.timedelta(minutes=1)
                else:
                    target_time_naive = next_potential_target_naive
                
                wait_duration_seconds = (target_time_naive - now_naive_for_calc).total_seconds()

                if wait_duration_seconds < 0: 
                    logger.warning(f"Calculated negative wait duration ({wait_duration_seconds:.3f}s). Defaulting to 0.1s.")
                    wait_duration_seconds = 0.1
                
                if logger.isEnabledFor(logging.DEBUG) or wait_duration_seconds > 5: 
                    if SYSTEM_TZ:
                        target_local_for_log = target_time_naive.replace(tzinfo=SYSTEM_TZ).strftime('%H:%M:%S.%f')[:-3]
                    else: 
                        target_local_for_log = target_time_naive.strftime('%H:%M:%S.%f')[:-3] + " (Naive Target/No SYSTEM_TZ)"
                    logger.info(f"--- Waiting {wait_duration_seconds:.3f}s until next check cycle (approx {target_local_for_log}) ---")
                
                await asyncio.sleep(wait_duration_seconds)

                actual_check_time_local = datetime.datetime.now(SYSTEM_TZ) 
                actual_check_time_utc_for_this_cycle = actual_check_time_local.astimezone(datetime.timezone.utc) 
                
                logger.info(log_header(f"Check Cycle Start @ {actual_check_time_local.isoformat()}"))
                logger.debug(f"(Target Offset: {INSTANCE_SECOND_OFFSET:.2f}s past minute | Actual UTC: {actual_check_time_utc_for_this_cycle.isoformat()})")
                
                time_since_last_heartbeat = actual_check_time_utc_for_this_cycle - last_heartbeat_time_utc
                if COMMUNITY_API_ENABLED:
                    heartbeat_action_taken_this_cycle = "Skipped (Interval)" 
                    if time_since_last_heartbeat >= HEARTBEAT_INTERVAL:
                        logger.info(log_subheader("Attempting Heartbeat Send (Community API Enabled)..."))
                        success = await send_heartbeat(client_for_this_cycle)
                        heartbeat_action_taken_this_cycle = "Attempted/Sent"
                        if success:
                            last_heartbeat_time_utc = actual_check_time_utc_for_this_cycle 
                            logger.info("✅ Heartbeat send attempt completed successfully.")
                        else:
                            logger.warning(log_warning("Heartbeat send attempt failed after retries. Will try again next interval."))
                    elif logger.isEnabledFor(logging.DEBUG):
                         logger.debug(f"Skipping heartbeat send. Time since last: {format_timedelta(time_since_last_heartbeat)} (Interval: {format_timedelta(HEARTBEAT_INTERVAL)})")
                else:
                    heartbeat_action_taken_this_cycle = "Disabled (Community API)"
                    logger.info("Community API reporting is DISABLED by config. Skipping heartbeat send.")

                tasks_triggered_this_cycle, _ = await check_and_trigger_tasks(
                    client_for_this_cycle, 
                    SYSTEM_TZ, 
                    last_check_time_utc 
                )
                
                last_check_time_utc = actual_check_time_utc_for_this_cycle

                summary_time_local = datetime.datetime.now(SYSTEM_TZ)
                cycle_duration = (summary_time_local - actual_check_time_local).total_seconds()
                log_msg = f"\n{log_separator('*', 70)}\n📊 Check Cycle COMPLETE @ {summary_time_local.isoformat()}\n"
                log_msg += f"   Duration: {cycle_duration:.3f}s | Tasks Triggered: {tasks_triggered_this_cycle} | Heartbeat Action: {heartbeat_action_taken_this_cycle}\n"
                log_msg += f"{log_separator('*', 70)}\n"
                logger.info(log_msg)
            
        except asyncio.CancelledError:
            logger.info(log_subheader("SCHEDULER RECEIVED CANCELLATION REQUEST"))
            break
        except FileNotFoundError as e:
            logger.critical(log_error(f"CRITICAL: Database file not found: {e}. Stopping scheduler."), exc_info=True)
            break 
        except httpx.ConnectError as e:
            logger.error(log_error(f"Scheduler cannot connect to main app API ({MAIN_APP_API_URL}): {e}"), exc_info=False) 
            logger.info("Will retry connection to main app API in the next cycle (approx 1 min).")
            await asyncio.sleep(60) 
            last_check_time_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1) 

        except Exception as e:
            logger.error(log_error(f"Unexpected error in scheduler main loop: {e}"), exc_info=True)
            logger.info("Will attempt to recover and continue in the next cycle (approx 1 min).")
            await asyncio.sleep(60) 
            last_check_time_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)

    logger.info(log_header("SCHEDULER STOPPED"))

if __name__ == '__main__':
    if not logging.getLogger("meshtastic_dashboard.scheduler").hasHandlers():
        _shandler = logging.StreamHandler(sys.stdout)
        _sformatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - [%(module)s.%(funcName)s:%(lineno)d] %(message)s')
        _shandler.setFormatter(_sformatter)
        logger.addHandler(_shandler)
        _slevel_str = os.environ.get("SCHEDULER_LOG_LEVEL", os.environ.get("LOG_LEVEL", "INFO")).upper()
        logger.setLevel(getattr(logging, _slevel_str, logging.INFO))
        logger.info("Scheduler logger initialized for standalone execution.")
    
    logger.info(f"Scheduler starting directly (invoked via __main__)...")
    try:
        asyncio.run(run_scheduler_periodically())
    except KeyboardInterrupt:
        logger.info(log_subheader("SCHEDULER INTERRUPTED BY USER (KeyboardInterrupt)"))
    except Exception as main_err:
        logger.critical(log_error(f"Scheduler failed during critical execution phase: {main_err}"), exc_info=True)
        sys.exit(1)

