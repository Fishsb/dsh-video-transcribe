**English** | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Français](README.fr.md) | [한국어](README.ko.md)

# Video Transcribe

Extract text/transcripts from YouTube and Bilibili videos. Supports CC subtitle extraction (fastest) and ASR transcription with LLM post-correction.

## Features

- **YouTube + Bilibili support**: auto-detect platform from URL
- **Subtitle-first strategy**: tries CC subtitles before ASR (10-100x faster)
- **Multiple ASR engines**: faster-whisper (multilingual), Qwen3-ASR (Chinese-optimized)
- **LLM post-correction**: fixes ASR errors, technical terms, proper nouns
- **Bilibili fallback**: yt-dlp HTTP 412 -> automatic yutto fallback

## Requirements

- Python 3.8+
- FFmpeg (must be in PATH)

## Installation

```bash
# Core dependencies
pip install yt-dlp yutto faster-whisper torch openai

# Optional: Qwen3-ASR (better for Chinese)
pip install qwen-asr

# Optional: Bilibili login for CC subtitles and 1080P+
pip install playwright && playwright install
```

## Usage

```bash
# Basic usage (auto-detect platform, try subtitles first)
python video_transcribe.py "https://www.youtube.com/watch?v=..."

# Bilibili video
python video_transcribe.py "https://www.bilibili.com/video/BV..."

# Specify output directory
python video_transcribe.py "URL" --output-dir ./transcripts

# With specific ASR model
python video_transcribe.py "URL" --asr-model whisper-small

# With Qwen3-ASR (better for Chinese)
python video_transcribe.py "URL" --asr-model qwen3-asr --qwen3-path /path/to/Qwen3-ASR-1.7B

# Skip LLM correction
python video_transcribe.py "URL" --no-llm-correction

# With custom LLM for correction
python video_transcribe.py "URL" --llm-model gpt-4o-mini --api-key YOUR_KEY
```

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--output-dir` | . | Output directory |
| `--lang` | zh | Language code for ASR |
| `--asr-model` | auto | ASR model (auto/whisper-small/whisper-base/whisper-medium/whisper-large/qwen3-asr) |
| `--whisper-size` | small | Whisper model size |
| `--qwen3-path` | | Path to local Qwen3-ASR model |
| `--no-llm-correction` | false | Skip LLM post-correction |
| `--llm-model` | gpt-4o-mini | LLM model for correction |
| `--api-key` | | API key (or set OPENAI_API_KEY env var) |
| `--base-url` | | Base URL for OpenAI-compatible API |

## How It Works

```
URL -> detect platform
  |-- YouTube -> yt-dlp
  +-- Bilibili -> yt-dlp (fallback: yutto on HTTP 412)
       |-- CC subtitles available? -> extract (fastest)
       +-- No subtitles -> download audio -> ASR
            |-- Chinese + Qwen3 available -> Qwen3-ASR (recommended)
            +-- Other / no Qwen3 -> faster-whisper
                 +-- LLM correction -> output markdown
```

## Output Format

```markdown
# Video Title

**Source**: URL
**Method**: CC Subtitle / Qwen3-ASR / faster-whisper/small
**Date**: 2025-01-01 12:00
**Corrected**: Yes/No
**Characters**: 12345

---

Full corrected transcript text here...
```

## Bilibili Login (for CC subtitles and 1080P+)

CC subtitles and high-quality audio often require login:

```bash
# Login via Playwright (opens browser for QR code scan)
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

# Pass cookies to yt-dlp
yt-dlp --cookies-from-browser chrome "URL" ...
```

## License

MIT
