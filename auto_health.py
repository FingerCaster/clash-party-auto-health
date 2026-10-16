#!/usr/bin/env python3
"""Local Mihomo health quarantine with persistent Clash Party override and recovery."""
import argparse
import concurrent.futures
import contextlib
import copy
import datetime as dt
import glob
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import struct
import sys
import tempfile
import time
import urllib.parse

IS_WINDOWS = os.name == 'nt'
if IS_WINDOWS:
    import msvcrt
else:
    import fcntl

APP = (Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming'))) / 'mihomo-party'
       if IS_WINDOWS else Path.home() / 'Library/Application Support/mihomo-party')
HOME_DIR = APP / 'adaptive-auto-selection'
OVERRIDE_ID = 'adaptive-auto-selection'
MARKER = '|(?<auto_health_quarantine>'
DEFAULTS = {
    'profile_id': None,
    'test_url': 'https://www.gstatic.com/generate_204',
    'timeout_ms': 3000, 'interval_seconds': 60, 'tolerance_ms': 80,
    'max_failed_times': 2, 'window_size': 10, 'min_samples': 8,
    'failed_samples': 3, 'consecutive_failures': 3,
    'recovery_interval_seconds': 120, 'recovery_successes': 3,
    'probe_workers': 6,
}


def runtime_settings():
    path = HOME_DIR / 'runtime.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def node_binary():
    configured = runtime_settings().get('node_path')
    candidates = [configured, shutil.which('node'), '/opt/homebrew/bin/node', '/usr/local/bin/node']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError('Node.js not found; install Node.js or reinstall Auto Health')


def resources_dir():
    configured = runtime_settings().get('resources_dir')
    if configured:
        return Path(configured)
    if IS_WINDOWS:
        raise RuntimeError('Windows runtime.json is missing; run install_windows.ps1 first')
    return Path('/Applications/Clash Party.app/Contents/Resources')


def core_binary():
    configured = runtime_settings().get('core_path')
    return str(configured or resources_dir() / 'sidecar/mihomo-smart')


def child_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}


@contextlib.contextmanager
def process_lock():
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    with (HOME_DIR / 'run.lock').open('a+b') as lock:
        if IS_WINDOWS and lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        acquired = False
        try:
            if IS_WINDOWS:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            if error.errno not in (11, 13, 35, 36):
                raise
        try:
            yield acquired
        finally:
            if acquired:
                lock.seek(0)
                if IS_WINDOWS:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)


def stamp(value):
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 0


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data.encode('utf-8') if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def yaml_library():
    # Use the same YAML implementation shipped with the installed Clash Party.
    # Ruby/Psych treats unquoted ::/0 as a Ruby symbol and loses one colon.
    target = HOME_DIR / 'js-yaml.cjs'
    if target.exists():
        return target
    asar = resources_dir() / 'app.asar'
    with asar.open('rb') as file:
        sizes = struct.unpack('<4I', file.read(16))
        tree = json.loads(file.read(sizes[3]).decode())
        node = tree
        for part in 'node_modules/js-yaml/dist/js-yaml.cjs.js'.split('/'):
            node = node['files'][part]
        file.seek(8 + sizes[1] + int(node['offset']))
        content = file.read(node['size']).decode()
    atomic_write(target, content)
    return target


def yaml_load(text):
    code = "const fs=require('node:fs'),y=require(process.argv[1]);process.stdout.write(JSON.stringify(y.load(fs.readFileSync(0,'utf8'))));"
    return json.loads(subprocess.check_output(
        [node_binary(), '-e', code, str(yaml_library())], input=text, text=True,
        encoding='utf-8', **child_options()))


def yaml_dump(obj):
    code = "const fs=require('node:fs'),y=require(process.argv[1]);process.stdout.write(y.dump(JSON.parse(fs.readFileSync(0,'utf8')),{lineWidth:-1,noRefs:true}));"
    return subprocess.check_output(
        [node_binary(), '-e', code, str(yaml_library())], input=json.dumps(obj), text=True,
        encoding='utf-8', **child_options())


