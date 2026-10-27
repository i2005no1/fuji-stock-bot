/**
 * 富士到貨機器人 — LINE 指令回覆（Cloudflare Worker）
 *
 * 在 LINE 輸入「查詢」「庫存」「狀態」→ 回覆目前所有追蹤商品的庫存
 * 輸入「說明」→ 回覆指令列表
 *
 * 需要在 Cloudflare 設定兩個 Secret：
 *   LINE_CHANNEL_ACCESS_TOKEN   Messaging API 分頁的 Channel access token
 *   LINE_CHANNEL_SECRET         Basic settings 分頁的 Channel secret（驗證訊息確實來自 LINE）
 */

// 追蹤的商品清單直接讀 GitHub 上的 stock_state.json，Python 腳本加商品這裡就會自動跟上
const STATE_URL = "https://raw.githubusercontent.com/i2005no1/fuji-stock-bot/main/stock_state.json";
const API_BASE = "https://myfuji.com.tw/wp-json/wc/store/v1";
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";

const STATUS_CMDS = ["查詢", "庫存", "狀態", "status"];
const HELP_CMDS = ["說明", "指令", "help"];
const HELP_TEXT = "📷 富士到貨機器人\n\n指令：\n・查詢／庫存／狀態：查看目前庫存\n・說明：顯示這段說明\n\n有貨時會自動通知你 🔔";

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("Fuji stock bot is running");

    const body = await request.text();
    if (!(await verifySignature(body, request.headers.get("x-line-signature"), env.LINE_CHANNEL_SECRET))) {
      return new Response("Invalid signature", { status: 401 });
    }

    const events = JSON.parse(body).events || [];
    // 先回 200 給 LINE，查庫存在背景做
    ctx.waitUntil(Promise.all(events.map((e) => handleEvent(e, env))));
    return new Response("OK");
  },
};

async function handleEvent(event, env) {
  if (event.type !== "message" || event.message.type !== "text") return;
  const text = event.message.text.trim().toLowerCase();

  let reply;
  if (STATUS_CMDS.includes(text)) {
    try {
      reply = await buildStatus();
    } catch (e) {
      reply = `⚠️ 查詢失敗：${e.message}\n請稍後再試`;
    }
  } else if (HELP_CMDS.includes(text) || event.source.type === "user") {
    // 一對一聊天輸入其他文字就回說明；群組裡則不理會，避免干擾聊天
    reply = HELP_TEXT;
  } else {
    return;
  }

  await fetch("https://api.line.me/v2/bot/message/reply", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}`,
    },
    body: JSON.stringify({ replyToken: event.replyToken, messages: [{ type: "text", text: reply }] }),
  });
}

async function buildStatus() {
  const state = await getJson(STATE_URL);
  const sections = await Promise.all(
    Object.keys(state).map(async (url) => {
      try {
        const { name, stock } = await liveStock(url);
        return formatProduct(name, stock, url);
      } catch {
        // 官網擋 Cloudflare 或暫時連不上時，改用 GitHub 排程最近一次的檢查結果
        return formatProduct(slugOf(url), state[url], url) + "\n（官網暫時連不上，顯示 5 分鐘內的檢查結果）";
      }
    })
  );
  const now = new Date().toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false });
  return `📷 富士商品庫存\n🕒 ${now}\n\n${sections.join("\n\n")}`;
}

async function liveStock(url) {
  const [product] = await getJson(`${API_BASE}/products?slug=${slugOf(url)}`);
  if (!product) throw new Error("找不到商品");
  const name = decodeEntities(product.name);
  if (!product.variations?.length) return { name, stock: { 商品: !!product.is_in_stock } };

  const entries = await Promise.all(
    product.variations.map(async (v) => {
      const detail = await getJson(`${API_BASE}/products/${v.id}`);
      const vname = v.attributes.map((a) => a.value).join(" / ") || String(v.id);
      return [vname, !!detail.is_in_stock && detail.is_purchasable !== false];
    })
  );
  return { name, stock: Object.fromEntries(entries) };
}

function formatProduct(name, stock, url) {
  const lines = Object.entries(stock).map(([k, v]) => `${v ? "✅" : "❌"} ${k}：${v ? "有貨" : "缺貨"}`);
  const anyInStock = Object.values(stock).some(Boolean);
  return `【${name}】\n${lines.join("\n")}${anyInStock ? `\n👉 ${url}` : ""}`;
}

async function getJson(url) {
  const res = await fetch(url, { headers: { "User-Agent": UA, Accept: "application/json" } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json(); // 被防火牆擋時回傳的是 HTML，這裡會丟錯而走備援
}

function slugOf(url) {
  return url.replace(/\/+$/, "").split("/").pop();
}

function decodeEntities(s) {
  return s
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
    .replace(/&quot;/g, '"')
    .replace(/&#039;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

async function verifySignature(body, signature, secret) {
  if (!signature || !secret) return false;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  let sigBytes;
  try {
    sigBytes = Uint8Array.from(atob(signature), (c) => c.charCodeAt(0));
  } catch {
    return false;
  }
  return crypto.subtle.verify("HMAC", key, sigBytes, enc.encode(body));
}
