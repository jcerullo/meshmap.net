import asyncio
import json
import logging
import time
import uuid
import base64
import os
import sys
import sqlite3
from typing import Dict, List, Optional, Set, Any, Union, Tuple, AsyncGenerator
from datetime import datetime, timedelta
from collections import deque 
import statistics 

import meshtastic
import meshtastic.tcp_interface
import meshtastic.node
from pubsub import pub
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response, HTTPException, Depends, Query, Path, status, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

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

DEFAULT_TARGET_HOST = "10.0.0.25" 
DEFAULT_TARGET_PORT = 4403
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_WEBSERVER_PORT = 8001 
DEFAULT_WEBSERVER_HOST = "0.0.0.0" 
DEFAULT_DB_PATH = "meshtastic_network_data.db" 
AVERAGE_METRICS_HISTORY_DAYS = 60 
NODE_ACTIVITY_TIMEOUT_SECONDS = 3600 * 4 
PROACTIVE_TRACEROUTE_INTERVAL_SECONDS = 3600 
ENABLE_PROACTIVE_TRACEROUTE = False 

TARGET_HOST = os.environ.get("MESHTASTIC_HOST", DEFAULT_TARGET_HOST)
TARGET_PORT = int(os.environ.get("MESHTASTIC_PORT", DEFAULT_TARGET_PORT))
LOG_LEVEL_STR = os.environ.get("LOG_LEVEL", "INFO")
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR.upper(), DEFAULT_LOG_LEVEL)
WEBSERVER_PORT = int(os.environ.get("NETWORK_WEBSERVER_PORT", DEFAULT_WEBSERVER_PORT))
WEBSERVER_HOST = os.environ.get("WEBSERVER_HOST", DEFAULT_WEBSERVER_HOST)
DB_PATH = os.environ.get("NETWORK_DB_PATH", DEFAULT_DB_PATH)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("meshtastic_network_analyzer")
logger.info(f"Using Pydantic V{2 if PYDANTIC_V2 else 1}.")

if LOG_LEVEL > logging.DEBUG:
    logging.getLogger("meshtastic").setLevel(logging.WARNING)
    logging.getLogger("pubsub").setLevel(logging.WARNING)
    logging.getLogger("bleak").setLevel(logging.WARNING)

