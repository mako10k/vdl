"""Bounded job processes and attempt-owned artifact cleanup."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from vdl.config import Config
from vdl.queue import Job, JobQueue


def track_partial(path: Path) -> None:
    """Register a newly allocated output before writing it (never existing input)."""
    directory = os.environ.get('VDL_ATTEMPT_DIR')
    if directory:
        manifest = Path(directory) / 'outputs.jsonl'
        with manifest.open('a') as stream:
            stream.write(json.dumps(str(path.resolve())) + '\n')


def track_record(record_id: int) -> None:
    directory = os.environ.get('VDL_ATTEMPT_DIR')
    if directory:
        with (Path(directory) / 'records.jsonl').open('a') as stream:
            stream.write(json.dumps(record_id) + '\n')


def cleanup_attempt(config: Config, directory: Path) -> None:
    manifest = directory / 'outputs.jsonl'
    queue = JobQueue(config.db_path)
    with queue._conn() as conn:
        protected = {str(Path(r[0]).resolve()) for r in conn.execute(
            "SELECT file_path FROM downloads WHERE status='completed' AND file_path IS NOT NULL"
        )} if conn.execute("SELECT 1 FROM sqlite_master WHERE name='downloads'").fetchone() else set()
        protected.update(str(Path(r[0]).resolve()) for r in conn.execute(
            "SELECT keyframe_path FROM scenes WHERE keyframe_path IS NOT NULL"))
    records = directory / 'records.jsonl'
    if records.exists():
        with queue._conn() as conn:
            for line in records.read_text().splitlines():
                try:
                    record_id = int(json.loads(line))
                except (ValueError, TypeError):
                    continue
                conn.execute(
                    "UPDATE downloads SET status='failed', error='attempt interrupted before completion' "
                    "WHERE id=? AND status='pending'", (record_id,))
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            try:
                path = Path(json.loads(line)).resolve()
                allowed = any(path.is_relative_to(root.resolve()) for root in (
                    config.download_dir, config.temp_dir, config.keyframes_dir))
                if allowed and str(path) not in protected:
                    path.unlink(missing_ok=True)
            except (ValueError, TypeError):
                continue
    shutil.rmtree(directory)


def stop_group(proc: subprocess.Popen) -> None:
    """Kill the whole attempt, including ffmpeg children; reap the runner."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # A runner can exit before its children. Always finish the process group.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def process_identity(pid: int) -> str | None:
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[19]
    except FileNotFoundError:
        return None


def recover_attempts(config: Config) -> list[int]:
    """Before reclaiming jobs, stop any attempts orphaned by daemon death."""
    recovered = []
    for directory in config.temp_dir.glob('job-*'):
        metadata = directory / 'process.json'
        if not metadata.exists():
            continue
        record = json.loads(metadata.read_text())
        pid = record.get('pid')
        identity = process_identity(pid) if pid else None
        if pid and (identity is None or identity == record.get('identity')):
            try:
                if identity is None or os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
                    for _ in range(100):
                        stat = Path(f'/proc/{pid}/stat')
                        if not stat.exists() or stat.read_text().split(') ', 1)[1].split()[0] == 'Z':
                            break
                        time.sleep(.01)
            except ProcessLookupError:
                pass
        cleanup_attempt(config, directory)
        recovered.append(int(record['job_id']))
    return recovered


def run_isolated(config: Config, job: Job, *, command: list[str] | None = None) -> dict:
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    import tempfile
    directory = Path(tempfile.mkdtemp(prefix=f'job-{job.id}-', dir=config.temp_dir))
    result_path = directory / 'result.json'
    env = {**os.environ, 'VDL_ATTEMPT_DIR': str(directory), 'TMPDIR': str(directory)}
    argv = command or [sys.executable, '-m', 'vdl.job_runner', str(config.home), str(job.id)]
    queue = JobQueue(config.db_path)
    started = changed = time.monotonic()
    previous = None
    proc = None
    metadata = directory / 'process.json'
    metadata.write_text(json.dumps({'job_id': job.id}))
    try:
        proc = subprocess.Popen(argv, env=env, start_new_session=True)
        temporary = directory / 'process.tmp'
        temporary.write_text(json.dumps({'job_id': job.id, 'pid': proc.pid, 'identity': process_identity(proc.pid)}))
        temporary.replace(metadata)
        while proc.poll() is None:
            current = queue.get(job.id)
            if current is None or current.status != 'running':
                raise RuntimeError('job cancelled or no longer running')
            progress = (current.progress_pct, current.progress_current, current.progress_msg)
            now = time.monotonic()
            if progress != previous:
                changed = now
                previous = progress
            if now - started >= config.job_max_seconds:
                raise TimeoutError(f'job execution timeout ({config.job_max_seconds:g}s)')
            if now - changed >= config.job_stall_seconds:
                raise TimeoutError(f'job stalled without progress ({config.job_stall_seconds:g}s)')
            time.sleep(min(0.25, config.job_stall_seconds / 4, config.job_max_seconds / 4))
        if not result_path.exists():
            raise RuntimeError(f'job runner exited {proc.returncode} without a result')
        result = json.loads(result_path.read_text())
        if not result['ok']:
            raise RuntimeError(result['error'])
        if proc.returncode != 0:
            raise RuntimeError(f'job runner exited {proc.returncode}')
        return result['result']
    finally:
        if proc is not None:
            stop_group(proc)
        cleanup_attempt(config, directory)
