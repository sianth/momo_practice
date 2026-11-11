import re
import time
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
from curl_cffi import requests

st.set_page_config(page_title="MOMO 商品價格分析", layout="wide")

HOME_URL = "https://www.momoshop.com.tw/"
SEARCH_URL = "https://apisearch.momoshop.com.tw/momoSearchCloud/moec/textSearch"
MAX_PAGES = 20

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://www.momoshop.com.tw",
    "Referer": "https://www.momoshop.com.tw/",
}


# ----------------------------- 爬蟲邏輯 -----------------------------
def build_payload(keyword, page):
    return {
        "host": "momoshop",
        "flag": "searchEngine",
        "data": {
            "searchValue": keyword,
            "searchType": "1",
            "cateLevel": "-1",
            "curPage": str(page),
            "priceS": "0",
            "priceE": "9999999",
            "showType": "chessboardType",
            "isFuzzy": "0",
            "rtnCateDatainfo": {
                "cateCode": "",
                "cateLv": "-1",
                "keyword": keyword,
                "curPage": str(page),  # 與外層一致
                "historyDoPush": "false",
                "timestamp": int(time.time() * 1000),  # 每頁用當下時間
            },
            "flag": 2018,
            "serviceCode": "MT01",
            "addressSearchData": {},
        },
    }


def fetch_products(keyword, pages, interval, on_progress):
    """逐頁抓取 MOMO 搜尋結果。回傳 (DataFrame, 警告訊息清單)。"""
    rows, warnings = [], []
    session = requests.Session(impersonate="chrome")  # 模擬 Chrome 的 TLS 指紋與 UA
    try:
        session.get(HOME_URL, headers=HEADERS, timeout=20)  # 先取得 cookie

        for p in range(1, pages + 1):
            on_progress((p - 1) / pages, f"抓取第 {p} / {pages} 頁…")
            try:
                resp = session.post(
                    SEARCH_URL, headers=HEADERS, json=build_payload(keyword, p), timeout=20
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}:{resp.text[:200]}")
                try:
                    body = resp.json()
                except ValueError:
                    raise RuntimeError(f"回傳不是 JSON,可能被擋:{resp.text[:200]}")
            except Exception as e:
                if p == 1:
                    raise  # 第一頁就失敗,直接回報錯誤
                warnings.append(f"第 {p} 頁失敗,已保留前面的結果:{type(e).__name__}: {e}")
                break

            products = (body.get("rtnSearchData") or {}).get("goodsInfoList") or []
            if not products:  # 該頁沒資料,提前結束
                break

            for item in products:
                rows.append({
                    "品名": item.get("goodsName", ""),
                    "價格": item.get("goodsPrice", ""),
                    "商品編號": item.get("goodsCode", ""),
                    "頁碼": p,
                })
            if p < pages:
                time.sleep(interval)  # 禮貌性間隔
    finally:
        session.close()

    on_progress(1.0, "完成")
    return pd.DataFrame(rows), warnings


def parse_price(series):
    """取出字串中的第一組數字(如 '$1,273' -> 1273),避免價格區間被串成一個超大數字。"""
    num = series.astype(str).str.extract(r"(\d[\d,]*)", expand=False)
    return pd.to_numeric(num.str.replace(",", "", regex=False), errors="coerce")


def guess_brand(name):
    """由品名粗略推測品牌:優先取開頭【】內文字,否則取第一個詞。"""
    name = str(name).strip()
    m = re.match(r"^[【\[]([^】\]]+)[】\]]", name)
    if m:
        return m.group(1).strip()
    token = re.split(r"[\s/|｜]", name, maxsplit=1)[0]
    return token[:10] if token else "未知"


