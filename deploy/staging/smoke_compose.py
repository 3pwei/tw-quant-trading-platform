#!/usr/bin/env python3
"""Exercise the staging Compose topology on a disposable Linux CI Docker host.

Run as root for UID-owned bind/volume initialization. Never runs on the staging
host: refuses if its installation exists. Images are already built; no pulls.
The optional public backend double is for PR CI only, not Candidate acceptance.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from acceptance_evidence import INSPECT, check_health, continuity

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-a", required=True)
    parser.add_argument("--runtime-b", required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--factory", type=Path)
    parser.add_argument("--public-fixture", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0 or Path("/srv/trading-platform-staging").exists():
        parser.error("requires disposable root CI host without a staging installation")
    if bool(args.factory) == bool(args.public_fixture):
        parser.error("provide either exact Candidate factory or public CI fixture")
    for tool in ("docker", "systemctl"):
        if not shutil.which(tool):
            parser.error("missing CI tool: " + tool)
    proxy_binary = "/usr/lib/systemd/systemd-socket-proxyd"
    if not Path(proxy_binary).is_file():
        parser.error("systemd socket proxy is missing")
    # Refuse port collisions before creating any resources. Never terminate its owner.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 18080))
    env = {k: v for k, v in os.environ.items() if not k.startswith("STAGING_")}

    def run(*argv, timeout=60, data=None):
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout, input=data)
        if result.returncode:
            if argv[:2] == ("python3", str(HERE / "ingress_probe.py")):
                # This probe emits only bounded status fields, never response bytes.
                print(result.stdout, end="", flush=True)
                state = subprocess.run(["systemctl", "show", "--property=ActiveState", "--property=SubState",
                    "--property=Result", "--property=ExecMainCode", "--property=ExecMainStatus",
                    "p8-staging-ingress.service"], capture_output=True, text=True, timeout=10)
                print(state.stdout, end="", flush=True)
            # Compose output can include injected configuration; do not print it.
            raise RuntimeError("P8_COMPOSE_FAIL command=" + argv[0] + " exit=" + str(result.returncode))
        return result.stdout.strip()

    def inspect(container, fmt):
        return run("docker", "inspect", "--format", fmt, container)

    ingress = Path("/var/lib/tw-quant-staging-ingress")
    units = ("p8-staging-ingress.socket", "p8-staging-ingress.service")
    if ingress.exists() or ingress.is_symlink():
        parser.error("refusing an existing ingress directory")
    for unit in units:
        state = subprocess.run(["systemctl", "show", "--property=LoadState", "--value", unit],
                               capture_output=True, text=True, timeout=10)
        if state.stdout.strip() != "not-found":
            parser.error("refusing an existing ingress unit")
    with tempfile.TemporaryDirectory(prefix="p8-compose-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        factory = root / "factory"
        factory.write_bytes(args.factory.read_bytes() if args.factory else b"public-fixture-unused")
        factory.chmod(0o400)
        os.chown(factory, 10001, 10001)
        config = HERE.joinpath("prepare-host.sh").read_text()
        for role in ("market", "execution", "gateway"):
            matches = re.findall(r'write_config "\$\{INSTALL_ROOT\}/config/' + role + r"\.env\" <<'EOF'\n(.*?)\nEOF", config, re.S)
            if len(matches) != 1:
                raise RuntimeError("expected one shared environment template for " + role)
            root.joinpath(role + ".env").write_text(matches[0] + "\n")
        project = "p8-candidate-check-" + str(os.getpid())
        compose = ["docker", "compose", "--project-name", project, "--env-file", str(root / "compose.env"),
                   "-f", str(HERE / "docker-compose.yml")]
        if args.public_fixture:
            override = root / "public-fixture.json"
            override.write_text(json.dumps({"services": {"market-api": {
                "command": ["python", "/p8-public-backend.py"],
                "volumes": [str(args.public_fixture.resolve()) + ":/p8-public-backend.py:ro"],
            }}}))
            compose += ["-f", str(override)]
        linked = []
        ingress_created = False
        proxy_started = False
        try:
            run("python3", str(HERE / "prepare_ingress_identity.py"))
            ingress.mkdir(mode=0o700)
            ingress_created = True
            os.chown(ingress, 10000, 10000)
            for unit in units:
                run("systemctl", "link", "--runtime", str(HERE / unit))
                linked.append(unit)
            run("systemctl", "daemon-reload")
            for release, runtime in (("A", args.runtime_a), ("B", args.runtime_b)):
                values = {"STAGING_RUNTIME_IMAGE": runtime, "STAGING_GATEWAY_IMAGE": args.gateway,
                          "STAGING_PROVIDER_FACTORY_FILE": str(factory), "STAGING_INGRESS_DIRECTORY": str(ingress)}
                values.update({"STAGING_" + role.upper() + "_ENV_FILE": str(root / (role + ".env"))
                               for role in ("market", "execution", "gateway")})
                root.joinpath("compose.env").write_text("".join(k + "=" + v + "\n" for k, v in values.items()))
                run(*compose, "config", "--quiet")
                for name, uid in (("staging-data", 10001), ("execution-health", 10001), ("gateway-data", 10000), ("gateway-config", 10000)):
                    volume = project + "_" + name
                    run("docker", "volume", "create", volume)
                    mount = run("docker", "volume", "inspect", "--format", "{{.Mountpoint}}", volume)
                    os.chown(mount, uid, uid)
                    os.chmod(mount, 0o750)
                run("docker", "run", "--rm", "--pull=never", "--network", "none", "--mount",
                    "source=" + project + "_staging-data,target=/data", runtime,
                    "python", "-m", "tw_quant.synthetic_data", "--output", "/data/synthetic.csv")
                run("docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--mount", "source=" + project + "_staging-data,target=/data",
                    "--mount", "type=bind,source=" + str(HERE / "durable_probe.py") + ",target=/durable_probe.py,readonly",
                    "--env", "BROKER_PROVIDER=disabled", "--env", "LIVE_TRADING_ENABLED=false",
                    runtime, "python", "/durable_probe.py", "seed")
                baseline = None
                run(*compose, "up", "--no-build", "--pull", "never", "--detach", "--force-recreate", timeout=180)
                for phase in ("initial", "restart"):
                    if phase == "restart":
                        run(*compose, "restart", "market-api", "execution-worker", "gateway", timeout=120)
                    observed = {"containers": {}, "records": {}, "release": release,
                                "expected_images": {"runtime": runtime, "gateway": args.gateway}}
                    for service in ("market-api", "execution-worker", "gateway"):
                        cid = run(*compose, "ps", "-q", service)
                        deadline = time.monotonic() + 90
                        while inspect(cid, "{{.State.Health.Status}}") != "healthy":
                            if time.monotonic() >= deadline:
                                raise RuntimeError("P8_COMPOSE_FAIL health service=" + service)
                            time.sleep(1)
                        assert inspect(cid, "{{.HostConfig.ReadonlyRootfs}}") == "true"
                        assert "ALL" in json.loads(inspect(cid, "{{json .HostConfig.CapDrop}}"))
                        assert "no-new-privileges:true" in json.loads(inspect(cid, "{{json .HostConfig.SecurityOpt}}"))
                        assert not any((json.loads(inspect(cid, "{{json .NetworkSettings.Ports}}")) or {}).values())
                        assert inspect(cid, "{{.Config.User}}") == ("10000:10000" if service == "gateway" else "10001:10001")
                        observed["containers"][service] = json.loads(inspect(cid, INSPECT))
                        expected_image = args.gateway if service == "gateway" else runtime
                        assert inspect(cid, "{{.Image}}") == run("docker", "image", "inspect", "--format", "{{.Id}}", expected_image)
                        if service == "execution-worker":
                            assert inspect(cid, "{{.HostConfig.NetworkMode}}") == "none"
                            health = json.loads(run("docker", "exec", cid, "cat", "/run/tw-quant-execution/health.json"))
                            observed["execution"] = check_health(health, observed["containers"][service]["started"])
                            observed["durable"] = json.loads(run("docker", "exec", "-i", cid, "python", "-", "snapshot",
                                data=(HERE / "durable_probe.py").read_text()))
                            assert health["locked"] is True and health["external_order_calls"] == 0 and health["external_cancel_calls"] == 0
                        else:
                            networks = json.loads(inspect(cid, "{{json .NetworkSettings.Networks}}"))
                            assert len(networks) == 1
                            for network in networks:
                                assert run("docker", "network", "inspect", "--format", "{{.Internal}}", network) == "true"
                    if baseline is not None:
                        continuity(baseline, observed, restarted=True)
                    baseline = observed
                    sock = ingress / "gateway.sock"
                    state = sock.stat()
                    assert sock.is_socket() and state.st_uid == 10000 and state.st_gid == 10000 and state.st_mode & 0o777 == 0o600
                    if not proxy_started:
                        # Prove a healthy container without host ingress does not pass.
                        absent = subprocess.run(["python3", str(HERE / "ingress_probe.py")], capture_output=True, timeout=10)
                        assert absent.returncode != 0
                        run("systemctl", "start", "p8-staging-ingress.socket")
                        proxy_started = True
                    run("python3", str(HERE / "ingress_probe.py"), timeout=10)
                    assert run("systemctl", "show", "--property=Listen", "--value", units[0]) == "127.0.0.1:18080 (Stream)"
                    run("systemctl", "is-active", "--quiet", *units)
                    print("P8_COMPOSE_GATE=PASS release=" + release + " phase=" + phase, flush=True)
        finally:
            # Only this generated project is removed, including its synthetic volumes.
            preserving_failure = sys.exc_info()[0] is not None
            cleanup_failed = False
            try:
                subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)
            except (subprocess.SubprocessError, OSError):
                cleanup_failed = True
            try:
                if linked:
                    run("systemctl", "stop", *linked)
                for unit in linked:
                    Path("/run/systemd/system", unit).unlink()
                if linked:
                    run("systemctl", "daemon-reload")
                if ingress_created:
                    shutil.rmtree(ingress)
            except (subprocess.SubprocessError, OSError, RuntimeError):
                cleanup_failed = True
            if cleanup_failed:
                if not preserving_failure:
                    raise RuntimeError("P8_COMPOSE_CLEANUP=FAIL")
                print("P8_COMPOSE_CLEANUP=FAIL (original failure preserved)", file=sys.stderr)


if __name__ == "__main__":
    main()
