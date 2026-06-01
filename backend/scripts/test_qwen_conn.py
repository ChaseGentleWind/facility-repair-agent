"""
独立测试 DashScope 连通性，绕开 FastAPI / LangGraph。
在和 uvicorn 相同的终端运行：
    cd backend
    python scripts/test_qwen_conn.py
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv(dotenv_path="../.env")
load_dotenv(dotenv_path=".env")


async def main() -> None:
    print("=== Process env (proxy related) ===")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy", "NO_PROXY"):
        print(f"  {k} = {os.environ.get(k)!r}")

    print("\n=== All env vars containing 'proxy' (any case) ===")
    for k, v in os.environ.items():
        if "proxy" in k.lower():
            print(f"  {k} = {v!r}")

    print("\n=== httpx-detected proxies ===")
    import httpx
    from httpx._utils import get_environment_proxies
    print(f"  get_environment_proxies() = {get_environment_proxies()}")

    print("\n=== Windows system proxy (urllib.getproxies) ===")
    import urllib.request
    print(f"  urllib.request.getproxies() = {urllib.request.getproxies()}")

    api_key = os.environ.get("QWEN_API_KEY")
    base_url = os.environ.get("QWEN_BASE_URL",
                              "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model = os.environ.get("QWEN_MODEL", "qwen-plus")
    print(f"\n=== Config ===")
    print(f"  base_url = {base_url}")
    print(f"  model    = {model}")
    print(f"  api_key  = {(api_key or '')[:8]}...{(api_key or '')[-4:]}")

    if not api_key:
        print("ERROR: QWEN_API_KEY missing")
        sys.exit(1)

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    print("\n=== Calling chat.completions (default httpx, may use system proxy) ===")
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "回复一个字: 通"}],
            extra_body={"enable_thinking": False},
            timeout=15,
        )
        print("OK:", resp.choices[0].message.content)
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")

    print("\n=== Calling chat.completions (httpx with proxies disabled) ===")
    import httpx
    no_proxy_client = httpx.AsyncClient(trust_env=False, timeout=15)
    client2 = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=no_proxy_client)
    try:
        resp = await client2.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "回复一个字: 通"}],
            extra_body={"enable_thinking": False},
            timeout=15,
        )
        print("OK:", resp.choices[0].message.content)
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
    finally:
        await no_proxy_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
