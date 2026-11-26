"""Protect local MQTT observation endpoints from accidental network exposure.

This is not device authentication or a safe public reverse-proxy deployment.
Run the local server with proxy header processing disabled.
"""
import hmac
import os
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


_HOST = re.compile(r'^(?:127\.0\.0\.1|localhost|\[::1\])(?::([0-9]{1,5}))?$', re.IGNORECASE)
_HEADERS = {'Cache-Control': 'no-store'}


def _deny(status):
    raise HTTPException(status_code=status, detail='Observation access unavailable', headers=_HEADERS)


def require_local_observation_access(request: Request):
    """Require a direct loopback request, and a bearer token when configured."""
    if request.client is None or request.client.host not in ('127.0.0.1', '::1'):
        _deny(403)
    # These endpoints are not supported behind a forwarding proxy. Never trust
    # client-supplied addresses or hosts as proof of a direct connection.
    if any(name.lower() == b'forwarded' or name.lower().startswith(b'x-forwarded-')
           for name, _value in request.scope.get('headers', [])):
        _deny(403)
    hosts = request.headers.getlist('host')
    if len(hosts) != 1:
        _deny(403)
    match = _HOST.fullmatch(hosts[0])
    if not match or (match.group(1) is not None and not 1 <= int(match.group(1)) <= 65535):
        _deny(403)
    origins = request.headers.getlist('origin')
    if len(origins) > 1:
        _deny(403)
    if origins:
        try:
            origin = urlsplit(origins[0])
            if (origin.scheme != request.url.scheme or origin.netloc.lower() != hosts[0].lower()
                    or origin.path or origin.query or origin.fragment or origin.username is not None):
                _deny(403)
        except ValueError:
            _deny(403)
    token = os.environ.get('AGV_OBSERVATION_API_TOKEN')
    if token is None:
        return
    if not (32 <= len(token) <= 512 and all(33 <= ord(c) <= 126 for c in token)):
        _deny(503)
    values = request.headers.getlist('authorization')
    if (len(values) != 1 or not values[0].startswith('Bearer ')
            or not hmac.compare_digest(values[0][7:].encode('utf-8'), token.encode('ascii'))):
        _deny(401)
