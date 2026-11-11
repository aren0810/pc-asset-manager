"""Phase 2の台帳保全・CSV・画面遷移の回帰確認。"""
import csv
from contextlib import closing
from datetime import date, timedelta
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import urlencode

import app
import db
import phase2
import test_app


class Phase2Test(test_app.AppTest):
    # 基底クラスの既存3テストは再実行しない。
    test_crud_search_and_persistence = None
    test_validation = None
    test_no_reseed_after_deleting_all = None

    def csv_text(self, rows):
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(db.FIELDS.values())
        for changes in rows:
            values = self.values(**changes)
            writer.writerow([values[k] for k in db.FIELDS])
        return stream.getvalue()

    def test_csv_atomic_validation_and_upsert_history(self):
        content = self.csv_text([{'asset_number':'PC-001', 'model':'CSV更新'},
                                 {'asset_number':'CSV-NEW', 'purchase_date':'2026-02-30'}])
        status, page = self.request('/assets/import', {'csrf_token':app.CSRF_TOKEN, 'csv':content})
        self.assertEqual(status, 400)
        self.assertIn('3行目', page)
        with db.connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT model FROM assets WHERE id=1').fetchone()[0], 'ThinkPad T14')
            self.assertEqual(connection.execute('SELECT count(*) FROM asset_history').fetchone()[0], 0)
        content = self.csv_text([{'asset_number':'PC-001', 'model':'CSV更新'},
                                 {'asset_number':'CSV-NEW', 'notes':'複数行\n備考,"引用"'}])
        self.assertIn('CSV 2件', self.request('/assets/import', {'csrf_token':app.CSRF_TOKEN, 'csv':content})[1])
        with db.connect(self.path) as connection:
            self.assertEqual(len(db.list_assets(connection)), 6)
            self.assertEqual(connection.execute('SELECT count(*) FROM asset_history').fetchone()[0], 2)
        self.assertIn('CSV更新', self.request('/assets/1/history')[1])
        self.assertIn('複数行', self.request('/history')[1])
        # 同一データの再取込は履歴を増やさない。
        self.request('/assets/import', {'csrf_token':app.CSRF_TOKEN, 'csv':content})
        with db.connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM asset_history').fetchone()[0], 2)

    def test_csv_duplicate_headers_csrf_and_formula_roundtrip(self):
        for content, message in [(self.csv_text([{}, {}]), '重複'), ('管理番号\nX\n', '列名'),
                                  (self.csv_text([]), 'データ行'), (self.csv_text([]) + '"未終了', '引用符')]:
            status, page = self.request('/assets/import', {'csrf_token':app.CSRF_TOKEN, 'csv':content})
            self.assertEqual(status, 400)
            self.assertIn(message, page)
        self.assertEqual(self.request('/assets/import', {'csv':'x'})[0], 403)
        self.request('/assets/new', self.values(asset_number='=1+1', notes="'元の値\n備考"))
        status, text = self.request('/assets/export?' + urlencode({'q':'=1+1'}))
        self.assertEqual(status, 200)
        self.assertTrue(text.startswith('\ufeff'))
        self.assertIn("'=1+1", text)
        self.request('/assets/import', {'csrf_token':app.CSRF_TOKEN, 'csv':text})
        with db.connect(self.path) as connection:
            asset = connection.execute('SELECT * FROM assets WHERE asset_number=?', ('=1+1',)).fetchone()
            self.assertEqual(asset['notes'], "'元の値\n備考")
            self.assertEqual(len(db.list_assets(connection)), 6)

    def test_filters_sort_paging_and_export_all_matches(self):
        with db.connect(self.path) as connection:
            for n in range(30):
                values = {k:self.values(asset_number=f'ZZ-{n:02}', department='検証部署')[k] for k in db.FIELDS}
                db.save(connection, values)
        query = urlencode({'department':'検証部署', 'sort':'asset_number','order':'desc'})
        status, page = self.request('/?' + query)
        self.assertEqual(status, 200)
        self.assertIn('検索結果：30 件', page)
        self.assertIn('ZZ-29', page)
        self.assertNotIn('ZZ-00', page)
        self.assertIn('ZZ-00', self.request('/?' + query + '&page=2')[1])
        exported = self.request('/assets/export?' + query + '&page=1')[1]
        rows = list(csv.reader(io.StringIO(exported.lstrip('\ufeff'))))
        self.assertEqual(len(rows), 31)
        self.assertEqual(rows[1][0], 'ZZ-29')
        self.assertEqual(rows[-1][0], 'ZZ-00')
        self.assertEqual(self.request('/?page=invalid&sort=DROP%20TABLE')[0], 200)
        self.assertIn('検索結果：1 件', self.request('/?status=' + urlencode({'status':'修理中'}).split('=',1)[1])[1])

    def test_conflict_delete_history_and_no_id_reuse(self):
        self.request('/assets/new', self.values())
        with db.connect(self.path) as connection:
            original = connection.execute("SELECT * FROM assets WHERE asset_number='TEST-001'").fetchone()
        url = f'/assets/{original["id"]}'
        self.request(url+'/edit', self.values(model='変更後', version=original['updated_at']))
        self.assertEqual(self.request(url+'/edit', self.values(model='古い画面',version=original['updated_at']))[0], 409)
        self.assertEqual(self.request(url+'/delete', {'csrf_token':app.CSRF_TOKEN, 'version':original['updated_at']})[0], 409)
        with db.connect(self.path) as connection:
            latest = connection.execute('SELECT * FROM assets WHERE id=?', (original['id'],)).fetchone()
        self.request(url+'/delete', {'csrf_token':app.CSRF_TOKEN, 'version':latest['updated_at']})
        self.assertIn('削除', self.request(url+'/history')[1])
        self.assertIn('変更後', self.request('/history')[1])
        self.request('/assets/new', self.values())
        with db.connect(self.path) as connection:
            self.assertGreater(connection.execute("SELECT id FROM assets WHERE asset_number='TEST-001'").fetchone()[0], original['id'])

    def test_multipart_file_upload(self):
        content = self.csv_text([{'asset_number':'UPLOAD-001','department':'日本語部署'}])
        self.assertIn('CSV 1件', self.request('/assets/import', csv_file=content.encode('utf-8-sig'))[1])
        self.assertIn('日本語部署', self.request('/?q=UPLOAD-001')[1])
        self.assertEqual(self.request('/assets/import', csv_file=content.encode('cp932'))[0], 400)
        self.assertIn('CSVファイル', self.request('/assets/import')[1])

    def test_warranty_boundaries(self):
        today = date(2026, 11, 11)
        for days, expected in [(-1,'保証期限切れ'),(0,'保証残り0日'),(90,'保証残り90日'),(91,'')]:
            asset = {'status':'在庫','warranty_date':(today+timedelta(days=days)).isoformat()}
            self.assertEqual(phase2.warranty(asset,today), expected)
        self.assertEqual(phase2.warranty({'status':'廃棄','warranty_date':'2020-01-01'},today),'')
        self.assertEqual(phase2.warranty({'status':'使用中','warranty_date':''},today),'')