def escaped_name(name):
    special = set('\\.^$|?*+()[]{}')
    return ''.join('\\' + c if c in special else c for c in name)


def base_exclusion(value):
    text = str(value or '')
    base = text.split(MARKER, 1)[0]
    return '' if MARKER in text and base == 'a^' else base


def transform(config, excluded, policy):
    result = copy.deepcopy(config)
    physical = {p['name'] for p in result.get('proxies', [])}
    blocked = sorted(set(excluded) & physical)
    suffix = MARKER + '^(?:' + '|'.join(escaped_name(n) for n in blocked) + ')$)' if blocked else ''
    for group in result.get('proxy-groups', []):
        if group.get('type') != 'url-test':
            continue
        base = base_exclusion(group.get('exclude-filter'))
        if suffix:
            # A nonempty never-matching prefix avoids an empty regex alternative.
            group['exclude-filter'] = (base or 'a^') + suffix
        elif base:
            group['exclude-filter'] = base
        else:
            group.pop('exclude-filter', None)
        group.update({'tolerance': policy['tolerance_ms'],
                      'interval': policy['interval_seconds'],
                      'timeout': policy['timeout_ms'], 'lazy': False,
                      'max-failed-times': policy['max_failed_times'],
                      'url': policy['test_url'], 'expected-status': '204',
                      'empty-fallback': 'REJECT'})
    return result


def render_override(excluded, policy):
    header = (
        '// 自动生成的运行快照：后台每轮按检测结果重新写入。\n'
        '// CURRENT_QUARANTINE 中的节点是当前暂时隔离、禁止进入自动组的节点。\n'
        '// 名单不是可用节点推荐或永久黑名单；恢复检测达标后会自动移除。\n'
        '// 未隔离的节点也持续检测；恢复过的节点再次失败仍会重新隔离。\n'
        '// 请修改 policy.json 调整规则，不要手改下面的运行快照。\n'
    )
    snapshot = {
        'generatedAt': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
        'nodes': sorted(excluded),
    }
    rules = json.dumps(policy, ensure_ascii=False, indent=2)
    current = json.dumps(snapshot, ensure_ascii=False, indent=2)
    return (header + 'const AUTO_HEALTH_RULES = ' + rules + ';\n'
            + 'const AUTO_HEALTH_CURRENT_QUARANTINE = ' + current + ';\n') + r'''
function main(config) {
  if (!config || !Array.isArray(config['proxy-groups'])) return config;
  const physical = new Set((config.proxies || []).map(p => p.name));
  const special = new Set('\\.^$|?*+()[]{}'.split(''));
  const escape = name => Array.from(name, c => special.has(c) ? '\\' + c : c).join('');
  const blocked = AUTO_HEALTH_CURRENT_QUARANTINE.nodes.filter(name => physical.has(name));
  const marker = '|(?<auto_health_quarantine>';
  const suffix = blocked.length ? marker + '^(?:' + blocked.map(escape).join('|') + ')$)' : '';
  const p = AUTO_HEALTH_RULES;
  for (const group of config['proxy-groups']) {
    if (group.type !== 'url-test') continue;
    const previous = String(group['exclude-filter'] || '');
    const prefix = previous.split(marker)[0];
    const base = previous.includes(marker) && prefix === 'a^' ? '' : prefix;
    if (suffix) group['exclude-filter'] = (base || 'a^') + suffix;
    else if (base) group['exclude-filter'] = base;
    else delete group['exclude-filter'];
    Object.assign(group, {
      tolerance: p.tolerance_ms, interval: p.interval_seconds,
      timeout: p.timeout_ms, lazy: false, 'max-failed-times': p.max_failed_times,
      url: p.test_url, 'expected-status': '204', 'empty-fallback': 'REJECT'
    });
  }
  return config;
}
'''


