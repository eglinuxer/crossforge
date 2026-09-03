cmake_minimum_required(VERSION 3.16)
project(crossforge_vcpkg_tier3 LANGUAGES CXX)

find_package(Boost CONFIG REQUIRED COMPONENTS json)
find_package(protobuf CONFIG REQUIRED)

add_executable(crossforge-vcpkg-tier3
  consumer.cpp
  generated/message.pb.cc
)
target_include_directories(crossforge-vcpkg-tier3 PRIVATE generated)
target_link_libraries(crossforge-vcpkg-tier3 PRIVATE
  Boost::json
  protobuf::libprotobuf
)
