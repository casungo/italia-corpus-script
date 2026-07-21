"""Local corpus CLI backed by the published SQLite artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

from .snapshot import SCHEMA_VERSION

DEFAULT_REPO = "ahmeabd/italia-corpus"


def _print(value, as_json: bool) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2) if as_json else value)


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}; run `italia-corpus download`")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _get(args: argparse.Namespace) -> int:
    with _connect(args.database) as db:
        if args.article:
            sql = "SELECT * FROM articles WHERE document_urn = ? AND anchor = ?"
            values: list[str] = [args.urn, args.article]
            if args.vigente_al:
                sql += " AND (valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to > ?)"
                values.extend([args.vigente_al, args.vigente_al])
            sql += " ORDER BY COALESCE(valid_from, '') DESC, version DESC LIMIT 1"
            row = db.execute(sql, values).fetchone()
        else:
            row = db.execute("SELECT * FROM documents WHERE urn = ?", (args.urn,)).fetchone()
    if not row:
        return 1
    value = dict(row)
    _print(value if args.json else value["text"], args.json)
    return 0


def _search(args: argparse.Namespace) -> int:
    sql = """
        SELECT d.* FROM documents_fts f JOIN documents d ON d.urn = f.urn
        WHERE documents_fts MATCH ?
    """
    values: list[str] = [args.query]
    if args.vigente_al:
        sql += " AND (d.valid_from IS NULL OR d.valid_from <= ?) AND (d.valid_to IS NULL OR d.valid_to > ?)"
        values.extend([args.vigente_al, args.vigente_al])
    sql += " ORDER BY rank LIMIT ?"
    values.append(str(args.limit))
    with _connect(args.database) as db:
        rows = [dict(row) for row in db.execute(sql, values)]
    if not rows:
        return 1
    _print(rows if args.json else "\n\n".join(f"{r['urn']}\n{r['titolo']}" for r in rows), args.json)
    return 0


def _verify(args: argparse.Namespace) -> int:
    root = args.directory
    sums = root / "SHA256SUMS"
    failures = []
    if not sums.is_file():
        failures.append("SHA256SUMS missing")
    else:
        for line in sums.read_text(encoding="ascii").splitlines():
            try:
                digest, name = line.split("  ", 1)
                path = root / name
                if Path(name).name != name or len(digest) != 64:
                    raise ValueError
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    failures.append(f"checksum: {name}")
            except ValueError:
                failures.append(f"malformed checksum line: {line}")
    manifest = root / "manifest.json"
    index_path = root / "urn-index.json"
    database = root / "corpus.sqlite"
    if not manifest.is_file():
        failures.append("manifest schema")
    else:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        if manifest_data.get("schema_version") != SCHEMA_VERSION:
            failures.append("manifest schema")
    if not index_path.is_file():
        failures.append("urn-index.json missing")
    else:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        documents = index.get("documents", {})
        codes = index.get("by_codice_redazionale", {})
        if index.get("schema_version") != SCHEMA_VERSION:
            failures.append("urn index schema")
        for urn, document in documents.items():
            code = document.get("codice_redazionale")
            if not code or codes.get(code) != {"path": document.get("path"), "urn": urn}:
                failures.append(f"index reference: {urn}")
    if not database.is_file():
        failures.append("corpus.sqlite missing")
    elif manifest.is_file() and index_path.is_file():
        with sqlite3.connect(database) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
            count = db.execute("SELECT count(*) FROM documents").fetchone()[0]
            article_count = db.execute("SELECT count(*) FROM articles").fetchone()[0]
            urns = {row[0] for row in db.execute("SELECT urn FROM documents")}
        if integrity != "ok":
            failures.append(f"SQLite integrity: {integrity}")
        if foreign_keys:
            failures.append("SQLite foreign keys")
        if count != manifest_data.get("counts", {}).get("acts") or urns != set(documents):
            failures.append("SQLite document count/references")
        if article_count != manifest_data.get("counts", {}).get("articles"):
            failures.append("SQLite article count")
    _print({"ok": not failures, "failures": failures}, True)
    return 1 if failures else 0


def _download(args: argparse.Namespace) -> int:
    api = f"https://api.github.com/repos/{args.repo}/releases/{'latest' if args.version == 'latest' else 'tags/snapshot-' + args.version}"
    request = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json", "User-Agent": "italia-corpus/2"})
    with urllib.request.urlopen(request) as response:
        release = json.load(response)
    args.directory.mkdir(parents=True, exist_ok=True)
    wanted = set(args.assets)
    found = 0
    for asset in release["assets"]:
        if asset["name"] in wanted:
            urllib.request.urlretrieve(asset["browser_download_url"], args.directory / asset["name"])
            found += 1
    return 0 if found == len(wanted) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="italia-corpus")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("get")
    get.add_argument("--urn", required=True)
    get.add_argument("--article", help="stable article anchor, for example art-575")
    get.add_argument("--vigente-al", help="return the article only if valid on YYYY-MM-DD")
    get.add_argument("--database", type=Path, default=Path("corpus.sqlite"))
    get.set_defaults(handler=_get)
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--vigente-al")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--database", type=Path, default=Path("corpus.sqlite"))
    search.set_defaults(handler=_search)
    verify = sub.add_parser("verify")
    verify.add_argument("directory", nargs="?", type=Path, default=Path("."))
    verify.set_defaults(handler=_verify)
    download = sub.add_parser("download")
    download.add_argument("--version", default="latest")
    download.add_argument("--repo", default=DEFAULT_REPO)
    download.add_argument("--directory", type=Path, default=Path("."))
    download.add_argument("--assets", nargs="+", default=["corpus.sqlite", "manifest.json", "SHA256SUMS"])
    download.set_defaults(handler=_download)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.handler(args)
    except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
