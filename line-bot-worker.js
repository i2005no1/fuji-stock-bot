/**
 * 富士到貨機器人 — LINE 指令回覆（Cloudflare Worker）
 *
 * 在 LINE 輸入「查詢」「庫存」「狀態」→ 回覆目前所有追蹤商品的庫存
 * 輸入「我的ID」→ 回覆自己的 userId（在群組裡另外附上 groupId），可填進 LINE_TO
 * 輸入「說明」→ 回覆指令列表
 *
 * 需要在 Cloudflare 設定兩個 Secret：
 *   LINE_CHANNEL_ACCESS_TOKEN   Messaging API 分頁的 Channel access token
 *   LINE_CHANNEL_SECRET         Basic settings 分頁的 Channel secret（驗證訊息確實來自 LINE）
 */

// 追蹤的商品清單直接讀 GitHub 上的 stock_state.json，Python 腳本加商品這裡就會自動跟上
const STATE_URL = "https://raw.githubusercontent.com/i2005no1/fuji-stock-bot/main/stock_state.json";
const API_BASE = "https://myfuji.com.tw/wp-json/wc/store/v1";
const PCHOME_API = "https://ecapi-cdn.pchome.com.tw/ecshop/prodapi/v2/prod";
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";

const STATUS_CMDS = ["查詢", "庫存", "狀態", "status"];
const HELP_CMDS = ["說明", "指令", "help"];
const ID_CMDS = ["我的id", "id", "myid"];
const HELP_TEXT = "📷 富士到貨機器人\n\n指令：\n・查詢／庫存／狀態：查看目前庫存\n・我的ID：查看你的 LINE User ID\n・說明：顯示這段說明\n\n有貨時會自動通知你 🔔";

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
  } else if (ID_CMDS.includes(text.replace(/\s+/g, ""))) {
    reply = formatIds(event.source);
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

function formatIds(source) {
  const lines = [`🆔 你的 User ID：\n${source.userId || "（LINE 沒有提供，請改在一對一聊天輸入）"}`];
  if (source.groupId) lines.push(`👥 這個群組的 Group ID：\n${source.groupId}`);
  if (source.roomId) lines.push(`👥 這個聊天室的 Room ID：\n${source.roomId}`);
  return lines.join("\n\n");
}

async function buildStatus() {
  const state = await getJson(STATE_URL);
  const sections = await Promise.all(
    Object.keys(state).map(async (url) => {
      try {
        const { name, stock, qty } = await liveStock(url);
        return formatProduct(name, stock, url, qty);
      } catch {
        // 網站擋 Cloudflare 或暫時連不上時，改用 GitHub 排程最近一次的檢查結果
        return formatProduct(labelOf(url), state[url], url) + "\n（網站暫時連不上，顯示 5 分鐘內的檢查結果）";
      }
    })
  );
  const now = new Date().toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false });
  return `📷 富士商品庫存\n🕒 ${now}\n\n${sections.join("\n\n")}`;
}

async function liveStock(url) {
  const host = new URL(url).hostname;
  if (host.endsWith("pchome.com.tw")) return pchomeStock(url);
  if (host.endsWith("momoshop.com.tw")) return momoStock(url);

  const [product] = await getJson(`${API_BASE}/products?slug=${slugOf(url)}`);
  if (!product) throw new Error("找不到商品");
  const name = decodeEntities(product.name);
  if (!product.variations?.length) {
    return { name, stock: { 商品: !!product.is_in_stock }, qty: { 商品: stockQty(product) } };
  }

  const entries = await Promise.all(
    product.variations.map(async (v) => {
      const detail = await getJson(`${API_BASE}/products/${v.id}`);
      const vname = v.attributes.map((a) => a.value).join(" / ") || String(v.id);
      return [vname, !!detail.is_in_stock && detail.is_purchasable !== false, stockQty(detail)];
    })
  );
  return {
    name,
    stock: Object.fromEntries(entries.map(([k, inStock]) => [k, inStock])),
    qty: Object.fromEntries(entries.map(([k, , n]) => [k, n])),
  };
}

// PChome 24h：ButtonType 為 ForSale 才是可下單，OrderRefill 是補貨中
async function pchomeStock(url) {
  const id = slugOf(url);
  const [button] = await getJson(`${PCHOME_API}/button&id=${id}&fields=Id,Qty,ButtonType`);
  const inStock = button.ButtonType === "ForSale";
  let name = labelOf(url);
  try {
    // 商品名稱 API 只有 JSONP 格式穩定有回應
    const res = await fetch(`${PCHOME_API}/${id}&fields=Id,Name&_callback=jsonp`, { headers: { "User-Agent": UA } });
    const info = JSON.parse((await res.text()).match(/jsonp\((.*)\);\}catch/s)[1]);
    name = Object.values(info)[0].Name;
  } catch {}
  return { name, stock: { 商品: inStock }, qty: { 商品: inStock && button.Qty > 0 ? button.Qty : null } };
}

// momo：商品頁的 product:availability 標記為 in stock / out of stock
async function momoStock(url) {
  const res = await fetch(url, { headers: { "User-Agent": UA, "Accept-Language": "zh-TW" } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const page = await res.text();
  const avail = page.match(/<meta name="product:availability" content="([^"]*)"/);
  if (!avail) throw new Error("無法解析 momo 庫存");
  const title = page.match(/<meta property="og:title" content="([^"]*)"/);
  const name = title ? decodeEntities(title[1]).split(" - momo")[0] : labelOf(url);
  return { name, stock: { 商品: avail[1] === "in stock" }, qty: {} };
}

// 官網沒直接公開庫存數，但有管理庫存時「加入購物車上限」會等於剩餘數量（未設定時是 9999）
function stockQty(p) {
  if (!p.is_in_stock) return null;
  if (p.low_stock_remaining > 0) return p.low_stock_remaining;
  const max = p.add_to_cart?.maximum;
  return max > 0 && max < 9999 ? max : null;
}

function formatProduct(name, stock, url, qty = {}) {
  const lines = Object.entries(stock).map(([k, v]) => {
    const count = v && qty[k] ? `（剩 ${qty[k]} 台）` : "";
    return `${v ? "✅" : "❌"} ${k}：${v ? "有貨" : "缺貨"}${count}`;
  });
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

// 連不上網站時顯示的商品代稱
function labelOf(url) {
  const { hostname, searchParams } = new URL(url);
  if (hostname.endsWith("pchome.com.tw")) return `PChome ${slugOf(url)}`;
  if (hostname.endsWith("momoshop.com.tw")) return `momo ${searchParams.get("i_code")}`;
  return slugOf(url);
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
