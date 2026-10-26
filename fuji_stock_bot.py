"""
富士相機到貨追蹤機器人
監控 myfuji.com.tw 商品頁，任一顏色有貨時推播到 LINE 聊天室（LINE Messaging API）。

用法：
    python fuji_stock_bot.py            # 持續監控（預設每 5 分鐘）
    python fuji_stock_bot.py --once     # 只檢查一次（給 Windows 工作排程器用）
    python fuji_stock_bot.py --test     # 發一則測試訊息到 LINE

需要的環境變數（或寫在同資料夾的 config.json）：
    LINE_CHANNEL_ACCESS_TOKEN   Messaging API channel 的長期 access token
    LINE_TO                     要推播的 userId / groupId / roomId
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

PRODUCT_URL = "https://myfuji.com.tw/product/fujifilm-x-m5xc15-45mm-kit/"
API_BASE = "https://myfuji.com.tw/wp-json/wc/store/v1"
CHECK_INTERVAL_SEC = 300
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "stock_state.json"
CONFIG_FILE = BASE_DIR / "config.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_config():
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or cfg.get("LINE_CHANNEL_ACCESS_TOKEN")
    to = os.environ.get("LINE_TO") or cfg.get("LINE_TO")
    if not token or not to:
        sys.exit("缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_TO，請設定環境變數或 config.json")
    return token, to


def fetch_page(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-TW"})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == retries:
                raise
            log(f"抓取失敗（第 {attempt} 次）：{e}，10 秒後重試")
            time.sleep(10)


def parse_stock(page):
    """回傳 {變體名稱: 是否有貨}。"""
    m = re.search(r'data-product_variations="([^"]*)"', page)
    if m and m.group(1) not in ("", "false"):
        variations = json.loads(html.unescape(m.group(1)))
        result = {}
        for v in variations:
            name = " / ".join(str(x) for x in v.get("attributes", {}).values()) or str(v.get("variation_id"))
            result[name] = bool(v.get("is_in_stock")) and bool(v.get("is_purchasable", True))
        return result
    # 備援：單一商品，看 schema.org availability 或 product class
    if "schema.org\\/InStock" in page or "schema.org/InStock" in page:
        return {"商品": True}
    m = re.search(r'<div id="product-\d+" class="([^"]*)"', page)
    if m:
        return {"商品": " instock " in f" {m.group(1)} "}
    raise RuntimeError("無法解析庫存狀態，網頁結構可能已改變")


def stock_from_api():
    """透過 WooCommerce Store API 取得 {變體名稱: 是否有貨}。"""
    slug = PRODUCT_URL.rstrip("/").rsplit("/", 1)[-1]
    products = json.loads(fetch_page(f"{API_BASE}/products?slug={slug}"))
    if not products:
        raise RuntimeError(f"API 找不到商品 {slug}")
    product = products[0]
    if not product.get("variations"):
        return {"商品": bool(product.get("is_in_stock"))}
    result = {}
    for v in product["variations"]:
        name = " / ".join(a["value"] for a in v.get("attributes", [])) or str(v["id"])
        detail = json.loads(fetch_page(f"{API_BASE}/products/{v['id']}"))
        result[name] = bool(detail.get("is_in_stock")) and bool(detail.get("is_purchasable", True))
    return result


def describe(text):
    """失敗時印出網站實際回傳的內容摘要，方便判斷是否被擋。"""
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:300]
    return f"長度={len(text)} 標題={title.group(1).strip() if title else '無'} 內容={snippet}"


def get_stock():
    try:
        return stock_from_api()
    except Exception as e:
        log(f"API 讀取失敗：{e}，改用商品頁")
    page = fetch_page(PRODUCT_URL)
    try:
        return parse_stock(page)
    except RuntimeError:
        log("商品頁回傳內容：" + describe(page))
        raise


def push_line(token, to, text):
    body = json.dumps({"to": to, "messages": [{"type": "text", "text": text}]}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"LINE 推播失敗：HTTP {resp.status}")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def check_once(token, to):
    stock = get_stock()
    log("庫存：" + "，".join(f"{k}={'有貨' if v else '缺貨'}" for k, v in stock.items()))

    prev = load_state()
    # 只在「缺貨 → 有貨」時通知，避免重複洗版
    newly = [k for k, v in stock.items() if v and not prev.get(k, False)]
    if newly:
        text = (
            "📷 富士 X-M5 + XC15-45mm Kit 到貨啦！\n"
            f"有貨顏色：{'、'.join(newly)}\n"
            f"{PRODUCT_URL}"
        )
        push_line(token, to, text)
        log(f"已通知 LINE：{newly}")
    STATE_FILE.write_text(json.dumps(stock, ensure_ascii=False), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只檢查一次")
    ap.add_argument("--test", action="store_true", help="發送測試訊息")
    ap.add_argument("--interval", type=int, default=CHECK_INTERVAL_SEC, help="檢查間隔秒數")
    args = ap.parse_args()

    token, to = load_config()
    if args.test:
        push_line(token, to, "✅ 富士到貨機器人測試訊息")
        log("測試訊息已送出")
        return
    if args.once:
        check_once(token, to)
        return

    log(f"開始監控，每 {args.interval} 秒檢查一次")
    while True:
        try:
            check_once(token, to)
        except Exception as e:
            log(f"錯誤：{e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
