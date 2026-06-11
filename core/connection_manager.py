import time
import json
import shlex
import logging
import asyncio
import serial.tools.list_ports
from meshcore.meshcore import MeshCore
from meshcore.events import EventType

logger = logging.getLogger("ConnectionManager")

try:
    from bleak import BleakScanner
    BLE_AVAILABLE = True
except ImportError:
    BLE_AVAILABLE = False

class ConnectionManager:
    def __init__(self, bot):
        self.bot = bot
        self.mc = None
        self.isConnected = False
        self.connectionType = None
        self.deviceInfo = None

    async def connect(self):
        """
        Connect to the hardware node.
        Attempts Serial auto-discovery first, then BLE, then falls back to TCP.
        """
        conn_config = self.bot.config.get("connection", {})
        conn_type = conn_config.get("type", "auto")

        logger.info(f"Starting connection sequence (type: {conn_type})")

        target_port = conn_config.get("port")
        target_address = conn_config.get("address")
        baudrate = conn_config.get("baudrate", 115200)

        final_type = conn_type

        if conn_type == 'auto':
            logger.info("Running device auto-discovery...")
            
            # 1. Serial Discovery
            ports = list(serial.tools.list_ports.comports())
            if ports:
                # Filter for typical USB serial adapters
                usb_ports = []
                for p in ports:
                    desc = p.description.lower()
                    if any(x in desc for x in ['cp210', 'ch34', 'ftdi', 'usb', 'serial', 'uart']):
                        usb_ports.append(p)
                
                selected = usb_ports[0] if usb_ports else ports[0]
                logger.info(f"Auto-discovered Serial port: {selected.device} ({selected.description})")
                final_type = 'serial'
                target_port = selected.device
            
            # 2. BLE Discovery
            elif BLE_AVAILABLE:
                logger.info("No serial port found. Scanning BLE for companion nodes...")
                try:
                    devices = await BleakScanner.discover(timeout=3.0)
                    meshcore_ble = [d for d in devices if d.name and d.name.startswith("MeshCore-")]
                    if meshcore_ble:
                        selected = meshcore_ble[0]
                        logger.info(f"Auto-discovered BLE device: {selected.name} ({selected.address})")
                        final_type = 'ble'
                        target_address = selected.address
                except Exception as e:
                    logger.warning(f"BLE scan failed: {e}")
            
            # 3. Fallback to TCP if config has defaults
            if final_type == 'auto':
                host = conn_config.get("host")
                tcp_port = conn_config.get("tcpPort")
                if host and tcp_port:
                    logger.info(f"Auto-discovery fallback to TCP: {host}:{tcp_port}")
                    final_type = 'tcp'
                else:
                    raise RuntimeError("Auto-discovery failed: No Serial or BLE companion nodes detected, and no TCP host configured.")

        self.connectionType = final_type

        # Construct Native MeshCore connection
        if final_type == 'serial':
            if not target_port:
                raise ValueError("Serial port is required but not specified.")
            logger.info(f"Connecting to Serial port: {target_port} ({baudrate} baud)")
            self.mc = await MeshCore.create_serial(port=target_port, baudrate=baudrate, only_error=True)
        elif final_type == 'ble':
            logger.info(f"Connecting to BLE device: {target_address or 'Auto-scan'}")
            # If target_address is empty, create_ble will scan and pick first
            self.mc = await MeshCore.create_ble(address=target_address, only_error=True)
        elif final_type == 'tcp':
            host = conn_config.get("host", "127.0.0.1")
            tcp_port = conn_config.get("tcpPort", 5000)
            logger.info(f"Connecting to TCP: {host}:{tcp_port}")
            self.mc = await MeshCore.create_tcp(host=host, port=tcp_port, only_error=True)
        else:
            raise ValueError(f"Unsupported connection type: {final_type}")

        if not self.mc:
            raise RuntimeError("Failed to create MeshCore connection instance.")

        # Bind event subscriptions
        self._subscribe_events()

        # Run connection handshake
        await self._run_handshake()

    async def disconnect(self):
        """Disconnect and release the node serial/BLE interface."""
        if self.mc:
            logger.info("Closing connection to hardware node...")
            try:
                res = self.mc.stop()
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"Error while stopping meshcore client: {e}")
            self.isConnected = False
            self.mc = None

    async def _get_contact(self, arg):
        if not self.mc:
            return None
        await self.mc.ensure_contacts()
        contact = None
        try:
            int(arg, 16)
            contact = self.mc.get_contact_by_key_prefix(arg)
        except ValueError:
            pass
        if contact is None:
            contact = self.mc.get_contact_by_name(arg)
        return contact

    async def execute(self, cmd_str):
        """
        Execute command string natively on the client.
        Provides compatibility with string-based Module API commands.
        Natively supports all commands without external CLI delegation.
        """
        if not self.mc or not self.isConnected:
            return {"error": "Device not connected"}

        if isinstance(cmd_str, (list, tuple)):
            cmds = list(cmd_str)
        else:
            try:
                cmds = shlex.split(cmd_str)
            except Exception as e:
                return {"error": f"Invalid command encoding: {e}"}

        if not cmds:
            return {"error": "Empty command"}

        cmd = cmds[0]
        if cmd.startswith('$'):
            cmd = cmd[1:]
        if cmd.startswith('.'):
            cmd = cmd[1:]
        cmd = cmd.lower()

        # Enforce module channel access restrictions if running in a module context
        from core.module_manager import active_module_var
        active_module = active_module_var.get(None)
        if active_module:
            is_allowed = True
            denied_channel = None

            if cmd in ("chan", "ch"):
                if len(cmds) > 1:
                    chan_id = cmds[1]
                    if not self.bot.module_manager.is_channel_allowed(active_module, chan_id):
                        is_allowed = False
                        denied_channel = chan_id
                else:
                    is_allowed = False
                    denied_channel = "unknown"
            elif cmd in ("public", "dch"):
                if not self.bot.module_manager.is_channel_allowed(active_module, 0):
                    is_allowed = False
                    denied_channel = "0 (Public)"
            elif cmd == "get_channel":
                if len(cmds) > 1:
                    chan_id = cmds[1]
                    if not self.bot.module_manager.is_channel_allowed(active_module, chan_id):
                        is_allowed = False
                        denied_channel = chan_id
            elif cmd in ("set_channel", "add_channel"):
                if cmd == "set_channel" and len(cmds) > 2:
                    slot_or_name = cmds[1]
                    new_name = cmds[2]
                    # Allowed if either the slot/existing channel or the new name is allowed
                    if not (self.bot.module_manager.is_channel_allowed(active_module, slot_or_name) or
                            self.bot.module_manager.is_channel_allowed(active_module, new_name)):
                        is_allowed = False
                        denied_channel = f"{slot_or_name} -> {new_name}"
                elif cmd == "add_channel" and len(cmds) > 1:
                    new_name = cmds[1]
                    if not self.bot.module_manager.is_channel_allowed(active_module, new_name):
                        is_allowed = False
                        denied_channel = new_name
            elif cmd == "remove_channel":
                if len(cmds) > 1:
                    chan_id = cmds[1]
                    if not self.bot.module_manager.is_channel_allowed(active_module, chan_id):
                        is_allowed = False
                        denied_channel = chan_id

            if not is_allowed:
                logger.warning(f"Access denied: Module '{active_module}' attempted to access unauthorized channel '{denied_channel}'.")
                return {"error": f"Access denied: Module '{active_module}' is not authorized to use channel '{denied_channel}'."}

        try:
            if cmd in ("infos", "i", "query", "q", "ver", "v"):
                await self.mc.commands.send_appstart()
                return self.mc.self_info
            elif cmd in ("self_telemetry", "t"):
                res = await self.mc.commands.get_self_telemetry()
                return res.payload
            elif cmd == "clock":
                res = await self.mc.commands.get_time()
                return res.payload
            elif cmd == "reboot":
                res = await self.mc.commands.reboot()
                return res.payload
            elif cmd in ("sync_time", "clock sync", "st"):
                res = await self.mc.commands.set_time(int(time.time()))
                if res.type == EventType.ERROR:
                    return {"error": "Failed to sync time on node"}
                return {"ok": "time synced"}
            elif cmd == "time":
                if len(cmds) < 2:
                    return {"error": "Usage: time <epoch>"}
                res = await self.mc.commands.set_time(int(cmds[1]))
                if res.type == EventType.ERROR:
                    return {"error": "Failed to set time"}
                return {"ok": "time set"}
            elif cmd in ("sleep", "s"):
                if len(cmds) < 2:
                    return {"error": "Usage: sleep <seconds>"}
                secs = int(cmds[1])
                await asyncio.sleep(secs)
                return {"ok": f"slept for {secs} seconds"}
            elif cmd in ("wait_key", "wk"):
                return {"info": "wait_key ignored in non-interactive mode"}
            elif cmd in ("apply_to", "at"):
                if len(cmds) < 3:
                    return {"error": "Usage: apply_to <filter> <command_line>"}
                contact_filter = cmds[1]
                line_to_apply = cmds[2]
                
                await self.mc.ensure_contacts()
                upd_before = None
                upd_after = None
                contact_type = None
                min_hops = None
                max_hops = None
                flags = None
                count = 0
                results = []

                filters = contact_filter.split(",")
                for f in filters:
                    if f == "all":
                        pass
                    elif f.startswith("u"):
                        val_str = f[2:]
                        t = time.time()
                        if val_str.endswith("d"):
                            t = t - float(val_str[0:-1]) * 86400
                        elif val_str.endswith("h"):
                            t = t - float(val_str[0:-1]) * 3600
                        elif val_str.endswith("m"):
                            t = t - float(val_str[0:-1]) * 60
                        else:
                            t = int(val_str)
                        if f[1] == "<":
                            upd_before = t
                        elif f[1] == ">":
                            upd_after = t
                    elif f.startswith("t"):
                        if f[1] == "=":
                            contact_type = int(f[2:])
                    elif f.startswith("d"):
                        min_hops = 0
                    elif f.startswith("f"):
                        max_hops = -1
                    elif f.startswith("h"):
                        if f[1] == ">":
                            min_hops = int(f[2:]) + 1
                        elif f[1] == "<":
                            max_hops = int(f[2:]) - 1
                        elif f[1] == "=":
                            min_hops = int(f[2:])
                            max_hops = int(f[2:])
                    elif f.startswith("b"):
                        if f[1] == "=":
                            flags = int(f[2:])

                contacts = getattr(self.mc, '_contacts', {}) or getattr(self.mc, 'contacts', {})
                for c in dict(contacts).values():
                    if (contact_type is None or c.get("type") == contact_type) and \
                       (upd_before is None or c.get("lastmod", 0) < upd_before) and \
                       (upd_after is None or c.get("lastmod", 0) > upd_after) and \
                       (min_hops is None or c.get("out_path_len", 0) >= min_hops) and \
                       (max_hops is None or c.get("out_path_len", 0) <= max_hops) and \
                       (flags is None or (c.get("flags", 0) & flags) == flags):
                        
                        count += 1
                        c_name = c.get("adv_name") or c.get("name")
                        sub_cmd_str = line_to_apply
                        if sub_cmd_str.startswith("send ") or sub_cmd_str.startswith("msg "):
                            sub_cmd = ["msg", c_name, sub_cmd_str.split(" ", 1)[1]]
                        elif c.get("type") in (2, 3, 4):
                            sub_cmd = ["cmd", c_name, sub_cmd_str]
                        else:
                            sub_cmd = None
                        
                        if sub_cmd:
                            res_sub = await self.execute(sub_cmd)
                            results.append({"contact": c_name, "result": res_sub})
                
                return {"matches": count, "results": results}
            elif cmd in ("msg", "m", "{"):
                if len(cmds) < 3:
                    return {"error": "Usage: msg <recipient_name> <message>"}
                recipient = cmds[1]
                text = cmds[2]

                contact = await self._get_contact(recipient)
                if not contact:
                    return {"error": f"Unknown contact: {recipient}"}

                res = await self.mc.commands.send_msg(contact, text)
                if res.type == EventType.ERROR:
                    return {"error": f"Error sending message: {res}"}
                
                payload = dict(res.payload)
                if "expected_ack" in payload and isinstance(payload["expected_ack"], bytes):
                    payload["expected_ack"] = payload["expected_ack"].hex()
                return payload
            elif cmd in ("wait_ack", "wa", "}"):
                res = await self.mc.wait_for_event(EventType.ACK, timeout=5)
                if res is None:
                    return {"error": "Timeout waiting ack"}
                return res.payload
            elif cmd in ("chan", "ch"):
                if len(cmds) < 3:
                    return {"error": "Usage: chan <channel_idx_or_name> <message>"}
                chan_arg = cmds[1]
                text = cmds[2]

                if chan_arg.isdigit():
                    nb = int(chan_arg)
                else:
                    chan = self._get_channel_by_name(chan_arg)
                    if not chan:
                        return {"error": f"Unknown channel name: {chan_arg}"}
                    nb = chan.get("channel_idx", 0)

                res = await self.mc.commands.send_chan_msg(nb, text)
                if res.type == EventType.ERROR:
                    return {"error": f"Error sending channel message: {res}"}
                return res.payload
            elif cmd in ("public", "dch"):
                if len(cmds) < 2:
                    return {"error": "Usage: public <message>"}
                res = await self.mc.commands.send_chan_msg(0, cmds[1])
                if res.type == EventType.ERROR:
                    return {"error": f"Error sending public message: {res}"}
                return res.payload
            elif cmd in ("recv", "r"):
                res = await self.mc.commands.get_msg()
                return res.payload
            elif cmd in ("wait_msg", "wm"):
                if await self.mc.wait_for_event(EventType.MESSAGES_WAITING, timeout=60):
                    res = await self.mc.commands.get_msg()
                    return res.payload
                return {"error": "timeout waiting message"}
            elif cmd in ("sync_msgs", "sm"):
                msgs = []
                while True:
                    res = await self.mc.commands.get_msg()
                    if res.type == EventType.NO_MORE_MSGS or res.type == EventType.ERROR:
                        break
                    msgs.append(res.payload)
                return msgs
            elif cmd in ("msgs_subscribe", "ms"):
                return {"info": "daemon dynamically publishes all received messages to event bus"}
            elif cmd in ("get_channels", "channels", "chans"):
                ch = 0
                channels = []
                while True:
                    res = await self.mc.commands.get_channel(ch)
                    if res.type == EventType.ERROR:
                        break
                    info = dict(res.payload)
                    if "channel_secret" in info and isinstance(info["channel_secret"], bytes):
                        info["channel_secret"] = info["channel_secret"].hex()
                    channels.append(info)
                    ch += 1
                self.mc.channels = channels
                return channels
            elif cmd == "get_channel":
                if len(cmds) < 2:
                    return {"error": "Usage: get_channel <n>"}
                chan_arg = cmds[1]
                if chan_arg.isdigit():
                    nb = int(chan_arg)
                else:
                    if not hasattr(self.mc, 'channels') or not self.mc.channels:
                        await self.execute("channels")
                    chan = self._get_channel_by_name(chan_arg)
                    if not chan:
                        return {"error": f"Unknown channel: {chan_arg}"}
                    nb = chan.get("channel_idx", 0)
                res = await self.mc.commands.get_channel(nb)
                if res.type == EventType.ERROR:
                    return {"error": f"Error getting channel info: {res}"}
                info = dict(res.payload)
                if "channel_secret" in info and isinstance(info["channel_secret"], bytes):
                    info["channel_secret"] = info["channel_secret"].hex()
                return info
            elif cmd in ("set_channel", "add_channel"):
                if len(cmds) < 3:
                    return {"error": "Usage: set_channel <idx_or_name> <name> [key_hex]"}
                chan_arg = cmds[1]
                name_arg = cmds[2]
                key_arg = None
                if len(cmds) > 3:
                    try:
                        key_arg = bytes.fromhex(cmds[3])
                    except ValueError:
                        return {"error": "Key must be a valid hex string"}
                if chan_arg.isdigit():
                    nb = int(chan_arg)
                else:
                    if not hasattr(self.mc, 'channels') or not self.mc.channels:
                        await self.execute("channels")
                    chan = self._get_channel_by_name(chan_arg)
                    if not chan:
                        nb = len(getattr(self.mc, 'channels', []))
                    else:
                        nb = chan.get("channel_idx", 0)
                res = await self.mc.commands.set_channel(nb, name_arg, key_arg)
                if res.type == EventType.ERROR:
                    return {"error": f"Failed to set channel: {res}"}
                res_info = await self.mc.commands.get_channel(nb)
                if res_info.type == EventType.ERROR:
                    return {"error": f"Failed to retrieve updated channel info: {res_info}"}
                info = dict(res_info.payload)
                if "channel_secret" in info and isinstance(info["channel_secret"], bytes):
                    info["channel_secret"] = info["channel_secret"].hex()
                if not hasattr(self.mc, 'channels'):
                    self.mc.channels = []
                while len(self.mc.channels) <= nb:
                    self.mc.channels.append({})
                self.mc.channels[nb] = info
                return info
            elif cmd == "remove_channel":
                if len(cmds) < 2:
                    return {"error": "Usage: remove_channel <idx_or_name>"}
                chan_arg = cmds[1]
                if chan_arg.isdigit():
                    nb = int(chan_arg)
                else:
                    if not hasattr(self.mc, 'channels') or not self.mc.channels:
                        await self.execute("channels")
                    chan = self._get_channel_by_name(chan_arg)
                    if not chan:
                        return {"error": f"Unknown channel: {chan_arg}"}
                    nb = chan.get("channel_idx", 0)
                empty_key = bytes.fromhex(16 * "00")
                res = await self.mc.commands.set_channel(nb, "", empty_key)
                if res.type == EventType.ERROR:
                    return {"error": f"Failed to remove channel: {res}"}
                res_info = await self.mc.commands.get_channel(nb)
                if res_info.type != EventType.ERROR:
                    info = dict(res_info.payload)
                    if "channel_secret" in info and isinstance(info["channel_secret"], bytes):
                        info["channel_secret"] = info["channel_secret"].hex()
                    if hasattr(self.mc, 'channels') and nb < len(self.mc.channels):
                        self.mc.channels[nb] = info
                return {"ok": f"channel {nb} removed"}
            elif cmd == "scope":
                if len(cmds) < 2:
                    return {"error": "Usage: scope <scope_val>"}
                scope = cmds[1]
                if scope in ("None", "0", "clear", ""):
                    scope = "*"
                res = await self.mc.commands.set_flood_scope(scope)
                if res.type == EventType.ERROR:
                    return {"error": f"Failed to set scope: {res}"}
                return res.payload
            elif cmd in ("advert", "a"):
                res = await self.mc.commands.send_advert()
                if res.type == EventType.ERROR:
                    return {"error": f"Error sending advert: {res}"}
                return res.payload
            elif cmd in ("floodadv", "flood_advert"):
                res = await self.mc.commands.send_advert(flood=True)
                if res.type == EventType.ERROR:
                    return {"error": f"Error sending advert: {res}"}
                return res.payload
            elif cmd == "get":
                if len(cmds) < 2:
                    return {"error": "Usage: get <param>"}
                param = cmds[1].lower()
                
                # Check for self_info params
                if param == "name":
                    await self.mc.commands.send_appstart()
                    return {"name": self.mc.self_info.get("name")}
                elif param == "tx":
                    await self.mc.commands.send_appstart()
                    return {"tx_power": self.mc.self_info.get("tx_power")}
                elif param == "coords":
                    await self.mc.commands.send_appstart()
                    return {"lat": self.mc.self_info.get("adv_lat"), "lon": self.mc.self_info.get("adv_lon")}
                elif param == "lat":
                    await self.mc.commands.send_appstart()
                    return {"lat": self.mc.self_info.get("adv_lat")}
                elif param == "lon":
                    await self.mc.commands.send_appstart()
                    return {"lon": self.mc.self_info.get("adv_lon")}
                elif param == "radio":
                    await self.mc.commands.send_appstart()
                    radio = {
                        "radio_freq": self.mc.self_info.get("radio_freq"),
                        "radio_bw": self.mc.self_info.get("radio_bw"),
                        "radio_sf": self.mc.self_info.get("radio_sf"),
                        "radio_cr": self.mc.self_info.get("radio_cr")
                    }
                    res = await self.mc.commands.send_device_query()
                    if res.type != EventType.ERROR and "repeat" in res.payload:
                        radio["repeat"] = res.payload["repeat"]
                    return radio
                elif param == "repeat":
                    res = await self.mc.commands.send_device_query()
                    if res.type == EventType.ERROR:
                        return {"error": f"Error querying repeat: {res}"}
                    return {"repeat": res.payload.get("repeat")}
                elif param == "path_hash_mode":
                    res = await self.mc.commands.send_device_query()
                    if res.type == EventType.ERROR:
                        return {"error": f"Error querying path_hash_mode: {res}"}
                    return {"path_hash_mode": res.payload.get("path_hash_mode")}
                elif param == "bat":
                    res = await self.mc.commands.get_bat()
                    if res.type == EventType.ERROR:
                        return {"error": f"Error getting bat: {res}"}
                    return res.payload
                elif param == "private_key":
                    res = await self.mc.commands.export_private_key()
                    if res.type == EventType.ERROR:
                        return {"error": f"Error exporting private key: {res}"}
                    payload = dict(res.payload)
                    if "private_key" in payload and isinstance(payload["private_key"], bytes):
                        payload["private_key"] = payload["private_key"].hex()
                    return payload
                elif param == "fstats":
                    res = await self.mc.commands.get_bat()
                    if res.type == EventType.ERROR:
                        return {"error": f"Error getting bat: {res}"}
                    return res.payload
                elif param == "multi_acks":
                    await self.mc.commands.send_appstart()
                    return {"multi_acks": self.mc.self_info.get("multi_acks")}
                elif param == "manual_add_contacts":
                    await self.mc.commands.send_appstart()
                    return {"manual_add_contacts": self.mc.self_info.get("manual_add_contacts")}
                elif param == "autoadd_config":
                    res = await self.mc.commands.get_autoadd_config()
                    if res.type == EventType.ERROR:
                        return {"error": f"Error getting autoadd_config: {res}"}
                    return res.payload
                elif param == "telemetry_mode_base":
                    await self.mc.commands.send_appstart()
                    return {"telemetry_mode_base": self.mc.self_info.get("telemetry_mode_base")}
                elif param == "telemetry_mode_loc":
                    await self.mc.commands.send_appstart()
                    return {"telemetry_mode_loc": self.mc.self_info.get("telemetry_mode_loc")}
                elif param == "telemetry_mode_env":
                    await self.mc.commands.send_appstart()
                    return {"telemetry_mode_env": self.mc.self_info.get("telemetry_mode_env")}
                elif param == "advert_loc_policy":
                    await self.mc.commands.send_appstart()
                    return {"advert_loc_policy": self.mc.self_info.get("adv_loc_policy")}
                elif param == "auto_update_contacts":
                    return {"auto_update_contacts": getattr(self.mc, 'auto_update_contacts', False)}
                elif param == "custom":
                    res = await self.mc.commands.get_custom_vars()
                    if res.type == EventType.ERROR:
                        return {"error": "Error getting custom variables"}
                    return res.payload
                elif param in ("stats_core", "stats_radio", "stats_packets", "stats", "status"):
                    stats = {}
                    res_core = await self.mc.commands.get_stats_core()
                    if res_core.type != EventType.ERROR:
                        stats.update(res_core.payload)
                    res_rad = await self.mc.commands.get_stats_radio()
                    if res_rad.type != EventType.ERROR:
                        stats.update(res_rad.payload)
                    res_pkt = await self.mc.commands.get_stats_packets()
                    if res_pkt.type != EventType.ERROR:
                        stats.update(res_pkt.payload)
                    return stats
                elif param == "allowed_repeat_freq":
                    res = await self.mc.commands.get_allowed_repeat_freq()
                    return res.payload
                elif param == "default_scope":
                    res = await self.mc.commands.get_default_flood_scope()
                    return res.payload
                else:
                    res = await self.mc.commands.get_custom_vars()
                    if res.type != EventType.ERROR:
                        vname = param[1:] if param.startswith("_") else param
                        if vname in res.payload:
                            return {vname: res.payload[vname]}
                    return {"error": f"Unknown variable: {param}"}
            elif cmd == "set":
                if len(cmds) < 3:
                    return {"error": "Usage: set <param> <value>"}
                setting = cmds[1].lower()
                value = cmds[2]
                
                if setting == "name":
                    res = await self.mc.commands.set_name(value)
                    if res.type == EventType.ERROR:
                        return {"error": f"Failed to set name: {res}"}
                    return res.payload
                elif setting in ("tx", "tx_power"):
                    res = await self.mc.commands.set_tx_power(value)
                    if res.type == EventType.ERROR:
                        return {"error": f"Failed to set TX power: {res}"}
                    return res.payload
                elif setting == "pin":
                    res = await self.mc.commands.set_devicepin(value)
                    if res.type == EventType.ERROR:
                        return {"error": f"Failed to set pin: {res}"}
                    return res.payload
                elif setting == "radio":
                    params = value.split(",")
                    if len(params) > 4:
                        repeat = params[4] in ("repeat", "on", "1", "yes")
                        res = await self.mc.commands.set_radio(params[0], params[1], params[2], params[3], repeat)
                    else:
                        res = await self.mc.commands.set_radio(*params)
                    if res.type == EventType.ERROR:
                        return {"error": f"Failed to set radio config: {res}"}
                    return res.payload
                elif setting == "path_hash_mode":
                    res = await self.mc.commands.set_path_hash_mode(int(value))
                    if res.type == EventType.ERROR:
                        return {"error": f"Failed to set path_hash_mode: {res}"}
                    return res.payload
                elif setting == "lat":
                    res = await self.mc.commands.set_coord_lat(float(value))
                    if res.type == EventType.ERROR:
                        return {"error": f"Failed to set lat: {res}"}
                    return res.payload
                elif setting == "lon":
                    res = await self.mc.commands.set_coord_lon(float(value))
                    if res.type == EventType.ERROR:
                        return {"error": f"Failed to set lon: {res}"}
                    return res.payload
                elif setting == "coords":
                    parts = value.split(",")
                    if len(parts) == 2:
                        await self.mc.commands.set_coord_lat(float(parts[0]))
                        await self.mc.commands.set_coord_lon(float(parts[1]))
                        return {"lat": parts[0], "lon": parts[1]}
                    return {"error": "Coords must be in format lat,lon"}
                elif setting == "autoadd_config":
                    val_int = int(value, 16) if value.lower().startswith("0x") else int(value)
                    res = await self.mc.commands.set_autoadd_config(val_int)
                    if res.type == EventType.ERROR:
                        return {"error": f"Failed to set autoadd_config: {res}"}
                    return res.payload
                else:
                    return {"error": f"Unsupported setting: {setting}"}
            elif cmd in ("node_discover", "nd"):
                prefix_only = True
                if len(cmds) == 1:
                    types = 0xFF
                else:
                    arg_val = cmds[1]
                    try:
                        types = int(arg_val)
                    except ValueError:
                        if "all" in arg_val:
                            types = 0xFF
                        else:
                            types = 0
                            if "rep" in arg_val or "rpt" in arg_val:
                                types |= 4
                            if "cli" in arg_val or "comp" in arg_val:
                                types |= 2
                            if "room" in arg_val:
                                types |= 8
                            if "sens" in arg_val:
                                types |= 16
                    if "full" in arg_val:
                        prefix_only = False

                res = await self.mc.commands.send_node_discover_req(types, prefix_only=prefix_only)
                if res is None or res.type == EventType.ERROR:
                    return {"error": "Error sending discover request"}
                
                exp_tag = res.payload["tag"].to_bytes(4, "little").hex()
                dn = []
                while True:
                    r = await self.mc.wait_for_event(
                        EventType.DISCOVER_RESPONSE,
                        attribute_filters={"tag": exp_tag},
                        timeout=5
                    )
                    if r is None or r.type == EventType.ERROR:
                        break
                    dn.append(r.payload)
                return dn
            elif cmd in ("contacts", "list", "lc"):
                await self.mc.ensure_contacts()
                contacts = getattr(self.mc, '_contacts', {}) or getattr(self.mc, 'contacts', {})
                res_dict = {}
                for k, v in contacts.items():
                    res_dict[k] = dict(v)
                return res_dict
            elif cmd in ("reload_contacts", "rc"):
                await self.mc.commands.get_contacts()
                contacts = getattr(self.mc, '_contacts', {}) or getattr(self.mc, 'contacts', {})
                res_dict = {}
                for k, v in contacts.items():
                    res_dict[k] = dict(v)
                return res_dict
            elif cmd in ("contact_info", "ci"):
                if len(cmds) < 2:
                    return {"error": "Usage: contact_info <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                return contact
            elif cmd == "contact_timeout":
                if len(cmds) < 3:
                    return {"error": "Usage: contact_timeout <contact_name_or_key> <timeout_val>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                contact["timeout"] = float(cmds[2])
                self._save_contacts()
                return {"ok": f"timeout set to {cmds[2]} for {contact.get('adv_name')}"}
            elif cmd in ("share_contact", "sc"):
                if len(cmds) < 2:
                    return {"error": "Usage: share_contact <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                res = await self.mc.commands.share_contact(contact)
                if res.type == EventType.ERROR:
                    return {"error": f"Error sharing contact: {res}"}
                return res.payload
            elif cmd in ("export_contact", "ec"):
                if len(cmds) < 2:
                    return {"error": "Usage: export_contact <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                res = await self.mc.commands.export_contact(contact)
                if res.type == EventType.ERROR:
                    return {"error": f"Error exporting contact: {res}"}
                return res.payload
            elif cmd in ("import_contact", "ic"):
                if len(cmds) < 2:
                    return {"error": "Usage: import_contact <URI>"}
                uri = cmds[1]
                if uri.startswith("meshcore://"):
                    payload_hex = uri[11:]
                else:
                    payload_hex = uri
                res = await self.mc.commands.import_contact(bytes.fromhex(payload_hex))
                if res.type == EventType.ERROR:
                    return {"error": f"Error importing contact: {res}"}
                await self.mc.commands.get_contacts()
                self._load_contacts()
                return {"ok": "contact imported"}
            elif cmd in ("upload_contact", "uc"):
                if len(cmds) < 2:
                    return {"error": "Usage: upload_contact <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                res = await self.mc.commands.export_contact(contact)
                if res.type == EventType.ERROR:
                    return {"error": f"Error exporting contact: {res}"}
                import requests
                resp = requests.post("https://map.meshcore.dev/api/v1/nodes", json={"links": [res.payload['uri']]})
                return {"status_code": resp.status_code, "response": resp.text}
            elif cmd == "card":
                res = await self.mc.commands.export_contact()
                if res.type == EventType.ERROR:
                    return {"error": f"Error exporting card: {res}"}
                return res.payload
            elif cmd == "upload_card":
                res = await self.mc.commands.export_contact()
                if res.type == EventType.ERROR:
                    return {"error": f"Error exporting card: {res}"}
                import requests
                resp = requests.post("https://map.meshcore.dev/api/v1/nodes", json={"links": [res.payload['uri']]})
                return {"status_code": resp.status_code, "response": resp.text}
            elif cmd == "remove_contact":
                if len(cmds) < 2:
                    return {"error": "Usage: remove_contact <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                res = await self.mc.commands.remove_contact(contact)
                if res.type == EventType.ERROR:
                    return {"error": f"Error removing contact: {res}"}
                pubkey = contact["public_key"]
                if hasattr(self.mc, 'contacts') and self.mc.contacts and pubkey in self.mc.contacts:
                    del self.mc.contacts[pubkey]
                if hasattr(self.mc, '_contacts') and self.mc._contacts and pubkey in self.mc._contacts:
                    del self.mc._contacts[pubkey]
                self._save_contacts()
                return res.payload
            elif cmd == "path":
                if len(cmds) < 2:
                    return {"error": "Usage: path <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                return {
                    "adv_name": contact.get("adv_name", "Unknown"),
                    "out_path_hash_len": contact.get("out_path_hash_len", 0),
                    "out_path_len": contact.get("out_path_len", 0),
                    "out_path": contact.get("out_path", "")
                }
            elif cmd in ("disc_path", "dp"):
                if len(cmds) < 2:
                    return {"error": "Usage: disc_path <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.send_path_discovery_sync(contact, timeout)
                if res is None:
                    return {"error": "timeout"}
                return res.payload
            elif cmd in ("reset_path", "rp"):
                if len(cmds) < 2:
                    return {"error": "Usage: reset_path <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                res = await self.mc.commands.reset_path(contact)
                if res.type == EventType.ERROR:
                    return {"error": f"Error resetting path: {res}"}
                contact["out_path"] = ""
                contact["out_path_len"] = -1
                self._save_contacts()
                return res.payload
            elif cmd in ("change_path", "cp"):
                if len(cmds) < 3:
                    return {"error": "Usage: change_path <contact_name_or_key> <path>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                path = cmds[2]
                if path == "0":
                    path = ""
                elif "," in path and not ":" in path:
                    path_hash_size = int(len(path.split(",")[0])/2)
                    path = path + f":{path_hash_size-1}"
                path = path.replace(",", "")
                res = await self.mc.commands.change_contact_path(contact, path)
                if res.type == EventType.ERROR:
                    return {"error": f"Error setting path: {res}"}
                return res.payload
            elif cmd in ("advert_path", "ap"):
                if len(cmds) < 2:
                    return {"error": "Usage: advert_path <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                contact_key = contact["public_key"] if contact else cmds[1]
                res = await self.mc.commands.get_advert_path(contact_key)
                if res is None:
                    return {"error": "Error sending command"}
                if res.type == EventType.ERROR:
                    return {"error": str(res)}
                return res.payload
            elif cmd in ("change_flags", "cf"):
                if len(cmds) < 3:
                    return {"error": "Usage: change_flags <contact_name_or_key> <flags>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                res = await self.mc.commands.change_contact_flags(contact, int(cmds[2]))
                if res.type == EventType.ERROR:
                    return {"error": f"Error setting flags: {res}"}
                return res.payload
            elif cmd in ("req_acl", "ra"):
                if len(cmds) < 2:
                    return {"error": "Usage: req_acl <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.req_acl_sync(contact, timeout)
                if res is None:
                    return {"error": "Error getting ACL data"}
                return res
            elif cmd in ("req_telemetry", "rt"):
                if len(cmds) < 2:
                    return {"error": "Usage: req_telemetry <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.req_telemetry_sync(contact, timeout)
                if res is None:
                    return {"error": "Error getting telemetry data"}
                return {
                    "name": contact.get("adv_name", "Unknown"),
                    "pubkey_pre": contact.get("public_key", "")[0:16],
                    "lpp": res
                }
            elif cmd in ("req_regions", "rr"):
                if len(cmds) < 2:
                    return {"error": "Usage: req_regions <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.req_regions_sync(contact, timeout)
                if res is None:
                    return {"error": "Error getting regions data"}
                return {"repeater": contact.get("adv_name", "Unknown"), "regions": res}
            elif cmd in ("req_owner", "ro"):
                if len(cmds) < 2:
                    return {"error": "Usage: req_owner <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.req_owner_sync(contact, timeout)
                if res is None:
                    return {"error": "Error getting owner data"}
                return res
            elif cmd == "req_clock":
                if len(cmds) < 2:
                    return {"error": "Usage: req_clock <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.req_basic_sync(contact, timeout)
                if res is None:
                    return {"error": "Error getting clock data"}
                clock_val = int.from_bytes(bytes.fromhex(res["data"][0:8]), byteorder="little", signed=False)
                return {"clock": clock_val}
            elif cmd in ("req_mma", "rm"):
                if len(cmds) < 4:
                    return {"error": "Usage: req_mma <contact_name_or_key> <from> <to>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                
                def parse_secs(val):
                    if val.endswith("s"):
                        return int(val[0:-1])
                    elif val.endswith("m"):
                        return int(val[0:-1]) * 60
                    elif val.endswith("h"):
                        return int(val[0:-1]) * 3600
                    return int(val) * 60

                from_secs = parse_secs(cmds[2])
                to_secs = parse_secs(cmds[3])
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.req_mma_sync(contact, from_secs, to_secs, timeout)
                if res is None:
                    return {"error": "Error getting mma data"}
                return res
            elif cmd == "pending_contacts":
                pending = getattr(self.mc, 'pending_contacts', {})
                res_dict = {}
                for k, v in pending.items():
                    res_dict[k] = dict(v)
                return res_dict
            elif cmd == "flush_pending":
                self.mc.flush_pending_contacts()
                return {"ok": "pending contacts flushed"}
            elif cmd == "add_pending":
                if len(cmds) < 2:
                    return {"error": "Usage: add_pending <pending_name_or_key>"}
                arg = cmds[1]
                contact = self.mc.pop_pending_contact(arg)
                if contact is None:
                    pending = getattr(self.mc, 'pending_contacts', {})
                    for c in pending.values():
                        if c.get("adv_name") == arg:
                            contact = self.mc.pop_pending_contact(c["public_key"])
                            break
                if contact is None:
                    return {"error": f"Contact {arg} does not exist in pending"}
                res = await self.mc.commands.add_contact(contact)
                if res.type == EventType.ERROR:
                    return {"error": f"Failed to add contact: {res}"}
                if not hasattr(self.mc, 'contacts') or self.mc.contacts is None:
                    self.mc.contacts = {}
                self.mc.contacts[contact["public_key"]] = contact
                if not hasattr(self.mc, '_contacts') or self.mc._contacts is None:
                    self.mc._contacts = {}
                self.mc._contacts[contact["public_key"]] = contact
                self._save_contacts()
                return res.payload
            elif cmd in ("login", "l"):
                if len(cmds) < 3:
                    return {"error": "Usage: login <contact_name_or_key> <password>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                password = cmds[2]
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.send_login_sync(contact, password, timeout=timeout)
                if res is None:
                    return {"error": "Login timeout/failed"}
                return {"login_success": res.type == EventType.LOGIN_SUCCESS}
            elif cmd == "logout":
                if len(cmds) < 2:
                    return {"error": "Usage: logout <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                res = await self.mc.commands.send_logout(contact)
                return res.payload
            elif cmd in ("cmd", "c", "["):
                if len(cmds) < 3:
                    return {"error": "Usage: cmd <contact_name_or_key> <command>"}
                dest = None
                if len(cmds[1]) == 12:
                    try:
                        dest = bytes.fromhex(cmds[1])
                    except ValueError:
                        dest = None
                if dest is None:
                    dest = await self._get_contact(cmds[1])
                if dest is None:
                    return {"error": f"Unknown destination: {cmds[1]}"}
                
                res = await self.mc.commands.send_cmd(dest, cmds[2])
                if res.type == EventType.ERROR:
                    return {"error": f"Error sending cmd: {res}"}
                payload = dict(res.payload)
                if "expected_ack" in payload and isinstance(payload["expected_ack"], bytes):
                    payload["expected_ack"] = payload["expected_ack"].hex()
                return payload
            elif cmd in ("wmt8", "]"):
                if await self.mc.wait_for_event(EventType.MESSAGES_WAITING, timeout=8):
                    res = await self.mc.commands.get_msg()
                    return res.payload
                return {"error": "timeout waiting message"}
            elif cmd in ("req_status", "rs"):
                if len(cmds) < 2:
                    return {"error": "Usage: req_status <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.req_status_sync(contact, timeout)
                if res is None:
                    return {"error": "Error getting status data"}
                return res
            elif cmd in ("req_neighbours", "rn"):
                if len(cmds) < 2:
                    return {"error": "Usage: req_neighbours <contact_name_or_key>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.fetch_all_neighbours(contact, timeout=timeout)
                if res is None:
                    return {"error": "Error getting neighbours data"}
                return res
            elif cmd == "req_binary":
                if len(cmds) < 3:
                    return {"error": "Usage: req_binary <contact_name_or_key> <hex_payload>"}
                contact = await self._get_contact(cmds[1])
                if not contact:
                    return {"error": f"Unknown contact: {cmds[1]}"}
                timeout = 0 if not "timeout" in contact else contact["timeout"]
                res = await self.mc.commands.req_binary(contact, bytes.fromhex(cmds[2]), timeout)
                if res is None:
                    return {"error": "Error getting binary data"}
                return res
            elif cmd in ("trace", "tr"):
                if len(cmds) < 2:
                    return {"error": "Usage: trace <path>"}
                path = cmds[1]
                flags = None
                if not "," in path:
                    if ":" in path:
                        flags = int(path.split(":")[1])
                        path = path.split(":")[0]
                    path = bytes.fromhex(path)
                
                res = await self.mc.commands.send_trace(path=path, flags=flags)
                if res and res.type != EventType.ERROR:
                    tag = int.from_bytes(res.payload['expected_ack'], byteorder="little")
                    timeout = res.payload["suggested_timeout"] / 1000 * 1.2
                    ev = await self.mc.wait_for_event(
                        EventType.TRACE_DATA,
                        attribute_filters={"tag": tag},
                        timeout=timeout
                    )
                    if ev is None:
                        return {"error": "timeout waiting trace"}
                    elif ev.type == EventType.ERROR:
                        return {"error": "Error waiting trace"}
                    else:
                        return ev.payload
                return {"error": "Error sending trace command"}
            else:
                return {"error": f"Unsupported command: {cmd}"}
        except Exception as e:
            cmd_log_str = " ".join(cmds) if isinstance(cmd_str, (list, tuple)) else cmd_str

            logger.error(f"Error executing command '{cmd_log_str}': {e}", exc_info=True)
            return {"error": str(e)}

    async def sync_time(self):
        """Sync node RTC with host system time."""
        logger.info("Synchronizing node RTC clock...")
        try:
            res = await self.mc.commands.set_time(int(time.time()))
            if res.type == EventType.ERROR:
                logger.error("RTC Clock synchronization failed.")
            else:
                self.bot.state_cache.update("timeSynced", True)
                logger.info("RTC Clock successfully synchronized with host system time.")
                self.bot.event_bus.publish("time_sync", {"ok": "time synced"})
        except Exception as e:
            logger.error(f"Error syncing time: {e}", exc_info=True)

    def _subscribe_events(self):
        # Native registrations
        self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_private_message)
        self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_message)
        self.mc.subscribe(EventType.ADVERTISEMENT, self._on_advertisement)
        self.mc.subscribe(EventType.PATH_UPDATE, self._on_path_update)
        self.mc.subscribe(EventType.NEW_CONTACT, self._on_new_contact)
        self.mc.subscribe(EventType.DISCONNECTED, self._on_disconnect)

    async def _run_handshake(self):
        # Establish background contact loading
        await self.mc.ensure_contacts()
        self._load_contacts()
        self._save_contacts()
        await self.mc.start_auto_message_fetching()

        logger.info("Executing connection handshake device query...")
        res = await self.mc.commands.send_device_query()
        if res.type == EventType.ERROR:
            raise RuntimeError(f"Handshake device query failed: {res}")

        self.deviceInfo = res.payload
        self.isConnected = True

        # Update cache
        self.bot.state_cache.update("connectionStatus", "connected")
        self.bot.state_cache.update_from_telemetry(res.payload)

        logger.info(f"Handshake complete. Connected to node: {self.mc.self_info.get('name', 'Unknown')}")
        self.bot.event_bus.publish("connect", res.payload)

        # Trigger clock sync immediately
        await self.sync_time()

    def _on_private_message(self, event):
        try:
            payload = event.payload or {}
            msg = {
                "sender": payload.get("sender") or payload.get("from") or "unknown",
                "text": payload.get("message") or payload.get("crypted") or "",
                "channel": 0,
                "timestamp": payload.get("time") or int(time.time()),
                "snr": payload.get("snr"),
                "rssi": payload.get("rssi"),
                "path": payload.get("path", [])
            }
            self.bot.event_bus.publish("message", msg)
        except Exception as e:
            logger.error(f"Error handling private message event: {e}", exc_info=True)

    def _on_channel_message(self, event):
        try:
            payload = event.payload or {}
            msg = {
                "sender": payload.get("sender") or payload.get("from") or "unknown",
                "text": payload.get("message") or payload.get("crypted") or "",
                "channel": payload.get("chan_name") or payload.get("chan_nb") or 0,
                "timestamp": payload.get("time") or int(time.time()),
                "snr": payload.get("snr"),
                "rssi": payload.get("rssi"),
                "path": payload.get("path", [])
            }
            self.bot.event_bus.publish("message", msg)
        except Exception as e:
            logger.error(f"Error handling channel message event: {e}", exc_info=True)

    def _on_advertisement(self, event):
        try:
            self.bot.event_bus.publish("advert", event.payload)
            payload = event.payload or {}
            pubkey = payload.get("public_key")
            if pubkey:
                if hasattr(self, 'mc') and self.mc:
                    if not hasattr(self.mc, '_contacts') or self.mc._contacts is None:
                        self.mc._contacts = {}
                    
                    if pubkey not in self.mc._contacts:
                        self.mc._contacts[pubkey] = {
                            "public_key": pubkey,
                            "type": payload.get("type", 1),
                            "flags": payload.get("flags", 0),
                            "adv_name": payload.get("adv_name") or payload.get("name") or f"Unknown-{pubkey[:6]}",
                            "last_advert": payload.get("last_advert") or int(time.time()),
                            "adv_lat": payload.get("adv_lat", 0.0),
                            "adv_lon": payload.get("adv_lon", 0.0),
                            "lastmod": payload.get("lastmod") or int(time.time())
                        }
                    else:
                        contact = self.mc._contacts[pubkey]
                        if payload.get("adv_name") or payload.get("name"):
                            contact["adv_name"] = payload.get("adv_name") or payload.get("name")
                        contact["last_advert"] = payload.get("last_advert") or int(time.time())
                        if "adv_lat" in payload:
                            contact["adv_lat"] = payload["adv_lat"]
                        if "adv_lon" in payload:
                            contact["adv_lon"] = payload["adv_lon"]
                        contact["lastmod"] = payload.get("lastmod") or int(time.time())
                self._save_contacts()
        except Exception as e:
            logger.error(f"Error handling advertisement event: {e}", exc_info=True)

    def _on_path_update(self, event):
        try:
            self.bot.event_bus.publish("path_update", event.payload)
        except Exception as e:
            logger.error(f"Error handling path update event: {e}", exc_info=True)

    def _on_new_contact(self, event):
        try:
            self.bot.event_bus.publish("new_contact", event.payload)
            self._save_contacts()
        except Exception as e:
            logger.error(f"Error handling new contact event: {e}", exc_info=True)

    def _on_disconnect(self, event):
        try:
            logger.critical("Connection lost to companion node! Triggering graceful shutdown...")
            self.isConnected = False
            self.bot.state_cache.update("connectionStatus", "disconnected")
            self.bot.event_bus.publish("disconnect", event.payload)
            self.bot.loop.call_soon_threadsafe(self.bot.shutdown_event.set)
        except Exception as e:
            logger.error(f"Error handling disconnect event: {e}", exc_info=True)

    def _get_channel_by_name(self, name):
        channels = getattr(self.mc, 'channels', [])
        for ch in channels:
            if ch.get("channel_name") == name:
                return ch
        return None

    def _load_contacts(self):
        """Load persistent contacts from config/contacts.json into self.mc._contacts."""
        import os
        contacts_file = os.path.abspath("config/contacts.json")
        if not os.path.exists(contacts_file):
            return
        try:
            with open(contacts_file, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            
            if not hasattr(self, 'mc') or not self.mc:
                return

            if not hasattr(self.mc, '_contacts') or self.mc._contacts is None:
                self.mc._contacts = {}

            loaded_count = 0
            for k, v in saved.items():
                if k not in self.mc._contacts:
                    self.mc._contacts[k] = v
                    loaded_count += 1
                else:
                    new_lastmod = self.mc._contacts[k].get("lastmod", 0)
                    old_lastmod = v.get("lastmod", 0)
                    if old_lastmod > new_lastmod:
                        self.mc._contacts[k] = v
                        loaded_count += 1
            
            logger.info(f"Loaded {loaded_count} new/updated persistent contacts into memory.")
        except Exception as e:
            logger.error(f"Error loading persistent contacts: {e}", exc_info=True)

    def _save_contacts(self):
        """Save local contacts copy to config/contacts.json."""
        import os
        contacts_file = os.path.abspath("config/contacts.json")
        try:
            existing_contacts = {}
            if os.path.exists(contacts_file):
                try:
                    with open(contacts_file, 'r', encoding='utf-8') as f:
                        existing_contacts = json.load(f)
                except Exception:
                    pass

            merged = {}
            if hasattr(self, 'mc') and self.mc and hasattr(self.mc, '_contacts') and self.mc._contacts:
                for k, v in self.mc._contacts.items():
                    merged[k] = dict(v)

            for k, v in existing_contacts.items():
                if k not in merged:
                    merged[k] = v
                else:
                    new_lastmod = merged[k].get("lastmod", 0)
                    old_lastmod = v.get("lastmod", 0)
                    if old_lastmod > new_lastmod:
                        merged[k] = v

            os.makedirs(os.path.dirname(contacts_file), exist_ok=True)
            with open(contacts_file, 'w', encoding='utf-8') as f:
                json.dump(merged, f, indent=2)
            
            logger.info(f"Saved {len(merged)} contacts persistently to {contacts_file}.")
        except Exception as e:
            logger.error(f"Error saving contacts: {e}", exc_info=True)
