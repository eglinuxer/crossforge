cmake_minimum_required(VERSION 3.16)
project(crossforge_host_probe LANGUAGES C)

if(CMAKE_CROSSCOMPILING)
  message(FATAL_ERROR "Crossforge host probe was configured as a cross build")
endif()

file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/crossforge-host-build.txt"
  "cross=${CMAKE_CROSSCOMPILING}\n"
  "compiler=${CMAKE_C_COMPILER}\n"
  "make_program=${CMAKE_MAKE_PROGRAM}\n"
  "processor=${CMAKE_SYSTEM_PROCESSOR}\n"
  "sysroot=${CMAKE_SYSROOT}\n")

add_executable(crossforge-host-probe probe.c)
install(TARGETS crossforge-host-probe RUNTIME DESTINATION bin)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/crossforge-host-build.txt"
  DESTINATION share/crossforge-host-probe)
