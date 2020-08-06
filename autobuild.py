import sys
import os
import argparse
from os import environ
from github import Github
from pathlib import Path, PurePosixPath
from subprocess import check_call
import subprocess
from sys import stdout
import fnmatch
import traceback
from tabulate import tabulate
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import requests
import shlex
import time
import tempfile
import shutil

# After which overall time it should stop building (in seconds)
BUILD_TIMEOUT = 18000

# Packages that take too long to build, and should be handled manually
SKIP = [
    'mingw-w64-clang',
]


def timeoutgen(timeout):
    end = time.time() + timeout

    def new():
        return max(end - time.time(), 0)
    return new


get_timeout = timeoutgen(BUILD_TIMEOUT)


def run_cmd(msys2_root, args, **kwargs):
    executable = os.path.join(msys2_root, 'usr', 'bin', 'bash.exe')
    env = kwargs.pop("env", os.environ.copy())
    env["CHERE_INVOKING"] = "1"
    env["MSYSTEM"] = "MSYS"
    env["MSYS2_PATH_TYPE"] = "minimal"
    check_call([executable, '-lc'] + [shlex.join([str(a) for a in args])], env=env, **kwargs)


@contextmanager
def fresh_git_repo(url, path):
    if not os.path.exists(path):
        check_call(["git", "clone", url, path])
    else:
        check_call(["git", "fetch", "origin"], cwd=path)
        check_call(["git", "reset", "--hard", "origin/master"], cwd=path)
    try:
        yield
    finally:
        assert os.path.exists(path)
        check_call(["git", "clean", "-xfdf"], cwd=path)
        check_call(["git", "reset", "--hard", "HEAD"], cwd=path)


@contextmanager
def gha_group(title):
    print(f'\n::group::{title}')
    stdout.flush()
    try:
        yield
    finally:
        print('::endgroup::')
        stdout.flush()


class BuildError(Exception):
    pass


class MissingDependencyError(BuildError):
    pass


class BuildTimeoutError(BuildError):
    pass


def download_asset(asset, target_path: str, timeout=15) -> str:
    with requests.get(asset.browser_download_url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(target_path, 'wb') as h:
            for chunk in r.iter_content(4096):
                h.write(chunk)


def upload_asset(type_: str, path: os.PathLike, replace=True):
    # type_: msys/mingw/failed
    path = Path(path)
    gh = Github(*get_credentials())

    current_user = gh.get_user()
    if current_user.login != "github-actions[bot]" or current_user.type != "Bot":
        print("WARNING: upload skipped, not running in CI")
        return

    repo = gh.get_repo('msys2/msys2-devtools')
    release = get_release_assets(repo, "staging-" + type_)
    if replace:
        for asset in release.get_assets():
            if path.name == asset.name:
                asset.delete_asset()
    release.upload_asset(str(path))


def get_python_path(msys2_root, msys2_path):
    return Path(os.path.normpath(msys2_root + msys2_path))


def to_pure_posix_path(path):
    return PurePosixPath("/" + str(path).replace(":", "", 1).replace("\\", "/"))


@contextmanager
def backup_pacman_conf(msys2_root):
    conf = get_python_path(msys2_root, "/etc/pacman.conf")
    backup = get_python_path(msys2_root, "/etc/pacman.conf.backup")
    shutil.copyfile(conf, backup)
    try:
        yield
    finally:
        os.replace(backup, conf)


@contextmanager
def staging_dependencies(pkg, msys2_root, builddir):
    gh = Github(*get_credentials())
    repo = gh.get_repo('msys2/msys2-devtools')

    def add_to_repo(repo_root, repo_type, asset):
        repo_dir = Path(repo_root) / get_repo_subdir(repo_type, asset)
        os.makedirs(repo_dir, exist_ok=True)
        print(f"Downloading {asset.name}...")
        package_path = os.path.join(repo_dir, asset.name)
        download_asset(asset, package_path)

        repo_name = "autobuild-" + (
            str(get_repo_subdir(repo_type, asset)).replace("/", "-").replace("\\", "-"))
        repo_db_path = os.path.join(repo_dir, f"{repo_name}.db.tar.gz")

        conf = get_python_path(msys2_root, "/etc/pacman.conf")
        with open(conf, "r", encoding="utf-8") as h:
            text = h.read()
            uri = to_pure_posix_path(repo_dir).as_uri()
            if uri not in text:
                text.replace("#RemoteFileSigLevel = Required",
                             "RemoteFileSigLevel = Never")
                with open(conf, "w", encoding="utf-8") as h2:
                    h2.write(f"""[{repo_name}]
Server={uri}
SigLevel=Never
""")
                    h2.write(text)

        run_cmd(msys2_root, ["repo-add", to_pure_posix_path(repo_db_path),
                             to_pure_posix_path(package_path)], cwd=repo_dir)

    repo_root = os.path.join(builddir, "_REPO")
    try:
        shutil.rmtree(repo_root, ignore_errors=True)
        os.makedirs(repo_root, exist_ok=True)
        with backup_pacman_conf(msys2_root):
            to_add = []
            for name, dep in pkg['ext-depends'].items():
                pattern = f"{name}-{dep['version']}-*.pkg.*"
                repo_type = "msys" if dep['repo'].startswith('MSYS2') else "mingw"
                for asset in get_release_assets(repo, "staging-" + repo_type):
                    if fnmatch.fnmatch(asset.name, pattern):
                        to_add.append((repo_type, asset))
                        break
                else:
                    raise MissingDependencyError(f"asset for {pattern} not found")

            for repo_type, asset in to_add:
                add_to_repo(repo_root, repo_type, asset)

            # in case they are already installed we need to upgrade
            run_cmd(msys2_root, ["pacman", "--noconfirm", "-Suy"])
            yield
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)
        # downgrade again
        run_cmd(msys2_root, ["pacman", "--noconfirm", "-Suuy"])


