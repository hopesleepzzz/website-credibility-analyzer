import ipaddress
import json
import re
import socket
from io import BytesIO
from urllib.parse import quote_plus, urljoin, urlparse

import fitz
import pytesseract
import requests
import streamlit as st
from bs4 import BeautifulSoup
from PIL import ExifTags, Image

MAX_BYTES = 12 * 1024 * 1024
MAX_HTML_BYTES = 35 * 1024 * 1024
MAX_PDF_PAGES = 150
MAX_OCR_PAGES = 30
OCR_DPI = 150

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

OFFICIAL_DOMAINS = {
    "go.kr", "gov.kr", "korea.kr", "kostat.go.kr", "kdca.go.kr", "mfds.go.kr",
    "moe.go.kr", "moef.go.kr", "msit.go.kr", "me.go.kr", "molit.go.kr",
    "re.kr", "ac.kr", "kdi.re.kr", "stepi.re.kr", "kiep.go.kr", "kihasa.re.kr",
    "who.int", "wmo.int", "ipcc.ch", "un.org", "unesco.org", "oecd.org",
    "nih.gov", "cdc.gov", "nasa.gov", "noaa.gov", "europa.eu", "gov.uk", "edu", "gov"
}

INSTITUTION_KEYWORDS = (
    "대학교", "대학", "연구원", "연구소", "학회", "기관", "부처", "청", "위원회",
    "공사", "공단", "협회", "재단", "센터", "병원", "의과대학", "법학전문대학원",
    "University", "Institute", "Research", "Department", "Ministry", "Agency",
    "Laboratory", "Center", "Centre", "Foundation", "Academy", "Hospital"
)

ROLE_KEYWORDS = (
    "교수", "부교수", "조교수", "명예교수", "연구원", "선임연구원", "책임연구원",
    "수석연구원", "박사", "의사", "전문의", "변호사", "기자", "편집장", "위원",
    "국장", "과장", "센터장", "원장", "소장", "대표", "애널리스트", "전문가",
    "Professor", "Researcher", "Fellow", "PhD", "M.D.", "Director", "Analyst",
    "Editor", "Journalist", "Specialist", "Officer"
)

STOPWORDS = {
    "있다", "없다", "한다", "된다", "이다", "것이다", "수", "등", "및", "또는",
    "대한", "위한", "통해", "따라", "관련", "경우", "때문", "이상", "이하", "약",
    "최근", "현재", "기준", "결과", "분석", "조사", "발표", "자료", "내용", "정보",
    "the", "and", "for", "that", "with", "from", "this", "are", "was", "were",
    "have", "has", "had", "into", "about", "their", "which", "more", "than"
}


def public_host(host: str) -> bool:
    try:
        for item in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(item[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return False
        return True
    except (OSError, ValueError):
        return False


def validate_url(raw: str) -> str:
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


def download(url: str):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; SourceCredibilityAnalyzer/4.2; "
            "+https://streamlit.io)"
        )
    }
    for _ in range(6):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or not public_host(p.hostname):
            raise ValueError("안전하지 않은 주소로 이동해 다운로드를 중단했습니다.")
        with requests.get(
            url, headers=headers, timeout=20, stream=True, allow_redirects=False
        ) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                if not loc:
                    raise ValueError("이동 주소를 확인할 수 없습니다.")
                url = urljoin(url, loc)
                continue
            if r.status_code == 403:
                raise ValueError(
                    "대상 웹사이트에서 자동 요청을 차단했습니다(HTTP 403). "
                    "브라우저에서 페이지를 저장하거나 PDF/이미지로 내려받은 뒤 '로컬 파일 업로드'로 분석해 보세요."
                )
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "").split(";")[0].lower()
            is_html_resp = ctype in ("text/html", "application/xhtml+xml")
            byte_limit = MAX_HTML_BYTES if is_html_resp else MAX_BYTES
            chunks, total = [], 0
            for chunk in r.iter_content(65536):
                if chunk:
                    total += len(chunk)
                    if total > byte_limit:
                        limit_mb = byte_limit // (1024 * 1024)
                        raise ValueError(
                            f"{'웹페이지(HTML)' if is_html_resp else '파일'}은 최대 {limit_mb}MB까지 분석할 수 있습니다."
                        )
                    chunks.append(chunk)
            raw_bytes = b"".join(chunks)
            if not is_html_resp and (
                b"<html" in raw_bytes[:2000].lower()
                or b"<!doctype html" in raw_bytes[:2000].lower()
            ):
                if len(raw_bytes) > MAX_HTML_BYTES:
                    raise ValueError(
                        f"웹페이지(HTML)는 최대 {MAX_HTML_BYTES // (1024 * 1024)}MB까지 분석할 수 있습니다."
                    )
            return raw_bytes, ctype, url, r.status_code
    raise ValueError("웹페이지 이동 횟수가 너무 많습니다.")


def detect(data: bytes, ctype: str = "", url: str = "") -> str:
    h = data[:16]
    if h.startswith(b"%PDF-") or ctype == "application/pdf":
        return "pdf"
    if (
        h.startswith(b"\x89PNG\r\n\x1a\n")
        or h.startswith(b"\xff\xd8\xff")
        or h[:6] in (b"GIF87a", b"GIF89a")
        or h.startswith(b"BM")
        or (len(h) >= 12 and h[:4] == b"RIFF" and h[8:12] == b"WEBP")
        or ctype.startswith("image/")
    ):
        return "image"
    if (
        ctype in ("text/html", "application/xhtml+xml")
        or b"<html" in data[:2000].lower()
        or b"<!doctype html" in data[:2000].lower()
    ):
        return "html"
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith(tuple(IMAGE_EXTENSIONS)):
        return "image"
    raise ValueError(
        "지원 형식을 판별하지 못했습니다. HTML, PDF, PNG, JPG, GIF, BMP, WEBP, TIFF를 지원합니다."
    )


def meta(soup: BeautifulSoup, attr: str, key: str) -> str:
    tag = soup.find("meta", attrs={attr: key})
    return (tag.get("content") or "").strip() if tag else ""


