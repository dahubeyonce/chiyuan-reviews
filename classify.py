"""
NPS-aligned classification of Google Maps reviews.

This module is the single source of truth for "what topics a review touches"
across all CHIYUAN tools (dashboard, qualitative analysis, internal reports).

It mirrors the 6 領域 used in our internal NPS questionnaire so that Google
Maps data and NPS data become directly comparable.

Two outputs per review
----------------------
- topics: list[str]            ← multi-label, may have 1+ of the 6 NPS 領域
- sentiment_group: str         ← "promoter" / "passive" / "detractor"
                                 (mirrors NPS 9-10 / 7-8 / 0-6 buckets)

NPS-to-stars mapping
--------------------
Google Maps stars do not map 1:1 to NPS 0-10, but the *spirit* of NPS buckets
translates cleanly:

    NPS 9-10 (promoter)   ↔  ★5
    NPS 7-8  (passive)    ↔  ★3-4
    NPS 0-6  (detractor)  ↔  ★1-2

When comparing %s across the two datasets, keep in mind:
- NPS sampling = customers who took the survey (self-selecting in different way)
- Google Maps  = customers who chose to publicly review (skewed to strong opinions)
"""

from __future__ import annotations
import re
from typing import List


# --------------------------------------------------------------------------
# 6 NPS 領域 → regex patterns
# --------------------------------------------------------------------------
# Design rules:
# - 每個領域可有多條 pattern,任一條 match 即計入該領域
# - 一筆評論可同時 match 多個領域 (multi-label,跟 NPS 問卷一致)
# - 純情緒詞(好喝/難喝)優先歸入產品品質,因為大部分是在講飲料
# - 「跨店品質落差」依使用者裁定: 歸產品品質
# - 「水蜜桃回歸/終於買到」歸行銷吸引(季節限定主題)
# - 「我永遠的最愛/忠實粉絲」歸品牌形象
# - 規格問題(冰量/糖度/份量)歸產品品質
#
# 維護:新發現規則加在對應領域的 patterns 末端即可。
# 不要為了「乾淨」刪掉看起來重複的字詞 — regex 對中文短評容錯越多越好。

