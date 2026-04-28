import streamlit as st
import pdfplumber
import pandas as pd
import re
from io import BytesIO
from datetime import datetime

st.set_page_config(page_title="공공기관 행정문서 검증 시스템", layout="wide")
st.title("📋 공공기관 행정문서 검증 시스템")
st.markdown("공고문과 제출서류를 업로드하면 누락·날짜오류·개인정보 노출을 한 번에 점검합니다.")

# ───────────────────────────────────────────
# PDF 텍스트 추출 함수
# pdfplumber로 각 페이지 텍스트를 읽고,
# 반복 글자(한한한한 → 한) 패턴을 정규식으로 제거함
# ───────────────────────────────────────────
def extract_text(file):
    with pdfplumber.open(file) as pdf:
        text = ""
        for page in pdf.pages:
            t = page.extract_text(x_tolerance=2, y_tolerance=2)
            if t:
                t = re.sub(r'(.)\1{2,}', r'\1', t)
                text += t + "\n"
    return text

# ───────────────────────────────────────────
# 개인정보 탐지 함수
# 주민번호·전화번호·이메일 같은 패턴을 정규식으로 탐지하고,
# 학교명·가족관계 같은 블라인드 위반 키워드도 함께 잡음
# ───────────────────────────────────────────
def detect_privacy(text):
    issues = []

    patterns = {
        "주민등록번호": r'\d{6}-[1-4]\d{6}',
        "전화번호": r'01[0-9]-\d{3,4}-\d{4}',
        "이메일": r'[a-zA-Z0-9]+@[a-zA-Z0-9]+\.[a-zA-Z]{2,}',
        "생년월일(8자리)": r'\b(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\b',
    }

    school_keywords = ["고등학교", "중학교", "대학교", "대학", "출신학교"]
    blind_keywords = ["아버지", "어머니", "부모님", "가족", "형제", "종교", "출신지", "고향"]

    for label, pattern in patterns.items():
        matches = re.findall(pattern, text)
        if matches:
            issues.append({"항목": label, "내용": matches[0][:30], "위험도": "🔴 높음", "조치": "즉시 마스킹 필요"})

    for kw in school_keywords:
        if kw in text:
            issues.append({"항목": "학교명 노출", "내용": kw, "위험도": "🟡 중간", "조치": "블라인드 위반 확인 필요"})
            break

    for kw in blind_keywords:
        if kw in text:
            issues.append({"항목": "블라인드 위반 의심", "내용": kw, "위험도": "🟡 중간", "조치": "내용 수정 필요"})

    return issues

# ───────────────────────────────────────────
# 날짜 오류 탐지 함수
# 문서에서 날짜 패턴을 모두 추출한 뒤,
# 마감일보다 늦은 날짜가 있으면 기준일 초과로 표시함
# ───────────────────────────────────────────
def detect_dates(text, deadline):
    issues = []
    patterns = [
        r'(20\d{2})[.\-/](0[1-9]|1[0-2])[.\-/](0[1-9]|[12]\d|3[01])',
        r'(20\d{2})년\s*(0?[1-9]|1[0-2])월\s*(0?[1-9]|[12]\d|3[01])일',
    ]
    found_dates = []
    for pattern in patterns:
        for m in re.findall(pattern, text):
            try:
                found_dates.append(datetime(int(m[0]), int(m[1]), int(m[2])))
            except:
                pass

    for d in found_dates:
        if d > deadline:
            issues.append({
                "항목": "기준일 초과 날짜",
                "내용": d.strftime("%Y년 %m월 %d일"),
                "위험도": "🔴 높음",
                "조치": f"마감일({deadline.strftime('%Y.%m.%d')}) 이후 — 자격 미충족 가능"
            })
    return issues

# ───────────────────────────────────────────
# 화면 레이아웃: 좌측에 업로드, 우측에 결과
# ───────────────────────────────────────────
col_left, col_right = st.columns([1, 2])

with col_left:
    st.subheader("📂 파일 업로드")

    # 공고문 업로드
    notice_file = st.file_uploader("① 공고문 PDF", type=["pdf"])

    # 마감일 입력
    deadline_input = st.date_input("② 접수 마감일", value=datetime(2025, 10, 8))
    deadline = datetime.combine(deadline_input, datetime.min.time())

    # 제출서류 업로드 (여러 개)
    submit_files = st.file_uploader(
        "③ 제출서류 PDF (여러 개 가능)",
        type=["pdf"],
        accept_multiple_files=True
    )

    run_btn = st.button("🔍 검증 시작", use_container_width=True)

