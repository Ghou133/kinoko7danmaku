"""Optional resident adapter for an unmodified GPT-SoVITS api_v2.py.

Run with the upstream directory as cwd and its usual -a/-p/-c arguments.
Only one model pair is resident. Every mutating endpoint and complete audio
response shares a lock; status requests remain available during inference.
"""

import asyncio
import functools
import os
from pathlib import Path
import runpy
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO


_worker_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='kinoko-sovits')


def file_signature(path):
    resolved = Path(path).resolve(strict=True)
    info = resolved.stat()
    if not resolved.is_file():
        raise ValueError(f'Not a model/audio file: {resolved}')
    return os.path.normcase(str(resolved)), info.st_size, info.st_mtime_ns


class ResidentModels:
    """Cache only successful loads, checked against files and the live pipeline."""

    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.signatures = None
        self.model_objects = None
        self.reference_signatures = None
        self.model_loads = 0
        self.model_reuses = 0
        self.last_load_seconds = 0.0
        # Also catch upstream setters and the pipeline's own recovery reloads.
        for name in ('init_t2s_weights', 'init_vits_weights'):
            original = getattr(pipeline, name)

            @functools.wraps(original)
            def watched(*args, _original=original, **kwargs):
                self.invalidate()
                return _original(*args, **kwargs)

            setattr(pipeline, name, watched)

    def clear_reference(self):
        cache = getattr(self.pipeline, 'prompt_cache', None)
        if cache is not None:
            for key in ('ref_audio_path', 'prompt_semantic', 'prompt_text',
                        'prompt_lang', 'phones', 'bert_features', 'norm_text'):
                cache[key] = None
            cache['refer_spec'] = []
            cache['aux_ref_audio_paths'] = []
            cache.pop('raw_audio', None)
            cache.pop('raw_sr', None)
        self.reference_signatures = None

    def invalidate(self):
        self.signatures = None
        self.model_objects = None
        self.clear_reference()

    def _live_signatures(self):
        config = self.pipeline.configs
        return (file_signature(config.t2s_weights_path),
                file_signature(config.vits_weights_path))

    def _live_objects(self):
        return (id(self.pipeline.t2s_model), id(self.pipeline.vits_model))

    def select(self, gpt_path, sovits_path):
        started = time.perf_counter()
        try:
            requested = (file_signature(gpt_path), file_signature(sovits_path))
            if (self.signatures == requested
                    and self._live_signatures() == requested
                    and self.model_objects == self._live_objects()):
                self.model_reuses += 1
                return 'hit', 0.0
            self.invalidate()
            self.pipeline.init_t2s_weights(requested[0][0])
            self.pipeline.init_vits_weights(requested[1][0])
            if self._live_signatures() != requested:
                raise RuntimeError('Model file or active configuration changed during loading; retry.')
            self.signatures = requested
            self.model_objects = self._live_objects()
            self.model_loads += 1
            self.last_load_seconds = time.perf_counter() - started
            return 'load', self.last_load_seconds
        except BaseException:
            self.invalidate()
            raise

    def check_reference(self, request):
        paths = [request.get('ref_audio_path')]
        paths.extend(request.get('aux_ref_audio_paths') or [])
        signatures = tuple(file_signature(path) for path in paths if path)
        if signatures != self.reference_signatures:
            self.clear_reference()
        cache = getattr(self.pipeline, 'prompt_cache', {})
        if cache.get('prompt_lang') != request.get('prompt_lang'):
            # Upstream compares text alone when deciding to reuse text features.
            cache['prompt_text'] = None
        self.reference_signatures = signatures

    def status(self):
        pair = self.signatures
        return {
            'protocol': 1,
            'version': '1.0',
            'atomic_model_selection': True,
            'resident_model_reuse': True,
            'active_model': ({'gpt_weights_path': pair[0][0],
                              'sovits_weights_path': pair[1][0]} if pair else None),
            'model_loads': self.model_loads,
            'model_reuses': self.model_reuses,
            'last_load_seconds': self.last_load_seconds,
        }


async def joined_worker(function, *args):
    """Cancellation must never leave model work running outside the model lock."""
    import anyio

    # Starlette disconnect cancellation uses AnyIO scopes. Shield the operation
    # and also join a worker if the ASGI task itself is cancelled by its server.
    with anyio.CancelScope(shield=True):
        future = asyncio.get_running_loop().run_in_executor(_worker_pool, functools.partial(function, *args))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
            # Retrieve worker exceptions; cancellation still belongs to caller.
            if not future.cancelled():
                future.exception()
            raise


