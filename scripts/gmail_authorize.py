#!/usr/bin/env python3
"""Gmail API(読み取り専用)のリフレッシュトークンを取得し、AWSの .env に書き込む(一度だけ実行する)。

あなたのMac(ブラウザが使える環境)で実行する。トークンやシークレットは画面に出さず、SSHの標準入力でAWSへ渡して
.env(権限600)の GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN を更新する。

事前準備: Google Cloud Consoleで OAuthクライアント(種類=デスクトップアプリ)を作り、JSONをダウンロードする。
  python3 scripts/gmail_authorize.py --client-secret ~/Downloads/client_secret_xxx.json
オプション: --no-upload (AWSへは書かず、認可と確認だけ) / --host ubuntu@52.199.161.97 / --key ~/.ssh/...pem / --remote-dir /home/ubuntu/work/amazon-fba
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
PROFILE_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/profile'
SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
ENV_KEYS = ('GMAIL_CLIENT_ID', 'GMAIL_CLIENT_SECRET', 'GMAIL_REFRESH_TOKEN')


def load_client_secret(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    section = data.get('installed') or data.get('web')
    if not section or not section.get('client_id') or not section.get('client_secret'):
        raise ValueError('client_secret JSON に installed.client_id / client_secret が見つかりません(OAuthクライアントの種類は「デスクトップアプリ」にしてください)')
    return {'client_id': section['client_id'], 'client_secret': section['client_secret']}


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


def build_auth_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    return AUTH_URL + '?' + urllib.parse.urlencode({
        'client_id': client_id, 'redirect_uri': redirect_uri, 'response_type': 'code', 'scope': SCOPE,
        'access_type': 'offline', 'prompt': 'consent', 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256'})


def _post_form(url: str, fields: dict) -> dict:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method='POST',
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Google token endpoint HTTP {exc.code}: {exc.read().decode(errors="ignore")[:300]}') from exc


def exchange_code(client: dict, code: str, redirect_uri: str, verifier: str) -> dict:
    tokens = _post_form(TOKEN_URL, {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri,
                                    'client_id': client['client_id'], 'client_secret': client['client_secret'],
                                    'code_verifier': verifier})
    if not tokens.get('refresh_token'):
        raise RuntimeError('refresh_token が返りませんでした。Googleアカウントの「サードパーティのアクセス」から、このアプリを一度削除して、もう一度実行してください。')
    return tokens


def merge_env(text: str, updates: dict) -> str:
    """.env の本文に updates を反映する(既存キーは置き換え、無ければ末尾に追加。他の行は変えない)。"""
    lines, done = [], set()
    for line in text.splitlines():
        key = line.split('=', 1)[0].strip()
        if key in updates and not line.lstrip().startswith('#') and '=' in line:
            lines.append(f'{key}={updates[key]}')
            done.add(key)
        else:
            lines.append(line)
    for key, value in updates.items():
        if key not in done:
            lines.append(f'{key}={value}')
    return '\n'.join(lines) + '\n'


REMOTE_SCRIPT = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from scripts.gmail_authorize import merge_env
updates = json.load(sys.stdin)
path = os.path.join(sys.argv[1], ".env")
text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
open(path, "w", encoding="utf-8").write(merge_env(text, updates))
os.chmod(path, 0o600)
print("updated", sorted(updates))
'''


def upload_env(updates: dict, host: str, key: str | None, remote_dir: str) -> None:
    """SSHの標準入力でAWSに渡す(コマンドラインにトークンを出さない)。"""
    cmd = ['ssh'] + (['-i', os.path.expanduser(key)] if key else []) + [host, f'cd {remote_dir} && .venv/bin/python -c {_shell_quote(REMOTE_SCRIPT)} {remote_dir}']
    done = subprocess.run(cmd, input=json.dumps(updates), text=True, capture_output=True)
    if done.returncode != 0:
        raise RuntimeError(f'AWSへの書き込みに失敗: {done.stderr.strip()[:300]}')
    print('AWS:', done.stdout.strip())


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def wait_for_code(client_id: str, challenge: str, timeout: int = 300) -> tuple[str, str]:
    """ローカルの一時サーバでGoogleからのリダイレクトを受け、(code, redirect_uri) を返す。"""
    state = secrets.token_urlsafe(16)
    box: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get('state', [''])[0] == state and 'code' in query:
                box['code'] = query['code'][0]
                body = '認可できました。このタブは閉じてかまいません。'
            else:
                box['error'] = query.get('error', ['invalid'])[0]
                body = '認可できませんでした。ターミナルを確認してください。'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(body.encode('utf-8'))

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    redirect_uri = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=lambda: [server.handle_request() for _ in range(5) if 'code' not in box and 'error' not in box], daemon=True)
    thread.start()
    url = build_auth_url(client_id, redirect_uri, challenge, state)
    print('ブラウザでGoogleの許可画面を開きます。開かない場合は、次のURLを手動で開いてください:\n' + url)
    webbrowser.open(url)
    thread.join(timeout)
    server.server_close()
    if 'code' not in box:
        raise RuntimeError(f"認可を完了できませんでした({box.get('error', 'タイムアウト')})")
    return box['code'], redirect_uri


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--client-secret', required=True)
    parser.add_argument('--host', default='ubuntu@52.199.161.97')
    parser.add_argument('--key', default='~/.ssh/LightsailDefaultKey-ap-northeast-1.pem')
    parser.add_argument('--remote-dir', default='/home/ubuntu/work/amazon-fba')
    parser.add_argument('--no-upload', action='store_true')
    args = parser.parse_args(argv)
    client = load_client_secret(args.client_secret)
    verifier, challenge = pkce_pair()
    code, redirect_uri = wait_for_code(client['client_id'], challenge)
    tokens = exchange_code(client, code, redirect_uri, verifier)
    request = urllib.request.Request(PROFILE_URL, headers={'Authorization': f"Bearer {tokens['access_token']}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        profile = json.loads(response.read().decode())
    print(f"認可したGmail: {profile.get('emailAddress')} (読み取り専用 / メール{profile.get('messagesTotal')}通)")
    updates = {'GMAIL_CLIENT_ID': client['client_id'], 'GMAIL_CLIENT_SECRET': client['client_secret'],
               'GMAIL_REFRESH_TOKEN': tokens['refresh_token']}
    if args.no_upload:
        print('--no-upload のため、AWSには書き込みません(トークンは保存していません)。')
        return 0
    upload_env(updates, args.host, args.key, args.remote_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
