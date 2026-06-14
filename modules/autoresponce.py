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
        
        # Declare only the exact channels to the bot API
        self.api.declare_channels(self.channels)
        
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
        
        if self.api.is_self(sender):
            return

        # Direct messages do not map to target channels
        if channel is None:
            return

        # Fast synchronous pattern check
        if "test" not in text.lower():
            return

        asyncio.create_task(self._handle_message_async(sender, text, channel))

    async def _handle_message_async(self, sender, text, channel):
        # Verify if incoming channel index matches any of our monitored indices
        is_target_channel = False
        target_idx = None
        for ch in self.channels:
            if await self.api.matches_channel(channel, ch):
                is_target_channel = True
                target_idx = self.channel_indices.get(ch)
                break

        if not is_target_channel:
            return

        logger.info(f"[{self.name}] Match found! Sender: {sender}, Msg: {text}, Channel Index: {target_idx}")
        await self._send_reply(sender, target_idx)

    async def _send_reply(self, recipient, channel_idx):
        reply = f"@[{recipient}] ACK"
        logger.info(f"[{self.name}] Replying on channel {channel_idx}: {reply}")
        res = await self.api.bot.connection_manager.execute(["chan", str(channel_idx), reply])
        logger.info(f"[{self.name}] Reply response: {res}")
