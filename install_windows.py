#!/usr/bin/env python3
"""Install the Auto Health daemon for the current Windows Clash Party profile."""
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import auto_health as h

HERE = Path(__file__).resolve().parent


def choose_app(explicit=None):
    candidates = [Path(explicit)] if explicit else []
    if not explicit:
        roots = [Path(os.environ.get(k, '')) for k in ['LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)'] if os.environ.get(k)]
        for root in roots:
            for name in ['Clash Party', 'mihomo-party', 'Mihomo Party', 'clash-party']:
                candidates.extend([root / name, root / 'Programs' / name])
        import winreg
        for hive in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
            for view in [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]:
                try:
                    with winreg.OpenKey(hive, r'Software\Microsoft\Windows\CurrentVersion\Uninstall', 0, winreg.KEY_READ | view) as key:
                        for index in range(winreg.QueryInfoKey(key)[0]):
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as item:
                                try:
                                    name = winreg.QueryValueEx(item, 'DisplayName')[0].lower()
                                    if 'clash party' not in name and 'mihomo party' not in name:
                                        continue
                                    for value in ['InstallLocation', 'DisplayIcon']:
                                        try:
                                            p = str(winreg.QueryValueEx(item, value)[0]).strip('"').split(',')[0].strip('"')
                                            candidates.append(Path(p).parent if p.lower().endswith('.exe') else Path(p))
                                        except OSError:
                                            pass
                                except OSError:
                                    pass
                except OSError:
                    pass
    found = {str(p.resolve()): p.resolve() for p in candidates if (p / 'resources/app.asar').is_file()}
    if len(found) != 1:
        raise RuntimeError('Cannot uniquely find Clash Party. Pass -AppDir with its installation folder.')
    return next(iter(found.values()))


def prepare_metadata(profile, overrides, runtime, old_policy=None, old_state=None):
    profile, overrides = copy.deepcopy(profile), copy.deepcopy(overrides)
    current = profile.get('current')
    items = [p for p in profile.get('items', []) if p.get('id') == current]
    if not current or len(items) != 1:
        raise RuntimeError('Import a subscription and select it in Clash Party first.')
    if not any(g.get('type') == 'url-test' for g in runtime.get('proxy-groups', [])):
        raise RuntimeError('The current profile needs at least one url-test automatic group.')
    if not runtime.get('proxies'):
        raise RuntimeError('This implementation requires inline proxies; provider-only profiles are not supported.')
    if old_policy and old_policy.get('profile_id') != current:
        raise RuntimeError('Auto Health is already bound to another profile. Select that profile before reinstalling.')
    if old_state and old_state.get('profile_id') not in (None, current):
        raise RuntimeError('The state belongs to another profile; do not copy state.json from another computer.')
    policy = {**h.DEFAULTS, **(old_policy or {}), 'profile_id': current}
    state = copy.deepcopy(old_state) if old_state is not None else {'nodes': {}, 'profile_id': current}
    selected = items[0]
    bound = selected.setdefault('override', [])
    if not isinstance(bound, list):
        raise RuntimeError('Unsupported profile override format.')
    if h.OVERRIDE_ID not in bound:
        bound.append(h.OVERRIDE_ID)
    entries = overrides.setdefault('items', [])
    entry = next((x for x in entries if x.get('id') == h.OVERRIDE_ID), None)
    if entry and (entry.get('ext') != 'js' or entry.get('global')):
        raise RuntimeError('The managed override ID conflicts with an existing override.')
    if entry is None:
        entry = {'id': h.OVERRIDE_ID, 'type': 'local', 'ext': 'js', 'global': False}
        entries.append(entry)
    entry.update(name='自动选择健康隔离与恢复', updated=int(time.time() * 1000))
    excluded = {n for n, value in state.get('nodes', {}).items() if value.get('quarantined')}
    return profile, overrides, policy, state, excluded


def run_task(action, task_name, **fields):
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
               '-File', str(HERE / 'task_windows.ps1'), '-Action', action, '-TaskName', task_name]
    for key, value in fields.items():
        command.extend(['-' + key, str(value)])
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8-sig',
                            timeout=45, **h.child_options())
    if result.returncode:
        raise RuntimeError('Windows scheduled task action failed: ' + action + '. ' + result.stderr.strip()[-500:])
    return result.stdout.strip()