def build_package(pkg, msys2_root, builddir):
    assert os.path.isabs(builddir)
    assert os.path.isabs(msys2_root)
    os.makedirs(builddir, exist_ok=True)

    repo_name = {"MINGW-packages": "M", "MSYS2-packages": "S"}.get(pkg['repo'], pkg['repo'])
    repo_dir = os.path.join(builddir, repo_name)
    is_msys = pkg['repo'].startswith('MSYS2')

    with staging_dependencies(pkg, msys2_root, builddir), \
            fresh_git_repo(pkg['repo_url'], repo_dir):
        pkg_dir = os.path.join(repo_dir, pkg['repo_path'])
        makepkg = 'makepkg' if is_msys else 'makepkg-mingw'

        try:
            run_cmd(msys2_root, [
                makepkg,
                '--noconfirm',
                '--noprogressbar',
                '--skippgpcheck',
                '--nocheck',
                '--syncdeps',
                '--rmdeps',
                '--cleanbuild'
            ], cwd=pkg_dir, timeout=get_timeout())

            env = environ.copy()
            if not is_msys:
                env['MINGW_INSTALLS'] = 'mingw64'
            run_cmd(msys2_root, [
                makepkg,
                '--noconfirm',
                '--noprogressbar',
                '--skippgpcheck',
                '--allsource'
            ], env=env, cwd=pkg_dir, timeout=get_timeout())
        except subprocess.TimeoutExpired as e:
            raise BuildTimeoutError(e)
        except subprocess.CalledProcessError as e:

            for item in pkg['packages']:
                with tempfile.TemporaryDirectory() as tempdir:
                    failed_path = os.path.join(tempdir, f"{item}-{pkg['version']}.failed")
                    with open(failed_path, 'wb') as h:
                        # github doesn't allow empty assets
                        h.write(b'oh no')
                    upload_asset("failed", failed_path)

            raise BuildError(e)
        else:
            for entry in os.listdir(pkg_dir):
                if fnmatch.fnmatch(entry, '*.pkg.tar.*') or \
                        fnmatch.fnmatch(entry, '*.src.tar.*'):
                    path = os.path.join(pkg_dir, entry)
                    upload_asset("msys" if is_msys else "mingw", path)


def run_build(args):
    builddir = os.path.abspath(args.builddir)
    msys2_root = os.path.abspath(args.msys2_root)

    if not sys.platform == "win32":
        raise SystemExit("ERROR: Needs to run under native Python")

    if not shutil.which("git"):
        raise SystemExit("ERROR: git not in PATH")

    if not os.path.isdir(msys2_root):
        raise SystemExit("ERROR: msys2_root doesn't exist")

    try:
        run_cmd(msys2_root, [])
    except Exception as e:
        raise SystemExit("ERROR: msys2_root not functional", e)

    for pkg in get_packages_to_build()[2]:
        with gha_group(f"[{ pkg['repo'] }] { pkg['name'] }..."):
            try:
                build_package(pkg, msys2_root, builddir)
            except MissingDependencyError as e:
                print("missing deps")
                print(e)
                continue
            except BuildTimeoutError:
                print("timeout")
                break
            except BuildError:
                print("failed")
                traceback.print_exc(file=sys.stdout)
                continue


