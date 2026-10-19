#!/usr/bin/env python3
"""环境自检：开跑之前先确认三组模型配置是不是真的能用。

用法：
    python examples/check_env.py

逐项检查（不打印任何密钥）：
1. 环境变量是否填了（不是占位符）
2. base_url 域名能不能解析 + 443 能不能连
3. 用你的 key 请求 {base_url}/models 能不能拿到 200
4. 你配置的模型名是否在可用清单里（这一步最常见，写错就是 Unsupported model）

退出码 0 表示三组都通。
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402

ROLES = (
    ("coder", "coder_configured", "CODER"),
    ("reviewer", "reviewer_configured", "REVIEWER"),
    ("fixer", "fixer_configured", "FIXER"),
)

TIMEOUT = 15


def check_dns(host: str) -> str:
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return f"解析失败（{type(exc).__name__}）"
    return "解析正常"


def check_tcp(host: str) -> str:
    try:
        with socket.create_connection((host, 443), timeout=8):
            return "443 可连"
    except OSError as exc:
        return f"连接失败（{type(exc).__name__}）"


def fetch_models(base_url: str, api_key: str) -> tuple[str, list[str]]:
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}（{_short(exc.read(160).decode('utf-8', 'replace'))}）", []
    except Exception as exc:  # noqa: BLE001 - 自检脚本，尽力给出原因
        return f"请求失败（{type(exc).__name__}）", []

    data = payload.get("data") if isinstance(payload, dict) else None
    models = [item.get("id", "") for item in data] if isinstance(data, list) else []
    return f"HTTP 200（{len(models)} 个模型）", models


def _short(text: str) -> str:
    return " ".join((text or "").split())[:80]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(f"配置文件：{config.PROJECT_ROOT / '.env'}")
    print("=" * 74)

    failures: list[str] = []

    for role, configured_fn, prefix in ROLES:
        api_key = getattr(config, f"{prefix}_LLM_API_KEY")
        base_url = getattr(config, f"{prefix}_LLM_BASE_URL") or "(未设置)"
        model = getattr(config, f"{prefix}_LLM_MODEL")

        print(f"\n[{role}]  model={model}")
        print(f"        base_url={base_url}")

        if not getattr(config, configured_fn)():
            print("        ❌ API Key 未配置（或仍是占位符）")
            failures.append(f"{role}: 缺少 API Key")
            continue

        if base_url == "(未设置)":
            print("        ❌ 没有配置 base_url")
            failures.append(f"{role}: 缺少 base_url")
            continue

        host = urlparse(base_url).hostname or ""
        dns = check_dns(host)
        print(f"        DNS：{dns}")
        if "失败" in dns:
            failures.append(f"{role}: 域名 {host} 无法解析")
            continue

        tcp = check_tcp(host)
        print(f"        TCP：{tcp}")
        if "失败" in tcp:
            failures.append(f"{role}: {host} 443 连不上")
            continue

        status, models = fetch_models(base_url, api_key)
        print(f"        /models：{status}")
        if not status.startswith("HTTP 200"):
            failures.append(f"{role}: /models 不可用（{status}）")
            continue

        if models and model not in models:
            print(f"        ⚠️  模型名不在清单里，可用示例：{', '.join(models[:6])}")
            failures.append(f"{role}: 模型 {model} 不在 /models 清单里")
        else:
            print(f"        ✅ 模型可用，可以开跑")

    print("\n" + "=" * 74)
    if failures:
        print("自检未全部通过：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("三组模型全部就绪 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