class NetworkDatabaseManager:
    """Handle database operations focused on Meshtastic network analysis data."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        logger.info(f"Initializing network database at: {self.db_path}")
        self.init_database()

    def _get_connection(self):
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row 
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception as e:
            logger.warning(f"Could not set WAL journal mode (might be unsupported): {e}")
        return conn

    def init_database(self):
        """Initialize the database with necessary tables and indices for network analysis."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS packets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    timestamp REAL NOT NULL,
                    rx_time INTEGER,
                    from_id TEXT,
                    to_id TEXT,
                    channel INTEGER,
                    portnum TEXT,
                    packet_type TEXT,
                    rx_snr REAL,
                    rx_rssi INTEGER,
                    hop_limit INTEGER,
                    want_ack BOOLEAN,
                    decoded TEXT,
                    raw TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )""")

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    node_num INTEGER UNIQUE,
                    long_name TEXT,
                    short_name TEXT,
                    macaddr TEXT,
                    hw_model TEXT,
                    firmware_version TEXT,
                    role TEXT,
                    is_local BOOLEAN DEFAULT FALSE,
                    last_heard INTEGER,
                    battery_level INTEGER,
                    voltage REAL,
                    channel_utilization REAL,
                    air_util_tx REAL,
                    uptime_seconds INTEGER, -- Added from Telemetry for direct node access
                    snr REAL,
                    rssi INTEGER,
                    latitude REAL,
                    longitude REAL,
                    altitude INTEGER,
                    position_time INTEGER,
                    telemetry_time INTEGER,
                    user_info TEXT,
                    position_info TEXT,
                    device_metrics_info TEXT,
                    environment_metrics_info TEXT, -- Added Environment Metrics JSON
                    module_config_info TEXT,
                    channel_info TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )""")

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    altitude INTEGER,
                    precision_bits INTEGER,
                    ground_speed INTEGER,
                    ground_track INTEGER,
                    sats_in_view INTEGER,
                    pdop REAL, hdop REAL, vdop REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
                )""")

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    battery_level INTEGER,
                    voltage REAL,
                    channel_utilization REAL,
                    air_util_tx REAL,
                    uptime_seconds INTEGER,
                    temperature REAL,
                    relative_humidity REAL,
                    barometric_pressure REAL,
                    gas_resistance REAL,
                    iaq REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
                )""")

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS traceroutes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    packet_event_id TEXT UNIQUE NOT NULL,
                    requester_node_id TEXT NOT NULL,
                    responder_node_id TEXT NOT NULL,
                    route_json TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(packet_event_id) REFERENCES packets(event_id) ON DELETE CASCADE,
                    FOREIGN KEY(requester_node_id) REFERENCES nodes(node_id) ON DELETE SET NULL,
                    FOREIGN KEY(responder_node_id) REFERENCES nodes(node_id) ON DELETE SET NULL
                )""")

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS average_metrics_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    average_snr REAL,
                    average_rssi REAL,
                    average_battery REAL,        -- Added
                    average_chan_util REAL,    -- Added
                    average_air_util_tx REAL,  -- Added
                    active_node_count INTEGER NOT NULL, -- Renamed from node_count, maybe filter by last_heard
                    total_node_count INTEGER NOT NULL,  -- Added total nodes known at the time
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )""")

                cursor.execute("CREATE INDEX IF NOT EXISTS idx_packets_timestamp ON packets(timestamp DESC)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_packets_from_id ON packets(from_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_last_heard ON nodes(last_heard DESC)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_node_timestamp ON positions(node_id, timestamp DESC)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_node_timestamp ON telemetry(node_id, timestamp DESC)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_traceroutes_timestamp ON traceroutes(timestamp DESC)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_avg_metrics_timestamp ON average_metrics_history(timestamp DESC)")

                cursor.execute("CREATE TRIGGER IF NOT EXISTS trigger_node_updated_at AFTER UPDATE ON nodes FOR EACH ROW BEGIN UPDATE nodes SET updated_at = CURRENT_TIMESTAMP WHERE node_id = OLD.node_id; END;")

                conn.commit()
            logger.info("Network database initialization complete.")
        except sqlite3.Error as e:
            logger.exception(f"Database initialization failed: {e}")
            raise

    def save_packet(self, packet: Dict):
        """Save a processed packet to the database."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                event_id = packet.get('event_id')
                timestamp = packet.get('timestamp')
                rx_time = packet.get('rxTime')
                from_id = packet.get('fromId')
                to_id = packet.get('toId')
                channel = packet.get('channel')
                portnum_str = packet.get('decoded', {}).get('portnum') if packet.get('decoded') else None
                packet_type = packet.get('app_packet_type')
                rx_snr = packet.get('rxSnr')
                rx_rssi = packet.get('rxRssi')
                hop_limit = packet.get('hopLimit')
                want_ack = packet.get('wantAck', False)
                decoded_json = json.dumps(packet.get('decoded')) if packet.get('decoded') else None
                raw_json = json.dumps(packet) 

                if not event_id or not timestamp:
                    logger.error(f"Skipping packet save: Missing event_id or timestamp. Packet: {packet}")
                    return

                cursor.execute("""
                INSERT OR REPLACE INTO packets (
                    event_id, timestamp, rx_time, from_id, to_id, channel,
                    portnum, packet_type, rx_snr, rx_rssi, hop_limit, want_ack,
                    decoded, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (event_id, timestamp, rx_time, from_id, to_id, channel,
                      portnum_str, packet_type, rx_snr, rx_rssi, hop_limit, want_ack,
                      decoded_json, raw_json))

                if packet_type == "Position" and from_id and packet.get('decoded'):
                    position_data = packet['decoded'].get('position')
                    if position_data and isinstance(position_data, dict):
                        self.save_position(cursor, from_id, timestamp, position_data)

                elif packet_type == "Telemetry" and from_id and packet.get('decoded'):
                    telemetry_data = packet['decoded'].get('telemetry')
                    if telemetry_data and isinstance(telemetry_data, dict):
                        device_metrics = telemetry_data.get('deviceMetrics')
                        env_metrics = telemetry_data.get('environmentMetrics')
                        self.save_telemetry(cursor, from_id, timestamp, device_metrics, env_metrics)

                elif packet_type == "Routing" and from_id and to_id and packet.get('decoded'):
                    routing_data = packet['decoded'].get('routing')
                    if routing_data and isinstance(routing_data, dict) and 'route' in routing_data:
                        route_list = routing_data.get('route')
                        if isinstance(route_list, list) and all(isinstance(n, int) for n in route_list):
                            self.save_traceroute(cursor, event_id, requester_id=to_id, responder_id=from_id,
                                                 route=route_list, timestamp=timestamp)

                conn.commit()
        except sqlite3.Error as e:
            logger.exception(f"Database error saving packet {packet.get('event_id', 'N/A')}: {e}")
        except Exception as e:
            logger.exception(f"Unexpected error saving packet {packet.get('event_id', 'N/A')}: {e}")

    def save_position(self, cursor: sqlite3.Cursor, node_id: str, timestamp: float, position: Dict):
        """Save a position update to the positions table."""
        lat = position.get('latitudeI')
        lon = position.get('longitudeI')
        latitude, longitude = None, None
        if lat is not None and lon is not None:
            latitude, longitude = lat / 1e7, lon / 1e7
        else:
            latitude = position.get('latitude')
            longitude = position.get('longitude')

        if latitude is None or longitude is None:
             logger.warning(f"Skipping position save for node {node_id}: Missing lat/lon. Data: {position}")
             return

        try:
            cursor.execute("""
            INSERT INTO positions (
                node_id, timestamp, latitude, longitude, altitude,
                precision_bits, ground_speed, ground_track, sats_in_view,
                pdop, hdop, vdop
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (node_id, timestamp, latitude, longitude,
                  position.get('altitude'), position.get('precisionBits'),
                  position.get('groundSpeed'), position.get('groundTrack'),
                  position.get('satsInView'), position.get('pdop'),
                  position.get('hdop'), position.get('vdop')))
        except sqlite3.Error as e:
            logger.error(f"DB Error saving position for node {node_id}: {e}")

    def save_telemetry(self, cursor: sqlite3.Cursor, node_id: str, timestamp: float,
                       device_metrics: Optional[Dict], env_metrics: Optional[Dict]):
        """Save a telemetry update to the telemetry table."""
        if not device_metrics and not env_metrics: return

        bat = device_metrics.get('batteryLevel') if device_metrics else None
        volt = device_metrics.get('voltage') if device_metrics else None
        chan_util = device_metrics.get('channelUtilization') if device_metrics else None
        air_util = device_metrics.get('airUtilTx') if device_metrics else None
        uptime = device_metrics.get('uptimeSeconds') if device_metrics else None

        temp = env_metrics.get('temperature') if env_metrics else None
        hum = env_metrics.get('relativeHumidity') if env_metrics else None
        press = env_metrics.get('barometricPressure') if env_metrics else None
        gas = env_metrics.get('gasResistance') if env_metrics else None
        iaq = env_metrics.get('iaq') if env_metrics else None

        if any(v is not None for v in [bat, volt, chan_util, air_util, uptime, temp, hum, press, gas, iaq]):
            try:
                cursor.execute("""
                INSERT INTO telemetry (
                    node_id, timestamp, battery_level, voltage,
                    channel_utilization, air_util_tx, uptime_seconds, temperature,
                    relative_humidity, barometric_pressure, gas_resistance, iaq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (node_id, timestamp, bat, volt, chan_util, air_util, uptime,
                      temp, hum, press, gas, iaq))
            except sqlite3.Error as e:
                logger.error(f"DB Error saving telemetry for node {node_id}: {e}")
        else:
            logger.debug(f"Skipping telemetry save for node {node_id}: All metric values were None.")

    def save_node(self, node_id_str: str, data: Dict):
        """Save or update node information in the database."""
        if not node_id_str or not data:
            logger.warning("Attempted to save node with empty ID or data.")
            return

        node_num = data.get('num')
        user_info = data.get('user', {})
        long_name = user_info.get('longName') or data.get('long_name')
        short_name = user_info.get('shortName') or data.get('short_name')
        macaddr = user_info.get('macaddr') or data.get('macaddr')
        hw_model = user_info.get('hwModel') or data.get('hwModelStr') or data.get('hw_model')
        firmware_version = data.get('firmwareVersion') or data.get('firmware_version')
        role_enum = data.get('role')
        role_str = str(role_enum) if role_enum is not None else data.get('role') 
        is_local = data.get('isLocal', False)

        device_metrics = data.get('deviceMetrics', {})
        battery_level = data.get('batteryLevel') or device_metrics.get('batteryLevel') or data.get('battery_level')
        voltage = data.get('voltage') or device_metrics.get('voltage')
        channel_utilization = data.get('channelUtilization') or device_metrics.get('channelUtilization') or data.get('channel_utilization')
        air_util_tx = data.get('airUtilTx') or device_metrics.get('airUtilTx') or data.get('air_util_tx')
        uptime_seconds = data.get('uptimeSeconds') or device_metrics.get('uptimeSeconds') 
        telemetry_time = data.get('telemetry_time') 

        env_metrics = data.get('environmentMetrics', {}) 

        position_info = data.get('position', {})
        latitude, longitude, altitude = None, None, None
        lat_f = position_info.get('latitude')
        lon_f = position_info.get('longitude')
        if lat_f is not None and lon_f is not None: latitude, longitude = lat_f, lon_f
        elif 'latitudeI' in position_info and 'longitudeI' in position_info:
             lat_i = position_info['latitudeI']
             lon_i = position_info['longitudeI']
             if lat_i is not None and lon_i is not None: latitude, longitude = lat_i / 1e7, lon_i / 1e7
        altitude = position_info.get('altitude') or data.get('altitude')
        position_time = data.get('position_time') or position_info.get('time')

        last_heard = data.get('lastHeard') or data.get('last_heard')
        snr = data.get('snr')
        rssi = data.get('rssi')

        user_info_json = json.dumps(user_info) if user_info else None
        position_info_json = json.dumps(position_info) if position_info else None
        device_metrics_json = json.dumps(device_metrics) if device_metrics else None
        env_metrics_json = json.dumps(env_metrics) if env_metrics else None 
        module_config_json = json.dumps(data.get('moduleConfig')) if data.get('moduleConfig') else None
        channel_info_json = json.dumps(data.get('channelSettings')) if data.get('channelSettings') else None

        if node_num is None and node_id_str.startswith('!'):
            try: node_num = int(node_id_str[1:], 16)
            except ValueError: logger.warning(f"Could not parse node number from ID {node_id_str}")

        if node_num is None:
            logger.error(f"Cannot save node {node_id_str}: node number is missing.")
            return

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute("""
                INSERT INTO nodes (
                    node_id, node_num, long_name, short_name, macaddr, hw_model, firmware_version,
                    role, is_local, last_heard, battery_level, voltage, channel_utilization,
                    air_util_tx, uptime_seconds, snr, rssi, latitude, longitude, altitude,
                    position_time, telemetry_time, user_info, position_info, device_metrics_info,
                    environment_metrics_info, module_config_info, channel_info, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP
                )
                ON CONFLICT(node_id) DO UPDATE SET
                    node_num=excluded.node_num,
                    long_name=COALESCE(excluded.long_name, nodes.long_name),
                    short_name=COALESCE(excluded.short_name, nodes.short_name),
                    macaddr=COALESCE(excluded.macaddr, nodes.macaddr),
                    hw_model=COALESCE(excluded.hw_model, nodes.hw_model),
                    firmware_version=COALESCE(excluded.firmware_version, nodes.firmware_version),
                    role=COALESCE(excluded.role, nodes.role),
                    is_local=excluded.is_local,
                    last_heard=COALESCE(excluded.last_heard, nodes.last_heard),
                    battery_level=COALESCE(excluded.battery_level, nodes.battery_level),
                    voltage=COALESCE(excluded.voltage, nodes.voltage),
                    channel_utilization=COALESCE(excluded.channel_utilization, nodes.channel_utilization),
                    air_util_tx=COALESCE(excluded.air_util_tx, nodes.air_util_tx),
                    uptime_seconds=COALESCE(excluded.uptime_seconds, nodes.uptime_seconds),
                    snr=COALESCE(excluded.snr, nodes.snr),
                    rssi=COALESCE(excluded.rssi, nodes.rssi),
                    latitude=COALESCE(excluded.latitude, nodes.latitude),
                    longitude=COALESCE(excluded.longitude, nodes.longitude),
                    altitude=COALESCE(excluded.altitude, nodes.altitude),
                    position_time=COALESCE(excluded.position_time, nodes.position_time),
                    telemetry_time=COALESCE(excluded.telemetry_time, nodes.telemetry_time),
                    user_info=COALESCE(excluded.user_info, nodes.user_info),
                    position_info=COALESCE(excluded.position_info, nodes.position_info),
                    device_metrics_info=COALESCE(excluded.device_metrics_info, nodes.device_metrics_info),
                    environment_metrics_info=COALESCE(excluded.environment_metrics_info, nodes.environment_metrics_info),
                    module_config_info=COALESCE(excluded.module_config_info, nodes.module_config_info),
                    channel_info=COALESCE(excluded.channel_info, nodes.channel_info),
                    updated_at=CURRENT_TIMESTAMP
                """, (
                    node_id_str, node_num, long_name, short_name, macaddr, hw_model, firmware_version,
                    role_str, is_local, last_heard, battery_level, voltage, channel_utilization,
                    air_util_tx, uptime_seconds, snr, rssi, latitude, longitude, altitude,
                    position_time, telemetry_time, user_info_json, position_info_json, device_metrics_json,
                    env_metrics_json, module_config_json, channel_info_json
                ))
                conn.commit()
        except sqlite3.IntegrityError as e:
            logger.error(f"DB Integrity Error saving node {node_id_str} (node_num: {node_num}): {e}. Data: {json.dumps(data, indent=2)}")
        except sqlite3.Error as e:
            logger.exception(f"Database error saving node {node_id_str}: {e}")
        except Exception as e:
            logger.exception(f"Unexpected error saving node {node_id_str}: {e}")

    def get_all_nodes(self) -> Dict[str, Dict]:
        """Get all nodes from the database, keyed by node_id."""
        nodes = {}
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM nodes ORDER BY node_num")
                rows = cursor.fetchall()
                for row in rows:
                    node = dict(row)
                    node_id = node.get('node_id')
                    if not node_id: continue

                    for field in ['user_info', 'position_info', 'device_metrics_info', 'environment_metrics_info', 'module_config_info', 'channel_info']:
                        try:
                            if node.get(field): node[field] = json.loads(node[field])
                        except (json.JSONDecodeError, TypeError) as e:
                            logger.warning(f"Error decoding JSON field '{field}' for node {node_id}: {e}")
                            node[field] = {"error": f"Failed to decode {field} JSON from DB"} if node.get(field) else None
                    nodes[node_id] = node
            return nodes
        except sqlite3.Error as e:
            logger.exception(f"Error getting nodes from database: {e}")
            return {}
        except Exception as e:
            logger.exception(f"Unexpected error getting nodes: {e}")
            return {}

    def save_traceroute(self, cursor: sqlite3.Cursor, packet_event_id: str, requester_id: str, responder_id: str, route: List[int], timestamp: float):
        """Save a traceroute result to the traceroutes table."""
        try:
            route_json = json.dumps(route)
            cursor.execute("""
            INSERT OR REPLACE INTO traceroutes (
                packet_event_id, requester_node_id, responder_node_id, route_json, timestamp
            ) VALUES (?, ?, ?, ?, ?)
            """, (packet_event_id, requester_id, responder_id, route_json, timestamp))
        except sqlite3.Error as e:
            logger.error(f"DB Error saving traceroute result for packet {packet_event_id}: {e}")
        except Exception as e:
             logger.error(f"Unexpected error saving traceroute result for packet {packet_event_id}: {e}")

    def get_traceroute_history(self, requester_id: Optional[str] = None, responder_id: Optional[str] = None,
                               start_time: Optional[float] = None, end_time: Optional[float] = None,
                               limit: int = 100) -> List[Dict]:
        """Get traceroute results from the database with optional filters."""

        results = []
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                query = "SELECT * FROM traceroutes WHERE 1=1"
                params = []
                if requester_id: query += " AND requester_node_id = ?"; params.append(requester_id)
                if responder_id: query += " AND responder_node_id = ?"; params.append(responder_id)
                if start_time: query += " AND timestamp >= ?"; params.append(start_time)
                if end_time: query += " AND timestamp <= ?"; params.append(end_time)
                query += " ORDER BY timestamp DESC LIMIT ?"
                params.append(limit)
                cursor.execute(query, tuple(params))
                rows = cursor.fetchall()
                results = [dict(row) for row in rows]
            return results
        except sqlite3.Error as e:
            logger.exception(f"Error getting traceroute history from database: {e}")
            return []
        except Exception as e:
            logger.exception(f"Unexpected error getting traceroute history: {e}")
            return []

    def _calculate_current_average_metrics(self, active_cutoff_time: float) -> Dict[str, Any]:
        """Helper method to query nodes and calculate current average network metrics."""
        metrics = {
            'average_snr': None, 'average_rssi': None, 'average_battery': None,
            'average_chan_util': None, 'average_air_util_tx': None,
            'active_node_count': 0, 'total_node_count': 0
        }
        snr_vals, rssi_vals, bat_vals, chan_vals, air_vals = [], [], [], [], []
        all_node_ids = set()
        active_node_ids = set()

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT node_id, snr, rssi, battery_level, channel_utilization, air_util_tx, last_heard
                    FROM nodes
                    WHERE is_local = FALSE
                """)
                rows = cursor.fetchall()
                metrics['total_node_count'] = len(rows)

                for row in rows:
                    all_node_ids.add(row['node_id'])

                    if row['last_heard'] and row['last_heard'] >= active_cutoff_time:
                        active_node_ids.add(row['node_id'])
                        if isinstance(row['snr'], (int, float)): snr_vals.append(float(row['snr']))
                        if isinstance(row['rssi'], (int, float)): rssi_vals.append(float(row['rssi']))
                        if isinstance(row['battery_level'], (int, float)): bat_vals.append(float(row['battery_level']))
                        if isinstance(row['channel_utilization'], (int, float)): chan_vals.append(float(row['channel_utilization']))
                        if isinstance(row['air_util_tx'], (int, float)): air_vals.append(float(row['air_util_tx']))

                metrics['active_node_count'] = len(active_node_ids)

                if snr_vals: metrics['average_snr'] = round(statistics.mean(snr_vals), 2)
                if rssi_vals: metrics['average_rssi'] = round(statistics.mean(rssi_vals), 1)
                if bat_vals: metrics['average_battery'] = round(statistics.mean(bat_vals), 1)
                if chan_vals: metrics['average_chan_util'] = round(statistics.mean(chan_vals), 2)
                if air_vals: metrics['average_air_util_tx'] = round(statistics.mean(air_vals), 2)

            return metrics
        except sqlite3.Error as e:
            logger.error(f"DB error calculating average metrics: {e}", exc_info=True)
            return metrics 
        except Exception as e:
            logger.error(f"Unexpected error calculating average metrics: {e}", exc_info=True)
            return metrics 

    def save_average_metrics(self, metrics: Dict[str, Any]):
        """Save calculated average metrics to the history table."""
        if metrics['active_node_count'] <= 0 and metrics['total_node_count'] <=0:
            return 

        current_timestamp = time.time()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO average_metrics_history (
                        timestamp, average_snr, average_rssi, average_battery,
                        average_chan_util, average_air_util_tx,
                        active_node_count, total_node_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (current_timestamp,
                      metrics['average_snr'], metrics['average_rssi'], metrics['average_battery'],
                      metrics['average_chan_util'], metrics['average_air_util_tx'],
                      metrics['active_node_count'], metrics['total_node_count']))
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"DB error saving average metrics: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Unexpected error saving average metrics: {e}", exc_info=True)

    def calculate_and_save_average_metrics(self, active_cutoff_time: float):
        """Calculates current averages from nodes table and saves to history."""

        current_metrics = self._calculate_current_average_metrics(active_cutoff_time)
        self.save_average_metrics(current_metrics)
        return current_metrics 

    def get_average_metrics_history(self, limit: int = 100, start_time: Optional[float] = None, end_time: Optional[float] = None) -> List[Dict]:
        """Get historical average metrics."""

        results = []
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                query = "SELECT * FROM average_metrics_history WHERE 1=1"
                params = []
                if start_time: query += " AND timestamp >= ?"; params.append(start_time)
                if end_time: query += " AND timestamp <= ?"; params.append(end_time)
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
        """Get the single most recent average metrics entry."""

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
        """Remove average metrics history older than max_age_days."""

        if max_age_days <= 0:
            logger.warning("Pruning for average metrics history is disabled (max_age_days <= 0).")
            return

        cutoff_timestamp = time.time() - (max_age_days * 24 * 60 * 60)
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM average_metrics_history WHERE timestamp < ?", (cutoff_timestamp,))
                deleted_count = cursor.rowcount
                conn.commit()
                if deleted_count > 0:
                    logger.info(f"Pruned {deleted_count} old average metrics history records (older than {max_age_days} days).")
        except sqlite3.Error as e:
            logger.error(f"DB error pruning average metrics history: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Unexpected error pruning average metrics history: {e}", exc_info=True)

    def get_network_summary_stats(self, active_cutoff_time: float) -> Dict:
        """Calculates overall network summary statistics."""
        summary = {
            'total_nodes': 0,
            'active_nodes': 0,
            'nodes_with_position': 0,
            'nodes_with_telemetry': 0,
            'router_nodes': 0,
            'client_nodes': 0,
            'other_role_nodes': 0,
            'avg_hop_limit_packets': None, 

        }
        hop_limits = []
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT node_id, last_heard, latitude, telemetry_time, role
                    FROM nodes
                    WHERE is_local = FALSE
                """)
                rows = cursor.fetchall()
                summary['total_nodes'] = len(rows)
                for row in rows:
                    is_active = row['last_heard'] and row['last_heard'] >= active_cutoff_time
                    if is_active:
                        summary['active_nodes'] += 1
                    if row['latitude'] is not None: 
                        summary['nodes_with_position'] += 1
                    if row['telemetry_time'] is not None: 
                         summary['nodes_with_telemetry'] += 1

                    role = str(row['role']).upper() if row['role'] else 'UNKNOWN'
                    if 'ROUTER' in role: summary['router_nodes'] += 1
                    elif 'CLIENT' in role: summary['client_nodes'] += 1
                    else: summary['other_role_nodes'] += 1

            current_averages = self.get_most_recent_average_metrics() or {}
            summary.update({k: current_averages.get(k) for k in [
                'average_snr', 'average_rssi', 'average_battery',
                'average_chan_util', 'average_air_util_tx', 'active_node_count'
            ]})

            if 'active_node_count' in summary and summary['active_node_count'] is not None:
                 summary['active_nodes'] = summary['active_node_count'] 

            return summary

        except sqlite3.Error as e:
            logger.exception(f"DB Error getting network summary stats: {e}")
            return summary
        except Exception as e:
            logger.exception(f"Unexpected error getting network summary stats: {e}")
            return summary