def walk_jsonld(obj, out: list):
    if isinstance(obj, dict):
        out.append(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk_jsonld(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_jsonld(v, out)


def extract_author_details(v) -> dict:
    info = {"name": "", "job_title": "", "affiliation": "", "same_as": []}
    if isinstance(v, str):
        info["name"] = v.strip()
        return info
    if isinstance(v, dict):
        info["name"] = str(v.get("name") or "").strip()
        info["job_title"] = str(v.get("jobTitle") or v.get("roleName") or "").strip()
        aff = v.get("affiliation") or v.get("worksFor") or v.get("memberOf")
        if isinstance(aff, dict):
            info["affiliation"] = str(aff.get("name") or "").strip()
        elif isinstance(aff, list):
            names = [
                str(x.get("name") if isinstance(x, dict) else x).strip()
                for x in aff
                if x
            ]
            info["affiliation"] = ", ".join(filter(None, names))
        elif isinstance(aff, str):
            info["affiliation"] = aff.strip()
        same_as = v.get("sameAs") or v.get("url")
        if isinstance(same_as, str) and same_as.startswith(("http://", "https://")):
            info["same_as"] = [same_as]
        elif isinstance(same_as, list):
            info["same_as"] = [
                s for s in same_as if isinstance(s, str) and s.startswith(("http://", "https://"))
            ]
        return info
    if isinstance(v, list):
        merged_names, merged_jobs, merged_affs, merged_urls = [], [], [], []
        for item in v:
            sub = extract_author_details(item)
            if sub["name"]:
                merged_names.append(sub["name"])
            if sub["job_title"]:
                merged_jobs.append(sub["job_title"])
            if sub["affiliation"]:
                merged_affs.append(sub["affiliation"])
            merged_urls.extend(sub["same_as"])
        info["name"] = ", ".join(dict.fromkeys(merged_names))
        info["job_title"] = ", ".join(dict.fromkeys(merged_jobs))
        info["affiliation"] = ", ".join(dict.fromkeys(merged_affs))
        info["same_as"] = list(dict.fromkeys(merged_urls))
    return info


def person(v) -> str:
    details = extract_author_details(v)
    parts = [p for p in (details["name"], details["job_title"], details["affiliation"]) if p]
    return " / ".join(parts)


def is_official_domain(host: str) -> bool:
    host = (host or "").lower().strip(".")
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS)


def clean_pdf_date(raw: str) -> str:
    raw = (raw or "").strip()
    m = re.match(r"^D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?", raw)
    if not m:
        return raw
    y, mo, d, h, mi = m.groups()
    date_part = "-".join(p for p in (y, mo, d) if p)
    if h and mi:
        return f"{date_part} {h}:{mi}"
    return date_part


def decode_html_bytes(data: bytes) -> str:
    head_sample = data[:4096].decode("ascii", errors="ignore").lower()
    m = re.search(r'charset=["\']?([a-z0-9_\-]+)', head_sample)
    candidates = []
    if m:
        enc = m.group(1).strip()
        if enc.lower() in ("euc-kr", "ks_c_5601-1987", "cp949"):
            candidates.extend(["cp949", "euc-kr"])
        else:
            candidates.append(enc)
    candidates.extend(["utf-8", "cp949", "euc-kr"])
    for enc in dict.fromkeys(candidates):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def parse_html(data: bytes, url: str) -> dict:
    raw_size_kb = round(len(data) / 1024, 1)
    html_str = decode_html_bytes(data)
    soup = BeautifulSoup(html_str, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    author = meta(soup, "name", "author") or meta(soup, "property", "article:author")
    date = (
        meta(soup, "property", "article:published_time")
        or meta(soup, "name", "date")
        or meta(soup, "itemprop", "datePublished")
    )
    publisher = meta(soup, "name", "publisher") or meta(soup, "property", "og:site_name")
    description = meta(soup, "name", "description") or meta(soup, "property", "og:description")
    keywords = meta(soup, "name", "keywords")

    author_struct = {"name": author, "job_title": "", "affiliation": "", "same_as": []}
    objects = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk_jsonld(json.loads(script.string or script.get_text()), objects)
        except Exception:
            pass
    for obj in objects:
        if obj.get("author"):
            extracted = extract_author_details(obj.get("author"))
            author = author or person(obj.get("author"))
            for k in ("name", "job_title", "affiliation"):
                if not author_struct[k] and extracted[k]:
                    author_struct[k] = extracted[k]
            author_struct["same_as"] = list(
                dict.fromkeys(author_struct["same_as"] + extracted["same_as"])
            )
        date = date or str(obj.get("datePublished") or "")
        publisher = publisher or person(obj.get("publisher"))
        if not keywords and obj.get("keywords"):
            kw = obj.get("keywords")
            keywords = ", ".join(kw) if isinstance(kw, list) else str(kw)

    links, about = [], []
    host = (urlparse(url).hostname or "").lower()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        p = urlparse(href)
        if p.scheme not in ("http", "https") or not p.hostname:
            continue
        label = a.get_text(" ", strip=True) or href
        if p.hostname.lower() != host and len(links) < 250:
            links.append({"label": label[:100], "url": href})
        if any(
            w in (label + " " + p.path).lower()
            for w in ("소개", "문의", "연락처", "about", "contact", "편집", "editorial")
        ):
            if len(about) < 20:
                about.append({"label": label[:80], "url": href})
        if len(links) >= 250 and len(about) >= 20:
            break

    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    main_container = (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"role": "main"})
        or soup.body
        or soup
    )
    clean_text = " ".join(main_container.get_text(" ", strip=True).split())[:30000]

    return {
        "type": "HTML 웹페이지",
        "title": title,
        "author": author,
        "author_struct": author_struct,
        "date": date,
        "publisher": publisher,
        "description": description,
        "keywords": keywords,
        "text": clean_text,
        "links": links,
        "about": about,
        "metadata": {
            "원본 HTML 크기": f"{raw_size_kb} KB",
            "호스트 도메인": host or "확인되지 않음",
            "공식 기관 도메인 여부": "공식·학술 도메인 패턴에 해당" if is_official_domain(host) else "일반 도메인",
            "소개·연락처 페이지 링크 수": f"{len(about)}개",
        },
    }


def run_ocr(pil_image: Image.Image) -> str:
    return pytesseract.image_to_string(pil_image, lang="kor+eng").strip()


