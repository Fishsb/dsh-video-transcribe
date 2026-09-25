---
name: video-subtitle-extract
description: 从视频中提取文案/字幕。当用户要求"提取视频文案/字幕/转文字/转写/语音识别/把视频变成文章"，给出视频文件路径（mp4/mkv/mov 等）或视频链接（B站/抖音/YouTube 等）需要产出最终文案时使用。支持双链路：本地视频（字幕轨抽取/语音转写）与在线链接（元数据采集→字幕优先→下载兜底→转写→大模型纠正成稿）。本技能内置本机环境探测（ffmpeg、yt-dlp、faster-whisper 及 D 盘本地模型缓存），直接调用脚本即可。B站链接建议优先用 `bili` 子命令（官方 API 直连，绕过 yt-dlp 412 反爬）。
---

# 视频文案提取（Video Subtitle Extract）

## Overview

从**本地视频**或**在线视频链接**中提取可用文案，最终产出：最终文案.md（大模型纠正后）+ 原始转写.srt / .txt + 元数据.json。核心工具是 `scripts/video_text_extract.py` 单脚本，覆盖完整链路。

**核心思路（用户方案）**：能拿字幕就不下载；下载时连带元数据（标题/简介/评论/UP画像）；本地模型快速转写；最后由 WorkBuddy 用大模型结合元数据纠正成稿。

## 决策树

```
【本地视频】视频文件路径
  probe → 有内嵌字幕轨? ──是──▶ subtitle 抽取（秒级）
            否
            画面有烧录字幕? ──否──▶ transcribe 转写（主力）
                             └是──▶ OCR（PaddleOCR 未装，需先装）

【在线链接】B站/抖音/YouTube 等 URL
  ① 优先判断平台：
     - B站（bilibili.com / BV号） → **优先调用现成工具 `D:\lk\Video-Transcribe\video_transcribe.py`**（GPU 加速 + BBDown + 清代理一体，✅主力；自动附带评论 20 条 + 弹幕 xml/ass）
     - 其他平台 → `pipeline` 一键（yt-dlp）
  ② 链路：抓元数据 → 检索字幕 → 有字幕下载SRT | 无字幕下音频 → 转写 → (WorkBuddy)大模型纠正成稿
  ③ **（B站可选）弹幕评论拆解点评**：用 `danmaku` 子命令解析弹幕时间线 → 结合评论 → 在最终文案中内嵌标注（不改原文，见下方「内嵌标注」章节）
  ④ **（B站可选）画面识图**：教学/课程/屏幕录制类视频，画面常含 PPT/文档/网页实拍等音频没念的关键信息 → `frames` 子命令（下载低画质视频 → ffmpeg 抽帧 → 视觉模型 OCR）→ 产出 `画面信息.md`，纠错成稿时作为「画面信息补充」章节（不改原文，见下方「画面识图」章节）

⚠ B站反爬坑：yt-dlp 直接抓 B站 常触发 HTTP 412 Precondition Failed（实测 2026-08-07）。
   → 一律用 BBDown（内置 WBI 签名+多线程，彻底避开 412），不要反复重试 yt-dlp。
   → B站在线视频首选现成工具 Video-Transcribe（下方"与现成工具的分工"）；技能脚本 `bili` 子命令保留为兜底。
```

优先级：**软字幕 > 语音转写 > OCR**；在线场景 **字幕 > 下载转写**。

## 弹幕评论拆解点评（内嵌标注，2026-08-07 新增）

**目的**：UP 口述是个人输出，可能有偏差或主观性。用**弹幕 + 评论**对文案观点做**对照标注**——读者看文案时能看到"这个观点其他人怎么想"。**不改文案原文一个字**，标注是附加层（引用块内嵌在观点段落后）。

**触发**：用户说"结合评论弹幕点评/标注文案/看看大家怎么看"；或提取 B站访谈/观点类视频时默认询问是否要标注。

