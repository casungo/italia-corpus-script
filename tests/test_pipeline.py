from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import sqlite3
import subprocess
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
from italia_corpus import pipeline
from italia_corpus.snapshot import (
    QualityGateError, SCHEMA_VERSION, build_legacy_archive, build_sqlite, safe_extract_zip,
    validate_report,
    validate_required_coverage, write_indexes,
)
from italia_corpus.git_ops import push_snapshot, stage_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


def test_codes_include_substantive_attached_articles() -> None:
    index = {
        "urn:nir:stato:regio.decreto:1930-10-19;1398": "atti/030U1398.md",
        "urn:nir:stato:regio.decreto:1942-03-16;262": "atti/042U0262.md",
    }
    _, penal, stats = akn_xml_to_markdown((FIXTURES / "codice_penale.xml").read_text(), index, "atti/030U1398.md")
    _, civil, _ = akn_xml_to_markdown((FIXTURES / "codice_civile.xml").read_text(), index, "atti/042U0262.md")
    assert '<a id="art-575" data-akn-name="article"></a>' in penal and "Chiunque cagiona la morte" in penal
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


def test_canonical_collection_priority_precedes_article_count() -> None:
    report = ConversionReport()
    primary = discover_candidate(
        "Codici", "V", "primary.xml", (FIXTURES / "codice_civile.xml").read_bytes(), report
    )
    assert primary is not None
    larger = replace(primary, collection="Altro", source_articles=999, source="larger.xml")
    assert select_canonical([larger, primary], report) == [primary]


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


def test_temporal_attachment_abrogation_and_deep_lists_golden() -> None:
    _, markdown, stats = akn_xml_to_markdown(
        (FIXTURES / "temporal.xml").read_text(),
        {"urn:nir:stato:legge:2020-01-01;9": "atti/020G0009.md"},
        "atti/020G0009.md", "M",
    )
    assert '<a id="art-1" data-akn-name="article" data-valid-from="2020-01-01" data-valid-to="2022-01-01"></a>' in markdown
    assert '<a id="art-1-v2" data-akn-name="article" data-valid-from="2022-01-01"></a>' in markdown
    assert "Testo abrogato" in markdown and "Livello tre" in markdown
    assert "Contenuto allegato" in markdown
    assert "schema_version: 3" in markdown
    assert stats.article_intervals[:2] == [
        {"anchor": "art-1", "valid_from": "2020-01-01", "valid_to": "2022-01-01"},
        {"anchor": "art-1-v2", "valid_from": "2022-01-01", "valid_to": None},
    ]


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
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == len(candidates)
        assert db.execute("SELECT count(*) FROM articles").fetchone()[0] == report.articles
    assert manifest["counts"]["acts"] == len(candidates)
    index = json.loads((tmp_path / "urn-index.json").read_text())
    assert index["schema_version"] == SCHEMA_VERSION
    assert index["by_codice_redazionale"]["030U1398"]["urn"].endswith(";1398")
    assert manifest["by_collection"]["codes"]["converted"] == len(candidates)
    assert cli_main(["get", "--urn", "urn:nir:stato:regio.decreto:1930-10-19;1398", "--database", str(database)]) == 0
    assert cli_main(["search", "omicidio", "--database", str(database)]) == 0
    assert cli_main([
        "get", "--urn", "urn:nir:stato:legge:2020-01-01;9", "--article", "art-1",
        "--vigente-al", "2021-01-01", "--database", str(database),
    ]) == 0


def test_multi_collection_snapshot_integration(tmp_path: Path) -> None:
    report = ConversionReport()
    civil = discover_candidate(
        "Codici", "V", "civil.xml", (FIXTURES / "codice_civile.xml").read_bytes(), report
    )
    penal = discover_candidate(
        "Leggi", "V", "penal.xml", (FIXTURES / "codice_penale.xml").read_bytes(), report
    )
    assert civil is not None and penal is not None
    candidates = select_canonical([civil, penal], report)
    render_candidates(candidates, tmp_path, report)
    manifest = write_indexes(tmp_path, candidates, report, 2, 2)
    build_sqlite(tmp_path, tmp_path / "corpus.sqlite")
    assert manifest["collections"] == {"requested": 2, "downloaded": 2}
    assert set(manifest["by_collection"]) == {"Codici", "Leggi"}
    assert manifest["counts"]["acts"] == 2
    with sqlite3.connect(tmp_path / "corpus.sqlite") as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 2


def test_legacy_archive_precedes_atomic_staging(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repository, check=True)
    legacy = repository / "Codici"
    legacy.mkdir()
    (legacy / "old.md").write_text("legacy\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "legacy"], cwd=repository, check=True, capture_output=True)

    snapshot = tmp_path / "snapshot"
    (snapshot / "atti").mkdir(parents=True)
    (snapshot / "atti" / "new.md").write_text("new\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "SHA256SUMS").write_text("")
    archive = build_legacy_archive(repository, ["Codici"], artifacts)
    assert archive is not None and archive.stat().st_size > 0
    assert archive.name in (artifacts / "SHA256SUMS").read_text()

    staged = stage_snapshot(snapshot, repository, ["Codici"])
    assert staged is not None
    tag, _ = staged
    push_snapshot(repository, "main", tag)
    assert not legacy.exists()
    assert (repository / "atti" / "new.md").is_file()
    refs = subprocess.run(
        ["git", "show-ref"], cwd=remote, check=True, capture_output=True, text=True
    ).stdout
    assert "refs/heads/main" in refs and f"refs/tags/{tag}" in refs


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
        {"dry_run": True, "baseline": None, "smoke_test": False, "download_cache": None},
    )]


