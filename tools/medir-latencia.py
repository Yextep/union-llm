"""Latencia de ida y vuelta hacia cada movil: ping por Wi-Fi y TCP por el tunel SSH del clúster."""
import json
from pathlib import Path
import re
import shlex
import socket
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cluster

ECHO = '''import socket
server = socket.socket(); server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", 60190)); server.listen(1); print("listo", flush=True)
client, _ = server.accept(); client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
while (data := client.recv(4096)):
    if len(data) >= 9:
        client.sendall(data[:1])
'''


def tcp_round_trips(alias, samples=200, split=False, tty=False):
    """Con split, imita a RPC: cabecera y datos en dos envios antes de esperar respuesta."""
    remote = shlex.join(['proot-distro', 'login', 'debian', '--', 'python3', '-c', ECHO])
    options = [option for option in cluster.SSH_OPTIONS if option != '-T'] + ['-tt'] if tty else cluster.SSH_OPTIONS
    tunnel = subprocess.Popen(['ssh', *options, '-o', 'ExitOnForwardFailure=yes',
                               '-L', '127.0.0.1:60191:127.0.0.1:60190', alias, remote],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        if 'listo' not in tunnel.stdout.readline():
            raise RuntimeError('no arranco el eco en ' + alias)
        with socket.create_connection(('127.0.0.1', 60191), timeout=10) as connection:
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            times = []
            for _ in range(samples):
                start = time.perf_counter()
                if split:
                    connection.sendall(b'h' * 9)
                    connection.sendall(b'd' * 100)
                else:
                    connection.sendall(b'x' * 9)
                connection.recv(64)
                times.append((time.perf_counter() - start) * 1000)
                time.sleep(0.02)
        return times
    finally:
        tunnel.terminate()
        tunnel.wait()


def ping(ip, samples=50):
    output = subprocess.run(['ping', '-c', str(samples), '-i', '0.2', ip], capture_output=True, text=True).stdout
    return [float(value) for value in re.findall(r'time=([0-9.]+)', output)]


def summary(values):
    values = sorted(values)
    return {'mediana_ms': round(statistics.median(values), 2), 'p90_ms': round(values[int(len(values) * 0.9)], 2),
            'max_ms': round(values[-1], 2), 'muestras': len(values)}


if __name__ == '__main__':
    inventory = json.loads(cluster.INVENTORY.read_text())['nodos']
    report = {}
    for alias in sys.argv[1:] or cluster.node_aliases():
        report[alias] = {'ping_wifi': summary(ping(inventory[alias]['ip'])),
                         'tcp_por_tunel_ssh': summary(tcp_round_trips(alias)),
                         'dos_envios_ssh_T': summary(tcp_round_trips(alias, split=True)),
                         'dos_envios_ssh_tt': summary(tcp_round_trips(alias, split=True, tty=True))}
        print(alias, json.dumps(report[alias]), flush=True)
    (cluster.RESULTS / 'optimizacion').mkdir(parents=True, exist_ok=True)
    (cluster.RESULTS / 'optimizacion' / 'latencia.json').write_text(json.dumps(report, indent=2) + '\n')