**数据准备（两路）**：
1. 弹幕：现成工具已自动下载 `*.xml`/`*.ass` 到输出目录；或技能脚本 `danmaku` 子命令一键下载+解析：
   `... danmaku "https://www.bilibili.com/video/BVxxx" --outdir <dir>` → 产出 `弹幕时间线.txt`（[mm:ss] 内容，按时间排序，可对齐文案段落）
2. 评论：现成工具已自动抓取（`x/v2/reply`，热评 20 条）——若未抓取，用 API 手动拉取（见执行要点 6）

**标注体系（WorkBuddy 执行）**：
| 标记 | 含义 | 判定 |
|:----|:----|:----|
| ✅ 印证 | 弹幕/评论支持该观点 | 有具体弹幕/评论表达认同或补充佐证 |
| ⚠️ 争议 | 有不同意见或反驳 | 弹幕/评论出现反对、质疑、不同角度 |
| 💡 补充 | 弹幕/评论提供了视频没说的信息 | 新案例/新数据/延伸讨论 |
| ❓ 存疑 | UP 口述可能不准确 | 数据/术语/说法被弹幕指出或明显存疑，建议核实 |

**⚠️ 争议优先规则（2026-08-07 强化，用户反馈）**：
- **有反对声就必须标注**：一个观点只要弹幕/评论出现相左意见（哪怕只有一条），必须标 ⚠️——不因"反对声少"而忽略；反对声是观众给内容的质检报告
- **争议条目排在印证前面**：标注块内 ⚠️/❓ 优先于 ✅/💡 展示（读者先看到不同声音）
- **相左意见全收录**：同一观点的多条相左意见（尤其来自不同角度：商业模式/读者分层/文化差异等）逐条列出，不合并取舍

> 📌 边界声明：本技能只负责**提取与标注**；标注数据（弹幕时间戳/评论原文）是提取产物，**如何用于沉淀由专家团规则决定**（见专家团 `knowledge-base-ops-protocol.md` 模式D"相左意见同等权重"）——两个体系职责分离，本技能不写沉淀规则。

**标注格式（内嵌在最终文案.md 观点段落后）**：
```markdown
> 💬 **弹幕/评论标注**（弹幕 74 条 + 热评 3 条）：
> - ⚠️ 弹幕[06:31]有不同看法："不如不写，从来没订过现实部分"
> - 💡 弹幕[08:29]补充建议："就不能写个架空的自由主义国家吗，然后上交"
> - ✅ 热评印证（赞419）："群像在网文来说就是难写，玄鉴当初也才百订…"
```
- 标注放在**观点段落之后**、**段落原文不改**
- 每条标注带来源（弹幕时间点 / 评论点赞数），可追溯
- 每个观点最多选 2-3 条最有代表性的证据，不堆砌
- 无有效证据的观点不标注（不强行凑）
- 标注密度：核心观点全标，次要细节选择性标注

**执行流程**：读 `弹幕时间线.txt` + 评论 → 对照最终文案各章节观点 → 内嵌标注 → 交付带标注的最终文案.md（在交付说明中注明"已含弹幕评论标注"）。

## 画面识图（2026-08-07 新增，08-07 晚升级：时间轴对齐抽帧）

**目的**：教学/课程/屏幕录制类视频的画面常含**音频没念的关键信息**——PPT 标题、文档正文实拍、参考书网页、图表、字幕条。音频转写会漏掉这些，识图补上。

**触发**：教学/教程/拆书/屏幕录制类视频（画面有文档、PPT、网页的概率高）；或用户说"画面里有什么信息/PPT内容"。

