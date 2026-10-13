"""Private, reloadable LLM configuration; APIs never return credentials."""

from .config import LLMSettings, Settings
from .storage import lock, write_json


def public_llm(settings: LLMSettings) -> dict:
    providers = settings.providers or settings.endpoints()
    return {
        "active_provider": settings.active_provider or (providers[0].id if providers else ""),
        "failover": settings.failover,
        "providers": [
            {
                **p.model_dump(exclude={"api_keys"}),
                "key_count": len(p.api_keys),
                "key_labels": [f"Key {i + 1} · 已保存" for i in range(len(p.api_keys))],
            }
            for p in providers
        ],
    }


def save_llm(settings: Settings, body: dict) -> dict:
    old = {p.id: p for p in settings.llm.providers or settings.llm.endpoints()}
    values = dict(body)
    providers = []
    for entry in values.get("providers", []):
        item = {k: v for k, v in entry.items() if k not in {"key_count", "key_labels"}}
        if item.get("api_keys") is None:
            item["api_keys"] = old[item["id"]].api_keys if item["id"] in old else []
        else:
            item["api_keys"] = [k.strip() for k in item["api_keys"] if k.strip()]
        providers.append(item)
    values["providers"] = providers
    updated = LLMSettings.model_validate(values)
    # SecretStr serialization masks values; unwrap ONLY for the private file.
    private = updated.model_dump(mode="json")
    private["api_key"] = updated.api_key.get_secret_value()
    for entry, provider in zip(private["providers"], updated.providers, strict=True):
        entry["api_keys"] = [key.get_secret_value() for key in provider.api_keys]
    with lock(settings.paths.cache / "llm-config.lock"):
        path = settings.paths.cache / "llm.local.json"
        write_json(path, private)
        path.chmod(0o600)
        settings.llm = updated
    return public_llm(updated)
