import io
import re
import zipfile
from datetime import datetime
from html import unescape

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

APP_TITLE = "보험사 DART FY26 1Q 실적 비교"
BSNS_YEAR = "2026"
REPRT_CODE = "11013"  # 1분기보고서
REPORT_LABEL = "FY2026 1Q"

LIFE_INSURERS = ["삼성생명", "한화생명", "교보생명", "신한라이프", "NH농협생명"]
NONLIFE_INSURERS = ["삼성화재", "메리츠화재", "DB손해보험", "현대해상", "KB손해보험"]
DISPLAY_NAMES = {
    "DB손해보험": "DB손보",
    "KB손해보험": "KB손보",
    "NH농협생명": "NH농협생명",
}

# 회사명 검색 후보. DART 고유번호 파일에서 실제 사명이 다를 수 있어 후보를 둔다.
CORP_ALIASES = {
    "삼성생명": ["삼성생명", "삼성생명보험"],
    "한화생명": ["한화생명", "한화생명보험"],
    "교보생명": ["교보생명", "교보생명보험"],
    "신한라이프": ["신한라이프", "신한라이프생명보험"],
    "NH농협생명": ["농협생명", "NH농협생명", "엔에이치농협생명", "농협생명보험"],
    "삼성화재": ["삼성화재", "삼성화재해상보험"],
    "메리츠화재": ["메리츠화재", "메리츠화재해상보험"],
    "DB손해보험": ["DB손해보험", "디비손해보험", "동부화재해상보험"],
    "현대해상": ["현대해상", "현대해상화재보험"],
    "KB손해보험": ["KB손해보험", "케이비손해보험", "엘아이지손해보험"],
}

FIN_TARGETS = {
    "당기순이익": ["당기순이익", "분기순이익", "반기순이익", "연결당기순이익", "지배기업의 소유주에게 귀속되는 당기순이익"],
    "보험서비스손익": ["보험서비스손익", "보험손익"],
    "투자서비스손익": ["투자서비스손익", "투자손익", "투자영업손익"],
}

NOTE_TARGETS = {
    "신계약 CSM": ["신계약 csm", "신계약csm", "신계약 보험계약서비스마진", "신계약 보험계약 서비스마진"],
    "기말 CSM 잔액": ["기말 csm", "기말csm", "csm 잔액", "보험계약서비스마진 잔액", "보험계약 서비스마진 잔액", "기말 보험계약서비스마진"],
    "예실차": ["예실차", "예상과 실제의 차이", "경험조정", "experience variance"],
    "K-ICS 비율": ["k-ics", "kics", "킥스", "지급여력비율", "보험금지급능력"],
}

DART_BASE = "https://opendart.fss.or.kr/api"

st.set_page_config(page_title=APP_TITLE, layout="wide", page_icon="📊")


def api_get_json(endpoint: str, params: dict) -> dict:
    url = f"{DART_BASE}/{endpoint}"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    status = data.get("status")
    if status not in ("000", "013"):
        raise RuntimeError(f"DART API 오류 {status}: {data.get('message')}")
    return data


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def load_corp_codes(api_key: str) -> pd.DataFrame:
    url = f"{DART_BASE}/corpCode.xml"
    r = requests.get(url, params={"crtfc_key": api_key}, timeout=60)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml = zf.read(zf.namelist()[0]).decode("utf-8", errors="ignore")
    soup = BeautifulSoup(xml, "xml")
    rows = []
    for item in soup.find_all("list"):
        rows.append({
            "corp_code": item.corp_code.text.strip() if item.corp_code else "",
            "corp_name": item.corp_name.text.strip() if item.corp_name else "",
            "stock_code": item.stock_code.text.strip() if item.stock_code else "",
            "modify_date": item.modify_date.text.strip() if item.modify_date else "",
        })
    return pd.DataFrame(rows)