**命令**：
```bash
"C:/Users/lk/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" scripts/video_text_extract.py frames "https://www.bilibili.com/video/BVxxx" --outdir <dir> [--frames N] [--at mm:ss,mm:ss]
```
- 链路：BBDown 下载 360P 视频 → **按时间点抽帧** → **视觉模型 OCR**（vision-analyze 技能，默认 minimax-m3，opencode.ai 网关）
- **抽帧时间点决定（大模型语义判断，非关键词硬匹配）**：
  1. `--at mm:ss,mm:ss` 显式指定（WorkBuddy 看转写稿后按需定）
  2. 默认**大模型语义选帧**：把 `*_segments.json`（带时间戳文案）发给 LLM（deepseek-v4-flash，opencode.ai 网关），**理解式判断**哪些位置需要抽帧——识别"引导观众看画面/展示材料/讲重点"的任意表达（含口语化/隐性："这地方有讲究""感受一下""看这个细节"），不依赖关键词表；返回时间点+理由（理由=画面内容提示）
  3. LLM 失败自动降级**关键词启发式**（信号词三级 + 分段择优兜底，见下）
  4. 都拿不到才退回均匀采样（明确打印警告）
  - **LLM 容错**：max_tokens 自动加量重试（finish_reason=length 时 ×1.5）、JSON 强容错解析（剥离代码块/注释/尾逗号）、失败降级不阻塞
  - **关键词兜底三级标准**（LLM 不可用时）：A 级·指示（看这里/大家看/注意/划重点/就是这里/仔细看…）；B 级·强调（重点/关键/说白了/举个例子/接下来…）；C 级·操作（打开/切到/这个页面/上面写着…）
- 产物：`画面信息.md`（每帧 OCR 文本 + **帧标题带命中理由**：LLM 判断为"展示什么/引导看什么"的语义理由，关键词为信号词；WorkBuddy 可直接对齐章节）
- 耗时：3 帧约 1-2 分钟；8 帧约 3-5 分钟，建议后台运行

**用法**（WorkBuddy 纠错成稿时）：
- 读 `画面信息.md` → 按帧时间点**对齐到对应文案章节**（segments.json 里该时间点附近的段落 = 该帧对应的内容）→ 整理成**「画面信息补充」章节**追加到最终文案末尾（在整理说明之前），**不改原文**
- 每条画面信息标注时间点（如 `[02:15]`），与弹幕标注格式统一，读者可对照视频
- 画面信息与正文矛盾时，以画面实拍为准（如参考书书名、具体文字），并在补充章节注明来源帧号

**边界**：视觉模型 OCR 可能误读（历史误读率高），重要文字（书名/数字/专名）建议用 `--focus` 聚焦复核或与音轨交叉验证；画面信息仅作补充，不替代音轨转写。

## 最终文案格式规范（2026-08-07 晚新增，三层信息组织）

**目的**：一份最终文案 = 三层信息：**①纠错原文（主干）②弹幕/评论标注（观点对照）③画面信息（视觉补充）**。三层按固定顺序组织，读者从上往下读即完整理解。

**文件头（元信息块）**：
```markdown
# <视频标题>
> 来源: <BV号> | UP主: <名字> | 时长: <mm:ss> | 播放: <N> 万
> 方法: faster-whisper/<model> GPU 转写 + WorkBuddy 纠错 | 日期: <YYYY-MM-DD>
> 标注: 弹幕 <N> 条 + 评论 <N> 组 | 画面: <N> 帧识图
```

**正文结构**：
```
## 一、<章节名>
<纠错后的原文段落>
> 💬 弹幕/评论标注（⚠️ 争议优先，见上节）
## 二、<章节名>
...
## 画面信息补充（AI 识图）
> 每条含 [时间点] + 内容 + 对应章节说明
## 整理说明（WorkBuddy 纠错备注）
1. 转写纠错说明（专名修正表）
2. 数据清单（弹幕/评论/画面来源文件）
3. 标注规则执行情况
```

