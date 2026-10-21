"""Assemble public upload and customer handover files with provenance and hashes."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".delivery-package"
MARKETPLACE = (
    "README.md", "SUBMISSION.md", "GUIDE.md", "descriptor-widget.xml",
    "description.txt", "bonuspro-icon.svg", "bonuspro-manual.zip",
)
HANDOVER = (
    "docs/DELIVERY.md", "docs/DELIVERY_STATUS.json", "docs/CUSTOMER_GUIDE.md",
    "docs/HANDOVER.md", "docs/LIVE_CHECK_RU.md", "docs/PERSONAL_DISCOUNTS.md",
    "docs/RETURN_POLICY.md", "docs/CUTOVER.md", "docs/DEPLOYMENT.md",
    "docs/BACKUPS.md", "docs/OPERATIONS.md", "docs/REGISTERED_APP.md",
)

def git_object(ref):
    return subprocess.check_output(
        ["git", "rev-parse", "--verify", ref], cwd=ROOT, text=True
    ).strip()

def public_source(relative):
    path = ROOT / relative
    if not path.is_file() or path.is_symlink():
        raise ValueError("package input must be a regular file: " + relative)
    if not path.resolve().is_relative_to(ROOT):
        raise ValueError("package input must stay inside repository: " + relative)
    for parent in path.parents:
        if parent == ROOT:
            break
        if parent.is_symlink():
            raise ValueError("package input parent must not be a symlink")
    return path

def main():
    status = json.loads(public_source("docs/DELIVERY_STATUS.json").read_text(encoding="utf-8"))
    report = json.loads(public_source(".marketplace-validation.json").read_text(encoding="utf-8"))
    assert report["descriptor"] == "valid against official XSD"
    commit = git_object("HEAD")
    recorded = status["last_confirmed_deployment"]["application_git_objects"]
    current = {path: git_object("HEAD:" + path) for path in recorded}
    run_id = os.environ.get("GITHUB_RUN_ID")
    repository = os.environ.get("GITHUB_REPOSITORY", "zhantugelmukagali/romeoastana")
    run_url = (
        "https://github.com/" + repository + "/actions/runs/" + run_id
        if run_id else None
    )
    manifest = {
        "source_commit": commit,
        "build_run_url": run_url,
        "application_git_objects": current,
        "matches_last_confirmed_application_sources": current == recorded,
        "last_confirmed_deployment": status["last_confirmed_deployment"],
        "previous_automatic_checks": status["automatic_checks"],
        "live_acceptance": status["live_acceptance"],
        "deferred_by_owner": status["deferred_by_owner"],
        "publication": status["publication"],
        "scope": (
            "File integrity and source provenance only. This build does not "
            "inspect or deploy the VPS and does not confirm marketplace approval."
        ),
    }
    # A fixed allowlist excludes credentials, environment files and historical drafts.
    for kind in ("marketplace", "handover"):
        directory = OUT / kind
        if OUT.is_symlink() or directory.is_symlink():
            raise ValueError("package output must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.iterdir()):
            raise ValueError("delivery output must be empty: " + str(directory))
        if kind == "marketplace":
            for name in MARKETPLACE:
                shutil.copyfile(public_source("marketplace/" + name), directory / name)
        else:
            for name in HANDOVER:
                destination = directory / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(public_source(name), destination)
            for name in MARKETPLACE:
                destination = directory / "marketplace" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(public_source("marketplace/" + name), destination)
            (directory / "START.txt").write_text(
                "Start with docs/DELIVERY.md. Marketplace uploads are in marketplace/.\n"
                "No passwords, credentials or database backups are included.\n",
                encoding="utf-8",
            )
        (directory / "schema-validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (directory / "release-manifest.json").write_text(
            json.dumps({**manifest, "bundle": kind}, ensure_ascii=False, indent=2)
            + "\n", encoding="utf-8"
        )
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        checksums = "".join(
            sha256(path.read_bytes()).hexdigest() + "  "
            + path.relative_to(directory).as_posix() + "\n" for path in files
        )
        (directory / "SHA256SUMS").write_text(checksums, encoding="utf-8")
        print(json.dumps({
            "bundle": kind, "files": len(files) + 1, "source_commit": commit,
            "matches_last_confirmed_application_sources": current == recorded,
        }))
    print("DELIVERY_PACKAGES_READY: upload materials and customer handover assembled")

if __name__ == "__main__":
    main()
