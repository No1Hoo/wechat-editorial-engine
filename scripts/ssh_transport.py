"""Delivery SSH transport: explicit physical egress on Windows; scoped runtime."""
import os
import json
import shlex
import sys
from pathlib import Path

from scripts.remote_safety import safe_remote_path


def _runtime_config():
    path = Path(__file__).resolve().parents[1] / 'config/delivery_runtime.json'
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid delivery runtime config')
    return value


def remote_root():
    value = os.environ.get('JACKIE_REMOTE_ROOT') or _runtime_config().get('root')
    if not value:
        raise ValueError('缺少 JACKIE_REMOTE_ROOT 或 config/delivery_runtime.json 的 root')
    safe_remote_path(value, 'JACKIE_REMOTE_ROOT')
    if not value.startswith('/'):
        raise ValueError('Remote runtime must use an absolute path')
    return value.rstrip('/')


def remote_python():
    value = os.environ.get('JACKIE_REMOTE_PYTHON') or (
        'python3' if os.environ.get('JACKIE_REMOTE_ROOT') else _runtime_config().get('python', 'python3'))
    safe_remote_path(value, 'JACKIE_REMOTE_PYTHON')
    return value


def transport_args(program):
    if program not in ('ssh', 'scp'):
        raise ValueError('Unsupported transport')
    args = [program, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30']
    if sys.platform == 'win32':
        from tools.jackie_studio.config import get_active_bind_address
        args += ['-o', 'BindAddress=' + get_active_bind_address()]
    return args


def ops_command(args):
    return 'cd ' + shlex.quote(remote_root()) + ' && ' + shlex.join(
        [remote_python(), 'scripts/mp_ops.py', *args])
