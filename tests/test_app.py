"""実際の HTTP サーバーと一時 SQLite を使った基本操作確認。"""
from pathlib import Path
import tempfile
import io
import os
from types import SimpleNamespace
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import app
import db


class AppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.ROOT)
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.addCleanup(self.temp.cleanup)
        self.offline = os.environ.get('ASSET_TEST_OFFLINE') == '1'
        if self.offline:
            db.initialize(self.path)
            self.server = SimpleNamespace(db_path=self.path)
            return
        self.server = app.create_server(0, self.path)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        if self.offline:
            return
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path='/', values=None):
        data = None if values is None else urlencode(values).encode()
        if self.offline:
            class LocalRequest:
                def __init__(self, raw):
                    self.reader = io.BytesIO(raw)
                    self.writer = io.BytesIO()
                def makefile(self, mode, *args):
                    return self.reader if mode == 'rb' else self.writer
                def sendall(self, output):
                    self.writer.write(output)
            method = 'GET' if data is None else 'POST'
            raw = f'{method} {path} HTTP/1.0\r\nHost: localhost\r\n'.encode()
            if data is not None:
                raw += f'Content-Length: {len(data)}\r\nContent-Type: application/x-www-form-urlencoded\r\n'.encode()
            request = LocalRequest(raw + b'\r\n' + (data or b''))
            app.Handler(request, ('127.0.0.1', 12345), self.server)
            headers, body = request.writer.getvalue().split(b'\r\n\r\n', 1)
            status = int(headers.split()[1])
            if status == 303:
                location = next(line.split(b': ', 1)[1].decode() for line in headers.split(b'\r\n') if line.startswith(b'Location:'))
                return self.request(location)
            return status, body.decode()
        try:
            with urlopen(Request(self.base + path, data=data), timeout=5) as response:
                return response.status, response.read().decode()
        except HTTPError as error:
            return error.code, error.read().decode()

    def values(self, **changes):
        values = {key: '' for key in db.FIELDS}
        values.update(asset_number='TEST-001', model='検証PC', status='在庫', csrf_token=app.CSRF_TOKEN)
        values.update(changes)
        return values

    def test_crud_search_and_persistence(self):
        status, page = self.request()
        self.assertEqual(status, 200)
        self.assertIn('検索結果：5 件', page)
        for query in ('PC-001', '山田', 'ThinkPad'):
            status, page = self.request('/?' + urlencode({'q': query}))
            self.assertIn('検索結果：1 件', page)
        self.assertIn('検索結果：0 件', self.request('/?q=%25')[1])
        self.assertIn('該当する資産はありません', self.request('/?q=ZZZZZ')[1])
        self.assertEqual(self.request('/assets/new')[0], 200)
        self.assertIn('資産を登録しました', self.request('/assets/new', self.values(notes='<script>alert(1)</script>'))[1])
        with db.connect(self.path) as connection:
            asset = connection.execute("SELECT * FROM assets WHERE asset_number='TEST-001'").fetchone()
            asset_id = asset['id']
            self.assertTrue(asset['created_at'])
        edit_url = f'/assets/{asset_id}/edit'
        self.assertIn('&lt;script&gt;', self.request(edit_url)[1])
        self.assertIn('資産を更新しました', self.request(edit_url, self.values(user_name='確認担当', status='使用中'))[1])
        self.assertIn('確認担当', self.request('/?' + urlencode({'q': '確認担当'}))[1])
        db.initialize(self.path)
        with db.connect(self.path) as connection:
            self.assertEqual(len(db.list_assets(connection)), 6)
            self.assertEqual(connection.execute('SELECT status FROM assets WHERE id=?', (asset_id,)).fetchone()[0], '使用中')
        delete_url = f'/assets/{asset_id}/delete'
        self.assertIn('削除の確認', self.request(delete_url)[1])
        with db.connect(self.path) as connection:
            self.assertEqual(len(db.list_assets(connection)), 6)
        self.assertIn('資産を削除しました', self.request(delete_url, {'csrf_token': app.CSRF_TOKEN})[1])
        self.assertEqual(self.request(edit_url)[0], 404)
        self.assertEqual(self.request(delete_url, {'csrf_token': app.CSRF_TOKEN})[0], 404)

    def test_validation(self):
        for changes, message in [({'asset_number': ''}, '管理番号を入力'),
                                 ({'asset_number': 'PC-001'}, '既に登録'),
                                 ({'status': '不明'}, '状態を選択'),
                                 ({'purchase_date': '2026-02-30'}, '正しい日付'),
                                 ({'purchase_date': '2026-01-01', 'warranty_date': '2025-01-01'}, '購入日以降'),
                                 ({'notes': 'あ' * 2001}, '2000文字以内')]:
            status, page = self.request('/assets/new', self.values(**changes))
            self.assertEqual(status, 400)
            self.assertIn(message, page)
        self.assertEqual(self.request('/assets/new', self.values(csrf_token='不正'))[0], 403)
        self.assertEqual(self.request('/missing')[0], 404)
        self.assertEqual(self.request('/static/style.css')[0], 200)
        with db.connect(self.path) as connection:
            self.assertEqual(len(db.list_assets(connection)), 5)

    def test_no_reseed_after_deleting_all(self):
        with db.connect(self.path) as connection:
            connection.execute('DELETE FROM assets')
        db.initialize(self.path)
        with db.connect(self.path) as connection:
            self.assertEqual(len(db.list_assets(connection)), 0)


if __name__ == '__main__':
    unittest.main()