def add_price_band(prices, step):
    """依固定寬度切出價格區間(回傳字串 Series)。"""
    max_p = int(prices.max())
    edges = list(range(0, (max_p // step + 2) * step, step))
    labels = [f"{a:,}–{b - 1:,}" for a, b in zip(edges[:-1], edges[1:])]
    band = pd.cut(prices, bins=edges, labels=labels, right=False)
    return band.astype(str)


def band_sort_key(label):
    return int(label.split("–")[0].replace(",", ""))


# ------------------------------- 介面 -------------------------------
st.title("MOMO 商品價格分析")

with st.sidebar:
    st.header("搜尋條件")
    with st.form("search_form"):
        keyword = st.text_input("產品名稱", value="行動電源")
        pages = st.number_input("爬取頁數", min_value=1, max_value=MAX_PAGES, value=2, step=1)
        interval = st.slider("每頁間隔(秒)", 0.0, 10.0, 1.5, 0.5)
        submitted = st.form_submit_button("開始爬取", type="primary")

if submitted:
    if not keyword.strip():
        st.warning("請輸入產品名稱")
    else:
        bar = st.progress(0.0, text="準備中…")
        try:
            data, warns = fetch_products(
                keyword.strip(),
                int(pages),
                interval,
                lambda ratio, text: bar.progress(ratio, text=text),
            )
        except Exception as e:
            bar.empty()
            st.error(f"爬取失敗:{type(e).__name__}: {e}")
        else:
            bar.empty()
            for w in warns:
                st.warning(w)
            if data.empty:
                st.warning("沒有搜尋到任何商品,請換個關鍵字。")
            else:
                st.session_state["data"] = data
                st.session_state["meta"] = {
                    "keyword": keyword.strip(),
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }

raw = st.session_state.get("data")
if raw is None:
    st.info("請在左側輸入產品名稱與頁數,按「開始爬取」。")
    st.stop()

meta = st.session_state["meta"]

# --- 資料整理 ---
df = raw.copy()
df["價格"] = parse_price(df["價格"])
df = df[df["品名"].astype(str).str.strip() != ""]
df = df.dropna(subset=["價格"])
if df["商品編號"].astype(str).str.strip().ne("").any():
    df = df.drop_duplicates(subset=["商品編號"])
else:
    df = df.drop_duplicates(subset=["品名", "價格"])
df = df.reset_index(drop=True)

if df.empty:
    st.warning("沒有可用的價格資料(價格欄位無法解析為數字)。")
    st.stop()

df["價格"] = df["價格"].astype(int)
df["排名"] = df.index + 1
has_code = df["商品編號"].astype(str).str.strip().ne("").all()
if has_code:
    df["連結"] = "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=" + df["商品編號"].astype(str)

st.caption(f"關鍵字:{meta['keyword']}　|　抓取時間:{meta['time']}")

# --- 統計 ---
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("商品數", f"{len(df):,}")
c2.metric("平均價格", f"{df['價格'].mean():,.0f}")
c3.metric("中位數", f"{df['價格'].median():,.0f}")
c4.metric("最高價", f"{df['價格'].max():,.0f}")
c5.metric("最低價", f"{df['價格'].min():,.0f}")

# --- 圖表設定 ---
with st.sidebar:
    st.header("圖表設定")
    total = len(df)
    if total > 5:
        show_n = st.slider("折線圖顯示筆數", 5, total, min(70, total))
    else:
        show_n = total
    default_step = 500 if df["價格"].max() > 2000 else 100
    band_step = st.number_input("價格區間寬度(元)", min_value=10, value=default_step, step=10)
    top_brands = st.slider("旭日圖顯示前 N 個品牌(其餘歸「其他」)", 3, 20, 8)

df["價格區間"] = add_price_band(df["價格"], int(band_step))
df["品牌(推測)"] = df["品名"].apply(guess_brand)
top = df["品牌(推測)"].value_counts().head(top_brands).index
df["品牌分組"] = df["品牌(推測)"].where(df["品牌(推測)"].isin(top), "其他")

tab_line, tab_pie, tab_sun, tab_data = st.tabs(["折線圖", "圓餅圖", "旭日圖", "資料表"])

with tab_line:
    view = df.head(show_n)
    fig = px.line(
        view,
        x="排名",
        y="價格",
        markers=True,
        hover_name="品名",
        labels={"排名": "商品排名(依搜尋結果順序)", "價格": "價格"},
        title=f"MOMO 上「{meta['keyword']}」售價",
    )
    fig.add_hline(
        y=df["價格"].mean(),
        line_dash="dash",
        line_color="red",
        annotation_text=f"平均 {df['價格'].mean():,.0f}",
    )
    fig.update_layout(height=520)
    st.plotly_chart(fig)

with tab_pie:
    counts = df["價格區間"].value_counts()
    order = sorted(counts.index, key=band_sort_key)  # 依區間下限由小到大
    pie_df = pd.DataFrame({"價格區間": order, "商品數": [int(counts[o]) for o in order]})
    fig = px.pie(
        pie_df,
        names="價格區間",
        values="商品數",
        title="價格區間分布",
    )
    # sort 是 go.Pie 的 trace 屬性,要用 update_traces 設定(px.pie 沒有此參數)
    fig.update_traces(sort=False, textinfo="percent+label")
    fig.update_layout(height=520)
    st.plotly_chart(fig)

with tab_sun:
    st.caption("品牌為依品名開頭文字粗略推測,僅供參考。")
    fig = px.sunburst(
        df,
        path=["價格區間", "品牌分組"],
        title="價格區間 → 品牌(推測)",
    )
    fig.update_traces(textinfo="label+percent parent")
    fig.update_layout(height=620)
    st.plotly_chart(fig)

with tab_data:
    show_cols = [c for c in ["排名", "品名", "價格", "品牌(推測)", "價格區間", "連結", "頁碼"] if c in df.columns]
    st.dataframe(
        df[show_cols],
        hide_index=True,
        column_config={"連結": st.column_config.LinkColumn("連結")} if "連結" in show_cols else None,
    )
    st.download_button(
        "下載資料 (CSV)",
        data=df[show_cols].to_csv(index=False).encode("utf-8-sig"),
        file_name=f"MOMO_{meta['keyword']}_{datetime.now():%Y%m%d}.csv",
        mime="text/csv",
    )
