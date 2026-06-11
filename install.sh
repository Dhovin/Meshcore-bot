#!/usr/bin/env bash
# install.sh: Linux Installer for MeshCore-bot Central Hub

set -e # Exit immediately on error

echo "=================================================="
echo "Starting MeshCore-bot Linux Installation Script"
echo "=================================================="

# Helper function to check package manager
detect_package_manager() {
  if command -v apt-get &> /dev/null; then
    echo "apt"
  elif command -v dnf &> /dev/null; then
    echo "dnf"
  elif command -v pacman &> /dev/null; then
    echo "pacman"
  else
    echo "unknown"
  fi
}

PKG_MGR=$(detect_package_manager)

# 1. System Prerequisites Checks
echo "[Install] Checking system prerequisites..."

# Python 3.10+ Check
if ! command -v python3 &> /dev/null; then
  echo "[Error] Python 3 is missing. Python 3.10+ is required."
  exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
MAJOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f1)
MINOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f2)

if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]; }; then
  echo "[Error] Python 3.10+ is required. Found Python $PYTHON_VERSION"
  exit 1
else
  echo "[Install] Python 3.10+ is verified. Found Python $PYTHON_VERSION"
fi

# 2. Package installation for virtual environment & pip support
echo "[Install] Installing virtualenv and pip support if needed..."
if [ "$PKG_MGR" = "apt" ]; then
  sudo apt-get update
  sudo apt-get install -y python3-venv python3-pip python3-dev build-essential
elif [ "$PKG_MGR" = "dnf" ]; then
  sudo dnf install -y python3-pip python3-virtualenv python3-devel development-tools
elif [ "$PKG_MGR" = "pacman" ]; then
  sudo pacman -Syu --noconfirm python-pip python-virtualenv base-devel
else
  echo "[Install] Non-standard package manager. Assuming python3-venv is already present."
fi

# 3. Setup Project Virtual Environment
# Detect repository directory
if [ -f "bin/meshbot" ] && [ -d "core" ]; then
  REPO_DIR=$(pwd)
else
  echo "[Install] Standalone execution detected (not running in repository directory)."
  INSTALL_DIR="${HOME}/Meshcore-bot"
  if [ -d "$INSTALL_DIR" ]; then
    echo "[Install] Existing directory found at ${INSTALL_DIR}. Updating repository..."
    cd "$INSTALL_DIR"
    git pull
  else
    echo "[Install] Cloning MeshCore-bot repository into ${INSTALL_DIR}..."
    git clone https://github.com/Dhovin/Meshcore-bot.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
  fi
  REPO_DIR=$(pwd)
fi

VENV_DIR="${REPO_DIR}/venv"

echo "[Install] Creating Python virtual environment in ${VENV_DIR}..."
python3 -m venv "${VENV_DIR}"

echo "[Install] Upgrading pip, setuptools, and wheel in virtual environment..."
"${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel

echo "[Install] Installing Python libraries (pyserial, bleak, meshcore, meshcore-cli, paho-mqtt, pynacl) inside virtual environment..."
"${VENV_DIR}/bin/pip" install pyserial bleak meshcore meshcore-cli paho-mqtt pynacl

# 4. Setup Project Configuration
CONFIG_FILE="${REPO_DIR}/config/config.json"
TEMPLATE_FILE="${REPO_DIR}/config/config.json.template"

# Create a backup template config if not exists
if [ ! -f "$TEMPLATE_FILE" ] && [ -f "$CONFIG_FILE" ]; then
  cp "$CONFIG_FILE" "$TEMPLATE_FILE"
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[Install] Copying template configuration to config.json..."
  if [ -f "$TEMPLATE_FILE" ]; then
    cp "$TEMPLATE_FILE" "$CONFIG_FILE"
  else
    # Fallback default configuration
    mkdir -p "${REPO_DIR}/config"
    cat > "$CONFIG_FILE" <<EOF
{
  "connection": {
    "type": "auto",
    "address": "",
    "port": "",
    "baudrate": 115200,
    "host": "127.0.0.1",
    "tcpPort": 5000
  },
  "core": {
    "timeSyncInterval": "0 0 * * *",
    "shutdownTimeoutMs": 10000
  },
  "modules": {
    "template": {
      "enabled": true,
      "messagePrefix": "[MeshBot]",
      "logChannel": 0
    }
  }
}
EOF
  fi
else
  echo "[Install] Existing config.json found. Keeping original settings."
fi

# 5. Create the global shell wrapper runner
echo "[Install] Deploying global CLI runner wrapper to /usr/local/bin/meshbot..."
WRAPPER_PATH="/usr/local/bin/meshbot"

sudo bash -c "cat > ${WRAPPER_PATH}" <<EOF
#!/bin/sh
# Shell wrapper routing meshbot command calls to the virtual environment
exec "${VENV_DIR}/bin/python" "${REPO_DIR}/bin/meshbot" "\$@"
EOF

sudo chmod +x "${WRAPPER_PATH}"
echo "[Install] CLI wrapper successfully created at ${WRAPPER_PATH}."

# 6. Generate systemd Service file
echo "[Install] Generating systemd service unit file..."
SERVICE_PATH="/etc/systemd/system/meshcore-bot.service"

sudo bash -c "cat > ${SERVICE_PATH}" <<EOF
[Unit]
Description=MeshCore-bot Central Hub Daemon
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${REPO_DIR}
ExecStart=${WRAPPER_PATH} start-daemon
Restart=on-failure
SupplementaryGroups=dialout tty

[Install]
WantedBy=multi-user.target
EOF

# 7. Reload systemd, enable and start service
echo "[Install] Enabling and starting systemd service..."
sudo systemctl daemon-reload
sudo systemctl enable meshcore-bot.service
sudo systemctl start meshcore-bot.service

echo "=================================================="
echo "MeshCore-bot Installation Complete!"
echo "=================================================="
echo "Verification instructions:"
echo "1. Check service status: sudo systemctl status meshcore-bot"
echo "2. View logs: sudo journalctl -u meshcore-bot -f"
echo "3. Run config wizard: meshbot config"
echo "4. Check bot config status: meshbot status"
echo "=================================================="
