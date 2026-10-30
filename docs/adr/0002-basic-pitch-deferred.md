# ADR 0002：暂不提供 basic-pitch「快速模式」

- 状态：已采纳（2026-10-11）；可以随时恢复

## 背景

原方案计划用 Spotify basic-pitch 做「秒级预览 / 兜底」引擎。

## 实测（M4，Scarlatti K.208，149 秒音频）

| 引擎 | 推理时间 | 导入时间 | onset F1（相对 MAESTRO 标注） |
|---|---|---|---|
| basic-pitch 0.4（CoreML） | 6.1 s | 14.5 s | **0.72**（精确率 0.60） |
| Transkun（MPS） | 12.4 s | <1 s | **1.00** |

## 决策

不提供这个模式。Transkun 在 M4 上转 3 分钟的曲子只需约 15 秒，basic-pitch 节省的几秒抵不上大量错音。需要更快的预览时，更好的办法是只用 Transkun 转前 30 秒。

## 影响

- 已移除 `preview` 可选依赖；`Priors.engine` 只保留 `transkun`。
- 如果要恢复，在 `engines/` 下加一个 `basicpitch_eng.py`，并在 `ENGINES` 里注册即可，接口保持不变。
