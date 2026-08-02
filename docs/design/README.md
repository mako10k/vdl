# vdl 設計・計画

## 設計（llmthink）

| 項目 | 値 |
|------|-----|
| DSL | [`vdl-architecture.dsl`](./vdl-architecture.dsl) |
| thought id | `vdl-architecture` |
| 状態 | **finalized** |
| 監査 | fatal=0 / error=0 / warning=0 |

確認:

```bash
llmthink thought show --id vdl-architecture summary
llmthink thought show --id vdl-architecture final
```

### 決定サマリ

1. **ResourceKind** = video / index / html / unknown（排他）
2. **video** → レジストリ照会 → 直 DL
3. **index (m3u8)** → fingerprint 重複判定 → ffmpeg 結合（MPD は MVP 外）
4. **html** → 候補抽出 → 済/新規表示 → 選択 → video/index 再入
5. **レジストリ** = SQLite `~/.vdl/registry.db`（completed のみ重複）
6. **CLI** = `get` / `list` / `status` / `info`（`vdl`）
7. 既存 `src/vdl` はドラフトとして設計に収束

## 計画（perttool）

| 項目 | 値 |
|------|-----|
| PERT | [`../../plans/vdl-mvp.pert`](../../plans/vdl-mvp.pert) |
| Mermaid | [`../../plans/vdl-mvp.mmd`](../../plans/vdl-mvp.mmd) |
| 先行制約 makespan | 15h |
| リソース制約 makespan | 20h（DEV capacity=1） |
| 状態 | **RELEASED**（全タスク done） |
| ギャップ監査 | [`gap-audit.md`](./gap-audit.md) |

```bash
perttool document check plans/vdl-mvp.pert
perttool dag analyze plans/vdl-mvp.pert
perttool dag next plans/vdl-mvp.pert   # 完了後は推奨タスクなし
pytest
```
