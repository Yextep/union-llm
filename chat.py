"""Chat LAN: una generacion y un modelo persistente repartido entre los dispositivos disponibles."""
import argparse
import fcntl
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

from cluster import BIN, PROJECT, LOGS, Cluster, NodeError, cpu_args, node_aliases

SETTINGS = json.loads((PROJECT / 'chat.json').read_text())
WEB = PROJECT / 'web'


def lan_address():
    inventory = Path('/root/.config/union-lan/nodos.json')
    # Consultar la ruta de la LAN no manda paquetes ni necesita Internet.
    destination = '192.168.1.10'
    if inventory.exists():
        data = json.loads(inventory.read_text())
        nodes = data.get('nodos', {})
        if isinstance(nodes, dict):
            for node in nodes.values():
                destination = node.get('ip', destination)
                break
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((destination, 8022))
        return probe.getsockname()[0]


def local_address(value):
    try:
        address = ipaddress.IPv4Address(value)
        networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', '169.254.0.0/16')
        if any(address in ipaddress.ip_network(net) for net in networks):
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('Usa una direccion IPv4 de la LAN o 127.0.0.1.')


def model_path(manifest_name):
    """Ruta del modelo de un manifiesto, comprobando que el archivo este completo."""
    manifest = json.loads((PROJECT / manifest_name).read_text())
    model = PROJECT / 'modelos' / manifest['archivo']
    if not model.exists() or model.stat().st_size != manifest['bytes']:
        raise FileNotFoundError(f'Falta el modelo completo de {manifest_name}. Descargalo con descargar_modelo.py.')
    return model


def draft_model():
    name = SETTINGS.get('modelo_borrador_manifest')
    return model_path(name) if name else None


def engine_command(cluster, model, draft=None):
    command = [str(BIN / 'llama-server'), '-m', str(model), *cpu_args(cluster.found['central']),
               '-c', str(SETTINGS['contexto']), '-b', '128', '-ub', '128', '--parallel', '1', '--host', '127.0.0.1',
               '--port', str(SETTINGS['puerto_motor']), *cluster.engine_args(), '-fit', 'off',
               '--alias', 'union-llm', '--offline', '--no-warmup', '--no-webui', '--verbosity', '4',
               # Pensar antes de responder multiplica los tokens; a esta velocidad se desactiva por defecto.
               '--reasoning', 'on' if SETTINGS.get('razonar') else 'off']
    if draft:
        # El borrador corre en la CPU del central y propone varios tokens que el modelo repartido
        # verifica en una sola pasada: cada viaje por la Wi-Fi rinde varios tokens.
        threads = cpu_args(cluster.found['central'], None)
        command += ['-md', str(draft), '-ngld', '0', '--spec-type', 'draft-simple',
                    '--spec-draft-n-max', str(SETTINGS['borrador_max']),
                    '--spec-draft-p-min', str(SETTINGS.get('borrador_p_min', 0)), '-td', threads[1]]
        if '-C' in threads:
            command += ['-Cd', threads[threads.index('-C') + 1], '--cpu-strict-draft', '1']
    return command


def draft_memory(draft):
    """Pesos del borrador mas margen para su cache KV y su calculo."""
    return int(draft.stat().st_size * 1.3) if draft else 0


