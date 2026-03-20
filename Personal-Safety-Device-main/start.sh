#!/bin/bash
# Kavach Device — Raspberry Pi startup script
# Usage: chmod +x start.sh && ./start.sh

set -e
cd "$(dirname "$0")"

echo "========================================="
echo "  Kavach Personal Safety Device"
echo "========================================="
echo ""

# Install system-level packages (only runs once, skips if already installed)
SYSPKGS="libportaudio2 portaudio19-dev"
MISSING=""
for pkg in $SYSPKGS; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        MISSING="$MISSING $pkg"
    fi
done
if [ -n "$MISSING" ]; then
    echo "[Kavach] Installing system packages:$MISSING"
    sudo apt-get update -qq
    sudo apt-get install -y -qq $MISSING
    echo "[Kavach] System packages installed."
fi

# Create venv if it doesn't exist
if [ ! -f "venv/bin/python" ]; then
    echo "[Kavach] Creating virtual environment..."
    python3 -m venv --system-site-packages venv
    echo "[Kavach] Virtual environment created."
fi

# Activate venv
echo "[Kavach] Activating virtual environment..."
source venv/bin/activate

# Install/update dependencies
echo "[Kavach] Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet \
    SQLAlchemy requests pyserial cryptography \
    numpy sounddevice \
    RPi.GPIO smbus2 spidev \
    adafruit-circuitpython-bno055 adafruit-circuitpython-busdevice

# Try tflite-runtime first (lighter), fall back to tensorflow
if ! python -c "import tflite_runtime" 2>/dev/null && ! python -c "import tensorflow" 2>/dev/null; then
    echo "[Kavach] Installing TFLite runtime..."
    pip install --quiet tflite-runtime 2>/dev/null || {
        echo "[Kavach] tflite-runtime not available, installing tensorflow..."
        pip install --quiet tensorflow
    }
fi

# Try max30102 (may not be available for all Python versions)
pip install --quiet max30102 2>/dev/null || echo "[Kavach] max30102 not available — will use FakeHeartRate fallback."

echo "[Kavach] Dependencies ready."
echo ""

# Generate encryption key if missing
if [ ! -f "keys/chacha.key" ]; then
    echo "[Kavach] Generating encryption key..."
    mkdir -p keys
    python -c "
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
key = ChaCha20Poly1305.generate_key()
open('keys/chacha.key', 'wb').write(key)
print('[Kavach] Encryption key generated.')
"
    echo ""
    echo "  WARNING: Copy keys/chacha.key to the server (Kavach-Server-main/keys/)"
    echo "           Both must share the same key for encryption to work."
    echo ""
    read -p "  Press Enter once you've copied the key..."
fi

# Download YAMNet model if missing
if [ ! -f "models/yamnet.tflite" ]; then
    echo "[Kavach] Downloading YAMNet audio model..."
    python setup_audio.py
fi

echo ""
echo "[Kavach] Starting device..."
echo "========================================="
echo ""

python main.py
