import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ctxfilter import auto
from ctxfilter.core import Limits, run_action, _stat_fields
from ctxfilter.mcp_server import handle_rpc

ROOT = Path(__file__).resolve().parents[1]


class AutoReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'input.txt'

    def write(self, text):
        self.path.write_bytes(text if isinstance(text, bytes) else text.encode())
        return str(self.path)

    def read(self, **kwargs):
        return auto.read_file(str(self.path), **kwargs)

    def test_small_empty_multibyte_and_many_lines(self):
        for content in ('', '日本語🙂' * 50, 'a\n' * 150, 'x' * 825):
            self.write(content)
            result = self.read()
            self.assertEqual(result['route'], 'full')
            self.assertEqual(result['text'], content)
            self.assertNotIn('identity', result)
            self.assertNotIn('limits', result)

    def test_threshold_boundary(self):
        for size, route in ((4095, 'full'), (4096, 'full'), (4097, 'preview')):
            self.write('a' * size)
            r = self.read(limits=Limits(max_output_bytes=8192))
            self.assertEqual(r['route'], route)
        self.write('a')
        self.assertEqual(self.read(small_bytes=0)['route'], 'preview')

    def test_serialized_budget_boundary_and_escaping(self):
        self.write('"\\\t\n' * 350)
        r = self.read(limits=Limits(max_output_bytes=16384))
        n = len(auto._encoded(r).encode())
        self.assertEqual(self.read(limits=Limits(max_output_bytes=n))['route'], 'full')
        self.assertEqual(self.read(limits=Limits(max_output_bytes=n-1))['route'], 'preview')
        for budget in (256, 512, 1800, 2500, 4096, 16384):
            r = self.read(limits=Limits(max_output_bytes=budget))
            self.assertLessEqual(len(auto._encoded(r).encode()), budget)
            if r['ok'] and r['route'] == 'preview':
                self.assertEqual(r['next_byte'], len(r['excerpt'].encode()))
                self.assertFalse(r['scan_complete'])

    def test_large_route_no_body_read_for_size(self):
        self.write('x' * 2_000_000)
        real_open = auto.open_regular
        class NoRead:
            def __init__(self, h): self.h = h
            def read(self, *a): raise AssertionError('large size decision read body')
            def fileno(self): return self.h.fileno()
            def close(self): self.h.close()
        def wrapped(p):
            h, st = real_open(p)
            return NoRead(h), st
        with patch.object(auto, 'open_regular', wrapped):
            r = self.read()
        self.assertTrue(r['ok'])
        self.assertEqual(r['route'], 'preview')
        self.assertEqual(r['reason'], 'size_threshold')
        self.assertLess(r['next_byte'], 2_000_000)
        tail = run_action('fetch', path=str(self.path), identity=r['identity'], byte_offset=1_999_999)
        self.assertEqual(tail['excerpt'], 'x')
        self.assertTrue(tail['scan_complete'])
        self.assertFalse(tail['truncated'])

    def test_binary_invalid_utf8_permissions_and_directory(self):
        self.write(b'a\x00b')
        r = self.read()
        self.assertEqual((r['route'], r['reason'], r['kind']), ('preview', 'binary', 'binary'))
        self.write(b'\xff')
        self.assertEqual(self.read()['error']['code'], 'invalid_utf8')
        self.path.chmod(0)
        try:
            self.assertEqual(self.read()['error']['code'], 'permission_denied')
        finally:
            self.path.chmod(0o600)
        self.assertEqual(auto.read_file(self.tmp.name)['error']['code'], 'is_directory')

    def test_read_mutation_and_replacement(self):
        self.write('before')
        real_open = auto.open_regular
        for replace in (False, True):
            self.write('before')
            class MutatingRead:
                def __init__(inner, h): inner.h = h
                def fileno(inner): return inner.h.fileno()
                def close(inner): inner.h.close()
                def read(inner, n):
                    value = inner.h.read(n)
                    if replace:
                        p = self.path.with_suffix('.new')
                        p.write_text('after!')
                        os.replace(p, self.path)
                    else:
                        self.path.write_text('after!')
                    return value
            def wrapped(p):
                h, st = real_open(p)
                return MutatingRead(h), st
            with patch.object(auto, 'open_regular', wrapped):
                self.assertEqual(self.read()['error']['code'], 'changed_during_read')

    def test_search_continuation_not_false_negative(self):
        self.write('a' * 1000 + 'TARGET')
        limits = Limits(max_scan_bytes=128, chunk_size=64)
        r = self.read(query='TARGET', limits=limits)
        self.assertEqual(r['route'], 'search')
        self.assertFalse(r['scan_complete'])
        self.assertEqual(r['matches'], [])
        for _ in range(20):
            if r['scan_complete']: break
            r = self.read(query='TARGET', from_byte=r['next_search_byte'], identity=r['identity'], limits=limits)
        self.assertTrue(r['scan_complete'])
        self.assertEqual(r['matches'][0]['byte_offset'], 1000)
        self.path.write_text('changed')
        stale = self.read(query='TARGET', from_byte=1, identity=r['identity'])
        self.assertEqual(stale['error']['code'], 'stale_identity')

    def test_mcp_cli_real_process_and_no_network(self):
        self.write('日本語\n')
        calls = [dict(jsonrpc='2.0', id=1, method='initialize', params={}),
                 dict(jsonrpc='2.0', id=2, method='tools/list'),
                 dict(jsonrpc='2.0', id=3, method='tools/call', params=dict(name='read_file', arguments=dict(path=str(self.path))))]
        p = subprocess.run([sys.executable, str(ROOT/'bin/ctxfilter'), 'mcp'], input=''.join(json.dumps(x)+'\n' for x in calls), text=True, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        messages = [json.loads(x) for x in p.stdout.splitlines()]
        self.assertIn('read_file', [t['name'] for t in messages[1]['result']['tools']])
        r = json.loads(messages[2]['result']['content'][0]['text'])
        self.assertEqual(r['text'], '日本語\n')
        cli = subprocess.run([sys.executable, str(ROOT/'bin/ctxfilter'), 'read', '--path', str(self.path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(cli.stdout), r)
        with patch('socket.socket', side_effect=AssertionError('network')), patch('subprocess.Popen', side_effect=AssertionError('child process')):
            self.assertEqual(self.read()['route'], 'full')
            self.write('a' * 5000)
            self.assertEqual(self.read()['route'], 'preview')
            self.assertEqual(self.read(query='a')['route'], 'search')

    def test_mcp_output_budget_and_bad_arguments(self):
        self.write('"' * 4096)
        for budget in (256, 1500, 4096):
            r = handle_rpc(dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(name='read_file', arguments=dict(path=str(self.path), max_output_bytes=budget))))['result']
            self.assertLessEqual(len(r['content'][0]['text'].encode()), budget)
            self.assertEqual(r['isError'], not json.loads(r['content'][0]['text'])['ok'])
        for bad in ({'small_bytes': True}, {'small_bytes': 1.5}, {'goal': 'classify'}, {'query': ''}):
            r = handle_rpc(dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(name='read_file', arguments=dict(path=str(self.path), **bad))))
            self.assertTrue(r['result']['isError'])

if __name__ == '__main__':
    unittest.main()
