import unittest
from datetime import datetime
from modules.weather_bot import (
    get_emoji_for_forecast,
    format_compressed_forecast,
    split_string_to_byte_chunks,
    shorten_to_bytes,
    calculate_heading_and_distance,
    degrees_to_compass8
)

class TestWeatherBotUtils(unittest.TestCase):

    def test_degrees_to_compass8(self):
        self.assertEqual(degrees_to_compass8(0), 'N')
        self.assertEqual(degrees_to_compass8(360), 'N')
        self.assertEqual(degrees_to_compass8(45), 'NE')
        self.assertEqual(degrees_to_compass8(90), 'E')
        self.assertEqual(degrees_to_compass8(180), 'S')
        self.assertEqual(degrees_to_compass8(220), 'SW')
        self.assertEqual(degrees_to_compass8(270), 'W')
        self.assertEqual(degrees_to_compass8(315), 'NW')

    def test_calculate_heading_and_distance(self):
        # Justin, TX coordinates: 33.0906, -97.2911
        # Denton, TX coordinates: 33.2148, -97.1331
        res = calculate_heading_and_distance(33.0906, -97.2911, 33.2148, -97.1331)
        self.assertEqual(res["heading"], "NE")
        self.assertTrue(10.0 < res["distance"] < 25.0) # distance is around ~20.2km

    def test_get_emoji_for_forecast(self):
        self.assertEqual(get_emoji_for_forecast("Thunderstorms"), "⛈️")
        self.assertEqual(get_emoji_for_forecast("Heavy rain and wind"), "🌧️")
        self.assertEqual(get_emoji_for_forecast("Heavy Snow"), "❄️")
        self.assertEqual(get_emoji_for_forecast("Patchy Fog"), "🌫️")
        self.assertEqual(get_emoji_for_forecast("Windy and Sunny"), "💨")
        self.assertEqual(get_emoji_for_forecast("Mostly Sunny"), "🌤️")
        self.assertEqual(get_emoji_for_forecast("Sunny"), "☀️")
        self.assertEqual(get_emoji_for_forecast("Overcast"), "☁️")
        self.assertEqual(get_emoji_for_forecast("Random"), "⛅")

    def test_shorten_to_bytes(self):
        text = "Hello world! This is a long message."
        # Fits in limit
        self.assertEqual(shorten_to_bytes(text, 100), text)
        # Truncates at whitespace
        shortened = shorten_to_bytes(text, 20)
        self.assertTrue(len(shortened.encode('utf-8')) <= 20)
        self.assertEqual(shortened, "Hello world! This")

    def test_split_string_to_byte_chunks(self):
        text = "Line 1. Line 2. Line 3. Line 4."
        chunks = split_string_to_byte_chunks(text, 15)
        # Each chunk should be <= 15 bytes and split on sentence/space boundary
        for chunk in chunks:
            self.assertTrue(len(chunk.encode('utf-8')) <= 15)
        self.assertEqual(chunks[0], "Line 1.")
        self.assertEqual(chunks[1], "Line 2.")

    def test_format_compressed_forecast(self):
        periods = [
            {
                "startTime": "2026-06-10T08:00:00-05:00",
                "isDaytime": True,
                "temperature": 85,
                "shortForecast": "Mostly Sunny"
            },
            {
                "startTime": "2026-06-10T20:00:00-05:00",
                "isDaytime": False,
                "temperature": 69,
                "shortForecast": "Mostly Clear"
            },
            {
                "startTime": "2026-06-11T08:00:00-05:00",
                "isDaytime": True,
                "temperature": 82,
                "shortForecast": "Thunderstorms"
            },
            {
                "startTime": "2026-06-11T20:00:00-05:00",
                "isDaytime": False,
                "temperature": 69,
                "shortForecast": "Scattered Showers"
            },
            {
                "startTime": "2026-06-12T08:00:00-05:00",
                "isDaytime": True,
                "temperature": 86,
                "shortForecast": "Thunderstorms"
            },
            {
                "startTime": "2026-06-12T20:00:00-05:00",
                "isDaytime": False,
                "temperature": 74,
                "shortForecast": "Partly Cloudy"
            }
        ]
        
        forecast_str = format_compressed_forecast("76246", periods)
        self.assertTrue("Wx 76246:" in forecast_str)
        self.assertTrue("today: 🌤️ hi: 85 low: 69" in forecast_str)
        # 2026-06-11 is Thursday. Thursday is "Thur" in weekdays
        self.assertTrue("Thur: ⛈️ hi: 82 low: 69" in forecast_str)
        # 2026-06-12 is Friday. Friday is "Fri" in weekdays
        self.assertTrue("Fri: ⛈️ hi: 86 low: 74" in forecast_str)

if __name__ == '__main__':
    unittest.main()
