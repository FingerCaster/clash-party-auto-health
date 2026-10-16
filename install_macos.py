#!/usr/bin/env python3
"""Install Auto Health into the current user's Clash Party profile on macOS."""
import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

import auto_health as h

HERE = Path(__file__).resolve().parent
LABEL = 'local.clash-party.auto-health'

def plist(data_dir, health_dir, python, interval):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array><string>{python}</string><string>{health_dir / 'auto_health.py'}</string><string>--data-dir</string><string>{data_dir}</string><string>--log-file</string><string>{health_dir / 'agent.log'}</string></array>
<key>RunAtLoad</key><true/><key>StartInterval</key><integer>{int(interval)}</integer>
<key>ProcessType</key><string>Background</string><key>StandardOutPath</key><string>{health_dir / 'agent.log'}</string><key>StandardErrorPath</key><string>{health_dir / 'agent-error.log'}</string>
</dict></plist>'''

def load(path):
    return h.yaml_load(Path(path).read_text(encoding='utf-8-sig'))

def main(args):
    if platform.system() != 'Darwin':
        raise RuntimeError('This installer must run on macOS.')
    data = (Path(args.data_dir).expanduser() if args.data_dir else h.APP).resolve()
    resources = (Path(args.resources_dir).expanduser() if args.resources_dir else Path('/Applications/Clash Party.app/Contents/Resources')).resolve()
    node = Path(args.node or shutil.which('node') or '')
    core = Path(args.core or resources / 'sidecar/mihomo-smart')
    for path, label in [(data / 'profile.yaml', 'profile.yaml'), (data / 'work/config.yaml', 'work/config.yaml'), (resources / 'app.asar', 'Clash Party app.asar'), (node, 'Node.js'), (core, 'mihomo core')]:
        if not path.is_file():
            raise RuntimeError(f'{label} was not found: {path}')
    if subprocess.run(['pgrep', '-f', 'Clash Party.app'], stdout=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError('Quit Clash Party completely before installation.')
    profile_path, override_path = data / 'profile.yaml', data / 'override.yaml'
    health = data / 'adaptive-auto-selection'
    original = {p: p.read_bytes() if p.exists() else None for p in [profile_path, override_path, data / 'work/config.yaml', health / 'policy.json', health / 'state.json']}
    with tempfile.TemporaryDirectory(prefix='clash-auto-health-') as temp:
        h.APP, h.HOME_DIR = data, Path(temp)
        runtime_settings = {'node_path': str(node), 'resources_dir': str(resources), 'core_path': str(core)}
        h.atomic_write(h.HOME_DIR / 'runtime.json', json.dumps(runtime_settings))
        profile, overrides, runtime = load(profile_path), load(override_path) if original[override_path] else {'items': []}, load(data / 'work/config.yaml')
        current = profile.get('current')
        if not current or sum(item.get('id') == current for item in profile.get('items', [])) != 1:
            raise RuntimeError('Import and select a subscription in Clash Party first.')
        if not runtime.get('proxies') or not any(g.get('type') == 'url-test' for g in runtime.get('proxy-groups', [])):
            raise RuntimeError('The active profile needs inline proxies and at least one url-test group.')
        old_policy = json.loads(original[health / 'policy.json']) if original[health / 'policy.json'] else None
        old_state = json.loads(original[health / 'state.json']) if original[health / 'state.json'] else None
        if old_policy and old_policy.get('profile_id') != current:
            raise RuntimeError('Existing Auto Health state belongs to another subscription.')
        policy = {**h.DEFAULTS, **(old_policy or {}), 'profile_id': current}
        state = copy.deepcopy(old_state) if old_state else {'nodes': {}, 'profile_id': current}
        selected = next(item for item in profile['items'] if item.get('id') == current)
        bound = selected.setdefault('override', [])
        if h.OVERRIDE_ID not in bound: bound.append(h.OVERRIDE_ID)
        entries = overrides.setdefault('items', [])
        entry = next((item for item in entries if item.get('id') == h.OVERRIDE_ID), None)
        if entry and (entry.get('ext') != 'js' or entry.get('global')):
            raise RuntimeError('The managed override ID conflicts with another override.')
        if entry is None:
            entries.append({'id': h.OVERRIDE_ID, 'type': 'local', 'ext': 'js', 'global': False})
            entry = entries[-1]
        entry.update(name='自动选择健康隔离与恢复', updated=int(time.time() * 1000))
        excluded = {name for name, value in state.get('nodes', {}).items() if value.get('quarantined')}
        candidate = h.patch_group_block(original[data / 'work/config.yaml'].decode('utf-8-sig'), h.transform(runtime, excluded, policy)['proxy-groups'])
        candidate_path = h.HOME_DIR / 'candidate.yaml'; h.atomic_write(candidate_path, candidate)
        check = subprocess.run([str(core), '-t', '-d', str(data / 'work'), '-f', str(candidate_path)], capture_output=True, text=True, timeout=45)
        if check.returncode: raise RuntimeError('Mihomo rejected the generated configuration.')
        files = {health / 'auto_health.py': (HERE / 'auto_health.py').read_text(encoding='utf-8'), health / 'js-yaml.cjs': h.yaml_library().read_text(encoding='utf-8'), health / 'runtime.json': json.dumps(runtime_settings, ensure_ascii=False, indent=2), health / 'policy.json': json.dumps(policy, ensure_ascii=False, indent=2), health / 'state.json': json.dumps(state, ensure_ascii=False, indent=2), health / 'installation.json': json.dumps({'profile_id': current, 'data_dir': str(data), 'installed_at': dt.datetime.now().astimezone().isoformat()}, ensure_ascii=False, indent=2), data / 'override' / (h.OVERRIDE_ID + '.js'): h.render_override(excluded, policy), override_path: h.yaml_dump(overrides), profile_path: h.yaml_dump(profile)}
    plist_path = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    files[plist_path] = plist(data, health, sys.executable, policy['interval_seconds'])
    backup = health / 'backups' / ('macos-install-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S')); backup.mkdir(parents=True, exist_ok=True)
    for path, content in original.items():
        if content is not None: h.atomic_write(backup / (path.name + '.bak'), content)
    for path, content in files.items(): h.atomic_write(path, content)
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist_path)], check=True)
    print(json.dumps({'installed': True, 'profile_id': current, 'data_dir': str(data), 'backup': str(backup), 'status': str(health / 'status.json')}, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir'); parser.add_argument('--resources-dir'); parser.add_argument('--node'); parser.add_argument('--core')
    try: main(parser.parse_args())
    except Exception as error: print('ERROR: ' + str(error), file=sys.stderr); raise SystemExit(1)
