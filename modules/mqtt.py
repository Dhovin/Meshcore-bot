import logging
import asyncio
import json
import hashlib
import time
import re
import os
import base64
from datetime import datetime, timezone
from pathlib import Path
from enum import Enum, Flag
from meshcore import EventType

logger = logging.getLogger("MQTTModule")

# Import paho-mqtt
try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    logger.warning("paho-mqtt not installed. MQTT publishing will be unavailable. Install it with 'pip install paho-mqtt'.")

# Import PyNaCl for Ed25519 signing
try:
    import nacl.bindings
    import nacl.signing
    import nacl.exceptions
    PYNACL_AVAILABLE = True
except ImportError:
    PYNACL_AVAILABLE = False
    logger.warning("PyNaCl not installed. Local Ed25519 token signing will be unavailable. Install it with 'pip install pynacl'.")


# ==============================================================================
# Protocol Enums & Helpers (from meshcore-packet-capture)
# ==============================================================================

class AdvertFlags(Flag):
    """Advertisement flags for MeshCore packets"""
    ADV_TYPE_NONE = 0x00
    ADV_TYPE_CHAT = 0x01
    ADV_TYPE_REPEATER = 0x02
    ADV_TYPE_ROOM = 0x03
    ADV_TYPE_SENSOR = 0x04
    
    ADV_LATLON_MASK = 0x10    # Has location data
    ADV_FEAT1_MASK = 0x20     # Future feature 1
    ADV_FEAT2_MASK = 0x40     # Future feature 2
    ADV_NAME_MASK = 0x80      # Has name data
    
    IsCompanion = ADV_TYPE_CHAT
    IsRepeater = ADV_TYPE_REPEATER
    IsRoomServer = ADV_TYPE_ROOM
    HasLocation = ADV_LATLON_MASK
    HasName = ADV_NAME_MASK


class PayloadType(Enum):
    """Payload types for MeshCore packets"""
    REQ = 0x00
    RESPONSE = 0x01
    TXT_MSG = 0x02
    ACK = 0x03
    ADVERT = 0x04
    GRP_TXT = 0x05
    GRP_DATA = 0x06
    ANON_REQ = 0x07
    PATH = 0x08
    TRACE = 0x09
    MULTIPART = 0x0A
    CONTROL = 0x0B
    Type12 = 0x0C
    Type13 = 0x0D
    Type14 = 0x0E
    RAW_CUSTOM = 0x0F


class PayloadVersion(Enum):
    VER_1 = 0x00
    VER_2 = 0x01
    VER_3 = 0x02
    VER_4 = 0x03


class RouteType(Enum):
    TRANSPORT_FLOOD = 0x00
    FLOOD = 0x01
    DIRECT = 0x02
    TRANSPORT_DIRECT = 0x03


class DeviceRole(Enum):
    Companion = "Companion"
    Repeater = "Repeater"
    RoomServer = "RoomServer"


# Group order constant for Ed25519
L_ORDER = 2**252 + 27742317777372353535851937790883648493


def int_to_bytes_le(value: int, length: int) -> bytes:
    return value.to_bytes(length, byteorder='little')


def bytes_to_int_le(data: bytes) -> int:
    return int.from_bytes(data, byteorder='little')


def ed25519_sign_with_expanded_key(message: bytes, scalar: bytes, prefix: bytes, public_key: bytes) -> bytes:
    """RFC 8032 Ed25519 signing using pre-expanded private key (orlp format: scalar || prefix)"""
    if not PYNACL_AVAILABLE:
        raise ImportError("PyNaCl is required for native Ed25519 signing.")
    
    # 1. Nonce r = H(prefix || message) mod L
    h_r = hashlib.sha512(prefix + message).digest()
    r = bytes_to_int_le(h_r) % L_ORDER
    r_bytes = int_to_bytes_le(r, 32)
    
    # 2. R = r * B
    R = nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(r_bytes)
    
    # 3. Challenge k = H(R || public_key || message) mod L
    h_k = hashlib.sha512(R + public_key + message).digest()
    k = bytes_to_int_le(h_k) % L_ORDER
    
    # 4. s = (r + k * scalar) mod L
    scalar_int = bytes_to_int_le(scalar)
    s = (r + k * scalar_int) % L_ORDER
    s_bytes = int_to_bytes_le(s, 32)
    
    return R + s_bytes


class AuthTokenPayload:
    def __init__(self, public_key: str, iat: int = None, exp: int = None, aud: str = None, **kwargs):
        self.public_key = public_key.upper()
        self.iat = iat if iat is not None else int(time.time())
        self.exp = exp
        self.aud = aud
        self.custom_claims = kwargs

    def to_dict(self):
        payload = {
            'publicKey': self.public_key,
            'iat': self.iat
        }
        if self.exp is not None:
            payload['exp'] = self.exp
        if self.aud is not None:
            payload['aud'] = self.aud
        payload.update(self.custom_claims)
        return payload


def base64url_encode(data: bytes) -> str:
    b64 = base64.b64encode(data).decode('ascii')
    return b64.replace('+', '-').replace('/', '_').replace('=', '')


def create_auth_token_internal(payload: AuthTokenPayload, private_key_hex: str, public_key_hex: str) -> str:
    header = {
        'alg': 'Ed25519',
        'typ': 'JWT'
    }
    payload.public_key = public_key_hex.upper()
    header_json = json.dumps(header, separators=(',', ':'))
    payload_json = json.dumps(payload.to_dict(), separators=(',', ':'))
    header_encoded = base64url_encode(header_json.encode('utf-8'))
    payload_encoded = base64url_encode(payload_json.encode('utf-8'))
    signing_input = f"{header_encoded}.{payload_encoded}"
    signing_input_bytes = signing_input.encode('utf-8')
    
    private_bytes = bytes.fromhex(private_key_hex)
    public_bytes = bytes.fromhex(public_key_hex)
    
    if len(private_bytes) != 64:
        raise ValueError(f"Private key must be 64 bytes, got {len(private_bytes)}")
    if len(public_bytes) != 32:
        raise ValueError(f"Public key must be 32 bytes, got {len(public_bytes)}")
        
    scalar = private_bytes[:32]
    prefix = private_bytes[32:64]
    
    signature_bytes = ed25519_sign_with_expanded_key(signing_input_bytes, scalar, prefix, public_bytes)
    signature_hex = signature_bytes.hex()
    
    return f"{header_encoded}.{payload_encoded}.{signature_hex}"


