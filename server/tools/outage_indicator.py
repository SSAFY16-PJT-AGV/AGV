"""Visible status for a test-owned isolated AGV communication experiment.

Usage: python outage_indicator.py STATUS_JSON ACK_JSON
The parent atomically replaces STATUS_JSON with a new monotonically numbered
event. This process atomically writes a ready/shown/closed/error acknowledgement
to ACK_JSON. An acknowledgement proves the GUI processed the event, not that
the human saw it or that the claimed network state was independently checked.
"""
import json
import os
from pathlib import Path
import sys
from datetime import datetime


PHASES = {
    'preparing': ('시험 환경 준비 중', '#364152', '#ffffff'),
    'ready': ('정상 연결 · 시험 대기', '#11613f', '#ffffff'),
    'injecting': ('단절 주입 준비', '#b85b00', '#ffffff'),
    'disconnected': ('통신 단절 감지', '#ae2020', '#ffffff'),
    'frozen': ('단절 중 위치·점유 확인', '#ae2020', '#ffffff'),
    'reconnecting': ('재연결 시도 중', '#8a6400', '#ffffff'),
    'recovered': ('연결 복구 · 이동 재개 확인', '#095e87', '#ffffff'),
    'failed': ('시험 중단 · 확인 필요', '#672333', '#ffffff'),
}


def parse_update(value, last_seq):
    """Validate a complete event and ignore already displayed sequence IDs."""
    if (not isinstance(value, dict) or set(value) != {'seq', 'phase', 'message', 'at'}
            or type(value['seq']) is not int or value['seq'] < 1
            or not isinstance(value['phase'], str) or value['phase'] not in PHASES):
        raise ValueError('invalid indicator event')
    for name, limit in (('message', 160), ('at', 80)):
        text = value[name]
        if (not isinstance(text, str) or not text.strip() or len(text) > limit
                or any(ord(char) < 32 or ord(char) == 127 for char in text)):
            raise ValueError('invalid indicator event ' + name)
    if value['seq'] <= last_seq:
        return None
    return dict(value)


def atomic_write(path, value):
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
        stream.flush()
    os.replace(temporary, path)


def main(argv=None):
    import tkinter as tk

    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        raise ValueError('status and acknowledgement paths are required')
    status, ack = (Path(item).resolve() for item in argv)
    if status == ack or status.parent != ack.parent or not status.parent.is_dir():
        raise ValueError('distinct status files in one test folder are required')

    root = tk.Tk()
    root.title('AGV 통신 단절 시험 상태')
    root.geometry('620x175+20+30')
    root.resizable(False, False)
    root.attributes('-topmost', True)
    frame = tk.Frame(root, bg='#364152', padx=16, pady=12)
    frame.pack(fill='both', expand=True)
    title = tk.Label(frame, text='시험 준비 중', bg='#364152', fg='white',
                     font=('맑은 고딕', 22, 'bold'), anchor='w')
    title.pack(fill='x')
    detail = tk.Label(frame, text='실제 로봇이 아닌 격리 시뮬레이션', bg='#364152', fg='white',
                      font=('맑은 고딕', 13), anchor='w')
    detail.pack(fill='x', pady=(3, 0))
    clock = tk.Label(frame, text='이벤트 시각: 대기', bg='#364152', fg='white',
                     font=('맑은 고딕', 12), anchor='w')
    clock.pack(fill='x', pady=(3, 0))
    state = {'last_seq': 0, 'exit_code': 0}

    def close(event='closed', **fields):
        try:
            atomic_write(ack, dict(event=event, pid=os.getpid(), **fields))
        except OSError:
            pass
        if event != 'closed':
            state['exit_code'] = 2
        else:
            # Closing this window makes the visible test unobservable.
            state['exit_code'] = 3
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', close)

    def poll():
        try:
            try:
                raw = status.read_text(encoding='utf-8')
            except FileNotFoundError:
                root.after(100, poll)
                return
            event = parse_update(json.loads(raw), state['last_seq'])
            if event is not None:
                text, background, foreground = PHASES[event['phase']]
                frame.configure(bg=background)
                for label in (title, detail, clock):
                    label.configure(bg=background, fg=foreground)
                title.configure(text=text)
                detail.configure(text=event['message'])
                clock.configure(text='이벤트 시각: ' + event['at'])
                root.update_idletasks()
                atomic_write(ack, {'event': 'shown', 'pid': os.getpid(), 'seq': event['seq'],
                                   'phase': event['phase'],
                                   'shown_at': datetime.now().astimezone().isoformat(timespec='milliseconds')})
                state['last_seq'] = event['seq']
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            close('error', error=repr(exc))
            return
        root.after(100, poll)

    root.update_idletasks()
    atomic_write(ack, {'event': 'ready', 'pid': os.getpid(),
                       'shown_at': datetime.now().astimezone().isoformat(timespec='milliseconds')})
    root.after(0, poll)
    root.mainloop()
    return state['exit_code']


if __name__ == '__main__':
    sys.exit(main())
