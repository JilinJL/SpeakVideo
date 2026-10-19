# divide 通用部署

适用于工作台与 GPU 算力不在同一台机器的情况。本机独立部署请使用 main。

## 部署前准备

工作台安装 Python 3.11、uv、FFmpeg/ffprobe。GPU 机器单独准备 Qwen 服务、匹配的 Torch/CUDA 与完整模型；本分支不托管其安装。网关准备 Linux、Go、反向代理与自己的 TLS 域名。

为客户端与 Worker 分别生成强随机 Token，通过私有配置或环境保存，设置相同的 Worker ID。示例中的地址仅是占位值。

## 网关

在 gateway/ 执行 go test ./... 与 go build。使用 deploy/ 中的 systemd 和 nginx 模板，调整账户、路径、监听地址与域名。网关只监听回环地址，由 HTTPS 反向代理对外提供 /v1/ 和 WSS，开放 WebSocket Upgrade。

可选择手动 SSH 部署，或设置 SPEAKVIDEO_SSH_WRAPPER 为自己的 SSH 包装脚本绝对路径，再使用 deploy/aliyun.py。包装脚本接收一个远端命令字符串；脚本不会提供任何服务器凭据。deploy/update_gateway.py 仅适合与其默认目录布局一致的既有安装，使用前审阅常量并准备回滚。不要直接执行未配置的部署脚本。

## GPU Worker

核对实际 Qwen 版本和 API，再将 worker/ 适配文件部署到 Qwen 项目的 scripts/speakvideo/。worker/run.ps1 从该目录层级推导 Qwen 根目录；使用安装计划任务脚本前核对路径与现有任务归属。具体配置、协议与验证命令见 windows-integration.md。

Worker 只向网关建立出站 WSS；Qwen 仅绑定 127.0.0.1:8001，不将推理端口暴露公网。先前已运行的 Qwen 项目可能具有不同的新接口，不要用整个适配目录覆盖其完整源码。

## 工作台

首次复制 config.example.toml 为 config.local.toml，将 tts.mode 改为 gateway，填写自己的 HTTPS base_url 和 worker_id；通过 SPEAKVIDEO_TTS_API_KEY 或私有配置提供客户端 Token。运行 uv sync --python 3.11、uv run speakvideo doctor、uv run speakvideo serve，浏览器打开 127.0.0.1:8765。

LLM 可在页面选择兼容接口；使用自己的 Key，或连接无需 Key 的本地服务。Mac 常驻入口可参考 deploy/mac_workbench.py，先核对项目路径与已占用端口。

## 验收与恢复

先检查网关认证和 Worker 注册，再验证能力文档、音色清单与 SHA256、真实短句配音、断线重连、重试及 MP4。协议 smoke 的假推理不替代真实模型验收。

更新前备份源码、服务配置、Token、项目、音色和原声。只在无任务时停止自己的进程，保留上一个网关二进制和 nginx 配置；确认新服务就绪后再继续创作。私人运行记录写入被忽略的目录。