def app_is_running():
    code = ("$p = @(Get-Process -Name 'Clash Party','Mihomo Party','mihomo-party' -ErrorAction SilentlyContinue); "
            "if ($p.Count -gt 0) { exit 9 }")
    p = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', code],
                       capture_output=True, timeout=15, **h.child_options())
    if p.returncode not in (0, 9):
        raise RuntimeError('Cannot check whether Clash Party has exited.')
    return p.returncode == 9


def transact(files, task_name, task_fields, backup_root):
    before = {path: path.read_bytes() if path.exists() else None for path in files}
    old_task = run_task('Export', task_name)
    backup = backup_root / ('windows-install-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True, mode=0o700)
    manifest = []
    for index, (path, content) in enumerate(before.items()):
        saved = str(index) + '.bin'
        if content is not None:
            (backup / saved).write_bytes(content)
        manifest.append({'path': str(path), 'backup': saved if content is not None else None})
    h.atomic_write(backup / 'files.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    if old_task:
        h.atomic_write(backup / 'task.xml', old_task)
    attempted = []
    try:
        for path, content in files.items():
            if (path.read_bytes() if path.exists() else None) != before[path]:
                raise RuntimeError('A configuration changed during installation. Close Clash Party and retry.')
            attempted.append(path)
            h.atomic_write(path, content)
        run_task('Register', task_name, **task_fields)
        if not run_task('Export', task_name):
            raise RuntimeError('Scheduled task readback was empty.')
        for path, content in files.items():
            if path.read_text(encoding='utf-8') != content:
                raise RuntimeError('Installed file readback failed: ' + path.name)
    except Exception as error:
        rollback_errors = []
        for path in reversed(attempted):
            try:
                if before[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    h.atomic_write(path, before[path])
            except Exception as rollback:
                rollback_errors.append(type(rollback).__name__)
        try:
            if old_task:
                run_task('Restore', task_name, XmlFile=backup / 'task.xml')
            else:
                run_task('Remove', task_name)
        except Exception as rollback:
            rollback_errors.append(type(rollback).__name__)
        if rollback_errors:
            raise RuntimeError('Installation failed; inspect rollback backup: ' + str(backup)) from error
        raise RuntimeError('Installation failed and changes were rolled back. ' + str(error)) from error
    return backup


def main(args):
    if os.name != 'nt':
        raise RuntimeError('This installer must run on the target Windows computer.')
    app = choose_app(args.app_dir)
    data = Path(args.data_dir) if args.data_dir else (app / 'data' if (app / 'PORTABLE').exists() else h.APP)
    data = data.resolve()
    if not (data / 'profile.yaml').is_file() or not (data / 'work/config.yaml').is_file():
        raise RuntimeError('Profile files not found. Import/select a subscription first, or pass -DataDir.')
    node = Path(args.node or shutil.which('node') or '')
    if not node.is_file():
        raise RuntimeError('Install Node.js LTS and reopen PowerShell, or pass -NodePath.')
    node = node.resolve()
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise RuntimeError('pythonw.exe not found beside Python. Use a standard Windows Python installation.')
    live_home = data / 'adaptive-auto-selection'
    profile_path, override_path = data / 'profile.yaml', data / 'override.yaml'
    inputs = [profile_path, override_path, data / 'work/config.yaml', live_home / 'policy.json', live_home / 'state.json']
    original = {p: p.read_bytes() if p.exists() else None for p in inputs}
    with tempfile.TemporaryDirectory(prefix='clash-auto-health-') as temporary:
        h.APP, h.HOME_DIR = data, Path(temporary)
        runtime_settings = {'node_path': str(node), 'resources_dir': str(app / 'resources')}
        h.atomic_write(h.HOME_DIR / 'runtime.json', json.dumps(runtime_settings))
        profile = h.yaml_load(original[profile_path].decode('utf-8-sig'))
        overrides = h.yaml_load(original[override_path].decode('utf-8-sig')) if original[override_path] else {'items': []}
        runtime = h.yaml_load(original[data / 'work/config.yaml'].decode('utf-8-sig'))
        app_config = h.yaml_load((data / 'config.yaml').read_text(encoding='utf-8-sig')) if (data / 'config.yaml').exists() else {}
        core_name = args.core or app_config.get('core', 'mihomo')
        if core_name not in ('mihomo', 'mihomo-alpha', 'mihomo-smart'):
            raise RuntimeError('Unsupported core selection. Pass -Core mihomo, mihomo-alpha, or mihomo-smart.')
        core = app / 'resources/sidecar' / (core_name + '.exe')
        if not core.is_file():
            raise RuntimeError('Selected core executable was not found: ' + core.name)
        runtime_settings['core_path'] = str(core)
        old_policy = json.loads(original[live_home / 'policy.json']) if original[live_home / 'policy.json'] else None
        old_state = json.loads(original[live_home / 'state.json']) if original[live_home / 'state.json'] else None
        profile, overrides, policy, state, excluded = prepare_metadata(profile, overrides, runtime, old_policy, old_state)
        running = app_is_running()
        task_name = 'ClashParty-AutoHealth-' + hashlib.sha256(str(data).lower().encode('utf-8')).hexdigest()[:10]
        report = {'profile_id': policy['profile_id'], 'data_dir': str(data), 'app_dir': str(app),
                  'node': str(node), 'python': sys.executable, 'core': core_name, 'task_name': task_name,
                  'app_running': running, 'fresh_state': old_state is None}
        if not args.install:
            print(json.dumps(dict(report, check='passed', next='Exit Clash Party, then run install_windows.ps1 -Install'), ensure_ascii=True, indent=2))
            return
        if running:
            raise RuntimeError('Exit Clash Party from its system tray menu before installation. Closing the window is not enough.')
        if h.find_core():
            raise RuntimeError('A core is still running for this data directory. Fully exit Clash Party before installation.')
        candidate = h.HOME_DIR / 'candidate.yaml'
        h.atomic_write(candidate, h.patch_group_block(original[data / 'work/config.yaml'].decode('utf-8-sig'), h.transform(runtime, excluded, policy)['proxy-groups']))
        test = subprocess.run([str(core), '-t', '-d', str(data / 'work'), '-f', str(candidate)],
                              capture_output=True, timeout=45, **h.child_options())
        if test.returncode:
            raise RuntimeError('Mihomo rejected the candidate configuration; no profile files were changed.')
        files = {
            live_home / 'auto_health.py': (HERE / 'auto_health.py').read_text(encoding='utf-8'),
            live_home / 'js-yaml.cjs': h.yaml_library().read_text(encoding='utf-8'),
            live_home / 'runtime.json': json.dumps(runtime_settings, ensure_ascii=False, indent=2),
            live_home / 'policy.json': json.dumps(policy, ensure_ascii=False, indent=2),
            live_home / 'state.json': json.dumps(state, ensure_ascii=False, indent=2),
            data / 'override' / (h.OVERRIDE_ID + '.js'): h.render_override(excluded, policy),
            override_path: h.yaml_dump(overrides), profile_path: h.yaml_dump(profile),
            live_home / 'windows-installation.json': json.dumps(dict(report, installed_at=dt.datetime.now().astimezone().isoformat()), ensure_ascii=False, indent=2),
        }
    h.APP, h.HOME_DIR = data, live_home
    with h.process_lock() as acquired:
        if not acquired:
            raise RuntimeError('Auto Health is busy. Wait for the current cycle to finish and rerun the installer.')
        if any((p.read_bytes() if p.exists() else None) != content for p, content in original.items()):
            raise RuntimeError('Profile/state changed during preflight. Retry installation.')
        backup = transact(files, task_name, {'Pythonw': pythonw, 'Daemon': live_home / 'auto_health.py',
            'DataDir': data, 'LogFile': live_home / 'agent.log'}, live_home / 'backups')
    print(json.dumps(dict(report, installed=True, backup=str(backup), next='Start Clash Party and wait 60-90 seconds, then check status.json'), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--install', action='store_true')
    p.add_argument('--app-dir')
    p.add_argument('--data-dir')
    p.add_argument('--node')
    p.add_argument('--core')
    try:
        main(p.parse_args())
    except Exception as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
