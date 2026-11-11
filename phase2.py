"""CSV台帳連携と一覧条件。HTTPに依存しない処理。"""
import csv
import io
from datetime import date

import db

MAX_CSV_BYTES = 2_000_000


def select_assets(connection, params):
    get = lambda key, default='': params.get(key, [default])[0]
    assets = db.list_assets(connection, get('q').strip())
    assets = [a for a in assets if (not get('status') or a['status'] == get('status'))
              and (not get('department') or a['department'] == get('department'))]
    key = get('sort', 'asset_number')
    if key not in ('asset_number', 'department', 'user_name', 'purchase_date', 'warranty_date', 'updated_at'):
        key = 'asset_number'
    return sorted(assets, key=lambda a: (a[key], a['id']), reverse=get('order') == 'desc')


def warranty(asset, today=None):
    if not asset['warranty_date'] or asset['status'] == '廃棄':
        return ''
    days = (date.fromisoformat(asset['warranty_date']) - (today or date.today())).days
    return '保証期限切れ' if days < 0 else f'保証残り{days}日' if days <= 90 else ''


def export_csv(assets):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(db.FIELDS.values())
    for asset in assets:
        # Excelの数式評価を防ぐ。再取込時に元の値へ復元する。
        writer.writerow(["'" + asset[k] if asset[k].startswith(('=', '+', '-', '@', '\t', '\r', '\n', "'")) else asset[k] for k in db.FIELDS])
    return stream.getvalue().encode('utf-8-sig')


def import_csv(connection, content, validate):
    """呼出元が書込みトランザクションを確保。エラー時は一切変更しない。"""
    errors, pending, seen = [], [], set()
    try:
        reader = csv.reader(io.StringIO(content.lstrip('\ufeff'), newline=''), strict=True)
        if next(reader, None) != list(db.FIELDS.values()):
            return ['1行目：列名・列順が違います。CSV出力したファイルの見出しを使用してください。'], 0
        for row in reader:
            line = reader.line_num
            if line > 10001:
                errors.append('CSVは見出しを含め10001行以内にしてください。')
                break
            if len(row) != len(db.FIELDS):
                errors.append(f'{line}行目：列数が違います。')
                continue
            values = {k: (v[1:] if len(v) > 1 and v[0] == "'" and v[1] in "=+-@\t\r\n'" else v).strip() for k, v in zip(db.FIELDS, row)}
            problems = validate(values)
            number = values['asset_number']
            if number in seen:
                problems.append('CSV内で管理番号が重複しています。')
            seen.add(number)
            errors.extend(f'{line}行目：{problem}' for problem in problems)
            previous = connection.execute('SELECT * FROM assets WHERE asset_number=?', (number,)).fetchone()
            pending.append((values, previous))
    except csv.Error:
        errors.append('CSVの引用符またはデータ形式が不正です。')
    if not pending and not errors:
        errors.append('データ行がありません。')
    if errors:
        return errors, 0
    for values, previous in pending:
        db.save(connection, values, previous)
    return [], len(pending)
