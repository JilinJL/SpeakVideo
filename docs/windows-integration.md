# Windows 侧对接内容（Windows Integration）

本文只描述 **Windows 侧需要实现 / 已实现 / 待升级** 的对接内容。Mac 侧（LLM、ffmpeg 混音、页面、交付打包）见 [实施状态](implementation.md)，公网契约见 [网关接口契约](gateway-contract.md)，运维动作见 [部署说明](deployment.md)。

适用对象：`C:\qwen3-tts-base-web` 这台 Windows GPU 机器（worker_id = `example-worker`）。

## 1. 链路与角色边界

```text
Mac SpeakVideo :8765
   → HTTPS https://tts.example.com/v1/*
   → nginx TLS → 127.0.0.1:18765 Go gateway（阿里云）
   ← WSS Windows example-worker 主动出站 /v1/worker/ws
   → Windows 回环 127.0.0.1:8001 Qwen / CUDA
```

| 组件 | 负责 | 明确不做 |
| --- | --- | --- |
| Go gateway | 鉴权、单一 worker 会话管理、request_id 关联、HTTP↔WSS 转换、内存校验 | 不落盘作业/音频，不做推理，不认识音色内容 |
| Windows agent (`speakvideo_agent.py`) | WSS 协议、作业队列与 SQLite 元数据、PT 资产库、内存 WAV 回传 | 不加载 GPU 模型，不存中间音频 |
| Windows Qwen (`qwen3_tts_web`) | 真实推理、参考克隆、音色设计 | 只监听 127.0.0.1，不直接对公网暴露 |

Windows 侧对外只有一条出站 WSS。**不需要**在 Windows 开任何入站端口。

## 2. 能力与版本检查

部署后读取 worker.health、worker.capabilities 和 worker.metrics，检查 Qwen readiness、协议版本与 Worker ID。网关旧路由可能返回 404；工作台可回退读取健康检查中的文档，文档不替代管理路由的实际可用性。

Gateway 模式以 Worker 的音色库为主资产；工作台只拉取并核对 SHA256。实际 Qwen 项目可能具有自己的新接口，请先核对差异，不要整体覆盖其安装。

## 3. 环境与依赖

| 依赖 | 用途 | 缺失时 |
| --- | --- | --- |
| Python 3.11.16 真实解释器 | agent 运行 | `run.ps1` 抛「Real Python interpreter is unavailable」 |
| `httpx` | 调用回环 Qwen | 必需 |
| `websockets` | WSS 出站 | 必需 |
| `filelock` | agent 单实例锁 | 必需 |
| `psutil` | CPU/内存采样 | 可选；缺失时 metrics 置 `cpu_memory_status: "psutil not installed"`，字段为 null |
| `nvidia-smi`（PATH） | GPU 采样 | 可选；缺失时 `gpu_status` 说明原因，`gpus: []` |
| `torch` / `qwen_tts` / `soundfile` | 仅 Qwen 服务与 PT 结构校验 | agent 本身不 import torch |

注意：`run.ps1` 不直接使用 `.venv\Scripts\python.exe`（uv trampoline 在 Windows SSH 会话下可能指向不可访问的 junction），而是读 `.venv\pyvenv.cfg` 的 `home =` 行得到真实解释器路径，并设置 `__PYVENV_LAUNCHER__`。改 `pyvenv.cfg` 之前先备份为 `.speakvideo.bak`。

## 4. 目录与文件布局

```text
C:\qwen3-tts-base-web\
  config.toml                     # [qwen3_tts] 原有 + [tts_agent] 新增
  pt\<role>\<role>_<emotion>.pt   # 推理用 PT 副本（Windows 侧资产）
  pt\<role>\<role>_<emotion>.json  # 元数据 ref_text/notes/origin
  pt\<role>\<role>_<emotion>.wav   # 参考音频（克隆时留存）
  .speakvideo\
    jobs.sqlite3                  # 作业元数据，绝不存音频
    agent.lock                    # agent 单实例锁
    voice-trash\<uuid32>\        # 软删除/覆盖归档 voice.pt|metadata.json|reference.wav|entry.json
    logs\agent.stdout.log|agent.stderr.log
    logs\qwen.stdout.log|qwen.stderr.log
  scripts\speakvideo\            # 本仓库 worker/ 的全部文件
```

`QWEN3_TTS_ROOT` 未设置时 root 取进程 cwd；`run.ps1` 会显式设为 Qwen 根目录。

