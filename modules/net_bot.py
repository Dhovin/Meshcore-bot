import os
import json
import logging
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("NetBotModule")

class NetBot:
    def __init__(self):
        self.name = "net_bot"
        self.api = None
        self.config = {}
        self.config_schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "channel": {"type": "string"},
                "day_of_week": {"type": "string"},
                "time": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
                "keyword": {"type": "string"},
                "state_file": {"type": "string"},
                "timezone": {"type": "string"}
            },
            "required": ["enabled", "channel", "day_of_week", "time", "keyword"]
        }
        
        self.channel = "#net"
        self.day_of_week = "Tuesday"
        self.time = "19:00"
        self.keyword = "#checkin"
        self.state_file = "net_state.json"
        self.timezone = "UTC"
        self.state_file_path = ""
        
        # Subscriptions and tasks handles
        self.unsubscribe_msg = None
        self.unschedule_start = None
        self.unschedule_r1pm = None
        self.unschedule_r30m = None
        self.net_end_task = None
        
        # Active session state
        self.is_active = False
        self.net_start_time = None
        self.checkins = []
        self.channel_idx = 0

    def run_config(self, current_config):
        """
        Interactive configuration wizard for the NetBot module.
        Prompts the user for key settings.
        """
        config = dict(current_config) if current_config else {}
        
        print("\n--- Configure Net Settings ---")
        
        # 1. Enabled
        current_enabled = config.get("enabled", True)
        val = input(f"Enable Net Module? (y/n) [current: {'y' if current_enabled else 'n'}]: ").strip().lower()
        if val:
            config["enabled"] = val in ("y", "yes", "true", "1")
        elif "enabled" not in config:
            config["enabled"] = current_enabled
            
        # 2. Channel
        current_channel = config.get("channel", "#net")
        val = input(f"Enter Channel to monitor [current: {current_channel}]: ").strip()
        if val:
            config["channel"] = val
        elif "channel" not in config:
            config["channel"] = current_channel
            
        # 3. Day of week
        current_dow = config.get("day_of_week", "Tuesday")
        val = input(f"Enter Day of the week for the Net [current: {current_dow}]: ").strip()
        if val:
            config["day_of_week"] = val.capitalize()
        elif "day_of_week" not in config:
            config["day_of_week"] = current_dow
            
        # 4. Time
        current_time = config.get("time", "19:00")
        val = input(f"Enter Time (HH:MM) for the Net [current: {current_time}]: ").strip()
        if val:
            config["time"] = val
        elif "time" not in config:
            config["time"] = current_time
            
        # 5. Keyword
        current_keyword = config.get("keyword", "#checkin")
        val = input(f"Enter Keyword to check-in [current: {current_keyword}]: ").strip()
        if val:
            config["keyword"] = val
        elif "keyword" not in config:
            config["keyword"] = current_keyword

        # 6. State file
        current_state_file = config.get("state_file", "net_state.json")
        val = input(f"Enter State File Name [current: {current_state_file}]: ").strip()
        if val:
            config["state_file"] = val
        elif "state_file" not in config:
            config["state_file"] = current_state_file
            
        return config

    def init(self, api, config):
        self.api = api
        self.config = config
        self.channel = config.get("channel", "#net")
        self.day_of_week = config.get("day_of_week", "Tuesday")
        self.time = config.get("time", "19:00")
        self.keyword = config.get("keyword", "#checkin")
        self.state_file = config.get("state_file", "net_state.json")
        
        # Try to inherit from bot timezone if not explicitly provided
        self.timezone = config.get("timezone") or getattr(api.bot, "timezone", "UTC")
        
        config_dir = os.path.dirname(os.path.abspath(self.api.bot.config_path))
        self.state_file_path = os.path.join(config_dir, self.state_file)
        
        # Declare the channel index/name to the module manager
        self.api.declare_channels(self.channel)
        logger.info(f"[{self.name}] Initialized with channel: {self.channel}, timezone: {self.timezone}")

    async def start(self):
        logger.info(f"[{self.name}] Starting NetBot module...")
        
        # 1. Resolve channel index
        try:
            self.channel_idx = await self.api.request_channel(self.channel)
            logger.info(f"[{self.name}] Net channel '{self.channel}' mapped to index {self.channel_idx}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to request channel '{self.channel}': {e}")
            self.channel_idx = 0
            
        # 2. Subscribe to incoming messages
        self.unsubscribe_msg = self.api.subscribe("message", self._on_message)
        
        # 3. Schedule periodic tasks
        net_cron, r1pm_cron, r30m_cron = self._get_cron_expressions(self.day_of_week, self.time)
        
        logger.info(f"[{self.name}] Scheduling Net start with cron: '{net_cron}'")
        self.unschedule_start = self.api.schedule_task(net_cron, self._on_net_start, timezone=self.timezone)
        
        logger.info(f"[{self.name}] Scheduling 1 PM reminder with cron: '{r1pm_cron}'")
        self.unschedule_r1pm = self.api.schedule_task(r1pm_cron, self._on_1pm_reminder, timezone=self.timezone)
        
        logger.info(f"[{self.name}] Scheduling 30-min reminder with cron: '{r30m_cron}'")
        self.unschedule_r30m = self.api.schedule_task(r30m_cron, self._on_30m_reminder, timezone=self.timezone)
        
        # 4. Load persisted state and check if we should resume
        self._load_state()
        if self.is_active and self.net_start_time:
            try:
                start_dt = datetime.fromisoformat(self.net_start_time)
                # Ensure the parsed datetime is aware using the configured timezone
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=ZoneInfo(self.timezone))
                
                now = datetime.now(ZoneInfo(self.timezone))
                elapsed = (now - start_dt).total_seconds()
                
                if elapsed < 3600 and elapsed >= 0:
                    remaining = 3600 - elapsed
                    logger.info(f"[{self.name}] Resuming active Net started at {self.net_start_time}. Remaining time: {remaining:.1f}s")
                    self.net_end_task = asyncio.create_task(self._wait_and_end_net(remaining))
                else:
                    logger.info(f"[{self.name}] Active Net from state file has expired (elapsed: {elapsed:.1f}s). Ending it.")
                    await self._end_net()
            except Exception as e:
                logger.error(f"[{self.name}] Error trying to resume Net: {e}", exc_info=True)
                # Reset invalid/corrupted state
                self.is_active = False
                self.checkins = []
                self._save_state()

    def stop(self):
        logger.info(f"[{self.name}] Stopping NetBot module...")
        
        if self.unsubscribe_msg:
            self.unsubscribe_msg()
        if self.unschedule_start:
            self.unschedule_start()
        if self.unschedule_r1pm:
            self.unschedule_r1pm()
        if self.unschedule_r30m:
            self.unschedule_r30m()
            
        if self.net_end_task:
            self.net_end_task.cancel()
            self.net_end_task = None
            
        logger.info(f"[{self.name}] Stopped successfully.")

    def _get_cron_expressions(self, day_of_week_str, time_str):
        day_map = {
            "sunday": 0, "mon": 1, "monday": 1, "tue": 2, "tuesday": 2,
            "wed": 3, "wednesday": 3, "thu": 4, "thursday": 4,
            "fri": 5, "friday": 5, "sat": 6, "saturday": 6
        }
        dow = day_map.get(day_of_week_str.strip().lower())
        if dow is None:
            raise ValueError(f"Invalid day_of_week: {day_of_week_str}")
        
        parts = time_str.strip().split(':')
        if len(parts) != 2:
            raise ValueError(f"Invalid time format: {time_str}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            raise ValueError(f"Invalid hour/minute: {time_str}")
            
        net_cron = f"{minute} {hour} * * {dow}"
        r1pm_cron = f"0 13 * * {dow}"
        
        total_minutes = hour * 60 + minute
        prior_minutes = total_minutes - 30
        prior_dow = dow
        if prior_minutes < 0:
            prior_minutes += 24 * 60
            prior_dow = (dow - 1) % 7
        prior_hour = prior_minutes // 60
        prior_minute = prior_minutes % 60
        
        r30m_cron = f"{prior_minute} {prior_hour} * * {prior_dow}"
        
        return net_cron, r1pm_cron, r30m_cron

    def _load_state(self):
        if not os.path.exists(self.state_file_path):
            self.is_active = False
            self.net_start_time = None
            self.checkins = []
            return
            
        try:
            with open(self.state_file_path, 'r') as f:
                data = json.load(f)
                self.is_active = data.get("is_active", False)
                self.net_start_time = data.get("net_start_time")
                self.checkins = data.get("checkins", [])
                logger.info(f"[{self.name}] Loaded state: is_active={self.is_active}, checkins_count={len(self.checkins)}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to load state from {self.state_file_path}: {e}")
            self.is_active = False
            self.net_start_time = None
            self.checkins = []

    def _save_state(self):
        try:
            data = {
                "is_active": self.is_active,
                "net_start_time": self.net_start_time,
                "checkins": self.checkins
            }
            with open(self.state_file_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to save state to {self.state_file_path}: {e}")

    def _on_message(self, data):
        if not self.is_active:
            return
            
        sender = data.get("sender", "unknown")
        text = data.get("text", "")
        channel = data.get("channel")
        
        # Don't respond to ourselves
        if self.api.is_self(sender):
            return
            
        # Direct messages do not map to the target Net channel
        if channel is None:
            return

        asyncio.create_task(self._handle_message_async(sender, text, channel))

    async def _handle_message_async(self, sender, text, channel):
        if not await self.api.matches_channel(channel, self.channel):
            return
            
        # Case-insensitive substring match
        if self.keyword.lower() not in text.lower():
            return
            
        # Unique user deduplication
        if sender in self.checkins:
            logger.debug(f"[{self.name}] User '{sender}' already checked in. Ignoring subsequent message.")
            return
            
        self.checkins.append(sender)
        self._save_state()
        
        count = len(self.checkins)
        # Grammar adjustment:
        # 1 user: "Welcome Alice, 1 user has checked in."
        # >1 users: "Welcome Bob, 2 users have checked in."
        user_str = "user" if count == 1 else "users"
        have_str = "has" if count == 1 else "have"
        reply = f"Welcome {sender}, {count} {user_str} {have_str} checked in."
        
        logger.info(f"[{self.name}] New check-in: {sender}. Total count: {count}")
        await self._send_broadcast(reply)

    async def _send_broadcast(self, text):
        res = await self.api.bot.connection_manager.execute(["chan", str(self.channel_idx), text])
        logger.debug(f"[{self.name}] Broadcast message response: {res}")

    async def _on_net_start(self):
        logger.info(f"[{self.name}] Weekly Net start triggered!")
        self.is_active = True
        self.net_start_time = datetime.now(ZoneInfo(self.timezone)).isoformat()
        self.checkins = []
        self._save_state()
        
        welcome_msg = f"Welcome to the weekly {self.channel}. Please respond with {self.keyword} to checkin."
        await self._send_broadcast(welcome_msg)
        
        if self.net_end_task:
            self.net_end_task.cancel()
        self.net_end_task = asyncio.create_task(self._wait_and_end_net(3600))

    async def _on_1pm_reminder(self):
        logger.info(f"[{self.name}] 1 PM reminder triggered.")
        reminder_msg = f"Reminder: The weekly {self.channel} is today at {self.time}."
        await self._send_broadcast(reminder_msg)

    async def _on_30m_reminder(self):
        logger.info(f"[{self.name}] 30-minute reminder triggered.")
        reminder_msg = f"Reminder: The weekly {self.channel} starts in 30 minutes at {self.time}."
        await self._send_broadcast(reminder_msg)

    async def _wait_and_end_net(self, duration):
        try:
            await asyncio.sleep(duration)
            await self._end_net()
        except asyncio.CancelledError:
            logger.info(f"[{self.name}] Net end timer cancelled.")

    async def _end_net(self):
        logger.info(f"[{self.name}] Ending the Net session.")
        count = len(self.checkins)
        user_str = "user" if count == 1 else "users"
        end_msg = f"Thank you for joining. {count} {user_str} checkedin today."
        await self._send_broadcast(end_msg)
        
        self.is_active = False
        self.checkins = []
        self._save_state()
