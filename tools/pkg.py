"""
These commands are used to build Salt packages.
"""
# pylint: disable=resource-leakage,broad-except
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import pathlib
import shutil
import sys

import yaml
from ptscripts import Context, command_group

log = logging.getLogger(__name__)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Define the command group
pkg = command_group(name="pkg", help="Packaging Related Commands", description=__doc__)


@pkg.command(
    name="set-salt-version",
    arguments={
        "salt_version": {
            "help": (
                "The salt version to write to 'salt/_version.txt'. If not passed "
                "it will be discovered by running 'python3 salt/version.py'."
            ),
            "nargs": "?",
            "default": None,
        },
        "overwrite": {
            "help": "Overwrite 'salt/_version.txt' if it already exists",
        },
    },
)
def set_salt_version(ctx: Context, salt_version: str, overwrite: bool = False):
    """
    Write the Salt version to 'salt/_version.txt'
    """
    salt_version_file = REPO_ROOT / "salt" / "_version.txt"
    if salt_version_file.exists():
        if not overwrite:
            ctx.error("The 'salt/_version.txt' file already exists")
            ctx.exit(1)
        salt_version_file.unlink()
    if salt_version is None:
        if not REPO_ROOT.joinpath(".git").exists():
            ctx.error(
                "Apparently not running from a Salt repository checkout. "
                "Unable to discover the Salt version."
            )
            ctx.exit(1)
            ctx.info("Discovering the Salt version...")
        ret = ctx.run(shutil.which("python3"), "salt/version.py", capture=True)
        salt_version = ret.stdout.strip().decode()
        ctx.info(f"Discovered Salt version: {salt_version!r}")

    if not REPO_ROOT.joinpath("salt").is_dir():
        ctx.error(
            "The path 'salt/' is not a directory. Unable to write 'salt/_version.txt'"
        )
        ctx.exit(1)

    try:
        REPO_ROOT.joinpath("salt/_version.txt").write_text(salt_version)
    except Exception as exc:
        ctx.error(f"Unable to write 'salt/_version.txt': {exc}")
        ctx.exit(1)

    ctx.info(f"Successfuly wrote {salt_version!r} to 'salt/_version.txt'")

    gh_env_file = os.environ.get("GITHUB_ENV", None)
    if gh_env_file is not None:
        variable_text = f"SALT_VERSION={salt_version}"
        ctx.info(f"Writing '{variable_text}' to '$GITHUB_ENV' file:", gh_env_file)
        with open(gh_env_file, "w", encoding="utf-8") as wfh:
            wfh.write(f"{variable_text}\n")

    gh_output_file = os.environ.get("GITHUB_OUTPUT", None)
    if gh_output_file is not None:
        variable_text = f"salt-version={salt_version}"
        ctx.info(f"Writing '{variable_text}' to '$GITHUB_OUTPUT' file:", gh_output_file)
        with open(gh_output_file, "w", encoding="utf-8") as wfh:
            wfh.write(f"{variable_text}\n")

    ctx.exit(0)


@pkg.command(
    name="pre-archive-cleanup",
    arguments={
        "cleanup_path": {
            "help": (
                "The salt version to write to 'salt/_version.txt'. If not passed "
                "it will be discovered by running 'python3 salt/version.py'."
            ),
            "metavar": "PATH_TO_CLEANUP",
        },
        "pkg": {
            "help": "Perform extended, pre-packaging cleanup routines",
        },
    },
)
def pre_archive_cleanup(ctx: Context, cleanup_path: str, pkg: bool = False):
    """
    Clean the provided path of paths that shouyld not be included in the archive.

    For example:

        * `__pycache__` directories
        * `*.pyc` files
        * `*.pyo` files

    When running on Windows and macOS, some additional cleanup is also done.
    """
    with open(str(REPO_ROOT / "pkg" / "common" / "env-cleanup-rules.yml")) as rfh:
        patterns = yaml.safe_load(rfh.read())

    if pkg:
        patterns = patterns["pkg"]
    else:
        patterns = patterns["ci"]

    if sys.platform.lower().startswith("win"):
        patterns = patterns["windows"]
    elif sys.platform.lower().startswith("darwin"):
        patterns = patterns["darwin"]
    else:
        patterns = patterns["linux"]

    def unnest_lists(patterns):
        if isinstance(patterns, list):
            for pattern in patterns:
                yield from unnest_lists(pattern)
        else:
            yield patterns

    dir_patterns = set()
    for pattern in unnest_lists(patterns["dir_patterns"]):
        dir_patterns.add(pattern)

    file_patterns = set()
    for pattern in unnest_lists(patterns["file_patterns"]):
        file_patterns.add(pattern)

    for root, dirs, files in os.walk(cleanup_path, topdown=True, followlinks=False):
        for dirname in dirs:
            path = pathlib.Path(root, dirname).resolve()
            if not path.exists():
                continue
            match_path = path.as_posix()
            for pattern in dir_patterns:
                if fnmatch.fnmatch(str(match_path), pattern):
                    ctx.info(
                        f"Deleting directory: {match_path}; Matching pattern: {pattern!r}"
                    )
                    shutil.rmtree(str(path))
                    break
        for filename in files:
            path = pathlib.Path(root, filename).resolve()
            if not path.exists():
                continue
            match_path = path.as_posix()
            for pattern in file_patterns:
                if fnmatch.fnmatch(str(match_path), pattern):
                    ctx.info(
                        f"Deleting file: {match_path}; Matching pattern: {pattern!r}"
                    )
                    try:
                        os.remove(str(path))
                    except FileNotFoundError:
                        pass
                    break


@pkg.command(
    name="generate-hashes",
    arguments={
        "files": {
            "help": "The files to generate the hashes for.",
            "nargs": "*",
        },
    },
)
def generate_hashes(ctx: Context, files: list[pathlib.Path]):
    """
    Generate "blake2b", "sha512" and "sha3_512" hashes for the passed files.
    """
    for fpath in files:
        ctx.info(f"* Processing {fpath} ...")
        hashes = {}
        for hash_name in ("blake2b", "sha512", "sha3_512"):
            ctx.info(f"   * Calculating {hash_name} ...")
            with fpath.open("rb") as rfh:
                try:
                    digest = hashlib.file_digest(rfh, hash_name)  # type: ignore[attr-defined]
                except AttributeError:
                    # Python < 3.11
                    buf = bytearray(2**18)  # Reusable buffer to reduce allocations.
                    view = memoryview(buf)
                    digest = getattr(hashlib, hash_name)()
                    while True:
                        size = rfh.readinto(buf)
                        if size == 0:
                            break  # EOF
                        digest.update(view[:size])
            digest_file_path = fpath.parent / f"{fpath.name}.{hash_name}"
            hexdigest = digest.hexdigest()
            ctx.info(f"   * Writing {digest_file_path} ...")
            digest_file_path.write_text(digest.hexdigest())
            hashes[hash_name] = hexdigest
        hashes_json_path = fpath.parent / f"{fpath.name}.json"
        ctx.info(f"   * Writing {hashes_json_path} ...")
        hashes_json_path.write_text(json.dumps(hashes))
    ctx.info("Done")
