# 独立版实现与接口

工作台只依赖轻量 Web／媒体库，不导入 Torch。运行环境管理器通过独立 Python 进程执行安装、模型下载和推理。安装／下载使用独立执行队列，工程编辑可以继续；GPU 任务和环境变更使用互斥锁，防止执行过程中修改音色来源或环境。

## 运行环境 API

所有请求通过本机工作台发出。写请求拒绝跨站 Origin；工作台与推理服务只监听回环地址。推理端额外使用本地随机身份令牌。

| 接口 | 请求／返回 |
|---|---|
| `GET /api/runtime` | 配置、设备检查、环境安装信息、模型状态、进程归属、连接、日志与任务 |
| `PUT /api/runtime/config` | `model`、`device`、`source`、`models_dir`、`offline`、`port` |
| `POST /api/runtime/actions` | `{"action":"install / start / stop / restart / test"}`（传其中一个值） |
| `POST /api/runtime/models/actions` | `{"action":"download / validate / remove","key":"base-small / base / voicedesign"}` |

模型／环境动作返回现有任务对象。通过 `GET /api/tasks` 查询，`POST /api/tasks/{id}/pause` 请求停止。下载／安装任务状态落盘，服务重启将执行中任务标记 interrupted；重试后复用已完成资源，不声称自动续跑推理请求。

模型配置与模式切换只在无任务时允许；托管进程运行时先停止再变更配置。外部模型目录只读管理，不允许删除。推理异常不清除模型和配置。

## 语言模型

`GET/PUT /api/llm/config` 使用原有脱敏配置契约。`GET /api/llm/models?provider_id=...` 通过后端查询已保存供应商的 `/models`。`POST /api/llm/test` 接收 `provider_id`，可加 `structured:true` 测试 JSON 输出；返回任务对象。

兼容 OpenAI Chat Completions，支持流式响应。本地服务允许不填 Key；本机推理与 LLM 生命周期互相独立。失败时不给用户偷偷修改 JSON 模式或替换供应商。

## 模型和音色

- Base 0.6B／1.7B 执行克隆和配音，VoiceDesign 生成描述音色试听，再通过 Base 保存为克隆音色。
- 下载完整快照后检查 JSON、tokenizer、权重文件长度和分片路径。未完整校验不会进入加载。
- 模型目录、文件状态及运行时修订进入音频指纹；切换模型使相关缓存失效。
- 新 PT 保存为纯数据／张量结构，以 `weights_only=True` 读取，再验证字段，构造官方 prompt 对象。
- 旧 pickle 只通过单独的显式可信转换命令加载；原文件保留。
- 工作台缓存的配音按组恢复，前端预览和最终视频沿用同一时间线。

## 分支同步

main 维护本机独立能力，divide 维护 Gateway／Worker。通用编辑器修复按提交挑选同步。分支之间不共享私有配置与运行目录，不整体互相合并。