class MigrationBackupTest(unittest.TestCase):
    def test_legacy_migration_and_daily_backup_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'assets.sqlite3'
            db.initialize(path)
            # Day 1と同じassetsのみのDBへ戻し、実データ保持を検証。
            with db.connect(path) as connection:
                connection.execute('DROP TABLE asset_history')
                connection.execute('PRAGMA user_version=0')
                connection.execute("UPDATE assets SET notes='既存の日本語台帳' WHERE id=1")
            db.backup(path)
            db.initialize(path)
            db.initialize(path)
            with db.connect(path) as connection:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
                self.assertEqual(connection.execute('SELECT notes FROM assets WHERE id=1').fetchone()[0], '既存の日本語台帳')
                self.assertEqual(connection.execute('SELECT count(*) FROM asset_history').fetchone()[0], 0)
                connection.execute('DELETE FROM assets')
            db.backup(path)
            copies = list((Path(directory)/'backups').glob('*.sqlite3'))
            with closing(sqlite3.connect(copies[0])) as connection:
                self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(connection.execute('SELECT count(*) FROM assets').fetchone()[0], 5)
            for day in range(1,11):
                (Path(directory)/'backups'/f'assets-2020-01-{day:02}.sqlite3').touch()
            db.backup(path)
            self.assertEqual(len(list((Path(directory)/'backups').glob('*.sqlite3'))), 7)
