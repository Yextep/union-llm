"""Genera resultados/OPTIMIZACION.md a partir de las mediciones guardadas, sin repetirlas."""
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / 'resultados'
FOLDER = RESULTS / 'optimizacion'


def load(name):
    path = FOLDER / (name + '.json')
    return json.loads(path.read_text()) if path.exists() else None


def speeds(rows):
    prompt = next((r for r in rows if r['n_prompt'] and not r['n_gen']), None)
    generation = next((r for r in rows if r['n_gen'] and not r['n_prompt']), None)
    show = lambda r: f"{r['avg_ts']:.2f} ± {r['stddev_ts']:.2f}" if r else '—'
    return show(prompt), show(generation)


def split(data):
    return ', '.join(f'{alias} {count}' for alias, count in data.get('reparto', {}).items())


def bench_table(title, rows):
    lines = [f'## {title}', '', '| Variante | Reparto (capas) | Prompt, tok/s | Generacion, tok/s |',
             '| --- | --- | ---: | ---: |']
    for name, label in rows:
        data = load(name)
        if data:
            prompt, generation = speeds(data['mediciones'])
            lines.append(f'| {label} | {split(data)} | {prompt} | {generation} |')
    return lines + ['']


def main():
    lines = ['# Optimizacion de Union LLM', '',
             'Datos generados por `tools/informe-optimizacion.py` desde `resultados/optimizacion/`.',
             'Mediciones con `llama-bench` (prompt de 64 tokens, 32 generados) salvo que se indique otra cosa.', '',
             '## a) CPU del central, Qwen2.5-Coder-3B Q4_K_M', '',
             '| Compilacion e hilos | Prompt, tok/s | Generacion, tok/s |', '| --- | ---: | ---: |']
    for name, label in [('base-t4', 'ARMv8-A generica, 4 hilos (anterior)'), ('dotprod-t4', 'dotprod, 4 hilos'),
                        ('dotprod-t2-rapidos', 'dotprod, 2 hilos en los nucleos rapidos'),
                        ('dotprod-t6', 'dotprod, 6 hilos'), ('dotprod-t8', 'dotprod, 8 hilos')]:
        data = load(name)
        if data:
            prompt, generation = speeds(data)
            lines.append(f'| {label} | {prompt} | {generation} |')
    lines.append('')
    lines += bench_table('a) Qwen2.5-Coder-3B repartido entre central, movil3 y movil4 (Wi-Fi anterior)', [
        ('3b-3disp-base', 'Compilacion anterior, 4 hilos, central por RPC'),
        ('3b-3disp-dotprod-rapidos', 'dotprod, nucleos rapidos, central por RPC'),
        ('3b-3disp-dotprod-rapidos-centralcpu', 'dotprod, nucleos rapidos, central en su CPU'),
        ('3b-diag-solo-movil3', 'Diagnostico: todo en movil3'),
        ('3b-diag-central-por-rpc', 'Diagnostico: todo en el central, por RPC local')])
    lines += bench_table('b) Qwen2.5-Coder-7B Q4_K_M: no cabe en ningun dispositivo solo', [
        ('7b-minimo', 'Minimo de dispositivos, Wi-Fi anterior'), ('7b-todos', 'Todos los dispositivos, Wi-Fi anterior'),
        ('7b-minimo-5ghz', 'Minimo de dispositivos, Wi-Fi 5 GHz')])
    big = [(name, label) for name, label in [
        ('chat-27b-minimo', 'Qwen3.8-27B denso, UD-IQ2_XXS (7,3 GB)'),
        ('chat-ernie-minimo', 'ERNIE-4.5-21B-A3B MoE, UD-Q2_K_XL (8,2 GB)')] if load(name)]
    if big:
        lines += ['## d) Modelos grandes en el chat real: denso frente a MoE', '',
                  'Wi-Fi 5 GHz. Minimo de dispositivos, temperatura 0, sin razonamiento previo, 64 tokens por respuesta.',
                  'La carga es la del ultimo intento: parte de las capas ya estaba en la cache de los moviles.', '',
                  '| Modelo | Reparto (capas) | Carga, min | Pregunta | Prompt, tok/s | Generacion, tok/s |',
                  '| --- | --- | ---: | --- | ---: | ---: |']
        for name, label in big:
            data = load(name)
            for number, answer in enumerate(data['respuestas'], 1):
                timings = answer['timings']
                lines.append(f"| {label} | {split(data)} | {data['carga_segundos'] / 60:.0f} | {number} | "
                             f"{timings['prompt_per_second']:.2f} | {timings['predicted_per_second']:.2f} |")
        lines.append('')
        for name, label in big:
            for answer in load(name)['respuestas']:
                lines += [f'**{label}.** {answer["pregunta"]}', '', '~~~text', answer['respuesta'].strip(), '~~~', '']
    before, after = load('latencia-con-wakelock'), load('latencia-5ghz')
    if before and after:
        lines += ['## Red: Wi-Fi anterior frente a 5 GHz', '',
                  'Viaje de ida y vuelta de un mensaje pequeno por el tunel SSH (mediana / percentil 90, ms).', '',
                  '| Movil | Wi-Fi anterior | Wi-Fi 5 GHz |', '| --- | ---: | ---: |']
        for alias in sorted(after):
            old, new = before[alias]['tcp_por_tunel_ssh'], after[alias]['tcp_por_tunel_ssh']
            lines.append(f"| {alias} | {old['mediana_ms']} / {old['p90_ms']} | {new['mediana_ms']} / {new['p90_ms']} |")
        lines += ['', 'Ancho de banda medido del central a un movil: 1,5-1,8 MB/s antes y 15-17 MB/s en 5 GHz,',
                  'con y sin SSH. La carga inicial de un modelo de 7 GB paso de mas de una hora a 5-7 minutos.', '']
    chats = [(name, label) for name, label in [('chat-3b-todos-sin-borrador', 'Sin borrador'),
                                               ('chat-3b-todos-con-borrador', 'Borrador Qwen2.5-0.5B, hasta 8 tokens')]
             if load(name)]
    if chats:
        lines += ['## Decodificacion especulativa, 3B repartido (chat real, temperatura 0, Wi-Fi anterior)', '',
                  '| Variante | Pregunta | Generacion, tok/s | Borrador aceptado |', '| --- | --- | ---: | ---: |']
        for name, label in chats:
            for number, answer in enumerate(load(name)['respuestas'], 1):
                timings = answer['timings']
                accepted = f"{timings['draft_n_accepted']}/{timings['draft_n']}" if timings.get('draft_n') else '—'
                lines.append(f"| {label} | {number} | {timings['predicted_per_second']:.2f} | {accepted} |")
        lines.append('')
    (RESULTS / 'OPTIMIZACION.md').write_text('\n'.join(lines))
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
