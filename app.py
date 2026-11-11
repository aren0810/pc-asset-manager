"""追加依存のないローカル向け PC 資産管理 Web アプリ。"""
import argparse
import json
from datetime import date
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sqlite3
from string import Template
from urllib.parse import parse_qs, urlencode, urlsplit

import db
import phase2

ROOT = Path(__file__).resolve().parent
CSRF_TOKEN = secrets.token_urlsafe(32)


def html(value):
    return escape(str(value), quote=True)


def render(name, **values):
    return Template((ROOT / 'templates' / name).read_text(encoding='utf-8')).substitute(values)


def validate(values):
    errors = []
    if not values['asset_number']:
        errors.append('管理番号を入力してください。')
    for key, label in db.FIELDS.items():
        limit = 2000 if key == 'notes' else 200
        if '\x00' in values[key]:
            errors.append(f'{label}に使用できない文字が含まれています。')
        if len(values[key]) > limit:
            errors.append(f'{label}は{limit}文字以内で入力してください。')
    if values['status'] not in db.STATUSES:
        errors.append('状態を選択してください。')
    for key in ('purchase_date', 'warranty_date'):
        if values[key]:
            try:
                if date.fromisoformat(values[key]).isoformat() != values[key]:
                    raise ValueError
            except ValueError:
                errors.append(f'{db.FIELDS[key]}は正しい日付（YYYY-MM-DD）を入力してください。')
    if not errors and values['purchase_date'] and values['warranty_date'] and values['purchase_date'] > values['warranty_date']:
        errors.append('保証期限は購入日以降にしてください。')
    return errors


