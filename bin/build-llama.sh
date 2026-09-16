#!/bin/bash -e
# Build the llama.cpp fork (the git submodule at ./modules/llama.cpp) with CUDA, inside
# the container. The build directory lives in the bind-mounted submodule, so
# the result persists on the host across container restarts, image rebuilds
# and `dock.sh clean`. Re-run after updating the submodule.
#
#   CUDA_ARCH   CMake CUDA architectures; default "native" detects the GPU
#               present at build time (needs the GPU reserved for the build).
#   JOBS        parallel compile jobs; default: all cores.

cd "$(dirname "$(readlink -f "$0")")/../modules/llama.cpp"

if [ ! -f CMakeLists.txt ]; then
    echo "llama.cpp submodule is empty. On the host run: git submodule update --init" >&2
    exit 1
fi

cmake -S . -B build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH:-native}" \
    -DLLAMA_OPENSSL=ON \
    -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_EXAMPLES=ON \
    -DLLAMA_BUILD_SERVER=ON

cmake --build build --target llama-server llama-cli -j "${JOBS:-$(nproc)}"

echo
echo "built: $(pwd)/build/bin/llama-server"
./build/bin/llama-server --version 2>&1 | head -2
