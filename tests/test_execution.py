import json
import os
import sys
from pathlib import Path

import pytest

from vdl.config import Config
from vdl.execution import cleanup_attempt, run_isolated
from vdl.queue import JobKind, JobQueue
from vdl.registry import Registry, SourceType


def claimed(tmp_path):
    config = Config(home=tmp_path)
    config.ensure_dirs()
    queue = JobQueue(config.db_path)
    queue.enqueue(JobKind.DOWNLOAD, priority=100, payload={'url': 'https://example.test/video'})
    return config, queue, queue.claim_next()


@pytest.mark.parametrize('deadline', ['stall', 'elapsed'])
def test_timeout_kills_children_and_cleans_owned_partial_files(tmp_path, deadline):
    cfg, q, job = claimed(tmp_path)
    cfg.job_stall_seconds = .6 if deadline == 'stall' else 10
    cfg.job_max_seconds = .6 if deadline == 'elapsed' else 10
    partial = cfg.download_dir / 'partial.mp4'
    pidfile = tmp_path / 'child.pid'
    code = '''
import os, pathlib, subprocess, sys, time, json
root=pathlib.Path(os.environ['VDL_ATTEMPT_DIR'])
partial=pathlib.Path(sys.argv[1]); partial.write_bytes(b'incomplete')
(root/'outputs.jsonl').write_text(json.dumps(str(partial))+'\\n')
child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
pathlib.Path(sys.argv[2]).write_text(str(child.pid))
time.sleep(60)
'''
    with pytest.raises(TimeoutError, match='stalled' if deadline == 'stall' else 'execution timeout'):
        run_isolated(cfg, job, command=[sys.executable, '-c', code, str(partial), str(pidfile)])
    assert not partial.exists()
    assert not list(cfg.temp_dir.glob('job-*'))
    pid = int(pidfile.read_text())
    stat = Path(f'/proc/{pid}/stat')
    assert not stat.exists() or stat.read_text().split(') ', 1)[1].split()[0] == 'Z'


def test_cleanup_preserves_committed_artifacts_and_marks_unfinished_record_failed(tmp_path):
    cfg, q, job = claimed(tmp_path)
    reg = Registry(cfg.db_path)
    accepted = cfg.download_dir / 'accepted.mp4'
    accepted.write_bytes(b'accepted')
    rec = reg.create('https://example.test/a', source_type=SourceType.DIRECT)
    reg.mark_completed(rec.id, file_path=str(accepted), file_size=8, file_hash='accepted')
    interrupted = reg.create('https://example.test/b', source_type=SourceType.DIRECT)
    partial = cfg.download_dir / 'partial.mp4'
    partial.write_bytes(b'partial')
    directory = cfg.temp_dir / 'job-fixture'
    directory.mkdir()
    (directory/'outputs.jsonl').write_text('\n'.join(json.dumps(str(p)) for p in [accepted, partial]))
    (directory/'records.jsonl').write_text(str(interrupted.id)+'\n')
    cleanup_attempt(cfg, directory)
    assert accepted.read_bytes() == b'accepted'
    assert not partial.exists()
    assert reg.get(interrupted.id).status == 'failed'
    assert reg.get(rec.id).status == 'completed'


def test_real_runner_returns_result_for_empty_maintenance(tmp_path):
    cfg = Config(home=tmp_path)
    cfg.ensure_dirs()
    Registry(cfg.db_path)
    q = JobQueue(cfg.db_path)
    q.enqueue(JobKind.TS_TO_MP4, priority=50)
    job = q.claim_next()
    result = run_isolated(cfg, job)
    assert isinstance(result, dict)
    assert not list(cfg.temp_dir.glob('job-*'))


def test_restart_reaps_orphaned_attempt_before_reclaim(tmp_path):
    import subprocess
    from vdl.execution import process_identity, recover_attempts
    cfg, q, job = claimed(tmp_path)
    directory = cfg.temp_dir/'job-orphan'
    directory.mkdir()
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    (directory/'process.json').write_text(json.dumps({
        'job_id': job.id, 'pid': proc.pid, 'identity': process_identity(proc.pid)}))
    try:
        assert recover_attempts(cfg) == [job.id]
        assert proc.wait(timeout=3) != 0
        assert not directory.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_concurrent_attempts_cannot_overwrite_each_others_partial_outputs(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from vdl.util import reserve_path
    def write(number):
        path = reserve_path(tmp_path, 'same.mp4')
        path.write_text(str(number))
        return path
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(write, range(20)))
    assert len(set(paths)) == 20
    assert {p.read_text() for p in paths} == {str(i) for i in range(20)}