# ==============================================================================
# MeshCore-Bot Packet Capture Module
# ==============================================================================

class Mqtt:
    def __init__(self):
        self.name = "mqtt"
        self.api = None
        self.config = {}
        
        # Schema for validation of configuration in config.json under modules.mqtt
        self.config_schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "output_file": {"type": "string"},
                "verbose": {"type": "boolean"},
                "debug": {"type": "boolean"},
                "iata": {"type": "string"},
                "brokers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "server": {"type": "string"},
                            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                            "topic_status": {"type": "string"},
                            "topic_packets": {"type": "string"},
                            "topic_raw": {"type": "string"},
                            "use_tls": {"type": "boolean"},
                            "use_ws": {"type": "boolean"},
                            "token_private_key": {"type": "string"},
                            "token_audience": {"type": "string"},
                            "iata": {"type": "string"},
                            "qos": {"type": "integer", "minimum": 0, "maximum": 2},
                            "retain": {"type": "boolean"}
                        },
                        "required": ["server", "port"]
                    }
                }
            },
            "required": ["enabled"]
        }
        
        self.mqtt_clients = []
        self.mqtt_connected = {}
        self.jwt_tokens = {}
        
        self.rf_data_cache = {}
        self.recent_rf_packets = {}
        self.packet_count = 0
        
        # Device details
        self.device_name = None
        self.device_public_key = None
        self.device_private_key = None
        
        # Subscriptions & tasks
        self.unsubscribe_rx_log = None
        self.unsubscribe_raw = None
        self.unsubscribe_connect = None
        self.unsubscribe_advert = None
        self.unsubscribe_path_update = None
        self.unsubscribe_new_contact = None
        self.unschedule_status = None
        self.unschedule_jwt = None
        
        self.output_handle = None

    def run_config(self, current_config):
        """Interactive config wizard block for mqtt."""
        config = dict(current_config) if current_config else {}
        print("\n--- Configure Packet Capture Settings ---")
        
        # Enabled
        enabled_val = config.get("enabled", True)
        val = input(f"Enable Packet Capture Module? (y/n) [current: {'y' if enabled_val else 'n'}]: ").strip().lower()
        if val:
            config["enabled"] = val in ("y", "yes", "true")
            
        # Global IATA code
        current_iata = config.get("iata", "LOC")
        val = input(f"Global IATA Regional Code (e.g. ORD, JFK) [current: {current_iata}]: ").strip().upper()
        if val:
            config["iata"] = val
            
        # Local output file
        current_output = config.get("output_file", "")
        val = input(f"Local file path to write captured packets JSON (empty to disable) [current: {current_output}]: ").strip()
        if val is not None:
            config["output_file"] = val

        # 1. Preset community brokers check/menu
        current_brokers = config.get("brokers", [])
        
        presets = [
            # Mapping & Global Platforms
            {"name": "Let's Mesh US Server", "server": "mqtt-us-v1.letsmesh.net", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt-us-v1.letsmesh.net"},
            {"name": "Let's Mesh EU Server", "server": "mqtt-eu-v1.letsmesh.net", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt-eu-v1.letsmesh.net"},
            {"name": "MeshMapper", "server": "mqtt.meshmapper.net", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt.meshmapper.net"},
            {"name": "Waev", "server": "mqtt.waev.app", "port": 8883, "use_tls": True},
            {"name": "Meshomatic", "server": "mqtt.meshomatic.net", "port": 1883, "use_tls": False},
            
            # US Northeast & Midwest Communities
            {"name": "Greater Boston Mesh", "server": "mqttmc01.bostonme.sh", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqttmc01.bostonme.sh"},
            {"name": "Chicago Mesh", "server": "mqtt.chicagolandmesh.org", "port": 1883, "use_tls": False},
            {"name": "NYC Mesh", "server": "mqtt.nycmesh.net", "port": 1883, "use_tls": False},
            {"name": "Minnesota Mesh", "server": "mqtt.minnesotamesh.org", "port": 1883, "use_tls": False},
            {"name": "NodakMesh (North Dakota)", "server": "mqtt.nodakmesh.org", "port": 1883, "use_tls": False},
            {"name": "Wisconsin Mesh", "server": "mqtt.wisconsinmesh.org", "port": 1883, "use_tls": False},
            
            # US South & West Communities
            {"name": "North Texas Mesh (NTX)", "server": "ntxmesh.dhovin.me", "port": 1883, "use_tls": True},
            {"name": "Austin Mesh", "server": "mqtt.austinmesh.org", "port": 1883, "use_tls": False},
            {"name": "Denver Mesh", "server": "mqtt.denvermesh.org", "port": 1883, "use_tls": False},
            {"name": "Seattle Mesh (Emerald City)", "server": "mqtt.emeraldcitymesh.org", "port": 1883, "use_tls": False},
            {"name": "San Francisco Bay Area Mesh", "server": "mqtt.bayareamesh.org", "port": 1883, "use_tls": False},
            {"name": "Portland Mesh (PDX)", "server": "mqtt.pdxmesh.org", "port": 1883, "use_tls": False},
            {"name": "Southern California (SoCal Mesh)", "server": "mqtt.socalmesh.org", "port": 1883, "use_tls": False},
            {"name": "Atlanta Mesh", "server": "mqtt.atlantamesh.org", "port": 1883, "use_tls": False},
            {"name": "Utah Mesh", "server": "mqtt.utahmesh.org", "port": 1883, "use_tls": False},
            {"name": "Florida Mesh", "server": "mqtt.floridamesh.org", "port": 1883, "use_tls": False},
            
            # International Communities
            {"name": "Toronto Mesh (Canada)", "server": "mqtt.torontomesh.org", "port": 1883, "use_tls": False},
            {"name": "UK Mesh Network", "server": "mqtt.meshtastic.uk", "port": 1883, "use_tls": False},
            {"name": "Germany Mesh (Mesh-DE)", "server": "mqtt.mesh-de.net", "port": 1883, "use_tls": False}
        ]

        print("\n--- Preset community MQTT brokers available ---")
        categories = [
            ("Mapping & Global Platforms", [0, 1, 2, 3, 4]),
            ("US Northeast & Midwest Communities", [5, 6, 7, 8, 9, 10]),
            ("US South & West Communities", [11, 12, 13, 14, 15, 16, 17, 18, 19, 20]),
            ("International Communities", [21, 22, 23])
        ]

        # Display presets categorised
        for cat_name, idxs in categories:
            print(f"\n{cat_name}:")
            for idx in idxs:
                preset = presets[idx]
                server = preset["server"]
                is_active = any(b.get("server", "").lower() == server.lower() for b in current_brokers)
                status_str = " [Already Added]" if is_active else ""
                
                features = []
                if preset.get("use_tls"):
                    features.append("TLS")
                if preset.get("use_ws"):
                    features.append("WS")
                features_str = f" ({', '.join(features)})" if features else ""
                
                print(f"  [{idx + 1}] {preset['name']} ({server}:{preset['port']}{features_str}){status_str}")

        print("\n--------------------------------------------------------------------------------")
        val = input("Enter comma-separated numbers to add (e.g. 1,3,12), 'all' to add all, or 'none' to skip: ").strip().lower()
        
        selected_indices = []
        if val == "all":
            selected_indices = list(range(len(presets)))
        elif val and val not in ("none", "skip"):
            parts = val.split(",")
            for p in parts:
                p = p.strip()
                if p.isdigit():
                    idx = int(p) - 1
                    if 0 <= idx < len(presets):
                        selected_indices.append(idx)

        added_count = 0
        for idx in selected_indices:
            preset = presets[idx]
            server = preset["server"]
            is_active = any(b.get("server", "").lower() == server.lower() for b in current_brokers)
            if not is_active:
                broker_config = {
                    "server": preset["server"],
                    "port": preset["port"]
                }
                if preset.get("use_tls") is not None:
                    broker_config["use_tls"] = preset["use_tls"]
                if preset.get("use_ws") is not None:
                    broker_config["use_ws"] = preset["use_ws"]
                if preset.get("token_audience") is not None:
                    broker_config["token_audience"] = preset["token_audience"]
                
                current_brokers.append(broker_config)
                added_count += 1
                
        if added_count > 0:
            config["brokers"] = current_brokers
            print(f"Added {added_count} community broker preset(s).")

        # 2. Additional Custom Brokers configuration loop
        while True:
            if not current_brokers:
                add_broker = input("\nNo MQTT Brokers configured. Add a custom MQTT Broker config? (y/n) [n]: ").strip().lower()
            else:
                print(f"\nCurrently configured brokers: {len(current_brokers)}")
                add_broker = input("Add an additional custom MQTT Broker config? (y/n) [n]: ").strip().lower()

            if add_broker not in ("y", "yes", "true"):
                break
                
            broker = {}
            broker["server"] = input("Broker IP or Hostname: ").strip()
            if not broker["server"]:
                print("Server hostname cannot be empty.")
                continue
                
            port_val = input("Broker Port [1883]: ").strip()
            broker["port"] = int(port_val) if port_val else 1883
            
            username = input("Username (empty for none): ").strip()
            if username:
                broker["username"] = username
                
            password = input("Password (empty for none): ").strip()
            if password:
                broker["password"] = password
            
            jwt_val = input("Use JWT token auth (e.g. for custom LetsMesh)? (y/n) [n]: ").strip().lower()
            if jwt_val == "y":
                broker["token_audience"] = input("JWT Audience (e.g. letsmesh.net): ").strip()
                pk_val = input("JWT Private Key (hex string, empty to fetch from device): ").strip()
                if pk_val:
                    broker["token_private_key"] = pk_val
                    
            use_tls_val = input("Use TLS? (y/n) [n]: ").strip().lower()
            broker["use_tls"] = use_tls_val == "y"
            
            # Check if this server is already configured
            is_dup = any(b.get("server", "").lower() == broker["server"].lower() and b.get("port") == broker["port"] for b in current_brokers)
            if is_dup:
                print(f"Broker with server '{broker['server']}' and port {broker['port']} is already configured.")
                continue
                
            current_brokers.append(broker)
            config["brokers"] = current_brokers
            print(f"Added custom broker: {broker['server']}:{broker['port']}")
                
        return config

    def init(self, api, config):
        """Lifecycle hook: save API and config, set local properties, open file handle."""
        self.api = api
        self.config = config
        self.verbose = config.get("verbose", False)
        self.debug = config.get("debug", False)
        
        output_file = config.get("output_file", "")
        if output_file:
            try:
                # Ensure parent directory exists
                os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
                self.output_handle = open(output_file, 'a', encoding='utf-8')
                logger.info(f"[{self.name}] Output file opened: {output_file}")
            except Exception as e:
                logger.error(f"[{self.name}] Failed to open output file '{output_file}': {e}")

    async def start(self):
        """Lifecycle hook: subscribe to event bus and establish MQTT loops."""
        logger.info(f"[{self.name}] Starting packet capture module...")
        
        # Subscribe to Event Bus events
        self.unsubscribe_rx_log = self.api.subscribe("rx_log_data", self._on_rx_log_data)
        self.unsubscribe_raw = self.api.subscribe("raw_data", self._on_raw_data)
        self.unsubscribe_connect = self.api.subscribe("connect", self._on_connect)
        self.unsubscribe_advert = self.api.subscribe("advert", self._on_advert)
        self.unsubscribe_path_update = self.api.subscribe("path_update", self._on_path_update)
        self.unsubscribe_new_contact = self.api.subscribe("new_contact", self._on_new_contact)
        
        # Query device info if already connected
        await self._sync_device_info()
        
        # Initialize MQTT brokers
        if MQTT_AVAILABLE:
            await self._connect_mqtt()
            
        # Schedule status checks & JWT renewals using native scheduler
        # Check JWT renewal every 10 minutes
        self.unschedule_jwt = self.api.schedule_task("*/10 * * * *", self._periodic_jwt_check)
        # Refresh status and stats every 5 minutes
        self.unschedule_status = self.api.schedule_task("*/5 * * * *", self._periodic_status)
        
        logger.info(f"[{self.name}] Started and event handlers registered.")

    def stop(self):
        """Lifecycle hook: shut down MQTT loops, unsubscribe, and close file."""
        logger.info(f"[{self.name}] Stopping packet capture module...")
        
        # Unsubscribe event bus
        if self.unsubscribe_rx_log: self.unsubscribe_rx_log()
        if self.unsubscribe_raw: self.unsubscribe_raw()
        if self.unsubscribe_connect: self.unsubscribe_connect()
        if self.unsubscribe_advert: self.unsubscribe_advert()
        if self.unsubscribe_path_update: self.unsubscribe_path_update()
        if self.unsubscribe_new_contact: self.unsubscribe_new_contact()
        
        # Unschedule cron tasks
        if self.unschedule_jwt: self.unschedule_jwt()
        if self.unschedule_status: self.unschedule_status()
        
        # Disconnect MQTT brokers
        for client_info in self.mqtt_clients:
            broker_num = client_info["broker_num"]
            client = client_info.get("client")
            if client:
                try:
                    logger.info(f"[{self.name}] Disconnecting MQTT broker {broker_num}...")
                    # Publish offline status before exit
                    status_topic = self.get_topic("status", broker_num)
                    if status_topic and self.mqtt_connected.get(broker_num, False):
                        payload = json.dumps({
                            "status": "offline",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "origin": self.device_name or "MeshCore Device",
                            "origin_id": self.device_public_key.upper() if self.device_public_key else 'DEVICE'
                        })
                        client.publish(status_topic, payload, qos=0, retain=True)
                    client.disconnect()
                    client.loop_stop()
                except Exception as e:
                    logger.warning(f"[{self.name}] Error disconnecting broker {broker_num}: {e}")
                    
        self.mqtt_clients.clear()
        
        # Close output file
        if self.output_handle:
            try:
                self.output_handle.close()
            except Exception:
                pass
            self.output_handle = None
            
        logger.info(f"[{self.name}] Stopped successfully.")

    # ==============================================================================
    # Event Handlers
    # ==============================================================================

    def _on_connect(self, data):
        """Fires when ConnectionManager runs handshake and gets device info."""
        logger.info(f"[{self.name}] Node connection event detected.")
        asyncio.create_task(self._sync_device_info_and_reconnect_mqtt())

    def _on_rx_log_data(self, payload):
        """Fires when EventType.RX_LOG_DATA is published."""
        if self.debug:
            logger.debug(f"[{self.name}] Received rx_log_data event: {payload}")
            
        try:
            snr = payload.get('snr')
            if snr is not None:
                # Try to get raw hex packet from payload or raw_hex
                raw_hex = payload.get('payload') or payload.get('raw_hex')
                # If raw_hex has prefix, strip it (first 2 bytes = 4 hex chars)
                if raw_hex and 'raw_hex' in payload and raw_hex.startswith(payload.get('raw_hex')[:4]):
                    raw_hex = raw_hex[4:]
                    
                if raw_hex:
                    packet_prefix = raw_hex[:32]
                    rf_data = {
                        'snr': snr,
                        'rssi': payload.get('rssi'),
                        'timestamp': time.time(),
                        'raw_hex': raw_hex,
                        'payload_length': payload.get('payload_length')
                    }
                    self.rf_data_cache[packet_prefix] = rf_data
                    
                    # Log recent RF packet to prevent double capture
                    self.recent_rf_packets[raw_hex.upper()] = time.time()
                    
                    # Clean up cache
                    current_time = time.time()
                    self.rf_data_cache = {
                        k: v for k, v in self.rf_data_cache.items()
                        if current_time - v['timestamp'] < 15.0
                    }
                    self.recent_rf_packets = {
                        k: v for k, v in self.recent_rf_packets.items()
                        if current_time - v < 2.0
                    }
                    
                    # Process and format the packet immediately
                    asyncio.create_task(self._process_packet_from_rf(raw_hex, rf_data))
        except Exception as e:
            logger.error(f"[{self.name}] Error handling rx_log_data: {e}", exc_info=True)

    def _on_raw_data(self, payload):
        """Fires when EventType.RAW_DATA is published."""
        if self.debug:
            logger.debug(f"[{self.name}] Received raw_data event.")
            
        try:
            raw_hex = None
            if hasattr(payload, 'data'):
                raw_hex = payload.data
            elif isinstance(payload, dict):
                raw_hex = payload.get('data') or payload.get('raw_hex')
                
            if raw_hex:
                if raw_hex.startswith('0x'):
                    raw_hex = raw_hex[2:]
                raw_hex = raw_hex.upper()
                
                current_time = time.time()
                # Deduplicate if already processed via rx_log_data
                recent_rf_time = self.recent_rf_packets.get(raw_hex)
                if recent_rf_time is not None and (current_time - recent_rf_time) < 2.0:
                    return
                    
                self.recent_rf_packets[raw_hex] = current_time
                self.recent_rf_packets = {
                    k: v for k, v in self.recent_rf_packets.items()
                    if current_time - v < 2.0
                }
                
                # Retrieve RF stats if cached
                packet_prefix = raw_hex[:32]
                rf_data = self.rf_data_cache.get(packet_prefix)
                
                asyncio.create_task(self._process_packet_from_rf(raw_hex, rf_data))
        except Exception as e:
            logger.error(f"[{self.name}] Error handling raw_data: {e}", exc_info=True)

    def _on_advert(self, payload):
        """Fires when node advertisement is detected."""
        if self.debug:
            logger.debug(f"[{self.name}] Advertisement detected: {payload}")

    def _on_path_update(self, payload):
        """Fires when path update is detected."""
        if self.debug:
            logger.debug(f"[{self.name}] Path update: {payload}")

    def _on_new_contact(self, payload):
        """Fires when a new contact is discovered."""
        if self.debug:
            logger.debug(f"[{self.name}] New contact: {payload}")

    # ==============================================================================
    # Packet Processing & Decoders
    # ==============================================================================

    async def _process_packet_from_rf(self, raw_hex: str, rf_data: dict):
        try:
            packet_data = self._format_packet_data(raw_hex, rf_data)
            if not packet_data:
                return
                
            self.packet_count += 1
            logger.info(f"📦 [{self.name}] Capture #{self.packet_count}: {packet_data['route']} type {packet_data['packet_type']}, {packet_data['len']} bytes, SNR: {packet_data['SNR']}, RSSI: {packet_data['RSSI']}, hash: {packet_data['hash']}")
            
            # Local console verbose logging
            if self.verbose:
                logger.info(json.dumps(packet_data, indent=2))
                
            # Log to local output file
            if self.output_handle:
                try:
                    self.output_handle.write(json.dumps(packet_data) + "\n")
                    self.output_handle.flush()
                except Exception as e:
                    logger.error(f"[{self.name}] Error writing to output file: {e}")
                    
            # Publish to MQTT brokers
            self._publish_packet_to_mqtt(packet_data)
        except Exception as e:
            logger.error(f"[{self.name}] Error processing packet: {e}", exc_info=True)

    def _format_packet_data(self, raw_hex: str, rf_data: dict = None) -> dict:
        byte_data = bytes.fromhex(raw_hex)
        packet_len = len(byte_data)
        
        # Decode using Packet.cpp rules
        decoded = self._decode_packet(raw_hex)
        
        route = "U"
        packet_type = "0"
        payload_len = "0"
        
        if decoded:
            route_map = {
                "TRANSPORT_FLOOD": "F",
                "FLOOD": "F",
                "DIRECT": "D",
                "TRANSPORT_DIRECT": "T"
            }
            route = route_map.get(decoded.get('route_type'), "U")
            
            payload_type_map = {
                "REQ": "0", "RESPONSE": "1", "TXT_MSG": "2", "ACK": "3",
                "ADVERT": "4", "GRP_TXT": "5", "GRP_DATA": "6", "ANON_REQ": "7",
                "PATH": "8", "TRACE": "9", "MULTIPART": "10", "CONTROL": "11",
                "Type12": "12", "Type13": "13", "Type14": "14", "RAW_CUSTOM": "15"
            }
            packet_type = payload_type_map.get(decoded.get('payload_type'), "0")
            
            if rf_data and rf_data.get('payload_length') is not None:
                payload_len = str(rf_data.get('payload_length'))
            else:
                path_len_bytes = decoded.get('path_byte_len', 0)
                has_transport = decoded.get('route_type') in ['TRANSPORT_FLOOD', 'TRANSPORT_DIRECT']
                transport_bytes = 4 if has_transport else 0
                payload_len = str(max(0, packet_len - 1 - transport_bytes - 1 - path_len_bytes))

        # Origin details
        origin_id = self.device_public_key or os.getenv('PACKETCAPTURE_ORIGIN_ID')
        if not origin_id:
            origin_id = hashlib.sha256((self.device_name or 'Unknown').encode()).hexdigest()
            
        current_time = datetime.now(timezone.utc)
        
        packet_data = {
            "origin": self.device_name or "MeshCore Device",
            "origin_id": origin_id.upper() if origin_id else 'DEVICE',
            "timestamp": current_time.isoformat(),
            "type": "PACKET",
            "direction": "rx",
            "time": current_time.strftime("%H:%M:%S"),
            "date": current_time.strftime("%d/%m/%Y"),
            "len": str(packet_len),
            "packet_type": packet_type,
            "route": route,
            "payload_len": payload_len,
            "raw": raw_hex.upper(),
            "SNR": str(rf_data.get('snr', 'Unknown')) if rf_data else "Unknown",
            "RSSI": str(rf_data.get('rssi', 'Unknown')) if rf_data else "Unknown",
            "hash": self._calculate_packet_hash(raw_hex, decoded.get('payload_type_value') if decoded else None)
        }
        
        if route == "D" and decoded and 'path' in decoded:
            packet_data["path"] = ",".join(decoded['path'])
            
        return packet_data

    def _decode_packet(self, raw_hex: str) -> dict:
        byte_data = bytes.fromhex(raw_hex)
        if len(byte_data) < 2:
            return None
            
        try:
            header = byte_data[0]
            route_type = RouteType(header & 0x03)
            has_transport = route_type in [RouteType.TRANSPORT_FLOOD, RouteType.TRANSPORT_DIRECT]
            
            offset = 5 if has_transport else 1
            if len(byte_data) <= offset:
                return None
                
            path_len_byte = byte_data[offset]
            offset += 1
            
            path_byte_len, path_hash_bytes = self._decode_packed_path_length(path_len_byte)
            if len(byte_data) < offset + path_byte_len:
                return None
                
            path_bytes = byte_data[offset:offset + path_byte_len]
            offset += path_byte_len
            
            payload = byte_data[offset:]
            payload_version = PayloadVersion((header >> 6) & 0x03)
            if payload_version != PayloadVersion.VER_1:
                return None
                
            payload_type = PayloadType((header >> 2) & 0x0F)
            path_values = self._split_path_hops(path_bytes, path_hash_bytes)
            
            message = {
                "payload_type": payload_type.name,
                "payload_type_value": payload_type.value,
                "payload_version": payload_version.name,
                "route_type": route_type.name,
                "path": path_values,
                "path_len_byte": path_len_byte,
                "path_byte_len": path_byte_len,
                "path_hash_bytes": path_hash_bytes,
            }
            
            if payload_type is PayloadType.ADVERT:
                advert_data = self._parse_advert(payload)
                if advert_data.get("advert_parse_ok"):
                    message.update(advert_data)
                    
            return message
        except Exception:
            return None

    def _decode_packed_path_length(self, path_len_byte: int) -> tuple:
        hop_count = path_len_byte & 0x3F
        bytes_per_hop = (path_len_byte >> 6) + 1
        if bytes_per_hop == 4:
            return path_len_byte, 1
        return hop_count * bytes_per_hop, bytes_per_hop

    def _split_path_hops(self, path_bytes: bytes, bytes_per_hop: int) -> list:
        path_hex = path_bytes.hex()
        hop_chars = max(bytes_per_hop, 1) * 2
        nodes = [path_hex[i:i + hop_chars] for i in range(0, len(path_hex), hop_chars)]
        return nodes

    def _calculate_packet_hash(self, raw_hex: str, payload_type_val: int = None) -> str:
        try:
            byte_data = bytes.fromhex(raw_hex)
            header = byte_data[0]
            if payload_type_val is None:
                payload_type_val = (header >> 2) & 0x0F
                
            route_type = header & 0x03
            has_transport = route_type in [0x00, 0x03]
            offset = 5 if has_transport else 1
            
            if len(byte_data) <= offset:
                return "0000000000000000"
                
            path_len_byte = byte_data[offset]
            offset += 1
            
            path_byte_len, _ = self._decode_packed_path_length(path_len_byte)
            payload_start = offset + path_byte_len
            if payload_start > len(byte_data):
                return "0000000000000000"
                
            payload_data = byte_data[payload_start:]
            
            hash_obj = hashlib.sha256()
            hash_obj.update(bytes([payload_type_val]))
            if payload_type_val == 9:  # TRACE
                hash_obj.update(path_len_byte.to_bytes(2, byteorder='little'))
            hash_obj.update(payload_data)
            
            return hash_obj.hexdigest()[:16].upper()
        except Exception:
            return "0000000000000000"

    def _parse_advert(self, payload: bytes) -> dict:
        if len(payload) < 100:
            return {"advert_parse_ok": False}
            
        try:
            pub_key = payload[0:32]
            timestamp = int.from_bytes(payload[32:36], "little")
            signature = payload[36:100]
            
            advert = {
                "advert_parse_ok": True,
                "public_key": pub_key.hex(),
                "advert_time": timestamp,
                "signature": signature.hex(),
            }
            
            app_data = payload[100:]
            if len(app_data) == 0:
                return advert
                
            flags_byte = app_data[0]
            flags = AdvertFlags(flags_byte)
            
            adv_type = flags_byte & 0x0F
            role_map = {
                AdvertFlags.ADV_TYPE_CHAT.value: DeviceRole.Companion.name,
                AdvertFlags.ADV_TYPE_REPEATER.value: DeviceRole.Repeater.name,
                AdvertFlags.ADV_TYPE_ROOM.value: DeviceRole.RoomServer.name,
                AdvertFlags.ADV_TYPE_SENSOR.value: "Sensor"
            }
            advert["mode"] = role_map.get(adv_type, f"Type{adv_type}")
            
            i = 1
            if AdvertFlags.ADV_LATLON_MASK in flags:
                if len(app_data) >= i + 8:
                    lat = int.from_bytes(app_data[i:i+4], 'little', signed=True)
                    lon = int.from_bytes(app_data[i+4:i+8], 'little', signed=True)
                    advert["lat"] = round(lat / 1000000.0, 6)
                    advert["lon"] = round(lon / 1000000.0, 6)
                    i += 8
                    
            if AdvertFlags.ADV_FEAT1_MASK in flags:
                if len(app_data) >= i + 2:
                    advert["feat1"] = int.from_bytes(app_data[i:i+2], 'little')
                    i += 2
                    
            if AdvertFlags.ADV_FEAT2_MASK in flags:
                if len(app_data) >= i + 2:
                    advert["feat2"] = int.from_bytes(app_data[i:i+2], 'little')
                    i += 2
                    
            if AdvertFlags.ADV_NAME_MASK in flags:
                if len(app_data) > i:
                    try:
                        advert["name"] = app_data[i:].decode('utf-8', errors='ignore').rstrip('\x00')
                    except Exception:
                        pass
                        
            return advert
        except Exception as e:
            return {"advert_parse_ok": False, "advert_error": str(e)}

    # ==============================================================================
    # Device State Synchronizer
    # ==============================================================================

    async def _sync_device_info(self):
        """Query ConnectionManager and state cache for details about the connected radio."""
        state = self.api.get_state()
        self.device_name = state.get("deviceName")
        self.device_public_key = state.get("publicKey")
        
        # If not connected yet or cache is empty, try direct fetch if connected
        if not self.device_public_key or self.device_public_key == "Unknown":
            if self.api.bot.connection_manager.isConnected and self.api.bot.connection_manager.mc:
                mc = self.api.bot.connection_manager.mc
                if mc and mc.self_info:
                    self.device_name = mc.self_info.get("name")
                    self.device_public_key = mc.self_info.get("public_key")
                    if isinstance(self.device_public_key, bytes):
                        self.device_public_key = self.device_public_key.hex()
                        
        if self.device_public_key:
            self.device_public_key = self.device_public_key.upper()
            
        logger.info(f"[{self.name}] Synced device info: Name={self.device_name}, PubKey={self.device_public_key}")

    async def _sync_device_info_and_reconnect_mqtt(self):
        """Called upon connection handshake: sync keys, then connect/reconnect MQTT."""
        await self._sync_device_info()
        if MQTT_AVAILABLE:
            await self._connect_mqtt()

    # ==============================================================================
    # MQTT Connections & JWT renewal
    # ==============================================================================

    async def _connect_mqtt(self):
        # Clean up any existing brokers
        for client_info in self.mqtt_clients:
            broker_num = client_info["broker_num"]
            client = client_info.get("client")
            if client:
                try:
                    client.disconnect()
                    client.loop_stop()
                except Exception: pass
        self.mqtt_clients.clear()
        self.mqtt_connected.clear()

        brokers = self.config.get("brokers", [])
        if not brokers:
            logger.info(f"[{self.name}] No MQTT brokers configured.")
            return

        for idx, b_cfg in enumerate(brokers, 1):
            try:
                server = b_cfg.get("server")
                port = b_cfg.get("port", 1883)
                
                # Check IATA for letsmesh
                is_letsmesh = 'letsmesh.net' in server.lower() or 'letsmesh.net' in b_cfg.get("token_audience", "").lower()
                iata_code = b_cfg.get("iata") or self.config.get("iata", "LOC")
                if is_letsmesh and iata_code == "LOC":
                    logger.warning(f"[{self.name}] Let's Mesh broker requires a valid IATA regional code. Skipping broker {idx} ({server}).")
                    continue
                
                client_id = f"meshbot_{self.device_public_key or 'device'}"
                if idx > 1:
                    client_id += f"_{idx}"
                transport = "websockets" if b_cfg.get("use_ws", False) else "tcp"
                client = mqtt.Client(client_id=client_id, clean_session=True, transport=transport)
                client.reconnect_delay_set(min_delay=1, max_delay=120)
                client.user_data_set({"broker_num": idx})
                
                # Append client metadata to self.mqtt_clients early so get_topic has access
                self.mqtt_clients.append({
                    "client": client,
                    "broker_num": idx,
                    "config": b_cfg
                })
                
                # Setup callbacks
                client.on_connect = self._on_mqtt_connect
                client.on_disconnect = self._on_mqtt_disconnect
                
                # Setup Last Will and Testament
                status_topic = self.get_topic("status", idx)
                if status_topic:
                    lwt_payload = json.dumps({
                        "status": "offline",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "origin": self.device_name or "MeshCore Device",
                        "origin_id": self.device_public_key.upper() if self.device_public_key else 'DEVICE'
                    })
                    client.will_set(status_topic, lwt_payload, qos=0, retain=True)

                # Setup authentication (Username/password or Token/JWT)
                token_aud = b_cfg.get("token_audience")
                if token_aud:
                    # JWT authentication
                    username = f"v1_{self.device_public_key.upper()}"
                    token = await self._generate_jwt(token_aud, idx, b_cfg)
                    if token:
                        client.username_pw_set(username, token)
                        logger.info(f"[{self.name}] Broker {idx}: Configured with JWT authentication.")
                    else:
                        logger.error(f"[{self.name}] Broker {idx}: Failed to generate JWT token. Skipping auth.")
                else:
                    # Username/password authentication
                    uname = b_cfg.get("username")
                    pword = b_cfg.get("password")
                    if uname:
                        client.username_pw_set(uname, pword)

                # TLS Configuration
                if b_cfg.get("use_tls", False):
                    import ssl
                    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
                    
                client.connect(server, port, keepalive=60)
                client.loop_start()
                
                logger.info(f"[{self.name}] Broker {idx}: Loop started for {server}:{port}")
            except Exception as e:
                # Remove failed client from list
                self.mqtt_clients = [c for c in self.mqtt_clients if c["broker_num"] != idx]
                logger.error(f"[{self.name}] Failed to connect to MQTT broker {idx}: {e}")

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        broker_num = userdata.get("broker_num", 1)
        if rc == 0:
            self.mqtt_connected[broker_num] = True
            logger.info(f"🟢 [{self.name}] Successfully connected to MQTT Broker {broker_num}.")
            # Publish initial online status thread-safely
            if self.api and self.api.bot and self.api.bot.loop:
                asyncio.run_coroutine_threadsafe(self._publish_status_online(broker_num), self.api.bot.loop)
        else:
            self.mqtt_connected[broker_num] = False
            logger.error(f"🔴 [{self.name}] Connection to MQTT Broker {broker_num} failed with code {rc}.")

    def _on_mqtt_disconnect(self, client, userdata, rc):
        broker_num = userdata.get("broker_num", 1)
        self.mqtt_connected[broker_num] = False
        logger.warning(f"🟡 [{self.name}] Disconnected from MQTT Broker {broker_num} (code {rc}).")

    async def _publish_status_online(self, broker_num):
        status_topic = self.get_topic("status", broker_num)
        if not status_topic:
            return
            
        client_info = self._get_client_info(broker_num)
        if not client_info:
            return
        client = client_info["client"]
        
        # Gather battery, neighbors, uptime details from cache
        state = self.api.get_state()
        status_payload = {
            "status": "online",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "origin": self.device_name or "MeshCore Device",
            "origin_id": self.device_public_key.upper() if self.device_public_key else 'DEVICE',
            "firmware": state.get("firmwareVersion", "unknown"),
            "model": state.get("hardwareModel", "unknown"),
            "battery": state.get("battery", 100),
            "neighbors": state.get("neighborCount", 0)
        }
        
        try:
            client.publish(status_topic, json.dumps(status_payload), qos=0, retain=True)
            logger.info(f"🟢 [{self.name}] Published online status to broker {broker_num} on topic {status_topic}.")
        except Exception as e:
            logger.error(f"[{self.name}] Error publishing online status to broker {broker_num}: {e}")

    async def _generate_jwt(self, audience: str, broker_num: int, b_cfg: dict) -> str:
        """Create signed JWT token using configuration private key, on-device signing, or fallback."""
        if not self.device_public_key:
            logger.warning(f"[{self.name}] Cannot generate JWT without device public key. Handshake incomplete?")
            return ""

        # Check if local private key is configured
        prv_key_hex = b_cfg.get("token_private_key") or self.device_private_key
        
        claims = {
            "aud": audience,
            "client": "MeshCore-Bot/packet-capture-module"
        }
        
        payload = AuthTokenPayload(
            public_key=self.device_public_key,
            exp=int(time.time()) + 86400, # 24 Hours
            **claims
        )
        
        # Method 1: Local PyNaCl signing with explicit private key
        if prv_key_hex and PYNACL_AVAILABLE:
            try:
                token = create_auth_token_internal(payload, prv_key_hex, self.device_public_key)
                # Cache expiry
                self.jwt_tokens[broker_num] = {
                    "token": token,
                    "expires_at": payload.exp,
                    "audience": audience
                }
                return token
            except Exception as e:
                logger.error(f"[{self.name}] Local JWT signing failed: {e}")

        # Method 2: On-device signing fallback
        if self.api.bot.connection_manager.isConnected and self.api.bot.connection_manager.mc:
            mc = self.api.bot.connection_manager.mc
            if hasattr(mc, 'commands') and hasattr(mc.commands, 'sign'):
                try:
                    logger.info(f"[{self.name}] Requesting on-device Ed25519 signature from hardware...")
                    
                    header = {'alg': 'Ed25519', 'typ': 'JWT'}
                    payload_dict = payload.to_dict()
                    header_encoded = base64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))
                    payload_encoded = base64url_encode(json.dumps(payload_dict, separators=(',', ':')).encode('utf-8'))
                    signing_input = f"{header_encoded}.{payload_encoded}"
                    signing_input_bytes = signing_input.encode('utf-8')
                    
                    # Call sign command on radio (BLE / Serial / TCP)
                    sig_evt = await mc.commands.sign(signing_input_bytes)
                    if sig_evt and hasattr(sig_evt, 'type') and sig_evt.type != EventType.ERROR:
                        sig_bytes = sig_evt.payload.get("signature")
                        if sig_bytes:
                            signature_hex = sig_bytes.hex() if isinstance(sig_bytes, bytes) else sig_bytes
                            token = f"{header_encoded}.{payload_encoded}.{signature_hex}"
                            self.jwt_tokens[broker_num] = {
                                "token": token,
                                "expires_at": payload.exp,
                                "audience": audience
                            }
                            return token
                except Exception as e:
                    logger.error(f"[{self.name}] Hardware on-device signing request failed: {e}")

        # Method 3: Fetch private key from device for future local signings
        if not prv_key_hex and self.api.bot.connection_manager.isConnected:
            try:
                mc = self.api.bot.connection_manager.mc
                if hasattr(mc, 'commands') and hasattr(mc.commands, 'export_private_key'):
                    logger.info(f"[{self.name}] Attempting to export private key from device...")
                    res = await mc.commands.export_private_key()
                    if res and hasattr(res, 'type') and res.payload:
                        prv_key = res.payload.get("private_key")
                        if prv_key:
                            self.device_private_key = prv_key.hex() if isinstance(prv_key, bytes) else prv_key
                            logger.info(f"[{self.name}] Successfully cached private key from hardware.")
                            # Retry signing locally
                            if PYNACL_AVAILABLE:
                                token = create_auth_token_internal(payload, self.device_private_key, self.device_public_key)
                                self.jwt_tokens[broker_num] = {
                                    "token": token,
                                    "expires_at": payload.exp,
                                    "audience": audience
                                }
                                return token
            except Exception as e:
                logger.error(f"[{self.name}] Failed to export private key from device: {e}")

        logger.error(f"[{self.name}] Could not generate JWT for broker {broker_num}. Local libraries or keys missing.")
        return ""

    def _get_client_info(self, broker_num: int) -> dict:
        for c in self.mqtt_clients:
            if c["broker_num"] == broker_num:
                return c
        return None

    # ==============================================================================
    # MQTT Publishing & Topics
    # ==============================================================================

    def _publish_packet_to_mqtt(self, packet_data: dict):
        if not MQTT_AVAILABLE or not self.mqtt_clients:
            return
            
        for client_info in self.mqtt_clients:
            broker_num = client_info["broker_num"]
            client = client_info["client"]
            if not self.mqtt_connected.get(broker_num, False):
                continue
                
            try:
                # 1. Publish standard packet
                packets_topic = self.get_topic("packets", broker_num)
                if packets_topic:
                    client.publish(packets_topic, json.dumps(packet_data), qos=0, retain=False)
                    logger.info(f"📤 [{self.name}] Published packet to broker {broker_num} on topic: {packets_topic}")
                    
                # 2. Publish raw packet (if configured)
                raw_topic = self.get_topic("raw", broker_num)
                if raw_topic:
                    raw_payload = {
                        "origin": packet_data["origin"],
                        "origin_id": packet_data["origin_id"],
                        "timestamp": packet_data["timestamp"],
                        "type": "RAW",
                        "data": packet_data["raw"]
                    }
                    client.publish(raw_topic, json.dumps(raw_payload), qos=0, retain=False)
            except Exception as e:
                logger.error(f"[{self.name}] Failed to publish packet to broker {broker_num}: {e}")

    def get_topic(self, topic_type: str, broker_num: int) -> str:
        topic_type_upper = topic_type.upper()
        client_info = self._get_client_info(broker_num)
        if not client_info:
            return None
        b_cfg = client_info["config"]
        
        # Check broker-specific override topic
        config_key = f"topic_{topic_type.lower()}"
        custom_topic = b_cfg.get(config_key)
        if custom_topic:
            return self.resolve_topic_template(custom_topic, broker_num)
            
        # Standard MeshCore observer defaults containing region and public key
        iata_defaults = {
            'STATUS': 'meshcore/{IATA}/{PUBLIC_KEY}/status',
            'PACKETS': 'meshcore/{IATA}/{PUBLIC_KEY}/packets',
            'RAW': None
        }
        
        chosen_default = iata_defaults.get(topic_type_upper)
        if not chosen_default:
            return None
            
        return self.resolve_topic_template(chosen_default, broker_num)

    def resolve_topic_template(self, template: str, broker_num: int) -> str:
        if not template:
            return template
            
        client_info = self._get_client_info(broker_num)
        if not client_info:
            return template
        b_cfg = client_info["config"]
        
        iata = b_cfg.get("iata") or self.config.get("iata", "LOC")
        pubkey = self.device_public_key or 'DEVICE'
        
        resolved = template.replace('{IATA}', iata.upper())
        resolved = resolved.replace('{IATA_lower}', iata.lower())
        resolved = resolved.replace('{PUBLIC_KEY}', pubkey.upper())
        return resolved

    # ==============================================================================
    # Periodic tasks (Scheduled via bot scheduler)
    # ==============================================================================

    async def _periodic_status(self):
        """Periodically publishes node stats & battery state to all connected brokers."""
        if not MQTT_AVAILABLE or not self.mqtt_clients:
            return
            
        # Sync latest details from state cache
        await self._sync_device_info()
        
        for client_info in self.mqtt_clients:
            broker_num = client_info["broker_num"]
            if self.mqtt_connected.get(broker_num, False):
                await self._publish_status_online(broker_num)

    async def _periodic_jwt_check(self):
        """Checks if any active JWT token is nearing expiry and proactively renews it."""
        if not MQTT_AVAILABLE or not self.mqtt_clients:
            return
            
        current_time = time.time()
        for client_info in self.mqtt_clients:
            idx = client_info["broker_num"]
            b_cfg = client_info["config"]
            if not b_cfg.get("token_audience"):
                continue
                
            token_info = self.jwt_tokens.get(idx)
            # Renew if missing, expired, or expiring in < 10 mins (600 seconds)
            if not token_info or (token_info["expires_at"] - current_time) < 600:
                logger.info(f"[{self.name}] JWT token for broker {idx} nearing expiry. Renewing...")
                
                # Generate new token
                new_token = await self._generate_jwt(b_cfg.get("token_audience"), idx, b_cfg)
                if new_token:
                    logger.info(f"[{self.name}] Successfully generated new JWT for broker {idx}. Reconnecting client...")
                    client = client_info["client"]
                    
                    # Update credentials on client
                    username = f"v1_{self.device_public_key.upper()}"
                    client.username_pw_set(username, new_token)
                    
                    # Disconnect to trigger auto-reconnection with new token
                    client.disconnect()
