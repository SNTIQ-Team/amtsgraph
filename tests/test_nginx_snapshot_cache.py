"""Exercise the actual deployment location in an isolated nginx + synthetic source.

Run directly with Python's stdlib (no production API, database or pytest needed):
    python3 tests/test_nginx_snapshot_cache.py
Set NGINX_BIN only to choose an already installed nginx executable.
"""
import contextlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request


def location_block(config, prefix):
    marker = f'location {prefix} {{'
    start = config.index(marker)
    brace = config.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (config[end] == '{') - (config[end] == '}')
        end += 1
    return config[start:end]


class SnapshotCacheTest(unittest.TestCase):
    def setUp(self):
        nginx = os.environ.get('NGINX_BIN') or shutil.which('nginx')
        if not nginx:
            self.skipTest('nginx is not installed; run this test on the deployment host')
        self.tmp = tempfile.TemporaryDirectory(prefix='q-lwm-nginx-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = {'snapshot': 'release-one', 'requests': 0}
        state = self.state

        class Source(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                state['requests'] += 1
                requested = self.headers.get('If-Lexgraph-Snapshot')
                status = 409 if requested and requested != state['snapshot'] else 200
                body = json.dumps({'status': 'stale_snapshot' if status == 409 else 'ok',
                                   'snapshot': state['snapshot']}).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.send_header('X-Lexgraph-Snapshot', state['snapshot'])
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.source = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Source)
        thread = threading.Thread(target=self.source.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.source.server_close)
        self.addCleanup(self.source.shutdown)
        config = (Path(__file__).resolve().parents[1] / 'deploy/nginx-api.conf').read_text()
        # Reuse production maps/cache directives and the exact /lex location;
        # only isolate paths, ports, cache size, timeouts and diagnostic header.
        prefix = config[:config.index('\nserver {')]
        prefix = re.sub(r'proxy_cache_path[^;]+;',
                        f'proxy_cache_path {root}/cache levels=1:2 keys_zone=sntiq_cache:1m max_size=2m;', prefix)
        block = location_block(config, '/lex/')
        block = block.replace('http://127.0.0.1:8002/', f'http://127.0.0.1:{self.source.server_port}/')
        block = block.replace('proxy_read_timeout 30s;', 'proxy_read_timeout 1s; proxy_connect_timeout 1s;')
        block = block[:-1] + 'add_header X-Test-Cache $upstream_cache_status always;\n}'
        with contextlib.closing(socket.socket()) as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        conf = root / 'nginx.conf'
        conf.write_text(f'pid {root}/nginx.pid; error_log {root}/error.log;\n'
                        f'events {{ worker_connections 64; }}\nhttp {{ access_log off;\n'
                        f'{prefix}\nserver {{ listen 127.0.0.1:{self.port}; {block} }}\n}}\n')
        args = [nginx, '-p', str(root), '-c', str(conf)]
        valid = subprocess.run(args + ['-t'], capture_output=True, text=True)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.nginx = subprocess.Popen(args + ['-g', 'daemon off; master_process off;'],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            if self.nginx.poll() is None:
                self.nginx.terminate()
                self.nginx.wait(timeout=5)
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while True:
            self.assertIsNone(self.nginx.poll(), (root / 'error.log').read_text())
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    self.fail('isolated nginx did not listen')
                time.sleep(.02)

    def get(self, pin=None, origin=None):
        headers = {}
        if pin is not None:
            headers['If-Lexgraph-Snapshot'] = pin
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/lex/acts/synthetic/markdown?norm=1', headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=4)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            return response.status, data, response.headers.get('X-Test-Cache')

    def warm(self, origin=None):
        status, data, cache = self.get(origin=origin)
        self.assertEqual((status, data['snapshot'], cache), (200, 'release-one', 'MISS'))
        before = self.state['requests']
        self.assertEqual(self.get(origin=origin)[2], 'HIT')
        self.assertEqual(self.state['requests'], before)

    def test_warm_cache_does_not_ignore_a_snapshot_precondition(self):
        for origin in [None, 'https://sntiq.com']:
            self.warm(origin)
            for pin in ['wrong', '0', 'release-one']:
                before = self.state['requests']
                status, data, cache = self.get(pin, origin)
                self.assertEqual(status, 200 if pin == 'release-one' else 409, (pin, cache, data))
                self.assertEqual(cache, 'BYPASS')
                self.assertEqual(self.state['requests'], before + 1)
            self.assertEqual(self.get(origin=origin)[2], 'HIT')

    def test_publication_cannot_serve_or_write_the_wrong_generation(self):
        self.warm()
        self.state['snapshot'] = 'release-two'
        self.assertEqual(self.get('release-two'), (200, {'status': 'ok', 'snapshot': 'release-two'}, 'BYPASS'))
        self.assertEqual(self.get(), (200, {'status': 'ok', 'snapshot': 'release-one'}, 'HIT'), 'pinned success must not write the ordinary cache')
        self.assertEqual(self.get('release-one')[0], 409, 'old matching cache must not mask the current worker precondition')
        self.assertEqual(self.get('release-two')[2], 'BYPASS')

    def test_source_outage_does_not_substitute_stale_text_for_pinned_request(self):
        self.warm()
        self.source.shutdown()
        self.source.server_close()
        status, data, cache = self.get('release-one')
        self.assertIn(status, (502, 504))
        self.assertIsNone(data)
        self.assertEqual(cache, 'BYPASS')
        self.assertEqual(self.get()[0], 200, 'ordinary public stale fallback remains available')


if __name__ == '__main__':
    unittest.main(verbosity=2)
