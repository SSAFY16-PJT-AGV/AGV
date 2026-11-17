"""Finish an isolated outage test without abandoning owned child processes."""
import json
import sys


def finalize_run(result, run_dir, children, stop_process, record, *, takeover=None, had_error=False):
    """Attempt every cleanup even when another cleanup or log operation fails.

    A prior test exception remains the primary error; absent one, cleanup or
    evidence failures cause the run to fail rather than reporting success.
    """
    errors = []
    if takeover is not None:
        for operation in ('disconnect', 'loop_stop'):
            try:
                getattr(takeover, operation)()
            except Exception as exc:
                errors.append(f'takeover.{operation}: {exc!r}')
    for name in ('game', 'server', 'broker', 'indicator'):
        process = children.get(name)
        if process is not None:
            try:
                stop_process(process)
            except Exception as exc:
                errors.append(f'{name}: {exc!r}')
    try:
        if errors:
            record('cleanup_failed', errors=errors[:])
        else:
            record('stopped_test_processes')
    except Exception as exc:
        errors.append(f'cleanup event: {exc!r}')
    if errors:
        result['passed'] = False
        result['cleanup_errors'] = errors[:]
    try:
        (run_dir / 'result.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    except Exception as exc:
        errors.append(f'result file: {exc!r}')
        result['passed'] = False
        result['cleanup_errors'] = errors[:]
    if errors:
        print('Outage test cleanup/evidence error: ' + '; '.join(errors), file=sys.stderr, flush=True)
        if not had_error:
            raise RuntimeError('outage test cleanup or result recording failed')
