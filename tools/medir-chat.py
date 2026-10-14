"""Mide el motor del chat de punta a punta, cargando el modelo con el mismo servicio que el chat.

El servicio reintenta solo si un dispositivo cae durante la carga; cada intento aprovecha las
capas que ya quedaron en la cache de los moviles.

Uso: tools/medir-chat.py modelo-manifiesto.json nombre [--borrador manifiesto.json] [--tokens 128]
"""
import argparse
import datetime
import fcntl
import http.client
import json
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat
import cluster

PROMPTS = [
    'Escribe una funcion en Python que devuelva los numeros primos menores que n usando la criba '
    'de Eratostenes. Incluye un comentario breve.',
    'Explica en tres frases que es una red LAN y para que sirve.',
]


def request(path, payload, timeout=7200):
    connection = http.client.HTTPConnection('127.0.0.1', chat.SETTINGS['puerto_motor'], timeout=timeout)
    try:
        connection.request('POST', path, json.dumps(payload), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('manifiesto')
    parser.add_argument('nombre')
    parser.add_argument('--borrador')
    parser.add_argument('--tokens', type=int, default=128)
    parser.add_argument('--dispositivos', choices=['minimo', 'todos'])
    parser.add_argument('--excluir', nargs='*', default=[])
    parser.add_argument('--horas', type=float, default=4, help='Tiempo maximo para conseguir cargar el modelo')
    args = parser.parse_args()
    if args.dispositivos:
        cluster.CONFIG['dispositivos'] = args.dispositivos
    cluster.CONFIG['excluir'] = args.excluir
    chat.SETTINGS.update(modelo_manifest=args.manifiesto, modelo_borrador_manifest=args.borrador)
    folder = cluster.RESULTS / 'optimizacion'
    folder.mkdir(parents=True, exist_ok=True)
    with (cluster.PROJECT / '.ejecucion.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        service = chat.ModelService()
        try:
            messages = []
            while service.status()['fase'] != 'listo':
                state = service.status()
                if state['fase'] == 'error':
                    raise RuntimeError(state['mensaje'])
                if state['mensaje'] not in messages:
                    messages.append(state['mensaje'])
                if time.monotonic() - started > args.horas * 3600:
                    raise RuntimeError('No se consiguio cargar el modelo a tiempo.')
                time.sleep(5)
            load_seconds = time.monotonic() - started
            attempts = service.attempts
            print(f'Modelo cargado en {load_seconds / 60:.0f} min tras {attempts} intento(s)', flush=True)
            answers = []
            for prompt in PROMPTS:
                begin = time.monotonic()
                status, body = request('/v1/chat/completions', {
                    'model': 'union-llm', 'temperature': 0, 'max_tokens': args.tokens,
                    'messages': [{'role': 'system', 'content': chat.SETTINGS['system_prompt']},
                                 {'role': 'user', 'content': prompt}]})
                if status != 200:
                    raise RuntimeError(body.decode(errors='replace')[:500])
                data = json.loads(body)
                timings = data.get('timings', {})
                answers.append({'pregunta': prompt, 'segundos': round(time.monotonic() - begin, 1),
                                'respuesta': data['choices'][0]['message'].get('content', ''), 'timings': timings})
                accepted = ''
                if timings.get('draft_n'):
                    accepted = f", borrador aceptado {timings['draft_n_accepted']}/{timings['draft_n']}"
                print(f"prompt {timings.get('prompt_per_second', 0):.2f} tok/s, generacion "
                      f"{timings.get('predicted_per_second', 0):.2f} tok/s ({timings.get('predicted_n')} tokens)"
                      f"{accepted}", flush=True)
            layers, missing = service.cluster.layers, service.cluster.missing
        finally:
            service.close()
    report = {'fecha_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'manifiesto': args.manifiesto,
              'borrador': args.borrador, 'reparto': layers, 'sin_usar': missing, 'configuracion': cluster.CONFIG,
              'carga_segundos': round(load_seconds), 'intentos_de_carga': attempts, 'mensajes': messages,
              'respuestas': answers}
    (folder / (args.nombre + '.json')).write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    main()
