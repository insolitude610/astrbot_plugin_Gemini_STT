# Gemini STT Bridge for AstrBot (社区魔改版)

> 基于原版 [Weather-719/astrbot_plugin_Gemini_STT](https://github.com/Weather-719/astrbot_plugin_Gemini_STT) v2.3.6 二次开发

# 注意现在只有使用never模式才可以正常使用！

一个面向 AstrBot 的语音桥接插件：  
**将语音消息自动转写为文本，并交由框架继续按正常会话流程回复。**

> 插件定位：只做"语音识别 + 转发"，不抢框架人格回复逻辑。  
> 目标体验：**语音输入 ≈ 自动帮你打字输入**。

---

## 🔧 原版核心特性

- 🎤 支持语音输入自动转写（`silk / amr / wav / mp3`）
- 🔁 转写结果自动转发给 AstrBot 框架（`request_llm`）
- 🧠 可与框架现有人格、记忆系统协作
- 🧩 支持群聊开关与群白名单
- ⚙️ 支持失败策略可配置（放行/拦截/提示）
- 📝 支持输出模式：
  - `simple`：仅原话转写
  - `rich`：原话 + 语言 + 语气 + 环境音 + 大意
- 🧹 支持模型名自动清洗（兼容 `[满血D]xxx` 等模型ID）
- 🛡️ 可选附加语音来源标记与说话人元信息（谁说的）

---

## 🚀 魔改版新增功能

### 1. API 认证方式可选
新增 `api_key_header` 配置项，支持四种认证方式：
- `bearer`：`Authorization: Bearer xxx`
- `x-api-key`：`x-api-key: xxx`
- `api-key`：`api-key: xxx`
- `query`：URL 查询参数 `?key=xxx`（Google Gemini 原生格式，适配 aihubmix 等中转站的 `/gemini/` 端点）

### 2. Whisper STT 引擎支持
新增 `stt_provider` 配置项，可选择 STT 引擎：
- `gemini`：Gemini 原生 API（`/v1beta/models/...:generateContent`）
- `whisper`：OpenAI Whisper 兼容 API（`/v1/audio/transcriptions`，multipart form-data）

### 3. 独立模型 ID 配置
将原版单一的 `model` 配置拆分为：
- `gemini_model`：Gemini 引擎专用模型 ID
- `whisper_model`：Whisper 引擎专用模型 ID
切换引擎时无需手动改模型名。

### 4. 跳过本地文件等待
新增 `bypass_local_file` 开关。当 NapCat 与 AstrBot 不在同一容器/机器、本地文件路径不可达时，开启后直接走 `get_record` API 的 base64 兜底，免除 10 秒等待。

### 5. 标点符号修复
新增 `enable_punctuation` 开关。开启后 STT 转写结果会再调一次 AstrBot 的提供商为其添加逗号、句号等标点符号（Whisper 等引擎转写结果通常无标点）。可通过 `punctuation_provider_id` 指定提供商，留空则自动使用默认聊天提供商。

### 6. Rich 模式提示词修正
将原版 rich 模式提示词中 "以下6项" 修正为 "以下5项"，与实际的5个输出维度匹配。

---

## 🚀 工作流程

1. 插件高优先级接收消息；
2. 非语音消息：直接放行，不干预；
3. 语音消息：识别并转写；
4. 按配置生成转发内容（simple/rich）；
5. （可选）标点修复：调用 AstrBot LLM 添加标点；
6. 调用框架 `request_llm` 转发；
7. 框架继续标准处理链（人格、记忆、后处理等）。

---

## 📦 安装依赖

- Python 3.10+
- `aiohttp`
- `pilk`（可选，处理 silk 时建议安装）
- `ffmpeg`（需自行安装并加入环境变量）

---

## ⚙️ 关键配置说明

### 新增配置项

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `api_key_header` | API Key 传递方式（bearer/x-api-key/api-key/query） | `bearer` |
| `stt_provider` | STT 引擎（gemini/whisper） | `gemini` |
| `gemini_model` | Gemini 引擎模型 ID | `gemini-2.0-flash` |
| `whisper_model` | Whisper 引擎模型 ID | `whisper-1` |
| `bypass_local_file` | 跳过本地文件等待，直达 base64 兜底 | `false` |
| `enable_punctuation` | 启用标点符号修复 | `false` |
| `punctuation_provider_id` | 标点修复用提供商 ID（留空=自动使用默认） | 空 |

### 配置切换示例

**Gemini 原生模式（aihubmix）：**
```
api_url: https://aihubmix.com/gemini
stt_provider: gemini
api_key_header: query
```

**Whisper 模式（aihubmix）：**
```
api_url: https://aihubmix.com/v1
stt_provider: whisper
api_key_header: bearer
```

---

## ⚠️ 已知问题

- NapCat 与 AstrBot 隔离部署时本地文件路径不可达（解决：开启 `bypass_local_file`）
- 复杂插件链路下可能出现双链路处理

---

## 🙏 致谢

- 原版作者：[Weather-719](https://github.com/Weather-719)
- 原版仓库：https://github.com/Weather-719/astrbot_plugin_Gemini_STT
