"""Convierte el experimento guardado en un informe breve, sin repetir inferencias."""
import json
from pathlib import Path
import re

project = Path(__file__).resolve().parent
folder = project / 'resultados'
data = json.loads((folder / 'experimento.json').read_text())
# Los experimentos antiguos guardaban una lista fija de moviles.
WORKERS = data.get('trabajadores') or ['central', *data['configuracion']['moviles']]


def speed(mode, kind):
    rows = data[mode]['mediciones']
    if kind == 'prompt':
        row = next(row for row in rows if row['n_prompt'] > 0 and row['n_gen'] == 0)
    else:
        row = next(row for row in rows if row['n_gen'] > 0 and row['n_prompt'] == 0)
    return row['avg_ts'], row['stddev_ts']


def buffers(filename):
    text = (project / 'logs' / filename).read_text()
    return re.findall(r'(\S+)\s+model buffer size\s*=\s*([0-9.]+)\s*MiB', text)


def rpc_allocations():
    rows = []
    for alias in WORKERS:
        text = (project / 'logs' / ('rpc-' + alias + '.log')).read_text()
        sizes = [int(value) for value in re.findall(r'\[alloc_buffer\] device: \d+, size: (\d+)', text)]
        if sizes:
            rows.append((alias, max(sizes) / 1048576))
    return rows


local_pp, local_pp_sd = speed('central', 'prompt')
local_tg, local_tg_sd = speed('central', 'generation')
remote_pp, remote_pp_sd = speed('distribuido', 'prompt')
remote_tg, remote_tg_sd = speed('distribuido', 'generation')
lines = [
    '# Resultado de Union LLM', '',
    '| Configuracion | Prompt, tokens/s | Generacion, tokens/s |',
    '| --- | ---: | ---: |',
    f'| Central, {data["configuracion"]["threads"]} hilos | {local_pp:.2f} ± {local_pp_sd:.2f} | {local_tg:.2f} ± {local_tg_sd:.2f} |',
    f'| {len(WORKERS)} trabajadores RPC, {data["configuracion"]["threads"]} hilos cada uno | {remote_pp:.2f} ± {remote_pp_sd:.2f} | {remote_tg:.2f} ± {remote_tg_sd:.2f} |', '',
    f'La generacion distribuida alcanzo {remote_tg / local_tg:.2f} veces la velocidad del central.',
    'Medidas de este modelo de 0,5B en CPU, con Wi-Fi, SSH y PRoot; dos repeticiones.', '',
    '## Buffers de los trabajadores RPC', '',
    'Mayor reserva individual observada en cada trabajador (MiB). Puede contener pesos y otras estructuras; no equivale al RSS total.', '',
    '| Trabajador | Mayor reserva RPC, MiB |', '| --- | ---: |',
]
for alias, size in rpc_allocations():
    lines.append(f'| {alias} | {size:.2f} |')
declared = [(mode, device, size) for mode in ('central', 'distribuida')
            for device, size in buffers('respuesta-' + mode + '.log')]
if declared:
    lines += ['', 'Buffers de pesos declarados por el cliente:', '',
              '| Modo | Buffer | MiB |', '| --- | --- | ---: |']
    lines.extend(f'| {mode} | {device} | {size} |' for mode, device, size in declared)
lines += ['', ', '.join(f'RPC{i} corresponde a {alias}' for i, alias in enumerate(WORKERS)) + '.', '',
          '## Actividad real de trabajadores', '',
          '| Trabajador | CPU acumulada durante carga y ejecucion, s | Pico RSS del proceso RPC, MiB |',
          '| --- | ---: | ---: |']
for alias, after in data['trabajadores_despues'].items():
    before = data['trabajadores_antes'][alias]
    delta = after['cpu_seconds'] - before['cpu_seconds']
    lines.append(f"| {alias} | {delta:.2f} | {after['peak_rss_kib'] / 1024:.2f} |")
lines += ['', 'El pico se refiere al trabajador RPC, no incluye el coordinador ni el resto de Android.', '',
          '## Respuesta real', '', 'Pregunta: Explica en una frase que es una red LAN.', '',
          '```text', data['texto_distribuido'].strip(), '```', '',
          f"Respuesta identica en ambos modos: {'si' if data['respuesta_identica'] else 'no'}.", '',
          'La comprobacion confirma la ejecucion repartida; este modelo pequeno no demuestra una mejora general de eficiencia.', '']
report = '\n'.join(lines)
(folder / 'RESUMEN.md').write_text(report)
print(report)
