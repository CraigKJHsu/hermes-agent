"""Controller-owned, read-only source discovery receipts; no config values."""
import hashlib
import os
import fnmatch
from pathlib import Path
import subprocess
import time

from hermes_constants import get_hermes_home


def _file_receipt(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return {'path': str(path), 'sha256': digest.hexdigest()}


def runtime_source_inventory(*, home=None, repository=None):
    home = Path(home) if home is not None else get_hermes_home()
    repository = Path(repository) if repository is not None else Path(__file__).resolve().parent.parent
    observed_at = int(time.time())
    errors = []
    def scan(directory, *, profile_scan=False):
        receipts = []
        complete = True
        try:
            children = sorted(directory.iterdir())
        except OSError as exc:
            errors.append({'path': str(directory), 'error_type': type(exc).__name__})
            return receipts, False
        for child in children:
            try:
                if profile_scan:
                    if not child.is_dir():
                        continue
                    # Enumerate explicitly: glob may suppress traversal errors.
                    matches = [p for p in child.iterdir() if p.name == 'config.yaml']
                else:
                    matches = [child] if fnmatch.fnmatch(child.name, '*source-locators*.json') else []
                for path in matches:
                    receipts.append(_file_receipt(path))
            except OSError as exc:
                complete = False
                errors.append({'path': str(child), 'error_type': type(exc).__name__})
        return receipts, complete
    profiles, profiles_complete = scan(home / 'profiles', profile_scan=True)
    locators, locators_complete = scan(home / 'operator-evidence')
    try:
        default = _file_receipt(home / 'config.yaml')
    except OSError as exc:
        default = None
        errors.append({'path': str(home / 'config.yaml'), 'error_type': type(exc).__name__})
    # Fixed read-only Git verbs. Never inherit Git overrides or activate fsmonitor.
    env = {'PATH': os.defpath, 'HOME': str(Path.home()), 'LANG': 'C', 'LC_ALL': 'C'}
    command = ['/usr/bin/git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-C', str(repository)]
    def git(*args):
        return subprocess.check_output(command + list(args), env=env, text=True, timeout=15, stderr=subprocess.DEVNULL).strip()
    try:
        root = Path(git('rev-parse', '--show-toplevel')).resolve()
        if root != repository.resolve():
            raise ValueError('Repository root mismatch')
        head = git('rev-parse', 'HEAD')
        status = git('status', '--porcelain=v1', '--untracked-files=normal')
        after_head = git('rev-parse', 'HEAD')
        checkout = {'path': str(repository), 'verified': True, 'head': head, 'observed_head_after': after_head,
                    'head_stable': head == after_head, 'working_tree_dirty': bool(status),
                    'status_entry_count': len(status.splitlines()) if status else 0}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        checkout = {'path': str(repository), 'verified': False, 'error_type': type(exc).__name__}
    return {'schema': 'controller_runtime_source_inventory_v1', 'source': 'hermes_controller_computed_readonly',
            'observed_at': observed_at,
            'complete': bool(profiles_complete and locators_complete and default is not None
                             and not errors and checkout.get('verified') and checkout.get('head_stable')),
            'profile_glob': str(home / 'profiles' / '*' / 'config.yaml'),
            'profile_glob_complete': profiles_complete, 'profiles': profiles, 'default_config': default,
            'source_locators': locators, 'source_locators_complete': locators_complete, 'errors': errors, 'installed_checkout': checkout}
