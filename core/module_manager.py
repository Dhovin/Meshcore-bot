import os
import sys
import logging
import asyncio
import importlib.util
import contextvars
from core.validator import validate as validate_schema

logger = logging.getLogger("ModuleManager")

active_module_var = contextvars.ContextVar("active_module", default=None)

class ModuleAPI:
    def __init__(self, module_name, bot):
        self.module_name = module_name
        self.bot = bot
        # Ensure allowed channels registry is initialized for this module
        self.bot.module_manager.module_channels.setdefault(self.module_name, set())

    def subscribe(self, event_name, callback):
        """Subscribe to an event on the central Event Bus, wrapping it to run in the module's context."""
        if asyncio.iscoroutinefunction(callback):
            async def wrapped_async(*args, **kwargs):
                token = active_module_var.set(self.module_name)
                try:
                    return await callback(*args, **kwargs)
                finally:
                    active_module_var.reset(token)
            return self.bot.event_bus.subscribe(event_name, wrapped_async)
        else:
            def wrapped_sync(*args, **kwargs):
                token = active_module_var.set(self.module_name)
                try:
                    return callback(*args, **kwargs)
                finally:
                    active_module_var.reset(token)
            return self.bot.event_bus.subscribe(event_name, wrapped_sync)

    async def send(self, command_string):
        """
        Execute a command directly on the connected hardware node.
        Sanitizes commands to prevent multi-line injection.
        """
        sanitized = self._sanitize_command(command_string)
        return await self.bot.connection_manager.execute(sanitized)

    def get_state(self):
        """Retrieve a read-only deep-copied snapshot of the state cache."""
        return self.bot.state_cache.get_state()

    def schedule_task(self, cron_expression, callback, timezone=None):
        """Schedule a task on the central task scheduler, wrapping it to run in the module's context."""
        if timezone is None:
            # Check if the module instance has a 'timezone' attribute
            module_instance = self.bot.module_manager.modules.get(self.module_name)
            if module_instance and hasattr(module_instance, 'timezone'):
                timezone = getattr(module_instance, 'timezone')

        if asyncio.iscoroutinefunction(callback):
            async def wrapped_async(*args, **kwargs):
                token = active_module_var.set(self.module_name)
                try:
                    return await callback(*args, **kwargs)
                finally:
                    active_module_var.reset(token)
            return self.bot.scheduler.schedule(cron_expression, wrapped_async, self.module_name, timezone=timezone)
        else:
            def wrapped_sync(*args, **kwargs):
                token = active_module_var.set(self.module_name)
                try:
                    return callback(*args, **kwargs)
                finally:
                    active_module_var.reset(token)
            return self.bot.scheduler.schedule(cron_expression, wrapped_sync, self.module_name, timezone=timezone)

    def declare_channels(self, channels):
        """
        Declare the channels the module wants to use.
        Accepts a channel name (string), index (integer), or a list/set/tuple of them.
        """
        if not isinstance(channels, (list, set, tuple)):
            channels = [channels]
        
        allowed = self.bot.module_manager.module_channels.setdefault(self.module_name, set())
        for ch in channels:
            if ch is not None:
                allowed.add(ch)
                # If it's a string representation of an int, add it as int too
                if isinstance(ch, str) and ch.isdigit():
                    allowed.add(int(ch))
                elif isinstance(ch, int):
                    allowed.add(str(ch))
        logger.info(f"Module '{self.module_name}' declared allowed channels: {list(allowed)}")

    async def request_channel(self, channel_name):
        """
        Request a channel by name. If it does not exist on the node,
        automatically add/create it. Returns the channel index.
        """
        if not channel_name:
            return 0
            
        # If it's already an integer index
        if isinstance(channel_name, int):
            self.declare_channels(channel_name)
            return channel_name
        if isinstance(channel_name, str) and channel_name.isdigit():
            idx = int(channel_name)
            self.declare_channels(idx)
            return idx
            
        self.declare_channels(channel_name)

        # Ensure connection
        if not self.bot.connection_manager.isConnected or not self.bot.connection_manager.mc:
            logger.warning(f"Device not connected. Cannot request channel '{channel_name}'. Returning default index 0.")
            return 0
            
        # 1. Fetch channel list
        channels = await self.bot.connection_manager.execute("channels")
        if not isinstance(channels, list):
            logger.error(f"Failed to fetch channels list: {channels}")
            return 0
            
        # 2. Check if the channel already exists
        for ch in channels:
            if ch and ch.get("channel_name") == channel_name:
                idx = ch.get("channel_idx", 0)
                self.declare_channels(idx)
                return idx
                
        # 3. Channel does not exist, find first empty channel slot
        empty_idx = None
        for ch in channels:
            if ch and ch.get("channel_name") == "":
                empty_idx = ch.get("channel_idx")
                break
                
        if empty_idx is None:
            logger.error(f"No available empty channel slot to add '{channel_name}'")
            return 0
            
        # 4. Add/set the channel at the empty slot index
        logger.info(f"Channel '{channel_name}' requested by module '{self.module_name}' does not exist. Adding it at index {empty_idx}...")
        res = await self.bot.connection_manager.execute(["set_channel", str(empty_idx), channel_name])
        if isinstance(res, dict) and "error" in res:
            logger.error(f"Failed to create channel '{channel_name}': {res['error']}")
            return 0
            
        logger.info(f"Successfully added channel '{channel_name}' at index {empty_idx}")
        self.declare_channels(empty_idx)
        return empty_idx

    def _sanitize_command(self, cmd):
        if not isinstance(cmd, str):
            raise ValueError("Command must be a string")
        clean_cmd = cmd.strip()
        if '\n' in clean_cmd or '\r' in clean_cmd:
            raise ValueError("Command injection attempt detected: newlines are not allowed.")
        return clean_cmd

