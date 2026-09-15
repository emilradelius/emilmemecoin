"""Classifying news with Claude.

This is the one place an LLM genuinely earns its place in a trading system.
Judging whether a headline carries new, material, directional information is a
language problem: keyword rules cannot tell "Volvo beats estimates" from
"Volvo expected to beat estimates" from "Why Volvo's beat doesn't matter", and
those three imply completely different actions.

What the model is asked for is deliberately narrow. It scores the *story* -
materiality, novelty, direction, surprise. It is never asked whether to buy.
Position sizing, risk and timing stay in code, where they can be tested and
where they behave identically every run. An LLM asked "should I buy this?"
gives a plausible answer every time, including when the honest answer is that
nothing here is tradeable.

Cost control is structural, not incidental: dedup and entity matching run
first, so only stories that survive both reach the model. A
:class:`ClassifierBudget` caps monthly spend and degrades to skipping rather
than overspending.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import Direction, EventType, NewsAssessment, NewsItem

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"

# Anthropic list prices, USD per million tokens. Update from the pricing page.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

SYSTEM_PROMPT = """\
You assess financial news for a systematic trading system. You score stories; \
you never recommend trades.

For each story, judge:

MATERIALITY (0-1): could this move the share price on its own? An earnings \
surprise or a takeover bid is material. A product mention, a conference \
appearance, or a journalist's opinion is not.

NOVELTY (0-1): is this new information, or a restatement of something the \
market already knows? Most financial news is recap: previews of scheduled \
events, summaries of yesterday's move, "here's why the stock fell". Score \
these low even when the underlying event was important. A story that explains \
a move that has already happened has near-zero novelty.

DIRECTION: bullish, bearish, or neutral for the named company. Neutral is the \
correct answer far more often than not - use it freely.

SURPRISE (-1 to 1): for earnings and guidance only, how far from expectation. \
A beat that was widely anticipated is a small positive, not a large one. Use \
0 when no expectation is referenced.

CONFIDENCE (0-1): how sure you are of the above, given only what the headline \
and summary actually say. Headlines are often ambiguous or clickbait; say so \
with a low score rather than guessing.