def resolve_corp_code(corp_df: pd.DataFrame, company: str):
    aliases = CORP_ALIASES.get(company, [company])
    # 1) 정확히 일치
    for alias in aliases:
        m = corp_df[corp_df["corp_name"].str.fullmatch(re.escape(alias), na=False)]
        if not m.empty:
            row = m.iloc[0]
            return row["corp_code"], row["corp_name"]
    # 2) 포함 검색
    for alias in aliases:
        m = corp_df[corp_df["corp_name"].str.contains(alias, na=False, regex=False)]
        if not m.empty:
            # 상장 종목 우선, 없으면 첫 번째
            listed = m[m["stock_code"].astype(str).str.len() > 0]
            row = (listed.iloc[0] if not listed.empty else m.iloc[0])
            return row["corp_code"], row["corp_name"]
    return None, None


def load_financials(api_key: str, corp_code: str, fs_div: str) -> list:
    data = api_get_json("fnlttSinglAcntAll.json", {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": BSNS_YEAR,
        "reprt_code": REPRT_CODE,
        "fs_div": fs_div,
    })
    return data.get("list", []) or []


def to_number(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in ("-", "—"):
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
    s = re.sub(r"[^0-9.-]", "", s)
    if s in ("", ".", "-", "-."):
        return None
    try:
        n = float(s)
        return -n if neg else n
    except ValueError:
        return None


def won_to_eok(value):
    n = to_number(value)
    if n is None:
        return None
    return round(n / 100_000_000, 1)


def pick_account(accounts: list, keywords: list):
    if not accounts:
        return None, None
    candidates = []
    for row in accounts:
        nm = str(row.get("account_nm", "")).strip()
        sj_nm = str(row.get("sj_nm", "")).strip()
        val = row.get("thstrm_amount")
        if val in (None, ""):
            continue
        compact_nm = re.sub(r"\s+", "", nm.lower())
        for kw in keywords:
            compact_kw = re.sub(r"\s+", "", kw.lower())
            if compact_kw and compact_kw in compact_nm:
                score = 0
                if nm == kw:
                    score += 10
                if "손익" in sj_nm or "포괄" in sj_nm:
                    score += 2
                candidates.append((score, row))
                break
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0], reverse=True)
    row = candidates[0][1]
    return won_to_eok(row.get("thstrm_amount")), row.get("account_nm")


def get_preferred_financials(api_key: str, corp_code: str):
    accounts = load_financials(api_key, corp_code, "CFS")
    fs_used = "연결"
    if not accounts:
        accounts = load_financials(api_key, corp_code, "OFS")
        fs_used = "별도"
    result = {}
    source_accounts = {}
    for item, kws in FIN_TARGETS.items():
        val, acct = pick_account(accounts, kws)
        result[item] = val
        source_accounts[item] = acct
    return result, fs_used, source_accounts


def find_q1_receipt(api_key: str, corp_code: str):
    # 2026년 1분기보고서 접수번호 검색. 정확도 위해 4~5월 범위를 넓게 둔다.
    data = api_get_json("list.json", {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": "20260401",
        "end_de": "20260531",
        "pblntf_ty": "A",
        "page_no": "1",
        "page_count": "100",
    })
    rows = data.get("list", []) or []
    for row in rows:
        name = row.get("report_nm", "")
        if "분기보고서" in name and "2026.03" in name:
            return row.get("rcept_no"), name
    for row in rows:
        name = row.get("report_nm", "")
        if "분기보고서" in name:
            return row.get("rcept_no"), name
    return None, None


