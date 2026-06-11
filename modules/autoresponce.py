import logging
import asyncio
import re

logger = logging.getLogger("AutoresponceModule")

class Autoresponce:
    def __init__(self):
        self.name = "autoresponce"
        self.api = None
        self.config = {}
        
        # Validation schema for config.json under modules.autoresponce
        self.config_schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "channels": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            },
            "required": ["enabled"]
        }
        
        self.unsubscribe_msg = None
        self.channel_indices = {}

    def run_config(self, current_config):
        """
        Interactive configuration wizard for the Autoresponce module.
        Prompts the user for key settings.
        """
        config = dict(current_config) if current_config else {}
        
        print("\n--- Configure Autoresponce Settings ---")
        
        # 1. Enabled
        current_enabled = config.get("enabled", True)
        val = input(f"Enable Autoresponce Module? (y/n) [current: {'y' if current_enabled else 'n'}]: ").strip().lower()
        if val:
            config["enabled"] = val in ("y", "yes", "true")
            
        # 2. Channels list
        current_channels = config.get("channels", ["#test", "#testing"])
        val = input(f"Enter Comma-Separated Channels to listen on [current: {', '.join(current_channels)}]: ").strip()
        if val:
            config["channels"] = [x.strip() for x in val.split(",") if x.strip()]
            
        return config

    def init(self, api, config):
        """
        Lifecycle hook called upon module loading.
        Injects the ModuleAPI instance and module-specific configuration.
        """
        self.api = api
        self.config = config
        self.channels = config.get("channels", ["#test", "#testing"])
        
        # Declare all possible channel string variations to the bot API
        declared = []
        for ch in self.channels:
            declared.append(ch)
            if ch.startswith("#"):
                declared.append(ch[1:])
            else:
                declared.append(f"#{ch}")
        self.api.declare_channels(declared)
        
        logger.info(f"[{self.name}] Initialized with channels: {self.channels}")

    async def start(self):
        """
        Lifecycle hook called when the bot has successfully started up.
        Use this to register event subscriptions and request/validate channels.
        """
        logger.info(f"[{self.name}] Starting module...")
        
        self.channel_indices = {}
        for ch in self.channels:
            try:
                # request_channel registers the channel index to self.api's allowed set
                idx = await self.api.request_channel(ch)
                self.channel_indices[ch] = idx
                logger.info(f"[{self.name}] Channel '{ch}' index: {idx}")
                
                # Request variant with/without '#' as well to keep them in allowed list and mapping
                if ch.startswith("#"):
                    idx_clean = await self.api.request_channel(ch[1:])
                    self.channel_indices[ch[1:]] = idx_clean
                else:
                    idx_hash = await self.api.request_channel(f"#{ch}")
                    self.channel_indices[f"#{ch}"] = idx_hash
            except Exception as e:
                logger.error(f"[{self.name}] Failed to request channel '{ch}': {e}")
                
        self.unsubscribe_msg = self.api.subscribe("message", self._on_message)
        logger.info(f"[{self.name}] Subscribed to messages. Monitoring indices: {list(self.channel_indices.values())}")

    def stop(self):
        """
        Lifecycle hook called during graceful shutdown.
        Enforces cleaning up subscriptions.
        """
        logger.info(f"[{self.name}] Stopping module...")
        if self.unsubscribe_msg:
            self.unsubscribe_msg()
        logger.info(f"[{self.name}] Module stopped.")

    def _on_message(self, data):
        """
        Triggered when a message event is published.
        """
        sender = data.get("sender", "unknown")
        text = data.get("text", "")
        channel = data.get("channel")
        
        # Prevent self loops
        mc = self.api.bot.connection_manager.mc
        if mc and mc.self_info:
            self_name = mc.self_info.get("name")
            if self_name and sender == self_name:
                return

        # Direct messages do not map to target channels
        if channel is None:
            return

        # Verify if incoming channel index matches any of our monitored indices
        is_target_channel = False
        target_idx = None
        for ch, idx in self.channel_indices.items():
            if idx is not None and channel == idx:
                is_target_channel = True
                target_idx = idx
                break

        if not is_target_channel:
            return

        # Check if the word "test" is present in the message (case-insensitive)
        if "test" in text.lower():
            logger.info(f"[{self.name}] Match found! Sender: {sender}, Msg: {text}, Channel Index: {target_idx}")
            asyncio.create_task(self._send_reply(sender, target_idx))

    async def _send_reply(self, recipient, channel_idx):
        reply = f"@{recipient} ACK"
        logger.info(f"[{self.name}] Replying on channel {channel_idx}: {reply}")
        res = await self.api.bot.connection_manager.execute(["chan", str(channel_idx), reply])
        logger.info(f"[{self.name}] Reply response: {res}")
