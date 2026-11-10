"""SQLite の初期化と資産データへのアクセス。"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'data' / 'assets.sqlite3'
STATUSES = ('使用中', '在庫', '修理中', '貸出中', '廃棄')
FIELDS = {
    'asset_number': '管理番号', 'manufacturer': 'メーカー', 'model': '機種名',
    'serial_number': 'シリアル番号', 'os': 'OS', 'purchase_date': '購入日',
    'warranty_date': '保証期限', 'user_name': '利用者', 'department': '部署',
    'location': '設置場所', 'status': '状態', 'notes': '備考',
}


@contextmanager
def connect(path=DB_PATH):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(path=DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        # テーブルの有無で初回を判定。全件削除しても再投入しない。
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'").fetchone()
        connection.execute('''CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY,
            asset_number TEXT NOT NULL UNIQUE CHECK(length(trim(asset_number)) > 0),
            manufacturer TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            serial_number TEXT NOT NULL DEFAULT '', os TEXT NOT NULL DEFAULT '',
            purchase_date TEXT NOT NULL DEFAULT '', warranty_date TEXT NOT NULL DEFAULT '',
            user_name TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('使用中','在庫','修理中','貸出中','廃棄')),
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
        )''')
        if not exists:
            samples = [
                ('PC-001', 'Lenovo', 'ThinkPad T14', 'DEMO-L001', 'Windows 11 Pro', '2025-04-01', '2028-03-31', '山田 太郎', '営業部', '東京本社 2F', '使用中', '営業用。サンプルデータ（架空）。'),
                ('PC-002', 'Dell', 'Latitude 5450', 'DEMO-D002', 'Windows 11 Pro', '2025-06-10', '2028-06-09', '', '情報システム部', '東京本社 保管庫', '在庫', '入社者向け予備機。'),
                ('PC-003', 'HP', 'EliteBook 840', 'DEMO-H003', 'Windows 11 Pro', '2024-02-15', '2027-02-14', '佐藤 花子', '経理部', '大阪支店', '修理中', 'キーボード不良。修理受付済み。'),
                ('PC-004', 'Apple', 'MacBook Air', 'DEMO-A004', 'macOS', '2025-09-01', '2026-08-31', '鈴木 一郎', '企画部', '東京本社 3F', '貸出中', '展示会準備用。'),
                ('PC-005', '富士通', 'LIFEBOOK U', 'DEMO-F005', 'Windows 10 Pro', '2019-04-01', '2022-03-31', '', '総務部', '廃棄済み', '廃棄', 'データ消去・廃棄証明書保管済み。'),
            ]
            connection.executemany(f"INSERT INTO assets ({','.join(FIELDS)}) VALUES ({','.join('?' for _ in FIELDS)})", samples)


def list_assets(connection, query=''):
    # LIKE の特殊文字も通常の検索文字として扱う。
    pattern = '%' + query.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
    return connection.execute("""SELECT * FROM assets WHERE
        asset_number LIKE ? ESCAPE '!' OR user_name LIKE ? ESCAPE '!' OR model LIKE ? ESCAPE '!'
        ORDER BY asset_number""", (pattern, pattern, pattern)).fetchall()
