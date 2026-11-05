#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Record retained user quotes and byte-bound versions, not speaker identity.

New snapshots are immutable and content-addressed. The single mutable pointer,
production_review.json, is replaced last under an exclusive cooperative lock.
Readers therefore see either the old record or a complete new transaction.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BEIJING = timezone(timedelta(hours=8))
ARTIFACTS = {"topic": "review/topic_brief.json", "sample": "review/sample.md", "manuscript": "content.json"}


def digest(value):
    return hashlib.sha256(Path(value).read_bytes() if isinstance(value, (str, Path)) else value).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _bytes_or_none(path):
    return path.read_bytes() if path.exists() else None


def _atomic_write(path, raw):
    """Write a complete sibling before replacing the visible path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".approval-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _approval_lock(folder):
    lock = folder / ".approval.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("批准事务锁已存在；核实活动事务或中断状态后再重试") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(str(os.getpid()).encode("ascii"))
        yield
    finally:
        lock.unlink()


def _record(target_dir, stages, user_quote, actor, approved_at):
    folder = Path(target_dir).resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"稿件目录不存在: {target_dir}")
    if not isinstance(user_quote, str) or not user_quote.strip():
        raise ValueError("user_quote 必须是非空真实用户发言原话")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor 必须为非空记录身份")
    now = datetime.now(BEIJING)
    when = datetime.fromisoformat(approved_at.replace("Z", "+00:00")) if approved_at else now
    if when.tzinfo is None or when > now:
        raise ValueError("批准时间须有时区且不在未来")
    when_str = when.isoformat()
    with _approval_lock(folder):
        # Validate every artifact before writing even the first snapshot.
        artifacts = {}
        for stage in stages:
            path = (folder / ARTIFACTS[stage]).resolve()
            if not path.is_relative_to(folder) or not path.is_file():
                raise FileNotFoundError("阶段产物不存在或越界: " + ARTIFACTS[stage])
            artifacts[stage] = (path, path.read_bytes())
        pr_path = folder / "production_review.json"
        if pr_path.is_symlink() or not (folder / 'evidence').resolve().is_relative_to(folder):
            raise ValueError("批准记录或证据目录越界")
        prior = _bytes_or_none(pr_path)
        if prior is None:
            from scripts.production_review import WORKFLOW
            from scripts.editorial_policy import policy_sha256
            record = {"schema": 1, "workflow": WORKFLOW, "policy_sha256": policy_sha256(),
                      "approvals": {}, "assessments": {}, "unresolved_blockers": []}
        else:
            record = json.loads(prior.decode("utf-8"))
        approvals = record.setdefault("approvals", {})
        results, snapshots = {}, {}
        for stage in stages:
            artifact_sha = digest(artifacts[stage][1])
            old = approvals.get(stage, {})
            # Same-version replay is a read-only no-op only if its snapshot is valid.
            if (old.get("status") == "approved" and old.get("artifact_sha256") == artifact_sha
                    and old.get("user_quote") == user_quote and old.get("actor") == actor):
                from scripts.production_review import _approval
                _approval(folder, old, stage, None, now)
                results[stage] = json.loads((folder / old["evidence_file"]).read_text(encoding="utf-8")) if old["evidence_file"].endswith(".json") else old.copy()
                continue
            evidence = {"schema": 1, "stage": stage, "actor_type": "user", "actor": actor,
                        "source": "user_message", "user_quote": user_quote,
                        "approved_at": when_str, "artifact": ARTIFACTS[stage],
                        "artifact_sha256": artifact_sha,
                        "verification_scope": "retained_quote_and_artifact_bytes_not_identity"}
            raw = _json_bytes(evidence)
            evidence_sha = digest(raw)
            relative = f"evidence/user_{stage}_approval_{evidence_sha}.json"
            snapshots[folder / relative] = raw
            approvals[stage] = {"status": "approved", "actor_type": "user", "actor": actor,
                                "source": "user_message", "scope": stage,
                                "artifact_sha256": artifact_sha, "approved_at": when_str,
                                "user_quote": user_quote, "evidence_file": relative,
                                "evidence_sha256": evidence_sha}
            results[stage] = evidence
        # Equal timestamps are valid for a single message approving two stages.
        previous = None
        for stage in ARTIFACTS:
            entry = approvals.get(stage)
            if not entry:
                continue
            timestamp = datetime.fromisoformat(entry["approved_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp > now or (previous and timestamp < previous):
                raise ValueError("批准时间缺时区、在未来或顺序倒置")
            previous = timestamp
        created = []
        committed = False
        try:
            for path, raw in snapshots.items():
                if path.exists():
                    if path.read_bytes() != raw:
                        raise ValueError("不可变批准快照发生冲突")
                else:
                    created.append(path)
                    _atomic_write(path, raw)
            if _bytes_or_none(pr_path) != prior or any(path.read_bytes() != raw for path, raw in artifacts.values()):
                raise ValueError("批准事务 CAS 冲突：产物或生产记录已变化")
            # No new approval: keep existing record bytes (including line endings).
            if snapshots:
                _atomic_write(pr_path, _json_bytes(record))
            committed = True
        finally:
            if not committed:
                for path in created:
                    path.unlink(missing_ok=True)
        return results


def record_stage_approval(target_dir, stage, user_quote, actor="user", approved_at=None):
    if stage == "topic_and_sample":
        return record_combined_topic_and_sample(target_dir, user_quote, actor, approved_at)
    if stage not in ARTIFACTS:
        raise ValueError("未知生产阶段: " + str(stage))
    return _record(target_dir, (stage,), user_quote, actor, approved_at)[stage]


def record_combined_topic_and_sample(target_dir, user_quote, actor="user", approved_at=None):
    return _record(target_dir, ("topic", "sample"), user_quote, actor, approved_at)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="留存阶段原话和当前版本；不认证发言者身份")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--stage", required=True, choices=[*ARTIFACTS, "topic_and_sample"])
    parser.add_argument("--quote", required=True)
    parser.add_argument("--actor", default="user")
    parser.add_argument("--at")
    args = parser.parse_args()
    print(json.dumps(record_stage_approval(args.dir, args.stage, args.quote, args.actor, args.at), ensure_ascii=False, indent=2))
