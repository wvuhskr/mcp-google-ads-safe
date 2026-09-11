"""Synthetic files and fake transports only; controller supplies OS denial too."""
import hashlib
import json
import logging
import os
import socket
import stat
import sys
import webbrowser
from base64 import urlsafe_b64encode
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from google_auth_oauthlib import flow as native
from requests.adapters import HTTPAdapter

from mcp_google_ads_safe import auth_helper as helper
from tests.conftest import OFFLINE_GADS_FACTORY

SENTINEL = 'SYNTHETIC-SECRET-NEVER-PRINT'


@pytest.fixture(autouse=True)
def denied_boundaries(monkeypatch):
    attempts = []

    def deny(*args, **kwargs):
        attempts.append('forbidden')
        raise AssertionError('Real browser/socket/HTTP forbidden')

    monkeypatch.setattr(socket.socket, 'bind', deny)
    monkeypatch.setattr(socket.socket, 'listen', deny)
    monkeypatch.setattr(HTTPAdapter, 'send', deny)
    monkeypatch.setattr(native.InstalledAppFlow, 'run_local_server', deny)
    monkeypatch.setattr(webbrowser, 'get', deny)
    yield
    assert attempts == []


@pytest.fixture
def files(tmp_path):
    # resolve() is needed on macOS, where pytest's temp root can contain /var's link.
    folder = tmp_path.resolve()
    source = folder / 'desktop.json'
    target = folder / 'profile.yaml'
    source.write_text(json.dumps({'installed': {
        'client_id': 'dummy-client', 'client_secret': SENTINEL,
        'auth_uri': helper.AUTH_URI, 'token_uri': helper.TOKEN_URI,
        'redirect_uris': ['http://localhost'],
    }}))
    argv = ['--client-secrets', str(source), '--profile', str(target),
            '--login-customer-id', '1112223333']
    return SimpleNamespace(folder=folder, source=source, target=target, argv=argv)


@pytest.fixture
def fake_flow(monkeypatch):
    record = SimpleNamespace(token='dummy-refresh', action=lambda: None, runs=[], configs=[], launches=[])

    class FakeFlow:
        oauth2session = requests.Session()

        @classmethod
        def from_client_config(cls, config, **kwargs):
            record.configs.append((config, kwargs))
            return cls()

        def run_local_server(self, **kwargs):
            record.runs.append(kwargs)
            assert self.oauth2session.trust_env is False
            assert self.oauth2session.verify is True
            assert self.oauth2session.proxies == {}
            record.action()
            return SimpleNamespace(refresh_token=record.token)

    monkeypatch.setattr(native, 'InstalledAppFlow', FakeFlow)
    monkeypatch.setattr(webbrowser, 'get', lambda *args: SimpleNamespace(
        open=lambda url, **kwargs: record.launches.append(url) or True))
    return record


def test_first_create_private_supported_profile(files, fake_flow, capsys, monkeypatch):
    assert helper.main(files.argv) == 0
    profile = json.loads(files.target.read_text())
    assert profile == dict(client_id='dummy-client', client_secret=SENTINEL,
                           refresh_token='dummy-refresh', login_customer_id='1112223333',
                           use_proto_plus=True)
    assert stat.S_IMODE(files.target.stat().st_mode) == 0o600
    assert list(files.folder.glob('.google-ads-auth-*')) == []
    assert SENTINEL not in capsys.readouterr().out
    from mcp_google_ads_safe import client
    captured = []
    monkeypatch.setattr(client.GoogleAdsClient, 'load_from_dict',
                        staticmethod(lambda cfg, **kw: captured.append((cfg, kw)) or 'fake-sdk'))
    assert OFFLINE_GADS_FACTORY(str(files.target)) == 'fake-sdk'
    assert captured == [(profile, {'version': 'v25'})]
    config, options = fake_flow.configs[0]
    assert config == {'installed': dict(client_id='dummy-client', client_secret=SENTINEL,
                                       auth_uri=helper.AUTH_URI, token_uri=helper.TOKEN_URI,
                                       redirect_uris=['http://127.0.0.1'])}
    assert options == dict(scopes=[helper.SCOPE], autogenerate_code_verifier=True)
    assert fake_flow.runs == [dict(host='127.0.0.1', bind_addr='127.0.0.1', port=0,
                                  timeout_seconds=180, open_browser=True,
                                  browser='google-ads-safe-sign-in',
                                  authorization_prompt_message=None, prompt='consent')]


