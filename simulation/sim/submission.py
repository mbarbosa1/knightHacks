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
        # encoding='utf-8' added for Windows (Hasita's laptop): Windows defaults to cp1252, which cannot write
        # Japanese report text. macOS and Linux already default to UTF-8, so nothing changes there.
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        # Windows check added for Hasita's laptop: Windows cannot open a folder with os.open (PermissionError).
        # os.replace is already durable there. macOS and Linux (os.name == 'posix') still flush the folder as before.
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_job(root, run_id):
    # encoding='utf-8' for Windows (Hasita's laptop); same as the default on macOS and Linux.
    return json.loads((Path(root) / str(run_id) / 'submission.json').read_text(encoding='utf-8'))


def write_job(root, job):
    atomic_json(Path(root) / job['run_id'] / 'submission.json', job)


def append_report(root, obs):
    path = Path(root) / str(obs['run_id']) / 'pending_observations.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:   # utf-8 for Windows (Hasita's laptop); same as the macOS/Linux default
        f.write(json.dumps(obs, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())