def parse_pdf(data: bytes) -> dict:
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
            if uri and uri.startswith(("http://", "https://")) and len(links) < 250:
                links.append({"label": f"{page.number + 1}쪽 링크", "url": uri})

    targets = scanned_candidates[:MAX_OCR_PAGES]
    for page_index in targets:
        try:
            page = doc.load_page(page_index)
            pix = page.get_pixmap(
                matrix=fitz.Matrix(OCR_DPI / 72, OCR_DPI / 72),
                colorspace=fitz.csRGB,
                alpha=False,
            )
            with Image.open(BytesIO(pix.tobytes("png"))) as img:
                extracted = run_ocr(img.convert("RGB"))
            if extracted:
                texts[page_index] = extracted
                ocr_pages.append(page_index + 1)
        except Exception as exc:
            ocr_errors.append(str(exc)[:180])
            break

    count = doc.page_count
    doc.close()
    full_text = "\n".join(texts)[:30000]
    raw_author = (md.get("author") or "").strip()
    raw_date = clean_pdf_date(md.get("creationDate") or md.get("modDate") or "")
    metadata = {
        "페이지 수": str(count),
        "PDF 생성 프로그램": md.get("creator") or "확인되지 않음",
        "PDF 변환 프로그램": md.get("producer") or "확인되지 않음",
        "수정 날짜": clean_pdf_date(md.get("modDate") or "") or "확인되지 않음",
        "OCR 처리 페이지": ", ".join(map(str, ocr_pages)) if ocr_pages else "없음",
        "OCR 대상 페이지 수": f"{len(targets)}쪽 (최대 {MAX_OCR_PAGES}쪽)",
        "OCR 상태": (
            "OCR 텍스트 추출 완료"
            if ocr_pages
            else "스캔 페이지가 없거나 OCR 텍스트가 추출되지 않음"
        ),
    }
    warning = ""
    if scanned_candidates and not ocr_pages:
        warning = (
            "스캔 페이지에서 OCR 텍스트를 추출하지 못했습니다. "
            "Tesseract OCR과 한국어(kor) 언어 데이터 설치 여부를 확인하세요."
        )
    if len(scanned_candidates) > MAX_OCR_PAGES:
        warning += (
            f" 스캔 의심 페이지 {len(scanned_candidates)}쪽 중 "
            f"앞의 {MAX_OCR_PAGES}쪽만 OCR 처리했습니다."
        )
    if ocr_errors:
        warning += " OCR 오류: " + ocr_errors[0]
    metadata["OCR 안내"] = warning

    return {
        "type": "PDF 문서",
        "title": (md.get("title") or "").strip(),
        "author": raw_author,
        "author_struct": {"name": raw_author, "job_title": "", "affiliation": "", "same_as": []},
        "date": raw_date,
        "publisher": (md.get("producer") or md.get("creator") or "").strip(),
        "description": (md.get("subject") or "").strip(),
        "keywords": (md.get("keywords") or "").strip(),
        "text": full_text,
        "links": links,
        "about": [],
        "metadata": metadata,
    }


def parse_image(data: bytes) -> dict:
    with Image.open(BytesIO(data)) as im:
        exif = {}
        try:
            for key, value in im.getexif().items():
                name = ExifTags.TAGS.get(key, str(key))
                if name in (
                    "Artist",
                    "DateTime",
                    "DateTimeOriginal",
                    "Software",
                    "Copyright",
                    "ImageDescription",
                ):
                    exif[name] = str(value)[:300]
        except Exception:
            pass
        text, warning = "", ""
        try:
            text = run_ocr(im.convert("RGB"))
        except Exception as exc:
            warning = (
                "이미지 OCR을 실행하지 못했습니다. Tesseract OCR과 한국어(kor) "
                "언어 데이터를 설치해야 합니다. (" + str(exc)[:120] + ")"
            )
        img_format = im.format or "알 수 없음"
        img_size = f"{im.width} × {im.height} 픽셀"

    raw_author = exif.get("Artist", "")
    return {
        "type": "이미지",
        "title": exif.get("ImageDescription", ""),
        "author": raw_author,
        "author_struct": {"name": raw_author, "job_title": "", "affiliation": "", "same_as": []},
        "date": exif.get("DateTimeOriginal", exif.get("DateTime", "")),
        "publisher": exif.get("Copyright", ""),
        "description": "",
        "keywords": "",
        "text": text[:15000],
        "links": [],
        "about": [],
        "metadata": {
            "이미지 형식": img_format,
            "이미지 크기": img_size,
            "촬영·생성 프로그램": exif.get("Software", "확인되지 않음"),
            "OCR 상태": "텍스트 추출됨" if text else "텍스트 없음 또는 OCR 미설치",
            "OCR 안내": warning,
        },
    }


def analyze_author_profile(source_url: str, info: dict, is_local: bool = False) -> dict:
    struct = dict(info.get("author_struct") or {})
    name = (struct.get("name") or info.get("author") or "").strip()
    job_title = (struct.get("job_title") or "").strip()
    affiliation = (struct.get("affiliation") or "").strip()
    same_as = list(struct.get("same_as") or [])

    snippet_source = " ".join(
        filter(None, [info.get("author", ""), info.get("publisher", ""), info.get("text", "")[:1200]])
    )

    detected_institutions = []
    for kw in INSTITUTION_KEYWORDS:
        for m in re.finditer(rf"([가-힣A-Za-z0-9·\-\s]{{2,28}}{re.escape(kw)})", snippet_source):
            cand = " ".join(m.group(1).split()).strip(" ,.;:()[]")
            if 3 <= len(cand) <= 45 and cand not in detected_institutions:
                detected_institutions.append(cand)
            if len(detected_institutions) >= 5:
                break
        if len(detected_institutions) >= 5:
            break

    detected_roles = []
    for rk in ROLE_KEYWORDS:
        if re.search(re.escape(rk), snippet_source, re.IGNORECASE):
            if rk not in detected_roles:
                detected_roles.append(rk)
        if len(detected_roles) >= 6:
            break

    if not affiliation and detected_institutions:
        affiliation = ", ".join(detected_institutions[:2])
    if not job_title and detected_roles:
        job_title = ", ".join(detected_roles[:3])

    host = urlparse(source_url).hostname or "" if not is_local else ""
    official_links = [
        x for x in info.get("links", []) if is_official_domain(urlparse(x["url"]).hostname or "")
    ]
    author_official_urls = [u for u in same_as if is_official_domain(urlparse(u).hostname or "")]

    topic_terms = extract_keywords(
        " ".join(filter(None, [info.get("title", ""), info.get("description", ""), info.get("keywords", "")])),
        limit=6,
    )
    if not topic_terms:
        topic_terms = extract_keywords(info.get("text", "")[:2500], limit=6)

    confirmed_facts = []
    needs_verification = []

    if name:
        confirmed_facts.append(f"메타정보 또는 구조화 데이터에서 작성자 표기('{name}')가 확인되었습니다.")
    else:
        needs_verification.append("문서 메타정보에서 작성자 실명이 추출되지 않았습니다.")

    if affiliation:
        confirmed_facts.append(f"소속 기관 단서('{affiliation}')가 메타정보 또는 본문 도입부에서 확인되었습니다.")
    else:
        needs_verification.append("작성자의 공식 소속 기관을 메타정보만으로 특정하지 못했습니다.")

    if job_title:
        confirmed_facts.append(f"직책·역할 관련 표기('{job_title}')가 확인되었습니다.")
    else:
        needs_verification.append("작성자의 구체적인 직책이나 자격 정보가 명시되지 않았습니다.")

    if not is_local and host:
        if is_official_domain(host):
            confirmed_facts.append(f"출처 도메인('{host}')이 공공·연구·교육 기관 도메인 패턴에 해당합니다.")
        else:
            needs_verification.append(
                f"출처 도메인('{host}')은 일반 도메인이며, 공식 기관 소속 여부는 기관 누리집에서 별도 확인이 필요합니다."
            )

    if author_official_urls:
        confirmed_facts.append(f"작성자 프로필 링크 중 공식 기관 도메인({len(author_official_urls)}건)이 연결되어 있습니다.")
    elif official_links:
        confirmed_facts.append(f"본문 내 공식·연구기관 도메인 링크 {len(official_links)}건이 확인되었습니다.")

    if topic_terms:
        needs_verification.append(
            "문서의 주요 주제 키워드(" + ", ".join(topic_terms[:4]) + ")와 작성자의 실제 전공·담당 업무가 일치하는지 확인이 필요합니다."
        )
    else:
        needs_verification.append("작성자의 전문 분야와 본문 주제의 일치 여부를 공식 이력에서 대조해야 합니다.")

    query_parts = [p for p in (name, affiliation.split(",")[0] if affiliation else "", " ".join(topic_terms[:2])) if p]
    search_query = " ".join(query_parts).strip() or (info.get("title") or "")[:60]

    return {
        "name": name or "확인되지 않음",
        "affiliation": affiliation or "확인되지 않음",
        "job_title": job_title or "확인되지 않음",
        "specialty_clues": topic_terms,
        "confirmed_facts": confirmed_facts,
        "needs_verification": needs_verification,
        "official_links": official_links,
        "search_query": search_query,
    }


