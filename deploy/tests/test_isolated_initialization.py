"""Fresh Python process: verify real application imports use only the demo database."""
from pathlib import Path
import subprocess
import sys
import tempfile


def test_real_init_and_site_login_stay_in_isolated_state():
    repo=Path(__file__).resolve().parents[2]
    base=repo/'.integration-checks';base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base,prefix='remote-demo-') as directory:
        script=r'''
from pathlib import Path
import json,sqlite3,sys
from types import SimpleNamespace
from unittest.mock import patch
from werkzeug.test import Client
from werkzeug.wrappers import Response
import deploy.serve as serve
from deploy.gateway import public_entry
root=Path(sys.argv[1]);serve.STATE=root/'state';serve.CONFIG=serve.STATE/'server.local.json'
serve.DIST=root/'dist';serve.DIST.mkdir();(serve.DIST/'index.html').write_text('isolated')
project=root/'materials';project.mkdir()
serve.environment()
args=SimpleNamespace(public_url='https://fixture.example.ts.net',username='judge_fixture',project_root=str(project),
    llm_key=None,llm_provider=None,llm_url=None,llm_model=None,llm_models=None)
with patch.object(serve,'prompt_password',side_effect=['FixtureEntry!2026abcd','FixtureAccount!2026xyz']):serve.initialize(args)
config=serve.load_config();app=serve.build_app(config)
client=Client(public_entry(app),Response)
assert client.post('/__demo_access',data={'password':'FixtureEntry!2026abcd'},base_url=args.public_url).status_code==302
assert client.post('/api/user/login',json={'username':'admin','password':'123456'},base_url=args.public_url).status_code==403
response=client.post('/api/user/login',json={'username':args.username,'password':'FixtureAccount!2026xyz'},base_url=args.public_url)
assert response.status_code==200 and response.json['code']==200,response.status_code
token=response.json['data']['token']
response=client.get('/api/agent/projects',base_url=args.public_url,headers={'Authorization':'Bearer '+token})
assert response.status_code==200
assert any(row['root_directory']==str(project.resolve()) for row in response.json['data'])
c=sqlite3.connect(serve.STATE/'site.db')
assert c.execute('SELECT COUNT(*) FROM models').fetchone()[0]==0
assert c.execute('SELECT COUNT(*) FROM datasets').fetchone()[0]==0
assert 'FixtureEntry!2026abcd' not in serve.CONFIG.read_text(encoding='utf-8')
print('ISOLATED_INIT_AND_LOGIN_OK')
'''
        result=subprocess.run([sys.executable,'-c',script,directory],cwd=repo,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
        assert result.returncode==0,(result.stdout[-1000:]+result.stderr[-1800:])
        assert 'ISOLATED_INIT_AND_LOGIN_OK' in result.stdout
