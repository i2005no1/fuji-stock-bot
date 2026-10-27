"""
富士相機到貨追蹤機器人
監控 myfuji.com.tw、PChome、momo 商品，任一商品／顏色有貨時推播到 LINE 聊天室（LINE Messaging API）。

用法：
    python fuji_stock_bot.py            # 持續監控（預設每 5 分鐘）
    python fuji_stock_bot.py --once     # 只檢查一次（給 Windows 工作排程器用）
    python fuji_stock_bot.py --test     # 發一則測試訊息到 LINE

需要的環境變數（或寫在同資料夾的 config.json）：
    LINE_CHANNEL_ACCESS_TOKEN   Messaging API channel 的長期 access token
    LINE_TO                     all = 廣播給所有好友；或 userId / groupId（多個用逗號分隔）
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
from urllib.parse import urlparse

# 要追蹤的商品頁網址，想加新商品就在這裡多加一行（支援 myfuji、PChome 24h、momo）
PRODUCT_URLS = [
    "https://myfuji.com.tw/product/fujifilm-x-m5xc15-45mm-kit/",
    "https://myfuji.com.tw/product/fujifilm-x-t30iiixc13-33mm-kit/",
    # RICOH GR IIIx 公司貨
    "https://24h.pchome.com.tw/prod/DGADJN-1900JXSR9",   # 標準版
    "https://24h.pchome.com.tw/prod/DGADJN-1900K6GUY",   # 標準版 + GF-2 閃光燈組
    "https://24h.pchome.com.tw/prod/DGADLR-1900KHK6N",   # HDF 全配組
    "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=13432322",  # +Q20III 閃光燈組
    "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=12985987",  # HDF +Q20III 閃光燈組
]
API_BASE = "https://myfuji.com.tw/wp-json/wc/store/v1"
PCHOME_API = "https://ecapi-cdn.pchome.com.tw/ecshop/prodapi/v2/prod"
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


def slug_of(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


def stock_from_api(url):
    """透過 WooCommerce Store API 取得 (商品名稱, {變體名稱: 是否有貨})。"""
    slug = slug_of(url)
    products = json.loads(fetch_page(f"{API_BASE}/products?slug={slug}"))
    if not products:
        raise RuntimeError(f"API 找不到商品 {slug}")
    product = products[0]
    name = html.unescape(product.get("name") or slug)
    if not product.get("variations"):
        return name, {"商品": bool(product.get("is_in_stock"))}
    result = {}
    for v in product["variations"]:
        vname = " / ".join(a["value"] for a in v.get("attributes", [])) or str(v["id"])
        detail = json.loads(fetch_page(f"{API_BASE}/products/{v['id']}"))
        result[vname] = bool(detail.get("is_in_stock")) and bool(detail.get("is_purchasable", True))
    return name, result


def describe(text):
    """失敗時印出網站實際回傳的內容摘要，方便判斷是否被擋。"""
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:300]
    return f"長度={len(text)} 標題={title.group(1).strip() if title else '無'} 內容={snippet}"


def stock_from_pchome(url):
    """PChome 24h：ButtonType 為 ForSale 才是可下單，OrderRefill 是補貨中。"""
    pid = slug_of(url)
    button = json.loads(fetch_page(f"{PCHOME_API}/button&id={pid}&fields=Id,Qty,ButtonType"))[0]
    name = f"PChome {pid}"
    try:
        # 商品名稱 API 只有 JSONP 格式穩定有回應
        text = fetch_page(f"{PCHOME_API}/{pid}&fields=Id,Name&_callback=jsonp")
        info = json.loads(re.search(r"jsonp\((.*)\);\}catch", text, re.S).group(1))
        name = next(iter(info.values()))["Name"]
    except Exception as e:
        log(f"PChome 商品名稱讀取失敗：{e}")
    return name, {"商品": button.get("ButtonType") == "ForSale"}


def stock_from_momo(url):
    """momo：商品頁的 product:availability 標記為 in stock / out of stock。"""
    page = fetch_page(url)
    m = re.search(r'<meta name="product:availability" content="([^"]*)"', page)
    if not m:
        log("momo 商品頁回傳內容：" + describe(page))
        raise RuntimeError("無法解析 momo 庫存狀態，網頁結構可能已改變")
    t = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    name = html.unescape(t.group(1)).split(" - momo")[0] if t else f"momo {url.rsplit('=', 1)[-1]}"
    return name, {"商品": m.group(1) == "in stock"}


def get_stock(url):
    host = urlparse(url).netloc
    if host.endswith("pchome.com.tw"):
        return stock_from_pchome(url)
    if host.endswith("momoshop.com.tw"):
        return stock_from_momo(url)
    try:
        return stock_from_api(url)
    except Exception as e:
        log(f"API 讀取失敗：{e}，改用商品頁")
    page = fetch_page(url)
    try:
        return slug_of(url), parse_stock(page)
    except RuntimeError:
        log("商品頁回傳內容：" + describe(page))
        raise


def push_line(token, to, text):
    """LINE_TO 設為 all 時廣播給所有好友；多個 ID 用逗號分隔；否則推播給單一對象。"""
    messages = [{"type": "text", "text": text}]
    ids = [x.strip() for x in to.split(",") if x.strip()]
    if to.strip().lower() == "all":
        endpoint, payload = "broadcast", {"messages": messages}
    elif len(ids) > 1:
        endpoint, payload = "multicast", {"to": ids, "messages": messages}
    else:
        endpoint, payload = "push", {"to": to.strip(), "messages": messages}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.line.me/v2/bot/message/{endpoint}",
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
    prev = load_state()
    state = {}
    errors = []
    for url in PRODUCT_URLS:
        old = prev.get(url) if isinstance(prev.get(url), dict) else {}
        try:
            name, stock = get_stock(url)
        except Exception as e:
            log(f"{slug_of(url)} 檢查失敗：{e}")
            errors.append(url)
            state[url] = old  # 保留上次狀態
            continue
        log(f"{name}：" + "，".join(f"{k}={'有貨' if v else '缺貨'}" for k, v in stock.items()))

        # 只在「缺貨 → 有貨」時通知，避免重複洗版
        newly = [k for k, v in stock.items() if v and not old.get(k, False)]
        if newly:
            colors = "" if newly == ["商品"] else f"有貨顏色：{'、'.join(newly)}\n"
            push_line(token, to, f"📷 {name} 到貨啦！\n{colors}{url}")
            log(f"已通知 LINE：{name} {newly}")
        state[url] = stock

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    if errors:
        raise RuntimeError(f"{len(errors)} 個商品檢查失敗")


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
