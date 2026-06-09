# MeshCore-bot Central Hub Framework

MeshCore-bot is a modular, secure, and cross-platform Python-based central hub for companion radio nodes running the Meshcore protocol. Rather than wrapping CLI subprocesses, MeshCore-bot connects natively to the official Python `meshcore` library. It translates incoming message telemetry and node diagnostics into structured JSON, which it broadcasts over an internal event bus, alongside providing a custom task scheduler, centralized read-only state cache, and dynamic plugin system.

---

## Key Features

1. **Native Hardware Link**: Imports the official Python `meshcore` library directly for native Serial/BLE/TCP communication, avoiding subprocess pipelines and pipe buffering latency.
2. **Auto-Discovery**: Scans serial ports (filtering for typical USB serial bridges), Bluetooth Low Energy nodes (names beginning with `MeshCore-`), or falls back to TCP.
3. **Cron Task Scheduler**: Features a custom, zero-dependency asyncio cron parser supporting standard 5-field cron expressions on self-aligning minute boundaries.
4. **Time Sync**: Automatically synchronizes the radio RTC clock on connection and periodically pushes time updates to prevent drift.
5. **Centralized State Cache**: Maintains a read-only store of telemetry (battery, uptime, neighbors) and returns deep-copied state dictionaries to prevent plugin mutation.
6. **Plugin System & Lifecycles**: Discovers, validates, and loads scripts from the `/modules` folder. Executes `init`, `start`, and `stop` lifecycle hooks.
7. **Graceful Shutdown**: Intercepts `SIGINT`/`SIGTERM` to safely close connections and halt plugins (enforcing a strict 10-second stop timeout limit).
8. **Command Validation**: Sanitizes command strings to block multi-line shell injections.

---

## Directory Structure

```
MeshCore-Bot/
├── bin/
│   └── meshbot             # Shebang-executable Python CLI & setup wizard
├── config/
│   ├── config.json         # Centralized configuration settings
│   ├── schema.json         # JSON Schema for config.json validation
│   └── meshbot.pid         # Process ID lockfile generated at startup
├── core/
│   ├── bot.py              # Main bot coordinator & bootstrapper
│   ├── connection_manager.py # Native meshcore library connector & auto-discovery
│   ├── event_bus.py        # Asynchronous sync/async event broker
│   ├── module_manager.py   # Dynamic importlib module loader & sandbox
│   ├── scheduler.py        # Custom asyncio-based cron scheduler
│   ├── state_cache.py      # Telemetry state store with deep copies
│   └── validator.py        # Zero-dependency JSON Schema validator
├── modules/
│   └── template.py         # Blueprint template for custom modules
├── scripts/
│   ├── pre_push.py         # Runs pre-push validation (tests & schema check)
│   └── validate_config.py  # Configuration schema validator script
├── tests/
│   └── test_framework.py   # Unittest framework test suite
├── install.sh              # Linux installation and systemd setup script
├── uninstall.sh            # Linux service removal script
├── setup-dev.sh            # Developer git repository configuration script
└── README.md
```

---

## Installation & Deployment

### Prerequisites

- **Python**: Version 3.10 or higher.
- **System Packages** (for BLE support): `bluez` and development build tools are recommended on Linux.

### Development Environment Setup

Initialize the repository, update origin remote, and register the git pre-push hook:
```bash
chmod +x setup-dev.sh
./setup-dev.sh
```

To run unit tests manually during development:
```bash
python -m unittest discover -s tests -p "*.py"
```

### Linux Deployment

#### Quick One-Liner Installation & Uninstall

To automatically download the code, clone it into your home directory (`~/Meshcore-bot`), set up the service, dependencies, virtual environment, and install the global `meshbot` CLI command with a single line:
```bash
curl -sSL https://raw.githubusercontent.com/Dhovin/Meshcore-bot/main/install.sh | bash
```

To completely stop services, wipe configuration and databases, and clean up the system-wide installation wrapper:
```bash
curl -sSL https://raw.githubusercontent.com/Dhovin/Meshcore-bot/main/uninstall.sh | bash
```

