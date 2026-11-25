#!/usr/bin/env bash
# Build OpenPatch firmware anywhere Docker runs (Linux, macOS, Windows/WSL). Same images as CI.
#   firmware/build.sh test        Python model + C core tests
#   firmware/build.sh nrf52840    pump firmware  -> firmware/build/out/nrf52840/openpatch-pump.uf2
#   firmware/build.sh attiny1616  safety MCU     -> firmware/build/out/attiny1616/*.hex
#   firmware/build.sh all
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ZEPHYR_IMAGE="${ZEPHYR_IMAGE:-ghcr.io/zephyrproject-rtos/ci:v0.27.4}"
RUN=(docker run --rm -v "$ROOT":/work -w /work -e HOME=/tmp)
[ "$(uname -s)" = "Linux" ] && RUN+=(--user "$(id -u):$(id -g)")

target_test() {
  # runs as root to install gcc; writes nothing into the checkout
  docker run --rm -v "$ROOT":/work -w /work -e PYTHONDONTWRITEBYTECODE=1 \
    -e OPENPATCH_FW_LIB=/tmp/libopenpatch_fw.so python:3.12-slim bash -c \
    "apt-get -qq update >/dev/null && apt-get -qq install -y gcc >/dev/null && firmware/scripts/test-host.sh"
}
target_nrf52840() { "${RUN[@]}" "$ZEPHYR_IMAGE" firmware/scripts/build-nrf52840.sh; }
target_attiny1616() {
  docker build -t openpatch-avr -f "$ROOT/firmware/docker/avr.Dockerfile" "$ROOT/firmware/docker"
  "${RUN[@]}" openpatch-avr firmware/scripts/build-attiny1616.sh
}

case "${1:-all}" in
  test) target_test ;;
  nrf52840) target_nrf52840 ;;
  attiny1616) target_attiny1616 ;;
  all) target_test; target_nrf52840; target_attiny1616 ;;
  *) echo "usage: $0 [test|nrf52840|attiny1616|all]"; exit 2 ;;
esac
