"""Windows production demo: public UI/API on 8080, loopback-only MCP on 8092."""
import argparse
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import threading

REPO=Path(__file__).resolve().parents[1]
BACKEND=REPO/'CXSDet-YOLOmm/CXSDet/web-flask'
DIST=REPO/'CXSDet-YOLOmm/CXSDet/web-haixun/dist'
STATE=REPO/'.demo-state'
CONFIG=STATE/'server.local.json'
sys.path.insert(0,str(REPO));sys.path.insert(0,str(BACKEND))


def environment():
    STATE.mkdir(parents=True,exist_ok=True)
    # Never reuse the developer's private database, key, or model configuration.
    values={'DB_PATH':str(STATE/'site.db'),'FILE_STORE_PATH':str(STATE/'file_store'),
        'FLASK_TEMP_DIR':str(STATE/'temp'),'AGENT_SECRET_PATH':str(STATE/'agent_secret.key'),
        'FILE_PUBLIC_URL':'/api/file','AGENT_ENGINE':'hxengine',
        'HX_ENGINE_BRIDGE_ORIGIN':'http://127.0.0.1:8092','FRONTEND_ORIGINS':'',
        'AGENT_CANDIDATE_ROOT':str(STATE/'temp/candidates'),'HX_ENGINE_STATE_DIR':str(STATE/'hxengine'),
        'YOLO_CONFIG_DIR':str(STATE/'ultralytics')}
    for name in ['AGENT_SECRET_KEY','CXS_JWT_SECRET','AGENT_ALLOW_STUB','AGENT_ENGINE_ALLOW_STUB','VL_API_KEY','VL_API_URL','VL_MODEL']:
        os.environ.pop(name,None)
    os.environ.update(values)
    vendor=str(BACKEND/'vendor')
    sys.path.insert(0,vendor)
    os.environ['PYTHONPATH']=str(BACKEND)+os.pathsep+vendor+os.pathsep+os.environ.get('PYTHONPATH','')
    if not os.environ.get('HX_ENGINE_BIN'):
        for base in [REPO,REPO.parent]:
            binary=base/'.runtime/hxengine/hxengine.exe'
            if binary.is_file(): os.environ['HX_ENGINE_BIN']=str(binary);break


def load_config():
    if not CONFIG.is_file(): raise ValueError('请先执行deploy/serve.py --init')
    config=json.loads(CONFIG.read_text(encoding='utf-8'))
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}',config.get('username','')): raise ValueError('演示账号配置无效')
    return config


def prompt_password(label,env_key):
    preset=(os.environ.get(env_key) or '').strip()
    first=preset or getpass.getpass(label)
    if not 16<=len(first)<=128 or len(set(first))<8: raise ValueError('口令需16–128字符，避免重复字符')
    if preset: return first
    if first!=getpass.getpass('Repeat password: '): raise ValueError('两次口令不一致')
    return first


