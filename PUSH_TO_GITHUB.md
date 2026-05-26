# Push 到 GitHub & 啟動每日自動爬蟲

照這份做完後,GitHub 會每天台北時間早上 8:00 自動跑爬蟲、更新 `data/` 資料夾。

預計時間：**約 10 分鐘**。

---

## Step 0 · 確認你電腦上有 git

打開 cmux 或 Terminal,跑：

```bash
git --version
```

有看到版本號(例如 `git version 2.43.0`)就 OK。沒有的話跑 `xcode-select --install` 裝 Apple 內建的開發者工具。

---

## Step 1 · 在 GitHub 開一個新 repo

1. 用瀏覽器開 [https://github.com/new](https://github.com/new)
2. **Repository name:** 隨便取,例如 `chiyuan-reviews`
3. **Privacy:** 選 **Private**(只有你能看;dashboard fetch raw URL 還是可以動,但只有你跟你授權的 collaborator 才能 push)
   - 如果要讓你的 dashboard 從**沒有登入的瀏覽器**也能 fetch raw URL,需要選 Public。**建議先 Private,日後有需要再改。**
4. **不要**勾「Add a README」「Add .gitignore」「Add a license」(我們本地已經有這些檔案了,GitHub 預先建會衝突)
5. 點 **Create repository**

GitHub 會顯示一個「Quick setup」頁,**先別關**,等下要看 URL。

---

## Step 2 · 在本機 push 上去

打開 cmux 或 Terminal,把這幾行**依序**跑(請把 `<你的帳號>` 和 `<repo名>` 換掉):

```bash
# 進到爬蟲資料夾
cd ~/Downloads/chiyuan-reviews-scraper

# 重要:先確認 stores.json / scraper.py 等都在
ls

# 初始化 git
git init
git branch -M main
git add .
git status                          # 確認要 commit 哪些檔案
git commit -m "Initial commit: scraper + GitHub Actions setup"

# 把本機 repo 連到 GitHub 上的 repo
# 把下面這行的 <你的帳號> 跟 <repo名> 換掉:
git remote add origin https://github.com/<你的帳號>/<repo名>.git

# Push 上去
git push -u origin main
```

第一次 push 時 GitHub 可能會問你登入,有兩種狀況：

**狀況 A:已經設過 git credential / GitHub CLI**
直接成功,看到 "branch 'main' set up to track..." 即完成。

**狀況 B:沒設過,跳出視窗問帳密**
不要用密碼登入(已被 GitHub 棄用)。最快的方法：
1. 用瀏覽器登入 [https://github.com/settings/tokens?type=beta](https://github.com/settings/tokens?type=beta)
2. Generate new token (fine-grained), repository access 選你剛開的那個 repo,Permissions 給 `Contents: Read and write`
3. 複製 token(只顯示一次)
4. 回到 cmux 重試 push,帳號輸 GitHub username,密碼貼這個 token

---

## Step 3 · 開啟 Actions 寫入權限

push 成功後,瀏覽器到你的 repo 頁面:

1. 點 **Settings** (右上)
2. 左側選 **Actions** → **General**
3. 拉到底找到 **Workflow permissions**
4. 選 **Read and write permissions**(這樣 Actions 跑完才能 commit 新資料回 repo)
5. **Save**

> 這個動作必做。沒做的話 Actions 跑完無法寫回 `data/`,等於白跑。

---

## Step 4 · 手動觸發第一次跑,驗證 OK

1. repo 頁面點 **Actions** tab
2. 左側點 **"Scrape Google Maps reviews"**
3. 右上點 **Run workflow** → **Run workflow**(綠色按鈕)
4. 等約 15-30 分鐘,綠勾出現 → 成功

跑成功的話 `data/` 資料夾會自動有新的 commit。你可以打開 `data/last_updated.txt` 看時間。

如果失敗(紅叉),點進去看 log,99% 是 Step 3 的權限沒設,或 Step 2 的某個 commit 沒推上來。

---

## Step 5 · (可選) 給你 dashboard 改成 fetch GitHub raw URL

從今天起 `data/reviews.json` 等檔案會每天更新。如果想讓 dashboard 自動接到新資料而不用手動複製貼上,改 `index.html` 開頭:

```html
<!-- 取代寫死的 REVIEWS_DATA 那段 -->
<script>
const RAW = 'https://raw.githubusercontent.com/<你的帳號>/<repo名>/main/data/';
let REVIEWS_DATA = [], STORE_RATINGS = [];

(async () => {
  const [r1, r2] = await Promise.all([
    fetch(RAW + 'reviews.json').then(r => r.json()),
    fetch(RAW + 'store_ratings.json').then(r => r.json()),
  ]);
  REVIEWS_DATA = r1.reviews;
  STORE_RATINGS = r2;
  initReviews();   // 你 dashboard 已有的初始化函式
})();
</script>
```

> ⚠️ Private repo 的 raw URL 從瀏覽器讀需要 token。**最簡單的做法是把 repo 改成 Public**(評論資料本來就是公開的,沒有秘密)。Settings → Danger Zone → Change repository visibility。
>
> 如果一定要 Private,要用 GitHub Pages 或加 token 認證,比較麻煩。

---

## 之後的事

- **每天早上 8:00**(台北)Actions 自動跑,你不用做任何事
- **手動補資料**:Actions tab → Run workflow
- **加新店**:編輯 `stores.json`、push 上來,隔天就會抓
- **看跑掛了沒**:Settings → Notifications → Actions → 勾 "Send notifications for failed workflows"

排程偶爾會晚 5-30 分鐘(GitHub 高峰時段),這是正常的不用擔心。
