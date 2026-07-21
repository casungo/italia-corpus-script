from italia_corpus.supplemental import _BodyParser, _akn_document
from italia_corpus.akn import akn_xml_to_markdown
import xml.etree.ElementTree as ET


def test_normattiva_body_and_supplemental_akn() -> None:
    parser = _BodyParser()
    parser.feed('<nav>noise</nav><div class="bodyTesto"><h2>Titolo</h2><p>Testo <b>vigente</b>.</p></div>')
    source = {
        "code": "001G0429", "date": "2001-10-20", "number": "380",
        "type": "DPR", "title": "Edilizia",
        "urn": "urn:nir:stato:decreto.presidente.repubblica:2001-06-06;380",
    }
    root = _akn_document(source, [("1", parser.text())])
    _, markdown, stats = akn_xml_to_markdown(
        ET.tostring(root, encoding="unicode"), {source["urn"]: "atti/001G0429.md"},
        "atti/001G0429.md", "O",
    )
    assert "Titolo" in markdown and "Testo vigente" in markdown
    assert stats.articles == 1