class ModelService:
    def __init__(self, enabled=True):
        self.guard = threading.Lock()
        self.phase = 'conectando' if enabled else 'interfaz'
        self.message = 'Buscando dispositivos…' if enabled else 'Vista de la interfaz; el modelo esta apagado.'
        self.active = None
        self.stop = threading.Event()
        self.cluster = None
        self.engine = None
        self.thread = None
        self.attempts = 0
        try:
            self.nodes = {name: 'Buscando…' for name in ['central', *node_aliases()]}
        except RuntimeError:
            self.nodes = {'central': 'Buscando…'}
        if enabled:
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def update(self, phase, message):
        with self.guard:
            self.phase, self.message = phase, message
        print(message, flush=True)

    def status(self):
        with self.guard:
            ready = self.phase in ('cargando', 'listo')
            nodes = self.nodes
            return {'fase': self.phase, 'mensaje': self.message, 'ocupado': self.active is not None,
                    'modelo': SETTINGS['modelo_nombre'], 'contexto': SETTINGS['contexto'],
                    'max_tokens': SETTINGS['max_tokens'],
                    'en_uso': sum(1 for detail in nodes.values() if detail.endswith('capas')),
                    'nodos': [{'nombre': name, 'conectado': ready and detail.endswith('capas'), 'detalle': detail}
                              for name, detail in nodes.items()]}

    def describe_nodes(self, penalized):
        """Estado legible de cada dispositivo para la interfaz."""
        nodes = {}
        for name in ['central', *node_aliases()]:
            if name in self.cluster.layers:
                nodes[name] = f'{self.cluster.layers[name]} capas'
            elif name in penalized:
                nodes[name] = 'Apartado tras un fallo'
            elif name in self.cluster.missing:
                nodes[name] = 'Sin conexion'
            else:
                nodes[name] = 'Disponible, no hace falta'
        with self.guard:
            self.nodes = nodes

    def run(self):
        """Mantiene el modelo cargado; si cae un dispositivo, reinicia con los disponibles."""
        penalized = {}
        attempt = 0
        while not self.stop.is_set():
            attempt += 1
            self.attempts += 1
            now = time.monotonic()
            penalized = {alias: until for alias, until in penalized.items() if until > now}
            try:
                self.serve(penalized)
                return
            except FileNotFoundError as exc:
                self.update('error', str(exc))
                return
            except NodeError as exc:
                # Se aparta 5 minutos para no repetir el mismo fallo; luego se vuelve a probar.
                penalized[exc.alias] = time.monotonic() + 300
                reason = f'Fallo {exc}.'
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                reason = str(exc)
            if self.phase == 'listo':
                attempt = 1
            wait = min(60, 5 * 2 ** (attempt - 1))
            self.update('reconectando', f'{reason} Nuevo intento con los dispositivos disponibles en {wait} s.')
            if self.stop.wait(wait):
                return

    def serve(self, penalized):
        handle = None
        try:
            if not (BIN / 'llama-server').exists():
                raise FileNotFoundError('Falta el motor. Ejecuta ./preparar.sh --chat.')
            model = model_path(SETTINGS['modelo_manifest'])
            draft = draft_model()
            self.update('conectando', 'Buscando dispositivos y calculando el reparto…')
            self.cluster = Cluster(model, log_prefix='chat-', skip=penalized, central_extra=draft_memory(draft))
            try:
                self.cluster.start()
            finally:
                if self.cluster.layers:
                    self.describe_nodes(penalized)
            if self.stop.is_set():
                return
            count = len(self.cluster.layers)
            devices = '1 dispositivo' if count == 1 else f'{count} dispositivos'
            self.update('cargando', f'Cargando las capas del modelo en {devices}…')
            handle = (LOGS / 'chat-motor.log').open('w')
            self.engine = subprocess.Popen(engine_command(self.cluster, model, draft),
                                           stdout=handle, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 3600
            ready = False
            while not self.stop.wait(1):
                failed = self.cluster.failed_node()
                if failed:
                    raise NodeError(failed, 'dejo de responder')
                if self.engine.poll() is not None:
                    raise RuntimeError('Se detuvo el motor; revisa logs/chat-motor.log.')
                if not ready:
                    connection = http.client.HTTPConnection('127.0.0.1', SETTINGS['puerto_motor'], timeout=2)
                    try:
                        connection.request('GET', '/health')
                        response = connection.getresponse()
                        response.read()
                        if response.status == 200:
                            ready = True
                            self.update('listo', f'{devices}: listo.' if count == 1 else f'{devices}: listos.')
                    except OSError:
                        pass
                    finally:
                        connection.close()
                    if not ready and time.monotonic() > deadline:
                        raise RuntimeError('La carga tardo mas de 60 minutos; revisa logs/chat-motor.log.')
        finally:
            self.cancel()
            if self.engine and self.engine.poll() is None:
                self.engine.terminate()
                try:
                    self.engine.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.engine.kill()
                    self.engine.wait()
            self.engine = None
            if self.cluster:
                self.cluster.close()
            if handle:
                handle.close()

    def claim(self, request_id):
        with self.guard:
            if self.phase != 'listo':
                return 503, self.message
            if self.active:
                return 409, 'El modelo esta respondiendo otra pregunta. Espera a que termine.'
            self.active = {'id': request_id, 'cancel': threading.Event(), 'connection': None, 'socket': None}
            return 200, self.active

    def cancel(self, request_id=None):
        with self.guard:
            active = self.active
            if not active or (request_id is not None and active['id'] != request_id):
                return False
            active['cancel'].set()
            connection = active['connection']
            upstream_socket = active['socket'] or (connection.sock if connection else None)
            if upstream_socket:
                try:
                    upstream_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return True

    def release(self, active):
        with self.guard:
            if self.active is active:
                self.active = None

    def close(self):
        self.stop.set()
        self.cancel()
        if self.thread:
            self.thread.join(timeout=120)


def validate_chat(payload):
    messages = payload.get('messages')
    request_id = payload.get('id')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError('Falta el identificador de la pregunta.')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
        raise ValueError('Escribe una pregunta o inicia un chat nuevo.')
    clean = []
    for index, item in enumerate(messages):
        role = 'user' if index % 2 == 0 else 'assistant'
        if (not isinstance(item, dict) or item.get('role') != role or
                not isinstance(item.get('content'), str) or not item['content'].strip()):
            raise ValueError('La conversacion debe alternar preguntas y respuestas.')
        clean.append({'role': role, 'content': item['content']})
    if clean[-1]['role'] != 'user':
        raise ValueError('La conversacion debe terminar con una pregunta.')
    tokens = payload.get('max_tokens', SETTINGS['max_tokens'])
    if type(tokens) is not int or not 1 <= tokens <= SETTINGS['max_tokens']:
        raise ValueError('El limite de respuesta no es valido.')
    return request_id, clean, tokens


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/estado':
            return self.json(200, self.server.service.status())
        assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                  '/style.css': ('style.css', 'text/css')}
        if path not in assets:
            return self.json(404, {'error': 'No encontrado.'})
        filename, mime = assets[path]
        body = (WEB / filename).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.json(403, {'error': 'Abre el chat desde su direccion de la LAN.'})
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.json(415, {'error': 'Se requiere JSON.'})
            size = int(self.headers.get('Content-Length', '0'))
            if not 1 <= size <= 131072:
                return self.json(413, {'error': 'La pregunta es demasiado grande.'})
            self.connection.settimeout(30)
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('El mensaje no es valido.')
            if self.path == '/api/detener':
                request_id = payload.get('id')
                if not isinstance(request_id, str) or not request_id:
                    raise ValueError('Falta el identificador de la pregunta.')
                return self.json(200, {'detenido': self.server.service.cancel(request_id)})
            if self.path != '/api/chat':
                return self.json(404, {'error': 'No encontrado.'})
            request_id, messages, tokens = validate_chat(payload)
        except (ValueError, TypeError, OSError) as exc:
            return self.json(400, {'error': str(exc)})
        status, active = self.server.service.claim(request_id)
        if status != 200:
            return self.json(status, {'error': active})
        self.generate(active, messages, tokens)

    def event(self, name, data):
        self.wfile.write(('event: ' + name + '\ndata: ' + json.dumps(data, ensure_ascii=False) + '\n\n').encode())
        self.wfile.flush()

    def generate(self, active, messages, tokens):
        connection = None
        streaming = False
        start = time.monotonic()
        try:
            full = [{'role': 'system', 'content': SETTINGS['system_prompt']}, *messages]
            trimmed = False
            while True:
                connection = http.client.HTTPConnection('127.0.0.1', SETTINGS['puerto_motor'], timeout=1200)
                with self.server.service.guard:
                    active['connection'] = connection
                if active['cancel'].is_set():
                    raise InterruptedError('Pregunta detenida.')
                connection.request('POST', '/v1/chat/completions/input_tokens',
                                   json.dumps({'model': 'union-llm', 'messages': full}), {'Content-Type': 'application/json'})
                with self.server.service.guard:
                    active['socket'] = connection.sock
                if active['cancel'].is_set():
                    raise InterruptedError('Pregunta detenida.')
                response = connection.getresponse()
                result = json.loads(response.read())
                connection.close()
                if response.status != 200:
                    raise RuntimeError('No se pudo preparar la pregunta: ' + str(result.get('error', result)))
                if result['input_tokens'] + tokens + 16 <= SETTINGS['contexto']:
                    break
                if len(full) <= 2:
                    raise ValueError('Esta pregunta supera el contexto del modelo. Reduce el texto o el codigo.')
                del full[1:3]
                trimmed = True
            connection = http.client.HTTPConnection('127.0.0.1', SETTINGS['puerto_motor'], timeout=1200)
            with self.server.service.guard:
                active['connection'] = connection
            if active['cancel'].is_set():
                raise InterruptedError('Pregunta detenida.')
            connection.request('POST', '/v1/chat/completions', json.dumps({
                'model': 'union-llm', 'messages': full, 'max_tokens': tokens, 'temperature': 0.3,
                'stream': True, 'stream_options': {'include_usage': True}}), {'Content-Type': 'application/json'})
            with self.server.service.guard:
                active['socket'] = connection.sock
            if active['cancel'].is_set():
                raise InterruptedError('Pregunta detenida.')
            response = connection.getresponse()
            if response.status != 200:
                detail = response.read().decode(errors='replace')
                raise RuntimeError('El motor no pudo responder: ' + detail[:500])
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            streaming = True
            self.event('inicio', {'historial_recortado': trimmed})
            usage, finish, done = {}, None, False
            while not active['cancel'].is_set():
                line = response.readline()
                if not line:
                    break
                if not line.startswith(b'data: '):
                    continue
                if line.strip() == b'data: [DONE]':
                    done = True
                    break
                chunk = json.loads(line[6:])
                if chunk.get('error'):
                    raise RuntimeError(str(chunk['error']))
                if chunk.get('usage'):
                    usage = chunk['usage']
                for choice in chunk.get('choices', []):
                    text = choice.get('delta', {}).get('content')
                    if text:
                        self.event('token', {'texto': text})
                    finish = choice.get('finish_reason') or finish
            if active['cancel'].is_set():
                self.event('fin', {'detenido': True})
            elif not done:
                raise RuntimeError('Se interrumpio la respuesta del modelo. Comprueba los dispositivos.')
            else:
                self.event('fin', {'segundos': round(time.monotonic() - start, 1),
                                   'tokens': usage.get('completion_tokens'), 'limite': finish == 'length'})
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
            try:
                message = 'Pregunta detenida.' if active['cancel'].is_set() else str(exc)
                if streaming:
                    self.event('error', {'error': message})
                else:
                    self.json(400 if isinstance(exc, ValueError) else 502, {'error': message})
            except OSError:
                pass
        finally:
            if connection:
                connection.close()
            self.server.service.release(active)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', type=local_address, default=None, help='IPv4 de la LAN; se detecta por defecto.')
    parser.add_argument('--port', type=int, default=SETTINGS['puerto_chat'])
    parser.add_argument('--solo-interfaz', action='store_true', help='Vista sin arrancar el modelo, para comprobar la interfaz.')
    args = parser.parse_args()
    with (PROJECT / '.ejecucion.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Ya hay un chat o una prueba utilizando los dispositivos.')
        host = args.host or local_address(lan_address())
        server = ThreadingHTTPServer((host, args.port), Handler)
        server.daemon_threads = True
        server.service = ModelService(enabled=not args.solo_interfaz)
        def terminate(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        print(f'Chat LAN: http://{host}:{args.port}\nCtrl+C para detener el chat y los trabajadores.', flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()
            server.service.close()


if __name__ == '__main__':
    main()
