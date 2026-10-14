"""Pruebas del reparto por memoria y de la recuperacion ante fallos, sin dispositivos reales."""
import threading
import unittest
from unittest import mock

import chat
import cluster

MIB = cluster.MIB
LAYOUT = {'capas': [100 * MIB] * 10, 'salida': 50 * MIB, 'entrada': 20 * MIB, 'expertos': 0}


def info(available_mib):
    return {'memoria_kib': {'MemAvailable': available_mib * 1024}}


class Split(unittest.TestCase):
    def test_proportional_and_output_on_last(self):
        split = cluster.split_layers(LAYOUT, {'a': 400 * MIB, 'b': 400 * MIB, 'c': 850 * MIB})
        self.assertEqual(sum(split.values()), 10)
        self.assertEqual(list(split), ['a', 'b', 'c'])
        # Con la misma fraccion de memoria, el ultimo recibe mas capas pero tambien la salida.
        self.assertLessEqual(split['a'] * 100, 400)
        self.assertLessEqual(split['c'] * 100 + 50, 850)

    def test_does_not_fit(self):
        self.assertIsNone(cluster.split_layers(LAYOUT, {'a': 500 * MIB, 'b': 520 * MIB}))

    def test_minimum_mode_uses_fewest_devices(self):
        found = {'central': info(1200 + 20 + 300), 'm1': info(800 + 900), 'm2': info(800 + 400)}
        with mock.patch.dict(cluster.CONFIG, dispositivos='minimo', reserva_mib=800, reserva_central_mib=1200):
            layers, _ = cluster.plan(LAYOUT, found)
        self.assertEqual(list(layers), ['central', 'm1'])
        with mock.patch.dict(cluster.CONFIG, dispositivos='todos', reserva_mib=800, reserva_central_mib=1200):
            layers, _ = cluster.plan(LAYOUT, found)
        self.assertEqual(list(layers), ['central', 'm2', 'm1'])

    def test_previous_split_is_kept_while_it_fits(self):
        found = {'central': info(1200 + 20 + 500), 'm1': info(800 + 900)}
        previous = {'central': 3, 'm1': 7}
        with mock.patch.dict(cluster.CONFIG, dispositivos='minimo', reserva_mib=800, reserva_central_mib=1200):
            self.assertEqual(cluster.plan(LAYOUT, found, previous=previous)[0], previous)
            # Si ya no cabe en m1 (7 capas y la salida), se calcula de nuevo.
            found['m1'] = info(800 + 700)
            found['central'] = info(1200 + 20 + 900)
            self.assertNotEqual(cluster.plan(LAYOUT, found, previous=previous)[0], previous)

    def test_previous_split_survives_a_small_memory_change_elsewhere(self):
        # m2 tiene ahora un poco mas de memoria que m1, pero el reparto anterior en m1 sigue cabiendo.
        found = {'central': info(1200 + 20 + 500), 'm1': info(800 + 900), 'm2': info(800 + 950)}
        previous = {'central': 3, 'm1': 7}
        with mock.patch.dict(cluster.CONFIG, dispositivos='minimo', reserva_mib=800, reserva_central_mib=1200):
            self.assertEqual(cluster.plan(LAYOUT, found, previous=previous)[0], previous)

    def test_plan_explains_missing_memory(self):
        with mock.patch.dict(cluster.CONFIG, reserva_mib=800, reserva_central_mib=1200):
            with self.assertRaisesRegex(RuntimeError, 'no cabe'):
                cluster.plan(LAYOUT, {'central': info(1300)})

    def test_engine_args_with_local_central(self):
        fake = cluster.Cluster.__new__(cluster.Cluster)
        fake.layout, fake.layers = LAYOUT, {'central': 3, 'm2': 3, 'm1': 4}
        fake.workers, fake.endpoints = ['m2', 'm1'], ['127.0.0.1:1', '127.0.0.1:2']
        self.assertEqual(fake.engine_args(), ['--rpc', '127.0.0.1:1,127.0.0.1:2', '-ngl', '8',
                                              '-sm', 'layer', '-ts', '3,5'])


class FakeCluster:
    """Primer arranque: falla movil4. Segundo: arranca sin el y se queda listo."""
    starts = []

    def __init__(self, model, log_prefix='', skip=(), central_extra=0):
        self.skip = set(skip)
        self.layers, self.missing = {}, {}
        FakeCluster.starts.append(self.skip)

    def start(self):
        if len(FakeCluster.starts) == 1:
            raise cluster.NodeError('movil4', 'su trabajador RPC termino al arrancar')
        self.layers = {'central': 5, 'movil2': 5}
        raise RuntimeError('fin de la prueba')

    def close(self):
        pass


class Recovery(unittest.TestCase):
    def test_restarts_without_the_failed_node(self):
        FakeCluster.starts = []
        service = chat.ModelService(enabled=False)
        service.stop = threading.Event()
        with mock.patch.object(chat, 'Cluster', FakeCluster), \
                mock.patch.object(chat.Path, 'exists', return_value=True), \
                mock.patch.object(chat.Path, 'stat', return_value=mock.Mock(st_size=chat.json.loads(
                    (chat.PROJECT / chat.SETTINGS['modelo_manifest']).read_text())['bytes'])), \
                mock.patch.object(service.stop, 'wait', side_effect=[False, True]):
            service.run()
        self.assertEqual(FakeCluster.starts, [set(), {'movil4'}])
        state = service.status()
        self.assertEqual(state['fase'], 'reconectando')
        self.assertEqual({node['nombre']: node['detalle'] for node in state['nodos']}['movil4'],
                         'Apartado tras un fallo')


if __name__ == '__main__':
    unittest.main()
