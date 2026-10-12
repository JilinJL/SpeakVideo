from __future__ import annotations

from dataclasses import dataclass

from ..script.schema import CastBinding, Script, validate_ids
from ..voices import VoiceStore
from .client import TTSClient


@dataclass
class PreflightReport:
    errors: list[str]
    warnings: list[str]
    line_count: int
    reusable: int
    slot_summary: dict[str, str]

    @property
    def ok(self):
        return not self.errors


def preflight(
    script: Script,
    cast: CastBinding,
    client: TTSClient,
    *,
    max_line_chars: int = 100,
    done_ids: set[str] | None = None,
    check_remote: bool = True,
) -> PreflightReport:
    errors, warnings = validate_ids(script), []
    local = VoiceStore(client.settings).list_roles()
    if not script.lines:
        errors.append("剧本没有台词")
    available = local
    if check_remote and client.mode == "direct":
        try:
            remote = client.list_roles()
            available = {r: sorted(set(es) & set(remote.get(r, []))) for r, es in local.items()}
        except Exception as exc:
            errors.append(f"无法读取服务音色: {exc}")
    elif check_remote and client.mode == "gateway":
        try:
            if not client.health().get("ready"):
                errors.append("网关执行器尚未就绪")
        except Exception as exc:
            errors.append(f"网关不可用: {exc}")
    if client.mode == "demo":
        warnings.append("测试模式：输出正弦测试音，不是真实配音")
    summary = {}
    for member in script.cast:
        binding = cast.get(member.slot)
        if not binding or not binding.role:
            errors.append(f"槽位 {member.slot} 未绑定角色")
            continue
        if binding.role not in available:
            errors.append(f"{member.slot} 角色 {binding.role} 无本地音色或远端不可用")
        summary[member.slot] = binding.role
    for line in script.lines:
        if len(line.words) > max_line_chars:
            errors.append(f"{line.id} 台词 {len(line.words)} 字，超过上限 {max_line_chars}")
        binding = cast.get(line.slot)
        if not binding:
            continue
        emotion = binding.mood_map.get(line.mood)
        if not emotion:
            errors.append(
                f"{line.id} 需要 {line.slot} 的 {line.mood}，mood_map 未映射；"
                f"{binding.role} 情感: {local.get(binding.role, [])}"
            )
        elif emotion not in available.get(binding.role, []):
            errors.append(f"{line.id} 音色 {binding.role}/{emotion} 不存在或尚未同步")
    return PreflightReport(
        errors,
        warnings,
        len(script.lines),
        sum(line.id in (done_ids or set()) for line in script.lines),
        summary,
    )
