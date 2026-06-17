import unittest
import asyncio
import os
import json
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from modules.net_bot import NetBot

class TestNetBotModule(unittest.TestCase):
    def setUp(self):
        self.module = NetBot()
        self.api = MagicMock()
        self.api.is_self = MagicMock(side_effect=lambda sender: sender == "TestBotDevice")
        self.api.matches_channel = AsyncMock(return_value=True)
        
        # Mock connection manager
        self.conn_manager = MagicMock()
        self.conn_manager.isConnected = True
        self.conn_manager.mc = MagicMock()
        self.conn_manager.mc.self_info = {"name": "TestBotDevice"}
        self.conn_manager.execute = AsyncMock(return_value={"ok": True})
        self.api.bot.connection_manager = self.conn_manager
        self.api.bot.timezone = "UTC"
        self.api.bot.config_path = "config/config.json"
        
        # Config block
        self.config = {
            "enabled": True,
            "channel": "#net",
            "day_of_week": "Tuesday",
            "time": "19:00",
            "keyword": "#checkin",
            "state_file": "test_net_state.json"
        }
        
        # Set up a test state file path to avoid writing to production state file
        self.test_state_file = os.path.join("config", "test_net_state.json")

    def tearDown(self):
        # Stop module to cancel any scheduled/pending tasks
        try:
            self.module.stop()
        except Exception:
            pass
        # Clean up test state file if created
        if os.path.exists(self.test_state_file):
            try:
                os.remove(self.test_state_file)
            except Exception:
                pass

    def test_init(self):
        self.module.init(self.api, self.config)
        self.assertEqual(self.module.api, self.api)
        self.assertEqual(self.module.channel, "#net")
        self.assertEqual(self.module.day_of_week, "Tuesday")
        self.assertEqual(self.module.time, "19:00")
        self.assertEqual(self.module.keyword, "#checkin")
        self.assertEqual(self.module.timezone, "UTC")
        self.assertTrue(self.module.state_file_path.endswith("test_net_state.json"))
        self.api.declare_channels.assert_called_with("#net")

    def test_cron_calculation_normal(self):
        # Tuesday 19:00 -> Net at 19:00 (dow=2), 1pm reminder at 13:00 (dow=2), 30m prior at 18:30 (dow=2)
        net_cron, r1_cron, r2_cron = self.module._get_cron_expressions("Tuesday", "19:00")
        self.assertEqual(net_cron, "0 19 * * 2")
        self.assertEqual(r1_cron, "0 13 * * 2")
        self.assertEqual(r2_cron, "30 18 * * 2")

    def test_cron_calculation_wrap(self):
        # Monday 00:15 -> Net at 00:15 (dow=1), 1pm reminder at 13:00 (dow=1), 30m prior at 23:45 on Sunday (dow=0)
        net_cron, r1_cron, r2_cron = self.module._get_cron_expressions("Monday", "00:15")
        self.assertEqual(net_cron, "15 0 * * 1")
        self.assertEqual(r1_cron, "0 13 * * 1")
        self.assertEqual(r2_cron, "45 23 * * 0")

    def test_cron_calculation_invalid_day(self):
        with self.assertRaises(ValueError):
            self.module._get_cron_expressions("Blomsday", "19:00")

    def test_cron_calculation_invalid_time(self):
        with self.assertRaises(ValueError):
            self.module._get_cron_expressions("Tuesday", "25:00")
        with self.assertRaises(ValueError):
            self.module._get_cron_expressions("Tuesday", "19:61")
        with self.assertRaises(ValueError):
            self.module._get_cron_expressions("Tuesday", "invalid")

    def test_start_schedules_tasks(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.api.request_channel = AsyncMock(return_value=5)
            self.module.init(self.api, self.config)
            
            loop.run_until_complete(self.module.start())
            
            self.assertEqual(self.module.channel_idx, 5)
            self.api.subscribe.assert_called_with("message", self.module._on_message)
            
            # 3 scheduled tasks
            self.assertEqual(self.api.schedule_task.call_count, 3)
            # Loaded state should default to inactive
            self.assertFalse(self.module.is_active)
        finally:
            loop.close()

    def test_on_message_ignores_when_inactive(self):
        self.module.init(self.api, self.config)
        self.module.is_active = False
        
        with patch.object(self.module, "_handle_message_async") as mock_handler:
            self.module._on_message({"sender": "Alice", "text": "checkin", "channel": 1})
            mock_handler.assert_not_called()

    def test_on_message_ignores_self(self):
        self.module.init(self.api, self.config)
        self.module.is_active = True
        
        with patch.object(self.module, "_handle_message_async") as mock_handler:
            self.module._on_message({"sender": "TestBotDevice", "text": "#checkin", "channel": 1})
            mock_handler.assert_not_called()

    def test_on_message_ignores_direct_message(self):
        self.module.init(self.api, self.config)
        self.module.is_active = True
        
        with patch.object(self.module, "_handle_message_async") as mock_handler:
            self.module._on_message({"sender": "Alice", "text": "#checkin", "channel": None})
            mock_handler.assert_not_called()

    def test_handle_message_valid_checkin(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.is_active = True
            self.module.channel_idx = 1
            self.module.checkins = []
            
            async def run_test():
                await self.module._handle_message_async("Alice", "Hey #checkin now please", 1)
                
                # Check user added
                self.assertIn("Alice", self.module.checkins)
                # Check broadcast welcome message sent (1 check-in -> "1 user has")
                self.conn_manager.execute.assert_called_with(["chan", "1", "Welcome Alice, 1 user has checked in."])
                
                # Verify persisted state file exists and shows active check-in
                self.assertTrue(os.path.exists(self.test_state_file))
                with open(self.test_state_file, 'r') as f:
                    data = json.load(f)
                    self.assertTrue(data["is_active"])
                    self.assertEqual(data["checkins"], ["Alice"])
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_handle_message_grammar_plural(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.is_active = True
            self.module.channel_idx = 1
            self.module.checkins = ["Alice"]
            
            async def run_test():
                await self.module._handle_message_async("Bob", "#checkin", 1)
                
                self.assertEqual(self.module.checkins, ["Alice", "Bob"])
                # Check broadcast welcome message sent (2 check-ins -> "2 users have")
                self.conn_manager.execute.assert_called_with(["chan", "1", "Welcome Bob, 2 users have checked in."])
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_handle_message_deduplication(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.is_active = True
            self.module.channel_idx = 1
            self.module.checkins = ["Alice"]
            
            async def run_test():
                await self.module._handle_message_async("Alice", "#checkin again", 1)
                
                # Check checkins count remains 1
                self.assertEqual(len(self.module.checkins), 1)
                # Check no new reply sent
                self.conn_manager.execute.assert_not_called()
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_on_net_start(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 1
            self.module.checkins = ["OldUser"]
            
            async def run_test():
                await self.module._on_net_start()
                
                self.assertTrue(self.module.is_active)
                self.assertIsNotNone(self.module.net_start_time)
                # Checkins list should be cleared on start
                self.assertEqual(self.module.checkins, [])
                
                # Check welcome message sent
                self.conn_manager.execute.assert_called_with(["chan", "1", "Welcome to the weekly #net. Please respond with #checkin to checkin."])
                
                # End net task should be scheduled
                self.assertIsNotNone(self.module.net_end_task)
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_reminders(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 1
            
            async def run_test():
                await self.module._on_1pm_reminder()
                self.conn_manager.execute.assert_called_with(["chan", "1", "Reminder: The weekly #net is today at 19:00."])
                
                await self.module._on_30m_reminder()
                self.conn_manager.execute.assert_called_with(["chan", "1", "Reminder: The weekly #net starts in 30 minutes at 19:00."])
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_end_net(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.is_active = True
            self.module.channel_idx = 1
            self.module.checkins = ["Alice", "Bob"]
            
            async def run_test():
                await self.module._end_net()
                
                self.assertFalse(self.module.is_active)
                self.assertEqual(self.module.checkins, [])
                # Final broadcast sent
                self.conn_manager.execute.assert_called_with(["chan", "1", "Thank you for joining. 2 users checkedin today."])
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_resume_net_active_and_valid(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.api.request_channel = AsyncMock(return_value=1)
            self.module.init(self.api, self.config)
            
            # Setup active state started 30 mins (1800s) ago
            start_time = (datetime.now(ZoneInfo("UTC")) - timedelta(minutes=30)).isoformat()
            state_data = {
                "is_active": True,
                "net_start_time": start_time,
                "checkins": ["Alice"]
            }
            with open(self.test_state_file, 'w') as f:
                json.dump(state_data, f)
                
            loop.run_until_complete(self.module.start())
            
            self.assertTrue(self.module.is_active)
            self.assertEqual(self.module.checkins, ["Alice"])
            # End net task should be scheduled (remaining time ~ 1800s)
            self.assertIsNotNone(self.module.net_end_task)
        finally:
            loop.close()

    def test_resume_net_expired(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.api.request_channel = AsyncMock(return_value=1)
            self.module.init(self.api, self.config)
            
            # Setup active state started 2 hours ago (expired)
            start_time = (datetime.now(ZoneInfo("UTC")) - timedelta(hours=2)).isoformat()
            state_data = {
                "is_active": True,
                "net_start_time": start_time,
                "checkins": ["Alice"]
            }
            with open(self.test_state_file, 'w') as f:
                json.dump(state_data, f)
                
            loop.run_until_complete(self.module.start())
            
            # Should end the net immediately, resetting is_active to False
            self.assertFalse(self.module.is_active)
            self.assertEqual(self.module.checkins, [])
            # Final message sent
            self.conn_manager.execute.assert_called_with(["chan", "1", "Thank you for joining. 1 user checkedin today."])
        finally:
            loop.close()

if __name__ == '__main__':
    unittest.main()
