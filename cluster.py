"""Inferencia por capas entre los dispositivos de Union LAN, usando llama.cpp RPC y SSH."""
import argparse
import concurrent.futures
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

import gguf_capas

PROJECT = Path(__file__).resolve().parent
REMOTE = '/root/proyectos/union-llm'
WORKER = REMOTE + '/runtime/ggml-rpc-server'
LOGS = PROJECT / 'logs'
RESULTS = PROJECT / 'resultados'
CONFIG = json.loads((PROJECT / 'config.json').read_text())
BIN = PROJECT / os.environ.get('UNION_LLM_BUILD', 'build') / 'bin'
INVENTORY = Path('/root/.config/union-lan/nodos.json')
MODEL = PROJECT / 'modelos' / json.loads((PROJECT / 'modelo.json').read_text())['archivo']
# Un corte de Wi-Fi de hasta 2 minutos no tumba el tunel; mas alla, el dispositivo se da por perdido.
SSH_OPTIONS = ['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30',
               '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=8']
MIB = 1024 * 1024
GIB = 1024 * MIB

# Detiene trabajadores huerfanos de ejecuciones anteriores y mide la memoria despues.
PROBE = '''python3 - <<'PY'
import hashlib, json, os, signal, time
from pathlib import Path
binary = Path(%(binary)r)
stopped = 0
if %(clean)r:
    for entry in Path('/proc').iterdir():
        try:
            command = (entry / 'cmdline').read_bytes().split(b'\\0')[0] if entry.name.isdigit() else b''
            if command.startswith(%(prefix)r) and command.endswith(b'/ggml-rpc-server'):
                os.kill(int(entry.name), signal.SIGTERM)
                stopped += 1
        except OSError:
            pass
    if stopped:
        time.sleep(2)
mem = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()
       if line.startswith(('MemTotal:', 'MemAvailable:'))}
frequencies = {}
for cpu in Path('/sys/devices/system/cpu').glob('cpu[0-9]*'):
    try:
        frequencies[int(cpu.name[3:])] = int((cpu / 'cpufreq/cpuinfo_max_freq').read_text())
    except (OSError, ValueError):
        pass
fast = sorted(cpu for cpu, top in frequencies.items() if top == max(frequencies.values()))
dotprod = 'asimddp' in Path('/proc/cpuinfo').read_text()
print(json.dumps({'cpu': os.cpu_count(), 'rapidos': fast, 'dotprod': dotprod, 'memoria_kib': mem,
                  'huerfanos_detenidos': stopped,
                  'binario': hashlib.sha256(binary.read_bytes()).hexdigest() if binary.exists() else None}))
PY
'''


def remote_command(alias, script, timeout=60):
    command = ['ssh', *SSH_OPTIONS, alias, 'proot-distro login debian -- /bin/bash -s']
    return subprocess.run(command, input=script, text=True, capture_output=True, timeout=timeout, check=True)


def describe(exc):
    if isinstance(exc, subprocess.TimeoutExpired):
        return 'no respondio a tiempo'
    if isinstance(exc, subprocess.CalledProcessError):
        lines = (exc.stderr or '').strip().splitlines()
        return lines[-1] if lines else 'termino con codigo ' + str(exc.returncode)
    return str(exc)


def node_aliases():
    nodes = CONFIG.get('nodos', 'auto')
    if nodes == 'auto':
        try:
            nodes = sorted(json.loads(INVENTORY.read_text())['nodos'])
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError('No se pudo leer el inventario de Union LAN: ' + str(exc))
    excluded = set(CONFIG.get('excluir', []))
    return [alias for alias in nodes if alias not in excluded]


def probe(alias, clean=False):
    binary = str(BIN / 'ggml-rpc-server') if alias == 'central' else WORKER
    script = PROBE % {'binary': binary, 'clean': clean, 'prefix': os.fsencode(REMOTE + '/')}
    if alias == 'central':
        result = subprocess.run(['bash', '-s'], input=script, text=True, capture_output=True, timeout=60, check=True)
    else:
        result = remote_command(alias, script)
    return json.loads(result.stdout)


