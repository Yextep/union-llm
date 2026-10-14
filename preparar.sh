#!/usr/bin/env bash
set -euo pipefail
project_dir=$(dirname "$(readlink -f "$0")")
cd "$project_dir"
mkdir -p logs resultados modelos
if ! command -v cmake >/dev/null || ! command -v ninja >/dev/null; then
    apt-get update
    apt-get install -y --no-install-recommends cmake ninja-build build-essential git curl python3
fi
llama_revision=$(python3 -c 'import json; print(json.load(open("config.json"))["llama_commit"])')
if [ ! -d llama.cpp/.git ]; then
    git init llama.cpp
    git -C llama.cpp remote add origin https://github.com/ggml-org/llama.cpp.git
    git -C llama.cpp fetch --depth 1 origin "$llama_revision"
    git -C llama.cpp checkout --detach FETCH_HEAD
fi
if [ "$(git -C llama.cpp rev-parse HEAD)" != "$llama_revision" ]; then
    printf '%s\n' 'La revision de llama.cpp no coincide con config.json.' >&2
    exit 1
fi
cmake -S llama.cpp -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF -DGGML_RPC=ON -DGGML_NATIVE=OFF \
    -DGGML_CPU_ARM_ARCH="${UNION_LLM_ARCH:-armv8.2-a+dotprod+fp16}" -DGGML_OPENMP=OFF -DLLAMA_OPENSSL=OFF \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON \
    -DLLAMA_BUILD_APP=OFF -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF > logs/configuracion-cmake.log 2>&1
cmake --build build --target llama-completion llama-bench ggml-rpc-server llama-server \
    -j "${UNION_LLM_JOBS:-2}" > logs/compilacion.log 2>&1
# Trabajador estatico: funciona en el Debian de cualquier movil, aunque tenga otra glibc.
link_command=$(cd build && ninja -t commands bin/ggml-rpc-server | tail -1)
(cd build && eval "${link_command/ -o bin\/ggml-rpc-server / -static -o bin\/ggml-rpc-server-estatico }") \
    >> logs/compilacion.log 2>&1
python3 descargar_modelo.py
if [ "${1:-}" = --chat ]; then
    # Descarga el modelo que tenga configurado el chat en chat.json.
    python3 descargar_modelo.py "$(python3 -c 'import json; print(json.load(open("chat.json"))["modelo_manifest"])')"
fi
./union-llm desplegar