## 5. `config.toml` 的 `[tts_agent]`

```toml
[tts_agent]
gateway_url = "wss://tts.example.com/v1/worker/ws"   # 必须 ws:// 或 wss://
token = "<Worker Token，与 Client Token 不同，≥32 字符>"
worker_id = "example-worker"                                  # 与网关 WORKER_ID 完全一致
qwen_url = "http://127.0.0.1:8001"                   # 回环，公网不可达
reconnect_min = 1.0
reconnect_max = 30.0
request_timeout = 3600.0
max_queue = 32
```

- 每项都可用环境变量 `QWEN3_TTS_AGENT_<KEY>` 覆盖（如 `QWEN3_TTS_AGENT_TOKEN`）。
- `token` 缺失且未加 `--allow-anonymous` 时 agent 拒绝启动；`speakvideo_agent.main()` 进一步要求 `wss://` 与 token 同时存在。
- Token 不写入仓库、日志或任何文档。

## 6. WSS 握手与保活

`GET /v1/worker/ws`，请求头 `Authorization: Bearer <Worker Token>`、`X-Worker-ID: example-worker`。连接后**首帧**必须是：

```json
{"type":"worker.hello","protocol":"1","worker_id":"example-worker",
 "capabilities":{"tts":true,"voice_sync":true,"audio_encoding":"base64-wav",
 "durable_jobs":true,"rpc":true,"correlated_acks":true}}
```

- `protocol` 必须是 `"1"`，`worker_id` 必须与网关配置一致。
- `durable_jobs` / `rpc` / `correlated_acks` 必须都为 `true`，否则网关回 `worker.error: SpeakVideo worker extension required` 并关闭。
- 网关最多保留一个活动 worker，重复连接**不会**替换原连接（避免两条独立 GPU 队列）。新 agent 连上会被直接关闭，需要按指数退避重试，因此升级时必须先停旧 agent。
- WebSocket 层 ping/pong：agent `ping_interval=20, ping_timeout=20`；网关每 20s 发 ping，读超时 70s。`max_size` 两侧都取 96MiB。
- 应用层 `worker.ping` → 必须回 `worker.pong`（含 `worker_id`、`ts`）。

## 7. 作业事件（持久化配音）

```text
gateway → job.submit {job:{job_id,text,role,emotion,language,voice_sha256,generation_revision}, request_id}
agent   → job.accepted {job_id, request_id}          # request_id 原样回传
agent   → job.rejected {job_id, request_id, error}
agent   → job.running  {job_id}
agent   → job.result   {job_id, status, ...}
```

- `job_id` 形如 `^j_[a-f0-9]{64}$`，由 `sha256(worker_id + "\0" + Idempotency-Key)` 生成；不合法直接 `job.rejected`。
- 接受前必须完成 SQLite 落库（`PRAGMA journal_mode=WAL` + `synchronous=FULL`，`commit()` 之后才发 `job.accepted`）。
- 同 `job_id` 重复提交且 digest 一致 → 直接回 `job.accepted`，**不重复入队、不重复推理**。
- 同 `job_id` 参数不同（digest 不同）→ `job.rejected: idempotency key reused with different parameters`。
- 队列满（`max_queue`）→ `job.rejected: queue is full`。
- `job.result.status` ∈ `succeeded|failed|cancelled`；成功时带 `content_type: "audio/wav"`、`sample_rate`、`bytes`、`audio_base64`（base64 WAV，内存返回，不写文件）。
- `text` 必须非空白且 ≤ 10000 字符；`role`/`emotion` 必须能解析到 `pt/<role>/<role>_<emotion>.pt`。
- 推理前再次计算实际 `.pt` 的 SHA256，与提交时的 `voice_sha256` 比对，不一致直接失败，防止排队期间音色被替换。
- agent 重启时，SQLite 里所有 `queued/running/succeeded` 统一改为 `failed / worker_restarted_result_lost`，旧键重复提交也不重新推理。
- 内存结果缓存上限 128MiB（base64 口径），淘汰时被淘汰作业改判 `failed / audio_cache_evicted; explicitly retry`。

## 8. 扩展 RPC 全表

所有 RPC 都是「入站一条消息 → 回一条 `rpc.result`（或 `rpc.result` 带 `rpc_error`）」，必须原样回传 `request_id`。慢操作（GPU/文件）必须在独立 task 里跑，不能阻塞接收循环。