def observe(entry, history, policy, now):
    last_seen = entry.get('last_seen', 0)
    fresh = [h for h in history if stamp(h.get('time')) > last_seen]
    fresh.sort(key=lambda h: stamp(h['time']))
    if not fresh:
        return False
    samples = entry.setdefault('samples', [])
    for item in fresh:
        ts = stamp(item['time'])
        samples.append({'time': ts, 'ok': item.get('delay', 0) > 0})
        entry['last_seen'] = max(entry.get('last_seen', 0), ts)
    entry['samples'] = samples[-policy['window_size']:]
    if entry.get('quarantined'):
        return False
    samples = entry['samples']
    consecutive = len(samples) >= policy['consecutive_failures'] and all(
        not x['ok'] for x in samples[-policy['consecutive_failures']:])
    frequent = len(samples) >= policy['min_samples'] and sum(
        not x['ok'] for x in samples) >= policy['failed_samples']
    if consecutive or frequent:
        entry.update(quarantined=True, quarantined_at=now,
                     recovery_streak=0, next_probe=now)
        return True
    return False


def recover(entry, passed, policy, now):
    entry['recovery_streak'] = entry.get('recovery_streak', 0) + 1 if passed else 0
    entry['last_probe'] = now
    entry['next_probe'] = now + policy['recovery_interval_seconds']
    if entry['recovery_streak'] >= policy['recovery_successes']:
        entry.update(quarantined=False, recovered_at=now, recovery_streak=0,
                     samples=[], last_seen=now)
        return True
    return False


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


NODE_IPC_CLIENT = r'''
const http = require('node:http');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const payload = input.body == null ? '' : JSON.stringify(input.body);
const req = http.request({socketPath: input.socket, path: input.path,
  method: input.method, headers: {'Content-Type': 'application/json',
  'Content-Length': Buffer.byteLength(payload)}}, res => {
  const chunks = [];
  res.on('data', chunk => chunks.push(chunk));
  res.on('error', err => req.destroy(err));
  res.on('end', () => {
    clearTimeout(timer);
    if (res.statusCode >= 400) {
      process.stderr.write('Mihomo HTTP ' + res.statusCode); process.exitCode = 1;
    } else process.stdout.write(Buffer.concat(chunks).toString('utf8') || '{}');
  });
});
const timer = setTimeout(() => req.destroy(new Error('IPC timeout')), input.timeout_ms);
req.on('error', err => {
  clearTimeout(timer); process.stderr.write(err.code || err.message); process.exitCode = 1;
});
req.end(payload);
'''


def node_ipc_api(sock, path, method='GET', body=None, timeout=10):
    result = subprocess.run([node_binary(), '-e', NODE_IPC_CLIENT],
        input=json.dumps({'socket': sock, 'path': path, 'method': method, 'body': body,
                          'timeout_ms': max(1, int(timeout * 1000))}),
        text=True, encoding='utf-8', capture_output=True, timeout=timeout + 3,
        **child_options())
    if result.returncode:
        raise RuntimeError('Mihomo IPC failed: ' + result.stderr.strip()[:160])
    return json.loads(result.stdout)


def api(sock, path, method='GET', body=None, timeout=10):
    if IS_WINDOWS:
        return node_ipc_api(sock, path, method, body, timeout)
    connection = UnixConnection(sock, timeout)
    try:
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        content = response.read()
        if response.status >= 400:
            raise RuntimeError('Mihomo HTTP ' + str(response.status))
        return json.loads(content) if content else {}
    finally:
        connection.close()


def split_windows_commandline(command):
    import ctypes
    from ctypes import wintypes
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    argc = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        raise OSError(ctypes.get_last_error(), 'Unable to parse core command line')
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        kernel.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))


def windows_core_commands():
    command = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$p = @(Get-CimInstance Win32_Process -Filter \"Name='mihomo.exe' OR "
        "Name='mihomo-alpha.exe' OR Name='mihomo-smart.exe'\" | "
        "Select-Object -ExpandProperty CommandLine); ConvertTo-Json -InputObject $p -Compress")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        capture_output=True, text=True, encoding='utf-8-sig', timeout=15, **child_options())
    if result.returncode:
        raise RuntimeError('Cannot inspect the Windows Mihomo process')
    return json.loads(result.stdout or '[]')


