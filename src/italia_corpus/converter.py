"""Two-pass AKN discovery, canonical selection and rendering."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from zipfile import ZipFile

from .akn import (
    AKN_NS, AknFrontmatter, akn_xml_to_markdown, count_akn_articles, extract_frontmatter,
    parse_akn_xml,
)

_ILLEGAL_XML10 = re.compile(rb"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_FORMAT_RANK = {"V": 0, "M": 1, "O": 2}
_UPSTREAM_TRUNCATION_SIZE = 1024 * 1024
UPSTREAM_TRUNCATION_ERROR = "source payload is exactly 1 MiB and appears truncated"


@dataclass(frozen=True, slots=True)
class Candidate:
    xml_path: Path
    collection: str
    source_format: str
    metadata: AknFrontmatter
    content: str | None
    source_articles: int
    source: str = ""
    path_suffix: str = ""
    archive_name: str | None = None
    member_name: str | None = None

    @property
    def repo_path(self) -> str:
        return f"atti/{self.metadata.codice_redazionale}{self.path_suffix}.md"

    def rank(self) -> tuple[int, int, str, str]:
        priorities = [
            value.strip().casefold()
            for value in os.getenv("CANONICAL_COLLECTION_PRIORITY", "Codici").split(",")
            if value.strip()
        ]
        collection = self.collection.casefold()
        primary_rank = next(
            (index for index, name in enumerate(priorities) if name in collection),
            len(priorities),
        )
        return (
            _FORMAT_RANK.get(self.source_format, 9),
            primary_rank,
            self.collection.casefold(),
            (self.source or self.xml_path.as_posix()).casefold(),
        )


@dataclass
class ConversionError:
    source: str
    message: str
    collection: str = "*"
    metric: str = "conversion_error"


@dataclass
class ConversionReport:
    xml_received: int = 0
    converted: int = 0
    skipped: int = 0
    articles: int = 0
    internal_links: int = 0
    external_links: int = 0
    unresolved_links: int = 0
    urns: int = 0
    editorial_codes: int = 0
    duplicates: int = 0
    errors: list[ConversionError] = field(default_factory=list)
    unsupported_tags: set[str] = field(default_factory=set)
    hashes: dict[str, str] = field(default_factory=dict)
    document_articles: dict[str, int] = field(default_factory=dict)
    document_anchors: dict[str, list[str]] = field(default_factory=dict)
    article_intervals: dict[str, list[dict[str, str | None]]] = field(default_factory=dict)
    collections: dict[str, dict[str, int]] = field(default_factory=dict)

    def increment(self, collection: str, metric: str, amount: int = 1) -> None:
        counts = self.collections.setdefault(collection, {})
        counts[metric] = counts.get(metric, 0) + amount

    def to_dict(self) -> dict:
        value = asdict(self)
        value["unsupported_tags"] = sorted(self.unsupported_tags)
        return value


def _read_xml(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _source_xml_text(raw: bytes) -> str:
    content = raw.decode("utf-8", errors="replace")
    xml_bytes = raw
    if not content.lstrip().startswith("<"):
        try:
            decoded = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error) as exc:
            if len(raw) == _UPSTREAM_TRUNCATION_SIZE:
                raise ValueError(UPSTREAM_TRUNCATION_ERROR) from exc
            raise ValueError("payload is not XML") from exc
        prefix = decoded.lstrip().lower()
        if prefix.startswith((b"<html", b"<!doctype html")):
            raise ValueError("base64-wrapped HTML, not AKN XML")
        try:
            content = decoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("base64 payload is not UTF-8 XML") from exc
        xml_bytes = decoded
    if _ILLEGAL_XML10.search(xml_bytes):
        return _ILLEGAL_XML10.sub(b"", xml_bytes).decode("utf-8", errors="replace")
    return content


def discover_candidate(
    collection: str,
    source_format: str,
    source: str,
    raw: bytes,
    report: ConversionReport,
) -> Candidate | None:
    """Parse one XML candidate without materializing its collection on disk."""
    report.xml_received += 1
    report.increment(collection, "xml_received")
    try:
        content = _source_xml_text(raw)
        try:
            xml_root = parse_akn_xml(content)
        except ET.ParseError:
            if len(raw) == _UPSTREAM_TRUNCATION_SIZE:
                raise ValueError(UPSTREAM_TRUNCATION_ERROR) from None
            raise
        if xml_root.tag != f"{{{AKN_NS}}}akomaNtoso":
            raise ValueError("XML root is not Akoma Ntoso")
        metadata = extract_frontmatter(xml_root, source_format)
        if not metadata.urn:
            raise ValueError("missing URN")
        if not metadata.codice_redazionale:
            raise ValueError("missing codice_redazionale")
        article_count = count_akn_articles(xml_root)
        return Candidate(
            Path(source), collection, source_format, metadata, content, article_count, source
        )
    except Exception as exc:
        report.skipped += 1
        report.increment(collection, "skipped")
        report.errors.append(ConversionError(source, str(exc), collection, "invalid_xml"))
        return None


def discover_candidates(
    collection_dirs: list[tuple[str, str, Path]], report: ConversionReport
) -> list[Candidate]:
    """Parse metadata for every XML without rendering or mutating an index."""
    candidates: list[Candidate] = []
    for collection, source_format, root in sorted(collection_dirs):
        for path in sorted(root.rglob("*.xml"), key=lambda p: p.as_posix().casefold()):
            candidate = discover_candidate(
                collection, source_format, str(path), path.read_bytes(), report
            )
            if candidate:
                candidates.append(candidate)
    return candidates


def select_canonical(candidates: list[Candidate], report: ConversionReport) -> list[Candidate]:
    by_urn: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        urn = candidate.metadata.urn or ""
        by_urn.setdefault(urn, []).append(candidate)
    report.duplicates = sum(max(0, len(group) - 1) for group in by_urn.values())
    chosen = [min(group, key=Candidate.rank) for _, group in sorted(by_urn.items())]

    by_code: dict[str, list[Candidate]] = {}
    for candidate in chosen:
        by_code.setdefault(candidate.metadata.codice_redazionale or "", []).append(candidate)
    collisions = {code for code, group in by_code.items() if len(group) > 1}
    return [
        replace(
            candidate,
            path_suffix=f"-{hashlib.sha256((candidate.metadata.urn or '').encode()).hexdigest()[:12]}",
        )
        if (candidate.metadata.codice_redazionale or "") in collisions else candidate
        for candidate in chosen
    ]


@dataclass
class _RenderOutcome:
    repo_path: str
    collection: str
    source: str
    converted: bool = False
    articles: int = 0
    internal_links: int = 0
    external_links: int = 0
    unresolved_links: int = 0
    unsupported_tags: tuple[str, ...] = ()
    sha256: str = ""
    anchors: tuple[str, ...] = ()
    article_intervals: list[dict[str, str | None]] = field(default_factory=list)
    has_urn: bool = False
    has_editorial_code: bool = False
    error: ConversionError | None = None


def _render_to_outcome(
    candidate: Candidate, content: str, urn_index: dict[str, str], output_dir: str
) -> _RenderOutcome:
    outcome = _RenderOutcome(
        repo_path=candidate.repo_path,
        collection=candidate.collection,
        source=str(candidate.xml_path),
    )
    try:
        fm, markdown, stats = akn_xml_to_markdown(
            content, urn_index, candidate.repo_path, candidate.source_format
        )
        if not markdown.partition("---\n")[2].strip("-\n "):
            raise ValueError("empty rendered document")
        target = Path(output_dir) / candidate.repo_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8", newline="\n")
        outcome.converted = True
        outcome.articles = stats.articles
        outcome.internal_links = stats.internal_links
        outcome.external_links = stats.external_links
        outcome.unresolved_links = stats.unresolved_links
        outcome.unsupported_tags = tuple(stats.unsupported_tags or ())
        outcome.sha256 = hashlib.sha256(markdown.encode()).hexdigest()
        outcome.anchors = tuple(re.findall(r'<a id="([^"]+)"', markdown))
        outcome.article_intervals = stats.article_intervals
        outcome.has_urn = bool(fm.urn)
        outcome.has_editorial_code = bool(fm.codice_redazionale)
    except Exception as exc:
        outcome.error = ConversionError(
            str(candidate.xml_path), str(exc), candidate.collection, "render_error"
        )
    return outcome


def _apply_outcome(report: ConversionReport, outcome: _RenderOutcome) -> None:
    if outcome.error is not None:
        report.skipped += 1
        report.increment(outcome.collection, "skipped")
        report.errors.append(outcome.error)
        return
    report.converted += 1
    report.increment(outcome.collection, "converted")
    report.articles += outcome.articles
    report.increment(outcome.collection, "articles", outcome.articles)
    report.internal_links += outcome.internal_links
    report.external_links += outcome.external_links
    report.unresolved_links += outcome.unresolved_links
    report.unsupported_tags.update(outcome.unsupported_tags)
    report.hashes[outcome.repo_path] = outcome.sha256
    report.document_articles[outcome.repo_path] = outcome.articles
    report.document_anchors[outcome.repo_path] = list(outcome.anchors)
    report.article_intervals[outcome.repo_path] = outcome.article_intervals
    report.urns += outcome.has_urn
    report.editorial_codes += outcome.has_editorial_code


_RENDER_ARCHIVE: ZipFile | None = None
_RENDER_OUTPUT = ""
_RENDER_URN_INDEX: dict[str, str] = {}


def _open_render_archive(path: str, output: str, urn_index: dict[str, str]) -> None:
    global _RENDER_ARCHIVE, _RENDER_OUTPUT, _RENDER_URN_INDEX
    _RENDER_ARCHIVE = ZipFile(path)
    _RENDER_OUTPUT = output
    _RENDER_URN_INDEX = urn_index


def _render_archive_member(candidate: Candidate) -> _RenderOutcome:
    if _RENDER_ARCHIVE is None or candidate.member_name is None:
        raise RuntimeError("render archive was not initialized")
    content = _source_xml_text(_RENDER_ARCHIVE.read(candidate.member_name))
    return _render_to_outcome(candidate, content, _RENDER_URN_INDEX, _RENDER_OUTPUT)


RENDER_POOL_MIN_MEMBERS = 512


def _add_date_number_aliases(urn_index: dict[str, str]) -> None:
    """Register the invariant date;number tail of every act so alias URN forms resolve."""
    from .refs import URN_ALIAS_PREFIX

    tails: dict[str, set[str]] = {}
    for urn, path in urn_index.items():
        tail = urn.rsplit(":", 1)[-1]
        if ";" in tail:
            tails.setdefault(tail, set()).add(path)
    for tail, paths in tails.items():
        if len(paths) != 1:
            continue
        alias = f"{URN_ALIAS_PREFIX}{tail}"
        urn_index.setdefault(alias, next(iter(paths)))


def render_candidates(
    candidates: list[Candidate],
    output: Path,
    report: ConversionReport,
    archive_root: Path | None = None,
) -> dict[str, str]:
    urn_index = {candidate.metadata.urn or "": candidate.repo_path for candidate in candidates}
    _add_date_number_aliases(urn_index)
    output.mkdir(parents=True, exist_ok=True)

    def render(candidate: Candidate, content: str) -> None:
        _apply_outcome(report, _render_to_outcome(candidate, content, urn_index, str(output)))

    local_candidates = []
    archive_candidates: dict[str, list[Candidate]] = {}
    for candidate in sorted(candidates, key=lambda c: c.repo_path):
        if candidate.archive_name is None:
            local_candidates.append(candidate)
        else:
            archive_candidates.setdefault(candidate.archive_name, []).append(candidate)

    for candidate in local_candidates:
        if candidate.content is not None:
            render(candidate, candidate.content)
        else:
            render(candidate, _read_xml(candidate.xml_path))

    # ogni worker riceve in initializer l'urn_index completo: oltre cpu_count e' solo
    # memoria in piu' (su host piccoli il pool arriva a BrokenProcessPool)
    render_workers = min(8, max(1, os.cpu_count() or 1))
    for archive_name, grouped in sorted(archive_candidates.items()):
        if archive_root is None:
            raise RuntimeError(f"archive root is required for {archive_name}")
        archive_path = archive_root / archive_name
        if len(grouped) < RENDER_POOL_MIN_MEMBERS:
            with ZipFile(archive_path) as archive:
                for candidate in grouped:
                    if candidate.member_name is None:
                        raise RuntimeError(f"missing ZIP member reference for {candidate.source}")
                    content = _source_xml_text(archive.read(candidate.member_name))
                    render(candidate, content)
            continue
        with ProcessPoolExecutor(
            max_workers=render_workers,
            initializer=_open_render_archive,
            initargs=(str(archive_path), str(output), urn_index),
        ) as pool:
            chunk = max(1, len(grouped) // (render_workers * 8))
            for outcome in pool.map(_render_archive_member, grouped, chunksize=chunk):
                _apply_outcome(report, outcome)
    return urn_index


def convert_akn_dir_to_md(src_dir: str, md_dir: str, urn_index: dict[str, str],
                          collection_name: str) -> int:
    """Compatibility wrapper; new callers should use the explicit two-pass API."""
    report = ConversionReport()
    candidates = discover_candidates([(collection_name, "V", Path(src_dir))], report)
    chosen = select_canonical(candidates, report)
    render_candidates(chosen, Path(md_dir), report)
    urn_index.update({c.metadata.urn or "": c.repo_path for c in chosen})
    if report.errors:
        raise RuntimeError("; ".join(error.message for error in report.errors))
    return report.converted
