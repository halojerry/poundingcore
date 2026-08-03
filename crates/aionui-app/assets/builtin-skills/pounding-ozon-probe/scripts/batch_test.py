#!/usr/bin/env python3
"""
批量测试脚本：1688 URL → CDP 抓取 → 组装信封 → 提交 Worker
           Ozon URL → 跟卖流程 → 提交 Worker

用法:
  # 试跑（只组装，不提交）
  python3 batch_test.py --urls-file urls.txt --dry-run

  # 干跑前 5 个
  python3 batch_test.py --urls-file urls.txt --dry-run --limit 5

  # 实际提交
  python3 batch_test.py --urls-file urls.txt --submit

  # 从第 10 个开始，跑 20 个
  python3 batch_test.py --urls-file urls.txt --submit --start 10 --limit 20

  # 指定新店铺凭证
  python3 batch_test.py --urls-file urls.txt --submit \
    --client-id 5371047 --api-key 411afbd4-c7ea-4fb3-b14f-3d9c2f246214

环境变量:
  MXOU_API_BASE - Worker 地址 (默认 https://worker.mxou.cn)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure skill/scripts/ is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "batch_results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_urls_file(filepath: str) -> list[dict[str, str]]:
    """Parse URL list file. Returns list of {type, url, id}."""
    results: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            if "ozon.ru" in line:
                m = re.search(r"/product/[^/]+-(\d{6,20})", line)
                pid = m.group(1) if m else ""
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    results.append({"type": "ozon", "url": line, "id": pid})
            elif "1688.com" in line:
                m = re.search(r"offer/(\d+)", line)
                oid = m.group(1) if m else ""
                if oid and oid not in seen_ids:
                    seen_ids.add(oid)
                    results.append({"type": "1688", "url": line, "id": oid})

    return results


def process_1688_url(
    url: str,
    offer_id: str,
    client_id: str,
    api_key: str,
    worker_url: str,
    dry_run: bool,
    store_id: str = "",
) -> dict[str, Any]:
    """Process a single 1688 URL: CDP probe → graph envelope → submit."""
    from scripts.cloud_probe import build_graph_envelope_with_retry, submit_envelope

    result: dict[str, Any] = {
        "type": "1688",
        "url": url,
        "offer_id": offer_id,
        "timestamp": _now_iso(),
        "success": False,
    }

    try:
        # Step 1: Build GraphInput envelope (CDP + assembly)
        print(f"  🔍 [{offer_id}] CDP 抓取 + 组装信封...", flush=True)
        envelope = build_graph_envelope_with_retry(
            item_id=offer_id,
            detail_url=url,
            store_id=store_id,
            max_retries=3,
            retry_delay=15.0,
            max_skus=1,
        )

        if not envelope or not envelope.get("envelope"):
            result["error"] = "build_graph_envelope 返回空"
            print(f"  ❌ [{offer_id}] 信封为空", flush=True)
            return result

        draft = envelope.get("envelope", {}).get("draft", {})
        result["title"] = draft.get("title", "")[:80]
        result["price"] = draft.get("price", "")
        result["images_count"] = len(draft.get("images", []))
        result["envelope_saved"] = True

        print(f"  ✅ [{offer_id}] 信封组装完成: {result['title']}", flush=True)

        if dry_run:
            result["success"] = True
            result["dry_run"] = True
            return result

        # Step 2: Override store credentials
        envelope["ozon_client_id"] = client_id
        envelope["ozon_api_key"] = api_key

        # Step 3: Submit to Worker
        print(f"  📤 [{offer_id}] 提交到 Worker...", flush=True)
        submit_result = submit_envelope(envelope)
        result["submit_result"] = submit_result
        result["task_id"] = submit_result.get("task_id", "")
        result["success"] = submit_result.get("ok", False)
        result["error"] = submit_result.get("error", "")

        if result["success"]:
            print(f"  🎉 [{offer_id}] 已提交 task_id={result['task_id']}", flush=True)
        else:
            print(f"  ⚠️ [{offer_id}] 提交失败: {result['error']}", flush=True)

    except Exception as e:
        result["error"] = str(e)
        print(f"  ❌ [{offer_id}] 异常: {e}", flush=True)

    return result


def process_ozon_url(
    url: str,
    product_id: str,
    client_id: str,
    api_key: str,
    worker_url: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Process a single Ozon URL: follow-sell pipeline → submit."""
    from scripts.cloud_probe import follow_sell_cloud, submit_envelope

    result: dict[str, Any] = {
        "type": "ozon",
        "url": url,
        "product_id": product_id,
        "timestamp": _now_iso(),
        "success": False,
    }

    try:
        # Temporarily override env vars for this call
        old_cid = os.environ.get("OZON_CLIENT_ID", "")
        old_akey = os.environ.get("OZON_API_KEY", "")
        os.environ["OZON_CLIENT_ID"] = client_id
        os.environ["OZON_API_KEY"] = api_key

        print(f"  🔗 [{product_id}] 跟卖流程 (Ozon抓图 → 1688搜同款 → 上架)...", flush=True)
        follow_result = follow_sell_cloud(url, auto_submit=not dry_run)

        result["follow_result"] = follow_result
        result["card_copied"] = follow_result.get("card_copied", False)
        result["search_keyword"] = follow_result.get("search_keyword", "")
        result["slug"] = follow_result.get("slug", "")

        matches = follow_result.get("1688_matches", [])
        result["matches_count"] = len(matches)
    except Exception as e:
        result["error"] = str(e)
        print(f"  ❌ [{product_id}] 异常: {e}", flush=True)
        return result
    finally:
        # ✅ 始终恢复环境变量（即使 follow_sell_cloud 异常）
        if old_cid:
            os.environ["OZON_CLIENT_ID"] = old_cid
        else:
            os.environ.pop("OZON_CLIENT_ID", None)
        if old_akey:
            os.environ["OZON_API_KEY"] = old_akey
        else:
            os.environ.pop("OZON_API_KEY", None)

        # ⚠️ v0.14 E7: follow_result/matches 可能在异常时未绑定（finally 引用会 NameError 掩盖原异常）
        # 用 locals().get 安全读取
        follow_result = locals().get("follow_result") or {}
        matches = locals().get("matches") or []

        if not follow_result.get("success"):
            result["error"] = follow_result.get("error", "跟卖流程未找到匹配")
            print(f"  ⚠️ [{product_id}] 跟卖未找到匹配: {result['error']}", flush=True)
            if not dry_run:
                return result

        if matches:
            best = matches[0]
            result["best_match_id"] = best.get("id", "")
            result["best_match_title"] = best.get("title", "")
            print(f"  ✅ [{product_id}] 最佳匹配: {best.get('title', '')[:60]}", flush=True)

        if dry_run:
            result["success"] = follow_result.get("success", False)
            result["dry_run"] = True
            return result

        # Submit mode: result already includes task_id from auto_submit
        result["success"] = follow_result.get("success", False)
        result["task_id"] = follow_result.get("task_id", "")
        if follow_result.get("submit_result"):
            result["submit_result"] = follow_result["submit_result"]

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="批量测试 1688/Ozon URL → Worker 上架"
    )
    parser.add_argument(
        "--urls-file", required=True, help="URL 列表文件（每行一个 URL）"
    )
    parser.add_argument(
        "--worker-url",
        default=os.environ.get("MXOU_API_BASE", "https://worker.mxou.cn"),
        help="Worker 地址",
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("OZON_CLIENT_ID", ""),
        help="Ozon Client ID",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OZON_API_KEY", ""),
        help="Ozon API Key",
    )
    parser.add_argument(
        "--store-id", default="", help="Store profile ID（用于物流费率）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只组装信封，不提交 Worker"
    )
    parser.add_argument(
        "--submit", action="store_true", help="实际提交到 Worker"
    )
    parser.add_argument(
        "--start", type=int, default=0, help="从第 N 个 URL 开始（0-based）"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="最多处理 N 个 URL（0=不限制）"
    )
    parser.add_argument(
        "--delay", type=float, default=3.0, help="每个 URL 之间的延迟秒数"
    )
    parser.add_argument(
        "--type-filter",
        choices=["1688", "ozon", "all"],
        default="all",
        help="只处理特定类型的 URL",
    )

    args = parser.parse_args()

    if args.submit and not args.client_id:
        print("❌ --submit 需要 --client-id 和 --api-key（或设置 OZON_CLIENT_ID / OZON_API_KEY 环境变量）")
        return 1

    # ── Pre-flight check ──
    from scripts.lib.config_store import check_config
    config = check_config()
    cdp = config.get("cdp", {})

    issues = []
    if config.get("missing"):
        issues.append(f"缺少凭证: {', '.join(config['missing'])}")

    # Auto-launch Chrome via chrome_launcher (same as check command)
    _cdp_ok = False
    try:
        from scripts.lib.chrome_launcher import ensure_chrome_cdp
        from pathlib import Path as _P
        _prof = str(_P(__file__).resolve().parent.parent / "data" / "browser" / "profiles" / "1688" / "default")
        ok, msg = ensure_chrome_cdp(auto_restart=True, profile_dir=_prof)
        _cdp_ok = ok
        if not ok:
            issues.append(f"Chrome CDP 启动失败: {msg}")
    except ImportError:
        if not cdp.get("browser_available"):
            issues.append("Chrome 浏览器未安装")
        if not cdp.get("session_available") and not cdp.get("cdp_running"):
            issues.append("CDP Chrome 未启动 (端口 9222)")
            issues.append("→ 启动: Chrome --remote-debugging-port=9222 --remote-allow-origins='*'")

    # 1688 login check via CDP cookies (not session file)
    # ⚠️ v0.14 E4: 用 CdpTab 封装替代手写 websocket（只读检查，不关远程 tab）
    if args.type_filter in ("1688", "all") and _cdp_ok:
        try:
            from scripts.lib.cdp_client import CdpTab
            _tabs = requests.get("http://127.0.0.1:9222/json", timeout=5).json()
            tab = None
            for _t in _tabs:
                if _t.get("type") == "page" and "1688.com" in _t.get("url", ""):
                    tab = CdpTab("http://127.0.0.1:9222", _t.get("id", ""), _t.get("webSocketDebuggerUrl", ""))
                    break
            if tab:
                val = tab.evaluate(
                    "document.cookie.match(/cookie2=|__cn_logon__=/) ? 'LOGGED_IN' : 'NOT_LOGGED_IN'",
                    timeout=8,
                )
                if val != "LOGGED_IN":
                    issues.append("1688 未登录 (仅影响 1688 URL)")
                    issues.append("→ 请在 Chrome 中登录 https://login.1688.com/")
                tab.close(close_remote=False)  # 只关 WS，保留用户 1688 标签页
        except Exception:
            pass  # Non-critical, actual probe will catch it

    # Check Ozon DataDome trust
    # ⚠️ v0.14 E4: 用 CdpConnection 封装替代手写 websocket（新建 tab 检查后全关）
    if args.type_filter in ("ozon", "all") and cdp.get("session_available"):
        try:
            from scripts.lib.cdp_client import CdpConnection
            conn = CdpConnection("http://127.0.0.1:9222")
            tab = conn.new_tab("https://www.ozon.ru/")
            tab.wait_for_load(timeout=10)
            val = tab.evaluate(
                "!!document.body && document.body.innerText.includes('OZON')",
                timeout=8,
            )
            if not val:
                issues.append("Ozon 被 DataDome 拦截！需先在 Chrome 中访问 ozon.ru")
                issues.append("→ 打开 https://www.ozon.ru/ 浏览一个商品即可建立信任")
            tab.close()  # 新建 tab → 全关，不残留
            conn.close()
        except Exception:
            pass

    if issues:
        print("⚠️ 前置条件检查发现问题:")
        for issue in issues:
            print(f"  • {issue}")
        print("\n运行 python3 scripts/cli.py check 查看详细诊断")
        if not args.dry_run:
            print("提示: 使用 --dry-run 可以先试跑不提交")
        if any("CDP" in i or "Chrome" in i for i in issues):
            print("\n❌ CDP Chrome 问题会阻止所有 1688 抓取和 Ozon 跟卖")
            print("   只有 Ozon URL 的 1688 AK 搜索不受影响")
            # Don't exit - let user continue with what works
    else:
        print("✅ 前置条件检查通过\n")

    # Parse URLs
    print(f"📖 读取 {args.urls_file}...")
    all_urls = parse_urls_file(args.urls_file)
    print(f"   总计 {len(all_urls)} 个唯一 URL")

    # Filter by type
    if args.type_filter != "all":
        all_urls = [u for u in all_urls if u["type"] == args.type_filter]
        print(f"   过滤后 ({args.type_filter}): {len(all_urls)} 个")

    # Apply start/limit
    urls = all_urls[args.start :]
    if args.limit > 0:
        urls = urls[: args.limit]

    if not urls:
        print("❌ 没有要处理的 URL")
        return 1

    print(f"📋 本批处理: {len(urls)} 个 URL (start={args.start}, limit={args.limit or 'all'})")
    if args.dry_run:
        print("🔍 模式: DRY RUN (只组装信封，不提交)")
    elif args.submit:
        print(f"🚀 模式: 实际提交到 {args.worker_url}")
        print(f"   Client ID: {args.client_id}")
    else:
        print("⚠️  模式: 既不 --dry-run 也不 --submit，不会做任何事")
        print("   请添加 --dry-run (试跑) 或 --submit (提交)")
        return 1

    # Output log file
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = OUTPUT_DIR / f"batch_{ts}.json"
    summary_file = OUTPUT_DIR / f"batch_{ts}_summary.json"

    results: list[dict[str, Any]] = []
    stats = {"total": len(urls), "success": 0, "failed": 0, "skipped": 0}

    print(f"\n{'='*60}")
    print(f"开始处理 {len(urls)} 个 URL...")
    print(f"{'='*60}\n")

    for i, item in enumerate(urls):
        idx = args.start + i + 1  # 1-based for display
        url_type = item["type"]
        url = item["url"]
        uid = item["id"]

        print(f"[{idx}/{args.start + len(urls)}] {url_type.upper()} {uid}", flush=True)

        if url_type == "1688":
            r = process_1688_url(
                url=url,
                offer_id=uid,
                client_id=args.client_id,
                api_key=args.api_key,
                worker_url=args.worker_url,
                dry_run=args.dry_run,
                store_id=args.store_id,
            )
        else:
            r = process_ozon_url(
                url=url,
                product_id=uid,
                client_id=args.client_id,
                api_key=args.api_key,
                worker_url=args.worker_url,
                dry_run=args.dry_run,
            )

        results.append(r)
        if r.get("success"):
            stats["success"] += 1
        else:
            stats["failed"] += 1

        # ⚠️ v0.14 E7: 移除循环内全量覆写（O(n²) 写入）— 改为每 5 条增量落盘一次，
        # 最终 summary 阶段完整写一次。崩溃时最多丢最近 5 条，而非全量重写 N 次。
        if (i + 1) % 5 == 0:
            log_file.write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        # Delay between URLs
        if i < len(urls) - 1:
            time.sleep(args.delay)

    # Final summary
    summary = {
        "timestamp": _now_iso(),
        "config": {
            "worker_url": args.worker_url,
            "client_id": args.client_id[:8] + "***" if args.client_id else "",
            "dry_run": args.dry_run,
            "type_filter": args.type_filter,
            "start": args.start,
            "limit": args.limit,
        },
        "stats": stats,
        "results": [
            {
                "type": r["type"],
                "id": r.get("offer_id") or r.get("product_id"),
                "title": r.get("title", r.get("best_match_title", ""))[:80],
                "success": r["success"],
                "task_id": r.get("task_id", ""),
                "error": r.get("error", "")[:200],
            }
            for r in results
        ],
    }
    # ⚠️ v0.14 E7: 循环结束后完整写一次 log_file（增量每 5 条 + 此处兜底，保证全量落盘）
    log_file.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary_file.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n{'='*60}")
    print(f"📊 结果:")
    print(f"   成功: {stats['success']}")
    print(f"   失败: {stats['failed']}")
    print(f"   详情: {log_file}")
    print(f"   汇总: {summary_file}")
    print(f"{'='*60}")

    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
