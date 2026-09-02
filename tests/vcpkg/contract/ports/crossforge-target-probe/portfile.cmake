if(TARGET_TRIPLET STREQUAL HOST_TRIPLET)
    set(crossforge_expect_cross OFF)
else()
    set(crossforge_expect_cross ON)
endif()

set(crossforge_host_probe
    "${CURRENT_HOST_INSTALLED_DIR}/tools/crossforge-host-probe/crossforge-host-probe")

vcpkg_cmake_configure(
    SOURCE_PATH "${CMAKE_CURRENT_LIST_DIR}"
    OPTIONS
        "-DCROSSFORGE_EXPECT_CROSS=${crossforge_expect_cross}"
        "-DCROSSFORGE_HOST_PROBE=${crossforge_host_probe}"
)
vcpkg_cmake_install()
file(REMOVE_RECURSE
    "${CURRENT_PACKAGES_DIR}/debug/include"
    "${CURRENT_PACKAGES_DIR}/debug/share"
)
file(INSTALL "${CMAKE_CURRENT_LIST_DIR}/copyright"
    DESTINATION "${CURRENT_PACKAGES_DIR}/share/${PORT}")
