#!/usr/bin/env python
"""Print the ``PLATFORM_ADMIN_ACCOUNTS`` value implied by ``PLATFORM_ADMIN_WALLETS``.

Migration aid for issue #1648, which moved the platform-admin gate off "the
wallet on this request" and onto canonical account identity. ``PLATFORM_ADMIN_WALLETS``
keeps working unchanged as *evidence* (an account is admin if any of ITS linked
wallets is on that list), so nothing has to be migrated to keep the current
admins working. Running this is how you take the next step: pin admin to the
account itself, so it no longer depends on a wallet link surviving, and so the
gate answers without reading the account store at all — the break-glass path
during a database incident.

Read-only. Touches no rows, prints one env line plus the wallets that resolve
to no account (the ones that would silently stop granting admin if the wallet
env were later dropped).

Usage — against whatever ``DATABASE_URL`` is configured:

    "$ENV_BIN/python" backend/scripts/derive_platform_admin_accounts.py

    # or against prod, with the real values, from an ECS task shell:
    PLATFORM_ADMIN_WALLETS="0xabc… 0xdef…" \\
      python backend/scripts/derive_platform_admin_accounts.py

Then set ``PLATFORM_ADMIN_ACCOUNTS`` to the printed value
(``TF_VAR_platform_admin_accounts`` for the Fargate task definition — see
``infra/variables.tf``, and note the same "re-pass it on every apply or it
silently empties" gotcha the wallet variable carries).
"""

from __future__ import annotations

import sys


def main() -> int:
    from archimedes.services.platform_admin import derive_admin_accounts_from_wallets

    report = derive_admin_accounts_from_wallets()

    if not report["admin_wallets"]:
        print("PLATFORM_ADMIN_WALLETS is empty — nothing to derive.", file=sys.stderr)
        print(report["env_line"])
        return 0

    print(f"# {len(report['admin_wallets'])} admin wallet(s) configured", file=sys.stderr)
    for entry in report["resolved"]:
        print(f"#   {entry['wallet']} -> {entry['user_id']}  ({entry['email']})", file=sys.stderr)
    for wallet in report["unlinked_wallets"]:
        print(
            f"#   {wallet} -> NO ACCOUNT has this wallet linked. It grants admin to nobody "
            f"today, and will keep granting admin to nobody after the cutover.",
            file=sys.stderr,
        )
    print(report["env_line"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
