#!/usr/bin/env python3
"""Error codes and exception classes for pounding-ozon-hybrid."""

# Cloud/webhook error codes
ERR_CLOUD_UNAVAILABLE = "CLOUD_UNAVAILABLE"
ERR_CLOUD_REJECTED = "CLOUD_REJECTED"
ERR_CLOUD_TIMEOUT = "CLOUD_TIMEOUT"
ERR_CLOUD_FAILED = "CLOUD_FAILED"

# Config error codes
ERR_MISSING_CONFIG = "MISSING_CONFIG"


class SkillError(Exception):
    def __init__(self, message: str, code: str = "SKILL_ERROR"):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class ConfigError(SkillError):
    def __init__(self, message: str):
        super().__init__(message, "CONFIG_ERROR")


class ValidationError(SkillError):
    def __init__(self, message: str):
        super().__init__(message, "VALIDATION_ERROR")