class ApiTraceRouteData(PydanticBaseModel):
    id: int
    packet_event_id: str
    requester_node_id: str
    responder_node_id: str
    route_json: str
    route: List[int] = Field(description="List of node numbers in the path")
    timestamp: float
    created_at: Optional[Any] = None

    if PYDANTIC_V2 and model_validator:
        @model_validator(mode='before')
        def parse_route_json_v2(cls, values):
            route_json_str = values.get('route_json')
            if route_json_str and isinstance(route_json_str, str):
                try: values['route'] = json.loads(route_json_str)
                except json.JSONDecodeError: logger.warning(f"Failed to parse route_json: {route_json_str}"); values['route'] = []
            elif 'route' not in values: values['route'] = []
            return values
    elif not PYDANTIC_V2 and pydantic_validator_v1:
        @pydantic_validator_v1('route', pre=True, always=True)
        def parse_route_json_v1(cls, v, *, values, **kwargs):
            route_json_str = values.get('route_json')
            if route_json_str and isinstance(route_json_str, str):
                try: return json.loads(route_json_str)
                except json.JSONDecodeError: logger.warning(f"Failed to parse route_json: {route_json_str}"); return []
            return v if v is not None else []
    else: 
        @classmethod
        def __get_validators__(cls): yield cls.fallback_parse_route_json
        @classmethod
        def fallback_parse_route_json(cls, values):
            if isinstance(values, dict):
                route_json_str = values.get('route_json')
                if route_json_str and isinstance(route_json_str, str):
                    try: values['route'] = json.loads(route_json_str)
                    except json.JSONDecodeError: values['route'] = []
                elif 'route' not in values: values['route'] = []
            return values

class ApiAverageMetricsData(PydanticBaseModel):
    id: Optional[int] = None
    timestamp: float
    average_snr: Optional[float] = None
    average_rssi: Optional[float] = None
    average_battery: Optional[float] = None
    average_chan_util: Optional[float] = None
    average_air_util_tx: Optional[float] = None
    active_node_count: int
    total_node_count: int
    created_at: Optional[Any] = None

class ApiAverageMetricsResponse(PydanticBaseModel):
    most_recent: Optional[ApiAverageMetricsData] = None
    history: List[ApiAverageMetricsData] = []

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

class ApiEnvironmentMetrics(PydanticBaseModel): 
    time: Optional[int] = None
    temperature: Optional[float] = None
    relativeHumidity: Optional[float] = None
    barometricPressure: Optional[float] = None
    gasResistance: Optional[float] = None
    iaq: Optional[float] = None

class ApiPositionInfo(PydanticBaseModel):
    time: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[int] = None
    groundSpeed: Optional[int] = None
    satsInView: Optional[int] = None

class ApiNodeData(PydanticBaseModel):
      node_id: str = Field(description="Node ID (e.g., !aabbccdd)")
      node_num: Optional[int] = Field(description="Node number (integer)")
      user: Optional[ApiUserInfo] = None
      long_name: Optional[str] = None
      short_name: Optional[str] = None
      macaddr: Optional[str] = None
      hw_model: Optional[str] = None
      firmware_version: Optional[str] = None
      role: Optional[str] = None
      is_local: bool = False
      last_heard: Optional[int] = Field(description="Unix timestamp of last packet received")
      snr: Optional[float] = None
      rssi: Optional[int] = None
      battery_level: Optional[int] = None
      voltage: Optional[float] = None
      channel_utilization: Optional[float] = None
      air_util_tx: Optional[float] = None
      uptime_seconds: Optional[int] = None 
      latitude: Optional[float] = None
      longitude: Optional[float] = None
      altitude: Optional[int] = None
      position_time: Optional[int] = Field(description="Unix timestamp of last position update")
      telemetry_time: Optional[int] = Field(description="Unix timestamp of last telemetry update")
      created_at: Optional[Any] = None
      updated_at: Optional[Any] = None

      position_info: Optional[ApiPositionInfo] = Field(None, description="Latest full position structure")
      device_metrics_info: Optional[ApiDeviceMetrics] = Field(None, description="Latest full device metrics structure")
      environment_metrics_info: Optional[ApiEnvironmentMetrics] = Field(None, description="Latest full environment metrics structure") 