# ───────────────────────────────────────────
# 검증 실행: 버튼 누르면 오른쪽에 결과 표시
# ───────────────────────────────────────────
with col_right:
    if run_btn:
        if not notice_file:
            st.warning("공고문을 업로드해주세요.")
        elif not submit_files:
            st.warning("제출서류를 업로드해주세요.")
        else:
            # 공고문에서 필수 서류 키워드 추출
            notice_text = extract_text(notice_file)
            keywords = [
                "졸업증명서", "졸업예정증명서", "자기소개서", "입사지원서",
                "취업지원대상자 증명서", "장애인 증명서", "자격증",
                "추천서", "학교장 추천서", "경력증명서", "재직증명서",
                "건강보험", "고용보험", "검정고시"
            ]
            required = [kw for kw in keywords if kw in notice_text]

            # ─────────────────────────────
            # 증빙 매트릭스 생성
            # 각 요건마다 제출 여부·위험도·조치를 한 표로 정리
            # 이게 선행기술과의 핵심 차별점:
            # 공고문 요건 ↔ 제출서류를 직접 매칭하는 구조
            # ─────────────────────────────
            st.subheader("📊 증빙 매트릭스")

            submitted_names = " ".join([f.name for f in submit_files])
            matrix = []
            for kw in required:
                submitted = kw in submitted_names
                matrix.append({
                    "요건 (공고문)": kw,
                    "필요 증빙": kw,
                    "제출 여부": "✅ 제출" if submitted else "❌ 누락",
                    "위험도": "🟢 낮음" if submitted else "🔴 높음",
                    "조치": "통과" if submitted else "보완 필요"
                })

            df_matrix = pd.DataFrame(matrix)
            st.dataframe(df_matrix, use_container_width=True)

            missing_count = sum(1 for r in matrix if "누락" in r["제출 여부"])
            if missing_count:
                st.error(f"⚠️ 누락된 서류 {missing_count}건")
            else:
                st.success("모든 필수 서류 제출 확인")

            st.divider()

            # ─────────────────────────────
            # 개인정보 탐지 결과
            # ─────────────────────────────
            st.subheader("🔍 개인정보 노출 탐지")
            privacy_rows = []
            for f in submit_files:
                text = extract_text(f)
                issues = detect_privacy(text)
                if issues:
                    for issue in issues:
                        issue["파일명"] = f.name
                        privacy_rows.append(issue)
                else:
                    privacy_rows.append({
                        "파일명": f.name,
                        "항목": "없음",
                        "내용": "-",
                        "위험도": "🟢 낮음",
                        "조치": "이상 없음"
                    })

            df_privacy = pd.DataFrame(privacy_rows)
            st.dataframe(df_privacy[["파일명", "항목", "내용", "위험도", "조치"]], use_container_width=True)

            high_privacy = sum(1 for r in privacy_rows if "높음" in r["위험도"])
            if high_privacy:
                st.error(f"🔴 고위험 개인정보 {high_privacy}건 발견")
            else:
                st.success("고위험 개인정보 노출 없음")

            st.divider()

            # ─────────────────────────────
            # 날짜 오류 탐지 결과
            # ─────────────────────────────
            st.subheader("📅 날짜 오류 탐지")
            date_rows = []
            for f in submit_files:
                text = extract_text(f)
                issues = detect_dates(text, deadline)
                if issues:
                    for issue in issues:
                        issue["파일명"] = f.name
                        date_rows.append(issue)
                else:
                    date_rows.append({
                        "파일명": f.name,
                        "항목": "날짜 검증",
                        "내용": "기준일 이내",
                        "위험도": "🟢 낮음",
                        "조치": "이상 없음"
                    })

            df_dates = pd.DataFrame(date_rows)
            st.dataframe(df_dates[["파일명", "항목", "내용", "위험도", "조치"]], use_container_width=True)

            high_dates = sum(1 for r in date_rows if "높음" in r["위험도"])
            if high_dates:
                st.error(f"🔴 기준일 초과 날짜 {high_dates}건 발견")
            else:
                st.success("모든 날짜 기준일 이내")

            st.divider()

            # ─────────────────────────────
            # 엑셀 다운로드
            # 세 결과를 시트 3개짜리 엑셀로 저장
            # ─────────────────────────────
            buffer = BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                df_matrix.to_excel(writer, index=False, sheet_name="증빙매트릭스")
                df_privacy.to_excel(writer, index=False, sheet_name="개인정보탐지")
                df_dates.to_excel(writer, index=False, sheet_name="날짜오류탐지")
            buffer.seek(0)

            st.download_button(
                label="📥 전체 결과 엑셀 다운로드",
                data=buffer,
                file_name="행정문서검증결과.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )