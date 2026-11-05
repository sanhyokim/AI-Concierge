import sys

from ..tenants import TenantError, current

try:
    current()   # the company settings are read first, so a mistake in them is shown plainly (docs/tenant-config.md)
except TenantError as e:
    print(f"起動できません：{e}", file=sys.stderr)
    sys.exit(2)

from .server import main  # noqa: E402

sys.exit(main())
