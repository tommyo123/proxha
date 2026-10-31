"""Configure the Debian node_exporter service for ProxHA on one Proxmox host."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

SERVICE = 'prometheus-node-exporter.service'
DROP_IN = Path('/etc/systemd/system') / (SERVICE + '.d') / '50-proxha.conf'
MARKER = '# Managed by ProxHA: physical infrastructure metrics.'
COLLECTORS = ('diskstats', 'netdev', 'netclass', 'uname', 'stat')


def run(args, check=True):
    result = subprocess.run(args, text=True, capture_output=True, check=False, timeout=300)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} {args[1]} failed (exit {result.returncode}). Check the host service/package logs.')
    return result


def endpoint(address, port):
    value = ipaddress.ip_address(address)
    if value.is_unspecified or value.is_multicast:
        raise ValueError('Choose a specific local management IP address, not a wildcard or multicast address.')
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535.')
    return f'[{value}]:{port}' if value.version == 6 else f'{value}:{port}'


def configuration(address, port):
    listen = endpoint(address, port)
    flags = ' '.join(f'--collector.{name}' for name in COLLECTORS)
    return f'''{MARKER}
[Service]
ExecStart=
ExecStart=/usr/bin/prometheus-node-exporter --web.listen-address={listen} --collector.disable-defaults {flags}
'''


def devices(sysfs=Path('/sys')):
    disks, interfaces = [], []
    for path in sorted((sysfs / 'class/block').glob('*')):
        # Mapped volumes and optical drives are not physical-disk measurement sources.
        if path.name.startswith(('rbd', 'nbd', 'loop', 'zram', 'dm-', 'md', 'zd', 'sr')):
            continue
        if (path / 'device').exists() and not (path / 'partition').exists():
            disks.append(path.name)
    for path in sorted((sysfs / 'class/net').glob('*')):
        if (path / 'device').exists():
            try:
                active = (path / 'operstate').read_text().strip() == 'up'
            except OSError:
                active = False
            if active:
                interfaces.append(path.name)
    return {'disks': disks, 'interfaces': interfaces}


def inspect(address, port):
    listen = endpoint(address, port)
    addresses = json.loads(run(['ip', '-j', 'address', 'show']).stdout)
    if ipaddress.ip_address(address) not in {
        ipaddress.ip_address(a['local']) for item in addresses for a in item.get('addr_info', []) if a.get('local')
    }:
        raise ValueError('The listen address is not assigned to this host.')
    installed = run(['dpkg-query', '-W', '-f=${db:Status-Status}', 'prometheus-node-exporter'], check=False)
    managed = DROP_IN.is_file() and DROP_IN.read_text().startswith(MARKER + '\n')
    if DROP_IN.is_symlink() or DROP_IN.exists() and not managed:
        raise ValueError('The ProxHA service drop-in already exists and is not managed by this script.')
    package_installed = installed.returncode == 0 and installed.stdout.strip() == 'installed'
    enabled = run(['systemctl', 'is-enabled', SERVICE], check=False).stdout.strip()
    if enabled.startswith('masked'):
        raise ValueError('The exporter service is already masked. Resolve that state before setup.')
    if package_installed and not managed:
        raise ValueError('An existing node_exporter installation is not owned by ProxHA. Reuse its endpoint instead of replacing its configuration.')
    if not package_installed:
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((address, port))
    return {'host': socket.gethostname().split('.')[0], 'url': 'http://' + listen + '/metrics',
            'package_installed': package_installed, 'managed': managed, **devices()}


def write_configuration(content):
    DROP_IN.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.proxha-', dir=DROP_IN.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, DROP_IN)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(10):
        try:
            with opener.open(url, timeout=3) as response:
                data = response.read(4 * 1024**2 + 1)
            if len(data) > 4 * 1024**2 or b'node_uname_info{' not in data or b'node_boot_time_seconds ' not in data:
                raise ValueError('Exporter response is missing required host identity or boot-time metrics.')
            return
        except (OSError, ValueError):
            if attempt == 9:
                raise RuntimeError('Exporter did not pass the local HTTP check. Inspect systemctl status and journalctl for the service.') from None
            time.sleep(1)


def apply(plan, address, port):
    content = configuration(address, port)
    changed = not DROP_IN.is_file() or DROP_IN.read_text() != content
    masked = False
    try:
        if not plan['package_installed']:
            # Prevent package installation from starting an exporter on every interface.
            run(['systemctl', 'mask', '--runtime', SERVICE])
            masked = True
        if changed:
            write_configuration(content)
        if not plan['package_installed']:
            run(['apt-get', 'install', '--yes', '--no-install-recommends', 'prometheus-node-exporter'])
    finally:
        if masked:
            run(['systemctl', 'unmask', '--runtime', SERVICE])
        run(['systemctl', 'daemon-reload'])
    if changed and plan['package_installed']:
        run(['systemctl', 'restart', SERVICE])
    run(['systemctl', 'enable', '--now', SERVICE])
    verify(plan['url'])
    return {**plan, 'installed': True, 'service': SERVICE, 'drop_in': str(DROP_IN),
            'next': 'Test this URL from the ProxHA server, then save and enable its physical disks/interfaces in DRS Settings.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen-address', required=True, help='A local management IP reachable from ProxHA.')
    parser.add_argument('--port', type=int, default=9100)
    parser.add_argument('--apply', action='store_true', help='Install and start the exporter. Without this flag, inspect only.')
    args = parser.parse_args()
    try:
        if platform.system() != 'Linux':
            raise ValueError('Run this script on a Proxmox/Debian host.')
        if args.apply and os.geteuid() != 0:
            raise ValueError('Installation requires root or sudo.')
        plan = inspect(args.listen_address, args.port)
        print(json.dumps(apply(plan, args.listen_address, args.port) if args.apply else plan, indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
