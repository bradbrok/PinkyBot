"""HTTP middleware modules for the pinky daemon.

Currently houses :mod:`pinky_daemon.middleware.security_headers` (#437).
Future cross-cutting middleware introduced by the #433 hardening track
(rate limiting #438, CSRF #443, elevated-session enforcement #439) should
live here too.
"""
