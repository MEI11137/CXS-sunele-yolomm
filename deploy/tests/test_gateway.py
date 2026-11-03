import io
from pathlib import Path
import sys

from flask import Flask,Response,jsonify,request
import pytest
from werkzeug.security import generate_password_hash
from werkzeug.test import Client
from werkzeug.wrappers import Response as WResponse

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from deploy.gateway import install_gateway,public_entry,private_bridge


@pytest.fixture
def gateway(tmp_path):
    dist=tmp_path/'dist';dist.mkdir();(dist/'index.html').write_text('DEMO SPA')
    (dist/'assets').mkdir();(dist/'assets/app.js').write_text('asset')
    app=Flask(__name__)
    @app.route('/api/echo',methods=['GET','POST'])
    def echo():return jsonify(auth=request.headers.get('Authorization'),project=request.headers.get('X-Project-Id'),query=request.args.get('q'),scheme=request.scheme)
    @app.post('/api/user/login')
    def login():return jsonify(code=200,data={'token':'site-jwt'})
    @app.post('/api/file/project/p/upload')
    def upload():return jsonify(name=request.files['file'].filename)
    @app.get('/api/file/bucket/image.png')
    def image():return Response(b'image',mimetype='image/png')
    @app.get('/api/agent/sessions/s/events/stream')
    def stream():return Response(iter(['data: one\n\n','data: two\n\n']),mimetype='text/event-stream')
    @app.route('/api/agent/engine/bridge/tools')
    def bridge():return jsonify(private=True)
    config={'public_url':'https://judge.example.ts.net','session_secret':'s'*64,'access_password_hash':generate_password_hash('strong-demo-password!')}
    install_gateway(app,dist,config,login_allowed=lambda username:username=='judge',token_allowed=lambda token:token=='judge-token')
    return app,Client(public_entry(app),WResponse),config


def enter(client):
    return client.post('/__demo_access',data={'password':'strong-demo-password!'},base_url='https://judge.example.ts.net')


def api(client,path,**kwargs):
    return client.get(path,base_url='https://judge.example.ts.net',headers={'Authorization':'Bearer judge-token','X-Project-Id':'p'},**kwargs)


def test_gate_and_session_are_distinct_from_site_jwt(gateway):
    app,client,_=gateway
    assert api(client,'/api/echo').status_code==401
    assert client.get('/',base_url='https://judge.example.ts.net').status_code==401
    assert client.post('/__demo_access',data={'password':'wrong'},base_url='https://judge.example.ts.net').status_code==403
    response=enter(client)
    assert response.status_code==302 and 'Secure' in response.headers['Set-Cookie'] and 'HttpOnly' in response.headers['Set-Cookie']
    assert api(client,'/api/echo?q=ok').json=={'auth':'Bearer judge-token','project':'p','query':'ok','scheme':'https'}
    assert client.get('/api/echo',base_url='https://judge.example.ts.net').status_code==401
    assert client.get('/api/echo',base_url='https://judge.example.ts.net',headers={'Authorization':'Bearer admin-token'}).status_code==401


def test_spa_paths_assets_and_no_source_or_api_fallback(gateway):
    _,client,_=gateway;enter(client)
    assert client.get('/projects/p/review/r',base_url='https://judge.example.ts.net').data==b'DEMO SPA'
    assert client.get('/assets/app.js',base_url='https://judge.example.ts.net').data==b'asset'
    for path in ['/api/not-found','/.env','/assets/no.js','/../../secret.key']:
        assert api(client,path).status_code==404
    assert client.get('/',base_url='https://evil.example').status_code==400


def test_private_routes_and_mutable_credentials_are_not_public(gateway):
    _,client,_=gateway;enter(client)
    for path in ['/api/agent/engine/bridge/tools','/api/agent/dev/connections','/api/agent/local-folders','/api/agent/mcp/connections']:
        assert api(client,path).status_code==403
    for name in ['admin','other']:
        assert client.post('/api/user/login',json={'username':name,'password':'any'},base_url='https://judge.example.ts.net').status_code==403
    assert client.post('/api/user/login',json={'username':'judge','password':'any'},base_url='https://judge.example.ts.net').status_code==200
    for path in ['/api/agent/connection','/api/user/register']:
        assert client.post(path,json={},base_url='https://judge.example.ts.net').status_code==403


def test_permission_mode_reaches_owner_checked_api_for_public_account(gateway):
    app,client,_=gateway
    @app.put('/api/agent/sessions/s/permission-mode')
    def mode(): return jsonify(mode=request.json['mode'])
    enter(client)
    path='/api/agent/sessions/s/permission-mode'
    assert client.put(path,json={'mode':'assist'},base_url='https://judge.example.ts.net').status_code==401
    response=client.put(path,json={'mode':'assist'},base_url='https://judge.example.ts.net',headers={'Authorization':'Bearer judge-token'})
    assert response.status_code==200 and response.json=={'mode':'assist'}


def test_private_bridge_accepts_only_internal_path_and_real_loopback(gateway):
    app,_,_=gateway;private=Client(private_bridge(app),WResponse)
    assert private.get('/api/agent/engine/bridge/tools',environ_overrides={'REMOTE_ADDR':'127.0.0.1'}).json=={'private':True}
    assert private.get('/api/agent/engine/bridge/tools',environ_overrides={'REMOTE_ADDR':'8.8.8.8','HTTP_X_FORWARDED_FOR':'127.0.0.1'}).status_code==404
    assert private.get('/api/echo',environ_overrides={'REMOTE_ADDR':'127.0.0.1'}).status_code==404


def test_sse_binary_cookie_and_image_upload_work_without_proxy_buffering(gateway):
    _,client,_=gateway;enter(client)
    stream=api(client,'/api/agent/sessions/s/events/stream',buffered=False)
    assert stream.content_type.startswith('text/event-stream')
    assert list(stream.response)==[b'data: one\n\n',b'data: two\n\n']
    client.set_cookie('cxs_file_session','judge-token',domain='judge.example.ts.net')
    assert client.get('/api/file/bucket/image.png',base_url='https://judge.example.ts.net').data==b'image'
    for filename,status in [('image.png',200),('model.pt',200),('dataset.zip',200),('program.exe',403)]:
        result=client.post('/api/file/project/p/upload',data={'file':(io.BytesIO(b'data'),filename)},base_url='https://judge.example.ts.net',headers={'Authorization':'Bearer judge-token'})
        assert result.status_code==status


def test_expired_gate_and_bruteforce_limit(gateway):
    _,client,_=gateway
    for _ in range(20):client.post('/__demo_access',data={'password':'wrong'},base_url='https://judge.example.ts.net')
    assert enter(client).status_code==429
    assert client.get('/__demo_health',base_url='https://judge.example.ts.net').json['status']=='ok'
