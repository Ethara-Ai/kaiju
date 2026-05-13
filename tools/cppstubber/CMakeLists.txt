cmake_minimum_required(VERSION 3.16)
project(cppstubber LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# ── Find LLVM & Clang ──────────────────────────────────────
find_package(LLVM REQUIRED CONFIG)
find_package(Clang REQUIRED CONFIG)

message(STATUS "LLVM version: ${LLVM_VERSION}")
message(STATUS "Clang include dirs: ${CLANG_INCLUDE_DIRS}")

list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
include(AddLLVM)

# LLVM is built without RTTI — we must match
if(NOT LLVM_ENABLE_RTTI)
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fno-rtti")
endif()

add_definitions(${LLVM_DEFINITIONS})
include_directories(${LLVM_INCLUDE_DIRS} ${CLANG_INCLUDE_DIRS})

# ── Build the stubber binary ───────────────────────────────
add_executable(cppstubber
    src/main.cpp
    src/stubber.cpp
)

# Prefer dynamic linking if LLVM was built that way
if(LLVM_LINK_LLVM_DYLIB)
    target_link_libraries(cppstubber PRIVATE clang-cpp LLVM)
else()
    target_link_libraries(cppstubber PRIVATE
        clangAST
        clangASTMatchers
        clangBasic
        clangFrontend
        clangLex
        clangRewrite
        clangSerialization
        clangTooling
        LLVMSupport
    )
endif()

install(TARGETS cppstubber RUNTIME DESTINATION bin)
