import sys

from deliverius.cli.console import use_utf8_output
from deliverius.cli.datagen import main

use_utf8_output()
sys.exit(main())
