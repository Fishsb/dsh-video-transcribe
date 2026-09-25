[English](README.md) | **简体中文** | [日本語](README.ja.md) | [Français](README.fr.md) | [한국어](README.ko.md)

# Video Transcribe

从 YouTube 和 Bilibili 视频中提取文本/字幕。支持 CC 字幕提取（最快）以及 ASR 语音识别转写，并提供 LLM 后纠错功能。

## 功能特性

- **YouTube + Bilibili 双平台支持**：自动从 URL 识别平台
- **字幕优先策略**：优先尝试 CC 字幕，再走 ASR 语音识别（快 10-100 倍）
- **多种 ASR 引擎**：faster-whisper（多语言）、Qwen3-ASR（中文优化）
- **LLM 后纠错（可选）**：修复 ASR 识别错误、专业术语、专有名词；**默认由 AI 助手（如 WorkBuddy）读取转写稿+元数据完成纠错**，脚本内置纠错默认关闭（`--llm-correction` 启用）
- **GPU 自动加速**：检测到 CUDA 显卡时自动用 `cuda + float16`（实测比 CPU 快 5.3 倍），并按 GPU/CPU 自动选模型档位
- **Bilibili 下载走 BBDown**：B站音频优先用开源下载器 BBDown（自带 WBI 签名 + 多线程），绕过 yt-dlp 的 HTTP 412 反爬，实测 26MB 音频 4.7 秒（直连，不走代理）
- **自动绕过代理**：启动时清除 HTTP_PROXY/HTTPS_PROXY 环境变量（WorkBuddy 沙箱会注入代理，曾导致下载慢 78~130 倍）
- **Bilibili 评论获取**：官方 `x/v2/reply` + `x/v2/reply/reply` 双接口直连（无需登录）——抓取热评（匿名限流约 3 条/视频）+ **每条热评的完整楼中楼回复讨论**（回复接口不限流，14/14 实测全量），输出含对话串（`↳` 缩进）。**全量评论需登录态**：设置环境变量 `BILIBILI_COOKIE="SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx"` 或 `BILIBILI_COOKIES_FILE=路径/cookies.txt`（Netscape 格式，yt-dlp 可导出），登录后自动翻页拿全部主评论
- **Bilibili 弹幕下载**：BBDown `--danmaku-only` 附带下载弹幕（`.xml` 标准格式 + `.ass`，存至输出目录），无需登录
- **时间戳保留（segments.json）**：ASR 转写同时落盘 `*_segments.json`（每段 start/end/text），供下游「按文案时间轴对齐抽帧」（如教学视频画面识图）精确定位——不再丢时间线
- **Bilibili 降级方案**：yt-dlp 元数据失败时自动切换到 B站官方 view API

## 环境要求

- Python 3.8+（本机用 hermes venv：`C:\Users\lk\AppData\Local\hermes\hermes-agent\venv`）
- FFmpeg（需加入 PATH，本机位于 `C:\Users\lk\Documents\视频\ffmpeg.exe`）
- BBDown（B站下载，已装于 `C:\Users\lk\bin\BBDown\BBDown.exe`，从 https://github.com/nilaoda/BBDown/releases 获取自包含版）

## 安装

```bash
# 核心依赖（hermes venv 已全部装好，一般无需重装）
pip install yt-dlp faster-whisper torch openai

# 可选：Bilibili 登录（用于获取 CC 字幕和 1080P+ 音频）
pip install playwright && playwright install
```

## 使用方法

```bash
# 基本用法（自动识别平台，优先尝试字幕）
python video_transcribe.py "https://www.youtube.com/watch?v=..."

# Bilibili 视频
python video_transcribe.py "https://www.bilibili.com/video/BV..."

# 指定输出目录
python video_transcribe.py "URL" --output-dir ./transcripts

# 指定 ASR 模型
python video_transcribe.py "URL" --whisper-size small

# 跳过 LLM 纠错（默认即跳过；转写稿交给 AI 助手/WorkBuddy 做最终纠错成稿）
python video_transcribe.py "URL"

# 启用脚本内置 LLM 纠错（需 --api-key 或环境变量）
python video_transcribe.py "URL" --llm-correction

# 使用自定义 LLM 进行纠错
python video_transcribe.py "URL" --llm-model gpt-4o-mini --api-key YOUR_KEY
```

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--output-dir` | D:\lk\output | 输出目录 |
| `--lang` | zh | ASR 识别语言代码 |
| `--whisper-size` | auto | Whisper 模型大小（auto/base/small/medium/large；auto 时 GPU 用 small、CPU 用 base） |
| `--llm-correction` | 关 | 启用脚本内置 LLM 后纠错（默认关闭，纠错由 AI 助手完成） |
| `--llm-model` | gpt-4o-mini | 用于纠错的 LLM 模型 |
| `--api-key` | | API 密钥（也可设置 OPENAI_API_KEY 环境变量） |
| `--base-url` | | OpenAI 兼容 API 的基础地址（也可设置 OPENAI_BASE_URL） |
| `--clean` / `--clean-all` | | 清理旧输出（保留最近 3 次 / 全清） |

> 注：LLM 纠错对接任何 OpenAI 兼容端点。例如用 DeepSeek：
> `python video_transcribe.py URL --llm-model deepseek-chat --base-url https://api.deepseek.com --api-key sk-xxx`

## 工作流程

```
URL -> 识别平台
  |-- YouTube -> yt-dlp
  +-- Bilibili -> BBDown（yt-dlp 412 时兜底）
       |-- 有 CC 字幕？ -> 提取字幕（最快）
       +-- 无字幕 -> 下载音频 -> ASR 语音识别（GPU 自动加速）
            +-- （默认）交给 AI 助手纠错成稿
            +-- （可选）脚本内置 LLM 纠错 -> 输出 Markdown
```

## 输出格式

```markdown
# 视频标题

**Source**: URL
**Method**: CC Subtitle / Qwen3-ASR / faster-whisper/small
**Date**: 2025-01-01 12:00
**Corrected**: Yes/No
**Characters**: 12345

---

完整的纠错后转写文本...
```

## Bilibili 登录（用于获取 CC 字幕和 1080P+）

CC 字幕和高质量音频通常需要登录：

```bash
# 通过 Playwright 登录（打开浏览器扫码）
python -c "
import asyncio
from playwright.async_api import async_playwright

async def login():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto('https://passport.bilibili.com/login')
        print('Scan QR code or log in...')
        await page.wait_for_url('https://www.bilibili.com/**', timeout=120000)
        cookies = await page.context.cookies()
        for c in cookies:
            if c['name'] in ('SESSDATA', 'bili_jct', 'DedeUserID'):
                print(f\"{c['name']}={c['value']}\")
        await browser.close()

asyncio.run(login())
"

# 将 Cookie 传递给 yt-dlp
yt-dlp --cookies-from-browser chrome "URL" ...
```

## 许可证

MIT
