"""Also runnable with GPT-SoVITS's runtime/python.exe (no pytest required)."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest


_spec = importlib.util.spec_from_file_location(
    'kinoko_sovits_api', Path(__file__).resolve().parents[1] / 'resource' / 'kinoko_sovits_api.py')
resident = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resident)


class FakePipeline:
    def __init__(self):
        self.configs = types.SimpleNamespace(t2s_weights_path='', vits_weights_path='')
        self.t2s_model = object()
        self.vits_model = object()
        self.prompt_cache = {'ref_audio_path': 'old', 'refer_spec': [object()], 'prompt_semantic': object()}
        self.loads = []
        self.fail = False
        self.error = None
        self.closed = 0
        self.second_started = threading.Event()
        self.gate = None

    def init_t2s_weights(self, path):
        self.loads.append(('gpt', path))
        self.configs.t2s_weights_path = path
        if self.fail:
            raise RuntimeError('bad weight')
        self.t2s_model = object()

    def init_vits_weights(self, path):
        self.loads.append(('sovits', path))
        self.configs.vits_weights_path = path
        self.vits_model = object()

    def run(self, request):
        try:
            if self.error:
                raise ValueError(self.error)
            yield 32000, b'first-pcm'
            if self.gate:
                self.second_started.set()
                if not self.gate.wait(5):
                    raise TimeoutError('test gate timed out')
            yield 32000, b'second-pcm'
        finally:
            self.closed += 1


class ModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = {}
        for name in ('a.ckpt', 'a.pth', 'b.ckpt', 'b.pth', 'ref.wav'):
            path = Path(self.temp.name) / name
            path.write_bytes(b'file')
            self.paths[name] = str(path)
        self.pipeline = FakePipeline()
        self.models = resident.ResidentModels(self.pipeline)

    def select(self, name='a'):
        return self.models.select(self.paths[name + '.ckpt'], self.paths[name + '.pth'])

    def test_a_a_b_a_loads_only_three_pairs(self):
        self.assertEqual(self.select()[0], 'load')
        self.assertEqual(self.select()[0], 'hit')
        self.assertEqual(self.select('b')[0], 'load')
        self.assertEqual(self.select()[0], 'load')
        self.assertEqual(len(self.pipeline.loads), 6)
        self.assertEqual(self.models.model_reuses, 1)

    def test_failure_cannot_be_reused_and_retry_loads_both(self):
        self.select()
        self.pipeline.fail = True
        with self.assertRaisesRegex(RuntimeError, 'bad weight'):
            self.select('b')
        self.assertIsNone(self.models.signatures)
        self.pipeline.fail = False
        self.assertEqual(self.select()[0], 'load')

    def test_modified_file_reloads(self):
        self.select()
        Path(self.paths['a.pth']).write_bytes(b'new weight file')
        self.assertEqual(self.select()[0], 'load')

    def test_replaced_live_model_reloads_even_with_same_paths(self):
        self.select()
        self.pipeline.t2s_model = object()
        self.assertEqual(self.select()[0], 'load')

    def test_external_setter_invalidates_confirmed_pair(self):
        self.select()
        self.pipeline.init_t2s_weights(self.paths['b.ckpt'])
        self.assertIsNone(self.models.signatures)
        self.assertEqual(self.select()[0], 'load')

    def test_switch_clears_reference_even_if_reference_path_is_same(self):
        self.select()
        self.pipeline.prompt_cache.update(ref_audio_path=self.paths['ref.wav'],
                                          prompt_semantic=object(), refer_spec=[object()])
        self.select('b')
        self.assertIsNone(self.pipeline.prompt_cache['ref_audio_path'])
        self.assertIsNone(self.pipeline.prompt_cache['prompt_semantic'])
        self.assertEqual(self.pipeline.prompt_cache['refer_spec'], [])

    def test_cache_hit_preserves_reference(self):
        self.select()
        marker = object()
        self.pipeline.prompt_cache['prompt_semantic'] = marker
        self.select()
        self.assertIs(self.pipeline.prompt_cache['prompt_semantic'], marker)

    def test_modified_reference_is_not_reused(self):
        request = {'ref_audio_path': self.paths['ref.wav']}
        self.models.check_reference(request)
        self.pipeline.prompt_cache['prompt_semantic'] = object()
        Path(self.paths['ref.wav']).write_bytes(b'changed reference')
        self.models.check_reference(request)
        self.assertIsNone(self.pipeline.prompt_cache['prompt_semantic'])


@unittest.skipUnless(importlib.util.find_spec('fastapi'), 'Run API integration tests with GPT-SoVITS runtime')
class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from fastapi import FastAPI
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = {}
        for name in ('a.ckpt', 'a.pth', 'b.ckpt', 'b.pth', 'ref.wav'):
            path = Path(self.temp.name) / name
            path.write_bytes(b'file')
            self.paths[name] = str(path)
        self.pipeline = FakePipeline()
        self.app = FastAPI()
        namespace = {'APP': self.app, 'tts_pipeline': self.pipeline}
        exec('async def tts_handle(req):\n    raise AssertionError("must be replaced")\n'
             '@APP.post("/tts")\nasync def original_tts(req: dict):\n    return await tts_handle(req)\n'
             '@APP.get("/set_gpt_weights")\nasync def set_gpt_weights(weights_path: str):\n'
             '    tts_pipeline.init_t2s_weights(weights_path)\n    return {"message": "success"}\n', namespace)
        namespace.update(check_params=lambda req: None,
                         wave_header_chunk=lambda sample_rate: b'WAVHEADER',
                         pack_audio=lambda io, audio, rate, media: types.SimpleNamespace(getvalue=lambda: audio))
        self.namespace = namespace
        resident.install(namespace)

    def payload(self, name='a', streaming=True):
        return {'gpt_weights_path': self.paths[name + '.ckpt'],
                'sovits_weights_path': self.paths[name + '.pth'],
                'tts': {'text': 'test', 'ref_audio_path': self.paths['ref.wav'],
                        'streaming_mode': streaming, 'media_type': 'wav'}}

    def start_request(self, path='/kinoko/tts', payload=None, method='POST', query=b''):
        queue = asyncio.Queue()
        queue.put_nowait({'type': 'http.request', 'body': json.dumps(payload or {}).encode(), 'more_body': False})
        messages = []

        async def send(message):
            messages.append(message)

        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
                 'http_version': '1.1', 'method': method, 'scheme': 'http',
                 'path': path, 'raw_path': path.encode(), 'query_string': query,
                 'root_path': '', 'headers': [(b'content-type', b'application/json')],
                 'server': ('test', 80), 'client': ('127.0.0.1', 1)}
        return asyncio.create_task(self.app(scope, queue.get, send)), messages, queue

    async def test_atomic_cache_and_audio_format(self):
        for expected in ('load', 'hit'):
            task, messages, _ = self.start_request(payload=self.payload())
            await asyncio.wait_for(task, 3)
            self.assertEqual(messages[0]['status'], 200)
            headers = dict(messages[0]['headers'])
            self.assertEqual(headers[b'x-kinoko-model-cache'], expected.encode())
            self.assertEqual(b''.join(m.get('body', b'') for m in messages),
                             b'WAVHEADERfirst-pcmsecond-pcm')
        self.assertEqual(len(self.pipeline.loads), 2)

    async def test_initial_generation_error_is_400_not_broken_200(self):
        self.pipeline.error = 'reference must be 3-10 seconds'
        task, messages, _ = self.start_request(payload=self.payload())
        await task
        self.assertEqual(messages[0]['status'], 400)
        self.assertIn(b'3-10 seconds', messages[1]['body'])
        self.assertEqual(self.pipeline.closed, 1)

    async def test_initial_audio_encoding_error_is_400(self):
        def fail_encoding(*args):
            raise ValueError('bad audio encoding')

        self.namespace['pack_audio'] = fail_encoding
        task, messages, _ = self.start_request(payload=self.payload())
        await task
        self.assertEqual(messages[0]['status'], 400)
        self.assertIn(b'bad audio encoding', messages[1]['body'])

    async def test_non_stream_closes_generator(self):
        task, messages, _ = self.start_request(payload=self.payload(streaming=False))
        await task
        self.assertEqual(messages[0]['status'], 200)
        self.assertEqual(messages[1]['body'], b'first-pcm')
        self.assertEqual(self.pipeline.closed, 1)

    async def test_upstream_tts_uses_safe_handler(self):
        self.pipeline.error = 'upstream error'
        task, messages, _ = self.start_request('/tts', self.payload()['tts'])
        await task
        self.assertEqual(messages[0]['status'], 400)

    async def test_disconnect_joins_worker_before_unlocking_setter(self):
        from urllib.parse import urlencode
        self.pipeline.gate = threading.Event()
        task, messages, queue = self.start_request(payload=self.payload())
        self.assertTrue(await asyncio.to_thread(self.pipeline.second_started.wait, 3))
        self.assertTrue(any(m.get('body') == b'first-pcm' for m in messages))
        setter, _, _ = self.start_request('/set_gpt_weights', method='GET',
                                         query=urlencode({'weights_path': self.paths['b.ckpt']}).encode())
        queue.put_nowait({'type': 'http.disconnect'})
        await asyncio.sleep(0.05)
        self.assertFalse(setter.done())
        self.assertEqual(len(self.pipeline.loads), 2)
        self.pipeline.gate.set()
        await asyncio.wait_for(asyncio.gather(task, setter), 3)
        self.assertEqual(self.pipeline.closed, 1)
        self.assertEqual(len(self.pipeline.loads), 3)

    async def test_status_remains_responsive_during_generation(self):
        self.pipeline.gate = threading.Event()
        task, _, _ = self.start_request(payload=self.payload())
        self.assertTrue(await asyncio.to_thread(self.pipeline.second_started.wait, 3))
        status, messages, _ = self.start_request('/kinoko/status', method='GET')
        await asyncio.wait_for(status, 1)
        self.assertEqual(json.loads(messages[1]['body'])['protocol'], 1)
        self.pipeline.gate.set()
        await asyncio.wait_for(task, 3)

    async def test_task_cancellation_joins_model_worker(self):
        self.pipeline.gate = threading.Event()
        task, _, _ = self.start_request(payload=self.payload())
        self.assertTrue(await asyncio.to_thread(self.pipeline.second_started.wait, 3))
        task.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        self.pipeline.gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertEqual(self.pipeline.closed, 1)


if __name__ == '__main__':
    unittest.main()
