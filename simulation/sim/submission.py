"""Durable local submissions. Prepared snapshots survive simulator restarts."""
import json
import os
from pathlib import Path
import tempfile
import threading

_locks = {}
_guard = threading.Lock()


def lock_for(run_id):
    with _guard:
        return _locks.setdefault(str(run_id), threading.Lock())


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.submission-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_job(root, run_id):
    return json.loads((Path(root) / str(run_id) / 'submission.json').read_text())


def write_job(root, job):
    atomic_json(Path(root) / job['run_id'] / 'submission.json', job)


def append_report(root, obs):
    path = Path(root) / str(obs['run_id']) / 'pending_observations.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        f.write(json.dumps(obs, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())
