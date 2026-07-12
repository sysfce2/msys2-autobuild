from typing import Any
from .asset_cleanup import clean_assets


def clean_gha_assets(args: Any) -> None:
    clean_assets(dry_run=args.dry_run)


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser("clean-assets", help="Clean up GHA assets", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.set_defaults(func=clean_gha_assets)
