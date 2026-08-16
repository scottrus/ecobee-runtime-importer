#!/usr/bin/env python3
"""Interactive, one-time ecobee login.

ecobee's developer portal no longer issues API keys, so authentication goes
through the Auth0 flow Home Assistant adopted in 2026.3, using ecobee's own web
client. That flow walks hosted login forms and, if the account has TOTP
multi-factor enabled, requires a code from an authenticator app — so it needs a
human and cannot be scheduled.

Run this once. It writes a credentials file and stops there — setup lives in the
README, which is the single authoritative source for it.

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
        default="./credentials.json",
        help="Where to write the credentials, mode 0600. Default: ./credentials.json",
    )
    parser.add_argument(
        "--print",
        dest="print_",
        action="store_true",
        help="Print the credentials instead of writing them. Opt-in, because it "
        "puts a live token in your terminal scrollback and clipboard history.",
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

    if args.print_:
        print("\n" + "=" * 72)
        print("Credentials — treat as a password. Do not paste into chat or tickets.")
        print("=" * 72)
        print(json.dumps(document, indent=2))
        print("=" * 72)
    else:
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

    # Deliberately no install instructions here. This script authenticates; the
    # README owns setup end to end. Restating its steps in a second place is
    # exactly how the two drifted — the printed copy kept telling people to
    # create a Secret in a namespace that nothing had created yet.
    print("Continue with the README.\n")
    return 0


def _fail(message: str) -> int:
    print(f"\n{message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
