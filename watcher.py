#!/usr/bin/env python3
"""
Hyperliquid 多地址監控 → LINE 推播
偵測事件：開倉 / 平倉 / 反手（方向翻轉）
不逐筆推 fill，改用持倉快照差分，一次動作只發一則。

HL_ADDRESSES 格式：逗號分隔，每項 `地址` 或 `地址=標籤`
"""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

HL_API = "https://api.hyperliquid.xyz/info"
LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"
TZ = timezone(timedelta(hours=8))

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_USER_ID = os.environ.get("LINE_USER_ID", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MIN_NOTIONAL = float(os.environ.get("MIN_NOTIONAL", "5000"))  # 低於此名目視為灰塵，忽略


def parse_targets():
    raw = os.environ.get("HL_ADDRESSES", "") or os.environ.get("HL_ADDRESS", "")
    targets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            addr, label = item.split("=", 1)
        else:
            addr, label = item, ""
        addr = addr.strip().lower()
        label = label.strip() or (addr[:6] + "…" + addr[-4:])
        targets.append((addr, label))
    return targets


def post_json(url, payload, headers=None, timeout=30):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body.strip() else {}


def hl_info(payload, retries=3):
    for i in range(retries):
        try:
            return post_json(HL_API, payload)
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"[warn] HL API 重試 {i+1}/{retries}: {e}", file=sys.stderr)
    return None


def fetch_state(addr):
    d = hl_info({"type": "clearinghouseState", "user": addr})
    ms = d.get("marginSummary", {})
    positions = {}
    for ap in d.get("assetPositions", []):
        p = ap["position"]
        szi = float(p["szi"])
        nv = float(p["positionValue"])
        if szi == 0 or nv < MIN_NOTIONAL:
            continue
        positions[p["coin"]] = {
            "szi": szi,
            "side": "LONG" if szi > 0 else "SHORT",
            "entryPx": float(p["entryPx"]),
            "notional": nv,
            "uPnl": float(p["unrealizedPnl"]),
            "lev": float(p["leverage"]["value"]),
            "liqPx": float(p["liquidationPx"]) if p.get("liquidationPx") else None,
        }
    summary = {
        "accountValue": float(ms.get("accountValue", 0)),
        "totalNtlPos": float(ms.get("totalNtlPos", 0)),
    }
    return positions, summary


def fetch_closed_pnl(addr, coin, since_ms):
    try:
        fills = hl_info({"type": "userFills", "user": addr, "aggregateByTime": True})
    except Exception:
        return None
    total = 0.0
    hit = False
    for f in fills or []:
        if f.get("coin") != coin:
            continue
        if since_ms and f.get("time", 0) < since_ms:
            continue
        d = f.get("dir", "")
        if "Close" in d or "Long > Short" in d or "Short > Long" in d:
            total += float(f.get("closedPnl", 0) or 0)
            hit = True
    return total if hit else None


def money(v):
    a = abs(v)
    s = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{s}${a/1e9:.2f}B"
    if a >= 1e6:
        return f"{s}${a/1e6:.2f}M"
    if a >= 1e3:
        return f"{s}${a/1e3:.1f}K"
    return f"{s}${a:.0f}"


def qty(v):
    a = abs(v)
    if a >= 1000:
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if a >= 1:
        return f"{v:,.3f}".rstrip("0").rstrip(".")
    return f"{v:.6f}".rstrip("0").rstrip(".")


def px(v):
    if v is None:
        return "-"
    return f"{v:,.4f}".rstrip("0").rstrip(".") if v < 10 else f"{v:,.2f}"


def load_state(targets):
    """讀狀態檔；舊的單地址格式自動遷移到第一個地址底下"""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[warn] 狀態檔讀取失敗，視為首次執行: {e}", file=sys.stderr)
        return {}
    if "addresses" in raw:
        return raw["addresses"]
    if "positions" in raw and targets:
        first = targets[0][0]
        print(f"[migrate] 舊格式狀態檔 → 歸到 {first[:10]}，避免誤報開倉")
        return {first: {"ts": raw.get("ts"), "positions": raw["positions"],
                        "summary": raw.get("summary", {})}}
    return {}


