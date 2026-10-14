#!/usr/bin/env bash
# Compara compilaciones e hilos en la CPU del central, una medicion tras otra.
# Uso: tools/medir-cpu.sh modelo.gguf
set -euo pipefail
project_dir=$(dirname "$(dirname "$(readlink -f "$0")")")
model=$1
out="$project_dir/resultados/optimizacion"
mkdir -p "$out"
run() {
    local name=$1 build=$2; shift 2
    printf 'Midiendo %s...\n' "$name"
    "$project_dir/$build/bin/llama-bench" -m "$model" -p 64 -n 32 -r 2 -ngl 0 -o json "$@" \
        > "$out/$name.json" 2> "$out/$name.log"
    python3 - "$out/$name.json" <<'PY'
import json, sys
for row in json.load(open(sys.argv[1])):
    kind = 'prompt' if row['n_prompt'] else 'generacion'
    print(f"  {kind}: {row['avg_ts']:.2f} ± {row['stddev_ts']:.2f} tokens/s")
PY
    sleep 20  # Deja enfriar la CPU entre mediciones.
}
run base-t4 build -t 4
run dotprod-t4 build-dotprod -t 4
run dotprod-t2-rapidos build-dotprod -t 2 -C 0xC0 --cpu-strict 1
run dotprod-t8 build-dotprod -t 8
run dotprod-t6 build-dotprod -t 6