def discover(clean=False, skip=()):
    """Consulta a la vez el central y los moviles; los que no responden quedan fuera."""
    aliases = ['central', *[alias for alias in node_aliases() if alias not in skip]]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(aliases)) as pool:
        jobs = {alias: pool.submit(probe, alias, clean) for alias in aliases}
    found, missing = {}, {}
    for alias, job in jobs.items():
        try:
            found[alias] = job.result()
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            missing[alias] = describe(exc)
    if 'central' not in found:
        raise RuntimeError('No se pudo consultar el central: ' + missing['central'])
    return found, missing


def build_needs_dotprod():
    cache = BIN.parent / 'CMakeCache.txt'
    lines = cache.read_text().splitlines() if cache.exists() else []
    return any(line.startswith('GGML_CPU_ARM_ARCH') and 'dotprod' in line for line in lines)


def worker_binary():
    """Trabajador para los moviles: estatico, para no depender de la version de Debian de cada uno."""
    static = BIN / 'ggml-rpc-server-estatico'
    return static if static.exists() else BIN / 'ggml-rpc-server'


def deploy(aliases=None):
    """Copia el trabajador RPC; devuelve los dispositivos en los que fallo."""
    binary = worker_binary()
    if not binary.exists():
        raise RuntimeError('Falta compilar. Ejecuta ./preparar.sh.')
    failed = {}
    with tempfile.NamedTemporaryFile(suffix='.tar.gz', dir=PROJECT) as bundle:
        with tarfile.open(bundle.name, 'w:gz') as archive:
            archive.add(binary, arcname='ggml-rpc-server')
            archive.add(PROJECT / 'llama.cpp/LICENSE', arcname='LICENSE-llama.cpp')
        for alias in node_aliases() if aliases is None else aliases:
            remote = shlex.join(['proot-distro', 'login', 'debian', '--', '/bin/bash', '-c',
                                f'mkdir -p {REMOTE}/runtime {REMOTE}/logs {REMOTE}/cache; '
                                f'tar -xzf - -C {REMOTE}/runtime; chmod 755 {WORKER}'])
            try:
                with open(bundle.name, 'rb') as stream:
                    subprocess.run(['ssh', *SSH_OPTIONS, alias, remote], stdin=stream, check=True,
                                   timeout=120, capture_output=True)
                result = remote_command(alias, f'{WORKER} --help\n')
                if 'number of threads' not in result.stderr:
                    raise RuntimeError('el ejecutable RPC no arranco')
                print('Trabajador desplegado y comprobado:', alias, flush=True)
            except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
                failed[alias] = describe(exc)
                # Sin borrarlo, su huella coincidiria y la proxima vez pareceria valido.
                with contextlib.suppress(subprocess.SubprocessError, OSError):
                    remote_command(alias, f'rm -f {WORKER}\n')
    return failed


def fast_cores(info):
    """Nucleos de mayor frecuencia si la configuracion pide usar solo esos."""
    if CONFIG['threads'] != 'rapidos' or not info.get('rapidos') or len(info['rapidos']) == info['cpu']:
        return []
    return info['rapidos']


def cpu_args(info, batch_flag='-tb'):
    """Hilos del coordinador, en los nucleos rapidos tanto para generar como para el prompt.

    Medido en el central con el 3B: anadir los nucleos lentos al prompt lo acelera poco y sus
    hilos, al esperar activamente, frenan despues la generacion; sin espera activa el prompt
    es mas lento que con dos hilos rapidos."""
    fast = fast_cores(info)
    if not fast:
        threads = str(CONFIG['threads'] if CONFIG['threads'] != 'rapidos' else info['cpu'])
        return ['-t', threads] + ([batch_flag, threads] if batch_flag else [])
    args = ['-t', str(len(fast)), '-C', hex(sum(1 << cpu for cpu in fast)), '--cpu-strict', '1']
    return args + ([batch_flag, str(len(fast))] if batch_flag else [])