| type | 入站字段 | 回包 | 说明 |
| --- | --- | --- | --- |
| `worker.capabilities` | `request_id` | `result` = 完整接口文档，schema_version=1.0、protocol=1 | 不依赖 GPU 推理 |
| `worker.health` | `include_docs`，可选、默认 true | Qwen `/api/health` 内容 + `worker_id` + `capabilities{voice_manage,voice_reference,metrics,interface_docs} + interface_docs（可选）` | 网关据此返回 200/503 |
| `worker.metrics` | — | `result` = 资源快照 | 见 §9 |
| `job.query` | `job_id`，`include_audio` | `job_id`、`status`、`error`（+ `audio_base64` 等） | `status="missing"` 表示未知；仅 `include_audio=true` 且 `succeeded` 才带音频 |
| `voice.list` | — | `result` = Qwen `/api/pt/list` 原文 | 含 `dir` 字段，Mac 直连模式用它发现 PT 目录 |
| `voice.download` | `role`、`emotion` | `content_base64` | 单文件 ≤ 32MiB |
| `voice.create` | `metadata`、`filename`、`content_base64` | `result` = Qwen `/api/pt/create` 原文 | 见 §10 |
| `voice.design` | `payload` | `audio_base64` | 内存 WAV，≤ 64MiB |
| `voice.delete` | `role`、`emotion`、`sha256` | `result` = `{deleted:true,...}` | 见 §10 |
| `voice.manage` | `payload`（含 `action`） | `result` | 见 §10 |

规则：

- RPC 传输/执行异常统一 `response["rpc_error"] = str(exc)`；**任务失败不是 RPC 失败**，仍是 `status="failed"` 的合法查询结果。
- `job.query` 里若 `include_audio=true` 但结果已被淘汰，回 `rpc_error`，网关返回 502；Mac 会保留 job_id 继续轮询，随后读到 `failed / audio_cache_evicted`。
- `voice.sync` / `voice.sync.result` 是既有事件（不是 rpc），网关用 `"sync:" + sync_id` 关联，`sync_id` 由网关生成。

## 9. `worker.metrics` 快照结构

```json
{"timestamp":"2026-10-13T03:00:00Z","queue_depth":0,"current_job_id":"",
 "cpu_percent":12.5,"memory_used_mb":8192,"memory_total_mb":32768,
 "gpus":[{"name":"NVIDIA GeForce RTX 4090","utilization_percent":0,
          "memory_used_mb":1024,"memory_total_mb":24564,"temperature_c":42}],
 "logs":["<最近 50 条 job 事件摘要>"]}
```

- 取不到就置 `null` / 空数组，并附 `cpu_memory_status` / `gpu_status` 说明，**不编造数值**。
- `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits`，超时 3s。
- 网关超时 8s，超时返回 503；Mac 只做展示，不影响配音。

## 10. PT 资产库（`voice.manage` / `voice.delete`）

### 10.1 资产命名与同盘文件

```text
pt/<role>/<role>_<emotion>.pt
pt/<role>/<role>_<emotion>.json   # {"ref_text":..., "notes":..., "origin":...}
pt/<role>/<role>_<emotion>.wav    # 参考音频，可能不存在
```

`role`/`emotion` 只允许 `^[\w-]{1,60}$`（agent 侧 `voice_path` 用 `{1,64}`）；路径必须相对、以 `.pt` 结尾、至少两级、不能含 `..` /`:`/绝对路径，且必须落在 `pt/` 内。目录为空时删除该 role 目录。

### 10.2 action 一览

| action | 必填字段 | 行为与返回 |
| --- | --- | --- |
| `list` | — | 扫描 `pt/*/*.pt`，返回 `{"assets":[{role,emotion,filename,sha256,bytes,has_reference,ref_text,notes,origin}], "source":"windows"}` |
| `import` | `role`、`emotion`、`content_base64`、`overwrite`、`ref_text`、`notes` | ≤32MiB；已存在且未 overwrite 报错；overwrite 永久替换旧文件，再原子落盘（临时文件 + `os.replace`），删除旧 `.wav`，写新 `.json` |
| `edit` | `role`、`emotion`、`new_role`、`new_emotion`、`ref_text`、`notes`、`sha256` | 先校验 `sha256`；改名用 `os.replace` 搬移 `.pt/.json/.wav`，目标已存在则拒绝；返回新名称的 detail |
| `delete` / `delete_permanent` | `role`、`emotion`、`sha256` | 校验 `sha256` 后永久删除 `.pt/.json/.wav`，返回 `{deleted:true,permanent:true,remote_status:"deleted"}`；新 Mac 客户端只使用 `delete_permanent`，防止旧 Agent 执行软删除 |
| `reference` | `role`、`emotion`、`sha256` | 校验 `sha256` 后返回同盘 `.wav` 的 `content_base64`；没有参考音频则报错 |

