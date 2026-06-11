import os
import re
import time
import math
import logging
import asyncio
import requests
from datetime import datetime

logger = logging.getLogger("WeatherBot")

# Emojis mapping for short, high-density LoRa transmission
WMO_EMOJIS = {
    0: "☀️",          # Clear sky
    1: "🌤️",          # Mainly clear
    2: "⛅",          # Partly cloudy
    3: "☁️",          # Overcast
    45: "🌫️",         # Fog
    48: "🌫️",         # Depositing rime fog
    51: "🌧️",         # Light drizzle
    53: "🌧️",         # Moderate drizzle
    55: "🌧️",         # Dense drizzle
    56: "🌧️",         # Light freezing drizzle
    57: "🌧️",         # Dense freezing drizzle
    61: "🌧️",         # Slight rain
    63: "🌧️",         # Moderate rain
    65: "🌧️",         # Heavy rain
    66: "🌧️",         # Light freezing rain
    67: "🌧️",         # Heavy freezing rain
    71: "❄️",         # Slight snow fall
    73: "❄️",         # Moderate snow fall
    75: "❄️",         # Heavy snow fall
    77: "❄️",         # Snow grains
    80: "🌧️",         # Slight rain showers
    81: "🌧️",         # Moderate rain showers
    82: "🌧️",         # Violent rain showers
    85: "❄️",         # Slight snow showers
    86: "❄️",         # Heavy snow showers
    95: "⛈️",         # Thunderstorm
    96: "⛈️",         # Thunderstorm with slight hail
    99: "⛈️",         # Thunderstorm with heavy hail
}

def degrees_to_compass8(degrees):
    directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
    index = round(degrees / 45) % 8
    return directions[index]

def calculate_heading_and_distance(my_lat, my_lon, target_lat, target_lon):
    R = 6371.0  # Earth radius in km
    
    lat1_rad = math.radians(my_lat)
    lon1_rad = math.radians(my_lon)
    lat2_rad = math.radians(target_lat)
    lon2_rad = math.radians(target_lon)
    
    d_lat = lat2_rad - lat1_rad
    d_lon = lon2_rad - lon1_rad
    
    a = (math.sin(d_lat / 2.0) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) * (math.sin(d_lon / 2.0) ** 2))
         
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    distance = R * c
    
    y = math.sin(d_lon) * math.cos(lat2_rad)
    x = (math.cos(lat1_rad) * math.sin(lat2_rad) -
         math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(d_lon))
         
    bearing_rad = math.atan2(y, x)
    heading = (math.degrees(bearing_rad) + 360.0) % 360.0
    
    return {
        "heading": degrees_to_compass8(heading),
        "distance": distance
    }

def reverse_geocode(lat, lon, user_agent):
    url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}"
    headers = {"User-Agent": user_agent}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if "error" in data:
                return ""
            address = data.get("address", {})
            location = ""
            
            if "village" in address:
                location += f"{address['village']}, "
            elif "town" in address:
                location += f"{address['town']}, "
            elif "city" in address:
                location += f"{address['city']}, "
                
            if "municipality" in address:
                location += f"{address['municipality']}, "
            if "state" in address:
                location += f"{address['state']}, "
            if "country" in address:
                location += f"{address['country']}"
                
            return re.sub(r',\s*$', '', location)
    except Exception as e:
        logger.error(f"Geocoding failed: {e}")
    return ""

async def resolve_zip(zipcode, user_agent):
    # 1. Zippopotam.us
    try:
        url = f"https://api.zippopotam.us/us/{zipcode}"
        def fetch():
            res = requests.get(url, headers={"User-Agent": user_agent}, timeout=5)
            if res.status_code == 200:
                return res.json()
            return None
        data = await asyncio.to_thread(fetch)
        if data and "places" in data and len(data["places"]) > 0:
            place = data["places"][0]
            lat = float(place["latitude"])
            lon = float(place["longitude"])
            display_name = f"{place['place name']}, {place['state abbreviation']}"
            return {"lat": lat, "lon": lon, "displayName": display_name}
    except Exception as e:
        logger.warning(f"Zippopotam lookup failed for ZIP {zipcode}: {e}")

    # 2. OSM Nominatim fallback
    try:
        url = f"https://nominatim.openstreetmap.org/search?postalcode={zipcode}&country=US&format=json"
        def fetch():
            res = requests.get(url, headers={"User-Agent": user_agent}, timeout=5)
            if res.status_code == 200:
                return res.json()
            return None
        data = await asyncio.to_thread(fetch)
        if data and len(data) > 0:
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            name_parts = data[0]["display_name"].split(",")
            city = name_parts[0].strip() if len(name_parts) > 0 else ""
            state = ""
            if len(name_parts) > 2:
                state = name_parts[2].strip()
            elif len(name_parts) > 1:
                state = name_parts[1].strip()
            display_name = f"{city}, {state}".strip(", ")
            return {"lat": lat, "lon": lon, "displayName": display_name}
    except Exception as e:
        logger.error(f"Nominatim lookup failed for ZIP {zipcode}: {e}")
        
    return None