**关键规则**：
- **原文一字不改**：标注（💬）和画面补充（🖼️）都是附加层，引用块或独立章节呈现
- **时间戳统一**：弹幕用 `[mm:ss]`（弹幕原始时间）、画面用 `[mm:ss]`（抽帧时间点）、评论用 `[赞N]`（点赞数）——读者可溯源
- **争议优先**：标注块内 ⚠️/❓ 排在 ✅/💡 前
- **画面补充按时间对齐**：每条画面信息标注对应的视频时间点和对应章节（如"对应章节三 简介问题"），避免与正文脱节
- 元信息块每次提取都更新，确保可复现

## 与现成工具的分工（重要，避免双份维护）

**主力**：`D:\lk\Video-Transcribe\video_transcribe.py`（lk 现成工具，2026-08-07 整合升级）
- B站/YouTube 在线视频一条命令出稿：`python video_transcribe.py <URL> --output-dir <dir>`
- 已内置：GPU 自动检测（RTX 2070 SUPER → cuda+float16，实测比 CPU 快 5.3 倍）、BBDown 下载（实测 26MB/4.7s）、自动清代理、元数据 API 兜底；**内置 LLM 纠错默认关闭（--llm-correction 启用），纠错由 WorkBuddy 完成（见执行要点 4）**
- 运行环境：hermes venv（同下）
```
cd /d/lk/Video-Transcribe && "C:/Users/lk/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" video_transcribe.py "https://www.bilibili.com/video/BV..." --output-dir "D:\lk\output"
```

**兜底**：本技能 `scripts/video_text_extract.py`
- 保留 Video-Transcribe 没有的能力：本地视频转写/字幕轨抽取（subtitle/probe/transcribe/auto 子命令）、`subs`/`fetch` 子命令、srt/txt/json 分目录输出
- B站在线场景仅在 Video-Transcribe 不可用时用 `bili` 子命令兜底
- 注意：技能脚本转写目前 CPU 优先（自动检测 GPU 的改动见脚本第 221 行附近），速度约慢 5 倍

## 命令速查（可复制）

脚本路径：`C:\Users\lk\.workbuddy\skills\video-subtitle-extract\scripts\video_text_extract.py`
运行环境：必须用 hermes venv 的 Python（faster-whisper 只装在这里）：
`C:\Users\lk\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`

```
# 本地视频
... probe    D:\videos\demo.mp4                          # 探测，看建议路线
... subtitle D:\videos\demo.mkv --track 0                # 抽内嵌字幕轨
... transcribe D:\videos\demo.mp4 --model small          # 语音转写（输出 srt/txt/json）
... auto     D:\videos\demo.mp4                          # 自动决策

# 在线链接（通用，yt-dlp）
... fetch    "https://www.bilibili.com/video/BVxxxx"     # 只抓元数据
... subs     "https://.../BVxxxx"                        # 只下中文字幕
... download "https://.../BVxxxx"                        # 下载视频（--audio-only 只要音频）
... pipeline "https://.../BVxxxx" --model small          # 一键全链路（非B站推荐）

# 在线链接（B站专用，✅绕过 412 反爬，推荐）
... bili     "https://www.bilibili.com/video/BVxxxx" --model small
   # 内部：view API 拿元数据 → BBDown 多线程下载最低码率音频(m4a) → 本地 ffmpeg 转 mp3 → faster-whisper 转写
   # 无公开字幕时（B站 CC 字幕需登录 cookie，未登录多为空）自动走音频转写
```

常用选项：`--model`（tiny/base/small 默认/medium/large-v3）、`--language`（默认 zh）、`--outdir`（默认 ./outputs）、`--no-vad`。

## B站专用链路（bili 子命令，重要）

**为什么需要**：yt-dlp 抓 B站 实测频繁触发 `HTTP Error 412: Precondition Failed`（B站反爬），`pipeline` 会直接失败。官方 API 直连（无需 cookie、无需 yt-dlp）稳定可靠。