def get_buildqueue():
    pkgs = []
    r = requests.get("https://packages.msys2.org/api/buildqueue")
    r.raise_for_status()
    dep_mapping = {}
    for pkg in r.json():
        pkg['repo'] = pkg['repo_url'].split('/')[-1]
        pkgs.append(pkg)
        for name in pkg['packages']:
            dep_mapping[name] = pkg

    # link up dependencies with the real package in the queue
    for pkg in pkgs:
        ver_depends = {}
        for dep in pkg['depends']:
            ver_depends[dep] = dep_mapping[dep]
        pkg['ext-depends'] = ver_depends

    return pkgs


def get_release_assets(repo, release_name):
    assets = []
    for asset in repo.get_release(release_name).get_assets():
        uploader = asset.uploader
        if uploader.type != "Bot" or uploader.login != "github-actions[bot]":
            raise SystemExit(f"ERROR: Asset '{asset.name}' not uploaded "
                             f"by GHA but '{uploader.login}'. Aborting.")
        assets.append(asset)
    return assets


def get_packages_to_build():
    gh = Github(*get_credentials())

    repo = gh.get_repo('msys2/msys2-devtools')
    assets = []
    for name in ["msys", "mingw"]:
        assets.extend([a.name for a in get_release_assets(repo, 'staging-' + name)])
    assets_failed = [a.name for a in get_release_assets(repo, 'staging-failed')]

    def pkg_is_done(pkg):
        for item in pkg['packages']:
            if not fnmatch.filter(assets, f"{item}-{pkg['version']}-*.pkg.tar.*"):
                return False
        return True

    def pkg_has_failed(pkg):
        for item in pkg['packages']:
            if f"{item}-{pkg['version']}.failed" in assets_failed:
                return True
        return False

    def pkg_is_skipped(pkg):
        return pkg['name'] in SKIP

    todo = []
    done = []
    skipped = []
    for pkg in get_buildqueue():
        if pkg_is_done(pkg):
            done.append(pkg)
        elif pkg_has_failed(pkg):
            skipped.append((pkg, "failed"))
        elif pkg_is_skipped(pkg):
            skipped.append((pkg, "skipped"))
        else:
            for dep in pkg['ext-depends'].values():
                if pkg_has_failed(dep) or pkg_is_skipped(dep):
                    skipped.append((pkg, "requires: " + dep['name']))
                    break
            else:
                todo.append(pkg)

    return done, skipped, todo


def show_build(args):
    done, skipped, todo = get_packages_to_build()

    with gha_group(f"TODO ({len(todo)})"):
        print(tabulate([(p["name"], p["version"]) for p in todo],
                       headers=["Package", "Version"]))

    with gha_group(f"SKIPPED ({len(skipped)})"):
        print(tabulate([(p["name"], p["version"], r) for (p, r) in skipped],
                       headers=["Package", "Version", "Reason"]))

    with gha_group(f"DONE ({len(done)})"):
        print(tabulate([(p["name"], p["version"]) for p in done],
                       headers=["Package", "Version"]))


def show_assets(args):
    gh = Github(*get_credentials())
    repo = gh.get_repo('msys2/msys2-devtools')

    for name in ["msys", "mingw"]:
        assets = get_release_assets(repo, 'staging-' + name)

        print(tabulate(
            [[
                asset.name,
                asset.size,
                asset.created_at,
                asset.updated_at,
            ] for asset in assets],
            headers=["name", "size", "created", "updated"]
        ))


def get_repo_subdir(type_, asset):
    entry = asset.name
    t = Path(type_)
    if type_ == "msys":
        if fnmatch.fnmatch(entry, '*.pkg.tar.*'):
            return t / "x86_64"
        elif fnmatch.fnmatch(entry, '*.src.tar.*'):
            return t / "sources"
        else:
            raise Exception("unknown file type")
    elif type_ == "mingw":
        if fnmatch.fnmatch(entry, '*.src.tar.*'):
            return t / "sources"
        elif entry.startswith("mingw-w64-x86_64-"):
            return t / "x86_64"
        elif entry.startswith("mingw-w64-i686-"):
            return t / "i686"
        else:
            raise Exception("unknown file type")


