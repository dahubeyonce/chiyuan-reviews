# 季緣 CHIYUAN — Google Maps 評論爬蟲

定期把所有分店的 Google Maps 評論抓下來，輸出 JSON / JS 給 `index.html` dashboard 用。

```
chiyuan-reviews-scraper/
├── stores.json                 # ← 唯一要編輯的設定檔（分店清單）
├── scraper.py                  # 主程式
├── requirements.txt
├── data/                       # ← 爬蟲輸出（commit 進來 dashboard 才讀得到）
│   ├── reviews.json            #   主資料
│   ├── store_ratings.json      #   各店總評分/評論數
│   ├── reviews_data.js         #   貼進 index.html 用
│   └── last_updated.txt
└── .github/workflows/scrape.yml  # 每天 08:00 (台北) 自動跑
```

---

## 一、第一次設定（5 分鐘）

需要：Python 3.10+、終端機。

```bash
# 1. clone 下來 / 或解壓縮到本地
cd chiyuan-reviews-scraper

# 2. 建虛擬環境（建議，但不強制）
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

# 3. 裝套件
pip install -r requirements.txt

# 4. 裝 Playwright 用的瀏覽器（約 130 MB，第一次比較久）
playwright install chromium
```

---

## 二、日常使用

### 全部跑一遍（10 家店 × 200 則）

```bash
python scraper.py
```

預計 15–30 分鐘（看網路）。結果寫到 `data/`。

### 常用旗標

```bash
python scraper.py --max 100              # 每店只抓 100 則
python scraper.py --store 小巨蛋          # 只跑一家店（debug 或補資料用）
python scraper.py --headed               # 顯示瀏覽器視窗，看它在做什麼
python scraper.py --verbose              # 詳細 log
```

### 結果長這樣

`data/reviews.json`:

```json
{
  "generated_at": "2026-05-25T10:30:00",
  "count": 1487,
  "reviews": [
    {
      "store": "小巨蛋",
      "rating": 5,
      "date": "2026-05-20",
      "author": "Mandy Kuo",
      "text": "Solid tea that's super well brewed..."
    }
  ]
}
```

`data/store_ratings.json`:

```json
[
  { "store": "小巨蛋", "rating": 4.7, "count": 1042 },
  { "store": "師大",   "rating": 4.5, "count": 959  }
]
```

`data/reviews_data.js` — 直接複製貼到 `index.html` 取代 `REVIEWS_DATA` 和 `STORE_RATINGS` 那兩個陣列。

---

## 三、改設定

### 加 / 移除 / 改名分店

只動 `stores.json`：

```json
{
  "stores": [
    { "name": "新店家", "url": "https://maps.app.goo.gl/xxxx" }
  ]
}
```

- `name` 要對到 dashboard 內顯示的中文名（dashboard 的 `STORE_RATINGS` 和篩選按鈕都吃這個名字）。
- `url` 短連結（`maps.app.goo.gl/xxx`）或完整 Google Maps URL 都可以。

---

## 四、上 GitHub Actions（自動排程）

`.github/workflows/scrape.yml` 已經寫好。把整個資料夾 push 到 GitHub repo 後：

1. **Repo settings → Actions → General → Workflow permissions** 改成 `Read and write permissions`（讓 Actions 可以 commit 回 repo）。
2. **預設排程：每天 00:00 UTC（台北時間早上 08:00）**。要改頻率，編 `scrape.yml` 裡的 `cron`。
3. **手動觸發：** Actions tab → "Scrape Google Maps reviews" → Run workflow。
4. Action 跑完後會自動 commit `data/` 裡的新檔案。Dashboard 如果 fetch GitHub raw URL 就會看到最新資料。

### Dashboard 怎麼讀新資料

兩種做法：

**A. 還是內嵌（沿用現在做法）**
每次 Action 跑完，手動把 `data/reviews_data.js` 複製貼到 `index.html` 對應位置。最簡單但要手工。

**B. 改成 fetch（推薦給長期）**
在 `index.html` 改成從 GitHub raw URL 抓 JSON：

```js
// 取代原本寫死的 REVIEWS_DATA = [...]
let REVIEWS_DATA = [];
let STORE_RATINGS = [];

async function loadReviews() {
  const base = 'https://raw.githubusercontent.com/<你的用戶名>/<repo>/main/data/';
  const [r1, r2] = await Promise.all([
    fetch(base + 'reviews.json').then(r => r.json()),
    fetch(base + 'store_ratings.json').then(r => r.json()),
  ]);
  REVIEWS_DATA = r1.reviews;
  STORE_RATINGS = r2;
  initReviews();   // 你 dashboard 原本有的初始化函式
}
loadReviews();
```