**链路步骤（脚本已封装，也可手动复刻）**：
1. **元数据**：`GET https://api.bilibili.com/x/web-interface/view?bvid=BVxxxx`（带 UA + Referer），取 `title / owner.name / pubdate / duration / stat / desc / tname`。
2. **字幕**：`/x/player/v2` 或 `/x/player/wbi/v2` 查 `subtitle.subtitles`——**未登录通常返回空**（B站 CC 字幕需 cookie），故 B站默认走音频转写路线。
3. **下载（交给开源 BBDown，本机已装）**：`bili` 子命令内部调用 `BBDown <BV号> --audio-only --audio-ascending --work-dir <输出目录>`。
   - BBDown 自带 WBI 签名 + 多线程 + 正确 CDN host，**不会被 412 拦**（yt-dlp 会），也**不踩 WorkBuddy 沙箱拦截删临时文件的坑**（它自己管理临时文件）。
   - `--audio-ascending` 自动选最低码率音轨（转写够用、文件最小）；完成后在 work-dir 产出 `<bvid>.m4a`。
   - 脚本再把 `.m4a` 用本地 ffmpeg 转成 `audio.mp3`（纯本地、秒级）。
4. **转写**：`transcribe` 子命令或脚本内 `transcribe_audio` → 输出 srt/txt/json。
5. **纠正成稿**：由 WorkBuddy 结合元数据整理（见执行要点 4）。

> 手动复刻时注意：BBDown 自包含版已放在 `C:\Users\lk\bin\BBDown\BBDown.exe`（含 .NET 运行时，无需另行安装）。
> **⚠ 速度关键（实测 2026-08-07）**：WorkBuddy 沙箱默认带 `HTTP_PROXY=127.0.0.1:10808`，会把 B站流量强制绕到海外代理节点，速度暴跌到 **~0.2 MB/s**（26MB 音频要 10 分钟）。**清空该代理 env 走直连后，B站下载实测 ~3–7 MB/s（26MB 仅 8 秒，提速 ~78 倍）**。脚本 `bili` 子命令已在 `bili_pipeline` 开头自动 `os.environ.pop` 掉 `HTTP_PROXY/HTTPS_PROXY`，无需手动处理。B站是国内域名，直连即最快路径。

## 本机环境事实（2026-08-07 实测，脚本已自动适配）

