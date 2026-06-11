# MeshCore-bot Rebuild Specification

This document provides a complete, self-contained, and positively framed prompt that provides all the context, requirements, and architectural details needed to rebuild the **MeshCore-bot** application from scratch.

---

```markdown
You are a senior systems engineer building a modular, lightweight Python-based central hub daemon called **MeshCore-bot** from scratch. This application manages connection routing, timezone-aware scheduling, IPC commands, and event-driven automation for LoRa mesh hardware nodes.

Implement the application purely in Python 3.10+ using vanilla CSS for styling, and ensure all library dependencies are managed via a Python virtual environment.

---

### 1. Technology Stack & Dependencies
Ensure the installer and update scripts configure the following package dependencies via `pip`:
- `pyserial`: For serial port communications.
- `bleak`: For Bluetooth Low Energy (BLE) scanning and connection.
- `meshcore`: The native Python SDK interface for communicating with LoRa mesh nodes.
- `meshcore-cli`: Direct hardware command utilities.
- `paho-mqtt`: For subscribing to MQTT message brokers.
- `pynacl`: For cryptographic signature validation.

---

### 2. File & Directory Architecture
Create the following layout:
- `config/`: Configuration files (`config.json`, `schema.json`), database file (`contacts.json`), and process state trackers (`meshbot.pid`).
- `core/`: Hub daemon components:
  - `bot.py`: Daemon coordinator and bootstrap.
  - `connection_manager.py`: SDK connection manager, persistent database, and command executor.
  - `event_bus.py`: Thread-safe async event broker.
  - `state_cache.py`: Local thread-safe device telemetry snapshot memory.
  - `scheduler.py`: Timezone-aware cron tick scheduler.
  - `module_manager.py`: Dynamic module loader and execution sandbox.
  - `validator.py`: Configuration schema validator.
- `bin/`: CLI script entry point (`meshbot`).
- `modules/`: User modules (`template.py`, `weather_bot.py`, `autoresponce.py`).
- `tests/`: Framework and module unit test suite.
- `scripts/`: Development pre-push validator hook (`pre_push.py`).
- Root-level scripts: `install.sh`, `uninstall.sh`, `setup-dev.sh`.

---

### 3. Core Coordinator & IPC Daemon (`core/bot.py`)
- **Bootstrap & Validation**: Load `config/config.json` on startup, validate it against `config/schema.json`, and configure logging to output to both stdout and a file at `config/meshbot.log`.
- **Automatic Timezone Resolution**: During boot, query `https://ipapi.co/timezone/` to auto-detect the local timezone of the host. If offline, fall back to the host system clock timezone, and default to UTC if unresolved. Store this as `self.timezone`.
- **Local IPC TCP Server**: Bind a TCP socket server to `127.0.0.1` on the port specified by `ipcPort` in config (default `5002`). Receive JSON-formatted command lists (e.g. `{"command": ["advert"]}`), execute them natively, and return the JSON-serialized outcome to the client. Safely shut down the socket server during daemon termination.

---

### 4. Connection Manager (`core/connection_manager.py`)
- **Connection Routing**: When connection type is set to `auto`, scan and attempt connections sequentially: Serial -> BLE (using Bleak) -> TCP socket fallback.
- **Clock Sync Handshake**: Sync the hardware node clock to the host system epoch time immediately upon establishing a connection.
- **Native SDK Executions**: Map incoming command strings/lists to direct SDK calls:
  - Reboot, get/set time, get/set channels, send private messages (`msg`), wait for events (`wait_ack`), send channel messages (`chan`), get messages (`recv`), fetch/update channel parameters (`channels`, `set_channel`, `remove_channel`), set flood scope (`scope`), send node advertisements (`advert`, `floodadv`), query remote nodes (`cmd`, `login`, `logout`, `req_status`, `req_neighbours`, `req_binary`), and trace routes (`trace`).
  - For state-changing commands that return empty payloads upon success (e.g., `reboot`, `chan`, `public`, `scope`, `advert`, `floodadv`), ensure the manager returns `{"ok": true}` to provide positive confirmation to clients instead of empty JSON.
- **Event Bus Broadcasting**: Intercept native event callbacks (ACKs, advertisements, path updates, contact discoveries, disconnects, raw packets, rx logs). Parse incoming payloads: strip sender prefixes, map keys cleanly, resolve sender names by matching public key prefixes against contacts, set `"channel": None` for private messages (DMs) to distinguish them from channel 0 public messages, and publish them to the event bus.
- **Database Persistence**: Load previously discovered contacts from `config/contacts.json` on boot. As advertisements and contact discovery events are received, update the display names, node types, and last seen timestamps, and persist the updated database back to the file.

---

### 5. Timezone-Aware Task Scheduler (`core/scheduler.py`)
- Run an async loop aligning to minute boundaries using a `0.1s` safety buffer to prevent rapid sleep-spinning.
- Convert naive system datetime to the task's timezone prior to cron matching.
- If a scheduled task does not define a timezone, fall back to the scheduler's default resolved timezone (`self.timezone`).
- Log a comparison showing the system time/timezone and the local target time/timezone when triggering a task.