不再提供 `trash` / `restore`。历史备份文件保留，新删除和替换不产生备份。更新这三个 Agent 文件后需重启 Windows Agent；Mac 无远程文件部署接口。

### 10.3 `voice.delete`（旧版直删 RPC，保留兼容）

Mac 的删除队列在两种情况下会走 `DELETE /v1/voices`：本地删除后补删远端。行为：`sha256` 必须匹配，不匹配报 `voice SHA256 changed; refusing to delete a different PT`；文件已不存在时返回 `{"deleted":true,"already_missing":true}`（幂等）；否则永久删除 PT 与关联元数据和参考音频。

### 10.4 `voice.create` 扩展行为

1. `metadata` 至少含 `role`/`emotion`/`ref_text`（网关要求 `ref_text` 非空白）/`overwrite`。
2. 若 `overwrite=true` 且目标已存在，**先归档旧资产**再调用 Qwen。
3. 调 `POST /api/pt/create`，body 用 `ref_audio=""` + `ref_audio_base64=<网关转的 base64>`，不传 Windows 本地路径。
4. 成功后把参考音频写成 `.wav`、写 `.json`（`origin: "reference-clone"`）。
5. 失败（Qwen 报错）时旧资产已在 trash 中，需要在日志/回包中说明，Mac 会重新拉取清单。

### 10.5 原子性与锁

- `sync`、`create`、`design`、`delete`、`manage` 全部串行在同一个 `asset_lock` 下；配音推理前也在该锁内做 SHA256 复核。
- 所有落盘都是「同目录临时文件 → `os.replace`」，不出现半成品 `.pt`。
- 清理：删除/覆盖只进 `.speakvideo/voice-trash/`，不直接抹掉用户资产。

## 11. Qwen 内存补丁（`patch_qwen.py`）

给现有 `qwen3_tts_web` 源码打补丁，实现「内存参考音频」与「设计不落盘」：

| 源码锚点（必须唯一匹配） | 改动 |
| --- | --- |
| `allow_download: bool = False` | 追加 `persist: bool = True` |
| `ref_audio: str` | 改为 `ref_audio: str = ""` + `ref_audio_base64: str = ""` |
| `if not os.path.exists(req.ref_audio):` | 变为 `if not req.ref_audio_base64 and not os.path.exists(req.ref_audio):` |
| `kwargs = {"ref_audio": req.ref_audio}` | 插入 `# SpeakVideo memory reference v1` 分支：base64 解码 → `soundfile` 读为 float32 → 立体声转单声道 → `(audio, sample_rate)` |
| `def save_generation(req, wavs, sr, generated):` | 函数体追加：`persist=false` 时直接返回内存 WAV（`X-Audio-Sample-Rate` 头） |

约束：

- 参考音频 ≤ 32MiB、时长 ≤ 120 秒，否则 `HTTPException(400)`。
- 锚点任何一个不是唯一匹配 → 抛 `Qwen source layout changed; review the patch before applying`，**拒绝乱改**。
- 打补丁前保留 `<name>.speakvideo-<sha256 前 12 位>.bak`；写入用临时文件 + replace；补丁后用 `ast.parse` 校验语法。
- 已有 `# SpeakVideo memory reference v1` 标记时视为已安装，直接退出。
- **补丁后必须重启 Qwen**；原有网页持久化 API 行为保持不变。

## 12. 常驻部署（Windows 任务计划）

```powershell
& C:\qwen3-tts-base-web\scripts\speakvideo\install.ps1
Start-ScheduledTask SpeakVideoQwen
Start-ScheduledTask SpeakVideoTtsAgent
Get-ScheduledTask SpeakVideoQwen,SpeakVideoTtsAgent

# 更新 agent 后（必须等旧进程释放 agent.lock）
& C:\qwen3-tts-base-web\scripts\speakvideo\restart-agent.ps1
```