#### Manual Installation

Alternatively, if you already cloned the repository manually, you can execute the installer inside the repository directory:
```bash
chmod +x install.sh
./install.sh
```

To uninstall manually from within the repository directory:
```bash
chmod +x uninstall.sh
./uninstall.sh
```

---

## Command Line Interface (`meshbot`)

Once installed, the global `meshbot` tool is accessible (symlinked via wrapper to `/usr/local/bin/meshbot`):

- **Start Daemon**: Starts the daemon. Interoperates with systemd on Linux (`sudo systemctl start meshcore-bot`), and runs in the foreground on Windows.
  ```bash
  meshbot start
  ```
- **Stop Daemon**: Stops the running daemon process. Runs `sudo systemctl stop meshcore-bot` on Linux, and terminates the PID on Windows.
  ```bash
  meshbot stop
  ```
- **Restart Daemon**: Restarts the daemon. Runs `sudo systemctl restart meshcore-bot` on Linux, and spawns a background process on Windows.
  ```bash
  meshbot restart
  ```
- **Configuration Wizard**: Runs an interactive wizard using readline to scan serial ports or BLE nodes, prompting the user for parameters before generating and validating `config.json`.
  ```bash
  meshbot config
  ```
- **Status Dashboard**: Prints service diagnostics. Runs `sudo systemctl status meshcore-bot` on Linux, and reads active PID lockfile status on Windows.
  ```bash
  meshbot status
  ```
- **Troubleshooting Logs**: Streams/tails the logs. Invokes `journalctl -u meshcore-bot -f` when systemd is active on Linux, and falls back to a real-time tail of `config/meshbot.log` on Windows.
  ```bash
  meshbot logs
  ```

---

## Safe Push Git Pipeline

To prevent push of broken code, the registered Git pre-push hook runs the script `/scripts/pre_push.py` automatically before any `git push` command is allowed to complete.

The pipeline performs the following tasks:
1. **Schema Validation**: Validates `config/config.json` against `config/schema.json` to ensure configuration integrity.
2. **Automated Unit Tests**: Runs the `tests/test_framework.py` test suite. If any tests fail, the git push is blocked.

---

## Creating Custom Modules

Modules are loaded from `/modules` using dynamic imports. To create a custom module, create a python script that exports a class whose name is the TitleCase equivalent of the file name (e.g. class `Template` in `template.py` or class `Module` as a fallback).

You can use [modules/template.py](file:///c:/Users/dhovi/Documents/GitHub/MeshCore-Bot/modules/template.py) as a reference blueprint.

### Module Interface

A valid module must implement:
- `name` (string): Unique identifier matching the configuration block in `config.json`.
- `config_schema` (optional dict): JSON Schema matching its properties.
- `init(api, config)` (sync or async function): Invoked at boot with the module API and its configuration block.
- `start()` (sync or async function): Invoked after all modules are loaded.
- `stop()` (sync or async function): Invoked on graceful shutdown. Clean up subscriptions and tasks here.

### Module API Reference

The framework injects a `ModuleAPI` instance into the `init` hook:

- **`api.subscribe(event_name, callback)`**:
  Subscribe to internal events. Returns an unsubscribe function.
  - Events:
    - `'message'`: Receives structured packet dictionary `{ sender, text, channel, timestamp, snr, rssi, path }`.
    - `'connect'`: Receives device details on connection.
    - `'advert'`: Receives raw node advertisement payloads.
    - `'path_update'`: Receives path/route update payloads.
- **`await api.send(command_string)`**:
  Send a command to the connected hardware node. Sanitized against multi-line shell injection. Returns a dictionary containing the node response.
  - Example: `await api.send('msg alice "Hello Alice"')`
- **`api.get_state()`**:
  Returns a read-only deep copy of the central state cache (battery, neighbor count, uptime, etc.).
- **`api.schedule_task(cron_expression, callback)`**:
  Schedules a task on a cron schedule. Returns a cancel function.
  - Example: `api.schedule_task('*/5 * * * *', self.my_periodic_task)`
