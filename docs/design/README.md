# vdl 設計・計画

## 1. コア DL（llmthink `vdl-architecture`）

| 項目 | 値 |
|------|-----|
| DSL | [`vdl-architecture.dsl`](./vdl-architecture.dsl) |
| 状態 | finalized |
| ギャップ | [`gap-audit.md`](./gap-audit.md) |
| 計画 | [`../../plans/vdl-mvp.pert`](../../plans/vdl-mvp.pert) |

## 2. キュー / デーモン / ベクトル索引（llmthink `vdl-queue-index`）

| 項目 | 値 |
|------|-----|
| DSL | [`vdl-queue-index.dsl`](./vdl-queue-index.dsl) |
| 状態 | finalized |
| 計画 | [`../../plans/vdl-queue-index.pert`](../../plans/vdl-queue-index.pert) |

### 決定サマリ

1. 全作業を **jobs キュー**（download 優先度 100 > 索引 40/30/20）
2. デーモン単一ワーカー、`claim_next` で priority 順
3. 索引: **scene_detect → clip_optimize → embed (Ollama)**
4. Chroma は modality 別コレクション（CLIP 512d / text 768d）
5. CLI は `get`（enqueue）/ `queue` / `daemon` / `registry`

```bash
llmthink thought show --id vdl-queue-index summary
perttool document check plans/vdl-queue-index.pert
pytest
```
