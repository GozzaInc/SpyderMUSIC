"""SpyderMUSIC ChromaDB process boundary. Windows spawn-safe; no Qt imports.

A single process owns PersistentClient and its collections. Requests are serialized;
consecutive upserts to the same collection are combined into bounded batches.
"""
import atexit
import multiprocessing as mp
import os
import queue
import threading
import time
import uuid


def _serve(commands, replies, path):
    import chromadb
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    # Chroma persisted the default MiniLM model identity in older collections.
    # Keep it unchanged; do not ship or initialize the enormous CUDA runtime.
    class CPUDefaultMiniLM(ONNXMiniLM_L6_V2):
        @staticmethod
        def name():
            return 'default'

    embedding_fn = CPUDefaultMiniLM(preferred_providers=['CPUExecutionProvider'])
    print('[SpyderChroma] MiniLM CPU embedding provider (no CUDA dependencies).', flush=True)
    client = chromadb.PersistentClient(path=path)
    collections = {}
    pending = None
    def execute(req):
        nonlocal pending
        token, name, op, kwargs = req
        if op == 'open':
            try:
                coll = client.get_or_create_collection(name=name, configuration={'hnsw': {'space': 'cosine'}}, embedding_function=embedding_fn)
            except TypeError:
                coll = client.get_or_create_collection(name=name, metadata={'hnsw:space': 'cosine'}, embedding_function=embedding_fn)
            collections[name] = coll
            print(f'[SpyderChroma] Collection {name!r} opened with CPU MiniLM embedding function', flush=True)
            return True
        coll = collections[name]
        return getattr(coll, op)(**kwargs)

    def reply(req, result=None, error=None):
        replies.put((req[0], result, error))

    while True:
        req = pending if pending is not None else commands.get()
        pending = None
        if req is None:
            break
        token, name, op, kwargs = req
        if op == 'upsert':
            stop_after = False
            batch = [req]
            deadline = time.monotonic() + .12
            while len(batch) < 32:
                try:
                    other = commands.get(timeout=max(0, deadline-time.monotonic()))
                except queue.Empty:
                    break
                if other is None:
                    pending = None
                    # Shutdown follows completed batch.
                    stop_after = True
                    break
                if other[1] != name or other[2] != 'upsert' or time.monotonic() > deadline:
                    pending = other
                    stop_after = False
                    break
                batch.append(other)
            else:
                stop_after = False
            # Keep latest value for each id, preserving upsert semantics.
            merged = {}
            for entry in batch:
                data = entry[3]
                for i, ident in enumerate(data['ids']):
                    merged[ident] = (data['documents'][i], data['metadatas'][i])
            try:
                execute((None, name, 'upsert', {
                    'ids': list(merged),
                    'documents': [v[0] for v in merged.values()],
                    'metadatas': [v[1] for v in merged.values()],
                }))
                for entry in batch: reply(entry, True)
            except Exception as exc:
                for entry in batch: reply(entry, error=f'{type(exc).__name__}: {exc}')
            if stop_after:
                break
            continue
        try:
            reply(req, execute(req))
        except Exception as exc:
            reply(req, error=f'{type(exc).__name__}: {exc}')


class ChromaService:
    def __init__(self, path):
        self.path = path
        self.ctx = mp.get_context('spawn')
        self.commands = self.ctx.Queue()
        self.replies = self.ctx.Queue()
        self.process = self.ctx.Process(target=_serve, args=(self.commands, self.replies, path), daemon=True, name='SpyderChroma')
        self.process.start()
        self.waiting = {}
        self.lock = threading.Lock()
        self.running = True
        self.reader = threading.Thread(target=self._receive, name='ChromaIPC', daemon=True)
        self.reader.start()
        atexit.register(self.close)

    def _receive(self):
        while self.running:
            try: token, result, error = self.replies.get(timeout=.25)
            except queue.Empty: continue
            with self.lock:
                state = self.waiting.get(token)
                if state is not None:
                    state[1][:] = [result, error]
                    state[0].set()

    def call(self, name, op, timeout=90, **kwargs):
        if not self.running:
            raise RuntimeError('ChromaDB service is shutting down')
        token = uuid.uuid4().hex
        event = threading.Event()
        state = [event, []]
        with self.lock:
            self.waiting[token] = state
        try:
            if not self.process.is_alive():
                raise RuntimeError('ChromaDB worker process is not running')
            self.commands.put((token, name, op, kwargs))
            if not event.wait(timeout):
                raise TimeoutError(f'ChromaDB {op} timeout after {timeout}s')
            result, error = state[1]
            if error: raise RuntimeError(error)
            return result
        finally:
            with self.lock: self.waiting.pop(token, None)

    def open(self, name):
        self.call(name, 'open')
        return RemoteCollection(self, name)

    def close(self):
        """Graceful worker stop, bounded wait, then terminate only our child."""
        with self.lock:
            if not self.running:
                return
            self.running = False
        try:
            if self.process.is_alive():
                self.commands.put(None)
                self.process.join(timeout=5)
                if self.process.is_alive():
                    print('[SpyderChroma] Graceful shutdown timed out; terminating owned worker.', flush=True)
                    self.process.terminate()
                    self.process.join(timeout=3)
                if self.process.is_alive() and hasattr(self.process, 'kill'):
                    print('[SpyderChroma] Worker still alive; killing owned worker.', flush=True)
                    self.process.kill()
                    self.process.join(timeout=2)
        except Exception as exc:
            print(f'[SpyderChroma] Shutdown error: {exc}', flush=True)
        finally:
            with self.lock:
                for event, result in self.waiting.values():
                    result[:] = [None, 'ChromaDB service shutting down']
                    event.set()
                self.waiting.clear()
            try:
                self.reader.join(timeout=0.5)
            except RuntimeError:
                pass
            for q in (self.commands, self.replies):
                try:
                    q.close()
                    q.join_thread()
                except Exception:
                    pass


class RemoteCollection:
    def __init__(self, service, name):
        self.service, self.name = service, name
    def upsert(self, **kwargs): return self.service.call(self.name, 'upsert', **kwargs)
    def update(self, **kwargs): return self.service.call(self.name, 'update', **kwargs)
    def query(self, **kwargs): return self.service.call(self.name, 'query', **kwargs)
    def get(self, **kwargs): return self.service.call(self.name, 'get', **kwargs)
    def delete(self, **kwargs): return self.service.call(self.name, 'delete', **kwargs)
    def count(self): return self.service.call(self.name, 'count')