NPS_CATEGORY_RULES: dict[str, list[re.Pattern]] = {

    "產品品質": [
        re.compile(
            # 正面核心 - 茶湯品質
            r"好喝|超好喝|好喝爆|讚|超讚|大讚|香|茶香|回甘|順口|清爽|不澀|不苦|品質好"
            r"|新鮮|不假|真材實料|真.*果肉|真.*水果|純茶|高山茶|有層次|濃郁"
            r"|很 ?ok|還不錯|不錯|喜歡這味"
        ),
        re.compile(
            # 負面核心 - 茶湯出問題
            r"水味|加水|沒.*茶味|沒茶味|無茶味|淡|淡到|超淡|很淡|難喝|超難喝|難喝爆"
            r"|苦澀|很苦|沒味道|無味|沒香|沒.*奶味|香料|香精|甜膩|甜到受不了|奶蓋很水|融化|稀釋"
        ),
        re.compile(
            # 產品名 - 提到任一款飲料都算討論到產品品質 (含常見錯字)
            r"水蜜桃|蜜桃|水密桃|peach|蓮霧|柚|葡萄柚|青韻|茶王|阿薩姆|茉莉|烏龍|金萱"
            r"|高山茶|東方美人|鮮乳|鮮奶|奶蓋|果茶|純茶|冬瓜|甘蔗|脆梅|杉林溪|貴妃"
            r"|紅玉|台茶|台茶18|台茶 ?\d+|奶茶|鮮乳茶|桃子|茶系|水果系|茶類|水果類|果系"
        ),
        re.compile(
            # 通用品質讚美短句 (中文+台灣口語+品質詞)
            r"好ㄏ|好和|品質優良|品質好|品質佳|是好茶|好茶|讚啦|讚的|推推|大推|推爆|推到不行"
            r"|美味|精緻|茶品|手搖飲?|飲料.*好|飲料.*讚"
        ),
        re.compile(
            # 提到「茶」字本身就算 (台灣手搖飲場景下幾乎都是在講飲料品質)
            r"(^|[^葉藝道色道園田藝行])茶([^葉藝道色園田藝行]|$)"
        ),
        re.compile(
            # 規格 - 糖、冰、料
            r"冰塊|冰量|滿冰|去冰|微冰|半冰|分糖|微糖|無糖|半糖|甜度"
            r"|糖.*沒搖|糖.*沉|糖漿|糖.*錯|沒搖|沒.*搖勻"
            r"|珍珠|加料|配料|果凍|茶凍|料.*少|果肉.*少|沒.*珍珠|沒.*加.*料"
        ),
        re.compile(
            # 跨店比較 - 依使用者裁定歸產品品質
            r"別.*店|別家|別間|別的.*分店|其他.*分店|這家.*別|這間.*別|.*分店.*不同|加水.*季緣|.*店.*好喝"
        ),
    ],

    "服務體驗": [
        re.compile(
            # 服務態度
            r"店員|服務|態度|店長|員工|工讀生|小姐|先生"
            r"|親切|友善|熱情|有禮貌|不耐|傲慢|催促|語氣|臭臉|臉色|冷漠|沒禮貌|不爽|沒理"
        ),
        re.compile(
            # 點餐/出餐正確性
            r"點錯|做錯|錯.*單|漏單|拿錯|給錯|沒.*加.*料|忘.*加|送錯|拿到.*不是|被漏"
        ),
        re.compile(
            # 速度
            r"等.*久|很慢|很快|超快|超久|超慢|拖|耗時|秒速|迅速"
            r"|分鐘|排隊"
        ),
        re.compile(
            # 主動推薦/介紹
            r"店員.*推薦|主動.*推薦|店員.*介紹|親自.*推薦"
        ),
        re.compile(
            # 出杯品管
            r"封口|灑出|沒封|蓋蓋|溢出|破.*杯|封貼|杯口"
        ),
    ],

    "品牌形象": [
        re.compile(
            # 品牌認同 - 強烈版
            r"忠實|每次都|粉絲|大推|超.*推|永遠.*愛|心心念念|我的最愛|最愛|愛.*季緣"
            r"|跟到|追店|追到|心目中|我永遠|信任|印象|品牌.*好|久仰|聞名|名聲"
            r"|愛喝|愛去|愛.*飲|喜歡這家|喜歡這間|喜歡這店|喜歡.*家.*店|非常喜歡.*店"
            r"|心中第一|心中.{0,2}TOP|心中.*最|TOP\s?\d|No\.?\s?\d|台灣.*No|手搖.*第一"
        ),
        re.compile(
            # 品牌認同 - 日常忠誠訊號
            r"常買|常喝|每.{0,2}週.{0,4}喝|每.{0,2}天.{0,4}喝|每.{0,2}月.{0,4}喝"
            r"|經過.{0,3}買|經過.{0,3}喝|路過.{0,3}買|每次.{0,3}來"
            r"|愛店|我的愛|喜歡的飲料店|喜歡的店|最頂|台灣最頂"
            r"|值得信賴|唯一支持|越.{0,2}越好|喝.{0,3}年|喝了.{0,4}年|多年.{0,2}愛"
            r"|期待.{0,3}很久|期待.{0,2}已久|心目中.{0,3}"
        ),
        re.compile(
            # 失望/不再來 (品牌承諾打破)
            r"失望|不會再|不再來|永遠不.*來|永遠不.*喝|避雷|這輩子.*不|沒辦法再"
        ),
    ],

    "定價價格": [
        re.compile(
            r"貴|偏高|不便宜|不值|不划算|劃算|划算"
            r"|cp ?值|c\.?p\.?值|cp/?值|cp 值|cp/值|cp"
            r"|便宜|高價|低價|物超所值|物美價廉"
            r"|\d+ ?元|\d+ ?塊|售價|價格|價位|單價|花.*錢|錢.*值|錢.*不值"
            r"|折價券|優惠券|折\d+|滿\d+|\$\d+|買一送一|買.*送"
        ),
    ],

    "營運體驗": [
        re.compile(
            # 動線、店內環境、排隊管理、系統
            r"動線|環境|店內|室內|座位|空間|裝潢"  # 注:裝潢同時也是行銷,在這列入因為動線/環境也屬營運
            r"|排隊.*混亂|插隊|排隊.*動線|點餐.*系統|點餐.*APP|APP|系統|介面"
            r"|外送.*單|外送員|外送.*多|現場.*等|現場.*久|外送.*壓"
            r"|沒.*提醒|沒.*告知|斷貨|沒.*供應|缺貨|售完|沒了|公告|事先"
            r"|生意.*好|人潮.*多|休息|營業.*時間|假日.*休|打電話.*接|電話.*打不|電話.*不通|電話.*無人"
            r"|沒.*扣|忘.*扣|環保杯|自帶杯"
        ),
        re.compile(
            # 發票/帳務
            r"發票|統編|退錢|帳|對帳|帳款|收費.*錯"
        ),
    ],

    "行銷吸引": [
        re.compile(
            # 季節限定/新品
            r"回歸|終於.*來|終於.*開|終於.*回|終於.*等到|終於有.*新|新店|新開.*店|新開的"
            r"|新品|新口味|季節|限定|期間限定|限時|期待.*已久|等很久|永遠買不到|搶不到|預售|預購"
            r"|蓮霧.*回|蜜桃.*回|季緣.*來了|.*來了.*商圈|商圈|新的點"
        ),
        re.compile(
            # 視覺/打卡/店面
            r"打卡|拍照|漂亮|很美|美美|高級感|質感|高級|有質感|風格|外觀"
            r"|店面.*漂亮|店面.*美"
        ),
    ],
}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def classify_topics(text: str) -> List[str]:
    """Return the list of NPS 領域 a review touches (multi-label)."""
    if not text or not text.strip():
        return []
    topics = []
    for category, patterns in NPS_CATEGORY_RULES.items():
        if any(p.search(text) for p in patterns):
            topics.append(category)
    return topics


def sentiment_group(rating: int) -> str:
    """Map a 1-5 star rating to NPS-style sentiment group."""
    if rating >= 5:
        return "promoter"
    if rating <= 2:
        return "detractor"
    return "passive"


# Public list of all 6 categories in display order (matches dashboard / NPS chart)
NPS_CATEGORIES_ORDERED = list(NPS_CATEGORY_RULES.keys())
