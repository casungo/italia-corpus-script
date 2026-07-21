import argparse
import os
import sys
from pathlib import Path

from .config import EXTRACTION_BUFFER_PATH, logger
from .github_client import github_client, verify_github_session
from .pipeline import extract_and_push


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"get", "search", "verify", "download"}:
        from .cli import main as cli_main
        raise SystemExit(cli_main())
    parser = argparse.ArgumentParser(
        description="Scarica e converte le collezioni Normattiva in uno snapshot verificato.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Genera snapshot e artifact senza commit, push, tag o release.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Snapshot precedente da usare per i controlli di regressione.",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Dry-run campionato: tutte le collezioni, massimo 1.000 XML per archivio.",
    )
    parser.add_argument(
        "--download-cache",
        type=Path,
        help="Cache persistente degli ZIP, separata per collezione, formato e data upstream.",
    )
    parser.add_argument(
        "root_path",
        nargs="?",
        default=os.getenv("ROOT_PATH"),
        help="Cartella di lavoro temporanea. Se omessa usa ROOT_PATH o chiede in console.",
    )
    args = parser.parse_args()
    if args.smoke_test and not args.dry_run:
        parser.error("--smoke-test richiede --dry-run")
    if args.smoke_test and args.baseline:
        parser.error("--smoke-test non può usare --baseline")
    root_path = (args.root_path or "").strip()
    if not root_path:
        root_path = (EXTRACTION_BUFFER_PATH or "").strip()
    if not root_path:
        parser.error("Specifica root_path o imposta ROOT_PATH / EXTRACTION_BUFFER_PATH.")

    logger.info("Root path: %s", root_path)
    gh = None
    if not args.dry_run:
        gh = github_client()
        verify_github_session(gh)
    output = extract_and_push(
        root_path,
        gh,
        dry_run=args.dry_run,
        baseline=args.baseline,
        smoke_test=args.smoke_test,
        download_cache=args.download_cache,
    )
    if args.dry_run:
        logger.info("Snapshot e artifact conservati in %s", output)
    logger.info("Done")


if __name__ == "__main__":
    main()
