#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Rebuild derived files; retained reviews and approvals are read-only inputs.

The legacy authorized argument is accepted but never renews evidence.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sync_article_hash_chain(target_dir, track="F", track_type="fishery", authorized=False):
    """Return separate build/review results without requiring delivery approval.

    Neither --authorized nor ALLOW_HASH_RESYNC grants permission to alter a
    retained review. A stale approval is reported, never updated or normalized.
    """
    from scripts.editorial_policy import passages, writing_review_errors
    from scripts.production_review import ARTIFACTS, _approval, review_errors
    from scripts.review_contract import review_errors as editorial_errors
    from datetime import datetime, timezone

    target = Path(target_dir).resolve()
    content = json.loads((target / "content.json").read_text(encoding="utf-8"))
    command = [sys.executable, "-X", "utf8", str(ROOT / "pipeline.py"),
               "render", "--dir", str(target), "--track", track,
               "--track-type", track_type]
    rendered = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if rendered.returncode:
        raise RuntimeError("派生渲染失败：" + (rendered.stderr or rendered.stdout))
    lines = ["# " + content["title"], "", content.get("digest", ""), ""]
    for location, text in passages(content):
        if location in ("title", "summary", "digest") or not text:
            continue
        prefix = "## " if location.endswith("/title") else "> 配图：" if location.endswith("/caption") else ""
        lines.extend([prefix + text, ""])
    readable = target / "review/readable_draft.md"
    readable.parent.mkdir(parents=True, exist_ok=True)
    readable.write_bytes("\n".join(lines).encode("utf-8"))

    report = {"derived_passed": True, "authorized_ignored": bool(authorized),
              "approvals": {}, "production": {}}
    record_path = target / "production_review.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
        previous = None
        for stage in ARTIFACTS:
            entry = record.get("approvals", {}).get(stage)
            if entry is None:
                report["approvals"][stage] = {"state": "pending", "errors": []}
                continue
            try:
                previous = _approval(target, entry, stage, previous, datetime.now(timezone.utc))
                report["approvals"][stage] = {"state": "current", "errors": []}
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                report["approvals"][stage] = {"state": "invalid", "errors": [str(exc)]}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        report["approval_record_errors"] = [str(exc)]
    for stage in ("topic", "sample", "delivery"):
        errors = review_errors(target, stage)
        report["production"][stage] = {"passed": not errors, "errors": errors}
    report["writing_errors"] = writing_review_errors(target)
    manifest = json.loads((target / "build_manifest.json").read_text(encoding="utf-8"))
    report["editorial_errors"] = editorial_errors(target, manifest.get("build_hash"), manifest.get("assets", []))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description="仅重建派生稿并只读报告批准/审核状态")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--track", default="F", choices=list("ABCDEFS"))
    parser.add_argument("--track-type", default="fishery")
    parser.add_argument("--authorized", action="store_true", help="兼容旧调用；不更新或绕过任何批准")
    args = parser.parse_args()
    result = sync_article_hash_chain(args.dir, args.track, args.track_type, args.authorized)
    return 0 if result["derived_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
