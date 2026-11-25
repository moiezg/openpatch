#!/usr/bin/env bash
# Build the ATtiny1616 safety MCU firmware with arduino-cli + megaTinyCore
# (inside firmware/docker/avr.Dockerfile, or anywhere arduino-cli has megaTinyCore installed).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FQBN="${FQBN:-megaTinyCore:megaavr:atxy6:chip=1616,clock=10internal,millis=disabled}"
SKETCH="$ROOT/firmware/build/attiny1616/sketch/attiny1616"
OUT="$ROOT/firmware/build/out/attiny1616"

rm -rf "$SKETCH"
mkdir -p "$SKETCH" "$OUT"
# arduino-cli compiles a sketch folder; bring the host-tested safety core into it
cp "$ROOT/firmware/attiny1616/attiny1616.ino" "$ROOT/firmware/safety/sm_core.c" "$ROOT/firmware/safety/sm_core.h" "$SKETCH/"

arduino-cli compile --fqbn "$FQBN" --warnings all --output-dir "$OUT" --build-path "${TMPDIR:-/tmp}/openpatch-avr-build" "$SKETCH"
echo "ATtiny1616 firmware: $OUT"
ls -l "$OUT"
