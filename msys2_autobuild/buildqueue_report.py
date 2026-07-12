from tabulate import tabulate

from .queue import Package, PackageStatus, get_cycles
from .utils import gha_group


def show_cycles(pkgs: list[Package]) -> None:
    cycles = get_cycles(pkgs)
    if cycles:
        def format_package(p: Package) -> str:
            return f"{p['name']} [{p['version_repo']} -> {p['version']}]"

        with gha_group(f"Dependency Cycles ({len(cycles)})"):
            print(tabulate([
                (format_package(a), "<-->", format_package(b)) for (a, b) in cycles],
                headers=["Package", "", "Package"]))


def show_buildqueue(pkgs: list[Package]) -> None:
    todo = []
    waiting = []
    done = []
    failed = []

    show_cycles(pkgs)

    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            status = pkg.get_status(build_type)
            details = pkg.get_status_details(build_type)
            details.pop("blocked", None)
            if status == PackageStatus.WAITING_FOR_BUILD:
                todo.append((pkg, build_type, status, details))
            elif status in (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED,
                            PackageStatus.FINISHED_BUT_INCOMPLETE):
                done.append((pkg, build_type, status, details))
            elif status in (PackageStatus.WAITING_FOR_DEPENDENCIES,
                            PackageStatus.MANUAL_BUILD_REQUIRED):
                waiting.append((pkg, build_type, status, details))
            else:
                failed.append((pkg, build_type, status, details))

    def show_table(name: str, items: list) -> None:
        with gha_group(f"{name} ({len(items)})"):
            print(tabulate([(p["name"], bt, p["version"], str(s), d) for (p, bt, s, d) in items],
                           headers=["Package", "Build", "Version", "Status", "Details"]))

    show_table("TODO", todo)
    show_table("WAITING", waiting)
    show_table("FAILED", failed)
    show_table("DONE", done)