def controller_from_args(args, workdir, normalise=os.path.normcase):
    try:
        configured = args[args.index('-d') + 1]
        pipe = args[args.index('-ext-ctl-pipe') + 1]
    except (ValueError, IndexError):
        return None
    return pipe if normalise(configured.rstrip('/\\')) == normalise(str(workdir).rstrip('/\\')) else None


def find_core():
    if IS_WINDOWS:
        paths = set()
        for command in windows_core_commands():
            if not command:
                continue
            pipe = controller_from_args(split_windows_commandline(command), APP / 'work')
            if pipe:
                paths.add(pipe)
        if len(paths) > 1:
            raise RuntimeError('Multiple cores use this profile directory; refusing ambiguous controller')
        for pipe in paths:
            try:
                api(pipe, '/version', timeout=3)
                return pipe
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                pass
        return None
    paths = sorted(glob.glob('/tmp/mihomo-party-' + str(os.getuid()) + '-*.sock'),
                   key=os.path.getmtime, reverse=True)
    for path in paths:
        try:
            api(path, '/version', timeout=2)
            return path
        except (OSError, RuntimeError, ValueError):
            pass
    return None


def probe(sock, name, policy):
    query = urllib.parse.urlencode({'url': policy['test_url'],
                                   'timeout': policy['timeout_ms'], 'expected': '204'})
    try:
        response = api(sock, '/proxies/' + urllib.parse.quote(name, safe='') + '/delay?' + query,
                       timeout=policy['timeout_ms'] / 1000 + 3)
        return response.get('delay', 0) > 0
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return False


def patch_group_block(text, groups):
    block = yaml_dump({'proxy-groups': groups})
    if block.startswith('---\n'):
        block = block[4:]
    match = re.search(r'(?ms)^proxy-groups:[^\n]*\n.*?(?=^[A-Za-z][A-Za-z0-9_-]*:|\Z)', text)
    if not match:
        raise RuntimeError('proxy-groups block not found')
    patched = text[:match.start()] + block + text[match.end():]
    before, after = yaml_load(text), yaml_load(patched)
    expected = copy.deepcopy(before)
    expected['proxy-groups'] = groups
    if after != expected:
        raise RuntimeError('Unexpected non-group change')
    return patched


def event(message, **fields):
    print(json.dumps({'time': dt.datetime.now().isoformat(timespec='seconds'),
                      'event': message, **fields}, ensure_ascii=False), flush=True)


def apply_rules(excluded, policy, sock):
    generated = render_override(excluded, policy)
    script_path = APP / 'override' / (OVERRIDE_ID + '.js')
    previous_script = script_path.read_text(encoding='utf-8') if script_path.exists() else None
    runtime_path = APP / 'work/config.yaml'
    before_text = runtime_path.read_text(encoding='utf-8')
    before = yaml_load(before_text)
    after = transform(before, excluded, policy)
    changed = after != before
    replacement = patch_group_block(before_text, after['proxy-groups']) if changed else before_text
    if changed:
        candidate = HOME_DIR / 'candidate.yaml'
        atomic_write(candidate, replacement)
        core = core_binary()
        validation = subprocess.run([core, '-t', '-d', str(APP / 'work'), '-f', str(candidate)],
                                    capture_output=True, text=True, encoding='utf-8', timeout=20,
                                    **child_options())
        if validation.returncode:
            raise RuntimeError('Candidate config rejected by core')
        # Refuse to overwrite a concurrent subscription switch/update.
        profile = yaml_load((APP / 'profile.yaml').read_text(encoding='utf-8'))
        if profile.get('current') != policy['profile_id'] or runtime_path.read_text(encoding='utf-8') != before_text:
            event('defer_concurrent_profile_update')
            return False
    if previous_script != generated:
        atomic_write(script_path, generated)
    if changed:
        atomic_write(runtime_path, replacement)
        try:
            api(sock, '/configs', 'PUT', {'path': str(runtime_path)}, timeout=30)
        except Exception:
            atomic_write(runtime_path, before_text)
            if previous_script is not None:
                atomic_write(script_path, previous_script)
            api(sock, '/configs', 'PUT', {'path': str(runtime_path)}, timeout=30)
            raise
        event('rules_applied', quarantined=len(excluded))
    return True


