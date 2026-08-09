"""Resolve source publication time from auditable, source-owned evidence."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Protocol

from arctic_route_data.errors import MissingMetadataError
from arctic_route_data.models import DataCategory
from arctic_route_data.timeutils import ensure_utc, isoformat_utc, parse_utc


class IssueTimeMethod(StrEnum):
    COPERNICUS_CATALOGUE = "copernicus_catalogue"
    HTTP_LAST_MODIFIED = "http_last_modified"
    DATASET_ATTRIBUTE = "dataset_attribute"
    EXPLICIT_CATALOG = "explicit_catalog"
    CONSERVATIVE_RETRIEVAL = "conservative_retrieval"


@dataclass(frozen=True, slots=True)
class IssueTimeEvidence:
    issue_time: datetime
    method: IssueTimeMethod
    authority: str
    reference: str
    observed_at: datetime
    raw_value: str
    authoritative: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_time", ensure_utc(self.issue_time, field="issue_time"))
        object.__setattr__(
            self, "observed_at", ensure_utc(self.observed_at, field="observed_at")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_time": isoformat_utc(self.issue_time),
            "method": self.method.value,
            "authority": self.authority,
            "reference": self.reference,
            "observed_at": isoformat_utc(self.observed_at),
            "raw_value": self.raw_value,
            "authoritative": self.authoritative,
        }


@dataclass(frozen=True, slots=True)
class CapturedHttpExchange:
    method: str
    request_url: str
    response_url: str
    request_params: Mapping[str, str]
    response_headers: Mapping[str, str]
    observed_at: datetime
    status_code: int | None = None

    def last_modified(self) -> datetime | None:
        value = next(
            (
                header_value
                for header_name, header_value in self.response_headers.items()
                if header_name.casefold() == "last-modified"
            ),
            None,
        )
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class IssueTimeContext:
    source_family: str
    data_type: str
    category: DataCategory
    source_label: str
    valid_times: tuple[datetime, ...]
    observed_at: datetime
    dataset_attributes: Mapping[str, Any]
    http_exchanges: tuple[CapturedHttpExchange, ...] = ()
    product_id: str | None = None
    dataset_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "valid_times",
            tuple(ensure_utc(value, field="valid_time") for value in self.valid_times),
        )
        object.__setattr__(
            self, "observed_at", ensure_utc(self.observed_at, field="observed_at")
        )


class IssueTimeResolver(Protocol):
    def resolve(self, context: IssueTimeContext) -> IssueTimeEvidence: ...


class IssueTimeResolutionError(MissingMetadataError):
    """No authoritative publication timestamp could be established."""


def parse_source_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    text = str(value).strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return datetime.combine(date.fromisoformat(text), time.min, tzinfo=UTC)
    try:
        return parse_utc(text, field="source issue_time")
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError) as exc:
            raise IssueTimeResolutionError(f"无法解析源站时间: {value!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)


def _plausible(value: datetime, context: IssueTimeContext) -> bool:
    value = ensure_utc(value)
    if value > context.observed_at + timedelta(minutes=10):
        return False
    if context.category is DataCategory.STATIC or not context.valid_times:
        return True
    earliest = min(context.valid_times)
    latest = max(context.valid_times)
    # Dynamic products may be published before a forecast, or after an observation.
    return earliest - timedelta(days=45) <= value <= latest + timedelta(days=45)


class ExplicitIssueTimeResolver:
    def __init__(self, evidence: IssueTimeEvidence) -> None:
        self.evidence = evidence

    def resolve(self, context: IssueTimeContext) -> IssueTimeEvidence:
        if not _plausible(self.evidence.issue_time, context):
            raise IssueTimeResolutionError("显式 issue_time 与数据时次或获取时刻明显矛盾")
        return self.evidence


class HttpLastModifiedResolver:
    def resolve(self, context: IssueTimeContext) -> IssueTimeEvidence:
        candidates: list[tuple[int, datetime, CapturedHttpExchange, str]] = []
        tokens = {
            token
            for valid_time in context.valid_times
            for token in (
                valid_time.strftime("%Y%m%d"),
                valid_time.strftime("%Y-%m-%d"),
                valid_time.strftime("%Y%m%d%H"),
            )
        }
        for exchange in context.http_exchanges:
            if exchange.status_code is not None and not 200 <= exchange.status_code < 300:
                continue
            value = exchange.last_modified()
            if value is None or not _plausible(value, context):
                continue
            request_text = " ".join(
                (
                    exchange.request_url,
                    exchange.response_url,
                    *[f"{key}={item}" for key, item in exchange.request_params.items()],
                )
            )
            score = sum(token in request_text for token in tokens)
            if any(suffix in request_text.casefold() for suffix in (".nc", ".grib", ".grib2")):
                score += 2
            raw_value = next(
                value
                for key, value in exchange.response_headers.items()
                if key.casefold() == "last-modified"
            )
            candidates.append((score, value, exchange, raw_value))
        if not candidates:
            raise IssueTimeResolutionError("HTTP 响应中没有可信的 Last-Modified")
        _, issue_time, exchange, raw_value = max(candidates, key=lambda item: (item[0], item[1]))
        return IssueTimeEvidence(
            issue_time=issue_time,
            method=IssueTimeMethod.HTTP_LAST_MODIFIED,
            authority=context.source_label,
            reference=exchange.response_url or exchange.request_url,
            observed_at=exchange.observed_at,
            raw_value=raw_value,
        )


class DatasetAttributeIssueTimeResolver:
    ATTRIBUTE_NAMES = (
        "issue_time",
        "publication_time",
        "date_issued",
        "date_modified",
        "date_created",
        "creation_date",
        "bulletin_date",
    )

    def resolve(self, context: IssueTimeContext) -> IssueTimeEvidence:
        normalized = {
            str(key).casefold(): value for key, value in context.dataset_attributes.items()
        }
        for name in self.ATTRIBUTE_NAMES:
            if name.casefold() not in normalized:
                continue
            raw_value = normalized[name.casefold()]
            try:
                issue_time = parse_source_datetime(raw_value)
            except IssueTimeResolutionError:
                continue
            if not _plausible(issue_time, context):
                continue
            return IssueTimeEvidence(
                issue_time=issue_time,
                method=IssueTimeMethod.DATASET_ATTRIBUTE,
                authority=context.source_label,
                reference=f"NetCDF global attribute: {name}",
                observed_at=context.observed_at,
                raw_value=str(raw_value),
            )
        raise IssueTimeResolutionError("数据属性中没有可信的发布时间字段")


DescribeCallable = Callable[..., Any]


class CopernicusCatalogueIssueTimeResolver:
    """Use official ``copernicusmarine.describe`` dataset catalogue metadata."""

    CATALOG_FIELDS = (
        "arco_updated_date",
        "updated_date",
        "released_date",
    )

    def __init__(self, describe: DescribeCallable | None = None) -> None:
        self._describe = describe

    def resolve(self, context: IssueTimeContext) -> IssueTimeEvidence:
        if not context.dataset_id:
            raise IssueTimeResolutionError("Copernicus 数据缺少 dataset_id")
        describe = self._describe
        if describe is None:
            try:
                import copernicusmarine
            except ImportError as exc:
                raise IssueTimeResolutionError(
                    "需要安装 acquisition extra 才能查询 Copernicus catalogue"
                ) from exc
            describe = copernicusmarine.describe
        catalogue = describe(dataset_id=context.dataset_id, disable_progress_bar=True)
        payload = (
            catalogue.model_dump(mode="json")
            if hasattr(catalogue, "model_dump")
            else catalogue
        )
        candidates = list(_find_catalogue_dates(payload, self.CATALOG_FIELDS))
        plausible = [item for item in candidates if _plausible(item[1], context)]
        if not plausible:
            raise IssueTimeResolutionError("Copernicus catalogue 没有与数据时次匹配的更新时间")
        field_name, issue_time, raw_value = max(plausible, key=lambda item: item[1])
        return IssueTimeEvidence(
            issue_time=issue_time,
            method=IssueTimeMethod.COPERNICUS_CATALOGUE,
            authority="Copernicus Marine Data Store",
            reference=(
                "copernicusmarine.describe"
                f"(dataset_id={context.dataset_id!r}) field={field_name}"
            ),
            observed_at=context.observed_at,
            raw_value=raw_value,
        )


def _find_catalogue_dates(
    value: Any, names: Sequence[str], path: str = "$"
) -> Iterable[tuple[str, datetime, str]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if str(key).casefold() in {name.casefold() for name in names} and child:
                with suppress(IssueTimeResolutionError):
                    yield child_path, parse_source_datetime(child), str(child)
            yield from _find_catalogue_dates(child, names, child_path)
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            yield from _find_catalogue_dates(child, names, f"{path}[{index}]")


class ConservativeRetrievalIssueTimeResolver:
    def resolve(self, context: IssueTimeContext) -> IssueTimeEvidence:
        return IssueTimeEvidence(
            issue_time=context.observed_at,
            method=IssueTimeMethod.CONSERVATIVE_RETRIEVAL,
            authority=context.source_label,
            reference="successful retrieval time (safe upper bound, not publication time)",
            observed_at=context.observed_at,
            raw_value=isoformat_utc(context.observed_at),
            authoritative=False,
        )


class CompositeIssueTimeResolver:
    def __init__(self, resolvers: Sequence[IssueTimeResolver]) -> None:
        self.resolvers = tuple(resolvers)

    def resolve(self, context: IssueTimeContext) -> IssueTimeEvidence:
        errors: list[str] = []
        for resolver in self.resolvers:
            try:
                return resolver.resolve(context)
            except IssueTimeResolutionError as exc:
                errors.append(f"{type(resolver).__name__}: {exc}")
        raise IssueTimeResolutionError("无法确定权威 issue_time；" + " | ".join(errors))


class SourceIssueTimeResolver:
    """Resolver routing shared by all 13 legacy downloaders."""

    def __init__(
        self,
        *,
        copernicus_describe: DescribeCallable | None = None,
        allow_conservative_retrieval: bool = False,
    ) -> None:
        self.copernicus_describe = copernicus_describe
        self.allow_conservative_retrieval = allow_conservative_retrieval

    def resolve(self, context: IssueTimeContext) -> IssueTimeEvidence:
        resolvers: list[IssueTimeResolver] = []
        if context.source_family == "copernicus_marine":
            resolvers.append(CopernicusCatalogueIssueTimeResolver(self.copernicus_describe))
        resolvers.extend((HttpLastModifiedResolver(), DatasetAttributeIssueTimeResolver()))
        if self.allow_conservative_retrieval:
            resolvers.append(ConservativeRetrievalIssueTimeResolver())
        return CompositeIssueTimeResolver(resolvers).resolve(context)
