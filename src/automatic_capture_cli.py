"""Where AI apps registered before 0.8 start capture_cli: it now lives in the turnbook
package. turnbook-install registers the new place; this file goes in 0.9."""
import sys

from turnbook.capture_cli import main

sys.exit(main())
