"""
hwpx_parser.py
──────────────
HWPX(한글 2022 이상 기본 저장 포맷) 파일에서 워드프로세서 실기 채점에
필요한 구조 정보를 추출하는 모듈.

HWPX는 zip으로 압축된 XML 묶음이며 핵심 파일은:
  - Contents/header.xml   : 글꼴/문단모양/글자모양/테두리채우기/스타일 정의
  - Contents/section0.xml : 실제 본문(문단, 표, 그림, 각주, 머리말/꼬리말 등)
  - Chart/chart*.xml      : 차트 객체 (있는 경우)

이 모듈은 python-docx 스타일로 "읽기 전용 구조 조회"만 제공한다.
서식이 지시사항과 "의미상" 맞는지 판단하는 규칙은 checker 쪽(app.py)에서
이 모듈이 반환한 구조화 데이터를 보고 별도로 비교한다.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from xml.etree import ElementTree as ET

NS = {
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hh": "http://www.hancom.co.kr/hwpml/2011/head",
    "hc": "http://www.hancom.co.kr/hwpml/2011/core",
    "hs": "http://www.hancom.co.kr/hwpml/2011/section",
}


def _tag(ns_key: str, name: str) -> str:
    return f"{{{NS[ns_key]}}}{name}"


# ─────────────────────────────────────────────────────────────
# 데이터 구조
# ─────────────────────────────────────────────────────────────

@dataclass
class TableInfo:
    row_cnt: int
    col_cnt: int
    border_fill_id: str | None
    border: dict | None            # {"left":..,"right":..,"top":..,"bottom":..} 각 {"type","width_mm","color"}
    has_caption: bool
    caption_side: str | None       # TOP/BOTTOM/LEFT/RIGHT
    outer_width_mm: float | None
    outer_height_mm: float | None
    cell_border_fill_ids: list = field(default_factory=list)  # 각 셀 borderFillIDRef 모음 (색/굵기 다양성 확인용)


@dataclass
class ChartInfo:
    exists: bool
    raw_xml_path: str | None = None
    chart_type_hint: str | None = None  # chart1.xml 안에서 대략적인 타입 추정(가능한 경우)


@dataclass
class ColumnInfo:
    col_count: int
    same_gap_hwpunit: int | None
    same_gap_mm: float | None
    divider_line: bool | None       # 구분선 존재 여부(개략)


@dataclass
class DropcapInfo:
    exists: bool
    style: str | None               # None / "None" / "TwoLines" 등 원문 값
    lines: int | None = None        # 2줄/3줄 등 (style 문자열에서 유추)


@dataclass
class FootnoteInfo:
    count: int


@dataclass
class HyperlinkInfo:
    urls: list = field(default_factory=list)


@dataclass
class StyleInfo:
    id: str
    type: str     # PARA / CHAR
    name: str


@dataclass
class FieldInfo:
    """누름틀(양식 개체필드) 정보."""
    count: int
    types: list = field(default_factory=list)


@dataclass
class HeaderFooterInfo:
    has_header: bool
    has_footer: bool


@dataclass
class PageNumInfo:
    exists: bool
    position: str | None = None
    format_type: str | None = None


@dataclass
class SectionPageInfo:
    """편집 용지 설정(secPr의 상위 hp:page 요소가 있는 경우)."""
    width_mm: float | None = None
    height_mm: float | None = None
    margin_left_mm: float | None = None
    margin_right_mm: float | None = None
    margin_top_mm: float | None = None
    margin_bottom_mm: float | None = None
    margin_header_mm: float | None = None
    margin_footer_mm: float | None = None


@dataclass
class HwpxDocument:
    tables: list
    charts: list
    columns: list
    dropcaps: list
    footnotes: FootnoteInfo
    hyperlinks: HyperlinkInfo
    styles: list
    fields: FieldInfo
    header_footer: HeaderFooterInfo
    page_num: PageNumInfo
    page_setup: SectionPageInfo | None
    full_text: str
    char_count: int
    raw_section_xml: str
    raw_header_xml: str


# ─────────────────────────────────────────────────────────────
# 단위 변환
# HWPUNIT: 1mm = 283.464566...(정확히는 7200/25.4) HWPUNIT
# ─────────────────────────────────────────────────────────────
HWPUNIT_PER_MM = 7200 / 25.4


def hwpunit_to_mm(v) -> float | None:
    if v is None:
        return None
    try:
        return round(int(v) / HWPUNIT_PER_MM, 2)
    except (ValueError, TypeError):
        return None


def parse_width_mm(s: str | None) -> float | None:
    """'0.12 mm' 같은 문자열에서 숫자만 추출."""
    if not s:
        return None
    m = re.match(r"([\d.]+)", s.strip())
    return float(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────
# 보안: 압축 해제 폭탄(zip bomb) 및 과도한 엔티티 확장 방어
# ─────────────────────────────────────────────────────────────
MAX_HWPX_FILE_SIZE = 20 * 1024 * 1024        # 업로드 원본(zip) 최대 20MB
MAX_UNCOMPRESSED_TOTAL = 100 * 1024 * 1024   # 전체 압축 해제 시 최대 100MB
MAX_UNCOMPRESSED_SINGLE = 50 * 1024 * 1024   # 개별 항목 최대 50MB
MAX_COMPRESSION_RATIO = 200                  # 압축 전/후 비율이 이보다 크면 의심(zip bomb 패턴)


class HwpxSecurityError(ValueError):
    """hwpx 파일이 크기·압축률 등 안전 기준을 넘어서 처리를 거부할 때 발생."""


def _safe_read_zip_member(zf: zipfile.ZipFile, name: str) -> bytes:
    """
    zip 항목을 읽기 전에 압축 해제 후 예상 크기와 압축률을 먼저 검사한다.
    실제로 압축을 풀기 전에 메타데이터(ZipInfo)만으로 1차 차단하므로,
    악의적으로 압축률을 극단적으로 높인 파일(zip bomb)에 대한 방어가 된다.
    """
    info = zf.getinfo(name)

    if info.file_size > MAX_UNCOMPRESSED_SINGLE:
        raise HwpxSecurityError(
            f"'{name}' 항목의 압축 해제 크기({info.file_size / 1024 / 1024:.1f}MB)가 "
            f"허용 범위(최대 {MAX_UNCOMPRESSED_SINGLE / 1024 / 1024:.0f}MB)를 초과합니다."
        )

    if info.compress_size > 0:
        ratio = info.file_size / info.compress_size
        if ratio > MAX_COMPRESSION_RATIO:
            raise HwpxSecurityError(
                f"'{name}' 항목의 압축률({ratio:.0f}배)이 비정상적으로 높습니다. "
                f"손상되었거나 악의적으로 조작된 파일일 수 있어 처리를 중단합니다."
            )

    return zf.read(name)


def _safe_parse_xml(xml_bytes: bytes, label: str) -> ET.Element:
    """
    XML 엔티티 확장(billion laughs 등)을 이용한 서비스 거부 공격을 막기 위해,
    파싱 전 원문에서 DTD/ENTITY 선언 자체를 거부한다. 정상적인 hwpx 문서는
    DOCTYPE·ENTITY 선언을 포함하지 않으므로 이 필터로 기능상 손실은 없다.
    """
    head = xml_bytes[:2000]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise HwpxSecurityError(
            f"{label}에서 허용되지 않는 DOCTYPE/ENTITY 선언이 발견되어 처리를 중단합니다."
        )
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise HwpxSecurityError(f"{label} 파싱 중 오류가 발생했습니다.") from exc


# ─────────────────────────────────────────────────────────────
# 메인 파서
# ─────────────────────────────────────────────────────────────

def load_hwpx(file_bytes: bytes) -> HwpxDocument:
    if len(file_bytes) > MAX_HWPX_FILE_SIZE:
        raise HwpxSecurityError(
            f"파일 크기({len(file_bytes) / 1024 / 1024:.1f}MB)가 "
            f"허용 범위(최대 {MAX_HWPX_FILE_SIZE / 1024 / 1024:.0f}MB)를 초과합니다."
        )

    try:
        zf = zipfile.ZipFile(BytesIO(file_bytes))
    except zipfile.BadZipFile as exc:
        raise HwpxSecurityError("올바른 hwpx(zip) 파일이 아닙니다.") from exc

    with zf:
        names = zf.namelist()

        # 압축 해제 시 총 용량이 과도하면 zip bomb으로 간주해 즉시 차단
        total_uncompressed = sum(i.file_size for i in zf.infolist())
        if total_uncompressed > MAX_UNCOMPRESSED_TOTAL:
            raise HwpxSecurityError(
                f"압축 해제 시 총 용량({total_uncompressed / 1024 / 1024:.1f}MB)이 "
                f"허용 범위(최대 {MAX_UNCOMPRESSED_TOTAL / 1024 / 1024:.0f}MB)를 초과합니다. "
                "손상되었거나 악의적으로 조작된 파일일 수 있습니다."
            )

        section_files = sorted([n for n in names if re.match(r"Contents/section\d+\.xml", n)])
        if not section_files:
            raise ValueError("Contents/section*.xml 을 찾을 수 없습니다. 올바른 HWPX 파일이 아닙니다.")

        header_bytes = _safe_read_zip_member(zf, "Contents/header.xml") if "Contents/header.xml" in names else b""
        section_bytes_list = [_safe_read_zip_member(zf, n) for n in section_files]
        chart_files = [n for n in names if n.startswith("Chart/") and n.endswith(".xml")]

    header_root = _safe_parse_xml(header_bytes, "header.xml") if header_bytes else None
    section_roots = [_safe_parse_xml(b, f"section{i}.xml") for i, b in enumerate(section_bytes_list)]

    border_fill_map = _parse_border_fills(header_root) if header_root is not None else {}
    styles = _parse_styles(header_root) if header_root is not None else []

    tables: list[TableInfo] = []
    columns: list[ColumnInfo] = []
    dropcaps: list[DropcapInfo] = []
    footnote_count = 0
    hyperlink_urls: list[str] = []
    field_types: list[str] = []
    has_header = False
    has_footer = False
    page_num_exists = False
    page_num_pos = None
    page_num_fmt = None
    page_setup: SectionPageInfo | None = None
    full_text_parts: list[str] = []

    for root in section_roots:
        # 표
        for tbl in root.iter(_tag("hp", "tbl")):
            tables.append(_parse_table(tbl, border_fill_map))

        # 단(컬럼) 설정 — hp:colPr
        for colpr in root.iter(_tag("hp", "colPr")):
            col_count = int(colpr.get("colCount", "1"))
            same_gap = colpr.get("sameGap")
            same_gap_i = int(same_gap) if same_gap and same_gap.isdigit() else None
            columns.append(ColumnInfo(
                col_count=col_count,
                same_gap_hwpunit=same_gap_i,
                same_gap_mm=hwpunit_to_mm(same_gap_i) if same_gap_i else None,
                divider_line=None,  # 구분선 유무는 hp:lineSeg 등 별도 요소라 근사 불가 -> 상위에서 XML 직접 검색 권장
            ))

        # 문단 첫 글자 장식 — dropcapstyle 속성 (표/문단 등 여러 곳에 있을 수 있어 'None' 아닌 것만)
        for elem in root.iter():
            dcs = elem.get("dropcapstyle")
            if dcs and dcs != "None":
                dropcaps.append(DropcapInfo(exists=True, style=dcs))

        # 각주
        footnote_count += len(list(root.iter(_tag("hp", "footNote"))))

        # 머리말/꼬리말
        if list(root.iter(_tag("hp", "header"))):
            has_header = True
        if list(root.iter(_tag("hp", "footer"))):
            has_footer = True

        # 쪽번호
        for pn in root.iter(_tag("hp", "pageNum")):
            page_num_exists = True
            page_num_pos = pn.get("pos")
            page_num_fmt = pn.get("formatType")

        # 누름틀(필드)
        for fb in root.iter(_tag("hp", "fieldBegin")):
            field_types.append(fb.get("type", ""))

        # 편집 용지(page) 설정 — secPr 내부에 있는 경우가 많음
        for secpr in root.iter(_tag("hp", "secPr")):
            pg = secpr.find(_tag("hp", "pagePr"))
            if pg is not None:
                margin = pg.find(_tag("hp", "margin"))
                page_setup = SectionPageInfo(
                    width_mm=hwpunit_to_mm(pg.get("width")),
                    height_mm=hwpunit_to_mm(pg.get("height")),
                    margin_left_mm=hwpunit_to_mm(margin.get("left")) if margin is not None else None,
                    margin_right_mm=hwpunit_to_mm(margin.get("right")) if margin is not None else None,
                    margin_top_mm=hwpunit_to_mm(margin.get("top")) if margin is not None else None,
                    margin_bottom_mm=hwpunit_to_mm(margin.get("bottom")) if margin is not None else None,
                    margin_header_mm=hwpunit_to_mm(margin.get("header")) if margin is not None else None,
                    margin_footer_mm=hwpunit_to_mm(margin.get("footer")) if margin is not None else None,
                )

        # 본문 텍스트 전체 (하이퍼링크 URL도 텍스트/필드 속성에서 찾음)
        for t in root.iter(_tag("hp", "t")):
            if t.text:
                full_text_parts.append(t.text)

        # 하이퍼링크 — hp:fieldBegin type="HYPERLINK" 안의 stringParam name="Path" 에 실제 URL이 들어있음
        for fb in root.iter(_tag("hp", "fieldBegin")):
            if "HYPERLINK" in (fb.get("type") or "").upper():
                params = fb.find(_tag("hp", "parameters"))
                if params is not None:
                    for sp in params.iter(_tag("hp", "stringParam")):
                        if sp.get("name") == "Path" and sp.text:
                            hyperlink_urls.append(sp.text)

    full_text = "".join(full_text_parts)
    hyperlink_urls = list(dict.fromkeys(hyperlink_urls))  # 순서 유지 중복제거

    doc = HwpxDocument(
        tables=tables,
        charts=[ChartInfo(exists=True, raw_xml_path=p) for p in chart_files] or [ChartInfo(exists=False)],
        columns=columns,
        dropcaps=dropcaps or [DropcapInfo(exists=False, style=None)],
        footnotes=FootnoteInfo(count=footnote_count),
        hyperlinks=HyperlinkInfo(urls=hyperlink_urls),
        styles=styles,
        fields=FieldInfo(count=len(field_types), types=field_types),
        header_footer=HeaderFooterInfo(has_header=has_header, has_footer=has_footer),
        page_num=PageNumInfo(exists=page_num_exists, position=page_num_pos, format_type=page_num_fmt),
        page_setup=page_setup,
        full_text=full_text,
        char_count=len(full_text),
        raw_section_xml="\n".join(s.decode("utf-8", "ignore") for s in section_bytes_list),
        raw_header_xml=header_bytes.decode("utf-8", "ignore"),
    )
    return doc


def _parse_border_fills(header_root) -> dict:
    """borderFill id -> {"left":{"type","width_mm","color"}, ...} 매핑."""
    result = {}
    for bf in header_root.iter(_tag("hh", "borderFill")):
        bf_id = bf.get("id")
        sides = {}
        for side_tag in ["leftBorder", "rightBorder", "topBorder", "bottomBorder"]:
            el = bf.find(_tag("hh", side_tag))
            if el is not None:
                sides[side_tag.replace("Border", "").lower()] = {
                    "type": el.get("type"),
                    "width_mm": parse_width_mm(el.get("width")),
                    "color": el.get("color"),
                }
        result[bf_id] = sides
    return result


def _parse_styles(header_root) -> list[StyleInfo]:
    result = []
    for st in header_root.iter(_tag("hh", "style")):
        result.append(StyleInfo(id=st.get("id"), type=st.get("type"), name=st.get("name")))
    return result


def _parse_table(tbl_elem, border_fill_map: dict) -> TableInfo:
    row_cnt = int(tbl_elem.get("rowCnt", "0"))
    col_cnt = int(tbl_elem.get("colCnt", "0"))
    bf_id = tbl_elem.get("borderFillIDRef")
    border = border_fill_map.get(bf_id)

    sz = tbl_elem.find(_tag("hp", "sz"))
    outer_w = hwpunit_to_mm(sz.get("width")) if sz is not None else None
    outer_h = hwpunit_to_mm(sz.get("height")) if sz is not None else None

    caption = tbl_elem.find(_tag("hp", "caption"))
    has_caption = caption is not None
    caption_side = caption.get("side") if caption is not None else None

    cell_bf_ids = []
    for tc in tbl_elem.iter(_tag("hp", "tc")):
        cbf = tc.get("borderFillIDRef")
        if cbf:
            cell_bf_ids.append(cbf)

    return TableInfo(
        row_cnt=row_cnt, col_cnt=col_cnt,
        border_fill_id=bf_id, border=border,
        has_caption=has_caption, caption_side=caption_side,
        outer_width_mm=outer_w, outer_height_mm=outer_h,
        cell_border_fill_ids=cell_bf_ids,
    )


# ─────────────────────────────────────────────────────────────
# 지시사항(PDF 텍스트) 안에서 채점 관련 "요구 수치"를 러프하게 뽑아내는 헬퍼
# (완벽한 NLU가 아니라, 흔한 패턴만 정규식으로 탐지 — 참고용 힌트 제공 목적)
# ─────────────────────────────────────────────────────────────

def extract_instruction_hints(instruction_text: str) -> dict:
    hints = {}

    m = re.search(r"단\s*간격[^0-9]{0,10}(\d+(?:\.\d+)?)\s*mm", instruction_text)
    if m:
        hints["단간격_mm"] = float(m.group(1))

    m = re.search(r"(?:다단\s*설정.{0,15}모양[:\s]*|모양[:\s]*)(둘|셋|넷)|"
                  r"(\d)\s*단(?:으로|,|\s)", instruction_text)
    if m:
        word_map = {"둘": 2, "셋": 3, "넷": 4}
        v = m.group(1) or m.group(2)
        hints["단수"] = word_map.get(v, int(v) if v and v.isdigit() else None)

    widths = re.findall(r"([\d.]+)\s*mm", instruction_text)
    if widths:
        hints["문서내_mm_값들"] = [float(w) for w in widths]

    if "캡션" in instruction_text:
        hints["캡션_요구"] = True
    if "블록 계산식" in instruction_text or "블록계산식" in instruction_text:
        hints["블록계산식_요구"] = True
    if "쪽 번호" in instruction_text or "쪽번호" in instruction_text:
        hints["쪽번호_요구"] = True
    if "머리말" in instruction_text:
        hints["머리말_요구"] = True
    if "꼬리말" in instruction_text:
        hints["꼬리말_요구"] = True
    if "각주" in instruction_text:
        hints["각주_요구"] = True
    if "하이퍼링크" in instruction_text:
        hints["하이퍼링크_요구"] = True
    if "누름틀" in instruction_text:
        hints["누름틀_요구"] = True
    if "차트" in instruction_text:
        hints["차트_요구"] = True
    if "표" in instruction_text:
        hints["표_요구"] = True
    if "글상자" in instruction_text:
        hints["글상자_요구"] = True
    if "문단 첫 글자 장식" in instruction_text or "첫글자 장식" in instruction_text:
        hints["첫글자장식_요구"] = True

    return hints