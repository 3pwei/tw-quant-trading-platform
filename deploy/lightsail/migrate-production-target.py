#!/usr/bin/env python3
from argparse import ArgumentParser
from pathlib import Path

from tw_quant.execution_service.target_migration import ProductionTargetMigration


parser = ArgumentParser(description="Adopt one explicitly supplied broker account as an owned target")
parser.add_argument("--database", required=True)
parser.add_argument("--secret-root", required=True)
parser.add_argument("--owner", required=True)
parser.add_argument("--broker", required=True)
parser.add_argument("--account", required=True)
parser.add_argument("--credentials", required=True)
parser.add_argument("--ca", required=True)
parser.add_argument("--apply", action="store_true")
args = parser.parse_args()
if not args.apply:
    parser.error("--apply is required; dry runs never mutate production")

target = ProductionTargetMigration(Path(args.database), Path(args.secret_root)).apply(
    owner_user_id=args.owner,
    broker_name=args.broker,
    account_id=args.account,
    credentials_file=Path(args.credentials),
    ca_file=Path(args.ca),
)
print(f"migration complete target_id={target.target_id} status={target.status.value}")
