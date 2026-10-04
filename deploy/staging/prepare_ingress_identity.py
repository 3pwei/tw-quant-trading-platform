#!/usr/bin/env python3
"""Provision one locked host identity; never adopt conflicting UID/GID ownership."""
import argparse
import grp
import os
import pwd
import shutil
import subprocess

NAME = "p8-staging-ingress"
UID = GID = 10000
SHELL = "/usr/sbin/nologin"


def lookup(function, key):
    try:
        return function(key)
    except KeyError:
        return None


def validate_identity():
    users = [lookup(pwd.getpwnam, NAME), lookup(pwd.getpwuid, UID)]
    groups = [lookup(grp.getgrnam, NAME), lookup(grp.getgrgid, GID)]
    for user in users:
        if user and (user.pw_name, user.pw_uid, user.pw_gid, user.pw_dir, user.pw_shell) != (
                NAME, UID, GID, "/nonexistent", SHELL):
            raise RuntimeError("P8_INGRESS_IDENTITY_FAIL conflicting host user; no ownership changed")
    for group in groups:
        if group and (group.gr_name != NAME or group.gr_gid != GID or group.gr_mem):
            raise RuntimeError("P8_INGRESS_IDENTITY_FAIL conflicting host group; no ownership changed")
    # Both name and ID must resolve consistently before accepting an existing record.
    if bool(users[0]) != bool(users[1]) or bool(groups[0]) != bool(groups[1]):
        raise RuntimeError("P8_INGRESS_IDENTITY_FAIL inconsistent host identity lookup")
    return bool(users[0]), bool(groups[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='validate without creating a user or group')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("host identity provisioning requires root")
    for tool in ("groupadd", "useradd"):
        if not shutil.which(tool):
            raise RuntimeError("missing host identity tool: " + tool)
    if not os.path.isfile(SHELL):
        raise RuntimeError("host nologin shell is missing")
    user_exists, group_exists = validate_identity()
    if args.check:
        print("P8_INGRESS_IDENTITY_CHECK=PASS")
        return
    if not group_exists:
        subprocess.run(["groupadd", "--system", "--gid", str(GID), NAME], check=True)
    if not user_exists:
        subprocess.run(["useradd", "--system", "--uid", str(UID), "--gid", str(GID),
                        "--no-create-home", "--home-dir", "/nonexistent", "--shell", SHELL,
                        "--no-log-init", NAME], check=True)
    assert validate_identity() == (True, True)
    print("P8_INGRESS_IDENTITY=PASS")


if __name__ == "__main__":
    main()