- 两个任务：`SpeakVideoQwen`、`SpeakVideoTtsAgent`；SYSTEM 账户、开机启动、失败 1 分钟后重启（`RestartCount 999`）、`MultipleInstances IgnoreNew`、`ExecutionTimeLimit 0`。
- `install.ps1` 发现同名任务指向别的启动器会直接抛错，不会覆盖。
- `restart-agent.ps1` 先 `Stop-ScheduledTask`，再轮询（最多 30s）直到命令行含 agent 路径的进程退出，才 `Start-ScheduledTask`，避免 stop/start 竞态。
- 双实例保护：Qwen 用 `.runtime.lock`，agent 用 `.speakvideo/agent.lock`（`FileLock(timeout=0)`，冲突立即失败）。
- 日志：`.speakvideo/logs/{qwen,agent}.{stdout,stderr}.log`；不打印 Token、台词或音频数据。

## 13. 本次升级与以后维护

更新前核对各端实际版本，备份配置与二进制，在无创作任务时按自己的部署流程操作。网关模板的备份与回滚方法见 deployment.md。

```text
1. Mac: 构建 gateway Linux 二进制并运行 Go / Python 检查。
2. 审阅远端变更和回滚步骤后更新网关，核对 SSH 包装脚本和目录。
3. 检查 WSS 自动重连、worker.capabilities 的 request_id、voice.manage 与 metrics。
4. Mac: 页面刷新 Windows 音色库，拉取清单并校验 PT SHA256。
```

今后修改 Windows 功能应在 qwen3-tts-base-web 主仓库提交、测试和部署。本仓库 worker/ 为协议适配与测试来源，不要将其直接覆盖当前 Windows 源码树；如确需改 Windows，先对照实际版本和变更范围再操作。

## 14. 当前网关大小限制

| 项 | 当前限制 | 用途 |
| --- | --- | --- |
| nginx `location ^~ /v1/` | 48MiB | 包容 PT 管理中的 base64 JSON |
| gateway 普通 body | 32MiB | 普通 JSON / multipart |
| gateway `/v1/voices/manage` | 48MiB | 原始 PT ≤32MiB，base64 后约 43MiB |
| `/v1/voices/download` | PT ≤32MiB | 超限返回 502 `invalid PT response` |

## 15. 验收清单（Windows 侧）

```powershell
# 协议与持久化（假推理，不占 GPU）
python C:\qwen3-tts-base-web\scripts\speakvideo\smoke.py
```

```bash
go -C gateway test -race ./...
```

- [ ] `smoke.py` 输出「dedupe, conflict, WAV, restart result loss」。
- [x] 本次真实 GPU：两句配音成功；`GET /v1/worker/metrics` 返回真实 GPU 数据；`GET /v1/health` 的 `capabilities` 含 `voice_manage/voice_reference/metrics`。
- [x] 本次 Mac 页面刷新音色库后清单 `source == "windows"`、`editable == true`，且 `.cache/remote-voices/` 下逐文件 SHA256 与 Windows 一致。
- [ ] 页面导入 / 改名 / 删除 / 恢复 / 试听参考音频各走一次；Windows `pt/` 与 `.speakvideo/voice-trash/` 变化符合 §10。
- [ ] agent 重启后旧作业为 `failed / worker_restarted_result_lost`，重复旧键不重复推理。
- [ ] Windows Qwen 原有 `output` 目录在设计、克隆、配音、重试前后文件数量 / 名称 / 长度 / mtime 不变（零中间音频的硬约束）。
- [ ] Qwen 仍只监听 `127.0.0.1:8001`；agent 无任何入站监听。

## 16. 不需要 Windows 实现的部分

LLM 剧本与阐述、ffmpeg 混音 / 响度 / BGM 闪避、SRT 与时间线、WAV/MP3 交付打包、网页与录音裁剪、工程与 manifest 状态机，全部在 Mac 侧完成。Windows 只回答「给一段文本和一个音色，回一份 WAV」和「音色资产的增删改查」。

## 17. 边界与禁忌

- 不在 Windows 保存中间音频；不在 `pt/` 之外放推理用 `.pt`。
- 不把 Mac 本地路径传给 Windows；参考音频一律 base64 内存传递。
- 不向网关日志输出 Token、台词或音频。
- 不推送 `config.local.toml`、`.cache/llm.local.json`、`.cache/gateway-secrets.json`、模型与音频交付物。
- `.cache/` 下只是本机缓存与探测脚本，不是交付代码。
