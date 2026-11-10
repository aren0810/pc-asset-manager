"""追加依存のないローカル向け PC 資産管理 Web アプリ。"""
import argparse
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
        self.send_page(title, render('form.html', title=title, action=action, token=CSRF_TOKEN,
                                    errors=error_block, fields=''.join(controls), timestamps=timestamps), status)

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
                assets = db.list_assets(connection, query)
                rows = []
                for asset in assets:
                    cells = ''.join(f'<td>{html(asset[key])}</td>' for key in ('asset_number', 'manufacturer', 'model', 'user_name', 'department', 'location', 'status'))
                    rows.append(f'<tr>{cells}<td class="actions"><a href="/assets/{asset["id"]}/edit">編集</a> <a href="/assets/{asset["id"]}/delete">削除</a></td></tr>')
                message = params.get('message', [''])[0]
                self.send_page('資産一覧', render('list.html', query=html(query), count=len(assets),
                    message=f'<p class="notice" role="status">{html(message)}</p>' if message else '',
                    rows=''.join(rows) or '<tr><td colspan="8">該当する資産はありません。</td></tr>'))
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
                        asset_number=html(asset['asset_number']), model=html(asset['model']), token=CSRF_TOKEN))
                return
        self.error_page(404, 'ページが見つかりません。')

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                raise ValueError
            params = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True)
        except (ValueError, UnicodeDecodeError):
            self.error_page(400, '送信データを確認してください。')
            return
        if not secrets.compare_digest(params.get('csrf_token', [''])[0].encode('utf-8'), CSRF_TOKEN.encode('utf-8')):
            self.error_page(403, '画面を再読み込みしてから操作してください。')
            return
        path = urlsplit(self.path).path
        parts = path.strip('/').split('/')
        new = path == '/assets/new'
        if not new and not (len(parts) == 3 and parts[0] == 'assets' and parts[1].isascii() and parts[1].isdigit() and len(parts[1]) < 19 and parts[2] in ('edit', 'delete')):
            self.error_page(404, 'ページが見つかりません。')
            return
        with db.connect(self.server.db_path) as connection:
            asset_id = None if new else int(parts[1])
            previous = None if new else connection.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            if not new and previous is None:
                self.error_page(404, '資産が見つかりません。')
                return
            if not new and parts[2] == 'delete':
                connection.execute('DELETE FROM assets WHERE id=?', (asset_id,))
                connection.commit()
                self.redirect('資産を削除しました。')
                return
            values = {key: params.get(key, [''])[0].strip() for key in db.FIELDS}
            errors = validate(values)
            if not errors:
                try:
                    if new:
                        connection.execute(f"INSERT INTO assets ({','.join(db.FIELDS)}) VALUES ({','.join('?' for _ in db.FIELDS)})", tuple(values.values()))
                    else:
                        connection.execute(f"UPDATE assets SET {','.join(key + '=?' for key in db.FIELDS)}, updated_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=?", (*values.values(), asset_id))
                    connection.commit()
                except sqlite3.IntegrityError:
                    errors.append('その管理番号は既に登録されています。別の管理番号を入力してください。')
            if errors:
                if previous:
                    values.update(created_at=previous['created_at'], updated_at=previous['updated_at'])
                self.form(values, asset_id, errors, 400)
            else:
                self.redirect('資産を登録しました。' if new else '資産を更新しました。')


def create_server(port=8000, db_path=db.DB_PATH):
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
