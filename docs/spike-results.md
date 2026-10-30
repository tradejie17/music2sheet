# M0 环境 Spike 结果（2026-10-10，M4 16GB，macOS 26.5）

测试曲目：从 MAESTRO v3 测试集通过 HTTP Range 抽取 3 首，均为整曲，未截取（`m2s.eval.datasets.fetch_maestro`）。
- `2014_wav--2`：Scarlatti K.208，149s
- `2013_wav--3`：Mozart K.280 第一乐章，194s
- `2008_wav--1`：Chopin Nocturne Op.15/2，232s

脚本：`backend/scripts/spike_*.py`

## 关口1：Transkun CPU vs MPS — **默认 MPS，CPU 兜底**

| 曲目 | CPU RTF | MPS RTF | MPS 显存 | CPU 峰值 RSS | onset F1（vs GT） | onset+offset F1 | CPU↔MPS 一致性 |
|---|---|---|---|---|---|---|---|
| Scarlatti | 0.111 | 0.083 | 3.65 GB | 2.3 GB | 1.000 | 0.729 | 1.0 |
| Mozart | 0.108 | 0.078 | 3.65 GB | 2.4 GB | 0.998 | 0.910 | 1.0 |
| Chopin | 0.113 | 0.082 | 3.65 GB | 2.4 GB | 0.989 | 0.609 | 1.0 |

- MPS 输出与 CPU 完全一致，速度约快 1.37 倍，显存 3.65GB（低于 6GB 门限），所以通过关口。semi-CRF 解码在 MPS 上没有出问题。
- 一首 3 分钟的曲子在 MPS 上约 15s 就能转完。
- Transkun 输出的音符没有置信度。sustain 和 soft 两种踏板都有输出。
- Chopin 的 onset+offset F1 偏低，原因是踏板多，判断 offset 本身就难。这个分数不影响记谱，因为记谱用的是「手指时值」。

## 关口1b：beat_this — **用 CPU**
- CPU 推理约 1.6–2.1s，MPS 约 1–3.4s（有预热开销），两者节拍结果完全一致（F=1.0）。用 CPU 更简单。
- **关键发现**：在 rubato 古典录音上，downbeat 很不稳定。
  - Mozart（3/4，136 BPM）：111 个小节检测为 3 拍，基本正确。
  - Scarlatti（45 BPM）和 Chopin（54 BPM）：每小节拍数在 1、2、3、4 之间跳动。
  - → meter 阶段**不能逐个相信 downbeat**。做法是先用众数和先验确定每小节拍数，再选择最佳相位，生成规则的强拍网格（相位和拍号允许用户在编辑器里修改）。慢速曲目还可能出现倍速或半速的歧义，BPM 先验在这里很重要。

## 关口2：依赖
- Python 3.11 + torch 2.14.1 + numpy 2.4 + music21 10.5 + partitura 1.9 + transkun 2.0.1 + beat-this 1.1.0：用 uv 一次装好，没有冲突。
- 把 setuptools 固定在 <81 后，transkun CLI 用到的 `pkg_resources` 还能导入。我们自己的引擎不调用 transkun CLI，而是用 `importlib.resources` 直接加载权重。
- basic-pitch：推迟到 M6 再处理（快速预览引擎），到时再决定是否需要单独的 venv。

## 关口3：MuseScore 4.7.5 CLI — **通过，但有一个怪癖**
- `mscore -o out.pdf in.musicxml` 首次运行约 6.7s，之后不到 1s。导出的 PDF 正确：带大括号的大谱表、调号、三连音、连音线、Ped./* 记号都在。
- **怪癖**：进程在退出阶段崩溃（`mutex lock failed`，返回码 134/-6），此时 PDF 已经写好。判定规则：**输出文件是新生成且非空就算成功**，忽略返回码，另加 60s 超时。

## 关口4：music21 导出
- `n.id` 会被导出为 `<note id>`。被小节线拆开的连音线片段会共用同一个 id，正好对应 ScoreDoc 里「一个逻辑音符」的概念。
- `expressions.PedalMark` 能导出 `<pedal>`，三连音会生成 `<time-modification>` 和 tuplet，跨小节的连音线正确，两个 PartStaff 会合并为 `<staves>2</staves>`。
- 待办：music21 默认标题是 "Music21 Fragment"，需要写入 metadata。
- OSMD 能否读取 `<note id>`：在 M4 前端阶段验证，用 idmap 键查找作为兜底。