def worker_command(info, binary, port):
    """Trabajador RPC fijado a los nucleos rapidos; los lentos frenan la generacion."""
    fast = fast_cores(info)
    threads = len(fast) if fast else (CONFIG['threads'] if CONFIG['threads'] != 'rapidos' else info['cpu'])
    prefix = ['taskset', '-c', ','.join(map(str, fast))] if fast else []
    return [*prefix, binary, '--host', '127.0.0.1', '--port', str(port), '--device', 'CPU',
            '--threads', str(threads), '--cache']


def usable_memory(alias, info, layout):
    """Memoria que puede ocupar el modelo sin dejar a Android sin margen."""
    reserve = CONFIG['reserva_central_mib'] if alias == 'central' else CONFIG['reserva_mib']
    usable = info['memoria_kib']['MemAvailable'] * 1024 - reserve * MIB
    if alias == 'central':
        # El coordinador conserva siempre los embeddings de entrada en su CPU.
        usable -= layout['entrada']
    return max(0, usable)


def split_layers(layout, memory):
    """Reparte capas contiguas en proporcion a la memoria util; la salida va al ultimo dispositivo."""
    sizes = layout['capas']

    def fill(fraction):
        counts, index = [], 0
        for position, usable in enumerate(memory.values()):
            limit = fraction * usable
            if position == len(memory) - 1:
                limit -= layout['salida']
                if limit < 0:
                    return None
            used = count = 0
            while index < len(sizes) and used + sizes[index] <= limit:
                used += sizes[index]
                index += 1
                count += 1
            counts.append(count)
        return counts if index == len(sizes) else None

    if fill(1) is None:
        return None
    low, high = 0.0, 1.0
    for _ in range(25):
        middle = (low + high) / 2
        if fill(middle) is None:
            low = middle
        else:
            high = middle
    return dict(zip(memory, fill(high)))


def fits(layout, split, memory):
    """Indica si un reparto concreto cabe en la memoria util actual."""
    index = 0
    for position, (alias, count) in enumerate(split.items()):
        used = sum(layout['capas'][index:index + count]) + (layout['salida'] if position == len(split) - 1 else 0)
        index += count
        if alias not in memory or used > memory[alias]:
            return False
    return index == len(layout['capas'])


def plan(layout, found, central_extra=0, previous=None):
    """Elige los dispositivos y las capas de cada uno.

    En modo "minimo" usa el menor numero de moviles en el que cabe el modelo, porque cada
    dispositivo anade viajes por la red en cada token; en modo "todos" usa todos.
    Si el reparto anterior usa los mismos dispositivos y sigue cabiendo, se conserva: asi cada
    movil recibe las mismas capas y las encuentra en su cache sin volver a transferirlas."""
    memory = {alias: usable_memory(alias, info, layout) for alias, info in found.items()}
    # Lo que el central necesita aparte, por ejemplo un modelo borrador para decodificacion especulativa.
    memory['central'] = max(0, memory['central'] - central_extra)
    workers = sorted((alias for alias in memory if alias != 'central'), key=memory.get, reverse=True)
    sizes = range(len(workers) + 1) if CONFIG.get('dispositivos', 'minimo') == 'minimo' else [len(workers)]
    for count in sizes:
        # El movil con mas memoria va al final porque recibe tambien la capa de salida.
        chosen = ['central', *reversed(workers[:count])]
        split = split_layers(layout, {alias: memory[alias] for alias in chosen})
        if split:
            # Con el mismo numero de dispositivos, el anterior es igual de rapido y ya esta en las caches.
            if previous and len(previous) == len(split) and fits(layout, previous, memory):
                return dict(previous), memory
            return split, memory
    need = (sum(layout['capas']) + layout['salida']) / GIB
    raise RuntimeError(f'El modelo no cabe en los dispositivos disponibles: necesita {need:.1f} GiB y hay '
                       f'{sum(memory.values()) / GIB:.1f} GiB utiles tras la reserva de memoria. '
                       'Anade dispositivos, cierra aplicaciones en los moviles o usa un modelo mas pequeno.')


def wait_port(port, process):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'El trabajador o tunel del puerto {port} termino antes de arrancar.')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f'No arranco el puerto {port}; consulta logs/.')


