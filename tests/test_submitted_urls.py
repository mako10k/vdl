from unittest.mock import patch

from vdl.config import Config
from vdl.queue import JobKind, JobQueue


def test_submission_history_includes_terminals_and_normalizes_urls(tmp_path):
    q = JobQueue(tmp_path / 'registry.db')
    download = q.enqueue(JobKind.DOWNLOAD, priority=100, payload={'url': 'https://EXAMPLE.test:443/a.mp4#start'})
    q.mark_failed(download.id, 'certificate expired', max_attempts=1)
    q.enqueue(JobKind.DOWNLOAD, priority=100, payload={'urls': ['https://example.test/b.mp4?token=1']})
    q.enqueue(JobKind.EMBED, priority=20, payload={'url': 'https://example.test/not-download.mp4'})
    malformed = [q.enqueue(JobKind.DOWNLOAD, priority=100) for _ in range(3)]
    with q._conn() as conn:
        conn.execute("UPDATE jobs SET status='archived' WHERE id=?", (download.id,))
        for job, payload in zip(malformed, ['invalid', '[]', 'null']):
            conn.execute('UPDATE jobs SET payload_json=? WHERE id=?', (payload, job.id))
    assert q.submitted_download_urls() == {
        'https://example.test/a.mp4', 'https://example.test/b.mp4?token=1'}


def test_api_checks_only_supplied_urls(tmp_path):
    from api.main import SubmittedUrlsBody, submitted_urls
    cfg = Config(home=tmp_path)
    q = JobQueue(cfg.db_path)
    q.enqueue(JobKind.DOWNLOAD, priority=100, payload={'url': 'https://example.test/a.mp4?token=1'})
    with patch('api.main.get_config', return_value=cfg):
        result = submitted_urls(SubmittedUrlsBody(urls=[
            'https://EXAMPLE.test:443/a.mp4?token=1#start',
            'https://example.test/a.mp4?token=2']))
    assert result == {'urls': ['https://EXAMPLE.test:443/a.mp4?token=1#start']}
