#!/usr/bin/env bash
# Build the nRF52840 pump firmware. Runs inside ghcr.io/zephyrproject-rtos/ci (Docker or GitHub
# Actions) or on any machine with west + the Zephyr SDK installed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ZEPHYR_VERSION="${ZEPHYR_VERSION:-v4.1.0}"
BOARD="${BOARD:-xiao_ble}"
WS="${OPENPATCH_ZEPHYR_WS:-$ROOT/firmware/build/zephyr-ws}"
BUILD="$ROOT/firmware/build/nrf52840"
OUT="$ROOT/firmware/build/out/nrf52840"

git config --global --add safe.directory '*' 2>/dev/null || true

if [ ! -d "$WS/.west" ]; then
  west init -m https://github.com/zephyrproject-rtos/zephyr --mr "$ZEPHYR_VERSION" "$WS"
  # only the modules an nRF52840 Bluetooth build needs
  (cd "$WS" && west config manifest.project-filter -- '-.*,+cmsis,+cmsis_6,+hal_nordic,+mbedtls,+tinycrypt')
fi
(cd "$WS" && west update --narrow -o=--depth=1)
export ZEPHYR_BASE="$WS/zephyr"

west build -p auto -b "$BOARD" -d "$BUILD" "$ROOT/firmware/nrf52840" -- \
  -DDTC_OVERLAY_FILE="$ROOT/firmware/nrf52840/boards/xiao_ble.overlay"

mkdir -p "$OUT"
cp "$BUILD/zephyr/zephyr.hex" "$OUT/openpatch-pump.hex"
[ -f "$BUILD/zephyr/zephyr.uf2" ] && cp "$BUILD/zephyr/zephyr.uf2" "$OUT/openpatch-pump.uf2"
echo "nRF52840 firmware: $OUT"
ls -l "$OUT"
