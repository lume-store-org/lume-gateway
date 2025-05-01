from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import requests
import os
import datetime

app = Flask(__name__)
CORS(app)

# URLs dos serviços - obtém do ambiente ou usa valores padrão para desenvolvimento
ITENS_SERVICE_URL = os.environ.get('ITENS_SERVICE_URL', 'http://service-itens:5001')
PEDIDOS_SERVICE_URL = os.environ.get('PEDIDOS_SERVICE_URL', 'http://service-pedidos:5002')
USUARIOS_SERVICE_URL = os.environ.get('USUARIOS_SERVICE_URL', 'http://service-usuarios:5003')

# Mapeamento de rotas para direcionar requisições para cada microserviço
SERVICE_ROUTES = {
    'itens': ITENS_SERVICE_URL,
    'pedidos': PEDIDOS_SERVICE_URL,
    'usuarios': USUARIOS_SERVICE_URL,
    'auth': USUARIOS_SERVICE_URL  # Auth usa o mesmo serviço dos usuários
}

"""
FLUXO DE COMUNICAÇÃO DA ARQUITETURA:

1. Cliente (Frontend/Browser) -> Envia requisições apenas para o API Gateway (porta 5000)
   Exemplo: http://localhost:5000/api/itens

2. API Gateway -> Analisa a URL e encaminha para o microserviço apropriado:
   - /api/itens -> service-itens:5001/itens (microserviço de itens)
   - /api/pedidos -> service-pedidos:5002/pedidos (microserviço de pedidos)
   - /api/usuarios ou /api/auth -> service-usuarios:5003/usuarios ou /auth (microserviço de usuários)

3. Microserviços -> Processam a requisição e retornam a resposta para o API Gateway

4. API Gateway -> Repassa a resposta de volta para o cliente

5. Comunicação entre microserviços (quando necessário):
   - Um microserviço pode chamar outro através da rede Docker interna
   - Exemplo: service-pedidos pode chamar service-itens para verificar estoque
   
Esta arquitetura implementa o padrão API Gateway para microserviços, onde:
- Apenas o API Gateway é acessível externamente (segurança)
- Clientes não conhecem a arquitetura interna (abstração)
- Centraliza lógicas de roteamento, autorização e transformação (separação de responsabilidades)
"""

# Middleware para registrar todas as requisições recebidas
@app.before_request
def log_request():
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    client_ip = request.remote_addr
    method = request.method
    path = request.path
    query = request.query_string.decode() if request.query_string else ""
    query_str = f"?{query}" if query else ""
    user_agent = request.headers.get('User-Agent', 'Unknown')
    
    # Formatar o log para ser bem visível
    print("\n" + "="*100)
    print(f"[API-GATEWAY] [{timestamp}] REQUISIÇÃO RECEBIDA")
    print(f"IP: {client_ip} | {method} {path}{query_str}")
    print(f"User-Agent: {user_agent}")
    
    # Se tiver corpo na requisição (POST, PUT), mostrar também
    if method in ['POST', 'PUT', 'PATCH'] and request.is_json:
        try:
            body = request.json
            print(f"Corpo: {body}")
        except:
            print("Corpo: [Não foi possível decodificar JSON]")
    print("="*100)

# Middleware para registrar todas as respostas enviadas
@app.after_request
def log_response(response):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = response.status_code
    status_text = "Sucesso" if 200 <= status < 400 else "Erro"
    
    print("\n" + "-"*100)
    print(f"[API-GATEWAY] [{timestamp}] RESPOSTA ENVIADA")
    print(f"Status: {status} ({status_text})")
    print("-"*100 + "\n")
    
    # Adicionar headers para prevenir cache
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

def proxy_request(service_url, path, method):
    """
    Encaminha requisições para o serviço apropriado e retorna a resposta
    """
    # Construir URL de destino - removendo o prefixo /api para mapear para as rotas internas
    target_path = path.replace('/api', '')
    target_url = f"{service_url}{target_path}"
    
    # Registrar a requisição sendo encaminhada
    print(f"[API-GATEWAY] Encaminhando: {method} {path} → {target_url}")
    
    # Headers da requisição original (exceto host)
    headers = {key: value for key, value in request.headers 
              if key.lower() != 'host' and key.lower() != 'content-length'}
    
    # Adicionar headers anti-cache
    headers['Cache-Control'] = 'no-cache, no-store'
    headers['Pragma'] = 'no-cache'
    
    # Dados do corpo da requisição
    data = request.get_data()
    
    # Parâmetros de consulta
    params = request.args
    
    # Reenviar a requisição para o serviço de destino
    try:
        response = requests.request(
            method=method,
            url=target_url,
            headers=headers,
            params=params,
            data=data,
            cookies=request.cookies,
            allow_redirects=False,
            timeout=10
        )
        
        print(f"[API-GATEWAY] Resposta de {target_url}: {response.status_code}")
        
        # Preparar resposta
        resp = Response(
            response.content,
            status=response.status_code
        )
        
        # Copiar cabeçalhos relevantes
        for header, value in response.headers.items():
            if header.lower() not in ('transfer-encoding', 'content-encoding', 'content-length'):
                resp.headers[header] = value
                
        # Garantir que headers anti-cache estão presentes
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        
        return resp
    except requests.exceptions.RequestException as e:
        print(f"[API-GATEWAY] ERRO ao encaminhar para {target_url}: {str(e)}")
        return jsonify({
            "erro": "Serviço indisponível",
            "detalhes": str(e)
        }), 503

@app.route('/api/<service>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
def route_request(service, subpath):
    """
    Roteia requisições para o serviço apropriado com base no prefixo da URL
    """
    if service not in SERVICE_ROUTES:
        print(f"[API-GATEWAY] ERRO: Serviço '{service}' não encontrado")
        return jsonify({"erro": "Serviço não encontrado"}), 404
    
    service_url = SERVICE_ROUTES[service]
    path = f"/{service}/{subpath}"
    
    return proxy_request(service_url, path, request.method)

@app.route('/api/<service>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
def route_service_root(service):
    """
    Roteia requisições para a raiz de um serviço
    """
    if service not in SERVICE_ROUTES:
        print(f"[API-GATEWAY] ERRO: Serviço '{service}' não encontrado")
        return jsonify({"erro": "Serviço não encontrado"}), 404
    
    service_url = SERVICE_ROUTES[service]
    path = f"/{service}"
    
    return proxy_request(service_url, path, request.method)

@app.route('/health', methods=['GET'])
def health():
    """
    Verifica o status de saúde de todos os serviços
    """
    results = {}
    
    for service_name, service_url in SERVICE_ROUTES.items():
        if service_name == 'auth':  # Pular 'auth' pois é o mesmo serviço que 'usuarios'
            continue
            
        try:
            response = requests.get(f"{service_url}/health", timeout=5)
            results[service_name] = {
                "status": "online" if response.status_code == 200 else "erro",
                "code": response.status_code
            }
        except requests.exceptions.RequestException:
            results[service_name] = {
                "status": "offline"
            }
    
    # Gateway está online
    gateway_status = "online"
    
    # Status geral
    status = {
        "api_gateway": gateway_status,
        "services": results
    }
    
    return jsonify(status)

@app.route('/', methods=['GET'])
def welcome():
    return jsonify({
        "mensagem": "API Gateway do E-commerce",
        "versao": "1.0.0",
        "servicos": list(SERVICE_ROUTES.keys())
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)