"""Check every enabled listener without a user session."""
import json
import ssl
import urllib.request

from . import runtime


def main():
    directory = runtime.config_dir()
    settings = runtime.load(directory)
    host = '127.0.0.1' if settings.host == '0.0.0.0' else '::1' if settings.host == '::' else settings.host
    host = f'[{host}]' if ':' in host else host
    allowed = settings.allowed_hosts[0]
    allowed = 'localhost' if allowed == '*' else allowed.replace('*.', 'health.')
    for scheme, port in settings.listeners():
        handlers = [urllib.request.ProxyHandler({})]
        if scheme == 'https':
            context = ssl.create_default_context(cafile=str(runtime.tls_path(directory, settings.tls, settings.tls.cert_file)))
            context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
            context.check_hostname = False
            handlers.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(f'{scheme}://{host}:{port}/api/auth/me', headers={'Host': f'{allowed}:{port}'})
        with opener.open(request, timeout=2) as response:
            if response.status != 200 or json.load(response).get('auth_enabled') is not True:
                raise SystemExit(1)


if __name__ == '__main__':
    main()
