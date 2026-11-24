"""Explicit operator provisioning for pinned relay identities."""

import json
import os


def add_commands(parser):
    commands = parser.add_subparsers(dest="ferry_command", required=True)
    keygen = commands.add_parser("relay-keygen", help="Create a new private relay identity")
    keygen.add_argument("--fleet", required=True)
    keygen.add_argument("--key-file")
    show = commands.add_parser("relay-fingerprint", help="Show full relay public identity")
    show.add_argument("--key-file")


def run(args):
    from pinky_daemon.ferry.relay_identity import generate_identity, load_identity

    path = args.key_file or os.environ.get("PINKYBOT_FERRY_RELAY_KEY_FILE")
    if not path:
        raise SystemExit("--key-file or PINKYBOT_FERRY_RELAY_KEY_FILE is required")
    try:
        identity = generate_identity(path, args.fleet) if args.ferry_command == "relay-keygen" else load_identity(path)
        print(json.dumps(identity.public(), separators=(",", ":")))
    except (ValueError, OverflowError):
        raise SystemExit("relay identity operation refused") from None