def save_state(addresses):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"addresses": addresses}, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def diff_events(addr, old_pos, new_pos, last_ts):
    events = []
    for coin, n in new_pos.items():
        o = old_pos.get(coin)
        if o is None:
            events.append({"kind": "OPEN", "coin": coin, "new": n})
        elif (o["szi"] > 0) != (n["szi"] > 0):
            events.append({"kind": "FLIP", "coin": coin, "old": o, "new": n,
                           "pnl": fetch_closed_pnl(addr, coin, last_ts)})
    for coin, o in old_pos.items():
        if coin not in new_pos:
            events.append({"kind": "CLOSE", "coin": coin, "old": o,
                           "pnl": fetch_closed_pnl(addr, coin, last_ts)})
    return events


def render_events(events):
    out = []
    for e in events:
        c = e["coin"]
        if e["kind"] == "OPEN":
            n = e["new"]
            arrow = "🟢 開多" if n["side"] == "LONG" else "🔴 開空"
            out.append(
                f"{arrow} {c}\n"
                f"　數量 {qty(n['szi'])}｜名目 {money(n['notional'])}\n"
                f"　進場 {px(n['entryPx'])}｜{n['lev']:.0f}x｜強平 {px(n['liqPx'])}"
            )
        elif e["kind"] == "CLOSE":
            o = e["old"]
            pnl = e.get("pnl")
            extra = f"\n　已實現 {money(pnl)}" if pnl is not None else ""
            out.append(
                f"⚪ 平倉 {c}（原 {o['side']}）\n"
                f"　原名目 {money(o['notional'])}｜進場 {px(o['entryPx'])}{extra}"
            )
        elif e["kind"] == "FLIP":
            o, n = e["old"], e["new"]
            pnl = e.get("pnl")
            extra = f"｜前倉已實現 {money(pnl)}" if pnl is not None else ""
            out.append(
                f"🔁 反手 {c}：{o['side']} → {n['side']}\n"
                f"　數量 {qty(n['szi'])}｜名目 {money(n['notional'])}\n"
                f"　進場 {px(n['entryPx'])}｜{n['lev']:.0f}x{extra}"
            )
    return out


