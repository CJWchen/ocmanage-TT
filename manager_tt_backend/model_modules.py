from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request

from .config_modules import ensure_dict, mask_secret, truncate_text

TENCENT_PROVIDER_ID = "tencent-coding-plan"
TENCENT_PROVIDER_BASE_URL = "https://api.lkeap.cloud.tencent.com/coding/v3"
TENCENT_PRIMARY_MODELS = (
    "tencent-coding-plan/tc-code-latest",
    "tencent-coding-plan/hunyuan-2.0-instruct",
    "tencent-coding-plan/hunyuan-2.0-thinking",
    "tencent-coding-plan/hunyuan-t1",
    "tencent-coding-plan/hunyuan-turbos",
    "tencent-coding-plan/minimax-m2.5",
    "tencent-coding-plan/kimi-k2.5",
    "tencent-coding-plan/glm-5",
)
TENCENT_MODULE_OWNED_PATHS = (
    "models.mode",
    "models.providers.tencent-coding-plan",
    "agents.defaults.model.primary",
    "agents.defaults.models.tencent-coding-plan/*",
    "plugins.entries.openai.enabled",
    "plugins.allow (append-only when already present)",
)
TENCENT_MODEL_DEFINITIONS = (
    {
        "id": "tc-code-latest",
        "name": "Auto",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 196608,
        "maxTokens": 32768,
    },
    {
        "id": "hunyuan-2.0-instruct",
        "name": "Tencent HY 2.0 Instruct",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 128000,
        "maxTokens": 16000,
    },
    {
        "id": "hunyuan-2.0-thinking",
        "name": "Tencent HY 2.0 Think",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 128000,
        "maxTokens": 32000,
    },
    {
        "id": "hunyuan-t1",
        "name": "Hunyuan-T1",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 64000,
        "maxTokens": 32000,
    },
    {
        "id": "hunyuan-turbos",
        "name": "hunyuan-turbos",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 32000,
        "maxTokens": 16000,
    },
    {
        "id": "minimax-m2.5",
        "name": "MiniMax-M2.5",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 196608,
        "maxTokens": 32768,
    },
    {
        "id": "kimi-k2.5",
        "name": "Kimi-K2.5",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 262144,
        "maxTokens": 32768,
    },
    {
        "id": "glm-5",
        "name": "GLM-5",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 202752,
        "maxTokens": 16384,
    },
)


def validate_tencent_primary_model(primary_model: str | None) -> str:
    candidate = primary_model.strip() if isinstance(primary_model, str) else ""
    if candidate not in TENCENT_PRIMARY_MODELS:
        allowed = ", ".join(TENCENT_PRIMARY_MODELS)
        raise ValueError(f"primaryModel 不支持: {candidate or '<empty>'}；允许值: {allowed}")
    return candidate


def tencent_provider_model_id(primary_model: str) -> str:
    validated = validate_tencent_primary_model(primary_model)
    return validated.split("/", 1)[1]


def build_tencent_provider_config(api_key: str) -> dict:
    secret = api_key.strip() if isinstance(api_key, str) else ""
    if not secret:
        raise ValueError("apiKey 不能为空")
    return {
        "baseUrl": TENCENT_PROVIDER_BASE_URL,
        "apiKey": secret,
        "api": "openai-completions",
        "models": copy.deepcopy(list(TENCENT_MODEL_DEFINITIONS)),
    }


