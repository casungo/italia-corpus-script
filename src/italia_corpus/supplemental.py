"""Fail-closed imports for important acts missing from predefined collections."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

import requests

from .akn import AKN_NS, ELI_NS

NORMATTIVA = "https://www.normattiva.it"
GAZZETTA_PDF = "https://www.gazzettaufficiale.it/eli/gu/2018/02/20/42/so/8/sg/pdf"

SOURCES = (
    {"code": "030U1398", "date": "1930-10-19", "publication_date": "1930-10-26", "number": "1398", "min_articles": "100", "type": "REGIO DECRETO", "title": "Codice penale", "urn": "urn:nir:stato:regio.decreto:1930-10-19;1398"},
    {"code": "042U0262", "date": "1942-03-16", "publication_date": "1942-04-04", "number": "262", "min_articles": "100", "type": "REGIO DECRETO", "title": "Codice civile", "urn": "urn:nir:stato:regio.decreto:1942-03-16;262"},
    {"code": "001G0429", "date": "2001-06-06", "publication_date": "2001-10-20", "number": "380", "min_articles": "1", "type": "DECRETO DEL PRESIDENTE DELLA REPUBBLICA", "title": "Testo unico delle disposizioni legislative e regolamentari in materia edilizia", "urn": "urn:nir:stato:decreto.presidente.repubblica:2001-06-06;380"},
    {"code": "011G0193", "date": "2011-08-01", "publication_date": "2011-09-22", "number": "151", "min_articles": "1", "type": "DECRETO DEL PRESIDENTE DELLA REPUBBLICA", "title": "Regolamento recante semplificazione della disciplina dei procedimenti relativi alla prevenzione degli incendi", "urn": "urn:nir:stato:decreto.presidente.repubblica:2011-08-01;151"},
    {"code": "006G0171", "date": "2006-04-03", "publication_date": "2006-04-14", "number": "152", "min_articles": "1", "type": "DECRETO LEGISLATIVO", "title": "Norme in materia ambientale", "urn": "urn:nir:stato:decreto.legislativo:2006-04-03;152"},
)


class _BodyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "div" and "bodyTesto" in (values.get("class") or "").split():
            self.depth = 1
        elif self.depth:
            self.depth += tag == "div"
            if tag in {"p", "br", "li", "h1", "h2", "h3"}:
                self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.depth and tag == "div":
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(
            line.strip() for line in "".join(self.parts).splitlines() if line.strip()
        )


def _akn_document(source: dict[str, str], articles: list[tuple[str, str]]) -> ET.Element:
    ET.register_namespace("", AKN_NS)
    ET.register_namespace("eli", ELI_NS)
    root = ET.Element(f"{{{AKN_NS}}}akomaNtoso")
    act = ET.SubElement(root, f"{{{AKN_NS}}}act")
    meta = ET.SubElement(act, f"{{{AKN_NS}}}meta")
    identification = ET.SubElement(meta, f"{{{AKN_NS}}}identification")
    work = ET.SubElement(identification, f"{{{AKN_NS}}}FRBRWork")
    ET.SubElement(work, f"{{{AKN_NS}}}FRBRalias", {"name": "urn:nir", "value": source["urn"]})
    proprietary = ET.SubElement(meta, f"{{{AKN_NS}}}proprietary")
    ET.SubElement(proprietary, f"{{{ELI_NS}}}id_local").text = source["code"]
    preface = ET.SubElement(act, f"{{{AKN_NS}}}preface")
    ET.SubElement(preface, f"{{{AKN_NS}}}docType").text = source["type"]
    ET.SubElement(preface, f"{{{AKN_NS}}}docNumber").text = source["number"]
    ET.SubElement(preface, f"{{{AKN_NS}}}docDate", {"date": source["date"]})
    ET.SubElement(preface, f"{{{AKN_NS}}}docTitle").text = source["title"]
    body = ET.SubElement(act, f"{{{AKN_NS}}}body")
    occurrences: dict[str, int] = {}
    for number, text in articles:
        occurrences[number] = occurrences.get(number, 0) + 1
        suffix = f"_{occurrences[number]}" if occurrences[number] > 1 else ""
        article = ET.SubElement(
            body, f"{{{AKN_NS}}}article", {"eId": f"art_{number}{suffix}"}
        )
        ET.SubElement(article, f"{{{AKN_NS}}}num").text = f"Art. {number}"
        paragraph = ET.SubElement(article, f"{{{AKN_NS}}}paragraph")
        content = ET.SubElement(paragraph, f"{{{AKN_NS}}}content")
        for value in text.splitlines():
            ET.SubElement(content, f"{{{AKN_NS}}}p").text = value
    return root


def _fetch_normattiva(source: dict[str, str]) -> ET.Element:
    session = requests.Session()
    detail = session.get(
        f"{NORMATTIVA}/atto/caricaDettaglioAtto",
        params={
            "atto.dataPubblicazioneGazzetta": source["publication_date"],
            "atto.codiceRedazionale": source["code"],
        },
        timeout=(30, 120),
    )
    detail.raise_for_status()
    matches = re.findall(
        r"showArticle\('([^']+)'[^>]*class=\"numero_articolo\"[^>]*>([^<]+)", detail.text
    )
    unique = list(dict.fromkeys((html.unescape(path), number.strip()) for path, number in matches))
    if not unique:
        raise RuntimeError(f"no current articles found for {source['code']}")

    cookies = session.cookies.get_dict()

    def fetch(item: tuple[str, str]) -> tuple[str, str]:
        path, number = item
        response = requests.get(
            NORMATTIVA + path, headers={"X-Requested-With": "XMLHttpRequest"},
            cookies=cookies, timeout=(30, 120)
        )
        response.raise_for_status()
        parser = _BodyParser()
        parser.feed(response.text)
        text = parser.text()
        if not text:
            raise RuntimeError(f"empty article {number} for {source['code']}")
        return number, text

    with ThreadPoolExecutor(max_workers=6) as executor:
        articles = list(executor.map(fetch, unique))
    return _akn_document(source, articles)


def _fetch_ntc() -> ET.Element:
    from pypdf import PdfReader

    response = requests.get(GAZZETTA_PDF, timeout=(30, 300))
    response.raise_for_status()
    from io import BytesIO
    pages = [page.extract_text() or "" for page in PdfReader(BytesIO(response.content)).pages]
    text = "\n".join(pages)
    marker = "NORME TECNICHE PER LE COSTRUZIONI"
    if marker not in text or len(text) < 100_000:
        raise RuntimeError("Gazzetta NTC PDF is missing or not text-extractable")
    source = {
        "code": "18A00716", "date": "2018-01-17", "number": "NTC 2018",
        "type": "DECRETO MINISTERIALE", "title": "Aggiornamento delle Norme tecniche per le costruzioni",
        "urn": "urn:nir:ministero.infrastrutture.trasporti:decreto:2018-01-17;ntc",
    }
    return _akn_document(source, [("allegato", text[text.index(marker):])])


def fetch_missing_sources(
    article_counts: dict[str, int], destination: Path
) -> list[tuple[str, str, Path]]:
    missing = [
        source for source in SOURCES
        if article_counts.get(source["code"], 0) < int(source["min_articles"])
    ]
    if article_counts.get("18A00716", 0) < 1:
        missing.append({"code": "18A00716"})
    if not missing:
        return []
    vigente = destination / "vigente"
    originale = destination / "originale"
    vigente.mkdir(parents=True, exist_ok=True)
    originale.mkdir(parents=True, exist_ok=True)
    for source in missing:
        code = source["code"]
        root = _fetch_ntc() if code == "18A00716" else _fetch_normattiva(source)
        target = originale if code == "18A00716" else vigente
        ET.ElementTree(root).write(target / f"{code}.xml", encoding="utf-8", xml_declaration=True)
    output = []
    if any(vigente.iterdir()):
        output.append(("Fonti supplementari Normattiva", "V", vigente))
    if any(originale.iterdir()):
        output.append(("Fonti supplementari Gazzetta", "O", originale))
    return output
