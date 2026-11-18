"""Compare declared robot permissions with a narrow, offline proposal.

This is not an MQTT authorization implementation. It neither reads nor changes
broker configuration and cannot determine the effective permissions of a user.
"""
import argparse
import json
import re
import sys
from pathlib import Path


_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z', re.ASCII)
_TOPIC = re.compile(r'[A-Za-z0-9_./+$#-]{1,512}\Z', re.ASCII)
_MAX_MANIFEST_BYTES = 256 * 1024


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('invalid JSON constant')


def review_robot_acl(manifest):
    """Return discrepancies in declared robot grants against the proposal.

    Fields are intentionally strict. A match proves only that the supplied
    declaration matches these example topic rules, not what a broker enforces.
    """
    if not isinstance(manifest, dict) or set(manifest) != {'robots'}:
        raise ValueError('invalid manifest')
    robots = manifest['robots']
    if not isinstance(robots, list) or not 1 <= len(robots) <= 128:
        raise ValueError('invalid robot list')

    identifiers, principals, issues = set(), set(), []
    for robot in robots:
        if not isinstance(robot, dict) or set(robot) != {'robot_id', 'principal', 'publish', 'subscribe'}:
            raise ValueError('invalid robot entry')
        robot_id, principal = robot['robot_id'], robot['principal']
        if (not isinstance(robot_id, str) or not _IDENTIFIER.fullmatch(robot_id)
                or not isinstance(principal, str) or not _IDENTIFIER.fullmatch(principal)):
            raise ValueError('invalid identifier')
        if robot_id in identifiers or principal in principals:
            raise ValueError('robot IDs and principals must be unique')
        identifiers.add(robot_id)
        principals.add(principal)
        expected = {'publish': {'agv/{}/{}'.format(robot_id, suffix)
                                for suffix in ('state', 'event', 'conn')},
                    'subscribe': {'agv/{}/order'.format(robot_id)}}
        for direction in ('publish', 'subscribe'):
            topics = robot[direction]
            if (not isinstance(topics, list) or len(topics) > 128
                    or any(not isinstance(topic, str) or not _TOPIC.fullmatch(topic) for topic in topics)
                    or len(topics) != len(set(topics))):
                raise ValueError('invalid topic list')
            actual = set(topics)
            for problem, difference in (('missing', expected[direction] - actual),
                                        ('unexpected', actual - expected[direction])):
                issues.extend({'robot_id': robot_id, 'direction': direction, 'problem': problem,
                               'topic': topic} for topic in sorted(difference))
    return issues


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline comparison of a proposed robot topic matrix')
    parser.add_argument('manifest', help='path to a JSON proposal (no credentials)')
    args = parser.parse_args(argv)
    try:
        with Path(args.manifest).open('rb') as source:
            data = source.read(_MAX_MANIFEST_BYTES + 1)
        if len(data) > _MAX_MANIFEST_BYTES:
            raise ValueError('manifest too large')
        manifest = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_pairs,
                              parse_constant=_reject_constant)
        issues = review_robot_acl(manifest)
    except (OSError, UnicodeError, ValueError, RecursionError):
        print(json.dumps({'error': 'invalid proposal'}), file=sys.stderr)
        return 2
    print(json.dumps({'scope': 'offline_proposal_only', 'matches_proposal': not issues,
                      'issues': issues}, ensure_ascii=True))
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
