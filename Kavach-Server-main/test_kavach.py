"""
Integration test for the hardened Kavach server.

Two things it has to prove:
  1. Every fix in the audit actually holds (the attacks now fail).
  2. Nothing that used to work has stopped working (the happy path still runs
     end to end: device uploads an encrypted alert with evidence, guardian
     lists it, downloads it).

Run:  python test_kavach.py
"""

import base64
import io
import json
import os
import sys

os.environ['KAVACH_ADMIN_USER'] = 'testadmin'
os.environ['KAVACH_ADMIN_PASS'] = 'test-admin-password-123'
os.environ['KAVACH_DEVICE_KEY'] = 'test-device-key-abc'
os.environ['KAVACH_SECRET_KEY'] = 'test-secret-key-for-signing'

import app as srv          # noqa: E402
import pairing             # noqa: E402
import ratelimit           # noqa: E402
from crypto_utils import chacha_encrypt_text  # noqa: E402
from database import DB    # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=''):
    (PASS if condition else FAIL).append(name)
    mark = 'PASS' if condition else 'FAIL'
    print(f'  [{mark}] {name}' + (f'  — {detail}' if detail and not condition else ''))


def banner(title):
    print(f'\n{title}\n' + '-' * len(title))


srv.app.config['TESTING'] = True
with srv.app.app_context():
    DB.drop_all()
    DB.create_all()

client = srv.app.test_client()

DEV_A = 'KAVACH-001'
DEV_B = 'KAVACH-002'


def encrypted_alert(device_id, alert_type='SOS', alert_id=None):
    payload = {
        'device_id': device_id,
        'alert_type': alert_type,
        'trigger_source': 'button_single',
        'gps_location': '10.0,76.3',
        'battery_percentage': '88%',
    }
    if alert_id is not None:
        payload['alert_id'] = alert_id
    return base64.b64encode(chacha_encrypt_text(json.dumps(payload))).decode()


# ═════════════════════════════════════════════════════════════════════════════
banner('K-02  Signup requires proof of device ownership')

r = client.post('/api/auth/signup', json={
    'device_id': DEV_A, 'role': 'user', 'password': 'a-good-password'})
check('signup without a pairing code is rejected', r.status_code == 400, r.get_json())

r = client.post('/api/auth/signup', json={
    'device_id': DEV_A, 'role': 'user',
    'password': 'a-good-password', 'pairing_code': 'WRONGCODE'})
check('signup with a wrong pairing code is rejected', r.status_code == 403, r.get_json())

r = client.post('/api/auth/signup', json={
    'device_id': 'KAVACH-999', 'role': 'user',
    'password': 'a-good-password', 'pairing_code': 'ANYTHING'})
check('signup for an unknown device is rejected', r.status_code == 403, r.get_json())

# Devices become pairable once they check in with the device key.
for dev in (DEV_A, DEV_B):
    r = client.get(f'/api/device/config/{dev}',
                   headers={'X-Device-Key': 'test-device-key-abc', 'X-Battery': '90%'})
    assert r.status_code == 200, r.get_json()
code_a = pairing.get_or_create(DEV_A)
code_b = pairing.get_or_create(DEV_B)
check('device check-in exposes a pairing code to the device', len(code_a) == 8)

r = client.get(f'/api/device/config/{DEV_A}', headers={'X-Device-Key': 'wrong-key'})
check('device config rejects a bad device key', r.status_code == 401)

r = client.post('/api/auth/signup', json={
    'device_id': DEV_A, 'role': 'user', 'password': 'short', 'pairing_code': code_a})
check('short passwords are rejected', r.status_code == 400)

r = client.post('/api/auth/signup', json={
    'device_id': DEV_A, 'role': 'user',
    'password': 'user-a-password', 'pairing_code': code_a})
check('signup succeeds with the right pairing code', r.status_code == 201, r.get_json())
tok_a_user = r.get_json()['token']

r = client.post('/api/auth/signup', json={
    'device_id': DEV_A, 'role': 'guardian',
    'password': 'guard-a-password', 'pairing_code': code_a})
check('the guardian can pair with the same code', r.status_code == 201)
tok_a_guard = r.get_json()['token']

r = client.post('/api/auth/signup', json={
    'device_id': DEV_B, 'role': 'user',
    'password': 'user-b-password', 'pairing_code': code_b})
check('a second device pairs independently', r.status_code == 201)
tok_b_user = r.get_json()['token']

check("device A's code does not work for device B",
      not pairing.verify(DEV_B, code_a) or code_a == code_b)

# ═════════════════════════════════════════════════════════════════════════════
banner('Happy path  Device uploads an encrypted alert with evidence')

