"""Package entrypoint: python -m gsd_bridge <command> [args]"""

import sys

from .cli import main

sys.exit(main())
