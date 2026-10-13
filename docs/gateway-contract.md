# P6 网关接口契约（protocol 1 · 2026-10-13 更新）

此契约由 `gateway/` Go 服务和 `worker/` Windows 扩展实现。现网 HTTPS 地址为 `https://tts.example.com`，worker 为 `example-worker`。网关只做鉴权与内存转发，作业元数据与 `.pt` 副本由 Windows agent 管理。接入需 `tts.mode="gateway"`、Client Token 与 `worker_id`，不只是修改 `base_url`。

所有客户端接口使用 HTTPS、`Authorization: Bearer <CLIENT_TOKEN>` 和 `X-Worker-ID: example-worker`，错误使用非 2xx + JSON `detail`。Worker Token 独立，只能连接 WSS worker 路由。所有音频响应为有效 PCM WAV，TTS 采样率 24000。普通请求体上限 32MiB，PT 管理 JSON 上限 48MiB（原始 PT ≤32MiB，base64 约 43MiB）；nginx 上限 48MiB。WAV 上限 64MiB，最多八个并发 HTTP 请求；单 GPU 串行。

## 健康与音色

- `GET /v1/health?include_docs=false` → readiness、worker_connected、模型、设备和布尔 capabilities。`include_docs` 默认 true，按需附完整接口文档。503 且 worker_connected=true 表示 Agent 连通但模型未就绪/检查失败；false 表示 Worker 未连接。
- `GET /v1/worker/capabilities` → 原样 `rpc.result` 信封，保留 `request_id`，接口文档在 `result`。没有开放任意 RPC 透传，仅转发 `worker.capabilities`。
- `GET /v1/worker/metrics` → CPU、内存、GPU、队列及最近事件；无需模型 ready=true。
- `POST /v1/voices/manage` → JSON `{action,...}`，允许 list/import/edit/delete_permanent/reference。返回 Windows 的 action 结果；永久删除需要 `permanent:true` 回执，不再提供回收站恢复。
- `DELETE /v1/voices` → 兼容旧删除队列，要求 role/emotion/sha256。
- `GET /v1/voices` → `{"roles": {"示例角色": ["平静"]}}`。
- `GET /v1/voices/download?role=示例角色&emotion=平静` → `.pt` 原始字节。
- `POST /v1/voices/sync` → multipart 字段 `bundle`，ZIP（内存构造）。ZIP 包含 `manifest.json` 与 `{role}/{role}_{emotion}.pt`。manifest 是数组：

```json
[{"role":"示例角色","emotion":"平静","path":"示例角色/示例角色_平静.pt","sha256":"<sha256>"}]
```

Windows 校验路径、哈希和 Qwen prompt 结构，原子落盘，再回复：

```json
{"voices":[{"role":"示例角色","emotion":"平静","sha256":"<sha256>"}]}
```

sync 保留给兼容客户端；当前 Mac 默认拉取 Windows 清单和 PT，提交前核对远端哈希，不自动推送本机旧音色。加载时也需要确保 pt 与 Python / torch / qwen_tts 版本兼容，校验失败应返回具体错误；Mac 不执行 torch.load。

## 音色生产

- `POST /v1/voices/create`：multipart `file` 为参考音频，`metadata` 为 JSON 字符串，字段 `role/emotion/ref_text/overwrite`。Windows 在内存接收音频、调用 clone prompt、保存 pt 副本，返回 JSON 成功信息。Mac 再调用 download，保存到经过哈希校验的缓存目录。Windows 管理 PT 主资产。
- `POST /v1/voices/design`：JSON `text/instruct/language/mode="file"/allow_download=false`，响应 WAV 字节；不自动固化。Mac 保存试听音频，用户确认后调用 create。

这两个接口通过 agent RPC 适配。Qwen 的内存补丁接收 `ref_audio_base64`，直接解码为数组 / 采样率，不把 Mac 路径传给 Windows，也不上传临时参考音频文件。音色设计使用 `persist=false`；本地设计模型缺失时明确失败，不擅自下载。

