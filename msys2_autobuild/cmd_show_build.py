from typing import Any

from .buildqueue_report import show_buildqueue
from .queue import get_buildqueue_with_status
from .utils import apply_optional_deps


def show_build(args: Any) -> None:
    apply_optional_deps(args.optional_deps or "")

    pkgs = get_buildqueue_with_status(full_details=args.details)
    show_buildqueue(pkgs)


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser(
        "show", help="Show all packages to be built", allow_abbrev=False)
    sub.add_argument(
        "--details", action="store_true", help="Show more details such as links to failed build logs (slow)")
    sub.add_argument("--optional-deps", action="store")
    sub.set_defaults(func=show_build)
