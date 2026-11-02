"""The package's name before 0.11: apps, hooks and Startup entries written by older installers run
`python -m graph_mind.mcp_server`, `graph_mind.hook`, `graph_mind.capture_cli`... Every
`graph_mind.X` is `turnbook.X`, the same module object, so those keep working until
`turnbook-install` rewrites them."""
import importlib
import importlib.abc
import importlib.util
import sys


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if not name.startswith(__name__ + "."):
            return None
        real = importlib.util.find_spec("turnbook" + name[len(__name__):])
        if real is None:
            return None
        if sys.argv and sys.argv[0] == "-m" and real.origin:
            # `python -m graph_mind.X`: turnbook/X.py run as __main__ (its loader is named
            # turnbook.X and refuses another name, so a loader for the file under this one)
            return importlib.util.spec_from_file_location(name, real.origin)
        return importlib.util.spec_from_loader(name, self)

    def create_module(self, spec):
        return importlib.import_module("turnbook" + spec.name[len(__name__):])

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _Alias())
