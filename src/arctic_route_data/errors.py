"""Domain errors with stable machine-readable codes."""


class WorkPackageAError(Exception):
    code = "A000"


class MetadataValidationError(WorkPackageAError, ValueError):
    code = "A101"


class MissingMetadataError(MetadataValidationError):
    code = "A102"


class ManifestConflictError(MetadataValidationError):
    code = "A103"


class DataValidationError(WorkPackageAError, ValueError):
    code = "A201"


class ChecksumMismatchError(DataValidationError):
    code = "A202"


class FutureInformationError(WorkPackageAError):
    code = "A301"


class StaleGenerationError(WorkPackageAError):
    code = "A302"


class CacheCapacityError(WorkPackageAError):
    code = "A303"


class DataNotFoundError(WorkPackageAError, LookupError):
    code = "A404"