class Handler(BaseHTTPRequestHandler):
    def send_page(self, title, content, status=200):
        body = render('base.html', title=html(title), content=content).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def error_page(self, status, message):
        self.send_page('エラー', f'<h1>エラー</h1><p role="alert">{html(message)}</p><a href="/">一覧へ戻る</a>', status)

    def redirect(self, message):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header('Location', '/?' + urlencode({'message': message}))
        self.end_headers()

    def form(self, values, asset_id=None, errors=(), status=200):
        controls = []
        for key, label in db.FIELDS.items():
            value = html(values.get(key, ''))
            if key == 'status':
                control = '<select id="status" name="status">' + ''.join(
                    f'<option{ " selected" if values.get(key, "在庫") == item else ""}>{item}</option>' for item in db.STATUSES) + '</select>'
            elif key == 'notes':
                control = f'<textarea id="{key}" name="{key}" maxlength="2000" rows="4">{value}</textarea>'
            else:
                kind = 'date' if key in ('purchase_date', 'warranty_date') else 'text'
                required = ' required' if key == 'asset_number' else ''
                control = f'<input id="{key}" name="{key}" type="{kind}" value="{value}" maxlength="200"{required}>'
            controls.append(f'<div class="field"><label for="{key}">{label}{"（必須）" if key == "asset_number" else ""}</label>{control}</div>')
        title = '資産の編集' if asset_id else '資産の新規登録'
        action = f'/assets/{asset_id}/edit' if asset_id else '/assets/new'
        error_block = '<ul class="errors" role="alert">' + ''.join(f'<li>{html(e)}</li>' for e in errors) + '</ul>' if errors else ''
        timestamps = '' if not values.get('created_at') else f'<p class="muted">登録日時: {html(values["created_at"])} ／ 更新日時: {html(values["updated_at"])}（UTC）</p>'
        controls.append(f'<input type="hidden" name="version" value="{html(values.get("updated_at", ""))}">')
        self.send_page(title, render('form.html', title=title, action=action, token=CSRF_TOKEN,
                                    errors=error_block, fields=''.join(controls), timestamps=timestamps), status)

    def import_form(self, errors=(), status=200):
        messages = '<ul class="errors" role="alert">' + ''.join(f'<li>{html(e)}</li>' for e in errors) + '</ul>' if errors else ''
        self.send_page('CSVインポート', '<h1>CSVインポート</h1><p>CSV出力ファイル、またはExcelで「CSV UTF-8（コンマ区切り）」として保存したファイルを選択してください（2MB・1万行まで）。管理番号が一致すれば全項目を更新、一致しなければ登録します。空欄は既存値を消去します。1行でもエラーがあれば全件反映しません。</p><p>取込前にCSV出力で現状を保存してください。列名・列順はCSV出力と揃えてください。先頭ゼロのある管理番号やシリアル番号はExcelで文字列として扱ってください。</p>' + messages + f'<form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{CSRF_TOKEN}"><label for="csv">CSVファイル（UTF-8）</label><input id="csv" name="csv" type="file" accept=".csv,text/csv" required><button type="submit">一括登録・更新する</button></form><a href="/">一覧へ戻る</a>', status)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/static/style.css':
            body = (ROOT / 'static' / 'style.css').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/css; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        with db.connect(self.server.db_path) as connection:
            if parsed.path == '/':
                params = parse_qs(parsed.query)
                query = params.get('q', [''])[0].strip()
                assets = phase2.select_assets(connection, params)
                count = len(assets)
                try:
                    page = max(1, min(int(params.get('page', ['1'])[0]), max(1, (count+24)//25)))
                except ValueError:
                    page = 1
                rows = []
                for asset in assets[(page-1)*25:page*25]:
                    cells = ''.join(f'<td>{html(asset[key])}</td>' for key in ('asset_number', 'manufacturer', 'model', 'user_name', 'department', 'location', 'status'))
                    warning = phase2.warranty(asset)
                    rows.append(f'<tr>{cells}<td>{html(warning)}</td><td class="actions"><a href="/assets/{asset["id"]}/edit">編集</a> <a href="/assets/{asset["id"]}/history">履歴</a> <a href="/assets/{asset["id"]}/delete">削除</a></td></tr>')
                get = lambda k, default='': params.get(k, [default])[0]
                def options(items, selected):
                    return ''.join(f'<option value="{html(v)}"{" selected" if v == selected else ""}>{html(label)}</option>' for v, label in items)
                departments = [r[0] for r in connection.execute("SELECT DISTINCT department FROM assets WHERE department<>'' ORDER BY department")]
                filters = '<label>状態<select name="status">' + options([('', 'すべて')] + [(v,v) for v in db.STATUSES], get('status')) + '</select></label>'
                filters += '<label>部署<select name="department">' + options([('', 'すべて')] + [(v,v) for v in departments], get('department')) + '</select></label>'
                filters += '<label>並び順<select name="sort">' + options([(k,db.FIELDS.get(k,'更新日時')) for k in ('asset_number','department','user_name','purchase_date','warranty_date','updated_at')], get('sort','asset_number')) + '</select></label>'
                filters += '<label>方向<select name="order">' + options([('asc','昇順'),('desc','降順')],get('order','asc')) + '</select></label>'
                conditions = {k:get(k) for k in ('q','status','department','sort','order')}
                links = ' '.join(f'<a href="/?{html(urlencode(dict(conditions,page=n)))}">{label}</a>' for n,label in ((page-1,'前へ'),(page+1,'次へ')) if 1 <= n <= max(1,(count+24)//25))
                message = get('message')
                self.send_page('資産一覧', render('list.html', query=html(query), count=count,
                    filters=filters, pagination=f'{page} / {max(1,(count+24)//25)} ページ（25件ずつ） {links}',
                    export_url=html('/assets/export?' + urlencode(conditions)),
                    message=f'<p class="notice" role="status">{html(message)}</p>' if message else '',
                    rows=''.join(rows) or '<tr><td colspan="9">該当する資産はありません。</td></tr>'))
                return
            if parsed.path == '/assets/export':
                body = phase2.export_csv(phase2.select_assets(connection, parse_qs(parsed.query)))
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv; charset=utf-8')
                self.send_header('Content-Disposition', 'attachment; filename="assets.csv"')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == '/assets/import':
                self.import_form()
                return
            if parsed.path == '/history' or parsed.path.endswith('/history'):
                parts = parsed.path.strip('/').split('/')
                if parsed.path != '/history' and not (len(parts) == 3 and parts[0] == 'assets' and parts[1].isascii() and parts[1].isdigit() and len(parts[1]) < 19):
                    self.error_page(404, 'ページが見つかりません。')
                    return
                records = connection.execute('SELECT * FROM asset_history' + (' WHERE asset_id=?' if len(parts)==3 else '') + ' ORDER BY id DESC', (int(parts[1]),) if len(parts)==3 else ()).fetchall()
                blocks = []
                for record in records:
                    before, after = json.loads(record['before_json']), json.loads(record['after_json'])
                    changes = ''.join(f'<li>{label}: {html(before.get(k,"（未登録）"))} → {html(after.get(k,"（削除）"))}</li>' for k,label in db.FIELDS.items() if before.get(k) != after.get(k))
                    blocks.append(f'<article><h2>{html(record["changed_at"])} UTC ／ {html(record["action"])} ／ {html(after.get("asset_number",before.get("asset_number","")))}</h2><ul>{changes}</ul></article>')
                self.send_page('変更履歴', '<h1>変更履歴</h1><p>Phase 2導入後の変更を記録します。削除履歴も保持します。日時はUTCです。</p>' + ''.join(blocks) + ('<p>履歴はありません。</p>' if not blocks else '') + '<a href="/">一覧へ戻る</a>')
                return
            if parsed.path == '/assets/new':
                self.form({})
                return
            parts = parsed.path.strip('/').split('/')
            if len(parts) == 3 and parts[0] == 'assets' and parts[1].isascii() and parts[1].isdigit() and len(parts[1]) < 19 and parts[2] in ('edit', 'delete'):
                asset = connection.execute('SELECT * FROM assets WHERE id=?', (int(parts[1]),)).fetchone()
                if asset is None:
                    self.error_page(404, '資産が見つかりません。')
                elif parts[2] == 'edit':
                    self.form(dict(asset), asset['id'])
                else:
                    self.send_page('削除の確認', render('delete.html', asset_id=asset['id'],
                        asset_number=html(asset['asset_number']), model=html(asset['model']), token=CSRF_TOKEN, version=html(asset['updated_at'])))
                return
        self.error_page(404, 'ページが見つかりません。')

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 12_000_000:
                raise ValueError
            raw = self.rfile.read(length)
            content_type = self.headers.get('Content-Type', '')
            if content_type.startswith('multipart/form-data'):
                from email.parser import BytesParser
                from email.policy import default
                message = BytesParser(policy=default).parsebytes(('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + raw)
                if not message.is_multipart() or message.defects:
                    raise ValueError
                params = {}
                for part in message.iter_parts():
                    name = part.get_param('name', header='content-disposition')
                    if part.defects or name in params:
                        raise ValueError
                    if name in ('csv', 'csrf_token'):
                        params[name] = [(part.get_payload(decode=True) or b'').decode('utf-8-sig')]
            else:
                params = parse_qs(raw.decode('utf-8'), keep_blank_values=True)
        except (ValueError, UnicodeDecodeError):
            self.error_page(400, '送信データを確認してください。CSVファイルはUTF-8で保存してください。')
            return
        if not secrets.compare_digest(params.get('csrf_token', [''])[0].encode('utf-8'), CSRF_TOKEN.encode('utf-8')):
            self.error_page(403, '画面を再読み込みしてから操作してください。')
            return
        path = urlsplit(self.path).path
        if path == '/assets/import':
            content = params.get('csv', [''])[0]
            if len(content.encode('utf-8')) > phase2.MAX_CSV_BYTES:
                self.import_form(errors=['CSVはUTF-8で2MB以内にしてください。'], status=400)
                return
            with db.connect(self.server.db_path) as connection:
                connection.execute('BEGIN IMMEDIATE')
                errors, count = phase2.import_csv(connection, content, validate)
            if errors:
                self.import_form(errors, 400)
            else:
                self.redirect(f'CSV {count}件を処理しました。変更内容は履歴で確認できます。')
            return
        parts = path.strip('/').split('/')
        new = path == '/assets/new'
        if not new and not (len(parts) == 3 and parts[0] == 'assets' and parts[1].isascii() and parts[1].isdigit() and len(parts[1]) < 19 and parts[2] in ('edit', 'delete')):
            self.error_page(404, 'ページが見つかりません。')
            return
        with db.connect(self.server.db_path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            asset_id = None if new else int(parts[1])
            previous = None if new else connection.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            if not new and previous is None:
                self.error_page(404, '資産が見つかりません。')
                return
            if not new and params.get('version', [''])[0] != previous['updated_at']:
                self.error_page(409, '他の画面で更新されています。再読み込みして変更内容を確認してください。')
                return
            if not new and parts[2] == 'delete':
                db.history(connection, asset_id, '削除', {k: previous[k] for k in db.FIELDS}, {})
                connection.execute('DELETE FROM assets WHERE id=?', (asset_id,))
                connection.commit()
                self.redirect('資産を削除しました。')
                return
            values = {key: params.get(key, [''])[0].strip() for key in db.FIELDS}
            errors = validate(values)
            if not errors:
                try:
                    db.save(connection, values, previous)
                    connection.commit()
                except sqlite3.IntegrityError:
                    connection.rollback()
                    errors.append('その管理番号は既に登録されています。別の管理番号を入力してください。')
            if errors:
                if previous:
                    values.update(created_at=previous['created_at'], updated_at=previous['updated_at'])
                self.form(values, asset_id, errors, 400)
            else:
                self.redirect('資産を登録しました。' if new else '資産を更新しました。')


def create_server(port=8000, db_path=db.DB_PATH):
    db.backup(db_path)
    db.initialize(db_path)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.db_path = db_path
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PC資産管理を起動します。')
    parser.add_argument('--port', type=int, default=8000, help='待受ポート（既定: 8000）')
    args = parser.parse_args()
    with create_server(args.port) as server:
        print(f'PC資産管理: http://127.0.0.1:{server.server_port} （終了: Ctrl+C）', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\n終了しました。')