class ApiNetworkSummary(PydanticBaseModel):
    timestamp: float = Field(description="Timestamp when the summary was generated")
    total_nodes: int = Field(description="Total number of unique nodes seen by the dashboard")
    active_nodes: int = Field(description="Number of nodes heard from recently")
    nodes_with_position: int = Field(description="Number of nodes that have ever reported a position")
    nodes_with_telemetry: int = Field(description="Number of nodes that have ever reported telemetry")
    router_nodes: int = Field(description="Number of nodes identified with a ROUTER role")
    client_nodes: int = Field(description="Number of nodes identified with a CLIENT role")
    other_role_nodes: int = Field(description="Number of nodes with other or unknown roles")
    average_snr: Optional[float] = Field(description="Average SNR across active nodes")
    average_rssi: Optional[float] = Field(description="Average RSSI across active nodes")
    average_battery: Optional[float] = Field(description="Average battery level across active nodes")
    average_chan_util: Optional[float] = Field(description="Average channel utilization across active nodes")
    average_air_util_tx: Optional[float] = Field(description="Average air utilization TX across active nodes")

def ensure_serializable(obj: Any) -> Any:
    """ Ensure obj is JSON serializable, handling Pydantic V1/V2 """
    if isinstance(obj, PydanticBaseModel):
        if PYDANTIC_V2:
            try:
                return obj.model_dump(mode='json')
            except Exception as e:
                logger.warning(f"Error during Pydantic V2 model_dump for {type(obj)}: {e}. Falling back.")
                return {k: ensure_serializable(v) for k, v in obj.__dict__.items() if not k.startswith('_')}
        else:
            return obj.dict() 
    elif isinstance(obj, dict):

        try:
            return {str(k): ensure_serializable(v) for k, v in obj.items()}
        except TypeError: 
             return {str(k): ensure_serializable(v) for k, v in dict(obj).items()}
    elif isinstance(obj, (list, tuple, deque, set)): 
        return [ensure_serializable(item) for item in obj]

    elif isinstance(obj, bytes):
        try:
            return obj.decode('utf-8')
        except UnicodeDecodeError:
            return f"base64:{base64.b64encode(obj).decode('utf-8')}"
    elif isinstance(obj, (datetime, time.struct_time)):
        return obj.isoformat() if hasattr(obj, 'isoformat') else str(obj)
    elif hasattr(obj, '_pb') and hasattr(obj, 'DESCRIPTOR'): 
        serializable_dict = {}
        for field in obj.DESCRIPTOR.fields:
            try:
                value = getattr(obj, field.name)
                serializable_dict[field.name] = ensure_serializable(value)
            except Exception:
                pass 
        return serializable_dict
    else:
        try:

            json.dumps(obj)
            return obj
        except (TypeError, OverflowError):

            return str(obj)