| 组件 | 状态 | 说明 |
|------|------|------|
| ffmpeg | ✅ | PATH 中（`C:\Users\lk\Documents\视频\`），脚本自动回退该目录 |
| ffprobe | ❌ 缺失 | 本机未装；转写前无需探测文件，不影响流程（如后续需要，用 ffmpeg 替代探测或直接转写） |
| yt-dlp | ✅ v2026.07.04 | hermes venv Scripts 中，非B站链接主力；**抓 B站会 412，勿用于 B站** |
| you-get | ✅ | 备用下载（yt-dlp 失效时） |
| BBDown | ✅ v1.6.3 | 开源 B站下载器，已装于 `C:\Users\lk\bin\BBDown\`（自包含版含 .NET 运行时，无需另行安装）；`bili` 子命令依赖此，自带 WBI 签名+多线程，彻底绕过 412 |
| faster-whisper | ✅ v1.2.1 | 只装在 hermes venv（见上），transcribe 必须用它跑 |
| 本地模型 | ✅ | **`D:\lk\.cache\faster-whisper\`** 已有 base + small（含 model.bin），脚本自动优先用本地缓存、不重复下载；medium/large-v3 未下载，用时会自动下载 |
| B站官方 API | ✅ | `api.bilibili.com` 直连可用（无需 cookie），`bili` 子命令依赖此 |
| PaddleOCR | ❌ 未装 | OCR 路线暂不可用 |
| Ollama/API | 不用配 | **纠正环节由 WorkBuddy 自己完成**（我就是大模型），无需额外 API |

## 执行要点（WorkBuddy 操作时注意）

1. **B站链接一律用 `bili` 子命令**：一条命令完成 元数据+音频下载+转写，绕过 yt-dlp 412。非B站链接才用 `pipeline`。
2. **transcribe 耗时（2026-08-07 更新）**：GPU 转写 53 分钟视频 small 模型约 **135 秒**（7 倍实时）；CPU 约 15–30 分钟（慢 5.3 倍）。**B站在线视频优先用现成工具 Video-Transcribe（自动 GPU）**；用技能脚本时确认能识别 GPU（脚本已支持 cuda 自动检测）。长任务用后台方式运行（`run_in_background`），完成后再汇报。
3. **首次用 medium/large-v3 会下载模型**（几个 GB），提示用户耐心等待；small/base 已本地就绪。
4. **纠正成稿（Step 5）由我执行**：读 outputs/{标题}/ 下的 元数据(info.json) + 原始转写.txt，按用户方案 Prompt 模板要求修正专名/删口语词/补标点/分段，输出 Markdown 最终文案.md。**必须做这一步，转写稿不能直接交付**。B站转写常见识别错误需修正：网门→网文、七点/起→起点、母点→起点、番浅/翻浅/提点→番茄、内头→内投、万定→万订、均定/军定→均订、两万军进→两万均订。
5. **后台任务查询兜底**：后台转写任务 ID 偶尔在 `TaskOutput` 查不到（会话原因），可改监控输出目录文件生成（srt/txt/json 子目录出现非空文件即完成），或检查 hermes python 进程是否在跑。
6. **评论区**：**现成工具 Video-Transcribe 已支持无登录抓评论（x/v2/reply 接口，前 20 条热评）和弹幕下载（BBDown --danmaku-only，xml+ass 存输出目录）**——B站在线视频直接用现成工具即可拿到。技能脚本的 `--write-comments` 仍需登录 cookie（`--cookies-from-browser edge`，需用户确认浏览器登录态）。
7. **下载转写优先纯音频**（体积小 90%）：`bili` 子命令用 BBDown `--audio-only --audio-ascending` 直接下最低码率音频，无需手动 `-x`。
8. **隐私**：本地工具不上传；元数据/评论落盘后注意管理。
9. **超长视频**：>1h 内存占用高，可 ffmpeg 分段转写后拼接。
10. **背景音乐干扰**：转写前做人声分离（Demucs/UVR5）可显著提升准确率。
11. **下载慢？先看是不是被代理拖慢（最高频坑）**：WorkBuddy 沙箱默认 `HTTP_PROXY=127.0.0.1:10808`，会把 B站流量硬拽到海外代理节点，速度从直连 ~3–7 MB/s 跌到 ~0.2 MB/s（差 ~37 倍）。`bili` 子命令已在 `bili_pipeline` 开头自动 `os.environ.pop` 掉 `HTTP_PROXY/HTTPS_PROXY`，走直连即可满速。用户若把代理工具从"全局模式"切到"自动路由"也**不够**——沙箱是靠 env 变量强制走代理的，与代理工具自身模式无关，必须清空 env 才生效。不要再自写单连接 ffmpeg 或并发分片下载（前者慢、后者会踩沙箱删文件坑，已废弃，统一用 BBDown）。

## 输出规范

- 本地视频：`{outdir}/srt`、`{outdir}/txt`、`{outdir}/json`，文件名 `{视频名}_{方法}_{语言}.{ext}`
- 在线链接：`{outdir}/{视频标题}/` 下：`最终文案.md`（我纠正后产出）、`原始转写.srt`、`原始转写.txt`、`info.json`（元数据原档）、`audio.mp3`（B站链路）
- 统一 UTF-8 无 BOM

## 扩展参考

- 完整方案文档（含 B站 API 细节、Prompt 模板、FAQ）：
  `C:\Users\lk\Documents\Codex\2026-08-07\wo\outputs\video-subtitle-extraction-guide.md`