def test_replace_requires_existing_regular_file_and_explicit_flag(files, fake_flow):
    assert helper.main(files.argv + ['--replace']) == 2
    files.target.write_text('original')
    assert helper.main(files.argv) == 2
    assert files.target.read_text() == 'original'
    fake_flow.action = lambda: pytest.fail('must not run before validation')
    assert helper.main(files.argv) == 2
    fake_flow.action = lambda: None
    assert helper.main(files.argv + ['--replace']) == 0
    assert json.loads(files.target.read_text())['refresh_token'] == 'dummy-refresh'


@pytest.mark.parametrize('problem', ['no-token', 'timeout', 'interrupt', 'failure'])
@pytest.mark.parametrize('replace', [False, True])
def test_failed_flow_preserves_files(files, fake_flow, problem, replace, capsys):
    if replace:
        files.target.write_text('original')
    if problem == 'no-token':
        fake_flow.token = ''
    else:
        error = {'timeout': native.WSGITimeoutError, 'interrupt': KeyboardInterrupt,
                 'failure': RuntimeError}[problem]
        def action():
            raise error(SENTINEL)
        fake_flow.action = action
    assert helper.main(files.argv + (['--replace'] if replace else [])) == 3
    assert files.target.read_text() == 'original' if replace else not files.target.exists()
    assert not list(files.folder.glob('.google-ads-auth-*'))
    assert SENTINEL not in capsys.readouterr().out


@pytest.mark.parametrize('key,value', [
    ('auth_uri', 'http://accounts.google.com/o/oauth2/auth'),
    ('auth_uri', 'https://evil.invalid/o/oauth2/auth'),
    ('auth_uri', 'https://user@accounts.google.com/o/oauth2/auth'),
    ('token_uri', 'https://oauth2.googleapis.com:443/token'),
    ('token_uri', 'https://oauth2.googleapis.com/other'),
    ('redirect_uris', ['https://evil.invalid/callback']),
    ('client_id', ''), ('client_secret', None),
])
def test_hostile_config_rejected_before_flow(files, fake_flow, key, value):
    config = json.loads(files.source.read_text())
    config['installed'][key] = value
    files.source.write_text(json.dumps(config))
    assert helper.main(files.argv) == 2
    assert fake_flow.runs == []


@pytest.mark.parametrize('kind', ['source-link', 'target-link', 'source-directory', 'target-directory',
                                  'source-fifo', 'oversize', 'web', 'same-path', 'hardlink', 'parent-link'])
def test_unsafe_files_rejected_before_flow(files, fake_flow, kind):
    if kind == 'source-link':
        real = files.source.rename(files.folder / 'real.json')
        files.source.symlink_to(real)
    elif kind == 'target-link':
        files.target.symlink_to(files.source)
    elif kind in ('source-directory', 'source-fifo'):
        files.source.unlink()
        if kind == 'source-directory':
            files.source.mkdir()
        else:
            os.mkfifo(files.source)
    elif kind == 'target-directory':
        files.target.mkdir()
    elif kind == 'oversize':
        files.source.write_bytes(b' ' * 65537)
    elif kind == 'web':
        files.source.write_text('{"web": {}}')
    elif kind == 'same-path':
        files.argv[3] = str(files.source)
    elif kind == 'hardlink':
        os.link(files.source, files.target)
    else:
        link = files.folder / 'linked'
        link.symlink_to(files.folder, target_is_directory=True)
        files.argv[3] = str(link / 'profile.yaml')
    assert helper.main(files.argv + ['--replace']) == 2
    assert fake_flow.runs == []


