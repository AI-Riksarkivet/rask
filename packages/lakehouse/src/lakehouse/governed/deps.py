"""The subject a governed door names when authentication is off.

Each service's own security module builds its doors on `lakehouse.governed.door_caller`; this is the one name they
share for a caller nobody authenticated.
"""

#: The subject used when OIDC is disabled. Named rather than inlined so a grep for who can act
#: without a token returns exactly one place.
ANONYMOUS_SUBJECT = "anon"
