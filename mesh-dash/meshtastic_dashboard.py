import asyncio
import json
import logging
import time
import base64
import os
import sys
import httpx
import sqlite3
import re 

from typing import Dict, List, Optional, Set, Any, Union, Tuple, AsyncGenerator, Literal
from datetime import datetime, timedelta, timezone 
from collections import deque
import statistics
from contextlib import asynccontextmanager
import requests 
from bs4 import BeautifulSoup 

import meshtastic
import meshtastic.tcp_interface
from pubsub import pub

from fastapi import FastAPI, Response, HTTPException, Query, Path, status, Request, APIRouter, Depends, Form 
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, PlainTextResponse, RedirectResponse 
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from jose import JWTError, jwt
from passlib.context import CryptContext

try:

    from pydantic import BaseModel as PydanticBaseModel, Field, field_validator, model_validator
    PYDANTIC_V2 = True
    try:
        from pydantic import validator as pydantic_validator_v1
    except ImportError:
        pydantic_validator_v1 = None 
except ImportError:

    from pydantic import BaseModel as PydanticBaseModel, Field, validator as pydantic_validator_v1
    PYDANTIC_V2 = False
    field_validator = None 
    model_validator = None
import uvicorn
from sse_starlette.sse import EventSourceResponse

try:
    from tasks_api import tasks_router, init_tasks_db
except ImportError as import_err:

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s", datefmt='%Y-%m-%d %H:%M:%S')
    logging.critical(f"FATAL: Could not import from tasks_api.py. Error: {import_err}", exc_info=True)
    sys.exit(1)
try:
    from task_scheduler import run_scheduler_periodically
except ImportError as import_err:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s", datefmt='%Y-%m-%d %H:%M:%S')
    logging.critical(f"FATAL: Could not import from task_scheduler.py. Error: {import_err}", exc_info=True)
    sys.exit(1)

try:

    from auto_reply_api import auto_reply_router

    from auto_reply import init_auto_reply_db, db_get_auto_reply_rules, check_message_for_auto_reply
    AUTO_REPLY_ENABLED = True
except ImportError as import_err:
    if not logging.getLogger().hasHandlers(): 
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s", datefmt='%Y-%m-%d %H:%M:%S')
    logging.error(f"Could not import auto-reply functionality (auto_reply.py or auto_reply_api.py missing/error): {import_err}", exc_info=False)
    AUTO_REPLY_ENABLED = False
    auto_reply_router = APIRouter()
    init_auto_reply_db = lambda: None 
    db_get_auto_reply_rules = lambda only_enabled=True: [] 
    check_message_for_auto_reply = lambda *args: [] 

DEFAULT_TARGET_HOST = "192.168.0.0" 
DEFAULT_TARGET_PORT = 4403
DEFAULT_LOG_LEVEL_STR = "INFO" 
DEFAULT_WEBSERVER_PORT = 8000
DEFAULT_WEBSERVER_HOST = "0.0.0.0"
DEFAULT_DB_PATH = "meshtastic_data.db"
DEFAULT_MAX_PACKETS_MEMORY = 200
DEFAULT_AVERAGE_METRICS_HISTORY_DAYS = 30
DEFAULT_EXTERNAL_MAP_API_BASE_URL = "https://meshdash.co.uk/com_api.php"
CONFIG_FILE_NAME = ".mesh-dash_config"

DEFAULT_AUTH_SECRET_KEY = "YOUR_VERY_SECRET_KEY_CHANGE_THIS_IN_CONFIG_FILE" 
DEFAULT_AUTH_TOKEN_EXPIRE_MINUTES = 30

try:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    SCRIPT_DIR = os.getcwd()
CONFIG_FILE_PATH = os.path.join(SCRIPT_DIR, CONFIG_FILE_NAME)
LOGIN_HTML_PATH = os.path.join(SCRIPT_DIR, "static", "login.html")

def load_configuration(config_path: str) -> Dict[str, Any]:
    config = {
        "MESHTASTIC_HOST": DEFAULT_TARGET_HOST,
        "MESHTASTIC_PORT": DEFAULT_TARGET_PORT,
        "LOG_LEVEL": DEFAULT_LOG_LEVEL_STR,
        "WEBSERVER_PORT": DEFAULT_WEBSERVER_PORT,
        "WEBSERVER_HOST": DEFAULT_WEBSERVER_HOST,
        "DB_PATH": DEFAULT_DB_PATH,
        "MAX_PACKETS_MEMORY": DEFAULT_MAX_PACKETS_MEMORY,
        "HISTORY_DAYS": DEFAULT_AVERAGE_METRICS_HISTORY_DAYS,
        "EXTERNAL_MAP_API_BASE_URL": DEFAULT_EXTERNAL_MAP_API_BASE_URL,
        "SEND_LOCAL_NODE_LOCATION": "true",
        "SEND_OTHER_NODES_LOCATION": "true",
        "INSTALLED_VERSION": "N/A",
        "AUTH_SECRET_KEY": DEFAULT_AUTH_SECRET_KEY,
        "AUTH_TOKEN_EXPIRE_MINUTES": DEFAULT_AUTH_TOKEN_EXPIRE_MINUTES,
        "INITIAL_ADMIN_USERNAME": None, 
        "INITIAL_ADMIN_PASSWORD": None, 
    }
    temp_logger = logging.getLogger("meshtastic_dashboard_config_loader")
    if not temp_logger.hasHandlers(): 
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s", datefmt='%Y-%m-%d %H:%M:%S')
        handler.setFormatter(formatter)
        temp_logger.addHandler(handler)
        temp_logger.setLevel(logging.INFO) 

    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                for line_number, line in enumerate(f, 1):
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        if (value.startswith('"') and value.endswith('"')) or \
                           (value.startswith("'") and value.endswith("'")):
                            value = value[1:-1]

                        if key in config:
                            default_val_type = type(config[key])
                            try:
                                if default_val_type == int:
                                    config[key] = int(value)
                                elif default_val_type == float:
                                    config[key] = float(value)
                                elif default_val_type == bool: 
                                    config[key] = value.lower() in ('true', '1', 't', 'yes', 'y')
                                else: 
                                    config[key] = value
                            except ValueError:
                                temp_logger.warning(f"Config (line {line_number}): Could not convert value '{value}' for key '{key}' to {default_val_type}. Using string.")
                                config[key] = value
                        else:
                            temp_logger.info(f"Config (line {line_number}): Ignoring unknown key '{key}'.")
            temp_logger.info(f"Successfully loaded configuration from: {config_path}")
        except Exception as e:
            temp_logger.error(f"Failed to read or parse config file {config_path}: {e}. Using defaults.", exc_info=True)
    else:
        temp_logger.info(f"Configuration file not found at {config_path}. Using default settings.")

    if 'handler' in locals() and temp_logger.hasHandlers(): 
        temp_logger.removeHandler(handler)

    return config

loaded_config = load_configuration(CONFIG_FILE_PATH)

TARGET_HOST = loaded_config["MESHTASTIC_HOST"]
TARGET_PORT = int(loaded_config["MESHTASTIC_PORT"])
LOG_LEVEL_STR = loaded_config["LOG_LEVEL"].upper()
WEBSERVER_PORT = int(loaded_config["WEBSERVER_PORT"])
WEBSERVER_HOST = loaded_config["WEBSERVER_HOST"]
DB_PATH = loaded_config["DB_PATH"]
MAX_PACKETS_IN_MEMORY = int(loaded_config["MAX_PACKETS_MEMORY"])
AVERAGE_METRICS_HISTORY_DAYS = int(loaded_config["HISTORY_DAYS"])
EXTERNAL_MAP_API_BASE_URL = loaded_config["EXTERNAL_MAP_API_BASE_URL"]
AUTH_SECRET_KEY = loaded_config["AUTH_SECRET_KEY"]
AUTH_TOKEN_EXPIRE_MINUTES = int(loaded_config["AUTH_TOKEN_EXPIRE_MINUTES"])

if AUTH_SECRET_KEY == DEFAULT_AUTH_SECRET_KEY or not AUTH_SECRET_KEY:
    logging.warning("SECURITY WARNING: AUTH_SECRET_KEY is using the default placeholder or is empty. "
                    "Please set a strong, unique secret in your configuration file (.mesh-dash_config) or environment.")
    if not AUTH_SECRET_KEY: 
        AUTH_SECRET_KEY = "TEMPORARY_INSECURE_KEY_CHANGE_IMMEDIATELY" 
        logging.warning("AUTH_SECRET_KEY was empty, using a temporary insecure key. THIS IS NOT SAFE FOR PRODUCTION.")

LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO) 

