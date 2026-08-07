"""Seam for the server.py @method handler split (mechanical move).

server.py's ~130 JSON-RPC handlers close over its module globals
(``_sessions``, ``_ok``, ``_err``, config helpers, ...).  To move them
out of the 19K-line module without rewriting a single handler body,
each ``methods_*`` module defines its handlers under a local
:class:`HandlerRegistry` and server.py calls :meth:`HandlerRegistry.install`
at the end of its own import, once every global the handlers close over
exists.  ``install()`` rebinds each handler's ``__globals__`` to
server.py's namespace with ``types.FunctionType``, so handler bodies
stay byte-identical and ``global X`` statements inside handlers keep
mutating server.py state exactly as before the split.

No import cycle: ``methods_*`` modules never import server at module
level — server imports them and passes itself to ``register()``.
"""

import types


class HandlerRegistry:
    """Deferred @method registrar used by the methods_* split modules."""

    def __init__(self) -> None:
        self._pending: list[tuple[str, types.FunctionType]] = []
        self._helpers: list[types.FunctionType] = []

    def method(self, name: str):
        """Drop-in for server.py's ``@method`` decorator (defers registration)."""

        def dec(fn):
            self._pending.append((name, fn))
            return fn

        return dec

    def profile_scoped(self, fn):
        """Drop-in for server.py's ``@_profile_scoped`` (applied at install)."""
        fn._hermes_profile_scoped = True
        return fn

    def helper(self, fn):
        """Register a module-level helper for the same globals rebinding.

        For non-handler functions a ``@method`` handler calls by NAME (e.g. the
        ``session.resume`` ownership split: the thin registered wrapper calls
        ``_session_resume``). The handler executes against server.py's
        namespace after ``install()``, so the callee must exist THERE with
        server-rebound globals too — a plain module function in a ``methods_*``
        module would resolve its own body's names against that module's (empty)
        namespace and NameError at first use.
        """
        self._helpers.append(fn)
        return fn

    def install(self, server) -> None:
        """Rebind pending handlers onto ``server``'s globals and register them."""
        g = vars(server)
        for fn in self._helpers:
            real = types.FunctionType(
                fn.__code__, g, fn.__name__, fn.__defaults__, fn.__closure__
            )
            real.__kwdefaults__ = fn.__kwdefaults__
            real.__doc__ = fn.__doc__
            real.__dict__.update(fn.__dict__)
            g[fn.__name__] = real
        for name, fn in self._pending:
            real = types.FunctionType(
                fn.__code__, g, fn.__name__, fn.__defaults__, fn.__closure__
            )
            real.__kwdefaults__ = fn.__kwdefaults__
            real.__doc__ = fn.__doc__
            real.__dict__.update(fn.__dict__)
            if getattr(fn, "_hermes_profile_scoped", False):
                real = server._profile_scoped(real)
            server._methods[name] = real
