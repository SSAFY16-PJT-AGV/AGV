"""Recheck two reviewed commits on a private loopback broker and temporary DB.

This runs the other branch's *existing tests* in disposable Git archive copies.
It never enables a physical runtime, connects to a device, or approves motion.
The two audited commit IDs are deliberately pinned; changes require review.
"""
import argparse
import io
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
import sysconfig
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[2]
REVIEW_COMMIT = 'ce5335736f75927cad4de02fe166de1c37ad7d1f'
ROBOT_COMMIT = '7e6e6bb741d35a490d8c14d56cb92d2544c4976b'
TESTS = (
    'server.tests.test_physical_reconcile_exchange.PhysicalReconcileExchangeTests',
    'server.tests.test_physical_reconcile_mqtt.ReconcileMqttTests.test_separate_topics_route_valid_signed_exchange_only',
    'server.tests.test_physical_reconcile_mqtt.ReconcileMqttTests.test_retained_and_wrong_topic_cannot_trigger_a_commit',
    'server.tests.test_physical_reconcile_team_mqtt.TeamReconcileMqttTests',
)


def parse_mosquitto_version(output):
    """Mosquitto 2 defaults to localhost when started with only -p."""
    match = re.search(r'mosquitto version\s+(\d+)\.(\d+)\.(\d+)', output, re.I)
    if not match:
        raise ValueError('cannot establish the Mosquitto version')
    version = tuple(int(part) for part in match.groups())
    if version[0] != 2:
        raise ValueError('Mosquitto 2 is required for its reviewed loopback-only default')
    return version


def extract_snapshot(raw, target):
    """Reject links and traversal before writing even the first entry."""
    target = Path(target)
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as tar:
        members = tar.getmembers()
        for item in members:
            posix, windows = PurePosixPath(item.name), PureWindowsPath(item.name)
            if (not item.name or posix.is_absolute() or windows.is_absolute()
                    or windows.drive or '..' in posix.parts or '..' in windows.parts
                    or not (item.isdir() or item.isfile())):
                raise ValueError('unexpected path or link in source archive')
        tar.extractall(target, members=members, filter='data')


def _archive(commit):
    result = subprocess.run(['git', 'archive', '--format=tar', commit], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return result.stdout


def ensure_reviewed_refs(repo, refs):
    """A moved or absent local tracking ref requires a fresh review."""
    for ref, expected in refs.items():
        result = subprocess.run(['git', 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}'],
                                cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', errors='replace')
        if result.returncode or result.stdout.strip() != expected:
            raise ValueError('reviewed Git ref is missing or changed: ' + ref)


def run(mosquitto, *, temp_parent=None):
    broker = Path(mosquitto).resolve(strict=True)
    if not broker.is_file():
        raise ValueError('Mosquitto executable is required')
    version = subprocess.run([str(broker), '-h'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding='utf-8', errors='replace', timeout=10, check=True)
    parsed_version = parse_mosquitto_version(version.stdout)
    ensure_reviewed_refs(ROOT, {
        'refs/remotes/origin/feature/physical-review-safeguards': REVIEW_COMMIT,
        'refs/remotes/origin/feature/field-run': ROBOT_COMMIT,
    })
    # All executables, source snapshots, sockets, and databases belong solely
    # to these tests. No inherited AGV/MQTT settings or proxies are used.
    with tempfile.TemporaryDirectory(prefix='agv-physical-compat-', dir=temp_parent) as folder:
        root = Path(folder)
        review, team = root / 'review', root / 'team'
        review.mkdir(); team.mkdir()
        extract_snapshot(_archive(REVIEW_COMMIT), review)
        extract_snapshot(_archive(ROBOT_COMMIT), team)
        inherited = {key: value for key, value in os.environ.items()
                     if not key.upper().startswith(('AGV_', 'MQTT_'))
                     and not key.upper().endswith('_PROXY')}
        inherited.update({'AGV_FIELD_REPO': str(team), 'MOSQUITTO_EXE': str(broker),
                          'PYTHONPATH': os.pathsep.join((str(review), sysconfig.get_paths()['purelib'])),
                          'PYTHON_DOTENV_DISABLED': '1', 'PYTHONUTF8': '1',
                          'TEMP': str(root), 'TMP': str(root), 'NO_PROXY': '127.0.0.1,localhost'})
        command = [sys.executable, '-W', 'error::ResourceWarning', '-m', 'unittest', *TESTS, '-v']
        completed = subprocess.run(command, cwd=review, env=inherited, timeout=180)
        if completed.returncode:
            raise RuntimeError('isolated compatibility tests failed: exit {}'.format(completed.returncode))
    return {'server_review_commit': REVIEW_COMMIT, 'team_robot_commit': ROBOT_COMMIT,
            'mosquitto_version': '.'.join(map(str, parsed_version)),
            'motion_authorized': False, 'physical_safety_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mosquitto', required=True, help='local Mosquitto 2 executable')
    parser.add_argument('--temp-parent', help='existing parent for disposable test files')
    args = parser.parse_args()
    result = run(args.mosquitto, temp_parent=args.temp_parent)
    print('review={} robot={} mosquitto={} motion_authorized=false'.format(
        result['server_review_commit'], result['team_robot_commit'], result['mosquitto_version']))


if __name__ == '__main__':
    main()