@pytest.mark.parametrize('kind', ['create-race', 'replace-race', 'parent-race', 'source-race'])
def test_detected_races_preserve_other_files(files, fake_flow, kind):
    replacing = kind != 'create-race'
    if replacing:
        files.target.write_text('original')
    moved = files.folder.with_name(files.folder.name + '-moved')

    def race():
        if kind == 'create-race':
            files.target.write_text('competitor')
        elif kind == 'replace-race':
            files.target.rename(files.folder / 'original.yaml')
            files.target.write_text('competitor')
        elif kind == 'source-race':
            files.source.write_text('{}')
        else:
            files.folder.rename(moved)
            files.folder.mkdir()
            files.target.write_text('competitor')

    fake_flow.action = race
    assert helper.main(files.argv + (['--replace'] if replacing else [])) == 2
    assert files.target.read_text() == ('original' if kind == 'source-race' else 'competitor')
    if kind == 'replace-race':
        assert (files.folder / 'original.yaml').read_text() == 'original'
    if kind == 'parent-race':
        assert (moved / 'profile.yaml').read_text() == 'original'
        assert not list(moved.glob('.google-ads-auth-*'))
    assert not list(files.folder.glob('.google-ads-auth-*'))


def test_atomic_first_create_never_clobbers_late_racer(files, fake_flow, monkeypatch):
    real_link = os.link
    def race(*args, **kwargs):
        files.target.write_text('late competitor')
        return real_link(*args, **kwargs)
    monkeypatch.setattr(os, 'link', race)
    assert helper.main(files.argv) == 2
    assert files.target.read_text() == 'late competitor'
    assert not list(files.folder.glob('.google-ads-auth-*'))


@pytest.mark.parametrize('replace', [False, True])
@pytest.mark.parametrize('after_commit', [False, True])
def test_sync_failures_distinguish_commit(files, fake_flow, monkeypatch, capsys, replace, after_commit):
    if replace:
        files.target.write_text('original')
    real_sync = os.fsync
    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) == after_commit:
            raise OSError(SENTINEL)
        return real_sync(fd)
    monkeypatch.setattr(os, 'fsync', fail)
    assert helper.main(files.argv + (['--replace'] if replace else [])) == (4 if after_commit else 2)
    message = capsys.readouterr().out
    assert SENTINEL not in message
    if after_commit:
        assert 'saved' in message and 'unchanged' not in message
        assert json.loads(files.target.read_text())['refresh_token'] == 'dummy-refresh'
    elif replace:
        assert files.target.read_text() == 'original'
    else:
        assert not files.target.exists()
    assert not list(files.folder.glob('.google-ads-auth-*'))


def test_no_sensitive_output_and_process_state_restored(files, fake_flow, monkeypatch, capfd):
    old_disable = logging.root.manager.disable
    monkeypatch.setenv('BROWSER', 'hostile ' + SENTINEL)
    cached = (webbrowser._browsers, webbrowser._tryorder, webbrowser._os_preferred_browser)
    def noisy():
        assert 'BROWSER' not in os.environ
        assert webbrowser._browsers is not cached[0]
        print(SENTINEL)
        print(SENTINEL, file=sys.stderr)
        logging.critical(SENTINEL)
        os.write(1, SENTINEL.encode())
        os.write(2, SENTINEL.encode())
        raise RuntimeError(SENTINEL)
    fake_flow.action = noisy
    assert helper.main(files.argv) == 3
    assert SENTINEL not in ''.join(capfd.readouterr())
    assert logging.root.manager.disable == old_disable
    assert os.environ['BROWSER'] == 'hostile ' + SENTINEL
    assert webbrowser._browsers is cached[0]
    assert webbrowser._tryorder is cached[1]


@pytest.mark.parametrize('args', [['--unknown', SENTINEL], ['--client-secrets', SENTINEL],
                                  ['--help']])
def test_parser_does_not_echo_bad_arguments(args, capsys):
    if args == ['--help']:
        with pytest.raises(SystemExit) as result:
            helper.main(args)
        assert result.value.code == 0
    else:
        assert helper.main(args) == 2
    assert SENTINEL not in ''.join(capsys.readouterr())


@pytest.mark.parametrize('bad_id', ['１２３４５６７８９０', '111-222-3333', '123', '1112223333\n'])
def test_strict_manager_id(files, fake_flow, bad_id):
    files.argv[5] = bad_id
    assert helper.main(files.argv) == 2
    assert fake_flow.runs == []