Be strict. In a typical feed the large majority of stories are NOISE with no \
tradeable content, and a classifier that finds signal everywhere is worse \
than useless - it will generate constant, confident, wrong trades. When in \
doubt, score low."""

CLASSIFY_TOOL = {
    "name": "record_assessment",
    "description": "Record the structured assessment of one news story.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "event_type": {
                "type": "string",
                "enum": [e.value for e in EventType],
                "description": "Use 'noise' for recaps, previews and opinion.",
            },
            "direction": {
                "type": "string",
                "enum": [d.value for d in Direction],
            },
            "materiality": {"type": "number", "minimum": 0, "maximum": 1},
            "novelty": {"type": "number", "minimum": 0, "maximum": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "surprise": {"type": "number", "minimum": -1, "maximum": 1},
            "rationale": {
                "type": "string",
                "description": "One short sentence. Why these scores.",
            },
        },
        "required": [
            "event_type", "direction", "materiality", "novelty",
            "confidence", "surprise", "rationale",
        ],
    },
}


@dataclass
class ClassifierBudget:
    """Monthly spend cap for classification calls.

    Persisted to disk. An in-memory-only cap resets every time the process
    restarts, which for a bot that restarts on deploys, crashes and reboots
    means the monthly ceiling is never actually reached and the cap does
    nothing. Give it a ``state_path`` in production.
    """

    monthly_usd_cap: float = 10.0
    spent_usd: float = 0.0
    calls: int = 0
    period: str = ""
    state_path: Path | None = None

    def __post_init__(self) -> None:
        self._load()

    @staticmethod
    def _current_period() -> str:
        now = datetime.now(timezone.utc)
        return f"{now.year}-{now.month:02d}"

    def _load(self) -> None:
        if self.state_path is None or not Path(self.state_path).exists():
            return
        try:
            data = json.loads(Path(self.state_path).read_text())
        except (OSError, ValueError):
            log.warning("could not read classifier budget state; starting fresh")
            return
        if data.get("period") == self._current_period():
            self.period = data["period"]
            self.spent_usd = float(data.get("spent_usd", 0.0))
            self.calls = int(data.get("calls", 0))

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            path = Path(self.state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "period": self.period, "spent_usd": self.spent_usd,
                "calls": self.calls,
            }))
        except OSError as exc:
            log.warning("could not persist classifier budget: %s", exc)

    def _roll(self) -> None:
        current = self._current_period()
        if self.period != current:
            self.period, self.spent_usd, self.calls = current, 0.0, 0
            self._save()

    def can_spend(self) -> bool:
        self._roll()
        return self.spent_usd < self.monthly_usd_cap

    def record(self, usd: float) -> None:
        self._roll()
        self.spent_usd += usd
        self.calls += 1
        self._save()

    def status(self) -> str:
        self._roll()
        return (f"${self.spent_usd:.3f} / ${self.monthly_usd_cap:.2f} this month, "
                f"{self.calls} classifications")


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cached_tokens: int = 0) -> float:
    in_price, out_price = PRICING.get(model, PRICING[DEFAULT_MODEL])
    # Cache reads bill at roughly a tenth of the input rate.
    billed_input = max(0, input_tokens - cached_tokens)
    return (
        billed_input * in_price / 1_000_000
        + cached_tokens * in_price * 0.1 / 1_000_000
        + output_tokens * out_price / 1_000_000
    )


class NewsClassifier:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        monthly_usd_cap: float = 10.0,
        effort: str = "low",
    ) -> None:
        self.model = model
        # Classification is a high-volume, comparatively simple route, so it
        # runs at low effort. Raise it if you find the scores unreliable -
        # measure before assuming.
        self.effort = effort
        self.budget = ClassifierBudget(monthly_usd_cap=monthly_usd_cap)
        self._client = None
        self._api_key = api_key

    def _get_client(self):
        if self._client is None:
            import anthropic
            # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
            # `ant auth login` profile when api_key is None.
            self._client = (
                anthropic.Anthropic(api_key=self._api_key)
                if self._api_key else anthropic.Anthropic()
            )
        return self._client

    def classify(self, item: NewsItem, *, symbol: str | None = None) -> NewsAssessment:
        """Score one story. Returns a zeroed assessment on any failure.

        Failing to a zero score means the pipeline treats an unclassifiable
        story as untradeable, which is the safe direction: an API outage
        should stop trading, not start it.
        """
        if not self.budget.can_spend():
            log.warning("classifier budget exhausted: %s", self.budget.status())
            return NewsAssessment(rationale="budget exhausted, not classified")

        prompt = self._build_prompt(item, symbol)
        try:
            import anthropic
            client = self._get_client()
            response = client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # The rubric is byte-stable across every call, so it caches.
                    "cache_control": {"type": "ephemeral"},
                }],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                tools=[CLASSIFY_TOOL],
                tool_choice={"type": "tool", "name": "record_assessment"},
                messages=[{"role": "user", "content": prompt}],
            )
        except ImportError:
            log.error("the 'anthropic' package is not installed")
            return NewsAssessment(rationale="anthropic SDK missing")
        except TypeError as exc:
            # The SDK raises TypeError at request time when it cannot resolve
            # any credential. Degrade rather than crash the whole poll loop.
            log.error(
                "no Claude credentials found (%s). Set ANTHROPIC_API_KEY or "
                "run `ant auth login`.", exc,
            )
            return NewsAssessment(rationale="no credentials")
        except anthropic.AuthenticationError:
            log.error("Claude API rejected the credentials")
            return NewsAssessment(rationale="authentication failed")
        except anthropic.RateLimitError:
            log.warning("rate limited by the Claude API; skipping this item")
            return NewsAssessment(rationale="rate limited")
        except anthropic.APIStatusError as exc:
            log.error("Claude API error %s: %s", exc.status_code, exc.message)
            return NewsAssessment(rationale=f"api error {exc.status_code}")
        except anthropic.APIConnectionError:
            log.error("could not reach the Claude API")
            return NewsAssessment(rationale="connection error")

        usage = response.usage
        cost = estimate_cost(
            self.model, usage.input_tokens, usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
        self.budget.record(cost)

        assessment = self._parse(response, symbol)
        assessment.model = self.model
        assessment.cost_usd = round(cost, 6)
        return assessment

    @staticmethod
    def _build_prompt(item: NewsItem, symbol: str | None) -> str:
        parts = [f"HEADLINE: {item.title}"]
        if item.summary:
            parts.append(f"SUMMARY: {item.summary[:1500]}")
        parts.append(f"SOURCE: {item.source}")
        if item.published_at:
            parts.append(f"PUBLISHED: {item.published_at.isoformat()}")
        if symbol:
            parts.append(f"COMPANY UNDER ASSESSMENT: {symbol}")
        parts.append("\nAssess this story using the record_assessment tool.")
        return "\n".join(parts)

    def _parse(self, response, symbol: str | None) -> NewsAssessment:
        for block in response.content:
            if block.type != "tool_use" or block.name != "record_assessment":
                continue
            # Tool inputs are parsed JSON objects already; never string-match
            # against the serialized form.
            data = block.input
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    continue
            try:
                return NewsAssessment(
                    event_type=EventType(data.get("event_type", "other")),
                    direction=Direction(data.get("direction", "neutral")),
                    materiality=float(data.get("materiality", 0.0)),
                    novelty=float(data.get("novelty", 0.0)),
                    confidence=float(data.get("confidence", 0.0)),
                    surprise=float(data.get("surprise", 0.0)),
                    rationale=str(data.get("rationale", ""))[:500],
                    primary_symbol=symbol,
                )
            except (ValueError, TypeError) as exc:
                log.warning("could not read assessment payload: %s", exc)

        if response.stop_reason == "refusal":
            log.warning("classification refused: %s", response.stop_details)
            return NewsAssessment(rationale="refused by safety classifier")
        return NewsAssessment(rationale="no assessment returned")
