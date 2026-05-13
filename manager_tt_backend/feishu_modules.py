from __future__ import annotations

import copy

from .config_modules import ensure_dict, mask_secret

FEISHU_CHANNEL_ID = "feishu"
FEISHU_DEFAULT_ACCOUNT_ID = "default"
FEISHU_DEFAULT_DOMAIN = "feishu"
FEISHU_DEFAULT_CONNECTION_MODE = "websocket"
FEISHU_MODULE_BASE_CHANGED_PATHS = (
    "channels.feishu.enabled",
    "channels.feishu.domain (default-only when missing)",
    "channels.feishu.connectionMode (default-only when missing)",
)
_FEISHU_ACCOUNT_ALLOWED_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_FEISHU_SUMMARY_FIELDS = (
    "domain",
    "connectionMode",
    "dmPolicy",
    "groupPolicy",
    "requireMention",
)


def validate_feishu_app_id(app_id: str | None) -> str:
    candidate = app_id.strip() if isinstance(app_id, str) else ""
    if not candidate:
        raise ValueError("appId 不能为空")
    return candidate


def validate_feishu_app_secret(app_secret: str | None) -> str:
    candidate = app_secret.strip() if isinstance(app_secret, str) else ""
    if not candidate:
        raise ValueError("appSecret 不能为空")
    return candidate


def normalize_feishu_account_id(account_id: str | None) -> str:
    candidate = account_id.strip() if isinstance(account_id, str) else ""
    if not candidate:
        return FEISHU_DEFAULT_ACCOUNT_ID
    if any(ch not in _FEISHU_ACCOUNT_ALLOWED_CHARS for ch in candidate):
        raise ValueError(f"非法 accountId: {account_id}")
    return candidate


def resolve_feishu_account_target(feishu_config: dict, requested_account_id: str | None = None) -> dict:
    if requested_account_id is not None and requested_account_id != "":
        return {"mode": "account", "accountId": normalize_feishu_account_id(requested_account_id)}

    accounts = feishu_config.get("accounts")
    default_account = feishu_config.get("defaultAccount")
    if isinstance(accounts, dict):
        if isinstance(default_account, str) and default_account.strip():
            return {"mode": "account", "accountId": normalize_feishu_account_id(default_account)}
        for account_id in accounts:
            if isinstance(account_id, str) and account_id.strip():
                return {"mode": "account", "accountId": normalize_feishu_account_id(account_id)}
        return {"mode": "account", "accountId": FEISHU_DEFAULT_ACCOUNT_ID}

    return {"mode": "top-level", "accountId": None}


def resolve_feishu_active_config(feishu_config: dict) -> tuple[dict, dict]:
    feishu = feishu_config if isinstance(feishu_config, dict) else {}
    target = resolve_feishu_account_target(feishu)
    if target["mode"] != "account":
        return target, feishu

    accounts = feishu.get("accounts")
    active = {}
    if isinstance(accounts, dict):
        account = accounts.get(target["accountId"])
        if isinstance(account, dict):
            active = copy.deepcopy(account)
    for key in _FEISHU_SUMMARY_FIELDS:
        if key not in active and key in feishu:
            active[key] = copy.deepcopy(feishu[key])
    if "enabled" not in active and "enabled" in feishu:
        active["enabled"] = feishu["enabled"]
    return target, active


def feishu_changed_paths(target: dict) -> list[str]:
    paths = list(FEISHU_MODULE_BASE_CHANGED_PATHS)
    if target.get("mode") == "account":
        account_id = target.get("accountId") or FEISHU_DEFAULT_ACCOUNT_ID
        paths.extend(
            [
                "channels.feishu.defaultAccount",
                f"channels.feishu.accounts.{account_id}.appId",
                f"channels.feishu.accounts.{account_id}.appSecret",
            ]
        )
    else:
        paths.extend(
            [
                "channels.feishu.appId",
                "channels.feishu.appSecret",
            ]
        )
    return paths


def apply_feishu_channel_package(config: dict, app_id: str, app_secret: str, account_id: str | None = None) -> tuple[dict, dict]:
    resolved_app_id = validate_feishu_app_id(app_id)
    resolved_app_secret = validate_feishu_app_secret(app_secret)
    merged = copy.deepcopy(config)

    channels = ensure_dict(merged, "channels")
    feishu = ensure_dict(channels, FEISHU_CHANNEL_ID)
    feishu["enabled"] = True
    feishu.setdefault("domain", FEISHU_DEFAULT_DOMAIN)
    feishu.setdefault("connectionMode", FEISHU_DEFAULT_CONNECTION_MODE)

    target = resolve_feishu_account_target(feishu, account_id)
    if target["mode"] == "account":
        target_account_id = target["accountId"] or FEISHU_DEFAULT_ACCOUNT_ID
        feishu["defaultAccount"] = target_account_id
        accounts = ensure_dict(feishu, "accounts")
        account = ensure_dict(accounts, target_account_id)
        account["appId"] = resolved_app_id
        account["appSecret"] = resolved_app_secret
    else:
        feishu["appId"] = resolved_app_id
        feishu["appSecret"] = resolved_app_secret

    return merged, target