def test_smoke_test_cli_requires_dry_run(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["italia-corpus-pipeline", "--smoke-test", "/tmp"])
    with pytest.raises(SystemExit, match="2"):
        pipeline_cli.main()


def test_download_is_fail_closed_on_empty_archives(tmp_path: Path, monkeypatch) -> None:
    class EmptyResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, size: int):
            return iter(())

    monkeypatch.setattr(pipeline, "DOWNLOAD_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(pipeline.requests, "get", lambda *args, **kwargs: EmptyResponse())
    with pytest.raises(RuntimeError, match="Collezione vuota.*empty download"):
        pipeline._download({"nome": "Collezione vuota"}, tmp_path / "empty.zip")


def test_download_reuses_only_a_valid_zip_cache(tmp_path: Path, monkeypatch, caplog) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("act.xml", "<xml/>")
    archive_bytes = payload.getvalue()

    class ZipResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, size: int):
            return iter((archive_bytes,))

    cache = tmp_path / "cache.zip"
    caplog.set_level(logging.INFO, logger="italia_corpus")
    monkeypatch.setattr(pipeline.requests, "get", lambda *args, **kwargs: ZipResponse())
    destination = tmp_path / "download.zip"
    assert pipeline._download(
        {"nome": "Cached", "formatoRichiesta": "V"}, destination, cache
    ) is False
    assert cache.with_suffix(".zip.sha256").is_file()
    inventory = json.loads((tmp_path / "inventory.json").read_text())
    assert inventory["archives"]["cache.zip"]["members"] == 1

    monkeypatch.setattr(
        pipeline.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("valid cache should avoid the network"),
    )
    destination.unlink()
    assert pipeline._download(
        {"nome": "Cached", "formatoRichiesta": "V"}, destination, cache
    ) is True
    assert destination.read_bytes() == cache.read_bytes()
    assert "format=V cache_hit=false" in caplog.text
    assert "format=V cache_hit=true" in caplog.text


def test_download_discards_cache_when_checksum_inventory_mismatches(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache.zip"
    with zipfile.ZipFile(cache, "w") as archive:
        archive.writestr("act.xml", "<xml/>")
    cache.with_suffix(".zip.sha256").write_text("0" * 64 + "  cache.zip\n")
    (tmp_path / "inventory.json").write_text(json.dumps({
        "schema_version": 1,
        "archives": {"cache.zip": {"sha256": "0" * 64, "size": cache.stat().st_size,
                                           "members": 1}},
    }))
    monkeypatch.setattr(pipeline, "DOWNLOAD_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(
        pipeline.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(pipeline.requests.RequestException("offline")),
    )
    with pytest.raises(RuntimeError, match="offline"):
        pipeline._download({"nome": "Cached", "formatoRichiesta": "V"}, tmp_path / "out.zip", cache)
    assert not cache.exists()


def test_download_verifies_every_cached_zip_member(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache.zip"
    with zipfile.ZipFile(cache, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("act.xml", "unique member payload")
    corrupted = cache.read_bytes().replace(b"unique member payload", b"broken member payload")
    cache.write_bytes(corrupted)
    digest = hashlib.sha256(corrupted).hexdigest()
    cache.with_suffix(".zip.sha256").write_text(f"{digest}  cache.zip\n")
    (tmp_path / "inventory.json").write_text(json.dumps({
        "schema_version": 1,
        "archives": {"cache.zip": {"sha256": digest, "size": cache.stat().st_size,
                                           "members": 1}},
    }))
    monkeypatch.setattr(pipeline, "DOWNLOAD_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(
        pipeline.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(pipeline.requests.RequestException("offline")),
    )
    with pytest.raises(RuntimeError, match="offline"):
        pipeline._download({"nome": "Cached", "formatoRichiesta": "V"}, tmp_path / "out.zip", cache)
    assert not cache.exists()


def test_collection_download_falls_back_to_an_advertised_format(
    tmp_path: Path, monkeypatch
) -> None:
    attempted = []

    def fake_download(params, destination, cache):
        attempted.append(params["formatoRichiesta"])
        if params["formatoRichiesta"] != "M":
            raise RuntimeError("empty")
        return False

    monkeypatch.setattr(pipeline, "_download", fake_download)
    params, cache_hit = pipeline._download_collection(
        {
            "nomeCollezione": "Intermittente",
            "formatoCollezione": "V",
            "formatiDisponibili": ["M", "O", "V"],
            "dataCreazione": "2026-07-21",
        },
        tmp_path / "archive.zip",
        tmp_path / "cache",
    )
    assert attempted == ["V", "O", "M"]
    assert params["formatoRichiesta"] == "M"
    assert cache_hit is False
