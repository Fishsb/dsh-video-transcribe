# 第三方内容声明

本仓库自有部分以 [MIT License](LICENSE) 授权。以下内容权利归各自所有者，
收录仅供演示与可复现性验证。

## 1. `upstream/Video-Transcribe/`

上游基座 [qkirara/Video-Transcribe](https://github.com/qkirara/Video-Transcribe)，
按 **MIT License** 原样收录（纯副本，用于对账与追溯，不在其中改动）。
许可证全文随附于 `upstream/Video-Transcribe/LICENSE`。

## 2. `samples/`

实证产物目录，内含 B 站视频《我用一个项目，讲清楚 Agent 的 4 种 Memory》
（[BV1JMbp6MEo5](https://www.bilibili.com/video/BV1JMbp6MEo5)，UP主 CodeCheers）的：

- `audio.m4a` — 原视频音轨（取自公开接口的最低码率流）
- `info.json` — 元数据与评论区内容（含其他用户的评论原文）
- `原始转写.txt` / `原始转写_分段.md` — 视频语音的转写文本
- `帧OCR原始结果*.md` — 画面文字识别结果
- `最终文案.md` / `最终文案_修订版.md` — 本项目产出的成品文案

这些内容用于证明链路端到端可跑通，**不代表本仓库对其享有权利**；
版权属原视频作者与评论发布者。若权利人提出异议，将移除相应文件。

## 3. 项目纪律说明

按本项目《三层目录职责》纪律：

- `upstream/` 为上游纯副本，**禁止手改**（改了就对不上上游）
- `samples/` 为实证产物，**只增不改**（实证就是实证）

因此上述文件按原样保留，不在此处修改其内容。
