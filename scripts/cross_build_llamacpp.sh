#!/usr/bin/env bash
# Stage-3 ARM cross build for the inference backend (plan 7.4 / step 2).
#
# IMPORTANT: the project's own code is pure Python, so there is nothing to
# cross compile here. This script builds llama.cpp (C++) for aarch64.
# It has NOT been executed in this repository's x86_64 Windows dev environment:
# no aarch64 toolchain, no ARM Linux host, no QEMU/Docker. Run it on a Linux
# build host with aarch64-linux-gnu-gcc / cmake / ninja installed, then copy
# build-aarch64/bin/llama-cli to the 飞腾/鲲鹏 target and run the inference probe.
#
# Usage:
#   LLAMA_CPP_SRC=/path/to/llama.cpp bash scripts/cross_build_llamacpp.sh
#   bash scripts/cross_build_llamacpp.sh --verify   # only inspect an existing build

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLCHAIN="$ROOT/adapt/toolchains/aarch64-linux-gnu.cmake"
BUILD_DIR="${BUILD_DIR:-$ROOT/build-aarch64}"

if [[ "${1:-}" == "--verify" ]]; then
  BIN="$BUILD_DIR/bin/llama-cli"
  [[ -f "$BIN" ]] || { echo "FAIL: $BIN not built yet"; exit 1; }
  file "$BIN"
  if file "$BIN" | grep -qiE "aarch64|ARM64"; then
    echo "PASS: llama-cli is an aarch64 binary"
  else
    echo "FAIL: llama-cli is NOT an aarch64 binary"; exit 1
  fi
  echo "NEXT: on the target run: $BIN -m qwen2.5-7b-instruct-q4_k_m.gguf -p '国产化适配测试' -n 32"
  exit 0
fi

SRC="${LLAMA_CPP_SRC:-}"
[[ -n "$SRC" && -d "$SRC" ]] || {
  echo "FAIL: set LLAMA_CPP_SRC to a local llama.cpp checkout (no network clone here)"; exit 1; }

for t in aarch64-linux-gnu-gcc aarch64-linux-gnu-g++ cmake; do
  command -v "$t" >/dev/null 2>&1 || { echo "FAIL: missing tool $t"; exit 1; }
done

echo "== cross building llama.cpp for aarch64 =="
cmake -S "$SRC" -B "$BUILD_DIR" -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=OFF -DLLAMA_NATIVE=OFF \
  -DGGML_BLAS=OFF -DLLAMA_BLAS=OFF \
  -DLLAMA_CURL=OFF -DBUILD_SHARED_LIBS=OFF

cmake --build "$BUILD_DIR" --target llama-cli -j"$(nproc)"
bash "$0" --verify
