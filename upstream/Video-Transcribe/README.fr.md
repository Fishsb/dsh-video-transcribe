[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | **Français** | [한국어](README.ko.md)

# Video Transcribe

Extrayez du texte et des transcriptions depuis des vidéos YouTube et Bilibili. Prend en charge l'extraction de sous-titres CC (le plus rapide) et la transcription ASR avec correction post-LLM.

## Fonctionnalités

- **Support YouTube + Bilibili** : détection automatique de la plateforme à partir de l'URL
- **Stratégie sous-titres en priorité** : tente les sous-titres CC avant l'ASR (10 à 100 fois plus rapide)
- **Moteurs ASR multiples** : faster-whisper (multilingue), Qwen3-ASR (optimisé pour le chinois)
- **Correction post-LLM** : corrige les erreurs ASR, les termes techniques et les noms propres
- **Basculement Bilibili** : yt-dlp HTTP 412 -> basculement automatique vers yutto

## Prérequis

- Python 3.8+
- FFmpeg (doit être dans le PATH)

## Installation

```bash
# Dépendances principales
pip install yt-dlp yutto faster-whisper torch openai

# Optionnel : Qwen3-ASR (meilleur pour le chinois)
pip install qwen-asr

# Optionnel : Connexion Bilibili pour les sous-titres CC et 1080P+
pip install playwright && playwright install
```

## Utilisation

```bash
# Utilisation de base (détection automatique de la plateforme, sous-titres en priorité)
python video_transcribe.py "https://www.youtube.com/watch?v=..."

# Vidéo Bilibili
python video_transcribe.py "https://www.bilibili.com/video/BV..."

# Répertoire de sortie personnalisé
python video_transcribe.py "URL" --output-dir ./transcripts

# Avec un modèle ASR spécifique
python video_transcribe.py "URL" --asr-model whisper-small

# Avec Qwen3-ASR (meilleur pour le chinois)
python video_transcribe.py "URL" --asr-model qwen3-asr --qwen3-path /path/to/Qwen3-ASR-1.7B

# Sans correction LLM
python video_transcribe.py "URL" --no-llm-correction

# Avec un LLM personnalisé pour la correction
python video_transcribe.py "URL" --llm-model gpt-4o-mini --api-key YOUR_KEY
```

## Paramètres

| Paramètre | Par défaut | Description |
|-----------|------------|-------------|
| `--output-dir` | . | Répertoire de sortie |
| `--lang` | zh | Code de langue pour l'ASR |
| `--asr-model` | auto | Modèle ASR (auto/whisper-small/whisper-base/whisper-medium/whisper-large/qwen3-asr) |
| `--whisper-size` | small | Taille du modèle Whisper |
| `--qwen3-path` | | Chemin vers le modèle Qwen3-ASR local |
| `--no-llm-correction` | false | Ignorer la correction post-LLM |
| `--llm-model` | gpt-4o-mini | Modèle LLM pour la correction |
| `--api-key` | | Clé API (ou définir la variable d'environnement OPENAI_API_KEY) |
| `--base-url` | | URL de base pour une API compatible OpenAI |

## Fonctionnement

```
URL -> détection de la plateforme
  |-- YouTube -> yt-dlp
  +-- Bilibili -> yt-dlp (basculement : yutto en cas de HTTP 412)
       |-- Sous-titres CC disponibles ? -> extraction (le plus rapide)
       +-- Pas de sous-titres -> téléchargement audio -> ASR
            |-- Chinois + Qwen3 disponible -> Qwen3-ASR (recommandé)
            +-- Autre / pas de Qwen3 -> faster-whisper
                 +-- Correction LLM -> sortie Markdown
```

## Format de sortie

```markdown
# Titre de la vidéo

**Source**: URL
**Method**: CC Subtitle / Qwen3-ASR / faster-whisper/small
**Date**: 2025-01-01 12:00
**Corrected**: Yes/No
**Characters**: 12345

---

Texte complet de la transcription corrigée...
```

## Connexion Bilibili (pour les sous-titres CC et 1080P+)

Les sous-titres CC et l'audio haute qualité nécessitent souvent une connexion :

```bash
# Connexion via Playwright (ouvre le navigateur pour le scan du QR code)
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

# Transmettre les cookies à yt-dlp
yt-dlp --cookies-from-browser chrome "URL" ...
```

## Licence

MIT
