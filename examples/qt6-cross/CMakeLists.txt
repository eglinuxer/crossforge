cmake_minimum_required(VERSION 3.19)
project(qt6_cross LANGUAGES CXX)

# Nothing here is cross-specific: the toolchain file and QT_HOST_PATH do
# that work, which is the point — a Qt project needs no special casing to
# be cross-built by this environment.
find_package(Qt6 REQUIRED COMPONENTS Core)

qt_standard_project_setup()

qt_add_executable(qt6_cross main.cpp)
target_link_libraries(qt6_cross PRIVATE Qt6::Core)
