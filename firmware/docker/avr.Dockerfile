# Toolchain for the ATtiny1616 safety MCU: arduino-cli + megaTinyCore (SpenceKonde)
FROM debian:bookworm-slim

ARG ARDUINO_CLI_VERSION=1.1.1
ENV ARDUINO_DIRECTORIES_DATA=/opt/arduino15 \
    ARDUINO_DIRECTORIES_DOWNLOADS=/opt/arduino15/staging \
    ARDUINO_DIRECTORIES_USER=/tmp/arduino-user \
    ARDUINO_BOARD_MANAGER_ADDITIONAL_URLS=http://drazzy.com/package_drazzy.com_index.json

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl python3 \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://downloads.arduino.cc/arduino-cli/arduino-cli_${ARDUINO_CLI_VERSION}_Linux_64bit.tar.gz" \
    | tar -xz -C /usr/local/bin arduino-cli \
 && arduino-cli core update-index \
 && arduino-cli core install megaTinyCore:megaavr \
 && chmod -R a+rX /opt/arduino15

WORKDIR /work