def stop_remote(alias):
    script = f'''if [ -f {REMOTE}/logs/rpc.pid ]; then
    rpc_pid=$(cat {REMOTE}/logs/rpc.pid)
    case "$rpc_pid" in ''|*[!0-9]*) exit 1;; esac
    if [ -r "/proc/$rpc_pid/cmdline" ] && tr '\\0' ' ' < "/proc/$rpc_pid/cmdline" | grep -qF 'union-llm/runtime/ggml-rpc-server'; then
        kill "$rpc_pid" 2>/dev/null || true
    fi
    rm -f {REMOTE}/logs/rpc.pid
fi
'''
    remote_command(alias, script, timeout=45)


def process_stats(pid):
    status = Path(f'/proc/{pid}/status').read_text()
    memory = {line.split(':')[0]: int(line.split()[1]) for line in status.splitlines()
              if line.startswith(('VmRSS:', 'VmHWM:'))}
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'cpu_seconds': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
            'rss_kib': memory.get('VmRSS'), 'peak_rss_kib': memory.get('VmHWM')}


def worker_stats(cluster):
    result = {}
    if 'central' in cluster.workers:
        result['central'] = process_stats(cluster.processes['central'].pid)
    script = f'''python3 - <<'PY'
import json, os
from pathlib import Path
pid = int(Path('{REMOTE}/logs/rpc.pid').read_text())
status = Path(f'/proc/{{pid}}/status').read_text()
memory = {{line.split(':')[0]: int(line.split()[1]) for line in status.splitlines() if line.startswith(('VmRSS:', 'VmHWM:'))}}
fields = Path(f'/proc/{{pid}}/stat').read_text().rsplit(')', 1)[1].split()
print(json.dumps({{'pid': pid, 'cpu_seconds': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'), 'rss_kib': memory.get('VmRSS'), 'peak_rss_kib': memory.get('VmHWM')}}))
PY
'''
    for alias in cluster.started_remotes:
        result[alias] = json.loads(remote_command(alias, script).stdout)
    return result


