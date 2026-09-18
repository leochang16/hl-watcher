# HL Position Watcher

監控多個 Hyperliquid 地址，**開倉 / 平倉 / 反手** 時推播到 LINE。

## 追蹤名單

| 名字 | 地址 | 加入時的依據 |
|---|---|---|
| ZEC新兵 | `0xe2ad376800c2da03d611700ffc1d013090733c8c` | 只有 1 個月紀錄，全期 PnL = 月 PnL |
| 滿槓老兵 | `0xbf732ea04197942783e34730ed6e0f6099575d58` | 130天/95個交易日，全期 PnL $23.0M |
| 冷門獵手 | `0x9321d8117e73b0c79035f0e87debcfd8dbb1d75a` | 111天/44個交易日，全期 PnL $14.0M |
| 七成空手 | `0x795cfd1b03eafc11c4ec958b8a94cfc9aa64a242` | 130天但只39天進場，全期 PnL $10.9M |

篩選標準（排除量化/做市）：月成交量÷淨值在 5–150 倍、成交筆數 < 100 筆/天、持倉集中且單方向、本月之前已獲利。

## 運作方式

每 5 分鐘抓一次每個地址的 `clearinghouseState` 持倉快照，和 `state.json` 做差分。

- **不逐筆推 fill** — 巨鯨單次動作會拆成 8+ 筆成交，逐筆推會洗版。
- 多個地址同時有事件時，**合併成一則**推播（省 LINE 額度）。
- 平倉／反手時額外查 `userFills` 補上已實現損益。
- 名目 < `MIN_NOTIONAL`（預設 $5,000）的灰塵部位直接忽略。

不會通知的事：加倉、減倉、價格變動、未實現損益變化。

## 通知內容

每檔持倉都會列：名目、數量、**進場價、現價、漲跌幅、未實現損益、強平價（距現價百分比）**。

現價是用 `positionValue ÷ szi` 反推的，不另外打行情 API。強平價距離為負代表在現價下方（多單），正代表在上方（空單）。

不顯示槓桿倍數——強平價已經把槓桿的效果表達完了。

## 設定

| 位置 | 名稱 | 內容 |
|---|---|---|
| Variables | `HL_ADDRESSES` | 逗號分隔，每項 `地址=名字`（名字可略） |
| Secrets | `LINE_USER_ID` | LINE Developers › Basic settings › Your user ID |
| Secrets | `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developers › Messaging API › Channel access token |

推播走「毛貓豬公」channel（ID 2011551446）。
**這個 channel 同時被歐洲行程 bot 使用（webhook 指向 Cloudflare Worker），所以 token 不要按 Reissue** — 重發會把舊 token 廢掉，那隻 bot 會斷線。

## 增減追蹤地址

改 `HL_ADDRESSES` variable 就好，不用動程式。

- **新增**：直接加到清單尾端。下次執行會幫它建基準線並推一則「新增追蹤」，**不會**把它現有的倉位誤報成開倉。
- **改名字**：只改 `=` 後面那段，state 是用地址當 key，不會重建基準線。
- **移除**：從清單抽掉。`state.json` 裡的舊資料會留著但不影響運作。

不要手動刪 `state.json`——刪了下次執行會把所有地址的現有倉位全部當成新開倉重新報一次。

## 本機測試

```bash
export HL_ADDRESSES="0xbf732ea04197942783e34730ed6e0f6099575d58=滿槓老兵"
export DRY_RUN=1          # 只印訊息不推 LINE
rm -f state.json && python3 watcher.py   # 建基準線
python3 watcher.py                       # 第二次應顯示「無變動」
```

## 已知限制

- **延遲**：GitHub Actions 的 cron 最短 5 分鐘，高峰期常被延遲 5–15 分鐘，所以實際通知延遲可能到 20 分鐘。要壓到 1 分鐘得換 VPS 或 Cloud Run。
- **快進快出會漏**：兩次輪詢之間開完又平掉的倉位不會被偵測到。
- **分批出場看不到**：只通知完全平倉，所以大倉分批減倉的過程不會通知，只在最後一批出完時發。冷門標的（如 LIT $24.9M）特別明顯，因為那種倉位不可能一次平掉。要抓早期訊號得把減倉納入通知。
- **LINE 免費方案**每月推播上限（目前 200 則）是整個 channel 共用的。
- **擁擠交易風險**：目前 ZEC新兵 和 滿槓老兵 都押 ZEC 多。他們同時平倉不是兩個獨立訊號，是同一個擁擠交易在出貨。
