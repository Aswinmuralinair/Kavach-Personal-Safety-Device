"""
ratelimit.py — Kavach Server

A small in-memory sliding-window rate limiter for the authentication routes.

Deliberately dependency-free: adding Flask-Limiter would mean another package
that has to install cleanly on the Pi and on Windows before the server can
start at all. This covers the case that matters here — stopping someone from
grinding through passwords or pairing codes against a single-instance server.

Limitation, stated plainly: the counters live in this process's memory, so
they reset when the server restarts and are not shared across workers. That
is fine for the single-process deployment this project uses. If the server is
ever put behind multiple gunicorn workers, move this to Redis.
"""

import functools
import logging
import threading
import time

from flask import jsonify, request

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_hits = {}          # bucket key → [timestamps]
_last_sweep = 0.0
_SWEEP_EVERY = 300  # seconds between garbage collections


def _client_ip() -> str:
    """
    Best-effort client identity.

    ngrok and any reverse proxy put the real client in X-Forwarded-For; we
    take the first entry. This is spoofable by a determined attacker, which
    is why it throttles rather than blocks, and why the password hashing is
    what actually protects the accounts.
    """
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _sweep(now: float) -> None:
    """Drop empty buckets occasionally so the dict cannot grow forever."""
    global _last_sweep
    if now - _last_sweep < _SWEEP_EVERY:
        return
    _last_sweep = now
    for key in [k for k, v in _hits.items() if not v]:
        _hits.pop(key, None)


def _status_of(response) -> int:
    """Status code of whatever a Flask view returned (response, or (body, code))."""
    if isinstance(response, tuple) and len(response) >= 2:
        try:
            return int(response[1])
        except (TypeError, ValueError):
            return 200
    return getattr(response, 'status_code', 200)


def rate_limit(max_failures: int, per_seconds: int, scope: str = ''):
    """
    Allow at most max_failures failed attempts per per_seconds from one client.

        @app.route('/api/auth/login', methods=['POST'])
        @rate_limit(10, 300)
        def auth_login(): ...

    Only *failures* count toward the limit — a response with status >= 400.
    Successful logins, successful signups and ordinary page loads are never
    counted, so a household that pairs several accounts in a row, or mistypes
    once and then gets it right, is never locked out. What gets throttled is
    exactly the thing worth throttling: repeated guessing.

    A successful attempt also clears that client's counter, so one good login
    resets the budget.

    Responds 429 with a Retry-After header once the window is full.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            now = time.time()
            key = f'{scope or fn.__name__}:{_client_ip()}'
            cutoff = now - per_seconds

            with _lock:
                _sweep(now)
                window = [t for t in _hits.get(key, []) if t > cutoff]
                _hits[key] = window
                blocked = len(window) >= max_failures
                retry_after = int(window[0] + per_seconds - now) + 1 if blocked else 0

            if blocked:
                logger.warning(
                    '[RateLimit] %s blocked on %s (%d failures in %ds)',
                    _client_ip(), fn.__name__, len(window), per_seconds
                )
                resp = jsonify({
                    'status': 'error',
                    'message': f'Too many failed attempts. Try again in {retry_after} seconds.',
                })
                resp.status_code = 429
                resp.headers['Retry-After'] = str(retry_after)
                return resp

            response = fn(*args, **kwargs)

            with _lock:
                if _status_of(response) >= 400:
                    _hits.setdefault(key, []).append(time.time())
                else:
                    _hits.pop(key, None)   # success clears the budget

            return response
        return wrapper
    return decorator


def reset_all() -> None:
    """Clear every counter. Used by the test suite."""
    with _lock:
        _hits.clear()
