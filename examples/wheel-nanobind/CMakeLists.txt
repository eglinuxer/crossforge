cmake_minimum_required(VERSION 3.18)
project(demoxx LANGUAGES CXX)

find_package(Python 3.9 COMPONENTS Interpreter Development.Module REQUIRED)

# nanobind ships its CMake config inside the installed package; _DIR points
# find_package straight at the directory holding nanobind-config.cmake.
execute_process(
  COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
  OUTPUT_VARIABLE nanobind_DIR OUTPUT_STRIP_TRAILING_WHITESPACE)
find_package(nanobind CONFIG REQUIRED)

nanobind_add_module(demoxx demoxx.cpp)
install(TARGETS demoxx LIBRARY DESTINATION .)