class Cluster:
    def __init__(self, model=MODEL, log_prefix='', skip=(), central_extra=0):
        self.model = Path(model)
        self.central_extra = central_extra
        self.log_prefix = log_prefix
        self.skip = set(skip)
        self.processes = {}
        self.handles = []
        self.started_remotes = []
        self.endpoints = []
        self.workers = []
        self.layers = {}
        self.memory = {}
        self.missing = {}
        self.found = {}
        self.layout = None

    def prepare(self, update=True):
        """Detecta los dispositivos, actualiza su trabajador si hace falta y calcula el reparto."""
        self.layout = gguf_capas.layout(self.model)
        found, self.missing = discover(clean=True, skip=self.skip)
        if build_needs_dotprod():
            for alias in [alias for alias, info in found.items() if not info.get('dotprod', True)]:
                del found[alias]
                self.missing[alias] = 'su CPU no tiene dotprod; compila con UNION_LLM_ARCH=armv8-a ./preparar.sh'
        self.found = found
        local = hashlib.sha256(worker_binary().read_bytes()).hexdigest()
        outdated = [alias for alias, info in found.items() if alias != 'central' and info['binario'] != local]
        if outdated and update:
            print('Desplegando el trabajador en:', ', '.join(outdated), flush=True)
            for alias, reason in deploy(outdated).items():
                del found[alias]
                self.missing[alias] = 'no se pudo desplegar: ' + reason
        for alias, reason in self.missing.items():
            print(f'Sin usar {alias}: {reason}', file=sys.stderr, flush=True)
        saved = PROJECT / 'cache' / ('reparto-' + self.model.name + '.json')
        previous = json.loads(saved.read_text()) if saved.exists() else None
        self.layers, self.memory = plan(self.layout, found, self.central_extra, previous)
        if self.layers == previous:
            print('Se reutiliza el reparto anterior: las capas ya estan en la cache de cada movil.', flush=True)
        for alias, usable in self.memory.items():
            used = f'{self.layers[alias]} capas' if alias in self.layers else 'sin usar, no hace falta'
            print(f'{alias}: {used}; {usable / GIB:.2f} GiB utiles', flush=True)
        return self

    def start(self):
        self.prepare()
        port = CONFIG['puerto_rpc']
        env = dict(os.environ, GGML_RPC_NO_RDMA='1', LLAMA_CACHE=str(PROJECT / 'cache'))
        LOGS.mkdir(exist_ok=True)
        RESULTS.mkdir(exist_ok=True)
        last = list(self.layers)[-1]
        remotes = [alias for alias, count in self.layers.items() if alias != 'central' and (count or alias == last)]
        if CONFIG['central'] == 'rpc' and self.layers['central']:
            local_log = (LOGS / (self.log_prefix + 'rpc-central.log')).open('w')
            self.handles.append(local_log)
            local = subprocess.Popen(worker_command(self.found['central'], str(BIN / 'ggml-rpc-server'), port),
                                     stdout=local_log, stderr=subprocess.STDOUT, env=env)
            self.processes['central'] = local
            self.workers.append('central')
            wait_port(port, local)
            self.endpoints.append('127.0.0.1:' + str(port))
        for index, alias in enumerate(remotes, 1):
            log = (LOGS / (self.log_prefix + 'rpc-' + alias + '.log')).open('w')
            self.handles.append(log)
            script = (f'mkdir -p {REMOTE}/logs {REMOTE}/cache\n'
                      f'printf "%s\\n" "$$" > {REMOTE}/logs/rpc.pid\n'
                      f'exec env GGML_RPC_NO_RDMA=1 LLAMA_CACHE={REMOTE}/cache '
                      + shlex.join(worker_command(self.found[alias], WORKER, port)))
            remote = shlex.join(['proot-distro', 'login', 'debian', '--', '/bin/bash', '-c', script])
            tunnel_port = port + index
            process = subprocess.Popen(['ssh', *SSH_OPTIONS, '-o', 'ExitOnForwardFailure=yes',
                                        '-L', f'127.0.0.1:{tunnel_port}:127.0.0.1:{port}', alias, remote],
                                       stdout=log, stderr=subprocess.STDOUT)
            self.processes[alias] = process
            self.started_remotes.append(alias)
            self.workers.append(alias)
            try:
                wait_port(tunnel_port, process)
            except RuntimeError as exc:
                raise NodeError(alias, str(exc)) from None
            self.endpoints.append('127.0.0.1:' + str(tunnel_port))
        print('Trabajadores preparados:', ', '.join(self.workers) or 'solo la CPU del central', flush=True)
        # El tunel puede escuchar antes de que el servidor remoto este preparado.
        self.list_devices()
        saved = PROJECT / 'cache' / ('reparto-' + self.model.name + '.json')
        saved.parent.mkdir(exist_ok=True)
        saved.write_text(json.dumps(self.layers) + '\n')
        return self

    def failed_node(self):
        """Primer dispositivo cuyo proceso termino, o None."""
        for alias, process in self.processes.items():
            if process.poll() is not None:
                return alias
        return None

    def engine_args(self, separator=','):
        """Argumentos de llama.cpp que reproducen el reparto calculado."""
        n_layer = len(self.layout['capas'])
        if not self.workers:
            return ['-ngl', '0']
        local = 0 if 'central' in self.workers else self.layers['central']
        counts = [self.layers[alias] for alias in self.workers]
        counts[-1] += 1  # La capa de salida cuenta como una mas y va al ultimo trabajador.
        return ['--rpc', ','.join(self.endpoints), '-ngl', str(n_layer + 1 - local), '-sm', 'layer',
                '-ts', separator.join(map(str, counts))]

    def list_devices(self):
        if not self.endpoints:
            return
        command = [str(BIN / 'llama-bench'), '--rpc', ','.join(self.endpoints), '--list-devices']
        deadline = time.monotonic() + 30
        while True:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            if result.returncode == 0 and result.stdout.count('RPC') >= len(self.endpoints):
                (LOGS / (self.log_prefix + 'dispositivos.txt')).write_text(result.stdout + result.stderr)
                print(result.stdout, flush=True)
                return
            failed = self.failed_node()
            if failed:
                raise NodeError(failed, 'su trabajador RPC termino al arrancar')
            if time.monotonic() >= deadline:
                raise RuntimeError('No respondieron todos los trabajadores RPC: ' + result.stderr[-500:])
            time.sleep(0.5)

    def close(self):
        for alias in self.started_remotes:
            try:
                stop_remote(alias)
            except (subprocess.SubprocessError, OSError) as exc:
                print('No se pudo confirmar el apagado de ' + alias + ': ' + describe(exc), file=sys.stderr)
        for process in reversed(list(self.processes.values())):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for handle in self.handles:
            handle.close()


