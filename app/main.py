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


def background_refresh():
    while True:
        fetch_tiles()
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
        age = time.time() - cache['ts']
        data = cache['data']
        error = cache['error']

    # Jeśli dane są starsze niż 2x REFRESH — odśwież synchronicznie
    if age > REFRESH * 2 or data is None:
        fetch_tiles()
        with lock:
            data  = cache['data']
            error = cache['error']
            age   = time.time() - cache['ts']

    if error and data is None:
        return jsonify({'error': error}), 502

    return jsonify({
        'data':      data,
        'cached_at': cache['ts'],
        'age':       round(age, 1),
    })


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


# Start background refresh
t = threading.Thread(target=background_refresh, daemon=True)
t.start()
