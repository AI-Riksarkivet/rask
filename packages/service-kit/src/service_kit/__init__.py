"""The rask platform library: exceptions, middleware, probes, the auth plane, the naming grammar and the contract the
lakehouse's events publish.

It is what a client of the lakehouse may pin. The lakehouse's own kernel is ``lakehouse_kit``, which imports this
package and which this package never imports. The package root exports nothing: every name is imported from the
submodule that defines it, so importing one piece costs that piece.
"""