def extract_sentences(text: str, max_items: int = 25) -> list:
    if not text:
        return []
    raw_parts = re.split(r"(?<=[.!?。다요죠음함임])\s+|\n+", text)
    candidates = []
    for part in raw_parts:
        s = " ".join(part.split()).strip()
        if 20 <= len(s) <= 240:
            has_claim_signal = bool(
                re.search(
                    r"(\d+\s*(%|명|건|원|달러|배|개|년|월)|발표|조사|연구|결과|증가|감소|원인|효과|따라서|밝혔|나타났|보고)",
                    s,
                )
            )
            if has_claim_signal and s not in candidates:
                candidates.append(s)
        if len(candidates) >= max_items:
            break
    if len(candidates) < 5:
        for part in raw_parts:
            s = " ".join(part.split()).strip()
            if 25 <= len(s) <= 240 and s not in candidates:
                candidates.append(s)
            if len(candidates) >= max_items:
                break
    return candidates


def extract_keywords(text: str, limit: int = 12) -> list:
    tokens = re.findall(r"[가-힣]{2,}|[A-Za-z]{3,}", text.lower())
    freq = {}
    for tok in tokens:
        if tok in STOPWORDS:
            continue
        freq[tok] = freq.get(tok, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], -len(x[0]), x[0]))
    return [w for w, _ in ranked[:limit]]


def extract_numbers(text: str) -> set:
    nums = set()
    for m in re.finditer(r"\d+(?:[.,]\d+)?\s*(?:%|퍼센트|만|억|조|명|건|배|개|년)?", text):
        norm = re.sub(r"\s+", "", m.group(0)).replace("퍼센트", "%")
        nums.add(norm)
    return nums


def has_negation(text: str) -> bool:
    return bool(
        re.search(
            r"(않는다|않았다|아니다|없다|없었다|부정|반박|오류|사실이\s*아|근거\s*없|not\s+|no\s+evidence|false|incorrect|refute)",
            text,
            re.IGNORECASE,
        )
    )


def build_official_search_links(claim: str) -> list:
    keywords = extract_keywords(claim, limit=5)
    core_query = " ".join(keywords) if keywords else claim[:60]
    q_gov = quote_plus(f"{core_query} site:go.kr OR site:re.kr OR site:ac.kr")
    q_scholar = quote_plus(core_query)
    q_kci = quote_plus(core_query)
    return [
        {
            "기관 유형": "대한민국 공공기관·정부부처 (go.kr)",
            "검색어": f"{core_query} site:go.kr",
            "url": f"https://www.google.com/search?q={quote_plus(core_query + ' site:go.kr')}",
        },
        {
            "기관 유형": "국책연구기관·대학 학술자료 (re.kr / ac.kr)",
            "검색어": f"{core_query} site:re.kr OR site:ac.kr",
            "url": f"https://www.google.com/search?q={q_gov}",
        },
        {
            "기관 유형": "학술 논문 검색 (Google Scholar)",
            "검색어": core_query,
            "url": f"https://scholar.google.com/scholar?q={q_scholar}",
        },
        {
            "기관 유형": "국내 학술지 인용색인 (KCI)",
            "검색어": core_query,
            "url": f"https://www.kci.go.kr/kciportal/po/search/poArtiSearList.kci?poArtiSearBean.searWord={q_kci}",
        },
    ]


def compare_claim_with_reference(
    claim: str,
    ref_text: str,
    ref_source_name: str = "",
    ref_source_url: str = "",
) -> dict:
    claim = " ".join(claim.split()).strip()
    ref_text = " ".join(ref_text.split()).strip()

    if not claim:
        raise ValueError("비교할 핵심 주장을 입력하거나 선택해 주세요.")

    claim_kw = set(extract_keywords(claim, limit=15))
    claim_nums = extract_numbers(claim)

    if not ref_text:
        return {
            "status": "근거 부족",
            "matched_keywords": [],
            "missing_keywords": sorted(claim_kw),
            "matched_numbers": [],
            "mismatched_numbers": sorted(claim_nums),
            "confirmed_facts": [
                f"비교 대상 주장('{claim}')에서 핵심 키워드 {len(claim_kw)}개를 추출했습니다."
            ],
            "needs_verification": [
                "대조할 공공기관·연구기관·대학 자료 본문이 입력되지 않아 교차 검증을 수행할 수 없습니다.",
                "제시된 기관 검색 바로가기를 통해 공식 자료를 확인한 뒤 근거 문단을 입력해 대조해 주세요.",
            ],
            "ref_source_name": ref_source_name or "미입력",
            "ref_source_url": ref_source_url,
            "is_official_ref": is_official_domain(urlparse(ref_source_url).hostname or "") if ref_source_url else False,
        }

    ref_kw = set(extract_keywords(ref_text, limit=80))
    ref_nums = extract_numbers(ref_text)

    matched_kw = sorted(claim_kw & ref_kw)
    missing_kw = sorted(claim_kw - ref_kw)
    kw_ratio = (len(matched_kw) / len(claim_kw)) if claim_kw else 0.0

    matched_nums = sorted(claim_nums & ref_nums)
    mismatched_nums = sorted(claim_nums - ref_nums)

    claim_neg = has_negation(claim)
    ref_neg = has_negation(ref_text)
    polarity_conflict = (claim_neg != ref_neg) and kw_ratio >= 0.4

    if kw_ratio < 0.25 and not matched_nums:
        status = "근거 부족"
    elif polarity_conflict or (claim_nums and mismatched_nums and not matched_nums and kw_ratio >= 0.35):
        status = "불일치"
    elif kw_ratio >= 0.65 and (not claim_nums or (matched_nums and not mismatched_nums)):
        status = "일치"
    else:
        status = "부분 일치"

    confirmed_facts = []
    needs_verification = []

    if matched_kw:
        confirmed_facts.append(
            "핵심 용어 " + str(len(claim_kw)) + "개 중 " + str(len(matched_kw)) + "개(" + ", ".join(matched_kw[:6]) + ")가 비교 자료에서 확인되었습니다."
        )
    if matched_nums:
        confirmed_facts.append(
            "수치·연도 표현(" + ", ".join(matched_nums) + ")이 비교 자료와 동일하게 나타납니다."
        )
    if ref_source_url:
        ref_host = urlparse(ref_source_url).hostname or ""
        if is_official_domain(ref_host):
            confirmed_facts.append(f"비교 출처 도메인('{ref_host}')이 공공·연구·교육 기관 도메인 패턴에 해당합니다.")
        elif ref_host:
            needs_verification.append(f"비교 출처 도메인('{ref_host}')이 공식 기관 누리집인지 확인이 필요합니다.")

    if missing_kw:
        needs_verification.append(
            "주장의 일부 핵심어(" + ", ".join(missing_kw[:6]) + ")는 제시된 비교 자료에서 직접 확인되지 않았습니다."
        )
    if mismatched_nums:
        needs_verification.append(
            "주장에 포함된 수치(" + ", ".join(mismatched_nums) + ")가 비교 자료의 수치와 다르거나 누락되어 원문 대조가 필요합니다."
        )
    if polarity_conflict:
        needs_verification.append(
            "주장과 비교 자료 사이에 긍정·부정 서술 방향의 차이가 감지되어 문맥 정밀 확인이 필요합니다."
        )
    if not confirmed_facts:
        confirmed_facts.append("주장과 비교 자료 간 직접적으로 겹치는 핵심어 또는 수치가 거의 확인되지 않았습니다.")

    return {
        "status": status,
        "matched_keywords": matched_kw,
        "missing_keywords": missing_kw,
        "matched_numbers": matched_nums,
        "mismatched_numbers": mismatched_nums,
        "confirmed_facts": confirmed_facts,
        "needs_verification": needs_verification,
        "ref_source_name": ref_source_name or "사용자 입력 참고 자료",
        "ref_source_url": ref_source_url,
        "is_official_ref": is_official_domain(urlparse(ref_source_url).hostname or "") if ref_source_url else False,
    }