class MeshtasticNetworkAnalyzer:
    """
    Class for storing and analyzing Meshtastic network data.
    """
    def __init__(self, db_manager: NetworkDatabaseManager):
        self.db = db_manager

        self.nodes: Dict[str, Dict] = self.db.get_all_nodes()
        logger.info(f"Loaded {len(self.nodes)} nodes from database.")
        self.local_node_info: Optional[Dict] = None
        self.local_node_id: Optional[str] = None
        self.connection_status: str = "Initializing"
        self.last_error: Optional[str] = None
        self.packet_counter = 0 
        self.last_network_summary: Optional[Dict] = None
        self.last_average_metrics: Optional[Dict] = None

        self.stats = {
            "packets_received_session": 0,
            "position_updates_session": 0,
            "telemetry_reports_session": 0,
            "traceroute_results_session": 0,
            "start_time": time.time(),
            "nodes_seen_session": set(self.nodes.keys()), 
        }

    def add_packet(self, packet: Dict) -> Optional[Dict]:
        """
        Process a raw packet, update node state, save to DB, and trigger analysis.
        Returns the processed packet dict or None if invalid.
        """
        if not packet or not isinstance(packet, dict):
            logger.warning(f"Received invalid packet data type: {type(packet)}")
            return None

        try:

            processed_packet = packet.copy()
            processed_packet = ensure_serializable(processed_packet) 

            rx_time = processed_packet.get('rxTime', int(time.time()))
            processed_packet['timestamp'] = float(rx_time) 
            processed_packet['event_id'] = f"pkt_{time.time_ns()}_{self.packet_counter}"
            self.packet_counter += 1

            from_num = packet.get('from')
            to_num = packet.get('to')
            from_id_str = f"!{from_num:08x}" if isinstance(from_num, int) else None
            to_id_str = None
            if isinstance(to_num, int):
                to_id_str = "^all" if to_num == 0xFFFFFFFF else f"!{to_num:08x}"

            processed_packet['fromId'] = from_id_str
            processed_packet['toId'] = to_id_str

            processed_packet = ensure_serializable(processed_packet)

        except Exception as e:
            logger.error(f"Failed initial processing/serialization of packet: {e}. Packet: {packet}", exc_info=True)
            return None

        packet_type, payload_data = self._classify_packet_type(processed_packet)
        processed_packet['app_packet_type'] = packet_type

        self.stats["packets_received_session"] += 1
        if packet_type == "Position": self.stats["position_updates_session"] += 1
        elif packet_type == "Telemetry": self.stats["telemetry_reports_session"] += 1
        elif packet_type == "Routing" and 'route' in (payload_data or {}):
            self.stats["traceroute_results_session"] += 1

        if from_id_str:
             self.stats["nodes_seen_session"].add(from_id_str)

        self.db.save_packet(processed_packet) 

        if from_id_str:
            node_update_data = {'lastHeard': rx_time}
            if 'rxSnr' in processed_packet: node_update_data['snr'] = processed_packet['rxSnr']
            if 'rxRssi' in processed_packet: node_update_data['rssi'] = processed_packet['rxRssi']
            if 'hopLimit' in processed_packet: node_update_data['lastHopLimit'] = processed_packet['hopLimit'] 

            if packet_type == "Position" and payload_data:
                node_update_data['position'] = payload_data
                node_update_data['position_time'] = rx_time
            elif packet_type == "Telemetry" and payload_data:
                if payload_data.get('deviceMetrics'):
                    node_update_data['deviceMetrics'] = payload_data['deviceMetrics']

                    if 'uptimeSeconds' in payload_data['deviceMetrics']:
                        node_update_data['uptimeSeconds'] = payload_data['deviceMetrics']['uptimeSeconds']
                if payload_data.get('environmentMetrics'):
                    node_update_data['environmentMetrics'] = payload_data['environmentMetrics'] 
                node_update_data['telemetry_time'] = rx_time
            elif packet_type == "User Info" and payload_data:
                node_update_data['user'] = payload_data

                if 'hwModel' in payload_data: node_update_data['hw_model'] = payload_data.get('hwModel')
                if 'role' in payload_data: node_update_data['role'] = str(payload_data.get('role')) 

            self.update_node(from_id_str, node_update_data, trigger_analysis=True, broadcast_change=True)

        if packet_type == "Routing" and 'route' in (payload_data or {}):
            if main_event_loop:
                asyncio.run_coroutine_threadsafe(
                     broadcast_traceroute_result(packet_event_id=processed_packet['event_id'],
                                                requester_id=to_id_str, 
                                                responder_id=from_id_str, 
                                                route=payload_data['route'],
                                                timestamp=processed_packet['timestamp']),
                     main_event_loop
                 )

        return processed_packet 

    def _classify_packet_type(self, packet: Dict) -> Tuple[str, Optional[Any]]:
        """ Determine packet type based on decoded content (Simplified from original)."""
        decoded = packet.get('decoded')
        if not isinstance(decoded, dict):
            if 'encrypted' in packet: return "Encrypted", packet.get('encrypted')

            return "Unknown", None

        portnum = decoded.get('portnum', 'UNKNOWN')

        if portnum == 'POSITION_APP' and 'position' in decoded: return "Position", decoded['position']
        if portnum == 'NODEINFO_APP' and 'user' in decoded: return "User Info", decoded['user']
        if portnum == 'TELEMETRY_APP' and 'telemetry' in decoded: return "Telemetry", decoded['telemetry']
        if portnum == 'ROUTING_APP' and 'routing' in decoded: return "Routing", decoded.get('routing')
        if portnum == 'TEXT_MESSAGE_APP': return "Message", decoded.get('payload') 

        if 'position' in decoded: return "Position", decoded['position']
        if 'telemetry' in decoded: return "Telemetry", decoded['telemetry']
        if 'user' in decoded: return "User Info", decoded['user']
        if 'routing' in decoded: return "Routing", decoded.get('routing')

        return "Other", decoded 

    def update_node(self, node_id_str: str, data: Dict, trigger_analysis: bool = False, broadcast_change: bool = False) -> None:
        """
        Update node information in memory, trigger DB save, optionally trigger network analysis, and broadcast.
        """
        if not node_id_str or not data:
            logger.warning(f"Attempted to update node with empty ID ('{node_id_str}') or data.")
            return

        try:
            serializable_data = ensure_serializable(data.copy())
        except Exception as e:
            logger.error(f"Failed to serialize node update data for {node_id_str}: {e}. Data: {data}", exc_info=True)
            return

        is_new_node = node_id_str not in self.nodes
        original_node_data = self.nodes.get(node_id_str, {}).copy() 

        def merge_dicts(target, source):
             if not isinstance(target, dict): target = {} 
             if not isinstance(source, dict): return target 

             for key, value in source.items():

                 if key not in target or target[key] is None or not isinstance(value, dict):
                      target[key] = value

                 elif isinstance(target.get(key), dict):
                      merge_dicts(target[key], value)

                 else:
                      target[key] = value
             return target

        if is_new_node:
             self.nodes[node_id_str] = {'node_id': node_id_str, 'created_at': time.time()} 
             logger.info(f"First time seeing node {node_id_str}, creating entry.")

        self.nodes[node_id_str] = merge_dicts(self.nodes.get(node_id_str, {}), serializable_data)

        if 'node_num' not in self.nodes[node_id_str] and node_id_str.startswith('!'):
             try: self.nodes[node_id_str]['node_num'] = int(node_id_str[1:], 16)
             except ValueError: pass 
        self.nodes[node_id_str]['node_id'] = node_id_str 
        self.nodes[node_id_str]['last_updated'] = time.time() 
        self.nodes[node_id_str]['isLocal'] = (node_id_str == self.local_node_id)

        if is_new_node:
            self.stats["nodes_seen_session"].add(node_id_str)

        db_node_data = self.nodes[node_id_str].copy()
        db_node_data['num'] = db_node_data.get('node_num') 

        self.db.save_node(node_id_str, db_node_data)

        metrics_changed = False
        for key in ['snr', 'rssi', 'batteryLevel', 'voltage', 'channelUtilization', 'airUtilTx', 'lastHeard']:

             if key in serializable_data and serializable_data[key] != original_node_data.get(key):
                 metrics_changed = True
                 break

        if trigger_analysis and metrics_changed and not self.nodes[node_id_str]['isLocal']: 
             logger.debug(f"Metrics update for node {node_id_str} triggering analysis.")

             if main_event_loop:
                 asyncio.run_coroutine_threadsafe(self.perform_network_analysis(broadcast=True), main_event_loop)

        if broadcast_change and main_event_loop:

             node_data_to_broadcast = ensure_serializable(self.nodes[node_id_str])
             asyncio.run_coroutine_threadsafe(
                 broadcast_data({"event": "node_update", "data": node_data_to_broadcast}),
                 main_event_loop
             )
             if is_new_node: 
                 asyncio.run_coroutine_threadsafe(self.broadcast_network_summary(), main_event_loop)

    def set_local_node_info(self, info: Optional[Any]) -> None:
        """Set local node info from myInfo object, update node entry, and broadcast."""

        if not info or not hasattr(info, 'my_node_num'):
            logger.warning(f"Setting local node info to None: Invalid info object: {type(info)}")
            if self.local_node_id and self.local_node_id in self.nodes:
                self.nodes[self.local_node_id]['isLocal'] = False

                if main_event_loop:
                     old_node_data_serializable = ensure_serializable(self.nodes[self.local_node_id])
                     asyncio.run_coroutine_threadsafe(
                          broadcast_data({"event": "node_update", "data": old_node_data_serializable}),
                          main_event_loop
                     )
            self.local_node_info = None
            self.local_node_id = None

            if main_event_loop:
                asyncio.run_coroutine_threadsafe(broadcast_data({"event": "local_node_info", "data": None}), main_event_loop)
            return

        try:
            node_id_num = info.my_node_num
            node_id_str = f"!{node_id_num:08x}" if isinstance(node_id_num, int) else None
            if not node_id_str: logger.error(f"Invalid local node num: {node_id_num}"); return

            if self.local_node_id and self.local_node_id != node_id_str and self.local_node_id in self.nodes:
                self.nodes[self.local_node_id]['isLocal'] = False
                if main_event_loop:
                     old_node_data_serializable = ensure_serializable(self.nodes[self.local_node_id])
                     asyncio.run_coroutine_threadsafe(
                         broadcast_data({"event": "node_update", "data": old_node_data_serializable}),
                         main_event_loop)

            self.local_node_id = node_id_str
            logger.info(f"Local node identified: {self.local_node_id} (Num: {node_id_num})")

            node_data = {
                'node_id': node_id_str,
                'node_num': node_id_num,
                'user': ensure_serializable(getattr(info, 'user', None)),
                'position': ensure_serializable(getattr(info, 'position', None)),
                'deviceMetrics': ensure_serializable(getattr(info, 'device_metrics', None)),
                'environmentMetrics': ensure_serializable(getattr(info, 'environment_metrics', None)), 
                'moduleConfig': ensure_serializable(getattr(info, 'module_config', None)),
                'channelSettings': ensure_serializable(getattr(info, 'channel_settings', None)),
                'firmware_version': getattr(info, 'firmware_version', None),
                'hw_model': getattr(info, 'hw_model_str', None), 
                'role': str(getattr(info, 'role', None)),
                'isLocal': True,
                'lastHeard': int(time.time()), 

                'snr': None,
                'rssi': None,

                'rebootCount': getattr(info, 'reboot_count', None),
                'bitrate': getattr(info, 'bitrate', None),

                'latitude': None, 'longitude': None, 'altitude': None, 'position_time': None,
                'battery_level': None, 'voltage': None, 'channel_utilization': None, 'air_util_tx': None, 'uptimeSeconds': None, 'telemetry_time': None,
            }

            pos = node_data['position']
            if pos:
                node_data['position_time'] = pos.get('time', int(time.time()))
                if 'latitudeI' in pos and 'longitudeI' in pos and pos['latitudeI'] is not None and pos['longitudeI'] is not None:
                    node_data['latitude'] = pos['latitudeI'] / 1e7
                    node_data['longitude'] = pos['longitudeI'] / 1e7
                else:
                    node_data['latitude'] = pos.get('latitude')
                    node_data['longitude'] = pos.get('longitude')
                node_data['altitude'] = pos.get('altitude')

            dm = node_data['deviceMetrics']
            if dm:
                node_data['telemetry_time'] = dm.get('time', int(time.time()))
                node_data['battery_level'] = dm.get('batteryLevel')
                node_data['voltage'] = dm.get('voltage')
                node_data['channel_utilization'] = dm.get('channelUtilization')
                node_data['air_util_tx'] = dm.get('airUtilTx')
                node_data['uptimeSeconds'] = dm.get('uptimeSeconds') 

            self.update_node(node_id_str, node_data, trigger_analysis=False, broadcast_change=False)

            self.local_node_info = {
                "node_id": node_id_str,
                "node_num": node_id_num,
                "name": node_data.get('user', {}).get('longName', 'Unknown'),
                "firmware": node_data.get('firmware_version'),
                "hardware": node_data.get('hw_model'),
                "role": node_data.get('role'),
                "position": node_data.get('position'), 
                "deviceMetrics": node_data.get('deviceMetrics'), 
            }

            if main_event_loop:
                local_info_serializable = ensure_serializable(self.local_node_info)
                asyncio.run_coroutine_threadsafe(
                     broadcast_data({"event": "local_node_info", "data": local_info_serializable}),
                     main_event_loop)

                if node_id_str in self.nodes:
                    full_node_data = ensure_serializable(self.nodes[node_id_str])
                    asyncio.run_coroutine_threadsafe(
                        broadcast_data({"event": "node_update", "data": full_node_data}),
                        main_event_loop)

        except Exception as e:
             logger.exception(f"Error setting local node info: {e}. Info object type: {type(info)}")
             self.local_node_info = None
             self.local_node_id = None

             if main_event_loop:
                  asyncio.run_coroutine_threadsafe(broadcast_data({"event": "local_node_info", "data": None}), main_event_loop)

    def set_connection_status(self, status: str) -> None:
        """Update connection status and broadcast the change."""
        if self.connection_status != status:
            logger.info(f"Connection status changed to: {status}")
            self.connection_status = status
            if main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "connection_status", "data": status}),
                    main_event_loop
                )

    def set_error(self, error: Optional[str]) -> None:
        """Set/clear the last error message and broadcast."""
        if self.last_error != error: 
            self.last_error = error
            if error:
                logger.error(f"Meshtastic Error Set: {error}")
            else:
                logger.info("Meshtastic Error Cleared.")
            if main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "error_status", "data": {"error": error}}),
                     main_event_loop)

    def get_serializable_session_stats(self) -> Dict:
        """Get current session statistics in a serializable format."""
        stats_copy = self.stats.copy()
        stats_copy["nodes_seen_session"] = len(self.stats["nodes_seen_session"])
        stats_copy["elapsed_time_session"] = round(time.time() - self.stats["start_time"])
        return ensure_serializable(stats_copy)

    async def perform_network_analysis(self, broadcast: bool = True) -> Dict:
        """
        Perform network analysis (calculate averages, summary) and optionally broadcast.
        Returns the latest summary.
        """
        logger.debug("Performing network analysis...")
        active_cutoff = time.time() - NODE_ACTIVITY_TIMEOUT_SECONDS

        self.last_average_metrics = self.db.calculate_and_save_average_metrics(active_cutoff)

        self.last_network_summary = self.db.get_network_summary_stats(active_cutoff)
        self.last_network_summary['timestamp'] = time.time() 

        if broadcast and main_event_loop:

            if self.last_average_metrics:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "average_metrics_update", "data": self.last_average_metrics}),
                     main_event_loop)

            await self.broadcast_network_summary() 

        return self.last_network_summary

    async def broadcast_network_summary(self):
        """Helper to broadcast the current network summary."""
        if self.last_network_summary and main_event_loop:
             asyncio.run_coroutine_threadsafe(
                 broadcast_data({"event": "network_summary", "data": self.last_network_summary}),
                 main_event_loop)

