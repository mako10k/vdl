"""TLS verification is opt-in disabled and survives queued execution."""
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from vdl.cli import main
from vdl.config import Config
from vdl.m3u8_dl import download_m3u8_ffmpeg
from vdl.queue import JobQueue
from vdl.service import DownloadService
from vdl.workers import enqueue_download, run_job


@pytest.mark.parametrize("insecure", [False, True])
def test_service_tls_verification(tmp_path, insecure):
    with patch("vdl.service.httpx.Client") as client:
        with DownloadService(Config(home=tmp_path), insecure=insecure):
            assert client.call_args.kwargs["verify"] is (not insecure)


@pytest.mark.parametrize("insecure", [False, True])
def test_cli_queue_and_worker_preserve_tls_choice(tmp_path, insecure):
    args = ["--home", str(tmp_path), "get", "https://example.test/a.mp4",
            "--no-daemon", "--no-index"]
    if insecure:
        args.append("--insecure")
    with patch("vdl.classifier.ResourceProbe") as probe, patch("httpx.Client") as client:
        from vdl.classifier import ResourceKind
        probe.return_value.probe.return_value = MagicMock(
            kind=ResourceKind.VIDEO, final_url="https://example.test/a.mp4")
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        assert client.call_args.kwargs["verify"] is (not insecure)
    q = JobQueue(tmp_path / "registry.db")
    job = q.claim_next()
    assert job.payload["insecure"] is insecure
    with patch("vdl.workers.DownloadService") as service:
        service.return_value.__enter__.return_value.process_url.return_value = []
        with pytest.raises(RuntimeError, match="no media downloaded"):
            run_job(Config(home=tmp_path), job)
        assert service.call_args.kwargs["insecure"] is insecure


@pytest.mark.parametrize("insecure", [False, True])
def test_ffmpeg_tls_option_on_both_attempts(tmp_path, insecure):
    commands = []

    def popen(cmd, **kwargs):
        commands.append(cmd)
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 1 if len(commands) == 1 else 0
        if len(commands) == 2:
            (tmp_path / "stream.mp4").write_bytes(b"video")
        return proc

    with patch("vdl.m3u8_dl.shutil.which", return_value="ffmpeg"), \
         patch("vdl.m3u8_dl.subprocess.Popen", side_effect=popen):
        download_m3u8_ffmpeg("https://example.test/a.m3u8", tmp_path,
                             insecure=insecure)
    assert len(commands) == 2
    for cmd in commands:
        tls_index = cmd.index("-tls_verify")
        assert cmd[tls_index + 1] == ("0" if insecure else "1")
        assert tls_index < cmd.index("-i")


def test_legacy_job_defaults_to_verified_tls(tmp_path):
    cfg = Config(home=tmp_path)
    q = JobQueue(cfg.db_path)
    job = enqueue_download(q, "https://example.test/a.mp4", no_index=True)
    with q._conn() as conn:
        conn.execute("UPDATE jobs SET payload_json = ? WHERE id = ?",
                     ('{"url":"https://example.test/a.mp4","no_index":true}', job.id))
    with patch("vdl.workers.DownloadService") as service:
        service.return_value.__enter__.return_value.process_url.return_value = []
        with pytest.raises(RuntimeError, match="no media downloaded"):
            run_job(cfg, q.get(job.id))
        assert service.call_args.kwargs["insecure"] is False


@pytest.mark.parametrize("command", ["get", "candidates"])
def test_html_candidates_carry_insecure(tmp_path, command):
    from vdl.classifier import ResourceKind
    args = ["--home", str(tmp_path), command, "https://example.test/page", "--insecure"]
    with patch("vdl.classifier.ResourceProbe") as probe, \
         patch("httpx.Client"), patch("vdl.service.DownloadService") as service:
        probe.return_value.probe.return_value = MagicMock(
            kind=ResourceKind.HTML, final_url="https://example.test/page")
        service.return_value.__enter__.return_value.fetch_html_candidates.return_value = (
            "https://example.test/page", [])
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        assert service.call_args.kwargs["insecure"] is True


def test_api_enqueue_carries_insecure(tmp_path):
    from api.main import EnqueueBody, enqueue
    cfg = Config(home=tmp_path)
    with patch("api.main.get_config", return_value=cfg), \
         patch("api.main._reject_if_html_page") as probe, \
         patch("api.main.ensure_runtime", return_value=("worker", 1, False)):
        enqueue(EnqueueBody(url="https://example.test/a.mp4", insecure=True))
        assert probe.call_args.kwargs["insecure"] is True
    job = JobQueue(cfg.db_path).claim_next()
    assert job.payload["insecure"] is True