async def fetch_nws(url, user_agent):
    try:
        def fetch():
            headers = {
                "User-Agent": user_agent,
                "Accept": "application/geo+json"
            }
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                return res.json()
            else:
                logger.warning(f"NWS fetch returned status {res.status_code} for {url}")
                return None
        return await asyncio.to_thread(fetch)
    except Exception as e:
        logger.error(f"Error fetching NWS URL {url}: {e}")
        return None

def get_emoji_for_forecast(forecast_text):
    text = (forecast_text or '').lower()
    if 'thunder' in text or 'storm' in text:
        return '⛈️'
    if any(x in text for x in ['snow', 'ice', 'sleet', 'freeze', 'flurry']):
        return '❄️'
    if any(x in text for x in ['rain', 'shower', 'drizzle']):
        return '🌧️'
    if any(x in text for x in ['fog', 'mist', 'haze']):
        return '🌫️'
    if any(x in text for x in ['wind', 'breezy', 'windy']):
        return '💨'
    if any(x in text for x in ['mostly sunny', 'partly sunny', 'mostly clear', 'partly cloudy']):
        return '🌤️'
    if any(x in text for x in ['sunny', 'clear']):
        return '☀️'
    if any(x in text for x in ['cloud', 'overcast', 'gloomy']):
        return '☁️'
    return '⛅'

def format_compressed_forecast(zipcode, periods):
    groups = {}
    for p in periods:
        start_time = p.get("startTime", "")
        if not start_time:
            continue
        date_str = start_time[:10]  # "YYYY-MM-DD"
        if date_str not in groups:
            groups[date_str] = {
                "dateStr": date_str,
                "daytime": None,
                "nighttime": None
            }
        group = groups[date_str]
        if p.get("isDaytime"):
            group["daytime"] = p
        else:
            group["nighttime"] = p

    # Sort groups by dateStr ascending and take the first 3
    sorted_groups = [groups[k] for k in sorted(groups.keys())][:3]

    header = f"Wx {zipcode if zipcode else ''}:\n"
    weekdays = ["Sun", "Mon", "Tue", "Wed", "Thur", "Fri", "Sat"]

    lines = []
    for index, group in enumerate(sorted_groups):
        if index == 0:
            label = "today"
        else:
            try:
                dt = datetime.strptime(group["dateStr"], "%Y-%m-%d")
                label = weekdays[(dt.weekday() + 1) % 7]
            except Exception:
                label = group["dateStr"]

        rep_period = group["daytime"] or group["nighttime"]
        forecast_text = rep_period.get("shortForecast", "") if rep_period else ""
        emoji = get_emoji_for_forecast(forecast_text)

        parts = []
        if group["daytime"]:
            parts.append(f"hi: {group['daytime'].get('temperature', 0)}")
        if group["nighttime"]:
            parts.append(f"low: {group['nighttime'].get('temperature', 0)}")

        lines.append(f"{label}: {emoji} {' '.join(parts)}")

    return header + '\n'.join(lines)

def format_iso_date(date_str):
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return date_str

def shorten_to_bytes(text, max_bytes):
    if not isinstance(text, str) or max_bytes < 0:
        return ""
        
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
        
    truncated = encoded[:max_bytes]
    decoded = truncated.decode('utf-8', errors='ignore')
    
    while len(decoded.encode('utf-8')) > max_bytes:
        decoded = decoded[:-1]
        
    match = re.match(r'^(.*)\s', decoded, re.DOTALL)
    if match and match.group(1):
        return match.group(1)
    return ""

def split_string_to_byte_chunks(text, max_bytes):
    if not isinstance(text, str) or max_bytes <= 0:
        return []
        
    chunks = []
    remaining = text.strip()
    
    while len(remaining) > 0:
        encoded = remaining.encode('utf-8')
        if len(encoded) <= max_bytes:
            chunks.append(remaining)
            break
            
        truncated_bytes = encoded[:max_bytes]
        candidate = truncated_bytes.decode('utf-8', errors='ignore')
        
        while len(candidate.encode('utf-8')) > max_bytes:
            candidate = candidate[:-1]
            
        if len(candidate) == 0:
            break
            
        split_idx = -1
        match_sentence = re.search(r'^(.*[.?!])\s', candidate, re.DOTALL)
        if match_sentence:
            split_idx = len(match_sentence.group(1))
        else:
            match_space = re.search(r'^(.*)\s', candidate, re.DOTALL)
            if match_space:
                split_idx = len(match_space.group(1))
                
        if split_idx > 0:
            final_chunk = remaining[:split_idx]
        else:
            final_chunk = candidate
            
        chunks.append(final_chunk.strip())
        remaining = remaining[len(final_chunk):].strip()
        
    return chunks