def extract_feishu_channel_fragment(config: dict, *, mask_app_secret: bool = False, target: dict | None = None) -> dict:
    channels = config.get("channels") if isinstance(config.get("channels"), dict) else {}
    feishu = channels.get(FEISHU_CHANNEL_ID) if isinstance(channels.get(FEISHU_CHANNEL_ID), dict) else {}
    fragment = {
        "channels": {
            FEISHU_CHANNEL_ID: {
                "enabled": bool(feishu.get("enabled")),
                "domain": feishu.get("domain"),
                "connectionMode": feishu.get("connectionMode"),
                "dmPolicy": feishu.get("dmPolicy"),
                "groupPolicy": feishu.get("groupPolicy"),
                "requireMention": feishu.get("requireMention"),
            }
        }
    }
    target_target = target or resolve_feishu_account_target(feishu)
    channel_fragment = fragment["channels"][FEISHU_CHANNEL_ID]

    if isinstance(feishu.get("defaultAccount"), str) and feishu.get("defaultAccount").strip():
        channel_fragment["defaultAccount"] = feishu["defaultAccount"].strip()
    if isinstance(feishu.get("appId"), str):
        channel_fragment["appId"] = feishu["appId"]
    if "appSecret" in feishu:
        secret = copy.deepcopy(feishu["appSecret"])
        if mask_app_secret and isinstance(secret, str):
            secret = mask_secret(secret)
        channel_fragment["appSecret"] = secret

    accounts = feishu.get("accounts")
    if isinstance(accounts, dict):
        account_ids: list[str] = []
        target_account_id = target_target.get("accountId")
        if isinstance(target_account_id, str) and target_account_id in accounts:
            account_ids.append(target_account_id)
        else:
            account_ids.extend(
                account_id
                for account_id in accounts
                if isinstance(account_id, str) and isinstance(accounts.get(account_id), dict)
            )
        if account_ids:
            account_fragment_map = {}
            for account_id in account_ids:
                account = accounts.get(account_id)
                if not isinstance(account, dict):
                    continue
                account_fragment = {}
                for key in ("appId", "name", "domain", "connectionMode", "enabled"):
                    if key in account:
                        account_fragment[key] = copy.deepcopy(account[key])
                if "appSecret" in account:
                    secret = copy.deepcopy(account["appSecret"])
                    if mask_app_secret and isinstance(secret, str):
                        secret = mask_secret(secret)
                    account_fragment["appSecret"] = secret
                account_fragment_map[account_id] = account_fragment
            if account_fragment_map:
                channel_fragment["accounts"] = account_fragment_map

    return fragment


def summarize_feishu_channel(config: dict) -> dict:
    channels = config.get("channels") if isinstance(config.get("channels"), dict) else {}
    feishu = channels.get(FEISHU_CHANNEL_ID) if isinstance(channels.get(FEISHU_CHANNEL_ID), dict) else {}
    target, active_config = resolve_feishu_active_config(feishu)
    secret_value = active_config.get("appSecret")
    secret_provider = secret_value if isinstance(secret_value, dict) else None
    accounts = feishu.get("accounts") if isinstance(feishu.get("accounts"), dict) else {}
    app_id = active_config.get("appId") if isinstance(active_config.get("appId"), str) else None
    return {
        "enabled": bool(active_config.get("enabled", feishu.get("enabled"))),
        "accountMode": target.get("mode"),
        "accountId": target.get("accountId"),
        "defaultAccount": feishu.get("defaultAccount"),
        "accountCount": len(accounts),
        "appId": app_id,
        "hasAppId": bool(app_id),
        "hasAppSecret": bool(secret_provider) or bool(isinstance(secret_value, str) and secret_value.strip()),
        "usesSecretProvider": bool(secret_provider),
        "domain": active_config.get("domain"),
        "connectionMode": active_config.get("connectionMode"),
        "dmPolicy": active_config.get("dmPolicy"),
        "groupPolicy": active_config.get("groupPolicy"),
        "requireMention": active_config.get("requireMention"),
    }