def line_push(text):
    if DRY_RUN or not LINE_TOKEN or not LINE_USER_ID:
        print("[dry-run] 不推播，訊息內容：\n" + text + "\n" + "-" * 40)
        return
    try:
        post_json(
            LINE_PUSH_API,
            {"to": LINE_USER_ID, "messages": [{"type": "text", "text": text[:4900]}]},
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
        )
        print("[ok] LINE 已推播")
    except urllib.error.HTTPError as e:
        print(f"[error] LINE 推播失敗 {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
        sys.exit(1)


def main():
    targets = parse_targets()
    if not targets:
        print("[fatal] 未設定 HL_ADDRESSES", file=sys.stderr)
        sys.exit(1)

    prev = load_state(targets)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    new_state = dict(prev)

    blocks, intro, failed = [], [], []

    for addr, label in targets:
        try:
            positions, summary = fetch_state(addr)
        except Exception as e:
            print(f"[error] {label} 抓取失敗: {e}", file=sys.stderr)
            failed.append(label)
            continue

        old = prev.get(addr)
        if old is None:
            desc = "、".join(
                f"{c} {p['side']} {money(p['notional'])}" for c, p in positions.items()
            ) or "無持倉"
            intro.append(f"◆ {label}\n　{desc}\n　淨值 {money(summary['accountValue'])}")
        else:
            evs = diff_events(addr, old.get("positions", {}), positions, old.get("ts"))
            if evs:
                body = "\n\n".join(render_events(evs))
                blocks.append(
                    f"◆ {label}\n{body}\n　└ 淨值 {money(summary['accountValue'])}"
                    f"｜總名目 {money(summary['totalNtlPos'])}"
                )

        new_state[addr] = {"ts": now_ms, "positions": positions, "summary": summary}

    now = datetime.now(TZ).strftime("%m/%d %H:%M")
    if intro:
        line_push(f"【HL 監控 · 新增追蹤】{now}\n\n" + "\n\n".join(intro)
                  + "\n\n之後有開倉／平倉／反手才會再通知。")
    if blocks:
        line_push(f"【HL 監控】{now}\n\n" + "\n\n".join(blocks))
    if not intro and not blocks:
        print(f"[ok] 無變動（{len(targets)} 個地址）")
    if failed:
        print(f"[warn] 本次未取得：{', '.join(failed)}（狀態保留，下次重試）", file=sys.stderr)

    save_state(new_state)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Hyperliquid 地址監控 → LINE 推播
偵測事件：開倉 / 平倉 / 反手（方向翻轉）
不逐筆推 fill，改用持倉快照差分，一次動作只發一則。
"""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

HL_API = "https://api.hyperliquid.xyz/info"
LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"
TZ = timezone(timedelta(hours=8))

ADDRESS = os.environ.get("HL_ADDRESS", "").strip().lower()
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_USER_ID = os.environ.get("LINE_USER_ID", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")


def post_json(url, payload, headers=None, timeout=30):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body.strip() else {}


def hl_info(payload, retries=3):
    for i in range(retries):
        try:
            return post_json(HL_API, payload)
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"[warn] HL API 重試 {i+1}/{retries}: {e}", file=sys.stderr)
    return None


def fetch_state():
    """回傳 (positions dict, account summary)"""
    d = hl_info({"type": "clearinghouseState", "user": ADDRESS})
    ms = d.get("marginSummary", {})
    positions = {}
    for ap in d.get("assetPositions", []):
        p = ap["position"]
        szi = float(p["szi"])
        if szi == 0:
            continue
        positions[p["coin"]] = {
            "szi": szi,
            "side": "LONG" if szi > 0 else "SHORT",
            "entryPx": float(p["entryPx"]),
            "notional": float(p["positionValue"]),
            "uPnl": float(p["unrealizedPnl"]),
            "lev": float(p["leverage"]["value"]),
            "liqPx": float(p["liquidationPx"]) if p.get("liquidationPx") else None,
        }
    summary = {
        "accountValue": float(ms.get("accountValue", 0)),
        "totalNtlPos": float(ms.get("totalNtlPos", 0)),
    }
    return positions, summary


def fetch_closed_pnl(coin, since_ms):
    """抓某幣種自 since_ms 之後的已實現損益總和"""
    try:
        fills = hl_info({"type": "userFills", "user": ADDRESS, "aggregateByTime": True})
    except Exception:
        return None
    total = 0.0
    hit = False
    for f in fills or []:
        if f.get("coin") != coin:
            continue
        if since_ms and f.get("time", 0) < since_ms:
            continue
        if "Close" in f.get("dir", "") or "Long > Short" in f.get("dir", "") or "Short > Long" in f.get("dir", ""):
            total += float(f.get("closedPnl", 0) or 0)
            hit = True
    return total if hit else None


def money(v):
    a = abs(v)
    s = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{s}${a/1e9:.2f}B"
    if a >= 1e6:
        return f"{s}${a/1e6:.2f}M"
    if a >= 1e3:
        return f"{s}${a/1e3:.1f}K"
    return f"{s}${a:.0f}"


def qty(v):
    a = abs(v)
    if a >= 1000:
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if a >= 1:
        return f"{v:,.3f}".rstrip("0").rstrip(".")
    return f"{v:.6f}".rstrip("0").rstrip(".")


def px(v):
    if v is None:
        return "-"
    return f"{v:,.4f}".rstrip("0").rstrip(".") if v < 10 else f"{v:,.2f}"


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] 狀態檔讀取失敗，視為首次執行: {e}", file=sys.stderr)
        return None


def save_state(positions, summary, ts_ms):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"ts": ts_ms, "positions": positions, "summary": summary}, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def diff_events(old_pos, new_pos, last_ts):
    """只回報 開倉 / 平倉 / 反手"""
    events = []
    for coin, n in new_pos.items():
        o = old_pos.get(coin)
        if o is None:
            events.append({"kind": "OPEN", "coin": coin, "new": n})
        elif (o["szi"] > 0) != (n["szi"] > 0):
            pnl = fetch_closed_pnl(coin, last_ts)
            events.append({"kind": "FLIP", "coin": coin, "old": o, "new": n, "pnl": pnl})
    for coin, o in old_pos.items():
        if coin not in new_pos:
            pnl = fetch_closed_pnl(coin, last_ts)
            events.append({"kind": "CLOSE", "coin": coin, "old": o, "pnl": pnl})
    return events


def format_message(events, summary):
    now = datetime.now(TZ).strftime("%m/%d %H:%M")
    lines = []
    for e in events:
        c = e["coin"]
        if e["kind"] == "OPEN":
            n = e["new"]
            arrow = "🟢 開多" if n["side"] == "LONG" else "🔴 開空"
            lines.append(
                f"{arrow} {c}\n"
                f"　數量 {qty(n['szi'])}｜名目 {money(n['notional'])}\n"
                f"　進場 {px(n['entryPx'])}｜{n['lev']:.0f}x｜強平 {px(n['liqPx'])}"
            )
        elif e["kind"] == "CLOSE":
            o = e["old"]
            pnl = e.get("pnl")
            pnl_s = f"\n　已實現 {money(pnl)}" if pnl is not None else ""
            lines.append(
                f"⚪ 平倉 {c}（原 {o['side']}）\n"
                f"　原名目 {money(o['notional'])}｜進場 {px(o['entryPx'])}{pnl_s}"
            )
        elif e["kind"] == "FLIP":
            o, n = e["old"], e["new"]
            pnl = e.get("pnl")
            pnl_s = f"｜前倉已實現 {money(pnl)}" if pnl is not None else ""
            lines.append(
                f"🔁 反手 {c}：{o['side']} → {n['side']}\n"
                f"　數量 {qty(n['szi'])}｜名目 {money(n['notional'])}\n"
                f"　進場 {px(n['entryPx'])}｜{n['lev']:.0f}x{pnl_s}"
            )
    head = f"【HL 監控】{now}"
    tail = f"淨值 {money(summary['accountValue'])}｜總名目 {money(summary['totalNtlPos'])}"
    short = ADDRESS[:6] + "…" + ADDRESS[-4:]
    return head + "\n" + "\n\n".join(lines) + f"\n\n{tail}\n{short}"


def line_push(text):
    if DRY_RUN or not LINE_TOKEN or not LINE_USER_ID:
        print("[dry-run] 不推播，訊息內容：\n" + text)
        return
    try:
        post_json(
            LINE_PUSH_API,
            {"to": LINE_USER_ID, "messages": [{"type": "text", "text": text[:4900]}]},
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
        )
        print("[ok] LINE 已推播")
    except urllib.error.HTTPError as e:
        print(f"[error] LINE 推播失敗 {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
        sys.exit(1)


def main():
    if not ADDRESS:
        print("[fatal] 未設定 HL_ADDRESS", file=sys.stderr)
        sys.exit(1)

    positions, summary = fetch_state()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    prev = load_state()

    if prev is None:
        save_state(positions, summary, now_ms)
        desc = "、".join(f"{c} {p['side']} {money(p['notional'])}" for c, p in positions.items()) or "無持倉"
        msg = (
            f"【HL 監控已啟動】\n"
            f"追蹤 {ADDRESS[:6]}…{ADDRESS[-4:]}\n"
            f"目前持倉：{desc}\n"
            f"淨值 {money(summary['accountValue'])}\n"
            f"之後有開倉／平倉／反手才會再通知。"
        )
        line_push(msg)
        return

    events = diff_events(prev.get("positions", {}), positions, prev.get("ts"))
    if events:
        line_push(format_message(events, summary))
    else:
        print(f"[ok] 無變動（{len(positions)} 個持倉）")

    save_state(positions, summary, now_ms)


if __name__ == "__main__":
    main()
