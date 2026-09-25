[English](README.md) | [简体中文](README.zh-CN.md) | **日本語** | [Français](README.fr.md) | [한국어](README.ko.md)

# Video Transcribe

YouTube および Bilibili 動画からテキスト/文字起こしを抽出します。CC 字幕の抽出（最速）と、LLM による事後補正付き ASR 文字起こしに対応しています。

## 機能

- **YouTube + Bilibili 対応**：URL からプラットフォームを自動検出
- **字幕優先戦略**：ASR より先に CC 字幕を試行（10〜100 倍高速）
- **複数 ASR エンジン**：faster-whisper（多言語対応）、Qwen3-ASR（中国語最適化）
- **LLM 事後補正**：ASR の誤認識、専門用語、固有名詞を修正
- **Bilibili フォールバック**：yt-dlp で HTTP 412 発生時に yutto へ自動切り替え

## 動作環境

- Python 3.8+
- FFmpeg（PATH に含まれていること）

## インストール

```bash
# コア依存パッケージ
pip install yt-dlp yutto faster-whisper torch openai

# オプション：Qwen3-ASR（中国語により適しています）
pip install qwen-asr

# オプション：Bilibili ログイン（CC 字幕と 1080P+ 音声に必要）
pip install playwright && playwright install
```

## 使い方

```bash
# 基本的な使い方（プラットフォーム自動検出、字幕優先）
python video_transcribe.py "https://www.youtube.com/watch?v=..."

# Bilibili 動画
python video_transcribe.py "https://www.bilibili.com/video/BV..."

# 出力ディレクトリを指定
python video_transcribe.py "URL" --output-dir ./transcripts

# 特定の ASR モデルを指定
python video_transcribe.py "URL" --asr-model whisper-small

# Qwen3-ASR を使用（中国語により適しています）
python video_transcribe.py "URL" --asr-model qwen3-asr --qwen3-path /path/to/Qwen3-ASR-1.7B

# LLM 補正をスキップ
python video_transcribe.py "URL" --no-llm-correction

# カスタム LLM で補正
python video_transcribe.py "URL" --llm-model gpt-4o-mini --api-key YOUR_KEY
```

## パラメータ

| パラメータ | デフォルト | 説明 |
|-----------|-----------|------|
| `--output-dir` | . | 出力ディレクトリ |
| `--lang` | zh | ASR の言語コード |
| `--asr-model` | auto | ASR モデル（auto/whisper-small/whisper-base/whisper-medium/whisper-large/qwen3-asr） |
| `--whisper-size` | small | Whisper モデルのサイズ |
| `--qwen3-path` | | ローカル Qwen3-ASR モデルのパス |
| `--no-llm-correction` | false | LLM 事後補正をスキップ |
| `--llm-model` | gpt-4o-mini | 補正に使用する LLM モデル |
| `--api-key` | | API キー（または OPENAI_API_KEY 環境変数を設定） |
| `--base-url` | | OpenAI 互換 API のベース URL |

## 処理の流れ

```
URL -> プラットフォームを検出
  |-- YouTube -> yt-dlp
  +-- Bilibili -> yt-dlp（HTTP 412 時は yutto にフォールバック）
       |-- CC 字幕あり？ -> 字幕を抽出（最速）
       +-- 字幕なし -> 音声をダウンロード -> ASR
            |-- 中国語 + Qwen3 利用可能 -> Qwen3-ASR（推奨）
            +-- その他 / Qwen3 なし -> faster-whisper
                 +-- LLM 補正 -> Markdown 出力
```

## 出力形式

```markdown
# 動画タイトル

**Source**: URL
**Method**: CC Subtitle / Qwen3-ASR / faster-whisper/small
**Date**: 2025-01-01 12:00
**Corrected**: Yes/No
**Characters**: 12345

---

補正済みの文字起こしテキスト...
```

## Bilibili ログイン（CC 字幕と 1080P+ 用）

CC 字幕と高音質音声にはログインが必要な場合があります：

```bash
# Playwright でログイン（ブラウザが開き QR コードをスキャン）
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

# Cookie を yt-dlp に渡す
yt-dlp --cookies-from-browser chrome "URL" ...
```

## ライセンス

MIT
