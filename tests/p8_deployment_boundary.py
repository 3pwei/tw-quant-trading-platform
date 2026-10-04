"""Explicit process boundary doubles for the P8 orchestration contract test.

No real Docker daemon, systemd unit, provider or broker is accessed here.
Unknown commands fail rather than being silently accepted.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import time
from types import SimpleNamespace

ROOT = Path(os.environ['P8_FIXTURE_ROOT'])
BUNDLE = ROOT / 'bundle'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(BUNDLE))


def now():
    return datetime.now(timezone.utc).isoformat()


def document():
    return json.loads((BUNDLE / 'candidate-manifest.json').read_text())


def state():
    return json.loads((ROOT / 'engine.json').read_text())


def save(value):
    (ROOT / 'engine.json').write_text(json.dumps(value))


def values():
    return dict(line.split('=', 1) for line in (ROOT / 'deployments/active-release.env').read_text().splitlines())


def image(ref):
    doc = document()
    images = [doc['images']['gateway'], *(r['runtime'] for r in doc['images']['releases'].values())]
    return next(i for i in images if i['ref'] == ref)


def health():
    return {'locked': True, 'enabled': False, 'ordering_enabled': False, 'connected': False,
            'execution_state': 'disabled', 'recovery_status': 'locked', 'broker_name': 'disabled',
            'external_order_calls': 0, 'external_cancel_calls': 0,
            'issue_codes': ['execution_disabled', 'execution_target_not_active'], 'heartbeat_at': now()}


def execute(args):
    if args == ['id', '-u']:
        raise ValueError('UID requires selected container')
    if args == ['cat', '/run/tw-quant-execution/health.json']:
        print(json.dumps(health()))
    elif args == ['python', '-m', 'tw_quant.execution_service', 'healthcheck']:
        return
    elif args == ['python', '-', 'snapshot']:
        sys.stdin.buffer.read()
        import durable_probe
        print(json.dumps(durable_probe.snapshot(ROOT / 'volumes/platform-staging_staging-data/staging.sqlite3')))
    else:
        raise ValueError('unsupported exec')


def docker(args):
    if args[0] == 'compose':
        while args[1] in ('--env-file', '-f'):
            args = [args[0], *args[3:]]
        action, *tail = args[1:]
        if action == 'config':
            if '--quiet' not in tail: print('name: platform-staging')
        elif action == 'ps' and tail[0] == '-q':
            print(state()['containers'][tail[-1]]['id'])
        elif action == 'up':
            v = values()
            # Model Compose shell precedence: detect missing compensation reload.
            ref = os.environ.get('STAGING_RUNTIME_IMAGE', v['STAGING_RUNTIME_IMAGE'])
            previous = state() if (ROOT / 'engine.json').exists() else {'generation': 0}
            generation = previous['generation'] + 1
            containers = {}
            for service in ('market-api', 'execution-worker', 'gateway'):
                selected = os.environ.get('STAGING_GATEWAY_IMAGE', v['STAGING_GATEWAY_IMAGE']) if service == 'gateway' else ref
                cid = hashlib.sha256(f'{generation}/{service}'.encode()).hexdigest()
                containers[service] = {'id': cid, 'image': image(selected)['config_digest'], 'started': now(),
                    'restarts': 0, 'running': True, 'health': 'healthy',
                    'network_mode': 'none' if service == 'execution-worker' else 'platform-staging_staging-internal'}
            save({'generation': generation, 'role': v['RELEASE_NAME'], 'containers': containers})
        elif action == 'restart':
            current = state()
            for d in current['containers'].values(): d['started'] = now()
            save(current)
        elif action == 'exec' and tail[:2] == ['-T', 'execution-worker']:
            execute(tail[2:])
        else:
            raise ValueError('unsupported compose command')
    elif args[:2] == ['volume', 'create']:
        (ROOT / 'volumes' / args[2]).mkdir(parents=True, exist_ok=True)
    elif args[:2] == ['volume', 'inspect']:
        print(ROOT / 'volumes' / args[-1])
    elif args[:2] == ['image', 'inspect']:
        target = image(args[-1])
        if '--format' not in args:
            print('[]')
            return
        fmt = args[args.index('--format') + 1]
        if fmt == '{{.Id}}': print(target['config_digest'])
        elif 'RepoDigests' in fmt: print(target['ref'].split(':')[0] + '@' + target['digest'])
        elif fmt.startswith('{{index .Config.Labels '):
            key = fmt.split('"')[1]
            doc = document()
            role = next((r for r in doc['images']['releases'].values() if r['runtime']['ref'] == args[-1]), None)
            labels = {'org.opencontainers.image.revision': doc['platform_source_sha'],
                      'io.tw-quant.pipeline.revision': doc['pipeline_revision'],
                      'io.tw-quant.core.sha256': doc['core']['wheel_sha256'],
                      'io.tw-quant.private-provider.sha256': doc['private_provider']['wheel_sha256'],
                      'io.tw-quant.p7.acceptance': doc['private_provider']['p7_acceptance_sha']}
            if role: labels['io.tw-quant.configuration.identity'] = role['configuration_identity']
            print(labels[key])
        else: raise ValueError('unsupported image inspection')
    elif args[:2] == ['image', 'save']:
        target = image(args[-1])
        payload = (ROOT / (target['config_digest'].split(':')[1] + '.json')).read_bytes()
        config = target['config_digest'].split(':')[1] + '.json'
        manifest = json.dumps([{'Config': config, 'RepoTags': [], 'Layers': []}]).encode()
        with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
            for name, content in ((config, payload), ('manifest.json', manifest)):
                info = tarfile.TarInfo(name); info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    elif args[0] == 'inspect':
        current = state()
        d = next(d for d in current['containers'].values() if d['id'] == args[-1])
        fmt = args[args.index('--format') + 1]
        if fmt.startswith('{"id":'): print(json.dumps(d))
        elif '.State.Health' in fmt:
            failed = (ROOT / 'fail-candidate').exists() and current['role'] == 'candidate'
            print('unhealthy' if failed else 'healthy')
        else:
            formats = {'{{.Image}}': d['image'], '{{.HostConfig.ReadonlyRootfs}}': 'true',
                       '{{json .HostConfig.SecurityOpt}}': '["no-new-privileges:true"]',
                       '{{json .HostConfig.CapDrop}}': '["ALL"]', '{{json .HostConfig.PortBindings}}': '{}',
                       '{{len .NetworkSettings.Networks}}': '1',
                       '{{range $n,$v := .NetworkSettings.Networks}}{{$n}}{{end}}': 'platform-staging_staging-internal',
                       '{{json .NetworkSettings.Ports}}': '{}', '{{.HostConfig.NetworkMode}}': d['network_mode']}
            print(formats[fmt])
    elif args[0] == 'exec':
        tail = args[1:]
        if tail[0] == '-i': tail = tail[1:]
        cid, *command = tail
        if command == ['id', '-u']:
            print('10000' if state()['containers']['gateway']['id'] == cid else '10001')
        else: execute(command)
    elif args[:2] == ['network', 'inspect']:
        print('true')
    elif args[0] == 'run':
        if args[-2:] == ['/durable_probe.py', 'seed']:
            import durable_probe
            durable_probe.PATH = str(ROOT / 'volumes/platform-staging_staging-data/staging.sqlite3')
            os.environ.update(BROKER_PROVIDER='disabled', LIVE_TRADING_ENABLED='false')
            durable_probe.seed()
        elif '/app/p8_runtime_acceptance.py' in args or 'unittest' in args:
            # Deliberately no private provider in a public orchestration fixture.
            with (ROOT / 'private-boundary-calls').open('a') as stream: stream.write('stub\n')
        elif 'tw_quant.synthetic_data' not in args:
            raise ValueError('unsupported container run')
    elif args[0] == 'logs':
        print('P8 public contract fixture log', flush=True)
        while True: time.sleep(1)
    else:
        raise ValueError('unsupported docker command')


def main():
    command, *args = sys.argv[1:]
    if command == 'docker': docker(args)
    elif command == 'chown': return
    elif command == 'stat' and args[-1] == '/var/lib/tw-quant-staging-ingress/gateway.sock':
        print('10000:10000:600:socket')
    elif command == 'systemctl':
        if args[0] == 'is-active': return
        if '--property=Listen' in args: print('127.0.0.1:18080 (Stream)')
        else: raise ValueError('unsupported unit inspection')
    elif command == 'python3':
        if args[0] == str(BUNDLE / 'ingress_probe.py'):
            print('P8_TEST_INGRESS_BOUNDARY=stub')
        elif args[:2] == [str(BUNDLE / 'acceptance_evidence.py'), 'soak']:
            import acceptance_evidence
            clock = [0]
            def monotonic():
                clock[0] += 20
                return clock[0]
            acceptance_evidence.time = SimpleNamespace(monotonic=monotonic,
                sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
            sys.argv = args
            acceptance_evidence.main()
        else:
            os.execv(sys.executable, [sys.executable, *args])
    else:
        raise ValueError('unsupported boundary command')


if __name__ == '__main__':
    main()
