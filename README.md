# HL Position Watcher

監控 Hyperliquid 地址 `0xe2ad376800c2da03d611700ffc1d013090733c8c`，
**開倉 / 平倉 / 反手** 時推播到 LINE。

## 運作方式

每 5 分鐘抓一次 `clearinghouseState` 持倉快照，和上次存的 `state.json` 做差分。

**不逐筆推 fill** — 這地址單次動作會拆成 8+ 筆成交，逐筆推會洗版。
平倉／反手時額外查 `userFills` 補上已實現損益。

不會通知的事：加倉、減倉、價格變動、未實現損益變化。

## 設定

| 位置 | 名稱 | 內容 |
|---|---|---|
| Variables | `HL_ADDRESS` | 要追蹤的地址 |
| Secrets | `LINE_USER_ID` | LINE Developers › Basic settings › Your user ID |
| Secrets | `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developers › Messaging API › Channel access token |

推播走「毛貓豬公」channel（ID 2011551446）。
**這個 channel 同時被歐洲行程 bot 使用（webhook 指向 Cloudflare Worker），所以 token 不要按 Reissue** — 重發會把舊 token 废掉，那隻 bot 會断線。

## 本機測試

```bash
export HL_ADDRESS=0xe2ad376800c2da03d611700ffc1d013090733c8c
export DRY_RUN=1          # 只印訊息不推 LINE
rm -f state.json && python3 watcher.py   # 初始化
```

## 已知限制

- **延遲**：GitHub Actions 的 cron 最短 5 分鐘，高峰期常被延遲 5–15 分鐘，所以實際通知延遲可能到 20 分鐘。要壓到 1 分鐘得換 VPS 或 Cloud Run。
- **快進快出會漏**：兩次輮詢之間開完又平掉的倉位不會被偵測到。這地址持倉週期是天級，影響不大。
- **LINE 免費方案**每月推播上限（目前 200 則）是整個 channel 共用的。
- **只看單一地址**。

## 換追蹤對象

改 `HL_ADDRESS` variable，然後**刪掉 repo 裏的 `state.json`**，下次執行會以新地址重新建基準線。
