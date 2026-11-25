cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(openpatch_pump)

set(OP_CORE ${CMAKE_CURRENT_SOURCE_DIR}/../core)

target_include_directories(app PRIVATE ${OP_CORE} src)
target_sources(app PRIVATE
  ${OP_CORE}/op_protocol.c
  ${OP_CORE}/op_fw.c
  src/main.c
  src/board.c
  src/ble.c
  src/storage.c
  src/shell_cmds.c
)
