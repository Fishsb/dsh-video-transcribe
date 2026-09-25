---
name: video-subtitle-extract
description: 从视频中提取文案/字幕。当用户要求"提取视频文案/字幕/转文字/转写/语音识别/把视频变成文章"，给出视频文件路径（mp4/mkv/mov 等）或视频链接（B站等）需要产出最终文案时使用。双链路：本地视频（字幕轨抽取/语音转写）与在线链接（元数据采集→字幕优先→取音轨→宿主 ASR 转写→大模型纠正成稿）。主路线零第三方依赖：scripts/vt_pipeline.py（纯标准库）+ ffmpeg + 宿主 ASR 服务；所有路径运行时探测 + 环境变量覆盖。
---

# 视频文案提取（Video Subtitle Extract）

## Overview

从**本地视频**或**在线视频链接**中提取可用文案，最终产出：最终文案.md + 原始转写.txt（+ 分段.md）+ info.json。

产出链路的**主路线**（2026-09-25 实测可跑）：

```
skill/scripts/vt_pipeline.py   # 纯 Python 标准库，零第三方依赖
  ├─ B站元数据（view / tag / reply / 弹幕 XML）—— 官方 API 直连，无需 cookie
  ├─ playurl 取最低码率音轨 → 下载 → ffmpeg 转 16k 单声道 wav
  └─ 宿主 ASR 服务（默认 http://127.0.0.1:3082/rpc，SenseVoice）转写；长音频自动切片
```

**核心思路**：能拿字幕就不下载；下载时连带元数据（标题/简介/评论/UP画像/弹幕）；转写走宿主 ASR；最后由大模型结合元数据纠正成稿（转写稿不得直接交付）。

## 环境变量（全部可选；路径一律运行时探测，代码不写死本机路径）

| 变量 | 作用 | 默认 |
|---|---|---|
| `VT_ASR_URL` | 宿主 ASR RPC 地址 | `http://127.0.0.1:3082/rpc` |
| `VT_FFMPEG` / `VT_FFPROBE` | ffmpeg / ffprobe 全路径 | PATH 探测 |
| `VT_OUTDIR` | 产物根目录 | `./output` |
| `VT_TOOL_DIR` | 额外候选工具目录（可多个，用 `;` 分隔） | 空 |
| `VT_PYTHON` | 写进元数据 / 跑辅助脚本的解释器 | `sys.executable` |
| `VT_BBDOWN` / `VT_VISION_PY` | 可选适配器的可执行文件 / 脚本全路径 | 空 |
| `VT_TIMEOUT_HTTP` / `VT_TIMEOUT_ASR` | 秒 | 60 / 600 |

## 决策树

```
【本地视频】视频文件路径
  probe → 有内嵌字幕轨? ──是──▶ subtitle 抽取（秒级）
            否
            画面有烧录字幕? ──否──▶ 转写（主路线：vt_pipeline.py transcribe）
                             └是──▶ OCR（可选适配器 frames，需视觉脚本；未配置则跳过）

【在线链接】B站 URL 或 BV 号
  ① 元数据：vt_pipeline.py meta <URL>     → info.json + 弹幕时间线.txt
  ② 取音轨：vt_pipeline.py audio <URL>    → audio.m4s + audio.wav（16k 单声道）
  ③ 转写　：vt_pipeline.py transcribe <URL> → 原始转写.txt / 原始转写_分段.md
  ④ 一条龙：vt_pipeline.py bili <URL>     → ①+②+③ 一次完成
  ⑤ 纠正成稿：读 info.json + 原始转写 → 最终文案.md（见「最终文案格式规范」）
```

优先级：**软字幕 > 语音转写 > OCR**；在线场景 **字幕 > 取音轨转写**。

## 主路线命令速查

用任意实存 Python 3.10+ 运行。**注意：本机裸 `python` / `py` 不在 PATH**（2026-09-25 实测），
请用解释器全路径，或把解释器目录加入 PATH，或用 `VT_PYTHON` 记录它：

```bash
PY="<你的 python.exe 全路径>"
SCRIPT="<本技能目录>/scripts/vt_pipeline.py"

"$PY" "$SCRIPT" probe                          # 探测 ffmpeg / ffprobe / 宿主 ASR（不联网、不写盘）
"$PY" "$SCRIPT" meta       <B站URL或BV号>       # 元数据 + 评论 + 弹幕 → info.json
"$PY" "$SCRIPT" danmaku    <B站URL或BV号>       # 只抓弹幕 → 弹幕时间线.txt
"$PY" "$SCRIPT" audio      <B站URL或BV号>       # 最低码率音轨 → audio.wav(16k 单声道)
"$PY" "$SCRIPT" transcribe <B站URL或BV号>       # 同上再调宿主 ASR 转写
"$PY" "$SCRIPT" transcribe <本地音频/视频> --outdir <目录>
"$PY" "$SCRIPT" bili       <B站URL或BV号>       # 一条龙
```

