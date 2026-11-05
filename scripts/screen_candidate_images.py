#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Deterministic dHash16 candidate screening against the full published index."""
import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.jackie_studio.engine.history import published_images
from scripts.check_image_reuse import dhash, ham

IMG_EXT = ('.jpg', '.jpeg', '.png', '.webp')


class ScreeningBlocked(ValueError):
    """The history contract or requested screening/export is incomplete."""


def _validate_history(hashes, coverage):
    if not isinstance(coverage, dict) or coverage.get('status') != 'ok' or coverage.get('complete') is not True:
        raise ScreeningBlocked('历史图库覆盖状态非完整 ok，阻断导出')
    if not hashes:
        raise ScreeningBlocked('历史图库为空，阻断导出')
    for record in hashes:
        if not isinstance(record, (tuple, list)) or len(record) != 2:
            raise ScreeningBlocked('历史图库记录结构损坏')
        name, digest = record
        if not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 256 or set(digest) - {'0', '1'}:
            raise ScreeningBlocked('历史图库 dHash16 损坏')
    published = coverage.get('published_articles')
    matched = coverage.get('matched_articles')
    if (type(published) is not int or published <= 0 or type(matched) is not int
            or matched != published or coverage.get('coverage') != 1.0
            or coverage.get('missing_articles') != []
            or type(coverage.get('image_count')) is not int
            or coverage['image_count'] != len(hashes)):
        raise ScreeningBlocked('历史图库覆盖元数据缺失或不一致')


def screen_candidates(candidates_dir, history_threshold=30, pairwise_threshold=30,
                      out_dir=None, select_count=None, ledger=None):
    """Return legacy ``(Path, hash, min_history_distance, history_id)`` tuples.

    Invalid/incomplete history raises ScreeningBlocked before any export. Existing
    positional arguments remain supported; ledger is an optional audit JSON path.
    """
    start = time.perf_counter()
    report = {'schema_version': 1, 'algorithm': 'dhash16', 'hash_bits': 256,
              'thresholds': {'history': history_threshold, 'pairwise': pairwise_threshold},
              'coverage': None, 'images': [], 'result': 'blocked', 'errors': [],
              'exported': [], 'timings_ms': {}}
    passed = []
    try:
        if (type(history_threshold) is not int or type(pairwise_threshold) is not int
                or not 30 <= history_threshold <= 256 or not 30 <= pairwise_threshold <= 256):
            raise ScreeningBlocked('历史和组内阈值须为 30 至 256 的整数')
        if select_count is not None and (type(select_count) is not int or select_count <= 0):
            raise ScreeningBlocked('导出张数须为正整数')
        directory = Path(candidates_dir).resolve()
        if not directory.is_dir():
            raise FileNotFoundError(f'候选目录不存在: {candidates_dir}')
        files = sorted((p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXT),
                       key=lambda p: (p.name.casefold(), p.name))
        if not files:
            raise ScreeningBlocked('候选目录为空')
        candidates = []
        for path in files:
            row = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'history_distance': None, 'closest_history': None,
                   'pairwise_distances': [], 'pairwise_min_distance': None,
                   'result': 'blocked'}
            report['images'].append(row)
            try:
                digest = dhash(str(path), n=16)
                row['dhash16'] = digest
                candidates.append((path, digest, row))
            except Exception as exc:
                row.update(result='unreadable', error=str(exc))
        report['timings_ms']['hashing'] = (time.perf_counter() - start) * 1000
        history_start = time.perf_counter()
        hashes, coverage = published_images()
        report['coverage'] = coverage
        _validate_history(hashes, coverage)
        hashes = sorted(hashes, key=lambda item: (item[0], item[1]))
        report['timings_ms']['history_load'] = (time.perf_counter() - history_start) * 1000
        for path, digest, row in candidates:
            distance, closest = min((ham(digest, old), name) for name, old in hashes)
            row.update(history_distance=distance, closest_history=closest)
            distances = [{'path': str(other), 'distance': ham(digest, old)}
                         for other, old, _, _ in passed]
            row['pairwise_distances'] = distances
            row['pairwise_min_distance'] = min((item['distance'] for item in distances), default=None)
            if distance <= history_threshold:
                row['result'] = 'rejected_history'
            elif any(item['distance'] <= pairwise_threshold for item in distances):
                row['result'] = 'rejected_pairwise'
            else:
                row['result'] = 'passed'
                passed.append((path, digest, distance, closest))
        if not passed:
            raise ScreeningBlocked('没有图片通过双重验距')
        if select_count is not None and len(passed) < select_count:
            raise ScreeningBlocked(f'通过图片 {len(passed)} 张，少于要求 {select_count} 张')
        report['result'] = 'passed'
    except Exception as exc:
        report['errors'].append(str(exc))
        raise
    finally:
        report['timings_ms']['total'] = (time.perf_counter() - start) * 1000
        if ledger:
            target = Path(ledger).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
        print(f"[*] 筛查结果: {report['result']} | 通过 {len(passed)} 张 | {report['timings_ms']['total']:.2f} ms")
    # Every prerequisite, including successful ledger persistence, precedes export.
    if out_dir:
        export_start = time.perf_counter()
        try:
            destination = Path(out_dir).resolve()
            destination.mkdir(parents=True, exist_ok=True)
            selected = passed[:select_count] if select_count else passed
            for index, (path, _, _, _) in enumerate(selected, 1):
                target = destination / f'{index:02d}_{path.name}'
                shutil.copy2(path, target)
                report['exported'].append(str(target))
        except Exception as exc:
            report['result'] = 'export_failed'
            report['errors'].append(str(exc))
            raise
        finally:
            report['timings_ms']['export'] = (time.perf_counter() - export_start) * 1000
            report['timings_ms']['total'] = (time.perf_counter() - start) * 1000
            if ledger:
                Path(ledger).resolve().write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    return passed


def main(argv=None):
    parser = argparse.ArgumentParser(description='Jackie Candidate Image Screener')
    parser.add_argument('--candidates', required=True, help='候选图片目录')
    parser.add_argument('--history-threshold', type=int, default=30)
    parser.add_argument('--pairwise-threshold', type=int, default=30)
    parser.add_argument('--out', help='导出目录')
    parser.add_argument('--select', type=int, help='导出张数')
    parser.add_argument('--ledger', help='可审计 JSON 路径')
    args = parser.parse_args(argv)
    try:
        screen_candidates(args.candidates, args.history_threshold, args.pairwise_threshold,
                          args.out, args.select, args.ledger)
    except Exception as exc:
        print(f'[!] {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