@st.cache_data(show_spinner=False, ttl=60 * 60 * 12)
def load_document_text(api_key: str, rcept_no: str) -> str:
    url = f"{DART_BASE}/document.xml"
    r = requests.get(url, params={"crtfc_key": api_key, "rcept_no": rcept_no}, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        texts = []
        for name in zf.namelist():
            raw = zf.read(name)
            for enc in ["utf-8", "euc-kr", "cp949"]:
                try:
                    s = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    s = raw.decode("utf-8", errors="ignore")
            soup = BeautifulSoup(s, "lxml")
            texts.append(soup.get_text(" "))
    text = unescape(" ".join(texts))
    text = re.sub(r"\s+", " ", text)
    return text


def extract_near_keyword(text: str, keywords: list, is_percent=False):
    if not text:
        return None, "본문 없음"
    lower = text.lower()
    for kw in keywords:
        idx = lower.find(kw.lower())
        if idx >= 0:
            window = text[max(0, idx - 300): idx + 900]
            if is_percent:
                m = re.search(r"(-?\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*%", window)
                if m:
                    return float(m.group(1).replace(",", "")), window[:180]
            nums = re.findall(r"\(?-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?|\(?-?\d+(?:\.\d+)?\)?", window)
            # 숫자가 너무 많을 수 있으므로 키워드 이후 첫 숫자 우선
            after = text[idx: idx + 900]
            nums_after = re.findall(r"\(?-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?|\(?-?\d+(?:\.\d+)?\)?", after)
            nums = nums_after or nums
            if nums:
                n = to_number(nums[0])
                if n is not None:
                    # 주석 표는 백만원/억원/원 혼재. 주변 단어로 단위 추정
                    unit_window = window.lower()
                    if "백만원" in unit_window or "단위: 백만" in unit_window:
                        val = round(n / 100, 1)  # 백만원 -> 억원
                    elif "억원" in unit_window or "단위: 억" in unit_window:
                        val = round(n, 1)
                    elif "원" in unit_window:
                        val = round(n / 100_000_000, 1)
                    else:
                        # 알 수 없으면 큰 숫자는 원으로, 작은 숫자는 억원으로 추정하지 않고 None 처리
                        val = round(n, 1) if abs(n) < 1_000_000 else round(n / 100_000_000, 1)
                    return val, window[:180]
            return None, window[:180]
    return None, "키워드 미발견"


def format_value(item, val):
    if val is None:
        return "미확인"
    if "K-ICS" in item:
        return f"{val:.1f}%"
    return f"{val:,.1f}억"


def collect_company(api_key: str, corp_df: pd.DataFrame, company: str):
    corp_code, dart_name = resolve_corp_code(corp_df, company)
    out = {
        "회사": company,
        "표시회사": DISPLAY_NAMES.get(company, company),
        "DART회사명": dart_name or "미확인",
        "corp_code": corp_code or "미확인",
        "재무제표기준": "미확인",
        "접수번호": "미확인",
        "보고서명": "미확인",
        "추출상태": "",
    }
    for item in list(FIN_TARGETS) + list(NOTE_TARGETS):
        out[item] = None
        out[f"{item}_근거"] = ""

    if not corp_code:
        out["추출상태"] = "회사코드 미확인"
        return out

    try:
        fin, fs_used, src = get_preferred_financials(api_key, corp_code)
        out["재무제표기준"] = fs_used
        for k, v in fin.items():
            out[k] = v
            out[f"{k}_근거"] = src.get(k) or "계정명 미확인"
    except Exception as e:
        out["추출상태"] += f"재무제표 오류: {e}; "

    try:
        rcept_no, report_nm = find_q1_receipt(api_key, corp_code)
        out["접수번호"] = rcept_no or "미확인"
        out["보고서명"] = report_nm or "미확인"
        if rcept_no:
            text = load_document_text(api_key, rcept_no)
            for item, kws in NOTE_TARGETS.items():
                val, ctx = extract_near_keyword(text, kws, is_percent=("K-ICS" in item))
                out[item] = val
                out[f"{item}_근거"] = ctx
        else:
            out["추출상태"] += "FY26 1Q 접수번호 미확인; "
    except Exception as e:
        out["추출상태"] += f"본문 파싱 오류: {e}; "

    if not out["추출상태"]:
        out["추출상태"] = "완료"
    return out


def build_comparison(rows, companies, items):
    df = pd.DataFrame(rows)
    table = pd.DataFrame({"항목": items})
    for comp in companies:
        sub = df[df["회사"] == comp]
        label = DISPLAY_NAMES.get(comp, comp)
        if sub.empty:
            table[label] = "미확인"
        else:
            row = sub.iloc[0]
            suffix = "(별도)" if row.get("재무제표기준") == "별도" else ""
            vals = []
            for item in items:
                f = format_value(item, row.get(item))
                if suffix and item in FIN_TARGETS and f != "미확인":
                    f += suffix
                vals.append(f)
            table[label] = vals
    return table


def make_comments(rows, segment_name):
    df = pd.DataFrame(rows)
    comments = []
    # 재무 3개 핵심항목 평균 확인
    for item in ["당기순이익", "보험서비스손익", "투자서비스손익"]:
        s = pd.to_numeric(df[item], errors="coerce")
        if s.notna().any():
            top_idx = s.idxmax()
            comments.append(f"{segment_name}에서는 {df.loc[top_idx, '표시회사']}의 {item}이 가장 크게 나타났습니다. 단, 연결/별도 기준 혼재 여부와 일회성 투자손익 영향을 함께 확인해야 합니다.")
            break
    csm = pd.to_numeric(df["기말 CSM 잔액"], errors="coerce")
    kics = pd.to_numeric(df["K-ICS 비율"], errors="coerce")
    if csm.notna().any():
        top = df.loc[csm.idxmax(), "표시회사"]
        comments.append(f"CSM은 장래 보험이익 체력을 보는 보조지표이므로, {top}의 기말 CSM 잔액과 신계약 CSM 흐름을 보험서비스손익과 함께 비교하는 것이 유효합니다.")
    if kics.notna().any():
        top = df.loc[kics.idxmax(), "표시회사"]
        comments.append(f"K-ICS 비율은 자본여력 지표이므로, {top}처럼 상대적으로 높은 회사는 성장·주주환원·리스크 흡수 여력 측면에서 별도 점검 가치가 있습니다.")
    if not comments:
        comments.append(f"{segment_name} 주요 주석 항목 일부는 자동 파싱이 되지 않았습니다. 원문 주석 표기 차이로 인한 것으로, 수동 확인 후 키워드 룰을 보강해야 합니다.")
    return comments[:3]


def to_excel(life_table, nonlife_table, raw_rows, life_comments, nonlife_comments):
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="xlsxwriter") as writer:
        workbook = writer.book
        title_fmt = workbook.add_format({"bold": True, "font_size": 16, "font_color": "#1F2937"})
        header_fmt = workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1, "align": "center"})
        cell_fmt = workbook.add_format({"border": 1, "align": "right"})
        item_fmt = workbook.add_format({"border": 1, "bold": True, "align": "left", "bg_color": "#F3F4F6"})
        note_fmt = workbook.add_format({"text_wrap": True, "valign": "top", "bg_color": "#FFF7ED", "border": 1})

        # Executive sheet
        ws = workbook.add_worksheet("임원보고")
        writer.sheets["임원보고"] = ws
        ws.write("A1", f"보험사 주요 경영실적 비교 ({REPORT_LABEL})", title_fmt)
        ws.write("A2", f"생성시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        row = 4
        for title, table, comments in [("생명보험 5개사", life_table, life_comments), ("손해보험 5개사", nonlife_table, nonlife_comments)]:
            ws.write(row, 0, title, title_fmt)
            row += 1
            for c, col in enumerate(table.columns):
                ws.write(row, c, col, header_fmt)
            row += 1
            for _, r in table.iterrows():
                for c, col in enumerate(table.columns):
                    ws.write(row, c, r[col], item_fmt if c == 0 else cell_fmt)
                row += 1
            row += 1
            ws.write(row, 0, "시사점", header_fmt)
            ws.merge_range(row, 1, row, min(5, len(table.columns)-1), "\n".join([f"- {x}" for x in comments]), note_fmt)
            row += 3
        ws.set_column(0, 0, 20)
        ws.set_column(1, 6, 18)

        life_table.to_excel(writer, sheet_name="생보비교", index=False)
        nonlife_table.to_excel(writer, sheet_name="손보비교", index=False)
        pd.DataFrame(raw_rows).to_excel(writer, sheet_name="원천추출", index=False)
    bio.seek(0)
    return bio


def render_table(table):
    st.dataframe(table, use_container_width=True, hide_index=True)


st.title("📊 보험사 DART 실적 비교 에이전트")
st.caption("개인용 · 모바일 웹앱 · FY2026 1Q 전용 MVP")

with st.sidebar:
    st.header("설정")
    api_key = st.text_input("DART OpenAPI Key", type="password", help="키는 서버에 저장하지 않고 현재 실행 세션에서만 사용합니다.")
    st.write("조회 기준")
    st.code(f"사업연도 {BSNS_YEAR} / 1분기보고서 {REPRT_CODE}")
    run = st.button("FY26 1Q 실적 불러오기", type="primary", use_container_width=True)

st.markdown("""
### 산출물 구성
- 생명보험 5개사 / 손해보험 5개사 비교표
- 연결 우선, 없으면 별도 기준 표기
- 억원/% 단위 변환
- 임원보고용 2~3줄 코멘트
- Excel 다운로드
""")

if run:
    if not api_key:
        st.error("DART API 키를 입력해주세요.")
        st.stop()

    progress = st.progress(0)
    status = st.empty()
    try:
        status.info("DART 회사 고유번호를 불러오는 중입니다.")
        corp_df = load_corp_codes(api_key)
        all_companies = LIFE_INSURERS + NONLIFE_INSURERS
        rows = []
        for i, comp in enumerate(all_companies, start=1):
            status.info(f"{comp} 자료 수집·정리 중... ({i}/{len(all_companies)})")
            rows.append(collect_company(api_key, corp_df, comp))
            progress.progress(i / len(all_companies))

        items = ["당기순이익", "보험서비스손익", "투자서비스손익", "신계약 CSM", "기말 CSM 잔액", "예실차", "K-ICS 비율"]
        life_rows = [r for r in rows if r["회사"] in LIFE_INSURERS]
        nonlife_rows = [r for r in rows if r["회사"] in NONLIFE_INSURERS]
        life_table = build_comparison(life_rows, LIFE_INSURERS, items)
        nonlife_table = build_comparison(nonlife_rows, NONLIFE_INSURERS, items)
        life_comments = make_comments(life_rows, "생명보험")
        nonlife_comments = make_comments(nonlife_rows, "손해보험")

        status.success("완료했습니다.")
        st.subheader("생명보험 5개사 비교")
        render_table(life_table)
        st.info("\n".join([f"- {x}" for x in life_comments]))

        st.subheader("손해보험 5개사 비교")
        render_table(nonlife_table)
        st.info("\n".join([f"- {x}" for x in nonlife_comments]))

        excel = to_excel(life_table, nonlife_table, rows, life_comments, nonlife_comments)
        st.download_button(
            "임원보고형 Excel 다운로드",
            data=excel,
            file_name=f"보험사_DART_{REPORT_LABEL}_실적비교.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

        with st.expander("원천 추출 상태 확인"):
            status_cols = ["회사", "DART회사명", "corp_code", "재무제표기준", "접수번호", "보고서명", "추출상태"]
            st.dataframe(pd.DataFrame(rows)[status_cols], use_container_width=True, hide_index=True)
            st.warning("주석/본문 항목은 회사별 공시 문구와 표 구조 차이로 오인식 가능성이 있습니다. '미확인' 항목은 원문 확인 후 키워드 룰 보강이 필요합니다.")

    except Exception as e:
        st.error(f"실행 중 오류가 발생했습니다: {e}")
        st.stop()
