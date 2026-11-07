"""Outcome-first agent loop controller.

The controller talks to the formi simulator over HTTP only.  It keeps its own
SQLite audit store, learns from call events, and changes workflow configuration
through the governance boundary described in docs/SIMULATOR.md.
"""

__version__ = "1.0.0"