def evaluate(source: str, info: dict, author_profile: dict, is_local: bool = False):
    p = urlparse(source) if not is_local else None
    host = (p.hostname or "").lower() if p else ""
    links = info["links"]
    org = [x for x in links if is_official_domain(urlparse(x["url"]).hostname or "")]
    checks = []
    possible = earned = 0

    def add(
        name: str,
        result: str,
        max_points: int,
        earned_points: int,
        fact: str,
        verify: str,
        applicable: bool = True,
    ):
        nonlocal possible, earned
        clamped_earned = max(0, min(max_points, earned_points)) if applicable else None
        checks.append(
            {
                "name": name,
                "result": result,
                "max_points": max_points if applicable else None,
                "points": clamped_earned,
                "fact": fact,
                "verify": verify,
            }
        )
        if applicable:
            possible += max_points
            earned += clamped_earned

    if not is_local:
        is_https = p.scheme == "https"
        add(
            "전송 보안(HTTPS)",
            "HTTPS 확인됨" if is_https else "HTTP 사용",
            10,
            10 if is_https else 0,
            "암호화 통신 프로토콜(HTTPS) 사용 여부가 확인되었습니다." if is_https else "비암호화(HTTP) 연결로 확인되었습니다.",
            "통신 보안과 본문 내용의 사실 여부는 별개이므로 출처 기관의 공식성을 함께 확인하세요.",
        )
    else:
        add(
            "전송 보안(HTTPS)",
            "로컬 파일(해당 없음)",
            10,
            0,
            "로컬 업로드 파일로 웹 전송 프로토콜 평가 대상에서 제외되었습니다.",
            "파일을 내려받은 원본 웹 주소의 공식 도메인 여부를 별도로 확인하세요.",
            applicable=False,
        )

    title_text = (info.get("title") or "").strip()
    title_pts = 10 if len(title_text) >= 12 else (5 if title_text else 0)
    add(
        "제목·문서 식별 정보",
        "확인됨" if title_text else "메타정보에서 확인되지 않음",
        10,
        title_pts,
        f"문서 제목('{title_text[:60]}')이 확인되었습니다." if title_text else "문서 제목 메타데이터가 비어 있습니다.",
        "제목이 본문 핵심 내용과 과장 없이 일치하는지 확인이 필요합니다.",
    )

    has_author = author_profile["name"] != "확인되지 않음"
    has_aff = author_profile["affiliation"] != "확인되지 않음"
    has_role = author_profile["job_title"] != "확인되지 않음"
    if has_author and has_aff:
        author_pts = 15
    elif has_author and has_role:
        author_pts = 12
    elif has_author or has_aff:
        author_pts = 7
    else:
        author_pts = 0
    add(
        "작성자 및 소속·직책 단서",
        f"{author_profile['name']} / {author_profile['affiliation']}"
        if has_author
        else ("소속 단서만 확인됨" if has_aff else "작성자 정보 미확인"),
        15,
        author_pts,
        " ".join(author_profile["confirmed_facts"][:2]) if author_profile["confirmed_facts"] else "작성자·소속 단서가 충분하지 않습니다.",
        "공식 기관 누리집·연구자 프로필에서 실제 재직 여부와 전문 분야 일치 여부를 확인하세요.",
    )

    date_text = (info.get("date") or "").strip()
    date_pts = 10 if re.search(r"\d{4}", date_text) else (5 if date_text else 0)
    add(
        "작성·발행 날짜 정보",
        date_text or "날짜 메타정보 없음",
        10,
        date_pts,
        f"날짜 메타정보('{date_text}')가 확인되었습니다." if date_text else "작성일 또는 수정일 메타정보가 확인되지 않았습니다.",
        "현재 시점에서도 유효한 최신 통계·제도인지 이후 개정·후속 발표 여부를 확인하세요.",
    )

    pub_text = (info.get("publisher") or "").strip()
    is_official_host = (not is_local) and is_official_domain(host)
    if pub_text and is_official_host:
        pub_pts = 15
    elif pub_text or is_official_host:
        pub_pts = 10
    else:
        pub_pts = 0
    add(
        "발행처·제작 정보",
        pub_text or ("공식 기관 도메인" if is_official_host else "발행처 정보 없음"),
        15,
        pub_pts,
        f"발행·제작 주체 표기('{pub_text[:60]}')가 확인되었습니다." if pub_text else (f"공식 기관 도메인('{host}')이 확인되었습니다." if is_official_host else "발행 기관이나 제작자 메타정보가 확인되지 않았습니다."),
        "발행 주체의 설립 목적이나 이해관계(홍보·상업적 목적 등)가 있는지는 별도 검토가 필요합니다.",
    )

    links_applicable = info["type"] != "이미지"
    link_count = len(links)
    if link_count >= 5:
        link_pts = 15
    elif link_count >= 2:
        link_pts = 10
    elif link_count == 1:
        link_pts = 5
    else:
        link_pts = 0
    add(
        "외부 참고 링크 수",
        f"{link_count}개 확인됨" if link_count else "확인되지 않음",
        15,
        link_pts,
        f"문서 내 외부 연결 링크 {link_count}개가 추출되었습니다." if link_count else "외부 출처로 연결되는 하이퍼링크가 추출되지 않았습니다.",
        "연결된 링크가 실제로 본문 주장을 뒷받침하는 1차 자료인지 직접 열어 확인해야 합니다.",
        applicable=links_applicable,
    )

    org_count = len(org)
    if org_count >= 3:
        org_pts = 20
    elif org_count == 2:
        org_pts = 14
    elif org_count == 1:
        org_pts = 8
    else:
        org_pts = 0
    add(
        "공공·연구·대학 도메인 인용",
        f"{org_count}개 확인됨" if org_count else "확인되지 않음",
        20,
        org_pts,
        f"공공·연구·대학 도메인(.go.kr, .re.kr, .ac.kr 등) 링크 {org_count}개가 확인되었습니다."
        if org_count
        else "등록된 공공·연구·대학 도메인 패턴의 외부 링크가 발견되지 않았습니다.",
        "인용된 기관 자료의 원문 수치·결론이 본문에 왜곡 없이 반영되었는지 대조가 필요합니다.",
        applicable=links_applicable,
    )

    text_len = len((info.get("text") or "").strip())
    if text_len >= 500:
        text_pts = 5
    elif text_len >= 50:
        text_pts = 3
    elif text_len > 0:
        text_pts = 1
    else:
        text_pts = 0
    add(
        "본문 텍스트 추출",
        f"추출 완료 ({text_len}자)" if text_len else "추출되지 않음",
        5,
        text_pts,
        f"분석 가능한 본문 텍스트(약 {text_len}자)가 추출되었습니다." if text_len else "추출된 본문 텍스트가 없습니다.",
        "OCR 또는 자동 파싱 과정에서 오탈자나 표 구조 누락이 발생했을 수 있으므로 원문과 대조하세요.",
    )

    score = round(earned / possible * 100) if possible else 0
    summary_statement = (
        "출처 식별 단서와 기관 관련 메타정보가 다수 확인되었으나, 핵심 주장의 사실 여부는 공식 자료 대조를 통해 최종 확인해야 합니다."
        if score >= 75
        else "일부 출처·작성자 단서가 확인되었으나 누락된 메타정보가 있어 공식 기관 자료와의 교차 검증이 필요합니다."
        if score >= 45
        else "자동으로 확인 가능한 출처·작성자 단서가 제한적이므로, 공식 기관의 1차 자료를 추가로 확보해 검증해야 합니다."
    )
    return checks, score, summary_statement, org, possible


