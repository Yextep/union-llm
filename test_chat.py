"""Pruebas HTTP del chat sin descargar ni cargar un LLM."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest

import chat


class FakeEngine(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.endswith('input_tokens'):
            body = json.dumps({'input_tokens': sum(len(m['content']) for m in payload['messages'])}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.server.received.append(payload)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"Hola"}}]}\n\n')
        self.wfile.flush()
        self.server.started.set()
        self.server.finish.wait(10)
        try:
            self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":1}}\n\ndata: [DONE]\n\n')
        except OSError:
            pass


class ChatIntegration(unittest.TestCase):
    def setUp(self):
        self.old_port = chat.SETTINGS['puerto_motor']
        self.engine = ThreadingHTTPServer(('127.0.0.1', 0), FakeEngine)
        self.engine.daemon_threads = True
        self.engine.started = threading.Event()
        self.engine.finish = threading.Event()
        self.engine.received = []
        chat.SETTINGS['puerto_motor'] = self.engine.server_port
        self.service = chat.ModelService(enabled=False)
        self.service.phase = 'listo'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), chat.Handler)
        self.server.daemon_threads = True
        self.server.service = self.service
        for server in (self.engine, self.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.service.cancel()
        self.engine.finish.set()
        for server in (self.server, self.engine):
            server.shutdown()
            server.server_close()
        chat.SETTINGS['puerto_motor'] = self.old_port

    def post(self, path, payload, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = origin
        connection.request('POST', path, json.dumps(payload), headers)
        response = connection.getresponse()
        return connection, response

    def test_stream_and_single_generation(self):
        first, response = self.post('/api/chat', {'id':'one', 'messages':[{'role':'user','content':'Hola'}]})
        self.assertEqual(response.status, 200)
        self.assertTrue(self.engine.started.wait(2))
        second, busy = self.post('/api/chat', {'id':'two', 'messages':[{'role':'user','content':'Otra'}]})
        self.assertEqual(busy.status, 409)
        busy.read(); second.close()
        self.engine.finish.set()
        stream = response.read().decode()
        first.close()
        self.assertIn('event: token', stream)
        self.assertIn('Hola', stream)
        self.assertIn('event: fin', stream)
        self.assertEqual(len(self.engine.received), 1)
        self.assertFalse(self.service.status()['ocupado'])

    def test_stop_interrupts_a_silent_upstream(self):
        connection, response = self.post('/api/chat', {'id':'stop-me','messages':[{'role':'user','content':'Hola'}]})
        self.assertEqual(response.status, 200)
        wrong, denied = self.post('/api/detener', {'id':'different'})
        self.assertFalse(json.loads(denied.read())['detenido']); wrong.close()
        self.assertTrue(self.service.status()['ocupado'])
        cancel, result = self.post('/api/detener', {'id':'stop-me'})
        self.assertTrue(json.loads(result.read())['detenido']); cancel.close()
        started = time.monotonic()
        self.assertIn('"detenido": true', response.read().decode())
        self.assertLess(time.monotonic() - started, 2)
        connection.close()
        self.assertFalse(self.service.status()['ocupado'])

    def test_history_is_trimmed_in_pairs_and_current_question_kept(self):
        messages = [{'role':'user','content':'A' * 1800}, {'role':'assistant','content':'B' * 100},
                    {'role':'user','content':'La pregunta actual'}]
        self.engine.finish.set()
        connection, response = self.post('/api/chat', {'id':'trim','messages':messages})
        stream = response.read().decode(); connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn('"historial_recortado": true', stream)
        actual = self.engine.received[0]['messages']
        self.assertEqual([m['role'] for m in actual], ['system', 'user'])
        self.assertEqual(actual[-1]['content'], 'La pregunta actual')

    def test_rejects_large_question_and_cross_origin_post(self):
        connection, response = self.post('/api/chat', {'id':'large','messages':[{'role':'user','content':'A' * 3000}]})
        self.assertEqual(response.status, 400)
        self.assertIn('contexto', response.read().decode()); connection.close()
        connection, response = self.post('/api/chat', {'id':'origin','messages':[{'role':'user','content':'Hola'}]}, 'https://example.com')
        self.assertEqual(response.status, 403); response.read(); connection.close()
        self.assertEqual(self.engine.received, [])


if __name__ == '__main__':
    unittest.main()
