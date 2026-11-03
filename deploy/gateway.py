"""Same-origin production UI and invitation gate; the private MCP bridge is separate."""
from collections import deque
from datetime import timedelta
from pathlib import Path
from threading import Lock
import time
from urllib.parse import urlsplit

from flask import request, session, redirect, jsonify, send_from_directory, abort, render_template_string
from werkzeug.security import check_password_hash
from werkzeug.wrappers import Response

PUBLIC_FLAG='haixun.public_demo'
# Upload policy for the public demo. Suffixes listed here are what a judge may
# send through the tunnel; everything else is refused before it reaches the app.
# Override per deployment with ``demo_upload_suffixes`` in server.local.json.
MAX_UPLOAD_BYTES=256*1024*1024
DEMO_UPLOAD_SUFFIXES={'.jpg','.jpeg','.png','.webp','.tif','.tiff','.bmp','.gif',
                      '.zip','.pdf','.docx','.md','.pt'}
PRIVATE_PATHS={'/api/agent/engine/bridge/tools','/api/agent/engine/bridge/call'}
FORM='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>海巡智检 受邀演示</title>
<style>body{margin:0;background:#f3f5f7;color:#202830;font:16px system-ui}main{max-width:390px;margin:15vh auto;padding:30px;background:white;border:1px solid #ddd;border-radius:16px}input,button{box-sizing:border-box;width:100%;padding:12px;margin-top:14px;border-radius:8px;border:1px solid #aaa}button{background:#202830;color:white}p{line-height:1.7}small{color:#666}</style>
<main><h1>海巡智检</h1><p>这是受邀访问的比赛演示环境。请输入主办团队提供的访问口令，进入后再登录网站演示账号。</p>
<form method="post" action="/__demo_access"><label for="pass">访问口令</label><input id="pass" type="password" name="password" required autocomplete="current-password" maxlength="256"><button>进入演示</button></form><p role="alert">{{ error }}</p><small>此入口不提供服务器管理员权限。</small></main></html>'''


def install_gateway(app,dist,config,*,login_allowed,token_allowed):
    dist=Path(dist).resolve()
    if not (dist/'index.html').is_file(): raise ValueError('请先在web-haixun运行npm run build')
    parsed=urlsplit(config['public_url'])
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment or parsed.port not in {None,443}:
        raise ValueError('public_url须为无路径的HTTPS网址')
    if len(config.get('session_secret',''))<40 or not config.get('access_password_hash','').startswith('scrypt:'):
        raise ValueError('请先初始化本机演示访问口令')
    app.config.update(SECRET_KEY=config['session_secret'],SESSION_COOKIE_NAME='haixun_demo_gate',
        SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SECURE=True,SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=2),MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES)
    hosts={parsed.hostname.lower(),'127.0.0.1','localhost'}
    upload_suffixes={str(s).strip().lower() for s in (config.get('demo_upload_suffixes') or DEMO_UPLOAD_SUFFIXES)}
    failures=deque(maxlen=20);lock=Lock()
    def denied(message,status=403): return jsonify(code=status,msg=message,data=None),status

    @app.errorhandler(413)
    def upload_too_large(_exc):
        return denied(f'上传内容超过 {MAX_UPLOAD_BYTES//1024//1024} MB 上限，请压缩后重试',413)

    @app.before_request
    def guard_public_entry():
        if not request.environ.get(PUBLIC_FLAG): return None
        if request.host.split(':',1)[0].lower() not in hosts: return denied('访问域名与部署配置不匹配',400)
        path=request.path
        if path=='/__demo_health': return jsonify(status='ok',entry='haixun-demo')
        if path=='/__demo_access': return None
        if not session.get('demo_access'):
            if path.startswith('/api/'): return denied('演示访问授权已失效，请刷新页面重新输入访问口令',401)
            return render_template_string(FORM,error=''),401
        if path.startswith('/api/'):
            if path.startswith(('/api/agent/dev','/api/agent/engine','/api/agent/local-folders','/api/agent/local-folder',
                                '/api/agent/mcp','/api/agent/credentials','/api/agent/grants')):
                return denied('远程演示不开放本机管理和内部工具接口')
            if path=='/api/user/login':
                body=request.get_json(silent=True) or {}
                username=body.get('username') if isinstance(body,dict) else None
                if not isinstance(username,str) or not login_allowed(username): return denied('请使用团队指定的普通演示账号')
                return None  # Existing website verifies the password and issues its own JWT.
            if path.startswith('/api/user/') and path not in {'/api/user/current','/api/user/logout'}:
                return denied('远程演示关闭注册和账号管理')
            if path=='/api/user': return denied('远程演示关闭账号管理')
            # 只放行"在预置清单内切换模型"这一个写接口，其余连接配置仍限本机。
            if path=='/api/agent/connection/model' and request.method=='POST':
                pass
            elif path.startswith('/api/agent/connection') and request.method!='GET':
                return denied('模型连接请在服务器本机预先配置')
            header=request.headers.get('Authorization','')
            token=header[7:] if header.startswith('Bearer ') else request.cookies.get('cxs_file_session') if path.startswith('/api/file/') else None
            if not token or not token_allowed(token): return denied('缺少有效的普通演示账号登录',401)
            if request.mimetype=='multipart/form-data':
                size=request.content_length
                if size and size>MAX_UPLOAD_BYTES:
                    return denied(f'上传内容超过 {MAX_UPLOAD_BYTES//1024//1024} MB 上限，请压缩后重试',413)
                if any(Path(f.filename or '').suffix.lower() not in upload_suffixes for f in request.files.values()):
                    return denied('公开演示不接受该类型的文件上传')
        return None

    @app.after_request
    def demo_headers(response):
        if request.environ.get(PUBLIC_FLAG):
            response.headers['Cache-Control']='no-store'
            response.headers['X-Content-Type-Options']='nosniff'
            response.headers['Referrer-Policy']='same-origin'
            response.headers['X-Frame-Options']='SAMEORIGIN'
        return response

    @app.route('/__demo_access',methods=['GET','POST'])
    def demo_access():
        if request.method=='GET': return render_template_string(FORM,error='')
        now=time.monotonic()
        with lock:
            while failures and now-failures[0]>60: failures.popleft()
            if len(failures)>=20: return render_template_string(FORM,error='尝试次数过多，请一分钟后重试'),429
            failures.append(now)
        password=request.form.get('password','')
        if len(password)>256 or not check_password_hash(config['access_password_hash'],password):
            return render_template_string(FORM,error='访问口令不正确'),403
        session.clear();session['demo_access']=True;session.permanent=True
        return redirect('/')

    @app.post('/__demo_logout')
    def demo_logout():
        session.clear();return redirect('/__demo_access')

    @app.get('/')
    @app.get('/<path:asset>')
    def demo_frontend(asset=''):
        if asset=='api' or asset.startswith('api/'): abort(404)
        target=(dist/asset).resolve()
        if not target.is_relative_to(dist) or any(part.startswith('.') for part in Path(asset).parts): abort(404)
        if target.is_file(): return send_from_directory(dist,asset)
        if asset.startswith('assets/') or Path(asset).suffix: abort(404)
        return send_from_directory(dist,'index.html')


def public_entry(app):
    def serve(environ,start_response):
        environ[PUBLIC_FLAG]=True
        # The configured external entry is HTTPS. Do not trust client-supplied forwarding headers.
        environ['wsgi.url_scheme']='https'
        return app(environ,start_response)
    return serve


def private_bridge(app):
    def serve(environ,start_response):
        if environ.get('REMOTE_ADDR') not in {'127.0.0.1','::1'} or environ.get('PATH_INFO') not in PRIVATE_PATHS:
            return Response('Private bridge only',status=404)(environ,start_response)
        environ[PUBLIC_FLAG]=False
        return app(environ,start_response)
    return serve
