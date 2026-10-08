import os
import time
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
import requests

app = Flask(__name__, template_folder='../templates', static_folder='../static')
CORS(app)

TOKEN    = os.environ.get('EMODUL_TOKEN', '')
USER_ID  = os.environ.get('EMODUL_USER_ID', '')
UDID     = os.environ.get('EMODUL_UDID', '')
REFRESH  = int(os.environ.get('REFRESH_INTERVAL', '30'))

BASE      = f'https://emodul.eu/api/v1/users/{USER_ID}/modules/{UDID}'
STAT_BASE = f'https://emodul.eu/api/v1/modules/{UDID}/statistics'
HEADERS   = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}

SERIES_NAMES = {
    '1050': 'CO',
    '1041': 'C.W.U.',
    '791':  'Spaliny',
    '795':  'Zewnętrzna',
}
COLORS = {
    '1050': '#d85a2a',
    '1041': '#2563eb',
    '791':  '#7c3aed',
    '795':  '#2d9c5a',
}

cache         = {'data': None, 'ts': 0, 'error': None}
history_cache = {}
lock          = threading.Lock()


def fetch_tiles():
    try:
        res = requests.get(BASE, headers=HEADERS, timeout=10)
        res.raise_for_status()
        with lock:
            cache['data']  = res.json()
            cache['ts']    = time.time()
            cache['error'] = None
    except Exception as e:
        with lock:
            cache['error'] = str(e)


SETTINGS_PATH = '/data/settings.json'
DEFAULT_SETTINGS = {
    'auto_enabled':         False,
    'auto_temp_threshold':  float(os.environ.get('AUTO_TEMP_THRESHOLD', '15')),
    'auto_temp_hysteresis': float(os.environ.get('AUTO_TEMP_HYSTERESIS', '2')),
    'auto_temp_mode':       int(os.environ.get('AUTO_TEMP_MODE_DEFAULT', '3')),
    'auto_temp_below_mode': int(os.environ.get('AUTO_TEMP_BELOW_MODE_DEFAULT', '0')),
}

def load_settings():
    try:
        os.makedirs('/data', exist_ok=True)
        with open(SETTINGS_PATH) as f:
            s = json.load(f)
            return {**DEFAULT_SETTINGS, **s}
    except Exception:
        return dict(DEFAULT_SETTINGS)

def save_settings(s):
    os.makedirs('/data', exist_ok=True)
    with open(SETTINGS_PATH, 'w') as f:
        json.dump(s, f)

last_auto_mode = None


def run_auto_logic(tiles_by_id):
    global last_auto_mode
    s = load_settings()
    if not s['auto_enabled']:
        return

    ext_tile  = tiles_by_id.get(1009)
    mode_tile = tiles_by_id.get(1001)
    if not ext_tile or not mode_tile:
        return

    temp_ext = ext_tile['params']['value'] / 10
    thr      = s['auto_temp_threshold']
    hys      = s['auto_temp_hysteresis']
    mode_map = {814: 0, 815: 1, 816: 2, 811: 3}
    current  = mode_map.get(mode_tile['params']['statusId'], -1)

    if temp_ext >= thr:
        target = s['auto_temp_mode']
    elif temp_ext < thr - hys:
        target = s['auto_temp_below_mode']
    else:
        return  # strefa histerezy — nie rób nic

    if target != current and target != last_auto_mode:
        try:
            res = requests.post(
                f'{BASE}/menu/MU/ido/2011',
                headers=HEADERS,
                json={'value': target},
                timeout=10,
            )
            res.raise_for_status()
            last_auto_mode = target
            print(f'[AUTO] Zmieniono tryb na {target} (temp_ext={temp_ext}°C, próg={thr}°C ±{hys}°C)')
        except Exception as e:
            print(f'[AUTO] Błąd zmiany trybu: {e}')


def background_refresh():
    while True:
        fetch_tiles()
        with lock:
            data = cache['data']
        if data:
            tiles_by_id = {t['id']: t for t in data.get('tiles', [])}
            run_auto_logic(tiles_by_id)
        time.sleep(REFRESH)


def parse_x(x):
    try:
        return int(datetime.strptime(x, '%Y%m%d%H%M').timestamp())
    except Exception:
        return None


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/data')
def get_data():
    with lock:
        age   = time.time() - cache['ts']
        data  = cache['data']
        error = cache['error']

    if age > REFRESH * 2 or data is None:
        fetch_tiles()
        with lock:
            data  = cache['data']
            error = cache['error']
            age   = time.time() - cache['ts']

    if error and data is None:
        return jsonify({'error': error}), 502

    return jsonify({'data': data, 'cached_at': cache['ts'], 'age': round(age, 1)})


@app.route('/api/history')
def get_history():
    range_param = request.args.get('range', 'day')
    if range_param not in ('day', 'week', 'month'):
        range_param = 'day'

    cached = history_cache.get(range_param)
    ttl = 600 if range_param == 'day' else 3600
    if cached and (time.time() - cached['ts']) < ttl:
        return jsonify(cached['data'])

    try:
        url = f'{STAT_BASE}/linear/range/{range_param}'
        res = requests.get(url, headers=HEADERS, timeout=15)
        res.raise_for_status()
        raw     = res.json()
        history = raw.get('data', {}).get('history', {})

        series = []
        for key, name in SERIES_NAMES.items():
            points = []
            for p in history.get(key, []):
                ts = parse_x(p['x'])
                if ts and p['y'] is not None:
                    points.append({'x': ts * 1000, 'y': p['y']})
            if points:
                series.append({'label': name, 'color': COLORS[key], 'points': points})

        result = {'series': series, 'range': range_param}
        history_cache[range_param] = {'data': result, 'ts': time.time()}
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/settings', methods=['GET'])
def get_settings():
    s = load_settings()
    s['auto_temp_threshold']  = AUTO_TEMP_THRESHOLD
    s['auto_temp_hysteresis'] = AUTO_TEMP_HYSTERESIS
    return jsonify(s)


@app.route('/api/settings', methods=['POST'])
def post_settings():
    body = request.get_json()
    s = load_settings()
    if 'auto_enabled' in body:
        s['auto_enabled'] = bool(body['auto_enabled'])
    if 'auto_temp_threshold' in body:
        s['auto_temp_threshold'] = float(body['auto_temp_threshold'])
    if 'auto_temp_hysteresis' in body:
        s['auto_temp_hysteresis'] = float(body['auto_temp_hysteresis'])
    if 'auto_temp_mode' in body:
        s['auto_temp_mode'] = int(body['auto_temp_mode'])
    if 'auto_temp_below_mode' in body:
        s['auto_temp_below_mode'] = int(body['auto_temp_below_mode'])
    save_settings(s)
    return jsonify(s)


@app.route('/api/mode', methods=['POST'])
def set_mode():
    body  = request.get_json()
    value = body.get('value')
    if value not in [0, 1, 2, 3]:
        return jsonify({'error': 'Invalid mode value'}), 400
    try:
        res = requests.post(
            f'{BASE}/menu/MU/ido/2011',
            headers=HEADERS,
            json={'value': value},
            timeout=10,
        )
        res.raise_for_status()
        threading.Thread(target=fetch_tiles, daemon=True).start()
        return jsonify({'ok': True, 'mode': value})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


t = threading.Thread(target=background_refresh, daemon=True)
t.start()
