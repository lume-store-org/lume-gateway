"""Lume Store API Gateway.

The only service exposed outside the Docker network. It:
1. receives the front's calls at /api/<resource>/...;
2. validates the Bearer token with the users service;
3. forwards the call to the right microservice with the authenticated user in the internal
   headers X-User-Id and X-User-Admin (client-sent headers with these names are dropped);
4. returns the response to the front.
"""
import logging
import os
import time

import requests
from flask import Flask, Response, g, jsonify, request, send_from_directory
from flask_cors import CORS

CATALOG_SERVICE_URL = os.environ.get('CATALOG_SERVICE_URL', 'http://lume-catalog:5001')
ORDERS_SERVICE_URL = os.environ.get('ORDERS_SERVICE_URL', 'http://lume-orders:5002')
USERS_SERVICE_URL = os.environ.get('USERS_SERVICE_URL', 'http://lume-users:5003')
FRONTEND_ORIGIN = os.environ.get('FRONTEND_ORIGIN', 'http://localhost:3000')

# Resource prefix -> microservice
SERVICE_ROUTES = {
    'products': CATALOG_SERVICE_URL,
    'orders': ORDERS_SERVICE_URL,
    'users': USERS_SERVICE_URL,
    'auth': USERS_SERVICE_URL,
}

# Routes open without login: (method, resource, full path or None for any subpath)
PUBLIC_ROUTES = {
    ('GET', 'products', None),
    ('POST', 'users', '/users'),
    ('POST', 'auth', '/auth/login'),
    ('POST', 'auth', '/auth/logout'),
}

INTERNAL_HEADERS = {'x-user-id', 'x-user-admin'}
HOP_BY_HOP = {'host', 'content-length', 'connection', 'transfer-encoding', 'content-encoding'}

logging.basicConfig(level=logging.INFO, format='[api-gateway] %(message)s')
log = logging.getLogger('api-gateway')

app = Flask(__name__)
app.json.ensure_ascii = False
CORS(app, resources={r'/api/*': {'origins': [FRONTEND_ORIGIN]}})


@app.before_request
def start_timer():
    g.started = time.monotonic()


@app.after_request
def log_request(response):
    # No body and no Authorization in the log: only method, path, status and time
    ms = (time.monotonic() - g.get('started', time.monotonic())) * 1000
    log.info('%s %s -> %s (%.0f ms)', request.method, request.path, response.status_code, ms)
    response.headers['Cache-Control'] = 'no-store'
    return response


def is_public(method, resource, path):
    return (method, resource, None) in PUBLIC_ROUTES or (method, resource, path) in PUBLIC_ROUTES


def authenticate():
    """Validates the token with the users service. Returns (user, error_response)."""
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None, None
    try:
        resp = requests.post(f'{USERS_SERVICE_URL}/auth/verify', json={'token': auth[7:]}, timeout=5)
    except requests.exceptions.RequestException:
        return None, (jsonify({'error': 'Serviço de usuários indisponível'}), 503)
    if resp.status_code != 200:
        return None, (jsonify({'error': 'Sessão inválida ou expirada'}), 401)
    return resp.json()['user'], None


def proxy(resource, path):
    if resource not in SERVICE_ROUTES:
        return jsonify({'error': 'Recurso não encontrado'}), 404
    if request.method == 'OPTIONS':
        return Response(status=204)

    user, error = authenticate()
    if error:
        return error
    if user is None and not is_public(request.method, resource, path):
        return jsonify({'error': 'Faça login para continuar'}), 401

    headers = {k: v for k, v in request.headers if k.lower() not in HOP_BY_HOP | INTERNAL_HEADERS}
    if user:
        headers['X-User-Id'] = str(user['id'])
        headers['X-User-Admin'] = '1' if user.get('is_admin') else '0'

    try:
        resp = requests.request(
            request.method,
            f'{SERVICE_ROUTES[resource]}{path}',
            headers=headers,
            params=request.args,
            data=request.get_data(),
            allow_redirects=False,
            timeout=10,
        )
    except requests.exceptions.RequestException:
        log.warning('%s service unavailable', resource)
        return jsonify({'error': 'Serviço indisponível'}), 503

    response = Response(resp.content, status=resp.status_code)
    for header, value in resp.headers.items():
        if header.lower() not in HOP_BY_HOP:
            response.headers[header] = value
    return response


@app.route('/api/<resource>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def resource_root(resource):
    return proxy(resource, f'/{resource}')


@app.route('/api/<resource>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def resource_subpath(resource, subpath):
    return proxy(resource, f'/{resource}/{subpath}')


@app.route('/health', methods=['GET'])
def health():
    services = {}
    for name, url in (('catalog', CATALOG_SERVICE_URL), ('orders', ORDERS_SERVICE_URL), ('users', USERS_SERVICE_URL)):
        try:
            resp = requests.get(f'{url}/health', timeout=3)
            services[name] = 'online' if resp.status_code == 200 else 'error'
        except requests.exceptions.RequestException:
            services[name] = 'offline'
    ok = all(s == 'online' for s in services.values())
    return jsonify({'gateway': 'online', 'services': services}), 200 if ok else 503


DOCS_HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Lume Store · API</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url: "/openapi.yaml", dom_id: "#swagger", deepLinking: true, tryItOutEnabled: true, persistAuthorization: true});</script>
</body></html>"""


@app.route('/docs', methods=['GET'])
def docs():
    return Response(DOCS_HTML, mimetype='text/html')


@app.route('/openapi.yaml', methods=['GET'])
def openapi():
    return send_from_directory(app.root_path, 'openapi.yaml', mimetype='application/yaml')


@app.route('/', methods=['GET'])
def info():
    return jsonify({'name': 'Lume Store API Gateway', 'version': '2.0.0', 'docs': '/docs', 'resources': list(SERVICE_ROUTES)})