class NodeError(RuntimeError):
    """Fallo atribuible a un dispositivo concreto."""

    def __init__(self, alias, reason):
        super().__init__(f'{alias}: {reason}')
        self.alias = alias


@contextlib.contextmanager
def running_cluster(model=MODEL, log_prefix=''):
    cluster = Cluster(model, log_prefix)
    try:
        yield cluster.start()
    finally:
        cluster.close()


def central_info(cluster=None):
    return cluster.found['central'] if cluster else probe('central')


def benchmark(mode, cluster=None, repetitions=2, model=MODEL, prompt=32, tokens=16):
    command = [str(BIN / 'llama-bench'), '-m', str(model), *cpu_args(central_info(cluster), None),
               '-p', str(prompt), '-n', str(tokens), '-r', str(repetitions), '-b', '32', '-ub', '32', '-o', 'json']
    command += cluster.engine_args('/') if cluster else ['-ngl', '0']
    print('Midiendo:', mode, flush=True)
    start = time.monotonic()
    with (LOGS / ('benchmark-' + mode + '.log')).open('w') as errors:
        result = subprocess.run(command, capture_output=False, stdout=subprocess.PIPE,
                                stderr=errors, text=True, timeout=7200, check=True)
    measurements = json.loads(result.stdout)
    (RESULTS / (mode + '.json')).write_text(json.dumps(measurements, indent=2) + '\n')
    return {'modo': mode, 'segundos_totales_incluye_carga': time.monotonic() - start,
            'comando': command, 'mediciones': measurements,
            'reparto': cluster.layers if cluster else {'central': None}}


def generate(prompt, cluster=None, tokens=48, label='respuesta', model=MODEL):
    formatted = ('<|im_start|>system\nResponde en espanol de forma breve.<|im_end|>\n'
                 '<|im_start|>user\n' + prompt + '<|im_end|>\n<|im_start|>assistant\n')
    command = [str(BIN / 'llama-completion'), '-m', str(model), *cpu_args(central_info(cluster)),
               '-c', '512', '-n', str(tokens), '--temp', '0', '-s', '123', '--no-conversation',
               '--no-display-prompt', '--no-warmup', '--verbosity', '4', '-p', formatted]
    command += cluster.engine_args() if cluster else ['-ngl', '0']
    with (LOGS / (label + '.log')).open('w') as errors:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=errors, text=True, timeout=3600, check=True)
    (RESULTS / (label + '.txt')).write_text(result.stdout)
    print(result.stdout, flush=True)
    return result.stdout


def experiment(repetitions):
    found, missing = discover()
    metadata = {'fecha_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'configuracion': CONFIG, 'recursos': found, 'sin_usar': missing,
                'modelo': json.loads((PROJECT / 'modelo.json').read_text())}
    baseline = benchmark('central', repetitions=repetitions)
    prompt = 'Explica en una frase que es una red LAN.'
    response_local = generate(prompt, label='respuesta-central')
    with running_cluster() as cluster:
        before = worker_stats(cluster)
        distributed = benchmark('distribuido', cluster, repetitions)
        response_distributed = generate(prompt, cluster, label='respuesta-distribuida')
        after = worker_stats(cluster)
        workers = cluster.workers
    metadata.update({'central': baseline, 'distribuido': distributed, 'trabajadores': workers,
                     'texto_central': response_local, 'texto_distribuido': response_distributed,
                     'respuesta_identica': response_local.strip() == response_distributed.strip(),
                     'trabajadores_antes': before, 'trabajadores_despues': after})
    (RESULTS / 'experimento.json').write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + '\n')
    subprocess.run([sys.executable, str(PROJECT / 'resumen.py')], check=True)
    print('Experimento completo. Resultados en', RESULTS, flush=True)