def fetch_assets(args):
    gh = Github(*get_credentials())
    repo = gh.get_repo('msys2/msys2-devtools')

    todo = []
    skipped = []
    for name in ["msys", "mingw"]:
        p = Path(args.targetdir)
        assets = get_release_assets(repo, 'staging-' + name)
        for asset in assets:
            asset_dir = p / get_repo_subdir(name, asset)
            asset_dir.mkdir(parents=True, exist_ok=True)
            asset_path = asset_dir / asset.name
            if asset_path.exists():
                if asset_path.stat().st_size != asset.size:
                    print(f"Warning: {asset_path} already exists but has a different size")
                skipped.append(asset)
                continue
            todo.append((asset, asset_path))

    print(f"downloading: {len(todo)}, skipped: {len(skipped)}")

    def fetch_item(item):
        asset, asset_path = item
        download_asset(asset, asset_path)
        return item

    with ThreadPoolExecutor(4) as executor:
        for i, item in enumerate(executor.map(fetch_item, todo)):
            print(f"[{i + 1}/{len(todo)}] {item[0].name}")

    print("done")


def trigger_gha_build(args):
    gh = Github(*get_credentials())
    repo = gh.get_repo('msys2/msys2-devtools')
    if repo.create_repository_dispatch('manual-build'):
        print("Build triggered")
    else:
        raise Exception("trigger failed")


def clean_gha_assets(args):
    gh = Github(*get_credentials())

    print("Fetching packages to build...")
    patterns = []
    for pkg in get_buildqueue():
        patterns.append(f"{pkg['name']}-{pkg['version']}*")
        for item in pkg['packages']:
            patterns.append(f"{item}-{pkg['version']}*")

    print("Fetching assets...")
    assets = {}
    repo = gh.get_repo('msys2/msys2-devtools')
    for release in ['staging-msys', 'staging-mingw', 'staging-failed']:
        for asset in get_release_assets(repo, release):
            assets.setdefault(asset.name, []).append(asset)

    for pattern in patterns:
        for key in fnmatch.filter(assets.keys(), pattern):
            del assets[key]

    for items in assets.values():
        for asset in items:
            print(f"Deleting {asset.name}...")
            if not args.dry_run:
                asset.delete_asset()

    if not assets:
        print("Nothing to delete")


def get_credentials():
    if "GITHUB_TOKEN" in environ:
        return [environ["GITHUB_TOKEN"]]
    elif "GITHUB_USER" in environ and "GITHUB_PASS" in environ:
        return [environ["GITHUB_USER"], environ["GITHUB_PASS"]]
    else:
        raise Exception("'GITHUB_TOKEN' or 'GITHUB_USER'/'GITHUB_PASS' env vars not set")


def main(argv):
    parser = argparse.ArgumentParser(description="Build packages", allow_abbrev=False)
    parser.set_defaults(func=lambda *x: parser.print_help())
    subparser = parser.add_subparsers(title="subcommands")

    sub = subparser.add_parser("build", help="Build all packages")
    sub.add_argument("msys2_root", help="The MSYS2 install used for building. e.g. C:\\msys64")
    sub.add_argument(
        "builddir",
        help="A directory used for saving temporary build results and the git repos")
    sub.set_defaults(func=run_build)

    sub = subparser.add_parser(
        "show", help="Show all packages to be built", allow_abbrev=False)
    sub.set_defaults(func=show_build)

    sub = subparser.add_parser(
        "show-assets", help="Show all staging packages", allow_abbrev=False)
    sub.set_defaults(func=show_assets)

    sub = subparser.add_parser(
        "fetch-assets", help="Download all staging packages", allow_abbrev=False)
    sub.add_argument("targetdir")
    sub.set_defaults(func=fetch_assets)

    sub = subparser.add_parser("trigger", help="Trigger a GHA build", allow_abbrev=False)
    sub.set_defaults(func=trigger_gha_build)

    sub = subparser.add_parser("clean-assets", help="Clean up GHA assets", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.set_defaults(func=clean_gha_assets)

    get_credentials()

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    main(sys.argv)