def test_pinned_initial_browser_url(monkeypatch):
    launches = []
    monkeypatch.setattr(webbrowser, 'get', lambda *args: SimpleNamespace(
        open=lambda url, **kwargs: launches.append(url) or True))
    with helper._browser() as name:
        browser = webbrowser._browsers[name][1]
        for url in ['http://accounts.google.com/o/oauth2/auth', 'https://evil.invalid/',
                    'https://user@accounts.google.com/o/oauth2/auth',
                    'https://accounts.google.com:443/o/oauth2/auth']:
            with pytest.raises(helper.SignInFailed):
                browser.open(url)
        assert browser.open(helper.AUTH_URI + '?state=dummy')
    assert launches == [helper.AUTH_URI + '?state=dummy']


def test_installed_library_pkce_state_and_final_token_transport(monkeypatch):
    sent = []
    def transport(adapter, request, **kwargs):
        sent.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response._content = json.dumps(dict(access_token='dummy-access', refresh_token='dummy-refresh',
                                             token_type='Bearer', expires_in=3600)).encode()
        return response
    monkeypatch.setattr(HTTPAdapter, 'send', transport)
    monkeypatch.setenv('HTTPS_PROXY', 'http://evil.invalid')
    monkeypatch.setenv('REQUESTS_CA_BUNDLE', '/does-not-exist')
    monkeypatch.setenv('NETRC', '/does-not-exist')
    config = {'installed': dict(client_id='dummy-client', client_secret=SENTINEL,
                               auth_uri=helper.AUTH_URI, token_uri=helper.TOKEN_URI)}
    flow = native.InstalledAppFlow.from_client_config(config, scopes=[helper.SCOPE],
                                                     autogenerate_code_verifier=True)
    helper._secure_session(flow.oauth2session)
    flow.redirect_uri = 'http://127.0.0.1:45678/'
    with helper._quiet():
        url, state = flow.authorization_url(prompt='consent')
        query = parse_qs(urlsplit(url).query)
        expected = urlsafe_b64encode(hashlib.sha256(flow.code_verifier.encode()).digest()).decode().rstrip('=')
        assert query['code_challenge'] == [expected]
        assert query['code_challenge_method'] == ['S256']
        assert query['scope'] == [helper.SCOPE]
        assert query['access_type'] == ['offline']
        assert query['state'] == [state] and state
        with pytest.raises(Exception):
            flow.fetch_token(authorization_response='https://127.0.0.1:45678/?code=dummy&state=wrong')
        assert sent == []
        flow.fetch_token(authorization_response=f'https://127.0.0.1:45678/?code=dummy&state={state}')
    assert len(sent) == 1
    request, options = sent[0]
    assert request.url == helper.TOKEN_URI and request.method == 'POST'
    assert options['verify'] is True and options['proxies'] == {} and options['timeout'] == 30
    assert parse_qs(request.body)['code_verifier'] == [flow.code_verifier]
    assert flow.credentials.refresh_token == 'dummy-refresh'
    for kwargs in [dict(verify=False), dict(proxies={'https': 'http://evil.invalid'}),
                   dict(cert='/does-not-exist')]:
        with pytest.raises(helper.SignInFailed):
            flow.oauth2session.post(helper.TOKEN_URI, **kwargs)
    with pytest.raises(helper.SignInFailed):
        flow.oauth2session.post('https://evil.invalid/token')
    with pytest.raises(helper.SignInFailed):
        flow.oauth2session.get(helper.TOKEN_URI)
    assert len(sent) == 1


def test_token_redirect_is_rejected_without_following(monkeypatch):
    sent = []
    def transport(adapter, request, **kwargs):
        sent.append(request.url)
        response = requests.Response()
        response.status_code = 302
        response.headers['Location'] = 'https://evil.invalid/token'
        response.request = request
        response._content = b''
        return response
    monkeypatch.setattr(HTTPAdapter, 'send', transport)
    session = requests.Session()
    helper._secure_session(session)
    with pytest.raises(helper.SignInFailed):
        session.post(helper.TOKEN_URI)
    assert sent == [helper.TOKEN_URI]