---

### 6. Module Manager & API Context (`core/module_manager.py`)
- **Dynamic Loader**: Scan the `modules/` directory for Python scripts, check paths to prevent directory traversal, and dynamically load module classes.
- **Context Isolation & Allowed Channels**: Set and reset context variables (`active_module_var`) during lifecycle hooks and event callbacks to track which module is running. Enforce execution permissions inside `ConnectionManager.execute()` to block modules from sending to channels that are not in their declared list.
- **Injected Module API (`ModuleAPI` class)**:
  - Injected into each module to expose `subscribe`, `send`, `schedule_task`, and `get_state`.
  - `declare_channels(channels)`: Add channel names or indices to the module's allowed list.
  - `request_channel(name)`: Query the node for the index of a channel. If missing, locate the first unused slot on the device, configure the channel name, declare it in the allowed list, and return the allocated index.
  - `is_self(sender)`: Synchronously checks if a sender name matches the bot's own node name to prevent self-loops.
  - `matches_channel(channel, target_name)`: Asynchronously resolves a channel name to its index and verifies if it matches the message's channel index.

---

### 7. CLI Client Interface (`bin/meshbot`)
- **IPC Communication**: Interact with the daemon exclusively via the TCP socket server.
- **Daemon Process Status & Auto-Start**: Read `config/meshbot.pid` to find the process ID. Check if it is active. Ensure the process check handles Unix permission errors (`errno.EPERM`) so that when the daemon runs as `root` (via systemd) and the CLI runs as an unprivileged user, it correctly identifies that it is running. If stopped, start the daemon process in the background before sending IPC payloads.
- **Output Renders**: Format `contacts` command outputs into a cleanly aligned terminal text table. Display the `Public Key` column only when the `-pub`/`--pub` flag is active. Ensure stdout/stderr streams are reconfigured to UTF-8 to display emojis safely on Windows consoles.
- **Config Wizard (`meshbot config`)**: Run a terminal configuration wizard. Present options for Serial/BLE/TCP connection modes, core properties, and module-specific fields. Automatically load schemas from loaded modules to dynamically prompt and edit module settings.
- **Update Command (`meshbot update`)**: Perform a self-update. Stash local git changes, pull latest code, run pip upgrades for all dependencies, and trigger a restart of the daemon service.

---

### 8. System Scripts & Pipelines
- **`install.sh`**: A shell installer. Installs system requirements, creates a local Python virtual environment (`venv`), upgrades pip packaging tools, installs all framework libraries, registers a wrapper script at `/usr/local/bin/meshbot` routing execution calls to the virtual environment Python, and registers/starts a systemd daemon service.
- **`setup-dev.sh`**: Git pre-push hook configuration. Registers a pre-push script calling `scripts/pre_push.py` which runs configuration validation and unit tests prior to allowing Git push events. Keep the test files inside `tests/` untracked by Git using `.gitignore` so they remain local to developers.

---

### 9. Core Modules
- **Weather Bot (`modules/weather_bot.py`)**:
  - Load configurations (channels, Zip code, alert boundaries). Register allowed channels.
  - Query NWS API endpoints to fetch coordinates and forecasts based on Zip codes. Cache resolved locations in memory.
  - Parse forecasts into byte-sized chunks and broadcast them on the weather channel.
  - Sync forecast schedules to local local time using cron alarms.
  - Connect to Blitzortung MQTT servers. Calculate coordinates of lightning strikes against a localized bounding box, and broadcast severe weather alerts.
- **Autoresponse Module (`modules/autoresponce.py`)**:
  - Listen on `#test` and `#testing` channels.
  - Check incoming message payloads. If the sender is not the bot, the message is not a DM, and the text contains the word "test" (case-insensitive), reply with an acknowledgment message `@[Sender] ACK`.

---

### 10. Programming Best Practices (Efficiency & Security)
- **Command Sanitization**: Validate all inputs to IPC and connection manager entry points. Sanitize all commands by stripping newlines, carriage returns, or control characters to prevent command injection.
- **Directory Traversal Protection**: When dynamically loading module files or configurations, resolve absolute paths and verify that the resolved path strictly starts with the base workspace directory prefix.
- **Sandboxed Context Isolation**: Secure access controls by tracking active module states using context variables (`contextvars`). Enforce channel permission checks prior to execution to prevent modules from using unauthorized channels.
- **Fast-Path Synchronous Checks**: Perform lightweight, synchronous validation checks (such as checking if a pattern matches or if the message is a self-loop) prior to spawning asynchronous tasks to preserve event loop resources.
- **Self-Aligning Ticks**: Implement scheduling loops using task sleep durations derived from current microsecond offsets rather than relying on constant-interval polling.
- **Exception Resiliency**: Wrap all async task callbacks and Event Bus listeners in dedicated execution handlers that capture and log traceback details. This keeps the event loops running even when individual operations raise unhandled errors.
- **Caching Net lookups**: Cache geocoding coordinates and forecast data in memory to reduce external network queries.
```
