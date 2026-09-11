"""Explicit Desktop sign-in command. Importing this module performs no sign-in."""
import argparse
import contextlib
import json
import logging
import os
import re
import stat
import sys
import uuid
import webbrowser
from urllib.parse import urlsplit

AUTH_URI = 'https://accounts.google.com/o/oauth2/auth'
TOKEN_URI = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/adwords'


class InvalidInput(ValueError):
    pass


class SignInFailed(RuntimeError):
    pass


class SavedUnconfirmed(RuntimeError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InvalidInput()


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _parent(path):
    """Open every folder without following links; caller owns the descriptor."""
    if not os.path.isabs(path) or '..' in path.split('/') or path.endswith('/'):
        raise InvalidInput()
    parts = path.split('/')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            if not part or part == '.':
                raise InvalidInput()
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if not parts[-1] or parts[-1] == '.':
            raise InvalidInput()
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _file_info(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise InvalidInput()
    return _identity(info)


def _same_parent(path, fd):
    check_fd, _ = _parent(path)
    try:
        if _identity(os.fstat(check_fd))[:2] != _identity(os.fstat(fd))[:2]:
            raise InvalidInput()
    finally:
        os.close(check_fd)


def _config(fd, name):
    before = _file_info(fd, name)
    if before is None or before[3] > 65536:
        raise InvalidInput()
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(opened, 'rb') as source:
        if _identity(os.fstat(source.fileno())) != before:
            raise InvalidInput()
        raw = source.read(65537)
        if len(raw) > 65536 or _identity(os.fstat(source.fileno())) != before:
            raise InvalidInput()
    try:
        value = json.loads(raw)
    except RecursionError:
        raise InvalidInput() from None
    if not isinstance(value, dict) or 'web' in value or not isinstance(value.get('installed'), dict):
        raise InvalidInput()
    installed = value['installed']
    for key in ('client_id', 'client_secret'):
        if not isinstance(installed.get(key), str) or not installed[key].strip():
            raise InvalidInput()
    for key, expected in (('auth_uri', AUTH_URI), ('token_uri', TOKEN_URI)):
        if key in installed and installed[key] != expected:
            raise InvalidInput()
    redirects = installed.get('redirect_uris', ['http://localhost'])
    if (not isinstance(redirects, list) or not redirects
            or any(item not in ('http://localhost', 'http://localhost/',
                                'http://127.0.0.1', 'http://127.0.0.1/') for item in redirects)):
        raise InvalidInput()
    return {'installed': {
        'client_id': installed['client_id'], 'client_secret': installed['client_secret'],
        'auth_uri': AUTH_URI, 'token_uri': TOKEN_URI,
        'redirect_uris': ['http://127.0.0.1'],
    }}, before


@contextlib.contextmanager
def _quiet():
    """CLI-only: silence logs, Python streams and inherited child descriptors."""
    previous = logging.root.manager.disable
    saved = []
    with open(os.devnull, 'w') as sink:
        try:
            for stream in (sys.stdout, sys.stderr):
                stream.flush()
            for fd in (1, 2):
                saved.append((fd, os.dup(fd)))
                os.dup2(sink.fileno(), fd)
            logging.disable(sys.maxsize)
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
        finally:
            logging.disable(previous)
            for fd, duplicate in saved:
                os.dup2(duplicate, fd)
                os.close(duplicate)


@contextlib.contextmanager
def _browser():
    """Discard ambient/cached BROWSER choices only for this CLI operation."""
    old_env = os.environ.pop('BROWSER', None)
    previous = webbrowser._browsers, webbrowser._tryorder, webbrowser._os_preferred_browser
    try:
        webbrowser._browsers, webbrowser._tryorder, webbrowser._os_preferred_browser = {}, None, None
        controller = webbrowser.get()

        class PinnedBrowser:
            def open(self, url, new=0, autoraise=True):
                parsed = urlsplit(url)
                if (parsed.scheme != 'https' or parsed.netloc != 'accounts.google.com'
                        or parsed.path != '/o/oauth2/auth' or parsed.fragment):
                    raise SignInFailed()
                if not controller.open(url, new=new, autoraise=autoraise):
                    raise SignInFailed()
                return True

        name = 'google-ads-safe-sign-in'
        webbrowser.register(name, None, PinnedBrowser())
        yield name
    finally:
        webbrowser._browsers, webbrowser._tryorder, webbrowser._os_preferred_browser = previous
        if old_env is not None:
            os.environ['BROWSER'] = old_env


def _secure_session(session):
    session.trust_env = False
    session.proxies = {}
    session.verify = True
    session.auth = None
    original_send = session.send

    def send(request, **kwargs):
        if (request.url != TOKEN_URI or request.method != 'POST'
                or kwargs.get('verify') is not True or kwargs.get('proxies')
                or kwargs.get('cert') is not None):
            raise SignInFailed()
        kwargs.update(verify=True, proxies={}, allow_redirects=False, timeout=30)
        response = original_send(request, **kwargs)
        if 300 <= response.status_code < 400:
            raise SignInFailed()
        return response

    session.send = send


def _sign_in(config):
    # Exception translation is confined to the opaque third-party sign-in boundary.
    # No exception text or sensitive output is retained or echoed.
    with _quiet():
        timeout_types = (TimeoutError,)
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError

            timeout_types = (TimeoutError, WSGITimeoutError)
            flow = InstalledAppFlow.from_client_config(
                config, scopes=[SCOPE], autogenerate_code_verifier=True)
            _secure_session(flow.oauth2session)
            with _browser() as browser:
                credentials = flow.run_local_server(
                    host='127.0.0.1', bind_addr='127.0.0.1', port=0,
                    timeout_seconds=180, open_browser=True, browser=browser,
                    authorization_prompt_message=None, prompt='consent')
            token = credentials.refresh_token
            if not isinstance(token, str) or not token.strip():
                raise SignInFailed()
            return token
        except (KeyboardInterrupt, TimeoutError):
            raise
        except Exception as error:
            if isinstance(error, timeout_types):
                raise TimeoutError() from None
            raise SignInFailed() from None


def _save(fd, name, path, previous, profile, recheck):
    temp = '.google-ads-auth-' + uuid.uuid4().hex
    committed = False
    publishing = False
    owned = False
    temp_identity = None

    def remove_temp():
        info = _file_info(fd, temp)
        if info is not None:
            if info[:2] != temp_identity:
                raise InvalidInput()
            os.unlink(temp, dir_fd=fd)

    try:
        opened = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        owned = True
        with os.fdopen(opened, 'w', encoding='utf-8') as output:
            temp_identity = _identity(os.fstat(output.fileno()))[:2]
            os.fchmod(output.fileno(), 0o600)
            # JSON is a YAML subset, with unambiguous quoting for every secret.
            json.dump(profile, output)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        recheck()
        _same_parent(path, fd)
        if _file_info(fd, name) != previous:
            raise InvalidInput()
        publishing = True
        if previous is None:
            os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        else:
            # ponytail: stdlib has no atomic compare-and-swap against a malicious
            # same-user rename after this final check; a stronger protocol needs review.
            os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            owned = False
        committed = True
        publishing = False
        if owned:
            remove_temp()
            owned = False
        os.fsync(fd)
    except (OSError, ValueError, KeyboardInterrupt) as error:
        if committed or (publishing and not isinstance(error, FileExistsError)):
            raise SavedUnconfirmed() from None
        raise
    finally:
        if owned:
            try:
                remove_temp()
            except (OSError, ValueError, KeyboardInterrupt):
                if committed or publishing:
                    raise SavedUnconfirmed() from None
                raise


def _run(args):
    if not re.fullmatch('[0-9]{10}', args.login_customer_id):
        raise InvalidInput()
    saved_or_uncertain = False

    def close_parent(fd):
        try:
            os.close(fd)
        except (OSError, KeyboardInterrupt):
            if saved_or_uncertain:
                raise SavedUnconfirmed() from None
            raise

    with contextlib.ExitStack() as stack:
        source_fd, source_name = _parent(args.client_secrets)
        stack.callback(close_parent, source_fd)
        target_fd, target_name = _parent(args.profile)
        stack.callback(close_parent, target_fd)
        config, source_identity = _config(source_fd, source_name)
        previous = _file_info(target_fd, target_name)
        if ((previous is not None and (not args.replace or previous[:2] == source_identity[:2]))
                or (previous is None and args.replace)
                or (os.fstat(source_fd).st_ino == os.fstat(target_fd).st_ino
                    and os.fstat(source_fd).st_dev == os.fstat(target_fd).st_dev
                    and source_name == target_name)):
            raise InvalidInput()

        def recheck():
            _same_parent(args.client_secrets, source_fd)
            if _file_info(source_fd, source_name) != source_identity:
                raise InvalidInput()

        token = _sign_in(config)
        profile = {key: config['installed'][key] for key in ('client_id', 'client_secret')}
        profile.update(refresh_token=token, login_customer_id=args.login_customer_id, use_proto_plus=True)
        saved_or_uncertain = True
        try:
            _save(target_fd, target_name, args.profile, previous, profile, recheck)
        except KeyboardInterrupt:
            # An interrupt can arrive after _save returns, before its caller resumes.
            raise SavedUnconfirmed() from None
        except (OSError, ValueError):
            # _save translates possibly committed failures to SavedUnconfirmed.
            saved_or_uncertain = False
            raise


def main(argv=None):
    parser = _Parser(description='Create a private Desktop Google Ads sign-in profile.', allow_abbrev=False)
    parser.add_argument('--client-secrets', required=True, help='Absolute Desktop client JSON path')
    parser.add_argument('--profile', required=True, help='Absolute private output profile path')
    parser.add_argument('--login-customer-id', required=True, help='Login manager ID: ten ASCII digits')
    parser.add_argument('--replace', action='store_true', help='Replace an existing regular profile explicitly')
    try:
        _run(parser.parse_args(argv))
    except SavedUnconfirmed:
        print('Profile may be saved, but disk confirmation or cleanup failed. Inspect it before any retry.')
        return 4
    except KeyboardInterrupt:
        print('Sign-in interrupted. Check the chosen profile before retrying.')
        return 3
    except TimeoutError:
        print('Sign-in cancelled or timed out. Existing profile unchanged.')
        return 3
    except SignInFailed:
        print('Sign-in failed or no refresh token returned. Existing profile unchanged.')
        return 3
    except (OSError, ValueError):
        print('Invalid input or file operation failed. Existing profile unchanged. Check paths and permissions.')
        return 2
    print('Private profile saved. Restart the server to load it.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
