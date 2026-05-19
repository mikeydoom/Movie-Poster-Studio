cmake_minimum_required(VERSION 3.14)
project(poster_native LANGUAGES CXX)

set(LIBVIPS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/vendor/libvips")

# ─── Build the DLL ───────────────────────────────────────────────────────────

add_library(poster_native SHARED poster_native.cpp poster_native.h)

target_compile_features(poster_native PRIVATE cxx_std_17)

# Suppress MSVC warnings about secure CRT / unreferenced params in vips headers.
target_compile_options(poster_native PRIVATE
    /W3
    /wd4996   # 'deprecated' CRT functions
    /wd4100   # unreferenced formal parameter
    /wd4458   # declaration hides class member
    /EHsc
)

target_compile_definitions(poster_native PRIVATE
    "_HAS_EXCEPTIONS=0"
    "WIN32_LEAN_AND_MEAN"
    "NOMINMAX"
    # Tell libvips headers we're building against the prebuilt DLL (not static).
    "VIPS_DISABLE_DEPRECATION_WARNINGS"
)

# libvips include paths: vips headers + GLib headers
target_include_directories(poster_native SYSTEM PRIVATE
    "${LIBVIPS_DIR}/include"
    "${LIBVIPS_DIR}/include/glib-2.0"
    "${LIBVIPS_DIR}/lib/glib-2.0/include"
)

# Import libraries
target_link_libraries(poster_native PRIVATE
    "${LIBVIPS_DIR}/lib/libvips.lib"
    "${LIBVIPS_DIR}/lib/libglib-2.0.lib"
    "${LIBVIPS_DIR}/lib/libgobject-2.0.lib"
)

# Name the output DLL "poster_native.dll" (no "lib" prefix on Windows)
set_target_properties(poster_native PROPERTIES
    PREFIX ""
    OUTPUT_NAME "poster_native"
)

# Install commands are intentionally absent here.
# CMAKE_INSTALL_PREFIX is not yet set when add_subdirectory("poster_native")
# runs. All install() rules for this target are in windows/CMakeLists.txt,
# which runs after the prefix is configured.
