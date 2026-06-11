import os
import sys
import json
import logging
import asyncio
import signal
from core.validator import validate as validate_schema
from core.event_bus import EventBus
from core.state_cache import StateCache
from core.scheduler import Scheduler
from core.connection_manager import ConnectionManager
from core.module_manager import ModuleManager

logger = logging.getLogger("MeshBot")

class SafeStreamWrapper:
    def __init__(self, stream):
        self.stream = stream
        self.encoding = getattr(stream, 'encoding', 'utf-8')

    def write(self, data):
        try:
            self.stream.write(data)
        except UnicodeEncodeError:
            try:
                encoding = self.encoding or 'ascii'
                safe_data = data.encode(encoding, errors='replace').decode(encoding)
                self.stream.write(safe_data)
            except Exception:
                self.stream.write(data.encode('ascii', errors='replace').decode('ascii'))

    def flush(self):
        if hasattr(self.stream, 'flush'):
            self.stream.flush()

class MeshBot:
    def __init__(self, config_path="config/config.json", schema_path="config/schema.json", modules_dir="modules"):
        self.config_path = config_path
        self.schema_path = schema_path
        self.modules_dir = modules_dir
        self.config = {}
        self.event_bus = EventBus()
        self.state_cache = StateCache()
        self.scheduler = Scheduler()
        self.connection_manager = ConnectionManager(self)
        self.module_manager = ModuleManager(self)
        self.loop = None
        self.shutdown_event = None
        self._shutting_down = False
        self.ipc_server = None
        self.timezone = "UTC"

    def load_and_validate_config(self):
        """Load config.json and validate it against schema.json."""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        with open(self.config_path, 'r') as f:
            self.config = json.load(f)

        if not os.path.exists(self.schema_path):
            raise FileNotFoundError(f"Schema file not found: {self.schema_path}")
        with open(self.schema_path, 'r') as f:
            schema = json.load(f)

        errors = validate_schema(schema, self.config)
        if errors:
            raise ValueError("Configuration schema validation failed:\n- " + "\n- ".join(errors))

    def setup_logging(self):
        """Set up standard framework-wide logging."""
        # Reconfigure stdout/stderr encoding on Windows to prevent UnicodeEncodeError with emojis
        if sys.platform == "win32":
            try:
                sys.stdout.reconfigure(encoding='utf-8')
                sys.stderr.reconfigure(encoding='utf-8')
            except Exception:
                pass

        log_dir = os.path.dirname(self.config_path)
        log_file = os.path.join(log_dir, "meshbot.log")
        
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        
        # Clear existing handlers configured by external libraries
        for h in list(root.handlers):
            root.removeHandler(h)
            
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        
        # Add stdout stream handler
        sh = logging.StreamHandler(SafeStreamWrapper(sys.stdout))
        sh.setFormatter(formatter)
        root.addHandler(sh)
        
        # Add persistent file handler
        try:
            fh = logging.FileHandler(log_file, encoding='utf-8')
            fh.setFormatter(formatter)
            root.addHandler(fh)
        except Exception as e:
            print(f"Warning: Could not create log file handler: {e}", file=sys.stderr)

    def run(self):
        """Main non-async entry point for the daemon."""
        self.setup_logging()
        try:
            self.load_and_validate_config()
        except Exception as e:
            logger.error(f"Configuration load/validation failure: {e}", exc_info=True)
            return False

        self.loop = asyncio.get_event_loop()
        self.shutdown_event = asyncio.Event()

        # Write PID file
        pid_file = os.path.join(os.path.dirname(self.config_path), "meshbot.pid")
        try:
            with open(pid_file, 'w') as f:
                f.write(str(os.getpid()))
        except Exception as e:
            logger.warning(f"Could not create PID file '{pid_file}': {e}")

        self._setup_signals()

        try:
            self.loop.run_until_complete(self.main())
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt caught, stopping...")
            if not self._shutting_down:
                self.loop.run_until_complete(self.shutdown())
        except Exception as e:
            logger.critical(f"Unhandled exception in event loop: {e}", exc_info=True)
        finally:
            self.loop.close()
        return True

    def _setup_signals(self):
        """Register graceful shutdown handlers for OS signals."""
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                self.loop.add_signal_handler(sig, lambda: self.loop.call_soon_threadsafe(self.shutdown_event.set))
        except NotImplementedError:
            # Fallback for Windows where add_signal_handler is not implemented
            def handle_sig(sig, frame=None):
                logger.info(f"Signal {sig} received, setting shutdown event.")
                self.loop.call_soon_threadsafe(self.shutdown_event.set)
            
            signal.signal(signal.SIGINT, handle_sig)
            signal.signal(signal.SIGTERM, handle_sig)

    async def resolve_timezone(self):
        cfg_tz = self.config.get("core", {}).get("timezone", "auto")
        if cfg_tz == "auto" or not cfg_tz:
            try:
                def fetch_ip_tz():
                    import urllib.request
                    try:
                        req = urllib.request.Request(
                            "https://ipapi.co/timezone/",
                            headers={"User-Agent": "MeshCore-bot/1.0"}
                        )
                        with urllib.request.urlopen(req, timeout=5) as response:
                            return response.read().decode('utf-8').strip()
                    except Exception:
                        return None
                detected = await asyncio.get_event_loop().run_in_executor(None, fetch_ip_tz)
                if detected:
                    from zoneinfo import ZoneInfo
                    try:
                        ZoneInfo(detected)
                        self.timezone = detected
                        logger.info(f"Automatically detected local timezone: {self.timezone}")
                        return
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Failed to auto-detect timezone: {e}")
                
            # Fallback to system default
            import datetime
            system_tz = datetime.datetime.now().astimezone().tzinfo
            if system_tz:
                self.timezone = str(system_tz)
            else:
                self.timezone = "UTC"
            logger.info(f"Using system default timezone: {self.timezone}")
        else:
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(cfg_tz)
                self.timezone = cfg_tz
                logger.info(f"Using configured timezone: {self.timezone}")
            except Exception:
                logger.warning(f"Configured timezone '{cfg_tz}' is invalid. Falling back to UTC.")
                self.timezone = "UTC"

    async def main(self):
        logger.info("Starting MeshCore-bot Central Hub...")
        
        # Resolve timezone
        await self.resolve_timezone()
        self.scheduler.timezone = self.timezone
        
        # 1. Connect to hardware node
        try:
            await self.connection_manager.connect()
        except Exception as e:
            logger.critical(f"Failed to connect to companion node: {e}", exc_info=True)
            self.shutdown_event.set()
            await self.shutdown()
            return

        # 2. Start Scheduler tick loop
        self.scheduler.start()

        # 3. Load modules
        await self.module_manager.load_modules(self.modules_dir)
        
        # 4. Start modules
        await self.module_manager.start_modules()

        # 4.5. Start IPC Server
        await self.start_ipc_server()

        # 5. Schedule periodic time synchronization
        sync_cron = self.config.get("core", {}).get("timeSyncInterval", "0 0 * * *")
        self.scheduler.schedule(sync_cron, self.connection_manager.sync_time, name="core_time_sync")

        logger.info("MeshCore-bot started successfully and running modules.")
        
        # Wait until shutdown event is set (by signal handler or connect failure)
        await self.shutdown_event.wait()
        logger.info(f"shutdown_event.wait() completed! Event state: {self.shutdown_event.is_set()}")
        await self.shutdown()

    async def shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Initiating graceful shutdown...")
        
        # 0. Stop IPC server
        if self.ipc_server:
            logger.info("Stopping IPC server...")
            try:
                self.ipc_server.close()
                await self.ipc_server.wait_closed()
            except Exception as e:
                logger.error(f"Error stopping IPC server: {e}", exc_info=True)

        # 1. Stop all active modules (10s timeout enforced internally)
        await self.module_manager.stop_modules()

        # 2. Stop scheduler background tasks
        self.scheduler.stop()

        # 3. Disconnect connection manager
        await self.connection_manager.disconnect()

        # Remove PID lockfile
        pid_file = os.path.join(os.path.dirname(self.config_path), "meshbot.pid")
        if os.path.exists(pid_file):
            try:
                os.remove(pid_file)
            except Exception as e:
                logger.warning(f"Could not remove PID file '{pid_file}': {e}")

        logger.info("Graceful shutdown sequence complete.")

    async def start_ipc_server(self):
        ipc_port = self.config.get("core", {}).get("ipcPort", 5002)
        try:
            self.ipc_server = await asyncio.start_server(
                self.handle_ipc_client, '127.0.0.1', ipc_port
            )
            logger.info(f"IPC Server started on 127.0.0.1:{ipc_port}")
        except Exception as e:
            logger.error(f"Failed to start IPC Server on port {ipc_port}: {e}", exc_info=True)

    async def handle_ipc_client(self, reader, writer):
        try:
            data = await reader.read(4096)
            if not data:
                return
            
            try:
                msg = json.loads(data.decode('utf-8'))
            except Exception as e:
                writer.write(json.dumps({"error": f"Invalid JSON payload: {e}"}).encode('utf-8'))
                await writer.drain()
                return

            if not isinstance(msg, dict) or "command" not in msg:
                writer.write(json.dumps({"error": "Missing 'command' field"}).encode('utf-8'))
                await writer.drain()
                return
            
            cmds = msg["command"]
            logger.info(f"IPC executing command on behalf of client: {cmds}")
            res = await self.connection_manager.execute(cmds)
            
            writer.write(json.dumps(res).encode('utf-8'))
            await writer.drain()
        except Exception as e:
            logger.error(f"Error handling IPC client: {e}", exc_info=True)
            try:
                writer.write(json.dumps({"error": str(e)}).encode('utf-8'))
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
