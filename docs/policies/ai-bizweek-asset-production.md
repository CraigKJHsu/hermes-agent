# AI BizWeek Image Asset Production Policy

## 核心原則

AI BizWeek 圖像任務必須分成四層處理：

1. Canonical Policy：完整規則放在 managed policy，不放在 Topic memory、Prompt Memory、Mem0 摘要或聊天回憶。
2. Asset Family：`page_hero` 與 `audio_brief` 是兩個獨立資產家族，每張圖只能宣告其中一個。
3. Prompt Compiler：產製前必須先判斷 asset family，只展開該 family 的完整規則與本集變數。
4. Deterministic Rendering：固定文字、品牌區、集數、國旗、核心數字、底部節目資訊列等元素，應優先由可讀回的版面模板後製，不應完全依賴圖片模型自由排字。

## 產製順序

每一次 AI BizWeek 圖像任務必須依序處理：

1. 讀取 task-scoped Source of Truth。
2. 呼叫 `managed_policy_read`，取得完整政策、policy receipt 與 `asset_policy_guidance`。
3. 判斷每張圖的唯一 `asset_family`。
4. 驗證該 family 的必要變數是否完整。
5. 只用該 family 的完整規則編譯生成 prompt。
6. 生成或取得背景／場景影像。
7. 對固定文字與固定版面進行 deterministic overlay 或模板化後製。
8. 讀回實際圖片檔，量測像素尺寸與比例，計算 SHA-256。
9. 產出 AI 揭露、政策 receipt 與 family-specific checklist evidence。
10. 交給 Grace review 檢查實際圖片與 metadata。

## Page Hero 與 Audio Brief 邊界

`page_hero` 不得包含 Audio Brief 固定版型元素，例如：

- `AI BizWeek` 左上 Podcast 品牌區
- `AUDIO BRIEF` 標籤
- 右上 EP 與國旗方框
- Podcast 麥克風與耳機圖示
- 底部節目資訊列

`audio_brief` 不得退化為 Page Hero 或一般 Facebook 資訊圖。它必須保留固定八區版面，包括品牌區、EP／國旗、核心指標、右側產業情境、產業看板、AI 槓桿流程、人物／品牌摘要與底部節目資訊列。

## Audio Brief Deterministic Rendering

Audio Brief 的固定系列元素太多，不得完全交給圖片模型自由生成與自由排字。

固定元素應優先用 HTML、Canvas、Pillow、SVG-to-PNG 或其他可讀回模板後製：

- `AI BizWeek`
- `一人公司商業誌`
- `AUDIO BRIEF`
- EP 編號
- 國旗
- 核心成果說明
- 核心指標
- AI 槓桿功能標籤
- 人物或品牌名稱
- 兩行案例摘要
- `約 6–10 分鐘`
- `用聽的，理解一個值得學習的一人公司案例`

圖片模型可用於生成右側產業攝影背景、產品情境、質感、手繪筆刷與局部裝飾；不得把固定文字正確性完全交給圖片模型。

## Grace Review Fail-Closed 規則

Grace review 必須拒絕下列情況：

- 未呼叫 `managed_policy_read` 或缺少 policy receipt。
- 只提供 prompt，沒有實際圖片檔。
- 沒有實測像素尺寸、比例與 SHA-256。
- `asset_family` 未宣告、宣告多個，或與成品版型不一致。
- Page Hero 與 Audio Brief 版型混用。
- Audio Brief 未完整八區。
- 固定文字不可讀、缺漏、簡體、亂碼或被自行改寫。
- 右側產業情境與案例不符。
- 使用未在 task-scoped Source of Truth 中出現的案例事實、人物、數字或地理資訊。
- 沒有 AI 生成或 AI 輔助揭露。

Grace 不得因為 worker 表示已完成、prompt 看起來正確、圖片檔存在，或任務需要收尾，就跳過實際圖片與 metadata 驗證。