優點：之後完全不用碰 dashboard，Action 跑完資料就更新。

---

## 五、Debug：爬蟲壞了怎麼辦

### 通用 debug 起手式

```bash
python scraper.py --store 小巨蛋 --max 20 --headed --verbose
```

`--headed` 會把瀏覽器叫出來，你親眼看它走到哪一步斷掉；`--verbose` 開最詳細 log。

### 已知會踩到的 Google Maps 雷（本腳本已處理，但若失效要回頭找這幾個地方）

| 症狀 | 真實原因 | 程式碼處理位置 |
|---|---|---|
| 點不到評論 tab，反而跳出「撰寫評論」 | `撰寫評論` 按鈕的 aria-label 也包含「評論」二字 | `_open_reviews_tab` 用 `role="tab"` 限定 |
| 短 URL 重導後沒有「評論」tab | 短連結重導後只有 Overview / Photos 2 個 tab | `_build_reviews_url` 自動把 URL 改成 `/place/.../reviews/` |
| 跑出來 selector 失敗、簡化版頁面 | Google 偵測 `navigator.webdriver=true` 切簡化版 | `scrape_store` 用 init script 把 webdriver 設為 undefined |
| 評論數量是兩倍但內容重複 | DOM 把 `data-review-id` 同時掛在外層和內層 div | `_count_reviews` 和 parse 階段都 dedupe by id |

### 情境 1：所有店都抓 0 則

代表 Google Maps 改了 DOM 結構，selector 失效。打開：

```bash
python scraper.py --store 小巨蛋 --headed --verbose
```

修法：打開 `scraper.py`，找到 `_REVIEW_SELECTORS`、`_find_feed`、`_extract_review` 這幾個函式。每個函式都用「多個 selector 依序試」的策略，加新的 selector 進去就好。

用 Chrome DevTools（Cmd+Opt+I）→ 對著評論卡片右鍵 → Inspect → 看 `data-review-id` 或 class 名是什麼，加進對應的 selector list。

### 情境 2：被 Google 擋（CAPTCHA / 異常流量）

- 短期：等幾分鐘再跑，或加 `--max 50` 降低總請求量
- 長期：在 `scraper.py` 的 `scrape_store()` 函式裡 `ctx = await browser.new_context(...)` 加 proxy 設定

### 情境 3：抓得到評論但日期都空白

代表日期格式 Google 又改了（語言、措辭）。看 `parse_relative_date()` 函式，加新的 regex pattern。

### 情境 4：排序變回「最相關」而非「最新」

跑完後檢查日期單調性：

```bash
python3 -c "
import json
data = json.load(open('data/reviews.json'))
dates = [r['date'] for r in data['reviews'] if r['date']]
print('前 10 筆:', dates[:10])
print('後 10 筆:', dates[-10:])
"
```

正常情況前面 10 筆應該都是近期日期，後面才會比較舊。如果日期跳來跳去，代表 `_sort_by_newest` 沒成功點到「最新」——通常是排序按鈕的 selector 變了。用 `--headed` 親眼看它有沒有點開排序選單。

### 情境 5：GitHub Actions 跑掛了

進 Actions tab 看 log。最常見：

- `playwright install chromium` 失敗 → 看 Action runner 磁碟空間
- 連線 timeout → 重新觸發一次（GitHub runners 偶爾出包）
- Push 權限被拒 → 回頭看上面第四節「Workflow permissions」設定
- 在 Actions 跑出 simplified layout（本地正常雲端失敗）→ `scrape_store` 的 `add_init_script` 沒生效，可能 Playwright 版本要升

---

## 六、檔案規格給未來換 dashboard 用

如果之後 dashboard 整個重寫（換 React / Next.js / 其他），`reviews.json` 的 schema 是穩定 contract：

```ts
type ReviewsFile = {
  generated_at: string;        // ISO datetime
  count: number;
  reviews: Array<{
    store: string;             // 中文分店名，對到 stores.json
    rating: 1 | 2 | 3 | 4 | 5;
    date: string;              // YYYY-MM-DD
    author: string;
    text: string;              // 可能是空字串
  }>;
};
```

只要新 dashboard 吃這個格式，腳本就完全不用動。

---

## 七、聯絡 / 維護

- 設定檔負責人：改 `stores.json` 即可
- 程式碼問題：看 `scraper.py` 開頭的 docstring + 本文件第五節
- 一次性手動補資料：`python scraper.py --store XXX`
