"""Search helpers: score aggregation + diversity (no Ollama required)."""

from __future__ import annotations

from vdl.index.embed import needs_clip_translation
from vdl.search import (
    SearchHit,
    _aggregate_by_download,
    _distance_to_score,
    _diversify_hits,
    _host_key,
)


def test_distance_to_score() -> None:
    assert _distance_to_score(0.0) == 1.0
    assert _distance_to_score(0.5) == 0.5
    assert _distance_to_score(None) == 0.0


def test_needs_clip_translation() -> None:
    assert needs_clip_translation("海辺") is True
    assert needs_clip_translation("車") is True
    assert needs_clip_translation("a red car") is False
    assert needs_clip_translation("beach") is False


def test_diversify_limits_per_domain() -> None:
    hits = [
        SearchHit(
            rank=i,
            download_id=i,
            scene_id=i,
            scene_index=0,
            score=1.0 - i * 0.01,
            distance=i * 0.01,
            modality="clip",
            title=f"t{i}",
            path=None,
            url=f"https://same.cdn.example.com/v{i}.mp4",
            document="",
            chroma_id=f"c{i}",
        )
        for i in range(1, 8)
    ]
    hits.append(
        SearchHit(
            rank=99,
            download_id=99,
            scene_id=99,
            scene_index=0,
            score=0.5,
            distance=0.5,
            modality="clip",
            title="other",
            path=None,
            url="https://other.example.org/x.mp4",
            document="",
            chroma_id="c99",
        )
    )
    # limit=3: 2 from dominant host + 1 other (no need to relax cap)
    out = _diversify_hits(hits, limit=3, max_per_domain=2)
    assert len(out) == 3
    hosts = [_host_key(h.url, h.title) for h in out]
    from collections import Counter

    c = Counter(hosts)
    assert c.get("same.cdn.example.com", 0) <= 2
    assert "other.example.org" in hosts


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