def initialize(args):
    if CONFIG.exists(): raise ValueError('演示配置已存在，不覆盖旧账号/密钥；修改网址请用--set-public-url')
    if not args.public_url or not args.project_root: raise ValueError('--init需要--public-url和--project-root')
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}',args.username) or args.username.lower() in {'admin','test'}:
        raise ValueError('请选择独立演示账号，例如judge_demo')
    from urllib.parse import urlsplit
    url=urlsplit(args.public_url)
    if url.scheme!='https' or not url.hostname or url.path not in {'','/'} or url.query or url.fragment or url.username or url.password or url.port not in {None,443}:
        raise ValueError('请输入实际分配给服务器的HTTPS主机网址，不带路径')
    from app import app
    from agent.identity import load_actor
    from agent.project_folders import validate_root
    from agent import permissions,store
    from services.user_service import user_service
    from werkzeug.security import generate_password_hash
    with app.app_context():
        with store.connect() as conn:
            admin=conn.execute('SELECT id FROM user WHERE role=1 AND status=1 ORDER BY id LIMIT 1').fetchone()
            if conn.execute('SELECT 1 FROM user WHERE username=?',(args.username,)).fetchone(): raise ValueError('账号已存在，未修改其密码')
            root=validate_root(load_actor(admin[0],source='local-demo-setup'),args.project_root,conn)
        access=prompt_password('Demo entry password (not your API key): ','HX_ACCESS')
        password=prompt_password('Website judge account password: ','HX_ACCOUNT')
        user_service.register(args.username,password,'评委演示','','')
        with store.connect() as conn:
            uid=conn.execute('SELECT id FROM user WHERE username=?',(args.username,)).fetchone()[0]
        # Trusted local maintenance binds an ordinary user's project. No public binding API is added.
        actor=load_actor(uid,source='local-demo-setup')
        project=permissions.create_project(actor,'评委演示项目')
        with store.connect() as conn:
            root=validate_root(load_actor(admin[0],source='local-demo-setup'),root,conn,project['id'])
            conn.execute('UPDATE agent_project SET root_directory=? WHERE id=? AND owner_id=?',(root,project['id'],uid));conn.commit()
        from deploy.demo_seed import seed_demo_workspace
        seed_demo_workspace(root)
        _attach_demo_model(actor,args)
        config={'public_url':args.public_url.rstrip('/'),'username':args.username,'project_id':project['id'],
            'demo_workspace_root':root,'allowed_models':_allowed_models(args),
            'access_password_hash':generate_password_hash(access,method='scrypt'),'session_secret':secrets.token_urlsafe(48)}
        with CONFIG.open('x',encoding='utf-8') as file: json.dump(config,file,ensure_ascii=False,indent=2)
        try: CONFIG.chmod(0o600)
        except OSError: pass
    print('Initialized isolated demo. Username='+args.username+'; workspace='+str(root)+'; no passwords printed. Run --setup-local to prepare resources.')


def _allowed_models(args):
    """演示环境允许客户端切换的模型清单；未配置时等同于只允许当前模型。"""
    raw=(os.environ.get('HX_LLM_MODELS') or '').strip() or (args.llm_models or '')
    names=[item.strip() for item in raw.split(',') if item.strip()]
    if not names:
        current=(args.llm_model or os.environ.get('HX_LLM_MODEL') or 'deepseek-chat').strip()
        names=[current] if current else []
    seen=[]
    for name in names:
        if name not in seen: seen.append(name)
    return seen


def _attach_demo_model(actor,args):
    """可选：预置演示账号的大模型连接（Key 加密入库），评委不必自己填。"""
    key=(args.llm_key or os.environ.get('HX_LLM_KEY') or '').strip()
    if not key:return False
    from agent.connections import save_connection
    save_connection(actor,
        provider=(args.llm_provider or os.environ.get('HX_LLM_PROVIDER') or 'custom').strip().lower(),
        api_url=(args.llm_url or os.environ.get('HX_LLM_URL') or 'https://api.deepseek.com/v1').strip(),
        model=(args.llm_model or os.environ.get('HX_LLM_MODEL') or 'deepseek-chat').strip(),
        api_key=key)
    return True


def check_ports(ports):
    sockets=[]
    try:
        for port in ports:
            sock=socket.socket();sockets.append(sock)
            sock.bind(('127.0.0.1',port))
    except OSError as exc: raise ValueError('演示端口被占用；不停止其他进程。先关闭本部署旧进程或本机准备模式。') from exc
    finally:
        for sock in sockets:sock.close()