def report(
    source: str,
    info: dict,
    author_profile: dict,
    checks: list,
    score: int,
    summary_statement: str,
    claim_results: list,
) -> str:
    lines = [
        "# 자료 출처 및 주장 검증 분석 보고서",
        "",
        "## 1. 기본 서지·메타정보",
        f"- **자료 형식**: {info['type']}",
        f"- **출처 경로**: {source}",
        f"- **제목**: {info['title'] or '확인되지 않음'}",
        f"- **작성자**: {author_profile['name']}",
        f"- **추정 소속 기관**: {author_profile['affiliation']}",
        f"- **추정 직책·역할**: {author_profile['job_title']}",
        f"- **날짜**: {info['date'] or '확인되지 않음'}",
        f"- **발행·제작**: {info['publisher'] or '확인되지 않음'}",
        f"- **출처 단서 확인 지수**: {score}/100 (평가 가능 항목 기준 환산)",
        f"- **종합 소견**: {summary_statement}",
        "",
        "## 2. 작성자 검증 요약",
        "### 확인된 사실",
    ]
    for item in author_profile["confirmed_facts"]:
        lines.append(f"- {item}")
    lines.append("### 추가 검증이 필요한 사항")
    for item in author_profile["needs_verification"]:
        lines.append(f"- {item}")

    lines += ["", "## 3. 출처 단서 항목별 분석"]
    for c in checks:
        if c["points"] is None:
            pt = "평가 대상 제외"
        else:
            pt = f"{c['points']}/{c['max_points']}점"
        lines.append(f"- **{c['name']}**: {c['result']} ({pt})")
        lines.append(f"  - 확인된 사실: {c['fact']}")
        lines.append(f"  - 추가 검증 필요: {c['verify']}")

    lines += ["", "## 4. 핵심 주장 및 공식 자료 대조 결과"]
    if claim_results:
        for idx, cr in enumerate(claim_results, 1):
            lines += [
                f"### 주장 {idx}. [{cr['status']}]",
                f"- **검증 대상 주장**: {cr['claim']}",
                f"- **비교 기준 자료**: {cr['ref_source_name']}"
                + (f" ({cr['ref_source_url']})" if cr.get("ref_source_url") else ""),
                f"- **분류 결과**: **{cr['status']}**",
                "- **확인된 사실**:",
            ]
            for f_item in cr["confirmed_facts"]:
                lines.append(f"  - {f_item}")
            lines.append("- **추가 검증이 필요한 사항**:")
            for v_item in cr["needs_verification"]:
                lines.append(f"  - {v_item}")
            lines.append("")
    else:
        lines.append("- 아직 대조가 수행된 핵심 주장이 없습니다.")

    if info["metadata"]:
        lines += ["", "## 5. 세부 기술 메타정보"]
        for k, v in info["metadata"].items():
            if v:
                lines.append(f"- **{k}**: {v}")

    lines += [
        "",
        "---",
        "※ 본 분석 결과는 자동으로 추출된 메타정보와 텍스트 대조에 기반한 객관적 참고 자료입니다. "
        "최종 사실 여부는 반드시 해당 공공기관·연구기관·대학의 공식 원문과 대조하여 판단하시기 바랍니다.",
    ]
    return "\n".join(lines)


def analyze_bytes(data: bytes, content_type: str = "", source_url: str = "", is_local: bool = False):
    kind = detect(data, content_type, source_url)
    info = (
        parse_html(data, source_url)
        if kind == "html"
        else parse_pdf(data)
        if kind == "pdf"
        else parse_image(data)
    )
    author_profile = analyze_author_profile(source_url, info, is_local=is_local)
    checks, score, summary_statement, org_links, possible = evaluate(
        source_url, info, author_profile, is_local=is_local
    )
    return info, author_profile, checks, score, summary_statement, org_links, possible


def render_status_badge(status: str):
    if status == "일치":
        st.success(f"비교 결과 분류: **{status}**")
    elif status == "부분 일치":
        st.info(f"비교 결과 분류: **{status}**")
    elif status == "불일치":
        st.error(f"비교 결과 분류: **{status}**")
    else:
        st.warning(f"비교 결과 분류: **{status}**")