@pytest.mark.parametrize('replace', [False, True])
def test_interrupt_just_after_publication_never_claims_unchanged(files, fake_flow, monkeypatch, capsys, replace):
    operation = 'replace' if replace else 'link'
    original = getattr(os, operation)
    if replace:
        files.target.write_text('original')
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt(SENTINEL)
    monkeypatch.setattr(os, operation, interrupted)
    assert helper.main(files.argv + (['--replace'] if replace else [])) == 4
    assert json.loads(files.target.read_text())['refresh_token'] == 'dummy-refresh'
    message = capsys.readouterr().out
    assert 'may be saved' in message and 'unchanged' not in message and SENTINEL not in message


@pytest.mark.parametrize('kind', ['malformed', 'deeply-nested', 'relative'])
def test_invalid_input_never_reaches_flow(files, fake_flow, kind, capsys):
    if kind == 'malformed':
        files.source.write_text(SENTINEL)
    elif kind == 'deeply-nested':
        files.source.write_text('[' * 2000 + ']' * 2000)
    else:
        files.argv[1] = 'desktop.json'
    assert helper.main(files.argv) == 2
    assert fake_flow.runs == []
    assert SENTINEL not in ''.join(capsys.readouterr())


@pytest.mark.parametrize('replace', [False, True])
@pytest.mark.parametrize('already_uncertain', [False, True])
@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_parent_close_after_publication_preserves_uncertain_status(
        files, fake_flow, monkeypatch, capsys, replace, already_uncertain, failure):
    if replace:
        files.target.write_text('original')
    real_save, real_close, real_sync = helper._save, os.close, os.fsync
    target_parent = []
    failures = []

    def save(fd, *args, **kwargs):
        target_parent.append(fd)
        return real_save(fd, *args, **kwargs)

    def close(fd):
        if target_parent and fd == target_parent[0] and not failures:
            failures.append(fd)
            real_close(fd)
            raise failure(SENTINEL)
        return real_close(fd)

    def sync(fd):
        if already_uncertain and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(SENTINEL)
        return real_sync(fd)

    monkeypatch.setattr(helper, '_save', save)
    monkeypatch.setattr(os, 'close', close)
    monkeypatch.setattr(os, 'fsync', sync)
    assert helper.main(files.argv + (['--replace'] if replace else [])) == 4
    assert failures == target_parent
    assert json.loads(files.target.read_text())['refresh_token'] == 'dummy-refresh'
    message = capsys.readouterr().out
    assert 'may be saved' in message and 'unchanged' not in message and SENTINEL not in message


@pytest.mark.parametrize('replace', [False, True])
def test_interrupt_after_save_returns_never_claims_unchanged(files, fake_flow, monkeypatch, capsys, replace):
    if replace:
        files.target.write_text('original')
    real_save = helper._save

    def interrupted(*args, **kwargs):
        real_save(*args, **kwargs)
        raise KeyboardInterrupt(SENTINEL)

    monkeypatch.setattr(helper, '_save', interrupted)
    assert helper.main(files.argv + (['--replace'] if replace else [])) == 4
    assert json.loads(files.target.read_text())['refresh_token'] == 'dummy-refresh'
    message = capsys.readouterr().out
    assert 'may be saved' in message and 'unchanged' not in message and SENTINEL not in message


@pytest.mark.parametrize('replace', [False, True])
def test_interrupt_after_run_returns_does_not_promise_unchanged(files, fake_flow, monkeypatch, capsys, replace):
    if replace:
        files.target.write_text('original')
    real_run = helper._run

    def interrupted(args):
        real_run(args)
        raise KeyboardInterrupt(SENTINEL)

    monkeypatch.setattr(helper, '_run', interrupted)
    assert helper.main(files.argv + (['--replace'] if replace else [])) == 3
    assert json.loads(files.target.read_text())['refresh_token'] == 'dummy-refresh'
    message = capsys.readouterr().out
    assert message == 'Sign-in interrupted. Check the chosen profile before retrying.\n'
    assert 'unchanged' not in message and SENTINEL not in message
