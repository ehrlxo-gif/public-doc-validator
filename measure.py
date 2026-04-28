import time
import os
import pdfplumber
import re
from datetime import datetime

folder = "test_pdfs"
deadline = datetime(2025, 10, 8)

patterns = {
    "주민등록번호": r'\d{6}-[1-4]\d{6}',
    "전화번호": r'01[0-9]-\d{3,4}-\d{4}',
}

keywords = ["졸업증명서", "자기소개서", "학교장 추천서", "자격증"]
date_pattern = r'(20\d{2})[.\-/](0[1-9]|1[0-2])[.\-/](0[1-9]|[12]\d|3[01])'

def check_file(filepath):
    with pdfplumber.open(filepath) as pdf:
        text = ""
        for page in pdf.pages:
            t = page.extract_text(x_tolerance=2, y_tolerance=2)
            if t:
                text += re.sub(r'(.)\1{2,}', r'\1', t) + "\n"

    missing = [kw for kw in keywords if kw not in text]

    privacy_found = []
    for label, pattern in patterns.items():
        if re.search(pattern, text):
            privacy_found.append(label)

    date_errors = []
    for m in re.findall(date_pattern, text):
        try:
            d = datetime(int(m[0]), int(m[1]), int(m[2]))
            if d > deadline:
                date_errors.append(d.strftime("%Y.%m.%d"))
        except:
            pass

    return missing, privacy_found, date_errors

files = [f for f in os.listdir(folder) if f.endswith(".pdf")]
print(f"총 {len(files)}개 파일 처리 시작...")

start = time.time()

total_missing = 0
total_privacy = 0
total_date_errors = 0

for filename in files:
    filepath = os.path.join(folder, filename)
    missing, privacy, dates = check_file(filepath)
    total_missing += len(missing)
    total_privacy += len(privacy)
    total_date_errors += len(dates)

end = time.time()
elapsed = end - start

print(f"\n처리 완료!")
print(f"소요 시간: {elapsed:.1f}초 ({elapsed/60:.1f}분)")
print(f"누락 탐지: {total_missing}건")
print(f"개인정보 탐지: {total_privacy}건")
print(f"날짜 오류 탐지: {total_date_errors}건")