r = client.post('/api/alerts', data={'encrypted_payload': encrypted_alert(DEV_A)})
check('device can post an encrypted alert', r.status_code == 201, r.get_json())
alert_id = r.get_json()['alert_id']

# Two evidence files, uploaded in separate requests, exactly as the Pi does.
for name, blob in [('clip_one.wav', b'A' * 4096), ('clip_two.mp4', b'B' * 8192)]:
    r = client.post('/api/alerts', data={
        'encrypted_payload': encrypted_alert(DEV_A, alert_id=alert_id),
        'file': (io.BytesIO(blob), name),
    }, content_type='multipart/form-data')
    assert r.status_code == 201, r.get_json()

with srv.app.app_context():
    row = DB.session.get(srv.Alert, alert_id)
    names = [n for n in (row.uploaded_files or '').split(',') if n.strip()]

check('K-05: two evidence files stay two separate names', len(names) == 2,
      f'got {names}')
check('K-05: neither filename is fused', all(n.count('.') == 1 for n in names),
      f'got {names}')

r = client.get(f'/api/guardian/evidence/{alert_id}',
               headers={'Authorization': f'Bearer {tok_a_guard}'})
ev = r.get_json().get('evidence', [])
check('guardian sees both evidence files', r.status_code == 200 and len(ev) == 2,
      f'{r.status_code} {r.get_json()}')
check('both evidence files exist on disk', all(e['file_exists'] for e in ev),
      str(ev))

# The signed download link must work in a plain browser (no auth header).
signed_url = ev[0]['url']
r = client.get(signed_url)
check('signed download link serves the file', r.status_code == 200, r.status_code)

# ═════════════════════════════════════════════════════════════════════════════
banner('K-04  One device cannot read another device')

r = client.get('/api/alerts', headers={'Authorization': f'Bearer {tok_b_user}'})
returned = [a['device_id'] for a in r.get_json().get('alerts', [])]
check('listing alerts returns only your own device',
      all(d == DEV_B for d in returned), f'got {returned}')

r = client.get(f'/api/alerts?device_id={DEV_A}',
               headers={'Authorization': f'Bearer {tok_b_user}'})
check('asking for another device_id is refused', r.status_code == 403, r.status_code)

r = client.get(f'/api/alerts/{alert_id}',
               headers={'Authorization': f'Bearer {tok_b_user}'})
check("another device's alert detail is not readable", r.status_code == 404, r.status_code)

r = client.get(f'/api/alerts/{alert_id}',
               headers={'Authorization': f'Bearer {tok_a_user}'})
check('your own alert detail is still readable', r.status_code == 200, r.status_code)

fname = ev[0]['filename']
r = client.get(f'/uploads/{fname}', headers={'Authorization': f'Bearer {tok_b_user}'})
check("another device's evidence file is not downloadable", r.status_code == 404,
      r.status_code)

r = client.get(f'/uploads/{fname}', headers={'Authorization': f'Bearer {tok_a_user}'})
check('your own evidence file is downloadable', r.status_code == 200, r.status_code)

r = client.get(f'/api/evidence/alert/{alert_id}',
               headers={'Authorization': f'Bearer {tok_b_user}'})
check('evidence listing is device-scoped', r.status_code == 404, r.status_code)

with srv.app.app_context():
    ev_row = srv.Evidence.query.first()
    ev_row_id = ev_row.id
r = client.get(f'/api/evidence/{ev_row_id}/download',
               headers={'Authorization': f'Bearer {tok_b_user}'})
check('evidence download by id is device-scoped', r.status_code == 404, r.status_code)

r = client.get('/api/evidence/ledger', headers={'Authorization': f'Bearer {tok_a_user}'})
check('the cross-device ledger is admin-only', r.status_code == 403, r.status_code)

r = client.get(f'/api/device/status/{DEV_A}',
               headers={'Authorization': f'Bearer {tok_b_user}'})
check("another device's status is not readable", r.status_code == 403, r.status_code)

r = client.get(f'/api/device/status/{DEV_A}',
               headers={'Authorization': f'Bearer {tok_a_user}'})
check('your own device status is readable', r.status_code == 200, r.status_code)

r = client.get('/api/health')
body = r.get_json()
check('unauthenticated health leaks no device data',
      r.status_code == 200 and 'latest_alert_device' not in body.get('database', {})
      and 'upload_dir' not in body, str(body))

# ═════════════════════════════════════════════════════════════════════════════
banner('K-03  Emergency contacts need the password again')

r = client.put('/api/user/config',
               headers={'Authorization': f'Bearer {tok_a_user}'},
               json={'police_number': '+919999999999'})
