"""OpenAI Images APIへの最小アダプター。"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import sleep
from typing import Any, Callable, Sequence

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI


@dataclass(frozen=True)
class ProviderAttempt:
    number: int
    result: str
    status_code: int | None = None
    request_id: str | None = None
    retry_after_seconds: float | None = None


class ProviderFailure(RuntimeError):
    """秘密値を含めない、分類済みのImages API失敗。"""

    def __init__(
        self,
        *,
        classification: str,
        outcome: str,
        status_code: int | None,
        request_id: str | None,
        attempts: Sequence[ProviderAttempt],
        retry_after_seconds: float | None = None,
    ) -> None:
        self.classification = classification
        self.outcome = outcome
        self.status_code = status_code
        self.request_id = request_id
        self.attempts = tuple(attempts)
        self.retry_after_seconds = retry_after_seconds
        super().__init__(classification)


class OpenAIImagesProvider:
    """自動再試行を止めたOpenAI Images APIクライアント。"""

    def __init__(
        self,
        api_key: str,
        *,
        client: Any | None = None,
        sleep_fn: Callable[[float], None] = sleep,
    ) -> None:
        self._client = client or OpenAI(api_key=api_key, max_retries=0)
        self._sleep = sleep_fn

    @staticmethod
    def _retry_after_seconds(error: APIStatusError) -> float | None:
        raw = error.response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def _failure_classification(error: Exception, status_code: int | None) -> tuple[str, str]:
        if isinstance(error, APITimeoutError):
            return "network_timeout", "unknown"
        if isinstance(error, APIConnectionError):
            return "network_unknown_outcome", "unknown"
        code = str(getattr(error, "code", "") or "").casefold()
        if "content" in code and ("policy" in code or "reject" in code):
            return "content_rejected", "known_failure"
        if "organization" in code and "verif" in code:
            return "organization_verification_required", "known_failure"
        if status_code == 429:
            return "rate_limited", "known_failure"
        if status_code is not None and status_code >= 500:
            return "provider_server_error", "unknown"
        if status_code in {401, 403}:
            return "authentication_failed", "known_failure"
        return "provider_request_failed", "known_failure"

    def _call(self, operation: Callable[[], Any]) -> tuple[Any, list[ProviderAttempt]]:
        attempts: list[ProviderAttempt] = []
        for number in range(1, 4):
            try:
                response = operation()
                request_id = getattr(response, "_request_id", None)
                attempts.append(ProviderAttempt(number, "succeeded", request_id=request_id))
                return response, attempts
            except APIStatusError as exc:
                status = exc.status_code
                request_id = getattr(exc, "request_id", None)
                if status != 429:
                    classification, outcome = self._failure_classification(exc, status)
                    attempts.append(ProviderAttempt(number, "failed", status, request_id))
                    raise ProviderFailure(
                        classification=classification,
                        outcome=outcome,
                        status_code=status,
                        request_id=request_id,
                        attempts=attempts,
                    ) from exc

                delay = self._retry_after_seconds(exc)
                can_retry = number < 3 and delay is not None and 0 <= delay <= 60
                attempts.append(
                    ProviderAttempt(
                        number,
                        "rate_limited_retrying" if can_retry else "rate_limited_stopped",
                        status,
                        request_id,
                        delay,
                    )
                )
                if can_retry:
                    self._sleep(delay)
                    continue
                raise ProviderFailure(
                    classification="rate_limited",
                    outcome="known_failure",
                    status_code=status,
                    request_id=request_id,
                    attempts=attempts,
                    retry_after_seconds=delay,
                ) from exc
            except (APITimeoutError, APIConnectionError) as exc:
                classification, outcome = self._failure_classification(exc, None)
                attempts.append(ProviderAttempt(number, "unknown_outcome"))
                raise ProviderFailure(
                    classification=classification,
                    outcome=outcome,
                    status_code=None,
                    request_id=None,
                    attempts=attempts,
                ) from exc
        raise AssertionError("retry loop ended unexpectedly")

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        count: int,
        size: str,
        quality: str,
        output_format: str,
    ) -> tuple[Any, list[ProviderAttempt]]:
        return self._call(
            lambda: self._client.images.generate(
                model=model,
                prompt=prompt,
                n=count,
                size=size,
                quality=quality,
                output_format=output_format,
            )
        )

    def edit(
        self,
        *,
        model: str,
        prompt: str,
        count: int,
        size: str,
        quality: str,
        output_format: str,
        image_paths: Sequence[Path],
        mask_path: Path | None = None,
    ) -> tuple[Any, list[ProviderAttempt]]:
        if not image_paths:
            raise ValueError("Images API editには入力画像が必要です。")
        with ExitStack() as stack:
            images = [stack.enter_context(path.open("rb")) for path in image_paths]
            mask = stack.enter_context(mask_path.open("rb")) if mask_path else None
            response, attempts = self._call(
                lambda: self._client.images.edit(
                    model=model,
                    prompt=prompt,
                    n=count,
                    size=size,
                    quality=quality,
                    output_format=output_format,
                    image=images,
                    **({"mask": mask} if mask is not None else {}),
                )
            )
            return response, attempts