def measure(model, repetitions, prompt, tokens, label):
    """Mide un modelo con el reparto automatico y guarda el resultado con su contexto."""
    found, missing = discover()
    with running_cluster(model, log_prefix=label + '-') as cluster:
        result = benchmark(label, cluster, repetitions, model, prompt, tokens)
    result.update({'fecha_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   'modelo': Path(model).name, 'configuracion': CONFIG, 'compilacion': BIN.parent.name,
                   'recursos': found, 'sin_usar': missing})
    folder = RESULTS / 'optimizacion'
    folder.mkdir(exist_ok=True)
    (folder / (label + '.json')).write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    (RESULTS / (label + '.json')).unlink(missing_ok=True)
    for row in result['mediciones']:
        kind = 'prompt' if row['n_prompt'] else 'generacion'
        print(f"{kind}: {row['avg_ts']:.2f} ± {row['stddev_ts']:.2f} tokens/s", flush=True)


def main():
    parser = argparse.ArgumentParser(description='LLM repartido por capas entre los dispositivos de Union LAN.')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('desplegar', help='Copiar y comprobar el trabajador RPC en los moviles')
    commands.add_parser('recursos', help='Consultar memoria y CPU en todos los dispositivos')
    split = commands.add_parser('reparto', help='Calcular el reparto de un modelo sin cargarlo')
    split.add_argument('--modelo', type=Path, default=MODEL)
    test = commands.add_parser('probar', help='Comparar central solo contra reparto entre dispositivos')
    test.add_argument('--repeticiones', type=int, default=2)
    bench = commands.add_parser('medir', help='Medir la velocidad de un modelo repartido')
    bench.add_argument('--modelo', type=Path, default=MODEL)
    bench.add_argument('--repeticiones', type=int, default=1)
    bench.add_argument('--prompt', type=int, default=32)
    bench.add_argument('--tokens', type=int, default=16)
    bench.add_argument('--nombre', default='medicion')
    question = commands.add_parser('preguntar', help='Generar texto con el LLM')
    question.add_argument('texto')
    question.add_argument('--modo', choices=['central', 'distribuido'], default='distribuido')
    question.add_argument('--tokens', type=int, default=48)
    args = parser.parse_args()
    if args.action == 'preguntar' and not 1 <= args.tokens <= 256:
        raise ValueError('El limite de salida debe estar entre 1 y 256 tokens.')
    for folder in (LOGS, RESULTS):
        folder.mkdir(exist_ok=True)
    with (PROJECT / '.ejecucion.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Ya hay otra ejecucion de union-llm en curso.')
        if args.action == 'desplegar':
            failed = deploy()
            if failed:
                raise RuntimeError('Fallo el despliegue en ' + '; '.join(f'{a}: {r}' for a, r in failed.items()))
        elif args.action == 'recursos':
            found, missing = discover()
            print(json.dumps({'disponibles': found, 'sin_respuesta': missing}, indent=2))
        elif args.action == 'reparto':
            Cluster(args.modelo).prepare(update=False)
        elif args.action == 'probar':
            if not 1 <= args.repeticiones <= 10:
                raise ValueError('Usa entre 1 y 10 repeticiones.')
            experiment(args.repeticiones)
        elif args.action == 'medir':
            measure(args.modelo, args.repeticiones, args.prompt, args.tokens, args.nombre)
        elif args.modo == 'central':
            generate(args.texto, tokens=args.tokens)
        else:
            with running_cluster() as cluster:
                generate(args.texto, cluster, args.tokens)


if __name__ == '__main__':
    # Con SIGTERM tambien se ejecuta la limpieza: sin ella quedarian trabajadores huerfanos.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('union-llm:', exc, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nEjecucion cancelada; se detuvieron los trabajadores de esta prueba.', file=sys.stderr)
        sys.exit(130)
