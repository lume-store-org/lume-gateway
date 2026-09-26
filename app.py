"""API Gateway do e-commerce.

Único serviço exposto para fora da rede Docker. Ele:
1. recebe as chamadas do front em /api/<serviço>/...;
2. valida o token Bearer no serviço de usuários;
3. repassa a chamada ao microserviço com o usuário autenticado nos headers internos
   X-Usuario-Id e X-Usuario-Admin (headers com esse nome vindos do cliente são descartados);
4. devolve a resposta ao front.
"""
import logging
import os
import time

import requests
from flask import Flask, Response, g, jsonify, request, send_from_directory
from flask_cors import CORS

ITENS_SERVICE_URL = os.environ.get('ITENS_SERVICE_URL', 'http://service-itens:5001')
PEDIDOS_SERVICE_URL = os.environ.get('PEDIDOS_SERVICE_URL', 'http://service-pedidos:5002')
USUARIOS_SERVICE_URL = os.environ.get('USUARIOS_SERVICE_URL', 'http://service-usuarios:5003')
FRONTEND_ORIGIN = os.environ.get('FRONTEND_ORIGIN', 'http://localhost:3000')

SERVICE_ROUTES = {
    'itens': ITENS_SERVICE_URL,
    'pedidos': PEDIDOS_SERVICE_URL,
    'usuarios': USUARIOS_SERVICE_URL,
    'auth': USUARIOS_SERVICE_URL,
}

# Rotas liberadas sem login: (método, serviço, caminho completo ou None para qualquer subcaminho)
PUBLICAS = {
    ('GET', 'itens', None),
    ('POST', 'usuarios', '/usuarios'),
    ('POST', 'auth', '/auth/login'),
    ('POST', 'auth', '/auth/logout'),
}

HEADERS_INTERNOS = {'x-usuario-id', 'x-usuario-admin'}
HOP_BY_HOP = {'host', 'content-length', 'connection', 'transfer-encoding', 'content-encoding'}

logging.basicConfig(level=logging.INFO, format='[api-gateway] %(message)s')
log = logging.getLogger('api-gateway')

app = Flask(__name__)
app.json.ensure_ascii = False
CORS(app, resources={r'/api/*': {'origins': [FRONTEND_ORIGIN]}})


@app.before_request
def iniciar_cronometro():
    g.inicio = time.monotonic()


@app.after_request
def registrar(response):
    # Sem corpo e sem Authorization no log: só método, caminho, status e tempo
    ms = (time.monotonic() - g.get('inicio', time.monotonic())) * 1000
    log.info('%s %s -> %s (%.0f ms)', request.method, request.path, response.status_code, ms)
    response.headers['Cache-Control'] = 'no-store'
    return response


def eh_publica(metodo, servico, caminho):
    return (metodo, servico, None) in PUBLICAS or (metodo, servico, caminho) in PUBLICAS


def autenticar():
    """Valida o token no serviço de usuários. Retorna (usuario, erro)."""
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None, None
    try:
        resp = requests.post(f'{USUARIOS_SERVICE_URL}/auth/verificar', json={'token': auth[7:]}, timeout=5)
    except requests.exceptions.RequestException:
        return None, (jsonify({"erro": "Serviço de usuários indisponível"}), 503)
    if resp.status_code != 200:
        return None, (jsonify({"erro": "Sessão inválida ou expirada"}), 401)
    return resp.json()['usuario'], None


def proxy(servico, caminho):
    if servico not in SERVICE_ROUTES:
        return jsonify({"erro": "Serviço não encontrado"}), 404
    if request.method == 'OPTIONS':
        return Response(status=204)

    usuario, erro = autenticar()
    if erro:
        return erro
    if usuario is None and not eh_publica(request.method, servico, caminho):
        return jsonify({"erro": "Faça login para continuar"}), 401

    headers = {k: v for k, v in request.headers if k.lower() not in HOP_BY_HOP | HEADERS_INTERNOS}
    if usuario:
        headers['X-Usuario-Id'] = str(usuario['id'])
        headers['X-Usuario-Admin'] = '1' if usuario.get('is_admin') else '0'

    try:
        resp = requests.request(
            request.method,
            f'{SERVICE_ROUTES[servico]}{caminho}',
            headers=headers,
            params=request.args,
            data=request.get_data(),
            allow_redirects=False,
            timeout=10,
        )
    except requests.exceptions.RequestException:
        log.warning('serviço %s indisponível', servico)
        return jsonify({"erro": "Serviço indisponível"}), 503

    resposta = Response(resp.content, status=resp.status_code)
    for header, valor in resp.headers.items():
        if header.lower() not in HOP_BY_HOP:
            resposta.headers[header] = valor
    return resposta


@app.route('/api/<servico>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def rota_servico(servico):
    return proxy(servico, f'/{servico}')


@app.route('/api/<servico>/<path:subcaminho>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def rota_subcaminho(servico, subcaminho):
    return proxy(servico, f'/{servico}/{subcaminho}')


@app.route('/health', methods=['GET'])
def health():
    servicos = {}
    for nome, url in (('itens', ITENS_SERVICE_URL), ('pedidos', PEDIDOS_SERVICE_URL), ('usuarios', USUARIOS_SERVICE_URL)):
        try:
            resp = requests.get(f'{url}/health', timeout=3)
            servicos[nome] = 'online' if resp.status_code == 200 else 'erro'
        except requests.exceptions.RequestException:
            servicos[nome] = 'offline'
    ok = all(s == 'online' for s in servicos.values())
    return jsonify({"api_gateway": "online", "services": servicos}), 200 if ok else 503


DOCS_HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>E-commerce · API</title>
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
    return jsonify({"mensagem": "API Gateway do E-commerce", "versao": "2.0.0", "docs": "/docs", "servicos": list(SERVICE_ROUTES)})
