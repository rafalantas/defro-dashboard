import os
import time
import threading
import requests
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

app = Flask(__name__, template_folder='../templates', static_folder='../static')
CORS(app)

TOKEN = os.environ.get('EMODUL_TOKEN', '')
USER_ID = os.environ.get('EMODUL_USER_ID', '')
UDID = os.environ.get('EMODUL_UDID', '')
REFRESH_INTERVAL = int(os.environ.get('REFRESH_INTERVAL', '30'))

BASE_URL = f'https://emodul.eu/api/v1/users/{USER_ID}/modules/{UDID}'
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Content-Type': 'application/json',
}

cache = {'data': None, 'ts': 0, 'error': None}


def fetch_from_emodul():
    try:
        res = requests.get(BASE_URL, headers=HEADERS, timeout=10)
        res.raise_for_status()
        cache['data'] = res.json()
        cache['ts'] = time.time()
        cache['error'] = None
    except Exception as e:
        cache['error'] = str(e)


def background_refresh():
    while True:
        fetch_from_emodul()
        time.sleep(REFRESH_INTERVAL)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/data')
def get_data():
    if cache['data'] is None:
        fetch_from_emodul()
    if cache['error']:
        return jsonify({'error': cache['error']}), 502
    return jsonify({
        'data': cache['data'],
        'cached_at': cache['ts'],
        'age': round(time.time() - cache['ts'], 1),
    })


@app.route('/api/mode', methods=['POST'])
def set_mode():
    body = request.get_json()
    value = body.get('value')
    if value not in [0, 1, 2, 3]:
        return jsonify({'error': 'Invalid mode value'}), 400
    try:
        res = requests.post(
            f'{BASE_URL}/menu/MU/ido/2011',
            headers=HEADERS,
            json={'value': value},
            timeout=10,
        )
        res.raise_for_status()
        threading.Thread(target=fetch_from_emodul, daemon=True).start()
        return jsonify({'ok': True, 'mode': value})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


if __name__ == '__main__':
    t = threading.Thread(target=background_refresh, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=5000)
