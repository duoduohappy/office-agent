# CMake toolchain for 飞腾 D3000M / 鲲鹏 920 (ARMv8 aarch64)
# Generated for stage-3 adaptation. Requires an aarch64 Linux cross toolchain.
# This file has NOT been used to build anything on the x86_64 Windows dev machine.

set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

set(CMAKE_C_COMPILER   aarch64-linux-gnu-gcc)
set(CMAKE_CXX_COMPILER aarch64-linux-gnu-g++)

# Do not let CMake probe the host architecture for target properties.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# ARMv8 baseline; NEON/DOTPROD are enabled by ggml at runtime, not hardcoded here,
# so the same binary works on 飞腾 FTC862 / 鲲鹏 TaiShan without -mcpu tuning.
set(CMAKE_C_FLAGS_INIT   "-O2 -pipe")
set(CMAKE_CXX_FLAGS_INIT "-O2 -pipe")