def show_result(
    source: str,
    info: dict,
    author_profile: dict,
    checks: list,
    score: int,
    summary_statement: str,
    org_links: list,
    status=None,
):
    st.success(f"{info['type']} 분석이 완료되었습니다." + (f" (HTTP {status})" if status else ""))
    if not source.startswith("로컬 파일:"):
        st.caption(f"최종 확인 주소: {source}")

    col_left, col_right = st.columns([1, 2])
    with col_left:
        st.metric("출처 단서 확인 지수", f"{score}/100")
        st.progress(score / 100)
        st.caption("항목별 실제 획득 점수를 평가 가능 총배점으로 나눈 백분율입니다.")
    with col_right:
        st.subheader(info["title"] or info["type"])
        st.write(summary_statement)
        st.write(
            f"**날짜:** {info['date'] or '자동 추출되지 않음'} · "
            f"**발행·제작:** {info['publisher'] or '자동 추출되지 않음'}"
        )

    st.divider()
    st.subheader("1. 작성자 검증 (소속 · 직책 · 전문 분야 단서)")
    a1, a2, a3 = st.columns(3)
    a1.metric("작성자", author_profile["name"])
    a2.metric("추정 소속 기관", author_profile["affiliation"])
    a3.metric("추정 직책·역할", author_profile["job_title"])

    if author_profile["specialty_clues"]:
        st.write("**문서 핵심 주제·전문 분야 키워드:** " + ", ".join(author_profile["specialty_clues"]))

    v_col1, v_col2 = st.columns(2)
    with v_col1:
        with st.container(border=True):
            st.markdown("**확인된 사실**")
            for item in author_profile["confirmed_facts"]:
                st.markdown(f"- {item}")
    with v_col2:
        with st.container(border=True):
            st.markdown("**추가 검증이 필요한 사항**")
            for item in author_profile["needs_verification"]:
                st.markdown(f"- {item}")

    q_encoded = quote_plus(author_profile["search_query"])
    st.markdown(
        f"공식 기관 소속·이력 대조 검색: "
        f"[공공·연구·대학 도메인 검색](https://www.google.com/search?q={q_encoded}+site%3Ago.kr+OR+site%3Are.kr+OR+site%3Aac.kr) · "
        f"[학술 논문·저자 검색](https://scholar.google.com/scholar?q={q_encoded})"
    )

    st.divider()
    st.subheader("2. 핵심 주장 추출 및 공식 자료 대조")
    st.caption(
        "본문에서 추출된 문장을 선택하거나 직접 주장을 입력한 뒤, 공공기관·연구기관·대학 등의 기준 자료를 불러오거나 붙여넣어 대조할 수 있습니다."
    )

    if "claim_results" not in st.session_state:
        st.session_state["claim_results"] = []

    extracted_sentences = extract_sentences(info.get("text", ""))
    sentence_options = ["(직접 주장 입력)"] + extracted_sentences
    selected_sentence = st.selectbox(
        "본문에서 추출된 주요 문장 선택",
        options=sentence_options,
        key="selected_claim_sentence",
    )

    default_claim = "" if selected_sentence == "(직접 주장 입력)" else selected_sentence
    claim_input = st.text_area(
        "검증할 핵심 주장",
        value=default_claim,
        height=90,
        placeholder="웹페이지나 문서에서 사실 여부를 확인하려는 핵심 주장을 입력하거나 위 목록에서 선택하세요.",
        key="claim_text_area",
    )

    if claim_input.strip():
        search_links = build_official_search_links(claim_input.strip())
        with st.expander("공공기관·연구기관·대학 자료 검색 바로가기", expanded=True):
            for sl in search_links:
                st.markdown(f"- **{sl['기관 유형']}**: [{sl['검색어']}]({sl['url']})")

    ref_mode = st.radio(
        "비교할 공식·학술 자료 입력 방식",
        ["공식 자료 본문 직접 붙여넣기", "공식 기관 웹페이지 URL에서 가져오기"],
        horizontal=True,
        key="ref_input_mode",
    )

    ref_source_name = st.text_input(
        "참고 기관·자료명 (예: 통계청 보도자료, 한국개발연구원 보고서, 서울대학교 연구논문)",
        key="ref_source_name",
    )
    ref_url_input = ""
    ref_text_input = ""

    if ref_mode == "공식 자료 본문 직접 붙여넣기":
        ref_text_input = st.text_area(
            "공공기관·연구기관·대학 자료의 비교 대상 본문",
            height=130,
            placeholder="공식 자료의 해당 문단이나 통계 수치를 붙여넣어 주세요. 비워둔 채 비교하면 '근거 부족'으로 분류됩니다.",
            key="ref_text_area",
        )
    else:
        ref_url_input = st.text_input(
            "비교할 공공기관·연구기관·대학 웹페이지 URL",
            placeholder="https://www.kostat.go.kr/...",
            key="ref_url_input",
        )

    if st.button("핵심 주장과 공식 자료 대조하기", type="primary", key="compare_claim_btn"):
        try:
            fetched_ref_text = ref_text_input
            resolved_ref_url = ""
            if ref_mode == "공식 기관 웹페이지 URL에서 가져오기":
                if not ref_url_input.strip():
                    raise ValueError("비교할 공식 자료 URL을 입력해 주세요.")
                valid_ref_url = validate_url(ref_url_input)
                with st.spinner("비교 기준 공식 자료를 불러오는 중입니다..."):
                    r_data, r_ctype, resolved_ref_url, _ = download(valid_ref_url)
                    r_kind = detect(r_data, r_ctype, resolved_ref_url)
                    r_info = (
                        parse_html(r_data, resolved_ref_url)
                        if r_kind == "html"
                        else parse_pdf(r_data)
                        if r_kind == "pdf"
                        else parse_image(r_data)
                    )
                    fetched_ref_text = r_info.get("text", "")
                    if not ref_source_name.strip():
                        ref_source_name = r_info.get("title") or resolved_ref_url

            comp = compare_claim_with_reference(
                claim_input,
                fetched_ref_text,
                ref_source_name=ref_source_name.strip(),
                ref_source_url=resolved_ref_url,
            )
            comp["claim"] = " ".join(claim_input.split()).strip()
            st.session_state["claim_results"].append(comp)
        except Exception as exc:
            st.error(f"주장 비교 중 오류가 발생했습니다: {exc}")

    if st.session_state["claim_results"]:
        st.markdown("#### 누적 주장 비교 결과")
        for idx, cr in enumerate(st.session_state["claim_results"], 1):
            with st.container(border=True):
                st.markdown(f"**주장 {idx}:** {cr['claim']}")
                st.caption(
                    f"비교 기준 자료: {cr['ref_source_name']}"
                    + (f" · {cr['ref_source_url']}" if cr.get("ref_source_url") else "")
                )
                render_status_badge(cr["status"])
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("**확인된 사실**")
                    for f_item in cr["confirmed_facts"]:
                        st.markdown(f"- {f_item}")
                with c2:
                    st.markdown("**추가 검증이 필요한 사항**")
                    for v_item in cr["needs_verification"]:
                        st.markdown(f"- {v_item}")

        if st.button("주장 비교 기록 초기화", key="clear_claims_btn"):
            st.session_state["claim_results"] = []
            st.rerun()

    st.divider()
    st.subheader("3. 항목별 출처 단서 분석")
    for c in checks:
        if c["points"] is None:
            point_label = "평가 대상 제외"
        else:
            point_label = f"획득 점수: {c['points']}/{c['max_points']}점"
        with st.container(border=True):
            st.markdown(f"**{c['name']}** — {c['result']} ({point_label})")
            st.markdown(f"- **확인된 사실:** {c['fact']}")
            st.markdown(f"- **추가 검증 필요:** {c['verify']}")

    if org_links:
        st.subheader("4. 문서 내 공공·연구·대학 도메인 링크")
        for item in org_links:
            st.markdown(f"- [{item['label']}]({item['url']})")

    if info["metadata"]:
        with st.expander("세부 파일·웹 메타정보 보기", expanded=False):
            st.json(info["metadata"])

    if info["text"]:
        with st.expander("추출된 본문 또는 OCR 텍스트 보기", expanded=False):
            st.text(info["text"][:20000])

    if info["metadata"].get("OCR 안내"):
        st.warning(info["metadata"]["OCR 안내"])

    md_report = report(
        source,
        info,
        author_profile,
        checks,
        score,
        summary_statement,
        st.session_state.get("claim_results", []),
    )
    st.download_button(
        "분석 보고서 다운로드 (.md)",
        md_report.encode("utf-8"),
        file_name="자료_출처_주장_검증_보고서.md",
        mime="text/markdown",
        use_container_width=True,
    )
    st.info(
        "본 도구는 메타정보와 텍스트 대조를 통해 확인 가능한 객관적 단서를 정리합니다. "
        "최종 사실 여부는 반드시 해당 공식 기관의 원문 자료와 대조하여 확인하세요."
    )