def apply_tencent_model_package(config: dict, api_key: str, primary_model: str) -> dict:
    validate_tencent_primary_model(primary_model)
    merged = copy.deepcopy(config)

    models = ensure_dict(merged, "models")
    models["mode"] = "merge"
    providers = ensure_dict(models, "providers")
    providers[TENCENT_PROVIDER_ID] = build_tencent_provider_config(api_key)

    agents = ensure_dict(merged, "agents")
    defaults = ensure_dict(agents, "defaults")
    default_model = ensure_dict(defaults, "model")
    default_model["primary"] = primary_model

    default_models = defaults.get("models")
    if not isinstance(default_models, dict):
        default_models = {}
        defaults["models"] = default_models
    for key in list(default_models):
        if isinstance(key, str) and key.startswith(f"{TENCENT_PROVIDER_ID}/"):
            default_models.pop(key)
    for full_model_name in TENCENT_PRIMARY_MODELS:
        default_models[full_model_name] = {}

    plugins = ensure_dict(merged, "plugins")
    plugin_entries = ensure_dict(plugins, "entries")
    openai_entry = plugin_entries.get("openai")
    if not isinstance(openai_entry, dict):
        openai_entry = {}
        plugin_entries["openai"] = openai_entry
    openai_entry["enabled"] = True

    allowlist = plugins.get("allow")
    if isinstance(allowlist, list) and "openai" not in allowlist:
        allowlist.append("openai")

    return merged


def extract_tencent_module_fragment(config: dict, *, mask_api_key: bool = False) -> dict:
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    providers = models.get("providers") if isinstance(models.get("providers"), dict) else {}
    provider_config = copy.deepcopy(providers.get(TENCENT_PROVIDER_ID) or {})
    if mask_api_key and isinstance(provider_config.get("apiKey"), str):
        provider_config["apiKey"] = mask_secret(provider_config["apiKey"])

    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    model_defaults = defaults.get("model") if isinstance(defaults.get("model"), dict) else {}
    default_models = defaults.get("models") if isinstance(defaults.get("models"), dict) else {}

    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    plugin_entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    openai_entry = plugin_entries.get("openai") if isinstance(plugin_entries.get("openai"), dict) else {}

    fragment = {
        "models": {
            "mode": models.get("mode"),
            "providers": {
                TENCENT_PROVIDER_ID: provider_config,
            },
        },
        "agents": {
            "defaults": {
                "model": {
                    "primary": model_defaults.get("primary"),
                },
                "models": {
                    model_name: copy.deepcopy(default_models.get(model_name, {}))
                    for model_name in TENCENT_PRIMARY_MODELS
                    if model_name in default_models
                },
            }
        },
        "plugins": {
            "entries": {
                "openai": {
                    **copy.deepcopy(openai_entry),
                    "enabled": bool(openai_entry.get("enabled")),
                }
            }
        },
    }
    if isinstance(plugins.get("allow"), list):
        fragment["plugins"]["allow"] = list(plugins["allow"])
    return fragment


def probe_tencent_model_package(api_key: str, primary_model: str, *, timeout_seconds: int = 20) -> dict:
    model_name = validate_tencent_primary_model(primary_model)
    url = f"{TENCENT_PROVIDER_BASE_URL.rstrip('/')}/chat/completions"
    body = json.dumps(
        {
            "model": tencent_provider_model_id(model_name),
            "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
            "max_tokens": 1,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {(api_key or '').strip()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw or "{}")
            message = ""
            choices = payload.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0] if isinstance(choices[0], dict) else {}
                content = (choice.get("message") or {}).get("content")
                if isinstance(content, str):
                    message = content.strip()
            return {
                "ok": True,
                "status": "ok",
                "httpStatus": getattr(response, "status", 200),
                "model": model_name,
                "url": url,
                "message": message or "probe 成功",
                "responseExcerpt": truncate_text(raw),
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        message = raw
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload.get("error"), dict):
            message = payload["error"].get("message") or raw
        elif isinstance(payload.get("error"), str):
            message = payload["error"]
        return {
            "ok": False,
            "status": "http_error",
            "httpStatus": exc.code,
            "model": model_name,
            "url": url,
            "message": truncate_text(message),
            "responseExcerpt": truncate_text(raw),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "network_error",
            "httpStatus": None,
            "model": model_name,
            "url": url,
            "message": truncate_text(str(exc)),
            "responseExcerpt": "",
        }