class WeatherBot:
    def __init__(self):
        self.name = "weather_bot"
        self.api = None
        self.config = {}
        
        self.subscriptions_file = "config/weather_subscriptions.json"
        
        # Default config settings
        self.weather_alarm = "06:00"
        self.user_agent = "MeshCoreWeatherBot/1.1.0 (contact@example.com)"
        self.zip_code = "20001"
        self.my_position = {"lat": 38.9072, "lon": -77.0369}
        self.channel_names = {"alerts": "weather", "weather": "weather"}
        self.timers = {"blitzCollection": 600, "meteoAlerts": 600}
        self.blitz_radius_miles = 10
        self.compas_names = {
            "N": "North", "NE": "North-East", "E": "East", "SE": "South-East",
            "S": "South", "SW": "South-West", "W": "West", "NW": "North-West"
        }
        self.config_meteo_alerts = {
            "enabled": True,
            "timeout": 180,
            "severityFilter": ["severe", "extreme"],
            "certaintyFilter": ["observed", "likely"],
            "messageTemplate": "{event} Alert for {region}\nEffective: {start} to {end}\nSeverity: {severity}\n{headline}",
            "severity": {
                "unknown": "Unknown", "minor": "Minor", "moderate": "Moderate",
                "severe": "Severe", "extreme": "Extreme"
            },
            "certainty": {
                "observed": "Observed", "likely": "Likely", "possible": "Possible",
                "unlikely": "Unlikely", "unknown": "Unknown"
            }
        }
        
        # Internal Cache/State variables
        self.blitz_area = None
        self.cached_forecast_url = None
        self.location_name = ""
        self.timezone = "UTC"
        self.seen_blitz = {}
        self.blitz_buffer = []
        self.meteo_alerts = {}
        self.geo_cache = {}
        
        # Lifecycles
        self.unsubscribe_msg = None
        self.unschedule_alarm = None
        self._mqtt_client = None
        self._is_running = False
        self._blitz_task = None
        self._meteo_task = None
        
        # Schema matching JSON schema draft-07
        self.config_schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "zipCode": {"type": "string"},
                "weatherAlarm": {"type": "string"},
                "channels": {
                    "type": "object",
                    "properties": {
                        "alerts": {"type": "string"},
                        "weather": {"type": "string"}
                    }
                },
                "blitzRadiusMiles": {"type": "number"},
                "meteoAlerts": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "timeout": {"type": "integer"},
                        "severityFilter": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "certaintyFilter": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    }
                }
            },
            "required": ["enabled"]
        }

    def run_config(self, current_config):
        """
        Interactive configuration wizard for the WeatherBot module.
        Prompts the user for key settings.
        """
        config = dict(current_config) if current_config else {}
        
        print("\n--- Configure Weather Bot Settings ---")
        
        # 1. Zip Code
        current_zip = config.get("zipCode", "20001")
        val = input(f"Enter Default Zip Code [current: {current_zip}]: ").strip()
        if val:
            config["zipCode"] = val
            
        # 2. Daily Alarm Time
        current_alarm = config.get("weatherAlarm", "06:00")
        val = input(f"Enter Daily Forecast Alarm Time (HH:MM) [current: {current_alarm}]: ").strip()
        if val:
            config["weatherAlarm"] = val
            
        # 3. Channels config
        channels = config.get("channels", {})
        alerts_ch = channels.get("alerts", "weather")
        val = input(f"Enter Channel Name/Index for weather alerts [current: {alerts_ch}]: ").strip()
        if val:
            channels["alerts"] = val
            
        weather_ch = channels.get("weather", "weather")
        val = input(f"Enter Channel Name/Index for daily forecasts [current: {weather_ch}]: ").strip()
        if val:
            channels["weather"] = val
        config["channels"] = channels
        
        # 4. Lightning warning radius
        current_radius = config.get("blitzRadiusMiles", 10)
        val = input(f"Enter Lightning Alert Radius (miles) [current: {current_radius}]: ").strip()
        if val:
            try:
                config["blitzRadiusMiles"] = float(val)
            except ValueError:
                print("Invalid number, keeping current radius.")
                
        # 5. Meteo alerts enabled
        meteo = config.get("meteoAlerts", {})
        current_meteo_enabled = meteo.get("enabled", True)
        val = input(f"Enable Severe Weather (NWS) Alerts? (y/n) [current: {'y' if current_meteo_enabled else 'n'}]: ").strip().lower()
        if val:
            meteo["enabled"] = val in ("y", "yes", "true")
            config["meteoAlerts"] = meteo
            
        return config

    def init(self, api, config):
        self.api = api
        self.config = config
        
        # Overwrite defaults if present in config
        self.weather_alarm = config.get("weatherAlarm", self.weather_alarm)
        self.user_agent = config.get("userAgent", self.user_agent)
        self.zip_code = str(config.get("zipCode", self.zip_code)).strip()
        
        if "myPosition" in config:
            self.my_position = config["myPosition"]
            
        if "channels" in config:
            self.channel_names.update(config["channels"])
            
        if "timers" in config:
            t = config["timers"]
            if "blitzCollection" in t:
                self.timers["blitzCollection"] = int(t["blitzCollection"] / 1000)
            if "meteoAlerts" in t:
                self.timers["meteoAlerts"] = int(t["meteoAlerts"] / 1000)
                
        self.blitz_radius_miles = config.get("blitzRadiusMiles", self.blitz_radius_miles)
        
        if "meteoAlerts" in config:
            self.config_meteo_alerts.update(config["meteoAlerts"])
            
        # Register requested channels with the main app
        requested = list(self.channel_names.values())
        api.declare_channels(requested)

        logger.info(f"[{self.name}] Initialized.")

    async def start(self):
        logger.info(f"[{self.name}] Starting lifecycle tasks...")
        self._is_running = True
        
        # 1. Resolve configured main zip code
        if self.zip_code:
            try:
                logger.info(f"[{self.name}] Resolving default ZIP code '{self.zip_code}'...")
                res = await resolve_zip(self.zip_code, self.user_agent)
                if res:
                    self.my_position = {"lat": res["lat"], "lon": res["lon"]}
                    self.location_name = res["displayName"]
                    logger.info(f"[{self.name}] Default ZIP geocoded to {self.my_position} ({self.location_name})")
            except Exception as e:
                logger.error(f"[{self.name}] Startup geocoding failed: {e}")
                
        # 2. Bounding Box calculations for lightning warnings
        if self.my_position and self.blitz_radius_miles:
            lat = self.my_position["lat"]
            lon = self.my_position["lon"]
            radius = self.blitz_radius_miles
            lat_offset = radius / 69.0
            lon_offset = radius / (69.0 * math.cos(math.radians(lat)))
            
            self.blitz_area = {
                "minLat": lat - lat_offset,
                "maxLat": lat + lat_offset,
                "minLon": lon - lon_offset,
                "maxLon": lon + lon_offset
            }
            logger.info(f"[{self.name}] Calculated lightning bounding box: {self.blitz_area}")
            
        # 3. Resolve NWS Points (timeZone and forecast URL)
        if self.my_position:
            try:
                points_url = f"https://api.weather.gov/points/{self.my_position['lat']},{self.my_position['lon']}"
                points_data = await fetch_nws(points_url, self.user_agent)
                if points_data:
                    self.cached_forecast_url = points_data.get("properties", {}).get("forecast")
                    self.timezone = points_data.get("properties", {}).get("timeZone", "UTC")
                    logger.info(f"[{self.name}] NWS timezone resolved: {self.timezone}, forecast URL: {self.cached_forecast_url}")
            except Exception as e:
                logger.error(f"[{self.name}] Failed NWS points metadata resolution: {e}")
                
        # 4. Connect to Blitzortung lightning notifications
        if self.blitz_area:
            self._mqtt_client = await self.register_blitzortung_mqtt(self.blitz_area)
            
        # 5. Subscribe to incoming messages
        self.unsubscribe_msg = self.api.subscribe("message", self._on_message)
        
        # 6. Schedule daily forecast alarm
        try:
            h, m = self.weather_alarm.split(":")
            cron_expr = f"{int(m)} {int(h)} * * *"
        except Exception:
            cron_expr = "0 6 * * *"
        self.unschedule_alarm = self.api.schedule_task(cron_expr, self.send_weather)
        logger.info(f"[{self.name}] Daily weather alarm scheduled at {cron_expr}")
        
        # 7. Start background loops
        self._blitz_task = asyncio.create_task(self._blitz_warning_loop())
        if self.config_meteo_alerts.get("enabled"):
            self._meteo_task = asyncio.create_task(self._meteo_alert_loop())
            
        logger.info(f"[{self.name}] Started successfully.")

    async def stop(self):
        logger.info(f"[{self.name}] Stopping module...")
        self._is_running = False
        
        if self.unsubscribe_msg:
            self.unsubscribe_msg()
            
        if self.unschedule_alarm:
            self.unschedule_alarm()
            
        if self._mqtt_client:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception as e:
                logger.error(f"[{self.name}] Error cleaning up MQTT: {e}")
                
        if self._blitz_task:
            self._blitz_task.cancel()
        if self._meteo_task:
            self._meteo_task.cancel()
            
        logger.info(f"[{self.name}] Stopped successfully.")

    def read_subscriptions(self):
        if not os.path.exists(self.subscriptions_file):
            return {}
        try:
            import json
            with open(self.subscriptions_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[{self.name}] Error reading subscriptions file: {e}")
            return {}

    def write_subscriptions(self, subs):
        try:
            import json
            os.makedirs(os.path.dirname(self.subscriptions_file), exist_ok=True)
            with open(self.subscriptions_file, 'w', encoding='utf-8') as f:
                json.dump(subs, f, indent=2)
        except Exception as e:
            logger.error(f"[{self.name}] Error writing subscriptions file: {e}")

    def add_subscription(self, sender, zipcode, display_name, lat, lon, forecast_url):
        subs = self.read_subscriptions()
        subs[sender] = {
            "publicKeyHex": sender,
            "zipCode": zipcode,
            "displayName": display_name,
            "lat": lat,
            "lon": lon,
            "forecastUrl": forecast_url,
            "subscribedAt": int(time.time() * 1000)
        }
        self.write_subscriptions(subs)

    def remove_subscription(self, sender):
        subs = self.read_subscriptions()
        if sender in subs:
            del subs[sender]
            self.write_subscriptions(subs)
            return True
        return False

    def _on_message(self, data):
        sender = data.get("sender", "unknown")
        text = data.get("text", "").strip()
        channel = data.get("channel", 0)
        
        # Prevent self loops
        mc = self.api.bot.connection_manager.mc
        if mc and mc.self_info:
            self_name = mc.self_info.get("name")
            if self_name and sender == self_name:
                return
                
        if not text:
            return
            
        asyncio.create_task(self._handle_message_async(sender, text, channel))

    async def _handle_message_async(self, sender, text, channel):
        # Strip sender prefixes (e.g., "Dhovin: 76246" -> "76246")
        clean_text = re.sub(r'^[A-Za-z0-9_.-]+:\s+', '', text).strip()
        lower_text = clean_text.lower()
        
        is_dm = (channel == 0 or channel is None)
        
        # Version / Info commands
        if lower_text in ("version", "info"):
            await self._reply(sender, channel, f"US WeatherBot v1.1.0 (Python port)")
            return
            
        # 1. Handle Subscription Commands (DM only)
        if lower_text.startswith("subscribe"):
            if not is_dm:
                await self._reply(sender, channel, "Error: Subscriptions must be requested via direct message.")
                return
            match = re.match(r'^subscribe\s+(\d{5})$', clean_text, re.IGNORECASE)
            if not match:
                await self._reply(sender, channel, "Usage: subscribe [5-digit zip code]")
                return
            zipcode = match.group(1)
            try:
                res = await resolve_zip(zipcode, self.user_agent)
                if not res:
                    await self._reply(sender, channel, f"Error: Could not resolve ZIP code {zipcode}. Subscription failed.")
                    return
                # Fetch NWS Points Metadata
                points_url = f"https://api.weather.gov/points/{res['lat']},{res['lon']}"
                points_data = await fetch_nws(points_url, self.user_agent)
                if not points_data:
                    await self._reply(sender, channel, f"Error: Failed NWS points metadata lookup for {zipcode}. Subscription failed.")
                    return
                forecast_url = points_data.get("properties", {}).get("forecast")
                
                self.add_subscription(sender, zipcode, res["displayName"], res["lat"], res["lon"], forecast_url)
                await self._reply(sender, channel, f"Subscribed! You will receive daily forecasts for {res['displayName']} ({zipcode}) every day at {self.weather_alarm} local time.")
            except Exception as e:
                logger.error(f"Subscription failed for ZIP {zipcode}: {e}")
                await self._reply(sender, channel, f"Error: Geocoding ZIP code {zipcode} failed.")
            return

        if lower_text == "unsubscribe":
            if not is_dm:
                await self._reply(sender, channel, "Error: Subscriptions must be managed via direct message.")
                return
            removed = self.remove_subscription(sender)
            if removed:
                await self._reply(sender, channel, "Unsubscribed. You will no longer receive daily forecasts.")
            else:
                await self._reply(sender, channel, "You do not have an active subscription.")
            return
            
        # 2. Interactive weather requests
        zipcode = None
        if re.match(r'^\d{5}$', clean_text):
            zipcode = clean_text
        else:
            match = re.match(r'^[!/#]?(weather|wx)\s+(\d{5})$', clean_text, re.IGNORECASE)
            if match:
                zipcode = match.group(2)
                
        if not zipcode:
            return
            
        # If we are on channel, ensure it matches weather channel index
        weather_channel_name = self.channel_names.get("weather", "weather")
        idx = await self._get_channel_idx(weather_channel_name)
        if not is_dm:
            is_weather_channel = False
            if idx is not None:
                if isinstance(channel, int) and channel == idx:
                    is_weather_channel = True
                elif isinstance(channel, str):
                    if channel.isdigit() and int(channel) == idx:
                        is_weather_channel = True
                    elif channel.lower() == weather_channel_name.lower():
                        is_weather_channel = True
                    elif channel.lower().lstrip('#') == weather_channel_name.lower().lstrip('#'):
                        is_weather_channel = True
            if not is_weather_channel:
                return
                
        logger.info(f"[{self.name}] Processing weather request for ZIP: {zipcode}")
        try:
            res = await resolve_zip(zipcode, self.user_agent)
            if not res:
                await self._reply(sender, channel, f"Error: Could not resolve ZIP code {zipcode}.")
                return
                
            points_url = f"https://api.weather.gov/points/{res['lat']},{res['lon']}"
            points_data = await fetch_nws(points_url, self.user_agent)
            if not points_data:
                await self._reply(sender, channel, f"Error: NWS metadata not resolved for {zipcode}.")
                return
            forecast_url = points_data.get("properties", {}).get("forecast")
            
            forecast_data = await fetch_nws(forecast_url, self.user_agent)
            if not forecast_data:
                await self._reply(sender, channel, f"Error: NWS forecast fetch failed for {zipcode}.")
                return
                
            periods = forecast_data.get("properties", {}).get("periods", [])
            if not periods:
                await self._reply(sender, channel, f"Error: No periods found in NWS forecast for {zipcode}.")
                return
                
            forecast_text = format_compressed_forecast(zipcode, periods)
            chunks = split_string_to_byte_chunks(forecast_text, 130)
            
            for chunk in chunks:
                await self._reply(sender, channel, chunk)
                await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error handling weather query for ZIP {zipcode}: {e}", exc_info=True)
            await self._reply(sender, channel, f"Error retrieving weather for ZIP {zipcode}. Try again later.")

    async def _reply(self, sender, channel, text):
        weather_channel_name = self.channel_names.get("weather", "weather")
        idx = await self._get_channel_idx(weather_channel_name)
        
        is_weather_channel = False
        if idx is not None:
            if isinstance(channel, int) and channel == idx:
                is_weather_channel = True
            elif isinstance(channel, str):
                if channel.isdigit() and int(channel) == idx:
                    is_weather_channel = True
                elif channel.lower() == weather_channel_name.lower():
                    is_weather_channel = True
                elif channel.lower().lstrip('#') == weather_channel_name.lower().lstrip('#'):
                    is_weather_channel = True
                
        if is_weather_channel:
            await self.api.bot.connection_manager.execute(["chan", str(idx), text])
        else:
            await self.api.bot.connection_manager.execute(["msg", sender, text])

    async def _get_channel_idx(self, channel_arg):
        if not self.api:
            return None
        try:
            return await self.api.request_channel(channel_arg)
        except Exception as e:
            logger.error(f"Error requesting channel '{channel_arg}': {e}")
            return None

    async def geocode_cached(self, key, lat, lon):
        if key in self.geo_cache:
            return self.geo_cache[key]
            
        def run_geocode():
            return reverse_geocode(lat, lon, self.user_agent)
            
        location = await asyncio.to_thread(run_geocode)
        if location:
            self.geo_cache[key] = location
        return location

    async def register_blitzortung_mqtt(self, blitz_area):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.warning(f"[{self.name}] 'paho-mqtt' package not found. Lightning warnings are disabled.")
            return None
            
        try:
            logger.info(f"[{self.name}] Initializing Blitzortung MQTT client...")
            client = mqtt.Client()
            
            def on_connect(client, userdata, flags, rc):
                if rc == 0:
                    logger.info(f"[{self.name}] Blitzortung MQTT connected successfully.")
                    client.subscribe("blitzortung/1.1/#")
                else:
                    logger.warning(f"[{self.name}] Blitzortung MQTT connection failed, code: {rc}")
                    
            def on_message(client, userdata, msg):
                try:
                    import json
                    payload = json.loads(msg.payload.decode('utf-8'))
                    lat = float(payload.get("lat", 0.0))
                    lon = float(payload.get("lon", 0.0))
                    
                    if lat < blitz_area["minLat"] or lat > blitz_area["maxLat"] or lon < blitz_area["minLon"] or lon > blitz_area["maxLon"]:
                        return
                        
                    blitz = calculate_heading_and_distance(
                        self.my_position["lat"], self.my_position["lon"], lat, lon
                    )
                    heading = blitz["heading"]
                    distance = blitz["distance"]
                    key = f"{heading}|{int(distance / 10)}"
                    
                    self.blitz_buffer.append({
                        "key": key,
                        "heading": heading,
                        "distance": distance,
                        "lat": lat,
                        "lon": lon
                    })
                except Exception as e:
                    logger.error(f"[{self.name}] Error handling lightning MQTT message: {e}")
                    
            client.on_connect = on_connect
            client.on_message = on_message
            
            client.connect("blitzortung.ha.sed.pl", 1883, 60)
            client.loop_start()
            return client
        except Exception as e:
            logger.error(f"[{self.name}] Failed to start MQTT client: {e}")
            return None

    async def blitz_warning(self):
        counter = {}
        for blitz in self.blitz_buffer:
            key = blitz["key"]
            counter[key] = counter.get(key, 0) + 1
            
        for key, count in list(counter.items()):
            if count < 10 or key in self.seen_blitz:
                continue
                
            data = None
            for b in self.blitz_buffer:
                if b["key"] == key:
                    data = b
                    break
            if not data:
                continue
                
            heading, distance = key.split("|")
            dist_val = int(distance)
            
            location = await self.geocode_cached(key, data["lat"], data["lon"])
            if not location:
                location = f"{data['lat']:.3f}, {data['lon']:.3f}"
                
            compass_dir = self.compas_names.get(heading, heading)
            alert_msg = f"🌩️ Lightning: {location} ({dist_val * 10}km {compass_dir})"
            
            await self.send_alert(alert_msg, "alerts")
            self.seen_blitz[key] = int(time.time() * 1000)
            
        self.blitz_buffer.clear()

    async def send_alert(self, message, channel_type):
        channel_name = self.channel_names.get(channel_type, "weather")
        idx = await self._get_channel_idx(channel_name)
        if idx is None:
            logger.warning(f"[{self.name}] Channel '{channel_name}' not found. Skipping alert.")
            return
            
        shortened = shorten_to_bytes(message, 155)
        
        logger.info(f"[{self.name}] Sending alert: {shortened}")
        await self.api.bot.connection_manager.execute(["chan", str(idx), shortened])
        await asyncio.sleep(30)

    async def _blitz_warning_loop(self):
        while self._is_running:
            try:
                await asyncio.sleep(self.timers.get("blitzCollection", 600))
                await self.blitz_warning()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.name}] Error in lightning collection loop: {e}", exc_info=True)

    async def check_meteo_alerts(self):
        timeout_ms = self.config_meteo_alerts.get("timeout", 180) * 60 * 1000
        now_ms = int(time.time() * 1000)
        
        expired = []
        for key, entry in list(self.meteo_alerts.items()):
            if entry["timestamp"] < now_ms - timeout_ms:
                expired.append(key)
        for key in expired:
            del self.meteo_alerts[key]
            
        try:
            lat = self.my_position.get("lat")
            lon = self.my_position.get("lon")
            if not lat or not lon:
                return
                
            url = f"https://api.weather.gov/alerts/active?point={lat},{lon}"
            headers = {"User-Agent": self.user_agent, "Accept": "application/geo+json"}
            
            def fetch():
                return requests.get(url, headers=headers, timeout=10)
            res = await asyncio.to_thread(fetch)
            if res.status_code != 200:
                logger.warning(f"[{self.name}] NWS active alerts fetch failed status: {res.status_code}")
                return
                
            data = res.json()
            features = data.get("features", [])
            
            active_ids = set()
            new_warnings = []
            
            sev_filter = [s.lower() for s in self.config_meteo_alerts.get("severityFilter", [])]
            cert_filter = [c.lower() for c in self.config_meteo_alerts.get("certaintyFilter", [])]
            
            for feature in features:
                props = feature.get("properties", {})
                if not props:
                    continue
                    
                fid = props.get("identifier") or feature.get("id")
                if fid:
                    active_ids.add(fid)
                    
                end_str = props.get("expires") or props.get("ends")
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str)
                        if end_dt.timestamp() < time.time():
                            continue
                    except Exception:
                        pass
                        
                severity = (props.get("severity") or "unknown").lower()
                certainty = (props.get("certainty") or "unknown").lower()
                
                if severity not in sev_filter or certainty not in cert_filter:
                    continue
                    
                if fid in self.meteo_alerts:
                    continue
                    
                new_warnings.append({
                    "id": fid,
                    "region": props.get("areaDesc") or "Unknown Area",
                    "event": props.get("event"),
                    "start": props.get("onset"),
                    "end": props.get("expires") or props.get("ends"),
                    "severity": severity,
                    "certainty": certainty,
                    "headline": props.get("headline") or "",
                    "instruction": props.get("instruction") or ""
                })
                
            if new_warnings:
                def get_start(w):
                    try:
                        return datetime.fromisoformat(w["start"])
                    except Exception:
                        return datetime.min
                new_warnings.sort(key=get_start)
                
                for item in new_warnings:
                    sev_mapped = self.config_meteo_alerts.get("severity", {}).get(item["severity"]) or item["severity"]
                    cert_mapped = self.config_meteo_alerts.get("certainty", {}).get(item["certainty"]) or item["certainty"]
                    
                    message = self.config_meteo_alerts["messageTemplate"].format(
                        event=item["event"],
                        region=item["region"],
                        start=format_iso_date(item["start"]),
                        end=format_iso_date(item["end"]),
                        severity=sev_mapped,
                        certainty=cert_mapped,
                        headline=item["headline"],
                        instruction=item["instruction"]
                    )
                    
                    await self.send_alert(message, "alerts")
                    self.meteo_alerts[item["id"]] = {
                        "timestamp": int(time.time() * 1000),
                        "event": item["event"],
                        "region": item["region"],
                        "cleared": False
                    }
                    await asyncio.sleep(30)
                    
            # Check cleared alerts
            for fid, cached in list(self.meteo_alerts.items()):
                if fid in active_ids or cached.get("cleared"):
                    continue
                    
                event = cached.get("event", "Weather Alert")
                region = cached.get("region", "Area")
                
                clear_msg = f"🟢 CLEAR: {event} has ended/been cleared for {region}."
                await self.send_alert(clear_msg, "alerts")
                
                cached["cleared"] = True
                cached["timestamp"] = int(time.time() * 1000)
                await asyncio.sleep(30)
                
        except Exception as e:
            logger.error(f"[{self.name}] Error checking NWS active alerts: {e}", exc_info=True)

    async def _meteo_alert_loop(self):
        while self._is_running:
            try:
                await self.check_meteo_alerts()
                await asyncio.sleep(self.timers.get("meteoAlerts", 600))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.name}] Error in active alerts loop: {e}", exc_info=True)

    async def send_weather(self):
        from zoneinfo import ZoneInfo
        from datetime import datetime
        system_tz = datetime.now().astimezone().tzinfo
        system_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            local_time = datetime.now(ZoneInfo(self.timezone)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            local_time = "Unknown"
        logger.info(
            f"[{self.name}] Triggering scheduled daily forecast alert. "
            f"Timezone Comparison -> System: {system_tz} (Time: {system_time}) | "
            f"Actual Local: {self.timezone} (Time: {local_time})"
        )

        logger.info(f"[{self.name}] Starting scheduled daily forecast alarms...")
        
        # 1. Main Channel Broadcast
        try:
            weather_text = await self.get_weather()
            chunks = split_string_to_byte_chunks(weather_text, 130)
            if chunks:
                channel_name = self.channel_names.get("weather", "weather")
                idx = await self._get_channel_idx(channel_name)
                if idx is None:
                    logger.warning(f"[{self.name}] Channel '{channel_name}' not found. Skipping daily forecast broadcast.")
                else:
                    for chunk in chunks:
                        await self.api.bot.connection_manager.execute(["chan", str(idx), chunk])
                        await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"[{self.name}] Main channel forecast broadcast failed: {e}")
            
        # 2. Direct Messages to Subscribers
        try:
            subs = self.read_subscriptions()
            sub_keys = list(subs.keys())
            if sub_keys:
                logger.info(f"[{self.name}] Dispatching daily forecasts to {len(sub_keys)} subscribers...")
                for key in sub_keys:
                    sub = subs[key]
                    await self.send_subscriber_forecast(sub)
                    await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"[{self.name}] Subscriber forecast dispatch failed: {e}")

    async def send_subscriber_forecast(self, sub):
        sender = sub["publicKeyHex"]
        try:
            logger.info(f"[{self.name}] Sending DM forecast to subscriber {sub['displayName']} ({sub['zipCode']})...")
            forecast_url = sub.get("forecastUrl")
            if not forecast_url:
                points_url = f"https://api.weather.gov/points/{sub['lat']},{sub['lon']}"
                points_data = await fetch_nws(points_url, self.user_agent)
                if points_data:
                    forecast_url = points_data.get("properties", {}).get("forecast")
                    
            if not forecast_url:
                return
                
            forecast_data = await fetch_nws(forecast_url, self.user_agent)
            if not forecast_data:
                return
                
            periods = forecast_data.get("properties", {}).get("periods", [])
            if not periods:
                return
                
            first_period = periods[0]
            synopsis = f"{first_period.get('name')}: {first_period.get('detailedForecast')}"
            synopsis_msg = shorten_to_bytes(synopsis, 145)
            
            forecast_text = format_compressed_forecast(sub["zipCode"], periods)
            
            # Send synopsis DM
            await self.api.bot.connection_manager.execute(["msg", sender, synopsis_msg])
            await asyncio.sleep(5)
            # Send 3-day forecast DM
            await self.api.bot.connection_manager.execute(["msg", sender, forecast_text])
            logger.info(f"[{self.name}] Subscriber forecast successfully sent to {sub['displayName']}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to dispatch subscriber forecast to {sender}: {e}")

    async def get_weather(self):
        try:
            if not self.cached_forecast_url:
                lat = self.my_position["lat"]
                lon = self.my_position["lon"]
                points_url = f"https://api.weather.gov/points/{lat},{lon}"
                points_data = await fetch_nws(points_url, self.user_agent)
                if points_data:
                    self.cached_forecast_url = points_data.get("properties", {}).get("forecast")
                    
            if not self.cached_forecast_url:
                return "Weather Forecast Unavailable: Points metadata unresolved."
                
            forecast_data = await fetch_nws(self.cached_forecast_url, self.user_agent)
            if not forecast_data:
                return "Weather Forecast Unavailable: NWS fetch failed."
                
            periods = forecast_data.get("properties", {}).get("periods", [])
            if not periods:
                return "No forecast periods available."
                
            return format_compressed_forecast(self.zip_code, periods)
        except Exception as e:
            logger.error(f"[{self.name}] getWeather failed: {e}")
            return f"Weather Forecast Unavailable: {e}"
