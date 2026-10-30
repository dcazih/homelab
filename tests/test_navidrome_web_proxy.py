import sys, unittest
import httpx
from fastapi.testclient import TestClient
sys.path.insert(0,'/app')
import main

class WebProxyTests(unittest.TestCase):
    def test_web_root_forwards_and_rewrites_redirect(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(302,headers={'location':'http://navidrome.test:4533/app/'})
        app=main.create_app('http://navidrome.test:4533',httpx.MockTransport(handler))
        with TestClient(app,follow_redirects=False) as client:
            response=client.get('/')
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.headers['location'],'/app/')
        self.assertEqual(seen[0].url.path,'/')

    def test_web_request_passes_cookie_without_shared_cookie_jar(self):
        def handler(request):
            return httpx.Response(200,text=request.headers.get('cookie','none'))
        app=main.create_app('http://navidrome.test:4533',httpx.MockTransport(handler))
        with TestClient(app) as client:
            self.assertEqual(client.get('/app/',headers={'cookie':'session=test'}).text,'session=test')
            self.assertEqual(client.get('/app/').text,'none')

if __name__=='__main__':unittest.main()
