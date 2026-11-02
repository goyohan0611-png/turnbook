"""Where AI apps registered before 0.8 start mcp_server: it now lives in the turnbook
package. turnbook-install registers the new place; this file goes in 0.9."""
import sys

from turnbook.mcp_server import main

sys.exit(main())