app = FastAPI(
    title="Meshtastic Network Analyzer API",
    description="API for analyzing Meshtastic network health, topology, and performance metrics.",
    version="1.0.0"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    db_manager = NetworkDatabaseManager(DB_PATH)
except Exception as e:
    logger.critical(f"FATAL: Could not initialize database manager: {e}", exc_info=True)
    sys.exit(1)

network_analyzer = MeshtasticNetworkAnalyzer(db_manager)

os.makedirs("static", exist_ok=True)

INDEX_HTML_PATH = "network_analyzer_index.html" 
if not os.path.exists(INDEX_HTML_PATH):
    logger.warning(f"{INDEX_HTML_PATH} not found. Serving placeholder page.")
    with open(INDEX_HTML_PATH, "w") as f:
        f.write("""<!DOCTYPE html><html><head><title>Meshtastic Network Analyzer</title>
                   <style>body { font-family: sans-serif; }</style></head>
                   <body><h1>Meshtastic Network Analyzer</h1>
                   <p>Connect to the <a href="/sse">SSE stream</a> for real-time network data.</p>
                   <p>API docs available at <a href="/docs">/docs</a>.</p>
                   <p>Status: <span id="status">Initializing...</span></p>
                   <pre id="summary">Network summary loading...</pre>
                   <script>
                       const statusElem = document.getElementById('status');
                       const summaryElem = document.getElementById('summary');
                       const evtSource = new EventSource("/sse");
                       evtSource.onmessage = function(event) { console.log("SSE Message:", event); }; // Generic log
                       evtSource.onerror = function(err) { console.error("SSE Error:", err); statusElem.textContent = "SSE Error"; };
                       evtSource.addEventListener("connection_status", function(event) { statusElem.textContent = JSON.parse(event.data); });
                       evtSource.addEventListener("network_summary", function(event) {
                           try {
                               const summary = JSON.parse(event.data);
                               summaryElem.textContent = JSON.stringify(summary, null, 2);
                           } catch (e) { console.error("Error parsing summary:", e); summaryElem.textContent = "Error parsing summary."; }
                       });
                   </script>
                   </body></html>""")

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
async def get_analyzer_dashboard(request: Request):
    """Serves the main HTML page for the Network Analyzer."""
    logger.info(f"Serving {INDEX_HTML_PATH} to {request.client.host}")
    return FileResponse(INDEX_HTML_PATH)

@app.get("/favicon.ico", include_in_schema=False)
async def get_favicon():

    return Response(status_code=status.HTTP_404_NOT_FOUND)

main_event_loop = None
sse_queues: List[asyncio.Queue] = []
sse_queues_lock = asyncio.Lock()

async def broadcast_data(data: Dict):
    """ Puts data onto all active SSE queues, ensuring payload is JSON string. """
    if 'event' not in data or 'data' not in data:
        logger.error(f"Invalid data format for broadcast: missing 'event' or 'data'. Data: {data}")
        return
    async with sse_queues_lock:
        if sse_queues:
            try:

                json_data_string = json.dumps(ensure_serializable(data['data']), separators=(',', ':'))
                event_dict_for_queue = {"event": data['event'], "data": json_data_string}

                if 'id' in data: event_dict_for_queue['id'] = data['id']
                if 'retry' in data: event_dict_for_queue['retry'] = data['retry']

                for q in sse_queues:
                    try:
                        await q.put(event_dict_for_queue)
                    except asyncio.QueueFull:
                         logger.warning(f"SSE queue full for a client while broadcasting {data['event']}. Client might be slow.")
                    except Exception as put_err:
                         logger.error(f"Error putting data onto specific SSE queue: {put_err}")

            except Exception as e:

                logger.error(f"Error serializing or preparing data for SSE broadcast: {e}. Event: {data['event']}", exc_info=True)

async def broadcast_traceroute_result(packet_event_id: str, requester_id: str, responder_id: str, route: List[int], timestamp: float):
    """ Broadcasts a completed traceroute result. """
    await broadcast_data({
        "event": "traceroute_result",
        "data": {
            "packet_event_id": packet_event_id,
            "requester_node_id": requester_id,
            "responder_node_id": responder_id,
            "route": route,
            "timestamp": timestamp,
        }
    })

def on_receive(packet, interface):
    """ Callback for received packets - passes to analyzer. """
    try:

        network_analyzer.add_packet(packet)
    except Exception as e:
        logger.exception(f"Error in on_receive callback: {e}")

def on_connection(interface, topic=pub.AUTO_TOPIC):
    """ Callback for connection status changes. """
    try:
        topic_str = pub.getCurrentTopic().getName() if hasattr(pub.getCurrentTopic(), 'getName') else str(topic)
        logger.debug(f"Connection event: {topic_str}")
        new_status = network_analyzer.connection_status 
        error_msg = None

        if "meshtastic.connection.established" in topic_str:
            new_status = "Connected"
            logger.info("Meshtastic connection established.")
            network_analyzer.set_error(None) 
            local_info = getattr(interface, 'myInfo', None)
            network_analyzer.set_local_node_info(local_info) 
            request_initial_node_state(interface) 

            if main_event_loop:
                asyncio.run_coroutine_threadsafe(network_analyzer.perform_network_analysis(broadcast=True), main_event_loop)

        elif "meshtastic.connection.lost" in topic_str:
            new_status = "Disconnected"
            error_msg = "Connection lost"
            logger.warning("Meshtastic connection lost.")
            network_analyzer.set_local_node_info(None)

        elif "meshtastic.connection.failed" in topic_str:
            new_status = "Error"
            error_msg = "Connection failed"
            logger.error("Meshtastic connection failed.")
            network_analyzer.set_local_node_info(None)

        network_analyzer.set_connection_status(new_status)
        if error_msg:
            network_analyzer.set_error(error_msg)

    except Exception as e:
        logger.exception(f"Error in on_connection callback: {e}")
        network_analyzer.set_connection_status("Error")
        network_analyzer.set_error(f"Connection callback error: {e}")
        network_analyzer.set_local_node_info(None)

def on_node_updated(node, interface):
    """ Callback when node info is updated by the Meshtastic library. """

    try:
        if node and isinstance(node, dict) and "num" in node:
            node_id_num = node["num"]
            node_id_str = f"!{node_id_num:08x}"
            cleaned_node_data = ensure_serializable(node)

            network_analyzer.update_node(node_id_str, cleaned_node_data, trigger_analysis=True, broadcast_change=True)
        else:
            logger.warning(f"Received invalid node update data format: {type(node)} - {node}")
    except Exception as e:
        logger.exception(f"Error in on_node_updated callback: {e}")

def request_initial_node_state(iface):
    """ Request node DB from the connected node. """
    if iface and hasattr(iface, 'requestNodes') and getattr(iface, 'isConnected', False):
        try:
            logger.info("Requesting node database refresh from device...")
            iface.requestNodes()
        except AttributeError as ae:
             logger.warning(f"Meshtastic interface missing expected request method: {ae}")
        except Exception as e:
            logger.error(f"Error requesting initial state from node: {e}", exc_info=True)
    elif iface:
         logger.warning("Cannot request node state: Interface not connected.")

@app.get("/sse", tags=["Realtime"])
async def sse_endpoint(request: Request):
    """ Server-Sent Events endpoint for real-time network updates. """
    client_queue = asyncio.Queue(maxsize=100) 
    async with sse_queues_lock:
        sse_queues.append(client_queue)
    client_host = request.client.host if request.client else "unknown"
    logger.info(f"SSE client connected: {client_host} (Total: {len(sse_queues)})")

    async def event_generator() -> AsyncGenerator[Dict[str, Any], None]:
        client_disconnected = False
        logger.info(f"SSE event_generator starting for {client_host}")
        try:
            logger.debug(f"Sending initial state to SSE client {client_host}")

            def create_event(event_name: str, data_payload: Any) -> Dict[str, Any]:
                try:
                     json_data_string = json.dumps(ensure_serializable(data_payload), separators=(',', ':'))
                     return {"event": event_name, "data": json_data_string}
                except Exception as json_e:
                     logger.error(f"Failed to serialize initial state for SSE event '{event_name}': {json_e}")
                     return {"event": "error", "data": json.dumps({"message": f"Serialization error for {event_name}"})}

            yield create_event("connection_status", network_analyzer.connection_status)
            yield create_event("local_node_info", network_analyzer.local_node_info)
            yield create_event("nodes", network_analyzer.nodes) 

            yield create_event("network_summary", network_analyzer.last_network_summary or {"message": "Calculating initial summary..."})
            yield create_event("average_metrics_update", network_analyzer.last_average_metrics or {"message": "Calculating initial averages..."})

            logger.debug(f"Initial state sent to SSE client {client_host}")
            logger.info(f"Entering SSE update loop for {client_host}")

            while not client_disconnected:
                try:

                    event_dict_from_queue = await asyncio.wait_for(client_queue.get(), timeout=25.0)

                    if await request.is_disconnected():
                        logger.info(f"SSE client {client_host} disconnected before sending data.")
                        client_disconnected = True; break

                    yield event_dict_from_queue
                    client_queue.task_done()

                except asyncio.TimeoutError:

                    if await request.is_disconnected():
                        logger.info(f"SSE client {client_host} disconnected (timeout check).")
                        client_disconnected = True; break
                    else:

                        yield {"comment": "heartbeat"}
                except asyncio.CancelledError:
                    logger.info(f"SSE generator task cancelled for {client_host}.")
                    client_disconnected = True; break
                except Exception as e:
                    logger.error(f"Error processing SSE queue item for {client_host}: {e}", exc_info=True)

                    try: yield {"event": "error", "data": json.dumps({"message": "Internal SSE error"})}
                    except: pass 
                    client_disconnected = True; break 

        except asyncio.CancelledError:
             logger.info(f"SSE event_generator coroutine cancelled for client {client_host}.")
             client_disconnected = True
        except Exception as e:
             logger.error(f"Unhandled error in SSE event_generator setup/initial send for {client_host}: {e}", exc_info=True)
             client_disconnected = True 
        finally:
            logger.info(f"SSE event_generator finally block for {client_host}. Removing queue.")
            async with sse_queues_lock:
                if client_queue in sse_queues:
                    sse_queues.remove(client_queue)
            logger.info(f"SSE queues remaining: {len(sse_queues)}")

    return EventSourceResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/status", response_model=Dict, tags=["API - Current State"])
async def get_status():
    """ Get current connection status and basic local node info. """
    return ensure_serializable({
        "connection_status": network_analyzer.connection_status,
        "local_node_info": network_analyzer.local_node_info, 
        "last_error": network_analyzer.last_error,
        "server_time_unix": time.time()
    })

def _map_node_to_api(node_data: Dict) -> Optional[ApiNodeData]:
     """ Helper to map internal node structure to API response model. """
     if not node_data or 'node_id' not in node_data: return None
     try:

         user_info = node_data.get('user_info', {}) or node_data.get('user', {})
         pos_info = node_data.get('position_info', {}) or node_data.get('position', {})
         metrics_info = node_data.get('device_metrics_info', {}) or node_data.get('deviceMetrics', {})
         env_metrics_info = node_data.get('environment_metrics_info', {}) or node_data.get('environmentMetrics', {}) 

         lat, lon = node_data.get('latitude'), node_data.get('longitude')
         if lat is None or lon is None:
              lat_i = pos_info.get('latitudeI')
              lon_i = pos_info.get('longitudeI')
              if lat_i is not None and lon_i is not None:
                   lat, lon = lat_i / 1e7, lon_i / 1e7
              else: 
                   lat = pos_info.get('latitude')
                   lon = pos_info.get('longitude')

         api_node = ApiNodeData(
             node_id=node_data.get('node_id'),
             node_num=node_data.get('node_num'),
             user=ApiUserInfo(**user_info) if user_info else None, 
             long_name=node_data.get('long_name') or user_info.get('longName'),
             short_name=node_data.get('short_name') or user_info.get('shortName'),
             macaddr=node_data.get('macaddr') or user_info.get('macaddr'),
             hw_model=node_data.get('hw_model') or user_info.get('hwModel'),
             firmware_version=node_data.get('firmware_version'),
             role=str(node_data.get('role')) if node_data.get('role') is not None else None,
             is_local=node_data.get('is_local', False),
             last_heard=node_data.get('lastHeard') or node_data.get('last_heard'),
             snr=node_data.get('snr'),
             rssi=node_data.get('rssi'),
             battery_level=node_data.get('battery_level') or metrics_info.get('batteryLevel'),
             voltage=node_data.get('voltage') or metrics_info.get('voltage'),
             channel_utilization=node_data.get('channel_utilization') or metrics_info.get('channelUtilization'),
             air_util_tx=node_data.get('air_util_tx') or metrics_info.get('airUtilTx'),
             uptime_seconds=node_data.get('uptimeSeconds') or metrics_info.get('uptimeSeconds'), 
             latitude=lat,
             longitude=lon,
             altitude=node_data.get('altitude') or pos_info.get('altitude'),
             position_time=node_data.get('position_time') or pos_info.get('time'),
             telemetry_time=node_data.get('telemetry_time') or metrics_info.get('time'),
             created_at=node_data.get('created_at'),
             updated_at=node_data.get('updated_at'),
             position_info=ApiPositionInfo(**pos_info) if pos_info else None, 
             device_metrics_info=ApiDeviceMetrics(**metrics_info) if metrics_info else None, 
             environment_metrics_info=ApiEnvironmentMetrics(**env_metrics_info) if env_metrics_info else None 
         )
         return api_node
     except Exception as e:
         logger.error(f"Error converting node {node_data.get('node_id')} to API model: {e}", exc_info=True)

         return ApiNodeData(node_id=node_data.get('node_id'), long_name="Error processing data")

@app.get("/api/nodes", response_model=Dict[str, ApiNodeData], tags=["API - Current State"])
async def get_nodes():
    """ Get all currently known nodes and their latest state (from memory). """
    api_nodes = {}

    current_nodes = network_analyzer.nodes
    for node_id, node_data in current_nodes.items():
         mapped_node = _map_node_to_api(node_data)
         if mapped_node:
             api_nodes[node_id] = mapped_node

    return api_nodes

@app.get("/api/nodes/{node_id}", response_model=ApiNodeData, tags=["API - Current State"])
async def get_node(node_id: str = Path(..., description="Node ID string (e.g., !aabbccdd)")):
    """ Get the latest state for a specific node. """
    node_data = network_analyzer.nodes.get(node_id)
    if node_data:
         mapped_node = _map_node_to_api(node_data)
         if mapped_node:
             return mapped_node
         else:

             raise HTTPException(status_code=500, detail="Error processing node data")
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Node '{node_id}' not found")

@app.get("/api/network/summary", response_model=ApiNetworkSummary, tags=["API - Current State"])
async def get_network_summary():
    """ Get the latest calculated network summary statistics. """
    if network_analyzer.last_network_summary:
        try:
            return ApiNetworkSummary(**network_analyzer.last_network_summary)
        except Exception as e:
             logger.error(f"Error validating network summary: {e}", exc_info=True)
             raise HTTPException(status_code=500, detail="Error retrieving network summary")
    else:

        logger.info("Network summary requested but not yet calculated, performing analysis...")
        summary = await network_analyzer.perform_network_analysis(broadcast=False) 
        if summary:
            try: return ApiNetworkSummary(**summary)
            except Exception as e:
                logger.error(f"Error validating newly calculated network summary: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail="Error retrieving network summary")
        else:
            raise HTTPException(status_code=503, detail="Network summary calculation failed")

@app.get("/api/metrics/averages", response_model=ApiAverageMetricsResponse, tags=["API - History"])
async def get_average_metrics_history_api(
    limit: int = Query(100, ge=1, le=5000, description="Max number of historical average records")
):
    """ Get the most recent calculated average network metrics and a list of historical averages. """
    most_recent = network_analyzer.db.get_most_recent_average_metrics()
    history = network_analyzer.db.get_average_metrics_history(limit=limit)
    try:
         return ApiAverageMetricsResponse(
             most_recent=ApiAverageMetricsData(**most_recent) if most_recent else None,
             history=[ApiAverageMetricsData(**item) for item in history]
         )
    except Exception as e:
        logger.error(f"Error validating average metrics data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error processing average metrics data")

@app.get("/api/traceroutes/history", response_model=List[ApiTraceRouteData], tags=["API - History"])
async def get_traceroutes_history_api(
    requester_id: Optional[str] = Query(None), responder_id: Optional[str] = Query(None),
    start_time: Optional[float] = Query(None), end_time: Optional[float] = Query(None),
    limit: int = Query(100, ge=1, le=1000)):
    """ Get historical traceroute results from the database with optional filters. """
    history = network_analyzer.db.get_traceroute_history(requester_id, responder_id, start_time, end_time, limit)
    try:
        return [ApiTraceRouteData(**item) for item in history]
    except Exception as e:
         logger.error(f"Error validating traceroute history data: {e}", exc_info=True)
         raise HTTPException(status_code=500, detail="Error processing traceroute history data")

@app.post("/api/traceroute/{destination_id}", status_code=status.HTTP_202_ACCEPTED, response_model=Dict, tags=["API - Actions"])
async def send_traceroute_request(
    destination_id: str = Path(..., description="Destination Node ID (e.g., !aabbccdd or ^local for self)"),
    hop_limit: int = Query(7, ge=0, le=7, description="Max hops (0=default max 7)") 
    ):
    """ Initiate a traceroute request to a destination node. Result arrives via SSE. """
    global interface 
    if not interface or not interface.isConnected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Not connected to Meshtastic device")
    if not destination_id.startswith("!") and destination_id != "^local":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid destination_id format. Must start with '!' or be '^local'.")

    hop_limit_to_send = hop_limit if hop_limit > 0 else 7 

    try:
        logger.info(f"Sending traceroute request via API to '{destination_id}' with hop limit {hop_limit_to_send}")
        if hasattr(interface, 'sendTraceRoute'):
            interface.sendTraceRoute(dest=destination_id, hopLimit=hop_limit_to_send)
            return {"status": "queued", "detail": "Traceroute request sent. Result will arrive via SSE event 'traceroute_result'."}
        else:
            logger.error("The connected meshtastic interface does not support sendTraceRoute.")
            raise HTTPException(status_code=501, detail="Traceroute feature not supported by the current interface.")
    except AttributeError as e:
        logger.error(f"Error sending traceroute: Interface might be missing method? {e}", exc_info=True)
        raise HTTPException(status_code=501, detail="Interface error: Traceroute not supported.")
    except meshtastic.MeshtasticException as e:
        logger.error(f"Meshtastic library error sending traceroute: {e}", exc_info=True)
        network_analyzer.set_error(f"Traceroute failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Meshtastic error sending traceroute: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending traceroute via API: {e}", exc_info=True)
        network_analyzer.set_error(f"Traceroute failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to send traceroute request: {e}")

async def periodic_network_analysis():
    """ Periodically run network analysis and broadcast results. """
    await asyncio.sleep(30) 
    while True:
        try:
            if network_analyzer.connection_status == "Connected":
                logger.debug("Running periodic network analysis...")
                await network_analyzer.perform_network_analysis(broadcast=True)
            else:
                 logger.debug("Skipping periodic analysis - not connected.")
        except Exception as e:
            logger.error(f"Error in periodic network analysis task: {e}", exc_info=True)
        await asyncio.sleep(60 * 5) 

async def check_connection_periodically():
    """ Periodically check the Meshtastic connection status (supplements callbacks). """
    global interface
    while True:
        await asyncio.sleep(30) 
        try:
            current_status = network_analyzer.connection_status
            is_connected_check = getattr(interface, 'isConnected', False) if interface else False

            if not is_connected_check and current_status == "Connected":
                logger.warning("Detected potential disconnection (periodic check). Setting status to Disconnected.")
                network_analyzer.set_connection_status("Disconnected")
                network_analyzer.set_error("Connection check failed")
                network_analyzer.set_local_node_info(None)
                if interface:
                    try: interface.close(); logger.info("Closed interface due to failed check.")
                    except Exception: pass
                    interface = None 

            elif interface is None and current_status not in ["Disconnected", "Initializing", "Connecting", "Error"]:
                 logger.warning(f"Interface is None, but status is '{current_status}'. Setting to Disconnected.")
                 network_analyzer.set_connection_status("Disconnected")

        except Exception as e:
            logger.error(f"Error in connection check task: {e}", exc_info=True)
            if network_analyzer.connection_status != "Error":
                network_analyzer.set_connection_status("Error")
                network_analyzer.set_error(f"Connection check task error: {e}")
            if interface:
                try: interface.close()
                except: pass
                interface = None

async def prune_history_periodically():
    """Periodically prune old records from history tables."""
    await asyncio.sleep(60 * 5) 
    while True:
        try:
            logger.info("Running periodic history pruning...")

            network_analyzer.db.prune_old_average_metrics(max_age_days=AVERAGE_METRICS_HISTORY_DAYS)

        except Exception as e:
            logger.error(f"Error in periodic history pruning task: {e}", exc_info=True)
        await asyncio.sleep(3600 * 6) 

async def proactive_traceroute_task():
     """(Optional) Periodically initiate traceroutes to known nodes."""
     if not ENABLE_PROACTIVE_TRACEROUTE:
         logger.info("Proactive tracerouting task is disabled.")
         return

     await asyncio.sleep(60 * 10) 
     logger.info("Starting proactive traceroute task...")

     while True:
         if interface and interface.isConnected:
             logger.info("Running proactive traceroutes...")
             nodes_to_trace = []
             try:

                 active_cutoff = time.time() - (3600 * 24)
                 current_nodes = network_analyzer.nodes.copy() 
                 nodes_to_trace = [
                     nid for nid, ndata in current_nodes.items()
                     if not ndata.get('isLocal', False) and ndata.get('lastHeard', 0) > active_cutoff
                 ]
                 logger.info(f"Found {len(nodes_to_trace)} nodes for proactive traceroute.")

                 for node_id in nodes_to_trace:
                     if not interface or not interface.isConnected:
                         logger.warning("Lost connection during proactive traceroute loop.")
                         break 

                     logger.debug(f"Initiating proactive traceroute to {node_id}")
                     try:
                         if hasattr(interface, 'sendTraceRoute'):
                              interface.sendTraceRoute(dest=node_id, hopLimit=7)
                              await asyncio.sleep(15) 
                         else:
                              logger.warning("Interface lacks sendTraceRoute, stopping proactive task.")
                              return 
                     except Exception as trace_err:
                          logger.error(f"Error sending proactive traceroute to {node_id}: {trace_err}")
                     await asyncio.sleep(2) 

             except Exception as e:
                 logger.error(f"Error during proactive traceroute node selection/loop: {e}", exc_info=True)

             logger.info(f"Proactive traceroute cycle finished. Sleeping for {PROACTIVE_TRACEROUTE_INTERVAL_SECONDS}s.")
         else:
              logger.debug("Skipping proactive traceroutes - not connected.")

         await asyncio.sleep(PROACTIVE_TRACEROUTE_INTERVAL_SECONDS)

interface: Optional[meshtastic.tcp_interface.TCPInterface] = None
connection_task: Optional[asyncio.Task] = None

async def connect_to_meshtastic():
    """ Background task to establish and manage the connection. """
    global interface
    retry_delay = 5 

    TOPIC_RECEIVED = "meshtastic.receive"
    TOPIC_CONNECTION_ESTABLISHED = "meshtastic.connection.established"
    TOPIC_CONNECTION_LOST = "meshtastic.connection.lost"
    TOPIC_CONNECTION_FAILED = "meshtastic.connection.failed"
    TOPIC_NODE_UPDATED = "meshtastic.node.updated" 

    while True:
        if interface is None or not interface.isConnected:
            network_analyzer.set_connection_status("Connecting")
            logger.info(f"Attempting connection to Meshtastic at tcp://{TARGET_HOST}:{TARGET_PORT}...")
            try:

                if interface:
                    try: interface.close()
                    except Exception as close_e: logger.warning(f"Error closing old interface: {close_e}")
                    finally: interface = None

                interface = meshtastic.tcp_interface.TCPInterface(
                    hostname=TARGET_HOST,
                    portNumber=TARGET_PORT,
                    connectNow=True, 
                    noProto=False    
                )

                await asyncio.sleep(4) 

                if interface.isConnected and getattr(interface, 'myInfo', None):
                    logger.info("Successfully connected to Meshtastic device and received initial info.")
                    network_analyzer.set_error(None) 
                    retry_delay = 5 

                    logger.info("Subscribing to Meshtastic pubsub events...")
                    pub.subscribe(on_receive, TOPIC_RECEIVED)
                    pub.subscribe(on_connection, TOPIC_CONNECTION_ESTABLISHED)
                    pub.subscribe(on_connection, TOPIC_CONNECTION_LOST)
                    pub.subscribe(on_connection, TOPIC_CONNECTION_FAILED)
                    pub.subscribe(on_node_updated, TOPIC_NODE_UPDATED)
                    logger.info("Subscribed to Meshtastic pubsub events.")

                    on_connection(interface, TOPIC_CONNECTION_ESTABLISHED)

                else:
                    logger.warning("Connection attempt finished, but not fully established or myInfo missing.")
                    network_analyzer.set_connection_status("Error")
                    network_analyzer.set_error("Failed to establish full Meshtastic protocol connection.")
                    if interface:
                        try: interface.close()
                        except: pass
                        interface = None

            except meshtastic.MeshtasticException as e:
                logger.error(f"Meshtastic library connection error: {e}")
                network_analyzer.set_connection_status("Error")
                network_analyzer.set_error(f"Meshtastic Error: {e}")
                if interface: interface = None
            except ConnectionRefusedError:
                err_msg = f"Connection refused by {TARGET_HOST}:{TARGET_PORT}."
                logger.error(err_msg)
                network_analyzer.set_connection_status("Error")
                network_analyzer.set_error(err_msg)
                if interface: interface = None
            except OSError as e:
                logger.error(f"Network OS Error connecting to {TARGET_HOST}:{TARGET_PORT}: {e}")
                network_analyzer.set_connection_status("Error")
                network_analyzer.set_error(f"Network Error: {e}")
                if interface: interface = None
            except Exception as e:
                logger.exception(f"Unexpected error during connection attempt: {e}")
                network_analyzer.set_connection_status("Error")
                network_analyzer.set_error(f"Unexpected connect error: {e}")
                if interface:
                    try: interface.close()
                    except: pass
                    interface = None

            if network_analyzer.connection_status in ["Error", "Disconnected", "Connecting"]: 
                logger.info(f"Waiting {retry_delay} seconds before retrying connection...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 120) 

        else:

            await asyncio.sleep(15)

@app.on_event("startup")
async def startup_event():
    """ Start background tasks on app startup. """
    global main_event_loop, connection_task
    logger.info(f"Starting Meshtastic Network Analyzer Server (v{app.version})...")
    main_event_loop = asyncio.get_running_loop()

    connection_task = asyncio.create_task(connect_to_meshtastic(), name="meshtastic_connector")
    asyncio.create_task(check_connection_periodically(), name="connection_checker")
    asyncio.create_task(periodic_network_analysis(), name="network_analyzer_periodic")
    asyncio.create_task(prune_history_periodically(), name="history_pruner")
    asyncio.create_task(proactive_traceroute_task(), name="proactive_tracerouter") 

    logger.info(f"Network Analyzer Server listening on http://{WEBSERVER_HOST}:{WEBSERVER_PORT}")
    logger.info(f"API documentation at http://{WEBSERVER_HOST}:{WEBSERVER_PORT}/docs")
    logger.info(f"Attempting to connect to Meshtastic node at {TARGET_HOST}:{TARGET_PORT}")
    logger.info(f"Using database file: {DB_PATH}")
    logger.info(f"Average metrics history retention: {AVERAGE_METRICS_HISTORY_DAYS} days")

@app.on_event("shutdown")
async def shutdown_event():
    """ Clean up on app shutdown. """
    global interface, connection_task
    logger.info("Shutting down Meshtastic Network Analyzer Server...")

    if connection_task and not connection_task.done():
        connection_task.cancel()
        try: await connection_task
        except asyncio.CancelledError: logger.info("Meshtastic connector task cancelled.")
        except Exception as e: logger.error(f"Error during connection task cancellation: {e}", exc_info=True)

    if interface:
        logger.info("Closing Meshtastic interface...")
        try: interface.close()
        except Exception as e: logger.error(f"Error closing Meshtastic interface: {e}", exc_info=True)
        interface = None

    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if tasks:
        logger.info(f"Cancelling {len(tasks)} background tasks...")
        [task.cancel() for task in tasks]
        await asyncio.gather(*tasks, return_exceptions=True) 

    logger.info("Shutdown complete.")

def parse_arguments():
    """ Parse command line arguments to override config. """
    global TARGET_HOST, TARGET_PORT, LOG_LEVEL, WEBSERVER_PORT, WEBSERVER_HOST, DB_PATH, LOG_LEVEL_STR, AVERAGE_METRICS_HISTORY_DAYS, ENABLE_PROACTIVE_TRACEROUTE, PROACTIVE_TRACEROUTE_INTERVAL_SECONDS
    import argparse
    parser = argparse.ArgumentParser(description="Meshtastic Network Analyzer Server")
    parser.add_argument("--host", help=f"Meshtastic device hostname/IP (default: {DEFAULT_TARGET_HOST})")
    parser.add_argument("--port", type=int, help=f"Meshtastic device port (default: {DEFAULT_TARGET_PORT})")
    parser.add_argument("--web-host", help=f"Web server host (default: {DEFAULT_WEBSERVER_HOST})")
    parser.add_argument("--web-port", type=int, help=f"Web server port (default: {DEFAULT_WEBSERVER_PORT})")
    parser.add_argument("--db-path", help=f"Path to SQLite database file (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help=f"Logging level (default: {logging.getLevelName(DEFAULT_LOG_LEVEL)})")
    parser.add_argument("--history-days", type=int,
                        help=f"Days of average metrics history to retain (default: {AVERAGE_METRICS_HISTORY_DAYS})")
    parser.add_argument("--enable-proactive-trace", action='store_true',
                        help=f"Enable periodic tracerouting to known nodes (default: {ENABLE_PROACTIVE_TRACEROUTE})")
    parser.add_argument("--proactive-trace-interval", type=int,
                         help=f"Interval in seconds for proactive traceroutes (default: {PROACTIVE_TRACEROUTE_INTERVAL_SECONDS})")

    args = parser.parse_args()

    if args.host: TARGET_HOST = args.host
    if args.port: TARGET_PORT = args.port
    if args.web_host: WEBSERVER_HOST = args.web_host
    if args.web_port: WEBSERVER_PORT = args.web_port
    if args.db_path: DB_PATH = args.db_path
    if args.history_days is not None: AVERAGE_METRICS_HISTORY_DAYS = args.history_days
    if args.enable_proactive_trace: ENABLE_PROACTIVE_TRACEROUTE = True
    if args.proactive_trace_interval is not None: PROACTIVE_TRACEROUTE_INTERVAL_SECONDS = args.proactive_trace_interval

    if args.log_level:
        LOG_LEVEL_STR = args.log_level
        LOG_LEVEL = getattr(logging, args.log_level)

        logging.getLogger().setLevel(LOG_LEVEL) 
        logger.setLevel(LOG_LEVEL) 
        if LOG_LEVEL > logging.DEBUG:
            logging.getLogger("meshtastic").setLevel(logging.WARNING)
            logging.getLogger("pubsub").setLevel(logging.WARNING)
        else:
            logging.getLogger("meshtastic").setLevel(logging.DEBUG)
            logging.getLogger("pubsub").setLevel(logging.INFO) 

if __name__ == "__main__":
    parse_arguments()

    uvicorn.run(
        "__main__:app", 
        host=WEBSERVER_HOST,
        port=WEBSERVER_PORT,
        reload=False, 
        log_level=LOG_LEVEL_STR.lower() 
    )