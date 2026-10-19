# SpeakVideo · divide

独立算力版。工作台负责剧本、音色管理和 FFmpeg 视频创作；GPU Worker 在另一台机器完成 Qwen 推理，Gateway 负责认证与 WSS 转发。

需要一台机器独立启动、内置模型安装下载管理时，请使用 [main](https://github.com/JilinJL/SpeakVideo/tree/main)。两个分支保留共同的创作功能，通用修复按提交挑选同步，不整体互相合并。

## 启动工作台

准备 Python 3.11、uv、FFmpeg/ffprobe，执行：

```bash
uv sync --python 3.11
# 仅首次复制，已有配置不要覆盖
cp config.example.toml config.local.toml
uv run speakvideo doctor
uv run speakvideo serve
```

在本地配置选择 `gateway`，填写自己的 HTTPS 地址、Worker ID；通过环境变量 `SPEAKVIDEO_TTS_API_KEY` 设置 Token。LLM 在侧栏配置兼容 OpenAI 的服务，可连接用户部署的本地语言模型。未配置 LLM 时仍可手工编辑剧本。

打开 http://127.0.0.1:8765 。Windows Worker、Gateway 和常驻服务分别参考 [部署说明](docs/deployment.md)、[接口契约](docs/gateway-contract.md)、[Windows 说明](docs/windows-integration.md)。示例中的域名、地址与机器路径必须替换为自己的部署值。

## 创作与数据

写剧本 → 绑定音色 → 生成配音 → 素材与片段剪辑 → 导出视频。保留按组重试、缓存恢复、BGM、字幕、封面、贴图发言泛光和完整交付功能。P1 橙色、P2 蓝色，字幕白字配角色颜色描边。

`projects/`、`voices/`、`voice-previews/`、`.cache/`、模型、配置和密钥均为私人数据，不进入版本控制。升级前完整备份。此分支的旧 PT 及远端主资产协议与 main 的安全音色格式不同，迁移时保留原声并重新克隆。

不包含任何已部署机器的凭据和私人音色。通用部署说明不代表你的远端服务已运行；本轮分支整理未改动现有 Windows、Gateway 或线上服务。

## 开发与许可

```bash
uv run pytest -q
uv run ruff check speakvideo tests worker
cd gateway
go test ./...
```

分支整理前保留的现有功能基线为 191 项 Python 测试通过。后续分布式硬件验收应使用自己的测试工程，并把记录保存在运行目录。

源码采用 [Apache-2.0](LICENSE)，移植代码和依赖见 [第三方说明](THIRD_PARTY_NOTICES.md)。参考声音与素材需要合法使用授权。
