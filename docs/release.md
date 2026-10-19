# 发布候选与公开流程

本文记录历史清理、审阅及公开步骤。2026-10-19 用户已明确授权：完成后推送并开源。发布使用仓库外清理后的候选，保留原仓库及远端镜像备份；只公开实际验证过的能力，尚未完成的 GPU 平台以预览状态说明。

## 分支与数据

- `divide` 保存现有分布式源码和创作功能，私人数据留在本机及仓库外完整备份。
- `codex/standalone` 在独立工作目录开发，验收后更新本地 `main`。
- Git bundle、未提交修改、个人配置、工程、音色、参考音频和成片在仓库外备份，附文件清单与 SHA256。
- 工作目录移出版本控制不等于历史已清理；旧历史不得直接公开。

## 源码与包检查

```bash
uv sync --locked --python 3.11
uv run ruff check speakvideo tests scripts
uv run ruff format --check speakvideo tests scripts
uv run pytest -q
node --check speakvideo/static/app.js
node --check speakvideo/static/runtime.js
node --check speakvideo/static/video-design.js
node --check speakvideo/static/video-editor.js
uv build
uv run python scripts/check_release.py
```

检查器阻止常见私人目录、权重、音色、媒体、机器路径和已知凭据形式，并检查 wheel 的字体和推理依赖配置。唯一明确选定的媒体例外是 `scripts/public_assets.json` 中列出的八个角色 PT 和一个 README 成片；文件路径和 SHA256 必须同时匹配，改动内容需重新审阅清单。示例来源及第三方许可边界分别记录在相邻 NOTICE。规则检查不能替代人工审阅、第三方素材授权确认或完整凭据审计。

## 隔离的历史候选

先在仓库外建立权限受限的替换规则文件，使用 git-filter-repo 的 `literal:` 或 `regex:` 规则，包含原机器地址、用户名、路径及实际发现的敏感值。该规则文件本身不得提交。

```bash
uv run python scripts/prepare_public_history.py /path/outside/repo/public-candidate.git \
  --replacements /path/outside/repo/private-replacements.txt
cd /path/outside/repo/public-candidate.git
python /path/to/SpeakVideo/scripts/check_release.py --history
```

脚本新建 bare 镜像，保留 main/divide 与标签，删除临时分支、远端跟踪引用及应用检查点引用，剔除私人数据、旧截图和音视频权重历史；仅保留路径和 SHA256 都符合上述清单的示例媒体。部署日志类文档仅保留两个分支当前审阅过的版本，旧私人记录从历史移除，替换文本与提交消息，再移除镜像的 origin。不会重写原工作仓库或执行 push。

公开前审阅候选的所有引用、提交映射、源码差异、许可证与恢复步骤。检查历史中的私钥、Token、账号与私有部署内容；实际发现有效凭据须先轮换。准备新的备份 bundle，并保存被替换的远端分支和标签清单。

## 最终门槛与恢复

1. 完成干净克隆安装，并在 acceptance 保留尚未完成的真实 GPU 平台验收；这些是稳定版门槛，预览版不得写为已验证。
2. 审阅并确认本次远端强制更新的 main/divide/标签映射以及仓库公开动作。
3. 按确认清单推送清理后的引用，逐项校验 GitHub 分支和 CI；再发布版本说明。
4. 历史改变后协作者重新克隆，不把旧历史整体合回公开分支。

需要恢复时，从仓库外 bundle 克隆到新的私人目录，恢复工作树备份与私有配置。不要把包含私人资料的旧 bundle 或旧引用推到公开仓库。强制推送后仍需按 GitHub 的流程处理旧对象缓存和可访问引用。

本候选不会把尚未实测的平台标为已验收，不将私有仓库已配置分支等同于已开源发布。
