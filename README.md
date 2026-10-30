# music2sheet

音频 → 五线谱。思路是「AI 初稿 + 人工微调」：钢琴独奏录音先自动转成大谱表，再在浏览器编辑器里对照原始录音校对，最后导出 PDF / MusicXML / MIDI。

当前为阶段 0：只处理钢琴独奏；运行环境是本地单用户，Apple Silicon（M4 16GB）。架构依据见 `音频转乐谱系统_V2架构方案`，实施计划见 `docs/`。

```
音频/YouTube → ffmpeg → Transkun（音符 + 踏板，MPS） ─┐
                     → beat_this（节拍/强拍，CPU）  ──┴→ 节拍清理 → 量化（简单/复合拍模型选择）
   → 双手分谱（启发式 DP + piano_svsep 精修）→ 声部 → 调号/拼写 → ScoreDoc（唯一数据源）
   → music21 → MusicXML → OSMD 网页预览 / MuseScore PDF / MIDI
```

## 安装

需要：macOS（Apple Silicon）、[uv](https://docs.astral.sh/uv/)、pnpm，以及 Homebrew 安装的 `ffmpeg`。

```bash
brew install ffmpeg deno                 # deno：yt-dlp 下载 YouTube 需要
brew install --cask musescore            # PDF 渲染（首次请手动打开一次 MuseScore 4）

cd backend
uv sync --all-extras --group dev         # Python 3.11 环境（uv 会自动下载）
bash scripts/setup_svsep.sh              # 可选：piano_svsep 分手模型（独立 venv，约 1 GB）

cd ../frontend
pnpm install && pnpm build               # 构建后由后端同源提供
```

首次运行时会自动下载模型权重：Transkun 自带在 pip 包里；beat_this 约 77 MB，存到 `~/.cache/torch`。

## 运行

```bash
cd backend
uv run m2s serve                         # http://127.0.0.1:8000 （API + 任务 worker + 前端）
```

前端开发模式（热更新）：一个终端运行 `uv run m2s serve`，另一个终端在 `frontend/` 下运行 `pnpm dev`，然后打开 http://localhost:5173。`/api` 会自动代理到 8000 端口。

命令行（不经过网页）：

```bash
uv run m2s transcribe 录音.wav -o out/ --time-sig 3/4 --key "F major"
# 输出：out/score.musicxml、score.pdf、doc.json（ScoreDoc）、raw.mid、transcription.json、beats.json
```

先验参数都是可选的：`--key`、`--bpm`、`--time-sig`、`--grid 1/8|1/16|1/32`、`--no-triplets`、`--style pop|classical`。不填则自动检测。`--simplify 0|1|2` 选择原样 / 易读 / 简化，默认 1（易读）。

## 编辑器

- **乐谱视图**（OSMD）：点击音符即选中。低置信度音符显示为橙色。
- **卷帘视图**：
  - 拖动音符改位置；按住 Alt 拖动不吸附网格。
  - 拖音符右边缘改时值，双击空白处添加音符，拖空白处框选。
  - 点顶部标尺跳转播放位置。灰色虚影是原始转录结果，绿色细条是延音踏板。
- **快捷键**：
  - Del 删除，S 切换谱表
  - ↑↓ 移半音，⇧↑↓ 移八度，←→ 按网格移动
  - ⌘Z / ⇧⌘Z 撤销/重做，空格播放/暂停
- **检查器**：修改谱表、声部、音名拼写、延音线，以及锁定状态。手动改过的属性会自动锁定，之后「重新分手 / 重新拼写」不会覆盖它们。
- **工具栏**：
  - 网格、拍号、弱起、调号
  - 重新分手、重新拼写、重测调号
  - 「重新生成…」：换一组先验参数重新量化。转录结果走缓存，几秒钟完成。
  - **简化**（下拉框）：选择乐谱的详细程度，切换后用缓存几秒钟重新生成（ADR 0007）。
    - 原样：转录到的每个音、每个踏板都照写，可能出现第二声部和很多连音线。
    - 易读（新任务默认）：保留所有旋律与和声音；去掉装饰音和重复的和弦音；每行谱表一个声部；不用六连音；踏板只在开头标一次「con Ped.」；音很高或很低时自动加 8va / 换谱号。
    - 简化：右手只留旋律（最多再加一个和声音），左手每个和声换成一个柱式和弦；节奏网格更粗，没有连音。适合视奏或初学。
    - 简化只改谱面，播放和 MIDI 用的是同一份 ScoreDoc，所以听到的也是简化后的音符；踏板保留，听感不变。
  - **♩≈N / 速度 ×2 / 速度 ÷2**：显示当前节拍层级。如果谱子读起来太碎（全是十六分、三十二分音符）就按 ×2，太稀（小节很长、全是二分音符）就按 ÷2。两者听起来一样，只是换一种记法；简化级别保持不变。
  - 编辑过的乐谱在重新生成前会先确认；手动修改留在历史版本里。
  - 导出 PDF / MusicXML / MIDI（量化或原始）
- **播放**（底部播放栏）：
  - 四种模式：原始录音、乐谱合成（采样钢琴）、叠加对照、左原音 / 右乐谱。
  - 合成可以跟随录音速度，也可以按谱面匀速。
  - 编辑后再播放，听到的就是改过的版本。
  - 钢琴采样首次播放时下载（约 20 MB），之后可离线使用。
- **保存**：编辑停止 600 ms 后自动保存为新版本。乐谱视图、PDF 和导出文件都由同一份 ScoreDoc 生成，内容始终一致。

## 测试与评估

```bash
cd backend
uv run pytest                                  # 单元 + API 测试（< 5 s）
uv run pytest -m slow tests/regression         # ASAP 回归门限（约 2 分钟，需先运行一次 eval-asap）
uv run python tests/e2e/smoke.py               # 浏览器冒烟测试（需 m2s serve 正在运行，且有一个已完成的任务）
uv run python tests/e2e/latency.py <job_id>    # 编辑 → 预览延迟
uv run python tests/e2e/toolbar.py <job_id> --base http://127.0.0.1:8401
                                               # 简化 / 速度 ×2 ÷2 / 重新生成（会多次替换该任务的乐谱，请用副本任务和带 worker 的独立服务器）

uv run m2s eval-maestro --n 15 --crop 90       # 转录：note-level F1（MAESTRO v3 测试集，按需下载单个文件）
uv run m2s eval-asap                           # 记谱：量化 / 拍号 / 调号 / 分手 / 弱起 / 渲染（228 首 ASAP 乐谱）
```

评估结果见 `docs/evaluation.md`，设计决策见 `docs/adr/`。

整体架构（流水线、底层逻辑、API、技术栈、评估，中英双语）见 [`docs/architecture.md`](docs/architecture.md)。

## 目录

```
backend/src/m2s/
  ingest/      ffmpeg 归一化、YouTube 下载
  engines/     Transkun、beat_this（子进程运行）；piano_svsep 预测脚本
  pipeline/    阶段 S1–S7、内容哈希缓存、重新推导
  quantize/    节拍清理、细分 Viterbi（简单/复合拍模型）、拍号与弱起
  hands/       双手分谱（启发式 DP + piano_svsep）、声部
  notation/    调号与拼写、ScoreDoc → music21（自行拆分时值、三连音括号）
  render/      MusicXML、MuseScore PDF、MIDI、严格校验器
  jobs/ api/   SQLite 任务表、单 worker、FastAPI
  eval/        MAESTRO / ASAP 评估、合成演奏生成器
frontend/src/  React：上传与任务、OSMD 乐谱视图、Canvas 卷帘、检查器、播放
data/          （git 忽略）任务、缓存、数据集、评估结果、svsep 环境
```

## 已知局限（阶段 0）

- 只处理钢琴独奏。多乐器分离和人声谱属于后续阶段。
- 拍号的「拍单位」无法单从音频判断，例如 2/2 和 2/4、3/8 和 3/4、12/8 和带三连音的 4/4。出现歧义时请填写拍号先验参数，或在编辑器里修改。
- 全曲只用一个调号和一个拍号。中途转调或变拍号暂不检测，可以在编辑器里改。
- 节拍追踪在自由速度（rubato）较大的古典录音上较弱，这时填写 BPM 和拍号先验参数帮助最大。
- MuseScore 4.7 在无界面模式下退出时会崩溃，但这发生在 PDF 写完之后。程序已按「输出文件存在即成功」处理。