class ModuleManager:
    def __init__(self, bot):
        self.bot = bot
        self.modules = {}
        self.module_channels = {}

    async def load_modules(self, modules_dir):
        """
        Scans modules_dir, imports modules dynamically, and runs schema validation.
        Protects against directory traversal.
        """
        resolved_base = os.path.abspath(modules_dir)

        if not os.path.exists(resolved_base):
            logger.warning(f"Modules directory not found: {resolved_base}")
            return

        for file in os.listdir(resolved_base):
            if not file.endswith('.py') or file == '__init__.py':
                continue

            full_path = os.path.abspath(os.path.join(resolved_base, file))

            # Directory traversal protection:
            if not full_path.startswith(resolved_base):
                logger.error(f"Traversal attempt blocked: {file}")
                continue

            module_name = os.path.splitext(file)[0]
            try:
                # Dynamic import
                spec = importlib.util.spec_from_file_location(module_name, full_path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                class_name = ''.join(x.title() for x in module_name.split('_'))
                ModuleClass = getattr(module, class_name, None)
                if not ModuleClass:
                    ModuleClass = getattr(module, 'Module', None)

                if not ModuleClass:
                    logger.error(f"Module class not found in {file}. Expected name '{class_name}' or 'Module'.")
                    continue

                instance = ModuleClass()
                
                # Validate shape
                self._validate_module_shape(instance, file)

                # Get and validate configuration block
                module_config = self._get_and_validate_config(instance)

                # Inject API
                api = ModuleAPI(instance.name, self.bot)

                # Initialize
                logger.info(f"Initializing module: {instance.name}")
                token = active_module_var.set(instance.name)
                try:
                    init_hook = instance.init(api, module_config)
                    if asyncio.iscoroutine(init_hook):
                        await init_hook
                finally:
                    active_module_var.reset(token)

                self.modules[instance.name] = instance
            except Exception as e:
                logger.error(f"Failed to load module {file}: {e}", exc_info=True)

    async def start_modules(self):
        """Starts all successfully loaded modules."""
        for name, instance in self.modules.items():
            try:
                logger.info(f"Starting module: {name}")
                token = active_module_var.set(name)
                try:
                    start_hook = instance.start()
                    if asyncio.iscoroutine(start_hook):
                        await start_hook
                finally:
                    active_module_var.reset(token)
            except Exception as e:
                logger.error(f"Error starting module '{name}': {e}", exc_info=True)

    async def stop_modules(self):
        """Stops all active modules, enforcing a 10-second timeout."""
        logger.info("Stopping active modules...")
        stop_tasks = {}

        for name, instance in self.modules.items():
            logger.info(f"Stopping module: {name}")
            token = active_module_var.set(name)
            try:
                stop_hook = instance.stop()
                if asyncio.iscoroutine(stop_hook):
                    stop_tasks[name] = asyncio.create_task(stop_hook)
            except Exception as e:
                logger.error(f"Error invoking stop hook for module '{name}': {e}", exc_info=True)
            finally:
                active_module_var.reset(token)

        if stop_tasks:
            try:
                # Await all stop hooks with a strict 10s timeout
                names = list(stop_tasks.keys())
                tasks = list(stop_tasks.values())
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10.0)
                for module_name, res in zip(names, results):
                    if isinstance(res, Exception):
                        logger.error(f"Error during graceful stop of module '{module_name}': {res}", exc_info=res)
            except asyncio.TimeoutError:
                logger.warning("Graceful stop of modules timed out (10s limit).")
            except Exception as e:
                logger.error(f"Error gathering module stop hooks: {e}", exc_info=True)

        logger.info("All modules stopped.")

    def is_channel_allowed(self, module_name, channel_id):
        """
        Checks if the given module is allowed to access the channel (index or name).
        """
        allowed = self.module_channels.get(module_name)
        if allowed is None:
            return False
            
        if channel_id in allowed:
            return True
            
        # Attempt to map string/int representations
        if isinstance(channel_id, int):
            if str(channel_id) in allowed:
                return True
        elif isinstance(channel_id, str):
            if channel_id.isdigit() and int(channel_id) in allowed:
                return True
                
        # Resolve via connection_manager channels cache
        channels_cache = []
        if self.bot.connection_manager and self.bot.connection_manager.mc:
            channels_cache = getattr(self.bot.connection_manager.mc, 'channels', []) or []
            
        if isinstance(channel_id, str) and not channel_id.isdigit():
            # channel_id is a name
            for ch in channels_cache:
                if ch and ch.get("channel_name") == channel_id:
                    idx = ch.get("channel_idx")
                    if idx in allowed or str(idx) in allowed:
                        return True
        else:
            # channel_id is an index
            try:
                idx = int(channel_id)
                for ch in channels_cache:
                    if ch and ch.get("channel_idx") == idx:
                        name = ch.get("channel_name")
                        if name in allowed:
                            return True
            except (ValueError, TypeError):
                pass
                
        return False

    def _validate_module_shape(self, instance, file_name):
        if not hasattr(instance, 'name') or not instance.name:
            raise ValueError(f"Module in {file_name} must have a non-empty 'name' attribute.")

        required_hooks = ['init', 'start', 'stop']
        for hook in required_hooks:
            if not hasattr(instance, hook) or not callable(getattr(instance, hook)):
                raise ValueError(f"Module '{instance.name}' is missing required hook: '{hook}()'.")

    def _get_and_validate_config(self, instance):
        config = self.bot.config or {}
        modules_config = config.get("modules", {})
        module_config = modules_config.get(instance.name, {})

        if hasattr(instance, 'config_schema') and instance.config_schema:
            errors = validate_schema(instance.config_schema, module_config, instance.name)
            if errors:
                raise ValueError(f"Configuration validation failed for module '{instance.name}':\n- " + "\n- ".join(errors))

        return module_config

