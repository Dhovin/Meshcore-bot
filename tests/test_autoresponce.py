import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from modules.autoresponce import Autoresponce

class TestAutoresponceModule(unittest.TestCase):
    def setUp(self):
        self.module = Autoresponce()
        self.api = MagicMock()
        
        # Mock connection manager
        self.conn_manager = MagicMock()
        self.conn_manager.isConnected = True
        self.conn_manager.mc = MagicMock()
        self.conn_manager.mc.self_info = {"name": "TestBotDevice"}
        self.conn_manager.execute = AsyncMock(return_value={"ok": True})
        
        self.api.bot.connection_manager = self.conn_manager
        
        # Setup config
        self.config = {
            "enabled": True,
            "channels": ["#test", "#testing"]
        }

    def test_init(self):
        self.module.init(self.api, self.config)
        self.assertEqual(self.module.api, self.api)
        self.assertEqual(self.module.config, self.config)
        self.assertEqual(self.module.channels, ["#test", "#testing"])
        
        # Verify declare_channels is called with variations
        self.api.declare_channels.assert_called()
        declared = self.api.declare_channels.call_args[0][0]
        self.assertIn("#test", declared)
        self.assertIn("test", declared)
        self.assertIn("#testing", declared)
        self.assertIn("testing", declared)

    def test_start(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Mock request_channel to return dummy indices
            async def mock_request_channel(ch):
                if "testing" in ch:
                    return 2
                return 1
            self.api.request_channel = AsyncMock(side_effect=mock_request_channel)
            
            self.module.init(self.api, self.config)
            loop.run_until_complete(self.module.start())
            
            # Verify subscription
            self.api.subscribe.assert_called_with("message", self.module._on_message)
            
            # Verify indices mapped
            self.assertEqual(self.module.channel_indices["#test"], 1)
            self.assertEqual(self.module.channel_indices["test"], 1)
            self.assertEqual(self.module.channel_indices["#testing"], 2)
            self.assertEqual(self.module.channel_indices["testing"], 2)
        finally:
            loop.close()

    def test_on_message_ignores_non_matching(self):
        self.module.init(self.api, self.config)
        self.module.channel_indices = {"#test": 1, "test": 1}
        
        # Messages not containing "test" should be ignored
        with patch.object(self.module, "_send_reply", new_callable=AsyncMock) as mock_reply:
            self.module._on_message({"sender": "Alice", "text": "Hello world", "channel": 1})
            mock_reply.assert_not_called()

    def test_on_message_ignores_other_channels(self):
        self.module.init(self.api, self.config)
        self.module.channel_indices = {"#test": 1, "test": 1}
        
        # Message has "test" but on channel 99 (not monitored)
        with patch.object(self.module, "_send_reply", new_callable=AsyncMock) as mock_reply:
            self.module._on_message({"sender": "Alice", "text": "This is a test message", "channel": 99})
            mock_reply.assert_not_called()

    def test_on_message_ignores_direct_messages(self):
        self.module.init(self.api, self.config)
        self.module.channel_indices = {"#test": 1, "test": 1}
        
        # Direct Message (channel is None)
        with patch.object(self.module, "_send_reply", new_callable=AsyncMock) as mock_reply:
            self.module._on_message({"sender": "Alice", "text": "test", "channel": None})
            mock_reply.assert_not_called()

    def test_on_message_ignores_self_messages(self):
        self.module.init(self.api, self.config)
        self.module.channel_indices = {"#test": 1, "test": 1}
        
        # Message from self
        with patch.object(self.module, "_send_reply", new_callable=AsyncMock) as mock_reply:
            self.module._on_message({"sender": "TestBotDevice", "text": "test", "channel": 1})
            mock_reply.assert_not_called()

    def test_on_message_matches_variations(self):
        self.module.init(self.api, self.config)
        self.module.channel_indices = {"#test": 1, "test": 1, "#testing": 2, "testing": 2}
        
        variations = [
            "test",
            "Test",
            "TEST",
            "This is a test.",
            "test case",
            "TESTING"  # Contains 'test' word (Wait, does '\btest\b' match testing? No, but re.search(r'\btest\b') won't match testing, but re.search or simply "test" in text.lower() will. Let's make sure it matches!)
        ]
        
        for text in variations:
            with patch('asyncio.create_task') as mock_create_task:
                self.module._on_message({"sender": "Alice", "text": text, "channel": 1})
                mock_create_task.assert_called_once()

    def test_send_reply(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            loop.run_until_complete(self.module._send_reply("Alice", 1))
            self.conn_manager.execute.assert_called_with(["chan", "1", "@Alice ACK"])
        finally:
            loop.close()

if __name__ == '__main__':
    unittest.main()
