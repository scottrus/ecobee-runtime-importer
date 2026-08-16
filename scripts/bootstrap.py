#!/usr/bin/env python3
"""Interactive, one-time ecobee login.

ecobee's developer portal no longer issues API keys, so authentication goes
through the Auth0 flow Home Assistant adopted in 2026.3, using ecobee's own web
client. That flow walks hosted login forms and, if the account has TOTP
multi-factor enabled, requires a code from an authenticator app — so it needs a
human and cannot be scheduled.

Run this once. It prints a credentials document, or writes one with `--out PATH`
(preferred — that keeps the token out of your scrollback). Store the refresh
token in a password manager and create the Kubernetes Secret from it.

Your password is used only to complete this login. It is not written anywhere by
this script and never reaches the running importer.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

try:
    from pyecobee import Ecobee
    from pyecobee.const import ECOBEE_PASSWORD, ECOBEE_USERNAME
    from pyecobee.errors import (
        EcobeeAuthFailedError,
        EcobeeAuthMfaRequiredError,
        EcobeeError,
    )
except ImportError:
    sys.exit(
        "python-ecobee-api is not installed.\n"
        "  uv venv && uv pip install -e '.[bootstrap,dev]'\n"
        "  .venv/bin/python scripts/bootstrap.py"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="One-time interactive ecobee login.",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="Write the credentials to this file (mode 0600) instead of printing "
        "them. Preferred: keeps the token out of your terminal scrollback and "
        "clipboard. Use ./credentials.json for local runs.",
    )
    args = parser.parse_args()

    print(__doc__)

    try:
        username = input("ecobee email: ").strip()
        password = getpass.getpass("ecobee password: ")
    except (EOFError, KeyboardInterrupt):
        return _fail("Cancelled. This script needs an interactive terminal.")

    api = Ecobee(config={ECOBEE_USERNAME: username, ECOBEE_PASSWORD: password})

    try:
        try:
            api.request_tokens_web()
        except EcobeeAuthMfaRequiredError as err:
            challenge = err.args[0]
            print(f"\nMulti-factor challenge ({challenge.mfa_type}).")
            try:
                code = input("6-digit code from your authenticator app: ").strip()
            except (EOFError, KeyboardInterrupt):
                return _fail("Cancelled at the MFA prompt.")
            api.submit_mfa_code(challenge, code)
    except EcobeeAuthFailedError as err:
        return _fail(f"ecobee rejected the credentials or code: {err}")
    except EcobeeError as err:
        return _fail(
            f"Login failed: {err}\n\n"
            "If this looks like an unexpected page or redirect, ecobee may have "
            "changed its login forms. Upgrading python-ecobee-api is the fix — "
            "Home Assistant depends on the same library, so the repair usually "
            "lands upstream quickly."
        )

    if not api.refresh_token:
        return _fail("Login reported success but returned no refresh token.")

    document = {
        "refresh_token": api.refresh_token,
        "access_token": api.access_token or "",
    }

    if args.out:
        # Reuses the importer's own store so the file lands atomically at 0600
        # and in exactly the shape the importer expects to read back.
        from ecobee_importer.tokens import FileTokenStore, Tokens

        FileTokenStore(args.out).save(
            Tokens(
                refresh_token=document["refresh_token"],
                access_token=document["access_token"],
            )
        )
        print(f"\nLogin succeeded. Credentials written to {args.out} (mode 0600).")
        print("Read the refresh token out of that file to create the Secret:\n")
        # sys.executable, not a bare `python`: on macOS and many distributions
        # only `python3` is on PATH, and inside a venv neither name is
        # guaranteed. This is the interpreter that just ran, so it exists.
        read_token = (
            f"{sys.executable} -c "
            "'import json,sys;print(json.load(open(sys.argv[1]))[\"refresh_token\"])' "
            f"{args.out}"
        )
        print(
            "  kubectl create secret generic ecobee-importer-tokens \\\n"
            "    -n ecobee-runtime-importer \\\n"
            f'    --from-literal=refresh_token="$({read_token})"\n'
        )
    else:
        print("\n" + "=" * 72)
        print("Credentials — treat as a password. Do not paste into chat or tickets.")
        print("=" * 72)
        print(json.dumps(document, indent=2))
        print("=" * 72)
        print(
            "\nNext:\n"
            "  1. Store the refresh token in your password manager.\n"
            "  2. Create the Secret:\n\n"
            "     kubectl create secret generic ecobee-importer-tokens \\\n"
            "       -n ecobee-runtime-importer \\\n"
            "       --from-literal=refresh_token='<value>'\n"
        )

    print(
        "The importer rotates this token in place once deployed. After the first\n"
        "rotation the cluster's Secret is authoritative and any copy you keep is\n"
        "only a bootstrap seed — restoring that copy over it will lock the account\n"
        "out and send you back to this script.\n"
    )
    return 0


def _fail(message: str) -> int:
    print(f"\n{message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
