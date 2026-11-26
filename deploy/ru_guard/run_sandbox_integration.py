"""Run one offline, resource-limited smoke test; no mounts or production APIs."""
import base64
import json
import subprocess
import sys
import preflight_controller as wire

IMAGE = 'ghcr.io/hiddify/hiddify-manager:v14.2.0@sha256:399c248e20d139a7dab9cf05566751bcbbce30b3a03ae832525e95dfb78333c9'


def program():
    modules = wire.MODULES + [('test_template_install', wire.ROOT), ('test_outbound_install', wire.ROOT),
                              ('sandbox_integration', wire.ROOT)]
    sources = {name: base64.b64encode((directory / (name + '.py')).read_bytes()).decode('ascii')
               for name, directory in modules}
    encoded = base64.b64encode(json.dumps(sources, separators=(',', ':')).encode()).decode('ascii')
    return ("import base64,json,sys,types\n"
            "sources=json.loads(base64.b64decode('" + encoded + "'))\n"
            "for name,encoded in sources.items():\n"
            " m=types.ModuleType(name);m.__file__='/opt/in-memory/'+name+'.py';sys.modules[name]=m\n"
            " exec(compile(base64.b64decode(encoded),m.__file__,'exec'),m.__dict__)\n"
            "try:sys.modules['sandbox_integration'].main()\n"
            "except Exception as exc:\n"
            " import traceback\n"
            " frame=traceback.extract_tb(exc.__traceback__)[-1]\n"
            " print('SANDBOX_FAILED_'+type(exc).__name__+'_LINE'+str(frame.lineno)+'_'+frame.name)\n"
            " sys.exit(1)\n").encode()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in wire.HOSTS:
        raise ValueError('choose a known host')
    # Image and command are fixed constants; no credentials or profiles are
    # provided to the offline container. --pull=never prevents network pulls.
    command = ('docker run --rm -i --pull=never --network none --read-only '
               '--tmpfs /tmp:rw,nosuid,nodev --memory 384m --cpus 0.5 --pids-limit 96 '
               '-e PYTHONDONTWRITEBYTECODE=1 --entrypoint python3 ' + IMAGE + ' -')
    result = subprocess.run(['ssh', '-T', '-i', str(wire.KEY), '-o', 'BatchMode=yes',
                             '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                             '-o', 'ForwardAgent=no', '-o', 'IdentityAgent=none',
                             '-o', 'ClearAllForwardings=yes', '-o', 'ConnectTimeout=15',
                             'root@' + wire.HOSTS[sys.argv[1]], command], input=program(),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    text = result.stdout.decode('ascii', 'replace').strip()
    if result.returncode or text != 'ISOLATED_STAGE_ROLLBACK_AND_CORES_OK':
        print(text if text.startswith('SANDBOX_FAILED_') and '\n' not in text and len(text) < 100
              else 'SANDBOX_NOT_CONFIRMED')
        return 1
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
