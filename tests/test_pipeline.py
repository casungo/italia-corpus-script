from __future__ import annotations

import base64
import io
import json
import sqlite3
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from italia_corpus.akn import AKN_NS, akn_xml_to_markdown
from italia_corpus.converter import (
    ConversionReport, discover_candidate, discover_candidates, render_candidates, select_canonical,
)
from italia_corpus.cli import main as cli_main
from italia_corpus import __main__ as pipeline_cli
from italia_corpus.snapshot import (
    QualityGateError, build_sqlite, safe_extract_zip, validate_report,
    validate_required_coverage, write_indexes,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_codes_include_substantive_attached_articles() -> None:
    index = {
        "urn:nir:stato:regio.decreto:1930-10-19;1398": "atti/030U1398.md",
        "urn:nir:stato:regio.decreto:1942-03-16;262": "atti/042U0262.md",
    }
    _, penal, stats = akn_xml_to_markdown((FIXTURES / "codice_penale.xml").read_text(), index, "atti/030U1398.md")
    _, civil, _ = akn_xml_to_markdown((FIXTURES / "codice_civile.xml").read_text(), index, "atti/042U0262.md")
    assert '<a id="art-575"></a>' in penal and "Chiunque cagiona la morte" in penal
    assert "Capacità giuridica" in civil and stats.articles == 2


def test_two_pass_output_is_order_independent(tmp_path: Path) -> None:
    roots = [("zeta", "O", FIXTURES), ("alfa", "V", FIXTURES)]
    outputs = []
    for number, collections in enumerate((roots, list(reversed(roots)))):
        report = ConversionReport()
        chosen = select_canonical(discover_candidates(collections, report), report)
        output = tmp_path / str(number)
        render_candidates(chosen, output, report)
        outputs.append({p.relative_to(output): p.read_bytes() for p in output.rglob("*.md")})
    assert outputs[0] == outputs[1]


def test_complex_structure_table_list_and_stable_fragment() -> None:
    index = {
        "urn:nir:stato:legge:2020-01-01;1": "atti/020G0001.md",
        "urn:nir:stato:regio.decreto:1930-10-19;1398": "atti/030U1398.md",
    }
    _, markdown, stats = akn_xml_to_markdown((FIXTURES / "complex.xml").read_text(), index, "atti/020G0001.md")
    assert "030U1398.md#art-575" in markdown
    assert "| Voce | Valore |" in markdown
    assert "Primo elemento" in markdown
    assert stats.internal_links == 1


def test_safe_extract_rejects_zip_slip_and_symlink(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../escape", "bad")
    with zipfile.ZipFile(stream) as archive, pytest.raises(QualityGateError):
        safe_extract_zip(archive, tmp_path)
    assert not (tmp_path.parent / "escape").exists()


def test_one_xml_can_be_discovered_directly_from_zip_bytes() -> None:
    report = ConversionReport()
    candidate = discover_candidate(
        "Codici", "V", "nested/codice.xml", (FIXTURES / "codice_civile.xml").read_bytes(), report
    )
    assert candidate is not None
    assert candidate.metadata.codice_redazionale == "042U0262"
    assert report.xml_received == 1


def test_base64_payload_is_decoded_only_when_it_is_valid_akn() -> None:
    report = ConversionReport()
    payload = base64.b64encode(b"<html><body>not AKN</body></html>")
    assert discover_candidate("Decreti Legislativi", "V", "007X0246.xml", payload, report) is None
    assert report.skipped == 1
    assert report.errors[0].message == "base64-wrapped HTML, not AKN XML"

    report = ConversionReport()
    payload = base64.b64encode((FIXTURES / "codice_civile.xml").read_bytes())
    candidate = discover_candidate("Codici", "V", "042U0262.xml", payload, report)
    assert candidate is not None
    assert candidate.metadata.codice_redazionale == "042U0262"

    report = ConversionReport()
    payload = base64.b64encode((FIXTURES / "codice_civile_attuazione.xml").read_bytes())
    candidate = discover_candidate("Codici", "V", "042U0318.xml", payload, report)
    assert candidate is not None
    assert candidate.metadata.codice_redazionale == "042U0318"

    report = ConversionReport()
    payload = base64.b64encode(b"<root><p>arbitrary XML</p></root>")
    assert discover_candidate("Codici", "V", "arbitrary.xml", payload, report) is None
    assert report.errors[0].message == "XML root is not Akoma Ntoso"

    report = ConversionReport()
    payload = base64.b64encode(
        f'<akomaNtoso xmlns="{AKN_NS}"><act><body/></act></akomaNtoso>'.encode()
    )
    assert discover_candidate("Codici", "V", "missing-metadata.xml", payload, report) is None
    assert report.errors[0].message == "missing URN"

    report = ConversionReport()
    assert discover_candidate("Regi decreti", "V", "truncated.xml", b"<" * 1048576, report) is None
    assert report.errors[0].message == "source payload is exactly 1 MiB and appears truncated"


def test_code_collision_reports_both_sources() -> None:
    report = ConversionReport()
    first = discover_candidate(
        "A", "V", "first.xml", (FIXTURES / "codice_civile.xml").read_bytes(), report
    )
    assert first is not None
    second = replace(first, metadata=replace(first.metadata, urn="urn:nir:stato:legge:2000;1"),
                     source="second.xml")
    chosen = select_canonical([first, second], report)
    assert chosen == []
    assert "first.xml <> second.xml" in report.errors[-1].source


def test_quality_gate_is_fail_closed() -> None:
    report = ConversionReport(xml_received=2, converted=1, skipped=1, urns=1, editorial_codes=1)
    with pytest.raises(QualityGateError):
        validate_report(report)


def test_manifest_and_sqlite(tmp_path: Path) -> None:
    report = ConversionReport()
    candidates = select_canonical(discover_candidates([("codes", "V", FIXTURES)], report), report)
    render_candidates(candidates, tmp_path, report)
    manifest = write_indexes(tmp_path, candidates, report, 1, 1)
    validate_report(report)
    database = tmp_path / "corpus.sqlite"
    build_sqlite(tmp_path, database)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 4
    assert manifest["counts"]["acts"] == 4
    assert json.loads((tmp_path / "urn-index.json").read_text())["schema_version"] == 2
    assert cli_main(["get", "--urn", "urn:nir:stato:regio.decreto:1930-10-19;1398", "--database", str(database)]) == 0
    assert cli_main(["search", "omicidio", "--database", str(database)]) == 0


def test_issue_coverage_gate_distinguishes_missing_source(tmp_path: Path) -> None:
    requirements = tmp_path / "coverage.json"
    requirements.write_text(json.dumps({
        "required": [{"codice_redazionale": "030U1398", "min_articles": 100}],
        "known_gaps": [{"description": "D.M. 17 gennaio 2018 absent upstream"}],
    }))
    report = ConversionReport(document_articles={"atti/030U1398.md": 2})
    with pytest.raises(QualityGateError, match="expected at least 100"):
        validate_required_coverage(report, requirements)


def test_dry_run_cli_does_not_initialize_github(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(sys, "argv", ["italia-corpus-pipeline", "--dry-run", str(tmp_path)])
    monkeypatch.setattr(pipeline_cli, "github_client", lambda: pytest.fail("GitHub initialized"))
    monkeypatch.setattr(
        pipeline_cli,
        "extract_and_push",
        lambda root, gh, **options: calls.append((root, gh, options)) or tmp_path,
    )
    pipeline_cli.main()
    assert calls == [(
        str(tmp_path), None,
        {"dry_run": True, "baseline": None, "smoke_test": False},
    )]


def test_smoke_test_cli_requires_dry_run(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["italia-corpus-pipeline", "--smoke-test", "/tmp"])
    with pytest.raises(SystemExit, match="2"):
        pipeline_cli.main()
