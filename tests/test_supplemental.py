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


def test_recurring_external_targets_are_supplemental_sources() -> None:
    from italia_corpus.supplemental import SOURCES

    by_code = {source["code"]: source for source in SOURCES}
    assert by_code["088G0458"]["urn"] == "urn:nir:stato:legge:1988-08-23;400"
    assert by_code["090G0294"]["urn"] == "urn:nir:stato:legge:1990-08-07;241"
    # la Costituzione NON è copribile: il suo codice Normattiva (047U0001) collide con la
    # legge cost. 1/1947 già in collezione, e la collisione sposterebbe entrambi gli atti
    # su path suffissati (gate: previous document disappeared). Reste esterna per design.
    assert "047U0001" not in by_code
