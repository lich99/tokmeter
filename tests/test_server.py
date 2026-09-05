import json
import threading
import urllib.error
import urllib.request

from tokmeter.server import bind


def test_server_serves_offline_assets_and_bounds_api(store):
    store.refresh()
    server = bind(store, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        html = urllib.request.urlopen(url).read().decode()
        assert "cdn.jsdelivr" not in html and "fonts.googleapis" not in html
        assert urllib.request.urlopen(url + "/static/app.js").status == 200
        status = json.load(urllib.request.urlopen(url + "/api/status"))
        assert status["persistentCache"] is False
        request = urllib.request.Request(url + "/api/refresh", method="POST")
        assert urllib.request.urlopen(request).status == 202
        for path in [
            "/api/aggregate?from=2&to=1",
            "/api/aggregate?from=0&to=999999999999999999",
            "/static/../../prices.json",
        ]:
            try:
                urllib.request.urlopen(url + path)
            except urllib.error.HTTPError as e:
                assert e.code in (400, 404)
            else:
                raise AssertionError(path)
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers={"Host": "attacker.example"}))
        except urllib.error.HTTPError as e:
            assert e.code == 403
        else:
            raise AssertionError("Untrusted Host accepted")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
