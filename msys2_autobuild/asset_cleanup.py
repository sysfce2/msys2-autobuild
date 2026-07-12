import fnmatch
import random
import re

from github.GithubException import GithubException
from github.GitRelease import GitRelease
from github.GitReleaseAsset import GitReleaseAsset

from .config import get_all_build_types
from .gh import (get_asset_filename, get_current_repo, get_release,
                 get_release_assets, make_writable)
from .queue import get_buildqueue


def get_assets_to_delete() -> list[GitReleaseAsset]:
    print("Fetching packages to build...")
    keep_patterns = []
    for pkg in get_buildqueue():
        for build_type in pkg.get_build_types():
            keep_patterns.append(pkg.get_failed_name(build_type))
            keep_patterns.extend(pkg.get_build_patterns(build_type))
    keep_pattern_regex = re.compile('|'.join(fnmatch.translate(p) for p in keep_patterns))

    def should_be_deleted(asset: GitReleaseAsset) -> bool:
        filename = get_asset_filename(asset)
        return not keep_pattern_regex.match(filename)

    def get_to_delete(release: GitRelease) -> list[GitReleaseAsset]:
        assets = get_release_assets(release, include_incomplete=True)
        to_delete = []
        for asset in assets:
            if should_be_deleted(asset):
                to_delete.append(asset)
        return to_delete

    def get_all_releases() -> list[GitRelease]:
        repo = get_current_repo()

        releases = []
        for build_type in get_all_build_types():
            releases.append(get_release(repo, "staging-" + build_type))
        releases.append(get_release(repo, "staging-failed"))
        return releases

    print("Fetching assets...")
    assets = []
    for release in get_all_releases():
        assets.extend(get_to_delete(release))

    return assets


def clean_assets(dry_run: bool = False) -> None:
    print("Deleting assets...")
    while True:
        assets = get_assets_to_delete()
        if not assets:
            break

        # Spread parallel cleanup jobs across different assets.
        random.shuffle(assets)
        stale_snapshot = False
        for asset in assets:
            print(f"Deleting {get_asset_filename(asset)}...")
            if dry_run:
                continue
            try:
                with make_writable(asset):
                    asset.delete_asset()
            except GithubException as e:
                if e.status != 404:
                    raise
                # Another parallel cleanup job already removed an asset from this snapshot.
                # Refresh the asset list so we stop working with stale objects.
                stale_snapshot = True
                break

        if dry_run:
            break
        if not stale_snapshot:
            continue
