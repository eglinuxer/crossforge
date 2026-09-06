cmake_minimum_required(VERSION 3.20)
project(crossforge_launcher_consumer LANGUAGES C CXX)

add_executable(crossforge-consumer main.cc answer.c)
target_compile_features(crossforge-consumer PRIVATE cxx_std_20)
