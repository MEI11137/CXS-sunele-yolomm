import threading
import urllib.request
import http.cookiejar
import urllib.parse

import pytest
from werkzeug.test import Client
from werkzeug.wrappers import Response
from test_gateway import gateway
from deploy.gateway import public_entry,private_bridge


def test_real_waitress_keeps_sse_and_private_boundary(gateway):
    waitress=pytest.importorskip('waitress')
    app,_,_=gateway
    public=waitress.create_server(public_entry(app),host='127.0.0.1',port=0,threads=4)
    private=waitress.create_server(private_bridge(app),host='127.0.0.1',port=0,threads=2)
    threads=[threading.Thread(target=s.run,daemon=True) for s in [public,private]]
    for t in threads:t.start()
    try:
        origin='http://127.0.0.1:'+str(public.effective_port)
        # Get the genuine signed gate cookie, then exercise real sockets. External clients use HTTPS.
        signed=Client(public_entry(app),Response).post('/__demo_access',data={'password':'strong-demo-password!'},base_url='https://judge.example.ts.net')
        cookie=signed.headers['Set-Cookie'].split(';',1)[0]
        request=urllib.request.Request(origin+'/api/agent/sessions/s/events/stream',headers={'Cookie':cookie,'Authorization':'Bearer judge-token'})
        with urllib.request.urlopen(request,timeout=5) as response:
            assert response.headers['Content-Type'].startswith('text/event-stream')
            assert response.readline()==b'data: one\n'
            assert response.readline()==b'\n'
            assert response.readline()==b'data: two\n'
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(origin+'/api/agent/engine/bridge/tools',timeout=5)
        assert failure.value.code==401
        private_url='http://127.0.0.1:'+str(private.effective_port)
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(private_url+'/api/echo',timeout=5)
        assert failure.value.code==404
    finally:
        for server in [public,private]:
            server.close();server.task_dispatcher.shutdown(timeout=2)
        for thread in threads:thread.join(timeout=2)