## 持久化配音作业

`POST /v1/tts/jobs`，请求头 `Idempotency-Key`（每个工程 / 行 / 内容 / 重试代次唯一）：

```json
{"text":"你好。","role":"示例角色","emotion":"平静","language":"Chinese",
 "voice_sha256":"<Mac pt SHA256>","generation_revision":"qwen3-base-1.7b-v1"}
```

响应 `202 {"id":"<job-id>"}`。现有 ID 为 `j_` + SHA256(worker_id + NUL + Idempotency-Key)，Mac 接受 1–128 个字母数字下划线横线，不含斜线。相同幂等键 / 相同内容返回相同作业，同键不同内容返回 409。Windows SQLite `synchronous=FULL` 在接受确认前提交任务 ID、参数摘要与状态，网关等到 `job.accepted` 才返回。

`GET /v1/tts/jobs/{id}`：

```json
{"id":"job1","status":"queued"}
{"id":"job1","status":"running"}
{"id":"job1","status":"succeeded","audio_url":"/v1/tts/jobs/job1/audio"}
{"id":"job1","status":"failed","error":"CUDA out of memory"}
```

合法状态：`queued/running/succeeded/failed/cancelled`。`audio_url` 必须与 gateway 同源，Mac 拒绝向跨域地址转发 token。

`GET /v1/tts/jobs/{id}/audio` → 24000Hz PCM WAV，鉴权后的内存响应。客户端不会自行改变原始采样率。音频从 agent 返回，gateway 不写临时文件。

## 恢复约束

Mac 在 POST 前保存幂等键、POST 后保存 job ID，再开始轮询。断线不抹掉 job ID；明确失败之后显式重试可创建新代次。当前机器配置轮询与请求等待为 3600 秒；首次默认配置为 1800 / 300 秒。

网关重启不丢 Windows 作业；WSS 自动重连后 `job.query` 从 Windows 查询状态与内存音频。Windows agent 重启时，旧 queued/running/succeeded 元数据统一标为失败 `worker_restarted_result_lost`，重复提交旧键也不会重复入队。音频缓存淘汰返回 `audio_cache_evicted`，须 Mac 显式重试。已下载到 Mac 的有效 WAV 不受影响。

Mac 的暂停停止继续提交与轮询，不调用 cancel。Windows 队列串行，独占 agent 进程锁；Qwen 自身仍使用 inference_lock，音色同步 / 创建 / 设计与配音共用 asset_lock。推理前再次核验实际 `.pt` 的 `voice_sha256`。

## WSS 协议适配

`GET /v1/worker/ws`：独立 Worker Token + `X-Worker-ID`，首帧 `worker.hello` 的 `protocol="1"`、`capabilities.rpc=true` 、`durable_jobs=true` 和 `correlated_acks=true` 必须匹配。最多一个活动 worker，重复连接不能替换原连接。

- 既有事件：`job.submit {job:{...}}` → `job.accepted/job.rejected/job.running/job.result`；`voice.sync {sync_id,files:[{path,sha256,content_base64}]}` → `voice.sync.result`。
- 每次 job.submit 另带随机 request_id，accepted/rejected 必须原样回传；稳定 job_id 用于幂等，request_id 用于本次确认。迟到确认不能匹配新的提交。
- 扩展 RPC：`worker.capabilities`、`worker.health`、`worker.metrics`、`job.query`、`voice.list/download/create/design/delete/manage`，每次带随机 `request_id`，返回 `rpc.result`。RPC 失败使用 `rpc_error`，任务失败仍是合法的 `status="failed",error=...` 查询结果。
- `job.query include_audio=true` 才返回 base64 WAV，网关解码后直接写 HTTP 响应；状态轮询不携带音频。
- WebSocket ping/pong 保活，不输出 Token、台词或音频数据到网关日志。