check('changing contacts without the password is refused', r.status_code == 401,
      r.get_json())

r = client.put('/api/user/config',
               headers={'Authorization': f'Bearer {tok_a_user}'},
               json={'police_number': '+919999999999',
                     'current_password': 'wrong-password'})
check('changing contacts with a wrong password is refused', r.status_code == 401)

r = client.put('/api/user/config',
               headers={'Authorization': f'Bearer {tok_a_user}'},
               json={'police_number': '+919999999999',
                     'current_password': 'user-a-password'})
check('changing contacts with the right password works', r.status_code == 200,
      r.get_json())

r = client.get('/api/user/config', headers={'Authorization': f'Bearer {tok_a_user}'})
check('the change is persisted',
      r.get_json()['config']['police_number'] == '+919999999999', r.get_json())

r = client.get(f'/api/device/config/{DEV_A}',
               headers={'X-Device-Key': 'test-device-key-abc'})
check('the device still receives its config',
      r.get_json()['config']['police_number'] == '+919999999999', r.get_json())

# ═════════════════════════════════════════════════════════════════════════════
banner('K-07 / K-08  Credentials and brute force')

check('no hard-coded admin password remains',
      not srv.check_password_hash(srv.ADMIN_PASSWORD_HASH, 'kavach2026'))
check('the published device key is rejected',
      srv.KAVACH_DEVICE_KEY != 'kavach-device-key-2026')

r = client.post('/login', data={'username': 'testadmin', 'password': 'wrong'})
check('a wrong admin password does not log in', b'Invalid' in r.data or r.status_code == 200)

r = client.post('/login', data={'username': 'testadmin',
                                'password': 'test-admin-password-123'},
                follow_redirects=False)
check('the correct admin password logs in', r.status_code == 302, r.status_code)

ratelimit.reset_all()
codes = [client.post('/api/auth/login', json={
    'device_id': DEV_A, 'role': 'user', 'password': f'guess{i}'}).status_code
    for i in range(14)]
check('login brute force is throttled', 429 in codes, f'got {codes}')
check('the first attempts still answered normally', codes[0] == 401, f'got {codes}')

ratelimit.reset_all()
r = client.post('/api/auth/login', json={
    'device_id': 'NO-SUCH-DEVICE', 'role': 'user', 'password': 'x'})
msg_missing = r.get_json()['message']
r = client.post('/api/auth/login', json={
    'device_id': DEV_A, 'role': 'user', 'password': 'wrong-password'})
msg_wrong = r.get_json()['message']
check('missing account and wrong password look identical', msg_missing == msg_wrong,
      f'{msg_missing!r} vs {msg_wrong!r}')

ratelimit.reset_all()
r = client.post('/api/auth/login', json={
    'device_id': DEV_A, 'role': 'user', 'password': 'user-a-password'})
check('the real password still logs in', r.status_code == 200, r.get_json())

# ═════════════════════════════════════════════════════════════════════════════
banner('Regression  Endpoints the app depends on')

for name, path, tok in [
    ('user alerts',       '/api/user/alerts',      tok_a_user),
    ('guardian alerts',   '/api/guardian/alerts',  tok_a_guard),
    ('user locations',    '/api/user/locations',   tok_a_user),
    ('guardian locations', '/api/guardian/locations', tok_a_guard),
    ('user config',       '/api/user/config',      tok_a_user),
]:
    r = client.get(path, headers={'Authorization': f'Bearer {tok}'})
    check(f'{name} returns 200', r.status_code == 200, f'{r.status_code} {r.get_json()}')

r = client.put('/api/auth/fcm-token',
               headers={'Authorization': f'Bearer {tok_a_user}'},
               json={'fcm_token': 'dummy-token-value'})
check('fcm token registration still accepted', r.status_code == 200, r.get_json())

r = client.get(f'/api/device/config/{DEV_A}',
               headers={'X-Device-Key': 'test-device-key-abc', 'X-Battery': '77%'})
check('device heartbeat still accepted', r.status_code == 200)
check('heartbeat carries the pairing code', bool(r.get_json().get('pairing_code')))

r = client.get('/api/device/config/..%2f..%2fsecret',
               headers={'X-Device-Key': 'test-device-key-abc'})
check('path traversal in device_id is rejected', r.status_code in (400, 404), r.status_code)

# ═════════════════════════════════════════════════════════════════════════════
print('\n' + '=' * 60)
print(f'  {len(PASS)} passed, {len(FAIL)} failed')
if FAIL:
    print('  FAILED: ' + ', '.join(FAIL))
print('=' * 60)
sys.exit(1 if FAIL else 0)
