"""No model/GPU inference; can run directly with the dots API venv (unittest)."""

import asyncio
import importlib.util
from pathlib import Path
import threading
import tempfile
import sys
from types import SimpleNamespace
import unittest

try:
    import anyio
    import httpx
    import numpy as np
    import torch
    from fastapi import HTTPException

    _path = Path(__file__).resolve().parents[2] / 'serve_api.py'
    if not _path.is_file():
        raise ImportError('Local dots API is not available')
    _spec = importlib.util.spec_from_file_location('dots_api_oom_test_server', _path)
    server = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(server)
    AVAILABLE = True
except ImportError:
    AVAILABLE = False


class FakeTensor:
    def __init__(self, samples=12):
        self.samples = np.zeros(samples, dtype=np.float32)

    def float(self):
        return self

    def cpu(self):
        return self

    def squeeze(self):
        return self

    def numpy(self):
        return self.samples


class FakeRuntime:
    def __init__(self, failure=None, *, fail_after=0, chunks=3):
        self.failure = failure
        self.fail_after = fail_after
        self.chunks = chunks
        self.calls = 0
        self.yielded = 0
        self.closed = False

    def generate(self, **kwargs):
        self.calls += 1
        if self.failure:
            raise self.failure
        return {'audio': FakeTensor(), 'sample_rate': 24000}

    def generate_stream(self, **kwargs):
        self.calls += 1
        try:
            for index in range(self.chunks):
                if self.failure and index == self.fail_after:
                    raise self.failure
                self.yielded += 1
                yield FakeTensor()
        finally:
            self.closed = True


