"""Search helpers: score aggregation (no Ollama required)."""

from __future__ import annotations

from vdl.search import SearchHit, _aggregate_by_download, _distance_to_score


def test_distance_to_score() -> None:
    assert _distance_to_score(0.0) == 1.0
    assert _distance_to_score(0.5) == 0.5
    assert _distance_to_score(None) == 0.0


def test_aggregate_by_download_picks_best_and_excludes() -> None:
    hits = [
        SearchHit(
            rank=1,
            download_id=2,
            scene_id=1,
            scene_index=0,
            score=0.9,
            distance=0.1,
            modality="clip",
            title="a",
            path="/a",
            url=None,
            document="d1",
            chroma_id="c1",
        ),
        SearchHit(
            rank=2,
            download_id=2,
            scene_id=2,
            scene_index=1,
            score=0.5,
            distance=0.5,
            modality="clip",
            title="a",
            path="/a",
            url=None,
            document="d2",
            chroma_id="c2",
        ),
        SearchHit(
            rank=3,
            download_id=3,
            scene_id=3,
            scene_index=0,
            score=0.8,
            distance=0.2,
            modality="clip",
            title="b",
            path="/b",
            url=None,
            document="d3",
            chroma_id="c3",
        ),
        SearchHit(
            rank=4,
            download_id=1,
            scene_id=4,
            scene_index=0,
            score=0.99,
            distance=0.01,
            modality="clip",
            title="self",
            path="/s",
            url=None,
            document="d4",
            chroma_id="c4",
        ),
    ]
    out = _aggregate_by_download(hits, exclude_download_id=1, limit=10)
    assert [h.download_id for h in out] == [2, 3]
    assert out[0].score == 0.9
    assert out[0].scene_index == 0