logging.basicConfig(
    level=LOG_LEVEL, 
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("meshtastic_dashboard") 

logger.info(f"--- Starting Meshtastic Dashboard ---")
logger.info(f"Using Pydantic V{2 if PYDANTIC_V2 else 1}.")
logger.info(f"Log level set to: {LOG_LEVEL_STR} (Effective Level: {logging.getLevelName(LOG_LEVEL)})")
logger.info(f"Meshtastic Target: tcp://{TARGET_HOST}:{TARGET_PORT}") 
logger.info(f"Web Server: http://{WEBSERVER_HOST}:{WEBSERVER_PORT}") 
logger.info(f"Database Path: {DB_PATH}") 
logger.info(f"Max packets in memory: {MAX_PACKETS_IN_MEMORY}")
logger.info(f"Average metrics history days: {AVERAGE_METRICS_HISTORY_DAYS}")
logger.info(f"Authentication Token Expire Minutes: {AUTH_TOKEN_EXPIRE_MINUTES}")

if AUTO_REPLY_ENABLED:
    logger.info("Auto-Reply feature is ENABLED.")
else:
    logger.warning("Auto-Reply feature is DISABLED due to import errors.")

if LOG_LEVEL > logging.DEBUG:
    for log_name in ["meshtastic", "pubsub", "bleak", "watchfiles", "uvicorn.access", "meshtastic_dashboard.tasks", "httpx", "jose", "passlib"]:
        logging.getLogger(log_name).setLevel(logging.WARNING)
else:
    logging.getLogger("meshtastic").setLevel(logging.DEBUG)
    for log_name in ["pubsub", "bleak", "watchfiles", "uvicorn.access", "httpx", "jose", "passlib"]:
        logging.getLogger(log_name).setLevel(logging.INFO)
    logging.getLogger("meshtastic_dashboard.tasks").setLevel(logging.DEBUG)

try:
    from system import config_router, ABS_DASH_CONFIG_PATH 
    SYSTEM_CONFIG_ENABLED = True
    logger.info(f"Successfully imported System Config API. Using config file: {ABS_DASH_CONFIG_PATH}")
except ImportError as import_err:
    logger.info(f"System Config API feature disabled (system.py missing or import error): {import_err}", exc_info=False)
    SYSTEM_CONFIG_ENABLED = False
    config_router = None 
    ABS_DASH_CONFIG_PATH = "N/A (module not loaded)"

class User(PydanticBaseModel):
    username: str
    disabled: Optional[bool] = None

class UserInDB(User):
    hashed_password: str

class TokenData(PydanticBaseModel):
    username: Optional[str] = None

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256" 

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=AUTH_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    encoded_jwt = jwt.encode(to_encode, AUTH_SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def replace_placeholders(message: str, node_data: dict) -> str:

    placeholder_mappings = {
        "node_id": "node_id","short_name": ["short_name", "user.shortName"],"long_name": ["long_name", "user.longName"],
        "name": ["short_name", "user.shortName", "long_name", "user.longName"],"latitude": "latitude","longitude": "longitude",
        "altitude": "altitude","position_time": "position_time","location": "custom:location",
        "battery_level": ["battery_level", "deviceMetrics.batteryLevel"],"voltage": ["voltage", "deviceMetrics.voltage"],
        "channel_utilization": ["channel_utilization", "deviceMetrics.channelUtilization"],
        "air_util_tx": ["air_util_tx", "deviceMetrics.airUtilTx"],"telemetry_time": ["telemetry_time", "deviceMetrics.time"],
        "snr": "snr","rssi": "rssi","hw_model": ["hw_model", "user.hwModel"],"firmware_version": "firmware_version",
        "role": "role","last_heard": "lastHeard"
    }
    def get_nested_value(data, path):
        if isinstance(path, str): return data.get(path)
        if isinstance(path, list):
            for p_item in path:
                if "." in p_item:
                    parts = p_item.split("."); current = data; valid_path = True
                    for part in parts:
                        if isinstance(current, dict) and part in current: current = current.get(part)
                        else: valid_path = False; break
                    if valid_path and current is not None: return current
                else:
                    value = data.get(p_item)
                    if value is not None: return value
        return None
    def get_custom_value(node_data_dict, custom_type):
        if custom_type == "location":
            lat = get_nested_value(node_data_dict, "latitude"); lon = get_nested_value(node_data_dict, "longitude")
            if lat is not None and lon is not None:
                try: return f"{float(lat):.6f}, {float(lon):.6f}"
                except ValueError: return "invalid lat/lon format"
            return "unknown location"
        return None
    placeholders = re.findall(r'\{([^}]+)\}', message)
    for placeholder in placeholders:
        value = None
        if placeholder in placeholder_mappings:
            mapping = placeholder_mappings[placeholder]
            if isinstance(mapping, str) and mapping.startswith("custom:"):
                custom_type = mapping.split(":")[1]; value = get_custom_value(node_data, custom_type)
            else: value = get_nested_value(node_data, mapping)
        if value is not None:
            formatted_value = f"{value:.2f}" if isinstance(value, float) else str(value)
            message = message.replace(f"{{{placeholder}}}", formatted_value)
        else: message = message.replace(f"{{{placeholder}}}", f"unknown {placeholder}")
    return message

class DatabaseManager:
    """Manages synchronous interactions with the SQLite database."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        logger.info(f"Initializing Meshtastic database at: {self.db_path}")
        self.init_database()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout = 5000;") 
            conn.execute("PRAGMA foreign_keys = ON;") 
        except Exception as e:
            logger.warning(f"Could not set WAL journal mode or other PRAGMAs for Meshtastic DB (might be unsupported or read-only FS): {e}")
        return conn

    def init_database(self):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("CREATE TABLE IF NOT EXISTS packets (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, timestamp REAL NOT NULL, rx_time INTEGER, from_id TEXT, to_id TEXT, channel INTEGER, portnum TEXT, packet_type TEXT, rx_snr REAL, rx_rssi INTEGER, hop_limit INTEGER, want_ack BOOLEAN, decoded TEXT, raw TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
                cursor.execute("CREATE TABLE IF NOT EXISTS nodes (node_id TEXT PRIMARY KEY, node_num INTEGER UNIQUE, long_name TEXT, short_name TEXT, macaddr TEXT, hw_model TEXT, firmware_version TEXT, role TEXT, is_local BOOLEAN DEFAULT FALSE, last_heard INTEGER, battery_level INTEGER, voltage REAL, channel_utilization REAL, air_util_tx REAL, snr REAL, rssi INTEGER, latitude REAL, longitude REAL, altitude INTEGER, position_time INTEGER, telemetry_time INTEGER, user_info TEXT, position_info TEXT, device_metrics_info TEXT, module_config_info TEXT, channel_info TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
                cursor.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, packet_event_id TEXT UNIQUE NOT NULL, from_id TEXT, to_id TEXT, channel INTEGER, text TEXT NOT NULL, timestamp REAL NOT NULL, rx_snr REAL, rx_rssi INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(packet_event_id) REFERENCES packets(event_id) ON DELETE CASCADE)")
                cursor.execute("CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL, timestamp REAL NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL, altitude INTEGER, precision_bits INTEGER, ground_speed INTEGER, ground_track INTEGER, sats_in_view INTEGER, pdop REAL, hdop REAL, vdop REAL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE)")
                cursor.execute("CREATE TABLE IF NOT EXISTS telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL, timestamp REAL NOT NULL, battery_level INTEGER, voltage REAL, channel_utilization REAL, air_util_tx REAL, uptime_seconds INTEGER, temperature REAL, relative_humidity REAL, barometric_pressure REAL, gas_resistance REAL, iaq REAL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE)")
                cursor.execute("CREATE TABLE IF NOT EXISTS average_metrics_history (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL UNIQUE NOT NULL, average_snr REAL, average_rssi REAL, node_count INTEGER NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
                cursor.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, hashed_password TEXT NOT NULL, disabled BOOLEAN DEFAULT FALSE, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_packets_timestamp ON packets(timestamp DESC)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_packets_from_id ON packets(from_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_packets_packet_type ON packets(packet_type)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_from_id ON messages(from_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_node_timestamp ON positions(node_id, timestamp DESC)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_node_timestamp ON telemetry(node_id, timestamp DESC)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_avg_metrics_timestamp ON average_metrics_history(timestamp DESC)")
                cursor.execute("CREATE TRIGGER IF NOT EXISTS trigger_node_updated_at AFTER UPDATE ON nodes FOR EACH ROW BEGIN UPDATE nodes SET updated_at = CURRENT_TIMESTAMP WHERE node_id = OLD.node_id; END;")
                conn.commit()
                logger.info("Meshtastic and User database initialization/verification complete.")
        except sqlite3.Error as e:
            logger.exception(f"Database initialization failed: {e}")
            raise

    def get_user(self, username: str) -> Optional[Dict]:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"Database error getting user {username}: {e}")
            return None

    def create_user(self, username: str, hashed_password: str) -> Optional[Dict]:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO users (username, hashed_password) VALUES (?, ?)", (username, hashed_password))
                conn.commit()
                user_id = cursor.lastrowid
                if user_id:
                    cursor.execute("SELECT id, username, disabled, created_at FROM users WHERE id = ?", (user_id,))
                    new_user = cursor.fetchone()
                    return dict(new_user) if new_user else None
                return None
        except sqlite3.IntegrityError: 
            logger.warning(f"Attempted to create user with existing username: {username}")
            return None
        except sqlite3.Error as e:
            logger.error(f"Database error creating user {username}: {e}")
            return None

    def save_packet(self, packet: Dict):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor(); event_id = packet.get('event_id'); timestamp = packet.get('timestamp'); rx_time = packet.get('rxTime')
                from_id = packet.get('fromId'); to_id = packet.get('toId'); mapped_channel_index = packet.get('channel')
                original_internal_channel_id = packet.get('original_channel_id')
                portnum_src = packet.get('decoded', {}).get('portnum') if packet.get('decoded') else None
                portnum_str = portnum_src.name if hasattr(portnum_src, 'name') and isinstance(portnum_src.name, str) else str(portnum_src) if portnum_src is not None else None
                packet_type = packet.get('app_packet_type'); rx_snr = packet.get('rxSnr'); rx_rssi = packet.get('rxRssi')
                hop_limit = packet.get('hopLimit'); want_ack = packet.get('wantAck', False)
                decoded_json = json.dumps(packet.get('decoded')) if packet.get('decoded') else None
                raw_json = json.dumps(packet.get('raw')) if packet.get('raw') else None
                if not event_id or not timestamp: logger.error(f"Skipping packet save: Missing event_id or timestamp. Packet: {packet}"); return
                cursor.execute("INSERT OR REPLACE INTO packets (event_id, timestamp, rx_time, from_id, to_id, channel, portnum, packet_type, rx_snr, rx_rssi, hop_limit, want_ack, decoded, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",(event_id, timestamp, rx_time, from_id, to_id, mapped_channel_index, portnum_str, packet_type, rx_snr, rx_rssi, hop_limit, want_ack, decoded_json, raw_json))
                if packet_type == "Message" and from_id and packet.get('decoded'):
                    channel_to_save_in_message = None
                    if original_internal_channel_id is not None:
                        try: channel_to_save_in_message = int(original_internal_channel_id)
                        except (ValueError, TypeError): logger.warning(f"save_packet: Could not convert original_channel_id '{original_internal_channel_id}' to int. Saving NULL."); channel_to_save_in_message = None
                    elif mapped_channel_index is not None: channel_to_save_in_message = mapped_channel_index
                    else: logger.warning(f"save_packet: Both original and mapped channel IDs are missing for message packet {event_id}. Saving NULL."); channel_to_save_in_message = None
                    decoded_data = packet.get('decoded'); text_payload = decoded_data.get('payload')
                    if isinstance(text_payload, bytes):
                        try: text_payload = text_payload.decode('utf-8')
                        except UnicodeDecodeError: logger.warning(f"Could not decode message payload as UTF-8 for packet {event_id}"); text_payload = None
                    if isinstance(text_payload, str) and text_payload: self.save_message(cursor, event_id, from_id, to_id, channel_to_save_in_message, text_payload, timestamp, rx_snr, rx_rssi)
                    elif 'data' in decoded_data and isinstance(decoded_data['data'], dict) and 'text' in decoded_data['data']:
                        text_payload = decoded_data['data']['text']
                        if isinstance(text_payload, str) and text_payload: self.save_message(cursor, event_id, from_id, to_id, channel_to_save_in_message, text_payload, timestamp, rx_snr, rx_rssi)
                elif packet_type == "Position" and from_id and packet.get('decoded'):
                    position_data = packet['decoded'].get('position')
                    if position_data and isinstance(position_data, dict): self.save_position(cursor, from_id, timestamp, position_data)
                elif packet_type == "Telemetry" and from_id and packet.get('decoded'):
                    telemetry_data = packet['decoded'].get('telemetry')
                    if telemetry_data and isinstance(telemetry_data, dict):
                        device_metrics = telemetry_data.get('deviceMetrics'); env_metrics = telemetry_data.get('environmentMetrics')
                        self.save_telemetry(cursor, from_id, timestamp, device_metrics, env_metrics)
                conn.commit()
        except sqlite3.Error as e: logger.exception(f"Database error saving packet {packet.get('event_id', 'N/A')}: {e}")
        except Exception as e: logger.exception(f"Unexpected error saving packet {packet.get('event_id', 'N/A')}: {e}")

    def save_message(self, cursor: sqlite3.Cursor, packet_event_id: str, from_id: str, to_id: Optional[str], channel: Optional[int], text: str, timestamp: float, rx_snr: Optional[float], rx_rssi: Optional[int]):
        logger.debug(f"save_message: Saving packet {packet_event_id} with channel value: {channel}")
        try:
            db_to_id = '!ffffffff' if to_id == '^all' else to_id
            cursor.execute("INSERT OR REPLACE INTO messages (packet_event_id, from_id, to_id, channel, text, timestamp, rx_snr, rx_rssi) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (packet_event_id, from_id, db_to_id, channel, text, timestamp, rx_snr, rx_rssi))
        except sqlite3.Error as e: logger.error(f"DB Error saving message for packet {packet_event_id}: {e}")

    def save_position(self, cursor: sqlite3.Cursor, node_id: str, timestamp: float, position_data_dict: Dict): 
        latitude, longitude = None, None

        lat_i = position_data_dict.get('latitudeI')
        lon_i = position_data_dict.get('longitudeI')

        if lat_i is not None and lon_i is not None:
            try:
                latitude = float(lat_i) / 1e7
                longitude = float(lon_i) / 1e7
            except (TypeError, ValueError):
                logger.warning(f"Could not convert latitudeI/longitudeI to float for node {node_id}. Data: {position_data_dict}. Falling back.")
                latitude = None 
                longitude = None

        if latitude is None or longitude is None: 
            lat_f = position_data_dict.get('latitude')
            lon_f = position_data_dict.get('longitude')
            if isinstance(lat_f, (int, float)) and isinstance(lon_f, (int, float)):
                latitude = float(lat_f)
                longitude = float(lon_f)

        if latitude is None or longitude is None:
            logger.warning(f"Skipping position save for node {node_id}: Missing or invalid lat/lon after all checks. Data: {position_data_dict}")
            return

        try:
            cursor.execute("""
            INSERT INTO positions (
                node_id, timestamp, latitude, longitude, altitude,
                precision_bits, ground_speed, ground_track, sats_in_view,
                pdop, hdop, vdop
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (node_id, timestamp, latitude, longitude,
                  position_data_dict.get('altitude'), position_data_dict.get('precisionBits'), 
                  position_data_dict.get('groundSpeed'), position_data_dict.get('groundTrack'),
                  position_data_dict.get('satsInView'), position_data_dict.get('pdop'),
                  position_data_dict.get('hdop'), position_data_dict.get('vdop')))
        except sqlite3.Error as e:
            logger.error(f"DB Error saving position for node {node_id}: {e}")

    def save_telemetry(self, cursor: sqlite3.Cursor, node_id: str, timestamp: float, device_metrics: Optional[Dict], env_metrics: Optional[Dict]):
        if not device_metrics and not env_metrics: return
        bat = device_metrics.get('batteryLevel') if device_metrics else None; volt = device_metrics.get('voltage') if device_metrics else None
        chan_util = device_metrics.get('channelUtilization') if device_metrics else None; air_util = device_metrics.get('airUtilTx') if device_metrics else None
        uptime = device_metrics.get('uptimeSeconds') if device_metrics else None; temp = env_metrics.get('temperature') if env_metrics else None
        hum = env_metrics.get('relativeHumidity') if env_metrics else None; press = env_metrics.get('barometricPressure') if env_metrics else None
        gas = env_metrics.get('gasResistance') if env_metrics else None; iaq = env_metrics.get('iaq') if env_metrics else None
        if any(v is not None for v in [bat, volt, chan_util, air_util, uptime, temp, hum, press, gas, iaq]):
            try: cursor.execute("INSERT INTO telemetry (node_id, timestamp, battery_level, voltage, channel_utilization, air_util_tx, uptime_seconds, temperature, relative_humidity, barometric_pressure, gas_resistance, iaq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",(node_id, timestamp, bat, volt, chan_util, air_util, uptime, temp, hum, press, gas, iaq))
            except sqlite3.Error as e: logger.error(f"DB Error saving telemetry for node {node_id}: {e}")
        else: logger.debug(f"Skipping telemetry save for node {node_id}: All metric values were None.")

    def save_node(self, node_id_str: str, data: Dict):
        if not node_id_str or not data: logger.warning("Attempted to save node with empty ID or data."); return
        node_num = data.get('num'); user_info = data.get('user', {}); position_info = data.get('position', {}); device_metrics = data.get('deviceMetrics', {}); module_config = data.get('moduleConfig', {}); channel_info = data.get('channelSettings', {})
        if node_num is None:
            if 'node_num' in data: node_num = data['node_num']
            elif node_id_str.startswith('!'):
                try: node_num = int(node_id_str[1:], 16)
                except ValueError: logger.warning(f"Could not parse node_num from ID {node_id_str}")
            if node_num is None: logger.error(f"Cannot save node {node_id_str}: node_num is missing."); return
        long_name = user_info.get('longName'); short_name = user_info.get('shortName'); macaddr = user_info.get('macaddr')
        hw_model = user_info.get('hwModel') or data.get('hwModelStr') or data.get('hw_model'); firmware_version = data.get('firmwareVersion') or data.get('firmware_version')
        role_enum = data.get('role'); role_str = str(role_enum) if role_enum is not None else None
        is_local = data.get('isLocal', False); last_heard = data.get('lastHeard'); snr = data.get('snr'); rssi = data.get('rssi')
        battery_level = data.get('battery_level', device_metrics.get('batteryLevel')); voltage = data.get('voltage', device_metrics.get('voltage'))
        channel_utilization = data.get('channel_utilization', device_metrics.get('channelUtilization')); air_util_tx = data.get('air_util_tx', device_metrics.get('airUtilTx'))
        telemetry_time = data.get('telemetry_time', device_metrics.get('time')); position_time = data.get('position_time', position_info.get('time'))
        latitude, longitude, altitude = None, None, None; lat_direct = data.get('latitude'); lon_direct = data.get('longitude'); alt_direct = data.get('altitude')
        if lat_direct is not None and lon_direct is not None: latitude, longitude, altitude = lat_direct, lon_direct, alt_direct
        else:
            lat_i = position_info.get('latitudeI'); lon_i = position_info.get('longitudeI')
            if lat_i is not None and lon_i is not None: latitude = lat_i / 1e7; longitude = lon_i / 1e7
            else: latitude = position_info.get('latitude'); longitude = position_info.get('longitude')
            altitude = alt_direct if alt_direct is not None else position_info.get('altitude')
        user_info_json = json.dumps(user_info) if isinstance(user_info, dict) and user_info else None
        position_info_json = json.dumps(position_info) if isinstance(position_info, dict) and position_info else None
        device_metrics_json = json.dumps(device_metrics) if isinstance(device_metrics, dict) and device_metrics else None
        module_config_json = json.dumps(module_config) if isinstance(module_config, dict) and module_config else None
        channel_info_json = json.dumps(channel_info) if isinstance(channel_info, dict) and channel_info else None
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO nodes (node_id, node_num, long_name, short_name, macaddr, hw_model, firmware_version, role, is_local, last_heard, battery_level, voltage, channel_utilization, air_util_tx, snr, rssi, latitude, longitude, altitude, position_time, telemetry_time, user_info, position_info, device_metrics_info, module_config_info, channel_info, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(node_id) DO UPDATE SET node_num=excluded.node_num, long_name=COALESCE(excluded.long_name, nodes.long_name), short_name=COALESCE(excluded.short_name, nodes.short_name), macaddr=COALESCE(excluded.macaddr, nodes.macaddr), hw_model=COALESCE(excluded.hw_model, nodes.hw_model), firmware_version=COALESCE(excluded.firmware_version, nodes.firmware_version), role=COALESCE(excluded.role, nodes.role), is_local=excluded.is_local, last_heard=COALESCE(excluded.last_heard, nodes.last_heard), battery_level=COALESCE(excluded.battery_level, nodes.battery_level), voltage=COALESCE(excluded.voltage, nodes.voltage), channel_utilization=COALESCE(excluded.channel_utilization, nodes.channel_utilization), air_util_tx=COALESCE(excluded.air_util_tx, nodes.air_util_tx), snr=excluded.snr, rssi=excluded.rssi, latitude=COALESCE(excluded.latitude, nodes.latitude), longitude=COALESCE(excluded.longitude, nodes.longitude), altitude=COALESCE(excluded.altitude, nodes.altitude), position_time=COALESCE(excluded.position_time, nodes.position_time), telemetry_time=COALESCE(excluded.telemetry_time, nodes.telemetry_time), user_info=CASE WHEN excluded.user_info IS NOT NULL THEN excluded.user_info ELSE nodes.user_info END, position_info=CASE WHEN excluded.position_info IS NOT NULL THEN excluded.position_info ELSE nodes.position_info END, device_metrics_info=CASE WHEN excluded.device_metrics_info IS NOT NULL THEN excluded.device_metrics_info ELSE nodes.device_metrics_info END, module_config_info=CASE WHEN excluded.module_config_info IS NOT NULL THEN excluded.module_config_info ELSE nodes.module_config_info END, channel_info=CASE WHEN excluded.channel_info IS NOT NULL THEN excluded.channel_info ELSE nodes.channel_info END, updated_at=CURRENT_TIMESTAMP WHERE excluded.last_heard IS NULL OR nodes.last_heard IS NULL OR excluded.last_heard >= nodes.last_heard",(node_id_str, node_num, long_name, short_name, macaddr, hw_model, firmware_version, role_str, is_local, last_heard, battery_level, voltage, channel_utilization, air_util_tx, snr, rssi, latitude, longitude, altitude, position_time, telemetry_time, user_info_json, position_info_json, device_metrics_json, module_config_json, channel_info_json))
                conn.commit()
        except sqlite3.IntegrityError as e: logger.error(f"DB Integrity Error for node {node_id_str} (num: {node_num}): {e}", exc_info=False)
        except sqlite3.Error as e: logger.exception(f"DB error saving node {node_id_str}: {e}")
        except Exception as e: logger.exception(f"Unexpected error saving node {node_id_str}: {e}")

    def get_recent_packets(self, limit: int = 100) -> List[Dict]:
        results = []; conn = self._get_connection()
        try:
            cursor = conn.cursor(); cursor.execute("SELECT * FROM packets ORDER BY timestamp DESC LIMIT ?", (limit,)); rows = cursor.fetchall()
            for row in rows:
                packet = dict(row)
                try:
                    if packet.get('decoded') and isinstance(packet['decoded'], str): packet['decoded'] = json.loads(packet['decoded'])
                    elif not isinstance(packet.get('decoded'), dict): packet['decoded'] = {}
                    if packet.get('raw') and isinstance(packet['raw'], str):
                        try: packet['raw'] = json.loads(packet['raw'])
                        except json.JSONDecodeError: logger.warning(f"Could not decode 'raw' JSON for packet {packet.get('event_id')}")
                    elif not isinstance(packet.get('raw'), dict): packet['raw'] = {}
                except (json.JSONDecodeError, TypeError) as e: logger.warning(f"Error decoding JSON for packet {packet.get('event_id')}: {e}")
                results.append(packet)
        except sqlite3.Error as e: logger.exception(f"Error getting recent packets: {e}")
        finally: conn.close()
        return results

    def get_all_nodes(self) -> Dict[str, Dict]:
        nodes = {}; conn = self._get_connection()
        try:
            cursor = conn.cursor(); cursor.execute("SELECT * FROM nodes ORDER BY node_num"); rows = cursor.fetchall()
            for row in rows:
                node = dict(row); node_id = node.get('node_id')
                if not node_id: continue
                for field in ['user_info', 'position_info', 'device_metrics_info', 'module_config_info', 'channel_info']:
                    try:
                        if node.get(field) and isinstance(node[field], str): node[field] = json.loads(node[field])
                        elif not isinstance(node.get(field), dict): node[field] = {}
                    except (json.JSONDecodeError, TypeError) as e: logger.warning(f"Error decoding JSON field '{field}' for node {node_id}: {e}"); node[field] = {}
                node['user'] = node.pop('user_info', {}); node['position'] = node.pop('position_info', {}); node['deviceMetrics'] = node.pop('device_metrics_info', {})
                node['moduleConfig'] = node.pop('module_config_info', {}); node['channelSettings'] = node.pop('channel_info', {})
                nodes[node_id] = node
        except sqlite3.Error as e: logger.exception(f"Error getting all nodes: {e}")
        finally: conn.close()
        return nodes

    def get_messages(self, from_id: Optional[str] = None, to_id: Optional[str] = None, channel: Optional[int] = None, start_time: Optional[float] = None, end_time: Optional[float] = None, limit: int = 100) -> List[Dict]:
        results = []; conn = self._get_connection()
        try:
            cursor = conn.cursor(); query = "SELECT * FROM messages WHERE 1=1"; params: List[Union[str, float, int]] = []
            if from_id: query += " AND from_id = ?"; params.append(from_id)
            if to_id: db_to_id = '!ffffffff' if to_id == '^all' else to_id; query += " AND to_id = ?"; params.append(db_to_id)
            if channel is not None: query += " AND channel = ?"; params.append(channel)
            if start_time is not None: query += " AND timestamp >= ?"; params.append(start_time)
            if end_time is not None: query += " AND timestamp <= ?"; params.append(end_time)
            query += " ORDER BY timestamp DESC LIMIT ?"; params.append(limit)
            cursor.execute(query, tuple(params)); rows = cursor.fetchall(); results = [dict(row) for row in rows]
        except sqlite3.Error as e: logger.exception(f"Error getting messages: {e}")
        finally: conn.close()
        return results

    def get_node_history(self, node_id: str, history_type: Literal["positions", "telemetry"], start_time: Optional[float] = None, end_time: Optional[float] = None, limit: int = 1000) -> List[Dict]:
        results = []; conn = self._get_connection()
        if history_type not in ['positions', 'telemetry']: logger.error(f"Invalid history type: {history_type}"); return []
        table = history_type
        try:
            cursor = conn.cursor(); query = f"SELECT * FROM {table} WHERE node_id = ?"; params: List[Union[str, float, int]] = [node_id]
            if start_time is not None: query += " AND timestamp >= ?"; params.append(start_time)
            if end_time is not None: query += " AND timestamp <= ?"; params.append(end_time)
            query += " ORDER BY timestamp DESC LIMIT ?"; params.append(limit)
            cursor.execute(query, tuple(params)); rows = cursor.fetchall(); results = [dict(row) for row in rows]
        except sqlite3.Error as e: logger.exception(f"Error getting node {history_type} for {node_id}: {e}")
        finally: conn.close()
        return results

    def count_node_items(self, node_id: Optional[str], item_type: Literal["messages_sent", "positions", "telemetry"], start_time: Optional[float] = None, end_time: Optional[float] = None) -> int:
        if item_type == "messages_sent": table = "messages"; node_column = "from_id"
        elif item_type == "positions": table = "positions"; node_column = "node_id"
        elif item_type == "telemetry": table = "telemetry"; node_column = "node_id"
        else: logger.error(f"Invalid item_type '{item_type}' in count_node_items."); raise ValueError(f"Invalid item_type: {item_type}")
        conn = self._get_connection()
        try:
            cursor = conn.cursor(); base_query = f"SELECT COUNT(*) FROM {table}"; conditions = []; params: List[Union[str, float]] = []
            if node_id: conditions.append(f"{node_column} = ?"); params.append(node_id)
            if start_time is not None: conditions.append("timestamp >= ?"); params.append(start_time)
            if end_time is not None: conditions.append("timestamp <= ?"); params.append(end_time)
            query = base_query + (" WHERE " + " AND ".join(conditions) if conditions else "")
            cursor.execute(query, tuple(params)); result = cursor.fetchone()
            return result[0] if result else 0
        except sqlite3.Error as e: logger.exception(f"DB error counting {item_type} for {node_id or 'all nodes'}: {e}"); return -1
        finally: conn.close()

    def _calculate_current_average_metrics(self) -> Tuple[Optional[float], Optional[float], int]:
        snr_values = []; rssi_values = []; node_count = 0; conn = self._get_connection()
        try:
            cursor = conn.cursor(); cursor.execute("SELECT snr, rssi FROM nodes WHERE snr IS NOT NULL AND rssi IS NOT NULL AND is_local = FALSE"); rows = cursor.fetchall()
            for row in rows:
                if isinstance(row['snr'], (int, float)): snr_values.append(float(row['snr']))
                if isinstance(row['rssi'], (int, float)): rssi_values.append(float(row['rssi']))
            node_count = len(rows)
            avg_snr = round(statistics.mean(snr_values), 2) if snr_values else None
            avg_rssi = round(statistics.mean(rssi_values), 1) if rssi_values else None
            return avg_snr, avg_rssi, node_count
        except sqlite3.Error as e: logger.error(f"DB error calculating avg metrics: {e}", exc_info=True); return None, None, 0
        except Exception as e: logger.error(f"Unexpected error calculating avg metrics: {e}", exc_info=True); return None, None, 0
        finally: conn.close()

    def save_average_metrics(self, avg_snr: Optional[float], avg_rssi: Optional[float], node_count: int):
        if (avg_snr is None and avg_rssi is None) or node_count <= 0: return
        current_timestamp = time.time(); conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO average_metrics_history (timestamp, average_snr, average_rssi, node_count) VALUES (?, ?, ?, ?)", (current_timestamp, avg_snr, avg_rssi, node_count))
            conn.commit()
        except sqlite3.Error as e: logger.error(f"DB error saving avg metrics: {e}", exc_info=True)
        except Exception as e: logger.error(f"Unexpected error saving avg metrics: {e}", exc_info=True)
        finally: conn.close()

    def calculate_and_save_average_metrics(self):
        avg_snr, avg_rssi, node_count = self._calculate_current_average_metrics()
        self.save_average_metrics(avg_snr, avg_rssi, node_count)

    def get_average_metrics_history(self, limit: int = 100, start_time: Optional[float] = None, end_time: Optional[float] = None) -> List[Dict]:
        results = []; conn = self._get_connection()
        try:
            cursor = conn.cursor(); query = "SELECT * FROM average_metrics_history WHERE 1=1"; params: List[Union[float, int]] = []
            if start_time is not None: query += " AND timestamp >= ?"; params.append(start_time)
            if end_time is not None: query += " AND timestamp <= ?"; params.append(end_time)
            query += " ORDER BY timestamp DESC LIMIT ?"; params.append(limit)
            cursor.execute(query, tuple(params)); rows = cursor.fetchall(); results = [dict(row) for row in rows]
        except sqlite3.Error as e: logger.exception(f"Error getting avg metrics history: {e}")
        finally: conn.close()
        return results

    def get_most_recent_average_metrics(self) -> Optional[Dict]:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM average_metrics_history ORDER BY timestamp DESC LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            logger.exception(f"Error getting most recent average metrics: {e}")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error getting most recent average metrics: {e}")
            return None
        finally:
            conn.close()

    def prune_old_average_metrics(self, max_age_days: int = AVERAGE_METRICS_HISTORY_DAYS):
        if max_age_days <= 0: logger.info("Pruning for avg metrics history disabled."); return
        cutoff_timestamp = time.time() - (max_age_days * 24 * 60 * 60)
        logger.info(f"Pruning avg metrics history older than {max_age_days} days (timestamp < {cutoff_timestamp})...")
        conn = self._get_connection()
        try:
            cursor = conn.cursor(); cursor.execute("DELETE FROM average_metrics_history WHERE timestamp < ?", (cutoff_timestamp,))
            deleted_count = cursor.rowcount; conn.commit()
            if deleted_count > 0: logger.info(f"Pruned {deleted_count} old avg metrics history records.")
            else: logger.info("No old avg metrics history records to prune.")
        except sqlite3.Error as e: logger.error(f"DB error pruning avg metrics history: {e}", exc_info=True)
        except Exception as e: logger.error(f"Unexpected error pruning avg metrics history: {e}", exc_info=True)
        finally: conn.close()

db_manager = DatabaseManager(DB_PATH)

async def get_current_active_user(request: Request) -> User:
    token_with_bearer = request.cookies.get("access_token")
    login_url = "/login"

    credentials_exception = HTTPException(
        status_code=status.HTTP_302_FOUND, 
        detail="Not authenticated", 
        headers={"Location": login_url},
    )

    if not token_with_bearer:

        raise credentials_exception

    if not token_with_bearer.startswith("Bearer "):
        logger.warning("Access token cookie does not start with Bearer.")

        response = RedirectResponse(url=login_url, status_code=status.HTTP_302_FOUND)
        response.delete_cookie("access_token", path="/") 
        raise HTTPException(status_code=status.HTTP_302_FOUND, detail="Malformed token.", headers=response.headers)

    token = token_with_bearer.split("Bearer ", 1)[1]

    try:
        payload = jwt.decode(token, AUTH_SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        if username is None:
            logger.warning("Token payload missing username (sub).")
            raise JWTError("Username (sub) missing in token") 
        token_data = TokenData(username=username)
    except JWTError as e:
        logger.warning(f"JWTError decoding token: {e}")
        response = RedirectResponse(url=login_url, status_code=status.HTTP_302_FOUND)
        response.delete_cookie("access_token", path="/")

        raise HTTPException(status_code=status.HTTP_302_FOUND, detail=f"Token validation failed: {e}", headers=response.headers)
    except Exception as e: 
        logger.error(f"Unexpected error decoding token: {e}", exc_info=True)
        response = RedirectResponse(url=login_url, status_code=status.HTTP_302_FOUND)
        response.delete_cookie("access_token", path="/")
        raise HTTPException(status_code=status.HTTP_302_FOUND, detail="Token decoding error.", headers=response.headers)

    user_dict = await asyncio.to_thread(db_manager.get_user, username=token_data.username)

    if user_dict is None:
        logger.warning(f"User {token_data.username} from token not found in DB.")
        response = RedirectResponse(url=login_url, status_code=status.HTTP_302_FOUND)
        response.delete_cookie("access_token", path="/")
        raise HTTPException(status_code=status.HTTP_302_FOUND, detail="User not found.", headers=response.headers)

    user = User(**user_dict)
    if user.disabled:
        logger.warning(f"User {user.username} is disabled.")
        response = RedirectResponse(url=login_url, status_code=status.HTTP_302_FOUND)
        response.delete_cookie("access_token", path="/")
        raise HTTPException(status_code=status.HTTP_302_FOUND, detail="User disabled.", headers=response.headers)

    return user

    def count_node_items(self, node_id: Optional[str], item_type: Literal["messages_sent", "positions", "telemetry"],
                         start_time: Optional[float] = None, end_time: Optional[float] = None) -> int:
        """
        Counts items associated with a specific node ID, or all nodes if node_id is None.

        Args:
            node_id: The specific node ID (e.g., '!aabbccdd') to count items for,
                     or None to count items across all nodes.
            item_type: The type of item to count ('messages_sent', 'positions', 'telemetry').
            start_time: Optional start timestamp filter (unix epoch float).
            end_time: Optional end timestamp filter (unix epoch float).

        Returns:
            The count of matching items, or -1 if a database error occurred.

        Raises:
            ValueError: If an invalid item_type is specified.
        """

        if item_type == "messages_sent":
            table = "messages"
            node_column = "from_id" 
        elif item_type == "positions":
            table = "positions"
            node_column = "node_id"
        elif item_type == "telemetry":
            table = "telemetry"
            node_column = "node_id"
        else:

            logger.error(f"Invalid item_type '{item_type}' passed to count_node_items.")
            raise ValueError(f"Invalid item_type specified: {item_type}")

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                base_query = f"SELECT COUNT(*) FROM {table}"
                conditions = []
                params: List[Union[str, float]] = []

                if node_id: 
                    conditions.append(f"{node_column} = ?")
                    params.append(node_id)

                if start_time is not None:
                    conditions.append("timestamp >= ?")
                    params.append(start_time)
                if end_time is not None:
                    conditions.append("timestamp <= ?")
                    params.append(end_time)

                if conditions:
                    where_clause = " WHERE " + " AND ".join(conditions)
                    query = base_query + where_clause
                else:
                    query = base_query 

                target_node_log = f"node {node_id}" if node_id else "all nodes"
                logger.debug(f"Executing count query for {target_node_log}, type {item_type}: {query} with params: {params}")

                cursor.execute(query, tuple(params))
                result = cursor.fetchone()

                count = result[0] if result else 0
                logger.debug(f"Count result for {target_node_log}, type {item_type}: {count}")
                return count
        except sqlite3.Error as e:
            target_node_log = f"node {node_id}" if node_id else "all nodes"
            logger.exception(f"Database error counting {item_type} for {target_node_log}: {e}")

            return -1 
        except Exception as e:
            target_node_log = f"node {node_id}" if node_id else "all nodes"
            logger.exception(f"Unexpected error counting {item_type} for {target_node_log}: {e}")
            return -1 

    def _calculate_current_average_metrics(self) -> Tuple[Optional[float], Optional[float], int]:
        """Calculates current average SNR/RSSI across non-local nodes."""
        snr_values = []
        rssi_values = []
        node_count = 0
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute("SELECT snr, rssi FROM nodes WHERE snr IS NOT NULL AND rssi IS NOT NULL AND is_local = FALSE")
                rows = cursor.fetchall()
                for row in rows:

                    if isinstance(row['snr'], (int, float)):
                        snr_values.append(float(row['snr']))
                    if isinstance(row['rssi'], (int, float)):
                        rssi_values.append(float(row['rssi']))

                node_count = len(rows) 

                avg_snr = round(statistics.mean(snr_values), 2) if snr_values else None
                avg_rssi = round(statistics.mean(rssi_values), 1) if rssi_values else None
                return avg_snr, avg_rssi, node_count
        except sqlite3.Error as e:
            logger.error(f"DB error calculating average metrics: {e}", exc_info=True)
            return None, None, 0
        except Exception as e:
            logger.error(f"Unexpected error calculating average metrics: {e}", exc_info=True)
            return None, None, 0

    def save_average_metrics(self, avg_snr: Optional[float], avg_rssi: Optional[float], node_count: int):
        """Saves the calculated average metrics to the history table."""

        if (avg_snr is None and avg_rssi is None) or node_count <= 0:
            return

        current_timestamp = time.time()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute("""
                    INSERT OR IGNORE INTO average_metrics_history (timestamp, average_snr, average_rssi, node_count)
                    VALUES (?, ?, ?, ?)
                """, (current_timestamp, avg_snr, avg_rssi, node_count))
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"DB error saving average metrics: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Unexpected error saving average metrics: {e}", exc_info=True)

    def calculate_and_save_average_metrics(self):
        """Calculates and saves the current average metrics."""
        avg_snr, avg_rssi, node_count = self._calculate_current_average_metrics()
        self.save_average_metrics(avg_snr, avg_rssi, node_count)

    def get_average_metrics_history(self, limit: int = 100, start_time: Optional[float] = None, end_time: Optional[float] = None) -> List[Dict]:
        """Retrieves the history of average metrics."""
        results = []
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                query = "SELECT * FROM average_metrics_history WHERE 1=1"
                params: List[Union[float, int]] = []

                if start_time is not None:
                    query += " AND timestamp >= ?"
                    params.append(start_time)
                if end_time is not None:
                    query += " AND timestamp <= ?"
                    params.append(end_time)

                query += " ORDER BY timestamp DESC LIMIT ?"
                params.append(limit)

                cursor.execute(query, tuple(params))
                rows = cursor.fetchall()
                results = [dict(row) for row in rows]
            return results
        except sqlite3.Error as e:
            logger.exception(f"Error getting average metrics history from database: {e}")
            return []
        except Exception as e:
            logger.exception(f"Unexpected error getting average metrics history: {e}")
            return []

    def get_most_recent_average_metrics(self) -> Optional[Dict]:
        """Gets the single most recent average metrics record."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM average_metrics_history ORDER BY timestamp DESC LIMIT 1")
                row = cursor.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.exception(f"Error getting most recent average metrics: {e}")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error getting most recent average metrics: {e}")
            return None

    def prune_old_average_metrics(self, max_age_days: int = AVERAGE_METRICS_HISTORY_DAYS):
        """Deletes average metrics history records older than the specified number of days."""
        if max_age_days <= 0:
            logger.info("Pruning for average metrics history is disabled (max_age_days <= 0).")
            return

        cutoff_timestamp = time.time() - (max_age_days * 24 * 60 * 60)
        logger.info(f"Pruning average metrics history older than {max_age_days} days (timestamp < {cutoff_timestamp})...")
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM average_metrics_history WHERE timestamp < ?", (cutoff_timestamp,))
                deleted_count = cursor.rowcount
                conn.commit() 
                if deleted_count > 0:
                    logger.info(f"Pruned {deleted_count} old average metrics history records.")
                else:
                     logger.info("No old average metrics history records found to prune.")
        except sqlite3.Error as e:
            logger.error(f"DB error pruning average metrics history: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Unexpected error pruning average metrics history: {e}", exc_info=True)

class MessageRequest(PydanticBaseModel):
    """Request model for sending a message."""
    message: str = Field(..., description="Message text to send")
    destination: Optional[str] = Field(None, description="Destination node ID (e.g., !hexid, ^all, empty/null for broadcast)")
    channel: Optional[int] = Field(0, description="Channel index to use for sending")

    if PYDANTIC_V2 and model_validator:
        @model_validator(mode='before')
        def check_message_not_empty(cls, values):
            message = values.get('message')
            if not message or str(message).strip() == "":
                raise ValueError('Message cannot be empty')
            return values
    elif not PYDANTIC_V2 and pydantic_validator_v1:
         @pydantic_validator_v1('message')
         def message_must_not_be_empty(cls, v):
             if not v or str(v).strip() == "":
                  raise ValueError('Message cannot be empty')
             return v
    else: 
        @field_validator('message')
        def message_must_not_be_empty_fallback(cls, v):
            if not v or str(v).strip() == "":
                 raise ValueError('Message cannot be empty')
            return v
        logger.warning("Could not detect Pydantic validator version reliably, using fallback validation.")

class ApiAverageMetricsData(PydanticBaseModel):
    """Data model for a single average metrics history point."""
    id: Optional[int] = None 
    timestamp: float
    average_snr: Optional[float] = None
    average_rssi: Optional[float] = None
    node_count: int
    created_at: Optional[Any] = None 

class ApiAverageMetricsResponse(PydanticBaseModel):
    """Response model for average metrics endpoint."""
    most_recent: Optional[ApiAverageMetricsData] = None
    history: List[ApiAverageMetricsData] = []

class URLRequest(PydanticBaseModel):
    """Request model for URL content extraction."""
    url: str
    block_id: Optional[int] = None
    text_only: Optional[bool] = False

class TextBlock(PydanticBaseModel):
    """Response model for a single text block from URL extraction."""
    element_type: str
    element_id: Optional[str] = None
    element_class: Optional[str] = None
    text: str

class NodeItemCountResponse(PydanticBaseModel):
    """Response model for the item count endpoint."""
    node_id: str
    item_type: Literal["messages_sent", "positions", "telemetry"] 
    count: int
    start_time_filter: Optional[float] = Field(None, description="Timestamp filter applied (if any)")
    end_time_filter: Optional[float] = Field(None, description="Timestamp filter applied (if any)")

class ApiTotalCountsResponse(PydanticBaseModel):
    """Response model for the total historical item counts endpoint."""
    total_messages: int = Field(description="Total historical messages sent by all nodes.")
    total_positions: int = Field(description="Total historical position records from all nodes.")
    total_telemetry: int = Field(description="Total historical telemetry records from all nodes.")

class ApiUserInfo(PydanticBaseModel):
    id: Optional[str] = None
    longName: Optional[str] = None
    shortName: Optional[str] = None
    macaddr: Optional[str] = None
    hwModel: Optional[str] = None

class ApiDeviceMetrics(PydanticBaseModel):
    time: Optional[int] = None
    batteryLevel: Optional[int] = None
    voltage: Optional[float] = None
    channelUtilization: Optional[float] = None
    airUtilTx: Optional[float] = None
    uptimeSeconds: Optional[int] = None

class ApiPositionInfo(PydanticBaseModel):
    time: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[int] = None
    groundSpeed: Optional[int] = None 
    satsInView: Optional[int] = None

    latitudeI: Optional[int] = None
    longitudeI: Optional[int] = None

    groundTrack: Optional[int] = None
    precisionBits: Optional[int] = None
    pdop: Optional[float] = None
    hdop: Optional[float] = None
    vdop: Optional[float] = None

class ApiNodeData(PydanticBaseModel):
    """Pydantic model representing a node for API responses."""
    node_id: str = Field(description="Node ID (e.g., !aabbccdd)")
    node_num: Optional[int] = Field(None, description="Node number (integer)")
    user: Optional[ApiUserInfo] = Field(None, description="Nested user information structure")

    long_name: Optional[str] = None
    short_name: Optional[str] = None
    macaddr: Optional[str] = None
    hw_model: Optional[str] = None
    firmware_version: Optional[str] = None
    role: Optional[str] = None
    is_local: bool = False
    last_heard: Optional[int] = Field(None, description="Unix timestamp of last packet received")
    snr: Optional[float] = None
    rssi: Optional[int] = None
    battery_level: Optional[int] = None
    voltage: Optional[float] = None
    channel_utilization: Optional[float] = None
    air_util_tx: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[int] = None
    position_time: Optional[int] = Field(None, description="Unix timestamp of last position update")
    telemetry_time: Optional[int] = Field(None, description="Unix timestamp of last telemetry update")
    created_at: Optional[Any] = None 
    updated_at: Optional[Any] = None 

    position: Optional[ApiPositionInfo] = Field(None, description="Latest full position structure")
    deviceMetrics: Optional[ApiDeviceMetrics] = Field(None, description="Latest full device metrics structure")

class ApiPacketData(PydanticBaseModel):
    """Pydantic model representing a packet for API responses."""
    event_id: str
    timestamp: float
    rxTime: Optional[int] = None
    fromId: Optional[str] = None
    toId: Optional[str] = None
    channel: Optional[int] = None
    rxSnr: Optional[float] = None
    rxRssi: Optional[int] = None
    hopLimit: Optional[int] = None
    decoded: Optional[Dict[str, Any]] = None 
    raw: Optional[Dict[str, Any]] = None     
    app_packet_type: Optional[str] = None    
    wantAck: Optional[bool] = None

class ApiMessageData(PydanticBaseModel):
    """Pydantic model representing a message for API responses."""
    id: int 
    packet_event_id: str 
    from_id: Optional[str] = None
    to_id: Optional[str] = None
    channel: Optional[int] = None
    text: str
    timestamp: float
    rx_snr: Optional[float] = None
    rx_rssi: Optional[int] = None
    created_at: Optional[Any] = None 

class ApiNodePositionHistory(PydanticBaseModel):
    """Pydantic model for a single position history record."""
    id: int
    node_id: str
    timestamp: float
    latitude: float
    longitude: float
    altitude: Optional[int] = None
    precision_bits: Optional[int] = None
    ground_speed: Optional[int] = None
    ground_track: Optional[int] = None
    sats_in_view: Optional[int] = None
    pdop: Optional[float] = None
    hdop: Optional[float] = None
    vdop: Optional[float] = None
    created_at: Optional[Any] = None

class ApiNodeTelemetryHistory(PydanticBaseModel):
    """Pydantic model for a single telemetry history record."""
    id: int
    node_id: str
    timestamp: float
    battery_level: Optional[int] = None
    voltage: Optional[float] = None
    channel_utilization: Optional[float] = None
    air_util_tx: Optional[float] = None
    uptime_seconds: Optional[int] = None
    temperature: Optional[float] = None
    relative_humidity: Optional[float] = None
    barometric_pressure: Optional[float] = None
    gas_resistance: Optional[float] = None
    iaq: Optional[float] = None
    created_at: Optional[Any] = None

class HookMessageRequest(PydanticBaseModel):
    """Pydantic model for the /api/hook request body."""
    node_id: str = Field(..., description="Destination node ID (e.g., !hexid). Must be provided and start with '!'.")
    message: str = Field(..., description="Message text to send. Cannot be empty.")
    channel: Optional[int] = Field(0, description="Optional channel index to use (default: 0)")

    if PYDANTIC_V2 and model_validator:
         @model_validator(mode='before')
         def check_hook_fields(cls, values):
             message = values.get('message')
             node_id = values.get('node_id')

             if not message or str(message).strip() == "":
                 raise ValueError('Message cannot be empty')
             if not node_id or str(node_id).strip() == "":
                  raise ValueError('Node ID cannot be empty for hook')
             if not str(node_id).startswith('!'):
                  raise ValueError('Node ID must start with "!" for hook')
             return values
    elif not PYDANTIC_V2 and pydantic_validator_v1:

         @pydantic_validator_v1('message')
         def hook_message_must_not_be_empty(cls, v):
             if not v or str(v).strip() == "":
                 raise ValueError('Message cannot be empty')
             return v
         @pydantic_validator_v1('node_id')
         def hook_nodeid_must_be_valid(cls, v):
             if not v or str(v).strip() == "":
                 raise ValueError('Node ID cannot be empty for hook')
             if not str(v).startswith('!'):
                 raise ValueError('Node ID must start with "!" for hook')
             return v
    else: 
         @field_validator('message')
         def hook_message_fallback(cls, v):
             if not v or str(v).strip() == "": raise ValueError('Message cannot be empty')
             return v
         @field_validator('node_id')
         def hook_nodeid_fallback(cls, v):
             if not v or str(v).strip() == "": raise ValueError('Node ID cannot be empty')
             if not str(v).startswith('!'): raise ValueError('Node ID must start with "!"')
             return v
         logger.warning("Using fallback validation for HookMessageRequest.")

class WebsiteMonitorRequest(PydanticBaseModel):
    """Request model for monitoring a website block and sending to Meshtastic."""
    url: str
    block_id: int
    prefix: str
    node_id: Optional[str] = None  
    channel: Optional[int] = Field(0, description="Channel index to use for sending")

def ensure_serializable(obj: Any) -> Any:
    """Recursively converts various types to JSON-serializable formats."""
    if isinstance(obj, PydanticBaseModel):
        if PYDANTIC_V2:
            try:

                return obj.model_dump(mode='json')
            except Exception as e:
                logger.warning(f"Error during Pydantic V2 model_dump for {type(obj)}: {e}. Falling back to dict.")

                return {k: ensure_serializable(v) for k, v in obj.model_dump().items()}
        else:

            return obj.dict()
    elif isinstance(obj, dict):
        return {k: ensure_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, deque)):
        return [ensure_serializable(item) for item in obj]
    elif isinstance(obj, set):

        return [ensure_serializable(item) for item in obj]
    elif isinstance(obj, bytes):
        try:

            return obj.decode('utf-8')
        except UnicodeDecodeError:

            return f"base64:{base64.b64encode(obj).decode('utf-8')}"
    elif isinstance(obj, datetime):

        return obj.isoformat()
    elif isinstance(obj, time.struct_time):
        try:

           return datetime.fromtimestamp(time.mktime(obj)).isoformat()
        except Exception:
           return str(obj) 

    elif hasattr(obj, '_pb') and hasattr(obj, 'DESCRIPTOR'):
        serializable_dict = {}
        for field in obj.DESCRIPTOR.fields:
            try:
                value = getattr(obj, field.name)

                if hasattr(value, '_pb') and hasattr(value, 'DESCRIPTOR'):

                     serializable_dict[field.name] = f"protobuf:{value.DESCRIPTOR.name}"
                else:
                     serializable_dict[field.name] = ensure_serializable(value)
            except Exception as e:
                logger.debug(f"Could not serialize protobuf field {field.name} for {type(obj)}: {e}")
                pass 
        return serializable_dict
    else:

        try:
            json.dumps(obj)
            return obj 
        except (TypeError, OverflowError):

            return str(obj)

class MeshtasticData:
    """In-memory store and processor for Meshtastic data."""
    def __init__(self, db_manager: DatabaseManager, max_packets_memory: int = 200):
        self.db = db_manager
        self.packets: deque[Dict] = deque(maxlen=max_packets_memory) 

        self.nodes: Dict[str, Dict] = self.db.get_all_nodes() 
        logger.info(f"Loaded {len(self.nodes)} nodes from Meshtastic database during initialization.")
        self.local_node_info: Optional[Dict] = None 
        self.local_node_id: Optional[str] = None
        self.connection_status: str = "Initializing"
        self.last_error: Optional[str] = None
        self.channel_map: Dict[Any, int] = {}
        logger.info("Initialized empty channel map (NOTE: This map is primarily built by set_local_node_info).")
        self.stats = {
            "packets_received_session": 0, "text_messages_session": 0,
            "position_updates_session": 0, "telemetry_reports_session": 0,
            "user_info_updates_session": 0, "waypoint_updates_session": 0,
            "other_packets_session": 0, "start_time": time.time(),
            "nodes_seen_session": set(self.nodes.keys()),
            "channels_seen_session": set()
        }
        self.packet_counter = 0

    def add_packet(self, packet: Dict) -> Optional[Dict]:

        logger.info(f"ADD_PACKET ENTRY: Input packet EventID(in):'{packet.get('event_id')}', FromID(in):'{packet.get('fromId')}', ToID(in):'{packet.get('toId')}', AppType(in):'{packet.get('app_packet_type')}', Channel(in):'{packet.get('channel')}', OrigChanID(in):'{packet.get('original_channel_id')}'")
        if not packet or not isinstance(packet, dict):
            logger.warning(f"ADD_PACKET REJECT: Invalid packet data type: {type(packet)}")
            return None
        try:
            processed_packet = packet.copy()
            logger.info(f"ADD_PACKET DEBUG: AFTER packet.copy() - processed_packet['fromId']: '{processed_packet.get('fromId')}', type: {type(processed_packet.get('fromId'))}, processed_packet['toId']: '{processed_packet.get('toId')}', type: {type(processed_packet.get('toId'))}")
            rx_time = processed_packet.get('rxTime', int(time.time()))
            processed_packet['rxTime'] = rx_time
            processed_packet['timestamp'] = float(rx_time)
            if 'event_id' not in processed_packet or not processed_packet['event_id']:
                processed_packet['event_id'] = f"pkt_{time.time_ns()}_{self.packet_counter}"
            self.packet_counter += 1
            logger.info(f"ADD_PACKET ID_TIME: Final EventID='{processed_packet['event_id']}', Timestamp='{processed_packet['timestamp']}'")
            if 'fromId' not in processed_packet or processed_packet.get('fromId') is None:
                from_num = processed_packet.get('from')
                if isinstance(from_num, int):
                    processed_packet['fromId'] = f"!{from_num:08x}"
                    logger.info(f"ADD_PACKET FROM_ID: Derived fromId='{processed_packet['fromId']}' from numeric 'from'={from_num} for EventID='{processed_packet['event_id']}'")
                else:
                    logger.info(f"ADD_PACKET FROM_ID: No 'fromId' in input or numeric 'from' found/valid. fromId remains/is set to '{processed_packet.get('fromId')}' for EventID='{processed_packet['event_id']}'")
            else:
                logger.info(f"ADD_PACKET FROM_ID: Using existing/provided fromId='{processed_packet['fromId']}' for EventID='{processed_packet['event_id']}'")
            if 'toId' not in processed_packet or processed_packet.get('toId') is None:
                to_num = processed_packet.get('to')
                if isinstance(to_num, int):
                    processed_packet['toId'] = "^all" if to_num == 0xFFFFFFFF else f"!{to_num:08x}"
                    logger.info(f"ADD_PACKET TO_ID: Derived toId='{processed_packet['toId']}' from numeric 'to'={to_num} for EventID='{processed_packet['event_id']}'")
                else:
                    logger.info(f"ADD_PACKET TO_ID: No 'toId' in input or numeric 'to' found/valid. toId remains/is set to '{processed_packet.get('toId')}' for EventID='{processed_packet['event_id']}'")
            else:
                logger.info(f"ADD_PACKET TO_ID: Using existing/provided toId='{processed_packet['toId']}' for EventID='{processed_packet['event_id']}'")
            raw_channel_from_input_packet = packet.get('channel')
            if 'original_channel_id' not in processed_packet:
                if raw_channel_from_input_packet is not None:
                    processed_packet['original_channel_id'] = raw_channel_from_input_packet
                    logger.info(f"ADD_PACKET ORIG_CHAN: Set 'original_channel_id' to '{raw_channel_from_input_packet}' from input packet's 'channel' field for EventID='{processed_packet['event_id']}'")
                else:
                    processed_packet['original_channel_id'] = None
                    logger.info(f"ADD_PACKET ORIG_CHAN: No 'channel' field in input packet to derive 'original_channel_id'. Set to None for EventID='{processed_packet['event_id']}'")
            else:
                logger.info(f"ADD_PACKET ORIG_CHAN: Using pre-existing 'original_channel_id'='{processed_packet['original_channel_id']}' for EventID='{processed_packet['event_id']}'")
            processing_channel_val: Optional[int] = None
            if raw_channel_from_input_packet is not None:
                try:
                    processing_channel_val = int(raw_channel_from_input_packet)
                except (ValueError, TypeError):
                    logger.warning(f"ADD_PACKET PROC_CHAN: Input 'channel' field '{raw_channel_from_input_packet}' is not a valid int for EventID='{processed_packet['event_id']}'. Defaulting to 0.")
                    processing_channel_val = 0
            else:
                logger.info(f"ADD_PACKET PROC_CHAN: Input packet 'channel' field missing for EventID='{processed_packet['event_id']}'. Defaulting to 0.")
                processing_channel_val = 0
            processed_packet['channel'] = processing_channel_val
            logger.info(f"ADD_PACKET CHANNELS_FINAL: EventID='{processed_packet['event_id']}', ProcChannel(for packets table)='{processed_packet['channel']}', OrigIntChannel(for messages table)='{processed_packet.get('original_channel_id')}'")
            packet_type, payload_data = self._classify_packet_type(processed_packet)
            processed_packet['app_packet_type'] = packet_type
            logger.info(f"ADD_PACKET CLASSIFIED: EventID='{processed_packet['event_id']}', AppType='{packet_type}', FromID='{processed_packet.get('fromId')}'")
            temp_from_id_before_ensure = processed_packet.get('fromId')
            temp_to_id_before_ensure = processed_packet.get('toId')
            processed_packet = ensure_serializable(processed_packet)
            if temp_from_id_before_ensure is not None and temp_from_id_before_ensure != processed_packet.get('fromId'):
                logger.warning(f"ADD_PACKET WARN: 'fromId' changed by ensure_serializable! Before: '{temp_from_id_before_ensure}', After: '{processed_packet.get('fromId')}' for EventID='{processed_packet['event_id']}'")
            if temp_to_id_before_ensure is not None and temp_to_id_before_ensure != processed_packet.get('toId'):
                logger.warning(f"ADD_PACKET WARN: 'toId' changed by ensure_serializable! Before: '{temp_to_id_before_ensure}', After: '{processed_packet.get('toId')}' for EventID='{processed_packet['event_id']}'")
        except Exception as e:
            event_id_for_log = processed_packet.get('event_id', packet.get('event_id', 'Unknown_EventID_In_Exception'))
            logger.error(f"ADD_PACKET FAIL: Failed initial processing/serialization of packet. EventID='{event_id_for_log}'. Error: {e}. Input Packet (brief): {{'fromId': '{packet.get('fromId')}', 'toId': '{packet.get('toId')}', 'channel': '{packet.get('channel')}'}}", exc_info=True)
            return None
        self.stats["packets_received_session"] += 1
        stat_map = {
            "Position": "position_updates_session", "Telemetry": "telemetry_reports_session",
            "Message": "text_messages_session", "User Info": "user_info_updates_session",
            "Waypoint": "waypoint_updates_session",
        }
        stat_key = stat_map.get(packet_type)
        if stat_key: self.stats[stat_key] += 1
        else:
            if packet_type not in ["Admin", "Unknown", "Encrypted", "Binary Data", "Other", "Routing", "Traceroute"]:
                logger.warning(f"Unmapped packet type for stats: {packet_type}")
            self.stats["other_packets_session"] += 1
        if processed_packet.get('fromId'): self.stats["nodes_seen_session"].add(processed_packet.get('fromId'))
        ch_for_stats = processed_packet.get('channel')
        if ch_for_stats is not None: self.stats["channels_seen_session"].add(ch_for_stats)
        self.packets.append(processed_packet)
        logger.info(f"ADD_PACKET DB CALL: Calling self.db.save_packet for EventID='{processed_packet.get('event_id')}', From='{processed_packet.get('fromId')}', To='{processed_packet.get('toId')}', Type='{processed_packet.get('app_packet_type')}', ProcChannel='{processed_packet.get('channel')}', OrigIntChannelField='{processed_packet.get('original_channel_id')}'")
        self.db.save_packet(processed_packet)
        current_from_id = processed_packet.get('fromId')
        if current_from_id:
            node_update_data = {'lastHeard': rx_time}
            if 'rxSnr' in processed_packet: node_update_data['snr'] = processed_packet['rxSnr']
            if 'rxRssi' in processed_packet: node_update_data['rssi'] = processed_packet['rxRssi']
            if packet_type == "Position" and payload_data:
                node_update_data['position'] = payload_data
                node_update_data['position_time'] = rx_time
            elif packet_type == "Telemetry" and payload_data:
                if payload_data.get('deviceMetrics'):
                    node_update_data['deviceMetrics'] = payload_data['deviceMetrics']
                node_update_data['telemetry_time'] = rx_time
            elif packet_type == "User Info" and payload_data:
                node_update_data['user'] = payload_data
                if 'hwModel' in payload_data:
                    node_update_data['hw_model'] = payload_data.get('hwModel')
            self.update_node(current_from_id, node_update_data, broadcast_change=True)
        else:
            logger.warning(f"ADD_PACKET: Skipping node update for EventID='{processed_packet['event_id']}' because fromId is missing or None.")
        if main_event_loop: 
            logger.info(f"ADD_PACKET SSE_BROADCAST: EventID='{processed_packet.get('event_id')}', From='{processed_packet.get('fromId')}', ProcChannel='{processed_packet.get('channel')}'")
            asyncio.run_coroutine_threadsafe(
                broadcast_data({"event": "packet", "data": processed_packet}),
                main_event_loop
            )
            asyncio.run_coroutine_threadsafe(broadcast_stats(), main_event_loop)
        return processed_packet

    def _classify_packet_type(self, packet: Dict) -> Tuple[str, Optional[Any]]:

        decoded = packet.get('decoded')
        if not isinstance(decoded, dict):
            if 'encrypted' in packet: return "Encrypted", packet.get('encrypted')
            payload = packet.get('payload')
            if isinstance(payload, bytes):
                try:
                    text = payload.decode('utf-8')
                    if len(text) > 0 and all(32 <= ord(c) <= 126 or c in '\r\n\t ' for c in text):
                        if not isinstance(packet.get('decoded'), dict): packet['decoded'] = {}
                        packet['decoded']['portnum'] = 'TEXT_MESSAGE_APP'
                        packet['decoded']['payload'] = text
                        return "Message", text
                except UnicodeDecodeError: pass
            return "Unknown", None
        portnum = decoded.get('portnum', 'UNKNOWN')
        portnum_str = portnum.name if hasattr(portnum, 'name') and isinstance(portnum.name, str) else str(portnum)
        if portnum_str == 'TEXT_MESSAGE_APP':
            payload = decoded.get('payload')
            if isinstance(payload, bytes):
                try: payload = payload.decode('utf-8')
                except UnicodeDecodeError: payload = f"base64:{base64.b64encode(payload).decode('utf-8')}"
            return "Message", str(payload) if payload is not None else ""
        elif portnum_str == 'POSITION_APP' and 'position' in decoded: return "Position", decoded['position']
        elif portnum_str == 'NODEINFO_APP' and 'user' in decoded: return "User Info", decoded['user']
        elif portnum_str == 'TELEMETRY_APP' and 'telemetry' in decoded: return "Telemetry", decoded['telemetry']
        elif portnum_str == 'ROUTING_APP': return "Routing", decoded.get('routing')
        elif portnum_str == 'TRACEROUTE_APP': return "Traceroute", decoded.get('traceroute')
        elif portnum_str == 'WAYPOINT_APP' and 'waypoint' in decoded: return "Waypoint", decoded['waypoint']
        elif portnum_str == 'ADMIN_APP': return "Admin", decoded.get('admin')
        if 'position' in decoded: return "Position", decoded['position']
        if 'telemetry' in decoded: return "Telemetry", decoded['telemetry']
        if 'user' in decoded: return "User Info", decoded['user']
        if 'waypoint' in decoded: return "Waypoint", decoded['waypoint']
        if 'payload' in decoded:
            payload = decoded['payload']
            if isinstance(payload, str): return "Message", payload
            if isinstance(payload, bytes):
                try:
                    text = payload.decode('utf-8')
                    if len(text) > 0 and sum(32 <= ord(c) <= 126 or c in '\r\n\t ' for c in text) / len(text) > 0.8:
                        return "Message", text
                    else: return "Binary Data", f"base64:{base64.b64encode(payload).decode('utf-8')}"
                except UnicodeDecodeError:
                    return "Binary Data", f"base64:{base64.b64encode(payload).decode('utf-8')}"
        return "Other", decoded

    def update_node(self, node_id_str: str, data: Dict, broadcast_change: bool = False) -> None:

        if not node_id_str or not data:
            logger.warning(f"Attempted to update node with empty ID ('{node_id_str}') or data.")
            return
        try:
            serializable_data = ensure_serializable(data.copy()) 
        except Exception as e:
            logger.error(f"Failed to serialize node update data for {node_id_str}: {e}. Data: {data}", exc_info=True)
            return
        is_new_node = node_id_str not in self.nodes
        if is_new_node:
            self.nodes[node_id_str] = {'node_id': node_id_str}
            logger.info(f"First time seeing node {node_id_str}, creating entry.")
            if node_id_str.startswith('!') and len(node_id_str) > 1:
                try: self.nodes[node_id_str]['node_num'] = int(node_id_str[1:], 16)
                except ValueError: pass
        def merge_dicts(target, source):
            if not isinstance(target, dict) or not isinstance(source, dict): return source
            for key, value in source.items():
                if value is None: continue
                if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                    merge_dicts(target[key], value)
                else: target[key] = value
            return target
        self.nodes[node_id_str] = merge_dicts(self.nodes.get(node_id_str, {'node_id': node_id_str}), serializable_data)
        if 'node_num' not in self.nodes[node_id_str] and node_id_str.startswith('!'):
            try: self.nodes[node_id_str]['node_num'] = int(node_id_str[1:], 16)
            except ValueError: logger.warning(f"Could not determine node_num for {node_id_str} after merge.")
        self.nodes[node_id_str]['node_id'] = node_id_str
        self.nodes[node_id_str]['last_updated'] = time.time()
        self.nodes[node_id_str]['isLocal'] = (node_id_str == self.local_node_id)
        if is_new_node: self.stats["nodes_seen_session"].add(node_id_str)
        db_node_data = self.nodes[node_id_str].copy()
        db_node_data['num'] = db_node_data.get('node_num')
        if 'channelSettings' not in db_node_data:
            if 'channels' in db_node_data: db_node_data['channelSettings'] = db_node_data.pop('channels')
            elif 'channel_info' in db_node_data: db_node_data['channelSettings'] = db_node_data.pop('channel_info')
        self.db.save_node(node_id_str, db_node_data)
        update_had_signal = ('snr' in serializable_data and serializable_data['snr'] is not None) or \
                            ('rssi' in serializable_data and serializable_data['rssi'] is not None)
        if update_had_signal:
            try: self.db.calculate_and_save_average_metrics()
            except Exception as e: logger.error(f"Error triggering average metrics update after node save: {e}", exc_info=True)
        if broadcast_change and main_event_loop: 
            node_data_to_broadcast = ensure_serializable(self.nodes[node_id_str])
            asyncio.run_coroutine_threadsafe(
                broadcast_data({"event": "node_update", "data": node_data_to_broadcast}), main_event_loop)
            if is_new_node:
                asyncio.run_coroutine_threadsafe(broadcast_stats(), main_event_loop)

    def set_local_node_info(self, info: Optional[Any]) -> None:

        global interface 
        old_local_node_id = self.local_node_id
        logger.debug(f"set_local_node_info called with info type: {type(info)}")
        if info: logger.debug(f"DEBUG: Attributes available on 'info' (myInfo) object: {dir(info)}")
        if interface and hasattr(interface, 'localNode'):
            logger.debug(f"DEBUG: interface.localNode is available.")
            if hasattr(interface.localNode, 'channels'): logger.debug(f"DEBUG: interface.localNode.channels content: {interface.localNode.channels} (type: {type(interface.localNode.channels)})")
            else: logger.debug("DEBUG: interface.localNode does not have a 'channels' attribute.")
        else: logger.debug("DEBUG: interface.localNode is not available.")
        self.channel_map.clear()
        logger.info("Cleared channel map.")
        if not info or not hasattr(info, 'my_node_num'):
            logger.warning(f"Setting local node info to None. Invalid info object: {type(info)}")
            self.local_node_info = None
            self.local_node_id = None
            if old_local_node_id and old_local_node_id in self.nodes:
                self.nodes[old_local_node_id]['isLocal'] = False
                if main_event_loop: 
                    old_node_data_serializable = ensure_serializable(self.nodes[old_local_node_id]) 
                    asyncio.run_coroutine_threadsafe(
                        broadcast_data({"event": "node_update", "data": old_node_data_serializable}), main_event_loop)
            if main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "local_node_info", "data": None}), main_event_loop)
            return
        try:
            node_id_num = info.my_node_num
            node_id_str = f"!{node_id_num:08x}" if isinstance(node_id_num, int) else None
            if not node_id_str:
                logger.error(f"Could not determine valid local node ID string from num: {node_id_num}")
                self.set_local_node_info(None)
                return
            if old_local_node_id and old_local_node_id != node_id_str and old_local_node_id in self.nodes:
                self.nodes[old_local_node_id]['isLocal'] = False
                if main_event_loop:
                    old_node_data_serializable = ensure_serializable(self.nodes[old_local_node_id])
                    asyncio.run_coroutine_threadsafe(
                        broadcast_data({"event": "node_update", "data": old_node_data_serializable}), main_event_loop)
            self.local_node_id = node_id_str
            logger.info(f"Local node identified: {self.local_node_id} (Num: {node_id_num})")
            user_info_obj = getattr(info, 'user', None)
            pos_info_obj = getattr(info, 'position', None)
            metrics_obj = getattr(info, 'device_metrics', None)
            module_conf_obj = getattr(info, 'module_config', None)
            channels_list_obj = None
            local_conf_obj = interface.localNode if interface else None
            if local_conf_obj and hasattr(local_conf_obj, 'channels') and local_conf_obj.channels:
                channels_list_obj = local_conf_obj.channels
                logger.debug("Using channels list from 'interface.localNode' object.")
            elif hasattr(info, 'channels') and isinstance(getattr(info, 'channels', None), list):
                channels_list_obj = getattr(info, 'channels')
                logger.debug("Using channels list from 'info' (myInfo) object as fallback.")
            else:
                logger.warning("Could not find a valid 'channels' list on 'interface.localNode' or 'info'. Channel map will be empty.")
                channels_list_obj = []
            processed_channels_data = []
            map_built_count = 0
            if channels_list_obj:
                logger.info(f"Building channel map from {len(channels_list_obj)} channels...")
                sorted_channels = sorted(channels_list_obj, key=lambda c: getattr(c, 'index', float('inf')))
                for channel_obj in sorted_channels:
                    ch_data = {}
                    settings = getattr(channel_obj, 'settings', None)
                    role_enum = getattr(channel_obj, 'role', None)
                    user_index = getattr(channel_obj, 'index', None)
                    logger.debug(f"  Processing channel - User Index: {user_index}, Settings: {'Exists' if settings else 'None'}, Role Enum: {role_enum}")
                    ch_data['index'] = user_index if user_index is not None else 'Unknown'
                    role_str = 'N/A'
                    try:
                        if 'meshtastic' in sys.modules and hasattr(sys.modules['meshtastic'], 'channel_pb2'):
                            role_str = sys.modules['meshtastic'].channel_pb2.Channel.Role.Name(role_enum) if role_enum is not None else 'N/A'
                        else: raise NameError("meshtastic.channel_pb2 not available or imported")
                    except NameError: role_str = str(role_enum) if role_enum is not None else 'N/A'
                    except Exception as role_err: logger.warning(f"Error getting channel role name for enum {role_enum}: {role_err}"); role_str = str(role_enum) if role_enum is not None else 'N/A'
                    ch_data['role'] = role_str
                    if settings:
                        psk_bytes = getattr(settings, 'psk', None)
                        modem_cfg = getattr(settings, 'modem_config', None)
                        ch_id_bytes = getattr(settings, 'channel_id', None)
                        internal_numeric_id = getattr(settings, 'id', None)
                        logger.debug(f"    Channel Settings found - name: {getattr(settings, 'name', None)}, psk: {'Yes' if psk_bytes else 'No'}, modemConfig: {modem_cfg}, channelId (protobuf): {ch_id_bytes}, numericId: {internal_numeric_id}")
                        ch_data['settings'] = {
                            "name": getattr(settings, 'name', None), "psk": psk_bytes.hex() if psk_bytes else None,
                            "modemConfig": modem_cfg,
                            "channelId": ch_id_bytes.hex() if isinstance(ch_id_bytes, bytes) else str(ch_id_bytes) if ch_id_bytes is not None else None,
                            "numericId": internal_numeric_id,
                            "uplinkEnabled": getattr(settings, 'uplink_enabled', False),
                            "downlinkEnabled": getattr(settings, 'downlink_enabled', False), }
                        if user_index is not None and internal_numeric_id is not None:
                            try:
                                map_key = int(internal_numeric_id)
                                logger.info(f"    -> Adding to channel map: Internal ID {map_key} maps to User Index {user_index}")
                                self.channel_map[map_key] = user_index
                                map_built_count += 1
                            except (ValueError, TypeError) as e:
                                logger.warning(f"    -> Could not use channel settings 'id' ({internal_numeric_id}) as int map key: {e}. Skipping map entry for index {user_index}")
                        else: logger.warning(f"    -> Could not fully map channel: User Index={user_index}, NumericInternalID={internal_numeric_id}. Missing required value.")
                    else:
                        ch_data['settings'] = None
                        logger.warning(f"  Channel at index {user_index} has no 'settings' attribute. Cannot map.")
                    processed_channels_data.append(ch_data)
                if map_built_count > 0: logger.info(f"Channel map build complete. Mapped {map_built_count} channels. Final map: {self.channel_map}")
                else: logger.warning("Channel map is empty after processing local channels.")
            node_data = {
                'node_id': node_id_str, 'node_num': node_id_num,
                'user': ensure_serializable(user_info_obj) if user_info_obj else {},
                'position': ensure_serializable(pos_info_obj) if pos_info_obj else {},
                'deviceMetrics': ensure_serializable(metrics_obj) if metrics_obj else {},
                'firmware_version': getattr(info, 'firmware_version', None),
                'hw_model': getattr(info, 'hw_model_str', getattr(info, 'hw_model', None)),
                'role': str(getattr(info, 'role', None)), 'isLocal': True, 'lastHeard': int(time.time()),
                'last_updated': time.time(),
                'moduleConfig': ensure_serializable(module_conf_obj) if module_conf_obj else {},
                'channels': processed_channels_data, 'snr': None, 'rssi': None, 'num': node_id_num,
                'firmwareVersion': getattr(info, 'firmware_version', None),
                'hwModelStr': getattr(info, 'hw_model_str', getattr(info, 'hw_model', None)),
                'channelSettings': processed_channels_data }
            if node_data.get('position'):
                pos = node_data['position']
                node_data['position_time'] = pos.get('time', int(time.time()))
                lat_i, lon_i = pos.get('latitudeI'), pos.get('longitudeI')
                if lat_i is not None and lon_i is not None: node_data['latitude'], node_data['longitude'] = lat_i / 1e7, lon_i / 1e7
                else: node_data['latitude'], node_data['longitude'] = pos.get('latitude'), pos.get('longitude')
                node_data['altitude'] = pos.get('altitude')
            if node_data.get('deviceMetrics'):
                metrics = node_data['deviceMetrics']
                node_data['telemetry_time'] = metrics.get('time', int(time.time()))
                node_data['battery_level'] = metrics.get('batteryLevel'); node_data['voltage'] = metrics.get('voltage')
                node_data['channel_utilization'] = metrics.get('channelUtilization'); node_data['air_util_tx'] = metrics.get('airUtilTx')
            logger.debug(f"DEBUG: node_data prepared for update_node: {node_data}")
            self.update_node(node_id_str, node_data, broadcast_change=False) 
            local_node_channels_for_json = node_data.get('channels', [])
            logger.debug(f"DEBUG: Data used for channels_json: {local_node_channels_for_json}")
            self.local_node_info = {
                "node_id": node_id_str, "node_num": node_id_num,
                "name": node_data.get('user', {}).get('longName', f'Node {node_id_num}'),
                "firmware": node_data.get('firmware_version'), "hardware": node_data.get('hw_model'),
                "battery": node_data.get('battery_level'), "voltage": node_data.get('voltage'),
                "channels_json": json.dumps(local_node_channels_for_json, separators=(',', ':')),
                "position": node_data.get('position'), }
            logger.debug(f"DEBUG: Final self.local_node_info: {self.local_node_info}")
            local_node_info_serializable = ensure_serializable(self.local_node_info)
            if main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "local_node_info", "data": local_node_info_serializable}), main_event_loop)
                if node_id_str in self.nodes:
                    node_data_to_broadcast = ensure_serializable(self.nodes[node_id_str])
                    asyncio.run_coroutine_threadsafe(
                        broadcast_data({"event": "node_update", "data": node_data_to_broadcast}), main_event_loop)
        except Exception as e:
            logger.exception(f"Error setting local node info: {e}. Info object type: {type(info)}")
            self.set_local_node_info(None)

    def set_connection_status(self, new_status: str) -> None:
        if self.connection_status != new_status:
            logger.info(f"Connection status changed to: {new_status}")
            self.connection_status = new_status
            if main_event_loop: 
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "connection_status", "data": new_status}), main_event_loop)

    def set_error(self, error: Optional[str]) -> None:
        self.last_error = error
        if error: logger.error(f"Meshtastic Error Set: {error}")
        if main_event_loop: 
            asyncio.run_coroutine_threadsafe(
                broadcast_data({"event": "error", "data": error}), main_event_loop)

    def get_serializable_stats(self) -> Dict:
        stats_copy = self.stats.copy()
        stats_copy["nodes_seen_session"] = len(self.stats["nodes_seen_session"])
        stats_copy["channels_seen_session"] = len(self.stats["channels_seen_session"])
        stats_copy["elapsed_time_session"] = time.time() - self.stats["start_time"]
        return ensure_serializable(stats_copy) 

    def get_formatted_packets_from_memory(self, limit: int = 50) -> List[Dict]:
        num_items = min(limit, len(self.packets))
        return list(reversed([self.packets[i] for i in range(len(self.packets) - 1, len(self.packets) - 1 - num_items, -1)]))

meshtastic_data = MeshtasticData(db_manager, max_packets_memory=MAX_PACKETS_IN_MEMORY)

background_tasks = set()
interface: Optional[meshtastic.tcp_interface.TCPInterface] = None
main_event_loop = None 
sse_queues: List[asyncio.Queue] = []
sse_queues_lock = asyncio.Lock()

def _remove_keys_from_config_file_util(config_path: str, keys_to_remove: List[str]) -> bool:
    """
    Reads a config file, removes lines defining specified keys, and rewrites the file.
    Returns True if successful or if no changes were needed, False on error.
    This is a synchronous function.
    """
    logger.info(f"Attempting to remove keys {keys_to_remove} from config file: {config_path}")
    if not os.path.exists(config_path):
        logger.warning(f"Config file not found at {config_path}. Cannot remove keys.")
        return True 

    temp_file_path = config_path + ".tmp_admin_remove"
    try:
        with open(config_path, "r") as f_read:
            lines = f_read.readlines()

        keys_actually_removed = False
        new_lines = []
        for line in lines:
            stripped_line = line.strip()
            should_remove_this_line = False
            if stripped_line and not stripped_line.startswith("#") and "=" in stripped_line:
                key_part = stripped_line.split("=", 1)[0].strip()
                if key_part in keys_to_remove:
                    should_remove_this_line = True
                    keys_actually_removed = True

            if not should_remove_this_line:
                new_lines.append(line)
            else:
                logger.info(f"Removing line from config: {line.strip()}")

        if keys_actually_removed:
            with open(temp_file_path, "w") as f_write:
                f_write.writelines(new_lines)
            os.replace(temp_file_path, config_path) 
            logger.info(f"Successfully removed keys {keys_to_remove} and updated config file: {config_path}")
        else:
            logger.info(f"Keys {keys_to_remove} not found in config file. No changes made to file content.")

        return True

    except IOError as e:
        logger.error(f"IOError modifying config file {config_path} to remove keys: {e}", exc_info=True)
        if os.path.exists(temp_file_path):
            try: os.remove(temp_file_path)
            except: pass
        return False
    except Exception as e:
        logger.error(f"Unexpected error modifying config file {config_path} to remove keys: {e}", exc_info=True)
        if os.path.exists(temp_file_path):
            try: os.remove(temp_file_path)
            except: pass
        return False

@asynccontextmanager
async def lifespan(app_instance: FastAPI): 
    """Handles application startup and shutdown events."""
    global main_event_loop, interface, loaded_config, db_manager, CONFIG_FILE_PATH, background_tasks

    app_instance.version = "2.4.5-auth"
    logger.info(f"--- Starting Meshtastic Dashboard Server (v{app_instance.version}) ---")
    main_event_loop = asyncio.get_running_loop()

    initial_username = loaded_config.get("INITIAL_ADMIN_USERNAME")
    initial_password = loaded_config.get("INITIAL_ADMIN_PASSWORD")

    if initial_username and initial_password:
        logger.info(f"Found initial admin credentials in config for user: '{initial_username}'. Processing...")

        existing_user = await asyncio.to_thread(db_manager.get_user, initial_username)

        user_creation_successful = False
        if existing_user:
            logger.warning(f"Initial admin user '{initial_username}' already exists in the database. Skipping database creation.")
            user_creation_successful = True 
        else:
            logger.info(f"Attempting to create initial admin user '{initial_username}' in the database.")

            hashed_password = get_password_hash(initial_password) 

            created_user_info = await asyncio.to_thread(db_manager.create_user, initial_username, hashed_password)

            if created_user_info:
                logger.info(f"Successfully created initial admin user '{initial_username}'.")
                user_creation_successful = True
            else:
                logger.error(f"Failed to create initial admin user '{initial_username}' in the database. Credentials will NOT be removed from config. Please check for errors (e.g., DB permissions, unique constraint).")
                user_creation_successful = False

        if user_creation_successful:
            keys_to_remove = ["INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_PASSWORD"]

            removal_success = await asyncio.to_thread(
                _remove_keys_from_config_file_util,
                CONFIG_FILE_PATH, 
                keys_to_remove
            )
            if removal_success:
                logger.info("Initial admin credentials have been processed and successfully removed/verified absent from the config file.")

                if "INITIAL_ADMIN_USERNAME" in loaded_config:
                    del loaded_config["INITIAL_ADMIN_USERNAME"]
                if "INITIAL_ADMIN_PASSWORD" in loaded_config:
                    del loaded_config["INITIAL_ADMIN_PASSWORD"]
            else:
                logger.error(f"Failed to remove initial admin credentials from the config file '{CONFIG_FILE_PATH}'. Please check file permissions or remove them manually for security.")
    else:
        logger.info("No initial admin username/password found in config. Skipping automatic user setup from config.")

    try:
        logger.info("Initializing Task database...")
        init_tasks_db() 
    except Exception as e:
        logger.critical(f"CRITICAL: Task Database initialization failed: {e}", exc_info=True)
        sys.exit(1)

    if AUTO_REPLY_ENABLED and init_auto_reply_db: 
        try:
            logger.info("Initializing Auto-Reply database table...")
            init_auto_reply_db()
            logger.info("Auto-Reply database table initialization complete.")
        except Exception as e:
            logger.error(f"Failed to initialize Auto-Reply database table: {e}", exc_info=True)
    elif not AUTO_REPLY_ENABLED:
        logger.warning("Auto-Reply functionality is disabled, skipping DB init.")

    tasks_to_create = {
        "meshtastic_connector": connect_to_meshtastic, 
        "stats_updater": update_stats_periodically, 
        "connection_checker": check_connection_periodically, 
        "history_pruner": prune_history_periodically, 
        "task_scheduler": run_scheduler_periodically 
    }

    logger.info("Starting background tasks...")
    for name, coro_func in tasks_to_create.items():
        if coro_func and callable(coro_func):
            task = asyncio.create_task(coro_func(), name=name)
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            logger.info(f"Background task '{name}' started.")
        else:
            logger.error(f"Could not start background task '{name}' because its function is missing or not callable.")

    logger.info(f"Server listening on http://{WEBSERVER_HOST}:{WEBSERVER_PORT}")
    logger.info(f"API documentation available at http://{WEBSERVER_HOST}:{WEBSERVER_PORT}/docs")
    logger.info(f"Login page available at http://{WEBSERVER_HOST}:{WEBSERVER_PORT}/login")
    logger.info(f"Attempting initial connection to Meshtastic at tcp://{TARGET_HOST}:{TARGET_PORT}")
    logger.info(f"Using Meshtastic data DB: {os.path.abspath(DB_PATH)}")
    try:
        tasks_db_path_to_log = getattr(sys.modules.get('tasks_api', {}), 'TASKS_DB_PATH', 'tasks.db')
        if not os.path.isabs(tasks_db_path_to_log):
            tasks_db_full_path = os.path.abspath(os.path.join(SCRIPT_DIR, tasks_db_path_to_log))
        else:
            tasks_db_full_path = tasks_db_path_to_log
        logger.info(f"Using Tasks scheduling DB: {tasks_db_full_path}")
    except Exception:
        logger.warning("Could not determine absolute path for tasks.db from tasks_api module.")
    logger.info(f"Average metrics history retention: {AVERAGE_METRICS_HISTORY_DAYS} days")
    logger.info(f"Max recent packets kept in memory: {MAX_PACKETS_IN_MEMORY}")

    yield 

    logger.info("--- Shutting down Meshtastic Dashboard Server ---")
    tasks_to_cancel = list(background_tasks)
    if tasks_to_cancel:
        logger.info(f"Cancelling {len(tasks_to_cancel)} background task(s)...")
        for task_item in tasks_to_cancel:
            if not task_item.done(): task_item.cancel()
        results = await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        background_tasks.clear()
        logger.debug(f"Background task cancellation results: {results}")
        logger.info("Background tasks cancelled.")

    if interface:
        logger.info("Closing Meshtastic interface...")
        try:
            await asyncio.to_thread(interface.close)
        except Exception as e:
            logger.error(f"Error closing Meshtastic interface: {e}", exc_info=True)
        interface = None
    logger.info("--- Shutdown complete ---")

app = FastAPI(
    title="Enhanced Meshtastic Dashboard API",
    description="API for monitoring Meshtastic networks with SSE, history, average metrics, task scheduling, auto-reply, and authentication. Includes performance improvements.",
    version="2.4.5-auth",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/login", response_class=HTMLResponse, tags=["Authentication"], include_in_schema=False)
async def login_page_get(request: Request, error: Optional[str] = None):

    if not os.path.exists(LOGIN_HTML_PATH):
        logger.error(f"Login page HTML file not found at {LOGIN_HTML_PATH}")
        return HTMLResponse("<h1>Login Page Not Found (500)</h1><p>Server configuration error.</p>", status_code=500)

    try:
        with open(LOGIN_HTML_PATH, "r") as f:
            content = f.read()
        if error:
            error_html = f'<p id="error-message" style="color: red; text-align: center; padding: 10px; background-color: #ffebee; border: 1px solid #ef9a9a; border-radius: 4px; margin-bottom: 15px;">{error}</p>'
            if '<form' in content:
                content = content.replace('<form', f'{error_html}\n<form', 1)
            elif '<body>' in content:
                content = content.replace('<body>', f'<body>{error_html}', 1)
            else:
                content = error_html + content
        return HTMLResponse(content)
    except Exception as e:
        logger.error(f"Error reading or modifying login page content: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Login</h1><p style='color: red;'>{error if error else 'Error displaying login page.'}</p>", status_code=500)

@app.post("/login", tags=["Authentication"], include_in_schema=False)
async def login_for_access_token_post(
    response: Response,
    username: str = Form(...),
    password: str = Form(...)
):
    user_dict = await asyncio.to_thread(db_manager.get_user, username) 

    if not user_dict or not verify_password(password, user_dict["hashed_password"]) or user_dict.get("disabled"): 
        logger.warning(f"Failed login attempt for username: {username}")
        return RedirectResponse(url="/login?error=Invalid username or password", status_code=status.HTTP_302_FOUND)

    access_token_expires = timedelta(minutes=AUTH_TOKEN_EXPIRE_MINUTES) 
    access_token = create_access_token( 
        data={"sub": user_dict["username"]}, expires_delta=access_token_expires
    )

    redirect_response_obj = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    redirect_response_obj.set_cookie(
        key="access_token", value=f"Bearer {access_token}", httponly=True,
        max_age=int(access_token_expires.total_seconds()), expires=int(access_token_expires.total_seconds()),
        samesite="Lax", secure=False, path="/"
    )
    logger.info(f"User '{username}' logged in successfully.")
    return redirect_response_obj

@app.get("/logout", tags=["Authentication"], include_in_schema=False)
async def logout_user():
    logger.info("User logging out.")
    response_obj = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response_obj.delete_cookie("access_token", path="/")
    return response_obj

app.include_router(tasks_router, prefix="/api/tasks", tags=["Tasks API"]) 

if SYSTEM_CONFIG_ENABLED and config_router: 
    app.include_router(config_router, prefix="/api/system/config", tags=["System Config API"])
    logger.info("System Config API router included at /api/system/config.")
else:
    logger.warning("System Config API router NOT included (feature disabled or import failed).")

if AUTO_REPLY_ENABLED: 
    app.include_router(auto_reply_router, prefix="/api/auto_reply", tags=["Auto Reply API"]) 
    logger.info("Auto-Reply API router included.")
else:
    logger.warning("Auto-Reply API router NOT included (feature disabled).")

STATIC_DIR = "static"
os.makedirs(STATIC_DIR, exist_ok=True)

INDEX_HTML_PATH = os.path.join(SCRIPT_DIR, "index.html")
NETWORK_HTML_PATH = os.path.join(SCRIPT_DIR, "network.html")
MAP_HTML_PATH = os.path.join(SCRIPT_DIR, "map.html")
DMES_HTML_PATH = os.path.join(SCRIPT_DIR, "dmes.html")

SETTINGS_HTML_PATH = os.path.join(SCRIPT_DIR, "settings.html")
SENSORS_HTML_PATH = os.path.join(SCRIPT_DIR, "sensors.html")
HOOK_HTML_PATH = os.path.join(SCRIPT_DIR, "hook.html")
TASKS_HTML_PATH = os.path.join(SCRIPT_DIR, "tasks.html")
AUTOREPLY_HTML_PATH = os.path.join(SCRIPT_DIR, "auto_reply.html")
DOX_HTML_PATH = os.path.join(SCRIPT_DIR, "api_documentation.html")
PUBLIC_HTML_PATH = os.path.join(SCRIPT_DIR, "channel_chat.html")
FAVICON_PATH = os.path.join(SCRIPT_DIR, STATIC_DIR, "favicon.ico")

if not os.path.exists(INDEX_HTML_PATH):
    logger.warning(f"{INDEX_HTML_PATH} not found. Serving placeholder page.")
    try:
        with open(INDEX_HTML_PATH, "w") as f:
            f.write("<!DOCTYPE html><html><head><title>Meshtastic Dashboard</title></head><body><h1>Meshtastic Dashboard</h1><p>Error: index.html not found.</p><p>Place your dashboard frontend file (index_5_8_db.html) here.</p></body></html>")
    except IOError as e:
        logger.error(f"Could not create placeholder index.html: {e}")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

def _add_cache_control_headers(response: Response):
    """Helper function to add cache control headers to a response."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache" 
    response.headers["Expires"] = "0" 
    return response

@app.get("/", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_dashboard(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the main dashboard HTML page."""
    logger.info(f"Serving {os.path.basename(INDEX_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(INDEX_HTML_PATH):
        response = FileResponse(INDEX_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.error(f"Critical: {INDEX_HTML_PATH} not found after check.")
        error_response = HTMLResponse("<h1>Error: Dashboard file not found.</h1>", status_code=500)
        return _add_cache_control_headers(error_response)

@app.get("/network", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_network_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the network visualization HTML page."""
    logger.info(f"Serving {os.path.basename(NETWORK_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(NETWORK_HTML_PATH):
        response = FileResponse(NETWORK_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.warning(f"{NETWORK_HTML_PATH} not found.")
        error_response = HTMLResponse("<!DOCTYPE html><html><body><h1>Network page not found</h1></body></html>", status_code=404)
        return _add_cache_control_headers(error_response)

@app.get("/map", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_map_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the map visualization HTML page."""
    logger.info(f"Serving {os.path.basename(MAP_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(MAP_HTML_PATH):
        response = FileResponse(MAP_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.warning(f"{MAP_HTML_PATH} not found.")
        error_response = HTMLResponse("<!DOCTYPE html><html><body><h1>Map page not found</h1></body></html>", status_code=404)
        return _add_cache_control_headers(error_response)

@app.get("/dmes", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_dmes_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the dmes visualization HTML page."""
    logger.info(f"Serving {os.path.basename(DMES_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(DMES_HTML_PATH):
        response = FileResponse(DMES_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.warning(f"{DMES_HTML_PATH} not found.")
        error_response = HTMLResponse("<!DOCTYPE html><html><body><h1>Direct Message page not found</h1></body></html>", status_code=404)
        return _add_cache_control_headers(error_response)

@app.get("/community", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_community_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """
    Fetches community/map HTML from an external API using the local node ID
    and serves it. Requires authentication.
    """
    client_host = request.client.host if request.client else 'unknown'
    logger.info(f"Serving external community page for /community route to {client_host} (User: {current_user.username})")

    local_node_id = meshtastic_data.local_node_id 
    if not local_node_id:
        logger.warning("Cannot fetch external community page: Local node ID not available.")
        error_response = HTMLResponse(
            "<h1>Error: Cannot fetch community page</h1><p>Local node ID is not available. Ensure the device is connected.</p>",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return _add_cache_control_headers(error_response)

    if not EXTERNAL_MAP_API_BASE_URL: 
        logger.error("External Community/Map API Base URL is not configured.")
        error_response = HTMLResponse(
            "<h1>Server Configuration Error</h1><p>The external community/map API URL is not set.</p>",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        return _add_cache_control_headers(error_response)

    target_url = f"{EXTERNAL_MAP_API_BASE_URL}?node_id={local_node_id}"
    logger.info(f"Fetching community page from external API: {target_url}")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client: 
            response_external = await client.get(target_url)
            response_external.raise_for_status()
            content_type = response_external.headers.get('content-type', '').lower()
            if 'text/html' not in content_type:
                logger.warning(f"External API ({target_url}) returned non-HTML content-type: {content_type}")
            logger.info(f"Successfully fetched community HTML from {target_url}")

            final_response = HTMLResponse(content=response_external.text, status_code=response_external.status_code)
            return _add_cache_control_headers(final_response)
    except httpx.TimeoutException:
        logger.error(f"Timeout occurred while fetching community page from {target_url}")
        error_response = HTMLResponse(
            f"<h1>Error Fetching Community Page</h1><p>The request to the external service timed out.</p><p>URL: {target_url}</p>",
            status_code=status.HTTP_504_GATEWAY_TIMEOUT
        )
        return _add_cache_control_headers(error_response)
    except httpx.RequestError as exc:
        logger.error(f"HTTP Request error fetching community page from {target_url}: {exc}")
        error_response = HTMLResponse(
            f"<h1>Error Fetching Community Page</h1><p>Could not connect to the external service.</p><p>Error: {exc}</p><p>URL: {target_url}</p>",
            status_code=status.HTTP_502_BAD_GATEWAY 
        )
        return _add_cache_control_headers(error_response)
    except httpx.HTTPStatusError as exc:
        logger.error(f"External API returned error status {exc.response.status_code} for {target_url}: {exc.response.text[:200]}...") 
        error_response = HTMLResponse(
            f"<h1>Error Fetching Community Page</h1><p>The external service returned an error (Status: {exc.response.status_code}).</p><p>URL: {target_url}</p>",
            status_code=status.HTTP_502_BAD_GATEWAY 
        )
        return _add_cache_control_headers(error_response)
    except Exception as e:
        logger.exception(f"Unexpected error fetching external community page for {target_url}: {e}")
        error_response = HTMLResponse(
            "<h1>Internal Server Error</h1><p>An unexpected error occurred while trying to fetch the community page.</p>",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        return _add_cache_control_headers(error_response)

@app.get("/settings", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_settings_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the Settings visualization HTML page."""
    logger.info(f"Serving {os.path.basename(SETTINGS_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(SETTINGS_HTML_PATH):
        response = FileResponse(SETTINGS_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.warning(f"{SETTINGS_HTML_PATH} not found.")
        error_response = HTMLResponse("<!DOCTYPE html><html><body><h1>Settings page not found</h1></body></html>", status_code=404)
        return _add_cache_control_headers(error_response)

@app.get("/channels", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_channels_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the public channel chat HTML page."""
    logger.info(f"Serving {os.path.basename(PUBLIC_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(PUBLIC_HTML_PATH):
        response = FileResponse(PUBLIC_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.warning(f"{PUBLIC_HTML_PATH} not found.")
        error_response = HTMLResponse("<!DOCTYPE html><html><body><h1>Public Chat page not found</h1></body></html>", status_code=404)
        return _add_cache_control_headers(error_response)

@app.get("/autoreply", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_autoreply_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the Auto Reply configuration HTML page."""
    logger.info(f"Serving {os.path.basename(AUTOREPLY_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(AUTOREPLY_HTML_PATH):
        response = FileResponse(AUTOREPLY_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.warning(f"{AUTOREPLY_HTML_PATH} not found.")
        error_response = HTMLResponse("<!DOCTYPE html><html><body><h1>Auto Reply page not found</h1></body></html>", status_code=404)
        return _add_cache_control_headers(error_response)

@app.get("/sensors", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_sensors_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the sensors data HTML page."""
    logger.info(f"Serving {os.path.basename(SENSORS_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(SENSORS_HTML_PATH):
        response = FileResponse(SENSORS_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.warning(f"{SENSORS_HTML_PATH} not found.")
        error_response = HTMLResponse("<!DOCTYPE html><html><body><h1>Sensors page not found</h1></body></html>", status_code=404)
        return _add_cache_control_headers(error_response)

@app.get("/hook", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_hook_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the webhook information HTML page."""
    logger.info(f"Serving {os.path.basename(HOOK_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(HOOK_HTML_PATH):
        response = FileResponse(HOOK_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.warning(f"{HOOK_HTML_PATH} not found.")
        error_response = HTMLResponse("<!DOCTYPE html><html><body><h1>Hooks page not found</h1></body></html>", status_code=404)
        return _add_cache_control_headers(error_response)

@app.get("/tasks", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_tasks_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the task scheduling HTML page."""
    logger.info(f"Serving {os.path.basename(TASKS_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(TASKS_HTML_PATH):
        response = FileResponse(TASKS_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.warning(f"{TASKS_HTML_PATH} not found.")
        error_response = HTMLResponse("<!DOCTYPE html><html><body><h1>Tasks page not found</h1></body></html>", status_code=404)
        return _add_cache_control_headers(error_response)

@app.get("/documentation", response_class=HTMLResponse, tags=["Dashboard Pages"])
async def get_dox_page(request: Request, current_user: User = Depends(get_current_active_user)): 
    """Serves the api_documentation visualization HTML page."""
    logger.info(f"Serving {os.path.basename(DOX_HTML_PATH)} to {request.client.host if request.client else 'unknown'} (User: {current_user.username})")
    if os.path.exists(DOX_HTML_PATH):
        response = FileResponse(DOX_HTML_PATH)
        return _add_cache_control_headers(response)
    else:
        logger.warning(f"{DOX_HTML_PATH} not found.")
        error_response = HTMLResponse("<!DOCTYPE html><html><body><h1>Api Documentation page not found</h1></body></html>", status_code=404)
        return _add_cache_control_headers(error_response)

@app.get("/favicon.ico", include_in_schema=False)
async def get_favicon(): 
    """Serves the favicon."""
    if os.path.exists(FAVICON_PATH):
        return FileResponse(FAVICON_PATH)
    return Response(status_code=status.HTTP_404_NOT_FOUND)

async def broadcast_data(data: Dict):

    if 'event' not in data or 'data' not in data: logger.error(f"Invalid data for broadcast: {data}"); return
    try:
        json_data_string = json.dumps(ensure_serializable(data['data']), separators=(',', ':'))
        event_dict_for_queue = {"event": data['event'], "data": json_data_string}
        if 'id' in data: event_dict_for_queue['id'] = data['id']
        if 'retry' in data: event_dict_for_queue['retry'] = data['retry']
    except (TypeError, ValueError) as e: logger.error(f"Error serializing SSE data: {e}. Data: {data}", exc_info=False); return
    except Exception as e: logger.error(f"Unexpected error preparing SSE data: {e}. Data: {data}", exc_info=True); return
    async with sse_queues_lock:
        if sse_queues:
            tasks = [q.put(event_dict_for_queue) for q in sse_queues]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception): logger.log(logging.DEBUG if logger.isEnabledFor(logging.DEBUG) else logging.WARNING, f"Error putting SSE data to queue {i}: {result}")

async def broadcast_stats():

    try: await broadcast_data({"event": "stats", "data": meshtastic_data.get_serializable_stats()})
    except Exception as e: logger.error(f"Error broadcasting stats: {e}", exc_info=True)

@app.get("/sse", tags=["Realtime API"])
async def sse_endpoint(request: Request): 
    client_queue = asyncio.Queue()

    async with sse_queues_lock:
        sse_queues.append(client_queue)
    client_host = request.client.host if request.client else "Unknown"
    logger.info(f"SSE client connected: {client_host} (Total connected: {len(sse_queues)})")

    async def event_generator() -> AsyncGenerator[Dict[str, Any], None]:
        client_disconnected = False
        logger.info(f"SSE event_generator starting for {client_host}")
        try:
            logger.debug(f"Sending initial state to SSE client {client_host}")
            def create_event_dict(event_name: str, data_payload: Any) -> Dict[str, Any]:
                return {"event": event_name, "data": json.dumps(ensure_serializable(data_payload), separators=(',', ':'))}

            yield create_event_dict("connection_status", meshtastic_data.connection_status)
            if meshtastic_data.local_node_info:
                yield create_event_dict("local_node_info", meshtastic_data.local_node_info)
            yield create_event_dict("nodes", list(meshtastic_data.nodes.values())) 
            yield create_event_dict("stats", meshtastic_data.get_serializable_stats())
            recent_packets = meshtastic_data.get_formatted_packets_from_memory(limit=50)
            for packet_item in recent_packets: 
                yield create_event_dict("packet", packet_item)
            logger.debug(f"Initial state sent to SSE client {client_host}")
            logger.info(f"Entering SSE update loop for {client_host}")
            while not client_disconnected:
                try:
                    event_dict_from_queue = await asyncio.wait_for(client_queue.get(), timeout=20.0)
                    if await request.is_disconnected():
                        logger.info(f"SSE client {client_host} disconnected (waiting).")
                        client_disconnected = True; break
                    yield event_dict_from_queue
                    client_queue.task_done()
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        logger.info(f"SSE client {client_host} disconnected (timeout).")
                        client_disconnected = True; break
                    else:
                        yield { "comment": "heartbeat" }
                except asyncio.CancelledError:
                    logger.info(f"SSE generator task cancelled for {client_host}.")
                    client_disconnected = True; break
                except Exception as e:
                    logger.error(f"Error in SSE queue for {client_host}: {e}", exc_info=True)
                    client_disconnected = True; break
        except asyncio.CancelledError:
            logger.info(f"SSE event_generator cancelled early for {client_host}.")
            client_disconnected = True
        except Exception as e:
            logger.error(f"Error in SSE event_generator setup for {client_host}: {e}", exc_info=True)
            client_disconnected = True
        finally:
            logger.info(f"SSE event_generator finally block for {client_host}. Removing queue.")
            async with sse_queues_lock:
                if client_queue in sse_queues:
                    sse_queues.remove(client_queue)
            logger.info(f"SSE client {client_host} disconnected. Queues remaining: {len(sse_queues)}")
    return EventSourceResponse(event_generator(), media_type="text/event-stream", ping=15)

TOPIC_RECEIVED = "meshtastic.receive"
TOPIC_CONNECTION_ESTABLISHED = "meshtastic.connection.established"
TOPIC_CONNECTION_LOST = "meshtastic.connection.lost"
TOPIC_CONNECTION_FAILED = "meshtastic.connection.failed"
TOPIC_NODE_UPDATED = "meshtastic.node.updated"

def on_receive(packet, interface):

    logger.debug(f"on_receive called. Raw packet data: {packet}")

    original_internal_channel_id = packet.get('channel')
    logger.debug(f"on_receive: Packet from {packet.get('from', 'N/A')} arrived with original internal channel ID: {original_internal_channel_id}")

    global main_event_loop 

    try:

        if 'channel' not in packet or original_internal_channel_id is None:
            logger.warning(f"Packet missing 'channel' field BEFORE processing. Raw content: {packet}")

        processed_packet = meshtastic_data.add_packet(packet) 

        if processed_packet and main_event_loop:

            logger.debug(f"on_receive: Broadcasting processed packet {processed_packet.get('event_id')} with mapped channel index: {processed_packet.get('channel')}")
            asyncio.run_coroutine_threadsafe(
                broadcast_data({"event": "packet", "data": processed_packet}),
                main_event_loop
            )

            asyncio.run_coroutine_threadsafe(broadcast_stats(), main_event_loop)

        if AUTO_REPLY_ENABLED and check_message_for_auto_reply and db_get_auto_reply_rules and processed_packet and main_event_loop and interface:

            packet_type = processed_packet.get('app_packet_type')
            if packet_type == "Message" and processed_packet.get('decoded'):
                decoded_data = processed_packet['decoded']

                text_payload = decoded_data.get('payload')
                if isinstance(text_payload, bytes):
                    try: text_payload = text_payload.decode('utf-8')
                    except UnicodeDecodeError: text_payload = None 
                elif not isinstance(text_payload, str):
                    text_payload = None 

                sender_id = processed_packet.get('fromId')

                channel_index = processed_packet.get('channel') 
                if channel_index is None:
                    channel_index = 0  
                    logger.debug(f"Auto-Reply: No mapped channel index available in processed packet, defaulting reply logic to channel 0")
                else:
                    logger.debug(f"Auto-Reply: Using mapped channel index {channel_index} for rule check.")

                if text_payload and sender_id:
                    logger.debug(f"Checking for auto-reply triggers in message from {sender_id} on effective channel {channel_index}: '{text_payload}'")
                    try:

                        def run_auto_reply_check(): 
                            nonlocal sender_id, text_payload, channel_index, interface 

                            try:

                                logger.debug("Auto-Reply Thread: Fetching enabled rules...")
                                enabled_rules = db_get_auto_reply_rules(only_enabled=True) 
                                logger.debug(f"Auto-Reply Thread: Found {len(enabled_rules)} enabled rules.")

                                if enabled_rules:

                                    logger.debug("Auto-Reply Thread: Checking message against rules...")
                                    replies_to_send = check_message_for_auto_reply(
                                        text_payload,
                                        sender_id,
                                        channel_index, 
                                        enabled_rules
                                    )
                                    logger.debug(f"Auto-Reply Thread: Found {len(replies_to_send)} replies to send.")

                                    if replies_to_send:

                                        node_data = None
                                        try:

                                            logger.debug(f"Auto-Reply Thread: Looking up node data for {sender_id}...")
                                            if hasattr(meshtastic_data, 'nodes') and sender_id in meshtastic_data.nodes:
                                                node_data = meshtastic_data.nodes.get(sender_id)
                                                logger.debug(f"AUTO_REPLY: Found node data in memory for {sender_id}")

                                            if not node_data: 
                                                node_data = {"node_id": sender_id}
                                                logger.debug(f"AUTO_REPLY: Using fallback node data for {sender_id}")

                                            if "node_id" not in node_data: node_data["node_id"] = sender_id

                                            logger.debug(f"AUTO_REPLY: Node data for placeholder replacement: {node_data is not None}")

                                        except Exception as node_err:
                                            logger.warning(f"AUTO_REPLY: Error retrieving node data for {sender_id}: {node_err}")
                                            node_data = {"node_id": sender_id}  

                                        for reply in replies_to_send:
                                            dest = reply.get('destination')
                                            msg_template = reply.get('message')
                                            chan = reply.get('channel') 
                                            if chan is None:
                                                chan = 0  
                                                logger.debug(f"No channel specified in reply rule, defaulting reply to channel 0")

                                            if dest and msg_template:

                                                try:
                                                    personalized_msg = replace_placeholders(msg_template, node_data)
                                                    if personalized_msg != msg_template:
                                                        logger.debug(f"AUTO_REPLY: Replaced placeholders. Original: '{msg_template}', Result: '{personalized_msg}'")
                                                except Exception as template_err:
                                                    logger.error(f"AUTO_REPLY: Error replacing placeholders: {template_err}", exc_info=True)
                                                    personalized_msg = msg_template 

                                                logger.info(f"Sending auto-reply to {dest} on channel {chan}: '{personalized_msg}'")
                                                try:
                                                    interface.sendText(
                                                        personalized_msg,
                                                        destinationId=dest,
                                                        channelIndex=chan,
                                                        wantAck=False
                                                    )
                                                    time.sleep(0.5) 
                                                except Exception as send_err:
                                                    logger.error(f"Failed to send auto-reply to {dest}: {send_err}", exc_info=True)
                            except Exception as auto_reply_inner_err:
                                logger.error(f"Error during threaded auto-reply check/send: {auto_reply_inner_err}", exc_info=True)

                        asyncio.run_coroutine_threadsafe(
                            asyncio.to_thread(run_auto_reply_check),
                            main_event_loop
                        )

                    except Exception as auto_reply_schedule_err:
                        logger.error(f"Error scheduling auto-reply check: {auto_reply_schedule_err}", exc_info=True)

    except Exception as e:

        logger.exception(f"Error in on_receive callback processing packet: {packet}. Error: {e}")

def on_connection(interface, topic=pub.AUTO_TOPIC):
    """Callback for Meshtastic connection status changes."""
    logger.debug(f"on_connection called with topic: {topic}")
    try:

        topic_str = ""
        try:
            topic_obj = pub.getCurrentTopic()
            if topic_obj: topic_str = topic_obj.getName()
        except Exception: 
            if hasattr(topic, 'getName'): topic_str = topic.getName()
            elif topic != pub.AUTO_TOPIC: topic_str = str(topic)

        logger.debug(f"Meshtastic Connection event identified: {topic_str}")
        new_status = meshtastic_data.connection_status
        error_msg = None

        if TOPIC_CONNECTION_ESTABLISHED in topic_str:
            new_status = "Connected"
            logger.info("Meshtastic connection established.")
            meshtastic_data.set_error(None) 

            wait_time = 5 
            logger.info(f"Connection established. Waiting {wait_time}s for node info to stabilize before building channel map...")

            time.sleep(wait_time)
            logger.info("Wait complete. Attempting to get local node info and build channel map.")

            local_info = getattr(interface, 'myInfo', None)
            if local_info:

                meshtastic_data.set_local_node_info(local_info)
            else:
                logger.warning("Connected but myInfo not available from interface after wait. Will request node list.")
                meshtastic_data.set_local_node_info(None) 

            request_initial_node_state(interface)

        elif TOPIC_CONNECTION_LOST in topic_str:
            new_status = "Disconnected"
            error_msg = "Connection lost"
            logger.warning("Meshtastic connection lost.")
            meshtastic_data.set_local_node_info(None) 

            meshtastic_data.channel_map.clear()
            logger.info("Cleared channel map due to connection loss.")

        elif TOPIC_CONNECTION_FAILED in topic_str:
            new_status = "Error"
            error_msg = "Connection failed"
            logger.error("Meshtastic connection failed.")
            meshtastic_data.set_local_node_info(None) 

            meshtastic_data.channel_map.clear()
            logger.info("Cleared channel map due to connection failure.")

        meshtastic_data.set_connection_status(new_status)
        if error_msg:
            meshtastic_data.set_error(error_msg)

    except Exception as e:

        logger.exception(f"Error in on_connection callback: {e}")
        meshtastic_data.set_connection_status("Error")
        meshtastic_data.set_error(f"Connection callback error: {e}")
        meshtastic_data.set_local_node_info(None)

        meshtastic_data.channel_map.clear()
        logger.warning("Cleared channel map due to error in on_connection callback.")

def on_node_updated(node, interface):
    """Callback when node information is updated by the Meshtastic library."""
    node_num_debug = node.get('num', 'Unknown') if isinstance(node, dict) else 'Invalid Node Data'
    logger.debug(f"Node update event received for node #: {node_num_debug}")
    try:

        if node and isinstance(node, dict) and "num" in node:
            node_id_num = node["num"]
            node_id_str = f"!{node_id_num:08x}" 

            is_local_update = (node_id_str == meshtastic_data.local_node_id)
            if is_local_update:
                logger.info(f"Received node update specifically for the local node ({node_id_str}). Re-fetching local info to rebuild channel map...")

                local_info = getattr(interface, 'myInfo', None)
                if local_info:

                    meshtastic_data.set_local_node_info(local_info)
                else:
                    logger.warning(f"Local node update received ({node_id_str}), but myInfo could not be fetched from interface. Updating node data partially, channel map might be stale.")

                    cleaned_node_data = ensure_serializable(node)
                    meshtastic_data.update_node(node_id_str, cleaned_node_data, broadcast_change=True)
            else:

                cleaned_node_data = ensure_serializable(node)

                meshtastic_data.update_node(node_id_str, cleaned_node_data, broadcast_change=True)
        else:
            logger.warning(f"Received invalid node update data format: {type(node)} - {node}")
    except Exception as e:
        logger.exception(f"Error in on_node_updated callback: {e}")

def request_initial_node_state(iface):
    """Requests the full node list from the Meshtastic device."""

    if iface and hasattr(iface, 'requestNodes') and getattr(iface, 'isConnected', False):
        try:
            logger.info("Requesting node database refresh from device...")

            iface.requestNodes() 
            logger.info("Node database refresh request sent.")
        except AttributeError as ae:
           logger.warning(f"Meshtastic interface missing expected 'requestNodes' method: {ae}")
        except Exception as e:
            logger.error(f"Error requesting initial node state from device: {e}", exc_info=True)
    elif not iface:
        logger.warning("Cannot request initial node state: Interface is None.")
    elif not getattr(iface, 'isConnected', False):
        logger.warning("Cannot request initial node state: Interface is not connected.")

@app.get("/api/status", response_model=Dict, tags=["API - Current State"])
async def get_status():
    """Gets the current connection status and local node info."""

    return ensure_serializable({
        "connection_status": meshtastic_data.connection_status,
        "local_node_info": meshtastic_data.local_node_info,
        "last_error": meshtastic_data.last_error,
        "server_time_unix": time.time() 
    })

@app.get("/api/stats", response_model=Dict, tags=["API - Current State"])
async def get_stats():
    """Gets the current session statistics."""
    return meshtastic_data.get_serializable_stats()

def _map_node_to_api(node_data: Dict) -> Optional[ApiNodeData]:
    """Maps the internal node dictionary to the ApiNodeData model."""
    if not node_data or 'node_id' not in node_data:
        return None
    try:

        user_info = node_data.get('user', {}) if isinstance(node_data.get('user'), dict) else {}
        pos_info = node_data.get('position', {}) if isinstance(node_data.get('position'), dict) else {}
        metrics_info = node_data.get('deviceMetrics', {}) if isinstance(node_data.get('deviceMetrics'), dict) else {}

        lat = node_data.get('latitude')
        lon = node_data.get('longitude')
        if lat is None or lon is None: 
            lat_i = pos_info.get('latitudeI')
            lon_i = pos_info.get('longitudeI')
            if lat_i is not None and lon_i is not None:
                try: 
                    lat, lon = float(lat_i) / 1e7, float(lon_i) / 1e7
                except (ValueError, TypeError):
                    lat, lon = None, None 
            else: 
                lat = pos_info.get('latitude')
                lon = pos_info.get('longitude')

        api_node = ApiNodeData(
            node_id=node_data.get('node_id'),
            node_num=node_data.get('node_num'),

            user=ApiUserInfo(**user_info) if user_info else None,
            position=ApiPositionInfo(**pos_info) if pos_info else None,
            deviceMetrics=ApiDeviceMetrics(**metrics_info) if metrics_info else None,

            long_name=node_data.get('long_name', user_info.get('longName')),
            short_name=node_data.get('short_name', user_info.get('shortName')),
            macaddr=node_data.get('macaddr', user_info.get('macaddr')),
            hw_model=node_data.get('hw_model', user_info.get('hwModel')),
            firmware_version=node_data.get('firmware_version'),
            role=node_data.get('role'),
            is_local=node_data.get('is_local', False),
            last_heard=node_data.get('lastHeard'), 
            snr=node_data.get('snr'),
            rssi=node_data.get('rssi'),
            battery_level=node_data.get('battery_level', metrics_info.get('batteryLevel')),
            voltage=node_data.get('voltage', metrics_info.get('voltage')),
            channel_utilization=node_data.get('channel_utilization', metrics_info.get('channelUtilization')),
            air_util_tx=node_data.get('air_util_tx', metrics_info.get('airUtilTx')),
            latitude=lat,
            longitude=lon,
            altitude=node_data.get('altitude', pos_info.get('altitude')),
            position_time=node_data.get('position_time', pos_info.get('time')),
            telemetry_time=node_data.get('telemetry_time', metrics_info.get('time')),
            created_at=node_data.get('created_at'), 
            updated_at=node_data.get('updated_at'), 
        )
        return api_node
    except Exception as e:

        logger.error(f"Error converting node {node_data.get('node_id')} to API model: {e}", exc_info=True)
        return None

@app.get("/api/counts/totals",
         response_model=ApiTotalCountsResponse,
         tags=["API - History", "API - Counts"])
async def get_total_counts():
    """
    Gets the total historical counts for messages sent, positions, and telemetry
    records across all nodes from the database.
    """
    logger.info("Request received for total historical counts.")
    try:
        results = await asyncio.gather(
            asyncio.to_thread(db_manager.count_node_items, None, "messages_sent"),
            asyncio.to_thread(db_manager.count_node_items, None, "positions"),
            asyncio.to_thread(db_manager.count_node_items, None, "telemetry"),
            return_exceptions=True
        )
        msg_count, pos_count, tel_count = results
        error_details = []
        if isinstance(msg_count, Exception):
            logger.error(f"Error fetching total message count: {msg_count}", exc_info=msg_count)
            error_details.append("messages"); msg_count = -1
        if isinstance(pos_count, Exception):
            logger.error(f"Error fetching total position count: {pos_count}", exc_info=pos_count)
            error_details.append("positions"); pos_count = -1
        if isinstance(tel_count, Exception):
            logger.error(f"Error fetching total telemetry count: {tel_count}", exc_info=tel_count)
            error_details.append("telemetry"); tel_count = -1

        if msg_count == -1 and "messages" not in error_details: error_details.append("messages (DB error)")
        if pos_count == -1 and "positions" not in error_details: error_details.append("positions (DB error)")
        if tel_count == -1 and "telemetry" not in error_details: error_details.append("telemetry (DB error)")

        if error_details:
            unique_errors = sorted(list(set(error_details)))
            logger.error(f"Database error or exception indicated during total count query for: {', '.join(unique_errors)}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"Database error or exception retrieving total counts for: {', '.join(unique_errors)}.")

        logger.info(f"Total counts retrieved: Msgs={msg_count}, Pos={pos_count}, Tel={tel_count}")
        return ApiTotalCountsResponse(total_messages=msg_count, total_positions=pos_count, total_telemetry=tel_count)
    except Exception as e:
        logger.exception(f"Unexpected error in get_total_counts endpoint: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Unexpected server error retrieving total counts.")

@app.get("/api/nodes", response_model=Dict[str, ApiNodeData], tags=["API - Current State"])
async def get_nodes():
    """Gets the current state of all known nodes."""
    api_nodes = {}

    current_nodes_copy = meshtastic_data.nodes.copy() 
    for node_id, node_data in current_nodes_copy.items():
        mapped_node = _map_node_to_api(node_data) 
        if mapped_node:
            api_nodes[node_id] = mapped_node
        else:

            logger.warning(f"Failed to map node data for node ID {node_id} during /api/nodes retrieval.")

            api_nodes[node_id] = ApiNodeData(node_id=node_id, user=ApiUserInfo(longName=f"Error processing node {node_id}"))
    return api_nodes

@app.get("/api/nodes/{node_id}", response_model=ApiNodeData, tags=["API - Current State"])
async def get_node(node_id: str = Path(..., description="Node ID string (e.g., !aabbccdd)")):
    """Gets the current state of a specific node by its ID."""
    node_data = meshtastic_data.nodes.get(node_id) 
    if node_data:

        mapped_node = _map_node_to_api(node_data.copy()) 
        if mapped_node:
            return mapped_node
        else:
            logger.error(f"Failed to map node data for requested node ID: {node_id}")
            raise HTTPException(status_code=500, detail=f"Error processing data for node {node_id}")
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Node '{node_id}' not found")

@app.get("/api/packets", response_model=List[ApiPacketData], tags=["API - Current State"])
async def get_recent_packets_memory(limit: int = Query(50, ge=1, le=MAX_PACKETS_IN_MEMORY, description="Max recent packets from memory")):
    """Gets the most recent packets stored in memory."""
    packets = meshtastic_data.get_formatted_packets_from_memory(limit)
    results = []
    for p in packets:
        try:

            results.append(ApiPacketData(**p))
        except Exception as e: 
            logger.warning(f"Packet from memory failed validation for API response: {p.get('event_id')}. Error: {e}. Packet data: {p}")
    return results

@app.get("/api/packets/history", response_model=List[ApiPacketData], tags=["API - History"])
async def get_packet_history(
    limit: int = Query(100, ge=1, le=10000, description="Max number of historical packets from DB"),
):
    """Gets historical packets directly from the database."""
    logger.debug(f"Request for packet history, limit={limit}")
    try:
        historical_packets_raw = await asyncio.to_thread(db_manager.get_recent_packets, limit)
    except Exception as e:
        logger.error(f"Error retrieving packet history from DB thread: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve packet history from database.")

    results = []
    for p_dict in historical_packets_raw:
        try:

            decoded_payload = p_dict.get('decoded', {}) 
            raw_payload = p_dict.get('raw', {})

            if isinstance(decoded_payload, str):
                try: decoded_payload = json.loads(decoded_payload)
                except json.JSONDecodeError: 
                    logger.warning(f"DB JSON decode error for 'decoded' in packet {p_dict.get('event_id')}")
                    decoded_payload = {"error": "failed to decode json from db"}
            if isinstance(raw_payload, str):
                try: raw_payload = json.loads(raw_payload)
                except json.JSONDecodeError: 
                    logger.warning(f"DB JSON decode error for 'raw' in packet {p_dict.get('event_id')}")
                    raw_payload = {"error": "failed to decode json from db"}

            api_packet = ApiPacketData(
                event_id=p_dict.get('event_id'), 
                timestamp=p_dict.get('timestamp'), 
                rxTime=p_dict.get('rx_time'), 
                fromId=p_dict.get('from_id'), 
                toId=p_dict.get('to_id'),     
                channel=p_dict.get('channel'),
                rxSnr=p_dict.get('rx_snr'),   
                rxRssi=p_dict.get('rx_rssi'), 
                hopLimit=p_dict.get('hop_limit'),
                decoded=decoded_payload, 
                raw=raw_payload, 
                app_packet_type=p_dict.get('packet_type'), 
                wantAck=p_dict.get('want_ack')
            )
            results.append(api_packet)
        except Exception as e: 
            logger.error(f"Error converting historical packet {p_dict.get('event_id')} to API model: {e}. Data: {p_dict}", exc_info=True)
    return results

@app.get("/api/messages/history", response_model=List[ApiMessageData], tags=["API - History"])
async def get_message_history(
    from_id: Optional[str] = Query(None, description="Filter by sender node ID (e.g., !aabbccdd)"),
    to_id: Optional[str] = Query(None, description="Filter by recipient node ID (^all for broadcast, e.g., !aabbccdd)"),
    channel: Optional[int] = Query(None, description="Filter by channel index"),
    start_time: Optional[float] = Query(None, description="Start timestamp (unix epoch float)"),
    end_time: Optional[float] = Query(None, description="End timestamp (unix epoch float)"),
    limit: int = Query(100, ge=1, le=5000, description="Max number of messages")):
    """Gets historical messages from the database matching the filter criteria."""
    logger.debug(f"Request for message history, limit={limit}, filters: from={from_id}, to={to_id}, channel={channel}, start={start_time}, end={end_time}")
    try:
        messages_raw = await asyncio.to_thread(
            db_manager.get_messages, from_id, to_id, channel, start_time, end_time, limit
        )
    except Exception as e:
        logger.error(f"Error retrieving message history from DB thread: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve message history from database.")

    results = []
    for msg_dict in messages_raw:
        try:
            results.append(ApiMessageData(**msg_dict)) 
        except Exception as parse_e:
            logger.error(f"Error parsing message history data (ID: {msg_dict.get('id')}) into API model: {parse_e}. Data: {msg_dict}", exc_info=True)
    return results

VALID_HISTORY_TYPES = Literal["positions", "telemetry"]
@app.get("/api/nodes/{node_id}/history/{history_type}",
         response_model=Union[List[ApiNodePositionHistory], List[ApiNodeTelemetryHistory]],
         tags=["API - History", "API - Nodes"])
async def get_node_history_api(
    node_id: str = Path(..., description="Node ID string (e.g., !aabbccdd)"),
    history_type: VALID_HISTORY_TYPES = Path(..., description="Type of history ('positions' or 'telemetry')"),
    start_time: Optional[float] = Query(None, description="Start timestamp (unix epoch float)"),
    end_time: Optional[float] = Query(None, description="End timestamp (unix epoch float)"),
    limit: int = Query(1000, ge=1, le=10000, description="Max number of history records")):
    """Gets position or telemetry history for a specific node from the database."""
    logger.debug(f"Request for node history: node={node_id}, type={history_type}, limit={limit}, filters: start={start_time}, end={end_time}")
    try:
        history_data_raw = await asyncio.to_thread(
            db_manager.get_node_history, node_id, history_type, start_time, end_time, limit
        )
    except Exception as e:
        logger.error(f"Error retrieving node {history_type} history for {node_id} from DB thread: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to retrieve {history_type} history for node {node_id}.")

    try:
        if history_type == 'positions':
            return [ApiNodePositionHistory(**item) for item in history_data_raw]
        elif history_type == 'telemetry':
            return [ApiNodeTelemetryHistory(**item) for item in history_data_raw]
    except Exception as parse_e: 
        logger.error(f"Error parsing node {history_type} history data for {node_id}: {parse_e}. Raw items count: {len(history_data_raw)}", exc_info=True)
        if history_data_raw:
            logger.debug(f"First problematic item data for node {history_type} history: {history_data_raw[0]}")
        raise HTTPException(status_code=500, detail=f"Error formatting node {history_type} history response.")

@app.get("/api/nodes/{node_id}/count/{item_type}",
         response_model=NodeItemCountResponse,
         tags=["API - History", "API - Nodes", "API - Counts"])
async def count_node_items_api(
    node_id: str = Path(..., description="Node ID string (e.g., !aabbccdd)"),
    item_type: Literal["messages_sent", "positions", "telemetry"] = Path(
        ..., description="Type of item to count ('messages_sent', 'positions', or 'telemetry')"
    ),
    start_time: Optional[float] = Query(None, description="Optional start timestamp (unix epoch float) for filtering"),
    end_time: Optional[float] = Query(None, description="Optional end timestamp (unix epoch float) for filtering")
):
    """
    Gets the count of specific items (messages sent, positions, telemetry records)
    associated with a given node ID, optionally filtered by time.
    """
    logger.info(f"Request received to count '{item_type}' for node '{node_id}' (Filters: start={start_time}, end={end_time})")
    try:
        count = await asyncio.to_thread(
            db_manager.count_node_items, node_id, item_type, start_time, end_time
        )
    except ValueError as ve: 
        logger.error(f"Value error during count for node {node_id}, type {item_type}: {ve}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except Exception as e: 
        logger.error(f"Unexpected error calling count_node_items for node {node_id}, type {item_type}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error executing count query.")

    if count == -1: 
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error while counting {item_type} for node {node_id}.")

    return NodeItemCountResponse(
        node_id=node_id, item_type=item_type, count=count,
        start_time_filter=start_time, end_time_filter=end_time
    )

@app.get("/api/metrics/averages", response_model=ApiAverageMetricsResponse, tags=["API - History"])
async def get_average_metrics_api(
    limit: int = Query(100, ge=1, le=5000, description="Max number of historical average records")
):
    """Gets the most recent average mesh metrics and a history of past metrics."""
    logger.debug(f"Request for average metrics history, limit={limit}")
    try:

        results = await asyncio.gather(
            asyncio.to_thread(db_manager.get_most_recent_average_metrics),
            asyncio.to_thread(db_manager.get_average_metrics_history, limit=limit),
            return_exceptions=True 
        )
        most_recent_result, history_result = results

        if isinstance(most_recent_result, Exception):
            logger.error(f"Error getting most recent avg metrics from DB thread: {most_recent_result}", exc_info=most_recent_result)
            raise HTTPException(status_code=500, detail="Failed to retrieve most recent average metrics.")
        if isinstance(history_result, Exception):
            logger.error(f"Error getting avg metrics history from DB thread: {history_result}", exc_info=history_result)
            raise HTTPException(status_code=500, detail="Failed to retrieve average metrics history.")

        most_recent = most_recent_result
        history = history_result

    except Exception as e: 
        logger.error(f"Error during concurrent retrieval of average metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve average metrics.")

    try:

        return ApiAverageMetricsResponse(
            most_recent=ApiAverageMetricsData(**most_recent) if most_recent else None,
            history=[ApiAverageMetricsData(**item) for item in history]
        )
    except Exception as parse_e: 
        logger.error(f"Error parsing average metrics data into API model: {parse_e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error formatting average metrics response.")

@app.post("/api/messages", status_code=status.HTTP_202_ACCEPTED, response_model=Dict, tags=["API - Actions"])
async def send_message(message_request: MessageRequest):
    """Queues a message to be sent via the Meshtastic interface and logs it to the database."""
    global interface, background_tasks 
    if not interface or not getattr(interface, 'isConnected', False):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Not connected to Meshtastic device")

    try:
        dest = message_request.destination if message_request.destination else "^all"
        chan_index = message_request.channel if message_request.channel is not None else 0

        logger.info(f"API Send: '{message_request.message}' to '{dest}' on channel {chan_index}")

        await asyncio.to_thread(
            interface.sendText,
            message_request.message,
            dest,
            channelIndex=chan_index,
            wantAck=False 
        )

        captured_local_node_id = meshtastic_data.local_node_id 
        logger.info(f"SEND_MESSAGE: Captured local_node_id for DB logging: '{captured_local_node_id}'")

        if captured_local_node_id: 
            current_time_epoch = time.time()
            sent_packet_data = {
                'fromId': captured_local_node_id, 
                'toId': dest,
                'channel': chan_index, 
                'original_channel_id': chan_index, 
                'timestamp': current_time_epoch,
                'rxTime': int(current_time_epoch), 
                'decoded': {
                    'portnum': 'TEXT_MESSAGE_APP', 
                    'payload': message_request.message
                },
                'app_packet_type': 'Message', 
                'rxSnr': None, 'rxRssi': None, 'hopLimit': None, 'wantAck': False, 'raw': {} 
            }
            logger.info(f"SEND_MESSAGE: Constructed sent_packet_data with fromId='{sent_packet_data['fromId']}' for EventID (to be gen): {sent_packet_data.get('event_id','N/A_pre_add')}")

            async def _log_sent_message_task(data_to_log: Dict):
                try:
                    await asyncio.to_thread(meshtastic_data.add_packet, dict(data_to_log)) 
                    logger.info(f"Logged sent message from {data_to_log.get('fromId')} to {data_to_log.get('toId')} on channel {data_to_log.get('channel')} to DB.")
                except Exception as log_e:
                    logger.error(f"Error logging sent message to DB: {log_e}", exc_info=True)

            log_task = asyncio.create_task(_log_sent_message_task(dict(sent_packet_data))) 
            background_tasks.add(log_task)
            log_task.add_done_callback(background_tasks.discard) 

        else:
            logger.warning("Could not log sent message to DB: Local node ID was unknown/None when message send was processed.")
        return {"status": "queued", "detail": "Message sent to Meshtastic interface thread for transmission."}

    except AttributeError as e: 
        logger.error(f"Error sending message: Interface might be missing sendText method or not initialized? {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Interface error sending message.")
    except BrokenPipeError as e: 
        logger.error(f"Broken pipe error sending message: {e}", exc_info=True)
        meshtastic_data.set_error(f"Send message failed (connection broken): {e}")
        if interface:
            try: 
                await asyncio.to_thread(interface.close)
            except Exception as close_err: 
                logger.warning(f"Error closing interface after broken pipe: {close_err}")
            finally:
                interface = None 
        meshtastic_data.set_connection_status("Disconnected")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Connection to Meshtastic device was broken. The system will attempt to reconnect.")
    except Exception as e: 
        logger.error(f"Unexpected error sending message via API: {e}", exc_info=True)
        meshtastic_data.set_error(f"Send message failed: {e}")
        if "Connection" in str(e) or "socket" in str(e).lower() or "broken" in str(e).lower():
            if interface:
                try: 
                    await asyncio.to_thread(interface.close)
                except Exception as close_err: 
                    logger.warning(f"Error closing interface after send error: {close_err}")
                finally:
                    interface = None
            meshtastic_data.set_connection_status("Disconnected")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to send message: {e}")

@app.post("/api/hook", status_code=status.HTTP_202_ACCEPTED, response_model=Dict, tags=["API - Actions"])
async def send_message_via_hook(hook_request: HookMessageRequest):
    """
    Receives a Node ID and Message via POST hook and sends it immediately.
    Logs the sent message to the database.
    """
    global interface, background_tasks 
    logger.info(f"Received request on /api/hook to send to {hook_request.node_id}")

    if not interface or not getattr(interface, 'isConnected', False):
        logger.error("Hook failed: Not connected to Meshtastic device.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Not connected to Meshtastic device")

    try:
        dest = hook_request.node_id
        chan_index = hook_request.channel if hook_request.channel is not None else 0

        logger.info(f"Sending message via Hook: '{hook_request.message}' to '{dest}' on channel {chan_index}")

        await asyncio.to_thread(
            interface.sendText,
            hook_request.message,
            dest,
            channelIndex=chan_index,
            wantAck=False
        )

        captured_local_node_id = meshtastic_data.local_node_id 
        logger.info(f"SEND_HOOK: Captured local_node_id for DB logging: '{captured_local_node_id}'")

        if captured_local_node_id: 
            current_time_epoch = time.time()
            sent_packet_data = {
                'fromId': captured_local_node_id, 
                'toId': dest,
                'channel': chan_index,
                'original_channel_id': chan_index,
                'timestamp': current_time_epoch,
                'rxTime': int(current_time_epoch),
                'decoded': {'portnum': 'TEXT_MESSAGE_APP', 'payload': hook_request.message},
                'app_packet_type': 'Message',
                'rxSnr': None, 'rxRssi': None, 'hopLimit': None, 'wantAck': False, 'raw': {}
            }
            logger.info(f"SEND_HOOK: Constructed sent_packet_data with fromId='{sent_packet_data['fromId']}' for EventID (to be gen): {sent_packet_data.get('event_id','N/A_pre_add')}")

            async def _log_hook_message_task(data_to_log: Dict):
                try:
                    await asyncio.to_thread(meshtastic_data.add_packet, dict(data_to_log)) 
                    logger.info(f"Logged sent hook message from {data_to_log.get('fromId')} to {data_to_log.get('toId')} on channel {data_to_log.get('channel')} to DB.")
                except Exception as log_e:
                    logger.error(f"Error logging sent hook message to DB: {log_e}", exc_info=True)

            log_hook_task = asyncio.create_task(_log_hook_message_task(dict(sent_packet_data))) 
            background_tasks.add(log_hook_task)
            log_hook_task.add_done_callback(background_tasks.discard)
        else:
            logger.warning("Could not log sent hook message to DB: Local node ID was unknown/None when hook send was processed.")

        return {"status": "queued", "detail": f"Hook message sent to Meshtastic interface thread for transmission to {dest}."}

    except AttributeError as e:
        logger.error(f"Error sending hook message: Interface might be missing sendText method? {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Interface error sending hook message.")
    except BrokenPipeError as e:
        logger.error(f"Broken pipe error sending hook message: {e}", exc_info=True)
        meshtastic_data.set_error(f"Hook send failed (connection broken): {e}")
        if interface:
            try: 
                await asyncio.to_thread(interface.close)
            except Exception as close_err: 
                logger.warning(f"Error closing interface after broken pipe: {close_err}")
            finally: 
                interface = None
        meshtastic_data.set_connection_status("Disconnected")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Connection to Meshtastic device was broken during hook execution. Will attempt reconnect.")
    except Exception as e:
        logger.error(f"Unexpected error sending message via hook: {e}", exc_info=True)
        meshtastic_data.set_error(f"Hook send failed: {e}")
        if "Connection" in str(e) or "socket" in str(e).lower() or "broken" in str(e).lower():
            if interface:
                try: 
                    await asyncio.to_thread(interface.close)
                except Exception as close_err: 
                    logger.warning(f"Error closing interface after hook send error: {close_err}")
                finally: 
                    interface = None
            meshtastic_data.set_connection_status("Disconnected")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to send hook message: {e}")

@app.post("/api/system/restart", status_code=status.HTTP_202_ACCEPTED, response_model=Dict, tags=["API - System"])
async def system_restart(current_user: User = Depends(get_current_active_user)):
    """
    Restarts the Meshtastic Dashboard server.
    This will reload the configuration from .mesh-dash_config.
    The client might receive a connection error as the server restarts.
    """
    global interface, background_tasks, main_event_loop

    logger.warning(f"System restart requested by user: {current_user.username}")
    logger.info("Initiating graceful shutdown for restart...")

    tasks_to_cancel = list(background_tasks)
    if tasks_to_cancel:
        logger.info(f"Cancelling {len(tasks_to_cancel)} background task(s) for restart...")
        for task_item in tasks_to_cancel:
            if not task_item.done():
                task_item.cancel()
        try:
            results = await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
            logger.debug(f"Background task cancellation results for restart: {results}")
        except Exception as e:
            logger.error(f"Error during background task cancellation for restart: {e}", exc_info=True)
        background_tasks.clear()
        logger.info("Background tasks cancelled for restart.")

    if interface:
        logger.info("Closing Meshtastic interface for restart...")
        try:
            await asyncio.to_thread(interface.close)
            logger.info("Meshtastic interface closed for restart.")
        except Exception as e:
            logger.error(f"Error closing Meshtastic interface for restart: {e}", exc_info=True)
        interface = None

    async with sse_queues_lock:
        for q in sse_queues:
            try:

                await q.put({"event": "server_restart", "data": json.dumps({"message": "Server is restarting..."})})
            except Exception:
                pass 
        sse_queues.clear()
    logger.info("SSE queues notified/cleared for restart.")

    await asyncio.sleep(1) 

    logger.info("Executing script restart...")
    try:

        python_executable = sys.executable
        script_args = sys.argv

        os.execv(python_executable, [python_executable] + script_args)

    except Exception as e:

        logger.critical(f"FATAL: Failed to execute restart: {e}", exc_info=True)

        sys.exit(1) 

    return {"message": "Server is restarting. You may need to reconnect."}

def _sync_fetch_and_parse_url(url: str) -> Tuple[Optional[BeautifulSoup], Optional[str]]:
    """Synchronous helper to fetch URL and parse with BeautifulSoup."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"}
        response = requests.get(url, headers=headers, timeout=15) 
        response.raise_for_status() 
        soup = BeautifulSoup(response.text, 'html.parser')
        return soup, None 
    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP request failed for URL {url}: {e}")
        return None, f"HTTP request failed: {e}"
    except Exception as e:
        logger.error(f"Error parsing URL {url}: {e}", exc_info=True)
        return None, f"Error parsing HTML: {e}"

def _sync_extract_blocks(soup: BeautifulSoup) -> List[Dict]:
    """Synchronous helper to extract text blocks from parsed HTML."""
    blocks = []
    for element in soup.find_all(['div', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                                 'article', 'section', 'main', 'aside', 'header', 'footer',
                                 'li', 'td', 'blockquote', 'pre', 'code']):
        text = element.get_text(strip=True)
        if not text: continue

        element_id = element.get('id')
        element_class = ' '.join(element.get('class', []))

        blocks.append({
            "element_type": element.name,
            "element_id": element_id,
            "element_class": element_class if element_class else None,
            "text": text
        })
    return blocks

@app.post("/extract", tags=["URL Content Extraction"])
async def extract_content(request_data: URLRequest):
    """
    Fetches content from a URL, parses it, and returns text blocks.
    Uses threads for blocking network I/O and parsing.
    """
    url = request_data.url
    block_id = request_data.block_id
    text_only = request_data.text_only

    try:
        soup, error = await asyncio.to_thread(_sync_fetch_and_parse_url, url)
        if error or soup is None:
            raise HTTPException(status_code=502, detail=f"Failed to fetch or parse URL {url}: {error}")

        blocks = await asyncio.to_thread(_sync_extract_blocks, soup)

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error during threaded URL processing for {url}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing URL in thread: {e}")

    if block_id is not None:
        if block_id < 0 or block_id >= len(blocks):
            raise HTTPException(
                status_code=404,
                detail=f"Block ID {block_id} not found. Valid range: 0-{len(blocks)-1}"
            )
        if text_only:
            return PlainTextResponse(blocks[block_id]["text"])
        return { "url": url, "block_id": block_id, "block": blocks[block_id] }
    else:
        return { "url": url, "total_blocks": len(blocks), "blocks": blocks }

@app.post("/monitor/website", tags=["Website Monitoring", "API - Actions"])
async def monitor_website(request_data: WebsiteMonitorRequest):
    """
    Fetches a specific block from a website, prepends text, and sends it
    as a Meshtastic message. Logs the sent message to the database.
    """
    global interface, background_tasks 
    url = request_data.url
    block_id = request_data.block_id
    prefix = request_data.prefix
    node_id = request_data.node_id 
    channel = request_data.channel if request_data.channel is not None else 0

    try:
        soup, error = await asyncio.to_thread(_sync_fetch_and_parse_url, url)
        if error or soup is None:
            raise HTTPException(status_code=502, detail=f"Monitor: Failed to fetch/parse URL {url}: {error}")

        blocks_raw = await asyncio.to_thread(_sync_extract_blocks, soup)
        block_texts = [b['text'] for b in blocks_raw]

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Monitor: Error during threaded URL processing for {url}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Monitor: Error processing URL in thread: {e}")

    if block_id < 0 or block_id >= len(block_texts):
        raise HTTPException(
            status_code=404,
            detail=f"Monitor: Block ID {block_id} not found. Valid range: 0-{len(block_texts)-1}"
        )
    extracted_text = block_texts[block_id]

    message_text = f"{prefix} {extracted_text}"
    MAX_MSG_LEN = 200 
    if len(message_text) > MAX_MSG_LEN:
        message_text = message_text[:MAX_MSG_LEN-3] + "..."
        logger.warning(f"Monitor message truncated to {MAX_MSG_LEN} chars: {message_text}")

    if not interface or not getattr(interface, 'isConnected', False):
        raise HTTPException(status_code=503, detail="Monitor: Not connected to Meshtastic device")

    try:
        dest = node_id if node_id else "^all" 
        is_valid_destination = False
        if dest == "^all": 
            is_valid_destination = True
        elif isinstance(dest, str) and dest.startswith('!') and len(dest) == 9: 
            try: 
                int(dest[1:], 16) 
                is_valid_destination = True
            except ValueError: 
                is_valid_destination = False

        if not is_valid_destination:
            logger.error(f"Monitor: Invalid destination node ID format provided: '{dest}'")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Monitor: Invalid destination node ID format: '{dest}'. Must be '^all' or like '!aabbccdd'."
            )

        logger.info(f"Monitor sending: '{message_text}' to '{dest}' on channel {channel}")

        await asyncio.to_thread(
            interface.sendText, message_text, dest, channelIndex=channel, wantAck=False
        )

        captured_local_node_id = meshtastic_data.local_node_id 
        logger.info(f"MONITOR_WEBSITE: Captured local_node_id for DB logging: '{captured_local_node_id}'")

        if captured_local_node_id: 
            current_time_epoch = time.time()
            sent_packet_data = {
                'fromId': captured_local_node_id, 
                'toId': dest, 
                'channel': channel, 
                'original_channel_id': channel,
                'timestamp': current_time_epoch, 
                'rxTime': int(current_time_epoch),
                'decoded': {'portnum': 'TEXT_MESSAGE_APP', 'payload': message_text},
                'app_packet_type': 'Message',
                'rxSnr': None, 'rxRssi': None, 'hopLimit': None, 'wantAck': False, 'raw': {}
            }
            logger.info(f"MONITOR_WEBSITE: Constructed sent_packet_data with fromId='{sent_packet_data['fromId']}' for EventID (to be gen): {sent_packet_data.get('event_id','N/A_pre_add')}")

            async def _log_monitor_message_task(data_to_log: Dict): 
                try:
                    await asyncio.to_thread(meshtastic_data.add_packet, dict(data_to_log)) 
                    logger.info(f"Logged sent website monitor message from {data_to_log.get('fromId')} to {data_to_log.get('toId')} on channel {data_to_log.get('channel')} to DB.")
                except Exception as log_e:
                    logger.error(f"Error logging sent website monitor message to DB: {log_e}", exc_info=True)

            log_monitor_task = asyncio.create_task(_log_monitor_message_task(dict(sent_packet_data))) 
            background_tasks.add(log_monitor_task)
            log_monitor_task.add_done_callback(background_tasks.discard)
        else:
            logger.warning("Could not log sent website monitor message to DB: Local node ID is unknown at the time of logging preparation.")

        return {
            "success": True,
            "message": f"Sent '{message_text}' to node '{dest}' on channel {channel}",
            "extracted_text": extracted_text
        }

    except HTTPException as he: raise he
    except BrokenPipeError as e:
        logger.error(f"Monitor: Broken pipe error sending message: {e}", exc_info=True)
        meshtastic_data.set_error(f"Monitor send failed (connection broken): {e}")
        if interface:
            try: 
                await asyncio.to_thread(interface.close)
            except: pass 
            finally: interface = None
        meshtastic_data.set_connection_status("Disconnected")
        raise HTTPException(status_code=503, detail="Monitor: Connection broken. Will attempt reconnect.")
    except Exception as e:
        logger.error(f"Monitor: Failed to send message: {e}", exc_info=True)
        if isinstance(e, ValueError) and "invalid literal for int() with base 16" in str(e): 
            raise HTTPException(status_code=400, detail=f"Monitor: Invalid node ID format caused internal error: {dest}")
        else:
            raise HTTPException(status_code=500, detail=f"Monitor: Failed to send message: {str(e)}")

async def update_stats_periodically():
    """Periodically broadcasts stats if SSE clients are connected."""
    logger.info("Starting periodic stats update task...")
    while True:
        await asyncio.sleep(10) 
        try:
            async with sse_queues_lock: 
                has_clients = bool(sse_queues)
            if has_clients:
                await broadcast_stats()
        except asyncio.CancelledError:
            logger.info("Stats update task cancelled.")
            break 
        except Exception as e:
            logger.error(f"Error in stats update task: {e}", exc_info=True)
            await asyncio.sleep(30) 

async def check_connection_periodically():
    """Periodically checks the Meshtastic connection status."""
    global interface
    logger.info("Starting periodic connection check task...")
    while True:
        await asyncio.sleep(20) 
        try:
            current_status = meshtastic_data.connection_status
            is_connected_check = False 
            if interface is not None:
                try:
                    is_connected_check = await asyncio.wait_for(
                        asyncio.to_thread(lambda: getattr(interface, 'isConnected', False)),
                        timeout=5.0 
                    )
                except asyncio.TimeoutError:
                    logger.warning("Connection check timed out accessing interface.isConnected.")
                    is_connected_check = False 
                except Exception as iface_err: 
                    logger.error(f"Error accessing interface.isConnected: {iface_err}")
                    is_connected_check = False

            if not is_connected_check and current_status == "Connected":
                logger.warning("Detected potential disconnection (periodic check). Setting status to Disconnected.")
                meshtastic_data.set_connection_status("Disconnected")
                meshtastic_data.set_error("Connection check failed")
                meshtastic_data.set_local_node_info(None) 
                if interface: 
                    try: await asyncio.to_thread(interface.close)
                    except Exception: pass 
                    finally: interface = None 

            elif is_connected_check and current_status != "Connected":
                logger.info(f"Interface seems connected, but status was '{current_status}'. Correcting to Connected.")
                meshtastic_data.set_connection_status("Connected")
                meshtastic_data.set_error(None) 
                if not meshtastic_data.local_node_info and interface:
                    local_info = getattr(interface, 'myInfo', None)
                    if local_info: 
                        meshtastic_data.set_local_node_info(local_info) 

            elif interface is None and current_status not in ["Disconnected", "Initializing", "Connecting", "Error"]:
                logger.warning(f"Interface is None, but status is '{current_status}'. Setting to Disconnected.")
                meshtastic_data.set_connection_status("Disconnected")
                meshtastic_data.set_local_node_info(None)

        except asyncio.CancelledError:
            logger.info("Connection check task cancelled.")
            break 
        except Exception as e:
            logger.error(f"Error in connection check task: {e}", exc_info=True)
            if meshtastic_data.connection_status != "Error": 
                meshtastic_data.set_connection_status("Error")
                meshtastic_data.set_error(f"Connection check task error: {e}")
            if interface: 
                try: await asyncio.to_thread(interface.close)
                except: pass
                finally: interface = None
            await asyncio.sleep(30) 

async def prune_history_periodically():
    """Periodically prunes old data from the database history tables."""
    logger.info("Starting periodic history pruning task...")
    await asyncio.sleep(60) 
    while True:
        try:
            logger.info("Running periodic history pruning...")
            await asyncio.to_thread(
                db_manager.prune_old_average_metrics, 
                max_age_days=AVERAGE_METRICS_HISTORY_DAYS
            )
            logger.info("Periodic history pruning finished.")
        except asyncio.CancelledError:
            logger.info("History pruning task cancelled.")
            break 
        except Exception as e:
            logger.error(f"Error in periodic history pruning task: {e}", exc_info=True)
        await asyncio.sleep(3600 * 6) 

async def connect_to_meshtastic():
    """Background task to establish and maintain the Meshtastic connection."""
    global interface
    retry_delay = 5 
    max_retry_delay = 60 
    connection_attempts = 0
    logger.info("Starting Meshtastic connection manager task...")

    while True:
        is_currently_connected = False
        if interface is not None:
            try:
                is_currently_connected = await asyncio.wait_for(
                    asyncio.to_thread(lambda: getattr(interface, 'isConnected', False)),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("connect_to_meshtastic: Timeout checking interface.isConnected.")
                is_currently_connected = False 
            except Exception as iface_err:
                logger.error(f"connect_to_meshtastic: Error checking interface status: {iface_err}")
                is_currently_connected = False

        if not is_currently_connected:
            connection_attempts += 1
            meshtastic_data.set_connection_status("Connecting")
            logger.info(f"Attempting connection to Meshtastic at tcp://{TARGET_HOST}:{TARGET_PORT}... (Attempt {connection_attempts})")

            if interface: 
                try:
                    logger.debug("Closing existing Meshtastic interface before reconnecting...")
                    await asyncio.to_thread(interface.close)
                except Exception as close_e:
                    logger.warning(f"Error closing old interface: {close_e}")
                finally:
                    interface = None 

            try:
                new_interface = await asyncio.to_thread(
                    meshtastic.tcp_interface.TCPInterface,
                    hostname=TARGET_HOST,
                    portNumber=TARGET_PORT, 
                    connectNow=True, 
                    noProto=False 
                )
                await asyncio.sleep(3) 

                is_connected_after = await asyncio.to_thread(getattr, new_interface, 'isConnected', False)
                my_info_after = await asyncio.to_thread(getattr, new_interface, 'myInfo', None)

                if is_connected_after and my_info_after:
                    logger.info(f"Successfully connected to Meshtastic device after {connection_attempts} attempts.")
                    interface = new_interface 

                    logger.info("Subscribing to Meshtastic pubsub events...")
                    pub.subscribe(on_receive, TOPIC_RECEIVED)
                    pub.subscribe(on_connection, TOPIC_CONNECTION_ESTABLISHED)
                    pub.subscribe(on_connection, TOPIC_CONNECTION_LOST)
                    pub.subscribe(on_connection, TOPIC_CONNECTION_FAILED)
                    pub.subscribe(on_node_updated, TOPIC_NODE_UPDATED)
                    logger.info("Subscribed to Meshtastic pubsub events.")

                    on_connection(interface, topic=TOPIC_CONNECTION_ESTABLISHED) 

                    retry_delay = 5 
                    connection_attempts = 0 
                else:
                    logger.warning("Connection attempt finished, but not fully established or myInfo missing.")
                    meshtastic_data.set_connection_status("Error")
                    meshtastic_data.set_error("Failed to establish full Meshtastic protocol connection.")
                    try: 
                        await asyncio.to_thread(new_interface.close)
                    except: pass 

            except BrokenPipeError as e:
                logger.error(f"Broken pipe error during connection attempt: {e}")
                meshtastic_data.set_connection_status("Error")
                meshtastic_data.set_error(f"Broken pipe error: {e}")
            except ConnectionRefusedError:
                err_msg = f"Connection refused by {TARGET_HOST}:{TARGET_PORT}."
                logger.error(err_msg)
                meshtastic_data.set_connection_status("Error")
                meshtastic_data.set_error(err_msg)
            except OSError as e: 
                logger.error(f"Network OS Error connecting to {TARGET_HOST}:{TARGET_PORT}: {e}")
                meshtastic_data.set_connection_status("Error")
                meshtastic_data.set_error(f"Network Error: {e}")
            except Exception as e: 
                logger.exception(f"Unexpected error during connection attempt: {e}")
                meshtastic_data.set_connection_status("Error")
                meshtastic_data.set_error(f"Unexpected connect error: {e}")

            if meshtastic_data.connection_status != "Connected":
                logger.info(f"Waiting {retry_delay} seconds before retrying connection...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay) 
        else: 
            retry_delay = 5 
            connection_attempts = 0 
            await asyncio.sleep(15) 

        try:
            await asyncio.sleep(0) 
        except asyncio.CancelledError:
            logger.info("Meshtastic connection task cancelled.")
            if interface: 
                try: await asyncio.to_thread(interface.close)
                except: pass
            break 

def parse_arguments():
    """Parses command-line arguments to override file/default settings."""
    global TARGET_HOST, TARGET_PORT, LOG_LEVEL, WEBSERVER_PORT, WEBSERVER_HOST, DB_PATH 
    global LOG_LEVEL_STR, AVERAGE_METRICS_HISTORY_DAYS, MAX_PACKETS_IN_MEMORY, EXTERNAL_MAP_API_BASE_URL

    import argparse
    parser = argparse.ArgumentParser(description="Meshtastic Web Dashboard Server")

    parser.add_argument("--host", default=TARGET_HOST, 
                        help=f"Meshtastic device hostname/IP (current: {TARGET_HOST})")
    parser.add_argument("--port", type=int, default=TARGET_PORT, 
                        help=f"Meshtastic device port (current: {TARGET_PORT})")
    parser.add_argument("--web-host", default=WEBSERVER_HOST, 
                        help=f"Web server host (current: {WEBSERVER_HOST})")
    parser.add_argument("--web-port", type=int, default=WEBSERVER_PORT, 
                        help=f"Web server port (current: {WEBSERVER_PORT})")
    parser.add_argument("--db-path", default=DB_PATH, 
                        help=f"Path to SQLite database file (current: {DB_PATH})")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], 
                        default=LOG_LEVEL_STR, help=f"Logging level (current: {LOG_LEVEL_STR})")
    parser.add_argument("--history-days", type=int, default=AVERAGE_METRICS_HISTORY_DAYS,
                        help=f"Days of average metrics history to retain (current: {AVERAGE_METRICS_HISTORY_DAYS})")
    parser.add_argument("--max-packets", type=int, default=MAX_PACKETS_IN_MEMORY,
                        help=f"Max recent packets to keep in memory (current: {MAX_PACKETS_IN_MEMORY})")
    parser.add_argument("--external-map-url", default=EXTERNAL_MAP_API_BASE_URL,
                        help=f"External map API base URL (current: {EXTERNAL_MAP_API_BASE_URL})")

    args = parser.parse_args()

    if args.host != TARGET_HOST: TARGET_HOST = args.host
    if args.port != TARGET_PORT: TARGET_PORT = args.port
    if args.web_host != WEBSERVER_HOST: WEBSERVER_HOST = args.web_host
    if args.web_port != WEBSERVER_PORT: WEBSERVER_PORT = args.web_port
    if args.db_path != DB_PATH: DB_PATH = args.db_path
    if args.history_days != AVERAGE_METRICS_HISTORY_DAYS: AVERAGE_METRICS_HISTORY_DAYS = args.history_days
    if args.max_packets != MAX_PACKETS_IN_MEMORY: MAX_PACKETS_IN_MEMORY = args.max_packets
    if args.external_map_url != EXTERNAL_MAP_API_BASE_URL: EXTERNAL_MAP_API_BASE_URL = args.external_map_url

    if args.log_level.upper() != LOG_LEVEL_STR:
        LOG_LEVEL_STR = args.log_level.upper()
        new_log_level_const = getattr(logging, LOG_LEVEL_STR, LOG_LEVEL) 

        if new_log_level_const != LOG_LEVEL:
            LOG_LEVEL = new_log_level_const

            logger.info(f"Log level CHANGED via command line to: {LOG_LEVEL_STR} (Effective Level: {logging.getLevelName(LOG_LEVEL)})")

            logging.getLogger().setLevel(LOG_LEVEL) 
            logger.setLevel(LOG_LEVEL) 

            if LOG_LEVEL > logging.DEBUG:
                for log_name in ["meshtastic", "pubsub", "bleak", "watchfiles", "uvicorn", "httpx", "aiosqlite", "sse_starlette", "auto_reply"]:
                    logging.getLogger(log_name).setLevel(logging.WARNING)
            else:
                logging.getLogger("meshtastic").setLevel(logging.DEBUG)
                for log_name in ["pubsub", "bleak", "watchfiles", "uvicorn", "httpx", "aiosqlite", "sse_starlette", "auto_reply"]:
                    logging.getLogger(log_name).setLevel(logging.INFO)
        else:
            logger.info(f"Log level specified via command line ({args.log_level.upper()}) is the same as current. No change in logging level.")

    logger.info(f"Final Effective Meshtastic Target: tcp://{TARGET_HOST}:{TARGET_PORT}")
    logger.info(f"Final Effective Web Server: http://{WEBSERVER_HOST}:{WEBSERVER_PORT}")
    logger.info(f"Final Effective Database Path: {DB_PATH}")
    logger.info(f"Final Effective Log Level: {LOG_LEVEL_STR} ({logging.getLevelName(LOG_LEVEL)})")

if __name__ == "__main__":
    parse_arguments() 

    try:
        logger.info("Initializing Task database...")
        init_tasks_db() 
    except Exception as e:
        logger.critical(f"CRITICAL: Task Database initialization failed: {e}", exc_info=True)
        sys.exit(1)

    if AUTO_REPLY_ENABLED and init_auto_reply_db:
        try:
            logger.info("Initializing Auto-Reply database table...")
            init_auto_reply_db() 
            logger.info("Auto-Reply database table initialization complete.")
        except Exception as e:
            logger.error(f"Failed to initialize Auto-Reply database table: {e}", exc_info=True)
    elif not AUTO_REPLY_ENABLED:
        logger.warning("Auto-Reply functionality is disabled, skipping DB init.")

    uvicorn.run(
        "__main__:app", 
        host=WEBSERVER_HOST,
        port=WEBSERVER_PORT,
        reload=False, 
        log_level=LOG_LEVEL_STR.lower(), 
    )