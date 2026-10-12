# P6 网关接口契约（Mac 客户端 v1）

这是 Mac 已实现客户端期望的契约，**不表示 Go gateway / Windows agent 已存在**。网关应只做鉴权与内存转发，作业与 `.pt` 副本由 Windows agent 管理。仅 `base_url` 不够，接入时还需设置 `tts.mode="gateway"`。

所有接口使用 HTTPS 和 `Authorization: Bearer <token>`，错误使用非 2xx + JSON `detail`；所有音频响应必须是有效 PCM WAV，TTS 采样率 24000。

## 健康与音色

- `GET /v1/health` → `{"ready": true}`。执行器不在线时必须返回非就绪。
- `GET /v1/voices` → `{"roles": {"示例角色": ["平静"]}}`。
- `GET /v1/voices/download?role=示例角色&emotion=平静` → `.pt` 原始字节。
- `POST /v1/voices/sync` → multipart 字段 `bundle`，ZIP（内存构造）。ZIP 包含 `manifest.json` 与 `{role}/{role}_{emotion}.pt`。manifest 是数组：

```json
[{"role":"示例角色","emotion":"平静","path":"示例角色/示例角色_平静.pt","sha256":"<sha256>"}]
```

Windows 应校验路径、哈希，原子落盘，再回复：

```json
{"voices":[{"role":"示例角色","emotion":"平静","sha256":"<sha256>"}]}
```

Mac 只在确认接收哈希一致后提交配音。同步所需音色子集，不需要删除其他副本。加载时也需要确保 pt 与 Python / torch / qwen_tts 版本兼容，校验失败应返回具体错误；Mac 不执行 torch.load。

## 音色生产

- `POST /v1/voices/create`：multipart `file` 为参考音频，`metadata` 为 JSON 字符串，字段 `role/emotion/ref_text/overwrite`。Windows 在内存接收音频、调用 clone prompt、保存 pt 副本，返回 JSON 成功信息。Mac 再调用 download，保存到自己的主资产目录。
- `POST /v1/voices/design`：JSON `text/instruct/language/mode="file"/allow_download=false`，响应 WAV 字节；不自动固化。Mac 保存试听音频，用户确认后调用 create。

这两个接口是 Mac 对未来网关的要求。原 Qwen 的 upload + create 依赖服务器磁盘路径；agent 需要提供适配，不能原样把 Mac 路径传给 Windows。

## 持久化配音作业

`POST /v1/tts/jobs`，请求头 `Idempotency-Key`（每个工程 / 行 / 内容 / 重试代次唯一）：

```json
{"text":"你好。","role":"示例角色","emotion":"平静","language":"Chinese",
 "voice_sha256":"<Mac pt SHA256>","generation_revision":"qwen3-base-1.7b-v1"}
```

响应 `{"id":"<job-id>"}`。ID 限制为字母数字下划线横线，不含斜线。相同幂等键 / 相同内容必须返回相同作业；同键不同内容拒绝。**须在响应之前持久化幂等映射与作业**，以覆盖请求成功但 Mac 丢失响应的情况。

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

Mac 会在 POST 前保存幂等键、POST 后保存 job ID，再开始轮询。断线不抹掉 job ID；明确失败之后显式重试可创建新代次。轮询等待时间默认 1800 秒（配置可调），单次 HTTP 请求默认 300 秒。

“只在内存保存音频”与“进程崩溃后永不重复推理”无法同时完整保证。agent 应持久化元数据；若其进程重启丢失结果音频，须返回明确失败 / 结果已丢失，Mac 再显式重试。网关仅做内存转发并不意味着 Windows 原 Qwen 服务也不会落盘。

Mac 的暂停停止继续提交与轮询，不会调用未定义的 cancel 接口。远端串行队列必须独立保证线程 / 进程间 GPU 互斥。
