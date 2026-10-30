"""Exception hierarchy.

Every failure mode the pipeline can hit has its own type so that callers can
decide independently whether to retry, alert, or abort.  ``transient`` marks the
errors that are safe to retry; authentication/CAPTCHA/MFA failures are never
transient and must never be retried automatically.
"""

from __future__ import annotations


class AdFlowError(Exception):
    """Base class for every failure."""

    #: Whether retrying the same operation could plausibly succeed.
    transient: bool = False
    #: Process exit code used when this error reaches the top level.
    exit_code: int = 1


class ConfigurationError(AdFlowError):
    """Missing or invalid environment configuration."""

    exit_code = 2


class AuthenticationError(AdFlowError):
    """Amazon session is missing, expired, or blocked by a security challenge.

    Never retried: repeated automated attempts against sign-in, CAPTCHA, OTP or
    MFA are exactly what Amazon's protections exist to stop.  A human must
    re-establish the session with ``python run.py login``.
    """

    exit_code = 3


class NavigationError(AdFlowError):
    """The Products page did not reach the expected state."""

    transient = True
    exit_code = 4


class PageSetupError(AdFlowError):
    """The Products page is reachable but not set up as the export needs.

    For example the required filter (``REQUIRED_FILTER_TEXT``) is not applied.
    Not transient: a person has to fix the page once in a normal browser.
    """

    exit_code = 5


class DownloadError(AdFlowError):
    """An export did not start, did not finish, or produced no file."""

    transient = True
    exit_code = 6


class CSVValidationError(AdFlowError):
    """An exported file is not a usable Products export."""

    exit_code = 7


class DataTransformationError(AdFlowError):
    """Exported values could not be converted to the table's column types."""

    exit_code = 8


class DatabaseError(AdFlowError):
    """MySQL connectivity, schema or transaction failure.

    ``transient`` is set per instance by :mod:`app.database`: a dropped
    connection or a lost lock race is retried, a schema or data error is not.
    """

    exit_code = 9


class LockError(AdFlowError):
    """Another run already holds the execution lock."""

    exit_code = 11


class DateMismatchError(AdFlowError):
    """The page shows a different date range than the one the run expects."""

    exit_code = 14


class IncompleteExportError(AdFlowError):
    """The merged page exports do not add up to the page's "of N results".

    Transient: a page that had not finished loading is the usual cause, and
    the next attempt starts again in a fresh browser.
    """

    transient = True
    exit_code = 15
