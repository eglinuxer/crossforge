cmake_minimum_required(VERSION 3.16)
project(crossforge_target_probe LANGUAGES C)

if(NOT DEFINED CROSSFORGE_EXPECT_CROSS)
  message(FATAL_ERROR "Crossforge expected-cross state is missing")
endif()
if(CROSSFORGE_EXPECT_CROSS AND NOT CMAKE_CROSSCOMPILING)
  message(FATAL_ERROR "Crossforge target probe silently became native")
endif()
if(NOT CROSSFORGE_EXPECT_CROSS AND CMAKE_CROSSCOMPILING)
  message(FATAL_ERROR "Crossforge native probe silently became cross")
endif()
if(NOT EXISTS "${CROSSFORGE_HOST_PROBE}")
  message(FATAL_ERROR "Crossforge host-triplet generator is missing")
endif()

execute_process(
  COMMAND "${CROSSFORGE_HOST_PROBE}"
  OUTPUT_FILE "${CMAKE_CURRENT_BINARY_DIR}/generated.h"
  RESULT_VARIABLE crossforge_generator_result
  ERROR_VARIABLE crossforge_generator_error
)
if(NOT crossforge_generator_result EQUAL 0)
  message(FATAL_ERROR
    "Crossforge host generator failed: ${crossforge_generator_error}")
endif()

file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/crossforge-target-build.txt"
  "cross=${CMAKE_CROSSCOMPILING}\n"
  "compiler=${CMAKE_C_COMPILER}\n"
  "host_tool=${CROSSFORGE_HOST_PROBE}\n"
  "linkage=${BUILD_SHARED_LIBS}\n"
  "make_program=${CMAKE_MAKE_PROGRAM}\n"
  "processor=${CMAKE_SYSTEM_PROCESSOR}\n"
  "sysroot=${CMAKE_SYSROOT}\n")

add_library(crossforge-target-probe probe.c)
target_include_directories(crossforge-target-probe PRIVATE
  "${CMAKE_CURRENT_BINARY_DIR}")
set_target_properties(crossforge-target-probe PROPERTIES
  OUTPUT_NAME crossforge-target-probe)

install(TARGETS crossforge-target-probe
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin)
install(FILES probe.h "${CMAKE_CURRENT_BINARY_DIR}/generated.h"
  DESTINATION include/crossforge-target-probe)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/crossforge-target-build.txt"
  DESTINATION share/crossforge-target-probe)
