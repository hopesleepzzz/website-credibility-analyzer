import ipaddress, socket, json, os
from io import BytesIO
from urllib.parse import urlparse, urljoin

import requests
import streamlit as st
import fitz
import pytesseract
from bs4 import BeautifulSoup
from PIL import Image, ExifTags

MAX_BYTES = 12 * 1024 * 1024
MAX_PDF_PAGES = 150
MAX_OCR_PAGES = 30
OCR_DPI = 150
TRUSTED_DOMAINS = {
    "go.kr", "gov.kr", "korea.kr", "who.int", "wmo.int", "ipcc.ch",
    "nih.gov", "cdc.gov", "europa.eu", "gov.uk", "un.org", "unesco.org"
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def public_host(host):
    try:
        for item in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(item[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except (OSError, ValueError):
        return False


def validate_url(raw):
    raw = raw.strip()
    if not raw:
        raise ValueError("주소를 입력해 주세요.")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("올바른 공개 HTTP/HTTPS 주소를 입력해 주세요.")
    if p.port not in (None, 80, 443) or not public_host(p.hostname):
        raise ValueError("허용되지 않는 주소 또는 포트입니다.")
    return raw


def download(url):
    headers = {"User-Agent": "SchoolCredibilityProject/3.0"}
    for _ in range(6):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or not public_host(p.hostname):
            raise ValueError("안전하지 않은 주소로 이동해 다운로드를 중단했습니다.")
        with requests.get(url, headers=headers, timeout=20, stream=True, allow_redirects=False) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                if not loc:
                    raise ValueError("이동 주소를 확인할 수 없습니다.")
                url = urljoin(url, loc)
                continue
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "").split(";")[0].lower()
            chunks, total = [], 0
            for chunk in r.iter_content(65536):
                if chunk:
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("파일은 최대 12MB까지 분석할 수 있습니다.")
                    chunks.append(chunk)
            return b"".join(chunks), ctype, url, r.status_code
    raise ValueError("웹페이지 이동 횟수가 너무 많습니다.")


def detect(data, ctype="", url=""):
    h = data[:16]
    if h.startswith(b"%PDF-") or ctype == "application/pdf":
        return "pdf"
    if (h.startswith(b"\x89PNG\r\n\x1a\n") or h.startswith(b"\xff\xd8\xff")
            or h[:6] in (b"GIF87a", b"GIF89a") or h.startswith(b"BM")
            or (len(h) >= 12 and h[:4] == b"RIFF" and h[8:12] == b"WEBP")
            or ctype.startswith("image/")):
        return "image"
    if ctype in ("text/html", "application/xhtml+xml") or b"<html" in data[:1000].lower() or b"<!doctype html" in data[:1000].lower():
        return "html"
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith(tuple(IMAGE_EXTENSIONS)):
        return "image"
    raise ValueError("지원 형식을 판별하지 못했습니다. HTML, PDF, PNG, JPG, GIF, BMP, WEBP, TIFF를 지원합니다.")


def meta(soup, attr, key):
    tag = soup.find("meta", attrs={attr: key})
    return (tag.get("content") or "").strip() if tag else ""


def walk_jsonld(obj, out):
    if isinstance(obj, dict):
        out.append(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk_jsonld(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_jsonld(v, out)


def person(v):
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return " / ".join(str(v[k]) for k in ("name", "jobTitle") if v.get(k))
    if isinstance(v, list):
        return ", ".join(filter(None, (person(x) for x in v)))
    return ""


def parse_html(data, url):
    soup = BeautifulSoup(data, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    author = meta(soup, "name", "author") or meta(soup, "property", "article:author")
    date = (meta(soup, "property", "article:published_time") or
            meta(soup, "name", "date") or meta(soup, "itemprop", "datePublished"))
    publisher = meta(soup, "name", "publisher") or meta(soup, "property", "og:site_name")
    objects = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk_jsonld(json.loads(script.string or script.get_text()), objects)
        except Exception:
            pass
    for obj in objects:
        author = author or person(obj.get("author"))
        date = date or str(obj.get("datePublished") or "")
        publisher = publisher or person(obj.get("publisher"))
    links, about = [], []
    host = (urlparse(url).hostname or "").lower()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        p = urlparse(href)
        if p.scheme not in ("http", "https") or not p.hostname:
            continue
        label = a.get_text(" ", strip=True) or href
        if p.hostname.lower() != host and len(links) < 200:
            links.append({"label": label[:100], "url": href})
        if any(w in (label + " " + p.path).lower()
               for w in ("소개", "문의", "연락처", "about", "contact")):
            about.append({"label": label, "url": href})
    return {
        "type": "HTML 웹페이지", "title": title, "author": author, "date": date,
        "publisher": publisher, "text": " ".join(soup.get_text(" ", strip=True).split())[:15000],
        "links": links, "about": about, "metadata": {}
    }


def run_ocr(pil_image):
    # pytesseract는 Python 패키지 외에 Tesseract OCR 실행 프로그램이 필요합니다.
    return pytesseract.image_to_string(pil_image, lang="kor+eng").strip()


def parse_pdf(data):
    doc = fitz.open(stream=data, filetype="pdf")
    if doc.is_encrypted:
        doc.close()
        raise ValueError("암호가 설정된 PDF는 분석할 수 없습니다.")
    if doc.page_count > MAX_PDF_PAGES:
        doc.close()
        raise ValueError(f"PDF는 최대 {MAX_PDF_PAGES}페이지까지 분석할 수 있습니다.")

    md = doc.metadata or {}
    texts, links = [], []
    ocr_pages, ocr_errors = [], []
    scanned_candidates = []
    for page in doc:
        page_text = page.get_text("text").strip()
        if len(page_text) < 35:
            scanned_candidates.append(page.number)
        texts.append(page_text)
        for item in page.get_links():
            uri = item.get("uri")
            if uri and uri.startswith(("http://", "https://")) and len(links) < 200:
                links.append({"label": f"{page.number + 1}쪽 링크", "url": uri})

    # 텍스트가 거의 없는 페이지에 한해 OCR을 시도합니다. 최대 30쪽으로 제한합니다.
    targets = scanned_candidates[:MAX_OCR_PAGES]
    for page_index in targets:
        try:
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=fitz.Matrix(OCR_DPI / 72, OCR_DPI / 72),
                                  colorspace=fitz.csRGB, alpha=False)
            image = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
            extracted = run_ocr(image)
            if extracted:
                texts[page_index] = extracted
                ocr_pages.append(page_index + 1)
        except Exception as exc:
            ocr_errors.append(str(exc)[:180])
            break

    count = doc.page_count
    doc.close()
    full_text = "\n".join(texts)[:30000]
    metadata = {
        "페이지 수": str(count),
        "PDF 생성 프로그램": md.get("creator") or "확인되지 않음",
        "PDF 변환 프로그램": md.get("producer") or "확인되지 않음",
        "수정 날짜": md.get("modDate") or "확인되지 않음",
        "OCR 처리 페이지": ", ".join(map(str, ocr_pages)) if ocr_pages else "없음",
        "OCR 대상 페이지 수": f"{len(targets)}쪽 (최대 {MAX_OCR_PAGES}쪽)",
        "OCR 상태": "OCR 텍스트 추출 완료" if ocr_pages else
                   "스캔 페이지가 없거나 OCR 텍스트가 추출되지 않음"
    }
    warning = ""
    if scanned_candidates and not ocr_pages:
        warning = ("스캔 페이지에서 OCR 텍스트를 추출하지 못했습니다. "
                   "Tesseract OCR과 한국어(kor) 언어 데이터 설치 여부를 확인하세요.")
    if len(scanned_candidates) > MAX_OCR_PAGES:
        warning += f" 스캔 의심 페이지 {len(scanned_candidates)}쪽 중 앞의 {MAX_OCR_PAGES}쪽만 OCR 처리했습니다."
    if ocr_errors:
        warning += " OCR 오류: " + ocr_errors[0]
    metadata["OCR 안내"] = warning
    return {
        "type": "PDF 문서", "title": (md.get("title") or "").strip(),
        "author": (md.get("author") or "").strip(),
        "date": (md.get("creationDate") or md.get("modDate") or "").strip(),
        "publisher": (md.get("producer") or md.get("creator") or "").strip(),
        "text": full_text, "links": links, "about": [], "metadata": metadata
    }


def parse_image(data):
    im = Image.open(BytesIO(data))
    exif = {}
    try:
        for key, value in im.getexif().items():
            name = ExifTags.TAGS.get(key, str(key))
            if name in ("Artist", "DateTime", "DateTimeOriginal", "Software",
                        "Copyright", "ImageDescription"):
                exif[name] = str(value)[:300]
    except Exception:
        pass
    text, warning = "", ""
    try:
        image_for_ocr = im.convert("RGB")
        text = run_ocr(image_for_ocr)
    except Exception as exc:
        warning = ("이미지 OCR을 실행하지 못했습니다. Tesseract OCR과 한국어(kor) "
                   "언어 데이터를 설치해야 합니다. (" + str(exc)[:120] + ")")
    return {
        "type": "이미지", "title": exif.get("ImageDescription", ""),
        "author": exif.get("Artist", ""),
        "date": exif.get("DateTimeOriginal", exif.get("DateTime", "")),
        "publisher": exif.get("Copyright", ""), "text": text[:15000],
        "links": [], "about": [],
        "metadata": {
            "이미지 형식": im.format or "알 수 없음",
            "이미지 크기": f"{im.width} × {im.height} 픽셀",
            "촬영·생성 프로그램": exif.get("Software", "확인되지 않음"),
            "OCR 상태": "텍스트 추출됨" if text else "텍스트 없음 또는 OCR 미설치",
            "OCR 안내": warning
        }
    }


def trusted(host):
    host = host.lower().strip(".")
    return any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS)


def evaluate(source, info, is_local=False):
    p = urlparse(source) if not is_local else None
    links = info["links"]
    org = [x for x in links if trusted(urlparse(x["url"]).hostname or "")]
    # 해당 자료 형식에서 확인할 수 없는 항목은 0점 처리하지 않고 평가 대상에서 제외합니다.
    checks = []
    possible = earned = 0

    def add(name, result, points, explanation, applicable=True):
        nonlocal possible, earned
        checks.append((name, result, points if applicable else None, explanation))
        if applicable:
            possible += points
            earned += points if points else 0

    if not is_local:
        add("출처 주소 보안", "HTTPS 사용" if p.scheme == "https" else "HTTP 사용",
            10 if p.scheme == "https" else 0,
            "HTTPS는 통신 보안이며 내용의 진실성을 보장하지 않습니다.")
    else:
        add("출처 주소 보안", "로컬 파일이므로 해당 없음", 0,
            "로컬 파일은 웹 주소의 HTTPS 여부를 평가할 수 없습니다.", False)

    add("제목·문서명", "확인됨" if info["title"] else "메타정보에서 확인되지 않음",
        10 if info["title"] else 0, "제목 메타정보의 존재 여부를 확인합니다.")
    add("작성자 정보", "발견" if info["author"] else "메타정보 없음",
        15 if info["author"] else 0, "작성자 신원이나 전문성까지 검증하지 않습니다.")
    add("날짜 정보", info["date"] or "메타정보 없음",
        10 if info["date"] else 0, "실제 게시일과 일치하는지는 별도 확인이 필요합니다.")
    add("발행·제작 정보", "발견" if info["publisher"] else "정보 없음",
        15 if info["publisher"] else 0, "발행자 또는 파일 제작 관련 메타정보입니다.")

    links_applicable = info["type"] != "이미지"
    add("외부 참고 링크", f"{len(links)}개 발견" if links else "없음",
        15 if links else 0, "링크가 주장을 뒷받침하는지는 확인하지 않습니다.",
        applicable=links_applicable)
    add("등록 기관 도메인 링크", f"{len(org)}개 발견" if org else "발견되지 않음",
        20 if org else 0, "예시로 등록한 도메인 목록과 대조합니다.",
        applicable=links_applicable)
    add("본문 텍스트", "추출됨" if info["text"].strip() else "추출되지 않음",
        5 if info["text"].strip() else 0,
        "텍스트가 있다는 사실은 신뢰성을 뜻하지 않습니다.")

    score = round(earned / possible * 100) if possible else 0
    label = ("확인 가능한 정보가 비교적 많음" if score >= 75 else
             "일부 정보 추가 확인 필요" if score >= 45 else
             "확인 가능한 정보가 부족함")
    return checks, score, label, org, possible


def report(source, info, checks, score, label, possible):
    lines = [
        "# 자료 정보 분석 보고서", "",
        f"- 형식: {info['type']}", f"- 출처: {source}",
        f"- 제목: {info['title'] or '확인되지 않음'}",
        f"- 작성자: {info['author'] or '확인되지 않음'}",
        f"- 날짜: {info['date'] or '확인되지 않음'}",
        f"- 발행·제작: {info['publisher'] or '확인되지 않음'}",
        f"- 정보 확인 지수: {score}/100 (평가 가능한 항목의 배점을 기준으로 환산)",
        f"- 결과: {label}", "", "## 항목별 결과"
    ]
    for name, result, points, explanation in checks:
        point_text = "평가 대상 제외" if points is None else f"{points}점"
        lines.append(f"- **{name}**: {result} ({point_text}) — {explanation}")
    lines += ["", "## 추가 메타정보"] + [f"- {k}: {v}" for k, v in info["metadata"].items()]
    lines += [
        "", "이 점수는 진실성 점수가 아니라 자동으로 확인 가능한 출처 단서의 지수입니다.",
        "메타정보는 수정될 수 있고 OCR은 오인식할 수 있습니다. 원문과 독립적인 공식 자료로 확인하세요."
    ]
    return "\n".join(lines)


def analyze_bytes(data, source_name, content_type="", source_url="", is_local=False):
    kind = detect(data, content_type, source_url)
    info = (parse_html(data, source_url) if kind == "html" else
            parse_pdf(data) if kind == "pdf" else parse_image(data))
    checks, score, label, org_links, possible = evaluate(source_url, info, is_local=is_local)
    return info, checks, score, label, org_links, possible


def show_result(source, info, checks, score, label, org_links, possible, status=None):
    st.success(f"{info['type']} 분석 완료" + (f" (HTTP {status})" if status else ""))
    if not source.startswith("로컬 파일:"):
        st.caption(f"최종 주소: {source}")
    a, b = st.columns([1, 2])
    with a:
        st.metric("정보 확인 지수", f"{score}/100")
        st.write(f"**종합 표시:** {label}")
        st.progress(score / 100)
        st.caption(f"평가 대상 항목의 배점을 100점 기준으로 환산했습니다.")
    with b:
        st.subheader(info["title"] or info["type"])
        st.write(f"**작성자:** {info['author'] or '자동 추출하지 못함'}")
        st.write(f"**날짜:** {info['date'] or '자동 추출하지 못함'}")
        st.write(f"**발행·제작:** {info['publisher'] or '자동 추출하지 못함'}")
    st.subheader("항목별 분석")
    for name, result, points, explanation in checks:
        point_label = "평가 대상 제외" if points is None else f"반영 점수: {points}점"
        with st.container(border=True):
            st.markdown(f"**{name}** — {result}")
            st.caption(f"{explanation} ({point_label})")
    if info["metadata"]:
        st.subheader("파일 메타정보")
        st.json(info["metadata"])
    if org_links:
        st.subheader("등록된 기관 도메인 링크")
        for item in org_links:
            st.markdown(f"- [{item['label']}]({item['url']})")
    if info["text"]:
        with st.expander("추출된 본문 또는 OCR 텍스트"):
            st.text(info["text"][:20000])
    if info["metadata"].get("OCR 안내"):
        st.warning(info["metadata"]["OCR 안내"])
    st.download_button(
        "분석 보고서 다운로드 (.md)",
        report(source, info, checks, score, label, possible).encode("utf-8"),
        file_name="자료_정보_분석_보고서.md", mime="text/markdown",
        use_container_width=True
    )
    st.warning("교육용 프로토타입입니다. 점수는 진실성 확률이 아니며, 메타정보는 수정될 수 있고 OCR은 오인식할 수 있습니다.")


st.set_page_config(page_title="자료 정보 분석기 V3", page_icon="🔎", layout="wide")
st.title("🔎 웹페이지·PDF·이미지 정보 분석기 V3")
st.write("웹 주소를 입력하거나 PDF·이미지 파일을 직접 업로드해 분석할 수 있습니다.")
st.info("정보 확인 지수는 자료의 진실성이 아니라 자동으로 확인 가능한 출처 단서의 지수입니다.")

with st.expander("지원 형식과 한계", expanded=False):
    st.markdown(f"""
    - 웹 주소: HTML, PDF, PNG/JPG, GIF, BMP, WEBP, TIFF
    - 로컬 파일 업로드: PDF 및 이미지
    - 파일 크기: 최대 12MB
    - PDF: 최대 {MAX_PDF_PAGES}페이지, 텍스트가 거의 없는 페이지는 스캔본으로 추정해 OCR 시도
    - PDF OCR: 스캔 의심 페이지 중 최대 {MAX_OCR_PAGES}페이지, 150 DPI
    - 이미지 OCR: 한국어와 영어(`kor+eng`)
    - OCR에는 `pytesseract`뿐 아니라 별도의 Tesseract OCR 프로그램과 한국어 언어 데이터가 필요합니다.
    - 일부 PDF는 암호화·복사 방지·복잡한 레이아웃 때문에 텍스트가 정확히 추출되지 않을 수 있습니다.
    """)

tab_url, tab_upload = st.tabs(["웹 주소 분석", "로컬 파일 업로드"])

with tab_url:
    url_input = st.text_input("분석할 웹페이지 또는 파일 URL",
                              placeholder="https://example.com/report.pdf",
                              key="url_input")
    if st.button("웹 주소 분석하기", type="primary", use_container_width=True):
        try:
            url = validate_url(url_input)
            with st.spinner("자료를 다운로드하고 분석하고 있습니다..."):
                data, ctype, final_url, status = download(url)
                info, checks, score, label, org_links, possible = analyze_bytes(
                    data, final_url, ctype, final_url, is_local=False
                )
            show_result(final_url, info, checks, score, label, org_links, possible, status)
        except requests.exceptions.Timeout:
            st.error("웹사이트 응답 시간이 초과되었습니다.")
        except requests.exceptions.RequestException as e:
            st.error(f"자료를 다운로드하지 못했습니다: {e}")
        except Exception as e:
            st.error(f"분석 중 문제가 발생했습니다: {e}")

with tab_upload:
    uploaded = st.file_uploader(
        "분석할 PDF 또는 이미지 파일을 선택하세요.",
        type=["pdf", "png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff"],
        accept_multiple_files=False
    )
    if uploaded is not None:
        st.caption(f"선택한 파일: {uploaded.name} · {uploaded.size / 1024:.1f} KB")
        if uploaded.size > MAX_BYTES:
            st.error("파일은 최대 12MB까지 분석할 수 있습니다.")
        elif st.button("업로드한 파일 분석하기", type="primary", use_container_width=True):
            try:
                with st.spinner("파일을 분석하고 있습니다. 스캔 PDF는 OCR 처리에 시간이 걸릴 수 있습니다..."):
                    data = uploaded.getvalue()
                    kind = detect(data, "", uploaded.name)
                    info, checks, score, label, org_links, possible = analyze_bytes(
                        data, uploaded.name, "", uploaded.name, is_local=True
                    )
                source = f"로컬 파일: {uploaded.name}"
                show_result(source, info, checks, score, label, org_links, possible)
            except Exception as e:
                st.error(f"파일 분석 중 문제가 발생했습니다: {e}")

st.caption("교육용 프로젝트 | URL 및 로컬 PDF·이미지 분석 | 최대 12MB")
