#!/usr/bin/env python3
"""
Hyperliquid 多地址監控 → LINE 推播
偵測事件：開倉 / 平倉 / 反手 / 減倉
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
REDUCE_PCT = float(os.environ.get("REDUCE_PCT", "5"))  # 自上次警示水位再減這個%就通知


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


_FILL_CACHE = {}


def fetch_fills_since(addr, since_ms):
    """用時間區間抓成交，必要時分頁。

    比 userFills 完整：userFills 會截斷且 aggregateByTime 會併掉同秒成交，
    實測同一地址 userFills 只給 1,236 筆／$865K，分頁抓給 10,139 筆／$1.08M。
    """
    key = (addr, since_ms)
    if key in _FILL_CACHE:
        return _FILL_CACHE[key]
    out, cur = [], int(since_ms or 0)
    for _ in range(5):                      # 上限 5 頁，避免異常時打爆
        try:
            chunk = hl_info({"type": "userFillsByTime", "user": addr, "startTime": cur})
        except Exception as e:
            print(f"[warn] 抓成交失敗 {addr[:10]}: {e}", file=sys.stderr)
            break
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 2000:
            break
        cur = chunk[-1]["time"] + 1
    dedup = {}
    for f in out:
        dedup[f.get("tid") or (f.get("time"), f.get("coin"), f.get("px"), f.get("sz"))] = f
    res = list(dedup.values())
    _FILL_CACHE[key] = res
    return res


def closed_stats(addr, coin, since_ms):
    """回傳 (已實現損益, 平掉數量)；查不到回 (None, None)"""
    fills = fetch_fills_since(addr, since_ms)
    total = 0.0
    sz = 0.0
    hit = False
    for f in fills:
        if f.get("coin") != coin:
            continue
        d = f.get("dir", "")
        if "Close" in d or "Long > Short" in d or "Short > Long" in d:
            total += float(f.get("closedPnl", 0) or 0)
            sz += abs(float(f.get("sz", 0) or 0))
            hit = True
    return (total, sz) if hit else (None, None)


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


def money_s(v):
    """帶正負號，用於損益"""
    return ("+" if v > 0 else "") + money(v)


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


def cur_px(p):
    """由名目與數量反推現價"""
    sz = abs(p["szi"])
    return p["notional"] / sz if sz else None


def liq_gap(p, cp):
    """強平價距現價百分比；多單為負、空單為正"""
    lq = p.get("liqPx")
    if not lq or not cp:
        return None
    return (lq - cp) / cp * 100


def pos_detail(p, pad="　　"):
    """單一持倉的明細行：進場 / 現價 / 未實現 / 強平"""
    cp = cur_px(p)
    chg = ((cp - p["entryPx"]) / p["entryPx"] * 100) if (cp and p["entryPx"]) else None
    if p["szi"] < 0 and chg is not None:
        chg = -chg
    lines = [f"{pad}進場 {px(p['entryPx'])}｜現價 {px(cp)}"
             + (f"｜{chg:+.1f}%" if chg is not None else "")]
    lines.append(f"{pad}未實現 {money_s(p['uPnl'])}")
    g = liq_gap(p, cp)
    if p.get("liqPx"):
        gs = f"（{g:+.1f}%）" if g is not None else ""
        lines.append(f"{pad}強平 {px(p['liqPx'])}{gs}")
    return "\n".join(lines)


def side_zh(p):
    return "多" if p["szi"] > 0 else "空"


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
    """回報 開倉 / 平倉 / 反手 / 減倉，並在 new_pos 上寫入 peak / alerted 追蹤欄位

    減倉用「累積」判斷：跟上次警示的水位比，而不是跟上一次輪詢比。
    否則每次只減 2% 就永遠碰不到門檻，倉位會無聲流掉。
    """
    events = []
    for coin, n in new_pos.items():
        o = old_pos.get(coin)
        cur = abs(n["szi"])

        if o is None:
            n["peak"] = cur
            n["alerted"] = cur
            events.append({"kind": "OPEN", "coin": coin, "new": n})
            continue

        if (o["szi"] > 0) != (n["szi"] > 0):
            n["peak"] = cur
            n["alerted"] = cur
            pnl, csz = closed_stats(addr, coin, last_ts)
            events.append({"kind": "FLIP", "coin": coin, "old": o, "new": n,
                           "pnl": pnl, "fill_sz": csz, "want_sz": abs(o["szi"])})
            continue

        prev_peak = o.get("peak") or abs(o["szi"])
        alerted = o.get("alerted") or abs(o["szi"])
        n["peak"] = max(prev_peak, cur)
        if cur > alerted:          # 加倉：抬高基準，不通知
            alerted = cur
        if cur < alerted * (1 - REDUCE_PCT / 100):
            pnl, csz = closed_stats(addr, coin, last_ts)
            events.append({"kind": "REDUCE", "coin": coin, "old": o, "new": n,
                           "from_sz": alerted, "peak_sz": n["peak"],
                           "pnl": pnl, "fill_sz": csz, "want_sz": alerted - cur})
            alerted = cur
        n["alerted"] = alerted

    for coin, o in old_pos.items():
        if coin not in new_pos:
            pnl, csz = closed_stats(addr, coin, last_ts)
            events.append({"kind": "CLOSE", "coin": coin, "old": o,
                           "pnl": pnl, "fill_sz": csz, "want_sz": abs(o["szi"])})
    return events


def pnl_line(e, label):
    """損益行；成交紀錄涵蓋不到倉位變化量時標警告"""
    pnl = e.get("pnl")
    if pnl is None:
        return ""
    line = f"\n　{label} {money_s(pnl)}"
    want, got = e.get("want_sz"), e.get("fill_sz")
    if want and got is not None and got < want * 0.9:
        line += f"\n　\u26a0 成交紀錄只涵蓋 {qty(got)}／{qty(want)}，金額可能不完整"
    return line


def render_events(events):
    out = []
    for e in events:
        c = e["coin"]
        if e["kind"] == "OPEN":
            n = e["new"]
            arrow = "\U0001f7e2 開多" if n["szi"] > 0 else "\U0001f534 開空"
            out.append(
                f"{arrow} {c}\n"
                f"　名目 {money(n['notional'])}｜數量 {qty(n['szi'])}\n"
                + pos_detail(n, "　")
            )
        elif e["kind"] == "CLOSE":
            o = e["old"]
            extra = pnl_line(e, "已實現")
            out.append(
                f"\u26aa 平倉 {c}（原 {side_zh(o)}）\n"
                f"　原名目 {money(o['notional'])}｜進場 {px(o['entryPx'])}{extra}"
            )
        elif e["kind"] == "REDUCE":
            n, o = e["new"], e["old"]
            cur = abs(n["szi"])
            frm, peak = e["from_sz"], e["peak_sz"]
            step = (1 - cur / frm) * 100 if frm else 0
            total = (1 - cur / peak) * 100 if peak else 0
            extra = pnl_line(e, "已實現")
            out.append(
                f"\U0001f7e1 減倉 {c}（{side_zh(n)}）\n"
                f"　此次 -{step:.1f}%：{qty(frm)} → {qty(cur)}\n"
                f"　自峰值共 -{total:.1f}%（峰值 {qty(peak)}）\n"
                f"　剩餘名目 {money(n['notional'])}\n"
                + pos_detail(n, "　") + extra
            )
        elif e["kind"] == "FLIP":
            o, n = e["old"], e["new"]
            extra = pnl_line(e, "前倉已實現")
            out.append(
                f"\U0001f501 反手 {c}：{side_zh(o)} → {side_zh(n)}\n"
                f"　名目 {money(n['notional'])}｜數量 {qty(n['szi'])}\n"
                + pos_detail(n, "　") + extra
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
            if positions:
                parts = []
                for c, p in sorted(positions.items(), key=lambda kv: -kv[1]["notional"]):
                    p["peak"] = abs(p["szi"])
                    p["alerted"] = abs(p["szi"])
                    parts.append(f"　{c} {side_zh(p)} {money(p['notional'])}\n" + pos_detail(p))
                desc = "\n".join(parts)
            else:
                desc = "　無持倉"
            intro.append(f"◆ {label}　淨值 {money(summary['accountValue'])}\n{desc}")
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
                  + "\n\n之後有開倉／平倉／反手／減倉才會再通知。")
    if blocks:
        line_push(f"【HL 監控】{now}\n\n" + "\n\n".join(blocks))
    if not intro and not blocks:
        print(f"[ok] 無變動（{len(targets)} 個地址）")
    if failed:
        print(f"[warn] 本次未取得：{', '.join(failed)}（狀態保留，下次重試）", file=sys.stderr)

    save_state(new_state)


if __name__ == "__main__":
    main()
