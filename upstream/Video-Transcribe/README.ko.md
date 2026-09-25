[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Français](README.fr.md) | **한국어**

# Video Transcribe

YouTube 및 Bilibili 영상에서 텍스트/자막을 추출합니다. CC 자막 추출(가장 빠름) 및 LLM 사후 교정이 포함된 ASR 전사를 지원합니다.

## 기능

- **YouTube + Bilibili 지원**: URL에서 플랫폼을 자동 감지합니다
- **자막 우선 전략**: ASR 전에 CC 자막을 먼저 시도합니다(10~100배 더 빠름)
- **다중 ASR 엔진**: faster-whisper(다국어 지원), Qwen3-ASR(중국어 최적화)
- **LLM 사후 교정**: ASR 오류, 전문 용어, 고유명사를 수정합니다
- **Bilibili 폴백**: yt-dlp HTTP 412 발생 시 yutto로 자동 전환합니다

## 요구 사항

- Python 3.8+
- FFmpeg(PATH에 포함되어 있어야 함)

## 설치

```bash
# 핵심 종속성
pip install yt-dlp yutto faster-whisper torch openai

# 선택사항: Qwen3-ASR(중국어에 더 적합)
pip install qwen-asr

# 선택사항: Bilibili 로그인(CC 자막 및 1080P+ 오디오용)
pip install playwright && playwright install
```

## 사용법

```bash
# 기본 사용법(플랫폼 자동 감지, 자막 우선 시도)
python video_transcribe.py "https://www.youtube.com/watch?v=..."

# Bilibili 영상
python video_transcribe.py "https://www.bilibili.com/video/BV..."

# 출력 디렉토리 지정
python video_transcribe.py "URL" --output-dir ./transcripts

# 특정 ASR 모델 지정
python video_transcribe.py "URL" --asr-model whisper-small

# Qwen3-ASR 사용(중국어에 더 적합)
python video_transcribe.py "URL" --asr-model qwen3-asr --qwen3-path /path/to/Qwen3-ASR-1.7B

# LLM 교정 건너뛰기
python video_transcribe.py "URL" --no-llm-correction

# 사용자 지정 LLM으로 교정
python video_transcribe.py "URL" --llm-model gpt-4o-mini --api-key YOUR_KEY
```

## 파라미터

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| `--output-dir` | . | 출력 디렉토리 |
| `--lang` | zh | ASR 언어 코드 |
| `--asr-model` | auto | ASR 모델(auto/whisper-small/whisper-base/whisper-medium/whisper-large/qwen3-asr) |
| `--whisper-size` | small | Whisper 모델 크기 |
| `--qwen3-path` | | 로컬 Qwen3-ASR 모델 경로 |
| `--no-llm-correction` | false | LLM 사후 교정 건너뛰기 |
| `--llm-model` | gpt-4o-mini | 교정에 사용할 LLM 모델 |
| `--api-key` | | API 키(또는 OPENAI_API_KEY 환경변수 설정) |
| `--base-url` | | OpenAI 호환 API의 기본 URL |

## 작동 방식

```
URL -> 플랫폼 감지
  |-- YouTube -> yt-dlp
  +-- Bilibili -> yt-dlp(HTTP 412 시 yutto로 폴백)
       |-- CC 자막 있음? -> 자막 추출(가장 빠름)
       +-- 자막 없음 -> 오디오 다운로드 -> ASR
            |-- 중국어 + Qwen3 사용 가능 -> Qwen3-ASR(권장)
            +-- 기타 / Qwen3 없음 -> faster-whisper
                 +-- LLM 교정 -> Markdown 출력
```

## 출력 형식

```markdown
# 영상 제목

**Source**: URL
**Method**: CC Subtitle / Qwen3-ASR / faster-whisper/small
**Date**: 2025-01-01 12:00
**Corrected**: Yes/No
**Characters**: 12345

---

교정 완료된 전체 전사 텍스트...
```

## Bilibili 로그인(CC 자막 및 1080P+용)

CC 자막 및 고음질 오디오는 로그인이 필요한 경우가 많습니다:

```bash
# Playwright를 통한 로그인(브라우저가 열리고 QR 코드를 스캔합니다)
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

# 쿠키를 yt-dlp에 전달
yt-dlp --cookies-from-browser chrome "URL" ...
```

## 라이선스

MIT
