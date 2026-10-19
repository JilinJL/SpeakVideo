# 独立部署与恢复

main 将工作台和本机推理放在同一台机器上，通过回环 HTTP 通信。工作台环境保持轻量；Torch/Qwen 位于 `runtime/venv/`。默认仅监听本机，不提供公网多用户服务。远端 Worker 部署使用 divide。

## 安装

运行 README 中对应平台的安装入口。`install.sh --no-start` 或 `install.ps1 -NoStart` 仅准备工作台。uv 缺失时安装到项目 `.uv/`，Python 使用 uv 管理的 3.11。不会改动全局 pip 镜像、已有 Python 环境或其他进程。

FFmpeg 缺失时，工作台显示系统安装命令：macOS `brew install ffmpeg`，Windows `winget install --id Gyan.FFmpeg -e`，Ubuntu/Debian `sudo apt-get update && sudo apt-get install -y ffmpeg`。其他 Linux 发行版使用自己的包管理器。安装后重新打开终端并检测。

本机推理按 MPS、CUDA 11.8、CUDA 12.8 的固定依赖配置安装。GPU 能力检查不代表所有显卡已实测；真实验收状态见 acceptance 文档。

## 无桌面部署

运行目录为当前目录，也可通过 `SPEAKVIDEO_ROOT` 指定独立数据目录。源码与模型不必同一目录；运行时通过环境传递数据根目录。

```bash
uv run speakvideo doctor
uv run speakvideo runtime status
uv run speakvideo runtime config --model base-small --device auto --source huggingface
uv run speakvideo runtime action install
uv run speakvideo runtime action download --key base-small
uv run speakvideo runtime action validate --key base-small
uv run speakvideo runtime action start
uv run speakvideo serve
# 停止推理
uv run speakvideo runtime action stop
```

工作台和推理均保持回环监听。远程浏览器使用 SSH 端口转发访问工作台，例如 `ssh -L 8765:127.0.0.1:8765 user@your-host`，再打开本机 8765。无需将推理端口公开。

## 离线模型

在「本机模型」设置模型**父目录**，其结构应为：

```text
models/
  Qwen3-TTS-12Hz-0.6B-Base/
    config.json
    model.safetensors
    ... tokenizer 配置 ...
    speech_tokenizer/
      config.json
      model.safetensors
      ...
```

支持权重分片索引。导入后点重新校验，再启用离线模式。模型下载器仅在用户提交下载时联网；推理进程使用完整本地模型并设置离线标志。离线机器还需要提前准备匹配平台的 Python 依赖环境；复制其他平台虚拟环境不可用。

切换来源不自动覆盖一个完整模型。下载失败重试会复用下载缓存；界面展示下载进度、文件阶段和校验错误。外部共享模型目录禁止从工作台删除。

## 旧版迁移

1. 原工作目录继续使用 divide，不覆盖其 `.cache/`、配置或工程。
2. main 使用独立克隆／工作目录，先完成本机初始化。
3. 停止工程任务后，将所需工程复制到 main 的 `projects/`；先保留旧副本。
4. 旧 `config.local.toml` 和 `.cache/tts-mode.json` 中的 Gateway 模式会提示切回 divide。移走旧配置前先备份；不要复制 Gateway Token 到独立版。
5. LLM 配置可选择性复制 `.cache/llm.local.json`，全局提示词为 `script-prompts.json`。
6. 旧音色优先用原声重新克隆。确实信任来源的旧 PT，可在推理环境显式转换：

```bash
# macOS / Linux；Windows 使用 runtime\venv\Scripts\python.exe
runtime/venv/bin/python -m speakvideo.inference.prompts old.pt converted.pt --trust-legacy-pickle
```

旧 pickle 转换可能执行其中的代码，只用于自己确认可信的文件。新输出不能覆盖原件，转换后可通过音色库导入。日常推理不进行不受限反序列化。已有工程的模型／模式指纹变化会要求重新确认或生成声音；不会把旧音频误标为新模型结果。

## 故障恢复

| 现象 | 操作 |
|---|---|
| 未检测到 GPU | 检查原生架构、驱动和设备编号；不会自动退回 CPU |
| 依赖安装失败 | 查看安装日志，修复网络后重新安装；只影响专用环境 |
| 模型下载失败 | 重试续传，或显式改选下载来源；完整模型仍可复用 |
| 模型校验失败 | 重新下载缺失文件，或换用完整外部模型目录 |
| 服务端口占用 | 改为其他本机端口；不会终止占用进程 |
| 加载失败／显存不足 | 停止服务，改选 0.6B；关闭同设备其他占用后再试 |
| 描述音色失败 | 下载完整 VoiceDesign，并检查其更高的资源需求 |
| 没有真实试音 | 先在音色库上传原声、填写台词并创建音色 |
| 页面重载后任务中断 | 检查已安装环境和模型，重新提交；配音保留有效缓存 |
| 本地 LLM 无法输出 JSON | 关闭不兼容的 JSON Mode 后测试；检查模型能力和输出长度 |

服务管理使用 PID、创建时间、启动实例标识确认进程归属；不会结束其他 Qwen 服务。工作台重启可重新识别自己启动的推理进程。关闭工作台不会删除推理进程或模型，退出前可在页面停止推理。

## 备份、更新、卸载

备份自己的 `projects/`、`voices/`、`voice-previews/`、`.cache/`、`runtime/config.json`、本地 TOML 和全局提示词。权重可以重新下载，但离线使用时也应备份。停止任务和推理，更新源码后重跑安装脚本；重跑安装不会主动删除模型或作品。

卸载：先执行 runtime stop，备份所需数据，再删除项目目录。自定义数据目录／模型目录由用户自行管理；项目不会卸载全局 Python、uv、FFmpeg 或 GPU 驱动。

## Base 档位与音色

0.6B 和 1.7B 的音色向量维度不同。切换模型后，保留原声并重新克隆到新的角色／情绪，原音色继续保留供原档位使用。服务会拒绝不兼容音色并提示恢复方法。

下载后的文件记录 SHA256；页面「重新校验」检查已有记录，可检测同长度文件内容损坏。无校验记录的外部模型仅做结构、长度和分片检查；可信来源由导入者确认。Hugging Face 哈希损坏时可点修复强制重新下载，ModelScope 可先移除损坏模型再重试。
