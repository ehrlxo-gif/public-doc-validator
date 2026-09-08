import html
import logging
import os
import re
import time
from datetime import datetime
from io import BytesIO

import pandas as pd
import pdfplumber
import streamlit as st

from hwpx_parser import load_hwpx, extract_instruction_hints, HwpxSecurityError, MAX_HWPX_FILE_SIZE

# ─── 상수 ────────────────────────────────────────────────────
MAX_FILE_SIZE   = 10 * 1024 * 1024   # 10 MB
SESSION_TIMEOUT = 30 * 60            # 30분

# ─── 워드프로세서 실기 채점 기준표 ──────────────────────────────
# 이미지로 제공된 감점표를 그대로 코드화한 것. 항목별로:
#  - "누락": 해당 요소를 아예 안 만들었을 때 감점
#  - "미수정": 만들긴 했는데 지시사항과 다르게(최대 감점 한도 내에서) 틀렸을 때 감점
#  - "세부": 구성요소 1개당 감점 범위(차등)
WORD_EXAM_RUBRIC = {
    "다단(단 설정)": {"누락": 5, "미수정": 3, "세부": None,
                    "설명": "예: 꼬리말 누락 -5 / 꼬리말은 만들었는데 색상 등을 잘못 지정 -3"},
    "쪽 테두리":     {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "제목(1)":       {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "제목(2)":       {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "제목(3)":       {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "누름틀":        {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "문단 첫 글자 장식": {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "스타일":        {"누락": 5, "미수정": 3, "세부": None, "설명": "제목(1)/(2) 등 하나라도 틀리면 각각 -3점 식으로 누적"},
    "표":            {"누락": 15, "미수정": 10, "세부": (1, 3),
                    "설명": "표 자체를 안 만들면 -15. 만들었으면 구성요소(테두리/배경/정렬 등) 1개당 1~3점 차등 감점, 최대 -10 (블록계산식과 캡션은 별도 항목)"},
    "차트":          {"누락": 15, "미수정": 10, "세부": (1, 3),
                    "설명": "차트 자체를 안 만들면 -15. 만들었으면 구성요소 1개당 1~3점 차등 감점, 최대 -10"},
    "블록 계산식":    {"누락": 5, "미수정": 3, "세부": None, "설명": "표의 감점 항목에서 제외되는 별도 채점 요소"},
    "캡션":          {"누락": 5, "미수정": 3, "세부": None, "설명": "표의 감점 항목에서 제외되는 별도 채점 요소"},
    "각주":          {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "하이퍼링크":     {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "글상자":        {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "쪽 번호":        {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "머리말":        {"누락": 5, "미수정": 3, "세부": None, "설명": None},
    "꼬리말":        {"누락": 5, "미수정": 3, "세부": None, "설명": None},
}

# 표/차트 외 '기능' 항목의 일반 규칙 (모든 개별 기능에 공통 적용)
WORD_EXAM_FUNC_MISSING = 5    # 기능 누락 시 감점
WORD_EXAM_FUNC_WRONG   = 3    # 기능은 만들었지만 미수정(지시사항과 다름) 시 감점

# 페이지/오탈자 등 별도 규칙
WORD_EXAM_PAGE_OVERFLOW = 5   # 2쪽 이상 생성 시(실격 아님, 감점만) 감점
WORD_EXAM_TYPO_WORD     = 3   # 오탈자(단어) 1개당 감점
WORD_EXAM_TYPO_HANJA    = 3   # 잘못된 한자 1개당 감점
WORD_EXAM_TRAILING_BREAK = 1  # 문서 맨 끝 강제 개행 1줄 존재 시 감점될 가능성 있음(케바케)
WORD_EXAM_TYPO_PARA_CAP  = "문단별 최대 감점 있음 (구체적 상한은 시행처 공고 기준 확인 필요)"

# 채점 3단계 안내 (참고용 텍스트, 자동 판정 대상 아님)
WORD_EXAM_STAGES = [
    "1단계 - 컴퓨터 채점: 컴퓨터 프로그램으로 채점 — 일정 점수 넘는 답안만 2단계로 감",
    "2단계 - 수작업 채점",
    "3단계 - 최종 점검",
]

# ─── 로깅 설정 ───────────────────────────────────────────────
logging.basicConfig(
    filename="validation.log",
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    encoding="utf-8"
)

# ─── 페이지 설정 ─────────────────────────────────────────────
st.set_page_config(
    page_title="행정문서 검증 시스템",
    page_icon="🏛",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─── CSS ─────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;700&display=swap');

html, body, [class*="css"], .stApp {
    font-family: 'Noto Sans KR', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif !important;
}
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0f2b5b 0%, #1a3a6b 55%, #0d1f40 100%) !important;
}
section[data-testid="stSidebar"] > div { padding-top: 0 !important; }
.stApp { background: #f1f5f9; }
.main .block-container { padding-top: 1rem; max-width: 1100px; }

.stButton > button {
    background: linear-gradient(135deg, #1a3a6b 0%, #2563eb 100%) !important;
    color: white !important; border: none !important;
    border-radius: 8px !important; padding: 12px 24px !important;
    font-weight: 600 !important; font-size: 15px !important;
    width: 100% !important; transition: all 0.2s !important;
    box-shadow: 0 3px 10px rgba(37,99,235,0.35) !important;
    letter-spacing: 0.02em !important;
}
.stButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 5px 16px rgba(37,99,235,0.5) !important;
}
[data-testid="stFileUploader"] section {
    border: 2px dashed #93c5fd !important; border-radius: 12px !important;
    background: #f0f7ff !important; transition: all 0.2s !important;
}
[data-testid="stFileUploader"] section:hover {
    border-color: #2563eb !important; background: #dbeafe !important;
}
[data-testid="stFileUploader"] section > button {
    background: #1a3a6b !important; color: white !important; border-radius: 6px !important;
}
[data-testid="stDownloadButton"] button {
    background: linear-gradient(135deg, #16a34a 0%, #15803d 100%) !important;
    color: white !important; border: none !important;
    border-radius: 8px !important; font-weight: 600 !important;
    box-shadow: 0 3px 10px rgba(22,163,74,0.3) !important; font-size: 15px !important;
}
.badge { display:inline-flex;align-items:center;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600; }
.badge-ok   { background:#dcfce7;color:#16a34a; }
.badge-warn { background:#fef3c7;color:#d97706; }
.badge-err  { background:#fee2e2;color:#dc2626; }
.badge-info { background:#dbeafe;color:#2563eb; }

.result-table { width:100%;border-collapse:collapse;font-size:13px; }
.result-table th {
    background:#1a3a6b;color:white;padding:10px 14px;text-align:left;font-size:12px;
    font-weight:600;letter-spacing:0.03em;
}
.result-table td { padding:10px 14px;border-bottom:1px solid #f3f4f6;vertical-align:middle; }
.result-table tr:last-child td { border-bottom:none; }
.result-table tr:hover td { background:#f8fafc; }
.tbl-wrap { background:white;border-radius:10px;overflow:hidden;box-shadow:0 1px 8px rgba(0,0,0,0.08);overflow-x:auto; }

.upload-hint { background:white;border-radius:10px;padding:13px 16px;margin-bottom:6px;
               box-shadow:0 1px 5px rgba(0,0,0,0.07);border-left:3px solid #2563eb; }
.upload-hint-title { font-size:13px;font-weight:700;color:#1a3a6b; }
.upload-hint-sub   { font-size:12px;color:#6b7280;margin-top:2px; }

.section-title { font-size:15px;font-weight:700;color:#1a3a6b;padding:12px 16px;
                 background:linear-gradient(90deg,#dbeafe,#f0f7ff);border-radius:8px;
                 border-left:4px solid #2563eb;margin-bottom:10px; }

@media (max-width:768px) {
    .main .block-container { padding:0.5rem !important; }
    .stat-grid { grid-template-columns:repeat(2,1fr) !important; }
    .result-table { font-size:11px !important; }
    .result-table td, .result-table th { padding:8px 8px !important; }
}

/* ── 다크모드 브라우저에서도 라이트 테마 강제 (텍스트 안 보이는 문제 방지) ── */
:root, .stApp, [data-testid="stAppViewContainer"] {
    color-scheme: light !important;
}
.stApp, .main, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    background: #f1f5f9 !important;
    color: #1f2937 !important;
}
.stApp p, .stApp span, .stApp label, .stApp div,
.stMarkdown, [data-testid="stCaptionContainer"],
[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label {
    color: #1f2937 !important;
}
[data-testid="stCaptionContainer"] p, .stCaption, small {
    color: #6b7280 !important;
}
/* 라디오/체크박스/슬라이더/넘버인풋 등 위젯 컨테이너 배경 */
[data-testid="stRadio"], [data-testid="stCheckbox"],
[data-testid="stNumberInput"], [data-testid="stSlider"],
[data-testid="stExpander"] {
    background: transparent !important;
}
[data-testid="stExpander"] summary {
    color: #1f2937 !important;
    background: #ffffff !important;
}
[data-testid="stExpander"] details {
    background: #ffffff !important;
    border-radius: 8px !important;
}
[data-testid="stNumberInput"] input {
    background: #ffffff !important;
    color: #1f2937 !important;
}
/* 사이드바는 원래 어두운 배경이므로 흰 글자 유지 */
section[data-testid="stSidebar"] * {
    color: #ffffff !important;
}
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {
    color: rgba(255,255,255,0.6) !important;
}
</style>
""", unsafe_allow_html=True)


# ─── 세션 타임아웃 ────────────────────────────────────────────
if "last_active" not in st.session_state:
    st.session_state.last_active = time.time()

_now = time.time()
if _now - st.session_state.last_active > SESSION_TIMEOUT:
    for _k in [k for k in st.session_state if k != "last_active"]:
        del st.session_state[_k]
    st.session_state.last_active = _now
    st.warning("⏱ 30분 이상 미사용으로 세션이 초기화되었습니다. 처음부터 다시 시작해주세요.")
    st.rerun()
else:
    st.session_state.last_active = _now


# ─── 헬퍼 함수 ───────────────────────────────────────────────

def safe(val: str) -> str:
    """XSS 방어: HTML 특수문자 이스케이프."""
    return html.escape(str(val))


def validate_file(f) -> tuple[bool, str]:
    """파일 크기·확장자 검증."""
    ext = os.path.splitext(f.name)[1].lower()
    if ext != ".pdf":
        return False, f"'{safe(f.name)}': PDF 파일만 허용됩니다 (현재: {safe(ext)})"
    if f.size == 0:
        return False, f"'{safe(f.name)}': 빈 파일입니다."
    if f.size > MAX_FILE_SIZE:
        return False, f"'{safe(f.name)}': {f.size/(1024*1024):.1f}MB — 최대 10MB 초과"
    return True, ""


def validate_hwpx_file(f) -> tuple[bool, str]:
    """hwpx 업로드 파일의 확장자·크기 검증. 실제 내부 구조 검증은 hwpx_parser가 담당."""
    ext = os.path.splitext(f.name)[1].lower()
    if ext != ".hwpx":
        return False, f"'{safe(f.name)}': hwpx 파일만 허용됩니다 (현재: {safe(ext)})"
    if f.size == 0:
        return False, f"'{safe(f.name)}': 빈 파일입니다."
    if f.size > MAX_HWPX_FILE_SIZE:
        return False, f"'{safe(f.name)}': {f.size/(1024*1024):.1f}MB — 최대 {MAX_HWPX_FILE_SIZE/(1024*1024):.0f}MB 초과"
    return True, ""


@st.cache_data(show_spinner=False)
def _cached_extract(file_bytes: bytes) -> tuple[str, str]:
    """PDF 바이트를 텍스트로 변환 (캐시됨). (text, error) 반환."""
    try:
        with pdfplumber.open(BytesIO(file_bytes)) as pdf:
            text = ""
            for page in pdf.pages:
                t = page.extract_text(x_tolerance=2, y_tolerance=2)
                if t:
                    text += re.sub(r'(.)\1{2,}', r'\1', t) + "\n"
            if not text.strip():
                return "", "텍스트를 추출할 수 없습니다. 스캔 이미지이거나 빈 문서일 수 있습니다."
            return text, ""
    except Exception as exc:
        msg = str(exc).lower()
        if "encrypt" in msg or "password" in msg:
            return "", "암호화된 PDF입니다. 비밀번호를 해제한 후 업로드해주세요."
        return "", "파일을 읽을 수 없습니다. 손상된 PDF이거나 지원되지 않는 형식입니다."


def get_text(f) -> tuple[str, str]:
    """UploadedFile → (text, error). 메모리에서만 처리, 디스크 미저장."""
    f.seek(0)
    return _cached_extract(f.read())


def detect_privacy(text: str) -> list[dict]:
    issues = []
    patterns = {
        "주민등록번호":    r'\d{6}-[1-4]\d{6}',
        "전화번호":        r'01[0-9]-\d{3,4}-\d{4}',
        "이메일":          r'[a-zA-Z0-9]+@[a-zA-Z0-9]+\.[a-zA-Z]{2,}',
        "생년월일(8자리)":  r'\b(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\b',
    }
    for label, pattern in patterns.items():
        matches = re.findall(pattern, text)
        if matches:
            issues.append({
                "항목": label, "내용": safe(str(matches[0])[:30]),
                "_lv": "err", "위험도": "🔴 높음", "조치": "즉시 마스킹 필요"
            })
    for kw in ["고등학교", "중학교", "대학교", "대학", "출신학교"]:
        if kw in text:
            issues.append({
                "항목": "학교명 노출", "내용": safe(kw),
                "_lv": "warn", "위험도": "🟡 중간", "조치": "블라인드 위반 확인 필요"
            })
            break
    for kw in ["아버지", "어머니", "부모님", "가족", "형제", "종교", "출신지", "고향"]:
        if kw in text:
            issues.append({
                "항목": "블라인드 위반 의심", "내용": safe(kw),
                "_lv": "warn", "위험도": "🟡 중간", "조치": "내용 수정 필요"
            })
    return issues


def detect_dates(text: str, deadline: datetime) -> list[dict]:
    issues = []
    for pattern in [
        r'(20\d{2})[.\-/](0[1-9]|1[0-2])[.\-/](0[1-9]|[12]\d|3[01])',
        r'(20\d{2})년\s*(0?[1-9]|1[0-2])월\s*(0?[1-9]|[12]\d|3[01])일',
    ]:
        for m in re.findall(pattern, text):
            try:
                d = datetime(int(m[0]), int(m[1]), int(m[2]))
                if d > deadline:
                    issues.append({
                        "항목": "기준일 초과 날짜",
                        "내용": safe(d.strftime("%Y년 %m월 %d일")),
                        "_lv": "err", "위험도": "🔴 높음",
                        "조치": safe(f"마감일({deadline.strftime('%Y.%m.%d')}) 이후 — 자격 미충족")
                    })
            except Exception:
                pass
    return issues


def score_word_exam(selections: dict) -> tuple[list[dict], int]:
    """
    워드프로세서 실기 채점기.
    selections: {
        항목명: {"status": "정상"|"누락"|"미수정", "세부감점": int(선택)},
        ...
        "_page_overflow": bool,
        "_typo_words": int,
        "_typo_hanja": int,
        "_trailing_break": bool,
    }
    반환: (감점 상세 rows, 총 감점)
    """
    rows: list[dict] = []
    total = 0

    for name, rule in WORD_EXAM_RUBRIC.items():
        sel = selections.get(name, {})
        status = sel.get("status", "정상")

        if status == "누락":
            penalty = rule["누락"]
            total += penalty
            rows.append({
                "항목": name, "상태": "❌ 누락", "감점": f"-{penalty}점",
                "_lv": "err", "비고": rule["설명"] or "-"
            })
        elif status == "미수정":
            if rule["세부"]:
                lo, hi = rule["세부"]
                penalty = sel.get("세부감점", hi)
                penalty = max(lo, min(hi, penalty))
            else:
                penalty = rule["미수정"]
            total += penalty
            rows.append({
                "항목": name, "상태": "⚠️ 미수정(오류)", "감점": f"-{penalty}점",
                "_lv": "warn", "비고": rule["설명"] or "-"
            })
        else:
            rows.append({
                "항목": name, "상태": "✅ 정상", "감점": "0점",
                "_lv": "ok", "비고": "-"
            })

    # 페이지 초과
    if selections.get("_page_overflow"):
        total += WORD_EXAM_PAGE_OVERFLOW
        rows.append({
            "항목": "페이지 초과(2쪽 이상 생성)", "상태": "❌ 위반",
            "감점": f"-{WORD_EXAM_PAGE_OVERFLOW}점", "_lv": "err",
            "비고": "실격 아님, 감점만 적용"
        })

    # 오탈자(단어)
    n_typo = selections.get("_typo_words", 0)
    if n_typo:
        p = n_typo * WORD_EXAM_TYPO_WORD
        total += p
        rows.append({
            "항목": "오탈자(단어)", "상태": f"{n_typo}개 발견",
            "감점": f"-{p}점", "_lv": "err",
            "비고": f"1개당 {WORD_EXAM_TYPO_WORD}점, {WORD_EXAM_TYPO_PARA_CAP}"
        })

    # 한자 오류
    n_hanja = selections.get("_typo_hanja", 0)
    if n_hanja:
        p = n_hanja * WORD_EXAM_TYPO_HANJA
        total += p
        rows.append({
            "항목": "한자 오류", "상태": f"{n_hanja}개 발견",
            "감점": f"-{p}점", "_lv": "err",
            "비고": f"1개당 {WORD_EXAM_TYPO_HANJA}점"
        })

    # 문서 끝 강제 개행
    if selections.get("_trailing_break"):
        rows.append({
            "항목": "문서 맨 끝 강제 개행(1줄)", "상태": "⚠️ 존재",
            "감점": f"최대 -{WORD_EXAM_TRAILING_BREAK}점 가능",
            "_lv": "warn", "비고": "계산상 -1점으로 처리되는 경우가 있음(확정 아님)"
        })

    return rows, total


# ─── hwpx 자동 대조 채점 ─────────────────────────────────────

@st.cache_data(show_spinner=False)
def _cached_load_hwpx(file_bytes: bytes):
    """hwpx 파싱 결과 캐시. 실패 시 (None, 에러메시지)."""
    try:
        doc = load_hwpx(file_bytes)
        return doc, ""
    except HwpxSecurityError as exc:
        # 크기·압축률·DOCTYPE 등 보안 기준 위반 — 사유를 그대로 안내해도 안전함
        # (내부 경로나 스택트레이스가 아니라 우리가 직접 작성한 안내문이기 때문)
        return None, f"파일이 안전 기준을 벗어나 처리할 수 없습니다: {exc}"
    except Exception:
        # 그 외 예외는 원문을 노출하지 않고 일반화된 메시지만 반환
        # (파이썬 예외 원문에 내부 경로 등 불필요한 정보가 섞여 나올 수 있어 차단)
        return None, "hwpx 파일을 읽을 수 없습니다. 올바른 hwpx 파일인지 확인해주세요."


def get_hwpx_doc(f):
    f.seek(0)
    return _cached_load_hwpx(f.read())


def auto_check_hwpx(instruction_text: str, doc) -> list[dict]:
    """
    지시사항 텍스트에서 뽑은 힌트와 hwpx 구조를 대조해서
    자동으로 확인 가능한 항목들을 검출 리포트로 만든다.
    이건 '없다/있다' 및 '숫자가 지시사항 안 어딘가에 등장하는지' 수준의
    보조 확인이며, 최종 판단은 사람이 표를 보고 확정해야 한다.
    """
    hints = extract_instruction_hints(instruction_text)
    rows: list[dict] = []

    def add(item, ok, detail):
        rows.append({
            "항목": item,
            "상태": "✅ 검출됨" if ok else "❌ 미검출",
            "_lv": "ok" if ok else "err",
            "세부내용": detail
        })

    # 표
    if hints.get("표_요구"):
        if doc.tables:
            t = doc.tables[0]
            border = t.border or {}
            b_desc = ", ".join(
                f'{k}:{v["type"]}/{v["width_mm"]}mm/{v["color"]}'
                for k, v in border.items()
            ) or "정보 없음"
            add("표", True,
                f'{t.row_cnt}행×{t.col_cnt}열, 크기 {t.outer_width_mm}×{t.outer_height_mm}mm, '
                f'캡션 {"있음(" + str(t.caption_side) + ")" if t.has_caption else "없음"}, 테두리[{b_desc}]')
        else:
            add("표", False, "지시사항에 표가 언급되었지만 문서에서 표를 찾지 못했습니다.")

    # 차트
    if hints.get("차트_요구"):
        has_chart = any(c.exists for c in doc.charts)
        add("차트", has_chart,
            f'차트 XML {"발견" if has_chart else "미발견"} '
            f'({[c.raw_xml_path for c in doc.charts if c.exists]})')

    # 단(컬럼) 설정
    if hints.get("단수") or hints.get("단간격_mm"):
        multi_col = [c for c in doc.columns if c.col_count and c.col_count > 1]
        if multi_col:
            c = multi_col[0]
            gap_ok = ""
            if hints.get("단간격_mm") and c.same_gap_mm is not None:
                diff = abs(c.same_gap_mm - hints["단간격_mm"])
                gap_ok = " (지시사항 값과 일치)" if diff < 0.5 else \
                         f" (지시사항 {hints['단간격_mm']}mm ≠ 문서 {c.same_gap_mm}mm — 확인 필요)"
            add("단(컬럼) 설정", True, f'{c.col_count}단, 간격 {c.same_gap_mm}mm{gap_ok}')
        else:
            add("단(컬럼) 설정", False, "지시사항에 다단 구성이 언급되었지만 2단 이상 구간을 찾지 못했습니다.")

    # 문단 첫 글자 장식
    if hints.get("첫글자장식_요구"):
        exists = any(d.exists for d in doc.dropcaps)
        style = next((d.style for d in doc.dropcaps if d.exists), None)
        add("문단 첫 글자 장식", exists, f"스타일: {style}" if exists else "미검출")

    # 각주
    if hints.get("각주_요구"):
        add("각주", doc.footnotes.count > 0, f"{doc.footnotes.count}개 검출")

    # 하이퍼링크
    if hints.get("하이퍼링크_요구"):
        add("하이퍼링크", bool(doc.hyperlinks.urls), f"검출된 URL: {doc.hyperlinks.urls or '없음'}")

    # 누름틀
    if hints.get("누름틀_요구"):
        add("누름틀", doc.fields.count > 0, f"{doc.fields.count}개 검출 (타입: {doc.fields.types})")

    # 머리말/꼬리말
    if hints.get("머리말_요구"):
        add("머리말", doc.header_footer.has_header, "검출됨" if doc.header_footer.has_header else "미검출")
    if hints.get("꼬리말_요구"):
        add("꼬리말", doc.header_footer.has_footer, "검출됨" if doc.header_footer.has_footer else "미검출")

    # 쪽번호
    if hints.get("쪽번호_요구"):
        add("쪽 번호", doc.page_num.exists,
            f"위치: {doc.page_num.position}, 모양: {doc.page_num.format_type}" if doc.page_num.exists else "미검출")

    # 캡션 (표 안에 포함되어 있지만 지시사항에 별도로 강조되면 별도 표기)
    if hints.get("캡션_요구") and doc.tables:
        t = doc.tables[0]
        add("캡션", t.has_caption, f"위치: {t.caption_side}" if t.has_caption else "미검출")

    if not rows:
        rows.append({
            "항목": "자동 대조", "상태": "ℹ️ 힌트 없음", "_lv": "info",
            "세부내용": "지시사항 텍스트에서 자동 대조 가능한 키워드(표/차트/단/각주/하이퍼링크/누름틀/머리말/꼬리말/쪽번호 등)를 찾지 못했습니다."
        })

    return rows


def badge_html(level: str, text: str) -> str:
    cls = {"ok": "badge-ok", "warn": "badge-warn", "err": "badge-err"}.get(level, "badge-info")
    return f'<span class="badge {cls}">{text}</span>'


def build_table(headers: list, rows: list) -> str:
    th   = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        f"<tr>{''.join(f'<td>{c}</td>' for c in row)}</tr>"
        for row in rows
    )
    return (
        f'<div class="tbl-wrap">'
        f'<table class="result-table"><thead><tr>{th}</tr></thead>'
        f'<tbody>{body}</tbody></table></div>'
    )


def step_item(n: int, label: str, state: str) -> str:
    if state == "done":
        bg, border = "rgba(22,163,74,0.18)", "#16a34a"
        bdg, icon, lbl = "background:#16a34a;color:white;", "✓", "color:#a7f3d0;font-weight:600;"
    elif state == "active":
        bg, border = "rgba(37,99,235,0.28)", "#60a5fa"
        bdg, icon, lbl = "background:#2563eb;color:white;", str(n), "color:white;font-weight:700;"
    else:
        bg, border = "transparent", "transparent"
        bdg, icon, lbl = (
            "background:rgba(255,255,255,0.1);color:rgba(255,255,255,0.3);",
            str(n), "color:rgba(255,255,255,0.35);"
        )
    return (
        f'<div style="display:flex;align-items:center;gap:10px;padding:9px 12px;'
        f'border-radius:8px;margin-bottom:5px;background:{bg};border-left:3px solid {border};">'
        f'<div style="width:24px;height:24px;border-radius:50%;display:flex;align-items:center;'
        f'justify-content:center;font-size:11px;font-weight:700;flex-shrink:0;{bdg}">{icon}</div>'
        f'<div style="font-size:13px;{lbl}">{label}</div></div>'
    )


def stat_card(count: int, label: str) -> str:
    color = "#dc2626" if count > 0 else "#16a34a"
    bg    = "#fee2e2" if count > 0 else "#dcfce7"
    return (
        f'<div style="background:{bg};border-radius:10px;padding:16px;text-align:center;'
        f'border-top:3px solid {color};">'
        f'<div style="font-size:26px;font-weight:700;color:{color};">{count}</div>'
        f'<div style="font-size:12px;color:#6b7280;margin-top:4px;">{label}</div></div>'
    )


def alert_html(level: str, text: str) -> str:
    c = {"ok": ("#16a34a","#dcfce7"), "err": ("#dc2626","#fee2e2"),
         "warn": ("#d97706","#fef3c7")}.get(level, ("#2563eb","#dbeafe"))
    return (
        f'<div style="background:{c[1]};color:{c[0]};border-left:4px solid {c[0]};'
        f'padding:10px 14px;border-radius:6px;font-size:13px;font-weight:600;margin-top:8px;">{text}</div>'
    )


def log_result(notice_name: str, file_names: list[str],
               missing_cnt: int, high_priv: int, high_date: int) -> None:
    logging.info(
        "공고문=%s | 제출서류=%s | 누락=%d | 개인정보고위험=%d | 날짜오류=%d",
        notice_name, ",".join(file_names), missing_cnt, high_priv, high_date
    )
    entry = {
        "시각": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "공고문": notice_name,
        "서류수": len(file_names),
        "누락": missing_cnt,
        "개인정보": high_priv,
        "날짜오류": high_date,
    }
    if "log_history" not in st.session_state:
        st.session_state.log_history = []
    st.session_state.log_history.insert(0, entry)
    st.session_state.log_history = st.session_state.log_history[:20]   # 최대 20건 유지


# ─── 헤더 ────────────────────────────────────────────────────
st.markdown("""
<div style="background:linear-gradient(135deg,#1a3a6b 0%,#2563eb 100%);color:white;
            padding:22px 26px;border-radius:12px;margin-bottom:20px;
            box-shadow:0 4px 20px rgba(26,58,107,0.3);">
    <div style="font-size:21px;font-weight:700;margin-bottom:5px;">🏛 공공기관 행정문서 검증 시스템</div>
    <div style="font-size:13px;color:rgba(255,255,255,0.75);">
        공고문과 제출서류를 업로드하면 누락·날짜오류·개인정보 노출을 한 번에 점검합니다
    </div>
</div>
""", unsafe_allow_html=True)

# ─── 업로드 ──────────────────────────────────────────────────
col1, col2 = st.columns(2)

with col1:
    st.markdown("""
    <div class="upload-hint">
        <div class="upload-hint-title">📄 ① 공고문 PDF</div>
        <div class="upload-hint-sub">필수 제출 서류 목록을 자동으로 추출합니다 · 최대 10MB</div>
    </div>""", unsafe_allow_html=True)
    notice_file = st.file_uploader("공고문", type=["pdf"], label_visibility="collapsed")

with col2:
    st.markdown("""
    <div class="upload-hint">
        <div class="upload-hint-title">📅 ② 접수 마감일</div>
        <div class="upload-hint-sub">날짜 오류 탐지 기준일로 사용됩니다</div>
    </div>""", unsafe_allow_html=True)
    deadline_input = st.date_input("마감일", value=datetime(2025, 10, 8),
                                   label_visibility="collapsed")
    deadline = datetime.combine(deadline_input, datetime.min.time())

    st.markdown("""
    <div class="upload-hint" style="margin-top:12px;">
        <div class="upload-hint-title">📁 ③ 제출서류 PDF (여러 개 가능)</div>
        <div class="upload-hint-sub">여러 파일을 동시에 드래그&드롭 · 각 최대 10MB</div>
    </div>""", unsafe_allow_html=True)
    submit_files = st.file_uploader(
        "제출서류", type=["pdf"], accept_multiple_files=True, label_visibility="collapsed"
    )

# ─── 파일 유효성 사전 검사 ───────────────────────────────────
_file_errors: list[str] = []

if notice_file:
    ok, err = validate_file(notice_file)
    if not ok:
        _file_errors.append(err)

for _f in (submit_files or []):
    ok, err = validate_file(_f)
    if not ok:
        _file_errors.append(err)

if _file_errors:
    for _e in _file_errors:
        st.markdown(
            f'<div style="background:#fee2e2;color:#dc2626;border-left:4px solid #dc2626;'
            f'padding:10px 14px;border-radius:6px;font-size:13px;font-weight:600;margin:4px 0;">'
            f'⛔ {_e}</div>',
            unsafe_allow_html=True
        )

# 유효한 파일만 남기기
_valid_notice = notice_file if (notice_file and validate_file(notice_file)[0]) else None
_valid_submits = [f for f in (submit_files or []) if validate_file(f)[0]]

# 업로드 상태
if _valid_notice:
    st.markdown(
        f'<div style="background:#dcfce7;color:#16a34a;border-left:4px solid #16a34a;'
        f'padding:8px 14px;border-radius:6px;font-size:13px;font-weight:500;margin:6px 0;">'
        f'✅ 공고문: {safe(_valid_notice.name)}</div>',
        unsafe_allow_html=True
    )
if _valid_submits:
    st.markdown(
        f'<div style="background:#dbeafe;color:#2563eb;border-left:4px solid #2563eb;'
        f'padding:8px 14px;border-radius:6px;font-size:13px;font-weight:500;margin:6px 0;">'
        f'📁 제출서류 {len(_valid_submits)}개 준비됨</div>',
        unsafe_allow_html=True
    )

run_btn = st.button("🔍 검증 시작", use_container_width=True)

# ─── 사이드바 ─────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div style="padding:20px 14px 16px;border-bottom:1px solid rgba(255,255,255,0.12);margin-bottom:16px;">
        <div style="font-size:20px;margin-bottom:3px;">🏛</div>
        <div style="font-size:13px;font-weight:700;color:white;">행정문서 검증 시스템</div>
        <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-top:1px;">공공기관 제출서류 자동 점검</div>
    </div>
    """, unsafe_allow_html=True)

    s1 = "done" if _valid_notice else "active"
    s2 = ("done" if _valid_submits
          else ("active" if _valid_notice else "pending"))
    s3 = ("done" if run_btn
          else ("active" if (_valid_notice and _valid_submits) else "pending"))
    s4 = "active" if run_btn else "pending"

    steps_html = "".join([
        step_item(1, "공고문 업로드", s1),
        step_item(2, "서류 업로드", s2),
        step_item(3, "검증 시작", s3),
        step_item(4, "결과 확인", s4),
    ])
    st.markdown(f'<div style="padding:0 6px;margin-bottom:16px;">{steps_html}</div>',
                unsafe_allow_html=True)

    st.markdown("""
    <div style="border-top:1px solid rgba(255,255,255,0.12);padding:16px 6px 0;">
        <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.35);
                    letter-spacing:0.1em;margin-bottom:10px;padding-left:4px;">점검 항목</div>
        <div style="font-size:12px;color:rgba(255,255,255,0.6);padding:6px 4px;">📋 필수 서류 누락 탐지</div>
        <div style="font-size:12px;color:rgba(255,255,255,0.6);padding:6px 4px;">🔒 개인정보 노출 탐지</div>
        <div style="font-size:12px;color:rgba(255,255,255,0.6);padding:6px 4px;">📅 날짜 기준 오류 탐지</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div style="border-top:1px solid rgba(255,255,255,0.12);padding:16px 6px 0;margin-top:12px;">
        <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.35);
                    letter-spacing:0.1em;margin-bottom:10px;padding-left:4px;">보안 정책</div>
        <div style="font-size:11px;color:rgba(255,255,255,0.5);padding:4px 4px;line-height:1.6;">
            🔐 파일 메모리 처리<br>
            ⏱ 30분 세션 자동 만료<br>
            📏 최대 10MB/파일<br>
            🛡 PDF 전용 허용
        </div>
    </div>
    """, unsafe_allow_html=True)

    # 검증 이력
    if "log_history" in st.session_state and st.session_state.log_history:
        st.markdown("""
        <div style="border-top:1px solid rgba(255,255,255,0.12);padding-top:16px;margin-top:12px;">
            <div style="font-size:10px;font-weight:700;color:rgba(255,255,255,0.35);
                        letter-spacing:0.1em;margin-bottom:10px;padding-left:4px;">최근 검증 이력</div>
        </div>
        """, unsafe_allow_html=True)
        for entry in st.session_state.log_history[:5]:
            total_err = entry["누락"] + entry["개인정보"] + entry["날짜오류"]
            dot = "🔴" if total_err > 0 else "🟢"
            st.markdown(
                f'<div style="font-size:11px;color:rgba(255,255,255,0.55);'
                f'padding:5px 6px;border-bottom:1px solid rgba(255,255,255,0.06);">'
                f'{dot} {entry["시각"][11:16]} {safe(entry["공고문"][:10])}… '
                f'위험{total_err}건</div>',
                unsafe_allow_html=True
            )

# ─── 결과 ─────────────────────────────────────────────────────
if run_btn:
    if not _valid_notice:
        st.error("공고문을 업로드해주세요.")
    elif not _valid_submits:
        st.error("유효한 제출서류를 업로드해주세요.")
    else:
        pdf_errors: list[str] = []

        with st.spinner("문서 분석 중..."):
            # 공고문 텍스트 추출
            notice_text, notice_err = get_text(_valid_notice)
            if notice_err:
                pdf_errors.append(f"공고문 — {notice_err}")

            keywords = [
                "졸업증명서", "졸업예정증명서", "자기소개서", "입사지원서",
                "취업지원대상자 증명서", "장애인 증명서", "자격증",
                "추천서", "학교장 추천서", "경력증명서", "재직증명서",
                "건강보험", "고용보험", "검정고시"
            ]
            required = [kw for kw in keywords if kw in notice_text]
            submitted_names = " ".join([f.name for f in _valid_submits])

            # 증빙 매트릭스
            matrix = []
            for kw in required:
                ok = kw in submitted_names
                matrix.append({
                    "요건 (공고문)": kw, "필요 증빙": kw,
                    "제출 여부": "✅ 제출" if ok else "❌ 누락",
                    "_ok": ok,
                    "위험도": "🟢 낮음" if ok else "🔴 높음",
                    "조치": "통과" if ok else "보완 필요"
                })

            # 제출서류 텍스트 (캐시 활용, 메모리 처리)
            texts: dict[str, str] = {}
            for f in _valid_submits:
                text, err = get_text(f)
                if err:
                    pdf_errors.append(f"{safe(f.name)} — {err}")
                texts[f.name] = text

            # 개인정보 탐지
            privacy_rows: list[dict] = []
            for f in _valid_submits:
                if f.name not in texts or not texts[f.name]:
                    continue
                issues = detect_privacy(texts[f.name])
                if issues:
                    for iss in issues:
                        iss["파일명"] = safe(f.name)
                        privacy_rows.append(iss)
                else:
                    privacy_rows.append({
                        "파일명": safe(f.name), "항목": "없음", "내용": "-",
                        "_lv": "ok", "위험도": "🟢 낮음", "조치": "이상 없음"
                    })

            # 날짜 오류 탐지
            date_rows: list[dict] = []
            for f in _valid_submits:
                if f.name not in texts or not texts[f.name]:
                    continue
                issues = detect_dates(texts[f.name], deadline)
                if issues:
                    for iss in issues:
                        iss["파일명"] = safe(f.name)
                        date_rows.append(iss)
                else:
                    date_rows.append({
                        "파일명": safe(f.name), "항목": "날짜 검증", "내용": "기준일 이내",
                        "_lv": "ok", "위험도": "🟢 낮음", "조치": "이상 없음"
                    })

        # PDF 처리 오류 안내
        for err_msg in pdf_errors:
            st.markdown(
                f'<div style="background:#fef3c7;color:#d97706;border-left:4px solid #d97706;'
                f'padding:10px 14px;border-radius:6px;font-size:13px;font-weight:600;margin-bottom:6px;">'
                f'⚠️ {err_msg}</div>',
                unsafe_allow_html=True
            )

        missing_cnt = sum(1 for r in matrix if not r["_ok"])
        high_priv   = sum(1 for r in privacy_rows if r.get("_lv") == "err")
        high_date   = sum(1 for r in date_rows if r.get("_lv") == "err")
        total_risk  = missing_cnt + high_priv + high_date

        # 검증 결과 로그
        log_result(
            _valid_notice.name,
            [f.name for f in _valid_submits],
            missing_cnt, high_priv, high_date
        )

        st.markdown("<br>", unsafe_allow_html=True)

        # 요약 통계 카드
        st.markdown(
            f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;'
            f'margin-bottom:20px;" class="stat-grid">'
            f'{stat_card(missing_cnt, "서류 누락")}'
            f'{stat_card(high_priv,   "개인정보 고위험")}'
            f'{stat_card(high_date,   "날짜 오류")}'
            f'{stat_card(total_risk,  "전체 위험 항목")}'
            f'</div>',
            unsafe_allow_html=True
        )

        # ── 증빙 매트릭스 ────────────────────────────────────
        st.markdown('<div class="section-title">📊 증빙 매트릭스</div>', unsafe_allow_html=True)
        if matrix:
            tbl_rows = [
                [safe(r["요건 (공고문)"]), safe(r["필요 증빙"]),
                 badge_html("ok", "✅ 제출") if r["_ok"] else badge_html("err", "❌ 누락"),
                 badge_html("ok", "🟢 낮음")  if r["_ok"] else badge_html("err", "🔴 높음"),
                 safe(r["조치"])]
                for r in matrix
            ]
            st.markdown(
                build_table(["요건 (공고문)", "필요 증빙", "제출 여부", "위험도", "조치"], tbl_rows),
                unsafe_allow_html=True
            )
        else:
            st.info("공고문에서 필수 서류 키워드를 찾지 못했습니다. 공고문 내용을 확인해주세요.")

        st.markdown(
            alert_html("err", f"⚠️ 누락된 서류 {missing_cnt}건 — 보완 후 재제출 필요") if missing_cnt
            else alert_html("ok", "✅ 모든 필수 서류 제출 확인"),
            unsafe_allow_html=True
        )
        st.markdown("<br>", unsafe_allow_html=True)

        # ── 개인정보 탐지 ────────────────────────────────────
        st.markdown('<div class="section-title">🔒 개인정보 노출 탐지</div>', unsafe_allow_html=True)
        if privacy_rows:
            p_rows = [
                [r["파일명"], r["항목"], r["내용"],
                 badge_html(r.get("_lv", "ok"), r["위험도"]), safe(r["조치"])]
                for r in privacy_rows
            ]
            st.markdown(
                build_table(["파일명", "항목", "내용", "위험도", "조치"], p_rows),
                unsafe_allow_html=True
            )
        st.markdown(
            alert_html("err", f"🔴 고위험 개인정보 {high_priv}건 — 즉시 마스킹 필요") if high_priv
            else alert_html("ok", "✅ 고위험 개인정보 노출 없음"),
            unsafe_allow_html=True
        )
        st.markdown("<br>", unsafe_allow_html=True)

        # ── 날짜 오류 탐지 ───────────────────────────────────
        st.markdown('<div class="section-title">📅 날짜 오류 탐지</div>', unsafe_allow_html=True)
        if date_rows:
            d_rows = [
                [r["파일명"], r["항목"], r["내용"],
                 badge_html(r.get("_lv", "ok"), r["위험도"]), r["조치"]]
                for r in date_rows
            ]
            st.markdown(
                build_table(["파일명", "항목", "내용", "위험도", "조치"], d_rows),
                unsafe_allow_html=True
            )
        st.markdown(
            alert_html("err", f"🔴 기준일 초과 날짜 {high_date}건 발견") if high_date
            else alert_html("ok", "✅ 모든 날짜 기준일 이내"),
            unsafe_allow_html=True
        )
        st.markdown("<br>", unsafe_allow_html=True)

        # ── 엑셀 다운로드 ────────────────────────────────────
        df_matrix = pd.DataFrame([
            {k: v for k, v in r.items() if not k.startswith("_")} for r in matrix
        ])
        df_privacy = pd.DataFrame([
            {"파일명": r["파일명"], "항목": r["항목"], "내용": r["내용"],
             "위험도": r["위험도"], "조치": r["조치"]}
            for r in privacy_rows
        ])
        df_dates = pd.DataFrame([
            {"파일명": r["파일명"], "항목": r["항목"], "내용": r["내용"],
             "위험도": r["위험도"], "조치": r["조치"]}
            for r in date_rows
        ])

        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            if not df_matrix.empty:
                df_matrix.to_excel(writer, index=False, sheet_name="증빙매트릭스")
            if not df_privacy.empty:
                df_privacy.to_excel(writer, index=False, sheet_name="개인정보탐지")
            if not df_dates.empty:
                df_dates.to_excel(writer, index=False, sheet_name="날짜오류탐지")
        buf.seek(0)

        st.download_button(
            label="📥 전체 결과 엑셀 다운로드",
            data=buf,
            file_name="행정문서검증결과.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

# ─── 워드프로세서 실기 채점기 (별도 섹션) ───────────────────────
st.markdown("<br><hr>", unsafe_allow_html=True)
st.markdown('<div class="section-title">⌨️ 워드프로세서 실기 자동 감점 계산기</div>',
            unsafe_allow_html=True)

# ── 1단계: 지시사항 PDF + 작성한 hwpx 업로드 → 자동 대조 ──
st.markdown("**① 지시사항 PDF와 작성한 hwpx 파일을 올리면 일부 항목을 자동으로 대조합니다**")
we_up_col1, we_up_col2 = st.columns(2)
with we_up_col1:
    instr_pdf = st.file_uploader("지시사항 PDF", type=["pdf"], key="we_instr_pdf")
with we_up_col2:
    answer_hwpx = st.file_uploader("작성한 hwpx 파일", type=["hwpx"], key="we_answer_hwpx")

if instr_pdf and answer_hwpx:
    hwpx_ok, hwpx_validation_err = validate_hwpx_file(answer_hwpx)
    if not hwpx_ok:
        st.error(hwpx_validation_err)
    elif st.button("🔎 자동 대조 실행", use_container_width=True, key="we_autocheck_btn"):
        instr_text, instr_err = get_text(instr_pdf)
        hwpx_doc, hwpx_err = get_hwpx_doc(answer_hwpx)

        if instr_err:
            st.warning(f"지시사항 PDF — {instr_err}")
        if hwpx_err:
            st.error(hwpx_err)

        if instr_text and hwpx_doc is not None:
            auto_rows = auto_check_hwpx(instr_text, hwpx_doc)
            auto_table_rows = [
                [safe(r["항목"]), safe(r["상태"]), badge_html(r["_lv"], safe(r["상태"])), safe(r["세부내용"])]
                for r in auto_rows
            ]
            st.markdown(
                build_table(["항목", "판정", "상태", "세부내용"], auto_table_rows),
                unsafe_allow_html=True
            )
            st.caption(
                "⚠️ 이 대조는 '표/차트/각주/하이퍼링크 등 요소가 존재하는지'와 지시사항에 등장한 숫자와의 "
                "단순 일치 여부만 확인합니다. 서식이 지시사항과 세밀하게 일치하는지(정확한 색상·정렬·글꼴 등)는 "
                "아래 체크리스트에서 직접 확인 후 선택해주세요."
            )
            with st.expander("🔧 원본 구조 상세 보기 (디버깅용)"):
                st.write("표:", hwpx_doc.tables)
                st.write("단(컬럼):", hwpx_doc.columns)
                st.write("차트:", hwpx_doc.charts)
                st.write("페이지 설정:", hwpx_doc.page_setup)
                st.write("쪽번호:", hwpx_doc.page_num)
                st.write("머리말/꼬리말:", hwpx_doc.header_footer)
                st.write("누름틀:", hwpx_doc.fields)
                st.write("스타일:", hwpx_doc.styles)
        else:
            st.error("지시사항 PDF 또는 hwpx 파일에서 내용을 읽지 못해 자동 대조를 진행할 수 없습니다.")
else:
    st.info("지시사항 PDF와 작성한 hwpx 파일을 모두 올리면 자동 대조 버튼이 나타납니다. "
            "(자동 대조 없이 아래 체크리스트만으로 감점 계산도 가능합니다)")

st.markdown("<br>", unsafe_allow_html=True)
st.markdown("**② 최종 확인 후 아래 체크리스트로 감점을 계산하세요**")
st.caption("각 항목의 상태를 선택하면 채점 기준표에 따라 감점이 자동 합산됩니다. "
           "표/차트는 '미수정'일 때 구성요소 개수만큼 슬라이더로 세부 감점을 조정하세요.")

with st.expander("📋 채점 3단계 안내 보기"):
    for s in WORD_EXAM_STAGES:
        st.markdown(f"- {s}")

we_selections: dict = {}
we_cols = st.columns(2)
_rubric_items = list(WORD_EXAM_RUBRIC.items())
_half = (len(_rubric_items) + 1) // 2

for col_idx, chunk in enumerate([_rubric_items[:_half], _rubric_items[_half:]]):
    with we_cols[col_idx]:
        for name, rule in chunk:
            status = st.radio(
                name, ["정상", "미수정", "누락"],
                horizontal=True, key=f"we_status_{name}"
            )
            entry = {"status": status}
            if status == "미수정" and rule["세부"]:
                lo, hi = rule["세부"]
                entry["세부감점"] = st.slider(
                    f"　↳ {name} 세부 감점(구성요소 오류 정도)",
                    min_value=lo, max_value=WORD_EXAM_RUBRIC[name]["미수정"],
                    value=hi, key=f"we_detail_{name}"
                )
            we_selections[name] = entry
            if rule["설명"]:
                st.caption(f"　↳ {rule['설명']}")

st.markdown("**기타 감점 항목**")
we_c1, we_c2, we_c3, we_c4 = st.columns(4)
with we_c1:
    we_selections["_page_overflow"] = st.checkbox("2쪽 이상 생성됨")
with we_c2:
    we_selections["_typo_words"] = st.number_input("오탈자(단어) 개수", min_value=0, value=0, step=1)
with we_c3:
    we_selections["_typo_hanja"] = st.number_input("한자 오류 개수", min_value=0, value=0, step=1)
with we_c4:
    we_selections["_trailing_break"] = st.checkbox("문서 끝 강제 개행 존재")

if st.button("🧮 감점 계산하기", use_container_width=True):
    we_rows, we_total = score_word_exam(we_selections)

    st.markdown(
        f'<div style="background:{"#fee2e2" if we_total>0 else "#dcfce7"};'
        f'border-radius:10px;padding:16px;text-align:center;margin:12px 0;'
        f'border-top:3px solid {"#dc2626" if we_total>0 else "#16a34a"};">'
        f'<div style="font-size:28px;font-weight:700;color:{"#dc2626" if we_total>0 else "#16a34a"};">'
        f'총 감점 -{we_total}점</div>'
        f'<div style="font-size:12px;color:#6b7280;margin-top:4px;">100점 만점 기준 예상 점수: {max(0, 100-we_total)}점</div>'
        f'</div>', unsafe_allow_html=True
    )

    we_table_rows = [
        [safe(r["항목"]), safe(r["상태"]),
         badge_html(r["_lv"], safe(r["감점"])), safe(r["비고"])]
        for r in we_rows
    ]
    st.markdown(
        build_table(["항목", "상태", "감점", "비고"], we_table_rows),
        unsafe_allow_html=True
    )

    we_buf = BytesIO()
    we_df = pd.DataFrame([
        {"항목": r["항목"], "상태": r["상태"], "감점": r["감점"], "비고": r["비고"]}
        for r in we_rows
    ])
    with pd.ExcelWriter(we_buf, engine="openpyxl") as writer:
        we_df.to_excel(writer, index=False, sheet_name="워드실기채점결과")
    we_buf.seek(0)

    st.download_button(
        label="📥 채점 결과 엑셀 다운로드",
        data=we_buf,
        file_name="워드실기채점결과.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        key="we_download"
    )