@unittest.skipUnless(AVAILABLE, 'Run with the dots API venv to use server dependencies')
class DotsOomTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = server.STATE.copy()
        self.lock = server._gpu_lock
        server._gpu_lock = threading.Lock()
        server.STATE.update(
            runtime=FakeRuntime(), sample_rate=24000, device='cuda:0', model='fake',
            prompt_audio=None, prompt_text=None, ref_dir=None, last_error=None,
            defaults=dict(language=None, num_steps=32, guidance_scale=1,
                          speaker_scale=1, normalize_text=True),
            requests=0, stream_requests=0, total_wait_seconds=0,
            total_audio_seconds=0, total_stream_audio_seconds=0,
            total_generate_seconds=0,
        )

    def tearDown(self):
        self.assertFalse(server._gpu_lock.locked(), 'GPU lock leaked')
        server.STATE.clear()
        server.STATE.update(self.saved)
        server._gpu_lock = self.lock

    def runtime(self, **kwargs):
        runtime = FakeRuntime(**kwargs)
        server.STATE['runtime'] = runtime
        return runtime

    async def request(self, path, **kwargs):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
            base_url='http://test',
        ) as client:
            return await client.post(path, json={'text': '测试'}, **kwargs)

    async def assert_degraded(self, runtime):
        status = await server.health()
        self.assertEqual(status['status'], 'error')
        self.assertFalse(status['ready'])
        self.assertTrue(status['model_loaded'])
        self.assertTrue(status['needs_restart'])
        self.assertEqual(status['last_error']['code'], 'cuda_out_of_memory')
        calls = runtime.calls
        for path in ['/tts', '/tts/stream']:
            response = await self.request(path)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['detail']['code'], 'cuda_out_of_memory')
        self.assertEqual(runtime.calls, calls, 'Faulted model must not be retried')

    async def test_nonstream_oom_returns_structured_503_and_latches(self):
        runtime = self.runtime(failure=torch.OutOfMemoryError('CUDA out of memory.'))
        response = await self.request('/tts')
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json()['detail']['needs_restart'])
        self.assertEqual(runtime.calls, 1)
        await self.assert_degraded(runtime)

    async def test_typed_oom_without_cuda_text_is_detected_on_cuda_device(self):
        self.assertTrue(server._is_cuda_oom(torch.OutOfMemoryError('allocation failed')))
        server.STATE['device'] = 'cpu'
        self.assertFalse(server._is_cuda_oom(torch.OutOfMemoryError('out of memory')))

    async def test_first_stream_chunk_oom_is_503_without_wav_header(self):
        runtime = self.runtime(failure=torch.AcceleratorError('CUDA error: out of memory'))
        response = await self.request('/tts/stream')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'RIFF', response.content)
        self.assertEqual(response.json()['detail']['code'], 'cuda_out_of_memory')
        self.assertTrue(runtime.closed)
        self.assertEqual(runtime.calls, 1)
        await self.assert_degraded(runtime)

    async def test_midstream_oom_aborts_and_health_reports_failure(self):
        runtime = self.runtime(failure=torch.AcceleratorError('CUDA error: out of memory'), fail_after=1)
        response = await server.tts_stream(server.TtsRequest(text='测试'))
        self.assertEqual(runtime.yielded, 1, 'Only first PCM is prefetched')
        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            await asyncio.Event().wait()

        with self.assertRaises(HTTPException):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        self.assertEqual(messages[0]['status'], 200)
        self.assertTrue(messages[1]['body'].startswith(b'RIFF'))
        self.assertEqual(len(messages[2]['body']), 24)
        self.assertTrue(all(m.get('more_body', True) for m in messages))
        self.assertTrue(runtime.closed)
        self.assertEqual(runtime.calls, 1)
        await self.assert_degraded(runtime)

    async def test_stream_success_stays_incremental_and_releases_lock(self):
        runtime = self.runtime()
        response = await server.tts_stream(server.TtsRequest(text='测试'))
        self.assertEqual(runtime.yielded, 1)
        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            await asyncio.Event().wait()

        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        self.assertFalse(messages[-1]['more_body'])
        self.assertEqual(runtime.yielded, 3)
        self.assertEqual(runtime.calls, 1)
        self.assertTrue(runtime.closed)
        self.assertEqual(server.STATE['stream_requests'], 1)
        self.assertFalse(server._gpu_lock.locked())

    async def test_disconnect_before_first_body_releases_primed_generator(self):
        runtime = self.runtime()
        response = await server.tts_stream(server.TtsRequest(text='测试'))

        async def send(message):
            raise OSError('client disconnected')

        async def receive():
            return {'type': 'http.disconnect'}

        with self.assertRaises(Exception):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        self.assertTrue(runtime.closed)
        self.assertEqual(runtime.yielded, 1)

    async def test_asgi_legacy_disconnect_cancels_body_and_releases_lock(self):
        runtime = self.runtime()
        response = await server.tts_stream(server.TtsRequest(text='测试'))
        sent = asyncio.Event()

        async def send(message):
            if message['type'] == 'http.response.body':
                sent.set()
                await asyncio.Event().wait()

        async def receive():
            await sent.wait()
            return {'type': 'http.disconnect'}

        await response({'type': 'http', 'asgi': {'spec_version': '2.0'}}, receive, send)
        self.assertTrue(runtime.closed)
        self.assertEqual(runtime.yielded, 1)

    async def test_queue_rechecks_fault_after_acquiring_gpu_lock(self):
        runtime = self.runtime()
        started = threading.Event()
        server._gpu_lock.acquire()

        def infer():
            started.set()
            return server.synth(server.TtsRequest(text='测试'))

        task = asyncio.create_task(asyncio.to_thread(infer))
        await asyncio.to_thread(started.wait)
        server._record_cuda_oom(torch.AcceleratorError('CUDA error: out of memory'))
        server._gpu_lock.release()
        with self.assertRaises(HTTPException) as result:
            await task
        self.assertEqual(result.exception.status_code, 503)
        self.assertEqual(runtime.calls, 0)

    async def test_unrelated_or_cpu_errors_are_not_latched_as_cuda_oom(self):
        for failure in [
            MemoryError('out of memory'),
            RuntimeError('DefaultCPUAllocator: out of memory'),
            torch.OutOfMemoryError('CPU out of memory'),
            torch.AcceleratorError('CUDA error: illegal memory access'),
            ValueError('CUDA out of memory'),
        ]:
            with self.subTest(failure=repr(failure)):
                runtime = self.runtime(failure=failure)
                response = await self.request('/tts')
                self.assertEqual(response.status_code, 500)
                self.assertIsNone(server.STATE['last_error'])
                self.assertEqual(runtime.calls, 1)

    async def test_cpu_copy_oom_is_caught_and_lock_released(self):
        class CopyErrorTensor(FakeTensor):
            def cpu(self):
                raise torch.AcceleratorError('CUDA error: out of memory')

        class CopyErrorRuntime(FakeRuntime):
            def generate(self, **kwargs):
                self.calls += 1
                return {'audio': CopyErrorTensor(), 'sample_rate': 24000}

        runtime = CopyErrorRuntime()
        server.STATE['runtime'] = runtime
        response = await self.request('/tts')
        self.assertEqual(response.status_code, 503)
        await self.assert_degraded(runtime)

    async def test_health_loading_ok_and_low_vram_field(self):
        from unittest.mock import patch

        with patch.dict('os.environ', {'DOTS_TTS_LOW_VRAM': '1'}):
            status = await server.health()
        self.assertEqual(status['status'], 'ok')
        self.assertTrue(status['ready'])
        self.assertFalse(status['needs_restart'])
        self.assertIsNone(status['last_error'])
        self.assertTrue(status['low_vram'])
        server.STATE['runtime'] = None
        status = await server.health()
        self.assertEqual(status['status'], 'loading')
        self.assertFalse(status['ready'])

    async def test_nonstream_success_does_not_degrade(self):
        response = await self.request('/tts')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'RIFF'))
        self.assertEqual(server.STATE['requests'], 1)
        self.assertIsNone(server.STATE['last_error'])

    async def test_fresh_startup_clears_previous_failure(self):
        from unittest.mock import patch

        runtime = FakeRuntime()
        runtime.sample_rate = 24000
        runtime.device = 'cpu'
        fake_module = SimpleNamespace(DotsTtsRuntime=SimpleNamespace(
            from_pretrained=lambda *args, **kwargs: runtime))
        server._record_cuda_oom(torch.AcceleratorError('CUDA error: out of memory'))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            cfg = SimpleNamespace(
                model=str(path), precision='bfloat16', optimize=True, merge_steps=1,
                ref_dir=str(path), language=None, num_steps=32, guidance_scale=1,
                speaker_scale=1, normalize_text=True, prompt_audio=None,
                prompt_text=None, auto_voice=False, warmup=False,
                host='127.0.0.1', port=9881,
            )
            with patch.dict(sys.modules, {'dots_tts.runtime': fake_module}), \
                 patch.object(torch.cuda, 'is_available', return_value=False), \
                 patch.object(server, 'GRADIO_UPLOAD_DIR', path / 'uploads'), \
                 patch.object(server, 'GRADIO_AUDIO_DIR', path / 'audio'):
                async with server.lifespan(SimpleNamespace(state=SimpleNamespace(cfg=cfg))):
                    self.assertIsNone(server.STATE['last_error'])
                    self.assertIs(server.STATE['runtime'], runtime)
                    self.assertEqual((await server.health())['status'], 'ok')
        self.assertIsNone(server.STATE['runtime'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