st.set_page_config(page_title="자료 출처·주장 검증 분석기 V4.2.3", page_icon="🔎", layout="wide")
st.title("🔎 웹페이지·PDF·이미지 출처 및 주장 검증 분석기 V4.2.3")
st.write(
    "웹 주소 또는 PDF·이미지 파일에서 작성자 소속·직책 단서와 출처 정보를 추출하고, "
    "핵심 주장을 공공기관·연구기관·대학 자료와 대조해 분류합니다."
)

with st.expander("지원 형식 및 분석 기준 안내", expanded=False):
    st.markdown(
        f"""
        - **지원 형식**: 웹 URL(HTML, PDF, 이미지) 및 로컬 업로드(PDF, PNG, JPG, GIF, BMP, WEBP, TIFF)
        - **크기·페이지 제한**: 웹페이지(HTML) 최대 {MAX_HTML_BYTES // (1024 * 1024)}MB, 파일 최대 {MAX_BYTES // (1024 * 1024)}MB, PDF 최대 {MAX_PDF_PAGES}페이지 (스캔 의심 페이지 최대 {MAX_OCR_PAGES}페이지 150 DPI OCR)
        - **점수 산출 방식**: 항목별 실제 획득 점수를 평가 가능 항목 총배점으로 나누어 100점 만점으로 환산합니다.
        - **작성자 검증**: 메타데이터 및 JSON-LD 구조화 데이터에서 이름·직책·소속 기관 단서를 추출하고, 공식 기관 대조 검색 링크를 제공합니다.
        - **주장 비교 분류 기준**:
          - **일치**: 핵심 키워드와 수치 표현이 비교 자료에서 일관되게 확인됨
          - **부분 일치**: 일부 키워드·맥락은 확인되나 세부 수치나 조건이 일부 누락·차이남
          - **불일치**: 동일 주제를 다루나 핵심 수치 또는 긍정·부정 서술 방향이 상충함
          - **근거 부족**: 비교 자료가 제시되지 않았거나 주장을 뒷받침할 공통 근거가 확인되지 않음
        """
    )

tab_url, tab_upload = st.tabs(["웹 주소 분석", "로컬 파일 업로드"])

with tab_url:
    url_input = st.text_input(
        "분석할 웹페이지 또는 파일 URL",
        placeholder="https://example.com/article",
        key="url_input",
    )
    if st.button("웹 주소 분석하기", type="primary", use_container_width=True):
        try:
            url = validate_url(url_input)
            with st.spinner("자료를 불러와 출처와 작성자 단서를 분석하고 있습니다..."):
                data, ctype, final_url, status = download(url)
                info, author_profile, checks, score, summary_statement, org_links, _ = analyze_bytes(
                    data, ctype, final_url, is_local=False
                )
            st.session_state["analysis_payload"] = {
                "source": final_url,
                "info": info,
                "author_profile": author_profile,
                "checks": checks,
                "score": score,
                "summary_statement": summary_statement,
                "org_links": org_links,
                "status": status,
            }
            st.session_state["claim_results"] = []
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
        accept_multiple_files=False,
    )
    if uploaded is not None:
        st.caption(f"선택한 파일: {uploaded.name} · {uploaded.size / 1024:.1f} KB")
        if uploaded.size > MAX_BYTES:
            st.error("파일은 최대 12MB까지 분석할 수 있습니다.")
        elif st.button("업로드한 파일 분석하기", type="primary", use_container_width=True):
            try:
                with st.spinner("파일을 분석하고 있습니다. 스캔 PDF는 OCR 처리에 시간이 걸릴 수 있습니다..."):
                    data = uploaded.getvalue()
                    info, author_profile, checks, score, summary_statement, org_links, _ = analyze_bytes(
                        data, "", uploaded.name, is_local=True
                    )
                st.session_state["analysis_payload"] = {
                    "source": f"로컬 파일: {uploaded.name}",
                    "info": info,
                    "author_profile": author_profile,
                    "checks": checks,
                    "score": score,
                    "summary_statement": summary_statement,
                    "org_links": org_links,
                    "status": None,
                }
                st.session_state["claim_results"] = []
            except Exception as e:
                st.error(f"파일 분석 중 문제가 발생했습니다: {e}")

if "analysis_payload" in st.session_state:
    payload = st.session_state["analysis_payload"]
    show_result(
        payload["source"],
        payload["info"],
        payload["author_profile"],
        payload["checks"],
        payload["score"],
        payload["summary_statement"],
        payload["org_links"],
        payload["status"],
    )

st.caption("HTML · PDF · 이미지 출처 단서 및 핵심 주장 대조 분석기 | HTML 최대 35MB · 파일 최대 12MB")
