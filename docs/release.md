# 发布候选与公开流程

本次改造只准备本地分支与外部历史清理候选。强制更新远端、删除远端引用和修改仓库可见性，需要最终审阅后单独确认。

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

检查器阻止常见私人目录、权重、音色、媒体、机器路径和已知凭据形式，并检查 wheel 的字体和推理依赖配置。规则检查不能替代人工审阅、第三方素材授权确认或完整凭据审计。

## 隔离的历史候选

先在仓库外建立权限受限的替换规则文件，使用 git-filter-repo 的 `literal:` 或 `regex:` 规则，包含原机器地址、用户名、路径及实际发现的敏感值。该规则文件本身不得提交。

```bash
uv run python scripts/prepare_public_history.py /path/outside/repo/public-candidate.git \
  --replacements /path/outside/repo/private-replacements.txt
cd /path/outside/repo/public-candidate.git
python /path/to/SpeakVideo/scripts/check_release.py --history
```

脚本新建 bare 镜像，保留 main/divide 与标签，删除临时分支和远端跟踪引用，剔除私人数据、旧截图和音视频权重历史，替换文本与提交消息，再移除镜像的 origin。不会重写原工作仓库或执行 push。

公开前审阅候选的所有引用、提交映射、源码差异、许可证与恢复步骤。检查历史中的私钥、Token、账号与私有部署内容；实际发现有效凭据须先轮换。准备新的备份 bundle，并保存被替换的远端分支和标签清单。

## 最终门槛与恢复

1. 完成 acceptance 中尚未通过的目标平台真实验收与干净克隆安装。
2. 审阅并确认本次远端强制更新的 main/divide/标签映射以及仓库公开动作。
3. 按确认清单推送清理后的引用，逐项校验 GitHub 分支和 CI；再发布版本说明。
4. 历史改变后协作者重新克隆，不把旧历史整体合回公开分支。

需要恢复时，从仓库外 bundle 克隆到新的私人目录，恢复工作树备份与私有配置。不要把包含私人资料的旧 bundle 或旧引用推到公开仓库。强制推送后仍需按 GitHub 的流程处理旧对象缓存和可访问引用。

本候选不会把尚未实测的平台标为已验收，不将私有仓库已配置分支等同于已开源发布。