def run_once(force_probe=False):
    policy = {**DEFAULTS, **json.loads((HOME_DIR / 'policy.json').read_text(encoding='utf-8'))}
    profile = yaml_load((APP / 'profile.yaml').read_text(encoding='utf-8'))
    if profile.get('current') != policy['profile_id']:
        event('inactive_profile')
        return
    sock = find_core()
    if not sock:
        event('core_not_running')
        return
    data = api(sock, '/proxies')['proxies']
    runtime = yaml_load((APP / 'work/config.yaml').read_text(encoding='utf-8'))
    names = {p['name'] for p in runtime.get('proxies', [])}
    eligible = set()
    for g in runtime.get('proxy-groups', []):
        if g.get('type') == 'url-test':
            eligible.update(data.get(g['name'], {}).get('all', []))
    state_path = HOME_DIR / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'nodes': {}}
    nodes = state.setdefault('nodes', {})
    # Track existing automatic candidates plus previously quarantined nodes.
    now = time.time()
    for name in (eligible | set(nodes)) & names:
        proxy = data.get(name)
        if not proxy:
            continue
        entry = nodes.setdefault(name, {})
        history = proxy.get('extra', {}).get(policy['test_url'], {}).get('history', [])
        if observe(entry, history, policy, now):
            event('quarantine', node=name)
    due = [n for n, e in nodes.items() if n in names and e.get('quarantined') and
           (force_probe or e.get('next_probe', 0) <= now)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=policy['probe_workers']) as pool:
        futures = {pool.submit(probe, sock, n, policy): n for n in due}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            if recover(nodes[name], future.result(), policy, time.time()):
                event('recover', node=name)
    # Forget subscriptions removed for >7 days; retain brief disappearances.
    for name in list(nodes):
        if name in names:
            nodes[name]['present_at'] = now
        elif now - nodes[name].get('present_at', now) > 7 * 86400:
            del nodes[name]
    excluded = {n for n, e in nodes.items() if e.get('quarantined') and n in names}
    state.update(updated_at=now, profile_id=policy['profile_id'])
    atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2))
    applied = apply_rules(excluded, policy, sock)
    snapshot = {'updated_at': dt.datetime.now().isoformat(timespec='seconds'),
                'profile_id': policy['profile_id'], 'quarantined_count': len(excluded),
                'recovery_probes': len(due), 'rules_applied': applied,
                'recovering': {n: e.get('recovery_streak', 0) for n, e in nodes.items()
                               if n in excluded and e.get('recovery_streak', 0)}}
    atomic_write(HOME_DIR / 'status.json', json.dumps(snapshot, ensure_ascii=False, indent=2))
    event('cycle_complete', **{k: v for k, v in snapshot.items() if k != 'recovering'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force-probe', action='store_true', help='Run quarantined node probes now')
    parser.add_argument('--data-dir', type=Path, help='Clash Party data directory (portable installs)')
    parser.add_argument('--log-file', type=Path, help='Append UTF-8 logs when launched by pythonw')
    args = parser.parse_args()
    if args.data_dir:
        APP = args.data_dir.resolve()
        HOME_DIR = APP / 'adaptive-auto-selection'
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = args.log_file.open('a', encoding='utf-8', buffering=1)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    with process_lock() as acquired:
        if not acquired:
            raise SystemExit(0)
        try:
            run_once(args.force_probe)
        except Exception as exc:
            event('cycle_error', error=type(exc).__name__ + ': ' + str(exc))
            raise SystemExit(1)