def build_app(config):
    from app import app
    from agent.store import connect
    from utils.jwt_util import get_user_id
    from deploy.gateway import install_gateway
    def login_allowed(username):
        if username!=config['username']: return False
        with connect() as conn:
            row=conn.execute('SELECT role,status,password FROM user WHERE username=?',(username,)).fetchone()
        return bool(row and row[0]==0 and row[1]==1 and row[2]!=hashlib.md5(b'123456').hexdigest())
    def token_allowed(token):
        uid=get_user_id(token)
        if not uid:return False
        with connect() as conn:
            row=conn.execute('SELECT username,role,status FROM user WHERE id=?',(uid,)).fetchone()
        return bool(row and row[0]==config['username'] and row[1]==0 and row[2]==1)
    # 演示账号只被允许操作这一个工作区；旧配置没有该字段时按已绑定的项目回查。
    demo_root=config.get('demo_workspace_root')
    if not demo_root:
        with connect() as conn:
            row=conn.execute('SELECT root_directory FROM agent_project WHERE id=?',(config.get('project_id'),)).fetchone()
        demo_root=row[0] if row else None
    if demo_root:app.config['DEMO_WORKSPACE_ROOT']=demo_root
    allowed_models=config.get('allowed_models') or []
    if allowed_models:app.config['DEMO_ALLOWED_MODELS']=list(allowed_models)
    if not login_allowed(config['username']):raise ValueError('评委账号不存在、不是普通账号或仍使用默认密码')
    install_gateway(app,DIST,config,login_allowed=login_allowed,token_allowed=token_allowed)
    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--init',action='store_true');parser.add_argument('--check',action='store_true')
    parser.add_argument('--setup-local',action='store_true');parser.add_argument('--public-url')
    parser.add_argument('--set-public-url',action='store_true')
    parser.add_argument('--project-root');parser.add_argument('--username',default='judge_demo')
    parser.add_argument('--llm-key');parser.add_argument('--llm-url');parser.add_argument('--llm-model');parser.add_argument('--llm-provider')
    parser.add_argument('--llm-models',help='逗号分隔的可切换模型清单（演示环境白名单）')
    args=parser.parse_args();environment()
    if args.init:initialize(args);return
    config=load_config()
    if args.set_public_url:
        from urllib.parse import urlsplit
        url=urlsplit(args.public_url or '')
        if url.scheme!='https' or not url.hostname or url.path not in {'','/'} or url.query or url.fragment or url.username or url.password or url.port not in {None,443}:raise ValueError('需要实际HTTPS网址')
        config['public_url']=args.public_url.rstrip('/')
        CONFIG.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');print('Public URL updated; restart required.');return
    if importlib.util.find_spec('waitress') is None:raise ValueError('请先安装deploy/requirements.txt')
    binary=os.environ.get('HX_ENGINE_BIN') or shutil.which('hxengine')
    if not binary or not Path(binary).is_file():raise ValueError('请先设置HX_ENGINE_BIN为实际存在的hxengine.exe路径')
    from waitress import create_server
    from deploy.gateway import public_entry,private_bridge
    app=build_app(config)
    port=8091 if args.setup_local else 8080
    check_ports([port,8092])
    if args.check:
        print('CHECK_OK: demo DB is isolated; UI/API 8080, private MCP 8092, local preparation 8091.');return
    bridge=create_server(private_bridge(app),host='127.0.0.1',port=8092,threads=8,expose_tracebacks=False)
    try:
        server=create_server(app if args.setup_local else public_entry(app),host='127.0.0.1',port=port,threads=12,
                             expose_tracebacks=False,max_request_body_size=256*1024*1024,channel_timeout=900)
        threading.Thread(target=bridge.run,daemon=True).start()
        print('LOCAL PREPARATION ONLY: http://127.0.0.1:8091' if args.setup_local else 'DEMO READY: 127.0.0.1:8080; enable Funnel separately after checks.',flush=True)
        try:server.run()
        finally:server.close()
    finally:
        bridge.close()
        runtime=app.extensions.get('haixun_engine')
        if runtime:runtime.pool.close()


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:pass
    except Exception as exc:
        print('DEPLOY_FAILED: '+str(exc),file=sys.stderr);sys.exit(1)