class SerialModelMiddleware:
    """A pure ASGI lock stays held until streaming and cleanup have completed."""

    def __init__(self, app):
        self.app = app
        self.lock = asyncio.Lock()

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        serial = path in ('/tts', '/kinoko/tts', '/control') or path.startswith('/set_')
        if scope['type'] == 'http' and serial:
            async with self.lock:
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def install(upstream):
    """Attach the extension to an already initialized api_v2 module namespace."""
    from fastapi import Body
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    app = upstream['APP']
    pipeline = upstream['tts_pipeline']
    models = ResidentModels(pipeline)
    exhausted = object()

    def audio_chunks(request):
        """Same serialization as upstream, with ownership of the inner generator."""
        generator = None
        try:
            models.check_reference(request)
            generator = pipeline.run(request)
            media_type = request.get('media_type', 'wav')
            streaming = request.get('streaming_mode', False)
            first = True
            for rate, audio in generator:
                header = None
                if streaming and first and media_type == 'wav':
                    header = upstream['wave_header_chunk'](sample_rate=rate)
                    media_type = 'raw'
                first = False
                packed = upstream['pack_audio'](BytesIO(), audio, rate, media_type).getvalue()
                if header is not None:
                    yield header
                yield packed
                if not streaming:
                    break
        except Exception:
            models.invalidate()
            raise
        finally:
            if generator is not None:
                generator.close()

    class OwnedStreamingResponse(StreamingResponse):
        def __init__(self, iterator, first, media_type):
            self.sync_iterator = iterator

            async def content():
                yield first
                while True:
                    item = await joined_worker(next, iterator, exhausted)
                    if item is exhausted:
                        break
                    yield item

            super().__init__(content(), media_type=media_type)

        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                await joined_worker(self.sync_iterator.close)

    async def safe_tts_handle(request):
        request = dict(request)
        error = upstream['check_params'](request)
        if error is not None:
            return error
        if request.get('streaming_mode') or request.get('return_fragment'):
            request['return_fragment'] = True
        iterator = audio_chunks(request)
        try:
            # Validate/generate real audio before committing HTTP 200 and its WAV header.
            first = await joined_worker(next, iterator, exhausted)
            if first is exhausted:
                raise RuntimeError('TTS returned no audio')
            media_type = 'audio/' + request.get('media_type', 'wav')
            if request.get('streaming_mode'):
                return OwnedStreamingResponse(iterator, first, media_type)
            await joined_worker(iterator.close)
            return Response(first, media_type=media_type)
        except BaseException as error:
            await joined_worker(iterator.close)
            models.invalidate()
            if not isinstance(error, Exception):
                raise
            return JSONResponse(status_code=400,
                                content={'message': 'tts failed', 'Exception': str(error)})

    # runpy may return a copied namespace, so update the actual function globals.
    upstream['tts_handle'].__globals__['tts_handle'] = safe_tts_handle

    # Existing setter bodies are async functions containing synchronous model
    # loads. Keep their API/schema but move those bodies off the ASGI loop.
    for route in app.routes:
        if getattr(route, 'path', '').startswith('/set_'):
            original_endpoint = route.endpoint

            @functools.wraps(original_endpoint)
            async def offloaded_setter(*args, _endpoint=original_endpoint, **kwargs):
                return await joined_worker(lambda: asyncio.run(_endpoint(*args, **kwargs)))

            route.endpoint = offloaded_setter
            route.dependant.call = offloaded_setter

    @app.get('/kinoko/status')
    async def kinoko_status():
        return models.status()

    @app.post('/kinoko/tts')
    async def kinoko_tts(request: dict = Body(...)):
        gpt_path = request.get('gpt_weights_path')
        sovits_path = request.get('sovits_weights_path')
        tts_request = request.get('tts')
        if not isinstance(gpt_path, str) or not gpt_path or not isinstance(sovits_path, str) or not sovits_path:
            return JSONResponse(status_code=400, content={'message': 'Both model paths are required'})
        if not isinstance(tts_request, dict):
            return JSONResponse(status_code=400, content={'message': 'tts must be an object'})
        try:
            cache_state, load_seconds = await joined_worker(models.select, gpt_path, sovits_path)
        except Exception as error:
            return JSONResponse(status_code=400,
                                content={'message': 'model load failed', 'Exception': str(error)})
        response = await safe_tts_handle(tts_request)
        response.headers['X-Kinoko-Model-Cache'] = cache_state
        response.headers['X-Kinoko-Model-Load-Ms'] = str(round(load_seconds * 1000, 2))
        return response

    app.add_middleware(SerialModelMiddleware)
    app.state.kinoko_resident_models = models
    return app


def main():
    # api_v2 sees its original CLI arguments but its __main__ uvicorn block is not run.
    upstream = runpy.run_path(str(Path.cwd() / 'api_v2.py'), run_name='kinoko_upstream')
    app = install(upstream)
    host = upstream['host']
    upstream['uvicorn'].run(app=app, host=None if host == 'None' else host,
                            port=upstream['port'], workers=1)


if __name__ == '__main__':
    main()