产物落在 `$VT_OUTDIR/<bvid>/` 下（默认 `./output/<bvid>/`）：`info.json`、`弹幕时间线.txt`、
`audio.m4s`、`audio.wav`、`原始转写.txt`、`原始转写_分段.md`、`转写元信息.json`。
**绝不写 samples/**（那是实证产物，只增不改）。

## 可选适配器（本机 2026-09-25 实测**不存在**，主路线不需要）

```bash
# 以下都需要额外依赖，缺失不影响主路线；报错时按提示改用 vt_pipeline.py
"$PY" scripts/video_text_extract.py probe <文件>        # ✅ 可跑：ffprobe 探测 + 建议路线（无第三方依赖）
"$PY" scripts/video_text_extract.py subtitle <文件>     # ✅ 可跑：抽内嵌字幕轨（无第三方依赖）
"$PY" scripts/video_text_extract.py transcribe <文件>   # ⛔ 需 faster-whisper（未安装）
"$PY" scripts/video_text_extract.py bili <URL>          # ⛔ 需 BBDown（未安装）
"$PY" scripts/video_text_extract.py pipeline <URL>      # ⛔ 需 yt-dlp（未安装）
"$PY" scripts/video_text_extract.py frames <URL>        # ⛔ 需 BBDown + VT_VISION_PY 视觉脚本
```

| 适配器 | 依赖 | 本机状态（2026-09-25 实测） |
|---|---|---|
| BBDown | 自包含 .exe | ❌ 不存在（`where BBDown` 无结果） |
| yt-dlp | Python 包/单文件 | ❌ 不存在 |
| you-get | Python 包 | ❌ 不存在 |
| faster-whisper | Python 包 | ❌ 未安装（转写已由宿主 ASR 取代） |
| wbi 直连下载 | 纯标准库 + ffmpeg | ✅ **主路线就是这个** |

## 弹幕评论拆解点评（内嵌标注）

**目的**：UP 口述是个人输出，可能有偏差或主观性。用**弹幕 + 评论**对文案观点做**对照标注**——
**不改文案原文一个字**，标注是附加层（引用块内嵌在观点段落后）。

**数据准备（两路）**：
1. 弹幕：`vt_pipeline.py meta <URL>` 或 `danmaku <URL>` → `弹幕时间线.txt`（`[mm:ss] 内容`，按时间排序）。
   弹幕为空是正常结果：info.json 里 `danmaku_count: 0` **显式写出**，不静默缺字段。
2. 评论：`meta` 同时抓热评（`x/v2/reply`，前 20 条 + 一级回复）写入 `info.json.comments`。

**标注体系**：

| 标记 | 含义 | 判定 |
|:----|:----|:----|
| ✅ 印证 | 弹幕/评论支持该观点 | 有具体弹幕/评论表达认同或补充佐证 |
| ⚠️ 争议 | 有不同意见或反驳 | 弹幕/评论出现反对、质疑、不同角度 |
| 💡 补充 | 提供了视频没说的信息 | 新案例/新数据/延伸讨论 |
| ❓ 存疑 | UP 口述可能不准确 | 数据/术语/说法被弹幕指出或明显存疑，建议核实 |

- **有反对声就必须标注**（哪怕只有一条）；**争议条目排在印证前面**；相左意见逐条列出、不合并取舍。
- 每条标注带来源（弹幕时间点 / 评论点赞数），可追溯；每个观点最多选 2-3 条最有代表性证据。
- **无有效证据的观点不标注**（不凑数）。

> 📌 边界声明：本技能只负责**提取与标注**；标注数据是提取产物，如何沉淀由专家团规则决定。
> 两个体系职责分离，本技能不写沉淀规则。

**标注格式（内嵌在最终文案.md 观点段落后）**：

```markdown
> 💬 **弹幕/评论标注**（弹幕 74 条 + 热评 3 条）：
> - ⚠️ 弹幕[06:31]有不同看法："…"
> - ✅ 热评印证（赞419）："…"
```

## 画面补充（第三层信息 · 主路线已内置）

教学/课程/屏幕录制类视频的画面常含**音频没念的关键信息**（PPT 标题、文档正文、图表、字幕条）。
链路：**API 直连取低码率视频流 → ffmpeg 按时间点抽帧 → 本地视觉模型 OCR → `帧OCR原始结果.md`**。

```bash
"$PY" "$SCRIPT" frames <URL>                          # 均匀抽帧，默认间隔 20s
"$PY" "$SCRIPT" frames <URL> --at 00:30,02:20,03:00   # 显式时间点（更省且可控）
```

- **不依赖 BBDown**：走 `playurl` 接口取最低码率视频流（与取音轨同一套机制），第三方下载器缺失不影响本层。
- 视觉模型走本地 Ollama（默认 `qwen3.5:9b`），可用 `VT_VISION_URL` / `VT_VISION_MODEL` 覆盖。
- 抽帧时间点：`--at` 显式指定优先；未指定则按 `--interval`（默认 20s）均匀采样。
- 视觉模型 OCR 可能误读，重要文字（书名/数字/专名）建议交叉验证；画面信息仅作补充，不替代音轨转写。

> ⚠️ **空帧率守卫（踩坑后加的）**：空帧**不等于**画面无文字——实测旧流程曾把有内容的帧
> 记成空（视觉模型静默失败），导致成品里的画面断言在归档证据中"查无实据"，险些被误判为编造。
> `frames` 跑完会检查空帧占比，**> 1/3 时显式告警**，提示先排查 OCR 是否失败
> （对比帧图体积：空屏通常 <30KB）。机检见 `scripts/check-content.py` 的 C6 守卫。

> 📌 旧适配器 `video_text_extract.py frames` 仍保留，但它依赖 BBDown（本机不存在）与
> `VT_VISION_PY` 视觉脚本，**默认不可用**；主路线一律用 `vt_pipeline.py frames`。

> ⚠️ **抽帧方式会改变 OCR 结论（实测 2026-09-25）**：同一时刻 `02:40`，
> `--at` 精确 seek 取到的是**过渡帧**（仅 `Code Cheers!` 标题卡，12.8 KB），
> 而 `fps=1/20` 均匀采样取到的是**有内容帧**（`耳机没有声音`/`第一次交流(保存经历)`，53.8 KB）。
> 原因：演示类画面的内容是**瞬变**的，±1 帧就可能跨过一屏。
> ⇒ 对**屏幕录制/演示**类视频，**单点 `--at` 采样可能漏内容**；建议用 `--interval`
> 加密采样（如 10s），或对关键处多点取样后取并集。反过来，帧图体积可作**粗筛**：
> 明显偏小（接近标题卡/纯色）者优先怀疑漏内容。

## 最终文案格式规范（三层信息组织）

一份最终文案 = **①纠错原文（主干）②弹幕/评论标注（观点对照）③画面信息（视觉补充）**。

**文件头（元信息块）**：

```markdown
# <视频标题>
> 来源: <BV号> | UP主: <名字> | 时长: <mm:ss> | 播放: <N>
> 方法: 宿主 ASR(SenseVoice) 转写 + 大模型纠错 | 日期: <YYYY-MM-DD>
> 标注: 弹幕 <N> 条 + 评论 <N> 组
```

**正文结构**：`一、<章节名>` → 纠错原文 → 💬 标注块 → … → `画面信息补充` → `整理说明`。

- **原文一字不改**：标注（💬）与画面补充（🖼️）都是附加层。
- **时间戳统一**：弹幕 `[mm:ss]`、画面 `[mm:ss]`、评论 `[赞N]`。
- 元信息块每次提取都更新，确保可复现。

## 输出规范

- 在线链接：`{outdir}/{bvid}/` 下：`info.json`、`弹幕时间线.txt`、`audio.wav`、`原始转写.txt`、
  `原始转写_分段.md`、`转写元信息.json`，纠正后追加 `最终文案.md`。
- 本地视频（可选适配器）：`{outdir}/srt`、`{outdir}/txt`、`{outdir}/json`。
- 统一 UTF-8 **无 BOM**。

## 执行要点

1. **转写先看 ASR 是否在跑**：`vt_pipeline.py probe` 会打印 `/health` 结果；不通时用 `VT_ASR_URL` 覆盖，
   或先启动 ASR 服务。
2. **必须用 ffmpeg 转码**，不要手写 WAV 头：宿主 sherpa-onnx 解析非标准头会报
   `Cannot read properties of null (reading 'sampleRate')`，且服务端只回 `{"ok":false,...}`
   —— 脚本已把这类"HTTP 200 但 ok=false"当失败抛出，不会把空文本当成功。
3. **长音频自动切片**：默认超过 30 秒即 `ffmpeg -f segment` 切片逐片转写，产物带时间戳分段。
4. **纠正成稿这一步不能省**：读 `info.json` + `原始转写.txt`，修正专名/删口语词/补标点/分段，
   输出 Markdown 最终文案.md。**转写稿不得直接交付**。
5. **B站接口失败要显式**：info.json 的 `pipeline.notes` 会记录 tag/reply/弹幕接口的失败原因；
   弹幕/评论为空时写 0 而不是缺字段。
6. **原片音轨下载**：优先 `baseUrl`，失败自动退 `backupUrl`；全部失败才报错退出。
7. **隐私**：全部本地处理，不上传；元数据/评论落盘后注意管理。
8. **改完链路要回归**：`python scripts/check-regression.py --e2e` 一键跑完 R1–R4 并给结论
   （R1 样例只增不改 / R2 上游纯净 / R3 零硬编码 / R4 端到端实跑）。
   **"代码改了"不等于"链路通了"**；三态里 **SKIP 不计通过**（未验证 ≠ 通过，退出码 2 会传出来）。

## 扩展参考

- 方案与验收：`docs/方案与验收.md`；本机环境事实：`docs/本机环境事实.md`
- 既有单脚本（可选适配器）：`scripts/video_text_extract.py`
