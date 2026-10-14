"""Prueba real de codigo y seguimiento usando el chat y sus trabajadores."""
import datetime
import http.client
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cluster

BASE = 'http://192.168.1.3:8080'


def status():
    with urlopen(BASE + '/api/estado', timeout=5) as response:
        return json.load(response)


def workers():
    remotes = [node['nombre'] for node in status()['nodos']
               if node['nombre'] != 'central' and node.get('detalle', '').endswith('capas')]
    processes = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if args[0] == str(cluster.BIN / 'ggml-rpc-server').encode():
            processes['central'] = SimpleNamespace(pid=int(entry.name))
    return cluster.worker_stats(SimpleNamespace(workers=list(processes), processes=processes, started_remotes=remotes))


def ask(messages, tokens, label):
    connection = http.client.HTTPConnection('192.168.1.3', 8080, timeout=1200)
    start = time.monotonic()
    first_token, answer, metadata, event = None, '', {}, ''
    try:
        connection.request('POST', '/api/chat', json.dumps({'id':'prueba-' + label,
                           'messages':messages, 'max_tokens':tokens}), {'Content-Type':'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(response.read().decode())
        while True:
            line = response.readline().decode()
            if not line:
                break
            if line.startswith('event: '):
                event = line[7:].strip()
            elif line.startswith('data: '):
                data = json.loads(line[6:])
                if event == 'token':
                    if first_token is None:
                        first_token = time.monotonic() - start
                    answer += data['texto']
                    print(data['texto'], end='', flush=True)
                elif event == 'fin':
                    metadata = data
                elif event == 'error':
                    raise RuntimeError(data['error'])
        if not metadata or not answer.strip():
            raise RuntimeError('No se completo la respuesta.')
        print('\n', flush=True)
        return {'respuesta':answer, 'primer_token_segundos':first_token,
                'segundos_http':time.monotonic() - start, 'metadatos':metadata}
    finally:
        connection.close()


def main():
    deadline = time.monotonic() + 5400
    while True:
        current = status()
        if current['fase'] == 'error':
            raise RuntimeError(current['mensaje'])
        if current['fase'] == 'listo' and not current['ocupado']:
            break
        if time.monotonic() > deadline:
            raise RuntimeError('No quedo listo el modelo en 90 minutos.')
        time.sleep(5)
    print('Modelo listo; comenzando prueba real.', flush=True)
    before = workers()
    question = 'Responde solo con un bloque de codigo Python que defina sumar(a, b) y devuelva a + b.'
    first = ask([{'role':'user','content':question}], 64, 'codigo')
    after = workers()
    delta = {name:round(after[name]['cpu_seconds'] - before[name]['cpu_seconds'], 2) for name in before}
    print('CPU consumida por cada trabajador:', delta, flush=True)
    followup = ask([{'role':'user','content':question}, {'role':'assistant','content':first['respuesta']},
                    {'role':'user','content':'Que devuelve sumar(2, 3)? Responde solo el numero.'}], 8, 'seguimiento')
    report = {'fecha_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(), 'estado':status(),
              'primera_pregunta':question, 'codigo':first, 'seguimiento':followup,
              'trabajadores_antes':before, 'trabajadores_despues':after, 'cpu_segundos_generacion':delta}
    (cluster.RESULTS / 'chat-prueba-real.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    assert all(value > 0 for value in delta.values()), 'No se registro calculo en los tres trabajadores.'
    assert 'def sumar(' in first['respuesta'] and 'a + b' in first['respuesta'], 'La respuesta no contiene la funcion solicitada.'
    assert followup['respuesta'].strip() == '5', 'No respondio correctamente el seguimiento.'
    print('Prueba real completada: codigo, contexto y calculo en los tres dispositivos.', flush=True)


if __name__ == '__main__':
    main()
