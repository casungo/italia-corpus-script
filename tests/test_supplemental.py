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
    # i target esterni piu' ricorrenti fuori collezione (censimento sui render del corpus)
    for code, urn in {
        "088G0458": "urn:nir:stato:legge:1988-08-23;400",
        "090G0294": "urn:nir:stato:legge:1990-08-07;241",
        "031U0889": "urn:nir:stato:legge:1931-06-15;889",
        "035U1071": "urn:nir:stato:regio.decreto.legge:1935-06-20;1071",
        "062U1643": "urn:nir:stato:legge:1962-12-06;1643",
        "081U0689": "urn:nir:stato:legge:1981-11-24;689",
        "009G0201": "urn:nir:stato:legge:2009-12-31;196",
    }.items():
        assert by_code[code]["urn"] == urn
    # la Costituzione NON e' copribile: il suo codice Normattiva (047U0001) collide con la
    # legge cost. 1/1947 gia' in collezione, e la collisione sposterebbe entrambi gli atti
    # su path suffissati (gate: previous document disappeared). Reste esterna per design.
    assert "047U0001" not in by_code
