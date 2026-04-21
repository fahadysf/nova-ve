from enum import Enum


class UserRole(str, Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    USER = "user"


class ExtAuth(str, Enum):
    INTERNAL = "internal"
    LDAP = "ldap"
    SAML = "saml"


DEFAULT_SESSION_MAX_AGE = 14400  # 4 hours in seconds
ACCESS_TOKEN_EXPIRE_MINUTES = 60  # JWT token expiry
